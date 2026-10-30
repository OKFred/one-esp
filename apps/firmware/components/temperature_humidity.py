"""Polled READ-only sensor UART; construction never configures hardware.

The verified device replies with GB2312 ``<temperature>\xa1\xe6,<humidity>%\n``.
Only that unit-marked format is accepted. No AUTO/STOP, sensor configuration,
Modbus writes, UART0 access or clock setting is performed by this module.

``now_ms`` must be a monotonic, non-wrapping millisecond callback. While phase
is ``tx``, poll promptly: the four-byte request takes about 4.2 ms at 9600 baud,
and DE must return low before a fast sensor starts its reply.
"""


DEFAULT_INTERVAL_MS = 60000
MIN_INTERVAL_MS = 5000
TX_TIMEOUT_MS = 100
RX_TIMEOUT_MS = 2000
DRAIN_TIMEOUT_MS = 100
QUIET_MS = 10
MAX_FRAME_BYTES = 128
READ_CHUNK_BYTES = 64
REQUEST = b'READ'


def _decimal(raw, signed):
    if not raw or len(raw) > 8:
        raise ValueError('Invalid sensor number')
    negative = raw[0] == 45
    if negative:
        if not signed:
            raise ValueError('Invalid sensor number')
        raw = raw[1:]
    if not raw:
        raise ValueError('Invalid sensor number')
    parts = raw.split(b'.')
    if (len(parts) > 2 or not 1 <= len(parts[0]) <= 3 or
            (len(parts) == 2 and not 1 <= len(parts[1]) <= 3)):
        raise ValueError('Invalid sensor number')
    number = 0
    for part in parts:
        for digit in part:
            if not 48 <= digit <= 57:
                raise ValueError('Invalid sensor number')
            number = number * 10 + digit - 48
    divisor = 10 ** len(parts[1]) if len(parts) == 2 else 1
    value = number / divisor
    return -value if negative else value


def parse_reading(raw):
    """Return validated Celsius and percent, or raise a value-only-safe error."""
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise ValueError('Invalid sensor frame')
    raw = bytes(raw)
    if raw.endswith(b'\r\n'):
        raw = raw[:-2]
    elif raw.endswith(b'\n'):
        raw = raw[:-1]
    else:
        raise ValueError('Incomplete sensor frame')
    fields = raw.split(b',')
    if (len(fields) != 2 or not fields[0].endswith(b'\xa1\xe6') or
            not fields[1].endswith(b'%')):
        raise ValueError('Invalid sensor units')
    temperature = _decimal(fields[0][:-2], True)
    humidity = _decimal(fields[1][:-1], False)
    if not -40 <= temperature <= 125 or not 0 <= humidity <= 100:
        raise ValueError('Sensor reading outside range')
    return {'temperatureC': temperature, 'relativeHumidityPercent': humidity}


