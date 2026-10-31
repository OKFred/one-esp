"""Business settings persistence, secrets and interrupted transactions."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components import device_settings as settings


def mqtt_values(**changes):
    values = {'enabled': True, 'host': 'mqtt.example.invalid', 'server_hostname': 'mqtt.example.invalid',
              'username': 'test-account', 'password': 'original-test-secret', 'port': 8883,
              'client_id': 'client-test', 'site_id': 'site-test', 'gateway_id': 'gateway-test',
              'device_id': 'device-test', 'ca_file': '.mqtt-ca.der', 'keepalive': 60,
              'interval_seconds': 60}
    values.update(changes)
    return values


class BusinessSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def mqtt(self):
        path = self.directory / '.mqtt.json'
        path.write_text(json.dumps(mqtt_values()))
        (self.directory / '.mqtt-ca.der').write_bytes(b'\x30test-DER-data')
        return settings.SettingsStore(str(path), 'mqtt')

    def test_legacy_sensor_configuration_gets_defaults_without_writing_files(self):
        path = self.directory / '.sensor.json'
        path.write_text('{"enabled":true,"interval_seconds":60}')
        before = path.read_bytes()
        store = settings.SettingsStore(str(path), 'sensor')
        self.assertEqual(store.public()['values'], {**settings.SENSOR_DEFAULTS, 'enabled': True})
        self.assertEqual(store.revision, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(store.revision_path).exists())

    def test_mqtt_public_config_never_contains_password_and_reports_current_ca_digest(self):
        store = self.mqtt()
        public = store.public()
        self.assertNotIn('password', public['values'])
        self.assertNotIn('original-test-secret', json.dumps(public))
        self.assertEqual(public['secrets'], {'password': True})
        self.assertEqual(public['ca'], {'bytes': 14, 'sha256': hashlib.sha256(b'\x30test-DER-data').hexdigest()})

    def test_disabled_mqtt_retains_raw_configuration_and_blank_secret_then_reenables(self):
        store = self.mqtt()
        store.begin({'enabled': False, 'password': ''}, 0)
        store.commit()
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertEqual(restarted.values['password'], 'original-test-secret')
        self.assertEqual(restarted.values['host'], 'mqtt.example.invalid')
        self.assertFalse(restarted.values['enabled'])
        restarted.begin({'enabled': True}, 1)
        self.assertEqual(restarted.commit()['revision'], 2)
        self.assertTrue(restarted.values['enabled'])

    def test_host_repair_updates_default_sni_but_preserves_explicit_sni(self):
        store = self.mqtt()
        store.begin({'host': 'new.example.invalid'}, 0)
        self.assertEqual(store.pending['server_hostname'], 'new.example.invalid')
        store.rollback()
        store.begin({'host': 'new.example.invalid', 'server_hostname': 'custom.example.invalid'}, 0)
        self.assertEqual(store.pending['server_hostname'], 'custom.example.invalid')

    def test_revision_conflicts_unknown_fields_and_ca_path_changes_do_not_stage(self):
        store = self.mqtt()
        original = Path(store.path).read_bytes()
        for revision, values in ((1, {'enabled': False}), (True, {'enabled': False}),
                                  (0, {'ca_file': 'other.der'}), (0, {'ca_data': 'secret'}),
                                  (0, {'unrecognized': 1})):
            with self.subTest(values=tuple(values)):
                with self.assertRaises(ValueError):
                    store.begin(values, revision)
                self.assertIsNone(store.pending)
                self.assertFalse(Path(store.pending_path).exists())
                self.assertEqual(Path(store.path).read_bytes(), original)

    def test_sensor_rejects_unsafe_protocol_uart_and_timing_values(self):
        for changes in ({'protocol': 'raw_write'}, {'slave_address': 0}, {'slave_address': 248},
                        {'baudrate': 115200}, {'baudrate': True}, {'timeout_ms': 199},
                        {'timeout_ms': 5001}, {'interval_seconds': 4}, {'interval_seconds': True},
                        {'enabled': 'true'}, {'tx': 2}):
            with self.subTest(fields=tuple(changes)):
                with self.assertRaises(ValueError):
                    settings.validate_sensor(changes)
        self.assertEqual(settings.validate_sensor({'protocol': 'sht20_rtu', 'baudrate': 14400})['baudrate'], 14400)

    def test_transaction_recovery_rolls_back_values_revision_and_pending_after_power_loss(self):
        store = self.mqtt()
        old = dict(store.values)
        store.begin({'host': 'candidate.example.invalid'}, 0)
        Path(store.transaction_path).write_text(json.dumps({'values': old, 'revision': 0}))
        Path(store.path).write_text(json.dumps(store.pending))
        Path(store.revision_path).write_text('1')
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertEqual(restarted.values, old)
        self.assertEqual(restarted.revision, 0)
        self.assertFalse(Path(store.transaction_path).exists())
        self.assertFalse(Path(store.pending_path).exists())

    def test_interrupted_trial_is_not_promoted_by_restart(self):
        store = self.mqtt()
        store.begin({'password': 'candidate-test-secret'}, 0)
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertEqual(restarted.values['password'], 'original-test-secret')
        self.assertIsNone(restarted.pending)
        self.assertFalse(Path(store.pending_path).exists())
        self.assertIn('candidate-test-secret', restarted.secret_values())

    def test_redaction_history_is_bounded_and_keeps_current_and_candidate(self):
        store = self.mqtt()
        for n in range(8):
            store.begin({'password': 'test-secret-' + str(n)}, store.revision)
            store.commit()
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertLessEqual(len(restarted._redactions), settings.MAX_REDACTIONS)
        self.assertIn('test-secret-7', restarted.secret_values())
        restarted.begin({'password': 'candidate-test-secret'}, restarted.revision)
        self.assertIn('candidate-test-secret', restarted.secret_values())
        self.assertNotIn('candidate-test-secret', json.dumps(restarted.public()))

    def test_tampered_staging_file_is_rejected_without_replacing_active_settings(self):
        store = self.mqtt()
        before = Path(store.path).read_bytes()
        store.begin({'enabled': False}, 0)
        Path(store.pending_path).write_text('{"enabled":true}')
        with self.assertRaises(ValueError):
            store.commit()
        self.assertEqual(Path(store.path).read_bytes(), before)
        self.assertEqual(store.revision, 0)
        self.assertIsNone(store.pending)

    def test_failed_revision_write_restores_values_and_does_not_leave_busy_state(self):
        store = self.mqtt()
        old = dict(store.values)
        store.begin({'host': 'candidate.example.invalid'}, 0)
        replace = settings.os.replace
        failed = []
        def fail_revision(source, target):
            if target == store.revision_path and not failed:
                failed.append(True)
                raise OSError('synthetic storage error')
            return replace(source, target)
        with patch.object(settings.os, 'replace', side_effect=fail_revision):
            with self.assertRaises(OSError):
                store.commit()
        self.assertIsNone(store.pending)
        self.assertEqual(store.values, old)
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertEqual(restarted.values, old)
        self.assertEqual(restarted.revision, 0)

    def test_commands_flag_cannot_accept_truthy_strings(self):
        store = self.mqtt()
        with self.assertRaises(ValueError):
            store.begin({'commands_enabled': 'true'}, 0)

    def test_blank_fixed_broker_ip_restores_dns_resolution(self):
        store = self.mqtt()
        store.begin({'broker_ip': '192.0.2.10'}, 0)
        store.commit()
        store.begin({'broker_ip': ''}, 1)
        store.commit()
        self.assertNotIn('broker_ip', store.values)


if __name__ == '__main__':
    unittest.main()
