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

`pnpm test:firmware` and `pnpm device` run through `uv run --locked`; no virtual-environment activation or global Python packages are needed. The ESP32 continues to run MicroPython 1.22.2.

- `apps/firmware`: cooperative MicroPython runtime, bounded USB diagnostics, OLED framebuffer, safe GPIO metadata, revisioned configuration with Wi-Fi trial rollback.
- `apps/bridge`: Node.js serial transport, HTTP / WebSocket, replay and device command forwarding.
- `apps/web`: Vue 3, Ant Design Vue, offline Iconify, Canvas OLED and SVG pin map.
- `packages/protocol`: shared validators and state reduction.

Debug defaults off and expires automatically. Sensor acquisition and GPIO outputs are disabled. Optional [MCU-temperature MQTT telemetry](docs/mqtt-telemetry.md) uses private configuration and runs independently of debug; remote debugging and Cloudflare deployment remain planned. Credentials and device backups stay outside Git. See the Chinese guide for hardware upload, checks, and current limits.
