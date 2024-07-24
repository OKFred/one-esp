import sys
import time
import utime

# 添加父文件夹到系统路径
sys.path.append('..')

from components.env_reader import env_reader
from components.wifi_connector import wifi_connector
from components.screen_display import screen_display
from components.socket_test import socket_test
from components.time_sync import time_sync,   get_local_time
from components.modbus_talker import modbus_talker


def main():
    display_text=''
    has_timer = False
    # 读取 .env 文件中的WiFi配置信息
    env = env_reader('.env')
    ssid = env['SSID']
    password = env['PASSWORD']
    screen_display("Welcome!")
    text="Loading"
    time.sleep(0.4)
    screen_display(text)
    text=text+"..."
    time.sleep(0.3)
    screen_display(text)
    text=text+"..."
    time.sleep(0.2)
    screen_display(text)
    text=text+"..."
    time.sleep(0.1)
    screen_display(text)
    # 连接WiFi
    wifiObj=wifi_connector(ssid, password)
      

    # 要显示的文本
    text = f"WiFi Connected!\n {wifiObj['IP Address']}"

    # 在OLED显示屏上显示文本
    screen_display(text)
    display_text=text
    socketObj = {
        "host": "baidu.com",
        "port": 443,
    }
    has_timer = True
    while has_timer:
      current_time = utime.localtime()
      socketObj = socket_test(socketObj)
      print(socketObj)
      newText = f"{text}\nPing: {socketObj['host']} {current_time[3]}:{current_time[4]} {socketObj['data']['ping']}ms"
      # 同步时间
      sync_result= time_sync("ntp.aliyun.com")
      if sync_result == False:
        time.sleep(2)
        sync_result= time_sync("ntp.tencent.com")
        if sync_result == False:
          time.sleep(2)
          sync_result= time_sync("time.apple.com")
      # 打印当前时间
      print("当前时间",get_local_time())
      screen_display(newText)
      modbus_talker('AUTO',display_text)
      print('sleep 600s...')
      time.sleep(600)
      