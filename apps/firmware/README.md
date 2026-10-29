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

Configuration changes take effect after saving: timezone on the next clock refresh, an actual NTP server change schedules new synchronization, and an actual TCP host/port change clears the DNS cache and schedules a new probe. Saving identical values preserves the current probe/sync deadlines. Wi-Fi changes first pass the 60-second trial. `POLL_INTERVAL_MS` is stored but explicitly inactive while sensors remain disabled; `config.get` describes each field's application timing.

Run host regression tests from the repository root:

```text
python -B -m unittest discover -s apps/firmware/tests -v
```
