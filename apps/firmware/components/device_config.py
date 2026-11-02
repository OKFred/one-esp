"""Small, revisioned configuration store with a staged Wi-Fi trial."""

import os
try:
    import ujson as json
except ImportError:
    import json

from components.env_reader import env_reader


ALLOWED_FIELDS = ('SSID', 'PASSWORD', 'NTP_SERVER', 'TIMEZONE_HOURS',
                  'NETWORK_TEST_HOST', 'NETWORK_TEST_PORT', 'POLL_INTERVAL_MS')
SECRET_FIELDS = ('PASSWORD',)
DEFAULTS = {'NTP_SERVER': 'ntp.aliyun.com', 'TIMEZONE_HOURS': '8',
            'NETWORK_TEST_HOST': 'baidu.com', 'NETWORK_TEST_PORT': '443',
            'POLL_INTERVAL_MS': '500'}


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _replace(source, target):
    getattr(os, 'replace', os.rename)(source, target)


def _atomic_text(path, content):
    temporary = path + '.new'
    with open(temporary, 'w') as stream:
        stream.write(content)
        stream.flush()
    _replace(temporary, path)


def _read(path):
    with open(path) as stream:
        return stream.read()


def _encode(values):
    return ''.join(key + '=' + str(values[key]) + '\n' for key in sorted(values))


def _host(value):
    return bool(value) and len(value) <= 253 and all(
        ('a' <= char <= 'z') or ('A' <= char <= 'Z') or
        ('0' <= char <= '9') or char in '.-_' for char in value)


class ConfigStore:
    def __init__(self, path='.env'):
        self.path = path
        self.revision_path = path + '.revision'
        self.pending_path = path + '.pending'
        self.backup_path = path + '.previous'
        self.transaction_path = path + '.transaction'
        # Until the transaction marker is removed, a power failure or restart
        # rolls back both the values and their revision, never a half commit.
        if _exists(self.transaction_path):
            previous = json.loads(_read(self.transaction_path))
            _atomic_text(path, _encode(previous['values']))
            _atomic_text(self.revision_path, str(previous['revision']))
            os.remove(self.transaction_path)
        self.values = dict(DEFAULTS)
        if _exists(path):
            self.values.update(env_reader(path))
        self.revision = 0
        if _exists(self.revision_path):
            try:
                self.revision = int(_read(self.revision_path))
            except (ValueError, OSError):
                pass
        self.pending = None
        # A trial is never promoted merely because the device rebooted.
        _remove(self.pending_path)

    def public(self):
        return {
            'revision': self.revision,
            'values': {key: value for key, value in self.values.items() if key not in SECRET_FIELDS},
            'secrets': {key: bool(self.values.get(key)) for key in SECRET_FIELDS},
            'pending': self.pending is not None,
            'supportedFields': list(ALLOWED_FIELDS),
            'application': {
                'SSID': '60-second connection trial, then saved on success',
                'PASSWORD': '60-second connection trial, then saved on success',
                'TIMEZONE_HOURS': 'next clock display update',
                'NTP_SERVER': 'new non-blocking clock synchronization when the value changes',
                'NETWORK_TEST_HOST': 'new non-blocking TCP probe when the value changes',
                'NETWORK_TEST_PORT': 'new non-blocking TCP probe when the value changes',
                'POLL_INTERVAL_MS': 'legacy saved field; sensor.config controls acquisition cadence',
            },
            'unsupported': [],
            'separateConfiguration': {'MQTT': 'mqtt.config.get/set', 'RS485': 'sensor.config.get/set'},
        }

    def validate(self, changes):
        if type(changes) is not dict or not changes:
            raise ValueError('Configuration values are required')
        updated = dict(self.values)
        for key, value in changes.items():
            if key not in ALLOWED_FIELDS:
                raise ValueError('Unsupported configuration field')
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError('Invalid configuration value')
            value = str(value)
            if '\n' in value or '\r' in value or '\x00' in value:
                raise ValueError('Configuration cannot contain control characters')
            if key == 'SSID' and not 1 <= len(value.encode()) <= 32:
                raise ValueError('SSID must contain 1 to 32 bytes')
            if key == 'PASSWORD' and len(value.encode()) > 64:
                raise ValueError('Password is too long')
            if key in ('NTP_SERVER', 'NETWORK_TEST_HOST') and not _host(value):
                raise ValueError('Invalid host name')
            if key == 'TIMEZONE_HOURS':
                try:
                    number = float(value)
                except ValueError:
                    raise ValueError('Invalid timezone')
                if not -12 <= number <= 14:
                    raise ValueError('Timezone is out of range')
            if key in ('NETWORK_TEST_PORT', 'POLL_INTERVAL_MS'):
                try:
                    number = int(value)
                except ValueError:
                    raise ValueError('Invalid numeric configuration')
                lower, upper = (1, 65535) if key == 'NETWORK_TEST_PORT' else (100, 600000)
                if not lower <= number <= upper:
                    raise ValueError('Numeric configuration is out of range')
            updated[key] = value
        return updated

    def begin(self, changes, revision):
        if type(revision) is not int or revision != self.revision:
            raise ValueError('Configuration revision changed; reload before saving')
        if self.pending is not None:
            raise ValueError('A configuration trial is already running')
        candidate = self.validate(changes)
        wifi_changed = any(candidate.get(key) != self.values.get(key) for key in ('SSID', 'PASSWORD'))
        _atomic_text(self.pending_path, _encode(candidate))
        self.pending = candidate
        return wifi_changed

    def commit(self):
        if self.pending is None:
            raise ValueError('No configuration trial')
        candidate = self.pending
        try:
            if _exists(self.path):
                _atomic_text(self.backup_path, _read(self.path))
            _atomic_text(self.transaction_path, json.dumps({'values': self.values, 'revision': self.revision}))
            _replace(self.pending_path, self.path)
            _atomic_text(self.revision_path, str(self.revision + 1))
            os.remove(self.transaction_path)
        except OSError:
            if _exists(self.transaction_path):
                try:
                    _atomic_text(self.path, _encode(self.values))
                    _atomic_text(self.revision_path, str(self.revision))
                    os.remove(self.transaction_path)
                except OSError:
                    # Keep the transaction marker for recovery on next boot
                    # when storage cannot currently complete the rollback.
                    pass
            raise
        finally:
            # Failure before or after renaming must not leave a permanent
            # in-memory busy state or a promotable trial on the filesystem.
            self.pending = None
            _remove(self.pending_path)
        self.values = candidate
        self.revision += 1
        return self.public()

    def rollback(self):
        self.pending = None
        _remove(self.pending_path)
        return self.public()
