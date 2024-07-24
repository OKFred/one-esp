import ntptime
import utime
import time

def time_sync(server='time.apple.com'):
    try:
        ntptime.host = server
        ntptime.settime()
        print("Time synchronized successfully--时间同步成功")
        return True
    except OSError as e:
        print(f"Failed to synchronize time--时间同步失败: {e}")
        return False

def get_formatted_time():
    current_time = utime.localtime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        current_time[0], current_time[1], current_time[2], 
        current_time[3], current_time[4], current_time[5]
    )

# 获取本地时间并应用时区偏移
def get_local_time(offset_hours=8):
    utc_time = time.localtime()
    local_time = time.mktime(utc_time) + offset_hours * 3600
    datetime_tuple= time.localtime(local_time)
    # Extract the relevant parts
    year, month, day, hour, minute, second, weekday, yearday  = datetime_tuple
    # Format the string
    formatted_date_time = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(year, month, day, hour, minute, second)
    formatted_simple_date_time= "{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(month, day, hour, minute, second)
    formatted_time = formatted_date_time.split(" ")[1]
    obj = {
        "year": year,
        "month": month,
        "day": day,
        "hour": hour,
        "minute": minute,
        "second": second,
        "weekday": weekday,
        "yearday": yearday,
        "formatted_date_time": formatted_date_time,
        "formatted_simple_date_time": formatted_simple_date_time,
        "formatted_time": formatted_time
    }
    return obj
    
