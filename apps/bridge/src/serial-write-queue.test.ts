import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SerialWriteQueue } from './serial-write-queue.js';

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('paced complete-frame serial writes', () => {
  it('delivers a multibyte configuration larger than the UART ring during 150 ms reader pauses', async () => {
    const frame =
      '@one-esp ' +
      JSON.stringify({
        v: 1,
        id: 'large-config',
        command: 'config.set',
        bootId: 'current-boot',
        args: {
          revision: 0,
          values: {
            SSID: '中文网络',
            PASSWORD: 'x'.repeat(63),
            NTP_SERVER: 'ntp.aliyun.com',
            TIMEZONE_HOURS: 8,
            NETWORK_TEST_HOST: 'baidu.com',
            NETWORK_TEST_PORT: 443,
            POLL_INTERVAL_MS: 500,
          },
        },
      }) +
      '\n';
    expect(Buffer.byteLength(frame)).toBeGreaterThan(260);
    const received: Buffer[] = [];
    const sentAt: number[] = [];
    let ringBytes = 0;
    let highWater = 0;
    const reader = setInterval(() => {
      ringBytes = 0;
    }, 150);
    const writer = new SerialWriteQueue(async (chunk) => {
      ringBytes += chunk.length;
      highWater = Math.max(highWater, ringBytes);
      if (ringBytes > 260) throw new Error('UART ring overflow');
      received.push(Buffer.from(chunk));
      sentAt.push(Date.now());
    });
    const written = writer.write(frame);
    await vi.advanceTimersByTimeAsync(2000);
    await written;
    clearInterval(reader);

    expect(highWater).toBeLessThanOrEqual(260);
    expect(received.every((chunk) => chunk.length <= 64)).toBe(true);
    expect(Buffer.concat(received).equals(Buffer.from(frame, 'utf8'))).toBe(true);
    expect(sentAt.slice(1).every((time, index) => time - sentAt[index]! >= 60)).toBe(true);
    writer.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('preserves frame boundaries when independent callers write concurrently', async () => {
    const chunks: Buffer[] = [];
    const writer = new SerialWriteQueue(async (chunk) => {
      chunks.push(Buffer.from(chunk));
    });
    const first = '@one-esp ' + 'A'.repeat(310) + '\n';
    const second = '@one-esp ' + '中文'.repeat(65) + '\n';
    const firstWrite = writer.write(first);
    const secondWrite = writer.write(second);
    await vi.runAllTimersAsync();
    await Promise.all([firstWrite, secondWrite]);
    expect(Buffer.concat(chunks).toString('utf8')).toBe(first + second);
    writer.close();
  });

  it('rejects active and queued work on close and never continues it into another session', async () => {
    const oldChunks: Buffer[] = [];
    let finishNativeWrite: (() => void) | undefined;
    const oldWriter = new SerialWriteQueue(
      (chunk) =>
        new Promise<void>((resolve) => {
          oldChunks.push(Buffer.from(chunk));
          finishNativeWrite = resolve;
        }),
    );
    const active = oldWriter.write('old-frame'.repeat(40)).catch((error: Error) => error.message);
    const queued = oldWriter.write('old-queued-frame\n').catch((error: Error) => error.message);
    oldWriter.close();
    expect(await active).toContain('closed');
    expect(await queued).toContain('closed');
    await expect(oldWriter.write('too-late\n')).rejects.toThrow('closed');

    const newChunks: Buffer[] = [];
    const newWriter = new SerialWriteQueue(async (chunk) => {
      newChunks.push(Buffer.from(chunk));
    });
    const fresh = newWriter.write('fresh-session\n');
    finishNativeWrite!();
    await vi.runAllTimersAsync();
    await fresh;
    expect(oldChunks).toHaveLength(1);
    expect(Buffer.concat(newChunks).toString('utf8')).toBe('fresh-session\n');
    newWriter.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels a pending pacing delay immediately on close', async () => {
    const chunks: Buffer[] = [];
    const writer = new SerialWriteQueue(async (chunk) => {
      chunks.push(Buffer.from(chunk));
    });
    const written = writer.write('x'.repeat(500)).catch((error: Error) => error.message);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    writer.close();
    expect(await written).toContain('closed');
    await vi.runAllTimersAsync();
    expect(chunks).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not append another frame after a native write fails midway', async () => {
    const chunks: Buffer[] = [];
    const writer = new SerialWriteQueue(async (chunk) => {
      chunks.push(Buffer.from(chunk));
      if (chunks.length === 2) throw new Error('device unplugged');
    });
    const first = writer.write('x'.repeat(400)).catch((error: Error) => error.message);
    const second = writer.write('must-not-follow\n').catch((error: Error) => error.message);
    await vi.runAllTimersAsync();
    expect(await first).toBe('device unplugged');
    expect(await second).toBe('device unplugged');
    expect(chunks).toHaveLength(2);
    await expect(writer.write('later\n')).rejects.toThrow('closed');
    expect(vi.getTimerCount()).toBe(0);
  });
});
