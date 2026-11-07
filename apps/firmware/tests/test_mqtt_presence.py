"""Live presence and telemetry ACK isolation, with a synthetic TLS peer only."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_presence import MqttPresence, HEARTBEAT_MS, MAX_PRESENCE_BYTES
from components.mqtt_transport import MqttTransport
from components.mqtt_wire import decode_publish
import test_mqtt_transport as fixture


def frames(data):
    result = []
    while len(data) >= 2:
        length, shift, cursor = 0, 0, 1
        while cursor < len(data):
            byte = data[cursor]
            length += (byte & 127) << shift
            cursor += 1
            shift += 7
            if not byte & 128:
                break
        else:
            break
        if cursor + length > len(data):
            break
        result.append((data[0], data[cursor:cursor + length]))
        data = data[cursor + length:]
    return result, data


def will(frame):
    header, body = frames(frame)[0][0]
    assert header == 0x10 and body[7] & 0x2C == 0x2C
    cursor = 10
    fields = []
    for _ in range(3):
        size = int.from_bytes(body[cursor:cursor + 2], 'big')
        cursor += 2
        fields.append(body[cursor:cursor + size])
        cursor += size
    return fields[1].decode(), json.loads(fields[2])


class PresenceTransportTests(unittest.TestCase):
    add_connection = fixture.MqttTransportTests.add_connection

    def connect(self, stream=None):
        stream = stream or self.streams[-1]
        self.client.tick(True, True)
        for _ in range(20):
            if self.client._tx is None:
                break
            self.client.tick(True, True)
        stream.reads.append(fixture.CONNACK)
        self.client.tick(True, True)
        self.assertTrue(self.client.connected, self.client.status())

    def setUp(self):
        fixture.MqttTransportTests.setUp(self)
        self.config.update(site_id='unit-site', gateway_id='unit-gateway', device_id='unit-esp32')
        self.random_sequence = 0
        self.presence = MqttPresence(self.config, self.clock.now, 'unit-boot',
                                     utc_ready=lambda: False, random_bytes=self.random_bytes)
        self.client = MqttTransport(self.config, self.clock.now, self.sockets, self.ssl,
                                    self.resolver, lambda: self.poll, collect=self.collect,
                                    presence=self.presence)
        self.events = []
        self.sent = []
        self.cursor = 0
        self.buffer = b''

    def random_bytes(self, count):
        self.random_sequence += 1
        return self.random_sequence.to_bytes(count, 'big')

    def pump(self, count=30, acknowledge=True):
        stream = self.streams[-1]
        for _ in range(count):
            self.client.tick(True, True)
            self.buffer += b''.join(stream.writes[self.cursor:])
            self.cursor = len(stream.writes)
            packets, self.buffer = frames(self.buffer)
            for header, body in packets:
                self.sent.append((header, body))
                if header >> 4 == 3:
                    topic, payload, identifier, qos, retained = decode_publish(header & 15, body)
                    self.assertEqual(qos, 1)
                    if topic.endswith('/status'):
                        self.assertTrue(retained)
                        self.assertLessEqual(len(payload), MAX_PRESENCE_BYTES)
                        self.events.append((topic, json.loads(payload)))
                        if acknowledge:
                            stream.reads.append(b'\x40\x02' + identifier.to_bytes(2, 'big'))

    def test_birth_and_sixty_second_live_heartbeat_share_session_with_will(self):
        self.connect()
        topic, lwt = will(self.client._connect_frame)
        self.pump()
        birth = self.events[0][1]
        self.assertEqual(self.events[0][0], topic)
        self.assertEqual((birth['event'], birth['online'], birth['sequence']), ('online', True, 0))
        self.assertEqual(birth['boot_id'], 'unit-boot')
        self.assertEqual(lwt['session_id'], birth['session_id'])
        self.assertNotEqual(lwt['event_id'], birth['event_id'])
        self.assertEqual(lwt['boot_id'], birth['boot_id'])
        for key in ('sequence', 'source_time_utc', 'uptime_ms'):
            self.assertIsNone(lwt[key])
        self.assertEqual((lwt['event'], lwt['online'], lwt['reason']), ('offline', False, 'lwt'))
        self.assertEqual(self.events[1][1]['reason'], 'upstream_unknown')
        self.clock.value = HEARTBEAT_MS - 1
        self.pump()
        self.assertEqual(len(self.events), 2)
        self.clock.value += 1
        self.pump()
        heartbeat = self.events[-1][1]
        self.assertEqual((heartbeat['event'], heartbeat['uptime_ms']), ('heartbeat', HEARTBEAT_MS))
        self.assertEqual(heartbeat['session_id'], birth['session_id'])
        self.assertIsNone(heartbeat['source_time_utc'])

    def test_wrong_and_presence_puback_cannot_acknowledge_telemetry(self):
        self.connect()
        self.pump(acknowledge=False)
        pending = self.client._presence_inflight
        self.assertTrue(self.client.publish('unit/telemetry', b'original-temperature'))
        self.streams[-1].reads.append(b'\x40\x02\x7f\xff')
        self.pump(1, acknowledge=False)
        self.assertIs(self.client._presence_inflight, pending)
        self.assertFalse(self.client.take_ack())
        self.streams[-1].reads.append(b'\x40\x02' + pending[2].to_bytes(2, 'big'))
        self.pump(1, acknowledge=False)
        self.assertIsNone(self.client._presence_inflight)
        self.assertFalse(self.client.take_ack())
        self.assertEqual(self.client.status()['acks'], 0)
        self.assertEqual(self.client._pending[1], b'original-temperature')

    def test_configuration_handoff_waits_for_colliding_presence_identifier(self):
        previous = MqttTransport(self.config, self.clock.now, self.sockets, self.ssl,
                                  self.resolver, lambda: self.poll, collect=self.collect)
        self.assertTrue(previous.publish('unit/telemetry', b'original-before-handoff'))
        previous.close()
        self.connect()
        self.pump(acknowledge=False)
        presence_id = self.client._presence_inflight[2]
        self.assertEqual(previous._pending[2], presence_id)
        self.client.transfer_pending_from(previous)
        self.pump(acknowledge=False)
        self.assertEqual(self.client._ack_deadline, 0)
        self.assertFalse(self.client.take_ack())
        self.streams[-1].reads.append(b'\x40\x02' + presence_id.to_bytes(2, 'big'))
        self.pump(acknowledge=False)
        self.assertGreater(self.client._ack_deadline, 0)
        self.assertFalse(self.client.take_ack())
        self.assertEqual(self.client._pending[1], b'original-before-handoff')

    def test_disconnect_discards_queued_heartbeat_and_uses_new_session_and_will(self):
        self.connect()
        self.pump()
        first_session = self.presence.session_id
        self.clock.value = HEARTBEAT_MS
        self.presence.tick()
        obsolete = json.loads(self.presence._own[1])
        self.client.tick(False, False)
        self.assertFalse(self.presence.pending())
        _, stream = self.add_connection()
        self.cursor, self.buffer = 0, b''
        self.connect(stream)
        self.events.clear()
        self.pump()
        _, new_will = will(self.client._connect_frame)
        self.assertNotEqual(new_will['session_id'], first_session)
        self.assertEqual(new_will['session_id'], self.events[0][1]['session_id'])
        self.assertNotIn(obsolete['event_id'], [value['event_id'] for _, value in self.events])
        self.assertTrue(all(value['boot_id'] == 'unit-boot' for _, value in self.events))

    def test_expired_unsent_observations_are_not_replayed(self):
        self.presence.begin_session()
        self.presence.connected()
        self.clock.value = HEARTBEAT_MS
        self.assertIsNone(self.presence.take())
        self.assertFalse(self.presence.pending())

    def test_presence_rejects_invalid_identity_and_stays_bounded_without_a_reader(self):
        for changes in (dict(device_id='bad/authority'), dict(client_id=''), dict(site_id=None)):
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError):
                MqttPresence(dict(self.config, **changes), self.clock.now)
        with self.assertRaises(ValueError):
            MqttPresence(self.config, self.clock.now, 'bad\nboot')
        self.presence.begin_session()
        self.presence.connected()
        for _ in range(100):
            self.presence.observe(True)
        first, second = self.presence.take(), self.presence.take()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(self.presence.take())
        self.assertLess(len(first[1]), 1024)
        self.assertLess(len(second[1]), 1024)

    def test_sensor_failures_are_bounded_and_do_not_stop_own_heartbeat(self):
        self.connect()
        self.pump()
        for _ in range(100):
            self.presence.observe(False)
        self.pump()
        failures = [value for _, value in self.events if value['reason'] == 'read_failed']
        self.assertEqual(len(failures), 1)
        self.clock.value = HEARTBEAT_MS
        self.presence.observe(False)
        self.pump()
        self.assertEqual(self.events[-2][1]['event'], 'heartbeat')
        self.assertTrue(self.events[-2][1]['online'])
        self.assertEqual(self.events[-1][1]['reason'], 'read_failed')
        self.presence.observe(True)
        self.pump()
        self.assertEqual(self.events[-1][1]['device_id'], 'sht20')
        self.assertEqual(self.events[-1][1]['reason'], 'read_ok')
        self.assertTrue(self.events[-1][1]['online'])

    def test_graceful_shutdown_acknowledges_offline_before_disconnect(self):
        self.connect()
        self.pump()
        self.assertTrue(self.client.publish('unit/telemetry', b'unacknowledged'))
        self.client.begin_shutdown()
        self.pump()
        self.assertTrue(self.client.shutdown_complete)
        own = [value for _, value in self.events if value['device_id'] == 'unit-esp32']
        self.assertEqual((own[-1]['event'], own[-1]['reason']), ('offline', 'shutdown'))
        self.assertEqual(self.events[-1][1]['reason'], 'upstream_unknown')
        self.assertEqual(self.sent[-1], (0xE0, b''))
        self.assertFalse(self.client.take_ack())
        self.assertEqual(self.client._pending[1], b'unacknowledged')
        self.assertTrue(self.streams[-1].closed)

    def test_shutdown_timeout_closes_without_suppressing_broker_will(self):
        self.connect()
        self.pump()
        self.client.begin_shutdown()
        self.pump(acknowledge=False)
        self.assertFalse(self.client.shutdown_complete)
        self.clock.value += 2000
        self.pump(1, acknowledge=False)
        self.assertTrue(self.client.shutdown_complete)
        self.assertTrue(self.streams[-1].closed)
        self.assertNotIn((0xE0, b''), self.sent)


if __name__ == '__main__':
    unittest.main()
