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
)


FRAME = bytes.fromhex('32382e38a1e62c34382e34250a')


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


if __name__ == '__main__':
    unittest.main()
