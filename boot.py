# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()

print('hello world from boot.py of MicroPython!')
print('\n-----------------⚠️-----------------\n')

from application.main import main

main()