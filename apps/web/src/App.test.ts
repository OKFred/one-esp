import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { nextTick, ref } from 'vue';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createBridgeState, DeviceCommandSchema } from '@one-esp/protocol';
import type { DebugData } from './types';
import App from './App.vue';
import DeviceSettings from './components/DeviceSettings.vue';

vi.mock('./composables/useDevice', () => ({ useDevice: () => session }));
vi.mock('ant-design-vue', async () => {
  const { defineComponent, h } = await import('vue');
  const container = (name: string) =>
    defineComponent({
      name,
      inheritAttrs: false,
      setup:
        (_, { attrs, slots }) =>
        () =>
          h('div', attrs, slots.default?.()),
    });
  return {
    ConfigProvider: container('ConfigProvider'),
    Modal: container('Modal'),
    Tabs: container('Tabs'),
    TabPane: container('TabPane'),
    Input: container('Input'),
    InputPassword: container('InputPassword'),
    Select: defineComponent({
      name: 'Select',
      props: ['value', 'options'],
      emits: ['change', 'update:value'],
      setup:
        (_, { attrs }) =>
        () =>
          h('div', attrs),
    }),
    InputNumber: defineComponent({
      name: 'InputNumber',
      props: ['value', 'min', 'max', 'step'],
      emits: ['update:value'],
      setup:
        (props, { attrs }) =>
        () =>
          h('input', { ...attrs, value: props.value, min: props.min, max: props.max }),
    }),
    Switch: defineComponent({
      name: 'Switch',
      props: ['checked', 'disabled'],
      emits: ['change'],
      setup:
        (_, { attrs }) =>
        () =>
          h('button', attrs),
    }),
    Button: defineComponent({
      name: 'Button',
      props: ['disabled'],
      inheritAttrs: false,
      setup:
        (props, { attrs, slots }) =>
        () =>
          h('button', { ...attrs, disabled: props.disabled }, slots.default?.()),
    }),
    theme: { darkAlgorithm: [] },
  };
});

function makeSession() {
  return {
    state: ref({
      ...createBridgeState(),
      connection: { connected: true, mode: 'usb', port: 'COM3', error: null },
    }),
    ports: ref([]),
    channelConnected: ref(true),
    busy: ref(false),
    error: ref(''),
    notice: ref(''),
    pendingCount: ref(0),
    status: ref(undefined),
    debug: ref<DebugData>(),
    oled: ref(undefined),
    metrics: ref(undefined),
    deviceUptimeMs: ref(1000),
    pins: ref([]),
    deviceId: ref('test-device'),
    oledAge: ref(1000),
    screenStale: ref(false),
    screenRecoveryPending: ref(false),
    screenRecoveryError: ref(''),
    samplingPaused: ref(true),
    autoRenewEnabled: ref(true),
    autoRenewStatus: ref('idle'),
    autoRenewError: ref(''),
    lastRenewedAt: ref<number | null>(null),
    leaseRemainingMs: ref(0),
    stale: ref(false),
    refreshPorts: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
    replay: vi.fn(),
    command: vi.fn(),
    perform: vi.fn(async (_name: string, _args: Record<string, unknown>) => ({
      id: 'debug-command',
      ok: true,
    })),
  };
}
let session: ReturnType<typeof makeSession>;
let wrapper: VueWrapper;
const lite = {
  enabled: false,
  profile: 'lite',
  oledFps: 1,
  gpioHz: 1,
  expiresInMs: 0,
  droppedLogs: 0,
} satisfies DebugData;

beforeEach(() => {
  session = makeSession();
  wrapper = mount(App, {
    global: {
      stubs: {
        Icon: true,
        BoardView: true,
        OledScreen: true,
        ClockQuality: true,
        SystemMetrics: true,
      },
    },
  });
});
afterEach(() => wrapper.unmount());

it.each(['mqtt', 'sensor'])(
  'refreshes actual status after %s settings apply with debug paused',
  async (domain) => {
    expect(session.samplingPaused.value).toBe(true);
    wrapper.findComponent(DeviceSettings).vm.$emit('applied', domain);
    await nextTick();
    expect(session.perform).toHaveBeenCalledWith('snapshot.get');
  },
);

