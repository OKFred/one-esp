#MicroPython v1.22.2
#Device: ESP32-WROOM
#@description: 初始化OLED显示屏
#@author: Fred Zhang Qi
#@datetime: 2024-05-23

from machine import Pin, I2C
from lib.ssd1306 import SSD1306_I2C
import time

""" 假设你使用的是I2C接口的SSD1306 OLED显示屏：
ESP32 -> OLED Display
GND -> GND
VCC -> VCC
SCL -> GPIO4 (默认I2C时钟引脚)
SDA -> GPIO5 (默认I2C数据引脚) """
# 根据设备的引脚设置进行调整
i2c = None
max_width = 128  # oled_width
max_height = 64  # oled_height

def init_oled():
    global i2c
    try:
        if i2c is None:
            i2c = I2C(0, scl=Pin(4), sda=Pin(5))
        oled = SSD1306_I2C(max_width, max_height, i2c)
        return oled
    except OSError:
        print('OLED initialization failed; retrying in 30 seconds')
        return False
      
def is_chinese(char):
    # 简单地通过字符编码范围来区分中文字符
    return '\u4e00' <= char <= '\u9fff'

def get_text_width(text):
    width = 0
    for char in text:
        if is_chinese(char):
            width += 16  # 中文字符宽度为16像素
        else:
            width += 8  # 英文字符宽度为8像素
    return width

OLED_RETRY_MS = 30000
_last_init_attempt = time.ticks_ms()
oled = init_oled()
_observer = None
_last_text = ''
_last_buffer = bytes(oled.buffer) if oled else b''


def display_state():
    # Rendering mutates oled.buffer before I2C succeeds. Keep the last completed
    # physical write, so a failed write never appears as a fresh mirror frame.
    return {'available': bool(oled), 'buffer': _last_buffer,
            'width': max_width, 'height': max_height, 'format': 'MONO_VLSB',
            'text': _last_text}


def set_observer(observer):
    global _observer
    _observer = observer
    if _observer:
        try:
            _observer(display_state())
        except MemoryError:
            pass  # A mirror cannot prevent physical display initialization.

def screen_display(text):
    global _last_text, _last_buffer, oled, _last_init_attempt
    if not oled:
        now = time.ticks_ms()
        if time.ticks_diff(now, _last_init_attempt) < OLED_RETRY_MS:
            return False
        _last_init_attempt = now
        oled = init_oled()
        if not oled:
            return False
    oled.fill(0)  # 清空显示屏
    lines = text.split('\n')  # 根据换行符"\n"分割文本

    for i, line in enumerate(lines):
        if get_text_width(line) > max_width:
            # 如果一行的宽度超过了最大宽度，则需要进行换行处理
            current_line = ""
            words = line.split(' ')
            for word in words:
                potential_line = current_line + ' ' + word if current_line else word
                if get_text_width(potential_line) > max_width:
                    oled.text(current_line, 0, i * 16)
                    i += 1
                    current_line = word
                else:
                    current_line = potential_line
            oled.text(current_line, 0, i * 16)
        else:
            oled.text(line, 0, i * 16)

    if len(lines) > 4 and get_text_width(lines[-1]) > max_width:
        # 如果显示内容大于4行，并且最后一行溢出，则截断溢出内容并用"..."表示
        truncated_width = max_width - get_text_width("...")
        truncated_text = ""
        for char in lines[-1]:
            if get_text_width(truncated_text + char) <= truncated_width:
                truncated_text += char
            else:
                break
        oled.text(truncated_text + "...", 0, (max_height // 16 - 1) * 16)
        
    # Allocate the immutable snapshot before committing the physical write. If
    # this allocation fails, the old mirror is still a truthful last-success.
    completed_buffer = bytes(oled.buffer)
    oled.show()
    _last_buffer = completed_buffer
    _last_text = text
    if _observer:
        try:
            _observer(display_state())
        except MemoryError:
            pass  # The physical write succeeded; USB mirroring may retry later.
    return True

def screen_clear():
    # print('清理显示内容')
    oled.fill(0x00)
