# Keep the upstream ESP32_GENERIC features and partition layout.
if(NOT DEFINED ENV{ONE_ESP_MICROPYTHON} OR "$ENV{ONE_ESP_MICROPYTHON}" STREQUAL "")
    message(FATAL_ERROR "Set ONE_ESP_MICROPYTHON to the pinned MicroPython checkout")
endif()
file(TO_CMAKE_PATH "$ENV{ONE_ESP_MICROPYTHON}" ONE_ESP_MICROPYTHON_ROOT)
include("${ONE_ESP_MICROPYTHON_ROOT}/ports/esp32/boards/ESP32_GENERIC/mpconfigboard.cmake")
list(APPEND SDKCONFIG_DEFAULTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.tls")
