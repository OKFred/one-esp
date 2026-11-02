"""Polled read-only sensor UART; construction never configures hardware.

The verified device replies with GB2312 ``<temperature>\xa1\xe6,<humidity>%\n``.
ASCII mode accepts only that unit-marked format, without claiming a checksum.
SHT20 RTU mode reads input registers 1/2 with FC04 and mandatory Modbus CRC16.
No AUTO/STOP, sensor configuration, Modbus writes, UART0 access or clock setting
is performed by this module. Protocols are explicit; there is no fallback.

``now_ms`` must be a monotonic, non-wrapping millisecond callback. Reception is
polled without waiting. TX deliberately waits in the same call (usually about
4.2 ms for ASCII or 8.3 ms for RTU at 9600 baud, at most 100 ms), so a scheduler
delay cannot leave DE high and lose the beginning of a fast sensor's reply.
"""

try:
    import ubinascii as binascii
except ImportError:
    import binascii


DEFAULT_INTERVAL_MS = 60000
MIN_INTERVAL_MS = 5000
TX_TIMEOUT_MS = 100
TX_POLL_US = 50
RX_TIMEOUT_MS = 2000
DRAIN_TIMEOUT_MS = 100
QUIET_MS = 10
MAX_FRAME_BYTES = 128
READ_CHUNK_BYTES = 64
REQUEST = b'READ'


class SensorCrcError(ValueError):
    pass


class SensorModbusException(ValueError):
    pass


def crc16_modbus(raw):
    """Return the Modbus CRC16 integer; its low byte goes first on the wire."""
    crc = 0xffff
    for value in raw:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
    return crc


def build_rtu_request(slave_address=1):
    if type(slave_address) is not int or not 1 <= slave_address <= 247:
        raise ValueError('Invalid sensor slave address')
    body = bytes((slave_address, 4, 0, 1, 0, 2))
    crc = crc16_modbus(body)
    return body + bytes((crc & 255, crc >> 8))


