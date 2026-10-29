#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 连接wifi
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

# 依赖管理
import network
import time

def _connection_failed(wlan, status):
    if status in (network.STAT_WRONG_PASSWORD, 15, 204):
        # ESP32 may report authentication failure or a WPA handshake timeout.
        error, message = 'auth_failed', 'Check password'
    elif status == network.STAT_NO_AP_FOUND:
        error, message = 'no_ap', 'AP not found'
    elif status in (network.STAT_IDLE, network.STAT_CONNECTING):
        error, message = 'timeout', 'Connect timeout'
    else:
        error, message = 'connect_failed', 'Connect failed'
    try:
        wlan.disconnect()
    except OSError:
        pass
    print('WiFi failed--无线网连接失败: {} (status={})'.format(message, status))
    return {'ok': False, 'error': error, 'message': message, 'status': status}


# wifi连接；失败时返回原因，不再永久等待。
def wifi_connector(ssid, password, timeout=20):
    wifiObj = {'ok': True}
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print('Connecting to wifi--连接无线网中...')
        try:
            wlan.disconnect()
            # Bound this attempt so ESP32 exposes the final failure status.
            wlan.config(reconnects=0)
            wlan.connect(ssid, password)
            started = time.ticks_ms()
            while not wlan.isconnected():
                if time.ticks_diff(time.ticks_ms(), started) >= timeout * 1000:
                    return _connection_failed(wlan, wlan.status())
                # An old error status can remain while a new connection succeeds.
                time.sleep_ms(200)
            wlan.config(reconnects=-1)
        except OSError:
            return _connection_failed(wlan, None)
    print('wifi connected--无线网已连接')
    print('wifi info--无线信息：')
    labels = ['IP Address', 'Subnet Mask', 'Gateway', 'DNS']
    for label, value in zip(labels, wlan.ifconfig()):
        wifiObj[label] = value
    print(f'{wifiObj}')
    return wifiObj
