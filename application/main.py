import sys

# 添加父文件夹到系统路径
sys.path.append('..')

from components.env_reader import env_reader
from components.wifi_connector import wifi_connector
from components.screen_display import screen_display

def main():
    # 读取 .env 文件中的WiFi配置信息
    env = env_reader('.env')
    ssid = env.get('SSID')
    password = env.get('PASSWORD')

    # 连接WiFi
    wifiObj=wifi_connector(ssid, password)
    
    # 要显示的文本
    text = f"WiFi Connected!\n {wifiObj.get('IP Address')}"

    # 在OLED显示屏上显示文本
    screen_display(text)