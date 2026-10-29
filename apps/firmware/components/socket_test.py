#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 测试网络连通性
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

import network
import socket
import time

def socket_test(socketObj, timeout=2):
    s = None
    try:
        network.WLAN(network.STA_IF).active(True)
        response = network.WLAN(network.STA_IF).isconnected()
        if not response:
            socketObj.update({"message":"Network not connected--网络未连接","ok": False, "data": {}})
            return socketObj

        addr = socket.getaddrinfo(socketObj['host'], socketObj['port'])[0][-1]
        s = socket.socket()
        s.settimeout(timeout)  # DNS above is synchronous; connect has a timeout.
        start_time = time.ticks_ms()
        s.connect(addr)
        end_time = time.ticks_ms()
        ping = time.ticks_diff(end_time, start_time)
        # print(f"Round trip time--往返时间: {ping} 毫秒")
        socketObj.update({"data": {"ping": ping}, "message": "Connected", "ok": True})
        return socketObj
    except (OSError) as e:
        print(e)
        socketObj.update({"message": "Network not connected--网络未连接", "ok": False, "data": {}})
        return socketObj
    finally:
        if s is not None:
            s.close()
