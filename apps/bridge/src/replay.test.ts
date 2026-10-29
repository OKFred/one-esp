import { afterEach, expect, it, vi } from 'vitest';
import { createReplaySession } from './replay.js';
import type { DeviceEvent } from '@one-esp/protocol';

afterEach(() => vi.useRealTimers());

it('normal replay emits changing framebuffer seconds and pauses when debug is off', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-29T19:20:59Z'));
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  const first = events.find((event) => event.type === 'oled')!;
  expect(first.data.text).toContain('10-30 03:20:59');
  vi.advanceTimersByTime(1000);
  const next = events.filter((event) => event.type === 'oled').at(-1)!;
  expect(next.data.text).toContain('10-30 03:21:00');
  expect(next.data.buffer).not.toBe(first.data.buffer);
  session.command({ v: 1, id: 'pause', command: 'debug.set', args: { enabled: false } });
  const count = events.filter((event) => event.type === 'oled').length;
  vi.advanceTimersByTime(3000);
  expect(events.filter((event) => event.type === 'oled')).toHaveLength(count);
  session.stop();
  expect(vi.getTimerCount()).toBe(0);
});

it('replays resource metrics at 5/15 second cadence and stops on debug expiry', () => {
  vi.useFakeTimers();
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  const samples = () => events.filter((event) => event.type === 'metrics');
  expect(samples()).toHaveLength(1);
  expect(samples()[0]?.data.simulated).toBe(true);
  vi.advanceTimersByTime(4000);
  expect(samples()).toHaveLength(1);
  vi.advanceTimersByTime(1000);
  expect(samples()).toHaveLength(2);
  session.command({
    v: 1,
    id: 'lite',
    command: 'debug.set',
    args: { enabled: true, profile: 'lite', ttlSeconds: 20 },
  });
  const initial = samples().length;
  vi.advanceTimersByTime(14000);
  expect(samples()).toHaveLength(initial);
  vi.advanceTimersByTime(1000);
  expect(samples()).toHaveLength(initial + 1);
  expect(samples().at(-1)?.data.intervalMs).toBe(15000);
  vi.advanceTimersByTime(30000);
  expect(samples()).toHaveLength(initial + 1);
  session.stop();
  expect(vi.getTimerCount()).toBe(0);
});
