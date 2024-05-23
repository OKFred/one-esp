from machine import Pin, I2C
from lib.ssd1306 import SSD1306_I2C

# 初始化I2C和OLED显示屏

i2c = I2C(0, scl=Pin(4), sda=Pin(5)) # 根据设备的引脚设置进行调整
max_width = 128  # oled_width
max_height = 64  # oled_height

def init_oled():
    try:
        oled = SSD1306_I2C(max_width, max_height, i2c)
        return oled
    except OSError as e:
        print(f"OLED initialization failed--显示屏初始化失败: {e}")
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

oled = init_oled()
def screen_display(text):
    if not oled:
        print("OLED display not initialized--显示屏未初始化")
        return False
    oled.fill(0)  # 清空显示屏
    lines = []
    current_line = ""
    words = text.split(' ')
    
    for word in words:
        potential_line = current_line + ' ' + word if current_line else word
        if get_text_width(potential_line) > max_width:
            lines.append(current_line)
            current_line = word
        else:
            current_line = potential_line

    lines.append(current_line)
    
    if len(lines) * 16 > max_height:
        print("Text too long to display--文本过长")
        return False

    for i, line in enumerate(lines):
        oled.text(line, 0, i * 16)

    oled.show() 
    return True