def parse_rtu_reading(raw, slave_address=1):
    """Decode exactly one CRC-verified SHT20 FC04 response, never an exception."""
    if (type(slave_address) is not int or not 1 <= slave_address <= 247 or
            not isinstance(raw, (bytes, bytearray)) or len(raw) not in (5, 9)):
        raise ValueError('Invalid sensor RTU frame')
    raw = bytes(raw)
    if crc16_modbus(raw[:-2]) != (raw[-2] | (raw[-1] << 8)):
        raise SensorCrcError('Sensor CRC mismatch')
    if raw[0] != slave_address:
        raise ValueError('Unexpected sensor slave address')
    if raw[1] == 0x84 and len(raw) == 5:
        raise SensorModbusException('Sensor Modbus exception')
    if len(raw) != 9 or raw[1] != 4 or raw[2] != 4:
        raise ValueError('Unexpected sensor response')
    temperature_raw = (raw[3] << 8) | raw[4]
    if temperature_raw & 0x8000:
        temperature_raw -= 0x10000
    temperature = temperature_raw / 10
    humidity = ((raw[5] << 8) | raw[6]) / 10
    if not -40 <= temperature <= 125 or not 0 <= humidity <= 100:
        raise ValueError('Sensor reading outside range')
    return {'temperatureC': temperature, 'relativeHumidityPercent': humidity}


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
                 interval_ms=DEFAULT_INTERVAL_MS, stale_after_ms=None,
                 protocol='ascii_read', slave_address=1, baudrate=9600,
                 timeout_ms=RX_TIMEOUT_MS, time_module=None):
        if type(interval_ms) is not int or interval_ms < MIN_INTERVAL_MS:
            raise ValueError('Sensor interval must be at least 5000 ms')
        if type(protocol) is not str or protocol not in ('ascii_read', 'sht20_rtu'):
            raise ValueError('Invalid sensor protocol')
        if type(slave_address) is not int or not 1 <= slave_address <= 247:
            raise ValueError('Invalid sensor slave address')
        if type(baudrate) is not int or baudrate not in (9600, 14400, 19200):
            raise ValueError('Invalid sensor baud rate')
        if type(timeout_ms) is not int or not 200 <= timeout_ms <= 5000:
            raise ValueError('Invalid sensor receive timeout')
        if stale_after_ms is None:
            stale_after_ms = interval_ms + timeout_ms + TX_TIMEOUT_MS + DRAIN_TIMEOUT_MS
        if type(stale_after_ms) is not int or stale_after_ms < interval_ms:
            raise ValueError('Invalid sensor staleness interval')
        self._now = now_ms
        self._uart_factory = uart_factory
        self._pin_factory = pin_factory
        self._time_module = time_module
        self._uart = None
        self._direction = None
        self._tx_write = None
        self._tx_done = None
        self._set_direction = None
        self._ticks_us = None
        self._ticks_diff = None
        self._sleep_us = None
        self.enabled = True
        self.phase = 'idle'
        self.interval_ms = interval_ms
        self.protocol = protocol
        self.slave_address = slave_address
        self.baudrate = baudrate
        self.timeout_ms = timeout_ms
        self._request = REQUEST if protocol == 'ascii_read' else build_rtu_request(slave_address)
        self._request_len = len(self._request)
        self._rtu_quiet_ms = (35000 + baudrate - 1) // baudrate
        self._rx_last_data_at = None
        self._integrity = 'none'
        self._crc_failures = 0
        self._last_frame_hex = ''
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
        clock = self._time_module
        if clock is None:
            import time
            clock = time
        self._ticks_us = clock.ticks_us
        self._ticks_diff = clock.ticks_diff
        self._sleep_us = clock.sleep_us
        if not all(callable(method) for method in
                   (self._ticks_us, self._ticks_diff, self._sleep_us)):
            raise ValueError('Microsecond clock unavailable')
        if self._uart_factory is None or self._pin_factory is None:
            from machine import Pin, UART
            if self._uart_factory is None:
                self._uart_factory = UART
            if self._pin_factory is None:
                self._pin_factory = Pin
        self._direction = self._pin_factory(13, getattr(self._pin_factory, 'OUT', 1), value=0)
        self._uart = self._uart_factory(1, baudrate=self.baudrate, bits=8, parity=None,
                                       stop=1, tx=15, rx=14, timeout=0,
                                       timeout_char=2, flow=0, txbuf=256, rxbuf=256)
        if not callable(getattr(self._uart, 'txdone', None)):
            raise ValueError('UART completion check unavailable')
        # Allocate bound methods before asserting DE, where even a GC pause
        # could miss the sensor's first response bytes.
        self._tx_write = self._uart.write
        self._tx_done = self._uart.txdone
        self._set_direction = self._direction.value

    def _release_hardware(self):
        direction, uart = self._direction, self._uart
        self._direction, self._uart = None, None
        self._tx_write = self._tx_done = self._set_direction = None
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
        self._remember_frame(self._buffer or b'')
        self._integrity = 'none'
        self._rx_last_data_at = None
        if category == 'crc_mismatch':
            self._crc_failures += 1
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

    def _remember_frame(self, frame):
        try:
            self._last_frame_hex = binascii.hexlify(frame[:MAX_FRAME_BYTES]).decode()
        except Exception:
            self._last_frame_hex = ''

    def _receive(self, now):
        if now >= self._deadline:
            self._fail('rx_timeout')
            return None
        available = self._uart.any()
        if available:
            remaining = MAX_FRAME_BYTES - len(self._buffer)
            chunk = self._uart.read(min(available, remaining, READ_CHUNK_BYTES))
            if chunk:
                if len(chunk) > remaining:
                    self._fail('buffer_overflow')
                    return None
                self._buffer.extend(chunk)
                self._rx_last_data_at = now
        if self.protocol == 'sht20_rtu':
            return self._receive_rtu(now)
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
        self._integrity = 'format_only'
        return self._complete(values, frame, now)

    def _receive_rtu(self, now):
        if len(self._buffer) < 2:
            return None
        expected = 5 if self._buffer[1] & 0x80 else 9
        if len(self._buffer) > expected:
            self._fail('invalid_frame')
            return None
        if len(self._buffer) < expected:
            return None
        # RTU has no newline: require a full inter-frame silent interval before
        # accepting, so a following fragment cannot become a second response.
        if self._rx_last_data_at is None or now - self._rx_last_data_at < self._rtu_quiet_ms:
            return None
        frame = bytes(self._buffer)
        try:
            values = parse_rtu_reading(frame, self.slave_address)
        except SensorCrcError:
            self._fail('crc_mismatch')
            return None
        except SensorModbusException:
            self._fail('modbus_exception')
            return None
        except ValueError:
            self._fail('invalid_frame')
            return None
        self._integrity = 'crc_verified'
        return self._complete(values, frame, now)

    def _complete(self, values, frame, now):
        self._remember_frame(frame)
        self._samples += 1
        values['sample'] = self._samples
        values['readAtMs'] = now
        self._latest = values
        self._last_error = None
        self._buffer = None
        self._rx_last_data_at = None
        self.phase = 'idle'
        return dict(values)

    def _transmit(self):
        # Resolve every bound method and request before DE rises. No status,
        # logs, frame construction or outer runtime work runs while TX is live.
        write, done, direction = self._tx_write, self._tx_done, self._set_direction
        ticks, diff, pause = self._ticks_us, self._ticks_diff, self._sleep_us
        request, size = self._request, self._request_len
        limit, poll = TX_TIMEOUT_MS * 1000, TX_POLL_US
        category = None
        self.phase = 'tx'
        started = ticks()
        try:
            direction(1)
            if write(request) != size:
                category = 'tx_short_write'
            else:
                while not done():
                    if diff(ticks(), started) >= limit:
                        category = 'tx_timeout'
                        break
                    pause(poll)
        finally:
            # This must precede failure reporting, allocations and every return,
            # including exceptions and maintenance KeyboardInterrupt/SystemExit.
            direction(0)
        if category is not None:
            self._fail(category)
            return None
        now = self._now()
        self.phase = 'rx'
        self._deadline = now + self.timeout_ms
        return self._receive(now)

    def tick(self):
        """Poll RX or finish bounded synchronous TX; return only a new reading."""
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
                self._rx_last_data_at = None
                self._integrity = 'none'
                self._last_frame_hex = ''
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
                return self._transmit()
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
                'protocol': self.protocol, 'integrity': self._integrity,
                'crcFailures': self._crc_failures, 'lastFrameHex': self._last_frame_hex,
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
        self._integrity = 'none'
        self._rx_last_data_at = None
        self._release_hardware()
