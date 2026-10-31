import { describe, expect, it } from 'vitest';
import { DeviceCommandSchema } from '@one-esp/protocol';
import {
  createMqttForm,
  createSensorForm,
  readMqttForm,
  mqttValuesForSave,
  readSensorForm,
  sensorValuesForSave,
} from './device-settings';

const mqttValues = {
  enabled: true,
  host: 'broker.example.com',
  port: 8883,
  server_hostname: 'broker.example.com',
  username: 'test-account',
  client_id: 'test-client',
  site_id: 'test-site',
  gateway_id: 'test-gateway',
  device_id: 'test-device',
  interval_seconds: 60,
  keepalive: 60,
  commands_enabled: false,
};

describe('MQTT settings and secret preservation', () => {
  it.each(['.mqtt-ca.der', 'certs/existing-root.der'])(
    'keeps the existing certificate path %s read-only',
    (ca_file) => {
      const form = readMqttForm({ values: { ...mqttValues, ca_file } });
      expect(mqttValuesForSave(form, true)).not.toHaveProperty('ca_file');
      expect(
        DeviceCommandSchema.safeParse({
          v: 1,
          id: 'ca-path',
          bootId: 'test-boot',
          command: 'mqtt.config.set',
          args: { revision: 1, values: { ca_file } },
        }).success,
      ).toBe(false);
    },
  );
  it('never hydrates a returned password and keeps a blank save secret absent', () => {
    const form = readMqttForm({ values: { ...mqttValues, password: 'never-read-this' } });
    expect(form.password).toBe('');
    expect(JSON.stringify(form)).not.toContain('never-read-this');
    expect(mqttValuesForSave(form, true)).not.toHaveProperty('password');
    form.password = 'new-test-password';
    expect(mqttValuesForSave(form, true).password).toBe('new-test-password');
  });

  it('can disable reporting without clearing the existing account or secret', () => {
    const form = readMqttForm({ values: mqttValues });
    form.enabled = false;
    const saved = mqttValuesForSave(form, true);
    expect(saved).toMatchObject({
      enabled: false,
      host: 'broker.example.com',
      username: 'test-account',
      client_id: 'test-client',
      site_id: 'test-site',
      gateway_id: 'test-gateway',
      device_id: 'test-device',
    });
    expect(saved).not.toHaveProperty('password');
  });

  it('requires a secret for first activation but allows a previously configured one', () => {
    const form = readMqttForm({ values: mqttValues });
    expect(() => mqttValuesForSave(form, false)).toThrow('首次启用 MQTT');
    expect(() => mqttValuesForSave(form, true)).not.toThrow();
    expect(() => mqttValuesForSave({ ...createMqttForm(), enabled: true }, true)).toThrow();
  });

  it('follows a changed broker with SNI unless a separate TLS hostname was chosen', () => {
    const form = readMqttForm({ values: mqttValues });
    expect(form.server_hostname).toBe('');
    form.host = ' next.example.com ';
    expect(mqttValuesForSave(form, true)).toMatchObject({
      host: 'next.example.com',
      server_hostname: 'next.example.com',
    });
    form.server_hostname = ' tls.example.com ';
    expect(mqttValuesForSave(form, true).server_hostname).toBe('tls.example.com');
    const custom = readMqttForm({ values: { ...mqttValues, server_hostname: 'tls.example.com' } });
    expect(custom.server_hostname).toBe('tls.example.com');
  });

  it('sends an explicit empty fixed address to remove an existing broker IP', () => {
    const form = readMqttForm({ values: { ...mqttValues, broker_ip: '192.0.2.10' } });
    form.broker_ip = ' ';
    expect(mqttValuesForSave(form, true)).toHaveProperty('broker_ip', '');
  });
});

describe('strict RS485 protocol settings', () => {
  it('preserves the explicit RTU profile and validates the actual command envelope', () => {
    const values = sensorValuesForSave({
      ...createSensorForm(),
      enabled: true,
      protocol: 'sht20_rtu',
      interval_seconds: 5,
      baudrate: 19200,
      slave_address: 17,
      timeout_ms: 5000,
    });
    expect(readSensorForm({ values })).toEqual(values);
    expect(
      DeviceCommandSchema.safeParse({
        v: 1,
        id: 'sensor-settings',
        bootId: 'current-boot',
        command: 'sensor.config.set',
        args: { revision: 3, values },
      }).success,
    ).toBe(true);
    expect(values).not.toHaveProperty('crc');
    expect(
      DeviceCommandSchema.safeParse({
        v: 1,
        id: 'missing-device-context',
        command: 'sensor.config.set',
        args: { revision: 3, values },
      }).success,
    ).toBe(false);
  });

  it.each([
    { protocol: 'auto' },
    { baudrate: 115200 },
    { slave_address: 0 },
    { slave_address: 248 },
    { timeout_ms: 199 },
    { timeout_ms: 5001 },
    { interval_seconds: 4 },
    { interval_seconds: 3601 },
    { crc: false },
    { tx: 2 },
    { calibration: 10 },
  ])('rejects unsupported settings instead of silently changing them: %j', (invalid) => {
    const candidate = { ...createSensorForm(), ...invalid };
    expect(() => sensorValuesForSave(candidate as ReturnType<typeof createSensorForm>)).toThrow();
  });
});
