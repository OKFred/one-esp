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

    def test_sensor_numeric_endpoints_accept_only_integers_and_do_not_stage_invalid_values(self):
        path = self.directory / '.sensor.json'
        store = settings.SettingsStore(str(path), 'sensor')
        for field, lower, upper in (('interval_seconds', 5, 3600),
                                    ('slave_address', 1, 247), ('timeout_ms', 200, 5000)):
            for endpoint in (lower, upper):
                with self.subTest(field=field, endpoint=endpoint):
                    self.assertEqual(settings.validate_sensor({field: endpoint})[field], endpoint)
            for invalid in (lower - 1, upper + 1, True, float(lower), None):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    store.begin({field: invalid}, 0)
                self.assertIsNone(store.pending)
                self.assertFalse(Path(store.pending_path).exists())
                self.assertFalse(path.exists())

    def test_mqtt_unicode_secrets_obey_utf8_byte_limit_without_echoing_or_erasing_password(self):
        store = self.mqtt()
        secret = '\u00e9' * 128
        store.begin({'password': secret}, 0)
        self.assertEqual(len(store.pending['password'].encode('utf-8')), 256)
        self.assertNotIn(secret, json.dumps(store.public(store.pending), ensure_ascii=False))
        store.commit()
        before = Path(store.path).read_bytes()
        for invalid in (secret + '\u00e9', 'test\nsecret', 'test\x7fsecret'):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                store.begin({'password': invalid}, 1)
            self.assertIsNone(store.pending)
            self.assertEqual(Path(store.path).read_bytes(), before)
        store.begin({'password': '', 'enabled': False}, 1)
        store.commit()
        self.assertEqual(settings.SettingsStore(store.path, 'mqtt').values['password'], secret)

    def test_disabled_mqtt_still_validates_optional_broker_ip_and_normalizes_dns_selection(self):
        for empty in ('', None):
            with self.subTest(empty=empty):
                values = settings.validate_mqtt({'enabled': False, 'broker_ip': empty})
                self.assertNotIn('broker_ip', values)
        for address in ('0.0.0.0', '255.255.255.255', '192.0.2.10'):
            self.assertEqual(settings.validate_mqtt({'enabled': False, 'broker_ip': address})['broker_ip'], address)
        store = self.mqtt()
        before = Path(store.path).read_bytes()
        for invalid in (123, True, [], 'not-an-ip', '256.0.0.1', '192.00.2.1',
                        '192.0.2', '::1', '192.0.2.1 ', 'https://192.0.2.1'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.begin({'enabled': False, 'broker_ip': invalid}, 0)
            self.assertIsNone(store.pending)
            self.assertFalse(Path(store.pending_path).exists())
            self.assertEqual(Path(store.path).read_bytes(), before)

    def test_disabled_mqtt_identifiers_keep_the_same_ascii_and_length_limits(self):
        for name in ('client_id', 'site_id', 'gateway_id', 'device_id'):
            for valid in ('', 'a-_Z09', 'a' * 128):
                with self.subTest(name=name, valid_length=len(valid)):
                    self.assertEqual(settings.validate_mqtt({'enabled': False, name: valid})[name], valid)
            for invalid in ('a' * 129, '\u00e9', 'a/b', 'a b', 'a.b', '*'):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                    settings.validate_mqtt({'enabled': False, name: invalid})

    def test_disabled_mqtt_host_boundaries_and_required_ca_path_are_consistent(self):
        maximum_host = '.'.join(('a' * 63, 'b' * 63, 'c' * 63, 'd' * 61))
        self.assertEqual(len(maximum_host), 253)
        for name in ('host', 'server_hostname'):
            for valid in ('', 'broker.example.invalid', 'a' * 63, maximum_host):
                with self.subTest(name=name, valid_length=len(valid)):
                    self.assertEqual(settings.validate_mqtt({'enabled': False, name: valid})[name], valid)
            for invalid in ('a' * 64, maximum_host + 'e', '-broker', 'broker-',
                            'broker..invalid', 'mqtt://broker', 'broker/other', 'a_b', '\u00e9.invalid'):
                with self.subTest(name=name, invalid_length=len(invalid)), self.assertRaises(ValueError):
                    settings.validate_mqtt({'enabled': False, name: invalid})
        with self.assertRaises(ValueError):
            settings.validate_mqtt({'enabled': False, 'ca_file': ''})

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

    def test_power_loss_after_each_atomic_commit_replace_restores_the_original_revision(self):
        for cut_after in range(1, 5):
            with self.subTest(cut_after=cut_after):
                directory = self.directory / ('cut-' + str(cut_after))
                directory.mkdir()
                path = directory / '.mqtt.json'
                path.write_text(json.dumps(mqtt_values()))
                store = settings.SettingsStore(str(path), 'mqtt')
                original = dict(store.values)
                store.begin({'host': 'candidate.example.invalid'}, 0)
                replace = settings.os.replace
                operations = []
                def power_loss(source, target):
                    replace(source, target)
                    operations.append(target)
                    if len(operations) == cut_after:
                        raise SystemExit('synthetic power loss')
                with patch.object(settings.os, 'replace', side_effect=power_loss), self.assertRaises(SystemExit):
                    store.commit()
                restarted = settings.SettingsStore(str(path), 'mqtt')
                self.assertEqual(restarted.values, original)
                self.assertEqual(restarted.revision, 0)
                self.assertFalse(Path(store.transaction_path).exists())
                self.assertFalse(Path(store.pending_path).exists())

    def test_power_loss_after_commit_marker_removal_keeps_the_committed_revision(self):
        store = self.mqtt()
        store.begin({'host': 'committed.example.invalid'}, 0)
        remove = settings.os.remove
        def power_loss(path):
            remove(path)
            if path == store.transaction_path:
                raise SystemExit('synthetic power loss after durable commit')
        with patch.object(settings.os, 'remove', side_effect=power_loss), self.assertRaises(SystemExit):
            store.commit()
        restarted = settings.SettingsStore(store.path, 'mqtt')
        self.assertEqual(restarted.values['host'], 'committed.example.invalid')
        self.assertEqual(restarted.revision, 1)
        self.assertFalse(Path(store.transaction_path).exists())
        self.assertFalse(Path(store.pending_path).exists())

    def test_corrupt_recovery_journal_fails_closed_without_promoting_pending_configuration(self):
        store = self.mqtt()
        store.begin({'host': 'candidate.example.invalid'}, 0)
        original = Path(store.path).read_bytes()
        for recovery in ('{bad-json', json.dumps({'values': store.values, 'revision': True}),
                         json.dumps({'values': store.values, 'revision': -1})):
            with self.subTest(recovery=recovery[:10]):
                Path(store.transaction_path).write_text(recovery)
                with self.assertRaises(ValueError):
                    settings.SettingsStore(store.path, 'mqtt')
                self.assertEqual(Path(store.path).read_bytes(), original)
                self.assertEqual(Path(store.transaction_path).read_text(), recovery)

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
