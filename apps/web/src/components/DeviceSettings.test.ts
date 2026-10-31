import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import type { DeviceCommand } from '@one-esp/protocol';
import type { AckData } from '../types';
import DeviceSettings from './DeviceSettings.vue';

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
  const input = (name: string, type: string) =>
    defineComponent({
      name,
      props: ['value', 'disabled'],
      emits: ['update:value'],
      setup:
        (props, { attrs, emit }) =>
        () =>
          h('input', {
            ...attrs,
            type,
            value: props.value,
            disabled: props.disabled,
            onInput: (event: Event) => {
              const value = (event.target as HTMLInputElement).value;
              emit(
                'update:value',
                type === 'number' ? (value === '' ? null : Number(value)) : value,
              );
            },
          }),
    });
  return {
    Modal: defineComponent({
      name: 'Modal',
      props: ['open', 'closable', 'maskClosable'],
      emits: ['cancel'],
      setup:
        (_, { slots }) =>
        () =>
          h('section', slots.default?.()),
    }),
    Button: defineComponent({
      name: 'Button',
      props: ['disabled', 'loading', 'htmlType'],
      setup:
        (props, { attrs, slots }) =>
        () =>
          h(
            'button',
            {
              ...attrs,
              type: props.htmlType || 'button',
              disabled: props.disabled,
              'data-loading': String(Boolean(props.loading)),
            },
            slots.default?.(),
          ),
    }),
    Input: input('Input', 'text'),
    InputPassword: input('InputPassword', 'password'),
    InputNumber: input('InputNumber', 'number'),
    Select: defineComponent({
      name: 'Select',
      props: ['value', 'options', 'disabled'],
      emits: ['update:value'],
      setup:
        (props, { emit }) =>
        () =>
          h(
            'select',
            {
              value: props.value,
              disabled: props.disabled,
              onChange: (event: Event) => {
                const value = (event.target as HTMLSelectElement).value;
                emit(
                  'update:value',
                  typeof props.options[0]?.value === 'number' ? Number(value) : value,
                );
              },
            },
            props.options.map((option: { value: string | number; label: string }) =>
              h('option', { value: option.value }, option.label),
            ),
          ),
    }),
    Switch: defineComponent({
      name: 'Switch',
      props: ['checked', 'disabled'],
      emits: ['update:checked'],
      setup:
        (props, { emit }) =>
        () =>
          h('input', {
            type: 'checkbox',
            checked: props.checked,
            disabled: props.disabled,
            onChange: (event: Event) =>
              emit('update:checked', (event.target as HTMLInputElement).checked),
          }),
    }),
    Tabs: container('Tabs'),
    TabPane: container('TabPane'),
  };
});

type Command = (name: DeviceCommand['command'], args?: Record<string, unknown>) => Promise<AckData>;
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
const sensorValues = {
  enabled: true,
  protocol: 'sht20_rtu',
  interval_seconds: 60,
  baudrate: 9600,
  slave_address: 1,
  timeout_ms: 2000,
};
const mqttResult = (host = 'broker.example.com', revision = 7) => ({
  revision,
  values: { ...mqttValues, host, server_hostname: host },
  secrets: { password: true },
  ca: { sha256: 'a'.repeat(64), bytes: 914 },
});
const sensorResult = (revision = 3) => ({ revision, values: { ...sensorValues } });
const ack = (result: Record<string, unknown>): AckData => ({ id: 'settings', ok: true, result });
const wrappers: VueWrapper[] = [];
afterEach(() => wrappers.splice(0).forEach((wrapper) => wrapper.unmount()));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function commandWithReads() {
  return vi.fn<Command>(async (name) => {
    if (name === 'mqtt.config.get') return ack(mqttResult());
    if (name === 'sensor.config.get') return ack(sensorResult());
    throw new Error('Unexpected settings write');
  });
}
async function open(command: Command) {
  const wrapper = mount(DeviceSettings, {
    props: { open: false, connected: true, identity: 'device-a/boot-a', command },
  });
  wrappers.push(wrapper);
  await wrapper.setProps({ open: true });
  return wrapper;
}
function field(wrapper: VueWrapper, label: string) {
  const parent = wrapper.findAll('label').find((item) => item.text().startsWith(label));
  if (!parent) throw new Error('Missing field: ' + label);
  return parent.find('input,select');
}
function saveButton(wrapper: VueWrapper, domain: 'mqtt' | 'sensor') {
  return wrapper.findAll('form')[domain === 'mqtt' ? 0 : 1]!.find('button[type="submit"]');
}
async function submit(wrapper: VueWrapper, domain: 'mqtt' | 'sensor') {
  await wrapper.findAll('form')[domain === 'mqtt' ? 0 : 1]!.trigger('submit');
}

