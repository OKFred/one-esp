"""One bounded direct-read temperature sample, independent of debug.

The transport owns QoS 1 retransmission. This sampler retains the original
envelope until PUBACK. The default reads MCU temperature; an optional provider
supplies fresh environment observations with their original UTC read time.
Private MQTT configuration is separate from the USB-editable Wi-Fi settings.
"""

try:
    import ujson as json
except ImportError:
    import json
try:
    import ubinascii as binascii
except ImportError:
    import binascii
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
import os
import time


MAX_CONFIG_BYTES = 4096
MAX_CA_BYTES = 8192
MAX_PAYLOAD_BYTES = 2048
LOG_INTERVAL_MS = 30000
PUBLISH_RETRY_MS = 1000
CONFIG_FIELDS = ('enabled', 'host', 'port', 'server_hostname', 'ca_file',
                 'username', 'password', 'client_id', 'site_id', 'gateway_id',
                 'device_id', 'interval_seconds', 'broker_ip', 'keepalive', 'keepalive_seconds',
                 'ca_data', 'commands_enabled')


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value.encode()) <= maximum and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _identity(value):
    return (_text(value, 128) and all(
        'a' <= char <= 'z' or 'A' <= char <= 'Z' or '0' <= char <= '9' or
        char in '_-' for char in value))


def _host(value):
    if not _text(value, 253):
        return False
    for label in value.split('.'):
        if not 1 <= len(label) <= 63 or label[0] == '-' or label[-1] == '-':
            return False
        if not all('a' <= char <= 'z' or 'A' <= char <= 'Z' or
                   '0' <= char <= '9' or char == '-' for char in label):
            return False
    return True


def _ipv4(value):
    if not isinstance(value, str):
        return False
    parts = value.split('.')
    return (len(parts) == 4 and all(
        part and len(part) <= 3 and all('0' <= char <= '9' for char in part) and
        (part == '0' or part[0] != '0') and int(part) <= 255 for part in parts))


def _integer(value, minimum, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def validate_config(values):
    """Return a new validated config; errors contain field names, never values."""
    if type(values) is not dict or any(key not in CONFIG_FIELDS for key in values):
        raise ValueError('Invalid MQTT configuration fields')
    enabled = values.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('Invalid MQTT enabled flag')
    if type(values.get('commands_enabled', False)) is not bool:
        raise ValueError('Invalid MQTT commands_enabled flag')
    if not enabled:
        return {'enabled': False}
    result = dict(values)
    result['enabled'] = True
    result['commands_enabled'] = values.get('commands_enabled', False)
    result['port'] = values.get('port', 8883)
    if not _integer(result['port'], 1, 65535):
        raise ValueError('Invalid MQTT TLS port')
    result['server_hostname'] = values.get('server_hostname', values.get('host'))
    for name in ('host', 'server_hostname'):
        if not _host(result.get(name)):
            raise ValueError('Invalid MQTT ' + name)
    for name in ('client_id', 'site_id', 'gateway_id', 'device_id'):
        if not _identity(result.get(name)):
            raise ValueError('Invalid MQTT ' + name)
    for name in ('username', 'password'):
        if not _text(result.get(name), 256):
            raise ValueError('Invalid MQTT ' + name)
    if not _text(result.get('ca_file'), 256):
        raise ValueError('Invalid MQTT ca_file')
    result['interval_seconds'] = values.get('interval_seconds', 60)
    if not _integer(result['interval_seconds'], 60, 86400):
        raise ValueError('Invalid MQTT interval_seconds')
    if ('keepalive' in values and 'keepalive_seconds' in values and
            values['keepalive'] != values['keepalive_seconds']):
        raise ValueError('Conflicting MQTT keepalive fields')
    result['keepalive'] = values.get('keepalive', values.get('keepalive_seconds', 60))
    result.pop('keepalive_seconds', None)
    if not _integer(result['keepalive'], 15, 300):
        raise ValueError('Invalid MQTT keepalive')
    if result.get('broker_ip') is not None and not _ipv4(result['broker_ip']):
        raise ValueError('Invalid MQTT broker_ip')
    if 'ca_data' in result:
        ca = result['ca_data']
        if not isinstance(ca, bytes) or not 0 < len(ca) <= MAX_CA_BYTES or ca[0] != 0x30:
            raise ValueError('Invalid MQTT DER CA data')
    return result


def load_config(path='.mqtt.json'):
    """Read bounded private config and a single DER CA, or return None if off.

    A missing config is opt-out. Other file/JSON/CA errors fail closed and are
    reported without returning the input or a secret-bearing exception message.
    """
    raw, failed, missing = None, False, False
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        failed = True
        missing = bool(error.args) and error.args[0] == 2
    if missing:
        return None
    if failed or raw is None or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('MQTT configuration unavailable or too large')
    values, failed = None, False
    try:
        values = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, TypeError):
        failed = True
    if failed:
        raise ValueError('Invalid MQTT configuration')
    result = validate_config(values)
    if not result['enabled']:
        return None
    ca_file = result['ca_file']
    if ca_file[0] not in '/\\' and not (len(ca_file) > 1 and ca_file[1] == ':'):
        normalized_path = str(path).replace('\\', '/')
        if '/' in normalized_path:
            ca_file = normalized_path.rsplit('/', 1)[0] + '/' + ca_file
    result['ca_file'] = ca_file
    ca, failed = None, False
    try:
        with open(result['ca_file'], 'rb') as stream:
            ca = stream.read(MAX_CA_BYTES + 1)
    except OSError:
        failed = True
    if failed or not ca or len(ca) > MAX_CA_BYTES or ca[0] != 0x30:
        raise ValueError('MQTT DER CA unavailable or invalid')
    result['ca_data'] = ca
    return result


