"""SNTP fractions, error bounds, holdover and polled socket regressions."""

import calendar
from pathlib import Path
import struct
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.clock_sync import ClockSync, POLL_IN, POLL_ERRORS


class Clock:
    def __init__(self, epoch=2000):
        self.value = 0
        self.delta = 946684800 if epoch == 2000 else 0
        self.base_us = (calendar.timegm((2026, 10, 30, 3, 20, 10)) - self.delta) * 1000000 + 123456

    def now(self):
        return self.value

    def gmtime(self, seconds):
        return time.gmtime(seconds + self.delta)[:8]

    def mktime(self, parts):
        return calendar.timegm(parts) - self.delta


class RTC:
    def __init__(self, clock, offset_us=-30000000, drift_ppm=0):
        self.clock = clock
        self.base = clock.base_us + offset_us
        self.at = clock.now()
        self.drift_ppm = drift_ppm
        self.writes = []
        self.fail = False

    def utc_us(self):
        elapsed = self.clock.now() - self.at
        return self.base + elapsed * 1000 + elapsed * self.drift_ppm // 1000

    def datetime(self, value=None):
        if value is None:
            seconds, micros = divmod(self.utc_us(), 1000000)
            parts = self.clock.gmtime(seconds)
            return (parts[0], parts[1], parts[2], parts[6], parts[3], parts[4], parts[5], micros)
        if self.fail:
            raise OSError('simulated RTC failure')
        self.writes.append(value)
        self.base = self.clock.mktime((value[0], value[1], value[2], value[4],
                                      value[5], value[6], value[3], 0)) * 1000000 + value[7]
        self.at = self.clock.now()


class Datagram:
    def __init__(self):
        self.sent = []
        self.received = []
        self.closed = False
        self.blocking = None
        self.send_error = None

    def setblocking(self, value):
        self.blocking = value

    def sendto(self, payload, address):
        if self.send_error:
            error, self.send_error = self.send_error, None
            raise error
        self.sent.append((bytes(payload), address))
        return len(payload)

    def recvfrom(self, count):
        return self.received.pop(0)

    def close(self):
        self.closed = True


class Poll:
    def __init__(self):
        self.socket = None
        self.timeouts = []
        self.errors = False
        self.unregistered = False

    def register(self, sock, mask):
        self.socket = sock

    def unregister(self, sock):
        self.unregistered = True

    def poll(self, timeout):
        self.timeouts.append(timeout)
        if self.errors:
            return [(self.socket, POLL_ERRORS)]
        return [(self.socket, POLL_IN)] if self.socket.received else []


class Sockets:
    AF_INET = 2
    SOCK_DGRAM = 2

    def __init__(self):
        self.created = []
        self.lookups = []
        self.send_error = None

    def getaddrinfo(self, host, port):
        self.lookups.append((host, port))
        return [(2, 2, 17, '', ('192.0.2.1', port))]

    def socket(self, family, kind):
        sock = Datagram()
        sock.send_error, self.send_error = self.send_error, None
        self.created.append(sock)
        return sock


