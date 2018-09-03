# Authors: Ade Lee <alee@redhat.com>
#
# Copyright (C) 2014  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import absolute_import

import base64
import logging
import time

import ldap
import os
import shutil
import traceback
import dbus

from configparser import DEFAULTSECT, ConfigParser, RawConfigParser

import six

from pki.client import PKIConnection
import pki.system

from ipalib import api, errors, x509
from ipalib.install import certmonger
from ipalib.constants import CA_DBUS_TIMEOUT, IPA_CA_RECORD
from ipaplatform import services
from ipaplatform.constants import constants
from ipaplatform.paths import paths
from ipapython import directivesetter
from ipapython import ipaldap
from ipapython import ipautil
from ipapython.dn import DN
from ipaserver.install import service
from ipaserver.install import sysupgrade
from ipaserver.install import replication
from ipaserver.install.installutils import stopped_service



logger = logging.getLogger(__name__)


def get_security_domain():
    """
    Get the security domain from the REST interface on the local Dogtag CA
    This function will succeed if the local dogtag CA is up.
    """
    connection = PKIConnection(
        protocol='https',
        hostname=api.env.ca_host,
        port='8443'
    )
    domain_client = pki.system.SecurityDomainClient(connection)
    info = domain_client.get_security_domain_info()
    return info


def is_installing_replica(sys_type):
    """
    We expect only one of each type of Dogtag subsystem in an IPA deployment.
    That means that if a subsystem of the specified type has already been
    deployed - and therefore appears in the security domain - then we must be
    installing a replica.
    """
    info = get_security_domain()
    try:
        sys_list = info.systems[sys_type]
        return len(sys_list.hosts) > 0
    except KeyError:
        return False


