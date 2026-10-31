import { MqttConfigValuesSchema, SensorConfigValuesSchema } from '@one-esp/protocol';

export interface MqttForm {
  enabled: boolean;
  host: string;
  port: number;
  server_hostname: string;
  username: string;
  password: string;
  client_id: string;
  site_id: string;
  gateway_id: string;
  device_id: string;
  interval_seconds: number;
  keepalive: number;
  commands_enabled: boolean;
  broker_ip: string;
}
export interface SensorForm {
  enabled: boolean;
  protocol: 'ascii_read' | 'sht20_rtu';
  interval_seconds: number;
  baudrate: 9600 | 14400 | 19200;
  slave_address: number;
  timeout_ms: number;
}
export function createMqttForm(): MqttForm {
  return {
    enabled: false,
    host: '',
    port: 8883,
    server_hostname: '',
    username: '',
    password: '',
    client_id: '',
    site_id: '',
    gateway_id: '',
    device_id: '',
    interval_seconds: 60,
    keepalive: 60,
    commands_enabled: false,
    broker_ip: '',
  };
}
export function createSensorForm(): SensorForm {
  return {
    enabled: false,
    protocol: 'ascii_read',
    interval_seconds: 60,
    baudrate: 9600,
    slave_address: 1,
    timeout_ms: 2000,
  };
}
function values(result: Record<string, unknown>) {
  return result.values && typeof result.values === 'object'
    ? (result.values as Record<string, unknown>)
    : {};
}
export function readMqttForm(result: Record<string, unknown>): MqttForm {
  const v = MqttConfigValuesSchema.parse(values(result));
  delete v.ca_file;
  const form = { ...createMqttForm(), ...v, password: '' };
  // Blank SNI means follow the broker host; do not retain an old host silently.
  form.server_hostname = v.server_hostname === v.host ? '' : (v.server_hostname ?? '');
  return form;
}
export function readSensorForm(result: Record<string, unknown>): SensorForm {
  return { ...createSensorForm(), ...SensorConfigValuesSchema.parse(values(result)) };
}
export function mqttValuesForSave(form: MqttForm, passwordConfigured: boolean) {
  const { password, broker_ip, ...other } = form;
  const v = MqttConfigValuesSchema.parse({
    ...other,
    host: form.host.trim(),
    server_hostname: form.server_hostname.trim() || form.host.trim(),
    ...(password ? { password } : {}),
    broker_ip: broker_ip.trim(),
  });
  if (v.enabled) {
    if (!v.host || !v.username || !v.client_id || !v.site_id || !v.gateway_id || !v.device_id)
      throw new Error('请填写服务器、账号、客户端 ID 和设备身份字段');
    if (!password && !passwordConfigured) throw new Error('首次启用 MQTT 需要填写密码');
  }
  return v;
}
export function sensorValuesForSave(form: SensorForm) {
  return SensorConfigValuesSchema.parse(form);
}
