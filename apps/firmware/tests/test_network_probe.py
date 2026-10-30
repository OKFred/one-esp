"""TCP probe state-machine tests; no network or ESP32 is required."""

import errno
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.network_probe import NetworkProbe, POLL_OUT, POLL_ERRORS


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value


class Socket:
    """Deliberately lacks getsockopt, getpeername and payload-write methods."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.addresses = []
        self.blocking = None
        self.closed = False

    def setblocking(self, blocking):
        self.blocking = blocking

    def connect(self, address):
        self.addresses.append(address)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise OSError(outcome)

    def close(self):
        self.closed = True


class Poll:
    def __init__(self):
        self.events = []
        self.register = Mock()
        self.unregister = Mock()
        self.timeouts = []

    def poll(self, timeout):
        self.timeouts.append(timeout)
        events, self.events = self.events, []
        return events


class NetworkProbeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.poll = Poll()
        self.sockets = Mock()
        self.sockets.SOCK_STREAM = 1
        self.address = ('192.0.2.10', 443)
        self.sockets.getaddrinfo.return_value = [(2, 1, 6, '', self.address)]
        self.probe = NetworkProbe(self.clock.now, self.sockets, lambda: self.poll)

    def socket(self, *outcomes):
        active = Socket(outcomes)
        self.sockets.socket.return_value = active
        return active

    def assert_released(self, active, registered=True):
        self.assertFalse(self.probe.busy)
        self.assertTrue(active.closed)
        if registered:
            self.poll.unregister.assert_called_once_with(active)
        self.assertIsNone(self.probe.tick())

    def test_pending_then_eisconn_confirms_success_without_payload(self):
        active = self.socket(errno.EINPROGRESS, errno.EISCONN)
        self.clock.value = 100
        self.assertIsNone(self.probe.start('example.test', 443))
        self.assertTrue(self.probe.busy)
        self.assertIs(active.blocking, False)
        self.sockets.getaddrinfo.assert_called_once_with('example.test', 443, 0, 1)
        self.poll.register.assert_called_once_with(active, POLL_OUT | POLL_ERRORS)
        self.clock.value = 137
        self.poll.events = [(active, POLL_OUT)]
        self.assertEqual(self.probe.tick(), {'ok': True, 'ping': 37,
                                          'startedAtMs': 100, 'completedAtMs': 137})
        self.assertEqual(active.addresses, [self.address, self.address])
        self.assertEqual(self.poll.timeouts, [0])
        self.assert_released(active)

    def test_esp32_missing_eisconn_uses_newlib_127_without_accepting_106(self):
        source = ROOT / 'components/network_probe.py'
        reduced_errno = types.SimpleNamespace(EINPROGRESS=119, EALREADY=120, EAGAIN=11)
        spec = importlib.util.spec_from_file_location('_esp32_probe_test', source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'uerrno': reduced_errno}), patch.object(sys, 'platform', 'esp32'):
            spec.loader.exec_module(module)
        for completed_errno, expected in ((127, True), (106, False)):
            with self.subTest(errno=completed_errno):
                self.setUp()
                self.probe = module.NetworkProbe(self.clock.now, self.sockets, lambda: self.poll)
                active = self.socket(119, completed_errno)
                self.assertIsNone(self.probe.start('example.test', 443))
                self.poll.events = [(active, POLL_OUT)]
                self.assertIs(self.probe.tick()['ok'], expected)
                self.assert_released(active)

    def test_exported_eisconn_takes_priority_over_platform_fallback(self):
        source = ROOT / 'components/network_probe.py'
        reduced_errno = types.SimpleNamespace(EINPROGRESS=119, EALREADY=120, EAGAIN=11, EISCONN=555)
        spec = importlib.util.spec_from_file_location('_exported_errno_probe_test', source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'uerrno': reduced_errno}), patch.object(sys, 'platform', 'esp32'):
            spec.loader.exec_module(module)
        self.probe = module.NetworkProbe(self.clock.now, self.sockets, lambda: self.poll)
        active = self.socket(119, 555)
        self.assertIsNone(self.probe.start('example.test', 443))
        self.poll.events = [(active, POLL_OUT)]
        self.assertTrue(self.probe.tick()['ok'])
        self.assert_released(active)

    def test_writable_refused_is_failure(self):
        active = self.socket(errno.EINPROGRESS, errno.ECONNREFUSED)
        self.probe.start('example.test', 443)
        self.poll.events = [(active, POLL_OUT)]
        result = self.probe.tick()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['ping'])
        self.assert_released(active)

    def test_second_connect_can_return_none_or_remain_pending(self):
        active = self.socket(errno.EINPROGRESS, errno.EALREADY, None)
        self.probe.start('example.test', 443)
        self.poll.events = [(active, POLL_OUT)]
        self.assertIsNone(self.probe.tick())
        self.assertTrue(self.probe.busy)
        self.poll.events = [(active, POLL_OUT)]
        self.assertTrue(self.probe.tick()['ok'])
        self.assert_released(active)

    def test_poll_errors_override_writable(self):
        for flag in (8, 16, 32):
            with self.subTest(flag=flag):
                self.setUp()
                active = self.socket(errno.EINPROGRESS)
                self.probe.start('example.test', 443)
                self.poll.events = [(active, POLL_OUT | flag)]
                self.assertFalse(self.probe.tick()['ok'])
                self.assertEqual(len(active.addresses), 1)
                self.assert_released(active)

    def test_timeout_closes_and_unregisters(self):
        active = self.socket(errno.EINPROGRESS)
        self.probe.start('example.test', 443)
        self.clock.value = 1999
        self.assertIsNone(self.probe.tick())
        self.clock.value = 2000
        self.assertEqual(self.probe.tick(), {'ok': False, 'ping': None,
                                          'startedAtMs': 0, 'completedAtMs': 2000})
        self.assert_released(active)

    def test_dns_cache_survives_sixty_seconds_and_target_change_resolves(self):
        self.socket(None)
        self.probe.start('example.test', 443)
        self.clock.value = 60000
        self.socket(None)
        self.probe.start('example.test', 443)
        self.sockets.getaddrinfo.assert_called_once()
        self.socket(None)
        self.probe.start('other.test', 443)
        self.assertEqual(self.sockets.getaddrinfo.call_count, 2)
        self.socket(None)
        self.probe.start('other.test', 80)
        self.assertEqual(self.sockets.getaddrinfo.call_count, 3)

    def test_dns_delay_is_excluded_from_ping_and_connect_deadline(self):
        def resolve(*args):
            self.clock.value = 5000
            return [(2, 1, 6, '', self.address)]
        self.sockets.getaddrinfo.side_effect = resolve
        active = self.socket(errno.EINPROGRESS, errno.EISCONN)
        self.assertIsNone(self.probe.start('example.test', 443))
        self.clock.value = 5030
        self.poll.events = [(active, POLL_OUT)]
        self.assertEqual(self.probe.tick(), {'ok': True, 'ping': 30,
                                          'startedAtMs': 0, 'completedAtMs': 5030})
        self.assert_released(active)

    def test_resolver_failure_is_an_immediate_sample_and_can_retry(self):
        self.sockets.getaddrinfo.side_effect = OSError(-2)
        self.assertEqual(self.probe.start('example.test', 443),
                         {'ok': False, 'ping': None, 'startedAtMs': 0, 'completedAtMs': 0})
        self.assertFalse(self.probe.busy)
        self.sockets.socket.assert_not_called()
        self.assertIsNone(self.probe.tick())
        self.sockets.getaddrinfo.side_effect = None
        active = self.socket(None)
        self.assertTrue(self.probe.start('example.test', 443)['ok'])
        self.assertEqual(self.sockets.getaddrinfo.call_count, 2)
        self.assert_released(active, registered=False)

    def test_immediate_success_and_failure_release_socket_without_polling(self):
        for outcome, ok in ((None, True), (errno.ECONNREFUSED, False)):
            with self.subTest(outcome=outcome):
                self.setUp()
                active = self.socket(outcome)
                self.assertEqual(self.probe.start('example.test', 443)['ok'], ok)
                self.poll.register.assert_not_called()
                self.assert_released(active, registered=False)

    def test_cancel_releases_without_sample_and_preserves_cache(self):
        active = self.socket(errno.EINPROGRESS)
        self.probe.start('example.test', 443)
        self.assertIsNone(self.probe.cancel())
        self.assert_released(active)
        self.socket(None)
        self.assertTrue(self.probe.start('example.test', 443)['ok'])
        self.sockets.getaddrinfo.assert_called_once()

    def test_all_pending_errno_values_are_accepted(self):
        for number in (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN, errno.EWOULDBLOCK):
            with self.subTest(number=number):
                self.setUp()
                active = self.socket(number)
                self.assertIsNone(self.probe.start('example.test', 443))
                self.probe.cancel()
                self.assert_released(active)

    def test_setup_and_poll_failures_always_release_socket(self):
        for operation in ('setblocking', 'register', 'poll'):
            with self.subTest(operation=operation):
                self.setUp()
                active = self.socket(errno.EINPROGRESS)
                if operation == 'setblocking':
                    active.setblocking = Mock(side_effect=OSError('socket setup failed'))
                elif operation == 'register':
                    self.poll.register.side_effect = OSError('register failed')
                else:
                    self.poll.poll = Mock(side_effect=OSError('poll failed'))
                result = self.probe.start('example.test', 443)
                if operation == 'poll':
                    result = self.probe.tick()
                self.assertFalse(result['ok'])
                self.assert_released(active, registered=operation != 'setblocking')

    def test_unregister_failure_does_not_prevent_close(self):
        active = self.socket(errno.EINPROGRESS)
        self.probe.start('example.test', 443)
        self.poll.unregister.side_effect = OSError('already gone')
        self.probe.cancel()
        self.assert_released(active)

    def test_socket_and_poll_creation_failure_do_not_leave_probe_busy(self):
        self.sockets.socket.side_effect = OSError('no socket available')
        self.assertFalse(self.probe.start('example.test', 443)['ok'])
        self.assertFalse(self.probe.busy)
        self.assertIsNone(self.probe.tick())
        self.sockets.socket.side_effect = None
        active = self.socket(errno.EINPROGRESS)
        self.probe = NetworkProbe(self.clock.now, self.sockets,
                                  Mock(side_effect=OSError('no poll available')))
        self.assertFalse(self.probe.start('example.test', 443)['ok'])
        self.assert_released(active, registered=False)


if __name__ == '__main__':
    unittest.main()
