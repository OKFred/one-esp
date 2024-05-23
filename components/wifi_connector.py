#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 连接wifi
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

# 依赖管理
import network
import time

# wifi连接
def wifi_connector(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print('Connecting to wifi--连接无线网中...')
        wlan.connect(ssid, password)
        while not wlan.isconnected():
            time.sleep(1)
    print('wifi connected--无线网已连接')
    print('wifi info--无线信息：')
    labels = ['IP Address', 'Subnet Mask', 'Gateway', 'DNS']
    for label, value in zip(labels, wlan.ifconfig()):
        print(f'{label}: {value}')