# MicroPython firmware

Target: ESP32-WROOM-32, MicroPython 1.22.2, 128×64 SSD1306 OLED.

Upload the contents of this directory to the device filesystem root. Preserve the device's private `.env`; do not upload tests, cache files or local configuration backups. Start with `.env.example` for a new device. `boot.py` starts the cooperative diagnostics runtime; it does not start the legacy Modbus component.

The sensor is disconnected. No RS485 UART, DE/RE signal or sensor sampling is initialized. OLED uses GPIO4=SCL and GPIO5=SDA. UART0 remains the USB REPL/console. GPIO36 and GPIO39 are sampled without changing their input-only mode; floating readings are not meaningful measurements. Other GPIOs are reported as configured, reserved or unknown.

The output-control allowlist in `components/board_profile.py` is empty. Future entries require a reviewed GPIO, a Pin object and an explicit `safeValue`. Reserved pins cannot be opted in. Session expiry restores the reviewed safe values.

## USB protocol

115200 baud, 8N1, no flow control. Lines begin with `@one-esp ` followed by one JSON object and LF. Ordinary boot/REPL output is not a protocol frame. The receiver uses `sys.stdin` and polling; it never initializes a second UART0 driver or executes received Python.

Debug defaults to off. Send `debug.set` with `enabled=true` to obtain a complete snapshot and enable capped full/lite telemetry. Mutating commands require the current `bootId` and an active session. Repeated command IDs return their cached acknowledgement instead of reapplying the operation. `config.get` omits passwords; network changes are staged and get up to 60 seconds to connect before rollback. During the trial the existing `.env` remains intact. Rebooting during an unfinished trial discards it.

Snapshot requests return `stage=queued`, coalesce and send at most one frame per main loop, with at least two seconds between completed snapshots. GPIO metadata is split into groups of six, all using `partial=true`; consumers merge by GPIO and clear state on boot/session changes. A damaged refresh fragment therefore retains previously known GPIO metadata. OLED keeps only the latest frame. Logs are redacted and bounded to 32 entries/4096 bytes including fixed object overhead. Ordinary telemetry and snapshots share a 7000-byte/second budget; acknowledgements use a separate 2048-byte/second allowance and a bounded pending queue. MQTT, sensor data and remote sessions are reserved for a later release.

The OLED clock uses integer epoch arithmetic, preserving second precision on ESP32 float32 builds. It checks RTC second boundaries every 100ms and redraws only visible changes; pending OLED frames precede periodic GPIO updates. TCP connects once after joining Wi-Fi and every 60 seconds. Its connection is polled without blocking the main loop with a 2-second deadline. `tcpSamples`, `tcpLastStartedMs`, `tcpLastTestMs` and `tcpNextInMs` report its actual schedule. Displayed TCP time measures connection setup.

`ClockSync` uses non-blocking UDP SNTP and writes ESP32 RTC microseconds, preserving NTP fractional seconds and compensating network/server processing delay. It validates packet origin, source, mode, leap and stratum; uncertain or invalid responses retain the previous RTC. The first success schedules another sample after 60 seconds. Subsequent intervals adapt between 30 and 300 seconds using a conservative drift/error budget targeting 500ms. Both NTP and TCP continue during debug (`maintenancePaused=false`, `ntpPaused=false`). DNS is cached, though first resolution remains synchronous.

`status.clock` exposes pre-correction `lastOffsetUs`, RTT, uncertainty, synchronization age, adaptive interval, sample count and estimated holdover error. `withinTarget` and `withinTargetAtNextSync` are conditional estimates, not guarantees of Internet UTC accuracy or oscillator stability. The initial 100ppm drift allowance is an assumption until two spaced samples are available. Failed syncs retain time and retry after 30/60 seconds; offline holdover may exceed the 500ms goal. The dashboard shows the quality estimate and loss of precision instead of claiming the target is always met. See ESP32 [RTC implementation](https://github.com/micropython/micropython/blob/v1.22.2/ports/esp32/machine_rtc.c) and [RFC 5905](https://www.rfc-editor.org/info/rfc5905/) for timestamp/precision background.

## Health metrics

The optional `metrics` capability publishes a separate latest-value event instead of enlarging the once-per-second status. CPU frequency, GC counters, ESP-IDF heap and RSSI are sampled every 5 seconds in FULL and 15 seconds in LITE. Temperature is cached for 30 seconds, filesystem capacity for 60 seconds, and physical Flash size is queried once. Missing APIs or failed reads return null; slow probe attempts remain interval-limited even after failure. Each event includes sampling timestamps and collection duration.

Collection runs only during an active debug session or an explicit read-only snapshot. Snapshots reuse cached samples and cannot force collection more often than once per 5 seconds. Disabling debug stops periodic collection. Sampling does not invoke GC, write files, scan Wi-Fi or run throughput tests, and adds no dependency. Metrics use one bounded frame (roughly under 1KB), the existing byte budget and at-most-one-frame-per-loop scheduling. Consumers accept firmware without the optional event.

`cpuHz` is frequency, not CPU utilization. Stock MicroPython 1.22.2 exposes RSSI but no WLAN TX/RX byte counters or overall CPU utilization; the existing TCP delay is a connection latency, not bandwidth. Python `memory.totalBytes` is `gc.mem_alloc()+gc.mem_free()`, a dynamic allocation budget rather than physical RAM. The GC free value includes expandable ESP-IDF memory, so the separate IDF heap values overlap it and must not be added. Filesystem partition capacity is distinct from physical Flash capacity. Original ESP32 `raw_temperature()` returns Fahrenheit; its converted Celsius value is an internal MCU reference affected by self-heating, not ambient temperature. See [v1.22 quick reference](https://docs.micropython.org/en/v1.22.0/esp32/quickref.html#general-board-control), [GC/IDF heap semantics](https://docs.micropython.org/en/v1.22.0/library/esp32.html#esp32.idf_heap_info), [statvfs fields](https://docs.micropython.org/en/v1.22.0/library/os.html#os.statvfs) and [v1.22.2 WLAN status implementation](https://github.com/micropython/micropython/blob/v1.22.2/ports/esp32/network_wlan.c#L319-L379).

Configuration changes take effect after saving: timezone on the next clock refresh, an actual NTP server change schedules new synchronization, and an actual TCP host/port change clears the DNS cache and schedules a new probe. Saving identical values preserves the current probe/sync deadlines. Wi-Fi changes first pass the 60-second trial. `POLL_INTERVAL_MS` is stored but explicitly inactive while sensors remain disabled; `config.get` describes each field's application timing.

Host tooling and regression tests use CPython 3.14.2 pinned by the root `.python-version`; the ESP32 uses MicroPython 1.22.2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed. Run from the repository root to synchronize the locked dependencies into the local `.venv` and execute the host tests:

```text
uv sync --locked
uv run --locked python -B -m unittest discover -s apps/firmware/tests -v
```

`pnpm test:firmware` wraps the same locked command. No environment activation or global Python dependency installation is required. For backup and upload, use `uv run --locked python tools/device.py backup --port COM3` or `uv run --locked python tools/device.py upload --port COM3`; see the root guide for the backup and credential-preservation behavior.
