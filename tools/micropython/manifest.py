# Executed by MicroPython's makemanifest.py, which does not provide __file__.
import os

include("$(PORT_DIR)/boards/manifest.py")

firmware = os.path.abspath(os.environ["ONE_ESP_FIRMWARE"])
recipes = os.path.abspath(os.environ["ONE_ESP_RECIPES"])

freeze(
    firmware,
    (
        'application/main.py',
        'application/runtime.py',
        'components/board_profile.py',
        'components/clock_sync.py',
        'components/debug_service.py',
        'components/device_addr_scanner.py',
        'components/device_config.py',
        'components/device_settings.py',
        'components/env_reader.py',
        'components/modbus_talker.py',
        'components/mqtt_commands.py',
        'components/mqtt_telemetry.py',
        'components/mqtt_transport.py',
        'components/mqtt_wire.py',
        'components/network_probe.py',
        'components/screen_display.py',
        'components/socket_test.py',
        'components/system_metrics.py',
        'components/temperature_humidity.py',
        'components/time_sync.py',
        'components/wifi_connector.py',
        'lib/ssd1306.py',
    ),
    opt=0,
)
freeze(os.path.join(recipes, "board", "generated"), ("one_esp_build.py",), opt=0)
