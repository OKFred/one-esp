export type DebugProfile = 'full' | 'lite';

export const debugProfileLimits = {
  full: { oledFps: 2, gpioHz: 10 },
  lite: { oledFps: 1, gpioHz: 1 },
} as const;

function bounded(value: unknown, minimum: number, maximum: number, fallback: number) {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(maximum, Math.max(minimum, value))
    : fallback;
}

export function normalizeDebugSettings(profile: unknown, oledFps?: unknown, gpioHz?: unknown) {
  const selected: DebugProfile = profile === 'lite' ? 'lite' : 'full';
  const limits = debugProfileLimits[selected];
  return {
    profile: selected,
    oledFps: bounded(oledFps, 0.2, limits.oledFps, limits.oledFps),
    gpioHz: bounded(gpioHz, 0.2, limits.gpioHz, limits.gpioHz),
  };
}

export function debugCommandArgs(
  enabled: boolean,
  profile: unknown,
  oledFps: unknown,
  gpioHz: unknown,
  ttlSeconds: unknown,
) {
  if (!enabled) return { enabled: false } as const;
  return {
    enabled: true as const,
    ...normalizeDebugSettings(profile, oledFps, gpioHz),
    ttlSeconds: Math.trunc(bounded(ttlSeconds, 1, 1800, 600)),
  };
}
