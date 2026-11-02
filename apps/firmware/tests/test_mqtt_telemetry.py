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
            {'broker_ip': '192.00.2.10'}, {'port': 0}, {'port': 65536}, {'port': True},
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

    def test_custom_tls_port_is_allowed_but_plaintext_or_verification_toggles_are_not(self):
        self.assertEqual(validate_config(configuration(port=8443))['port'], 8443)
        for values in ({'tls': False}, {'verify_certificate': False}, {'cert_reqs': 0}):
            with self.subTest(fields=tuple(values)), self.assertRaises(ValueError):
                validate_config(configuration(**values))


class TemperatureTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.client = Client()
        self.clock = types.SimpleNamespace(gmtime=Mock(return_value=(2026, 10, 30, 6, 30, 5, 4, 303)))
        self.esp32 = types.SimpleNamespace(raw_temperature=Mock(return_value=104))
        self.random = Mock(return_value=bytes(range(16)))
        self.logs = []
        self.sampler = self.make()

    def make(self, values=None, local_day=None):
        return TemperatureTelemetry(values or configuration(), self.client, lambda: self.now,
                                    time_module=self.clock, esp32_module=self.esp32,
                                    random_bytes=self.random,
                                    logger=lambda level, message: self.logs.append((level, message)),
                                    local_day=local_day)

    def payload(self):
        return json.loads(self.client.published[-1][1])

    def test_midnight_resets_display_count_and_counts_cross_day_puback_on_receipt_day(self):
        day = [10]
        self.sampler = self.make(local_day=lambda: day[0])
        self.sampler.tick(True, True)
        self.client.ack = True
        self.now = 1
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        self.now = 60000
        self.sampler.tick(True, True)
        pending = self.sampler._pending
        previous_summary = dict(self.sampler._last_sample)
        day[0] = 11
        self.sampler.tick(False, False)
        status = self.sampler.status()
        self.assertEqual(status['dailyAcknowledged'], 0)
        self.assertEqual(status['acknowledged'], 1)
        self.assertTrue(status['pending'])
        self.assertIs(self.sampler._pending, pending)
        self.assertEqual(self.sampler._last_sample, previous_summary)
        self.client.ack = True
        self.now = 60001
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        self.assertEqual(self.sampler.status()['acknowledged'], 2)
        self.assertEqual(self.sampler.status()['lastAcknowledged'], previous_summary)
        self.assertFalse(self.sampler.status()['pending'])

    def test_unknown_day_and_backward_clock_do_not_fabricate_or_reset_daily_count_twice(self):
        day = [None]
        self.sampler = self.make(local_day=lambda: day[0])
        self.assertIsNone(self.sampler.status()['dailyAcknowledged'])
        day[0] = 10
        self.sampler.tick(True, True)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        for value in (9, None, True, 10.0, -1):
            day[0] = value
            self.assertIsNone(self.sampler.status()['dailyAcknowledged'])
        day[0] = 10
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        day[0] = 12
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 0)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)

    def test_puback_processed_across_midnight_belongs_to_the_new_day(self):
        day = [10]
        self.sampler = self.make(local_day=lambda: day[0])
        self.sampler.tick(True, True)
        def tick_across_midnight(connected, ready):
            day[0] = 11
        self.client.tick = tick_across_midnight
        self.client.ack = True
        self.sampler.tick(True, True)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)

    def test_disabled_mqtt_still_rolls_the_day_without_touching_transport(self):
        day = [10]
        self.sampler = self.make(local_day=lambda: day[0])
        self.sampler.tick(True, True)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.sampler.reconfigure({'enabled': False}, self.client)
        calls = len(self.client.ticks)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        day[0] = 11
        self.sampler.tick(False, False)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 0)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)
        self.assertEqual(len(self.client.ticks), calls)

    def test_reconfigure_and_reenable_keep_today_count_but_restart_begins_at_zero(self):
        day = [10]
        self.sampler = self.make(local_day=lambda: day[0])
        self.sampler.tick(True, True)
        self.client.ack = True
        self.sampler.tick(True, True)
        replacement = Client()
        self.sampler.reconfigure(configuration(host='repair.example.invalid'), replacement)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        self.sampler.reconfigure({'enabled': False}, replacement)
        self.sampler.reconfigure(configuration(), replacement)
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)
        restarted = self.make(local_day=lambda: day[0])
        self.assertEqual(restarted.status()['dailyAcknowledged'], 0)
        self.assertEqual(restarted.status()['acknowledged'], 0)

    def test_day_provider_failure_does_not_drop_pending_or_erase_a_known_day(self):
        provider = Mock(return_value=10)
        self.sampler = self.make(local_day=provider)
        self.sampler.tick(True, True)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.now = 60000
        self.sampler.tick(True, True)
        pending = self.sampler._pending
        provider.side_effect = OSError('private-clock-detail')
        self.sampler.tick(False, False)
        self.assertIsNone(self.sampler.status()['dailyAcknowledged'])
        self.assertIs(self.sampler._pending, pending)
        self.assertEqual(self.sampler.status()['acknowledged'], 1)
        self.assertNotIn('private-clock-detail', repr(self.logs))
        provider.side_effect = None
        self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)

    def test_repeated_status_and_stray_puback_do_not_inflate_confirmed_count(self):
        self.sampler = self.make(local_day=lambda: 10)
        self.sampler.tick(True, True)
        self.client.ack = True
        self.sampler.tick(True, True)
        for _ in range(10):
            self.client.ack = True
            self.sampler.tick(True, False)
            self.assertEqual(self.sampler.status()['dailyAcknowledged'], 1)
            self.assertEqual(self.sampler.status()['acknowledged'], 1)

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


class ProviderTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.latest = None
        self.client = Client()
        self.provider = Mock(side_effect=lambda: self.latest)
        self.clock = types.SimpleNamespace(gmtime=Mock(side_effect=AssertionError('Do not replace provider UTC')))
        self.esp32 = types.SimpleNamespace(raw_temperature=Mock(side_effect=AssertionError('No CPU fallback')))
        self.random = Mock(side_effect=lambda size: bytes([self.random.call_count]) * size)
        self.logs = []
        self.sampler = TemperatureTelemetry(
            configuration(), self.client, lambda: self.now, time_module=self.clock,
            esp32_module=self.esp32, random_bytes=self.random,
            logger=lambda level, message: self.logs.append((level, message)), sample_provider=self.provider)

    def observation(self, sequence=1, read_at=None, **metrics):
        return {'sample': sequence, 'readAtMs': self.now if read_at is None else read_at,
                'readTimeUtc': '2026-10-31T01:02:03.125Z',
                'metrics': {'temperature_c': 22.3, 'humidity_pct': 55.4, **metrics},
                'source': {'driver': 'rs485.ascii.READ'}}

    def payload(self):
        return json.loads(self.client.published[-1][1])

    def test_provider_preserves_original_read_time_and_fixed_per_metric_sources(self):
        self.now = 5000
        self.latest = self.observation(read_at=1000, cpu_temperature_c=49.4, ignored_private='private-id')
        self.latest['source']['device_id'] = 'private-id'
        self.sampler.tick(True, True)
        sample = self.payload()
        self.assertEqual(sample['metrics'], {'temperature_c': 22.3, 'humidity_pct': 55.4, 'cpu_temperature_c': 49.4})
        self.assertEqual(sample['read_time_utc'], '2026-10-31T01:02:03.125Z')
        self.assertIsNone(sample['sample_time_utc'])
        self.assertEqual(sample['observation_kind'], 'direct_read')
        self.assertEqual(sample['source'], {'driver': 'rs485.ascii.READ', 'integrity': 'format_only', 'metric_sources': {
            'temperature_c': 'rs485.ascii.READ', 'humidity_pct': 'rs485.ascii.READ',
            'cpu_temperature_c': 'esp32.raw_temperature'}})
        self.assertNotIn('private-id', self.client.published[0][1].decode())
        summary = self.sampler.status()['lastSample']
        self.assertEqual(summary['temperatureC'], 22.3)
        self.assertEqual(summary['relativeHumidityPercent'], 55.4)
        self.assertEqual(summary['cpuTemperatureC'], 49.4)
        self.assertEqual(summary['payloadSha256'], hashlib.sha256(self.client.published[0][1]).hexdigest())
        self.assertEqual(self.sampler.status()['lastReadAtMs'], 1000)
        self.clock.gmtime.assert_not_called()
        self.esp32.raw_temperature.assert_not_called()

    def test_none_and_ntp_gate_are_quiet_and_provider_polls_are_bounded(self):
        self.sampler.tick(True, False)
        self.provider.assert_not_called()
        for self.now in (0, 1, 100, 999, 1000):
            self.sampler.tick(False, True)
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.sampler.status()['readFailures'], 0)
        self.assertEqual(self.logs, [])
        self.esp32.raw_temperature.assert_not_called()
        self.clock.gmtime.assert_not_called()

    def test_invalid_environment_values_are_omitted_without_zero_or_cpu_fallback(self):
        invalid = [
            {}, {'temperature_c': 20}, {'humidity_pct': 50},
            {'temperature_c': 20, 'humidity_percent': 50},
        ]
        for key, values in (('temperature_c', (-40.1, 125.1, True, None, '20', float('nan'), float('inf'))),
                            ('humidity_pct', (-0.1, 100.1, True, None, '50', float('nan'), float('-inf')))):
            for value in values:
                metrics = {'temperature_c': 20, 'humidity_pct': 50}
                metrics[key] = value
                invalid.append(metrics)
        for index, metrics in enumerate(invalid):
            self.now = index * 1000
            self.latest = self.observation(sequence=index)
            self.latest['metrics'] = metrics
            self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.logs, [])
        self.assertEqual(self.sampler.status()['samples'], 0)
        self.esp32.raw_temperature.assert_not_called()

    def test_invalid_metadata_utc_and_stale_observations_do_not_get_new_timestamps(self):
        changes = [
            {'sample': True}, {'sample': -1}, {'sample': 1.0}, {'readAtMs': True},
            {'readAtMs': -1}, {'readAtMs': 0.0}, {'readTimeUtc': None},
            {'readTimeUtc': '2026-02-30T01:02:03Z'}, {'readTimeUtc': '2026-10-31T01:02:03+08:00'},
            {'readTimeUtc': '2026-10-31T01:02:03.1234Z'},
            {'source': {'driver': 'xiaomi-gateway-v3-temperature'}}, {'source': None},
        ]
        for index, change in enumerate(changes):
            self.now = 200000 + index * 1000
            self.latest = self.observation(sequence=index)
            self.latest.update(change)
            self.sampler.tick(True, True)
        for index, age in enumerate((-1, 120001)):
            self.now += 1000
            self.latest = self.observation(sequence=50 + index, read_at=self.now - age)
            self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.sampler.status()['readFailures'], 0)
        self.random.assert_not_called()
        self.clock.gmtime.assert_not_called()

    def test_sequence_and_read_uptime_each_prevent_duplicate_cached_observations(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(True, True)
        self.client.ack = True
        self.now = 10
        self.sampler.tick(True, True)
        for self.now, sequence, read_at in ((60000, 2, 0), (61000, 1, 61000), (62000, 0, 62000)):
            self.latest = self.observation(sequence=sequence, read_at=read_at)
            self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)
        self.assertEqual(self.random.call_count, 1)
        self.now = 63000
        self.latest = self.observation(sequence=2, read_at=63000)
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)

    def test_five_second_observations_publish_latest_only_at_configured_interval(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(True, True)
        self.client.ack = True
        self.now = 10
        self.sampler.tick(True, True)
        for self.now in range(5000, 60000, 5000):
            self.latest = self.observation(sequence=1 + self.now // 5000,
                                           temperature_c=20 + self.now / 10000)
            self.latest['readTimeUtc'] = '2026-10-31T01:02:%02d.125Z' % (self.now // 1000)
            self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)
        self.assertEqual(self.provider.call_count, 1)
        latest_stamp = self.latest['readTimeUtc']
        self.now = 60000
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.payload()['metrics']['temperature_c'], 25.5)
        self.assertEqual(self.payload()['read_time_utc'], latest_stamp)
        self.assertEqual(self.sampler.status()['lastReadAtMs'], 55000)
        self.assertEqual(self.sampler.status()['nextSampleInMs'], 60000)
        self.clock.gmtime.assert_not_called()

    def test_delayed_initial_observation_does_not_shorten_next_publish_interval(self):
        self.now = 50000
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(True, True)
        first_stamp = self.payload()['read_time_utc']
        self.client.ack = True
        self.now = 60000
        self.latest = self.observation(sequence=2, read_at=60000, temperature_c=24)
        self.latest['readTimeUtc'] = '2026-10-31T01:03:03.125Z'
        self.sampler.tick(True, True)
        self.now = 109999
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.sampler.status()['lastAcknowledged']['readTimeUtc'], first_stamp)
        self.now = 110000
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)
        self.assertEqual(self.payload()['read_time_utc'], '2026-10-31T01:03:03.125Z')
        self.assertEqual(self.payload()['metrics']['temperature_c'], 24)
        self.assertEqual(self.sampler.status()['lastReadAtMs'], 60000)
        self.clock.gmtime.assert_not_called()

    def test_pending_is_immutable_and_ack_admits_only_the_current_latest_read(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(False, True)
        first_wire = self.client.published[0][1]
        first_summary = self.sampler.status()['lastSample']
        for self.now, sequence in ((60000, 2), (120000, 3)):
            self.latest = self.observation(sequence=sequence, temperature_c=sequence * 10, humidity_pct=sequence * 20)
            self.sampler.tick(False, True)
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(len(self.client.published), 1)
        self.assertIs(self.client.published[0][1], first_wire)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)
        latest = self.payload()
        self.assertEqual(latest['metrics'], {'temperature_c': 30, 'humidity_pct': 60})
        self.assertEqual(self.sampler.status()['lastReadAtMs'], 120000)
        self.assertEqual(self.sampler.status()['lastAcknowledged'], first_summary)
        self.assertNotEqual(latest['message_id'], json.loads(first_wire)['message_id'])
        second_wire = self.client.published[1][1]
        self.latest['metrics']['temperature_c'] = 99
        self.latest['source']['driver'] = 'private-replacement'
        self.now = 121000
        self.sampler.tick(True, True)
        self.assertIs(self.client.published[1][1], second_wire)
        self.assertEqual(json.loads(second_wire)['metrics']['temperature_c'], 30)
        self.assertEqual(self.sampler.status()['lastSample']['temperatureC'], 30)

    def test_unaccepted_publish_retries_original_bytes_even_if_latest_changes(self):
        self.client.allow_publish = False
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(False, True)
        original = self.client.published[0][1]
        self.now = 180000
        self.latest = self.observation(sequence=4, read_at=180000, temperature_c=44)
        self.sampler.tick(False, True)
        self.client.allow_publish = True
        self.now = 181000
        self.sampler.tick(True, True)
        for _, payload in self.client.published:
            self.assertIs(payload, original)
        self.assertEqual(json.loads(original)['metrics']['temperature_c'], 22.3)
        self.assertEqual(self.provider.call_count, 1)

    def test_old_latest_is_skipped_after_ack_and_fresh_latest_can_follow(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(True, True)
        self.now = 180001
        self.latest = self.observation(sequence=2, read_at=60000)
        self.client.ack = True
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 1)
        self.now = 181001
        self.latest = self.observation(sequence=3, read_at=181000)
        self.sampler.tick(True, True)
        self.assertEqual(len(self.client.published), 2)

    def test_optional_invalid_cpu_is_omitted_but_real_environment_zeroes_are_kept(self):
        for index, cpu in enumerate((None, True, 'unavailable', float('nan'), float('inf'), 10 ** 400)):
            self.now = index * 60000
            self.latest = self.observation(sequence=index, temperature_c=0, humidity_pct=0, cpu_temperature_c=cpu)
            if index:
                self.client.ack = True
            self.sampler.tick(True, True)
            self.assertEqual(self.payload()['metrics'], {'temperature_c': 0, 'humidity_pct': 0})
            self.assertIsNone(self.sampler.status()['lastSample']['cpuTemperatureC'])
            self.assertNotIn('cpu_temperature_c', self.payload()['source']['metric_sources'])
        self.esp32.raw_temperature.assert_not_called()

    def test_provider_error_is_safe_and_does_not_re_read_cpu_or_generate_zero(self):
        self.provider.side_effect = OSError('private-source-address')
        self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.sampler.status()['readFailures'], 1)
        self.assertNotIn('private-source-address', repr(self.logs))
        self.esp32.raw_temperature.assert_not_called()
        self.clock.gmtime.assert_not_called()

    def test_rtu_observation_requires_verified_crc_and_keeps_driver_provenance(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.latest['source'] = {'driver': 'rs485.modbus.sht20'}
        self.sampler.tick(True, True)
        self.assertEqual(self.client.published, [])
        self.latest['source']['integrity'] = 'crc_verified'
        self.now = 1000
        self.sampler.tick(True, True)
        source = self.payload()['source']
        self.assertEqual(source['driver'], 'rs485.modbus.sht20')
        self.assertEqual(source['integrity'], 'crc_verified')
        self.assertEqual(source['metric_sources']['temperature_c'], 'rs485.modbus.sht20')
        self.esp32.raw_temperature.assert_not_called()

    def test_reconfiguration_keeps_original_pending_bytes_and_provider_deduplication(self):
        self.latest = self.observation(sequence=7, read_at=0)
        self.sampler.tick(True, True)
        original = self.sampler._pending
        accepted = self.sampler._accepted
        latest_id = self.sampler.status()['lastSample']['messageId']
        replacement = Client()
        self.sampler.reconfigure(configuration(host='repair.example.invalid'), replacement)
        self.assertIs(self.sampler._pending, original)
        self.assertEqual(self.sampler._accepted, accepted)
        self.assertEqual(self.sampler._provider_last_sample, 7)
        self.assertEqual(self.sampler.status()['lastSample']['messageId'], latest_id)
        self.sampler.reconfigure({'enabled': False}, replacement)
        self.assertEqual(self.sampler.config['site_id'], 'test-site')
        self.assertIs(self.sampler._pending, original)
        self.sampler.reconfigure(configuration(), replacement)
        self.assertIs(self.sampler._pending, original)

    def test_pending_message_blocks_identity_change_before_mutating_publisher(self):
        self.latest = self.observation(sequence=1, read_at=0)
        self.sampler.tick(True, True)
        config, client, pending = self.sampler.config, self.sampler.client, self.sampler._pending
        with self.assertRaises(ValueError):
            self.sampler.prepare_reconfigure(configuration(site_id='different-site'), Client())
        self.assertIs(self.sampler.config, config)
        self.assertIs(self.sampler.client, client)
        self.assertIs(self.sampler._pending, pending)


if __name__ == '__main__':
    unittest.main()