class TemperatureHumidityReader:
    def __init__(self, now_ms, uart_factory=None, pin_factory=None,
                 interval_ms=DEFAULT_INTERVAL_MS, stale_after_ms=None):
        if type(interval_ms) is not int or interval_ms < MIN_INTERVAL_MS:
            raise ValueError('Sensor interval must be at least 5000 ms')
        if stale_after_ms is None:
            stale_after_ms = interval_ms + RX_TIMEOUT_MS + TX_TIMEOUT_MS + DRAIN_TIMEOUT_MS
        if type(stale_after_ms) is not int or stale_after_ms < interval_ms:
            raise ValueError('Invalid sensor staleness interval')
        self._now = now_ms
        self._uart_factory = uart_factory
        self._pin_factory = pin_factory
        self._uart = None
        self._direction = None
        self.enabled = True
        self.phase = 'idle'
        self.interval_ms = interval_ms
        self._stale_after_ms = stale_after_ms
        self._next_due = self._now()
        self._deadline = 0
        self._quiet_since = None
        self._buffer = None
        self._latest = None
        self._samples = 0
        self._failures = 0
        self._last_error = None

    @property
    def busy(self):
        return self.phase in ('draining', 'tx', 'rx')

    def _initialize(self):
        if self._uart is not None:
            return
        if self._uart_factory is None or self._pin_factory is None:
            from machine import Pin, UART
            if self._uart_factory is None:
                self._uart_factory = UART
            if self._pin_factory is None:
                self._pin_factory = Pin
        self._direction = self._pin_factory(13, getattr(self._pin_factory, 'OUT', 1), value=0)
        self._uart = self._uart_factory(1, baudrate=9600, bits=8, parity=None,
                                       stop=1, tx=15, rx=14, timeout=0,
                                       timeout_char=2, flow=0, txbuf=256, rxbuf=256)
        if not callable(getattr(self._uart, 'txdone', None)):
            raise ValueError('UART completion check unavailable')

    def _release_hardware(self):
        direction, uart = self._direction, self._uart
        self._direction, self._uart = None, None
        try:
            if direction is not None:
                try:
                    direction.value(0)
                except Exception:
                    pass
        finally:
            if uart is not None:
                try:
                    uart.deinit()
                except Exception:
                    pass

    def _fail(self, category, release=False):
        self._failures += 1
        self._last_error = category
        self.phase = 'idle'
        self._buffer = None
        self._quiet_since = None
        if release:
            self._release_hardware()
        elif self._direction is not None:
            try:
                self._direction.value(0)
            except Exception:
                self._release_hardware()
        # A late or failed attempt never triggers a catch-up burst.
        self._next_due = max(self._next_due, self._now() + self.interval_ms)

    def _receive(self, now):
        if now >= self._deadline:
            self._fail('rx_timeout')
            return None
        available = self._uart.any()
        if not available:
            return None
        remaining = MAX_FRAME_BYTES - len(self._buffer)
        chunk = self._uart.read(min(available, remaining, READ_CHUNK_BYTES))
        if not chunk:
            return None
        if len(chunk) > remaining:
            self._fail('buffer_overflow')
            return None
        self._buffer.extend(chunk)
        # ESP32 MicroPython 1.22.2 lacks bytearray membership operations.
        # find checks the bounded buffer in place, including fragmented input.
        if self._buffer.find(b'\n') < 0:
            if len(self._buffer) == MAX_FRAME_BYTES:
                self._fail('buffer_overflow')
            return None
        # Exactly one complete frame is expected. Do not scan junk for a later
        # plausible number, or attribute multiple unsolicited frames to READ.
        frame = bytes(self._buffer)
        if frame.find(b'\n') != len(frame) - 1:
            self._fail('invalid_frame')
            return None
        try:
            values = parse_reading(frame)
        except ValueError:
            self._fail('invalid_frame')
            return None
        self._samples += 1
        values['sample'] = self._samples
        values['readAtMs'] = now
        self._latest = values
        self._last_error = None
        self._buffer = None
        self.phase = 'idle'
        return dict(values)

    def _transmit_tick(self, now):
        if self._uart.txdone():
            self._direction.value(0)
            self.phase = 'rx'
            self._deadline = now + RX_TIMEOUT_MS
            return self._receive(now)
        if now >= self._deadline:
            self._fail('tx_timeout')
        return None

    def tick(self):
        """Perform bounded work once; return only a newly verified observation."""
        if not self.enabled:
            return None
        now = self._now()
        try:
            if self.phase == 'idle':
                if now < self._next_due:
                    return None
                self._initialize()
                self._next_due = now + self.interval_ms
                self._direction.value(0)
                self._buffer = bytearray()
                self._quiet_since = None
                self._deadline = now + DRAIN_TIMEOUT_MS
                self.phase = 'draining'
            if self.phase == 'draining':
                if now >= self._deadline:
                    self._fail('input_busy')
                    return None
                available = self._uart.any()
                if available:
                    self._uart.read(min(available, READ_CHUNK_BYTES))
                    self._quiet_since = None
                    return None
                if self._quiet_since is None:
                    self._quiet_since = now
                if now - self._quiet_since < QUIET_MS:
                    return None
                self._direction.value(1)
                self.phase = 'tx'
                self._deadline = now + TX_TIMEOUT_MS
                if self._uart.write(REQUEST) != len(REQUEST):
                    self._fail('tx_short_write')
                    return None
                return self._transmit_tick(now)
            if self.phase == 'tx':
                return self._transmit_tick(now)
            if self.phase == 'rx':
                return self._receive(now)
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise
        except Exception:
            # Never export a UART error's message or substitute a zero reading.
            self._fail('io_failed', release=True)
        return None

    def _stale(self, now):
        return (self._latest is None or self._last_error is not None or
                now - self._latest['readAtMs'] >= self._stale_after_ms)

    def reading(self):
        """Return a usable latest observation copy, never an old failed value."""
        if not self.enabled or self._stale(self._now()):
            return None
        return dict(self._latest)

    def status(self):
        now = self._now()
        latest = self._latest
        return {'enabled': self.enabled, 'phase': self.phase,
                'samples': self._samples, 'failures': self._failures,
                'lastError': self._last_error,
                'readAtMs': latest['readAtMs'] if latest is not None else None,
                'ageMs': max(0, now - latest['readAtMs']) if latest is not None else None,
                'stale': not self.enabled or self._stale(now),
                'intervalMs': self.interval_ms,
                'nextReadInMs': max(0, self._next_due - now),
                'temperatureC': latest['temperatureC'] if latest is not None else None,
                'relativeHumidityPercent': latest['relativeHumidityPercent'] if latest is not None else None}

    def close(self):
        """Disable future reads, return DE low, and release only UART1."""
        self.enabled = False
        self.phase = 'closed'
        self._buffer = None
        self._release_hardware()
