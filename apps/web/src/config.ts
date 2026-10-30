export interface DeviceConfigForm {
  SSID: string;
  PASSWORD: string;
  NTP_SERVER: string;
  TIMEZONE_HOURS: number;
  NETWORK_TEST_HOST: string;
  NETWORK_TEST_PORT: number;
  POLL_INTERVAL_MS: number;
}

export function createConfigForm(): DeviceConfigForm {
  return {
    SSID: '',
    PASSWORD: '',
    NTP_SERVER: 'ntp.aliyun.com',
    TIMEZONE_HOURS: 8,
    NETWORK_TEST_HOST: 'baidu.com',
    NETWORK_TEST_PORT: 443,
    POLL_INTERVAL_MS: 500,
  };
}

const numericFields = {
  TIMEZONE_HOURS: { label: '时区偏移', min: -12, max: 14, integer: false },
  NETWORK_TEST_PORT: { label: '网络测试端口', min: 1, max: 65535, integer: true },
  POLL_INTERVAL_MS: { label: '业务轮询间隔', min: 100, max: 60000, integer: true },
} as const;

function numericValue(key: keyof typeof numericFields, value: unknown): number {
  const rule = numericFields[key];
  if (
    (typeof value !== 'string' && typeof value !== 'number') ||
    (typeof value === 'string' && !value.trim())
  )
    throw new Error(`${rule.label}必须填写有效数值`);
  const result = Number(value);
  if (!Number.isFinite(result) || (rule.integer && !Number.isInteger(result)))
    throw new Error(`${rule.label}必须填写有效${rule.integer ? '整数' : '数值'}`);
  if (result < rule.min || result > rule.max)
    throw new Error(`${rule.label}范围为 ${rule.min}–${rule.max}`);
  return result;
}

/** Firmware .env values are strings; keep the editable model and wire numbers typed. */
export function readConfigValues(values: Record<string, unknown>): DeviceConfigForm {
  const form = createConfigForm();
  for (const key of ['SSID', 'NTP_SERVER', 'NETWORK_TEST_HOST'] as const) {
    if (values[key] !== undefined) {
      if (typeof values[key] !== 'string') throw new Error('设备返回的文本配置格式无效');
      form[key] = values[key];
    }
  }
  for (const key of Object.keys(numericFields) as Array<keyof typeof numericFields>) {
    if (values[key] !== undefined) form[key] = numericValue(key, values[key]);
  }
  // Passwords are write-only, even if an older firmware accidentally returns one.
  return form;
}

export function configValuesForSave(form: DeviceConfigForm) {
  const { PASSWORD: _ignoredPassword, ...values } = readConfigValues({ ...form });
  if (!values.SSID.trim()) throw new Error('Wi-Fi 名称不能为空');
  if (!values.NTP_SERVER.trim() || !values.NETWORK_TEST_HOST.trim())
    throw new Error('服务器地址不能为空');
  return form.PASSWORD ? { ...values, PASSWORD: form.PASSWORD } : values;
}