it('loads both domains before allowing a save and exposes only secret availability', async () => {
  const mqtt = deferred<AckData>();
  const sensor = deferred<AckData>();
  const command = vi.fn<Command>((name) =>
    name === 'mqtt.config.get' ? mqtt.promise : sensor.promise,
  );
  const wrapper = await open(command);
  expect(command.mock.calls.map(([name]) => name)).toEqual([
    'mqtt.config.get',
    'sensor.config.get',
  ]);
  mqtt.resolve(
    ack({ ...mqttResult(), values: { ...mqttValues, password: 'must-never-read-back' } }),
  );
  await flushPromises();
  expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeDefined();
  sensor.resolve(ack(sensorResult()));
  await flushPromises();
  expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeUndefined();
  expect(field(wrapper, '密码').element).toHaveProperty('value', '');
  expect(wrapper.text()).toContain('已配置 · 留空保留');
  expect(wrapper.html()).not.toContain('must-never-read-back');
  expect(wrapper.text()).toContain('证书验证始终开启');
  expect(wrapper.text()).toContain('a'.repeat(64));
});

it('waits for the final save promise before reporting applied and clears the submitted password', async () => {
  const pending = deferred<AckData>();
  const command = commandWithReads();
  command.mockImplementation((name) => {
    if (name === 'mqtt.config.set') return pending.promise;
    return Promise.resolve(ack(name === 'mqtt.config.get' ? mqttResult() : sensorResult()));
  });
  const wrapper = await open(command);
  await flushPromises();
  await field(wrapper, '密码').setValue('new-test-secret');
  await submit(wrapper, 'mqtt');
  const set = command.mock.calls.find(([name]) => name === 'mqtt.config.set')!;
  expect(set[1]).toMatchObject({ revision: 7, values: { password: 'new-test-secret' } });
  expect(saveButton(wrapper, 'mqtt').attributes('data-loading')).toBe('true');
  expect(saveButton(wrapper, 'sensor').attributes('disabled')).toBeDefined();
  expect(wrapper.text()).not.toContain('MQTT 配置已应用');
  expect(wrapper.emitted('applied')).toBeUndefined();
  pending.resolve(ack(mqttResult('broker.example.com', 8)));
  await flushPromises();
  expect(wrapper.text()).toContain('MQTT 配置已应用');
  expect(wrapper.text()).toContain('配置版本 8');
  expect(field(wrapper, '密码').element).toHaveProperty('value', '');
  expect(wrapper.html()).not.toContain('new-test-secret');
  expect(wrapper.emitted('applied')).toEqual([['mqtt']]);
});

