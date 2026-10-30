"""Synthetic private config, direct reads and bounded QoS 1 scheduling."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_telemetry import TemperatureTelemetry, load_config, validate_config


def configuration(**changes):
    values = {
        'enabled': True, 'host': 'mqtt.example.invalid', 'port': 8883,
        'server_hostname': 'mqtt.example.invalid', 'ca_file': '.mqtt-ca.der',
        'username': 'example-account', 'password': 'example-test-password',
        'client_id': 'test-client', 'site_id': 'test-site', 'gateway_id': 'test-gateway',
        'device_id': 'test-esp32', 'interval_seconds': 60, 'keepalive': 60,
    }
    values.update(changes)
    return values


class Client:
    def __init__(self):
        self.ticks = []
        self.published = []
        self.allow_publish = True
        self.fail_publish = False
        self.fail_tick = False
        self.ack = False
        self.closed = False

    def tick(self, connected, time_ready):
        self.ticks.append((connected, time_ready))
        if self.fail_tick:
            raise OSError('a synthetic transport error with private input')

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        if self.fail_publish:
            raise OSError('a synthetic transport error with private input')
        return self.allow_publish

    def take_ack(self):
        value, self.ack = self.ack, False
        return value

    def close(self):
        self.closed = True


class MqttConfigTests(unittest.TestCase):
    def test_missing_and_disabled_config_are_opt_out_without_reading_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.mqtt.json'
            self.assertIsNone(load_config(str(path)))
            path.write_text('{"enabled":false}')
            self.assertIsNone(load_config(str(path)))
        self.assertEqual(validate_config({}), {'enabled': False})

    def test_relative_ca_is_loaded_from_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.mqtt.json'
            ca = b'\x30synthetic-DER-test-bytes'
            (path.parent / '.mqtt-ca.der').write_bytes(ca)
            original = configuration()
            path.write_text(json.dumps(original))
            result = load_config(str(path))
            self.assertEqual(result['ca_data'], ca)
            self.assertEqual(Path(result['ca_file']), path.parent / '.mqtt-ca.der')
            self.assertNotIn('ca_data', original)

    def test_malformed_and_oversized_config_fail_without_echoing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.mqtt.json'
            for data in (b'{"password":"private-test-value",bad}', b'x' * 4097, b'\xff'):
                with self.subTest(length=len(data)):
                    path.write_bytes(data)
                    with self.assertRaises(ValueError) as failure:
                        load_config(str(path))
                    self.assertNotIn('private-test-value', str(failure.exception))

    def test_missing_pem_or_oversized_ca_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.mqtt.json'
            path.write_text(json.dumps(configuration()))
            for ca in (None, b'-----BEGIN CERTIFICATE-----', b'\x30' + b'x' * 8192):
                with self.subTest(ca_missing=ca is None):
                    if ca is not None:
                        (path.parent / '.mqtt-ca.der').write_bytes(ca)
                    with self.assertRaisesRegex(ValueError, 'MQTT DER CA'):
                        load_config(str(path))

    def test_identifiers_hosts_and_secret_fields_are_validated(self):
        changes = [
            {'site_id': 'site/other'}, {'device_id': 'wildcard+'}, {'gateway_id': 'x' * 129},
            {'site_id': '网络'}, {'host': 'mqtt://example.invalid'}, {'host': '-bad.invalid'},
            {'server_hostname': 'other.invalid/path'}, {'username': ''},
            {'password': 'private-test-value\n'}, {'ca_file': ''}, {'enabled': 'true'},
            {'unknown': 'private-test-value'}, {'broker_ip': '192.0.2.999'},
            {'broker_ip': '192.00.2.10'}, {'port': 1883}, {'port': True},
        ]
        for change in changes:
            with self.subTest(fields=list(change)):
                with self.assertRaises(ValueError) as failure:
                    validate_config(configuration(**change))
                self.assertNotIn('private-test-value', str(failure.exception))

    def test_interval_and_keepalive_are_bounded_integers(self):
        for value in (0, 1, 59, 86401, True, 60.0, '60'):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                validate_config(configuration(interval_seconds=value))
        for value in (0, 14, 301, True, 60.0, '60'):
            with self.subTest(keepalive=value), self.assertRaises(ValueError):
                validate_config(configuration(keepalive=value))
        value = configuration()
        value.pop('keepalive')
        value['keepalive_seconds'] = 90
        self.assertEqual(validate_config(value)['keepalive'], 90)
        self.assertNotIn('keepalive_seconds', validate_config(value))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            validate_config(configuration(keepalive_seconds=90))

    def test_defaults_and_numeric_endpoint_preserve_hostname_verification(self):
        values = configuration(broker_ip='192.0.2.9')
        values.pop('port')
        values.pop('server_hostname')
        values.pop('interval_seconds')
        values.pop('keepalive')
        result = validate_config(values)
        self.assertEqual(result['port'], 8883)
        self.assertEqual(result['server_hostname'], values['host'])
        self.assertEqual(result['broker_ip'], '192.0.2.9')
        self.assertEqual(result['interval_seconds'], 60)
        self.assertEqual(result['keepalive'], 60)


class TemperatureTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.client = Client()
        self.clock = types.SimpleNamespace(gmtime=Mock(return_value=(2026, 10, 30, 6, 30, 5, 4, 303)))
        self.esp32 = types.SimpleNamespace(raw_temperature=Mock(return_value=104))
        self.random = Mock(return_value=bytes(range(16)))
        self.logs = []
        self.sampler = self.make()

    def make(self, values=None):
        return TemperatureTelemetry(values or configuration(), self.client, lambda: self.now,
                                    time_module=self.clock, esp32_module=self.esp32,
                                    random_bytes=self.random,
                                    logger=lambda level, message: self.logs.append((level, message)))

    def payload(self):
        return json.loads(self.client.published[-1][1])

    def test_schema_v2_uses_actual_utc_and_explicit_internal_temperature(self):
        self.sampler.tick(True, True)
        topic, wire = self.client.published[0]
        sample = self.payload()
        self.assertIsInstance(wire, bytes)
        self.assertEqual(topic, 'iot/v1/test-site/test-gateway/devices/test-esp32/telemetry')
        self.assertEqual(sample, {
            'schema_version': 2, 'message_id': '00010203-0405-4607-8809-0a0b0c0d0e0f',
            'site_id': 'test-site', 'gateway_id': 'test-gateway', 'device_id': 'test-esp32',
            'read_time_utc': '2026-10-30T06:30:05Z', 'sample_time_utc': None,
            'observation_kind': 'direct_read', 'quality': 'ok',
            'metrics': {'cpu_temperature_c': 40.0},
            'source': {'driver': 'esp32.raw_temperature', 'sensor_location': 'mcu_internal'},
        })
        self.assertEqual(uuid.UUID(sample['message_id']).version, 4)
        self.assertEqual(uuid.UUID(sample['message_id']).variant, uuid.RFC_4122)
        self.assertNotIn('captured_at', sample)
        self.assertNotIn('example-test-password', wire.decode())
        self.assertNotIn('example-account', wire.decode())

    def test_no_sample_without_a_valid_ntp_gate(self):
        for self.now in (0, 1, 60000, 3600000):
            self.sampler.tick(True, False)
        self.esp32.raw_temperature.assert_not_called()
        self.clock.gmtime.assert_not_called()
        self.random.assert_not_called()
        self.assertEqual(self.client.published, [])
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)

    def test_invalid_rtc_does_not_fabricate_a_timestamp_or_zero_reading(self):
        invalid = [(2000, 1, 1, 0, 0, 0), (2026, 2, 29, 0, 0, 0),
                   (2026, 13, 1, 0, 0, 0), (2026, 1, 1, 24, 0, 0),
                   (2026, 1, 1, 0, 60, 0), (2026, 1, 1, 0, 0, 60),
                   (2026, 1, 1, 0, 0, '0')]
        for index, parts in enumerate(invalid):
            self.now = index * 60000
            self.clock.gmtime.return_value = parts
            self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.sampler.status()['readFailures'], len(invalid))
        self.random.assert_not_called()

    def test_valid_leap_day_uses_utc_fields_without_local_time_conversion(self):
        self.clock.gmtime.return_value = (2028, 2, 29, 0, 1, 2, 1, 60)
        self.sampler.tick(True, True)
        self.assertEqual(self.payload()['read_time_utc'], '2028-02-29T00:01:02Z')
        self.clock.gmtime.assert_called_once_with()

    def test_invalid_or_failed_sensor_read_is_omitted_and_retry_is_interval_limited(self):
        for index, raw in enumerate((None, True, 40.0, -1, 256, float('nan'))):
            self.now = index * 60000
            self.esp32.raw_temperature.return_value = raw
            self.sampler.tick(True, True)
            for offset in (1, 10, 100, 1000, 59999):
                self.now = index * 60000 + offset
                self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.esp32.raw_temperature.call_count, 6)
        self.assertEqual(self.sampler.status()['readFailures'], 6)
        self.now = 6 * 60000
        self.esp32.raw_temperature.side_effect = OSError('synthetic-private-password')
        self.sampler.tick(True, True)
        self.assertNotIn('synthetic-private-password', repr(self.logs))
        self.assertEqual(self.client.published, [])

    def test_real_zero_and_negative_celsius_are_valid_not_missing_sentinels(self):
        self.esp32.raw_temperature.return_value = 32
        self.sampler.tick(True, True)
        self.assertEqual(self.payload()['metrics']['cpu_temperature_c'], 0.0)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.now = 60000
        self.esp32.raw_temperature.return_value = 0
        self.sampler.tick(True, True)
        self.assertEqual(self.payload()['metrics']['cpu_temperature_c'], -17.8)

    def test_no_new_sample_before_sixty_seconds_or_before_matching_puback(self):
        self.sampler.tick(True, True)
        original = self.client.published[0][1]
        for self.now in (1, 59999, 60000, 120000):
            self.sampler.tick(True, True)
        self.esp32.raw_temperature.assert_called_once()
        self.assertEqual(len(self.client.published), 1)
        self.assertIs(self.client.published[0][1], original)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)
        self.assertEqual(self.esp32.raw_temperature.call_count, 2)
        self.assertEqual(len(self.client.published), 2)
        self.assertEqual(self.sampler.status()['nextSampleInMs'], 60000)

    def test_unaccepted_or_failed_publish_retries_identical_message_id_and_bytes(self):
        self.client.allow_publish = False
        self.sampler.tick(False, True)
        original = self.client.published[0][1]
        self.esp32.raw_temperature.return_value = 212
        self.clock.gmtime.return_value = (2026, 10, 31, 0, 0, 0)
        self.now = 999
        self.sampler.tick(False, True)
        self.assertEqual(len(self.client.published), 1)
        self.now = 1000
        self.client.fail_publish = True
        self.sampler.tick(False, True)
        self.now = 2000
        self.client.fail_publish = False
        self.client.allow_publish = True
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 3)
        for _, value in self.client.published:
            self.assertIs(value, original)
        self.esp32.raw_temperature.assert_called_once()
        self.assertEqual(self.payload()['metrics']['cpu_temperature_c'], 40.0)
        self.assertEqual(self.payload()['read_time_utc'], '2026-10-30T06:30:05Z')
        self.assertNotIn('private input', repr(self.logs))

    def test_accepted_pending_survives_network_and_clock_loss_without_new_reads(self):
        self.sampler.tick(True, True)
        for self.now in (5000, 60000, 3600000):
            self.sampler.tick(False, False)
        self.assertEqual(len(self.client.published), 1)
        self.assertTrue(self.sampler.status()['pending'])
        self.esp32.raw_temperature.assert_called_once()
        self.client.ack = True
        self.sampler.tick(True, False)
        self.assertFalse(self.sampler.status()['pending'])
        self.assertEqual(len(self.client.published), 1)

    def test_debug_is_not_an_input_and_sampling_continues_after_each_ack(self):
        self.sampler.tick(True, True)
        self.client.ack = True
        self.now = 10
        self.sampler.tick(True, True)
        self.now = 59999
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)
        self.now = 60000
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)
        self.assertEqual(self.esp32.raw_temperature.call_count, 2)

    def test_new_direct_reads_receive_distinct_uuid4_ids(self):
        self.random.side_effect = (b'\x00' * 16, b'\xff' * 16)
        self.sampler.tick(True, True)
        first = self.payload()['message_id']
        self.client.ack = True
        self.now = 60000
        self.sampler.tick(True, True)
        second = self.payload()['message_id']
        self.assertNotEqual(first, second)
        for value in (first, second):
            self.assertEqual(uuid.UUID(value).version, 4)
            self.assertEqual(uuid.UUID(value).variant, uuid.RFC_4122)

    def test_disabled_sampler_does_not_touch_hardware_or_transport(self):
        sampler = self.make({'enabled': False})
        sampler.tick(True, True)
        self.assertEqual(self.client.ticks, [])
        self.assertEqual(self.client.published, [])
        self.esp32.raw_temperature.assert_not_called()

    def test_status_and_failures_never_echo_private_inputs_and_close_preserves_pending(self):
        self.client.fail_tick = True
        self.sampler.tick(True, True)
        self.sampler.close()
        self.assertTrue(self.client.closed)
        self.assertTrue(self.sampler.status()['pending'])
        safe = repr(self.sampler.status()) + repr(self.logs)
        for value in ('example-account', 'example-test-password', 'test-client',
                      'test-site', 'test-esp32', 'mqtt.example.invalid', 'private input'):
            self.assertNotIn(value, safe)
        self.assertEqual(self.sampler.status()['transportFailures'], 1)

    def test_bad_entropy_does_not_create_predictable_or_invalid_message_ids(self):
        self.random.return_value = b'too-short'
        self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.sampler.status()['readFailures'], 1)

    def test_transport_status_is_exposed_without_echoing_status_exception(self):
        self.client.status = Mock(return_value={'phase': 'backoff', 'lastError': 'tls_or_protocol_failed'})
        self.assertEqual(self.sampler.status()['transport']['phase'], 'backoff')
        self.client.status.side_effect = ValueError('private input')
        self.assertEqual(self.sampler.status()['transport'], {'phase': 'unavailable'})
        self.assertNotIn('private input', repr(self.sampler.status()))

    def test_sample_and_ack_summaries_verify_original_bytes_after_payload_release(self):
        self.assertIsNone(self.sampler.status()['lastSample'])
        self.assertIsNone(self.sampler.status()['lastAcknowledged'])
        self.sampler.tick(True, True)
        payload = self.client.published[0][1]
        envelope = json.loads(payload)
        expected = {
            'messageId': envelope['message_id'], 'readTimeUtc': envelope['read_time_utc'],
            'cpuTemperatureC': 40.0, 'payloadSha256': hashlib.sha256(payload).hexdigest(),
        }
        self.assertEqual(self.sampler.status()['lastSample'], expected)
        self.assertIsNone(self.sampler.status()['lastAcknowledged'])
        self.client.ack = True
        self.now = 10
        self.sampler.tick(True, True)
        status = self.sampler.status()
        self.assertFalse(status['pending'])
        self.assertEqual(status['lastSample'], expected)
        self.assertEqual(status['lastAcknowledged'], expected)
        for summary in (status['lastSample'], status['lastAcknowledged']):
            self.assertEqual(set(summary), {'messageId', 'readTimeUtc', 'cpuTemperatureC', 'payloadSha256'})
        safe = repr(status)
        for private in ('test-site', 'test-gateway', 'test-esp32', 'test-client',
                        'example-account', 'example-test-password', 'iot/v1/'):
            self.assertNotIn(private, safe)

    def test_status_summary_copies_cannot_mutate_retained_evidence_or_payload(self):
        self.sampler.tick(True, True)
        payload = self.client.published[0][1]
        original = self.sampler.status()['lastSample']
        modified = self.sampler.status()
        modified['lastSample']['messageId'] = 'tampered'
        modified['lastSample']['payloadSha256'] = 'tampered'
        self.assertEqual(self.sampler.status()['lastSample'], original)
        self.client.ack = True
        self.sampler.tick(True, True)
        modified = self.sampler.status()
        modified['lastAcknowledged']['cpuTemperatureC'] = -999
        modified['lastAcknowledged']['readTimeUtc'] = 'tampered'
        self.assertEqual(self.sampler.status()['lastAcknowledged'], original)
        self.assertEqual(json.loads(payload)['message_id'], original['messageId'])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), original['payloadSha256'])

    def test_new_sample_keeps_previous_ack_summary_until_its_own_ack(self):
        self.random.side_effect = (b'\x00' * 16, b'\xff' * 16)
        self.sampler.tick(True, True)
        first = self.sampler.status()['lastSample']
        self.client.ack = True
        self.now = 10
        self.sampler.tick(True, True)
        self.now = 60000
        self.clock.gmtime.return_value = (2026, 10, 30, 6, 31, 5, 4, 303)
        self.esp32.raw_temperature.return_value = 212
        self.sampler.tick(True, True)
        second = self.sampler.status()['lastSample']
        self.assertNotEqual(first['messageId'], second['messageId'])
        self.assertNotEqual(first['payloadSha256'], second['payloadSha256'])
        self.assertEqual(second['cpuTemperatureC'], 100.0)
        self.assertEqual(second['readTimeUtc'], '2026-10-30T06:31:05Z')
        self.assertEqual(self.sampler.status()['lastAcknowledged'], first)
        self.client.ack = True
        self.now = 60001
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['lastAcknowledged'], second)
        self.assertEqual(first['cpuTemperatureC'], 40.0)

    def test_failed_reads_and_publish_retries_do_not_replace_summary(self):
        self.client.allow_publish = False
        self.sampler.tick(False, True)
        first = self.sampler.status()['lastSample']
        original_bytes = self.client.published[0][1]
        self.client.fail_publish = True
        self.now = 1000
        self.sampler.tick(False, True)
        self.assertEqual(self.sampler.status()['lastSample'], first)
        self.client.fail_publish = False
        self.client.allow_publish = True
        self.now = 2000
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['lastSample'], first)
        self.assertIsNone(self.sampler.status()['lastAcknowledged'])
        for _, payload in self.client.published:
            self.assertIs(payload, original_bytes)
        self.client.ack = True
        self.now = 3000
        self.sampler.tick(True, True)
        self.esp32.raw_temperature.return_value = None
        self.now = 60000
        self.sampler.tick(True, True)
        status = self.sampler.status()
        self.assertEqual(status['lastSample'], first)
        self.assertEqual(status['lastAcknowledged'], first)
        self.assertEqual(status['readFailures'], 1)
        self.assertEqual(status['samples'], 1)
        self.assertFalse(status['pending'])


if __name__ == '__main__':
    unittest.main()
