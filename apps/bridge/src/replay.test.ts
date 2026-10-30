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

it('keeps the default lease fresh through its renewal window and stays quiet after stop', () => {
  vi.useFakeTimers();
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  const debugEvents = () => events.filter((event) => event.type === 'debug');
  const logCount = events.filter((event) => event.type === 'log').length;
  expect(debugEvents()).toHaveLength(1);
  vi.advanceTimersByTime(4999);
  expect(debugEvents()).toHaveLength(1);
  vi.advanceTimersByTime(1);
  expect(debugEvents()).toHaveLength(2);
  expect(debugEvents().at(-1)?.data.expiresInMs).toBe(595_000);

  vi.advanceTimersByTime(475_000);
  expect(debugEvents()).toHaveLength(97);
  const heartbeat = debugEvents().at(-1)!;
  expect(heartbeat.ts).toBe(480_000);
  expect(heartbeat.data).toMatchObject({ enabled: true, expiresInMs: 120_000 });
  expect(events.filter((event) => event.type === 'log')).toHaveLength(logCount);

  session.command({ v: 1, id: 'stop', command: 'debug.set', args: { enabled: false } });
  expect(debugEvents().at(-1)?.data).toMatchObject({ enabled: false, expiresInMs: 0 });
  const stoppedCount = events.length;
  vi.advanceTimersByTime(10_000);
  expect(events).toHaveLength(stoppedCount);
  session.stop();
  expect(vi.getTimerCount()).toBe(0);
});

it('retains the device profile when pausing and refreshing a quiet replay', () => {
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  session.command({
    v: 1,
    id: 'lite',
    command: 'debug.set',
    args: { enabled: true, profile: 'lite' },
  });
  session.command({ v: 1, id: 'pause', command: 'debug.set', args: { enabled: false } });
  session.command({ v: 1, id: 'refresh', command: 'snapshot.get', args: {} });
  expect(events.filter((event) => event.type === 'debug').at(-1)?.data).toMatchObject({
    enabled: false,
    profile: 'lite',
    oledFps: 1,
    gpioHz: 1,
    expiresInMs: 0,
  });
  session.stop();
});

it('renews a replay lease without a snapshot burst or duplicate extension', () => {
  vi.useFakeTimers();
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  const hello = events.find((event) => event.type === 'hello')!;
  expect(hello.data.capabilities).toContain('debug-renew');
  session.command({
    v: 1,
    id: 'lite',
    command: 'debug.set',
    args: {
      enabled: true,
      profile: 'lite',
      ttlSeconds: 10,
      oledFps: 0.5,
      gpioHz: 0.4,
    },
  });
  vi.advanceTimersByTime(5000);
  const before = events.length;
  const renewal = {
    v: 1 as const,
    id: 'renew',
    command: 'debug.renew' as const,
    bootId: hello.bootId,
    args: { ttlSeconds: 20 },
  };
  session.command(renewal);
  expect(events.slice(before).map((event) => event.type)).toEqual(['ack', 'debug']);
  expect(events.at(-1)?.data).toMatchObject({
    enabled: true,
    profile: 'lite',
    oledFps: 0.5,
    gpioHz: 0.4,
    expiresInMs: 20000,
  });
  expect(events.at(-2)?.data.result).toMatchObject({ enabled: true, expiresInMs: 20000 });
  vi.advanceTimersByTime(4000);
  const duplicateBefore = events.length;
  session.command(renewal);
  expect(events.slice(duplicateBefore).map((event) => event.type)).toEqual(['ack']);
  session.command({ v: 1, id: 'check', command: 'snapshot.get', args: {} });
  expect(events.filter((event) => event.type === 'debug').at(-1)?.data.expiresInMs).toBe(16000);
  session.command({ v: 1, id: 'stop', command: 'debug.set', args: { enabled: false } });
  session.command({ ...renewal, id: 'late-renew' });
  expect(events.at(-1)?.data.ok).toBe(false);
  expect(events.filter((event) => event.type === 'debug').at(-1)?.data.enabled).toBe(false);
  session.stop();
  expect(vi.getTimerCount()).toBe(0);
});

it('rejects replay renewal after expiry or for a different boot', () => {
  vi.useFakeTimers();
  const events: DeviceEvent[] = [];
  const session = createReplaySession(
    'normal',
    (event) => events.push(event),
    () => {},
  );
  session.start();
  const bootId = events.find((event) => event.type === 'hello')!.bootId;
  const renewal = {
    v: 1 as const,
    id: 'wrong',
    command: 'debug.renew' as const,
    bootId: 'old-boot',
    args: { ttlSeconds: 600 },
  };
  session.command(renewal);
  expect(events.at(-1)?.data.ok).toBe(false);
  session.command({
    v: 1,
    id: 'short',
    command: 'debug.set',
    args: { enabled: true, ttlSeconds: 1 },
  });
  vi.advanceTimersByTime(1001);
  session.command({ ...renewal, id: 'expired', bootId });
  expect(events.at(-1)?.data.ok).toBe(false);
  expect(events.filter((event) => event.type === 'debug').at(-1)?.data.enabled).toBe(false);
  session.stop();
});
