"""Business command trials without a board, broker, or secret-bearing output."""
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import test_runtime as runtime_fixture
from test_device_settings import mqtt_values
from components.device_settings import SettingsStore, SENSOR_DEFAULTS
from components.mqtt_telemetry import TemperatureTelemetry


class TrialTransport:
    def __init__(self, connect=True, command_state='disabled'):
        self.connected = False
        self.should_connect = connect
        self.command_state = command_state
        self.closed = False
        self.released = False
        self._pending = None
        self.ticks = 0
        self.published = []
        self.commands_active = False

    def tick(self, network, clock):
        self.ticks += 1
        self.connected = self.should_connect and network and clock

    def status(self):
        return {'connected': self.connected, 'commands': {'state': self.command_state}}

    def close(self):
        self.closed = True
        self.connected = False

    def release_context(self):
        if not self.closed:
            raise AssertionError('Close the old connection before releasing its TLS context')
        self.released = True

    def prepare_pending_transfer_from(self, previous):
        if not self.connected or not previous.closed:
            raise AssertionError('A candidate must connect after the old socket closes')
        return previous._pending

    def transfer_pending_from(self, previous, prepared=None):
        self._pending, previous._pending = previous._pending, None

    def set_commands_active(self, active):
        self.commands_active = active

    def take_ack(self):
        return False

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        return True


