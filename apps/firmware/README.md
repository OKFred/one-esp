# MicroPython firmware

Target: ESP32-WROOM-32, MicroPython 1.22.2, 128×64 SSD1306 OLED.

Upload the contents of this directory to the device filesystem root. Preserve the device's private `.env`; do not upload tests, cache files or local configuration backups. Start with `.env.example` for a new device. `boot.py` starts the cooperative diagnostics runtime; it does not start the legacy Modbus component.

The sensor is disconnected. No RS485 UART, DE/RE signal or sensor sampling is initialized. OLED uses GPIO4=SCL and GPIO5=SDA. UART0 remains the USB REPL/console. GPIO36 and GPIO39 are sampled without changing their input-only mode; floating readings are not meaningful measurements. Other GPIOs are reported as configured, reserved or unknown.

The output-control allowlist in `components/board_profile.py` is empty. Future entries require a reviewed GPIO, a Pin object and an explicit `safeValue`. Reserved pins cannot be opted in. Session expiry restores the reviewed safe values.

## USB protocol

115200 baud, 8N1, no flow control. Lines begin with `@one-esp ` followed by one JSON object and LF. Ordinary boot/REPL output is not a protocol frame. The receiver uses `sys.stdin` and polling; it never initializes a second UART0 driver or executes received Python.

Debug defaults to off. Send `debug.set` with `enabled=true` to obtain a complete snapshot and enable capped full/lite telemetry. Mutating commands require the current `bootId` and an active session. Repeated command IDs return their cached acknowledgement instead of reapplying the operation. `config.get` omits passwords; network changes are staged and get up to 60 seconds to connect before rollback. During the trial the existing `.env` remains intact. Rebooting during an unfinished trial discards it.

Snapshot requests return `stage=queued`, coalesce and send at most one frame per main loop, with at least two seconds between completed snapshots. GPIO metadata is split into groups of six: the first event uses `partial=false`, later groups and periodic changes use `partial=true`; consumers merge these by GPIO. OLED keeps only the latest frame. Logs are redacted and bounded to 32 entries/4096 bytes including fixed object overhead. Ordinary telemetry and snapshots share a 7000-byte/second budget; acknowledgements use a separate 2048-byte/second allowance and a bounded pending queue. MQTT, sensor data and remote sessions are reserved for a later release.

The existing clock and TCP connection test remain available. With debug disabled, NTP and a TCP connection test run once after connecting and every 10 minutes; socket operations have a 2-second timeout. DNS resolution is synchronous and can still delay USB command handling. Enabling debug pauses these maintenance jobs (`maintenancePaused=true` in status) while the OLED clock continues using the RTC. A command arriving during an already-running DNS/socket operation takes effect when that operation returns. The displayed TCP time measures connection setup, not ICMP round-trip time.

Configuration changes take effect after saving: timezone on the next clock refresh, NTP server and TCP host/port on the next maintenance cycle (scheduled within 500 ms while debug is disabled). Wi-Fi changes first pass the 60-second trial. `POLL_INTERVAL_MS` is stored but explicitly inactive while sensors remain disabled; `config.get` describes each field's application timing.

Run host regression tests from the repository root:

```text
python -B -m unittest discover -s apps/firmware/tests -v
```
