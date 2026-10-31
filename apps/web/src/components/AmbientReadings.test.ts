import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import type { EnvironmentData } from '@one-esp/protocol';
import AmbientReadings from './AmbientReadings.vue';

const environment: EnvironmentData = {
  enabled: true,
  phase: 'idle',
  samples: 1,
  failures: 0,
  lastError: null,
  readAtMs: 1000,
  ageMs: 30000,
  stale: false,
  intervalMs: 60000,
  nextReadInMs: 30000,
  temperatureC: 28.8,
  relativeHumidityPercent: 48.4,
};
const props = {
  environment,
  connected: true,
  telemetryActive: true,
  deviceStale: false,
  uptimeMs: 31000,
  statusUptimeMs: 31000,
};

it('shows measured ambient readings, units, cadence and their independent collection', () => {
  const wrapper = mount(AmbientReadings, { props });
  expect(wrapper.text()).toContain('28.8 °C');
  expect(wrapper.text()).toContain('48.4 %RH');
  expect(wrapper.text()).toContain('每 60 秒读取 · 30 秒前读取');
  expect(wrapper.text()).toContain('环境温度与下方芯片内部温度分别显示');
  expect(wrapper.text()).toContain('设备采样和 MQTT 上报独立于网页调试开关');
  expect(wrapper.find('.ambient-freshness').text()).toBe('定时采集中');
});

it('supports old or disabled firmware without fabricating zero readings', () => {
  for (const missing of [
    undefined,
    {
      ...environment,
      enabled: false,
      temperatureC: null,
      relativeHumidityPercent: null,
      readAtMs: null,
      ageMs: null,
    },
  ]) {
    const wrapper = mount(AmbientReadings, { props: { ...props, environment: missing } });
    expect(wrapper.find('.ambient-freshness').text()).toBe('未启用');
    expect(wrapper.findAll('strong').every((value) => value.text().startsWith('—'))).toBe(true);
    expect(wrapper.text()).not.toContain('0.0');
    expect(wrapper.text()).not.toContain('NaN');
  }
});

it('labels cached data after disconnect and never calls it live collection', () => {
  const wrapper = mount(AmbientReadings, { props: { ...props, connected: false } });
  expect(wrapper.find('.ambient-freshness').text()).toBe('设备未连接 · 最后读数');
  expect(wrapper.text()).toContain('28.8 °C');
  expect(wrapper.text()).not.toContain('定时采集中');
});

it('ages the last readings after USB diagnostics stop without claiming device sampling stopped', async () => {
  const wrapper = mount(AmbientReadings, {
    props: { ...props, telemetryActive: false },
  });
  expect(wrapper.find('.ambient-freshness').text()).toBe('网页更新已暂停 · 最后读数');
  await wrapper.setProps({ uptimeMs: 91000 });
  expect(wrapper.text()).toContain('90 秒前读取');
  expect(wrapper.text()).toContain('28.8 °C');
  expect(wrapper.text()).toContain('设备采样和 MQTT 上报独立于网页调试开关');
});

it('other telemetry cannot make an old environment status appear current', async () => {
  const wrapper = mount(AmbientReadings, { props });
  await wrapper.setProps({ uptimeMs: 46000 });
  expect(wrapper.find('.ambient-freshness').text()).toBe('遥测更新中断 · 最后读数');
  expect(wrapper.text()).toContain('45 秒前读取');
  expect(wrapper.find('.ambient-freshness').classes()).toContain('tone-warning');
  await wrapper.setProps({ uptimeMs: 47000, statusUptimeMs: 47000, deviceStale: true });
  expect(wrapper.find('.ambient-freshness').text()).toContain('遥测更新中断');
});

it('marks old sensor data stale even while new status frames continue', () => {
  const wrapper = mount(AmbientReadings, {
    props: { ...props, uptimeMs: 125000, statusUptimeMs: 125000 },
  });
  expect(wrapper.find('.ambient-freshness').text()).toBe('读数已过期 · 等待新数据');
  expect(wrapper.text()).toContain('124 秒前读取');
  expect(wrapper.text()).toContain('28.8 °C');
});

it('distinguishes failed and pending reads without replacing them with zero', () => {
  const pending = {
    ...environment,
    samples: 0,
    readAtMs: null,
    ageMs: null,
    temperatureC: null,
    relativeHumidityPercent: null,
    stale: true,
  };
  const wrapper = mount(AmbientReadings, { props: { ...props, environment: pending } });
  expect(wrapper.find('.ambient-freshness').text()).toBe('等待首次读取');
  const failed = mount(AmbientReadings, {
    props: { ...props, environment: { ...pending, failures: 1, lastError: 'read_timeout' } },
  });
  expect(failed.find('.ambient-freshness').text()).toBe('最近读取失败 · 等待重试');
  expect(failed.findAll('strong').every((value) => value.text().startsWith('—'))).toBe(true);
});

