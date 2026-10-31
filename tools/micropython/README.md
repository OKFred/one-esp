# MicroPython frozen application profile

This recipe builds `one-esp-wroom32-frozen-static-tls` for the 4MB ESP32-WROOM-32 without PSRAM. It keeps ESP32_GENERIC defaults and the existing partition layout, disables DTLS and dynamic TLS buffers, and retains RX 16384 / TX 4096. Configuration and CA certificates remain reusable; certificate time checks stay enabled. It freezes 22 public application modules and one build marker, while retaining the default frozen standard library. `boot.py` and private configuration files are excluded.

`sources.json` pins MicroPython v1.29.0 (`0fd6c573ea815774668bbb16b8e197c8822368b2`), ESP-IDF v5.5.2 (`30aaf64524299d3bde422ca9a2848090d1bc5d0f`), and the matching Windows package `mpy-cross==1.29.0.post2` (MPY 6.3). `reference-app-sources.json` records the public source snapshot used for the reference image; future source edits require a new build.

The current reference adds revisioned MQTT and sensor settings, selectable RS485 RTU acquisition, and a bounded opt-in remote sensor-configuration inbox. It freezes 22 application modules, including `device_settings.py` and `mqtt_commands.py`. The actual parameterized Windows board, manifest and host patch were exercised by the earlier full build; this feature image was rebuilt incrementally from a new immutable source snapshot and 22-module manifest after preserving the accepted OLED artifacts. Image checksum, frozen module table, source hashes, strict TLS settings and partition compatibility passed offline checks. Effective SDK configuration is unchanged from the accepted static-buffer OLED image. `sources.json` preserves the dynamic, initial static and OLED reference histories. This does not claim relocated byte-identical output or device acceptance for the new feature revision.

Runtime acceptance is recorded separately in [validation](../../docs/validation.md). The dynamic-buffer profile failed device acceptance; its hashes and failure boundaries remain in [profile history](profile-history.md) and `sources.json.referenceHistory`. Static buffering changes allocation behavior and may consume more connection memory. A passing short window would not establish a precise corruption cause, 24-hour stability, or downstream delivery. Strict CA, hostname and time verification must stay enabled.

## Prerequisites

Use separate, already prepared checkouts and a working ESP-IDF v5.5.2 build terminal. Verify each checkout with `git rev-parse HEAD` against `sources.json`. Initialize the SDK submodules and MicroPython's `lib/micropython-lib` and `lib/berkeley-db-1.xx` at their recorded commits. This directory contains no SDK installer, vendor tree, firmware binary, or credentials.

Keep the SDK Python environment isolated from the project's Python 3.14 environment. The reference build used Python 3.12.12, IDF's constrained core requirements, Xtensa `esp-14.2.0_20251107`, ESP32 ULP `2.38_20240113`, CMake 3.30.2, and Ninja 1.12.1. IDF's fixed-tag `tools/tools.json` supplies platform archives and SHA256 checksums. An existing full IDF installation also works; ROM debug symbols are optional for this build-only workflow.

Set `MPY_CHECKOUT` to the pinned MicroPython checkout, `MPY_CROSS_EXE` to the actual matching compiler executable, and `ONE_ESP_BUILD_OUT` to a fresh build directory outside the source tree. `mpy-cross --version` must identify MicroPython v1.29.0 and MPY 6.3. Do not reuse the frozen reference build directory. Keep all `ONE_ESP_*` variables available during initial configuration and subsequent builds, because CMake evaluates the manifest in both phases.

## Native Windows

Use short ASCII paths without spaces or parentheses, as required by IDF. In the SDK build terminal, set `ONE_ESP_POSIX_BIN` to Git for Windows' `usr/bin` directory containing `cat.exe`, `sed.exe`, and `touch.exe`. The following PowerShell assignments affect only this process; they do not use `setx` or change global Python/PATH settings.

Run from the one-esp repository root after setting the prerequisite variables:

