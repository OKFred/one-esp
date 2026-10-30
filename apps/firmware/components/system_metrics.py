"""Cached, read-only ESP32 health samples for an active debug session.

The caller supplies a non-wrapping uptime clock and decides when to publish.
Calling sample is the only collection trigger; importing this module or
constructing a sampler does not poll hardware or start a background task.
"""


TEMPERATURE_INTERVAL_MS = 30000
STORAGE_INTERVAL_MS = 60000
MIN_SAMPLE_INTERVAL_MS = 5000


def _module(name, supplied):
    if supplied is not None:
        return supplied
    try:
        return __import__(name)
    except Exception:
        return None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class SystemMetrics:
    def __init__(self, now_ms, machine_module=None, gc_module=None, os_module=None,
                 esp32_module=None, esp_module=None, clock_module=None, wlan=None):
        self._now = now_ms
        self._machine = _module('machine', machine_module)
        self._gc = _module('gc', gc_module)
        self._os = _module('os', os_module)
        self._esp32 = _module('esp32', esp32_module)
        self._esp = _module('esp', esp_module)
        self._clock = _module('time', clock_module)
        self._wlan = wlan
        self._snapshot = None
        self._storage = None
        self._storage_attempted_at = None
        self._temperature = None
        self._temperature_attempted_at = None
        self._flash = None
        self._flash_attempted = False

    def _number(self, module, name):
        try:
            return _count(getattr(module, name)())
        except Exception:
            return None

    def _idf_heap(self):
        try:
            regions = self._esp32.idf_heap_info(self._esp32.HEAP_DATA)
            if not regions:
                return None
            total, free, largest = 0, 0, 0
            for region in regions:
                size, available, block = (_count(region[0]), _count(region[1]), _count(region[2]))
                if (size is None or available is None or block is None or
                        available > size or block > available):
                    return None
                total += size
                free += available
                largest = max(largest, block)
            return {'totalBytes': total, 'freeBytes': free, 'largestBlockBytes': largest}
        except Exception:
            return None

    def _read_storage(self, now):
        self._storage_attempted_at = now
        try:
            stat = self._os.statvfs('/')
            fragment, blocks, available = (_count(stat[1]), _count(stat[2]), _count(stat[4]))
            if not fragment or blocks is None or available is None or available > blocks:
                self._storage = None
            else:
                self._storage = {'sampledAtMs': now, 'totalBytes': fragment * blocks,
                                 'freeBytes': fragment * available}
        except Exception:
            self._storage = None

    def _read_temperature(self, now):
        self._temperature_attempted_at = now
        try:
            # Original ESP32 raw_temperature is integer Fahrenheit, not ambient.
            raw = self._esp32.raw_temperature()
            if not isinstance(raw, int) or isinstance(raw, bool):
                self._temperature = None
                return
            numerator = (raw - 32) * 50
            tenths = ((numerator + 4) // 9 if numerator >= 0
                      else -((-numerator + 4) // 9))
            self._temperature = {'sampledAtMs': now, 'celsius': tenths / 10}
        except Exception:
            self._temperature = None

    def capture_temperature(self):
        """Take one fresh MCU read alongside a physical sensor observation."""
        self._read_temperature(self._now())
        return self._temperature['celsius'] if self._temperature is not None else None

    def _rssi(self):
        try:
            if not self._wlan.isconnected():
                return None
            value = self._wlan.status('rssi')
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        except Exception:
            return None

    def _ticks_us(self):
        try:
            return self._clock.ticks_us()
        except Exception:
            return None

    def sample(self, interval_ms=MIN_SAMPLE_INTERVAL_MS):
        """Return one bounded snapshot, reusing cached values until their due time.

        MicroPython free memory can include an expandable IDF heap region; its
        free+allocated total is a Python allocation budget, not physical RAM.
        The separate IDF heap values overlap that budget and must not be added.
        """
        interval_ms = max(MIN_SAMPLE_INTERVAL_MS, int(interval_ms))
        now = self._now()
        if self._snapshot is not None and now - self._snapshot['sampledAtMs'] < interval_ms:
            # The sampling age stays truthful while the selected publication
            # cadence can change immediately between FULL and LITE profiles.
            self._snapshot['intervalMs'] = interval_ms
            return self._snapshot
        started = self._ticks_us()
        if not self._flash_attempted:
            self._flash_attempted = True
            self._flash = self._number(self._esp, 'flash_size')
        if self._storage_attempted_at is None or now - self._storage_attempted_at >= STORAGE_INTERVAL_MS:
            self._read_storage(now)
        if self._temperature_attempted_at is None or now - self._temperature_attempted_at >= TEMPERATURE_INTERVAL_MS:
            self._read_temperature(now)
        cpu = self._number(self._machine, 'freq')
        heap = self._idf_heap()
        rssi = self._rssi()
        # Read GC counters after other probes and their temporary allocations.
        free = self._number(self._gc, 'mem_free')
        allocated = self._number(self._gc, 'mem_alloc')
        self._snapshot = {
            'sampledAtMs': now,
            'intervalMs': interval_ms,
            'cpuHz': cpu if cpu else None,
            'memory': {'freeBytes': free, 'allocatedBytes': allocated,
                       'totalBytes': free + allocated if free is not None and allocated is not None else None},
            'idfHeap': heap,
            'storage': self._storage,
            'flashBytes': self._flash,
            'temperature': self._temperature,
            'rssiDbm': rssi,
            'sampleDurationUs': None,
        }
        if started is not None:
            try:
                self._snapshot['sampleDurationUs'] = max(0, self._clock.ticks_diff(self._clock.ticks_us(), started))
            except Exception:
                pass
        return self._snapshot
