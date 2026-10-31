"""Bounded, revisioned business settings. No hardware or network activity."""

import os
try:
    import ujson as json
except ImportError:
    import json
try:
    import uhashlib as hashlib
    import ubinascii as binascii
except ImportError:
    import hashlib
    import binascii

SENSOR_DEFAULTS = {'enabled': False, 'protocol': 'ascii_read', 'interval_seconds': 60,
                   'baudrate': 9600, 'slave_address': 1, 'timeout_ms': 2000}
MQTT_DEFAULTS = {'enabled': False, 'host': '', 'port': 8883, 'server_hostname': '',
                 'username': '', 'password': '', 'client_id': '', 'site_id': '',
                 'gateway_id': '', 'device_id': '', 'interval_seconds': 60,
                 'keepalive': 60, 'commands_enabled': False, 'ca_file': '.mqtt-ca.der'}
MQTT_EDITABLE = tuple(key for key in MQTT_DEFAULTS if key != 'ca_file') + ('broker_ip',)
MAX_BYTES = 4096
MAX_REDACTIONS = 4


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


def _read(path, maximum=MAX_BYTES):
    with open(path, 'rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Settings file is too large')
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, TypeError):
        raise ValueError('Invalid settings file')


def _write(path, value, maximum=MAX_BYTES):
    text = json.dumps(value)
    if len(text.encode('utf-8')) > maximum:
        raise ValueError('Settings file is too large')
    with open(path + '.new', 'w') as stream:
        stream.write(text)
        stream.flush()
    getattr(os, 'replace', os.rename)(path + '.new', path)


