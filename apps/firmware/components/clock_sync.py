"""Polled SNTP with integer microseconds and a conditional 500-ms error budget.

DNS is synchronous on ESP32 and cached. UDP waiting never blocks. Error bounds
trust the configured server's timestamps/root metrics and are estimates, not an
authentication or a guarantee against asymmetric paths or changing oscillators.
"""

import os
import socket
import struct
import time
try:
    import uselect as select
except ImportError:
    import select
try:
    import uerrno as errno
except ImportError:
    import errno


TARGET_MS = 500
MAX_SAMPLE_UNCERTAINTY_US = 250000
REPLY_TIMEOUT_MS = 2000
FIRST_INTERVAL_MS = 60000
MIN_INTERVAL_MS = 30000
MAX_INTERVAL_MS = 300000
BASE_DRIFT_PPM = 100
POLL_IN = getattr(select, 'POLLIN', 1)
POLL_OUT = getattr(select, 'POLLOUT', 4)
POLL_ERRORS = (getattr(select, 'POLLERR', 8) | getattr(select, 'POLLHUP', 16) |
               getattr(select, 'POLLNVAL', 32))
WOULD_BLOCK = (getattr(errno, 'EAGAIN', 11), getattr(errno, 'EWOULDBLOCK', 11))
NTP_2024 = 3913056000


def _uint(packet, offset):
    return struct.unpack('!I', packet[offset:offset + 4])[0]


