"""Live, bounded presence; observations are not an offline history queue."""

import os
import time
try:
    import ujson as json
except ImportError:
    import json

from components.mqtt_telemetry import _identity, _uuid4, _utc_stamp

HEARTBEAT_MS = 60000
MAX_PRESENCE_BYTES = 2048


class MqttPresence:
    def __init__(self, config, now_ms, boot_id=None, utc_ready=None,
                 time_module=None, random_bytes=None):
        if not all(_identity(config.get(key)) for key in
                   ('site_id', 'gateway_id', 'device_id', 'client_id')):
            raise ValueError('Invalid MQTT presence identity')
        if boot_id is not None and not _identity(boot_id):
            raise ValueError('Invalid MQTT presence boot identity')
        self._identity = tuple(config[key] for key in ('site_id', 'gateway_id', 'device_id', 'client_id'))
        self._now, self._boot_id = now_ms, boot_id
        self._utc_ready, self._clock = utc_ready, time_module or time
        self._random = random_bytes or os.urandom
        self._prefix = 'iot/v1/' + config['site_id'] + '/' + config['gateway_id'] + '/devices/'
        self.topic = self._prefix + config['device_id'] + '/status'
        self.session_id = None
        self._sequence = 0
        self._connected = False
        self._closing = False
        self._own = self._child = None
        self._next_heartbeat = 0
        self._next_failure = 0

    def _event(self, event, online, reason, child=False, will=False):
        stamp = None
        if not will:
            try:
                if self._utc_ready is None or self._utc_ready():
                    stamp = _utc_stamp(self._clock)
            except Exception:
                pass
        site, gateway, device, reporter = self._identity
        value = {'schema_version': 1, 'event_id': _uuid4(self._random),
                 'site_id': site, 'gateway_id': gateway,
                 'device_id': 'sht20' if child else device, 'reporter_id': reporter,
                 'boot_id': self._boot_id, 'session_id': self.session_id,
                 'sequence': None if will else self._sequence,
                 'event': event, 'online': online, 'reason': reason,
                 'source_time_utc': stamp, 'uptime_ms': None if will else self._now()}
        payload = json.dumps(value).encode('utf-8')
        if len(payload) > MAX_PRESENCE_BYTES:
            raise ValueError('MQTT presence payload is too large')
        if not will:
            self._sequence += 1
        return (self._prefix + 'sht20/status' if child else self.topic, payload,
                self._now() + HEARTBEAT_MS)

    def begin_session(self):
        self.disconnected()
        self.session_id = _uuid4(self._random)
        self._sequence = 0
        return self._event('offline', False, 'lwt', will=True)[:2]

    def connected(self):
        self._connected, self._closing = True, False
        self._own = self._event('online', True, 'connected')
        self._child = self._event('observation', None, 'upstream_unknown', child=True)
        self._next_heartbeat = self._now() + HEARTBEAT_MS
        self._next_failure = 0

    def disconnected(self):
        self._connected = self._closing = False
        self._own = self._child = None

    def tick(self):
        if self._connected and not self._closing and self._now() >= self._next_heartbeat:
            self._own = self._event('heartbeat', True, 'heartbeat')
            self._next_heartbeat = self._now() + HEARTBEAT_MS

    def observe(self, successful):
        if not self._connected or self._closing:
            return
        now = self._now()
        if successful is False and now < self._next_failure:
            return
        if successful is not None and type(successful) is not bool:
            raise ValueError('Invalid MQTT sensor presence')
        self._child = self._event('observation', successful,
                                  'upstream_unknown' if successful is None else
                                  'read_ok' if successful else 'read_failed', child=True)
        self._next_failure = now + HEARTBEAT_MS if successful is False else 0

    def shutdown(self):
        if not self._connected or self._closing:
            return
        self._closing = True
        self._own = self._event('offline', False, 'shutdown')
        self._child = self._event('observation', None, 'upstream_unknown', child=True)

    def take(self):
        now = self._now()
        for key in ('_own', '_child'):
            value = getattr(self, key)
            setattr(self, key, None)
            if value is not None and now < value[2]:
                return value
        return None

    def pending(self):
        return self._own is not None or self._child is not None
