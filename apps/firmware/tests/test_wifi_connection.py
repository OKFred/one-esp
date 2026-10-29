"""Host-side regression tests; no ESP32 or Wi-Fi credentials are required."""

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "unit-test-password"


def load_source(path, modules):
    spec = importlib.util.spec_from_file_location("_wifi_test_target", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules), patch.object(sys, "path", list(sys.path)):
        spec.loader.exec_module(module)
    return module


class Clock:
    PERIOD = 1 << 30

    def __init__(self, start=0):
        self.elapsed = 0
        self.start = start

    def ticks_ms(self):
        return (self.start + self.elapsed) % self.PERIOD

    def ticks_diff(self, new, old):
        half = self.PERIOD // 2
        return ((new - old + half) % self.PERIOD) - half

    def sleep_ms(self, milliseconds):
        self.elapsed += milliseconds
        if self.elapsed > 30000:
            raise AssertionError("Wi-Fi connection did not respect its deadline")

    def sleep(self, seconds):
        self.sleep_ms(int(seconds * 1000))


class WLAN:
    def __init__(self, clock, status=1001, success_after=None, connected=False,
                 connect_error=None):
        self.clock = clock
        self.final_status = status
        self.success_after = success_after
        self.connected = connected
        self.connect_error = connect_error
        self.started = None
        self.connect_calls = []
        self.config_calls = []
        self.disconnect_calls = 0

    def active(self, enabled):
        self.enabled = enabled

    def isconnected(self):
        return self.connected or (
            self.started is not None and self.success_after is not None
            and self.clock.elapsed - self.started >= self.success_after
        )

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        if self.connect_error:
            raise self.connect_error
        self.started = self.clock.elapsed

    def status(self):
        return 1010 if self.isconnected() else self.final_status

    def config(self, **kwargs):
        self.config_calls.append(kwargs)

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.started = None

    def ifconfig(self):
        return ("192.0.2.10", "255.255.255.0", "192.0.2.1", "192.0.2.1")


class WifiConnectorTests(unittest.TestCase):
    def make_connector(self, clock=None, **kwargs):
        clock = clock or Clock()
        wlan = WLAN(clock, **kwargs)
        network = types.SimpleNamespace(
            STA_IF=0, STAT_IDLE=1000, STAT_CONNECTING=1001,
            STAT_WRONG_PASSWORD=202, STAT_NO_AP_FOUND=201, STAT_GOT_IP=1010,
            WLAN=Mock(return_value=wlan),
        )
        module = load_source("components/wifi_connector.py", {
            "network": network, "time": clock,
        })
        return module.wifi_connector, wlan, clock

    def call(self, connector, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = connector("TestAP", TEST_PASSWORD, **kwargs)
        self.assertNotIn(TEST_PASSWORD, output.getvalue())
        self.assertNotIn(TEST_PASSWORD, repr(result))
        return result

    def test_success_preserves_network_fields_and_restores_reconnects(self):
        connector, wlan, _ = self.make_connector(success_after=750)
        result = self.call(connector)
        self.assertTrue(result["ok"])
        self.assertEqual(result["IP Address"], "192.0.2.10")
        self.assertEqual(result["Subnet Mask"], "255.255.255.0")
        self.assertEqual(result["Gateway"], "192.0.2.1")
        self.assertEqual(result["DNS"], "192.0.2.1")
        self.assertEqual(wlan.connect_calls, [("TestAP", TEST_PASSWORD)])
        self.assertIn({"reconnects": 0}, wlan.config_calls)
        self.assertEqual(wlan.config_calls[-1], {"reconnects": -1})

    def test_wrong_password_and_handshake_failures_prompt_for_password(self):
        for status in (202, 15, 204):
            with self.subTest(status=status):
                connector, wlan, clock = self.make_connector(status=status)
                result = self.call(connector, timeout=2)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "auth_failed")
                self.assertEqual(result["message"], "Check password")
                self.assertEqual(result["status"], status)
                self.assertGreaterEqual(clock.elapsed, 2000)
                self.assertGreater(wlan.disconnect_calls, 0)

    def test_missing_access_point_is_distinct_from_password_failure(self):
        connector, wlan, _ = self.make_connector(status=201)
        result = self.call(connector, timeout=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_ap")
        self.assertEqual(result["message"], "AP not found")
        self.assertEqual(result["status"], 201)
        self.assertGreater(wlan.disconnect_calls, 0)

    def test_idle_or_connecting_has_a_finite_timeout(self):
        for status in (1000, 1001):
            with self.subTest(status=status):
                connector, wlan, clock = self.make_connector(status=status)
                result = self.call(connector, timeout=2)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "timeout")
                self.assertEqual(result["message"], "Connect timeout")
                self.assertGreaterEqual(clock.elapsed, 2000)
                self.assertLess(clock.elapsed, 3000)
                self.assertGreater(wlan.disconnect_calls, 0)

    def test_unknown_failure_does_not_claim_password_is_wrong(self):
        connector, _, _ = self.make_connector(status=203)
        result = self.call(connector, timeout=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "connect_failed")
        self.assertEqual(result["message"], "Connect failed")

    def test_connect_oserror_becomes_a_failure_result(self):
        connector, wlan, _ = self.make_connector(connect_error=OSError(5))
        result = self.call(connector, timeout=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "connect_failed")
        self.assertEqual(result["message"], "Connect failed")
        self.assertGreater(wlan.disconnect_calls, 0)

    def test_old_failure_status_does_not_abort_a_new_connection(self):
        # v1.22.2 can retain a previous error while a new connection is pending.
        for old_status in (201, 202):
            with self.subTest(old_status=old_status):
                connector, _, clock = self.make_connector(
                    status=old_status, success_after=1250,
                )
                self.assertTrue(self.call(connector, timeout=3)["ok"])
                self.assertGreaterEqual(clock.elapsed, 1250)

    def test_timeout_survives_ticks_counter_wrap(self):
        clock = Clock(start=Clock.PERIOD - 500)
        connector, _, _ = self.make_connector(clock=clock)
        result = self.call(connector, timeout=2)
        self.assertEqual(result["error"], "timeout")
        self.assertGreaterEqual(clock.elapsed, 2000)
        self.assertLess(clock.elapsed, 3000)

    def test_already_connected_does_not_reconnect(self):
        connector, wlan, _ = self.make_connector(connected=True)
        self.assertTrue(self.call(connector)["ok"])
        self.assertEqual(wlan.connect_calls, [])
        self.assertEqual(wlan.disconnect_calls, 0)


