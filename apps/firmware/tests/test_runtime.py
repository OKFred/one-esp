"""Runtime ownership, Wi-Fi rollback and maintenance scheduling regressions."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.device_config import ConfigStore
from components.debug_service import WIRE_PREFIX


class Clock:
    def __init__(self):
        self.value = 0

    def ticks_ms(self):
        return self.value

    def ticks_diff(self, new, old):
        return new - old

    def time(self):
        return 846127200 + self.value // 1000


class Wlan:
    def __init__(self):
        self.connected = False
        self.connects = []
        self.delay_disconnect = False

    def active(self, enabled):
        pass

    def disconnect(self):
        if not self.delay_disconnect:
            self.connected = False

    def connect(self, ssid, password):
        self.connects.append((ssid, password))

    def config(self, **values):
        pass

    def isconnected(self):
        return self.connected

    def status(self, query=None):
        return -50 if query == 'rssi' else (1010 if self.connected else 202)

    def ifconfig(self):
        return ('192.0.2.10', '', '', '')


class Probe:
    def __init__(self, clock):
        self.clock = clock
        self.busy = False
        self.starts = []
        self.cancels = 0
        self.start_result = True
        self.finish_result = None

    def result(self, ok):
        return {'ok': ok, 'ping': 42 if ok else None,
                'startedAtMs': self.clock.value, 'completedAtMs': self.clock.value}

    def start(self, host, port):
        self.starts.append((self.clock.value, host, port))
        self.busy = self.start_result is None
        return None if self.busy else self.result(self.start_result)

    def tick(self):
        result, self.finish_result = self.finish_result, None
        if result is not None:
            self.busy = False
        return result

    def cancel(self):
        self.cancels += 1
        self.busy = False
        self.finish_result = None


class ClockSynchronizer:
    """Scheduler-facing contract; packet math is tested in test_clock_sync."""
    def __init__(self, clock):
        self.clock = clock
        self.server = None
        self.next_due_ms = 0
        self.busy = False
        self.starts = []
        self.cancels = 0
        self.requests = 0
        self.polls = 0
        self.synced = False
        self.samples = 0
        self.failures = 0
        self.last_sync = None
        self.interval_ms = 60000
        self.start_result = True
        self.finish_result = None

    def configure(self, server):
        if server != self.server:
            self.cancel()
            self.server = server
            self.next_due_ms = self.clock.value

    def request_sync(self):
        self.requests += 1
        self.next_due_ms = self.clock.value

    def complete(self, result):
        self.busy = False
        if result['ok']:
            self.synced = True
            self.samples += 1
            self.last_sync = self.clock.value
            self.next_due_ms = self.clock.value + self.interval_ms
        else:
            self.failures += 1
            self.next_due_ms = self.clock.value + 30000
        return result

    def start(self):
        self.starts.append((self.clock.value, self.server))
        self.busy = self.start_result is None
        if self.busy:
            return None
        return self.complete({'ok': self.start_result})

    def tick(self):
        self.polls += 1
        result, self.finish_result = self.finish_result, None
        return self.complete(result) if result is not None else None

    def cancel(self):
        self.cancels += 1
        self.busy = False
        self.finish_result = None

    def status(self):
        return {'synced': self.synced, 'targetMs': 500,
                'estimatedErrorMs': 40 if self.synced else None,
                'withinTarget': self.synced, 'samples': self.samples,
                'failures': self.failures, 'intervalMs': self.interval_ms,
                'nextInMs': max(0, self.next_due_ms - self.clock.value),
                'lastSyncAgeMs': None if self.last_sync is None else self.clock.value - self.last_sync,
                'busy': self.busy}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / '.env'
        path.write_text('SSID=Original\nPASSWORD=original-private\n', encoding='utf-8')
        self.config = ConfigStore(str(path))
        self.clock = Clock()
        self.wlan = Wlan()
        self.machine = types.SimpleNamespace(
            Pin=Mock(side_effect=lambda gpio: types.SimpleNamespace(value=lambda: 0)),
            UART=Mock(side_effect=AssertionError('UART must not be initialized')),
            unique_id=lambda: b'device', reset=Mock(),
        )
        self.probe = Probe(self.clock)
        self.sync = ClockSynchronizer(self.clock)
        self.local_time = Mock(return_value={'formatted_simple_date_time': '10-30 12:34:56',
                                            'formatted_time': '12:34:56'})
        modules = {
            'machine': self.machine,
            'network': types.SimpleNamespace(WLAN=lambda _: self.wlan, STA_IF=0,
                                            STAT_WRONG_PASSWORD=202, STAT_NO_AP_FOUND=201,
                                            STAT_IDLE=1000, STAT_CONNECTING=1001),
            'time': self.clock, 'gc': types.SimpleNamespace(mem_free=lambda: 200000, collect=Mock()),
            'uselect': types.SimpleNamespace(poll=lambda: Mock(), POLLIN=1),
            'components.time_sync': types.SimpleNamespace(get_local_time=self.local_time),
            'components.network_probe': types.SimpleNamespace(NetworkProbe=lambda _: self.probe),
            'components.clock_sync': types.SimpleNamespace(ClockSync=lambda _: self.sync),
        }
        spec = importlib.util.spec_from_file_location('_runtime_test', ROOT / 'application/runtime.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.module)
        self.output = io.StringIO()
        with patch.object(self.module, 'ConfigStore', return_value=self.config), contextlib.redirect_stdout(self.output):
            self.runtime = self.module.DiagnosticRuntime(mqtt_config_path=str(path.with_name('.mqtt.json')))
        self.display = Mock()
        self.runtime.display = self.display

    def command(self, name, args=None, identifier='test'):
        self.runtime.service.handle({'v': 1, 'id': identifier, 'command': name,
                                     'bootId': self.runtime.service.boot_id, 'args': args or {}})
        self.runtime.service.tick()

    def test_initialization_never_claims_sensor_or_uart_pins(self):
        self.assertEqual([call.args for call in self.machine.Pin.call_args_list], [(36,), (39,)])
        self.machine.UART.assert_not_called()
        self.assertFalse(self.runtime.status()['sensorEnabled'])

    def test_wifi_password_failure_keeps_usb_commands_alive(self):
        self.runtime.start_wifi(self.config.values)
        self.runtime.network_tick()
        self.clock.value = 20001
        self.runtime.network_tick()
        self.assertEqual(self.runtime.network_state, 'failed')
        self.assertIn('Check password', self.display.screen_display.call_args.args[0])
        self.command('debug.set', {'enabled': True})
        self.assertTrue(self.runtime.service.enabled)
        self.assertIn('"type": "ack"', self.output.getvalue())
        self.assertNotIn('original-private', self.output.getvalue())

    def test_failed_trial_restores_previous_credentials_and_reports_rollback(self):
        self.command('debug.set', {'enabled': True}, 'enable')
        self.command('config.set', {'revision': 0, 'values': {'SSID': 'New', 'PASSWORD': 'new-private'}})
        self.runtime.network_tick()
        self.clock.value = 60001
        self.runtime.network_tick()
        self.runtime.network_tick()
        self.runtime.service.tick()
        self.assertIsNone(self.config.pending)
        self.assertEqual(self.config.values['SSID'], 'Original')
        self.assertEqual(self.wlan.connects[-1], ('Original', 'original-private'))
        frames = [json.loads(line[len(WIRE_PREFIX):]) for line in self.output.getvalue().splitlines()]
        acks = [frame['data'] for frame in frames if frame['type'] == 'ack']
        self.assertEqual(acks[-1]['result']['stage'], 'rolled-back')
        self.assertNotIn('new-private', self.output.getvalue())

    def test_debug_keeps_pending_ntp_tcp_clock_and_usb_running(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.command('debug.set', {'enabled': True, 'ttlSeconds': 600}, 'enable')
        self.sync.start_result = None
        self.probe.start_result = None
        self.local_time.side_effect = [
            {'formatted_simple_date_time': '10-30 12:34:56', 'formatted_time': '12:34:56'},
            {'formatted_simple_date_time': '10-30 12:34:57', 'formatted_time': '12:34:57'},
        ]
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.runtime.clock_tick()
        self.assertEqual(self.sync.starts, [(0, 'ntp.aliyun.com')])
        self.assertEqual(self.probe.starts, [(0, 'baidu.com', 443)])
        self.assertTrue(self.sync.busy)
        self.assertTrue(self.probe.busy)
        self.clock.value = 1000
        self.runtime.maintenance_tick()
        self.runtime.clock_tick()
        self.command('config.get', identifier='while-network-pending')
        text = [call.args[0] for call in self.display.screen_display.call_args_list]
        self.assertIn('RTC 12:34:56', text[0])
        self.assertIn('RTC 12:34:57', text[1])
        self.assertIn('while-network-pending', self.output.getvalue())
        self.assertFalse(self.runtime.status()['maintenancePaused'])
        self.assertFalse(self.runtime.status()['maintenance']['ntpPaused'])
        self.assertGreaterEqual(self.sync.polls, 1)
        self.sync.finish_result = {'ok': True}
        self.probe.finish_result = self.probe.result(True)
        self.runtime.maintenance_tick()
        self.assertTrue(self.runtime.ntp_synced)
        self.assertEqual(self.runtime.tcp_ms, 42)
        self.assertIsNone(self.runtime.maintenance_busy)
        self.assertEqual(self.runtime.status()['clock']['targetMs'], 500)
        self.assertTrue(self.runtime.status()['clock']['withinTarget'])

    def test_tcp_runs_once_per_minute_during_debug_without_catchup_bursts(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.clock.value = 59999
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.probe.starts), 1)
        self.clock.value = 60000
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertEqual([start[0] for start in self.probe.starts], [0, 60000])
        self.clock.value = 245000
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertEqual([start[0] for start in self.probe.starts], [0, 60000, 245000])
        self.assertEqual(self.runtime.status()['maintenance']['tcpNextInMs'], 60000)
        self.assertEqual(self.runtime.status()['maintenance']['tcpSamples'], 3)
        self.assertEqual(self.runtime.status()['maintenance']['tcpLastTestMs'], 245000)
        self.assertEqual(len(self.sync.starts), 3)

    def test_failed_probe_waits_a_minute_before_retrying(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.probe.start_result = False
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertFalse(self.runtime.tcp_ok)
        self.assertIsNone(self.runtime.tcp_ms)
        self.clock.value = 59999
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.probe.starts), 1)
        self.clock.value = 60000
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.probe.starts), 2)

    def test_ntp_uses_component_deadline_even_during_debug(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.maintenance_tick()
        self.assertEqual(self.sync.starts, [(0, 'ntp.aliyun.com')])
        self.runtime.maintenance_tick()
        self.clock.value = 59999
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.sync.starts), 1)
        # The component, rather than a fixed runtime period, selects the next
        # interval from its uncertainty and drift estimate.
        self.sync.interval_ms = 175000
        self.clock.value = 60000
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertEqual(self.sync.starts[-1], (60000, 'ntp.aliyun.com'))
        self.assertEqual(self.sync.next_due_ms, 235000)
        self.clock.value = 234999
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.sync.starts), 2)
        self.clock.value = 235000
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.sync.starts), 3)

    def test_same_value_save_does_not_reset_network_schedules(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        ntp_due = self.sync.next_due_ms
        self.clock.value = 1000
        self.config.begin({'TIMEZONE_HOURS': 9, 'NETWORK_TEST_PORT': 443}, self.config.revision)
        self.config.commit()
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.probe.starts), 1)
        self.assertEqual(len(self.sync.starts), 1)
        self.assertEqual(self.runtime.tcp_at, 60000)
        self.assertEqual(self.sync.next_due_ms, ntp_due)

    def test_endpoint_changes_only_reset_the_affected_job(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        ntp_due = self.sync.next_due_ms
        self.clock.value = 1000
        self.config.values['NETWORK_TEST_HOST'] = 'new.example.com'
        self.runtime.maintenance_tick()
        self.assertIsNone(self.runtime.tcp_ok)
        self.assertEqual(self.sync.next_due_ms, ntp_due)
        self.clock.value = 1499
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.probe.starts), 1)
        self.clock.value = 1500
        self.runtime.maintenance_tick()
        self.assertEqual(self.probe.starts[-1], (1500, 'new.example.com', 443))
        tcp_at = self.runtime.tcp_at
        self.config.values['NTP_SERVER'] = 'other.example.com'
        self.runtime.maintenance_tick()
        self.assertEqual(self.runtime.tcp_at, tcp_at)
        self.assertEqual(self.sync.starts[-1], (1500, 'other.example.com'))
        # A server change does not invalidate the already calibrated RTC.
        self.assertTrue(self.runtime.status()['clock']['synced'])

    def test_wifi_loss_reconnect_and_restart_clean_up_both_jobs(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.probe.start_result = None
        self.sync.start_result = None
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertTrue(self.probe.busy)
        self.assertTrue(self.sync.busy)
        self.wlan.connected = False
        self.runtime.network_tick()
        self.assertFalse(self.probe.busy)
        self.assertFalse(self.sync.busy)
        self.clock.value = 100
        self.wlan.connected = True
        self.runtime.network_tick()
        self.assertEqual(self.runtime.tcp_at, 1100)
        self.assertEqual(self.sync.next_due_ms, 100)
        self.runtime.maintenance_tick()
        self.assertTrue(self.sync.busy)
        self.clock.value = 1100
        self.runtime.maintenance_tick()
        self.assertTrue(self.probe.busy)
        self.runtime.schedule_restart()
        self.assertFalse(self.probe.busy)
        self.assertFalse(self.sync.busy)
        self.clock.value = 62000
        self.runtime.maintenance_tick()
        self.assertFalse(self.probe.busy)
        self.assertFalse(self.sync.busy)
        self.assertEqual(len(self.probe.starts), 2)
        self.assertEqual(len(self.sync.starts), 2)

    def test_wifi_trial_closes_pending_ntp_and_tcp(self):
        self.probe.busy = True
        self.sync.busy = True
        self.runtime.start_wifi(self.config.values)
        self.assertFalse(self.probe.busy)
        self.assertFalse(self.sync.busy)

    def test_ntp_failure_retains_last_calibration_and_retries_its_deadline(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertTrue(self.runtime.ntp_synced)
        self.sync.start_result = False
        self.clock.value = 60000
        self.runtime.maintenance_tick()
        self.runtime.maintenance_tick()
        self.assertTrue(self.runtime.ntp_synced)
        self.assertEqual(self.runtime.status()['clock']['samples'], 1)
        self.assertEqual(self.runtime.status()['clock']['failures'], 1)
        self.clock.value = 89999
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.sync.starts), 2)
        self.clock.value = 90000
        self.runtime.maintenance_tick()
        self.assertEqual(len(self.sync.starts), 3)

    def test_clock_tracks_rtc_second_boundaries_without_extra_i2c_refreshes(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.local_time.side_effect = lambda _: {
            'formatted_simple_date_time': '10-30 12:34:%02d' % (self.clock.time() % 60),
            'formatted_time': '12:34:%02d' % (self.clock.time() % 60),
        }
        self.clock.value = 900
        self.runtime.clock_tick()
        self.clock.value = 999
        self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 1)
        self.clock.value = 1000
        self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 2)
        self.assertIn('RTC 12:34:01', self.display.screen_display.call_args.args[0])
        for now in range(1100, 2000, 100):
            self.clock.value = now
            self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 2)
        self.runtime.tcp_ok, self.runtime.tcp_ms = True, 51
        self.clock.value = 1901
        self.runtime.clock_tick()
        self.clock.value = 2000
        self.runtime.clock_tick()
        self.assertIn('TCP: 51ms', self.display.screen_display.call_args.args[0])

    def test_each_failed_stage_leaves_other_stages_running_and_retries_after_backoff(self):
        stages = [('usb', self.runtime, 'poll_commands'),
                  ('network', self.runtime, 'network_tick'),
                  ('maintenance', self.runtime, 'maintenance_tick'),
                  ('clock', self.runtime, 'clock_tick'),
                  ('telemetry', self.runtime.service, 'tick'),
                  ('mqtt', self.runtime, 'mqtt_tick'),
                  ('restart', self.runtime, '_restart_step')]
        for failed, target, method in stages:
            with self.subTest(stage=failed), contextlib.ExitStack() as stack:
                self.clock.value = 0
                self.runtime._stage_retry_at.clear()
                self.runtime._stage_log_at.clear()
                calls = []
                mocks = {}
                for name, owner, attribute in stages:
                    def operation(stage=name):
                        calls.append(stage)
                        if stage == failed and calls.count(stage) == 1:
                            raise ValueError('original-private new-private')
                    mocks[name] = stack.enter_context(patch.object(owner, attribute, side_effect=operation))
                self.runtime.tick_once()
                self.assertEqual(calls, [stage[0] for stage in stages])
                self.clock.value = 500
                self.runtime.tick_once()
                self.assertEqual(mocks[failed].call_count, 1)
                self.assertTrue(all(mock.call_count == 2 for name, mock in mocks.items() if name != failed))
                self.clock.value = 1000
                self.runtime.tick_once()
                self.assertEqual(mocks[failed].call_count, 2)
                self.assertNotIn(failed, self.runtime._stage_retry_at)
                message = self.runtime.service.logs[-1]['message']
                self.assertIn('Runtime ' + failed + ' failed (ValueError)', message)
                self.assertNotIn('private', message)

    def test_persistent_stage_failure_has_bounded_retries_and_logs(self):
        self.runtime.service.logs.clear()
        self.runtime.service.log_bytes = 0
        with patch.object(self.runtime, 'poll_commands'), \
                patch.object(self.runtime, 'network_tick'), \
                patch.object(self.runtime, 'maintenance_tick', side_effect=MemoryError('private')) as failing, \
                patch.object(self.runtime, 'clock_tick') as clock, \
                patch.object(self.runtime.service, 'tick'), \
                patch.object(self.runtime, '_restart_step'):
            for now in range(0, 30000, 100):
                self.clock.value = now
                self.runtime.tick_once()
            self.assertEqual(failing.call_count, 30)
            self.assertEqual(clock.call_count, 300)
            self.assertEqual(len(self.runtime.service.logs), 1)
            self.clock.value = 30000
            self.runtime.tick_once()
            self.assertEqual(len(self.runtime.service.logs), 2)
            self.assertTrue(all('private' not in entry['message'] for entry in self.runtime.service.logs))

    def test_maintenance_exception_does_not_stop_real_clock_or_telemetry_progress(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.service.expires_at = 600000
        self.local_time.side_effect = lambda _: {
            'formatted_simple_date_time': '10-30 12:34:%02d' % (self.clock.time() % 60),
            'formatted_time': '12:34:%02d' % (self.clock.time() % 60),
        }
        with patch.object(self.runtime, 'poll_commands'), \
                patch.object(self.runtime, 'maintenance_tick', side_effect=RuntimeError('private')):
            self.runtime.tick_once()
            first_seq = self.runtime.service.seq
            self.clock.value = 1000
            self.runtime.tick_once()
        shown = [call.args[0] for call in self.display.screen_display.call_args_list]
        self.assertIn('RTC 12:34:00', shown[0])
        self.assertIn('RTC 12:34:01', shown[1])
        self.assertGreater(self.runtime.service.seq, first_seq)
        self.assertTrue(self.runtime.service.enabled)

    def test_failed_display_does_not_commit_clock_view_or_retry_every_loop(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.clock.time = lambda: 846127200
        self.display.screen_display.side_effect = [OSError('private'), True]
        self.runtime.clock_tick()
        self.assertIsNone(self.runtime._last_clock_view)
        for now in range(100, 1000, 100):
            self.clock.value = now
            self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 1)
        self.clock.value = 1000
        self.runtime.clock_tick()
        self.assertIsNotNone(self.runtime._last_clock_view)
        self.assertEqual(self.display.screen_display.call_count, 2)
        self.clock.value = 1100
        self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 2)
        self.assertNotIn('private', self.runtime.service.logs[-1]['message'])

    def test_display_unavailable_return_is_retried_without_committing_clock_view(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.display.screen_display.return_value = False
        self.runtime.clock_tick()
        self.assertIsNone(self.runtime._last_clock_view)
        self.clock.value = 100
        self.runtime.clock_tick()
        self.assertEqual(self.display.screen_display.call_count, 1)

    def test_network_oserror_preserves_connection_failure_handling(self):
        with patch.object(self.runtime, 'network_tick', side_effect=OSError('private')), \
                patch.object(self.runtime, 'network_failure') as failure:
            self.runtime._network_step()
        failure.assert_called_once_with('Connect failed')

    def test_deliberate_interrupts_are_not_swallowed(self):
        for exception in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception), \
                    patch.object(self.runtime, 'poll_commands', side_effect=exception):
                with self.assertRaises(exception):
                    self.runtime.tick_once()

    def test_initialization_failure_does_not_trigger_automatic_hardware_reset(self):
        with patch.object(self.module, 'DiagnosticRuntime', side_effect=RuntimeError('private')):
            with self.assertRaises(RuntimeError):
                self.module.run()
        self.machine.reset.assert_not_called()

    def test_failed_display_import_retries_every_thirty_seconds_and_recovers(self):
        self.runtime.display = None

        def load():
            if loader.call_count == 1:
                raise ImportError('private-path')
            self.runtime.display = self.display

        with patch.object(self.runtime, '_load_display', side_effect=load) as loader:
            self.assertFalse(self.runtime.show('waiting'))
            for now in range(1000, 30000, 1000):
                self.clock.value = now
                self.assertFalse(self.runtime.show('waiting'))
            self.assertEqual(loader.call_count, 1)
            self.clock.value = 30000
            self.assertTrue(self.runtime.show('recovered'))
            self.assertEqual(loader.call_count, 2)
            self.display.screen_display.assert_called_once_with('recovered')
            self.assertNotIn('private', self.runtime.service.logs[-1]['message'])

    def test_initial_wifi_start_exception_retries_without_stopping_other_stages(self):
        self.runtime._wifi_initializing = True
        with patch.object(self.runtime, 'start_wifi', side_effect=[ValueError('private'), None]) as start, \
                patch.object(self.runtime, 'poll_commands') as commands, \
                patch.object(self.runtime, 'maintenance_tick'), \
                patch.object(self.runtime, 'clock_tick') as clock, \
                patch.object(self.runtime.service, 'tick'):
            self.runtime.tick_once()
            self.assertTrue(self.runtime._wifi_initializing)
            self.clock.value = 500
            self.runtime.tick_once()
            self.assertEqual(start.call_count, 1)
            self.clock.value = 1000
            self.runtime.tick_once()
            self.assertEqual(start.call_count, 2)
            self.assertFalse(self.runtime._wifi_initializing)
            self.assertEqual(commands.call_count, 3)
            self.assertEqual(clock.call_count, 3)

    def test_mqtt_without_private_configuration_is_disabled_without_importing_transport(self):
        with patch.object(self.runtime, '_mqtt_enabled_flag', wraps=self.runtime._mqtt_enabled_flag) as probe:
            for self.clock.value in (0, 1000, 60000):
                self.runtime.mqtt_tick()
            probe.assert_called_once()
        self.assertTrue(self.runtime._mqtt_initialized)
        self.assertIsNone(self.runtime.mqtt)
        self.assertEqual(self.runtime.status()['mqtt']['state'], 'disabled')

    def test_disabled_private_mqtt_config_does_not_import_tls_or_transport(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":false}', encoding='utf-8')
        with patch.dict(sys.modules, {'components.mqtt_telemetry': None, 'components.mqtt_transport': None}):
            self.runtime.mqtt_tick()
        self.assertIsNone(self.runtime.mqtt)
        self.assertEqual(self.runtime._mqtt_state, 'disabled')
        self.module.gc.collect.assert_not_called()

    def test_mqtt_enabled_flag_is_cached_without_heavy_imports_before_wifi_and_ntp(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        quality = {'synced': False, 'lastSyncAgeMs': None}
        with patch.dict(sys.modules, {'components.mqtt_telemetry': None, 'components.mqtt_transport': None}), \
                patch.object(self.runtime, '_mqtt_enabled_flag', wraps=self.runtime._mqtt_enabled_flag) as probe, \
                patch.object(self.runtime.clock_sync, 'status', side_effect=lambda: quality), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True):
            for self.clock.value in (0, 100, 1000):
                self.runtime.mqtt_tick()
                self.assertEqual(self.runtime._mqtt_state, 'waiting_network')
            self.wlan.connected = True
            for self.clock.value in (2000, 3000, 4000):
                self.runtime.mqtt_tick()
                self.assertEqual(self.runtime._mqtt_state, 'waiting_clock')
            probe.assert_called_once()
        self.assertTrue(self.runtime._mqtt_enabled)
        self.assertFalse(self.runtime._mqtt_initialized)
        self.assertIsNone(self.runtime.mqtt)
        self.module.gc.collect.assert_not_called()

    def test_mqtt_constructs_once_only_after_ready_and_gc_is_not_periodic(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        quality = {'synced': True, 'lastSyncAgeMs': 600001}
        config = {'enabled': True}
        client, publisher = Mock(), Mock()
        load, transport = Mock(return_value=config), Mock(return_value=client)
        sampler = Mock(return_value=publisher)
        with patch.dict(sys.modules, {
                'components.mqtt_telemetry': types.SimpleNamespace(load_config=load, TemperatureTelemetry=sampler),
                'components.mqtt_transport': types.SimpleNamespace(MqttTransport=transport)}), \
                patch.object(self.runtime.clock_sync, 'status', side_effect=lambda: quality), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True), \
                patch.object(self.module.gc, 'threshold', return_value=-1, create=True) as threshold:
            self.runtime.mqtt_tick()
            self.wlan.connected = True
            self.runtime.mqtt_tick()
            load.assert_not_called()
            transport.assert_not_called()
            quality['lastSyncAgeMs'] = 600000
            self.runtime.mqtt_tick()
            for self.clock.value in (10, 1000, 60000, 120000):
                self.runtime.mqtt_tick()
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,)])
        load.assert_called_once_with(self.runtime._mqtt_config_path)
        transport.assert_called_once_with(config, self.runtime.service.now)
        sampler.assert_called_once_with(config, client, self.runtime.service.now, logger=self.runtime.service.log)
        self.assertIs(self.runtime.mqtt, publisher)
        self.assertTrue(self.runtime._mqtt_initialized)
        self.assertEqual(self.module.gc.collect.call_count, 2)
        self.assertEqual(publisher.tick.call_count, 5)

    def test_mqtt_gc_threshold_preserves_disabled_original_and_restores_once_on_failure(self):
        with patch.object(self.module.gc, 'threshold', return_value=-1, create=True) as threshold:
            self.runtime._set_mqtt_gc_threshold()
            self.runtime._set_mqtt_gc_threshold()
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,)])
            self.runtime._mqtt_init_failed(None, False)
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,), (-1,)])
            self.runtime._restore_mqtt_gc_threshold()
            self.assertEqual(threshold.call_count, 3)
            self.assertIsNone(self.runtime._mqtt_gc_previous)
            self.assertEqual(self.runtime._mqtt_state, 'initialization_retry')

    def test_mqtt_disabled_path_restores_owned_gc_threshold_without_periodic_sets(self):
        with patch.object(self.module.gc, 'threshold', return_value=16384, create=True) as threshold:
            self.runtime._set_mqtt_gc_threshold()
            self.runtime._mqtt_enabled = False
            for self.clock.value in (0, 1000, 60000):
                self.runtime.mqtt_tick()
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,), (16384,)])
        self.module.gc.collect.assert_not_called()

    def test_mqtt_gc_threshold_missing_or_faulty_api_never_blocks_runtime(self):
        self.runtime._set_mqtt_gc_threshold()
        self.assertIsNone(self.runtime._mqtt_gc_previous)
        cases = ([OSError('private-get')],
                 [-1, OSError('private-set'), None],
                 [-1, OSError('private-set'), OSError('private-restore')],
                 [None], [True], [-2])
        for values in cases:
            with self.subTest(case=len(values)), \
                    patch.object(self.module.gc, 'threshold', side_effect=values, create=True):
                self.runtime._set_mqtt_gc_threshold()
                self.assertIsNone(self.runtime._mqtt_gc_previous)
        self.assertNotIn('private', repr(self.runtime.service.logs))

    def test_runtime_cleanup_restores_gc_threshold_even_when_mqtt_close_fails(self):
        self.runtime.mqtt = Mock()
        self.runtime.mqtt.close.side_effect = OSError('private-close')
        with patch.object(self.module.gc, 'threshold', return_value=16384, create=True) as threshold, \
                patch.object(self.module, 'DiagnosticRuntime', return_value=self.runtime), \
                patch.object(self.runtime, 'run', side_effect=KeyboardInterrupt), \
                patch.object(self.runtime.service, 'stop') as stop:
            self.runtime._set_mqtt_gc_threshold()
            with self.assertRaises(OSError):
                self.module.run()
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,), (16384,)])
        stop.assert_called_once()

    def test_runtime_cleanup_survives_gc_threshold_restore_error(self):
        with patch.object(self.module.gc, 'threshold', side_effect=[-1, None, OSError('private')], create=True), \
                patch.object(self.module, 'DiagnosticRuntime', return_value=self.runtime), \
                patch.object(self.runtime, 'run', side_effect=KeyboardInterrupt), \
                patch.object(self.runtime.service, 'stop') as stop:
            self.runtime._set_mqtt_gc_threshold()
            with self.assertRaises(KeyboardInterrupt):
                self.module.run()
        stop.assert_called_once()

    def test_mqtt_invalid_rtc_keeps_heavy_modules_unloaded(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        self.wlan.connected = True
        with patch.dict(sys.modules, {'components.mqtt_telemetry': None, 'components.mqtt_transport': None}), \
                patch.object(self.runtime.clock_sync, 'status', return_value={'synced': True, 'lastSyncAgeMs': 10}), \
                patch.object(self.module.time, 'gmtime', create=True) as rtc:
            for parts in ((2023, 10, 30, 6, 0, 0), (2026, 0, 1, 6, 0, 0),
                          (2026, 1, 32, 6, 0, 0), (2026, 1, 1, 24, 0, 0), (2026,)):
                rtc.return_value = parts
                self.runtime.mqtt_tick()
                self.assertEqual(self.runtime._mqtt_state, 'waiting_clock')
            rtc.side_effect = OSError('private')
            self.runtime.mqtt_tick()
            self.assertEqual(self.runtime._mqtt_state, 'waiting_clock')
        self.assertIsNone(self.runtime.mqtt)
        self.module.gc.collect.assert_not_called()

    def test_mqtt_configuration_error_is_fail_closed_without_repeated_reads(self):
        for text in ('{"enabled":"true"}', '{bad-json}', '[]'):
            with self.subTest(text=text):
                self.runtime._mqtt_enabled = None
                self.runtime._mqtt_initialized = False
                self.runtime._mqtt_state = 'disabled'
                self.clock.value += 40000
                Path(self.runtime._mqtt_config_path).write_text(text, encoding='utf-8')
                with patch.object(self.runtime, '_mqtt_enabled_flag', wraps=self.runtime._mqtt_enabled_flag) as probe:
                    self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
                    self.assertEqual(self.runtime._mqtt_state, 'configuration_error')
                    for offset in (1000, 60000, 300000):
                        self.clock.value += offset
                        self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
                    probe.assert_called_once()
                self.assertTrue(self.runtime._mqtt_initialized)
                self.assertFalse(self.runtime._mqtt_enabled)

    def test_mqtt_memory_failure_during_probe_retries_with_bounded_backoff(self):
        with patch.object(self.runtime, '_mqtt_enabled_flag', side_effect=[MemoryError('private'), False]) as probe:
            self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
            self.assertFalse(self.runtime._mqtt_initialized)
            self.assertEqual(self.runtime._mqtt_state, 'initialization_retry')
            for self.clock.value in (1000, 10000, 29999):
                self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
            self.assertEqual(probe.call_count, 1)
            self.clock.value = 30000
            self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
            self.assertEqual(probe.call_count, 2)
        self.assertTrue(self.runtime._mqtt_initialized)
        self.assertEqual(self.runtime._mqtt_state, 'disabled')
        self.assertEqual(self.module.gc.collect.call_count, 1)
        self.assertNotIn('private', repr(self.runtime.service.logs))

    def test_mqtt_heavy_oom_closes_partial_client_and_recovers_after_delay(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        self.wlan.connected = True
        failed_client, good_client, publisher = Mock(), Mock(), Mock()
        load = Mock(return_value={'enabled': True})
        transport = Mock(side_effect=[failed_client, good_client])
        sampler = Mock(side_effect=[MemoryError('private-config'), publisher])
        with patch.dict(sys.modules, {
                'components.mqtt_telemetry': types.SimpleNamespace(load_config=load, TemperatureTelemetry=sampler),
                'components.mqtt_transport': types.SimpleNamespace(MqttTransport=transport)}), \
                patch.object(self.runtime.clock_sync, 'status', return_value={'synced': True, 'lastSyncAgeMs': 10}), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True):
            self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
            failed_client.close.assert_called_once()
            self.assertIsNone(self.runtime.mqtt)
            self.assertEqual(self.runtime._mqtt_state, 'initialization_retry')
            for self.clock.value in (1000, 10000, 29999):
                self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
            self.assertEqual(load.call_count, 1)
            self.clock.value = 30000
            self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
        self.assertIs(self.runtime.mqtt, publisher)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(self.module.gc.collect.call_count, 4)
        good_client.close.assert_not_called()
        self.assertNotIn('private-config', repr(self.runtime.service.logs))

    def test_mqtt_init_retries_increase_to_five_minutes_without_reopening_config_probe(self):
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        self.wlan.connected = True
        load = Mock(side_effect=MemoryError('private-config'))
        with patch.dict(sys.modules, {
                'components.mqtt_telemetry': types.SimpleNamespace(load_config=load, TemperatureTelemetry=Mock())}), \
                patch.object(self.runtime, '_mqtt_enabled_flag', wraps=self.runtime._mqtt_enabled_flag) as probe, \
                patch.object(self.runtime.clock_sync, 'status', return_value={'synced': True, 'lastSyncAgeMs': 10}), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True):
            for delay in (30000, 60000, 120000, 240000, 300000, 300000):
                attempted_at = self.clock.value
                self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
                self.assertEqual(self.runtime._mqtt_retry_at, attempted_at + delay)
                self.clock.value += delay - 1
                calls = load.call_count
                self.runtime._run_stage('mqtt', self.runtime.mqtt_tick)
                self.assertEqual(load.call_count, calls)
                self.clock.value += 1
            probe.assert_called_once()
        self.assertEqual(load.call_count, 6)

    def test_mqtt_time_gate_requires_recent_real_sync_and_is_independent_of_debug(self):
        self.runtime.mqtt = Mock()
        self.runtime._mqtt_initialized = True
        self.wlan.connected = True
        self.assertFalse(self.runtime.service.enabled)
        quality = {'synced': False, 'lastSyncAgeMs': None}
        with patch.object(self.runtime.clock_sync, 'status', side_effect=lambda: quality), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True):
            self.runtime.mqtt_tick()
            self.runtime.mqtt.tick.assert_called_with(True, False)
            quality.update(synced=True, lastSyncAgeMs=600000)
            self.runtime.mqtt_tick()
            self.runtime.mqtt.tick.assert_called_with(True, True)
            quality['lastSyncAgeMs'] = 600001
            self.runtime.mqtt_tick()
            self.runtime.mqtt.tick.assert_called_with(True, False)
            quality['lastSyncAgeMs'] = -1
            self.runtime.mqtt_tick()
            self.runtime.mqtt.tick.assert_called_with(True, False)
        self.assertFalse(self.runtime.service.enabled)

    def test_mqtt_setup_failure_does_not_expose_configuration_or_stop_clock(self):
        self.runtime._mqtt_config_path = str(Path(self.config.path).with_name('.mqtt.json'))
        Path(self.runtime._mqtt_config_path).write_text('{"enabled":true}', encoding='utf-8')
        self.wlan.connected = True
        with patch.object(self.runtime, 'poll_commands'), patch.object(self.runtime, '_network_step'), \
                patch.object(self.runtime, 'maintenance_tick'), patch.object(self.runtime, 'clock_tick') as clock, \
                patch.object(self.runtime.service, 'tick'), \
                patch.object(self.runtime.clock_sync, 'status', return_value={'synced': True, 'lastSyncAgeMs': 10}), \
                patch.object(self.module.time, 'gmtime', return_value=(2026, 10, 30, 6, 0, 0), create=True):
            self.runtime.tick_once()
            self.assertIsNone(self.runtime.mqtt)
            self.assertEqual(self.runtime._mqtt_state, 'configuration_error')
            self.assertEqual(clock.call_count, 1)
            self.assertTrue(any('Runtime mqtt failed' in entry['message'] for entry in self.runtime.service.logs))

    def test_runtime_stop_closes_mqtt_even_if_cleanup_raises_and_still_stops_debug(self):
        self.runtime.mqtt = Mock()
        self.runtime.mqtt.close.side_effect = OSError('private-account')
        with patch.object(self.module, 'DiagnosticRuntime', return_value=self.runtime), \
                patch.object(self.runtime, 'run', side_effect=KeyboardInterrupt), \
                patch.object(self.runtime.service, 'stop') as stop:
            with self.assertRaises(OSError):
                self.module.run()
        self.runtime.mqtt.close.assert_called_once()
        stop.assert_called_once()

    def test_trial_cannot_commit_an_old_asynchronously_disconnecting_connection(self):
        self.wlan.connected = True
        self.wlan.delay_disconnect = True
        self.command('debug.set', {'enabled': True}, 'enable')
        self.command('config.set', {'revision': 0, 'values': {'SSID': 'New'}})
        self.runtime.network_tick()
        self.assertEqual(self.config.revision, 0)
        self.assertIsNotNone(self.config.pending)
        self.assertEqual(self.wlan.connects, [])
        self.assertEqual(self.runtime.network_state, 'disconnecting')
        self.wlan.connected = False
        self.clock.value = 10
        self.runtime.network_tick()
        self.assertEqual(self.wlan.connects, [('New', 'original-private')])
        self.assertEqual(self.config.revision, 0)
        self.runtime.network_tick()
        self.assertEqual(self.config.revision, 0)
        self.wlan.connected = True
        self.clock.value = 20
        self.runtime.network_tick()
        self.assertEqual(self.config.revision, 1)
        self.assertEqual(self.config.values['SSID'], 'New')


class ClockCompatibilityTests(unittest.TestCase):
    def test_fractional_timezone_passes_integer_seconds_to_micropython_localtime(self):
        timestamps = []

        def localtime(timestamp=None):
            if timestamp is not None:
                # MicroPython 1.22 rejects floats even when they are integral.
                if type(timestamp) is not int:
                    raise TypeError('integer required')
                timestamps.append(timestamp)
            return (2026, 10, 30, 14, 30, 0, 4, 303)

        fake_time = types.SimpleNamespace(localtime=localtime, mktime=lambda _: 1000)
        spec = importlib.util.spec_from_file_location('_time_sync_test', ROOT / 'components/time_sync.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'ntptime': types.SimpleNamespace(),
                                     'utime': fake_time, 'time': fake_time}):
            spec.loader.exec_module(module)
        result = module.get_local_time(5.5)
        self.assertEqual(timestamps, [20800])
        self.assertEqual(result['formatted_simple_date_time'], '10-30 14:30:00')


if __name__ == '__main__':
    unittest.main()
