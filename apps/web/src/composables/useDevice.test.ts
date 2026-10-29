import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
import { defineComponent } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createBridgeState, type DeviceEvent } from '@one-esp/protocol';
import { useDevice } from './useDevice';

class FakeSocket {
  static current: FakeSocket;
  onopen?: (event: Event) => void;
  onmessage?: (event: { data: string }) => void;
  onclose?: (event: Event) => void;
  onerror?: (event: Event) => void;
  constructor() {
    FakeSocket.current = this;
  }
  close() {
    this.onclose?.(new Event('close'));
  }
  receive(type: string, data: unknown) {
    this.onmessage?.({ data: JSON.stringify({ type, data }) });
  }
}

let wrapper: VueWrapper | undefined;
let device: ReturnType<typeof useDevice>;
const event = (
  type: DeviceEvent['type'],
  data: Record<string, unknown>,
  bootId = 'boot-a',
): DeviceEvent => ({ v: 1, deviceId: 'device-test', bootId, seq: 1, ts: 100, type, data });

beforeEach(async () => {
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal('crypto', { randomUUID: () => 'command-id' });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        path.endsWith('/ports') ? { ports: [] } : { id: 'command-id', accepted: true },
    })),
  );
  wrapper = mount(
    defineComponent({
      setup() {
        device = useDevice();
        return () => null;
      },
    }),
  );
  await flushPromises();
  FakeSocket.current.onopen?.(new Event('open'));
  FakeSocket.current.receive('connection', {
    connected: true,
    mode: 'usb',
    port: 'COM3',
    error: null,
  });
  FakeSocket.current.receive(
    'event',
    event('hello', { firmware: 'test', board: 'test', capabilities: [] }),
  );
});

afterEach(() => {
  wrapper?.unmount();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('device command lifecycle', () => {
  it('clears resource samples on disconnect/reboot and accepts older snapshots', () => {
    FakeSocket.current.receive('event', event('metrics', { sampledAtMs: 100 }));
    expect(device.metrics.value?.sampledAtMs).toBe(100);
    FakeSocket.current.receive('connection', {
      connected: false,
      mode: 'usb',
      port: 'COM3',
      error: null,
    });
    expect(device.metrics.value).toBeUndefined();
    FakeSocket.current.receive('event', event('metrics', { sampledAtMs: 100 }));
    FakeSocket.current.receive(
      'event',
      event('hello', { firmware: 'test', board: 'test', capabilities: [] }, 'boot-b'),
    );
    expect(device.metrics.value).toBeUndefined();
    const { metrics: _oldMetrics, ...olderSnapshot } = createBridgeState();
    FakeSocket.current.receive('snapshot', olderSnapshot);
    expect(device.state.value.metrics).toBeNull();
  });
  it('does not report HTTP accepted as success until the matching device acknowledgement arrives', async () => {
    let finished = false;
    const promise = device.command('snapshot.get').then((ack) => {
      finished = true;
      return ack;
    });
    await flushPromises();
    expect(finished).toBe(false);
    expect(device.pendingCount.value).toBe(1);
    FakeSocket.current.receive('event', event('ack', { id: 'command-id', ok: true, result: {} }));
    expect((await promise).ok).toBe(true);
    expect(device.pendingCount.value).toBe(0);
  });

  it('cancels a pending command when the device restarts', async () => {
    const promise = device.command('snapshot.get');
    const rejected = expect(promise).rejects.toThrow('设备已重启');
    FakeSocket.current.receive(
      'event',
      event('hello', { firmware: 'test', board: 'test', capabilities: [] }, 'boot-b'),
    );
    await rejected;
    expect(device.pendingCount.value).toBe(0);
  });

  it('keeps a staged Wi-Fi change pending until the final applied acknowledgement', async () => {
    let finished = false;
    const promise = device
      .command('config.set', { revision: 1, values: { SSID: 'test' } })
      .then((ack) => {
        finished = true;
        return ack;
      });
    FakeSocket.current.receive(
      'event',
      event('ack', {
        id: 'command-id',
        ok: true,
        result: { stage: 'testing', timeoutSeconds: 60 },
      }),
    );
    await flushPromises();
    expect(finished).toBe(false);
    expect(device.pendingCount.value).toBe(1);
    expect(device.notice.value).toContain('测试新 Wi-Fi');
    FakeSocket.current.receive(
      'event',
      event('ack', {
        id: 'command-id',
        ok: true,
        result: { stage: 'applied', revision: 2 },
      }),
    );
    expect((await promise).result?.revision).toBe(2);
    expect(device.pendingCount.value).toBe(0);
  });

  it('reports a staged Wi-Fi rollback as a failure instead of a successful save', async () => {
    const promise = device.command('config.set', { revision: 1, values: { SSID: 'test' } });
    const rejected = expect(promise).rejects.toThrow('password error');
    FakeSocket.current.receive(
      'event',
      event('ack', {
        id: 'command-id',
        ok: true,
        result: { stage: 'testing' },
      }),
    );
    FakeSocket.current.receive(
      'event',
      event('ack', {
        id: 'command-id',
        ok: false,
        result: { stage: 'rolled-back' },
        error: 'password error',
      }),
    );
    await rejected;
    expect(device.pendingCount.value).toBe(0);
  });

  it('rejects an unsuccessful device ACK and redacts a newly entered password from the error', async () => {
    const promise = device.command('config.set', {
      revision: 1,
      values: { PASSWORD: 'do-not-display' },
    });
    const rejected = expect(promise).rejects.toThrow('[已隐藏]');
    FakeSocket.current.receive(
      'event',
      event('ack', { id: 'command-id', ok: false, error: 'rejected do-not-display' }),
    );
    await rejected;
    expect(
      device.state.value.logs.some((entry) =>
        String(entry.data.message).includes('do-not-display'),
      ),
    ).toBe(false);
  });
});
