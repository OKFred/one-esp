#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 读取环境变量
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

def env_reader(filename):
    env = {}
    with open(filename, 'r') as file:
        for line in file:
            key, value = line.strip().split('=')
            env[key] = value
    return env
