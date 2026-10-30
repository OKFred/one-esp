"""Declared ownership only: observation must never reconfigure an unknown pin."""

BOARD_ID = 'esp32-oled-096'
OLED_SCL = 4
OLED_SDA = 5
SENSOR_ENABLED = False

# Deliberately empty. Each entry must explicitly provide both a Pin object and
# safeValue (0 or 1) after the external circuit has been reviewed.
DEBUG_OUTPUTS = {}

GPIO_NUMBERS = tuple(range(0, 20)) + (21, 22, 23, 25, 26, 27) + tuple(range(32, 40))
EXPOSED_GPIO = (36, 25, 26, 8, 11, 7, 6, 39, 16, 5, 4, 0, 2, 14, 12, 13, 15, 3, 1)
PROTECTED_GPIO = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)


def pin_description(gpio):
    result = {
        'gpio': gpio, 'label': 'GPIO' + str(gpio), 'mode': 'unknown',
        'owner': None, 'value': None, 'source': 'unavailable', 'writable': False,
    }
    if gpio in (4, 5):
        result.update(mode='i2c', owner='OLED', source='configured',
                      reason='I2C SCL' if gpio == 4 else 'I2C SDA')
    elif gpio in (1, 3):
        result.update(mode='uart', owner='USB console', source='configured',
                      reason='UART0 TX' if gpio == 1 else 'UART0 RX')
    elif 6 <= gpio <= 11:
        result.update(mode='reserved', owner='Flash', reason='Internal flash; do not probe')
    elif gpio in (0, 2, 12, 15):
        result.update(mode='reserved', owner='Boot configuration', reason='Strapping pin')
    elif gpio in (13, 14):
        result.update(owner='Sensor wiring reserved',
                      reason='RS485 sensor disconnected; peripheral is disabled')
    elif gpio in (36, 39):
        result.update(mode='input', reason='Input only; no pull-up/down; floating values may vary')
    elif gpio not in EXPOSED_GPIO:
        result['reason'] = 'Header exposure not confirmed on this board'
    else:
        result['reason'] = 'No application configuration; not reconfigured for monitoring'
    return result


class PinRegistry:
    def __init__(self, inputs=None, outputs=None):
        self.inputs = inputs or {}
        self.outputs = outputs or {}
        self.commanded = {}
        self.descriptions = {gpio: pin_description(gpio) for gpio in GPIO_NUMBERS}
        for gpio, entry in self.outputs.items():
            if gpio in PROTECTED_GPIO or gpio >= 34 or gpio not in GPIO_NUMBERS:
                raise ValueError('Protected GPIO in output allowlist')
            if entry.get('safeValue') not in (0, 1) or 'pin' not in entry:
                raise ValueError('Each output needs a Pin and safeValue')

    def snapshot(self):
        rows = []
        for gpio in GPIO_NUMBERS:
            # Static metadata is shared; sampled rows are copied so the delta
            # sender retains the previous reading without 34 allocations/tick.
            row = self.descriptions[gpio]
            if gpio in self.inputs or gpio in self.outputs:
                row = dict(row)
            if gpio in self.inputs:
                try:
                    row.update(mode='input', value=int(bool(self.inputs[gpio].value())), source='sampled')
                except OSError:
                    row['reason'] = 'Input read failed'
            if gpio in self.outputs:
                row.update(mode='output', owner='Debug allowlist', writable=True,
                           source='commanded', value=self.commanded.get(gpio),
                           reason='Software setting; electrical level is not measured')
            rows.append(row)
        return rows

    def write(self, gpio, value):
        if type(gpio) is not int or gpio not in self.outputs:
            raise ValueError('GPIO is not in the output allowlist')
        if type(value) is not int or value not in (0, 1):
            raise ValueError('GPIO value must be 0 or 1')
        self.outputs[gpio]['pin'].value(value)
        self.commanded[gpio] = value
        return {'gpio': gpio, 'value': value, 'source': 'commanded'}

    def restore_safe_values(self):
        for gpio, entry in self.outputs.items():
            entry['pin'].value(entry['safeValue'])
            self.commanded[gpio] = entry['safeValue']
