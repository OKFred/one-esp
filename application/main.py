import sys
import time

# 添加父文件夹到系统路径
sys.path.append('..')

from components.env_reader import env_reader
from components.wifi_connector import wifi_connector
from components.screen_display import screen_display
from components.socket_test import socket_test

has_timer = False

def main():
    # 读取 .env 文件中的WiFi配置信息
    env = env_reader('.env')
    ssid = env['SSID']
    password = env['PASSWORD']

    # 连接WiFi
    wifiObj=wifi_connector(ssid, password)
    
    # 要显示的文本
    text = f"WiFi Connected!\n {wifiObj['IP Address']}"

    # 在OLED显示屏上显示文本
    screen_display(text)
    socketObj = {
        "host": "baidu.com",
        "port": 443,
    }
    has_timer = True
    while has_timer:
      socketObj = socket_test(socketObj)
      print(socketObj)
      newText = f"{text}\nPing: {socketObj['host']} {socketObj['data']['ping']}ms"
      screen_display(newText)
      print('sleep 100s...')
      time.sleep(100)
      