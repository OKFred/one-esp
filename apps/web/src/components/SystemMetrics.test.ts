import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import type { MetricsData } from '@one-esp/protocol';
import SystemMetrics from './SystemMetrics.vue';

const metrics: MetricsData = {
  sampledAtMs: 60000,
  intervalMs: 5000,
  cpuHz: 160_000_000,
  memory: { freeBytes: 81920, allocatedBytes: 65536, totalBytes: 147456 },
  idfHeap: { totalBytes: 200000, freeBytes: 100000, largestBlockBytes: 50000 },
  storage: { sampledAtMs: 0, totalBytes: 2097152, freeBytes: 1572864 },
  flashBytes: 4194304,
  temperature: { sampledAtMs: 30000, celsius: 48.9 },
  rssiDbm: -50,
  sampleDurationUs: 400,
};

it('distinguishes measured frequency, overlapping heaps, storage and chip temperature', () => {
  const wrapper = mount(SystemMetrics, {
    props: { metrics, active: true, connected: true, uptimeMs: 60000, tcpMs: 42, tcpOk: true },
  });
  expect(wrapper.text()).toContain('160 MHz');
  expect(wrapper.text()).toContain('CPU 占用率：固件未提供');
  expect(wrapper.text()).toContain('80.0 KiB');
  expect(wrapper.text()).toContain('可用预算 144.0 KiB');
  expect(wrapper.text()).toContain('Python 与 ESP-IDF 堆有重叠，不相加');
  expect(wrapper.text()).toContain('物理 Flash 4.00 MiB');
  expect(wrapper.text()).toContain('48.9 °C');
  expect(wrapper.text()).toContain('非环境温度');
  expect(wrapper.text()).toContain('TCP 连接：42 ms');
  expect(wrapper.text()).toContain('Wi-Fi 吞吐率：固件未提供');
  expect(wrapper.text()).not.toContain('Mbps');
});

it('ages cached groups independently and labels paused collection', async () => {
  const wrapper = mount(SystemMetrics, {
    props: { metrics, active: true, connected: true, uptimeMs: 60000 },
  });
  expect(wrapper.text()).toContain('0 秒前采样');
  expect(wrapper.text()).toContain('30 秒前采样');
  expect(wrapper.text()).toContain('60 秒前采样');
  await wrapper.setProps({ active: false, uptimeMs: 75000 });
  expect(wrapper.text()).toContain('采集已暂停');
  expect(wrapper.text()).toContain('15 秒前采样');
  expect(wrapper.text()).toContain('75 秒前采样');
});

it('renders unavailable hardware APIs without inventing zero measurements', () => {
  const missing: MetricsData = {
    ...metrics,
    cpuHz: null,
    memory: { freeBytes: null, allocatedBytes: null, totalBytes: null },
    idfHeap: null,
    storage: null,
    flashBytes: null,
    temperature: null,
    rssiDbm: null,
    sampleDurationUs: null,
  };
  const wrapper = mount(SystemMetrics, {
    props: { metrics: missing, active: true, connected: true },
  });
  expect(wrapper.findAll('strong').every((item) => item.text() === '未提供')).toBe(true);
  expect(wrapper.text()).not.toContain('NaN');
  expect(wrapper.text()).not.toContain('0 °C');
  expect(wrapper.text()).not.toContain('0 MHz');
});

it('supports firmware snapshots without metrics', () => {
  const wrapper = mount(SystemMetrics, { props: { active: false, connected: true } });
  expect(wrapper.text()).toContain('尚无资源指标');
  expect(wrapper.text()).toContain('采集已暂停');
  expect(wrapper.find('.metrics-grid').exists()).toBe(false);
});
