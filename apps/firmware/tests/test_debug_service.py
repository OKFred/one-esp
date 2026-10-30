"""Protocol, session, GPIO and configuration regressions without hardware."""

import json
import binascii
import io
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
from components import debug_service
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

    def test_tls_quiet_period_defers_bulk_snapshots_but_confirms_commands(self):
        self.service.metrics_provider = self.metrics
        self.service.capture_oled({'available': True, 'buffer': bytes(1024), 'text': '12:34:56'})
        self.service.handle(self.command('debug.set', {'enabled': True}))
        with patch.object(self.service, '_oled_wire_data', wraps=self.service._oled_wire_data) as encode:
            for _ in range(12):
                self.service.tick(quiet=True)
                self.clock.advance(10)
            self.assertTrue(self.events('ack')[-1]['data']['ok'])
            self.assertTrue(self.service.enabled)
            self.assertFalse(self.events('oled'))
            self.assertFalse(self.events('metrics'))
            encode.assert_not_called()
            for _ in range(80):
                self.service.tick()
                self.clock.advance(100)
            self.assertTrue(self.events('oled'))
            self.assertTrue(self.events('metrics'))

    def test_tls_quiet_period_still_expires_debug_lease(self):
        self.service.handle(self.command('debug.set', {'enabled': True, 'ttlSeconds': 1}))
        self.clock.advance(1001)
        self.service.tick(quiet=True)
        self.assertFalse(self.service.enabled)

    def metrics(self, interval):
        now = self.service.now()
        return {'sampledAtMs': now, 'intervalMs': interval, 'cpuHz': 160000000,
                'memory': {'freeBytes': 60000, 'allocatedBytes': 80000, 'totalBytes': 140000},
                'idfHeap': {'totalBytes': 300000, 'freeBytes': 100000, 'largestBlockBytes': 50000},
                'storage': {'sampledAtMs': now, 'totalBytes': 2000000, 'freeBytes': 1500000},
                'flashBytes': 4194304, 'temperature': {'sampledAtMs': now, 'celsius': 45.0},
                'rssiDbm': -55, 'sampleDurationUs': 1200}

    def trial(self, command_id, candidate):
        self.trials.append((command_id, dict(candidate)))

    def command(self, name, args=None, identifier=None, boot='boot-a'):
        return {'v': 1, 'id': identifier or str(len(self.wire_frames())) + name,
                'command': name, 'bootId': boot, 'args': args or {}}

    def wire_frames(self):
        # Parse the joined UART stream; one frame may use several writer calls.
        return ''.join(self.frames).splitlines(keepends=True)

    def events(self, kind=None):
        events = [json.loads(line[len(WIRE_PREFIX):]) for line in self.wire_frames()]
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
        self.assertTrue(all(event['data']['partial'] for event in pin_events))
        self.assertGreater(sum(len(event['data']['pins']) for event in pin_events), 20)
        self.assertTrue(all(len(line.encode()) <= MAX_LINE_BYTES for line in self.wire_frames()))

    def test_idle_metrics_only_sample_on_explicit_snapshot(self):
        calls = []
        self.service.metrics_provider = lambda interval: calls.append(interval) or self.metrics(interval)
        for _ in range(10):
            self.clock.advance(10000)
            self.service.tick()
        self.assertEqual(calls, [])
        self.service.snapshot()
        self.drain_snapshot()
        self.assertFalse(self.service.enabled)
        self.assertEqual(calls, [5000])
        self.assertEqual(len(self.events('metrics')), 1)
        self.assertIn('metrics', self.events('hello')[-1]['data']['capabilities'])
        for _ in range(20):
            self.clock.advance(1000)
            self.service.tick()
        self.assertEqual(calls, [5000])

    def test_renew_extends_active_lease_without_changing_rates_or_dumping_snapshot(self):
        self.enable(profile='lite', ttlSeconds=30, oledFps=0.5, gpioHz=0.4)
        self.drain_snapshot()
        previous_expiry = self.service.expires_at
        self.frames.clear()
        self.clock.advance(1000)
        self.service.handle(self.command('debug.renew', {'ttlSeconds': 60}, 'renew-once'))
        expiry = self.service.expires_at
        self.assertGreater(expiry, previous_expiry)
        ack = self.ack()
        self.assertTrue(ack['ok'])
        self.assertEqual(ack['result']['profile'], 'lite')
        self.assertEqual(ack['result']['oledFps'], 0.5)
        self.assertEqual(ack['result']['gpioHz'], 0.4)
        self.assertFalse(self.service._snapshot_pending)
        self.service.tick()
        self.assertEqual([frame['type'] for frame in self.events()], ['ack', 'debug'])
        self.clock.advance(2000)
        self.service.handle(self.command('debug.renew', {'ttlSeconds': 60}, 'renew-once'))
        self.assertTrue(self.ack()['ok'])
        self.assertEqual(self.service.expires_at, expiry, 'A duplicate ID must not extend twice')

    def test_renew_rejects_disabled_expired_stopped_or_different_boot_sessions(self):
        for state in ('disabled', 'expired', 'stopped', 'wrong-boot', 'missing-boot'):
            with self.subTest(state=state):
                self.service.stop()
                if state != 'disabled':
                    self.enable(ttlSeconds=1)
                    self.drain_snapshot()
                if state == 'expired':
                    self.clock.advance(1500)
                elif state == 'stopped':
                    self.service.handle(self.command('debug.set', {'enabled': False}, 'stop-' + state))
                    self.assertTrue(self.ack()['ok'])
                command = self.command('debug.renew', {'ttlSeconds': 600}, 'renew-' + state,
                                       boot='other-boot' if state == 'wrong-boot' else 'boot-a')
                if state == 'missing-boot':
                    command.pop('bootId')
                self.service.handle(command)
                self.assertFalse(self.ack()['ok'])
                if state in ('disabled', 'expired', 'stopped'):
                    self.assertFalse(self.service.enabled)
                    self.assertEqual(self.service.expires_at, 0)
    def test_metrics_are_low_frequency_and_stop_at_session_expiry(self):
        self.service.metrics_provider = self.metrics
        for profile, interval in (('full', 5000), ('lite', 15000)):
            with self.subTest(profile=profile):
                self.enable(profile=profile, ttlSeconds=45)
                self.drain_snapshot()
                initial = len(self.events('metrics'))
                started = self.clock.value
                for _ in range(1000):
                    before = len(self.wire_frames())
                    self.service.tick()
                    self.assertLessEqual(len(self.wire_frames()) - before, 1)
                    self.clock.advance(50)
                metrics = self.events('metrics')[initial - 1:]
                self.assertGreaterEqual(len(metrics), 3)
                self.assertTrue(all(b['ts'] - a['ts'] >= interval
                                    for a, b in zip(metrics, metrics[1:])))
                self.assertLess(metrics[-1]['ts'] - started, 45000)
                self.assertFalse(self.service.enabled)
                before = len(self.events('metrics'))
                self.clock.advance(60000)
                for _ in range(10):
                    self.service.tick()
                self.assertEqual(len(self.events('metrics')), before)

    def test_optional_metrics_failure_does_not_block_snapshot_or_screen(self):
        calls = []

        def unavailable(interval):
            calls.append(interval)
            raise MemoryError('optional telemetry allocation failed')

        self.service.metrics_provider = unavailable
        self.enable()
        self.drain_snapshot()
        self.assertEqual(calls, [5000])
        self.assertEqual(len(self.events('oled')), 1)
        self.assertEqual(sum(len(frame['data']['pins']) for frame in self.events('pins')), 34)
        self.clock.advance(5100)
        self.service.capture_oled({'available': True, 'buffer': bytes(1024), 'text': 'still running'})
        for _ in range(50):
            self.service.tick()
            self.clock.advance(10)
        self.assertEqual(calls, [5000, 5000])
        self.assertEqual(self.events('oled')[-1]['data']['text'], 'still running')
        self.assertGreaterEqual(len(self.events('status')), 2)
        self.assertEqual(self.events('metrics'), [])

    def test_dropped_refresh_fragment_does_not_erase_known_pins_in_same_boot(self):
        host_pins = {}

        def receive(event):
            if not event['data']['partial']:
                host_pins.clear()
            for pin in event['data']['pins']:
                host_pins[pin['gpio']] = pin

        self.enable()
        self.drain_snapshot()
        for event in self.events('pins'):
            receive(event)
        self.assertEqual(len(host_pins), 34)
        old_gpio12 = host_pins[12]

        self.pins.inputs[36].current = 1
        self.frames.clear()
        self.service.snapshot()
        self.clock.advance(2000)
        self.drain_snapshot()
        fragments = self.events('pins')
        self.assertGreater(len(fragments), 2)
        for index, event in enumerate(fragments):
            if index != 2:  # Simulate one damaged six-pin UART fragment.
                receive(event)

        self.assertEqual(len(host_pins), 34,
                         'A same-boot refresh must not erase pins when one fragment is lost')
        self.assertEqual(host_pins[12], old_gpio12)
        self.assertEqual(host_pins[36]['value'], 1)
        self.assertTrue(all(event['data']['partial'] for event in fragments))

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

    def test_idle_oled_capture_retains_raw_bytes_without_base64_work(self):
        raw = bytes(range(256)) * 4
        with patch.object(debug_service.binascii, 'b2a_base64', side_effect=AssertionError('idle encoding')) as encode:
            for second in range(20):
                self.service.capture_oled({'available': True, 'buffer': raw, 'text': '12:34:' + str(second)})
                self.clock.advance(1000)
                self.service.tick()
            encode.assert_not_called()
        self.assertIs(self.service._oled['buffer'], raw)
        self.assertEqual(self.frames, [])
        self.assertTrue(self.service.emit('oled', self.service._oled))
        event = self.events('oled')[-1]
        self.assertEqual(binascii.a2b_base64(event['data']['buffer']), raw)
        self.assertIs(self.service._oled['buffer'], raw, 'Wire encoding must not become a retained cache')

    def test_mutable_capture_is_frozen_before_later_rendering_changes(self):
        raw = bytearray(1024)
        self.assertTrue(self.service.capture_oled({'available': True, 'buffer': raw, 'text': 'before'}))
        raw[0] = 255
        self.assertTrue(self.service.emit('oled', self.service._oled))
        self.assertEqual(binascii.a2b_base64(self.events('oled')[-1]['data']['buffer']), bytes(1024))

    def test_capture_allocation_failure_is_optional_and_recovers_latest_frame(self):
        raw = bytes(1024)
        original = self.service._oled
        with patch.object(self.service, '_redact', side_effect=MemoryError):
            self.assertFalse(self.service.capture_oled({'available': True, 'buffer': raw, 'text': 'failed'}))
        self.assertIs(self.service._oled, original)
        self.assertEqual(self.service.dropped_events, 1)
        self.assertTrue(self.service.capture_oled({'available': True, 'buffer': raw, 'text': 'recovered'}))
        self.assertTrue(self.service.emit('oled', self.service._oled))
        self.assertEqual(self.events('oled')[-1]['data']['text'], 'recovered')

    def test_emit_allocation_failures_write_nothing_and_keep_oled_retryable(self):
        raw = bytes(1024)
        self.service.capture_oled({'available': True, 'buffer': raw, 'text': 'latest'})
        for target, attribute in ((debug_service.binascii, 'b2a_base64'), (debug_service.json, 'dumps')):
            with patch.object(target, attribute, side_effect=MemoryError):
                self.assertFalse(self.service.emit('oled', self.service._oled))
            self.assertEqual(self.frames, [])
            self.assertEqual(self.service.seq, 0)
            self.assertIs(self.service._oled['buffer'], raw)
        self.assertTrue(self.service.emit('oled', self.service._oled))
        self.assertEqual(binascii.a2b_base64(self.events('oled')[-1]['data']['buffer']), raw)

    def test_utf8_budget_and_full_writer_counts_match_real_wire_bytes(self):
        original_dumps = json.dumps
        for byte_counts in (False, True):
            for kind, data in (('log', {'level': 'info', 'message': '\u6e29\u5ea6\U0001f600' * 30}),
                               ('status', {'wifi': {'ssid': '\u5bb6\u5ead\u7f51\u7edc\U0001f600'}})):
                with self.subTest(byte_counts=byte_counts, kind=kind):
                    stream = io.StringIO()
                    def write(part):
                        stream.write(part)
                        return len(part.encode('utf-8')) if byte_counts else len(part)
                    service = DebugService(write, 'test-device', 'boot-a', lambda: {}, self.pins,
                                           self.config, clock=self.clock, writer_counts_bytes=byte_counts)
                    event = {'v': 1, 'deviceId': 'test-device', 'bootId': 'boot-a',
                             'seq': 1, 'ts': 0, 'type': kind, 'data': data}
                    character_count = len(WIRE_PREFIX) + len(original_dumps(event, ensure_ascii=False)) + 1
                    service._budget_bytes = NORMAL_BYTES_PER_SECOND - character_count
                    with patch.object(debug_service.json, 'dumps', side_effect=lambda value: original_dumps(value, ensure_ascii=False)):
                        self.assertFalse(service.emit(kind, data), 'UTF-8 bytes must not be counted as characters')
                        self.assertEqual(stream.getvalue(), '')
                        service._budget_bytes = 0
                        self.assertTrue(service.emit(kind, data))
                    line = stream.getvalue()
                    self.assertEqual(json.loads(line[len(WIRE_PREFIX):])['data'], data)
                    self.assertEqual(service._budget_bytes, len(line.encode('utf-8')))
                    self.assertEqual(line.count('\n'), 1)

    def test_short_unicode_byte_write_aborts_without_offsets_and_resynchronizes_next_frame(self):
        wire, calls = bytearray(), []
        fail_body = True
        def write(part):
            nonlocal fail_body
            calls.append(part)
            encoded = part.encode('utf-8')
            if part.startswith('{') and fail_body:
                fail_body = False
                # Native write counts bytes, even if that number happens to
                # equal the full string's Unicode character count.
                count = len(part)
                wire.extend(encoded[:count])
                return count
            wire.extend(encoded)
            return len(encoded)
        service = DebugService(write, 'test-device', 'boot-a', lambda: {}, self.pins,
                               self.config, clock=self.clock, writer_counts_bytes=True)
        original_dumps = json.dumps
        with patch.object(debug_service.json, 'dumps', side_effect=lambda value: original_dumps(value, ensure_ascii=False)):
            self.assertFalse(service.emit('log', {'level': 'info', 'message': '\u6e29\u5ea6\U0001f600' * 20}))
            self.assertEqual(service.seq, 0)
            self.assertTrue(service.emit('log', {'level': 'info', 'message': 'recovered \u6062\u590d'}))
        self.assertEqual(calls[2], '\n', 'The next attempt must terminate the damaged line before any new prefix')
        lines = bytes(wire).splitlines()
        with self.assertRaises((ValueError, UnicodeError)):
            json.loads(lines[0][len(WIRE_PREFIX):])
        self.assertEqual(json.loads(lines[1][len(WIRE_PREFIX):])['data']['message'], 'recovered \u6062\u590d')
        self.assertEqual(service.seq, 1)
        self.assertEqual(service.dropped_events, 1)
        self.assertEqual(sum(part == WIRE_PREFIX for part in calls), 2)

    def test_writer_exception_resynchronizes_and_failed_attempts_still_consume_budget(self):
        fail_body = True
        def write(part):
            nonlocal fail_body
            if part.startswith('{') and fail_body:
                fail_body = False
                self.frames.append(part[:5])
                raise OSError('partial UART failure')
            self.frames.append(part)
        self.service.writer = write
        self.assertFalse(self.service.emit('status', {'value': 1}))
        reserved = self.service._budget_bytes
        self.assertGreater(reserved, 0)
        self.assertTrue(self.service.emit('status', {'value': 2}))
        lines = self.wire_frames()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1][len(WIRE_PREFIX):])['data']['value'], 2)
        self.assertGreater(self.service._budget_bytes, reserved)

    def test_memory_failure_does_not_poison_snapshot_progress_or_swallow_maintenance_interrupt(self):
        self.service.snapshot()
        self.service._snapshot_stage = 3
        with patch.object(debug_service.json, 'dumps', side_effect=MemoryError):
            self.service.tick()
        self.assertTrue(self.service._snapshot_pending)
        self.drain_snapshot()
        self.assertIsNone(self.service._snapshot_rows)
        self.assertEqual(sum(len(event['data']['pins']) for event in self.events('pins')), 34)
        with patch.object(self.service, '_redact', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.service.capture_oled({'available': True, 'buffer': bytes(1024), 'text': 'interrupt'})
        self.service.writer = lambda part: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.service.emit('status', {})

    def test_uart_cost_and_noisy_inputs_do_not_starve_the_one_hertz_screen(self):
        class NoisyInput(Pin):
            def value(self):
                self.current ^= 1
                return self.current

        def uart_write(line):
            self.frames.append(line)
            # 115200 8N1 takes ten wire bits per byte. Unlike a zero-cost writer,
            # this can leave the 10-Hz GPIO sampler due again on the next tick.
            wire_ms = (len(line.encode()) * 10000 + 115199) // 115200
            self.clock.advance(wire_ms)

        self.service.writer = uart_write
        self.service.metrics_provider = self.metrics
        self.pins.inputs = {36: NoisyInput(), 39: NoisyInput()}
        self.enable()
        self.drain_snapshot()
        self.service.device_id = 'abcdef012345'
        self.service.boot_id = '0123456789abcdef'
        self.frames.clear()
        last_second = -1
        while self.clock.value < 12000:
            previous = len(self.wire_frames())
            self.service.tick()
            self.assertLessEqual(len(self.wire_frames()) - previous, 1)
            second = self.clock.value // 1000
            if second != last_second:
                last_second = second
                self.service.capture_oled({
                    'available': True, 'buffer': bytes([second % 256]) * 1024,
                    'text': 'WiFi Connected!\n192.0.2.100\n10-30 03:15:' +
                            str(second).zfill(2) + '\nTCP: 42ms',
                })
                self.clock.advance(25)  # OLED I2C transfer.
            self.clock.advance(60)  # Other loop work on the ESP32.

        steady = [event for event in self.events() if event['ts'] >= 3000]
        oled = [event for event in steady if event['type'] == 'oled']
        self.assertGreaterEqual(len(oled), 9)
        self.assertGreaterEqual(len({event['data']['text'] for event in oled}), 9)
        self.assertLess(self.clock.value - oled[-1]['ts'], 1500)
        self.assertGreaterEqual(len([event for event in steady if event['type'] == 'status']), 8)
        self.assertGreaterEqual(len([event for event in steady if event['type'] == 'pins']), 30)

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
            previous = len(self.wire_frames())
            self.service.tick()
            normal = self.wire_frames()[previous:]
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
        self.assertLessEqual(sum(len(frame.encode()) for frame in self.wire_frames()), 2048)
        self.assertLessEqual(len(self.service._pending_acks), 16)
        for _ in range(9):
            self.clock.advance(1000)
            self.service.tick()
        self.assertEqual(len(self.events('ack')), 8)
        self.assertEqual(self.service._pending_acks, [])

    def test_busy_debug_tick_emits_at_most_one_frame_with_logs_and_commands(self):
        self.service.metrics_provider = self.metrics
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
            previous = len(self.wire_frames())
            if index % 20 == 0:
                self.service.handle(self.command('config.get', identifier='load-' + str(index)))
                self.assertEqual(len(self.wire_frames()), previous, 'Command handling must only enqueue ACKs')
            self.service.tick()
            emitted = self.wire_frames()[previous:]
            self.assertLessEqual(len(emitted), 1, 'One tick must never write consecutive frames')
            for line in emitted:
                seen.add(json.loads(line[len(WIRE_PREFIX):])['type'])
        self.assertTrue({'ack', 'status', 'debug', 'pins', 'oled', 'log', 'metrics'} <= seen)

    def test_ttl_safety_update_precedes_a_queued_ack_without_emitting_two_frames(self):
        output = Pin()
        self.pins.outputs[25] = {'pin': output, 'safeValue': 0}
        self.enable(ttlSeconds=1)
        self.service.handle(self.command('gpio.write', {'gpio': 25, 'value': 1}))
        self.assertEqual(output.current, 1)
        self.clock.advance(1001)
        previous = len(self.wire_frames())
        self.service.tick()
        self.assertFalse(self.service.enabled)
        self.assertEqual(output.current, 0)
        self.assertEqual(len(self.wire_frames()) - previous, 1)
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
