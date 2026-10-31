"""Remote settings boundary checks without a broker, clock dependency or secrets."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.mqtt_commands import (CommandProcessor, utc_seconds, MAX_CACHE_BYTES,
                                      MAX_CACHE_ENTRIES, MAX_REPLY_BYTES)


class MqttCommandTests(unittest.TestCase):
    def setUp(self):
        self.now = utc_seconds('2026-10-31T14:00:00Z')
        self.synchronized = True
        self.calls = []
        self.finishes = []

        def handle(command, finish):
            self.calls.append(command)
            self.finishes.append(finish)
            return {'ok': True, 'result': {'stage': 'applied', 'revision': 2}}

        self.handle = handle
        self.processor = CommandProcessor(handle, lambda: (self.now, self.synchronized))

    def request(self, identifier='request-1', command='sensor.config.set', **changes):
        body = {'v': 1, 'id': identifier, 'command': command,
                'args': {'revision': 1, 'values': {'interval_seconds': 60}},
                'expiresAtUtc': '2026-10-31T14:00:30Z'}
        if command == 'sensor.config.get':
            body['args'] = {}
        body.update(changes)
        return json.dumps(body).encode()

    def drain(self):
        emitted = []
        self.processor.drain(lambda identifier, payload: emitted.append((identifier, payload)) or True)
        return emitted

    def test_utc_parser_is_independent_of_host_epoch_and_checks_calendar(self):
        self.assertEqual(utc_seconds('2026-10-31T14:00:00Z'), 1793455200)
        self.assertEqual(utc_seconds('2026-10-31T14:00:00.123Z'), 1793455200)
        self.assertEqual(utc_seconds('2026-10-31T14:00:00.123456Z'), 1793455200)
        self.assertEqual(utc_seconds('2024-03-01T00:00:00Z') -
                         utc_seconds('2024-02-28T00:00:00Z'), 172800)
        for value in ('2025-02-29T00:00:00Z', '2026-13-01T00:00:00Z',
                      '2026-00-01T00:00:00Z', '2026-10-31T24:00:00Z',
                      '2026-10-31T14:00:00+00:00', '2026-10-31T14:00:00.Z',
                      '2026-10-31T14:00:00.12aZ'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                utc_seconds(value)

    def test_duplicate_replays_identical_ack_without_reapplying(self):
        request = self.request()
        self.assertTrue(self.processor.receive(request))
        first = self.drain()[0][1]
        self.assertTrue(self.processor.receive(request))
        self.assertIs(self.drain()[0][1], first)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.processor.status()['duplicates'], 1)
        self.assertFalse(self.processor.receive(self.request(args={'revision': 2, 'values': {'enabled': False}})))
        self.assertEqual(len(self.calls), 1)

    def test_expired_future_retained_unsynchronized_and_disallowed_commands_do_not_apply(self):
        cases = [self.request(expiresAtUtc='2026-10-31T14:00:00Z'),
                 self.request(expiresAtUtc='2026-10-31T14:05:01Z'),
                 self.request(command='mqtt.config.get'), self.request(command='gpio.write'),
                 self.request(v=True), self.request(extra='not allowed'),
                 self.request(args={'revision': True, 'values': {'enabled': False}})]
        for request in cases:
            self.assertFalse(self.processor.receive(request))
        self.assertFalse(self.processor.receive(self.request(), retained=True))
        self.synchronized = False
        self.assertFalse(self.processor.receive(self.request()))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.drain(), [])

    def test_exact_expiry_and_identifier_limits_accept_only_the_documented_boundary(self):
        cases = ((300, 'r' * 64, True), (301, 'r' * 64, False),
                 (0, 'r' * 64, False), (1, 'r' * 65, False), (1, '', False))
        for seconds, identifier, expected in cases:
            with self.subTest(seconds=seconds, id_length=len(identifier)):
                self.setUp()
                expiry = '2026-10-31T14:%02d:%02dZ' % (seconds // 60, seconds % 60)
                accepted = self.processor.receive(self.request(identifier=identifier, expiresAtUtc=expiry))
                self.assertEqual(accepted, expected)
                self.assertEqual(len(self.calls), int(expected))

    def test_cached_duplicate_after_expiry_or_clock_loss_only_replies_and_never_reapplies(self):
        request = self.request()
        self.assertTrue(self.processor.receive(request))
        original = self.drain()[0][1]
        self.now += 31
        self.synchronized = False
        self.assertTrue(self.processor.receive(request))
        self.assertIs(self.drain()[0][1], original)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.processor.receive(self.request(identifier='new-request')))
        self.assertEqual(len(self.calls), 1)

    def test_oversized_result_becomes_bounded_failure_without_dropping_duplicate_protection(self):
        self.processor = CommandProcessor(
            lambda command, finish: {'ok': True, 'result': {'unbounded': 'x' * 4096}},
            lambda: (self.now, True))
        request = self.request()
        self.assertTrue(self.processor.receive(request))
        reply = self.drain()[0][1]
        self.assertFalse(json.loads(reply)['ok'])
        self.assertLessEqual(len(reply), MAX_REPLY_BYTES)
        self.assertNotIn(b'x' * 100, reply)
        self.assertTrue(self.processor.receive(request))
        self.assertIs(self.drain()[0][1], reply)
        self.assertEqual(self.processor.status()['duplicates'], 1)

    def test_async_final_ack_is_retained_under_queue_pressure_and_cannot_regress(self):
        def handle(command, finish):
            self.finishes.append(finish)
            return {'ok': True, 'result': {'stage': 'testing'}}
        self.processor = CommandProcessor(handle, lambda: (self.now, True))
        request = self.request()
        self.processor.receive(request)
        self.processor.drain(lambda *args: False)
        self.assertEqual(self.processor.status()['pendingResponses'], 1)
        self.finishes[0]({'ok': True, 'result': {'stage': 'applied', 'revision': 2}})
        final = self.drain()[0][1]
        self.assertEqual(json.loads(final)['result']['stage'], 'applied')
        self.finishes[0]({'ok': False, 'error': 'late result'})
        self.processor.receive(request)
        self.assertIs(self.drain()[0][1], final)

    def test_synchronous_final_callback_is_not_overwritten_by_returned_testing_ack(self):
        def handle(command, finish):
            finish({'ok': True, 'result': {'stage': 'applied', 'revision': 2}})
            return {'ok': True, 'result': {'stage': 'testing'}}
        self.processor = CommandProcessor(handle, lambda: (self.now, True))
        self.processor.receive(self.request())
        self.assertEqual(json.loads(self.drain()[0][1])['result']['stage'], 'applied')

    def test_cache_and_rate_limits_bound_memory_without_reapplying_pending_work(self):
        for index in range(40):
            self.now += 1
            self.processor.receive(self.request(identifier='request-' + str(index),
                                                expiresAtUtc='2026-10-31T14:02:30Z'))
            self.drain()
            self.assertLessEqual(len(self.processor._cache), MAX_CACHE_ENTRIES)
            self.assertLessEqual(self.processor._bytes, MAX_CACHE_BYTES)
        before = len(self.calls)
        for index in range(10):
            self.processor.receive(self.request(identifier='burst-' + str(index),
                                                expiresAtUtc='2026-10-31T14:02:30Z'))
            self.drain()
        self.assertLessEqual(len(self.calls) - before, 4)

    def test_pending_or_unsent_cache_entries_are_not_evicted_to_repeat_side_effects(self):
        def handle(command, finish):
            self.calls.append(command)
            return {'ok': True, 'result': {'stage': 'testing'}}
        self.processor = CommandProcessor(handle, lambda: (self.now, True))
        first = self.request()
        self.processor.receive(first)
        for index in range(20):
            self.now += 1
            self.processor.receive(self.request(identifier='pending-' + str(index)))
        self.assertLessEqual(len(self.calls), MAX_CACHE_ENTRIES)
        before = len(self.calls)
        self.assertTrue(self.processor.receive(first))
        self.assertEqual(len(self.calls), before)

    def test_handler_exceptions_and_unserializable_responses_never_expose_exception_values(self):
        def handle(command, finish):
            raise ValueError('private-unit-only-secret')
        self.processor = CommandProcessor(handle, lambda: (self.now, True))
        self.processor.receive(self.request())
        reply = self.drain()[0][1]
        self.assertNotIn(b'private-unit-only-secret', reply)
        self.assertFalse(json.loads(reply)['ok'])
        self.assertLessEqual(len(reply), MAX_REPLY_BYTES)

    def test_oversized_or_non_json_payloads_never_reach_handler(self):
        for request in (b'x' * 2049, b'not-json', b'\xff', bytearray(self.request()), b''):
            self.assertFalse(self.processor.receive(request))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