it('disables MQTT through the switch without erasing hydrated credentials', async () => {
  const command = commandWithReads();
  command.mockImplementation(async (name) => {
    if (name === 'mqtt.config.set')
      return ack({ ...mqttResult(), values: { ...mqttValues, enabled: false } });
    return ack(name === 'mqtt.config.get' ? mqttResult() : sensorResult());
  });
  const wrapper = await open(command);
  await flushPromises();
  await field(wrapper, '启用 MQTT 上报').setValue(false);
  await submit(wrapper, 'mqtt');
  await flushPromises();
  const values = command.mock.calls.find(([name]) => name === 'mqtt.config.set')?.[1]?.values;
  expect(values).toMatchObject({
    enabled: false,
    username: 'test-account',
    client_id: 'test-client',
  });
  expect(values).not.toHaveProperty('password');
  expect(field(wrapper, '账号').element).toHaveProperty('value', 'test-account');
  expect(wrapper.text()).toContain('已配置 · 留空保留');
});

it('rereads the applied domain when a final ACK does not include canonical values', async () => {
  let reads = 0;
  const command = vi.fn<Command>(async (name) => {
    if (name === 'mqtt.config.get') return ack(mqttResult());
    if (name === 'sensor.config.get') return ack(sensorResult(++reads === 1 ? 3 : 4));
    return ack({ stage: 'applied' });
  });
  const wrapper = await open(command);
  await flushPromises();
  await submit(wrapper, 'sensor');
  await flushPromises();
  expect(command.mock.calls.filter(([name]) => name === 'sensor.config.get')).toHaveLength(2);
  expect(wrapper.text()).toContain('采集配置已应用');
  expect(wrapper.text()).toContain('配置版本 4');
});

it('requires a fresh read after an unconfirmed save before allowing another write', async () => {
  const pending = deferred<AckData>();
  const command = commandWithReads();
  command.mockImplementation((name) => {
    if (name === 'mqtt.config.set') return pending.promise;
    return Promise.resolve(ack(name === 'mqtt.config.get' ? mqttResult() : sensorResult()));
  });
  const wrapper = await open(command);
  await flushPromises();
  await submit(wrapper, 'mqtt');
  pending.reject(new Error('保存结果未确认，请重新读取实际配置'));
  await flushPromises();
  expect(wrapper.find('[role="alert"]').text()).toContain('保存结果未确认');
  expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeDefined();
  expect(wrapper.text()).not.toContain('MQTT 配置已应用');
  const reload = wrapper.findAll('button').find((button) => button.text() === '重新读取配置')!;
  await reload.trigger('click');
  await flushPromises();
  expect(command.mock.calls.filter(([name]) => name === 'mqtt.config.get')).toHaveLength(2);
  expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeUndefined();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
});

it('clears the password when closed and ignores a late successful write', async () => {
  const pending = deferred<AckData>();
  const command = commandWithReads();
  command.mockImplementation((name) => {
    if (name === 'mqtt.config.set') return pending.promise;
    return Promise.resolve(ack(name === 'mqtt.config.get' ? mqttResult() : sensorResult()));
  });
  const wrapper = await open(command);
  await flushPromises();
  await field(wrapper, '密码').setValue('discard-on-close');
  await submit(wrapper, 'mqtt');
  await wrapper.setProps({ open: false });
  expect(field(wrapper, '密码').element).toHaveProperty('value', '');
  pending.resolve(ack(mqttResult('stale.example.com', 100)));
  await flushPromises();
  expect(wrapper.text()).not.toContain('MQTT 配置已应用');
  expect(field(wrapper, '服务器').element).toHaveProperty('value', 'broker.example.com');
  await wrapper.setProps({ open: true });
  await flushPromises();
  expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeUndefined();
  expect(wrapper.html()).not.toContain('discard-on-close');
});

