# one-esp

MicroPython firmware and a responsive USB debugging console for the ESP32-WROOM-32 with a 0.96″ OLED.

[完整中文说明](README_zh_CN.md)

```sh
# Node.js 26.13.0 + pnpm 11.5.0
pnpm install --frozen-lockfile
pnpm dev
```

Open <http://127.0.0.1:5173>. Offline replay works without hardware. The local Hono bridge uses port 8787; production preview is `pnpm build && pnpm start`.

- `apps/firmware`: cooperative MicroPython runtime, bounded USB diagnostics, OLED framebuffer, safe GPIO metadata, revisioned configuration with Wi-Fi trial rollback.
- `apps/bridge`: Node.js serial transport, HTTP / WebSocket, replay and device command forwarding.
- `apps/web`: Vue 3, Ant Design Vue, offline Iconify, Canvas OLED and SVG pin map.
- `packages/protocol`: shared validators and state reduction.

Debug defaults off and expires automatically. Sensor acquisition and GPIO outputs are disabled. MQTT / Cloudflare remote debugging are planned, not deployed. Credentials and device backups stay outside Git. See the Chinese guide for hardware upload, checks, and current limits.
