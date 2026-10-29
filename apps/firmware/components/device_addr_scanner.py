#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 扫描 I2C 设备地址
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

def i2c_scan(i2c):
  devices = i2c.scan()
  if devices:
      print('I2C devices found:', [hex(device) for device in devices])
      return devices
  else:
      print('No I2C devices found--未发现I2C设备')
      return False