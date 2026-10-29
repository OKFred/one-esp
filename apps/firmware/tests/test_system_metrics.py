"""Read-only metrics budgets and optional API failures, without hardware."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from components.system_metrics import SystemMetrics


class SystemMetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.machine = SimpleNamespace(freq=Mock(return_value=240000000))
        self.gc = SimpleNamespace(mem_free=Mock(return_value=40000),
                                  mem_alloc=Mock(return_value=80000), collect=Mock())
        self.os = SimpleNamespace(statvfs=Mock(return_value=(4096, 512, 100, 75, 50, 0, 0, 0, 0, 255)))
        self.esp32 = SimpleNamespace(HEAP_DATA=4,
                                     idf_heap_info=Mock(return_value=[(100, 30, 20, 10), (200, 80, 60, 40)]),
                                     raw_temperature=Mock(return_value=104))
        self.esp = SimpleNamespace(flash_size=Mock(return_value=4194304))
        self.clock = SimpleNamespace(ticks_us=Mock(side_effect=lambda: self.now * 1000),
                                     ticks_diff=lambda current, previous: current - previous)
        self.wlan = SimpleNamespace(isconnected=Mock(return_value=True), status=Mock(return_value=-61))
        self.metrics = self.make()

    def make(self):
        return SystemMetrics(lambda: self.now, self.machine, self.gc, self.os,
                             self.esp32, self.esp, self.clock, self.wlan)

    def test_reads_units_and_separates_python_budget_from_idf_heap(self):
        sample = self.metrics.sample()
        self.assertEqual(sample, {
            'sampledAtMs': 0, 'intervalMs': 5000, 'cpuHz': 240000000,
            'memory': {'freeBytes': 40000, 'allocatedBytes': 80000, 'totalBytes': 120000},
            'idfHeap': {'totalBytes': 300, 'freeBytes': 110, 'largestBlockBytes': 60},
            'storage': {'sampledAtMs': 0, 'totalBytes': 51200, 'freeBytes': 25600},
            'flashBytes': 4194304,
            'temperature': {'sampledAtMs': 0, 'celsius': 40.0},
            'rssiDbm': -61, 'sampleDurationUs': 0,
        })
        self.machine.freq.assert_called_once_with()
        self.esp32.idf_heap_info.assert_called_once_with(4)
        self.os.statvfs.assert_called_once_with('/')
        self.wlan.status.assert_called_once_with('rssi')
        self.gc.collect.assert_not_called()

    def test_repeated_snapshots_do_not_bypass_fast_or_slow_probe_cadences(self):
        first = self.metrics.sample()
        for self.now in (1, 10, 100, 4999):
            self.assertIs(self.metrics.sample(), first)
        self.machine.freq.assert_called_once()
        self.now = 5000
        self.metrics.sample()
        self.assertEqual(self.machine.freq.call_count, 2)
        self.esp32.raw_temperature.assert_called_once()
        self.os.statvfs.assert_called_once()
        self.now = 30000
        sample = self.metrics.sample()
        self.assertEqual(sample['temperature']['sampledAtMs'], 30000)
        self.assertEqual(sample['storage']['sampledAtMs'], 0)
        self.assertEqual(self.esp32.raw_temperature.call_count, 2)
        self.now = 60000
        sample = self.metrics.sample()
        self.assertEqual(sample['storage']['sampledAtMs'], 60000)
        self.assertEqual(self.os.statvfs.call_count, 2)
        self.esp.flash_size.assert_called_once()

    def test_lite_interval_can_shorten_again_without_waiting_for_old_deadline(self):
        first = self.metrics.sample(15000)
        self.now = 14999
        self.assertIs(self.metrics.sample(15000), first)
        sample = self.metrics.sample(5000)
        self.assertIsNot(sample, first)
        self.assertEqual(sample['intervalMs'], 5000)
        self.now = 15000
        self.assertIs(self.metrics.sample(15000), sample)
        self.assertEqual(sample['intervalMs'], 15000)

    def test_lite_profile_updates_cached_cadence_without_repeating_probes(self):
        first = self.metrics.sample()
        self.now = 1000
        sample = self.metrics.sample(15000)
        self.assertIs(sample, first)
        self.assertEqual(sample['sampledAtMs'], 0)
        self.assertEqual(sample['intervalMs'], 15000)
        self.machine.freq.assert_called_once()

    def test_nonpositive_intervals_cannot_trigger_unbounded_collection(self):
        first = self.metrics.sample(0)
        self.now = 1
        self.assertIs(self.metrics.sample(-1), first)
        self.assertEqual(first['intervalMs'], 5000)

    def test_unavailable_apis_yield_null_without_import_time_or_constructor_probes(self):
        empty = SimpleNamespace()
        metrics = SystemMetrics(lambda: self.now, empty, empty, empty, empty, empty, empty, empty)
        self.machine.freq.assert_not_called()
        sample = metrics.sample()
        for key in ('cpuHz', 'idfHeap', 'storage', 'flashBytes', 'temperature', 'rssiDbm', 'sampleDurationUs'):
            self.assertIsNone(sample[key])
        self.assertEqual(sample['memory'], {'freeBytes': None, 'allocatedBytes': None, 'totalBytes': None})

    def test_api_errors_and_memory_error_are_independent_and_slow_failures_are_limited(self):
        self.machine.freq.side_effect = OSError('unavailable')
        self.gc.mem_alloc.side_effect = MemoryError()
        self.esp32.idf_heap_info.side_effect = MemoryError()
        self.os.statvfs.side_effect = OSError('unmounted')
        self.esp32.raw_temperature.side_effect = OSError('unsupported')
        self.esp.flash_size.side_effect = OSError('unsupported')
        self.wlan.status.side_effect = OSError('disconnected')
        sample = self.metrics.sample()
        self.assertEqual(sample['memory'], {'freeBytes': 40000, 'allocatedBytes': None, 'totalBytes': None})
        for key in ('cpuHz', 'idfHeap', 'storage', 'flashBytes', 'temperature', 'rssiDbm'):
            self.assertIsNone(sample[key])
        self.now = 5000
        self.metrics.sample()
        self.os.statvfs.assert_called_once()
        self.esp32.raw_temperature.assert_called_once()
        self.esp.flash_size.assert_called_once()
        self.now = 60000
        self.metrics.sample()
        self.assertEqual(self.os.statvfs.call_count, 2)
        self.assertEqual(self.esp32.raw_temperature.call_count, 2)
        self.esp.flash_size.assert_called_once()

    def test_slow_probe_failure_clears_stale_value_and_recovers_on_next_due_sample(self):
        self.metrics.sample()
        self.esp32.raw_temperature.side_effect = OSError('busy')
        self.os.statvfs.side_effect = OSError('busy')
        self.now = 60000
        sample = self.metrics.sample()
        self.assertIsNone(sample['storage'])
        self.assertIsNone(sample['temperature'])
        self.os.statvfs.side_effect = None
        self.esp32.raw_temperature.side_effect = None
        self.now = 90000
        sample = self.metrics.sample()
        self.assertIsNone(sample['storage'])
        self.assertEqual(sample['temperature']['sampledAtMs'], 90000)
        self.now = 120000
        self.assertEqual(self.metrics.sample()['storage']['sampledAtMs'], 120000)

    def test_disconnected_wifi_does_not_query_rssi_or_keep_previous_signal(self):
        self.assertEqual(self.metrics.sample()['rssiDbm'], -61)
        self.wlan.isconnected.return_value = False
        self.now = 5000
        self.assertIsNone(self.metrics.sample()['rssiDbm'])
        self.wlan.status.assert_called_once()

    def test_gc_counters_are_read_after_other_probes_without_collecting(self):
        calls = []
        self.machine.freq.side_effect = lambda: calls.append('cpu') or 160000000
        self.esp32.idf_heap_info.side_effect = lambda _: calls.append('idf') or [(100, 30, 20, 10)]
        self.gc.mem_free.side_effect = lambda: calls.append('free') or 100
        self.gc.mem_alloc.side_effect = lambda: calls.append('alloc') or 200
        self.metrics.sample()
        self.assertEqual(calls, ['cpu', 'idf', 'free', 'alloc'])
        self.gc.collect.assert_not_called()

    def test_temperature_fahrenheit_conversion_rounds_positive_and_negative_tenths(self):
        for raw, celsius in ((32, 0.0), (77, 25.0), (103, 39.4), (0, -17.8)):
            with self.subTest(raw=raw):
                self.esp32.raw_temperature.return_value = raw
                self.assertEqual(self.make().sample()['temperature']['celsius'], celsius)

    def test_malformed_optional_values_do_not_create_negative_or_misleading_metrics(self):
        self.machine.freq.return_value = 0
        self.gc.mem_free.return_value = -1
        self.esp32.idf_heap_info.return_value = [(100, -1, 2, 0)]
        self.os.statvfs.return_value = (4096, 0, 100, 75, 50)
        self.esp32.raw_temperature.return_value = True
        self.wlan.status.return_value = 'bad'
        sample = self.metrics.sample()
        for key in ('cpuHz', 'idfHeap', 'storage', 'temperature', 'rssiDbm'):
            self.assertIsNone(sample[key])
        self.assertEqual(sample['memory'], {'freeBytes': None, 'allocatedBytes': 80000, 'totalBytes': None})

    def test_sampling_duration_uses_ticks_diff_across_microsecond_clock_wrap(self):
        self.clock.ticks_us.side_effect = [65530, 7]
        self.clock.ticks_diff = lambda current, previous: (current - previous) % 65536
        self.assertEqual(self.metrics.sample()['sampleDurationUs'], 13)


if __name__ == '__main__':
    unittest.main()