class ClockSync:
    def __init__(self, now_ms, clock_module=None, rtc=None, socket_module=None,
                 poll_factory=None, token_factory=None):
        self._now = now_ms
        self._clock = clock_module if clock_module is not None else time
        if rtc is None:
            from machine import RTC
            rtc = RTC()
        self._rtc = rtc
        self._sockets = socket_module if socket_module is not None else socket
        self._poll_factory = poll_factory if poll_factory is not None else select.poll
        self._token_factory = token_factory if token_factory is not None else os.urandom
        epoch_year = self._clock.gmtime(0)[0]
        if epoch_year not in (1970, 2000):
            raise ValueError('Unsupported clock epoch')
        self._ntp_delta = 2208988800 if epoch_year == 1970 else 3155673600
        self.host = None
        self._address = None
        self._socket = None
        self._poll = None
        self._sent_at = None
        self._deadline = 0
        self._cookie = None
        self.busy = False
        self.next_due_ms = self._now()
        self.interval_ms = FIRST_INTERVAL_MS
        self.samples = 0
        self.failures = 0
        self._last_sync_at = None
        self._last_uncertainty_us = None
        self._last_offset_us = None
        self._last_rtt_ms = None
        self._drift_ppm = BASE_DRIFT_PPM
        self._measured_drift = False
        self._last_error = None

    def configure(self, host):
        if host != self.host:
            self.cancel()
            self.host = host
            self._address = None
            self.request_sync()

    def request_sync(self):
        self.next_due_ms = self._now()

    def cancel(self):
        active, poller = self._socket, self._poll
        self._socket = None
        self._poll = None
        self.busy = False
        self._sent_at = None
        if poller is not None and active is not None:
            try:
                poller.unregister(active)
            except Exception:
                pass
        if active is not None:
            try:
                active.close()
            except Exception:
                pass

    def _rtc_us(self):
        value = self._rtc.datetime()
        seconds = self._clock.mktime((value[0], value[1], value[2], value[4],
                                     value[5], value[6], value[3], 0))
        return seconds * 1000000 + value[7]

    def _write_rtc_us(self, value):
        seconds, micros = divmod(value, 1000000)
        parts = self._clock.gmtime(seconds)
        self._rtc.datetime((parts[0], parts[1], parts[2], parts[6],
                            parts[3], parts[4], parts[5], micros))

    def _failure(self, reason):
        now = self._now()
        self.cancel()
        self.failures += 1
        self._last_error = reason
        retry = 30000 if self.failures == 1 else 60000
        self.next_due_ms = now + retry
        return {'ok': False, 'error': reason}

    def _send_request(self):
        packet = bytearray(48)
        packet[0] = 0x23
        packet[40:48] = self._cookie
        sent_at = self._now()
        try:
            size = self._socket.sendto(packet, self._address)
        except OSError as error:
            if error.args and error.args[0] in WOULD_BLOCK:
                return
            raise
        if size != 48:
            raise OSError('Incomplete NTP request')
        self._sent_at = sent_at
        self._poll.register(self._socket, POLL_IN | POLL_ERRORS)

    def start(self, host=None):
        if host is not None:
            self.configure(host)
        self.cancel()
        self.busy = True
        try:
            if not self.host:
                return self._failure('server-missing')
            if self._address is None:
                self._address = self._sockets.getaddrinfo(self.host, 123)[0][-1]
            self._socket = self._sockets.socket(self._sockets.AF_INET, self._sockets.SOCK_DGRAM)
            self._socket.setblocking(False)
            self._poll = self._poll_factory()
            self._poll.register(self._socket, POLL_OUT | POLL_ERRORS)
            self._cookie = self._token_factory(8)
            if len(self._cookie) != 8:
                return self._failure('request-token')
            self._deadline = self._now() + REPLY_TIMEOUT_MS
            self._send_request()
        except Exception:
            return self._failure('request-failed')
        return None

    def _timestamp_us(self, packet, offset):
        seconds = _uint(packet, offset)
        fraction = _uint(packet, offset + 4)
        if seconds == 0 and fraction == 0:
            raise ValueError('timestamp-missing')
        # Same post-2024 era convention used by MicroPython's ntptime library.
        if seconds < NTP_2024:
            seconds += 0x100000000
        return (seconds - self._ntp_delta) * 1000000 + ((fraction * 1000000) >> 32)

    def _accept(self, packet, received_at, received_rtc):
        if len(packet) < 48:
            raise ValueError('short-reply')
        if packet[0] >> 6 == 3 or (packet[0] & 7) != 4 or ((packet[0] >> 3) & 7) not in (3, 4):
            raise ValueError('invalid-server')
        if not 1 <= packet[1] <= 15:
            raise ValueError('unsynchronized-server')
        receive_us = self._timestamp_us(packet, 32)
        transmit_us = self._timestamp_us(packet, 40)
        processing_us = transmit_us - receive_us
        rtt_ms = received_at - self._sent_at
        if processing_us < 0 or processing_us > rtt_ms * 1000 + 2000:
            raise ValueError('invalid-server-timing')
        delay_us = max(0, rtt_ms * 1000 - processing_us)
        root_delay = struct.unpack('!i', packet[4:8])[0]
        root_delay_us = max(0, root_delay * 1000000 // 65536)
        root_dispersion_us = _uint(packet, 8) * 1000000 // 65536
        exponent = packet[3] if packet[3] < 128 else packet[3] - 256
        precision_us = ((1000000 << exponent) if exponent >= 0 else
                        max(1, 1000000 >> -exponent))
        uncertainty = delay_us // 2 + root_delay_us // 2 + root_dispersion_us + precision_us + 2000
        if uncertainty > MAX_SAMPLE_UNCERTAINTY_US:
            raise ValueError('sample-too-uncertain')
        estimated_us = transmit_us + delay_us // 2
        offset_us = estimated_us - received_rtc
        previous_at = self._last_sync_at
        measurement_interval = MIN_INTERVAL_MS if self._measured_drift else FIRST_INTERVAL_MS
        can_measure = previous_at is not None and received_at - previous_at >= measurement_interval
        # Parse/read time is compensated immediately before the RTC write.
        apply_at = self._now()
        try:
            self._write_rtc_us(estimated_us + max(0, apply_at - received_at) * 1000)
        except Exception:
            raise ValueError('rtc-write-failed')
        completed_at = self._now()
        # Setting the RTC is not assumed instantaneous: GC or native-call work
        # after the timestamp calculation belongs in the saved error budget.
        uncertainty += max(0, completed_at - apply_at) * 1000
        if can_measure:
            elapsed_ms = received_at - previous_at
            drift_budget = abs(offset_us) + uncertainty + self._last_uncertainty_us
            drift = (drift_budget * 1000 + elapsed_ms - 1) // elapsed_ms
            self._drift_ppm = max(BASE_DRIFT_PPM, drift)
            self._measured_drift = True
        self._last_sync_at = completed_at
        self._last_uncertainty_us = uncertainty
        self._last_offset_us = offset_us
        self._last_rtt_ms = rtt_ms
        self.samples += 1
        self.failures = 0
        self._last_error = None
        if self._measured_drift:
            holdover_budget = TARGET_MS * 1000 - uncertainty - 50000
            interval = holdover_budget * 1000 // self._drift_ppm
            self.interval_ms = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, interval))
        else:
            self.interval_ms = FIRST_INTERVAL_MS
        self.next_due_ms = completed_at + self.interval_ms
        self.cancel()
        return {'ok': True, 'offsetUs': offset_us, 'rttMs': rtt_ms,
                'uncertaintyMs': (uncertainty + 999) // 1000,
                'intervalMs': self.interval_ms}

    def tick(self):
        if not self.busy:
            return None
        now = self._now()
        if now >= self._deadline:
            return self._failure('timeout')
        try:
            if self._sent_at is None:
                self._send_request()
                return None
            for event in self._poll.poll(0):
                if event[1] & POLL_ERRORS:
                    return self._failure('socket-error')
                if event[1] & POLL_IN:
                    packet, peer = self._socket.recvfrom(512)
                    received_at = self._now()
                    received_rtc = self._rtc_us()
                    if peer[0] != self._address[0] or peer[1] != 123:
                        return None
                    if len(packet) < 32 or packet[24:32] != self._cookie:
                        return None
                    try:
                        return self._accept(packet, received_at, received_rtc)
                    except ValueError as error:
                        return self._failure(str(error))
        except OSError as error:
            if error.args and error.args[0] in WOULD_BLOCK:
                return None
            return self._failure('receive-failed')
        except Exception:
            return self._failure('clock-update-failed')
        return None

    def status(self):
        now = self._now()
        age = None if self._last_sync_at is None else max(0, now - self._last_sync_at)
        estimated = None
        if age is not None:
            drift_us = (age * self._drift_ppm + 999) // 1000
            estimated = (self._last_uncertainty_us + drift_us + 999) // 1000
        next_estimated = None
        if estimated is not None:
            due_age = max(age, self.next_due_ms - self._last_sync_at)
            due_drift = (due_age * self._drift_ppm + 999) // 1000
            next_estimated = (self._last_uncertainty_us + due_drift + 999) // 1000
        return {'synced': self.samples > 0, 'targetMs': TARGET_MS,
                'estimatedErrorMs': estimated,
                'withinTarget': estimated is not None and estimated <= TARGET_MS,
                'lastSyncAgeMs': age, 'intervalMs': self.interval_ms,
                'nextInMs': max(0, self.next_due_ms - now),
                'lastOffsetUs': self._last_offset_us, 'rttMs': self._last_rtt_ms,
                'uncertaintyMs': None if self._last_uncertainty_us is None else
                    (self._last_uncertainty_us + 999) // 1000,
                'samples': self.samples, 'failures': self.failures,
                'busy': self.busy, 'driftPpm': self._drift_ppm,
                'driftMeasured': self._measured_drift,
                'nextEstimatedErrorMs': next_estimated,
                'withinTargetAtNextSync': next_estimated is not None and next_estimated <= TARGET_MS,
                'lastError': self._last_error}
