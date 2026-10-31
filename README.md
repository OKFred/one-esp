# one-esp

MicroPython firmware and a responsive USB debugging console for the ESP32-WROOM-32 with a 0.96″ OLED.

[完整中文说明](README_zh_CN.md)

```sh
# Node.js 26.13.0 + pnpm 11.5.0
pnpm install --frozen-lockfile
pnpm dev
```

Open <http://127.0.0.1:5173>. Offline replay works without hardware. The local Hono bridge uses port 8787; production preview is `pnpm build && pnpm start`.

Host Python tools and regression tests use CPython 3.14.2 from `.python-version`, with dependencies locked in `uv.lock` and isolated in the root `.venv`. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed, then run from the repository root:

```sh
uv sync --locked
pnpm test:firmware
uv run --locked python tools/device.py backup --port COM3
uv run --locked python tools/device.py upload --precompile --port COM3
```

`pnpm test:firmware` and `pnpm device` run through `uv run --locked`; no virtual-environment activation or global Python packages are needed. The ESP32 has booted MicroPython 1.29.0, with version, imports and private configuration checked. Application TLS, real environment PUBACKs and stability on 1.29.0 still require acceptance; existing CA trust is preserved rather than automatically replaced.

`--precompile` defaults to isolated `mpy-cross==1.29.0.post2`, whose compiler emits MicroPython 1.29.0 / MPY 6.3 bytecode. The 21 repository runtime sources already include the SSD1306 driver; `boot.py` remains a source entrypoint. For a device rolled back to MicroPython 1.22.2, add `--mpy-version 1.22.2` to select its matching MPY 6.2 compiler. This flag selects a compiler; it does not flash or downgrade MicroPython.

- `apps/firmware`: cooperative MicroPython runtime, bounded USB diagnostics, OLED framebuffer, safe GPIO metadata, revisioned configuration with Wi-Fi trial rollback.
- `apps/bridge`: Node.js serial transport, HTTP / WebSocket, replay and device command forwarding.
- `apps/web`: Vue 3, Ant Design Vue, offline Iconify, Canvas OLED and SVG pin map.
- `packages/protocol`: shared validators and state reduction.

Debug defaults off and expires automatically. GPIO output controls remain disabled. Optional RS485 temperature/humidity sampling defaults off and is enabled through private device configuration. [MCU and environment MQTT telemetry](docs/mqtt-telemetry.md) runs independently of debug; remote debugging and Cloudflare deployment remain planned. Credentials and device backups stay outside Git.

Current host checks pass 134 JS/protocol/web, 309 firmware and 24 upload-tool tests. Historical MicroPython 1.22.2 device evidence includes three temporary sensor reads and a later continuous environment-sampling window with six PUBACKs. Reconnection after a broker kick and the 24-hour stability gate remained unsuccessful in those windows; PG/Grafana ingestion has not been enabled for this device. Local sampling and MQTT delivery are displayed separately. See the [validation record](docs/validation.md) for the distinct test windows and current limits.
