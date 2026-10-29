import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSerialCallbackWakeup } from './serial-compat.js';
import * as compatibility from './serial-compat.js';
import { createSerialTransport } from './transport.js';

const serialMock = vi.hoisted(() => ({
  openError: null as Error | null,
  setError: null as Error | null,
  closeError: null as Error | null,
  deferClose: false,
  finishClose: undefined as (() => void) | undefined,
  writeSink: undefined as ((chunk: Buffer) => void) | undefined,
  chunks: [] as Buffer[],
  timeline: [] as string[],
  ports: [] as { emit(name: string): boolean }[],
}));

vi.mock('serialport', async () => {
  const { EventEmitter } = await import('node:events');
  return {
    SerialPort: class extends EventEmitter {
      isOpen = false;
      constructor() {
        super();
        serialMock.ports.push(this);
      }
      open(callback: (error: Error | null) => void) {
        serialMock.timeline.push(`open:${vi.getTimerCount()}`);
        this.isOpen = !serialMock.openError;
        callback(serialMock.openError);
      }
      set(_flags: unknown, callback: (error: Error | null) => void) {
        serialMock.timeline.push(`set:${vi.getTimerCount()}`);
        callback(serialMock.setError);
      }
      write(chunk: Buffer, callback: (error: Error | null) => void) {
        serialMock.chunks.push(Buffer.from(chunk));
        serialMock.writeSink?.(chunk);
        callback(null);
      }
      close(callback: (error: Error | null) => void) {
        serialMock.timeline.push(`close:${vi.getTimerCount()}`);
        const finish = () => {
          this.isOpen = Boolean(serialMock.closeError);
          if (!serialMock.closeError) this.emit('close');
          callback(serialMock.closeError);
        };
        if (serialMock.deferClose) serialMock.finishClose = finish;
        else finish();
      }
    },
  };
});

beforeEach(() => {
  vi.useFakeTimers();
  serialMock.openError = null;
  serialMock.setError = null;
  serialMock.closeError = null;
  serialMock.deferClose = false;
  serialMock.finishClose = undefined;
  serialMock.writeSink = undefined;
  serialMock.chunks.length = 0;
  serialMock.timeline.length = 0;
  serialMock.ports.length = 0;
});

afterEach(() => {
  vi.clearAllTimers();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Windows native callback compatibility', () => {
  it('wakes pending work without retaining the process and can restart after cleanup', () => {
    const intervals = vi.spyOn(globalThis, 'setInterval');
    const wakeup = createSerialCallbackWakeup({ platform: 'win32', nodeVersion: '26.4.0' });
    wakeup.start();
    wakeup.start();
    expect(vi.getTimerCount()).toBe(1);
    expect(intervals).toHaveBeenCalledOnce();
    const timer = intervals.mock.results[0]!.value as ReturnType<typeof setInterval>;
    expect(timer.hasRef()).toBe(false);
    wakeup.stop();
    wakeup.stop();
    expect(vi.getTimerCount()).toBe(0);
    wakeup.start();
    expect(vi.getTimerCount()).toBe(1);
    wakeup.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([
    ['linux', '26.13.0'],
    ['darwin', '26.13.0'],
    ['win32', '26.3.0'],
    ['win32', '24.18.0'],
    ['win32', 'unknown'],
  ])('does not create a timer on %s with Node %s', (platform, nodeVersion) => {
    const wakeup = createSerialCallbackWakeup({ platform, nodeVersion });
    wakeup.start();
    expect(vi.getTimerCount()).toBe(0);
    wakeup.stop();
  });
});

describe('serial transport compatibility lifecycle', () => {
  const createWakeup = createSerialCallbackWakeup;
  const listeners = () => ({ data: vi.fn(), error: vi.fn(), close: vi.fn() });
  const open = async (transport: ReturnType<typeof createSerialTransport>) => {
    const opened = transport.open(listeners());
    await vi.advanceTimersByTimeAsync(60);
    await opened;
  };

  beforeEach(() => {
    // Exercise the affected runtime on every test host using the real helper.
    vi.spyOn(compatibility, 'createSerialCallbackWakeup').mockImplementation(() =>
      createWakeup({ platform: 'win32', nodeVersion: '26.13.0' }),
    );
  });

  it('starts before native open/set and keeps the timer until close settles', async () => {
    const transport = createSerialTransport('virtual-port');
    expect(vi.getTimerCount()).toBe(0);
    await open(transport);
    expect(serialMock.timeline).toEqual(['open:1', 'set:1']);
    expect(Buffer.concat(serialMock.chunks).toString()).toBe('\n');
    expect(vi.getTimerCount()).toBe(1);
    serialMock.deferClose = true;
    const closed = transport.close();
    expect(serialMock.timeline.at(-1)).toBe('close:1');
    expect(vi.getTimerCount()).toBe(1);
    serialMock.finishClose!();
    await closed;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cleans up after native open or control-line setup failure', async () => {
    serialMock.openError = new Error('open failed');
    await expect(createSerialTransport('virtual-open').open(listeners())).rejects.toThrow(
      'open failed',
    );
    expect(vi.getTimerCount()).toBe(0);
    serialMock.openError = null;
    serialMock.setError = new Error('set failed');
    await expect(createSerialTransport('virtual-set').open(listeners())).rejects.toThrow(
      'set failed',
    );
    expect(serialMock.timeline.at(-1)).toBe('close:1');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cleans up on device close and reports it to the controller', async () => {
    const transport = createSerialTransport('virtual-port');
    const callbacks = listeners();
    const opened = transport.open(callbacks);
    await vi.advanceTimersByTimeAsync(60);
    await opened;
    serialMock.ports[0]!.emit('close');
    expect(callbacks.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    await transport.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cleans up even if the native close fails', async () => {
    const transport = createSerialTransport('virtual-port');
    await open(transport);
    serialMock.closeError = new Error('close failed');
    await expect(transport.close()).rejects.toThrow('close failed');
    expect(vi.getTimerCount()).toBe(0);
    serialMock.closeError = null;
    await transport.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('resynchronizes an interrupted old line before the new connection sends a snapshot', async () => {
    let deviceLine = '';
    let malformed = 0;
    const commands: unknown[] = [];
    serialMock.writeSink = (chunk) => {
      for (const char of chunk.toString('utf8')) {
        if (char !== '\n') deviceLine += char;
        else {
          if (deviceLine.startsWith('@one-esp ')) {
            try {
              commands.push(JSON.parse(deviceLine.slice('@one-esp '.length)));
            } catch {
              malformed += 1;
            }
          }
          deviceLine = '';
        }
      }
    };
    const previous = createSerialTransport('virtual-previous');
    await open(previous);
    const interrupted = previous.write('@one-esp ' + 'x'.repeat(500)).catch(() => {});
    await vi.advanceTimersByTimeAsync(0);
    expect(deviceLine).not.toBe('');
    await previous.close();
    await interrupted;

    const current = createSerialTransport('virtual-current');
    await open(current);
    expect(deviceLine).toBe('');
    const snapshot = { v: 1, id: 'fresh-snapshot', command: 'snapshot.get', args: {} };
    const written = current.write('@one-esp ' + JSON.stringify(snapshot) + '\n');
    await vi.advanceTimersByTimeAsync(180);
    await written;
    expect(commands).toEqual([snapshot]);
    expect(malformed).toBe(1);
    await current.close();
    expect(vi.getTimerCount()).toBe(0);
  });
});
