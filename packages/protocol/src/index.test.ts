import { describe, expect, it } from 'vitest';
import {
  DeviceCommandSchema,
  DeviceEventSchema,
  EnvironmentSchema,
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

const metrics = {
  sampledAtMs: 0,
  intervalMs: 5000,
  cpuHz: 160_000_000,
  memory: { freeBytes: 81920, allocatedBytes: 65536, totalBytes: 147456 },
  idfHeap: { totalBytes: 200000, freeBytes: 100000, largestBlockBytes: 50000 },
  storage: { sampledAtMs: 0, totalBytes: 2000000, freeBytes: 1000000 },
  flashBytes: 4194304,
  temperature: { sampledAtMs: 0, celsius: 48.9 },
  rssiDbm: -50,
  sampleDurationUs: 400,
};

const environment = {
  enabled: true,
  phase: 'idle',
  samples: 1,
  failures: 0,
  lastError: null,
  readAtMs: 1000,
  ageMs: 100,
  stale: false,
  intervalMs: 60000,
  nextReadInMs: 59900,
  temperatureC: 28.8,
  relativeHumidityPercent: 48.4,
};

describe('portable device protocol', () => {
  it('accepts enabled environment status and remains compatible with older firmware', () => {
    const status = {
      ...event,
      type: 'status' as const,
      data: {
        uptimeMs: 1100,
        memFree: 50000,
        wifi: { connected: true, status: 1010 },
        sensorEnabled: true,
        environment,
      },
    };
    expect(DeviceEventSchema.safeParse(status).success).toBe(true);
    const { environment: _unused, ...legacy } = status.data;
    expect(
      DeviceEventSchema.safeParse({ ...status, data: { ...legacy, sensorEnabled: false } }).success,
    ).toBe(true);
    expect(parseDeviceLine(WIRE_PREFIX + JSON.stringify(status))?.data.environment).toEqual(
      environment,
    );
  });
  it('rejects unknown environment keys and unbounded, invalid or non-finite fields', () => {
    for (const fields of [
      { privateEndpoint: 'unit-only' },
      { temperatureC: -40.1 },
      { temperatureC: 125.1 },
      { relativeHumidityPercent: -0.1 },
      { relativeHumidityPercent: 100.1 },
      { temperatureC: NaN },
      { temperatureC: Infinity },
      { relativeHumidityPercent: '48.4' },
      { samples: 1.5 },
      { failures: -1 },
      { readAtMs: -1 },
      { ageMs: -1 },
      { intervalMs: 0 },
      { nextReadInMs: -1 },
      { stale: 'false' },
      { phase: 'x'.repeat(41) },
      { lastError: 'x'.repeat(81) },
    ])
      expect(EnvironmentSchema.safeParse({ ...environment, ...fields }).success).toBe(false);
  });
  it('accepts missing readings as null and measured boundary values without inventing zero', () => {
    const missing = {
      ...environment,
      samples: 0,
      readAtMs: null,
      ageMs: null,
      temperatureC: null,
      relativeHumidityPercent: null,
      stale: true,
    };
    expect(EnvironmentSchema.parse(missing)).toEqual(missing);
    for (const temperatureC of [-40, 125])
      for (const relativeHumidityPercent of [0, 100])
        expect(
          EnvironmentSchema.safeParse({ ...environment, temperatureC, relativeHumidityPercent })
            .success,
        ).toBe(true);
  });
  it.each([
    [30.5, 54.6, '010404013102222ace'],
    [31.5, 50, '010404013b01f48ba2'],
    [1.5, 50, '010404000f01f4cb90'],
    [-40, 0, '010404fe700000cbb7'],
    [125, 100, '01040404e203e85a3c'],
    [0, 0, '01040400000000fb84'],
    [-20.5, 54.6, '010404ff330222bae6'],
  ] as const)(
    'independently verifies RTU reading %s C / %s RH against its CRC-valid frame',
    (temperatureC, relativeHumidityPercent, lastFrameHex) => {
      expect(
        EnvironmentSchema.safeParse({
          ...environment,
          protocol: 'sht20_rtu',
          integrity: 'crc_verified',
          temperatureC,
          relativeHumidityPercent,
          lastFrameHex,
        }).success,
      ).toBe(true);
    },
  );
  it('rejects a valid-looking USB temperature digit loss while the verified RTU frame still says 31.5 C', () => {
    const verified = {
      ...environment,
      protocol: 'sht20_rtu',
      integrity: 'crc_verified',
      temperatureC: 31.5,
      relativeHumidityPercent: 50,
      lastFrameHex: '010404013b01f48ba2',
    };
    expect(EnvironmentSchema.safeParse(verified).success).toBe(true);
    for (const changed of [
      { temperatureC: 1.5 },
      { relativeHumidityPercent: 5 },
      { temperatureC: null },
      { relativeHumidityPercent: null },
    ])
      expect(EnvironmentSchema.safeParse({ ...verified, ...changed }).success).toBe(false);
    expect(
      EnvironmentSchema.safeParse({
        ...verified,
        temperatureC: 31.500003,
        relativeHumidityPercent: 49.999999,
      }).success,
    ).toBe(true);
    expect(EnvironmentSchema.safeParse({ ...verified, temperatureC: 31.5002 }).success).toBe(false);
  });
  it('requires the exact successful FC04 frame and rejects every single-bit corruption', () => {
    const verified = {
      ...environment,
      protocol: 'sht20_rtu',
      integrity: 'crc_verified',
      temperatureC: 30.5,
      relativeHumidityPercent: 54.6,
      lastFrameHex: '010404013102222ace',
    };
    const frame = Uint8Array.from(verified.lastFrameHex.match(/../g)!, (byte) =>
      Number.parseInt(byte, 16),
    );
    for (let bit = 0; bit < frame.length * 8; bit += 1) {
      const corrupt = frame.slice();
      corrupt[bit >> 3]! ^= 1 << (bit & 7);
      const lastFrameHex = Array.from(corrupt, (byte) => byte.toString(16).padStart(2, '0')).join(
        '',
      );
      expect(EnvironmentSchema.safeParse({ ...verified, lastFrameHex }).success, 'bit ' + bit).toBe(
        false,
      );
    }
    for (const lastFrameHex of [
      undefined,
      null,
      '',
      verified.lastFrameHex.slice(2),
      verified.lastFrameHex + '00',
      '01040401310222ce2a',
    ])
      expect(EnvironmentSchema.safeParse({ ...verified, lastFrameHex }).success).toBe(false);
  });
  it('preserves legacy, ASCII and failed RTU states with old values instead of checking a failed raw frame as successful', () => {
    for (const changed of [
      {},
      { protocol: 'ascii_read', integrity: 'format_only', lastFrameHex: '312e35' },
      {
        protocol: 'sht20_rtu',
        integrity: 'none',
        lastError: 'crc_error',
        lastFrameHex: '010404013102222acf',
      },
      { protocol: 'sht20_rtu', integrity: 'none', lastError: 'timeout', lastFrameHex: null },
    ])
      expect(EnvironmentSchema.safeParse({ ...environment, ...changed }).success).toBe(true);
  });
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
  it('requires a current boot and bounded renewal duration without enable/rate fields', () => {
    const renew = {
      v: 1,
      id: 'renew',
      command: 'debug.renew',
      bootId: 'boot-1',
      args: { ttlSeconds: 600 },
    };
    expect(DeviceCommandSchema.safeParse(renew).success).toBe(true);
    expect(DeviceCommandSchema.safeParse({ ...renew, bootId: undefined }).success).toBe(false);
    for (const ttlSeconds of [0, 1801, 1.5, NaN, Infinity])
      expect(DeviceCommandSchema.safeParse({ ...renew, args: { ttlSeconds } }).success).toBe(false);
    expect(
      DeviceCommandSchema.safeParse({ ...renew, args: { ttlSeconds: 600, enabled: true } }).success,
    ).toBe(false);
    expect(
      DeviceCommandSchema.safeParse({ ...renew, args: { ttlSeconds: 600, profile: 'full' } })
        .success,
    ).toBe(false);
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
  it('validates measured resource metrics and explicit unavailable values', () => {
    const measured = { ...event, type: 'metrics', data: metrics };
    expect(DeviceEventSchema.safeParse(measured).success).toBe(true);
    expect(
      DeviceEventSchema.safeParse({ ...measured, data: { ...metrics, cpuHz: Infinity } }).success,
    ).toBe(false);
    expect(
      DeviceEventSchema.safeParse({
        ...measured,
        data: { ...metrics, memory: { ...metrics.memory, freeBytes: -1 } },
      }).success,
    ).toBe(false);
    expect(
      DeviceEventSchema.safeParse({
        ...measured,
        data: { ...metrics, temperature: { sampledAtMs: 0, celsius: NaN } },
      }).success,
    ).toBe(false);
    expect(
      DeviceEventSchema.safeParse({
        ...measured,
        data: {
          ...metrics,
          cpuHz: null,
          memory: { freeBytes: null, allocatedBytes: null, totalBytes: null },
          idfHeap: null,
          storage: null,
          flashBytes: null,
          temperature: null,
          rssiDbm: null,
          sampleDurationUs: null,
        },
      }).success,
    ).toBe(true);
  });
  it('retains only the latest resource sample and clears it on the next boot', () => {
    let state = applyDeviceEvent(createBridgeState(), { ...event, type: 'metrics', data: metrics });
    state = applyDeviceEvent(state, {
      ...event,
      type: 'metrics',
      seq: 2,
      data: { ...metrics, sampledAtMs: 5000 },
    });
    expect(state.metrics?.data.sampledAtMs).toBe(5000);
    expect(state.logs).toHaveLength(0);
    state = applyDeviceEvent(state, {
      ...event,
      type: 'hello',
      bootId: 'boot-2',
      data: { firmware: 'test', board: 'test', capabilities: [] },
    });
    expect(state.metrics).toBeNull();
  });
});
