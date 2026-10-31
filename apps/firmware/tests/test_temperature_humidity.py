"""Real-format decoding and bounded UART faults without attached hardware."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.temperature_humidity import (
    TemperatureHumidityReader, parse_reading, MAX_FRAME_BYTES, QUIET_MS,
    TX_TIMEOUT_MS, RX_TIMEOUT_MS, DRAIN_TIMEOUT_MS,
    crc16_modbus, build_rtu_request, parse_rtu_reading, SensorCrcError,
    SensorModbusException,
)


FRAME = bytes.fromhex('32382e38a1e62c34382e34250a')
RTU_FRAME = bytes.fromhex('010404013102222ace')


def with_crc(body):
    crc = crc16_modbus(body)
    return body + bytes((crc & 255, crc >> 8))


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value


class Pin:
    OUT = 1

    def __init__(self, number, mode, value):
        self.number, self.mode = number, mode
        self.values = [value]
        self.failure = None

    def value(self, value):
        self.values.append(value)
        if self.failure is not None:
            raise self.failure


class UART:
    def __init__(self):
        self.incoming = bytearray()
        self.writes = []
        self.read_sizes = []
        self.done = False
        self.write_count = 4
        self.read_failure = None
        self.write_failure = None
        self.deinitialized = False

    def any(self):
        return len(self.incoming)

    def read(self, count):
        self.read_sizes.append(count)
        if self.read_failure is not None:
            raise self.read_failure
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def write(self, data):
        self.writes.append(bytes(data))
        if self.write_failure is not None:
            raise self.write_failure
        return self.write_count

    def txdone(self):
        return self.done

    def deinit(self):
        self.deinitialized = True


class ParseReadingTests(unittest.TestCase):
    def test_verified_frame_has_explicit_celsius_and_humidity(self):
        self.assertEqual(parse_reading(FRAME),
                         {'temperatureC': 28.8, 'relativeHumidityPercent': 48.4})

    def test_crlf_negative_zero_and_range_limits_are_real_values(self):
        for frame, expected in (
                (b'-40\xa1\xe6,100%\r\n', (-40, 100)),
                (b'125.0\xa1\xe6,0.0%\n', (125, 0)),
                (b'0\xa1\xe6,0%\n', (0, 0))):
            with self.subTest(frame=frame):
                value = parse_reading(bytearray(frame))
                self.assertEqual((value['temperatureC'], value['relativeHumidityPercent']), expected)

    def test_rejects_incomplete_wrong_units_and_multiple_frames(self):
        for raw in (FRAME[:-1], b'28.8C,48.4%\n', b'28.8\xc2\xb0C,48.4%\n',
                    b'28.8\xa1\xe6,48.4\n', b'garbage\n' + FRAME, FRAME + FRAME,
                    b'28.8\xa1\xe6,48.4%,1\n', b'', '28.8C,48.4%\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_reading(raw)

    def test_rejects_nondecimal_nonfinite_out_of_range_and_oversized(self):
        for raw in (b'nan\xa1\xe6,48.4%\n', b'inf\xa1\xe6,48.4%\n',
                    b'1e2\xa1\xe6,48.4%\n', b' 28.8\xa1\xe6,48.4%\n',
                    b'+28.8\xa1\xe6,48.4%\n', b'28.8\xa1\xe6,-1%\n',
                    b'125.001\xa1\xe6,48.4%\n', b'-40.1\xa1\xe6,48.4%\n',
                    b'28.8\xa1\xe6,100.001%\n', b'28..8\xa1\xe6,48.4%\n',
                    b'.8\xa1\xe6,48.4%\n', b'28.\xa1\xe6,48.4%\n',
                    b'0' * (MAX_FRAME_BYTES + 1)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_reading(raw)


class ModbusParseTests(unittest.TestCase):
    def test_vendor_vectors_use_low_crc_byte_first_and_tenths(self):
        self.assertEqual(crc16_modbus(bytes.fromhex('010400010002')), 0x0b20)
        self.assertEqual(build_rtu_request(), bytes.fromhex('010400010002200b'))
        self.assertEqual(crc16_modbus(bytes.fromhex('01040401310222')), 0xce2a)
        self.assertEqual(parse_rtu_reading(RTU_FRAME),
                         {'temperatureC': 30.5, 'relativeHumidityPercent': 54.6})
        with self.assertRaises(SensorCrcError):
            parse_rtu_reading(RTU_FRAME[:-2] + RTU_FRAME[-2:][::-1])

    def test_negative_temperature_and_fractional_humidity_preserve_tenths(self):
        for temperature_raw, humidity_raw, expected in (
                (0xff85, 567, (-12.3, 56.7)), (0xfe70, 1000, (-40, 100)),
                (1250, 0, (125, 0)), (0, 1, (0, 0.1))):
            body = bytes((17, 4, 4, temperature_raw >> 8, temperature_raw & 255,
                          humidity_raw >> 8, humidity_raw & 255))
            value = parse_rtu_reading(with_crc(body), 17)
            self.assertEqual((value['temperatureC'], value['relativeHumidityPercent']), expected)

    def test_every_single_bit_corruption_is_rejected(self):
        for offset in range(len(RTU_FRAME)):
            for bit in range(8):
                damaged = bytearray(RTU_FRAME)
                damaged[offset] ^= 1 << bit
                with self.subTest(offset=offset, bit=bit), self.assertRaises(SensorCrcError):
                    parse_rtu_reading(damaged)

    def test_wrong_slave_function_and_byte_count_with_valid_crc_are_rejected(self):
        for body in (bytes.fromhex('02040401310222'), bytes.fromhex('01030401310222'),
                     bytes.fromhex('01040601310222'), bytes.fromhex('018302')):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_rtu_reading(with_crc(body))

    def test_modbus_exception_with_valid_crc_never_becomes_a_reading(self):
        with self.assertRaises(SensorModbusException):
            parse_rtu_reading(with_crc(bytes.fromhex('018402')))
        damaged = bytearray(with_crc(bytes.fromhex('018402')))
        damaged[-1] ^= 1
        with self.assertRaises(SensorCrcError):
            parse_rtu_reading(damaged)

    def test_truncation_multiple_frames_and_ascii_never_fall_back(self):
        for frame in (RTU_FRAME[:size] for size in range(len(RTU_FRAME))):
            with self.subTest(length=len(frame)), self.assertRaises(ValueError):
                parse_rtu_reading(frame)
        for frame in (RTU_FRAME + RTU_FRAME, RTU_FRAME + b'\n', FRAME, b'', 'bad'):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                parse_rtu_reading(frame)

    def test_crc_valid_out_of_range_values_are_not_measurements(self):
        for temperature_raw, humidity_raw in ((1251, 500), (0xfe6f, 500), (200, 1001),
                                                (0x8000, 500), (200, 0xffff)):
            body = bytes((1, 4, 4, temperature_raw >> 8, temperature_raw & 255,
                          humidity_raw >> 8, humidity_raw & 255))
            with self.subTest(raw=(temperature_raw, humidity_raw)), self.assertRaises(ValueError):
                parse_rtu_reading(with_crc(body))

    def test_broadcast_and_invalid_slave_addresses_are_rejected(self):
        for address in (0, 248, True, 1.0, '1'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                build_rtu_request(address)

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.uart = UART()
        self.uart_factory = Mock(return_value=self.uart)
        self.pins = []

        def pin_factory(*args, **kwargs):
            pin = Pin(*args, **kwargs)
            self.pins.append(pin)
            return pin

        self.pin_factory = Mock(side_effect=pin_factory)
        self.pin_factory.OUT = Pin.OUT
        self.reader = TemperatureHumidityReader(self.clock.now, self.uart_factory, self.pin_factory)

    def start_tx(self):
        self.assertIsNone(self.reader.tick())
        self.clock.value += QUIET_MS
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.reader.phase, 'tx')

    def start_rx(self):
        self.start_tx()
        self.uart.done = True
        self.clock.value += 5
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.reader.phase, 'rx')

    def sample(self, frame=FRAME):
        self.start_rx()
        self.uart.incoming.extend(frame)
        self.clock.value += 7
        return self.reader.tick()

    def test_constructor_has_no_hardware_and_initial_status_has_no_fake_zero(self):
        self.uart_factory.assert_not_called()
        self.pin_factory.assert_not_called()
        state = self.reader.status()
        self.assertTrue(state['enabled'])
        self.assertTrue(state['stale'])
        self.assertIsNone(state['temperatureC'])
        self.assertIsNone(state['relativeHumidityPercent'])
        self.assertIsNone(state['readAtMs'])
        self.assertIsNone(self.reader.reading())
        self.assertEqual(state['protocol'], 'ascii_read')
        self.assertEqual(state['integrity'], 'none')
        self.assertEqual(state['crcFailures'], 0)

    def test_initializes_only_uart1_and_verified_pins(self):
        self.reader.tick()
        self.pin_factory.assert_called_once_with(13, 1, value=0)
        self.uart_factory.assert_called_once_with(
            1, baudrate=9600, bits=8, parity=None, stop=1, tx=15, rx=14,
            timeout=0, timeout_char=2, flow=0, txbuf=256, rxbuf=256)
        self.assertEqual(self.reader.phase, 'draining')

    def test_drains_unrequested_old_data_before_only_read_command(self):
        self.uart.incoming.extend(FRAME)
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.uart.incoming, b'')
        self.assertIsNone(self.reader.reading())
        self.assertEqual(self.reader.status()['samples'], 0)
        self.clock.value += 1
        self.reader.tick()
        self.clock.value += QUIET_MS
        self.reader.tick()
        self.assertEqual(self.uart.writes, [b'READ'])
        self.assertEqual(self.pins[0].values[-1], 1)

    def test_noisy_input_drain_has_deadline_and_bounded_work(self):
        self.uart.incoming.extend(b'x' * 1000)
        self.reader.tick()
        self.assertEqual(self.uart.read_sizes, [64])
        self.clock.value = DRAIN_TIMEOUT_MS
        self.reader.tick()
        self.assertEqual(self.reader.status()['lastError'], 'input_busy')
        self.assertEqual(self.uart.writes, [])
        self.assertEqual(self.pins[0].values[-1], 0)

    def test_direction_waits_for_true_tx_completion_then_receives(self):
        self.start_tx()
        self.clock.value += 3
        self.reader.tick()
        self.assertEqual(self.pins[0].values[-1], 1)
        self.uart.done = True
        self.reader.tick()
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertEqual(self.reader.phase, 'rx')

    def test_success_is_timestamped_at_complete_frame_not_request(self):
        self.start_rx()
        self.uart.incoming.extend(FRAME[:-1])
        self.clock.value += 5
        self.assertIsNone(self.reader.tick())
        self.assertIsNone(self.reader.reading())
        self.uart.incoming.extend(b'\n')
        self.clock.value += 37
        result = self.reader.tick()
        self.assertEqual(result, {'temperatureC': 28.8, 'relativeHumidityPercent': 48.4,
                                  'sample': 1, 'readAtMs': 57})
        self.assertFalse(self.reader.busy)
        self.assertFalse(self.reader.status()['stale'])
        self.assertEqual(self.reader.status()['integrity'], 'format_only')
        self.assertEqual(self.reader.status()['lastFrameHex'], FRAME.hex())
        self.assertEqual(self.reader.status()['ageMs'], 0)
        result['temperatureC'] = 999
        self.assertEqual(self.reader.reading()['temperatureC'], 28.8)

    def test_micropython_buffer_without_contains_still_accepts_fragmented_frame(self):
        class LimitedBytearray(bytearray):
            def __contains__(self, value):
                raise NotImplementedError('MicroPython bytearray membership unavailable')

        self.start_rx()
        self.reader._buffer = LimitedBytearray()
        self.uart.incoming.extend(FRAME[:-1])
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.reader.phase, 'rx')
        self.uart.incoming.extend(b'\n')
        result = self.reader.tick()
        self.assertEqual(result['temperatureC'], 28.8)
        self.assertEqual(result['relativeHumidityPercent'], 48.4)
        self.assertEqual(result['sample'], 1)
        self.assertEqual(self.reader.status()['failures'], 0)

    def test_next_read_obeys_minute_start_cadence_without_catchup_burst(self):
        self.sample()
        self.clock.value = 59999
        self.reader.tick()
        self.assertEqual(self.uart.writes, [b'READ'])
        self.clock.value = 180000
        self.uart.done = False
        self.reader.tick()
        self.clock.value += QUIET_MS
        self.reader.tick()
        self.assertEqual(self.uart.writes, [b'READ', b'READ'])
        self.assertEqual(self.reader.status()['nextReadInMs'], 59990)

    def test_short_or_missing_write_never_produces_sample_and_lowers_direction(self):
        for count in (None, 0, 3):
            with self.subTest(count=count):
                self.setUp()
                self.uart.write_count = count
                self.reader.tick()
                self.clock.value += QUIET_MS
                self.reader.tick()
                self.assertEqual(self.reader.status()['lastError'], 'tx_short_write')
                self.assertEqual(self.pins[0].values[-1], 0)
                self.assertIsNone(self.reader.reading())

    def test_tx_timeout_lowers_direction_without_spinning(self):
        self.start_tx()
        self.clock.value += TX_TIMEOUT_MS
        self.reader.tick()
        state = self.reader.status()
        self.assertEqual(state['lastError'], 'tx_timeout')
        self.assertEqual(state['samples'], 0)
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertFalse(self.reader.busy)

    def test_rx_timeout_never_turns_missing_data_into_zero(self):
        self.start_rx()
        self.clock.value += RX_TIMEOUT_MS
        self.reader.tick()
        state = self.reader.status()
        self.assertEqual(state['lastError'], 'rx_timeout')
        self.assertEqual(state['failures'], 1)
        self.assertIsNone(state['temperatureC'])
        self.assertIsNone(state['relativeHumidityPercent'])

    def test_invalid_frame_and_oversized_unterminated_input_are_rejected(self):
        for data, error in ((b'junk\n' + FRAME, 'invalid_frame'),
                            (b'x' * MAX_FRAME_BYTES, 'buffer_overflow'),
                            (FRAME + FRAME, 'invalid_frame')):
            with self.subTest(data=data):
                self.setUp()
                self.start_rx()
                self.uart.incoming.extend(data)
                self.reader.tick()
                self.reader.tick()
                self.assertEqual(self.reader.status()['lastError'], error)
                self.assertIsNone(self.reader.reading())
                self.assertLessEqual(max(self.uart.read_sizes), 64)

    def test_failure_retains_old_value_but_marks_it_stale_until_new_success(self):
        self.sample()
        self.clock.value = 60000
        self.uart.done = False
        self.start_rx()
        self.clock.value += RX_TIMEOUT_MS
        self.reader.tick()
        state = self.reader.status()
        self.assertEqual(state['samples'], 1)
        self.assertEqual(state['temperatureC'], 28.8)
        self.assertEqual(state['relativeHumidityPercent'], 48.4)
        self.assertTrue(state['stale'])
        self.assertIsNone(self.reader.reading())
        self.clock.value += state['nextReadInMs']
        self.uart.done = False
        result = self.sample(b'29.1\xa1\xe6,49.0%\n')
        self.assertEqual(result['sample'], 2)
        self.assertEqual(self.reader.reading()['temperatureC'], 29.1)
        self.assertFalse(self.reader.status()['stale'])
        self.assertIsNone(self.reader.status()['lastError'])

    def test_old_success_ages_out_even_without_another_attempt(self):
        result = self.sample()
        self.clock.value = result['readAtMs'] + 62200
        self.assertTrue(self.reader.status()['stale'])
        self.assertIsNone(self.reader.reading())
        self.assertEqual(self.reader.status()['temperatureC'], 28.8)

    def test_uart_exception_releases_hardware_and_only_exports_fixed_category(self):
        self.start_rx()
        self.uart.incoming.extend(FRAME)
        self.uart.read_failure = OSError('private details must not be published')
        self.reader.tick()
        self.assertTrue(self.uart.deinitialized)
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertEqual(self.reader.status()['lastError'], 'io_failed')
        self.assertNotIn('private', repr(self.reader.status()))

    def test_partial_initialization_failure_releases_direction_and_retries_later(self):
        self.uart_factory.side_effect = OSError('unavailable')
        self.reader.tick()
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertEqual(self.reader.status()['lastError'], 'io_failed')
        self.clock.value = 59999
        self.reader.tick()
        self.uart_factory.assert_called_once()

    def test_allocation_failure_during_transmit_releases_bus_without_fake_data(self):
        self.reader.tick()
        self.uart.write_failure = MemoryError()
        self.clock.value += QUIET_MS
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertTrue(self.uart.deinitialized)
        self.assertEqual(self.reader.status()['failures'], 1)
        self.assertTrue(self.reader.status()['stale'])
        self.assertIsNone(self.reader.status()['temperatureC'])

    def test_close_is_idempotent_disables_future_work_and_retains_stale_evidence(self):
        self.sample()
        self.reader.close()
        self.reader.close()
        self.assertTrue(self.uart.deinitialized)
        self.assertEqual(self.pins[0].values[-1], 0)
        self.clock.value = 1000000
        self.reader.tick()
        self.assertEqual(self.uart.writes, [b'READ'])
        self.assertFalse(self.reader.status()['enabled'])
        self.assertEqual(self.reader.phase, 'closed')
        self.assertTrue(self.reader.status()['stale'])
        self.assertIsNone(self.reader.reading())

    def test_maintenance_interrupt_propagates_after_releasing_bus(self):
        self.reader.tick()
        self.uart.write_failure = KeyboardInterrupt()
        self.clock.value += QUIET_MS
        with self.assertRaises(KeyboardInterrupt):
            self.reader.tick()
        self.assertEqual(self.pins[0].values[-1], 0)
        self.assertTrue(self.uart.deinitialized)
        self.assertFalse(self.reader.enabled)

    def test_interval_and_stale_budget_validation(self):
        for value in (0, 4999, True, 5000.0):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                TemperatureHumidityReader(self.clock.now, interval_ms=value)
        with self.assertRaises(ValueError):
            TemperatureHumidityReader(self.clock.now, interval_ms=5000, stale_after_ms=4999)
        reader = TemperatureHumidityReader(self.clock.now, interval_ms=5000)
        self.assertEqual(reader.status()['intervalMs'], 5000)

    def test_tick_has_no_sleep_dependency(self):
        with patch('time.sleep', side_effect=AssertionError('must not block')):
            self.sample()


class RtuReaderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.uart = UART()
        self.uart.write_count = 8
        self.direction = Pin(13, Pin.OUT, value=0)
        self.pin_factory = Mock(return_value=self.direction)
        self.pin_factory.OUT = Pin.OUT
        self.uart_factory = Mock(return_value=self.uart)
        self.reader = TemperatureHumidityReader(self.clock.now, self.uart_factory,
                                                self.pin_factory, protocol='sht20_rtu')

    def start_rx(self):
        self.reader.tick()
        self.clock.value += QUIET_MS
        self.reader.tick()
        self.assertEqual(self.reader.phase, 'tx')
        self.uart.done = True
        self.clock.value += 10
        self.reader.tick()
        self.assertEqual(self.reader.phase, 'rx')

    def deliver(self, frame=RTU_FRAME):
        self.start_rx()
        self.uart.incoming.extend(frame)
        self.clock.value += 10
        self.assertIsNone(self.reader.tick())
        self.clock.value += 4
        return self.reader.tick()

    def test_default_rtu_read_request_and_crc_verified_status_after_response(self):
        self.uart_factory.assert_not_called()
        result = self.deliver()
        self.assertEqual(self.uart.writes, [bytes.fromhex('010400010002200b')])
        self.assertEqual(result['temperatureC'], 30.5)
        self.assertEqual(result['relativeHumidityPercent'], 54.6)
        self.assertEqual(result['readAtMs'], 34)
        state = self.reader.status()
        self.assertEqual(state['integrity'], 'crc_verified')
        self.assertEqual(state['lastFrameHex'], RTU_FRAME.hex())
        self.assertEqual(state['crcFailures'], 0)
        self.assertFalse(state['stale'])
        self.assertEqual(self.direction.values[-1], 0)

    def test_fragmented_response_waits_for_last_byte_and_rtu_silence(self):
        self.start_rx()
        for part in (RTU_FRAME[:3], RTU_FRAME[3:5], RTU_FRAME[5:]):
            self.uart.incoming.extend(part)
            self.clock.value += 2
            self.assertIsNone(self.reader.tick())
            self.assertIsNone(self.reader.reading())
        self.clock.value += 3
        self.assertIsNone(self.reader.tick())
        self.clock.value += 1
        self.assertEqual(self.reader.tick()['relativeHumidityPercent'], 54.6)

    def test_crc_corruption_is_counted_and_never_substitutes_or_falls_back(self):
        damaged = bytearray(RTU_FRAME)
        damaged[4] ^= 1
        self.assertIsNone(self.deliver(damaged))
        state = self.reader.status()
        self.assertEqual(state['lastError'], 'crc_mismatch')
        self.assertEqual(state['crcFailures'], 1)
        self.assertEqual(state['integrity'], 'none')
        self.assertEqual(state['samples'], 0)
        self.assertIsNone(self.reader.reading())
        self.assertEqual(state['lastFrameHex'], bytes(damaged).hex())
        self.assertEqual(self.uart.writes, [build_rtu_request()])

    def test_extra_frame_fragment_inside_silent_interval_is_rejected(self):
        self.start_rx()
        self.uart.incoming.extend(RTU_FRAME)
        self.clock.value += 10
        self.assertIsNone(self.reader.tick())
        self.uart.incoming.extend(b'\x01')
        self.clock.value += 1
        self.assertIsNone(self.reader.tick())
        self.assertEqual(self.reader.status()['lastError'], 'invalid_frame')
        self.assertIsNone(self.reader.reading())

    def test_exception_frame_has_safe_error_and_does_not_count_as_crc_failure(self):
        self.assertIsNone(self.deliver(with_crc(bytes.fromhex('018402'))))
        state = self.reader.status()
        self.assertEqual(state['lastError'], 'modbus_exception')
        self.assertEqual(state['crcFailures'], 0)
        self.assertEqual(state['integrity'], 'none')
        self.assertEqual(state['samples'], 0)

    def test_timeout_is_configurable_and_preserves_partial_hex_without_fake_data(self):
        self.reader = TemperatureHumidityReader(self.clock.now, self.uart_factory,
            self.pin_factory, protocol='sht20_rtu', slave_address=17, baudrate=19200,
            timeout_ms=200)
        self.start_rx()
        self.uart_factory.assert_called_once_with(
            1, baudrate=19200, bits=8, parity=None, stop=1, tx=15, rx=14,
            timeout=0, timeout_char=2, flow=0, txbuf=256, rxbuf=256)
        self.assertEqual(self.uart.writes, [build_rtu_request(17)])
        self.uart.incoming.extend(b'\x11\x04\x04')
        self.reader.tick()
        self.clock.value += 200
        self.reader.tick()
        self.assertEqual(self.reader.status()['lastError'], 'rx_timeout')
        self.assertEqual(self.reader.status()['lastFrameHex'], '110404')
        self.assertIsNone(self.reader.reading())

    def test_ascii_response_is_not_accepted_in_explicit_rtu_mode(self):
        self.assertIsNone(self.deliver(FRAME))
        self.assertEqual(self.reader.status()['lastError'], 'invalid_frame')
        self.assertEqual(self.reader.status()['samples'], 0)

    def test_constructor_bounds_protocol_address_baudrate_and_timeout(self):
        for options in ({'protocol': 'auto'}, {'protocol': None}, {'slave_address': 0},
                        {'slave_address': True}, {'baudrate': 115200}, {'baudrate': 9600.0},
                        {'timeout_ms': 199}, {'timeout_ms': 5001}, {'timeout_ms': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                TemperatureHumidityReader(self.clock.now, **options)

    def test_ascii_bad_frame_trace_is_bounded_and_never_claims_crc(self):
        self.uart.write_count = 4
        self.reader = TemperatureHumidityReader(self.clock.now, self.uart_factory, self.pin_factory)
        self.start_rx()
        self.uart.incoming.extend(b'x' * MAX_FRAME_BYTES)
        self.reader.tick()
        self.reader.tick()
        state = self.reader.status()
        self.assertEqual(state['lastError'], 'buffer_overflow')
        self.assertEqual(len(state['lastFrameHex']), 2 * MAX_FRAME_BYTES)
        self.assertEqual(state['crcFailures'], 0)
        self.assertEqual(state['integrity'], 'none')


if __name__ == '__main__':
    unittest.main()
