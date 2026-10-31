# MicroPython firmware

Target: ESP32-WROOM-32, MicroPython 1.29.0, 128×64 SSD1306 OLED.

MicroPython 1.29.0 has booted on the board; version, module imports and private configuration have been checked. Application TLS, real environment PUBACKs and new stability windows remain unaccepted. Historical real-board results below belong to their recorded 1.22.2 windows and do not establish that the upgrade fixed the failures. Keep the verified CA and complete backups; an upgrade does not automatically replace trust or waive certificate validation.

Upload the contents of this directory to the device filesystem root. Preserve the device's private `.env`; do not upload tests, cache files or local configuration backups. Start with `.env.example` for a new device. `boot.py` starts the cooperative diagnostics runtime; it does not start the legacy Modbus component.

The environment sensor is opt-in through private `.sensor.json`; missing/disabled configuration initializes no RS485 peripheral. When enabled, its fixed driver owns UART1 RX14/TX15 and GPIO13 shared by DE and /RE. OLED uses GPIO4=SCL and GPIO5=SDA. UART0 remains the USB REPL/console. GPIO36 and GPIO39 are sampled without changing their input-only mode; floating readings are not meaningful measurements. Other GPIOs are reported as configured, reserved or unknown.

The output-control allowlist in `components/board_profile.py` is empty; sensor ownership does not enable user writes to GPIO13/14/15. Future output entries require a reviewed GPIO, a Pin object and an explicit `safeValue`. Reserved pins cannot be opted in. Session expiry restores the reviewed safe values.

## USB protocol

115200 baud, 8N1, no flow control. Lines begin with `@one-esp ` followed by one JSON object and LF. Ordinary boot/REPL output is not a protocol frame. The receiver uses `sys.stdin` and polling; it never initializes a second UART0 driver or executes received Python.

Debug defaults to off. Send `debug.set` with `enabled=true` to obtain a complete snapshot and enable capped full/lite telemetry. Mutating commands require the current `bootId` and an active session. Repeated command IDs return their cached acknowledgement instead of reapplying the operation. `config.get` omits passwords; network changes are staged and get up to 60 seconds to connect before rollback. During the trial the existing `.env` remains intact. Rebooting during an unfinished trial discards it.

Snapshot requests return `stage=queued`, coalesce and send at most one frame per main loop, with at least two seconds between completed snapshots. GPIO metadata is split into groups of six, all using `partial=true`; consumers merge by GPIO and clear state on boot/session changes. A damaged refresh fragment therefore retains previously known GPIO metadata. OLED keeps only the latest frame. Logs are redacted and bounded to 32 entries/4096 bytes including fixed object overhead. Ordinary telemetry and snapshots share a 7000-byte/second budget; acknowledgements use a separate 2048-byte/second allowance and a bounded pending queue. Environment sampling and MQTT use separate private configuration and continue independently of debug. MQTT remote debug sessions remain follow-up work.

The TLS-handshake optimization temporarily defers bulk USB snapshots, OLED encoding and resource frames while acknowledgements, TTL expiry and the physical OLED continue. It is undergoing real-board validation; it is not evidence that every long-running reconnection failure has been diagnosed or fixed.

The OLED clock uses integer epoch arithmetic, preserving second precision on ESP32 float32 builds. It checks RTC second boundaries every 100ms and redraws only visible changes; pending OLED frames precede periodic GPIO updates. TCP connects once after joining Wi-Fi and every 60 seconds. Its connection is polled without blocking the main loop with a 2-second deadline. `tcpSamples`, `tcpLastStartedMs`, `tcpLastTestMs` and `tcpNextInMs` report its actual schedule. Displayed TCP time measures connection setup.

`ClockSync` uses non-blocking UDP SNTP and writes ESP32 RTC microseconds, preserving NTP fractional seconds and compensating network/server processing delay. It validates packet origin, source, mode, leap and stratum; uncertain or invalid responses retain the previous RTC. The first success schedules another sample after 60 seconds. Subsequent intervals adapt between 30 and 300 seconds using a conservative drift/error budget targeting 500ms. Both NTP and TCP continue during debug (`maintenancePaused=false`, `ntpPaused=false`). DNS is cached, though first resolution remains synchronous.

