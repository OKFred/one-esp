#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 读取环境变量
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

def env_reader(filename):
    env = {}
    with open(filename, 'r') as file:
        for line in file:
            # Spaces after '=' are credentials, not formatting. Strip only
            # physical line endings so SSIDs/passwords round-trip exactly.
            line = line.rstrip('\r\n')
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if '=' not in line:
                raise ValueError('Invalid environment configuration line')
            key, value = line.split('=', 1)
            env[key.strip()] = value
    return env
