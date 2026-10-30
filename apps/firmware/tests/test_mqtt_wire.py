"""MQTT 3.1.1 wire vectors and malformed-stream checks; no broker is used."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_wire import (MAX_PACKET_BYTES, MAX_PACKETS_PER_FEED,
                                  PacketReader, connect_packet, disconnect_packet,
                                  ping_packet, publish_packet)


class MqttWireTests(unittest.TestCase):
    def test_connect_matches_protocol_vector_and_auth_flags(self):
        self.assertEqual(connect_packet('test', 'user', 'pass'),
                         b'\x10\x1c\x00\x04MQTT\x04\xc2\x00\x3c'
                         b'\x00\x04test\x00\x04user\x00\x04pass')
        self.assertEqual(connect_packet('id', keepalive=300, clean_session=False),
                         b'\x10\x0e\x00\x04MQTT\x04\x00\x01\x2c\x00\x02id')
        self.assertEqual(connect_packet('id', '', b'\x00\xff')[-6:], b'\x00\x00\x00\x02\x00\xff')

    def test_connect_utf8_lengths_count_bytes_and_optional_password(self):
        packet = connect_packet('board-\u6e29', '\u7528\u6237')
        self.assertIn(b'\x00\x09board-\xe6\xb8\xa9', packet)
        self.assertIn(b'\x00\x06\xe7\x94\xa8\xe6\x88\xb7', packet)
        self.assertEqual(packet[9], 0x82)
        self.assertEqual(connect_packet('id', keepalive=0)[10:12], b'\x00\x00')

    def test_connect_rejects_invalid_fields_without_values_in_error(self):
        for changes in [dict(client_id=''), dict(client_id='bad\x00id'),
                        dict(client_id='bad\ud800id'), dict(client_id='bad\uffffid'),
                        dict(client_id='bad\U0001fffeid'), dict(client_id='bad\ufdd0id'),
                        dict(client_id=b'bytes-id'), dict(keepalive=True),
                        dict(keepalive=-1), dict(keepalive=65536),
                        dict(clean_session=1), dict(username=None, password='private-fixture')]:
            with self.subTest(changes=list(changes)):
                arguments = {'client_id': 'test-device'}
                arguments.update(changes)
                with self.assertRaises(ValueError) as raised:
                    connect_packet(**arguments)
                self.assertNotIn('private-fixture', str(raised.exception))

    def test_publish_is_qos1_non_retained_with_exact_payload_and_duplicate_bit(self):
        payload = b'\x00{"sample":12.5}\xff'
        expected = b'\x00\x03a/b\x12\x34' + payload
        packet = publish_packet('a/b', payload, 0x1234)
        self.assertEqual(packet, bytes((0x32, len(expected))) + expected)
        retry = publish_packet('a/b', payload, 0x1234, duplicate=True)
        self.assertEqual(retry[0], 0x3A)
        self.assertEqual(retry[1:], packet[1:])
        self.assertEqual(publish_packet('a', b'', 65535), b'\x32\x05\x00\x01a\xff\xff')

    def test_publish_rejects_wildcards_bad_utf8_and_packet_identifier_zero(self):
        for topic in ['', 'a/#', 'a/+', 'a\x00b', 'a\udfff']:
            with self.subTest(topic_length=len(topic)), self.assertRaises(ValueError):
                publish_packet(topic, b'{}', 1)
        for identifier in [0, -1, 65536, True, 1.0]:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                publish_packet('a', b'{}', identifier)
        with self.assertRaises(ValueError):
            publish_packet('a', '{}', 1)
        with self.assertRaises(ValueError):
            publish_packet('a', b'{}', 1, duplicate=1)

    def test_outgoing_packets_are_bounded_and_encode_multibyte_lengths(self):
        packet = publish_packet('a', b'x' * 128, 1)
        self.assertEqual(packet[:3], b'\x32\x85\x01')
        packet = publish_packet('a', b'x' * (MAX_PACKET_BYTES - 8), 1)
        self.assertEqual(len(packet), MAX_PACKET_BYTES)
        for call in [lambda: publish_packet('a', b'x' * (MAX_PACKET_BYTES - 7), 1),
                     lambda: connect_packet('a' * MAX_PACKET_BYTES),
                     lambda: connect_packet('a', 'u' * 2500, 'p' * 2500),
                     lambda: connect_packet('a', 'u', 'p' * (MAX_PACKET_BYTES + 1))]:
            with self.assertRaises(ValueError):
                call()

    def test_ping_and_disconnect_have_no_payload(self):
        self.assertEqual(ping_packet(), b'\xc0\x00')
        self.assertEqual(disconnect_packet(), b'\xe0\x00')

    def test_reader_handles_fragmentation_and_coalesced_packets(self):
        reader = PacketReader()
        stream = b'\x20\x02\x01\x00\x40\x02\x12\x34\xd0\x00'
        expected = [(2, 0, b'\x01\x00'), (4, 0, b'\x12\x34'), (13, 0, b'')]
        received = []
        for byte in stream:
            received.extend(reader.feed(bytes((byte,))))
        self.assertEqual(received, expected)
        self.assertEqual(PacketReader().feed(stream), expected)
        self.assertEqual(reader.feed(b''), [])
        self.assertEqual(reader.feed(bytearray(b'\xd0\x00')), [(13, 0, b'')])

    def test_reader_rejects_bad_headers_lengths_and_impossible_acknowledgements(self):
        packets = [b'\x00', b'\xf0', b'\x21', b'\x41', b'\xd1', b'\x30',
                   b'\x20\x00', b'\x20\x01', b'\x20\x03', b'\xd0\x01',
                   b'\x20\x02\x02\x00', b'\x20\x02\x00\x06',
                   b'\x20\x02\x01\x01', b'\x40\x02\x00\x00',
                   b'\x20\x80\x00', b'\x20\xff\xff\xff\xff']
        for packet in packets:
            with self.subTest(packet=packet):
                reader = PacketReader()
                with self.assertRaises(ValueError):
                    reader.feed(packet)
                self.assertEqual(reader.feed(b'\xd0\x00'), [(13, 0, b'')])

    def test_reader_rejects_each_server_refusal(self):
        for refusal in range(1, 6):
            with self.subTest(refusal=refusal), self.assertRaisesRegex(ValueError, 'connection refused'):
                PacketReader().feed(bytes((0x20, 2, 0, refusal)))

    def test_reader_partial_remaining_length_is_bounded_before_body_allocation(self):
        reader = PacketReader(max_packet_bytes=128)
        self.assertEqual(reader.feed(b'\x20\x80'), [])
        with self.assertRaisesRegex(ValueError, 'too large'):
            reader.feed(b'\x01')
        self.assertEqual(reader.feed(b'\x20\x02\x00\x00'), [(2, 0, b'\x00\x00')])
        with self.assertRaisesRegex(ValueError, 'too large'):
            PacketReader(max_packet_bytes=3).feed(b'\x40\x02')
        self.assertEqual(PacketReader(max_packet_bytes=2).feed(b'\xd0\x00'), [(13, 0, b'')])
        for limit in [True, 1, MAX_PACKET_BYTES + 1, 2.0]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                PacketReader(limit)

    def test_reader_rejects_flood_and_releases_partial_state(self):
        reader = PacketReader()
        self.assertEqual(len(reader.feed(b'\xd0\x00' * MAX_PACKETS_PER_FEED)), MAX_PACKETS_PER_FEED)
        with self.assertRaisesRegex(ValueError, 'flood'):
            reader.feed(b'\xd0\x00' * (MAX_PACKETS_PER_FEED + 1))
        self.assertEqual(reader.feed(b'\xd0\x00'), [(13, 0, b'')])
        reader.feed(b'\x20\x02\x00')
        reader.reset()
        self.assertEqual(reader.feed(b'\x40\x02\x00\x01'), [(4, 0, b'\x00\x01')])
        with self.assertRaises(ValueError):
            reader.feed('not-bytes')


if __name__ == '__main__':
    unittest.main()
