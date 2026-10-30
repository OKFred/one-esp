"""Boot allocation policy precedes imports and preserves maintenance exit."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class BootMemoryTests(unittest.TestCase):
    def test_boot_sets_threshold_before_runtime_and_restores_after_interrupt(self):
        threshold = Mock(return_value=-1)
        def run():
            self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,)])
            raise KeyboardInterrupt()
        source = Path(__file__).resolve().parents[1] / 'boot.py'
        with patch.dict(sys.modules, {'gc': types.SimpleNamespace(threshold=threshold),
                                     'application.runtime': types.SimpleNamespace(run=run)}):
            with self.assertRaises(KeyboardInterrupt):
                exec(compile(source.read_bytes(), 'boot.py', 'exec'), {})
        self.assertEqual([call.args for call in threshold.call_args_list], [(), (8192,), (-1,)])

    def test_missing_threshold_api_does_not_prevent_startup(self):
        run = Mock()
        source = Path(__file__).resolve().parents[1] / 'boot.py'
        with patch.dict(sys.modules, {'gc': types.SimpleNamespace(),
                                     'application.runtime': types.SimpleNamespace(run=run)}):
            exec(compile(source.read_bytes(), 'boot.py', 'exec'), {})
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
