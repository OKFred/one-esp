export interface OledData {
  width: 128;
  height: 64;
  format: 'MONO_VLSB';
  buffer: string;
  available: boolean;
  text: string;
}

export interface PinData {
  gpio: number;
  label: string;
  mode: 'input' | 'output' | 'i2c' | 'uart' | 'reserved' | 'unknown';
  owner: string | null;
  value: 0 | 1 | null;
  source: 'sampled' | 'commanded' | 'configured' | 'unavailable';
  writable: boolean;
  reason?: string;
}

export interface StatusData {
  uptimeMs: number;
  memFree: number | null;
  wifi: { connected: boolean; status: number | string; ssid?: string; ip?: string; rssi?: number };
  sensorEnabled: boolean;
  maintenance?: { tcpMs?: number | null; tcpOk?: boolean | null };
  clock?: {
    synced: boolean;
    targetMs: number;
    estimatedErrorMs: number | null;
    withinTarget: boolean;
    withinTargetAtNextSync?: boolean;
    lastSyncAgeMs: number | null;
    intervalMs: number;
    nextInMs: number;
  };
}

export interface DebugData {
  enabled: boolean;
  profile: 'full' | 'lite';
  expiresInMs: number;
  oledFps: number;
  gpioHz: number;
  droppedLogs: number;
}

export interface AckData {
  id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
}

export interface PortInfo {
  path: string;
  manufacturer?: string;
  vendorId?: string;
  productId?: string;
}
