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
