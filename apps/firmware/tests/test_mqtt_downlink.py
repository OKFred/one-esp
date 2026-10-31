"""Duplex ordering, subscription and sender handoff with entirely fake sockets."""

import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_transport import MqttTransport, ACK_TIMEOUT_MS
from components.mqtt_wire import (PacketReader, publish_packet, subscribe_packet,
                                  puback_packet, _frame, _u16)
from components.mqtt_commands import utc_seconds
from test_mqtt_transport import Clock, RawSocket, Stream, Poll, Resolver, CONNACK


class CapturingStream(Stream):
    def __init__(self):
        super().__init__()
        self.sent = bytearray()

    def write(self, data):
        count = super().write(data)
        if isinstance(count, int) and count > 0:
            self.sent.extend(bytes(data)[:count])
        return count


class MqttDownlinkTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.finishes = []
        self.time_ready = True
        self.config = {'host': 'broker.example.test', 'port': 8883,
                       'client_id': 'unit-client', 'username': 'unit-user',
                       'password': 'unit-fixture-secret', 'ca_data': b'unit-ca',
                       'site_id': 'unit', 'gateway_id': 'gateway', 'device_id': 'device',
                       'commands_enabled': True}

        def handler(command, finish):
            self.calls.append(command)
            self.finishes.append(finish)
            return {'ok': True, 'result': {'revision': 2, 'stage': 'applied'}}

        self.handler = handler
        self.client, self.stream = self.make_client()
        self.topic = 'iot/v1/unit/gateway/devices/device/commands'
        self.reply_topic = self.topic + '/ack'
        self.telemetry_topic = 'iot/v1/unit/gateway/devices/device/telemetry'

    def make_client(self, commands=True):
        stream = CapturingStream()
        raw = RawSocket()
        stream.raw = raw
        sockets = types.SimpleNamespace(AF_INET=2, SOCK_STREAM=1, socket=Mock(return_value=raw))
        ssl = types.SimpleNamespace(CERT_REQUIRED=2, wrap_socket=Mock(return_value=stream))
        settings = dict(self.config, commands_enabled=commands)
        client = MqttTransport(settings, self.clock.now, sockets, ssl,
            Resolver(), lambda: Poll(), collect=lambda: None,
            command_handler=self.handler,
            command_clock=lambda: (utc_seconds('2026-10-31T14:00:00Z') + self.clock.value // 1000,
                                   self.time_ready))
        return client, stream

    def connect(self, client=None, stream=None, grant=1):
        client, stream = client or self.client, stream or self.stream
        client.tick(True, True)
        stream.reads.append(CONNACK)
        client.tick(True, True)
        self.assertTrue(client.connected)
        if client._commands_enabled:
            identifier = client._subscribe_id
            stream.reads.append(b'\x90\x03' + _u16(identifier) + bytes((grant,)))
            client.tick(True, True)
        return client

    def request(self, identifier='request-1', method='sensor.config.set'):
        return json.dumps({'v': 1, 'id': identifier, 'command': method,
                           'args': {} if method.endswith('.get') else
                               {'revision': 1, 'values': {'interval_seconds': 60}},
                           'expiresAtUtc': '2026-10-31T14:00:30Z'}).encode()

    def incoming(self, payload=None, packet_id=17, topic=None, retained=False, qos=1):
        payload, topic = payload or self.request(), topic or self.topic
        if qos:
            frame = bytearray(publish_packet(topic, payload, packet_id))
            frame[0] |= int(retained)
            return bytes(frame)
        name = topic.encode()
        return _frame(0x31 if retained else 0x30, _u16(len(name)) + name + payload)

    def tick(self, count=1):
        for _ in range(count):
            self.client.tick(True, True)

    def test_subscription_grant_denial_and_disabled_mode_are_explicit(self):
        self.connect(grant=128)
        self.assertEqual(self.client.status()['commands']['state'], 'denied')
        self.assertTrue(self.client.connected)
        self.assertIn(subscribe_packet(self.topic, 1), self.stream.sent)
        self.assertTrue(self.client.publish(self.telemetry_topic, b'original'))
        self.tick()
        self.assertEqual(self.client._pending[2], 2)
        self.stream.reads.append(self.incoming())
        self.tick(2)
        self.assertEqual(self.calls, [])
        other, stream = self.make_client(commands=False)
        self.connect(other, stream)
        self.assertEqual(other.status()['commands']['state'], 'disabled')
        self.assertIsNone(other._commands)
        self.assertNotIn(b'\x82', stream.sent)

    def test_partial_telemetry_write_cannot_interleave_puback_or_command_reply(self):
        self.connect()
        start = len(self.stream.sent)
        payload = b'original-' + b'x' * 400
        self.client.publish(self.telemetry_topic, payload)
        identifier = self.client._pending[2]
        expected = publish_packet(self.telemetry_topic, payload, identifier)
        self.stream.write_results = [5]
        self.stream.reads.append(self.incoming())
        self.tick()
        self.assertEqual(bytes(self.stream.sent[start:]), expected[:5])
        self.tick(5)
        outgoing = bytes(self.stream.sent[start:])
        self.assertEqual(outgoing[:len(expected)], expected)
        packets = PacketReader(allow_commands=True).feed(outgoing[len(expected):])
        self.assertEqual(packets[0], (4, 0, _u16(17)))
        self.assertEqual(packets[1][0], 3)
        reply_id = self.client._reply_inflight[2]
        self.assertNotEqual(reply_id, identifier)
        self.assertEqual(len(self.calls), 1)
        self.stream.reads.append(puback_packet(reply_id))
        self.tick()
        self.assertFalse(self.client.take_ack())
        self.assertEqual(self.client.status()['commands']['replyAcknowledged'], 1)
        self.stream.reads.append(puback_packet(identifier))
        self.tick()
        self.assertTrue(self.client.take_ack())

    def test_wrong_topic_retained_expired_and_unsynchronized_requests_cannot_apply(self):
        self.connect()
        self.stream.reads = [self.incoming(topic=self.topic + '/other'), self.incoming(retained=True)]
        self.tick(4)
        self.time_ready = False
        self.stream.reads.append(self.incoming(packet_id=18))
        self.tick(2)
        self.time_ready = True
        expired = json.loads(self.request())
        expired['expiresAtUtc'] = '2026-10-31T13:59:59Z'
        self.stream.reads.append(self.incoming(json.dumps(expired).encode(), packet_id=19))
        self.tick(2)
        self.assertEqual(self.calls, [])
        status = self.client.status()['commands']
        self.assertEqual(status['wrongTopics'], 1)
        self.assertEqual(status['rejected'], 3)
        self.assertTrue(self.client.connected)
        self.assertIn(puback_packet(17), self.stream.sent)

    def test_duplicate_command_reuses_cached_result_and_qos0_has_no_protocol_ack(self):
        self.connect()
        self.stream.reads.append(self.incoming(qos=0))
        self.tick(3)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(puback_packet(17), self.stream.sent)
        self.stream.reads.append(self.incoming(packet_id=18))
        self.tick(3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.status()['commands']['duplicates'], 1)

    def test_async_final_reply_does_not_replace_an_inflight_testing_packet(self):
        def handler(command, finish):
            self.finishes.append(finish)
            return {'ok': True, 'result': {'stage': 'testing'}}
        self.handler = handler
        self.client, self.stream = self.make_client()
        self.connect()
        self.stream.reads.append(self.incoming())
        self.tick(3)
        first = self.client._reply_inflight
        self.assertEqual(json.loads(first[1])['result']['stage'], 'testing')
        self.finishes[0]({'ok': True, 'result': {'stage': 'applied', 'revision': 2}})
        self.tick()
        self.assertIs(self.client._reply_inflight, first)
        self.assertEqual(json.loads(self.client._reply_queue[0][1])['result']['stage'], 'applied')
        self.stream.reads.append(puback_packet(first[2]))
        self.tick(2)
        final = self.client._reply_inflight
        self.assertNotEqual(final[2], first[2])
        self.assertEqual(json.loads(final[1])['result']['stage'], 'applied')

    def test_reconnect_resubscribes_and_keeps_business_bytes_and_identifier(self):
        self.connect()
        payload = b'original payload'
        self.client.publish(self.telemetry_topic, payload)
        self.tick()
        pending = self.client._pending
        self.client.close()
        self.stream = CapturingStream()
        raw = RawSocket()
        self.stream.raw = raw
        self.client._sockets.socket.return_value = raw
        self.client._ssl.wrap_socket.return_value = self.stream
        self.connect()
        self.tick()
        self.assertIs(self.client._pending, pending)
        self.assertIs(self.client._pending[1], payload)
        self.assertIn(publish_packet(self.telemetry_topic, payload, pending[2], duplicate=True), self.stream.sent)
        self.assertEqual(self.client.status()['commands']['state'], 'ready')
        self.assertEqual(self.client.status()['connections'], 2)

    def test_subscription_timeout_preserves_telemetry_for_retry(self):
        self.client.publish(self.telemetry_topic, b'pending')
        self.client.tick(True, True)
        self.stream.reads.append(CONNACK)
        self.tick(2)
        self.clock.value = ACK_TIMEOUT_MS
        self.tick()
        self.assertEqual(self.client.status()['phase'], 'backoff')
        self.assertEqual(self.client.status()['lastError'], 'subscription_timeout')
        self.assertEqual(self.client._pending[1], b'pending')

    def test_handoff_waits_for_suback_when_original_publish_id_matches_new_subscription(self):
        old, old_stream = self.make_client(commands=False)
        self.connect(old, old_stream)
        payload = b'original payload bytes'
        old.publish(self.telemetry_topic, payload)
        old.tick(True, True)
        pending = old._pending
        self.assertEqual(pending[2], 1)
        old.close()
        old.release_context()
        self.client.tick(True, True)
        self.stream.reads.append(CONNACK)
        self.tick()
        self.assertEqual(self.client._subscribe_id, 1)
        self.client.transfer_pending_from(old)
        self.assertIs(self.client._pending, pending)
        self.assertIsNone(old._pending)
        start = len(self.stream.sent)
        self.tick(2)
        self.assertEqual(bytes(self.stream.sent[start:]), subscribe_packet(self.topic, 1))
        self.stream.reads.append(b'\x90\x03\x00\x01\x01')
        self.tick(2)
        self.assertIn(publish_packet(self.telemetry_topic, payload, 1, duplicate=True), self.stream.sent[start:])
        self.assertEqual(self.client.status()['connections'], 2)

    def test_handoff_and_context_release_require_inactive_old_sender(self):
        self.connect()
        with self.assertRaises(ValueError):
            self.client.release_context()
        other, other_stream = self.make_client()
        self.connect(other, other_stream)
        with self.assertRaises(ValueError):
            other.transfer_pending_from(self.client)

    def test_candidate_can_check_subscription_without_executing_commands_before_commit(self):
        self.client.set_commands_active(False)
        self.connect()
        self.assertEqual(self.client.status()['commands']['state'], 'ready')
        self.assertFalse(self.client.status()['commands']['active'])
        self.stream.reads.append(self.incoming())
        self.tick(2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.status()['commands']['blocked'], 1)
        self.client.set_commands_active(True)
        self.stream.reads.append(self.incoming(packet_id=18))
        self.tick(2)
        self.assertEqual(len(self.calls), 1)

    def test_command_reply_timeout_retains_exact_bytes_and_does_not_ack_business_message(self):
        self.connect()
        self.client.publish(self.telemetry_topic, b'business')
        self.stream.reads.append(self.incoming())
        self.tick(3)
        reply = self.client._reply_inflight
        self.assertIsNotNone(reply)
        original_id = self.client._pending[2]
        self.clock.value = 1000
        self.stream.reads.append(puback_packet(original_id))
        self.tick()
        self.assertTrue(self.client.take_ack())
        self.assertTrue(self.client.publish(self.telemetry_topic, b'next business'))
        self.tick()
        # A stalled reply cannot be mistaken for the business PUBACK.
        self.clock.value = ACK_TIMEOUT_MS
        self.tick()
        self.assertEqual(self.client.status()['phase'], 'backoff')
        self.assertEqual(self.client.status()['lastError'], 'command_reply_timeout')
        self.assertIs(self.client._reply_inflight, reply)
        self.assertIsNotNone(self.client._pending)
        self.assertFalse(self.client.take_ack())

    def test_prepared_handoff_does_not_repeat_allocating_validation_after_commit(self):
        old, old_stream = self.make_client(commands=False)
        self.connect(old, old_stream)
        payload = b'original before configuration change'
        old.publish(self.telemetry_topic, payload)
        old.tick(True, True)
        old.close()
        self.connect()
        prepared = self.client.prepare_pending_transfer_from(old)
        original = old._pending
        self.assertIsNone(self.client._pending)
        self.assertIs(old._pending, original)
        self.client.prepare_pending_transfer_from = Mock(side_effect=MemoryError)
        self.client._commands.status = Mock(side_effect=MemoryError)
        self.client.transfer_pending_from(old, prepared=prepared)
        self.assertIs(self.client._pending, original)
        self.assertIs(self.client._pending[1], payload)
        self.assertEqual(old._state, 'transferred')
        calls = old._sockets.socket.call_count
        old.tick(True, True)
        self.assertEqual(old._sockets.socket.call_count, calls)

    def test_failed_preparation_never_moves_original_payload(self):
        class FailingCount:
            def __radd__(self, other):
                raise MemoryError('allocation failure')
        old, old_stream = self.make_client(commands=False)
        self.connect(old, old_stream)
        old.publish(self.telemetry_topic, b'original')
        old.tick(True, True)
        old.close()
        self.connect()
        original = old._pending
        old._ack_count = FailingCount()
        with self.assertRaises(MemoryError):
            self.client.prepare_pending_transfer_from(old)
        self.assertIs(old._pending, original)
        self.assertIsNone(self.client._pending)
        self.assertFalse(old._handoff_complete)

    def test_socket_cleanup_finishes_before_parser_reset_allocation_can_fail(self):
        self.connect()
        raw = self.client._raw
        stream = self.client._stream
        self.client._reader.reset = Mock(side_effect=MemoryError('parser reset allocation'))
        with self.assertRaises(MemoryError):
            self.client.close()
        self.assertTrue(raw.closed)
        self.assertTrue(stream.closed)
        self.assertIsNone(self.client._raw)
        self.assertIsNone(self.client._stream)
        self.assertFalse(self.client.connected)


if __name__ == '__main__':
    unittest.main()