it.each(['identity', 'connection'] as const)(
  'rejects late save results after the device %s changes and can save the new context',
  async (change) => {
    const pending = deferred<AckData>();
    let nextDevice = false;
    const command = vi.fn<Command>((name) => {
      if (name === 'mqtt.config.set') return pending.promise;
      return Promise.resolve(
        ack(
          name === 'mqtt.config.get'
            ? mqttResult(nextDevice ? 'next.example.com' : undefined)
            : sensorResult(),
        ),
      );
    });
    const wrapper = await open(command);
    await flushPromises();
    await field(wrapper, '密码').setValue('old-context-secret');
    await submit(wrapper, 'mqtt');
    nextDevice = true;
    await wrapper.setProps(
      change === 'identity' ? { identity: 'device-b/boot-b' } : { connected: false },
    );
    expect(field(wrapper, '密码').element).toHaveProperty('value', '');
    if (change === 'connection') await wrapper.setProps({ connected: true });
    await flushPromises();
    expect(field(wrapper, '服务器').element).toHaveProperty('value', 'next.example.com');
    pending.resolve(ack(mqttResult('stale.example.com', 99)));
    await flushPromises();
    expect(field(wrapper, '服务器').element).toHaveProperty('value', 'next.example.com');
    expect(wrapper.text()).not.toContain('MQTT 配置已应用');
    expect(saveButton(wrapper, 'mqtt').attributes('disabled')).toBeUndefined();
    expect(wrapper.html()).not.toContain('old-context-secret');
  },
);

it('ignores old read responses when the dialog reopens on a newer context', async () => {
  const oldMqtt = deferred<AckData>();
  const oldSensor = deferred<AckData>();
  let mqttReads = 0;
  let sensorReads = 0;
  const command = vi.fn<Command>((name) =>
    name === 'mqtt.config.get'
      ? ++mqttReads === 1
        ? oldMqtt.promise
        : Promise.resolve(ack(mqttResult('current.example.com')))
      : ++sensorReads === 1
        ? oldSensor.promise
        : Promise.resolve(ack(sensorResult(8))),
  );
  const wrapper = await open(command);
  await wrapper.setProps({ open: false });
  await wrapper.setProps({ open: true, identity: 'device-b/boot-b' });
  await flushPromises();
  oldMqtt.resolve(ack(mqttResult('old.example.com', 99)));
  oldSensor.resolve(ack(sensorResult(99)));
  await flushPromises();
  expect(field(wrapper, '服务器').element).toHaveProperty('value', 'current.example.com');
  expect(wrapper.text()).not.toContain('配置版本 99');
});

it('reports remote readiness only after granted SUBACK, not merely an enabled execution switch', async () => {
  const wrapper = await open(commandWithReads());
  await flushPromises();
  await wrapper.setProps({ remote: { configured: true, active: true, state: 'subscribing' } });
  expect(wrapper.find('[aria-label="MQTT 下行状态"]').text()).toContain('等待');
  await wrapper.setProps({ remote: { configured: true, active: true, state: 'denied' } });
  expect(wrapper.find('[aria-label="MQTT 下行状态"]').text()).toContain('拒绝');
  await wrapper.setProps({ remote: { configured: true, active: true, state: 'ready' } });
  expect(wrapper.find('[aria-label="MQTT 下行状态"]').text()).toContain('已订阅');
});

it('uses explicit RTU mode with mandatory CRC and has no control to bypass it', async () => {
  const command = commandWithReads();
  const wrapper = await open(command);
  await flushPromises();
  expect(wrapper.text()).toContain('强制 CRC16 校验');
  expect(field(wrapper, '从站地址').exists()).toBe(true);
  expect(
    wrapper
      .findAll('label')
      .filter(
        (label) => label.find('input[type="checkbox"]').exists() && /CRC/i.test(label.text()),
      ),
  ).toHaveLength(0);
  await field(wrapper, '通信协议').setValue('ascii_read');
  expect(wrapper.text()).toContain('ASCII READ 协议没有 CRC 校验字节');
  expect(wrapper.findAll('label').some((label) => label.text().startsWith('从站地址'))).toBe(false);
  await submit(wrapper, 'sensor');
  await flushPromises();
  const values = command.mock.calls.find(([name]) => name === 'sensor.config.set')?.[1]?.values;
  expect(values).not.toHaveProperty('crc');
});
