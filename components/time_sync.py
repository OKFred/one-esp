import ntptime
import utime

def time_sync():
    try:
        ntptime.settime()
        print("Time synchronized successfully--时间同步成功")
    except OSError as e:
        print(f"Failed to synchronize time--时间同步失败: {e}")

def get_formatted_time():
    current_time = utime.localtime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        current_time[0], current_time[1], current_time[2], 
        current_time[3], current_time[4], current_time[5]
    )