it('preserves valid zero humidity and negative temperatures', () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      environment: { ...environment, temperatureC: -4.2, relativeHumidityPercent: 0 },
    },
  });
  expect(wrapper.text()).toContain('-4.2 °C');
  expect(wrapper.text()).toContain('0.0 %RH');
});

it('keeps local sampling health separate from disconnected MQTT delivery', async () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      mqtt: {
        enabled: true,
        samples: 7,
        acknowledged: 6,
        pending: true,
        transport: { connected: false, phase: 'backoff' },
      },
    },
  });
  expect(wrapper.find('.ambient-freshness').text()).toBe('定时采集中');
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 重连中 · 待发送');
  expect(wrapper.find('.ambient-delivery').classes()).toContain('tone-warning');
  await wrapper.setProps({
    mqtt: { enabled: true, acknowledged: 7, pending: false, transport: { connected: true } },
  });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 已连接 · 本次启动已确认 7 条');
  expect(wrapper.find('.ambient-delivery').classes()).not.toContain('tone-warning');
});

it('labels paused, disconnected or stale MQTT evidence as the last known status', async () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      mqtt: { enabled: true, acknowledged: 6, transport: { connected: true } },
      telemetryActive: false,
    },
  });
  expect(wrapper.find('.ambient-delivery').text()).toBe(
    '上次MQTT状态：MQTT 已连接 · 本次启动已确认 6 条',
  );
  await wrapper.setProps({ telemetryActive: true, connected: false });
  expect(wrapper.find('.ambient-delivery').text()).toContain('上次MQTT状态');
  await wrapper.setProps({ connected: true, deviceStale: true });
  expect(wrapper.find('.ambient-delivery').text()).toContain('上次MQTT状态');
  await wrapper.setProps({ deviceStale: false, uptimeMs: 42000 });
  expect(wrapper.find('.ambient-delivery').text()).toContain('上次MQTT状态');
});

it('does not fabricate confirmation counts for old firmware or incomplete MQTT fields', async () => {
  const wrapper = mount(AmbientReadings, { props });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT状态未提供');
  await wrapper.setProps({ mqtt: { enabled: true, transport: { connected: true } } });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 已连接 · 确认数未提供');
  expect(wrapper.find('.ambient-delivery').text()).not.toContain('已确认 0 条');
  await wrapper.setProps({
    mqtt: { enabled: true, acknowledged: 0, transport: { connected: true } },
  });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 已连接 · 本次启动已确认 0 条');
});

it.each([0, 999, 1000, 1001, 999999, 1000000, Number.MAX_SAFE_INTEGER])(
  'shows the complete daily count %s instead of clipping it or using the boot total',
  (count) => {
    const wrapper = mount(AmbientReadings, {
      props: {
        ...props,
        mqtt: {
          enabled: true,
          dailyAcknowledged: count,
          acknowledged: 123456,
          transport: { connected: true },
        },
      },
    });
    expect(wrapper.find('.ambient-delivery').text()).toBe(`MQTT 已连接 · 今日已确认 ${count} 条`);
  },
);

it.each([null, undefined, NaN, Infinity, -Infinity, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])(
  'does not substitute the boot total for an unavailable or invalid daily count %s',
  (count) => {
    const wrapper = mount(AmbientReadings, {
      props: {
        ...props,
        mqtt: {
          enabled: true,
          dailyAcknowledged: count,
          acknowledged: 42,
          transport: { connected: true },
        },
      },
    });
    expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 已连接 · 今日确认数未提供');
    expect(wrapper.find('.ambient-delivery').text()).not.toContain('42');
  },
);

it.each([1000, Number.MAX_SAFE_INTEGER])(
  'labels the complete old-firmware count %s as this boot',
  (count) => {
    const wrapper = mount(AmbientReadings, {
      props: {
        ...props,
        mqtt: { enabled: true, acknowledged: count, transport: { connected: true } },
      },
    });
    expect(wrapper.find('.ambient-delivery').text()).toBe(
      `MQTT 已连接 · 本次启动已确认 ${count} 条`,
    );
  },
);