it('defaults to automatic renewal while allowing it to be disabled independently of collection', async () => {
  const autoRenew = wrapper
    .findAllComponents({ name: 'Switch' })
    .find((item) => item.attributes('aria-label') === '自动续期');
  expect(autoRenew?.props('checked')).toBe(true);
  expect(wrapper.text()).toContain('开启采集后自动续期');
  autoRenew!.vm.$emit('change', false);
  await nextTick();
  expect(session.autoRenewEnabled.value).toBe(false);
  expect(wrapper.text()).toContain('自动续期已关闭');
  expect(session.perform).not.toHaveBeenCalled();
});

it('flags a stalled screen independently of healthy resource telemetry', async () => {
  session.samplingPaused.value = false;
  session.debug.value = { ...lite, enabled: true };
  session.screenStale.value = true;
  session.screenRecoveryPending.value = true;
  await nextTick();
  expect(wrapper.find('.freshness.expired').text()).toBe('画面停更');
  expect(wrapper.find('[aria-label="屏幕恢复状态"]').text()).toContain('正在恢复画面');
  expect(wrapper.find('oled-screen-stub').attributes('stale')).toBe('true');
  session.screenStale.value = false;
  session.screenRecoveryPending.value = false;
  await nextTick();
  expect(wrapper.find('[aria-label="屏幕恢复状态"]').exists()).toBe(false);
});

function numberControl(id: string) {
  const control = wrapper
    .findAllComponents({ name: 'InputNumber' })
    .find((item) => item.attributes('id') === id);
  if (!control) throw new Error('Missing numeric control: ' + id);
  return control;
}

it('restores reported LITE rates and starts sampling from the nearby display action', async () => {
  session.debug.value = { ...lite };
  await nextTick();
  expect(numberControl('oled-fps').props('value')).toBe(1);
  expect(numberControl('gpio-hz').props('value')).toBe(1);
  expect(numberControl('oled-fps').props('max')).toBe(1);
  expect(numberControl('gpio-hz').props('max')).toBe(1);
  const start = wrapper.findAll('button').find((button) => button.text() === '开启实时采集');
  expect(start).toBeDefined();
  await start!.trigger('click');
  await flushPromises();
  expect(session.perform).toHaveBeenCalledWith('debug.set', {
    enabled: true,
    profile: 'lite',
    oledFps: 1,
    gpioHz: 1,
    ttlSeconds: 600,
  });
  const args = session.perform.mock.calls[0]?.[1];
  expect(
    DeviceCommandSchema.safeParse({ v: 1, id: 'test', command: 'debug.set', args }).success,
  ).toBe(true);
});

it('preserves a user-selected profile across unrelated debug heartbeats and normalizes cleared fields', async () => {
  session.debug.value = { ...lite };
  await nextTick();
  const profileSelect = wrapper
    .findAllComponents({ name: 'Select' })
    .find((item) => item.attributes('id') === 'profile-select');
  profileSelect!.vm.$emit('change', 'full');
  await nextTick();
  expect(numberControl('oled-fps').props('value')).toBe(2);
  expect(numberControl('gpio-hz').props('max')).toBe(10);
  session.debug.value = { ...lite, expiresInMs: 10000 };
  await nextTick();
  expect(numberControl('oled-fps').props('value')).toBe(2);
  numberControl('oled-fps').vm.$emit('update:value', null);
  numberControl('gpio-hz').vm.$emit('update:value', NaN);
  numberControl('session-ttl').vm.$emit('update:value', null);
  await nextTick();
  const start = wrapper.findAll('button').find((button) => button.text() === '开启实时采集');
  await start!.trigger('click');
  await flushPromises();
  expect(session.perform).toHaveBeenLastCalledWith('debug.set', {
    enabled: true,
    profile: 'full',
    oledFps: 2,
    gpioHz: 10,
    ttlSeconds: 600,
  });
});
