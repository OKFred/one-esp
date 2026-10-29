# Local USB bridge

From the repository root, use Node.js 26 LTS and the pinned pnpm version:

```sh
pnpm install
pnpm dev
```

`pnpm dev` builds the shared protocol and starts the bridge and web development
servers. For a production preview, run `pnpm build` followed by `pnpm start`, then
open `http://127.0.0.1:8787`. Set `PORT` to change the bridge port.

The bridge listens on `127.0.0.1:8787`. It does not open a serial port at startup.
Use the web interface to select a port, explicitly connect, and enable the debug
session. Each successful USB open or reconnect requests a fresh snapshot with a
unique command ID, so an already running device can be identified while debug
remains off. Before that request, the port sends one LF to discard any unfinished
frame left by the previous connection. The `normal`, `wifi-error`, and `offline` replay scenarios are clearly
marked simulations and never control a physical device.

On Windows with Node.js >=26.4, the bridge temporarily wakes the event loop every
20 ms while a serial port is opening or open. This works around
[bindings-cpp PR #243](https://github.com/serialport/bindings-cpp/pull/243), where
native callbacks can leave Promise continuations asleep. The timer is unref'ed,
does not keep the process alive, and stops after open failure, device closure, or
close cleanup. Remove this workaround once an upstream native fix is released
and verified on the supported Windows/Node versions. Other platforms do not use
the timer.

USB is fixed to 115200 baud, 8 data bits, no parity, one stop bit, and no flow
control. DTR and RTS are deasserted immediately after opening. Some USB drivers
can briefly toggle control lines during open; this must be checked on the actual
board. The bridge never sends Ctrl-C, enters the REPL, or resets the device on
disconnect. Reconnection uses a bounded 1–10 second backoff, and disconnect cancels
it. Before uploading firmware, use the web Disconnect button or
`POST /api/disconnect` and wait for its success response. Stopping the bridge also
releases the port. A close error is reported rather than claiming the port is
free; resolve the error before starting an uploader. Disconnect does not stop the
program running on the board.

The bridge validates versioned `@one-esp ` JSON lines, accepts arbitrary serial
chunk boundaries, discards lines over 8 KiB, limits the log history to 200 entries,
and disconnects slow WebSocket viewers rather than growing an unbounded queue.
Passwords and credential-shaped fields are redacted from state and device logs;
command payloads are never logged.

USB commands are serialized as complete frames and sent in UTF-8 byte chunks of
at most 64 bytes, with 60 ms between chunks and queued commands. This gives
MicroPython's small UART0 receive ring time to drain while synchronous OLED
telemetry is being transmitted. An 8 KiB frame takes about 8 seconds. Closing a
port rejects active and queued writes; a new connection uses a fresh queue, so
commands cannot leak into another device session.

HTTP and WebSocket interface:

- `GET /api/ports`: `{ "ports": [...] }`; enumeration does not open a port
- `GET /api/state`: the current shared-protocol `BridgeState`
- `GET /api/health`: bridge process health
- `POST /api/connect` with `{ "port": "COM3" }`
- `POST /api/disconnect`
- `POST /api/replay` with `{ "scenario": "normal" }`; also accepts `wifi-error`
  and `offline`. Replay replaces any current USB connection. The offline scenario
  emits a complete simulated snapshot before marking the simulated device offline.
- `POST /api/command` with a validated shared-protocol command
- `GET /ws`: initial `{ "type": "snapshot", "data": BridgeState }`, then
  `{ "type": "event", "data": DeviceEvent }` and
  `{ "type": "connection", "data": ConnectionState }` messages

`POST /api/command` returns `{ "id": "...", "accepted": true }` when the command
has been handed to the selected transport. This is not execution success. Match
the later device `ack` event by `data.id` and inspect `data.ok` / `data.error`.
Replay ACKs identify simulated results; restart, configuration writes, and GPIO
writes are rejected in replay. Real mutations require an active USB connection,
the current device boot ID, and an unexpired debug lease. GPIO writes additionally
require a pin explicitly reported writable by firmware. Firmware remains the
authority for command permissions and configuration validation.

The server binds only to loopback. Request hosts and supplied HTTP/WebSocket
Origins must also be loopback; LAN and public access require a separate
authenticated transport. The static root
contains only the built web application, never firmware configuration or `.env`.
This Node application is not included in the future Cloudflare Worker entrypoint;
only shared protocol types and transport-independent behavior are reusable there.