it('keeps the last reported daily count while paused, then accepts the device midnight reset', async () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      telemetryActive: false,
      mqtt: {
        enabled: true,
        dailyAcknowledged: 1000,
        acknowledged: 2000,
        transport: { connected: true },
      },
    },
  });
  expect(wrapper.find('.ambient-delivery').text()).toBe(
    '上次MQTT状态：MQTT 已连接 · 今日已确认 1000 条',
  );
  await wrapper.setProps({ uptimeMs: 86431000 });
  expect(wrapper.find('.ambient-delivery').text()).toContain('今日已确认 1000 条');
  await wrapper.setProps({
    telemetryActive: true,
    statusUptimeMs: 86431000,
    mqtt: {
      enabled: true,
      dailyAcknowledged: 0,
      acknowledged: 2000,
      pending: true,
      transport: { connected: true },
    },
  });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 已连接 · 今日已确认 0 条 · 等待确认');
});

it('distinguishes disabled MQTT, waiting on prerequisites and invalid configuration', async () => {
  const wrapper = mount(AmbientReadings, { props: { ...props, mqtt: { state: 'disabled' } } });
  expect(wrapper.find('.ambient-delivery').text()).toBe('未开启MQTT');
  for (const state of ['waiting_network', 'waiting_clock']) {
    await wrapper.setProps({ mqtt: { state } });
    expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 等待网络或时钟');
  }
  await wrapper.setProps({ mqtt: { state: 'configuration_error' } });
  expect(wrapper.find('.ambient-delivery').text()).toBe('MQTT 配置异常');
});

it('labels a CRC-verified RTU reading and exposes its bounded raw frame separately', () => {
  const raw = '010404013102222ace';
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      environment: {
        ...environment,
        protocol: 'sht20_rtu',
        integrity: 'crc_verified',
        crcFailures: 2,
        lastFrameHex: raw,
      },
    },
  });
  const integrity = wrapper.find('[aria-label="采集协议与校验"]');
  expect(integrity.text()).toContain('SHT20 Modbus RTU');
  expect(integrity.text()).toContain('CRC16 校验通过');
  expect(integrity.text()).toContain('CRC 错误 2');
  expect(wrapper.find('details summary').text()).toContain('最近原始报文');
  expect(wrapper.find('code.raw-frame').text()).toBe(raw);
  expect(wrapper.text()).toContain('48.4 %RH');
});

it('never presents ASCII format checks or missing integrity as successful CRC', async () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      environment: {
        ...environment,
        protocol: 'ascii_read',
        integrity: 'format_only',
        crcFailures: 0,
      },
    },
  });
  expect(wrapper.find('[aria-label="采集协议与校验"]').text()).toContain('协议未提供 CRC');
  expect(wrapper.text()).not.toContain('CRC16 校验通过');
  expect(wrapper.find('details').exists()).toBe(false);
  await wrapper.setProps({
    environment: { ...environment, protocol: 'sht20_rtu', integrity: 'none' },
  });
  expect(wrapper.find('[aria-label="采集协议与校验"]').text()).toContain('校验结果待确认');
  expect(wrapper.text()).not.toContain('CRC16 校验通过');
  await wrapper.setProps({ environment });
  expect(wrapper.find('[aria-label="采集协议与校验"]').text()).toContain('协议待设备报告');
  expect(wrapper.text()).not.toContain('CRC16 校验通过');
});

it('keeps a CRC failure distinct from link freshness and does not invent a usable value', () => {
  const wrapper = mount(AmbientReadings, {
    props: {
      ...props,
      environment: {
        ...environment,
        protocol: 'sht20_rtu',
        integrity: 'none',
        crcFailures: 1,
        lastError: 'crc_mismatch',
        stale: true,
        temperatureC: null,
        relativeHumidityPercent: null,
        lastFrameHex: '010404013002222ace',
      },
    },
  });
  expect(wrapper.find('.ambient-freshness').text()).toBe('最近读取失败 · 等待重试');
  expect(wrapper.find('[aria-label="采集协议与校验"]').text()).toContain('CRC 错误 1');
  expect(wrapper.text()).not.toContain('CRC16 校验通过');
  expect(wrapper.findAll('strong').every((item) => item.text().startsWith('—'))).toBe(true);
  expect(wrapper.find('code').text()).toBe('010404013002222ace');
});

it('does not claim verified CRC for an ASCII or unidentified protocol with inconsistent fields', () => {
  for (const protocol of ['ascii_read', undefined] as const) {
    const wrapper = mount(AmbientReadings, {
      props: {
        ...props,
        environment: { ...environment, protocol, integrity: 'crc_verified' },
      },
    });
    expect(wrapper.find('[aria-label="采集协议与校验"]').text()).not.toContain('CRC16 校验通过');
  }
});
