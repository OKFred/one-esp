# Frozen profile history

## Dynamic TLS buffers: superseded reference

`one-esp-wroom32-frozen-dynamic-tls` used MicroPython v1.29.0 and ESP-IDF v5.5.2. Its application image was 1,796,864 bytes, SHA256 `b997b51204a4a1209147a5348c421a6e7a55a85c2e15d8cf0593915bc99e6359`. The full reference hashes and parameterized-recipe checks are preserved in `sources.json.referenceHistory`.

Frozen imports and configuration preservation passed. Normal MQTT operation did not: separate cold-start windows showed watchdog resets, and an instrumented window produced a `StoreProhibited` panic with a decoded stack through native heap allocation, socket receive and dynamic TLS RX buffering. Removing only MQTT tick avoided the extra reset in its short control. Removing MCU temperature collection, enlarging a worker stack, or bypassing the device DNS worker did not resolve it. See [validation](../../docs/validation.md) for the distinct windows.

The last phase marker and allocator stack narrow the observed boundary; they do not establish the original corruption write or an upstream vulnerability. The failed windows are not counted as MQTT reconnection or long-term stability acceptance.

## Static TLS buffers: current reference

`one-esp-wroom32-frozen-static-tls` retains the same versions and 20 public application sources. The only effective SDK configuration change is `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y` to `n`; the dropped dependent unset configuration line remains disabled. DTLS stays disabled, RX/TX remain 16384/4096, and strict CA/hostname/time validation is unchanged. The build metadata profile changes so the running image can be distinguished.

The static application is 1,794,192 bytes, SHA256 `e7fbb874d19dafd3377e5195e7e4726c2ea4b12a0c4c339985adfe235c24f930`, and fits the unchanged factory partition at `0x10000`. Source and build hashes are in `sources.json`; device checks belong in [validation](../../docs/validation.md). A short normal-production or controlled reconnect result must remain separate from 24-hour stability and downstream storage acceptance.
