"""Verified TLS/QoS 1 state-machine regressions without network or credentials."""

import errno
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_transport import (MqttTransport, ThreadResolver, DNS_CACHE_MS,
                                       DNS_TIMEOUT_MS, CONNECT_TIMEOUT_MS,
                                       SESSION_TIMEOUT_MS, ACK_TIMEOUT_MS,
                                       PING_TIMEOUT_MS, IO_BYTES_PER_TICK,
                                       MAX_BACKOFF_MS, POLL_OUT)
from components.mqtt_wire import connect_packet, publish_packet


CONNACK = b'\x20\x02\x00\x00'
PUBACK = b'\x40\x02\x00\x01'
ADDRESS = (2, 1, 6, ('192.0.2.40', 8883))


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value


class RawSocket:
    def __init__(self, connects=None):
        self.connects = list(connects or [None])
        self.addresses = []
        self.blocking = None
        self.closed = False

    def setblocking(self, blocking):
        self.blocking = blocking

    def connect(self, address):
        self.addresses.append(address)
        result = self.connects.pop(0) if self.connects else OSError(errno.EALREADY)
        if isinstance(result, Exception):
            raise result

    def close(self):
        self.closed = True


class Stream:
    def __init__(self):
        self.raw = None
        self.blocking = None
        self.reads = []
        self.write_results = []
        self.writes = []
        self.read_sizes = []
        self.closed = False

    def setblocking(self, value):
        self.blocking = value

    def write(self, data):
        self.writes.append(bytes(data))
        result = self.write_results.pop(0) if self.write_results else len(data)
        if isinstance(result, Exception):
            raise result
        return result

    def read(self, size):
        self.read_sizes.append(size)
        result = self.reads.pop(0) if self.reads else None
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True
        if self.raw:
            self.raw.close()


class Poll:
    def __init__(self):
        self.events = []
        self.register = Mock()
        self.unregister = Mock()
        self.timeouts = []

    def poll(self, timeout):
        self.timeouts.append(timeout)
        result, self.events = self.events, []
        return result


class Resolver:
    def __init__(self):
        self.resolve = Mock(return_value=ADDRESS)
        self.invalidate = Mock()


class MqttTransportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.poll = Poll()
        self.resolver = Resolver()
        self.streams = []
        self.raws = []
        self.config = {'host': 'broker.example.test', 'server_hostname': 'broker.example.test',
                       'port': 8883, 'client_id': 'unit-client', 'username': 'unit-user',
                       'password': 'unit-only-password', 'ca_data': b'unit-ca-der', 'keepalive': 60}
        self.sockets = types.SimpleNamespace(AF_INET=2, SOCK_STREAM=1, socket=Mock())
        self.ssl = types.SimpleNamespace(CERT_REQUIRED=2, wrap_socket=Mock())
        self.collect = Mock()
        self.add_connection()
        self.client = MqttTransport(self.config, self.clock.now, self.sockets, self.ssl,
                                    self.resolver, lambda: self.poll, collect=self.collect)

    def add_connection(self, connects=None):
        raw, stream = RawSocket(connects), Stream()
        stream.raw = raw
        self.raws.append(raw)
        self.streams.append(stream)
        self.sockets.socket.side_effect = lambda *args: self.raws[len(self.sockets.socket.call_args_list) - 1]
        self.ssl.wrap_socket.side_effect = lambda *args, **kwargs: self.streams[len(self.ssl.wrap_socket.call_args_list) - 1]
        return raw, stream

    def connect(self, stream=None):
        stream = stream or self.streams[-1]
        self.client.tick(True, True)
        stream.reads.append(CONNACK)
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'connected')

    def publish(self, payload=b'{"sample":"unit"}'):
        self.assertTrue(self.client.publish('iot/v1/unit/gateway/devices/device/telemetry', payload))
        self.client.tick(True, True)
        return payload

    def test_waits_for_network_and_valid_time_without_creating_a_socket(self):
        self.client.tick(False, False)
        self.assertEqual(self.client.status()['phase'], 'waiting_network')
        self.client.tick(True, False)
        self.assertEqual(self.client.status()['phase'], 'waiting_time')
        self.sockets.socket.assert_not_called()
        self.resolver.resolve.assert_not_called()
        self.collect.assert_not_called()

    def test_collects_only_at_tls_and_failure_boundaries_not_each_tick(self):
        observations = []
        self.collect.side_effect = lambda: observations.append(
            (self.ssl.wrap_socket.call_count, self.raws[0].closed,
             self.client._raw is None, self.client._stream is None))
        self.connect()
        self.assertEqual(observations, [(0, False, False, True)])
        for _ in range(20):
            self.client.tick(True, True)
        self.assertEqual(self.collect.call_count, 1)
        self.streams[0].reads.append(b'')
        self.client.tick(True, True)
        self.assertEqual(observations[-1], (1, True, True, True))
        self.assertEqual(self.collect.call_count, 2)
        for _ in range(20):
            self.client.tick(True, True)
        self.assertEqual(self.collect.call_count, 2)

    def test_maintenance_close_collects_once_and_keeps_pending_payload(self):
        self.connect()
        payload = self.publish()
        self.collect.reset_mock()
        self.client.close()
        self.collect.assert_called_once_with()
        self.assertIs(self.client._pending[1], payload)
        for _ in range(20):
            self.client.tick(False, False)
        self.collect.assert_called_once_with()

    def test_optional_collector_error_cannot_prevent_safe_failure_cleanup(self):
        self.collect.side_effect = OSError('unit collector unavailable')
        self.connect()
        self.streams[0].reads.append(b'')
        self.client.tick(True, True)
        self.assertTrue(self.raws[0].closed)
        self.assertEqual(self.client.status()['phase'], 'backoff')

    def test_verified_tls_options_are_required_and_never_downgraded(self):
        self.connect()
        self.assertIs(self.raws[0].blocking, False)
        self.assertIs(self.streams[0].blocking, False)
        self.assertEqual(self.ssl.wrap_socket.call_args.kwargs,
                         {'cert_reqs': 2, 'cadata': b'unit-ca-der',
                          'server_hostname': 'broker.example.test', 'do_handshake': False})
        self.assertEqual(b''.join(self.streams[0].writes),
                         connect_packet('unit-client', 'unit-user', 'unit-only-password', 60))

    def test_missing_ca_or_verification_capability_fails_closed(self):
        for changes in ({'ca_data': b''}, {'ca_data': None}, {'broker_ip': 'dns.example.test'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MqttTransport(dict(self.config, **changes), self.clock.now,
                              self.sockets, self.ssl, self.resolver)
        with self.assertRaises(ValueError):
            MqttTransport(self.config, self.clock.now, self.sockets, types.SimpleNamespace())
        self.sockets.socket.assert_not_called()

    def test_numeric_endpoint_preserves_hostname_verification_and_skips_dns(self):
        self.config['broker_ip'] = '192.0.2.55'
        self.connect()
        self.resolver.resolve.assert_not_called()
        self.assertEqual(self.raws[0].addresses, [('192.0.2.55', 8883)])
        self.assertEqual(self.ssl.wrap_socket.call_args.kwargs['server_hostname'], 'broker.example.test')

    def test_partial_tcp_connection_is_polled_with_zero_wait(self):
        self.raws[0].connects = [OSError(errno.EINPROGRESS), OSError(errno.EISCONN)]
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'connecting')
        self.ssl.wrap_socket.assert_not_called()
        self.poll.events = [(self.raws[0], POLL_OUT)]
        self.client.tick(True, True)
        self.assertEqual(self.poll.timeouts, [0])
        self.assertEqual(self.client.status()['phase'], 'mqtt_connect')

    def test_tcp_timeout_and_refused_connect_close_before_retry(self):
        self.raws[0].connects = [OSError(errno.EINPROGRESS)]
        self.client.tick(True, True)
        self.clock.value = CONNECT_TIMEOUT_MS
        self.client.tick(True, True)
        self.assertTrue(self.raws[0].closed)
        self.assertEqual(self.client.status()['lastError'], 'connect_timeout')
        self.clock.value += 999
        self.client.tick(True, True)
        self.assertEqual(self.sockets.socket.call_count, 1)

    def test_tls_rejection_never_falls_back_or_publishes_plaintext(self):
        self.ssl.wrap_socket.side_effect = ValueError('unit certificate verification failed')
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'backoff')
        self.assertEqual(self.ssl.wrap_socket.call_count, 1)
        self.assertTrue(self.raws[0].closed)
        self.assertEqual(self.streams[0].writes, [])
        self.assertNotIn('unit certificate', repr(self.client.status()))

    def test_deferred_certificate_errors_close_raw_even_if_tls_has_detached_it(self):
        for operation in ('write', 'read'):
            with self.subTest(operation=operation):
                self.setUp()
                self.client.tick(True, True)
                stream, raw = self.streams[0], self.raws[0]
                stream.close = Mock()
                failure = ValueError('private unit certificate hostname')
                if operation == 'write':
                    stream.write_results.append(failure)
                else:
                    stream.reads.append(failure)
                self.client.tick(True, True)
                self.assertTrue(raw.closed)
                stream.close.assert_called_once()
                self.assertEqual(self.client.status()['phase'], 'backoff')
                self.assertNotIn('private unit certificate', repr(self.client.status()))

    def test_deferred_handshake_waits_without_mistaking_none_for_eof(self):
        stream = self.streams[0]
        self.client.tick(True, True)
        stream.write_results = [None, OSError(errno.EAGAIN), 3]
        for _ in range(3):
            self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'mqtt_connect')
        self.assertFalse(stream.closed)
        self.assertEqual(stream.writes[0], stream.writes[1])
        self.assertEqual(stream.writes[1], stream.writes[2])
        stream.reads.append(CONNACK)
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'connected')

    def test_stream_eof_and_io_errors_release_tls(self):
        for outcome in (b'', OSError(errno.ECONNRESET)):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.connect()
                self.streams[0].reads.append(outcome)
                self.client.tick(True, True)
                self.assertEqual(self.client.status()['phase'], 'backoff')
                self.assertTrue(self.streams[0].closed)

    def test_publish_requires_matching_puback_and_consumes_it_once(self):
        self.connect()
        self.publish()
        self.assertFalse(self.client.take_ack())
        self.assertTrue(self.client.status()['inflight'])
        self.streams[0].reads.extend([b'\x40\x02\x00\x02', b'\x40', b'\x02\x00\x01'])
        self.client.tick(True, True)
        self.assertFalse(self.client.take_ack())
        self.client.tick(True, True)
        self.assertFalse(self.client.take_ack())
        self.client.tick(True, True)
        self.assertFalse(self.client.status()['inflight'])
        self.assertFalse(self.client.publish('unit/topic', b'next'))
        self.assertTrue(self.client.take_ack())
        self.assertFalse(self.client.take_ack())
        self.assertTrue(self.client.publish('unit/topic', b'next'))

    def test_qos1_retry_keeps_exact_payload_and_packet_id_with_dup(self):
        self.connect()
        payload = self.publish(b'{"message_id":"unit-uuid","value":51.2}')
        first = self.streams[0].writes[-1]
        self.assertEqual(first[0], 0x32)
        self.clock.value = ACK_TIMEOUT_MS
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastError'], 'ack_timeout')
        self.assertIs(self.client._pending[1], payload)
        self.clock.value += 1000
        _, stream = self.add_connection()
        self.connect(stream)
        self.client.tick(True, True)
        retry = stream.writes[-1]
        self.assertEqual(retry[0], 0x3A)
        self.assertEqual(first[1:], retry[1:])
        stream.reads.append(PUBACK)
        self.client.tick(True, True)
        self.assertTrue(self.client.take_ack())

    def test_accepts_only_one_pending_message_even_while_offline(self):
        self.assertTrue(self.client.publish('unit/topic', b'first'))
        self.assertFalse(self.client.publish('unit/topic', b'second'))
        self.client.tick(False, False)
        self.assertTrue(self.client.status()['inflight'])
        self.client.close()
        self.assertTrue(self.client.status()['inflight'])
        self.connect()
        self.client.tick(True, True)
        self.assertEqual(self.streams[0].writes[-1], publish_packet('unit/topic', b'first', 1))

    def test_tls_or_connack_timeout_is_bounded(self):
        self.client.tick(True, True)
        self.streams[0].write_results = [None]
        self.client.tick(True, True)
        self.clock.value = SESSION_TIMEOUT_MS
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastError'], 'tls_or_connack_timeout')
        self.assertTrue(self.streams[0].closed)

    def test_write_partial_none_and_eagain_never_spin_or_exceed_one_chunk(self):
        self.connect()
        self.assertTrue(self.client.publish('unit/topic', b'x' * 1000))
        stream = self.streams[0]
        stream.write_results = [None, OSError(errno.EWOULDBLOCK), 7, 20]
        initial = len(stream.writes)
        for _ in range(4):
            previous = len(stream.writes)
            self.client.tick(True, True)
            self.assertEqual(len(stream.writes), previous + 1)
            self.assertLessEqual(len(stream.writes[-1]), IO_BYTES_PER_TICK)
        self.assertEqual(len(stream.writes), initial + 4)
        self.assertEqual(stream.writes[initial], stream.writes[initial + 1])
        self.assertTrue(self.client.status()['inflight'])
        self.assertTrue(all(size == IO_BYTES_PER_TICK for size in stream.read_sizes))

    def test_malformed_ack_and_clean_session_mismatch_fail_closed(self):
        for packet in (b'\x20\x02\x01\x00', b'\x20\x02\x00\x05', b'\x30\x00'):
            with self.subTest(packet=packet):
                self.setUp()
                self.client.tick(True, True)
                self.streams[0].reads.append(packet)
                self.client.tick(True, True)
                self.assertEqual(self.client.status()['phase'], 'backoff')

    def test_keepalive_requires_pingresp_and_reconnects_when_missing(self):
        self.connect()
        self.clock.value = 30000
        self.client.tick(True, True)
        self.assertEqual(self.streams[0].writes[-1], b'\xc0\x00')
        self.clock.value += PING_TIMEOUT_MS
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastError'], 'ping_timeout')
        self.assertTrue(self.streams[0].closed)

    def test_pingresp_clears_timeout_and_status_has_no_private_values(self):
        self.connect()
        self.clock.value = 30000
        self.streams[0].reads.append(b'\xd0\x00')
        self.client.tick(True, True)
        self.clock.value += PING_TIMEOUT_MS
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['phase'], 'connected')
        for key in ('password', 'username', 'client_id', 'host'):
            self.assertNotIn(self.config[key], repr(self.client.status()))

    def test_dns_timeout_does_not_open_a_socket_and_invalidates_late_result(self):
        self.resolver.resolve.return_value = None
        self.client.tick(True, True)
        self.clock.value = DNS_TIMEOUT_MS
        self.client.tick(True, True)
        self.resolver.invalidate.assert_called_once()
        self.sockets.socket.assert_not_called()
        self.assertEqual(self.client.status()['lastError'], 'dns_timeout')

    def test_backoff_is_exponential_and_capped(self):
        self.resolver.resolve.side_effect = OSError('unit dns failure')
        delays = []
        for _ in range(9):
            self.client.tick(True, True)
            delay = self.client.status()['nextRetryInMs']
            delays.append(delay)
            self.clock.value += delay
        self.assertEqual(delays, [1000, 2000, 4000, 8000, 16000, 32000, MAX_BACKOFF_MS,
                                 MAX_BACKOFF_MS, MAX_BACKOFF_MS])

    def test_wrap_failure_records_fixed_cause_before_cleanup_and_keeps_pending(self):
        payload = b'{"sample":"retained"}'
        self.assertTrue(self.client.publish('unit/topic', payload))
        self.ssl.wrap_socket.side_effect = ValueError('invalid cert')
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastFailure'], {
            'phase': 'connecting', 'operation': 'wrap', 'errorType': 'ValueError',
            'errno': None, 'reason': 'invalid_cert'})
        self.assertTrue(self.raws[0].closed)
        self.assertIs(self.client._pending[1], payload)
        snapshot = self.client.status()['lastFailure']
        snapshot['reason'] = 'changed by caller'
        self.assertEqual(self.client.status()['lastFailure']['reason'], 'invalid_cert')

    def test_certificate_verification_failure_does_not_echo_exception_details(self):
        self.client.tick(True, True)
        self.streams[0].write_results = [ValueError(
            '\nThe certificate Common Name (CN) does not match private-unit-secret')]
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastFailure'], {
            'phase': 'mqtt_connect', 'operation': 'write', 'errorType': 'ValueError',
            'errno': None, 'reason': 'certificate_verify'})
        self.assertNotIn('private-unit-secret', repr(self.client.status()))

    def test_read_failure_preserves_only_numeric_errno(self):
        self.client.tick(True, True)
        self.streams[0].reads = [OSError(errno.ENOMEM, 'private-unit-secret')]
        self.client.tick(True, True)
        self.assertEqual(self.client.status()['lastFailure'], {
            'phase': 'mqtt_connect', 'operation': 'read', 'errorType': 'OSError',
            'errno': errno.ENOMEM, 'reason': 'transport_failed'})
        self.assertNotIn('private-unit-secret', repr(self.client.status()))

    def test_protocol_rejection_is_distinct_from_tls_verification(self):
        for packet, reason, operation in (
                (b'\x20\x02\x00\x05', 'connack_refused', 'packet_decode'),
                (b'\x30\x00', 'malformed_packet', 'packet_decode'),
                (b'\x20\x02\x01\x00', 'malformed_packet', 'connack')):
            with self.subTest(reason=reason, operation=operation):
                self.setUp()
                self.client.tick(True, True)
                self.streams[0].reads = [packet]
                self.client.tick(True, True)
                failure = self.client.status()['lastFailure']
                self.assertEqual(failure['reason'], reason)
                self.assertEqual(failure['operation'], operation)
                self.assertEqual(failure['phase'], 'mqtt_connect')

    def test_unrecognized_exception_and_class_name_cannot_leak_private_values(self):
        failures = (ValueError('private-unit-secret'), OSError('private-unit-secret'),
                    type('private-unit-secret', (Exception,), {})('private-unit-secret'),
                    OSError(True, 'private-unit-secret'))
        for error in failures:
            with self.subTest(error_type=type(error).__name__):
                self.setUp()
                self.client.tick(True, True)
                self.streams[0].reads = [error]
                self.client.tick(True, True)
                failure = self.client.status()['lastFailure']
                self.assertNotIn('private-unit-secret', repr(self.client.status()))
                self.assertIsNone(failure['errno'])
                self.assertIn(failure['errorType'], ('ValueError', 'OSError', 'Exception'))
                self.assertIn(failure['reason'], ('value_error', 'transport_failed'))

    def test_resolver_socket_and_connect_failures_have_distinct_operations(self):
        for target, failure, operation, phase in (
                ('resolve', OSError(errno.EIO), 'resolve', 'resolving'),
                ('open', OSError(errno.ENOMEM), 'open', 'resolving'),
                ('connect', OSError(errno.ECONNREFUSED), 'connect', 'connecting')):
            with self.subTest(target=target):
                self.setUp()
                if target == 'resolve':
                    self.resolver.resolve.side_effect = failure
                elif target == 'open':
                    self.sockets.socket.side_effect = failure
                else:
                    self.raws[0].connects = [failure]
                self.client.tick(True, True)
                detail = self.client.status()['lastFailure']
                self.assertEqual(detail['operation'], operation)
                self.assertEqual(detail['phase'], phase)
                self.assertEqual(detail['errno'], failure.args[0])

    def test_diagnostic_failure_does_not_change_qos1_retry_bytes(self):
        self.connect()
        payload = self.publish(b'{"message_id":"unit-id","temperature":28.8}')
        original = self.streams[0].writes[-1]
        self.streams[0].reads = [OSError(errno.ENOMEM, 'private-unit-secret')]
        self.client.tick(True, True)
        self.assertIs(self.client._pending[1], payload)
        self.assertEqual(self.client.status()['lastFailure']['errno'], errno.ENOMEM)
        self.clock.value += 1000
        _, stream = self.add_connection()
        self.connect(stream)
        self.client.tick(True, True)
        retried = stream.writes[-1]
        self.assertEqual(retried[0], 0x3A)
        self.assertEqual(retried[1:], original[1:])
        stream.reads = [PUBACK]
        self.client.tick(True, True)
        self.assertTrue(self.client.take_ack())


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.tasks = []
        self.threads = types.SimpleNamespace(start_new_thread=lambda fn, args: self.tasks.append((fn, args)))
        self.sockets = types.SimpleNamespace(AF_INET=2, SOCK_STREAM=1,
                    getaddrinfo=Mock(return_value=[(2, 1, 6, '', ('192.0.2.40', 8883))]))
        self.resolver = ThreadResolver(self.sockets, self.threads)

    def finish(self):
        fn, args = self.tasks.pop(0)
        fn(*args)

    def test_dns_runs_only_in_worker_and_cached_results_expire(self):
        self.assertIsNone(self.resolver.resolve('broker.example.test', 8883, 0))
        self.sockets.getaddrinfo.assert_not_called()
        self.finish()
        self.assertEqual(self.resolver.resolve('broker.example.test', 8883, 0), ADDRESS)
        self.assertEqual(self.resolver.resolve('broker.example.test', 8883, DNS_CACHE_MS - 1), ADDRESS)
        self.assertIsNone(self.resolver.resolve('broker.example.test', 8883, DNS_CACHE_MS))
        self.assertEqual(len(self.tasks), 1)

    def test_timeout_does_not_spawn_another_worker_until_old_lookup_exits(self):
        self.resolver.resolve('old.example.test', 8883, 0)
        self.resolver.invalidate()
        for now in range(1, 10):
            self.assertIsNone(self.resolver.resolve('new.example.test', 8883, now))
            self.assertEqual(len(self.tasks), 1)
        self.finish()
        self.assertIsNone(self.resolver.resolve('new.example.test', 8883, 10))
        self.assertEqual(len(self.tasks), 1)
        self.finish()
        self.assertEqual(self.resolver.resolve('new.example.test', 8883, 11), ADDRESS)

    def test_failed_or_zero_address_lookup_has_no_valid_cached_result(self):
        self.sockets.getaddrinfo.return_value = [(2, 1, 6, '', ('0.0.0.0', 8883))]
        self.resolver.resolve('broker.example.test', 8883, 0)
        self.finish()
        with self.assertRaises(OSError):
            self.resolver.resolve('broker.example.test', 8883, 1)


if __name__ == '__main__':
    unittest.main()
