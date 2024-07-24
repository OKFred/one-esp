#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 与Modbus从机通信
#@author: Fred Zhang Qi
#@datetime: 2024-05-24

import binascii
from machine import Pin, UART
from components.screen_display import screen_display,screen_clear
from components.time_sync import get_local_time
import time

# 初始化 UART

def prepare_write():
  uart1 = UART(1, baudrate=9600, bits=8, parity=None, stop=1, tx=Pin(15), rx=Pin(13))
  print(uart1)
  return uart1
    

def write_uart(data, uart1):
   newData = binascii.hexlify(data)
   print(newData)
   uart1.write(data)
   
# uart1.write('\x41\x55\x54\x4F')
# 用于读取接收到的数据

def prepare_read():
  uart2 = UART(2, baudrate=9600, bits=8, parity=None, stop=1, tx=Pin(13), rx=Pin(15))
  print(uart2)
  return uart2

def read_uart(uart2):
    if uart2.any():
        data = uart2.read()
        obj = parse_and_convert(data)
        return obj
    else:
        print('No data received.')
        return

# 解析和转换接收到的数据
def parse_and_convert(data):
    # 如果没有数据，则直接返回
    if not data or len(data) == 0:
       return {} 
    # 将字节数据转换为字符串
    obj={}
    hex_data = binascii.hexlify(data).decode()
    # 分割温度和湿度数据
    # print(data)
    # print(hex_data)
    # Extract temperature and humidity from the hex string
    temperature_hex = hex_data[0:8]
    humidity_hex = hex_data[14:22]
    temperature = hex_to_ascii(temperature_hex)
    humidity = hex_to_ascii(humidity_hex)

    # 输出结果
    obj['temperature'] = temperature
    obj['humidity']  = humidity
    return obj
    
# Convert hex to ASCII
def hex_to_ascii(hex_string):
    ascii_string = ""
    for i in range(0, len(hex_string), 2):
        hex_pair = hex_string[i:i+2]
        ascii_string += chr(int(hex_pair, 16))
    return ascii_string

def modbus_talker(data, display_text):
  receive_count = 0
  correct_count=0
  error_count=0
  uart1=prepare_write()
  write_uart(data, uart1)
  time.sleep(1)
  uart2=prepare_read()
  while True:
    obj  = read_uart(uart2)
    time_obj =get_local_time()
    this_date_time =time_obj["formatted_simple_date_time"]
    this_time = time_obj['formatted_time']
    if not obj:
        error_count=error_count+1
    else :
        correct_count=correct_count+1
        print(this_time+": 数据：", obj['temperature'], obj['humidity'])
        screen_clear()
        new_text = display_text +'\n' +this_date_time +'\n' +obj['temperature'] +'`C ' + obj['humidity'] +'%'
        screen_display(new_text)
    receive_count =receive_count +1
    print(this_time+": 汇总",receive_count, correct_count, error_count)
    time.sleep(1)
    