def _uuid4(random_bytes):
    value = bytearray(random_bytes(16))
    if len(value) != 16:
        raise ValueError('UUID entropy unavailable')
    value[6] = (value[6] & 15) | 64
    value[8] = (value[8] & 63) | 128
    text = binascii.hexlify(value).decode()
    return (text[:8] + '-' + text[8:12] + '-' + text[12:16] + '-' +
            text[16:20] + '-' + text[20:])


def _utc_parts_valid(parts):
    if len(parts) < 6:
        return False
    year, month, day, hour, minute, second = parts[:6]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in parts[:6]):
        return False
    if not 2020 <= year <= 9999 or not 1 <= month <= 12:
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return (1 <= day <= month_days[month - 1] and 0 <= hour < 24 and
            0 <= minute < 60 and 0 <= second < 60)


def _utc_stamp(clock):
    parts = clock.gmtime()
    if not _utc_parts_valid(parts):
        raise ValueError('UTC clock unavailable')
    return '%04d-%02d-%02dT%02d:%02d:%02dZ' % tuple(parts[:6])


def _utc_text_valid(value):
    if not isinstance(value, str) or len(value) not in (20, 22, 23, 24) or value[-1] != 'Z':
        return False
    if (value[4] != '-' or value[7] != '-' or value[10] != 'T' or
            value[13] != ':' or value[16] != ':'):
        return False
    if len(value) > 20 and (value[19] != '.' or not all('0' <= char <= '9' for char in value[20:-1])):
        return False
    digits = value[:4] + value[5:7] + value[8:10] + value[11:13] + value[14:16] + value[17:19]
    if not all('0' <= char <= '9' for char in digits):
        return False
    return _utc_parts_valid((int(value[:4]), int(value[5:7]), int(value[8:10]),
                             int(value[11:13]), int(value[14:16]), int(value[17:19])))


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        numeric = float(value)
        return numeric == numeric and numeric - numeric == 0
    except (OverflowError, ValueError):
        return False


