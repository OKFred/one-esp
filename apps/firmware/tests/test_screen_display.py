"""Only successfully written OLED pixels may become mirror snapshots."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class Oled:
    def __init__(self):
        self.buffer = bytearray(1024)
        self.fail = False

    def fill(self, value):
        self.buffer[:] = bytes([value]) * len(self.buffer)

    def text(self, text, x, y):
        content = text.encode('ascii')
        start = y * 16 + x
        self.buffer[start:start + len(content)] = content

    def show(self):
        if self.fail:
            raise OSError('I2C unavailable')


class ScreenDisplayTests(unittest.TestCase):
    def setUp(self):
        self.oled = Oled()
        self.now = 0
        spec = importlib.util.spec_from_file_location('_screen_display_test', ROOT / 'components/screen_display.py')
        self.module = importlib.util.module_from_spec(spec)
        modules = {
            'machine': types.SimpleNamespace(Pin=Mock(), I2C=Mock()),
            'lib.ssd1306': types.SimpleNamespace(SSD1306_I2C=Mock(return_value=self.oled)),
            'time': types.SimpleNamespace(ticks_ms=lambda: self.now, ticks_diff=lambda new, old: new - old),
        }
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.module)

    def test_failed_i2c_write_retains_last_successful_pixels_and_text(self):
        captured = []
        self.module.set_observer(lambda state: captured.append(state))
        self.assertTrue(self.module.screen_display('12:34:56'))
        successful = self.module.display_state()
        self.oled.fail = True
        with self.assertRaises(OSError):
            self.module.screen_display('12:34:57')
        self.assertNotEqual(bytes(self.oled.buffer), successful['buffer'])
        self.assertEqual(self.module.display_state(), successful)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[-1], successful)
        self.oled.fail = False
        self.assertTrue(self.module.screen_display('12:34:58'))
        recovered = self.module.display_state()
        self.assertEqual(recovered['text'], '12:34:58')
        self.assertNotEqual(recovered['buffer'], successful['buffer'])
        self.assertEqual(captured[-1], recovered)

    def test_initial_clear_frame_remains_immutable_before_first_successful_update(self):
        initial = self.module.display_state()
        self.oled.fail = True
        with self.assertRaises(OSError):
            self.module.screen_display('not written')
        self.assertEqual(self.module.display_state(), initial)
        self.assertEqual(initial['buffer'], bytes(1024))
        self.assertEqual(initial['text'], '')

    def test_unavailable_oled_reinitializes_at_most_every_thirty_seconds(self):
        self.module.oled = False
        with patch.object(self.module, 'init_oled', side_effect=[False, self.oled]) as initialize:
            for now in range(0, 30000, 1000):
                self.now = now
                self.assertFalse(self.module.screen_display('waiting'))
            initialize.assert_not_called()
            self.now = 30000
            self.assertFalse(self.module.screen_display('waiting'))
            self.now = 59000
            self.assertFalse(self.module.screen_display('waiting'))
            self.assertEqual(initialize.call_count, 1)
            self.now = 60000
            self.assertTrue(self.module.screen_display('12:34:58'))
            self.assertEqual(initialize.call_count, 2)
            self.assertEqual(self.module.display_state()['text'], '12:34:58')


if __name__ == '__main__':
    unittest.main()
