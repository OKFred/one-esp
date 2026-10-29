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
        self.ntp = Mock(return_value=True)
        self.tcp = Mock(return_value={'ok': True, 'data': {'ping': 42}})
        self.local_time = Mock(return_value={'formatted_simple_date_time': '10-30 12:34:56',
                                            'formatted_time': '12:34:56'})
        modules = {
            'machine': self.machine,
            'network': types.SimpleNamespace(WLAN=lambda _: self.wlan, STA_IF=0,
                                            STAT_WRONG_PASSWORD=202, STAT_NO_AP_FOUND=201,
                                            STAT_IDLE=1000, STAT_CONNECTING=1001),
            'time': self.clock, 'gc': types.SimpleNamespace(mem_free=lambda: 200000),
            'uselect': types.SimpleNamespace(poll=lambda: Mock(), POLLIN=1),
            'components.time_sync': types.SimpleNamespace(time_sync=self.ntp, get_local_time=self.local_time),
            'components.socket_test': types.SimpleNamespace(socket_test=self.tcp),
        }
        spec = importlib.util.spec_from_file_location('_runtime_test', ROOT / 'application/runtime.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.module)
        self.output = io.StringIO()
        with patch.object(self.module, 'ConfigStore', return_value=self.config), contextlib.redirect_stdout(self.output):
            self.runtime = self.module.DiagnosticRuntime()
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

    def test_debug_pauses_network_jobs_but_clock_keeps_refreshing(self):
        self.wlan.connected = True
        self.runtime.network_state = 'connected'
        self.runtime.service.enabled = True
        self.runtime.maintenance_tick()
        self.runtime.clock_tick()
        self.ntp.assert_not_called()
        self.tcp.assert_not_called()
        self.local_time.assert_called_once_with(8.0)
        self.assertTrue(self.runtime.status()['maintenancePaused'])
        self.runtime.service.enabled = False
        self.runtime.maintenance_tick()
        self.ntp.assert_called_once_with('ntp.aliyun.com', timeout=2)
        self.clock.value = 101
        self.runtime.maintenance_tick()
        self.tcp.assert_called_once_with({'host': 'baidu.com', 'port': 443}, timeout=2)
        self.assertEqual(self.runtime.tcp_ms, 42)

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
