# Frozen profile history

## Dynamic TLS buffers: superseded reference

`one-esp-wroom32-frozen-dynamic-tls` used MicroPython v1.29.0 and ESP-IDF v5.5.2. Its application image was 1,796,864 bytes, SHA256 `b997b51204a4a1209147a5348c421a6e7a55a85c2e15d8cf0593915bc99e6359`. The full reference hashes and parameterized-recipe checks are preserved in `sources.json.referenceHistory`.

Frozen imports and configuration preservation passed. Normal MQTT operation did not: separate cold-start windows showed watchdog resets, and an instrumented window produced a `StoreProhibited` panic with a decoded stack through native heap allocation, socket receive and dynamic TLS RX buffering. Removing only MQTT tick avoided the extra reset in its short control. Removing MCU temperature collection, enlarging a worker stack, or bypassing the device DNS worker did not resolve it. See [validation](../../docs/validation.md) for the distinct windows.

The last phase marker and allocator stack narrow the observed boundary; they do not establish the original corruption write or an upstream vulnerability. The failed windows are not counted as MQTT reconnection or long-term stability acceptance.

## Static TLS buffers: initial control reference

`one-esp-wroom32-frozen-static-tls` retains the same versions and 20 public application sources. The only effective SDK configuration change is `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y` to `n`; the dropped dependent unset configuration line remains disabled. DTLS stays disabled, RX/TX remain 16384/4096, and strict CA/hostname/time validation is unchanged. The build metadata profile changes so the running image can be distinguished.

The static application is 1,794,192 bytes, SHA256 `e7fbb874d19dafd3377e5195e7e4726c2ea4b12a0c4c339985adfe235c24f930`, and fits the unchanged factory partition at `0x10000`. Source and build hashes are in `sources.json`; device checks belong in [validation](../../docs/validation.md). A short normal-production or controlled reconnect result must remain separate from 24-hour stability and downstream storage acceptance.

## Static TLS buffers: task-oriented OLED reference

The task-oriented OLED reference kept the same SDK, partition and strict TLS settings as the initial static control. Among its 20 application modules, only `application/runtime.py` changed: the four-row OLED shows the current task or a higher-priority network fault, two detail rows, then the clock. The earlier static source and artifact hashes are preserved in `sources.json.referenceHistory`.

The actual parameterized Windows recipe completed a full build. Intermediate source revisions and artifacts were preserved before the final runtime was rebuilt incrementally from a new immutable source snapshot and manifest. The final runtime SHA256 is `3b7af6cc73345f0dae3717cb8da01fdc8af5d6feaa8f75587dc1e522e71b9596`; the application image is 1,796,096 bytes, SHA256 `6661e86c92baa695317ffb6f366f95c12ca02cd3a866eaadd4cd5eaad6c5d5ff`. Effective SDK configuration and partition checks match the initial static control. This build result does not transfer earlier device acceptance to the new application revision; see [validation](../../docs/validation.md) for its device checks. Linux and relocated byte-identical reproduction are not claimed.

## Static TLS buffers: business settings and RS485 configuration offline reference

The current static profile freezes 22 application modules and one metadata module alongside 28 default modules. New `components/device_settings.py` and `components/mqtt_commands.py` support revisioned settings and a bounded opt-in sensor-configuration inbox; the sensor driver also supports a separately selected RTU mode. Several existing application modules change, as recorded by their new source hashes. Strict CA, hostname and time checks, static TLS buffers, and the partition layout match the accepted OLED reference. The earlier OLED artifact and source hashes remain in `sources.json.referenceHistory`.

This feature image was built incrementally from a new immutable 22-module source snapshot and manifest using the parameterized Windows recipe after preserving prior canonical artifacts. Its application image is 1,819,296 bytes, SHA256 `865e05f27d73210306de5eaef4f9569dfb3ddb53b1252097c7b872c5e65ff739`; runtime SHA256 is `174a1622b74d7b491835841c9663aca490ee0dc2faf07b0ae04b07cea19d3bbf`. Build checks do not establish live memory headroom, the attached sensor's RTU compatibility, broker subscription permission or remote-command delivery. Remote commands are opt-in, and no new broker permission is implied by this image. See [validation](../../docs/validation.md) for separate device acceptance; Linux and relocated byte-identical reproduction are not claimed.

## Static TLS buffers: daily MQTT counters and compact OLED numbers

The daily-counter reference changes only `application/runtime.py` and `components/mqtt_telemetry.py` from the previous 865e settings image. It retains 22 application modules, one marker and 28 default modules. A separate RAM-only acknowledgement count follows the configured local day once a synchronized clock is available; it rolls over when a later day is observed, while the existing total counter stays separate. OLED display abbreviations do not change the underlying counts. Restarting the publisher/device restarts its RAM counters; this revision adds no durable daily-counter storage.

The application image is 1,820,240 bytes, SHA256 `eeb71eed6fc42f262392435e3063ac98aa4b5eb21880b959e377130924bf44bc`; runtime SHA256 is `a574100c8c84778fb35a19cf130b069d22dfadae414a33bcecdd3f2d1d255d38`. It was built incrementally from an immutable source snapshot and passed image, source, frozen-table, static SDK and partition checks. The previous 865e image remains preserved with offline build checks only; it was not attributed any device acceptance. This revision also requires separate device checks, including clock rollover behavior. See [validation](../../docs/validation.md). No Linux or relocated byte-identical reproduction is claimed.
