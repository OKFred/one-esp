"""Preserve one-second RTC motion on MicroPython's float32 builds."""

import importlib.util
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch


class MicroPythonSeconds(int):
    def __add__(self, other):
        if isinstance(other, float):
            # Model ESP32 converting an integer epoch to its float32 arithmetic.
            return struct.unpack('f', struct.pack('f', int(self) + other))[0]
        return int(self) + other


class ClockPrecisionTests(unittest.TestCase):
    def test_current_epochs_advance_each_second_with_fractional_timezones(self):
        now = [846127205]
        timestamps = []

        def localtime(value=None):
            if value is not None:
                self.assertIs(type(value), int)
                timestamps.append(value)
            return (2026, 10, 30, 3, 20, (value or now[0]) % 60, 4, 303)

        fake_time = types.SimpleNamespace(
            localtime=localtime, mktime=lambda _: MicroPythonSeconds(now[0]))
        source = Path(__file__).resolve().parents[1] / 'components/time_sync.py'
        spec = importlib.util.spec_from_file_location('_clock_precision', source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'ntptime': types.SimpleNamespace(),
                                     'utime': fake_time, 'time': fake_time}):
            spec.loader.exec_module(module)
        for offset in (8.0, 5.5, -3.5, 0.0):
            with self.subTest(offset=offset):
                timestamps.clear()
                first = module.get_local_time(offset)
                now[0] += 1
                second = module.get_local_time(offset)
                self.assertEqual(timestamps[1] - timestamps[0], 1)
                self.assertNotEqual(first['formatted_time'], second['formatted_time'])
                self.assertEqual(timestamps[-1], now[0] + int(offset * 3600))


if __name__ == '__main__':
    unittest.main()
