import { describe, expect, it } from 'vitest';
import {
  DeviceCommandSchema,
  DeviceEventSchema,
  WIRE_PREFIX,
  applyDeviceEvent,
  createBridgeState,
  decodeOledPixels,
  encodeCommand,
  parseDeviceLine,
} from './index.js';

const event = {
  v: 1 as const,
  deviceId: 'test',
  bootId: 'boot-1',
  seq: 0,
  ts: 0,
  type: 'log' as const,
  data: { level: 'info', message: 'ready' },
};

describe('portable device protocol', () => {
  it('does not interpret ordinary boot logs as protocol', () => {
    expect(parseDeviceLine('ets Jul 29 2019')).toBeNull();
    expect(parseDeviceLine(WIRE_PREFIX + JSON.stringify(event))).toEqual(event);
    expect(() => parseDeviceLine(WIRE_PREFIX + '{broken')).toThrow();
  });
  it('validates mutating commands and their target boot', () => {
    expect(
      DeviceCommandSchema.safeParse({ v: 1, id: 'one', command: 'device.restart', args: {} })
        .success,
    ).toBe(false);
    const command = {
      v: 1 as const,
      id: 'one',
      command: 'debug.set' as const,
      args: { enabled: true },
    };
    expect(encodeCommand(command)).toBe(WIRE_PREFIX + JSON.stringify(command) + '\n');
    expect(
      DeviceCommandSchema.safeParse({ ...command, args: { enabled: true, ttlSeconds: 99999 } })
        .success,
    ).toBe(false);
  });
  it('rejects invalid OLED frame lengths and correctly decodes MONO_VLSB', () => {
    const data = new Uint8Array(1024);
    data[0] = 1;
    data[128] = 2;
    const frame = {
      width: 128 as const,
      height: 64 as const,
      format: 'MONO_VLSB' as const,
      available: true,
      text: '',
      buffer: btoa(String.fromCharCode(...data)),
    };
    const pixels = decodeOledPixels(frame);
    expect(pixels[0]).toBe(1);
    expect(pixels[1]).toBe(0);
    expect(pixels[128 * 9]).toBe(1);
    expect(() => decodeOledPixels({ ...frame, buffer: 'AA==' })).toThrow();
  });
  it('bounds logs and redacts secrets', () => {
    let state = createBridgeState();
    for (let n = 0; n < 250; n++)
      state = applyDeviceEvent(state, {
        ...event,
        seq: n,
        data: { level: 'info', message: 'password=private token=secret' },
      });
    expect(state.logs).toHaveLength(200);
    expect(state.logs.at(-1)?.data.message).toBe('password=[redacted] token=[redacted]');
  });
  it('clears old snapshots on a new device boot', () => {
    let state = createBridgeState();
    state = applyDeviceEvent(state, {
      ...event,
      type: 'hello',
      data: { firmware: 'test', board: 'test', capabilities: [] },
    });
    state = applyDeviceEvent(state, event);
    state = applyDeviceEvent(state, {
      ...event,
      bootId: 'boot-2',
      type: 'hello',
      data: { firmware: 'test', board: 'test', capabilities: [] },
    });
    expect(state.logs).toHaveLength(0);
    expect(state.hello?.bootId).toBe('boot-2');
    expect(
      DeviceEventSchema.safeParse({ ...event, type: 'pins', data: { pins: [{ gpio: 45 }] } })
        .success,
    ).toBe(false);
  });
  it('merges GPIO changes without removing unchanged pin metadata', () => {
    const first = {
      gpio: 36,
      label: 'SVP',
      mode: 'input' as const,
      owner: null,
      value: 0 as const,
      source: 'sampled' as const,
      writable: false,
    };
    const second = { ...first, gpio: 39, label: 'SVN' };
    let state = applyDeviceEvent(createBridgeState(), {
      ...event,
      type: 'pins',
      data: { pins: [first, second] },
    });
    state = applyDeviceEvent(state, {
      ...event,
      type: 'pins',
      data: { pins: [{ ...second, value: 1 }], partial: true },
    });
    expect(state.pins?.data.pins).toEqual([first, { ...second, value: 1 }]);
  });
});
