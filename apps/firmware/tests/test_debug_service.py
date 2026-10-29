"""Protocol, session, GPIO and configuration regressions without hardware."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from components.board_profile import PinRegistry
from components.debug_service import (DebugService, MAX_LINE_BYTES, MAX_LOG_BYTES,
                                      MAX_LOGS, NORMAL_BYTES_PER_SECOND, WIRE_PREFIX)
from components.device_config import ConfigStore
from components import device_config
from components.env_reader import env_reader


class Clock:
    def __init__(self, start=0):
        self.value = start

    def ticks_ms(self):
        return self.value % (1 << 30)

    def ticks_diff(self, new, old):
        half = 1 << 29
        return ((new - old + half) % (1 << 30)) - half

    def advance(self, milliseconds):
        self.value += milliseconds


class Pin:
    def __init__(self, value=0):
        self.current = value
        self.writes = []

    def value(self, value=None):
        if value is not None:
            self.writes.append(value)
            self.current = value
        return self.current


class DebugServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / '.env'
        self.path.write_text('SSID=TestAP\nPASSWORD=secret-test-value\n', encoding='utf-8')
        self.config = ConfigStore(str(self.path))
        self.clock = Clock()
        self.frames = []
        self.restart_count = 0
        self.trials = []
        self.pins = PinRegistry({36: Pin(), 39: Pin(1)})
        self.service = DebugService(self.frames.append, 'test-device', 'boot-a',
                                    lambda: {'uptimeMs': self.service.now(), 'memFree': 123456,
                                             'wifi': {'connected': True, 'status': 1010},
                                             'sensorEnabled': False},
                                    self.pins, self.config, self.trial, self.restart,
                                    clock=self.clock)

    def restart(self):
        self.restart_count += 1

    def trial(self, command_id, candidate):
        self.trials.append((command_id, dict(candidate)))

    def command(self, name, args=None, identifier=None, boot='boot-a'):
        return {'v': 1, 'id': identifier or str(len(self.frames)) + name,
                'command': name, 'bootId': boot, 'args': args or {}}

    def events(self, kind=None):
        events = [json.loads(line[len(WIRE_PREFIX):]) for line in self.frames]
        return [event for event in events if kind is None or event['type'] == kind]

    def ack(self):
        for _ in range(200):
            if not self.service._pending_acks:
                break
            self.service.tick()
            self.clock.advance(10)
        return self.events('ack')[-1]['data']

    def enable(self, **values):
        self.service.handle(self.command('debug.set', dict(enabled=True, **values)))
        self.assertTrue(self.ack()['ok'])

    def drain_snapshot(self):
        for _ in range(100):
            if not self.service._snapshot_pending:
                return
            self.service.tick()
            self.clock.advance(50)
        self.fail('Snapshot did not complete within its bounded output budget')

    def test_disabled_is_quiet_and_debug_start_contains_a_complete_snapshot(self):
        self.service.tick()
        self.assertEqual(self.frames, [])
        self.enable()
        self.assertEqual([event['type'] for event in self.events()], ['ack'])
        self.drain_snapshot()
        self.assertEqual({event['type'] for event in self.events()},
                         {'ack', 'hello', 'status', 'debug', 'pins', 'oled'})
        pin_events = self.events('pins')
        self.assertFalse(pin_events[0]['data']['partial'])
        self.assertTrue(all(event['data']['partial'] for event in pin_events[1:]))
        self.assertGreater(sum(len(event['data']['pins']) for event in pin_events), 20)
        self.assertTrue(all(len(line.encode()) <= MAX_LINE_BYTES for line in self.frames))

    def test_output_is_denied_with_default_empty_allowlist(self):
        self.enable()
        self.service.handle(self.command('gpio.write', {'gpio': 25, 'value': 1}))
        self.assertFalse(self.ack()['ok'])
        self.assertIn('allowlist', self.ack()['error'])

    def test_session_expiry_restores_explicit_safe_values(self):
        output = Pin()
        self.pins.outputs[25] = {'pin': output, 'safeValue': 0}
        self.enable(ttlSeconds=1)
        self.service.handle(self.command('gpio.write', {'gpio': 25, 'value': 1}))
        self.assertTrue(self.ack()['ok'])
        self.clock.advance(1001)
        self.service.tick()
        self.assertFalse(self.service.enabled)
        self.assertEqual(output.writes, [1, 0])
        self.service.handle(self.command('device.restart'))
        self.assertFalse(self.ack()['ok'])
        self.assertEqual(self.restart_count, 0)

    def test_mutations_require_current_boot_and_active_session(self):
        self.service.handle(self.command('device.restart'))
        self.assertFalse(self.ack()['ok'])
        self.enable()
        self.service.handle(self.command('device.restart', boot='old-boot'))
        self.assertFalse(self.ack()['ok'])
        self.assertEqual(self.restart_count, 0)

    def test_duplicate_restart_is_applied_once(self):
        self.enable()
        command = self.command('device.restart', identifier='restart-once')
        self.service.handle(command)
        self.service.handle(command)
        self.assertEqual(self.restart_count, 1)
        self.assertTrue(self.ack()['ok'])

    def test_fragmented_protocol_and_oversize_unicode_frames_resynchronize(self):
        command = self.command('config.get')
        frame = WIRE_PREFIX + json.dumps(command) + '\r\n'
        for chunk in (frame[:7], frame[7:19], frame[19:]):
            self.service.feed(chunk)
        self.assertTrue(self.ack()['ok'])
        count = len(self.events('ack'))
        self.service.feed(WIRE_PREFIX + '\u754c' * 3000 + '\n')
        self.assertEqual(len(self.events('ack')), count)
        self.assertEqual(self.service._line, '')
        self.service.feed(WIRE_PREFIX + json.dumps(self.command('config.get')) + '\n')
        self.ack()
        self.assertEqual(len(self.events('ack')), count + 1)

    def test_secrets_never_appear_in_config_responses_or_buffered_logs(self):
        self.service.handle(self.command('config.get'))
        self.assertTrue(self.ack()['result']['secrets']['PASSWORD'])
        self.assertNotIn('PASSWORD', self.ack()['result']['values'])
        self.service.log('error', 'test secret-test-value should be hidden')
        self.enable()
        self.drain_snapshot()
        self.clock.advance(1000)
        for _ in range(5):
            self.service.tick()
        self.assertNotIn('secret-test-value', ''.join(self.frames))
        self.assertIn('[redacted]', ''.join(self.frames))

    def test_logs_are_byte_bounded_and_profiles_are_capped(self):
        for _ in range(100):
            self.service.log('info', '\u754c' * 1000)
        self.assertLessEqual(self.service.log_bytes, MAX_LOG_BYTES)
        self.assertGreater(self.service.dropped_logs, 0)
        self.enable(profile='lite', ttlSeconds=9999, oledFps=100, gpioHz=100)
        self.assertEqual(self.service.oled_fps, 1)
        self.assertEqual(self.service.gpio_hz, 1)
        self.assertEqual(self.ack()['result']['expiresInMs'], 1800000)

    def test_sampling_emits_only_changed_rows_as_partial_pins(self):
        self.enable()
        self.drain_snapshot()
        self.frames.clear()
        self.clock.advance(1000)
        self.service.tick()
        self.assertEqual(self.events('pins'), [])
        self.pins.inputs[36].current = 1
        self.clock.advance(200)
        self.service.tick()
        event = self.events('pins')[-1]
        self.assertTrue(event['data']['partial'])
        self.assertEqual([row['gpio'] for row in event['data']['pins']], [36])
        self.assertEqual(event['data']['pins'][0]['source'], 'sampled')

    def test_static_pins_are_sampled_at_most_ten_times_per_second(self):
        self.enable()
        self.drain_snapshot()
        with patch.object(self.pins, 'snapshot', wraps=self.pins.snapshot) as sample:
            for _ in range(100):
                self.clock.advance(10)
                self.service.tick()
        self.assertGreater(sample.call_count, 0)
        self.assertLessEqual(sample.call_count, 10)

    def test_failed_pin_transmission_retries_changed_value_on_next_sample(self):
        self.enable()
        self.drain_snapshot()
        self.frames.clear()
        self.pins.inputs[36].current = 1
        self.clock.advance(200)
        self.service._last_status = self.service.now()
        self.service._last_debug = self.service.now()
        self.service._budget_started = self.service.now()
        self.service._budget_bytes = NORMAL_BYTES_PER_SECOND
        self.service.tick()
        self.assertEqual(self.events('pins'), [])
        self.assertEqual(self.service._last_pin_rows[36]['value'], 0)
        self.clock.advance(1000)
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.events('pins')[-1]['data']['pins'][0]['value'], 1)

    def test_oled_is_latest_only_and_sent_when_changed(self):
        self.enable()
        self.drain_snapshot()
        self.frames.clear()
        self.service.capture_oled({'available': True, 'buffer': bytes(1024), 'text': 'first'})
        self.service.capture_oled({'available': True, 'buffer': b'\x01' + bytes(1023), 'text': 'latest'})
        self.clock.advance(1000)
        for _ in range(5):
            self.service.tick()
        self.assertEqual(len(self.events('oled')), 1)
        self.assertEqual(self.events('oled')[0]['data']['text'], 'latest')
        self.clock.advance(1000)
        self.service.tick()
        self.assertEqual(len(self.events('oled')), 1)

    def test_clock_wrap_does_not_extend_an_expired_session(self):
        self.clock.value = (1 << 30) - 500
        self.service._raw_tick = self.clock.ticks_ms()
        self.enable(ttlSeconds=1)
        self.clock.advance(1001)
        self.service.tick()
        self.assertFalse(self.service.enabled)

    def test_network_configuration_remains_staged_until_confirmed(self):
        self.enable()
        command = self.command('config.set', {
            'revision': 0, 'values': {'SSID': 'OtherAP', 'PASSWORD': 'new-secret'},
        })
        self.service.handle(command)
        self.assertTrue(self.ack()['ok'])
        self.assertEqual(self.ack()['result']['stage'], 'testing')
        self.assertEqual(env_reader(str(self.path))['SSID'], 'TestAP')
        self.assertEqual(len(self.trials), 1)
        self.config.rollback()
        self.assertEqual(self.config.values['SSID'], 'TestAP')
        self.assertEqual(env_reader(str(self.path))['PASSWORD'], 'secret-test-value')
        self.assertFalse(Path(str(self.path) + '.pending').exists())
        self.assertNotIn('new-secret', ''.join(self.frames))

    def test_non_network_configuration_commits_and_revision_prevents_stale_write(self):
        self.enable()
        command = self.command('config.set', {'revision': 0, 'values': {'TIMEZONE_HOURS': 9}})
        self.service.handle(command)
        self.assertTrue(self.ack()['ok'])
        self.assertEqual(self.ack()['result']['revision'], 1)
        self.assertEqual(ConfigStore(str(self.path)).values['TIMEZONE_HOURS'], '9')
        self.service.handle(self.command('config.set', {'revision': 0, 'values': {'TIMEZONE_HOURS': 8}}))
        self.assertFalse(self.ack()['ok'])
        self.assertEqual(self.config.values['TIMEZONE_HOURS'], '9')

    def test_unfinished_trial_is_discarded_on_boot(self):
        self.config.begin({'SSID': 'unconfirmed'}, 0)
        reloaded = ConfigStore(str(self.path))
        self.assertEqual(reloaded.values['SSID'], 'TestAP')
        self.assertIsNone(reloaded.pending)

    def test_control_characters_and_unknown_fields_cannot_inject_environment(self):
        for values in ({'PASSWORD': 'x\nSSID=injected'}, {'SENSOR_ENABLED': 'true'},
                       {'NETWORK_TEST_PORT': 70000}, {'TIMEZONE_HOURS': 'nan'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config.begin(values, self.config.revision)
        self.assertEqual(env_reader(str(self.path))['SSID'], 'TestAP')

    def test_env_reader_supports_comments_and_equals_inside_password(self):
        self.path.write_text('# private\n\nSSID=TestAP\nPASSWORD=a=b=c\n', encoding='utf-8')
        self.assertEqual(env_reader(str(self.path))['PASSWORD'], 'a=b=c')

    def test_protected_outputs_cannot_be_added_to_allowlist(self):
        for gpio in (1, 4, 5, 6, 12, 13, 14, 15, 36, 39):
            with self.subTest(gpio=gpio), self.assertRaises(ValueError):
                PinRegistry(outputs={gpio: {'pin': Pin(), 'safeValue': 0}})

    def test_empty_logs_are_bounded_by_entry_count_and_object_overhead(self):
        for _ in range(1000):
            self.service.log('debug', '')
        self.assertLessEqual(len(self.service.logs), MAX_LOGS)
        self.assertLessEqual(self.service.log_bytes, MAX_LOG_BYTES)
        self.assertGreater(self.service.log_bytes, 0)
        self.assertGreater(self.service.dropped_logs, 0)

    def test_snapshot_burst_coalesces_and_never_writes_a_synchronous_snapshot(self):
        self.enable()
        for index in range(3):
            self.service.handle(self.command('snapshot.get', identifier='snapshot-' + str(index)))
        self.assertEqual({event['type'] for event in self.events()}, {'ack'})
        byte_buckets = {}
        for _ in range(100):
            if not self.service._snapshot_pending:
                break
            previous = len(self.frames)
            self.service.tick()
            normal = self.frames[previous:]
            self.assertLessEqual(len(normal), 1)
            for frame in normal:
                event = json.loads(frame[len(WIRE_PREFIX):])
                if event['type'] == 'ack':
                    continue
                bucket = event['ts'] // 1000
                byte_buckets[bucket] = byte_buckets.get(bucket, 0) + len(frame.encode())
                self.assertLess(len(frame.encode()), 2000)
            self.clock.advance(50)
        self.assertFalse(self.service._snapshot_pending)
        self.assertEqual(len(self.events('hello')), 1)
        self.assertTrue(all(size <= NORMAL_BYTES_PER_SECOND for size in byte_buckets.values()))

    def test_ack_burst_is_paced_and_duplicate_id_remains_cached(self):
        for index in range(8):
            self.service.handle(self.command('config.get', identifier='get-' + str(index)))
        self.assertGreater(len(self.service._pending_acks), 0)
        self.assertEqual(self.frames, [])
        self.assertLessEqual(sum(len(frame.encode()) for frame in self.frames), 2048)
        self.assertLessEqual(len(self.service._pending_acks), 16)
        for _ in range(9):
            self.clock.advance(1000)
            self.service.tick()
        self.assertEqual(len(self.events('ack')), 8)
        self.assertEqual(self.service._pending_acks, [])

    def test_busy_debug_tick_emits_at_most_one_frame_with_logs_and_commands(self):
        self.enable()
        self.drain_snapshot()
        self.frames.clear()
        seen = set()
        for index in range(240):
            self.clock.advance(50)
            # Avoid a 100-ms signal period aliasing the 10-Hz sampler.
            self.pins.inputs[36].current = (index // 3) % 2
            self.service.capture_oled({'available': True,
                                      'buffer': bytes([index % 256]) * 1024,
                                      'text': str(index)})
            self.service.log('warn', 'queued log ' + str(index))
            previous = len(self.frames)
            if index % 20 == 0:
                self.service.handle(self.command('config.get', identifier='load-' + str(index)))
                self.assertEqual(len(self.frames), previous, 'Command handling must only enqueue ACKs')
            self.service.tick()
            emitted = self.frames[previous:]
            self.assertLessEqual(len(emitted), 1, 'One tick must never write consecutive frames')
            for line in emitted:
                seen.add(json.loads(line[len(WIRE_PREFIX):])['type'])
        self.assertTrue({'ack', 'status', 'debug', 'pins', 'oled', 'log'} <= seen)

    def test_ttl_safety_update_precedes_a_queued_ack_without_emitting_two_frames(self):
        output = Pin()
        self.pins.outputs[25] = {'pin': output, 'safeValue': 0}
        self.enable(ttlSeconds=1)
        self.service.handle(self.command('gpio.write', {'gpio': 25, 'value': 1}))
        self.assertEqual(output.current, 1)
        self.clock.advance(1001)
        previous = len(self.frames)
        self.service.tick()
        self.assertFalse(self.service.enabled)
        self.assertEqual(output.current, 0)
        self.assertEqual(len(self.frames) - previous, 1)
        self.assertEqual(self.events()[-1]['type'], 'ack')

    def test_host_validation_uses_ascii_dns_names_and_ip_addresses(self):
        values = self.config.validate({'NTP_SERVER': 'ntp-1.example.com',
                                       'NETWORK_TEST_HOST': '192.0.2.8'})
        self.assertEqual(values['NETWORK_TEST_HOST'], '192.0.2.8')
        for host in ('host:443', 'bad host', '\u4e2d\u6587.example'):
            with self.assertRaises(ValueError):
                self.config.validate({'NTP_SERVER': host})

    def test_credential_spaces_survive_save_and_reload(self):
        expected = {'SSID': ' My AP ', 'PASSWORD': '  private = value  '}
        self.config.begin(expected, 0)
        self.config.commit()
        reloaded = ConfigStore(str(self.path))
        for key, value in expected.items():
            self.assertEqual(reloaded.values[key], value)

    def test_commit_failure_after_transaction_restores_values_and_clears_pending(self):
        self.config.begin({'TIMEZONE_HOURS': 9}, 0)
        original_replace = device_config._replace

        def fail_pending_once(source, target):
            if source == self.config.pending_path:
                raise OSError('simulated storage failure')
            return original_replace(source, target)

        with patch.object(device_config, '_replace', side_effect=fail_pending_once), self.assertRaises(OSError):
            self.config.commit()
        self.assertIsNone(self.config.pending)
        self.assertEqual(ConfigStore(str(self.path)).values['TIMEZONE_HOURS'], '8')
        self.config.begin({'TIMEZONE_HOURS': 9}, 0)
        self.config.commit()
        self.assertEqual(self.config.revision, 1)

    def test_commit_failure_before_transaction_also_clears_pending(self):
        self.config.begin({'TIMEZONE_HOURS': 9}, 0)
        with patch.object(device_config, '_atomic_text', side_effect=OSError('full')), self.assertRaises(OSError):
            self.config.commit()
        self.assertIsNone(self.config.pending)
        self.assertFalse(Path(self.config.pending_path).exists())
        self.config.begin({'TIMEZONE_HOURS': 9}, 0)
        self.config.commit()
        self.assertEqual(self.config.revision, 1)


if __name__ == '__main__':
    unittest.main()
