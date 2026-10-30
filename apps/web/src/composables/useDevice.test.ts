import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
import { defineComponent } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createBridgeState, type DeviceCommand, type DeviceEvent } from '@one-esp/protocol';
import { useDevice } from './useDevice';

class FakeSocket {
  static current: FakeSocket;
  private heartbeat: ReturnType<typeof setInterval>;
  onopen?: (event: Event) => void;
  onmessage?: (event: { data: string }) => void;
  onclose?: (event: Event) => void;
  onerror?: (event: Event) => void;
  constructor() {
    FakeSocket.current = this;
    this.heartbeat = setInterval(() => this.receive('heartbeat', { ts: Date.now() }), 5000);
  }
  close() {
    this.stopHeartbeat();
    this.onclose?.(new Event('close'));
  }
  stopHeartbeat() {
    clearInterval(this.heartbeat);
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
  vi.useFakeTimers({
    toFake: ['Date', 'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'],
  });
  vi.stubGlobal('WebSocket', FakeSocket);
  let commandNumber = 0;
  vi.stubGlobal('crypto', {
    randomUUID: () => (++commandNumber === 1 ? 'command-id' : 'command-id-' + commandNumber),
  });
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
  vi.useRealTimers();
});

describe('device command lifecycle', () => {
  it('distinguishes intentionally paused telemetry from a silent active connection', () => {
    FakeSocket.current.receive('event', event('debug', { enabled: false, profile: 'lite' }));
    vi.advanceTimersByTime(20000);
    expect(device.samplingPaused.value).toBe(true);
    expect(device.stale.value).toBe(false);
    FakeSocket.current.receive('event', event('debug', { enabled: true, profile: 'full' }));
    vi.advanceTimersByTime(11000);
    expect(device.samplingPaused.value).toBe(false);
    expect(device.stale.value).toBe(true);
    FakeSocket.current.receive('event', event('status', { uptimeMs: 31000 }));
    expect(device.stale.value).toBe(false);
    FakeSocket.current.receive('event', event('debug', { enabled: false }));
    FakeSocket.current.close();
    expect(device.samplingPaused.value).toBe(false);
    expect(device.stale.value).toBe(true);
  });

  it('does not report a fresh OLED frame when only other telemetry updates', () => {
    FakeSocket.current.receive('event', event('oled', { text: 'old frame' }));
    vi.advanceTimersByTime(20000);
    FakeSocket.current.receive('event', event('status', { uptimeMs: 20000 }));
    expect(device.oledAge.value).toBe(20000);
    expect(device.age.value).toBe(0);
  });

  it('retains relative frame age from cached bridge snapshots', () => {
    const snapshot = createBridgeState();
    snapshot.connection = { connected: true, mode: 'usb', port: 'COM3', error: null };
    snapshot.lastSeen = Date.now() - 5000;
    snapshot.status = { ...event('status', { uptimeMs: 30000 }), ts: 30000 };
    snapshot.oled = { ...event('oled', { text: 'cached' }), ts: 10000 };
    snapshot.debug = { ...event('debug', { enabled: false }), ts: 30000 };
    FakeSocket.current.receive('snapshot', snapshot);
    expect(device.oledAge.value).toBe(25000);
    expect(device.samplingPaused.value).toBe(true);
    expect(device.stale.value).toBe(false);
  });
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

const debugState = (remaining: number, enabled = true) => ({
  enabled,
  profile: 'full',
  oledFps: 2,
  gpioHz: 10,
  expiresInMs: remaining,
  droppedLogs: 0,
});

function enableRenewCapability() {
  FakeSocket.current.receive(
    'event',
    event('hello', {
      firmware: 'test',
      board: 'test',
      capabilities: ['debug-renew'],
    }),
  );
}

function sentCommands(): DeviceCommand[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([path]) => String(path).endsWith('/command'))
    .map(([, options]) => JSON.parse(String(options?.body)) as DeviceCommand);
}

function acknowledge(command: DeviceCommand, result: Record<string, unknown>, ok = true) {
  FakeSocket.current.receive(
    'event',
    event('ack', {
      id: command.id,
      ok,
      result,
      ...(!ok ? { error: 'simulated renewal failure' } : {}),
    }),
  );
}

async function applyShortLease(seconds = 10) {
  enableRenewCapability();
  const promise = device.command('debug.set', {
    enabled: true,
    profile: 'full',
    ttlSeconds: seconds,
  });
  const command = sentCommands().at(-1)!;
  acknowledge(command, debugState(seconds * 1000));
  FakeSocket.current.receive('event', event('debug', debugState(seconds * 1000)));
  await promise;
  return command;
}

describe('automatic lease renewal', () => {
  it.each(['same', 'boot', 'port'] as const)(
    'preserves the applied TTL only when a reconnect confirms the %s identity',
    async (change) => {
      await applyShortLease(10);
      FakeSocket.current.close();
      vi.advanceTimersByTime(1000);
      const newSocket = FakeSocket.current;
      newSocket.onopen?.(new Event('open'));
      newSocket.receive('event', event('debug', debugState(8000)));
      vi.advanceTimersByTime(5000);
      expect(sentCommands()).toHaveLength(1);
      const newBoot = change === 'boot' ? 'boot-b' : 'boot-a';
      const snapshot = createBridgeState();
      snapshot.connection = {
        connected: true,
        mode: 'usb',
        port: change === 'port' ? 'COM4' : 'COM3',
        error: null,
      };
      snapshot.hello = event(
        'hello',
        { firmware: 'test', board: 'test', capabilities: ['debug-renew'] },
        newBoot,
      );
      snapshot.debug = event('debug', debugState(8000), newBoot);
      snapshot.lastSeen = Date.now();
      newSocket.receive('snapshot', snapshot);
      vi.advanceTimersByTime(5000);
      expect(sentCommands()).toHaveLength(2);
      expect(sentCommands()[1]).toMatchObject({
        command: 'debug.renew',
        bootId: newBoot,
        args: { ttlSeconds: change === 'same' ? 10 : 600 },
      });
    },
  );

  it('defaults on but never starts an inactive or unsupported device', () => {
    expect(device.autoRenewEnabled.value).toBe(true);
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(0, false)));
    vi.advanceTimersByTime(600000);
    expect(sentCommands()).toHaveLength(0);
    FakeSocket.current.receive(
      'event',
      event('hello', { firmware: 'legacy', board: 'test', capabilities: [] }),
    );
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    vi.advanceTimersByTime(1000);
    expect(device.autoRenewStatus.value).toBe('unsupported');
    expect(sentCommands()).toHaveLength(0);
  });

  it('renews through repeated TTLs using the applied duration and preserves user feedback', async () => {
    await applyShortLease();
    device.notice.value = 'ordinary notice';
    device.error.value = 'ordinary error';
    for (let cycle = 0; cycle < 3; cycle++) {
      vi.advanceTimersByTime(7000);
      const command = sentCommands().at(-1)!;
      expect(command).toMatchObject({
        command: 'debug.renew',
        bootId: 'boot-a',
        args: { ttlSeconds: 10 },
      });
      expect(Object.keys(command.args)).toEqual(['ttlSeconds']);
      expect(device.autoRenewStatus.value).toBe('renewing');
      expect(device.pendingCount.value).toBe(0);
      acknowledge(command, debugState(10000));
      FakeSocket.current.receive('event', event('debug', debugState(10000)));
      await flushPromises();
      expect(device.autoRenewStatus.value).toBe('waiting');
      expect(device.lastRenewedAt.value).toBe(Date.now());
      expect(device.leaseRemainingMs.value).toBe(10000);
    }
    expect(sentCommands().filter((command) => command.command === 'debug.renew')).toHaveLength(3);
    expect(device.notice.value).toBe('ordinary notice');
    expect(device.error.value).toBe('ordinary error');
  });

  it('adopts a fresh existing active lease and allows only one renewal in flight', () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    vi.advanceTimersByTime(1000);
    expect(sentCommands()).toHaveLength(1);
    expect(sentCommands()[0]?.args).toEqual({ ttlSeconds: 600 });
    vi.advanceTimersByTime(10000);
    expect(sentCommands()).toHaveLength(1);
    expect(device.autoRenewStatus.value).toBe('renewing');
  });

  it('honors disabled automatic renewal and does not renew stale or expired snapshots', () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    device.autoRenewEnabled.value = false;
    vi.advanceTimersByTime(5000);
    expect(sentCommands()).toHaveLength(0);
    expect(device.autoRenewStatus.value).toBe('idle');
    device.autoRenewEnabled.value = true;
    const snapshot = createBridgeState();
    snapshot.connection = { connected: true, mode: 'usb', port: 'COM3', error: null };
    snapshot.lastSeen = Date.now() - 20000;
    snapshot.hello = event('hello', {
      firmware: 'test',
      board: 'test',
      capabilities: ['debug-renew'],
    });
    snapshot.debug = event('debug', debugState(60000));
    FakeSocket.current.receive('snapshot', snapshot);
    vi.advanceTimersByTime(1000);
    expect(sentCommands()).toHaveLength(0);
    snapshot.lastSeen = Date.now() - 120000;
    FakeSocket.current.receive('snapshot', snapshot);
    vi.advanceTimersByTime(1000);
    expect(device.leaseRemainingMs.value).toBe(0);
    expect(sentCommands()).toHaveLength(0);
  });

  it('does not let a late renewal ACK override a manual stop', async () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    vi.advanceTimersByTime(1000);
    const renewal = sentCommands()[0]!;
    const stopped = device.command('debug.set', { enabled: false });
    const stop = sentCommands().at(-1)!;
    acknowledge(renewal, debugState(600000));
    await flushPromises();
    expect(device.autoRenewStatus.value).toBe('idle');
    expect(device.lastRenewedAt.value).toBeNull();
    acknowledge(stop, debugState(0, false));
    FakeSocket.current.receive('event', event('debug', debugState(0, false)));
    await stopped;
    vi.advanceTimersByTime(600000);
    expect(sentCommands().filter((command) => command.command === 'debug.renew')).toHaveLength(1);
  });

  it('skips renewal while a staged user operation is pending', async () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    const pending = device.command('config.set', { revision: 1, values: { SSID: 'test' } });
    const config = sentCommands()[0]!;
    acknowledge(config, { stage: 'testing' });
    vi.advanceTimersByTime(5000);
    expect(sentCommands()).toHaveLength(1);
    acknowledge(config, { stage: 'applied' });
    await pending;
    vi.advanceTimersByTime(1000);
    expect(sentCommands().at(-1)?.command).toBe('debug.renew');
  });

  it('reports a failed renewal separately and backs off instead of flooding commands', async () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    device.notice.value = 'keep notice';
    device.error.value = 'keep error';
    vi.advanceTimersByTime(1000);
    acknowledge(sentCommands()[0]!, {}, false);
    await flushPromises();
    expect(device.autoRenewStatus.value).toBe('error');
    expect(device.autoRenewError.value).toContain('simulated renewal failure');
    for (let tick = 0; tick < 29; tick++) {
      FakeSocket.current.receive('event', event('debug', debugState(59000 - tick * 1000)));
      vi.advanceTimersByTime(1000);
    }
    expect(sentCommands()).toHaveLength(1);
    FakeSocket.current.receive('event', event('debug', debugState(30000)));
    vi.advanceTimersByTime(1000);
    expect(sentCommands()).toHaveLength(2);
    expect(device.notice.value).toBe('keep notice');
    expect(device.error.value).toBe('keep error');
    acknowledge(sentCommands()[1]!, {}, false);
    await flushPromises();
    FakeSocket.current.receive('event', event('debug', debugState(0, false)));
    expect(device.autoRenewError.value).toContain('simulated renewal failure');
    expect(device.autoRenewStatus.value).toBe('idle');
  });

  it('times out a missing renewal ACK and cancels all activity on page unmount', async () => {
    enableRenewCapability();
    FakeSocket.current.receive('event', event('debug', debugState(60000)));
    vi.advanceTimersByTime(19000);
    await flushPromises();
    expect(device.autoRenewError.value).toContain('回执超时');
    expect(sentCommands()).toHaveLength(1);
    wrapper!.unmount();
    wrapper = undefined;
    vi.advanceTimersByTime(600000);
    expect(sentCommands()).toHaveLength(1);
  });

  it.each(['boot', 'port', 'socket'] as const)(
    'invalidates a pending renewal across %s changes',
    async (change) => {
      enableRenewCapability();
      FakeSocket.current.receive('event', event('debug', debugState(60000)));
      vi.advanceTimersByTime(1000);
      const renewal = sentCommands()[0]!;
      const oldSocket = FakeSocket.current;
      if (change === 'boot')
        oldSocket.receive(
          'event',
          event(
            'hello',
            { firmware: 'test', board: 'test', capabilities: ['debug-renew'] },
            'boot-b',
          ),
        );
      else if (change === 'port')
        oldSocket.receive('connection', {
          connected: true,
          mode: 'usb',
          port: 'COM4',
          error: null,
        });
      else oldSocket.close();
      acknowledge(renewal, debugState(600000));
      await flushPromises();
      expect(device.lastRenewedAt.value).toBeNull();
      if (change === 'boot') expect(device.bootId.value).toBe('boot-b');
      vi.advanceTimersByTime(1000);
      if (change === 'socket') {
        FakeSocket.current.onopen?.(new Event('open'));
        oldSocket.receive('event', event('debug', debugState(60000)));
      }
      vi.advanceTimersByTime(5000);
      expect(sentCommands()).toHaveLength(1);
    },
  );
});

