#!/usr/bin/python

__author__ = 'Chris Holcombe <chris.holcombe@canonical.com>'
import json
import os
from subprocess import check_output

from charmhelpers.fetch import add_source, apt_update, apt_install

import sys

sys.path.append('lib')
from ceph.ceph_helpers import send_request_if_needed, is_request_complete, \
    CephBrokerRq

from charmhelpers.core.templating import render

from charmhelpers.core.hookenv import (
    status_set,
    config, log,
    Hooks, relation_get, UnregisteredHookError, relation_ids, DEBUG)

from ceph.ceph_helpers import (
    get_mon_hosts
)

from common import Backend

hooks = Hooks()

valid_backup_periods = ['monthly', 'weekly', 'daily', 'hourly']
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config")


def emit_cephconf(ceph_context):
    ceph_path = os.path.join(os.sep,
                             "etc",
                             "ceph",
                             "ceph.conf")
    try:
        render('ceph.conf',
               ceph_path,
               ceph_context,
               perms=0o644)
    except IOError as err:
        log("Error creating /etc/ceph/ceph.conf file: {}".format(err.message),
            level='error')


def write_cephx_key(keyring):
    cephx_key = os.path.join(os.sep, 'etc', 'ceph',
                             'ceph.client.preserve.keyring')
    try:
        with open(cephx_key, 'w') as key_file:
            key_file.write("[client.preserve]\n\tkey = {}\n".format(keyring))
    except IOError as err:
        log("IOError writing ceph.client.preserve.keyring: {}".format(err))


@hooks.hook('install.real')
def install_ceph():
    add_source(config('ceph-source'), config('ceph-key'))
    add_source(config('gluster-source'), config('gluster-key'))
    apt_update(fatal=True)
    apt_install(packages=['ceph', 'glusterfs-common'],
                fatal=True)


def setup_cron_job(cron_spec, directories_list):
    cron_path = os.path.join(os.sep,
                             "etc",
                             "cron.d",
                             "backup")
    context = {'directories': directories_list}
    backend = Backend()

    # Overwrite the file if needed
    try:
        context['cron_spec'] = cron_spec
        context['backend'] = backend.get_backend()
        context['configdir'] = CONFIG_DIR
        # Add --vault flag if vault is related
        if relation_ids('vault'):
            context['vault'] = True
        else:
            context['vault'] = False

        render('backup_cron',
               cron_path,
               context,
               perms=0o644)
    except IOError as err:
        log("Error creating cron file: {}".format(err.message),
            level='error')


def write_config(config_file_name, contents):
    """
    Write out the json config for preserve to use to configure backups

    :param config_file_name: six.string_types. The config file name to create
    :param contents:  dict. A dict holding the values to write to the json file
    """
    if not os.path.exists(CONFIG_DIR):
        os.mkdir(CONFIG_DIR)
    path = os.path.join(CONFIG_DIR, config_file_name)
    try:
        status_set('maintenance', 'Writing config: {}'.format(path))
        with open(path, 'w') as config_file:
            config_file.write(json.dumps(contents))
    except IOError as err:
        log('error writing out config file: {}.  Error was: {}'.format(
            path, err.message))


def setup_backup_cron():
    backup_period = config('backup-frequency')
    if backup_period not in valid_backup_periods:
        # Fail
        status_set('blocked', 'Invalid backup period.')
        log('Invalid backup period: {bad}.  Valid periods are {good}'.format(
            bad=backup_period, good=valid_backup_periods), level='error')
    status_set('maintenance', 'Setting up backup cron job')
    setup_cron_job(cron_spec=config('backup-frequency'),
                   directories_list=config('backup-path').split(' '))


@hooks.hook('config-changed')
def config_changed():
    setup_backup_cron()


@hooks.hook('mon-relation-joined')
@hooks.hook('mon-relation-changed')
def ceph_relation_changed():
    # Request that pools be created
    rq = CephBrokerRq()
    rq.add_op_create_pool(name="preserve_data",
                          replica_count=3,
                          weight=None)
    if is_request_complete(rq, relation='mon'):
        log('Broker request complete', level=DEBUG)
        public_addr = relation_get('ceph-public-address')
        auth = relation_get('auth')
        key = relation_get('key')
        if key and auth and public_addr:
            mon_hosts = get_mon_hosts()
            context = {
                'auth_supported': auth,
                'mon_hosts': ' '.join(mon_hosts),
                'use_syslog': 'true',
                'loglevel': config('loglevel'),
            }
            emit_cephconf(ceph_context=context)
            write_config(config_file_name='ceph.json', contents={
                'config_file': '/etc/ceph/ceph.conf',
                'user_id': 'preserve',
                'data_pool': 'preserve_data',
            })
            write_cephx_key(key)
            setup_backup_cron()
    else:
        send_request_if_needed(rq, relation='mon')


@hooks.hook('mon-relation-departed')
def ceph_relation_departed():
    """
    Ceph has been disconnected

    """
    # Remove the config file so we no longer connect to Ceph with preserve
    os.remove(os.path.join(CONFIG_DIR, 'ceph.json'))


@hooks.hook('vault-relation-joined')
@hooks.hook('vault-relation-changed')
def vault_relation_changed():
    write_config(config_file_name='vault.json', contents={
        'host': 'http://{host}:{port}'.format(host=relation_get('host'),
                                              port='8200'),
        'token': relation_get('token'),
    })
    if not os.path.exists(CONFIG_DIR):
        log("Creating config directory: {}".format(CONFIG_DIR))
        status_set('maintenance', 'Creating config dir: {}'.format(CONFIG_DIR))
        os.mkdir(CONFIG_DIR)
    check_output(
        ["/snap/bin/preserve", "--configdir", CONFIG_DIR, "keygen", "--vault"])


@hooks.hook('vault-relation-departed')
def vault_relation_departed():
    """
    Vault has been disconnected

    """
    os.remove(os.path.join(CONFIG_DIR, 'vault.json'))


@hooks.hook('gluster-relation-departed')
def gluster_relation_departed():
    """
    Gluster has been disconnected

    """
    os.remove(os.path.join(CONFIG_DIR, 'gluster.json'))


@hooks.hook('gluster-relation-joined')
@hooks.hook('gluster-relation-changed')
def gluster_relation_changed():
    public_addr = relation_get('gluster-public-address')
    volumes = relation_get('volumes')
    # TODO: Which volume should we use?
    if public_addr and volumes:
        write_config(config_file_name='gluster.json', contents={
            'server': public_addr,
            'port': '24007',
            'volume_name': 'test'
        })
    setup_backup_cron()


def assess_status():
    backend_related = False
    vault_related = False
    if not relation_ids('mon') or relation_ids('gluster'):
        status_set('blocked',
                   'Please relate to a backend.  Either gluster or ceph-mon')
    backend_related = True
    if not relation_ids('vault'):
        status_set('blocked',
                   'Please relate to vault to store the key file')
    vault_related = True

    if vault_related and backend_related:
        status_set('active', 'Ready to run backups')


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status()