def validate_sensor(values):
    if type(values) is not dict or any(key not in SENSOR_DEFAULTS for key in values):
        raise ValueError('Invalid sensor configuration fields')
    result = dict(SENSOR_DEFAULTS)
    result.update(values)
    if type(result['enabled']) is not bool:
        raise ValueError('Invalid sensor enabled flag')
    if result['protocol'] not in ('ascii_read', 'sht20_rtu'):
        raise ValueError('Invalid sensor protocol')
    for key, low, high in (('interval_seconds', 5, 3600), ('slave_address', 1, 247),
                           ('timeout_ms', 200, 5000)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid sensor ' + key)
    if type(result['baudrate']) is not int or result['baudrate'] not in (9600, 14400, 19200):
        raise ValueError('Invalid sensor baudrate')
    return result


def validate_mqtt(values):
    allowed = tuple(MQTT_DEFAULTS) + ('broker_ip', 'keepalive_seconds')
    if type(values) is not dict or any(key not in allowed for key in values):
        raise ValueError('Invalid MQTT configuration fields')
    result = dict(MQTT_DEFAULTS)
    result.update(values)
    if 'keepalive_seconds' in result:
        if 'keepalive' in values and values['keepalive'] != result['keepalive_seconds']:
            raise ValueError('Conflicting MQTT keepalive fields')
        result['keepalive'] = result.pop('keepalive_seconds')
    for key in ('enabled', 'commands_enabled'):
        if type(result[key]) is not bool:
            raise ValueError('Invalid MQTT ' + key)
    if not result['server_hostname']:
        result['server_hostname'] = result['host']
    # Even disabled settings retain validated, bounded editable fields. Empty
    # required fields are allowed until the user enables this configuration.
    for key in ('host', 'server_hostname', 'username', 'password', 'client_id',
                'site_id', 'gateway_id', 'device_id', 'ca_file'):
        value = result[key]
        if (not isinstance(value, str) or len(value.encode()) > (256 if key in ('username', 'password', 'ca_file') else 253)
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Invalid MQTT ' + key)
    if type(result['port']) is not int or not 1 <= result['port'] <= 65535:
        raise ValueError('Invalid MQTT TLS port')
    for key, low, high in (('interval_seconds', 60, 86400), ('keepalive', 15, 300)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid MQTT ' + key)
    from components.mqtt_telemetry import validate_config
    if result['enabled']:
        validate_config(result)
    return result


class SettingsStore:
    def __init__(self, path, kind):
        if kind not in ('mqtt', 'sensor'):
            raise ValueError('Invalid settings kind')
        self.path, self.kind = path, kind
        self.pending_path = path + '.pending'
        self.previous_path = path + '.previous'
        self.transaction_path = path + '.transaction'
        self.revision_path = path + '.revision'
        self.redactions_path = path + '.redactions'
        self._validate = validate_mqtt if kind == 'mqtt' else validate_sensor
        self.pending = None
        self._redactions = []
        if _exists(self.transaction_path):
            previous = _read(self.transaction_path, MAX_BYTES + 256)
            restored = self._validate(previous['values'])
            if type(previous.get('revision')) is not int or previous['revision'] < 0:
                raise ValueError('Invalid settings recovery revision')
            _write(path, restored)
            _write(self.revision_path, previous['revision'])
            os.remove(self.transaction_path)
        raw = _read(path) if _exists(path) else {}
        self.values = self._validate(raw)
        self.revision = _read(self.revision_path) if _exists(self.revision_path) else 0
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError('Invalid settings revision')
        _remove(self.pending_path)
        if kind == 'mqtt' and _exists(self.redactions_path):
            values = _read(self.redactions_path)
            if type(values) is list:
                self._redactions = [v for v in values if isinstance(v, str) and 0 < len(v.encode()) <= 256][-MAX_REDACTIONS:]

    def secret_values(self):
        result = list(self._redactions)
        if self.kind == 'mqtt':
            for values in (self.values, self.pending):
                secret = values.get('password') if values else None
                if secret and secret not in result:
                    result.append(secret)
        return result[-(MAX_REDACTIONS + 2):]

    def public(self, source=None, revision=None, pending=None):
        source = self.values if source is None else source
        values = {key: value for key, value in source.items() if key != 'password'}
        result = {'revision': self.revision if revision is None else revision, 'values': values,
                  'pending': self.pending is not None if pending is None else pending,
                  'secrets': {'password': bool(source.get('password'))} if self.kind == 'mqtt' else {}}
        if self.kind == 'mqtt':
            ca = source['ca_file']
            if ca and ca[0] not in '/\\' and not (len(ca) > 1 and ca[1] == ':'):
                normalized = self.path.replace('\\', '/')
                if '/' in normalized:
                    ca = normalized.rsplit('/', 1)[0] + '/' + ca
            result['ca'] = None
            try:
                with open(ca, 'rb') as stream:
                    raw = stream.read(8193)
                if 0 < len(raw) <= 8192:
                    result['ca'] = {'bytes': len(raw), 'sha256': binascii.hexlify(hashlib.sha256(raw).digest()).decode()}
            except OSError:
                pass
        return result

    def begin(self, changes, revision):
        if type(revision) is not int or revision != self.revision:
            raise ValueError('Configuration revision changed; reload before saving')
        if self.pending is not None:
            raise ValueError('A configuration trial is already running')
        allowed = MQTT_EDITABLE if self.kind == 'mqtt' else tuple(SENSOR_DEFAULTS)
        if type(changes) is not dict or not changes or any(key not in allowed for key in changes):
            raise ValueError('Invalid settings changes')
        candidate = dict(self.values)
        candidate.update(changes)
        if self.kind == 'mqtt':
            if changes.get('broker_ip') == '':
                candidate.pop('broker_ip', None)
            if changes.get('password') in (None, ''):
                candidate['password'] = self.values.get('password', '')
            if ('host' in changes and 'server_hostname' not in changes and
                    self.values.get('server_hostname') in ('', self.values.get('host'))):
                candidate['server_hostname'] = changes['host']
        candidate = self._validate(candidate)
        if self.kind == 'mqtt':
            secrets = self.secret_values()
            if candidate.get('password') and candidate['password'] not in secrets:
                secrets.append(candidate['password'])
            self._redactions = secrets[-MAX_REDACTIONS:]
            _write(self.redactions_path, self._redactions)
        _write(self.pending_path, candidate)
        self.pending = candidate
        return candidate

    def commit(self, result_fields=None):
        if self.pending is None:
            raise ValueError('No settings trial')
        candidate = self.pending
        # Allocate the response/CA metadata before changing persistent state.
        result = self.public(candidate, self.revision + 1, False)
        if result_fields:
            result.update(result_fields)
        try:
            if _read(self.pending_path) != candidate:
                raise ValueError('Staged settings changed')
            _write(self.previous_path, self.values)
            _write(self.transaction_path, {'values': self.values, 'revision': self.revision}, MAX_BYTES + 256)
            getattr(os, 'replace', os.rename)(self.pending_path, self.path)
            _write(self.revision_path, self.revision + 1)
            os.remove(self.transaction_path)
        except (OSError, ValueError, MemoryError):
            if _exists(self.transaction_path):
                try:
                    _write(self.path, self.values)
                    _write(self.revision_path, self.revision)
                    os.remove(self.transaction_path)
                except (OSError, ValueError, MemoryError):
                    pass
            raise
        finally:
            self.pending = None
            _remove(self.pending_path)
        self.values = candidate
        self.revision += 1
        return result

    def rollback(self):
        self.pending = None
        _remove(self.pending_path)
        return self.public()