```powershell
$recipe = (Resolve-Path tools/micropython).Path
$env:ONE_ESP_RECIPES = $recipe
$env:ONE_ESP_FIRMWARE = (Resolve-Path apps/firmware).Path
$env:ONE_ESP_MICROPYTHON = $env:MPY_CHECKOUT
$env:MICROPY_MPYCROSS = $env:MPY_CROSS_EXE
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PATH = "$env:ONE_ESP_POSIX_BIN;$env:PATH"

$patch = "$recipe/patches/native-windows-qstr.patch"
git -C $env:MPY_CHECKOUT apply --check $patch
git -C $env:MPY_CHECKOUT apply $patch

$board = "$recipe/board".Replace('\', '/')
$manifest = "$recipe/manifest.py".Replace('\', '/')
Push-Location "$env:MPY_CHECKOUT/ports/esp32"
try {
  python "$env:IDF_PATH/tools/idf.py" -B $env:ONE_ESP_BUILD_OUT `
    -D MICROPY_BOARD=ONE_ESP_FROZEN `
    -D "MICROPY_BOARD_DIR=$board" `
    -D "MICROPY_FROZEN_MANIFEST=$manifest" build
} finally {
  Pop-Location
}
```

Apply the patch once to a clean pinned checkout. It changes only host-side qstr generation: argument/response files avoid Windows command limits, and the original cat/sed/GCC/sed stages run without cmd.exe quote parsing. It preserves all flags and sources. `MICROPY_MPYCROSS` prevents the build system from trying to compile a host compiler with GNU make. UTF-8 settings are needed for non-ASCII source comments. The parameterized board, manifest and Windows patch have been exercised by a complete native Windows firmware build and subsequent incremental source rebuilds with new immutable snapshots and manifest paths. The current frozen table contains 22 application modules, one marker and 28 default modules, 51 files in total. Its effective SDK configuration matches the accepted static OLED reference. Historical isolated checks and the dynamic-profile qstr byte comparison remain in `sources.json.referenceHistory`; no byte-equality comparison is claimed for this feature image. Linux remains unexecuted. Device memory, RTU protocol compatibility, MQTT permissions and remote-command acceptance require separate checks; remote commands remain opt-in.

## Linux

The following is the upstream build flow with the custom board and manifest; it has not been executed on Linux for this project. It assumes an exported, working ESP-IDF v5.5.2 environment, initialized submodules, normal POSIX utilities, and a matching mpy-cross. Do not apply the native Windows patch on this path.

```sh
export ONE_ESP_RECIPES="$PWD/tools/micropython"
export ONE_ESP_FIRMWARE="$PWD/apps/firmware"
export ONE_ESP_MICROPYTHON="$MPY_CHECKOUT"
export MICROPY_MPYCROSS="$MPY_CROSS_EXE"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
cd "$MPY_CHECKOUT/ports/esp32"
python "$IDF_PATH/tools/idf.py" -B "$ONE_ESP_BUILD_OUT" \
  -D MICROPY_BOARD=ONE_ESP_FROZEN \
  -D MICROPY_BOARD_DIR="$ONE_ESP_RECIPES/board" \
  -D MICROPY_FROZEN_MANIFEST="$ONE_ESP_RECIPES/manifest.py" build
```

## Inspect before deployment

The application is `micropython.bin`, with factory offset `0x10000` and maximum size `0x1F0000` (2,031,616 bytes). Keep the partition table unchanged. Inspect the final `sdkconfig`, `frozen_content.c`, image checksum, and source hashes; record a new artifact SHA256 for every build. The metadata module `one_esp_build` identifies frozen application firmware. Normal `pnpm device upload` refuses to upload application files to this profile; update public application code by rebuilding with this recipe and flashing the new application image. Read-only device backups remain available. Private configuration stays on the filesystem and is never included by this manifest. A patched checkout may report `v1.29.0-dirty`; its numeric implementation version remains 1.29.0.

This recipe performs no serial or flash operations. On the current four-wire CH340 setup, stock ESP32_GENERIC v1.29.0 was confirmed to lack `machine.bootloader()`: entering ROM requires physical BOOT/EN, and returning to the application after flashing requires EN. Do not infer software bootloader or RTS reset support from other ESP32 variants.

References: [MicroPython ESP32 build instructions](https://github.com/micropython/micropython/blob/v1.29.0/ports/esp32/README.md), [IDF native Windows setup](https://docs.espressif.com/projects/esp-idf/en/v5.5.2/esp32/get-started/windows-setup.html), [IDF tool archives and hashes](https://github.com/espressif/esp-idf/blob/v5.5.2/tools/tools.json).