def timestamp(value_us, delta):
    seconds, micros = divmod(value_us, 1000000)
    return struct.pack('!II', (seconds + delta) & 0xffffffff, (micros << 32) // 1000000)


class ClockSyncTests(unittest.TestCase):
    def make(self, epoch=2000, drift_ppm=0):
        self.clock = Clock(epoch)
        self.rtc = RTC(self.clock, drift_ppm=drift_ppm)
        self.sockets = Sockets()
        self.polls = []

        def poll():
            value = Poll()
            self.polls.append(value)
            return value

        self.sync = ClockSync(self.clock.now, self.clock, self.rtc, self.sockets,
                              poll, lambda count: b'cookie01')
        self.sync.configure('clock.example')
        return self.sync

    def reply(self, rtt=110, processing=10, root_delay=20, dispersion=10):
        sent_at = self.sync._sent_at
        start_us = self.clock.base_us + sent_at * 1000
        half_delay = (rtt - processing) * 500
        packet = bytearray(48)
        packet[0], packet[1], packet[3] = 0x24, 2, 236
        packet[4:8] = struct.pack('!i', root_delay * 65536 // 1000)
        packet[8:12] = struct.pack('!I', dispersion * 65536 // 1000)
        packet[24:32] = b'cookie01'
        packet[32:40] = timestamp(start_us + half_delay, self.sync._ntp_delta)
        packet[40:48] = timestamp(start_us + half_delay + processing * 1000, self.sync._ntp_delta)
        self.clock.value = sent_at + rtt
        return packet

    def complete(self, packet=None, **values):
        if packet is None:
            packet = self.reply(**values)
        self.sockets.created[-1].received.append((packet, ('192.0.2.1', 123)))
        return self.sync.tick()

    def test_fractional_utc_and_round_trip_compensation_reach_rtc(self):
        self.make()
        self.assertIsNone(self.sync.start())
        self.assertFalse(self.sockets.created[-1].blocking)
        result = self.complete()
        self.assertTrue(result['ok'])
        self.assertLessEqual(abs(self.rtc.utc_us() - (self.clock.base_us + 110000)), 2)
        self.assertNotEqual(self.rtc.writes[-1][-1], 0)
        self.assertGreater(result['offsetUs'], 29000000)
        self.assertEqual(self.sync.status()['driftPpm'], 100)
        self.assertFalse(self.sync.status()['driftMeasured'])
        self.assertEqual(self.sync.next_due_ms, self.clock.now() + 60000)
        self.assertTrue(self.sockets.created[-1].closed)
        self.assertTrue(self.polls[-1].unregistered)
        self.assertEqual(self.polls[-1].timeouts, [0])

    def test_1970_and_2000_epochs_preserve_integer_microseconds(self):
        for epoch in (1970, 2000):
            with self.subTest(epoch=epoch):
                self.make(epoch)
                self.sync.start()
                self.assertTrue(self.complete()['ok'])
                self.assertLessEqual(abs(self.rtc.utc_us() - (self.clock.base_us + 110000)), 2)

    def test_second_sample_measures_drift_and_adapts_interval(self):
        self.make(drift_ppm=1000)
        self.sync.start()
        self.complete()
        self.clock.value = self.sync.next_due_ms
        self.sync.start()
        self.assertTrue(self.complete()['ok'])
        status = self.sync.status()
        self.assertTrue(status['driftMeasured'])
        self.assertLess(status['lastOffsetUs'], -50000)
        self.assertGreater(status['driftPpm'], 1000)
        self.assertTrue(30000 <= status['intervalMs'] <= 300000)
        self.assertTrue(status['withinTarget'])
        self.assertEqual(self.sockets.lookups, [('clock.example', 123)])
        self.clock.value += 500000
        self.assertFalse(self.sync.status()['withinTarget'])
        self.assertGreater(self.sync.status()['estimatedErrorMs'], 500)

    def test_shortened_intervals_continue_tracking_worsening_drift(self):
        self.make(drift_ppm=15000)
        self.sync.start()
        self.complete()
        self.clock.value = self.sync.next_due_ms
        self.sync.start()
        self.complete()
        first_rate = self.sync.status()['driftPpm']
        self.assertTrue(30000 <= self.sync.interval_ms < 60000)
        self.rtc.drift_ppm = 100000
        self.clock.value = self.sync.next_due_ms
        self.sync.start()
        self.complete()
        self.assertGreater(self.sync.status()['driftPpm'], first_rate)
        self.assertEqual(self.sync.interval_ms, 30000)
        self.assertTrue(self.sync.status()['withinTarget'])
        self.assertFalse(self.sync.status()['withinTargetAtNextSync'])

    def test_parse_time_is_added_before_setting_rtc(self):
        self.make()
        original_datetime = self.rtc.datetime

        def delayed_datetime(value=None):
            if value is None:
                self.clock.value += 7
            return original_datetime(value)

        self.rtc.datetime = delayed_datetime
        self.sync.start()
        self.assertTrue(self.complete()['ok'])
        self.assertEqual(self.clock.value, 117)
        self.assertLessEqual(abs(self.rtc.utc_us() - (self.clock.base_us + 117000)), 2)

    def test_2036_ntp_era_wrap_is_continuous(self):
        self.make()
        self.clock.base_us = (calendar.timegm((2036, 2, 7, 6, 28, 15)) -
                              self.clock.delta) * 1000000 + 949999
        self.rtc.base = self.clock.base_us - 30000000
        self.sync.start()
        self.assertTrue(self.complete()['ok'])
        self.assertLessEqual(abs(self.rtc.utc_us() - (self.clock.base_us + 110000)), 2)

    def test_rtc_write_duration_is_counted_in_quality_and_schedule(self):
        self.make()
        original_datetime = self.rtc.datetime

        def slow_write(value=None):
            if value is not None:
                self.clock.value += 20
            return original_datetime(value)

        self.rtc.datetime = slow_write
        self.sync.start()
        result = self.complete()
        self.assertTrue(result['ok'])
        self.assertEqual(self.clock.value, 130)
        self.assertGreaterEqual(result['uncertaintyMs'], 92)
        self.assertEqual(self.sync.status()['lastSyncAgeMs'], 0)
        self.assertEqual(self.sync.next_due_ms, 60130)
        self.assertEqual(self.rtc.utc_us(), self.clock.base_us + 110000 - 1)

    def test_udp_would_block_retries_without_waiting_or_losing_deadline(self):
        self.make()
        self.sockets.send_error = OSError(11)
        self.assertIsNone(self.sync.start())
        self.assertTrue(self.sync.busy)
        self.assertIsNone(self.sync._sent_at)
        self.clock.value = 10
        self.assertIsNone(self.sync.tick())
        self.assertEqual(self.sync._sent_at, 10)
        self.assertTrue(self.complete()['ok'])

    def test_negative_root_delay_never_reduces_uncertainty(self):
        self.make()
        self.sync.start()
        result = self.complete(root_delay=-20)
        self.assertTrue(result['ok'])
        self.assertGreaterEqual(result['uncertaintyMs'], 62)

    def test_high_uncertainty_does_not_change_a_good_rtc(self):
        self.make()
        self.sync.start()
        self.complete()
        saved = list(self.rtc.writes)
        self.clock.value = self.sync.next_due_ms
        self.sync.start()
        result = self.complete(rtt=600, processing=0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'sample-too-uncertain')
        self.assertEqual(self.rtc.writes, saved)
        self.assertEqual(self.sync.status()['samples'], 1)

    def test_invalid_server_packets_never_update_rtc(self):
        cases = ('leap', 'mode', 'version', 'stratum', 'zero-time', 'processing', 'dispersion', 'precision')
        for case in cases:
            with self.subTest(case=case):
                self.make()
                self.sync.start()
                packet = self.reply()
                if case == 'leap': packet[0] |= 0xc0
                if case == 'mode': packet[0] = 0x23
                if case == 'version': packet[0] = 0x14
                if case == 'stratum': packet[1] = 0
                if case == 'zero-time': packet[40:48] = bytes(8)
                if case == 'processing': packet[40:48] = timestamp(self.clock.base_us + 5000000, self.sync._ntp_delta)
                if case == 'dispersion': packet[8:12] = struct.pack('!I', 65536)
                if case == 'precision': packet[3] = 0
                self.assertFalse(self.complete(packet)['ok'])
                self.assertEqual(self.rtc.writes, [])
                self.assertFalse(self.sync.busy)

    def test_stray_source_or_origin_is_ignored_until_deadline(self):
        self.make()
        self.sync.start()
        packet = self.reply()
        sock = self.sockets.created[-1]
        sock.received.append((packet, ('192.0.2.99', 123)))
        self.assertIsNone(self.sync.tick())
        packet[24:32] = b'oldreply'
        sock.received.append((packet, ('192.0.2.1', 123)))
        self.assertIsNone(self.sync.tick())
        self.assertTrue(self.sync.busy)
        self.clock.value = 2000
        self.assertEqual(self.sync.tick()['error'], 'timeout')
        self.assertEqual(self.sync.next_due_ms, 32000)
        self.assertTrue(sock.closed)
        self.assertEqual(self.rtc.writes, [])

    def test_rtc_write_failure_does_not_promote_sample(self):
        self.make()
        self.rtc.fail = True
        self.sync.start()
        result = self.complete()
        self.assertEqual(result, {'ok': False, 'error': 'rtc-write-failed'})
        self.assertFalse(self.sync.status()['synced'])
        self.assertEqual(self.sync.status()['samples'], 0)
        self.assertTrue(self.sockets.created[-1].closed)

    def test_cancel_and_host_change_preserve_holdover_and_refresh_dns(self):
        self.make()
        self.sync.start()
        self.complete()
        self.sync.start()
        sock = self.sockets.created[-1]
        self.sync.cancel()
        self.assertTrue(sock.closed)
        self.assertTrue(self.sync.status()['synced'])
        self.assertIsNone(self.sync.tick())
        self.sync.configure('new.example')
        self.assertEqual(self.sync.next_due_ms, self.clock.now())
        self.sync.start()
        self.assertEqual(self.sockets.lookups[-1], ('new.example', 123))
        self.sync.cancel()

    def test_unsuccessful_attempts_back_off_and_never_claim_accuracy(self):
        self.make()
        for wait in (30000, 60000):
            self.sync.start()
            self.clock.value += 2000
            self.sync.tick()
            self.assertEqual(self.sync.next_due_ms, self.clock.now() + wait)
            self.assertFalse(self.sync.status()['withinTarget'])
            self.assertIsNone(self.sync.status()['estimatedErrorMs'])


if __name__ == '__main__':
    unittest.main()
