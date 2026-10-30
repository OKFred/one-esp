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