class RuntimeBusinessSettingsTests(unittest.TestCase):
    setUp = runtime_fixture.RuntimeTests.setUp
    command = runtime_fixture.RuntimeTests.command
    sensor_reader = runtime_fixture.RuntimeTests.sensor_reader
    enable_sensor = runtime_fixture.RuntimeTests.enable_sensor

    def acknowledgements(self):
        return [json.loads(line[9:])['data'] for line in self.output.getvalue().splitlines()
                if line.startswith('@one-esp ') and json.loads(line[9:]).get('type') == 'ack']

    def mqtt_store_and_publisher(self, pending=True):
        config = mqtt_values()
        path = Path(self.runtime._mqtt_config_path)
        path.write_text(json.dumps(config))
        (path.parent / '.mqtt-ca.der').write_bytes(b'\x30synthetic-DER-test')
        store = SettingsStore(str(path), 'mqtt')
        self.runtime._settings_stores['mqtt'] = store
        previous = TrialTransport()
        previous.connected = True
        publisher = TemperatureTelemetry(config, previous, self.runtime.service.now,
                                         esp32_module=types.SimpleNamespace(raw_temperature=lambda: 100))
        if pending:
            original = b'{"original_observation":"synthetic"}'
            publisher._pending = original
            publisher._accepted = True
            previous._pending = ['test-topic', original, 17, True]
            publisher._provider_last_sample = 71
            publisher._provider_last_read_at = 1000
        self.runtime.mqtt = publisher
        self.runtime._mqtt_enabled = True
        return store, publisher, previous

    def test_usb_business_settings_are_usable_with_debug_off_but_sets_require_current_boot(self):
        self.command('sensor.config.get', identifier='read-sensor')
        self.assertFalse(self.runtime.service.enabled)
        self.assertEqual(self.acknowledgements()[-1]['result']['values']['protocol'], 'ascii_read')
        self.command('sensor.config.set', {'revision': 0, 'values': {'enabled': False}}, 'disable-sensor')
        self.assertTrue(self.acknowledgements()[-1]['ok'])
        self.assertEqual(self.acknowledgements()[-1]['result']['stage'], 'applied')
        self.runtime.service.handle({'v': 1, 'id': 'wrong-boot', 'command': 'sensor.config.set',
                                     'bootId': 'old-boot', 'args': {'revision': 1, 'values': {'enabled': True}}})
        self.runtime.service.tick()
        self.assertFalse(self.acknowledgements()[-1]['ok'])
        self.assertEqual(self.runtime._settings('sensor').revision, 1)
        self.assertFalse(self.runtime.service.enabled)

    def test_remote_entry_rejects_other_command_families_and_does_not_enable_debug(self):
        result = self.runtime.handle_sensor_command({'command': 'mqtt.config.get', 'args': {}}, Mock())
        self.assertFalse(result['ok'])
        result = self.runtime.handle_sensor_command({'command': 'sensor.config.get', 'args': {}}, Mock())
        self.assertTrue(result['ok'])
        self.assertFalse(self.runtime.service.enabled)

    def test_mqtt_password_redaction_survives_candidate_rollback(self):
        store, _, _ = self.mqtt_store_and_publisher(False)
        store.begin({'password': 'candidate-test-secret'}, 0)
        store.rollback()
        self.runtime.service.log('warn', 'original-test-secret candidate-test-secret')
        self.command('mqtt.config.get', identifier='read-mqtt')
        output = self.output.getvalue()
        self.assertNotIn('original-test-secret', output)
        self.assertNotIn('candidate-test-secret', output)
        self.assertTrue(self.acknowledgements()[-1]['result']['secrets']['password'])

    def test_sensor_trial_commits_only_after_verified_read_and_uses_configured_profile(self):
        observation = {'sample': 1, 'readAtMs': 1234, 'temperatureC': -2.5,
                       'relativeHumidityPercent': 60.2}
        reader = self.sensor_reader(observation)
        reader.status.return_value.update(failures=0)
        constructor = Mock(return_value=reader)
        completed = Mock()
        result = self.runtime._handle_settings_command('sensor.config.set', {'revision': 0, 'values': {
            'enabled': True, 'protocol': 'sht20_rtu', 'slave_address': 15,
            'baudrate': 14400, 'timeout_ms': 500}}, 'trial-sensor', completed)
        self.assertEqual(result['result']['stage'], 'testing')
        self.assertEqual(self.runtime._settings('sensor').revision, 0)
        with patch.dict(sys.modules, {'components.temperature_humidity':
                types.SimpleNamespace(TemperatureHumidityReader=constructor)}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 31, 1, 2, 3, 0, 0), create=True):
            self.runtime.sensor_tick()
        self.assertEqual(completed.call_args.args[0]['result']['stage'], 'applied')
        self.assertEqual(self.runtime._settings('sensor').revision, 1)
        constructor.assert_called_once_with(self.runtime.service.now, interval_ms=60000,
                                             protocol='sht20_rtu', slave_address=15,
                                             baudrate=14400, timeout_ms=500)
        self.assertEqual(self.runtime._sensor_sample()['source'],
                         {'driver': 'rs485.modbus.sht20', 'integrity': 'crc_verified'})

    def test_sensor_failed_read_rolls_back_and_releases_trial_uart(self):
        old = {'enabled': False, 'interval_seconds': 60}
        Path(self.runtime._sensor_config_path).write_text(json.dumps(old))
        completed = Mock()
        self.runtime._handle_settings_command('sensor.config.set',
            {'revision': 0, 'values': {'enabled': True, 'protocol': 'sht20_rtu'}}, 'trial', completed)
        reader = self.sensor_reader()
        reader.status.return_value['failures'] = 1
        self.runtime.sensor = reader
        self.runtime.settings_tick()
        self.assertFalse(completed.call_args.args[0]['ok'])
        self.assertEqual(completed.call_args.args[0]['result']['stage'], 'rolled-back')
        reader.close.assert_called_once()
        self.assertFalse(self.runtime._settings('sensor').values['enabled'])
        self.assertEqual(self.runtime._settings('sensor').revision, 0)
        self.assertIsNone(self.runtime.sensor)
        self.assertNotEqual(self.runtime.pins.descriptions[14]['mode'], 'uart')

    def test_sensor_restart_keeps_provider_sequence_monotonic_without_replacing_pending_message(self):
        _, publisher, _ = self.mqtt_store_and_publisher()
        original = publisher._pending
        first = {'sample': 71, 'readAtMs': 1234, 'temperatureC': 20, 'relativeHumidityPercent': 50}
        second = {'sample': 1, 'readAtMs': 2000, 'temperatureC': 21, 'relativeHumidityPercent': 51}
        self.runtime.sensor = self.sensor_reader(first)
        self.clock.value = 1234
        with patch.object(self.runtime, '_mqtt_clock_ready', return_value=True), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 31, 1, 2, 3, 0, 0), create=True):
            self.runtime.sensor_tick()
            self.assertEqual(self.runtime._sensor_sample()['sample'], 71)
            self.runtime._apply_sensor_values({**SENSOR_DEFAULTS, 'enabled': True})
            self.clock.value = 2000
            reader = self.sensor_reader(second)
            with patch.dict(sys.modules, {'components.temperature_humidity':
                    types.SimpleNamespace(TemperatureHumidityReader=Mock(return_value=reader))}):
                self.runtime.sensor_tick()
        self.assertEqual(self.runtime._sensor_sample()['sample'], 72)
        self.assertIs(publisher._pending, original)
        self.assertIsNotNone(publisher._provider_observation(2000))

    def test_disabling_sensor_keeps_environment_mode_instead_of_falling_back_to_cpu(self):
        _, publisher, _ = self.mqtt_store_and_publisher(False)
        self.runtime._handle_settings_command('sensor.config.set',
            {'revision': 0, 'values': {'enabled': False}}, 'disable', Mock())
        self.assertEqual(publisher._sample_provider, self.runtime._sensor_sample)
        self.assertIsNone(publisher._sample_provider())
        self.assertTrue(self.runtime._environment_mode)

    def test_mqtt_connection_trial_closes_old_tls_then_preserves_exact_pending_identity(self):
        store, publisher, old = self.mqtt_store_and_publisher()
        original_bytes, original_transport = publisher._pending, old._pending
        candidate = TrialTransport()
        def construct(*args, **kwargs):
            self.assertTrue(old.closed)
            self.assertTrue(old.released)
            self.assertFalse(kwargs['command_active'])
            return candidate
        completed = Mock()
        with patch.dict(sys.modules, {'components.mqtt_transport':
                types.SimpleNamespace(MqttTransport=construct)}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True):
            self.wlan.connected = True
            result = self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'host': 'repair.example.invalid', 'password': ''}}, 'mqtt-trial', completed)
            self.assertEqual(result['result']['stage'], 'testing')
            self.assertEqual(store.revision, 0)
            self.runtime.mqtt_tick()
        self.assertTrue(completed.call_args.args[0]['ok'])
        self.assertEqual(store.values['host'], 'repair.example.invalid')
        self.assertIs(self.runtime.mqtt, publisher)
        self.assertIs(publisher.client, candidate)
        self.assertIs(publisher._pending, original_bytes)
        self.assertIs(candidate._pending, original_transport)
        self.assertTrue(publisher._accepted)
        self.assertEqual(publisher._provider_last_sample, 71)
        self.assertTrue(candidate.commands_active)

    def test_mqtt_identity_change_is_rejected_before_closing_or_staging_pending_data(self):
        store, publisher, previous = self.mqtt_store_and_publisher()
        with self.assertRaisesRegex(ValueError, 'pending message'):
            self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'site_id': 'other-test-site'}}, 'change-site', Mock())
        self.assertFalse(previous.closed)
        self.assertIsNone(store.pending)
        self.assertEqual(store.revision, 0)

    def test_mqtt_subscription_denial_rolls_back_without_publishing_trial_telemetry(self):
        store, publisher, previous = self.mqtt_store_and_publisher()
        original = publisher._pending
        candidate = TrialTransport(command_state='denied')
        completed = Mock()
        with patch.dict(sys.modules, {'components.mqtt_transport':
                types.SimpleNamespace(MqttTransport=Mock(return_value=candidate))}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True):
            self.wlan.connected = True
            self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'commands_enabled': True}}, 'subscribe', completed)
            self.runtime.mqtt_tick()
        self.assertFalse(completed.call_args.args[0]['ok'])
        self.assertEqual(completed.call_args.args[0]['result']['commands']['state'], 'denied')
        self.assertEqual(store.revision, 0)
        self.assertIs(self.runtime.mqtt, publisher)
        self.assertIs(publisher._pending, original)
        self.assertEqual(candidate.published, [])
        self.assertTrue(candidate.released)
        self.assertFalse(store.values['commands_enabled'])

    def test_mqtt_trial_timeout_and_disable_preserve_pending_payload_and_password(self):
        store, publisher, previous = self.mqtt_store_and_publisher()
        original = publisher._pending
        candidate = TrialTransport(connect=False)
        completed = Mock()
        with patch.dict(sys.modules, {'components.mqtt_transport':
                types.SimpleNamespace(MqttTransport=Mock(return_value=candidate))}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True):
            self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'host': 'repair.example.invalid'}}, 'timeout', completed)
            self.clock.value = 45001
            self.runtime.mqtt_tick()
        self.assertEqual(completed.call_args.args[0]['result']['stage'], 'rolled-back')
        self.assertIs(publisher._pending, original)
        result = self.runtime._handle_settings_command('mqtt.config.set',
            {'revision': 0, 'values': {'enabled': False, 'password': ''}}, 'disable', Mock())
        self.assertTrue(result['ok'])
        self.assertEqual(store.values['password'], 'original-test-secret')
        self.assertFalse(publisher.config['enabled'])
        self.assertIs(publisher._pending, original)

    def test_mqtt_clock_normalizes_micropython_epoch_without_claiming_unsynced_time(self):
        with patch.object(self.module.time, 'time', return_value=1000), \
                patch.object(self.module.time, 'gmtime', return_value=(2000, 1, 1, 0, 0, 0, 0, 0), create=True), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=False):
            self.assertEqual(self.runtime._command_clock(), (946685800, False))

    def test_candidate_handshake_keeps_bulk_debug_output_quiet(self):
        self.runtime.mqtt = types.SimpleNamespace(client=types.SimpleNamespace(handshaking=False))
        self.runtime._mqtt_trial = {'client': types.SimpleNamespace(handshaking=True)}
        with patch.object(self.runtime.service, 'tick') as tick:
            self.runtime.debug_telemetry_tick()
        tick.assert_called_once_with(quiet=True)

    def test_sensor_valid_read_is_not_published_when_persistence_fails(self):
        observation = {'sample': 1, 'readAtMs': 0, 'temperatureC': 25,
                       'relativeHumidityPercent': 50}
        reader = self.sensor_reader(observation)
        completed = Mock()
        self.runtime._handle_settings_command('sensor.config.set',
            {'revision': 0, 'values': {'enabled': True}}, 'sensor', completed)
        store = self.runtime._settings('sensor')
        with patch.object(store, 'commit', side_effect=OSError('synthetic disk failure')), \
                patch.dict(sys.modules, {'components.temperature_humidity':
                    types.SimpleNamespace(TemperatureHumidityReader=Mock(return_value=reader))}):
            self.runtime.sensor_tick()
        self.assertFalse(completed.call_args.args[0]['ok'])
        self.assertEqual(store.revision, 0)
        self.assertIsNone(self.runtime._sensor_observation)
        self.assertIsNone(self.runtime.sensor)
        reader.close.assert_called_once()

    def test_mqtt_commit_failure_leaves_original_pending_ownership_untouched(self):
        store, publisher, previous = self.mqtt_store_and_publisher()
        original = previous._pending
        candidate = TrialTransport()
        completed = Mock()
        with patch.dict(sys.modules, {'components.mqtt_transport':
                types.SimpleNamespace(MqttTransport=Mock(return_value=candidate))}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True), \
                patch.object(store, 'commit', side_effect=OSError('synthetic disk failure')):
            self.wlan.connected = True
            self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'host': 'repair.example.invalid'}}, 'disk-fail', completed)
            self.runtime.mqtt_tick()
        self.assertFalse(completed.call_args.args[0]['ok'])
        self.assertIs(previous._pending, original)
        self.assertIsNone(candidate._pending)
        self.assertIs(publisher.client, previous)
        self.assertEqual(store.values['host'], 'mqtt.example.invalid')

    def test_notification_failure_does_not_roll_back_an_applied_configuration(self):
        store, publisher, previous = self.mqtt_store_and_publisher()
        candidate = TrialTransport()
        completed = Mock(side_effect=OSError('synthetic notification failure'))
        with patch.dict(sys.modules, {'components.mqtt_transport':
                types.SimpleNamespace(MqttTransport=Mock(return_value=candidate))}), \
                patch.object(self.runtime, '_mqtt_clock_ready', return_value=True):
            self.wlan.connected = True
            self.runtime._handle_settings_command('mqtt.config.set',
                {'revision': 0, 'values': {'host': 'repair.example.invalid'}}, 'notify-fail', completed)
            self.runtime.mqtt_tick()
        self.assertEqual(store.revision, 1)
        self.assertEqual(store.values['host'], 'repair.example.invalid')
        self.assertIs(publisher.client, candidate)
        self.assertTrue(candidate.connected)
        self.assertIsNone(self.runtime._mqtt_trial)


if __name__ == '__main__':
    unittest.main()