class DogtagInstance(service.Service):
    """
    This is the base class for a Dogtag 10+ instance, which uses a
    shared tomcat instance and DS to host the relevant subsystems.

    It contains functions that will be common to installations of the
    CA, KRA, and eventually TKS and TPS.
    """

    tracking_reqs = None
    server_cert_name = None

    ipaca_groups = DN(('ou', 'groups'), ('o', 'ipaca'))
    ipaca_people = DN(('ou', 'people'), ('o', 'ipaca'))
    groups_aci = (
        b'(targetfilter="(objectClass=groupOfUniqueNames)")'
        b'(targetattr="cn || description || objectclass || uniquemember")'
        b'(version 3.0; acl "Allow users from o=ipaca to read groups"; '
        b'allow (read, search, compare) '
        b'userdn="ldap:///uid=*,ou=people,o=ipaca";)'
    )

    def __init__(self, realm, subsystem, service_desc, host_name=None,
                 nss_db=paths.PKI_TOMCAT_ALIAS_DIR, service_prefix=None,
                 config=None):
        """Initializer"""

        super(DogtagInstance, self).__init__(
            'pki-tomcatd',
            service_desc=service_desc,
            realm_name=realm,
            service_user=constants.PKI_USER,
            service_prefix=service_prefix
        )

        self.admin_password = None
        self.fqdn = host_name
        self.pkcs12_info = None
        self.clone = False

        self.basedn = None
        self.admin_user = "admin"
        self.admin_dn = DN(
            ('uid', self.admin_user), self.ipaca_people
        )
        self.admin_groups = None
        self.tmp_agent_db = None
        self.subsystem = subsystem
        # replication parameters
        self.master_host = None
        self.master_replication_port = 389
        self.subject_base = None
        self.nss_db = nss_db
        self.config = config  # Path to CS.cfg

        # filled out by configure_instance
        self.pki_config_override = None
        self.ca_subject = None
        self.subject_base = None

    def is_installed(self):
        """
        Determine if subsystem instance has been installed.

        Returns True/False
        """
        return os.path.exists(os.path.join(
            paths.VAR_LIB_PKI_TOMCAT_DIR, self.subsystem.lower()))

    def spawn_instance(self, cfg_file, nolog_list=()):
        """
        Create and configure a new Dogtag instance using pkispawn.
        Passes in a configuration file with IPA-specific
        parameters.
        """
        subsystem = self.subsystem
        args = [paths.PKISPAWN,
                "-s", subsystem,
                "-f", cfg_file]

        with open(cfg_file) as f:
            logger.debug(
                'Contents of pkispawn configuration file (%s):\n%s',
                cfg_file, ipautil.nolog_replace(f.read(), nolog_list))

        try:
            ipautil.run(args, nolog=nolog_list)
        except ipautil.CalledProcessError as e:
            self.handle_setup_error(e)

    def clean_pkispawn_files(self):
        if self.tmp_agent_db is not None:
            logger.debug("Removing %s", self.tmp_agent_db)
            shutil.rmtree(self.tmp_agent_db, ignore_errors=True)

        client_dir = os.path.join(
            '/root/.dogtag/pki-tomcat/', self.subsystem.lower())
        logger.debug("Removing %s", client_dir)
        shutil.rmtree(client_dir, ignore_errors=True)

    def restart_instance(self):
        self.restart('pki-tomcat')

    def start_instance(self):
        self.start('pki-tomcat')

    def stop_instance(self):
        try:
            self.stop('pki-tomcat')
        except Exception:
            logger.debug("%s", traceback.format_exc())
            logger.critical(
                "Failed to stop the Dogtag instance."
                "See the installation log for details.")

    def enable_client_auth_to_db(self):
        """
        Enable client auth connection to the internal db.
        """
        with stopped_service('pki-tomcatd', 'pki-tomcat'):
            directivesetter.set_directive(
                self.config,
                'authz.instance.DirAclAuthz.ldap.ldapauth.authtype',
                'SslClientAuth', quotes=False, separator='=')
            directivesetter.set_directive(
                self.config,
                'authz.instance.DirAclAuthz.ldap.ldapauth.clientCertNickname',
                'subsystemCert cert-pki-ca', quotes=False, separator='=')
            directivesetter.set_directive(
                self.config,
                'authz.instance.DirAclAuthz.ldap.ldapconn.port', '636',
                quotes=False, separator='=')
            directivesetter.set_directive(
                self.config,
                'authz.instance.DirAclAuthz.ldap.ldapconn.secureConn',
                'true', quotes=False, separator='=')

            directivesetter.set_directive(
                self.config,
                'internaldb.ldapauth.authtype',
                'SslClientAuth', quotes=False, separator='=')

            directivesetter.set_directive(
                self.config,
                'internaldb.ldapauth.clientCertNickname',
                'subsystemCert cert-pki-ca', quotes=False, separator='=')
            directivesetter.set_directive(
                self.config,
                'internaldb.ldapconn.port', '636', quotes=False, separator='=')
            directivesetter.set_directive(
                self.config,
                'internaldb.ldapconn.secureConn', 'true', quotes=False,
                separator='=')
            # Remove internaldb password as is not needed anymore
            directivesetter.set_directive(paths.PKI_TOMCAT_PASSWORD_CONF,
                                       'internaldb', None, separator='=')

    def uninstall(self):
        if self.is_installed():
            self.print_msg("Unconfiguring %s" % self.subsystem)

        try:
            ipautil.run([paths.PKIDESTROY,
                         "-i", 'pki-tomcat',
                         "-s", self.subsystem])
        except ipautil.CalledProcessError as e:
            logger.critical("failed to uninstall %s instance %s",
                            self.subsystem, e)

    def http_proxy(self):
        """ Update the http proxy file  """
        template_filename = (
            os.path.join(paths.USR_SHARE_IPA_DIR,
                         "ipa-pki-proxy.conf.template"))
        sub_dict = dict(
            DOGTAG_PORT=8009,
            CLONE='' if self.clone else '#',
            FQDN=self.fqdn,
        )
        template = ipautil.template_file(template_filename, sub_dict)
        with open(paths.HTTPD_IPA_PKI_PROXY_CONF, "w") as fd:
            fd.write(template)

    def configure_certmonger_renewal(self):
        """
        Create a new CA type for certmonger that will retrieve updated
        certificates from the dogtag master server.
        """
        cmonger = services.knownservices.certmonger
        cmonger.enable()
        services.knownservices.dbus.start()
        cmonger.start()

        bus = dbus.SystemBus()
        obj = bus.get_object('org.fedorahosted.certmonger',
                             '/org/fedorahosted/certmonger')
        iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
        for suffix, args in [('', ''), ('-reuse', ' --reuse-existing')]:
            name = 'dogtag-ipa-ca-renew-agent' + suffix
            path = iface.find_ca_by_nickname(name)
            if not path:
                command = paths.DOGTAG_IPA_CA_RENEW_AGENT_SUBMIT + args
                iface.add_known_ca(
                    name,
                    command,
                    dbus.Array([], dbus.Signature('s')),
                    # Give dogtag extra time to generate cert
                    timeout=CA_DBUS_TIMEOUT)

    def __get_pin(self):
        try:
            return certmonger.get_pin('internal')
        except IOError as e:
            logger.debug(
                'Unable to determine PIN for the Dogtag instance: %s', e)
            raise RuntimeError(e)

    def configure_renewal(self):
        """ Configure certmonger to renew system certs """
        pin = self.__get_pin()

        for nickname in self.tracking_reqs:
            try:
                certmonger.start_tracking(
                    certpath=self.nss_db,
                    ca='dogtag-ipa-ca-renew-agent',
                    nickname=nickname,
                    pin=pin,
                    pre_command='stop_pkicad',
                    post_command='renew_ca_cert "%s"' % nickname,
                )
            except RuntimeError as e:
                logger.error(
                    "certmonger failed to start tracking certificate: %s", e)

    def track_servercert(self):
        """
        Specifically do not tell certmonger to restart the CA. This will be
        done by the renewal script, renew_ca_cert once all the subsystem
        certificates are renewed.
        """
        pin = self.__get_pin()
        try:
            certmonger.start_tracking(
                certpath=self.nss_db,
                ca='dogtag-ipa-ca-renew-agent',
                nickname=self.server_cert_name,
                pin=pin,
                pre_command='stop_pkicad',
                post_command='renew_ca_cert "%s"' % self.server_cert_name)
        except RuntimeError as e:
            logger.error(
                "certmonger failed to start tracking certificate: %s", e)

    def stop_tracking_certificates(self, stop_certmonger=True):
        """Stop tracking our certificates. Called on uninstall.
        """
        logger.debug(
            "Configuring certmonger to stop tracking system certificates "
            "for %s", self.subsystem)

        cmonger = services.knownservices.certmonger
        services.knownservices.dbus.start()
        cmonger.start()

        nicknames = list(self.tracking_reqs)
        if self.server_cert_name is not None:
            nicknames.append(self.server_cert_name)

        for nickname in nicknames:
            try:
                certmonger.stop_tracking(
                    self.nss_db, nickname=nickname)
            except RuntimeError as e:
                logger.error(
                    "certmonger failed to stop tracking certificate: %s", e)

        if stop_certmonger:
            cmonger.stop()

    def update_cert_cs_cfg(self, directive, cert):
        """
        When renewing a Dogtag subsystem certificate the configuration file
        needs to get the new certificate as well.

        ``directive`` is the directive to update in CS.cfg
        cert is IPACertificate.
        cs_cfg is the path to the CS.cfg file
        """

        with stopped_service('pki-tomcatd', 'pki-tomcat'):
            directivesetter.set_directive(
                self.config,
                directive,
                # the cert must be only the base64 string without headers
                (base64.b64encode(cert.public_bytes(x509.Encoding.DER))
                 .decode('ascii')),
                quotes=False,
                separator='=')

    def get_admin_cert(self):
        """
        Get the certificate for the admin user by checking the ldap entry
        for the user.  There should be only one certificate per user.
        """
        logger.debug('Trying to find the certificate for the admin user')
        conn = None

        try:
            ldap_uri = ipaldap.get_ldap_uri(protocol='ldapi', realm=self.realm)
            conn = ipaldap.LDAPClient(ldap_uri)
            conn.external_bind()

            entry_attrs = conn.get_entry(self.admin_dn, ['usercertificate'])
            admin_cert = entry_attrs.get('usercertificate')[0]

            # TODO(edewata) Add check to warn if there is more than one cert.
        finally:
            if conn is not None:
                conn.unbind()

        return admin_cert

    def handle_setup_error(self, e):
        logger.critical("Failed to configure %s instance: %s",
                        self.subsystem, e)
        logger.critical("See the installation logs and the following "
                        "files/directories for more information:")
        logger.critical("  %s", paths.TOMCAT_TOPLEVEL_DIR)

        raise RuntimeError("%s configuration failed." % self.subsystem)

    def add_ipaca_aci(self):
        """Add ACI to allow ipaca users to read their own group information

        Dogtag users aren't allowed to enumerate their own groups. The
        setup_admin() method needs the permission to wait, until all group
        information has been replicated.
        """
        dn = self.ipaca_groups
        mod = [(ldap.MOD_ADD, 'aci', [self.groups_aci])]
        try:
            api.Backend.ldap2.modify_s(dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            logger.debug("%s already has ACI to read group information", dn)
        else:
            logger.debug("Added ACI to read groups to %s", dn)

    def setup_admin(self):
        self.admin_user = "admin-%s" % self.fqdn
        self.admin_password = ipautil.ipa_generate_password()
        self.admin_dn = DN(
            ('uid', self.admin_user), self.ipaca_people
        )
        # remove user if left-over exists
        try:
            api.Backend.ldap2.delete_entry(self.admin_dn)
        except errors.NotFound:
            pass

        # add user
        entry = api.Backend.ldap2.make_entry(
            self.admin_dn,
            objectclass=["top", "person", "organizationalPerson",
                         "inetOrgPerson", "cmsuser"],
            uid=[self.admin_user],
            cn=[self.admin_user],
            sn=[self.admin_user],
            usertype=['adminType'],
            mail=['root@localhost'],
            userPassword=[self.admin_password],
            userstate=['1']
        )
        api.Backend.ldap2.add_entry(entry)

        wait_groups = []
        for group in self.admin_groups:
            group_dn = DN(('cn', group), self.ipaca_groups)
            mod = [(ldap.MOD_ADD, 'uniqueMember', [self.admin_dn])]
            try:
                api.Backend.ldap2.modify_s(group_dn, mod)
            except ldap.TYPE_OR_VALUE_EXISTS:
                # already there
                return None
            else:
                wait_groups.append(group_dn)

        # Now wait until the other server gets replicated this data
        ldap_uri = ipaldap.get_ldap_uri(self.master_host)
        master_conn = ipaldap.LDAPClient(ldap_uri, start_tls=True)
        logger.debug(
            "Waiting for %s to appear on %s", self.admin_dn, master_conn
        )
        deadline = time.time() + api.env.replication_wait_timeout
        while time.time() < deadline:
            time.sleep(1)
            try:
                master_conn.simple_bind(self.admin_dn, self.admin_password)
            except errors.ACIError:
                # user not replicated yet
                pass
            else:
                logger.debug("Successfully logged in as %s", self.admin_dn)
                break
        else:
            logger.error(
                "Unable to log in as %s on %s", self.admin_dn, master_conn
            )
            raise errors.NotFound(
                reason="{} did not replicate to {}".format(
                    self.admin_dn, master_conn
                )
            )

        # wait for group membership
        for group_dn in wait_groups:
            replication.wait_for_entry(
                master_conn,
                group_dn,
                timeout=api.env.replication_wait_timeout,
                attr='uniqueMember',
                attrvalue=self.admin_dn
            )

    def __remove_admin_from_group(self, group):
        dn = DN(('cn', group), self.ipaca_groups)
        mod = [(ldap.MOD_DELETE, 'uniqueMember', self.admin_dn)]
        try:
            api.Backend.ldap2.modify_s(dn, mod)
        except ldap.NO_SUCH_ATTRIBUTE:
            # already removed
            pass

    def teardown_admin(self):
        for group in self.admin_groups:
            self.__remove_admin_from_group(group)
        api.Backend.ldap2.delete_entry(self.admin_dn)

    def backup_config(self):
        """
        Create a backup copy of CS.cfg
        """
        config = self.config
        bak = config + '.ipabkp'
        if services.knownservices['pki_tomcatd'].is_running('pki-tomcat'):
            raise RuntimeError(
                "Dogtag must be stopped when creating backup of %s" % config)
        shutil.copy(config, bak)
        # shutil.copy() doesn't copy owner
        s = os.stat(config)
        os.chown(bak, s.st_uid, s.st_gid)

    def reindex_task(self, force=False):
        """Reindex ipaca entries

        pkispawn sometimes does not run its indextasks. This leads to slow
        unindexed filters on attributes such as description, which is used
        to log in with a certificate. Explicitly reindex attribute that
        should have been reindexed by CA's indextasks.ldif.

        See https://pagure.io/dogtagpki/issue/3083
        """
        state_name = 'reindex_task'
        if not force and sysupgrade.get_upgrade_state('dogtag', state_name):
            return

        cn = "indextask_ipaca_{}".format(int(time.time()))
        dn = DN(
            ('cn', cn), ('cn', 'index'), ('cn', 'tasks'), ('cn', 'config')
        )
        entry = api.Backend.ldap2.make_entry(
            dn,
            objectClass=['top', 'extensibleObject'],
            cn=[cn],
            nsInstance=['ipaca'],  # Dogtag PKI database
            nsIndexAttribute=[
                # from pki/base/ca/shared/conf/indextasks.ldif
                'archivedBy', 'certstatus', 'clientId', 'dataType',
                'dateOfCreate', 'description', 'duration', 'extension',
                'issuedby', 'issuername', 'metaInfo', 'notafter',
                'notbefore', 'ownername', 'publicKeyData', 'requestid',
                'requestowner', 'requestsourceid', 'requeststate',
                'requesttype', 'revInfo', 'revokedOn', 'revokedby',
                'serialno', 'status', 'subjectname',
            ],
            ttl=[10],
        )
        logger.debug('Creating ipaca reindex task %s', dn)
        api.Backend.ldap2.add_entry(entry)
        logger.debug('Waiting for task...')
        exitcode = replication.wait_for_task(api.Backend.ldap2, dn)
        logger.debug(
            'Task %s has finished with exit code %i',
            dn, exitcode
        )
        sysupgrade.set_upgrade_state('dogtag', state_name, True)

    def _configure_clone(self, subsystem_config, security_domain_hostname,
                         clone_pkcs12_path):
        subsystem_config.update(
            # Security domain registration
            pki_security_domain_hostname=security_domain_hostname,
            pki_security_domain_https_port=443,
            pki_security_domain_user=self.admin_user,
            pki_security_domain_password=self.admin_password,
            # Clone
            pki_clone=True,
            pki_clone_pkcs12_path=clone_pkcs12_path,
            pki_clone_pkcs12_password=self.dm_password,
            pki_clone_replication_security="TLS",
            pki_clone_replication_master_port=self.master_replication_port,
            pki_clone_replication_clone_port=389,
            pki_clone_replicate_schema=False,
            pki_clone_uri="https://%s" % ipautil.format_netloc(
                self.master_host, 443),
        )

    def _create_spawn_config(self, subsystem_config):
        loader = PKIIniLoader(
            subsystem=self.subsystem,
            fqdn=self.fqdn,
            domain=api.env.domain,
            basedn=self.basedn,  # o=ipaca / o=kra,o=ipaca
            subject_base=self.subject_base,
            ca_subject=self.ca_subject,
            admin_user=self.admin_user,
            admin_password=self.admin_password,
            dm_password=self.dm_password,
            pki_config_override=self.pki_config_override
        )
        return loader.create_spawn_config(subsystem_config)


class PKIIniLoader:
    ipaca_default = os.path.join(
        paths.USR_SHARE_IPA_DIR, 'ipaca_default.ini'
    )
    ipaca_customize = os.path.join(
        paths.USR_SHARE_IPA_DIR, 'ipaca_customize.ini'
    )

    security_domain_name = 'IPA'
    ipaca_database = 'ipaca'
    admin_nickname = 'ipa-ca-agent'
    token_stanzas = [
        'pki_audit_signing_token',
        'pki_subsystem_token',
        'pki_ca_signing_token',
        'pki_ocsp_signing_token',
        'pki_storage_token',
        'pki_transport_token',
    ]

    def __init__(self, subsystem, fqdn, domain, basedn,
                 subject_base, ca_subject, admin_user, admin_password,
                 dm_password, pki_config_override=None):
        self.pki_config_override = pki_config_override
        self.defaults = dict(
            # pretty much static
            ipa_security_domain_name=self.security_domain_name,
            ipa_ds_database=self.ipaca_database,
            ipa_ds_base_dn=basedn,
            ipa_admin_nickname=self.admin_nickname,
            ipa_ca_pem_file=paths.IPA_CA_CRT,
            # variable
            ipa_ca_subject=ca_subject,
            ipa_subject_base=subject_base,
            ipa_fqdn=fqdn,
            ipa_ocsp_uri="http://{}.{}/ca/ocsp".format(
                IPA_CA_RECORD, ipautil.format_netloc(domain)),
            ipa_admin_cert_p12=paths.DOGTAG_ADMIN_P12,
            ipa_admin_user=admin_user,
            pki_admin_password=admin_password,
            pki_ds_password=dm_password,
            # Dogtag's pkiparser defines these config vars by default:
            pki_dns_domainname=domain,
            pki_hostname=fqdn,
            pki_subsystem=subsystem.upper(),
            pki_subsystem_type=subsystem.lower(),
            home_dir=os.path.expanduser("~"),
            # for softhsm2 testing
            softhsm2_so=paths.LIBSOFTHSM2_SO
        )

    def _mangle_values(self, dct):
        """Stringify and quote % as %% to avoid interpolation errors

        * booleans are converted to 'True', 'False'
        * DN and numbers are converted to string
        * None is turned into empty string ''
        """
        result = {}
        for k, v in dct.items():
            if isinstance(v, (DN, bool, six.integer_types)):
                v = six.text_type(v)
            elif v is None:
                v = ''
            result[k] = v.replace('%', '%%')
        return result

    def _verify_immutable(self, config, immutable_settings, filename):
        section_name = self.defaults['pki_subsystem']
        errors = []
        for key, isvalue in sorted(immutable_settings.items()):
            cfgvalue = config.get(section_name, key)
            if isvalue != cfgvalue:
                errors.append(f"{key}: '{cfgvalue}' != '{isvalue}'")
        if errors:
            raise ValueError(
                '{} overrides immutable options:\n{}'.format(
                    filename, '\n'.join(errors)
                )
            )

    def create_spawn_config(self, subsystem_config):
        """Create config instance
        """
        section_name = self.defaults['pki_subsystem']
        defaults = self._mangle_values(self.defaults)

        # create a config template with interpolation support
        # read base config
        cfgtpl = ConfigParser(defaults=defaults)
        cfgtpl.optionxform = str
        with open(self.ipaca_default) as f:
            cfgtpl.read_file(f)

        # overwrite defaults with our defaults
        for key, value in defaults.items():
            cfgtpl.set(DEFAULTSECT, key, value)

        # overwrite CA/KRA config with subsystem settings
        subsystem_config = self._mangle_values(subsystem_config)
        for key, value in subsystem_config.items():
            cfgtpl.set(section_name, key, value)

        # get a list of settings that cannot be modified by users
        immutable_settings = {
            k: v for k, v in cfgtpl.items(section=section_name)
            if k.startswith('pki_')
        }

        # add ipaca_customize overlay,
        #  These are settings that can be modified by a user, too. We use
        # ipaca_customize.ini to set sensible defaults.
        with open(self.ipaca_customize) as f:
            cfgtpl.read_file(f)

        # sanity check
        self._verify_immutable(
            cfgtpl, immutable_settings, self.ipaca_customize
        )

        # load external overlay from command line
        if self.pki_config_override is not None:
            with open(self.pki_config_override) as f:
                cfgtpl.read_file(f)
            self._verify_immutable(
                cfgtpl, immutable_settings, self.pki_config_override
            )

        # key backup is not compatible with HSM support
        if (cfgtpl.has_option(section_name, 'pki_hsm_enable') and
                cfgtpl.getboolean(section_name, 'pki_hsm_enable')):
            cfgtpl.set(section_name, 'pki_backup_keys', 'False')
            cfgtpl.set(section_name, 'pki_backup_password', '')

        pki_token_name = cfgtpl.get(section_name, 'pki_token_name')
        for stanza in self.token_stanzas:
            if cfgtpl.has_option(section_name, stanza):
                cfgtpl.set(section_name, stanza, pki_token_name)

        # Next up, get rid of interpolation variables, DEFAULT,
        # irrelevant sections and unused variables. Only the subsystem
        # section is copied into a new raw config parser. A raw config
        # parser is necessary, because ConfigParser.write() write passwords
        # with '%' in a way, that is not accepted by Dogtag.
        config = RawConfigParser()
        config.optionxform = str
        config.add_section(section_name)
        for key, value in sorted(cfgtpl.items(section=section_name)):
            if key.startswith('pki_'):
                config.set(section_name, key, value)

        return config


def test():
    import sys

    loader = PKIIniLoader(
        subsystem='CA',
        fqdn='replica.ipa.example',
        domain='ipa.example',
        basedn='o=ipaca',
        subject_base='o=IPA,o=EXAMPLE',
        ca_subject='cn=CA,o=IPA,o=EXAMPLE',
        admin_user='admin',
        admin_password='Secret1',
        dm_password='Secret2',
        pki_config_override='install/share/ipaca_softhsm2.ini',
    )
    loader.ipaca_default = 'install/share/ipaca_default.ini'
    loader.ipaca_customize = 'install/share/ipaca_customize.ini'
    config = loader.create_spawn_config(dict(
        pki_external=True
    ))
    config.write(sys.stdout, False)


if __name__ == '__main__':
    test()
