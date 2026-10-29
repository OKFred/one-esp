import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import ClockQuality from './ClockQuality.vue';

const clock = {
  synced: true,
  targetMs: 500,
  estimatedErrorMs: 42.1,
  withinTarget: true,
  lastSyncAgeMs: 6000,
  intervalMs: 60000,
  nextInMs: 54000,
};

it('labels precision as an estimate and shows synchronization age', () => {
  const wrapper = mount(ClockQuality, { props: { clock } });
  expect(wrapper.text()).toContain('估计误差 ≤ 43 ms');
  expect(wrapper.text()).toContain('目标 500 ms');
  expect(wrapper.text()).toContain('6 秒前校准');
  expect(wrapper.text()).not.toContain('精度待恢复');
});

it('shows loss of precision instead of presenting the target as achieved', () => {
  const wrapper = mount(ClockQuality, {
    props: { clock: { ...clock, estimatedErrorMs: 650, withinTarget: false } },
  });
  expect(wrapper.text()).toContain('精度待恢复');
  expect(wrapper.text()).toContain('650 ms');
});

it('reports an unsynchronized clock without a numerical accuracy claim', () => {
  const wrapper = mount(ClockQuality, {
    props: {
      clock: {
        ...clock,
        synced: false,
        estimatedErrorMs: null,
        withinTarget: false,
        lastSyncAgeMs: null,
      },
    },
  });
  expect(wrapper.text()).toContain('等待网络校时');
  expect(wrapper.text()).not.toContain('估计误差');
});
