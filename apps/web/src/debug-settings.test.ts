import { describe, expect, it } from 'vitest';
import { DeviceCommandSchema } from '@one-esp/protocol';
import { debugCommandArgs, normalizeDebugSettings } from './debug-settings';

describe('debug settings at the firmware profile boundary', () => {
  it('converts stale FULL rates to the restored LITE limits before sending', () => {
    const args = debugCommandArgs(true, 'lite', 2, 10, 600);
    expect(args).toEqual({
      enabled: true,
      profile: 'lite',
      oledFps: 1,
      gpioHz: 1,
      ttlSeconds: 600,
    });
    expect(
      DeviceCommandSchema.safeParse({ v: 1, id: 'enable', command: 'debug.set', args }).success,
    ).toBe(true);
  });

  it('uses profile defaults for cleared or non-finite numeric controls', () => {
    for (const invalid of [null, undefined, NaN, Infinity, -Infinity]) {
      const args = debugCommandArgs(true, 'lite', invalid, invalid, invalid);
      expect(args).toEqual({
        enabled: true,
        profile: 'lite',
        oledFps: 1,
        gpioHz: 1,
        ttlSeconds: 600,
      });
      expect(
        DeviceCommandSchema.safeParse({ v: 1, id: 'enable', command: 'debug.set', args }).success,
      ).toBe(true);
    }
  });

  it('bounds edited rates and TTL while retaining valid fractional rates', () => {
    expect(debugCommandArgs(true, 'full', -1, 500, 600.9)).toEqual({
      enabled: true,
      profile: 'full',
      oledFps: 0.2,
      gpioHz: 10,
      ttlSeconds: 600,
    });
    expect(normalizeDebugSettings('lite', 0.5, 0.4)).toEqual({
      profile: 'lite',
      oledFps: 0.5,
      gpioHz: 0.4,
    });
    const capped = debugCommandArgs(true, 'full', 2, 10, 10000);
    if (!capped.enabled) throw new Error('Expected an enabled session');
    expect(capped.ttlSeconds).toBe(1800);
  });

  it('can always disable sampling even when unsaved numeric fields are invalid', () => {
    const args = debugCommandArgs(false, 'lite', NaN, null, NaN);
    expect(args).toEqual({ enabled: false });
    expect(
      DeviceCommandSchema.safeParse({ v: 1, id: 'disable', command: 'debug.set', args }).success,
    ).toBe(true);
  });
});