def _temperature(esp32):
    raw = esp32.raw_temperature()
    # The original ESP32 API exposes integer Fahrenheit from its internal MCU
    # sensor. Do not turn unsupported/null/sentinel reads into a 0-C sample.
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 255:
        raise ValueError('MCU temperature unavailable')
    numerator = (raw - 32) * 50
    tenths = ((numerator + 4) // 9 if numerator >= 0 else -((-numerator + 4) // 9))
    return tenths / 10


class TemperatureTelemetry:
    def __init__(self, config, client, now_ms, time_module=None,
                 esp32_module=None, random_bytes=None, logger=None, sample_provider=None,
                 local_day=None):
        self.config = validate_config(config)
        self.client = client
        self._now = now_ms
        self._clock = time_module or time
        if sample_provider is not None and not callable(sample_provider):
            raise ValueError('Invalid MQTT sample provider')
        if local_day is not None and not callable(local_day):
            raise ValueError('Invalid MQTT local day provider')
        self._local_day = local_day
        self._daily_day = None
        self._daily_current_day = None
        self._daily_ack_count = 0
        self._sample_provider = sample_provider
        self._provider_last_sample = -1
        self._provider_last_read_at = -1
        self._provider_poll_at = 0
        if esp32_module is None and sample_provider is None:
            try:
                import esp32
                esp32_module = esp32
            except ImportError:
                pass
        self._esp32 = esp32_module
        self._random = random_bytes or os.urandom
        self._log = logger
        self._interval_ms = self.config.get('interval_seconds', 60) * 1000
        self._topic = ('iot/v1/' + self.config['site_id'] + '/' + self.config['gateway_id'] +
                       '/devices/' + self.config['device_id'] + '/telemetry') if self.config['enabled'] else None
        self._next_sample_at = self._now()
        self._last_publish_attempt = -PUBLISH_RETRY_MS
        self._last_log_at = {}
        self._pending = None
        self._accepted = False
        self._clock_ready = False
        self._read_count = 0
        self._ack_count = 0
        self._read_failures = 0
        self._transport_failures = 0
        self._last_read_at = None
        self._last_sample = None
        self._last_acknowledged = None

    def _refresh_daily(self):
        # Keep lifetime diagnostics and in-flight payloads independent of the
        # displayed day. A backward clock correction must not reset twice.
        try:
            day = self._local_day() if self._local_day is not None else None
        except Exception:
            day = None
        self._daily_current_day = day if type(day) is int and day >= 0 else None
        if self._daily_current_day is not None and (
                self._daily_day is None or self._daily_current_day > self._daily_day):
            self._daily_day = self._daily_current_day
            self._daily_ack_count = 0

    def _warn(self, category, message):
        now = self._now()
        if not self._log or now - self._last_log_at.get(category, -LOG_INTERVAL_MS) < LOG_INTERVAL_MS:
            return
        self._last_log_at[category] = now
        try:
            self._log('warn', message)
        except Exception:
            pass

    def _provider_observation(self, now):
        observation = self._sample_provider()
        if type(observation) is not dict:
            return None
        sequence, read_at = observation.get('sample'), observation.get('readAtMs')
        if (type(sequence) is not int or sequence < 0 or type(read_at) is not int or read_at < 0 or
                sequence <= self._provider_last_sample or read_at <= self._provider_last_read_at or
                read_at > now or now - read_at > 2 * self._interval_ms):
            return None
        stamp = observation.get('readTimeUtc')
        if not _utc_text_valid(stamp):
            return None
        metrics, source = observation.get('metrics'), observation.get('source')
        if (type(metrics) is not dict or type(source) is not dict or
                source.get('driver') not in ('rs485.ascii.READ', 'rs485.modbus.sht20')):
            return None
        temperature, humidity = metrics.get('temperature_c'), metrics.get('humidity_pct')
        if (not _finite(temperature) or not -40 <= temperature <= 125 or
                not _finite(humidity) or not 0 <= humidity <= 100):
            return None
        # Copy only known numeric metrics and fixed provenance. Provider-owned
        # latest observations may change while one original MQTT payload waits.
        copied = {'temperature_c': temperature, 'humidity_pct': humidity}
        driver = source['driver']
        if driver == 'rs485.modbus.sht20' and source.get('integrity') != 'crc_verified':
            return None
        origins = {'temperature_c': driver, 'humidity_pct': driver}
        cpu = metrics.get('cpu_temperature_c')
        if _finite(cpu):
            copied['cpu_temperature_c'] = cpu
            origins['cpu_temperature_c'] = 'esp32.raw_temperature'
        provenance = {'driver': driver, 'metric_sources': origins,
                      'integrity': 'crc_verified' if driver == 'rs485.modbus.sht20' else 'format_only'}
        return (copied, stamp, provenance, read_at, sequence)

    def prepare_reconfigure(self, config, client):
        """Validate and allocate before a durable configuration commit."""
        updated = validate_config(config)
        if not updated['enabled']:
            updated = dict(self.config)
            updated.update(config)
            updated['enabled'] = False
        if self._pending is not None and any(updated.get(key) != self.config.get(key) for key in
                                             ('site_id', 'gateway_id', 'device_id', 'client_id')):
            raise ValueError('A pending message prevents MQTT identity changes')
        topic = self._topic
        if updated['enabled']:
            topic = ('iot/v1/' + updated['site_id'] + '/' + updated['gateway_id'] +
                     '/devices/' + updated['device_id'] + '/telemetry')
        return updated, client, updated.get('interval_seconds', 60) * 1000, topic

    def adopt_reconfiguration(self, prepared):
        # All attributes already exist; adoption only moves references.
        self.config, self.client, self._interval_ms, self._topic = prepared

    def reconfigure(self, config, client):
        """Adopt a verified transport without replacing a pending observation."""
        self.adopt_reconfiguration(self.prepare_reconfigure(config, client))

    def _sample(self, now):
        if self._sample_provider is None:
            self._next_sample_at = now + self._interval_ms
        try:
            sequence = None
            if self._sample_provider is None:
                metrics = {'cpu_temperature_c': _temperature(self._esp32)}
                stamp, read_at = _utc_stamp(self._clock), now
                source = {'driver': 'esp32.raw_temperature', 'sensor_location': 'mcu_internal'}
            else:
                observation = self._provider_observation(now)
                if observation is None:
                    return
                metrics, stamp, source, read_at, sequence = observation
            envelope = {
                'schema_version': 2, 'message_id': _uuid4(self._random),
                'site_id': self.config['site_id'], 'gateway_id': self.config['gateway_id'],
                'device_id': self.config['device_id'], 'read_time_utc': stamp,
                'sample_time_utc': None, 'observation_kind': 'direct_read',
                'quality': 'ok', 'metrics': metrics, 'source': source,
            }
            payload = json.dumps(envelope).encode('utf-8')
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise ValueError('Telemetry envelope too large')
            summary = {
                'messageId': envelope['message_id'], 'readTimeUtc': stamp,
                'cpuTemperatureC': metrics.get('cpu_temperature_c'),
                'payloadSha256': binascii.hexlify(hashlib.sha256(payload).digest()).decode(),
            }
            if sequence is not None:
                summary['temperatureC'] = metrics['temperature_c']
                summary['relativeHumidityPercent'] = metrics['humidity_pct']
        except Exception:
            self._read_failures += 1
            self._warn('read', 'MQTT direct-read sample unavailable; no telemetry fabricated')
            return
        self._pending = payload
        self._accepted = False
        self._read_count += 1
        self._last_read_at = read_at
        self._last_sample = summary
        if sequence is not None:
            self._provider_last_sample, self._provider_last_read_at = sequence, read_at
            # A delayed latest observation retains its original read time, but
            # must not shorten the configured interval between new envelopes.
            self._next_sample_at = now + self._interval_ms

    def tick(self, connected, time_ready):
        self._refresh_daily()
        if not self.config['enabled']:
            return
        self._clock_ready = time_ready is True
        try:
            self.client.tick(connected, self._clock_ready)
            if self._pending is not None and self._accepted and self.client.take_ack():
                self._refresh_daily()
                # No newer read can replace this summary until its one pending
                # payload is acknowledged. A later read creates a fresh dict.
                self._last_acknowledged = self._last_sample
                self._pending = None
                self._accepted = False
                self._ack_count += 1
                if self._daily_current_day is not None and self._daily_current_day == self._daily_day:
                    self._daily_ack_count += 1
                self._provider_poll_at = 0
        except Exception:
            self._transport_failures += 1
            self._warn('transport', 'MQTT transport unavailable; pending sample retained')
        now = self._now()
        if self._pending is None and self._clock_ready:
            if self._sample_provider is not None:
                if now >= self._next_sample_at and now >= self._provider_poll_at:
                    self._provider_poll_at = now + PUBLISH_RETRY_MS
                    self._sample(now)
            elif now >= self._next_sample_at:
                self._sample(now)
        if self._pending is not None and not self._accepted and now - self._last_publish_attempt >= PUBLISH_RETRY_MS:
            self._last_publish_attempt = now
            try:
                self._accepted = self.client.publish(self._topic, self._pending) is True
            except Exception:
                self._transport_failures += 1
                self._warn('transport', 'MQTT transport unavailable; pending sample retained')

    def status(self):
        """Safe status only: no account, endpoint, topic, payload or device ID."""
        self._refresh_daily()
        transport = None
        if callable(getattr(self.client, 'status', None)):
            try:
                transport = self.client.status()
            except Exception:
                transport = {'phase': 'unavailable'}
        return {'enabled': self.config['enabled'], 'clockReady': self._clock_ready,
                'pending': self._pending is not None, 'accepted': self._accepted,
                'samples': self._read_count, 'acknowledged': self._ack_count,
                'dailyAcknowledged': self._daily_ack_count if self._daily_current_day is not None and
                self._daily_current_day == self._daily_day else None,
                'readFailures': self._read_failures, 'transportFailures': self._transport_failures,
                'lastReadAtMs': self._last_read_at,
                'nextSampleInMs': max(0, self._next_sample_at - self._now()),
                'lastSample': dict(self._last_sample) if self._last_sample is not None else None,
                'lastAcknowledged': dict(self._last_acknowledged) if self._last_acknowledged is not None else None,
                'transport': transport}

    def close(self):
        # A deliberate runtime stop closes the socket without manufacturing a
        # delivery acknowledgement or changing the retained sample's bytes.
        self.client.close()
