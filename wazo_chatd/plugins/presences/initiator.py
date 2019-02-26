# Copyright 2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

from wazo_chatd.database.models import (
    Device,
    Line,
    User,
    Session,
    Tenant,
)
from wazo_chatd.database.helpers import session_scope
from wazo_chatd.exceptions import (
    UnknownDeviceException,
    UnknownLineException,
    UnknownSessionException,
    UnknownTenantException,
    UnknownUserException,
)

logger = logging.getLogger(__name__)

DEVICE_STATE_MAP = {
    'INUSE': 'talking',
    'UNAVAILABLE': 'unavailable',
    'NOT_INUSE': 'available',
    'RINGING': 'ringing',
    'ONHOLD': 'holding',

    'RINGINUSE': 'ringing',
    'UNKNOWN': 'unavailable',
    'BUSY': 'unavailable',
    'INVALID': 'unavailable',
}


def extract_device_name(line):
    if line.get('endpoint_sip'):
        return 'PJSIP/{}'.format(line['name'])
    elif line.get('endpoint_sccp'):
        return 'SCCP/{}'.format(line['name'])
    elif line.get('endpoint_custom'):
        return line['name']


class Initiator:

    def __init__(self, dao, auth):
        self._dao = dao
        self._auth = auth
        self._token = None

    @property
    def token(self):
        if not self._token:
            self._token = self._auth.token.new(expiration=120)['token']
        return self._token

    def initiate_tenants(self):
        self._auth.set_token(self.token)
        tenants = self._auth.tenants.list()['items']

        tenants = set(tenant['uuid'] for tenant in tenants)
        tenants_cached = set(tenant.uuid for tenant in self._dao.tenant.list_())

        tenants_missing = tenants - tenants_cached
        with session_scope():
            for uuid in tenants_missing:
                logger.debug('Create tenant "%s"', uuid)
                tenant = Tenant(uuid=uuid)
                self._dao.tenant.create(tenant)

        tenants_expired = tenants_cached - tenants
        with session_scope():
            for uuid in tenants_expired:
                try:
                    tenant = self._dao.tenant.get(uuid)
                except UnknownTenantException as e:
                    logger.warning(e)
                    continue
                logger.debug('Delete tenant "%s"', uuid)
                self._dao.tenant.delete(tenant)

    def initiate_users(self, confd):
        confd.set_token(self.token)
        users = confd.users.list(recurse=True)['items']
        self._add_and_remove_users(confd, users)
        self._add_and_remove_lines(confd, users)
        self._associate_line_device(confd, users)

    def _add_and_remove_users(self, confd, users):
        users = set((user['uuid'], user['tenant_uuid']) for user in users)
        users_cached = set((u.uuid, u.tenant_uuid) for u in self._dao.user.list_(tenant_uuids=None))

        users_missing = users - users_cached
        with session_scope():
            for uuid, tenant_uuid in users_missing:
                # Avoid race condition between init tenant and init user
                tenant = self._dao.tenant.find_or_create(tenant_uuid)

                logger.debug('Create user "%s"', uuid)
                user = User(uuid=uuid, tenant=tenant, state='unavailable')
                self._dao.user.create(user)

        users_expired = users_cached - users
        with session_scope():
            for uuid, tenant_uuid in users_expired:
                try:
                    user = self._dao.user.get([tenant_uuid], uuid)
                except UnknownUserException as e:
                    logger.warning(e)
                    continue
                logger.debug('Delete user "%s"', uuid)
                self._dao.user.delete(user)

    def _add_and_remove_lines(self, confd, users):
        lines = set((line['id'], user['uuid'], user['tenant_uuid']) for user in users for line in user['lines'])
        lines_cached = set((line.id, line.user_uuid, line.tenant_uuid) for line in self._dao.line.list_())

        lines_missing = lines - lines_cached
        with session_scope():
            for id_, user_uuid, tenant_uuid in lines_missing:
                try:
                    user = self._dao.user.get([tenant_uuid], user_uuid)
                except UnknownUserException as e:
                    logger.warning(e)
                    continue
                line = Line(id=id_)
                logger.debug('Create line "%s"', id_)
                self._dao.user.add_line(user, line)

        lines_expired = lines_cached - lines
        with session_scope():
            for id_, user_uuid, tenant_uuid in lines_expired:
                try:
                    user = self._dao.user.get([tenant_uuid], user_uuid)
                    line = self._dao.line.get(id_)
                except UnknownUserException:
                    logger.debug('Line "%s" already deleted', id_)
                    continue
                logger.debug('Delete line "%s"', id_)
                self._dao.user.remove_session(user, line)

    def _associate_line_device(self, confd, users):
        lines_info = [{'id': line['id'], 'device_name': extract_device_name(line)}
                      for user in users for line in user['lines']]
        with session_scope():
            for line_info in lines_info:
                try:
                    line = self._dao.line.get(line_info['id'])
                    device = self._dao.device.get_by(name=line_info['device_name'])
                except (UnknownLineException, UnknownDeviceException):
                    logger.debug(
                        'Unable to associate line "%s" with device "%s"',
                        line_info['id'],
                        line_info['device_name'],
                    )
                    continue
                logger.debug('Associate line "%s" with device "%s"', line.id, device.name)
                self._dao.line.associate_device(line, device)

    def initiate_sessions(self):
        self._auth.set_token(self.token)
        sessions = self._auth.sessions.list(recurse=True)['items']

        sessions = set(
            (session['uuid'], session['user_uuid'], session['tenant_uuid'])
            for session in sessions
        )
        sessions_cached = set(
            (session.uuid, session.user_uuid, session.tenant_uuid)
            for session in self._dao.session.list_()
        )

        sessions_missing = sessions - sessions_cached
        with session_scope():
            for uuid, user_uuid, tenant_uuid in sessions_missing:
                try:
                    user = self._dao.user.get([tenant_uuid], user_uuid)
                except UnknownUserException:
                    logger.debug('Session "%s" has no valid user "%s"', uuid, user_uuid)
                    continue

                logger.debug('Create session "%s" for user "%s"', uuid, user_uuid)
                session = Session(uuid=uuid, user_uuid=user_uuid)
                self._dao.user.add_session(user, session)

        sessions_expired = sessions_cached - sessions
        with session_scope():
            for uuid, user_uuid, tenant_uuid in sessions_expired:
                try:
                    user = self._dao.user.get([tenant_uuid], user_uuid)
                    session = self._dao.session.get(uuid)
                except (UnknownUserException, UnknownSessionException) as e:
                    logger.warning(e)
                    continue

                logger.debug('Delete session "%s" for user "%s"', uuid, user_uuid)
                self._dao.user.remove_session(user, session)

    def initiate_devices(self, amid):
        amid.set_token(self.token)
        events = amid.action('DeviceStateList')

        with session_scope():
            logger.debug('Delete all devices')
            self._dao.device.delete_all()
            for event in events:
                if event.get('Event') != 'DeviceStateChange':
                    continue

                device_args = {
                    'name': event['Device'],
                    'state': DEVICE_STATE_MAP.get(event['State'], 'unavailable'),
                }
                logger.debug(
                    'Create device "%s" with state "%s"', device_args['name'], device_args['state']
                )
                self._dao.device.create(Device(**device_args))
