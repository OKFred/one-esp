import { describe, expect, it } from 'vitest';
import { DeviceCommandSchema } from '@one-esp/protocol';
import { configValuesForSave, readConfigValues } from './config';

const firmwareValues = {
  SSID: 'test-network',
  NTP_SERVER: 'ntp.aliyun.com',
  TIMEZONE_HOURS: '8',
  NETWORK_TEST_HOST: 'baidu.com',
  NETWORK_TEST_PORT: '443',
  POLL_INTERVAL_MS: '500',
};

describe('firmware configuration adapter', () => {
  it('can read unchanged string .env values and submit them through the strict command schema', () => {
    const form = readConfigValues(firmwareValues);
    expect(form.TIMEZONE_HOURS).toBe(8);
    expect(form.NETWORK_TEST_PORT).toBe(443);
    expect(form.POLL_INTERVAL_MS).toBe(500);
    const values = configValuesForSave(form);
    expect(values).not.toHaveProperty('PASSWORD');
    expect(
      DeviceCommandSchema.safeParse({
        v: 1,
        id: 'unchanged-config',
        bootId: 'current-boot',
        command: 'config.set',
        args: { revision: 7, values: JSON.parse(JSON.stringify(values)) },
      }).success,
    ).toBe(true);
  });

  it('retains fractional and zero timezones while never reading a password into the form', () => {
    const form = readConfigValues({
      ...firmwareValues,
      TIMEZONE_HOURS: '5.5',
      PASSWORD: 'do-not-read',
    });
    expect(form.TIMEZONE_HOURS).toBe(5.5);
    expect(form.PASSWORD).toBe('');
    form.TIMEZONE_HOURS = 0;
    form.PASSWORD = 'new-password';
    const values = configValuesForSave(form);
    expect(values.TIMEZONE_HOURS).toBe(0);
    expect(values).toHaveProperty('PASSWORD', 'new-password');
  });

  it.each(['', 'invalid', null, true, '443.5', '70000'])(
    'rejects an invalid port %s instead of submitting a coerced default',
    (value) => {
      expect(() => readConfigValues({ ...firmwareValues, NETWORK_TEST_PORT: value })).toThrow();
    },
  );
});