function startScreen(text?: string, fps = 2) {
  FakeSocket.current.receive(
    'event',
    event('hello', {
      firmware: 'test',
      board: 'test',
      capabilities: ['oled'],
    }),
  );
  FakeSocket.current.receive('event', event('debug', { ...debugState(600000), oledFps: fps }));
  if (text !== undefined)
    FakeSocket.current.receive(
      'event',
      event('oled', {
        available: true,
        width: 128,
        height: 64,
        format: 'MONO_VLSB',
        buffer: 'frame',
        text,
      }),
    );
}

describe('screen liveness and viewer recovery', () => {
  it('requests one read-only snapshot when OLED stops despite continuing status and heartbeat', async () => {
    startScreen();
    vi.advanceTimersByTime(9000);
    FakeSocket.current.receive('event', event('status', { uptimeMs: 9000 }));
    expect(sentCommands()).toHaveLength(0);
    vi.advanceTimersByTime(1000);
    expect(device.stale.value).toBe(false);
    expect(device.screenStale.value).toBe(true);
    expect(sentCommands()).toMatchObject([{ command: 'snapshot.get', args: {} }]);
    expect(device.screenRecoveryPending.value).toBe(true);
    expect(device.pendingCount.value).toBe(0);
    // First-ever capture must not reset the recovery cooldown or invalidate its ACK.
    FakeSocket.current.receive('event', event('oled', { text: '10-30 13:50:00', buffer: 'first' }));
    acknowledge(sentCommands()[0]!, {});
    await flushPromises();
    vi.advanceTimersByTime(14000);
    expect(sentCommands()).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(sentCommands()).toHaveLength(2);
    vi.advanceTimersByTime(10000);
    expect(sentCommands()).toHaveLength(2);
    expect(sentCommands().every((command) => command.command === 'snapshot.get')).toBe(true);
  });

  it('does not let repeated clock snapshots conceal a frozen device clock', async () => {
    const frozen = '10-30 13:50:00';
    startScreen(frozen);
    for (let i = 0; i < 2; i++) {
      vi.advanceTimersByTime(5000);
      FakeSocket.current.receive('event', event('oled', { text: frozen, buffer: 'same' }));
      FakeSocket.current.receive('event', event('status', { uptimeMs: (i + 1) * 5000 }));
    }
    expect(device.oledAge.value).toBe(0);
    expect(device.screenStale.value).toBe(true);
    acknowledge(sentCommands()[0]!, {});
    await flushPromises();
    expect(device.screenStale.value).toBe(true);
    FakeSocket.current.receive('event', event('oled', { text: '10-30 13:50:01', buffer: 'moved' }));
    expect(device.screenStale.value).toBe(false);
    expect(device.screenRecoveryError.value).toBe('');
  });

  it('gives a low-FPS clock three frame periods and accepts intentional static screens', () => {
    startScreen('10-30 13:50:00', 0.2);
    vi.advanceTimersByTime(14000);
    expect(device.screenStale.value).toBe(false);
    vi.advanceTimersByTime(1000);
    expect(device.screenStale.value).toBe(true);
    expect(sentCommands()).toHaveLength(1);
    wrapper!.unmount();
    wrapper = undefined;
  });

  it('does not treat an intentionally static Wi-Fi error screen as a frozen clock', () => {
    startScreen('Check password');
    for (let i = 0; i < 6; i++) {
      vi.advanceTimersByTime(5000);
      FakeSocket.current.receive('event', event('status', { uptimeMs: (i + 1) * 5000 }));
    }
    expect(device.oledAge.value).toBe(30000);
    expect(device.screenStale.value).toBe(false);
    expect(sentCommands()).toHaveLength(0);
  });

  it('keeps user commands ahead of recovery and ignores late recovery failure after manual stop', async () => {
    startScreen();
    const user = device.command('config.get');
    vi.advanceTimersByTime(10000);
    expect(sentCommands()).toHaveLength(1);
    acknowledge(sentCommands()[0]!, {});
    await user;
    vi.advanceTimersByTime(1000);
    const recovery = sentCommands()[1]!;
    expect(recovery.command).toBe('snapshot.get');
    const stopping = device.command('debug.set', { enabled: false });
    const stop = sentCommands()[2]!;
    acknowledge(recovery, {}, false);
    acknowledge(stop, debugState(0, false));
    FakeSocket.current.receive('event', event('debug', debugState(0, false)));
    await stopping;
    await flushPromises();
    vi.advanceTimersByTime(60000);
    expect(device.screenRecoveryError.value).toBe('');
    expect(device.screenRecoveryPending.value).toBe(false);
    expect(sentCommands()).toHaveLength(3);
    expect(device.samplingPaused.value).toBe(true);
  });

  it('reconnects a half-open channel without waiting for its close event and ignores old messages', () => {
    const old = FakeSocket.current;
    old.receive('event', event('debug', debugState(0, false)));
    old.stopHeartbeat();
    vi.advanceTimersByTime(15000);
    expect(device.channelConnected.value).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(device.channelConnected.value).toBe(false);
    old.receive('event', event('oled', { text: 'late old frame' }));
    expect(device.oled.value).toBeUndefined();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.current).not.toBe(old);
    FakeSocket.current.onopen?.(new Event('open'));
    expect(device.channelConnected.value).toBe(true);
    expect(sentCommands()).toHaveLength(0);
  });

  it('refreshes only active screens on foreground return and removes wake listeners on unmount', async () => {
    startScreen('10-30 13:50:00');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(sentCommands()).toMatchObject([{ command: 'snapshot.get' }]);
    acknowledge(sentCommands()[0]!, {});
    await flushPromises();
    window.dispatchEvent(new Event('pageshow'));
    expect(sentCommands()).toHaveLength(1);
    wrapper!.unmount();
    wrapper = undefined;
    vi.advanceTimersByTime(60000);
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('pageshow'));
    expect(sentCommands()).toHaveLength(1);
  });

  it('revalidates the socket after a suspended tab wakes and waits for its new snapshot', () => {
    startScreen('10-30 13:50:00');
    const old = FakeSocket.current;
    vi.setSystemTime(Date.now() + 60000);
    window.dispatchEvent(new Event('pageshow'));
    expect(device.channelConnected.value).toBe(false);
    expect(sentCommands()).toHaveLength(0);
    vi.advanceTimersByTime(1000);
    const replacement = FakeSocket.current;
    expect(replacement).not.toBe(old);
    replacement.onopen?.(new Event('open'));
    expect(sentCommands()).toHaveLength(0);
    const snapshot = createBridgeState();
    snapshot.connection = { connected: true, mode: 'usb', port: 'COM3', error: null };
    snapshot.hello = event('hello', { firmware: 'test', board: 'test', capabilities: ['oled'] });
    snapshot.debug = event('debug', debugState(500000));
    snapshot.oled = event('oled', { text: '10-30 13:50:00', buffer: 'same' });
    snapshot.lastSeen = Date.now();
    replacement.receive('snapshot', snapshot);
    expect(device.screenStale.value).toBe(true);
    expect(sentCommands()).toMatchObject([{ command: 'snapshot.get' }]);
  });
});