class ApplicationFailureTests(unittest.TestCase):
    def test_failure_is_visible_and_stops_all_network_dependent_work(self):
        for error, message in (
            ("auth_failed", "Check password"),
            ("no_ap", "AP not found"),
            ("timeout", "Connect timeout"),
            ("connect_failed", "Connect failed"),
        ):
            with self.subTest(error=error):
                screen = Mock()
                socket_test, time_sync, get_local_time, modbus = (
                    Mock(), Mock(), Mock(), Mock(),
                )
                connector = Mock(return_value={
                    "ok": False, "error": error, "message": message, "status": 202,
                })
                env_reader = Mock(return_value={
                    "SSID": "TestAP", "PASSWORD": TEST_PASSWORD,
                })
                utime = types.SimpleNamespace(localtime=Mock())
                module = load_source("application/main.py", {
                    "time": types.SimpleNamespace(sleep=Mock()),
                    "utime": utime,
                    "components.env_reader": types.SimpleNamespace(env_reader=env_reader),
                    "components.wifi_connector": types.SimpleNamespace(wifi_connector=connector),
                    "components.screen_display": types.SimpleNamespace(screen_display=screen),
                    "components.socket_test": types.SimpleNamespace(socket_test=socket_test),
                    "components.time_sync": types.SimpleNamespace(
                        time_sync=time_sync, get_local_time=get_local_time,
                    ),
                    "components.modbus_talker": types.SimpleNamespace(modbus_talker=modbus),
                })
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    module.main()
                connector.assert_called_once_with("TestAP", TEST_PASSWORD)
                expected = "WiFi failed\n" + message + "\nEdit .env\nRestart to retry"
                self.assertEqual(screen.call_args.args[0], expected)
                self.assertTrue(all(len(line) <= 16 for line in expected.splitlines()))
                self.assertEqual(len(expected.splitlines()), 4)
                self.assertNotIn(TEST_PASSWORD, output.getvalue())
                self.assertNotIn(TEST_PASSWORD, repr(screen.call_args_list))
                for downstream in (socket_test, time_sync, get_local_time, modbus,
                                   utime.localtime):
                    downstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