`status.clock` exposes pre-correction `lastOffsetUs`, RTT, uncertainty, synchronization age, adaptive interval, sample count and estimated holdover error. `withinTarget` and `withinTargetAtNextSync` are conditional estimates, not guarantees of Internet UTC accuracy or oscillator stability. The initial 100ppm drift allowance is an assumption until two spaced samples are available. Failed syncs retain time and retry after 30/60 seconds; offline holdover may exceed the 500ms goal. The dashboard shows the quality estimate and loss of precision instead of claiming the target is always met. See ESP32 [RTC implementation](https://github.com/micropython/micropython/blob/v1.29.0/ports/esp32/machine_rtc.c) and [RFC 5905](https://www.rfc-editor.org/info/rfc5905/) for timestamp/precision background.

## Automatic renewal

The web viewer enables automatic lease renewal by default after sampling has been explicitly started. The `debug-renew` capability supports `debug.renew` with the current boot ID and a TTL from 1 to 1800 seconds. Firmware requires an active, unexpired session; it only extends that lease, preserves profile/rates, emits an acknowledgement plus immediate debug state, and does not queue a full snapshot. A delayed renewal cannot start sampling after manual stop, expiry or reboot.

Viewers renew ahead of expiry and retain the applied TTL across reconnects to the same device session. A full page reload or a new viewer adopts the default 600-second renewal duration. Closing a viewer stops its renewals, leaving the device to expire its current lease; other viewers can continue renewing. Sleep or browser freezing may still let the lease expire. Renewal failures remain visible. Bridge logs describe actual reported start/renew/stop transitions, without inferring a stop reason. Legacy firmware without the capability is not renewed through `debug.set`.

## Health metrics

The optional `metrics` capability publishes a separate latest-value event instead of enlarging the once-per-second status. CPU frequency, GC counters, ESP-IDF heap and RSSI are sampled every 5 seconds in FULL and 15 seconds in LITE. Temperature is cached for 30 seconds, filesystem capacity for 60 seconds, and physical Flash size is queried once. Missing APIs or failed reads return null; slow probe attempts remain interval-limited even after failure. Each event includes sampling timestamps and collection duration.

Regular health collection runs only during an active debug session or an explicit read-only snapshot. Snapshots reuse cached samples and cannot force collection more often than once per 5 seconds. Disabling debug stops this periodic health collection; independent MQTT/environment reads can still sample MCU temperature. Sampling does not invoke GC, write files, scan Wi-Fi or run throughput tests, and adds no dependency. Metrics use one bounded frame (roughly under 1KB), the existing byte budget and at-most-one-frame-per-loop scheduling. Consumers accept firmware without the optional event.

`cpuHz` is frequency, not CPU utilization. The current collector reports RSSI but does not collect WLAN TX/RX byte counters or overall CPU utilization; the existing TCP delay is a connection latency, not bandwidth. Python `memory.totalBytes` is `gc.mem_alloc()+gc.mem_free()`, a dynamic allocation budget rather than physical RAM. The GC free value includes expandable ESP-IDF memory, so the separate IDF heap values overlap it and must not be added. Filesystem partition capacity is distinct from physical Flash capacity. Original ESP32 `raw_temperature()` returns Fahrenheit; its converted Celsius value is an internal MCU reference affected by self-heating, not ambient temperature. See [v1.29 quick reference](https://docs.micropython.org/en/v1.29.0/esp32/quickref.html#general-board-control), [GC/IDF heap semantics](https://docs.micropython.org/en/v1.29.0/library/esp32.html#esp32.idf_heap_info), [statvfs fields](https://docs.micropython.org/en/v1.29.0/library/os.html#os.statvfs) and [v1.29.0 WLAN status implementation](https://github.com/micropython/micropython/blob/v1.29.0/ports/esp32/network_wlan.c).

## Runtime recovery

USB input, environment sampling, network state, network maintenance, clock display, telemetry and scheduled restart execute as separate guarded steps. An unexpected exception backs off the failed step for one second while other steps continue. Logs contain only the stage and exception class, at most once per stage per 30 seconds. KeyboardInterrupt/SystemExit remain available for deliberate maintenance and mpremote uploads.

The OLED mirror retains the last buffer/text successfully written over I2C. A failed physical write cannot publish a new simulated device time. The clock commits its view only after a successful display call. Display initialization retries at a bounded low frequency. This does not add an irreversible hardware watchdog that could interrupt raw-REPL uploads; permanent native-call blocking or failed essential runtime construction still needs device-level recovery.

## Independent MQTT telemetry

See [MQTT telemetry](../../docs/mqtt-telemetry.md) and the placeholder `.mqtt.example.json`. Actual `.mqtt.json` and `.mqtt-ca.der` remain private and Git-ignored. Missing/disabled configuration does not import MQTT/TLS or start a connection. With the environment sensor disabled, the default path reads MCU temperature every 60 seconds and converts Fahrenheit to `cpu_temperature_c`. This path remains independent of the USB debug lease and does not represent ambient temperature.

With the sensor enabled, one validated physical read supplies `temperature_c` and `humidity_pct`, plus an optional fresh `cpu_temperature_c` from the same read cycle. The provider preserves the original read UTC and uses fixed `source.metric_sources` labels. An unavailable, stale or previously consumed observation produces no new envelope; it does not substitute zero or relabel old values with a new timestamp.

The publish-only MQTT 3.1.1 client requires certificate/hostname validation, TLS port 8883, QoS 1 and retain=false. It keeps one original payload in RAM until matching PUBACK, with bounded retries; it does not accumulate offline minute records or persist pending data across reset. Recent successful NTP synchronization gates sampling and connection. DNS runs in one background worker; transport reads/writes are polled and bounded. Real-board checks of TLS latency, publication and OLED continuity are recorded in [validation](../../docs/validation.md); other deployments need their own acceptance. MQTT does not grant remote configuration, GPIO or REPL access.

## Environment sensor

Copy the placeholder `.sensor.example.json` into a separately deployed private `.sensor.json`, then set `enabled=true` and normally keep `interval_seconds=60`. The example defaults to disabled. Restart the application after changing this file; the standard source uploader preserves but does not deploy private settings.

The fixed driver uses 9600/8N1, sends only `READ` and validates the verified unit-marked reply without a GB2312 codec. It drains old input, bounds TX completion to 100ms and RX to 2 seconds, and retains at most 128 response bytes. During the approximately 4.2ms transmit phase, the runtime polls at 1ms and defers OLED/TLS work so it can lower DE promptly for a fast reply. No AUTO/STOP or sensor configuration writes are sent. Failure retains the previous value with stale status instead of fabricating a sample.

`status.environment` exposes values, `readAtMs`, age, phase, counts and staleness. The webpage distinguishes disabled, failed, expired, disconnected and paused updates; a cached status cannot make an old reading fresh. A separate MQTT line shows connection and confirmation state, labels cached evidence as the last known state, and never supplies zero for a missing confirmation count.

Three temporary real-device `READ` queries returned 28.8°C / 48.4%RH; later continuous environment sampling obtained six PUBACKs. A broker kick then exposed a certificate-verification failure during 1.22.2 reconnect despite synchronized NTP. A separate slim test using a reused SSLContext failed at DNS resolution. Sustained reconnection and the 24-hour gate are still unsuccessful; these observations are not evidence of downstream PG/Grafana delivery. See the [separate validation windows](../../docs/validation.md).

Other configuration changes take effect after saving: timezone on the next clock refresh, an actual NTP server change schedules new synchronization, and an actual TCP host/port change clears the DNS cache and schedules a new probe. Saving identical values preserves the current probe/sync deadlines. Wi-Fi changes first pass the 60-second trial. Legacy `POLL_INTERVAL_MS` is stored but does not control the new driver; use `.sensor.json`. `config.get` describes each field's application timing.

Host tooling and regression tests use CPython 3.14.2 pinned by the root `.python-version`; the current ESP32 firmware is MicroPython 1.29.0. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed. Run from the repository root to synchronize the locked dependencies into the local `.venv` and execute the host tests:

```text
uv sync --locked
uv run --locked python -B -m unittest discover -s apps/firmware/tests -v
```

`pnpm test:firmware` wraps the same locked command. No environment activation or global Python dependency installation is required. For backup and upload, use `uv run --locked python tools/device.py backup --port COM3` or `uv run --locked python tools/device.py upload --precompile --port COM3`; see the root guide for the backup and credential-preservation behavior.

`--precompile` defaults to isolated `mpy-cross==1.29.0.post2`, emitting MicroPython 1.29.0 / MPY 6.3 pure bytecode. The 21 runtime sources include `lib/ssd1306.py`; `boot.py` remains source. For a device rolled back to 1.22.2, add `--mpy-version 1.22.2` to use its MPY 6.2 compiler. The flag changes only the compiler, not the installed MicroPython firmware.

Current host checks pass 134 JS/protocol/web, 309 firmware and 24 upload-tool tests. On 1.29.0, repeat strict TLS, DNS/reconnection, UART, clock, OLED, USB, TTL and resource-headroom checks; source or host-test success is not new-firmware acceptance.
