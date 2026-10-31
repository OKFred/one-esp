import { z } from 'zod';

export const PROTOCOL_VERSION = 1;
export const WIRE_PREFIX = '@one-esp ';
export const MAX_WIRE_LINE = 8192;
export const MAX_LOGS = 200;
const record = z.record(z.string(), z.unknown());

export const PinStateSchema = z.object({
  gpio: z.number().int().min(0).max(39),
  label: z.string().max(80),
  mode: z.enum(['input', 'output', 'i2c', 'uart', 'reserved', 'unknown']),
  owner: z.string().nullable(),
  value: z.union([z.literal(0), z.literal(1), z.null()]),
  source: z.enum(['sampled', 'commanded', 'configured', 'unavailable']),
  writable: z.boolean(),
  reason: z.string().optional(),
});
export type PinState = z.infer<typeof PinStateSchema>;

export const OledFrameSchema = z
  .object({
    width: z.literal(128),
    height: z.literal(64),
    format: z.literal('MONO_VLSB'),
    buffer: z.string().max(1368),
    available: z.boolean(),
    text: z.string().max(2048),
  })
  .superRefine((frame, ctx) => {
    if (frame.available && !/^[A-Za-z0-9+/]{1366}==$/.test(frame.buffer)) {
      ctx.addIssue({
        code: 'custom',
        message: 'OLED frame must contain exactly 1024 bytes of Base64 data',
      });
    }
  });
export type OledFrame = z.infer<typeof OledFrameSchema>;

const bytes = z.number().int().nonnegative();
export const MetricsSchema = z.object({
  sampledAtMs: bytes,
  intervalMs: z.number().int().positive(),
  cpuHz: bytes.nullable(),
  memory: z.object({
    freeBytes: bytes.nullable(),
    allocatedBytes: bytes.nullable(),
    totalBytes: bytes.nullable(),
  }),
  idfHeap: z.object({ totalBytes: bytes, freeBytes: bytes, largestBlockBytes: bytes }).nullable(),
  storage: z.object({ sampledAtMs: bytes, totalBytes: bytes, freeBytes: bytes }).nullable(),
  flashBytes: bytes.nullable(),
  temperature: z.object({ sampledAtMs: bytes, celsius: z.number() }).nullable(),
  rssiDbm: z.number().nullable(),
  sampleDurationUs: bytes.nullable(),
});
export type MetricsData = z.infer<typeof MetricsSchema>;

export const EnvironmentSchema = z
  .object({
    enabled: z.boolean(),
    phase: z.string().min(1).max(40),
    samples: bytes,
    failures: bytes,
    lastError: z.string().max(80).nullable(),
    readAtMs: bytes.nullable(),
    ageMs: bytes.nullable(),
    stale: z.boolean(),
    intervalMs: z.number().int().positive(),
    nextReadInMs: bytes,
    temperatureC: z.number().min(-40).max(125).nullable(),
    relativeHumidityPercent: z.number().min(0).max(100).nullable(),
    protocol: z.enum(['ascii_read', 'sht20_rtu']).optional(),
    integrity: z.enum(['none', 'format_only', 'crc_verified']).optional(),
    crcFailures: bytes.optional(),
    lastFrameHex: z
      .string()
      .max(256)
      .regex(/^[0-9a-f]*$/i)
      .nullable()
      .optional(),
  })
  .strict();
export type EnvironmentData = z.infer<typeof EnvironmentSchema>;

const eventDataSchemas = {
  hello: z
    .object({ firmware: z.string(), board: z.string(), capabilities: z.array(z.string()) })
    .passthrough(),
  status: z
    .object({
      uptimeMs: z.number().nonnegative(),
      memFree: z.number().nullable(),
      wifi: z
        .object({
          connected: z.boolean(),
          status: z.union([z.number(), z.string()]),
          ssid: z.string().optional(),
          ip: z.string().optional(),
          rssi: z.number().nullable().optional(),
        })
        .passthrough(),
      sensorEnabled: z.boolean(),
      environment: EnvironmentSchema.optional(),
    })
    .passthrough(),
  oled: OledFrameSchema,
  metrics: MetricsSchema,
  pins: z.object({ pins: z.array(PinStateSchema).max(40), partial: z.boolean().optional() }),
  log: z.object({
    level: z.enum(['debug', 'info', 'warn', 'error']),
    message: z.string().max(2048),
  }),
  ack: z
    .object({
      id: z.string(),
      ok: z.boolean(),
      result: record.optional(),
      error: z.string().optional(),
    })
    .passthrough(),
  debug: z
    .object({
      enabled: z.boolean(),
      profile: z.enum(['full', 'lite']),
      expiresInMs: z.number().nonnegative(),
      oledFps: z.number().nonnegative(),
      gpioHz: z.number().nonnegative(),
      droppedLogs: z.number().nonnegative(),
    })
    .passthrough(),
};

export const DeviceEventSchema = z
  .object({
    v: z.literal(1),
    deviceId: z.string().min(1).max(100),
    bootId: z.string().min(1).max(100),
    seq: z.number().int().nonnegative(),
    ts: z.number().nonnegative(),
    type: z.enum(['hello', 'status', 'oled', 'pins', 'log', 'ack', 'debug', 'metrics']),
    data: record,
  })
  .superRefine((event, ctx) => {
    const parsed = eventDataSchemas[event.type].safeParse(event.data);
    if (!parsed.success)
      ctx.addIssue({
        code: 'custom',
        path: ['data'],
        message: 'Invalid ' + event.type + ' payload',
      });
  });
export type DeviceEvent = z.infer<typeof DeviceEventSchema>;

export const MqttConfigValuesSchema = z
  .object({
    enabled: z.boolean().optional(),
    host: z.string().max(253).optional(),
    port: z.number().int().min(1).max(65535).optional(),
    server_hostname: z.string().max(253).optional(),
    username: z.string().max(256).optional(),
    password: z.string().max(256).optional(),
    client_id: z.string().max(128).optional(),
    site_id: z.string().max(128).optional(),
    gateway_id: z.string().max(128).optional(),
    device_id: z.string().max(128).optional(),
    interval_seconds: z.number().int().min(60).max(86400).optional(),
    keepalive: z.number().int().min(15).max(300).optional(),
    commands_enabled: z.boolean().optional(),
    ca_file: z.literal('.mqtt-ca.der').optional(),
    broker_ip: z.string().max(15).optional(),
  })
  .strict();
export const SensorConfigValuesSchema = z
  .object({
    enabled: z.boolean().optional(),
    protocol: z.enum(['ascii_read', 'sht20_rtu']).optional(),
    interval_seconds: z.number().int().min(5).max(3600).optional(),
    baudrate: z.union([z.literal(9600), z.literal(14400), z.literal(19200)]).optional(),
    slave_address: z.number().int().min(1).max(247).optional(),
    timeout_ms: z.number().int().min(200).max(5000).optional(),
  })
  .strict();
const commandArgs = {
  'debug.renew': z.object({ ttlSeconds: z.number().int().min(1).max(1800) }).strict(),
  'debug.set': z
    .object({
      enabled: z.boolean(),
      profile: z.enum(['full', 'lite']).optional(),
      ttlSeconds: z.number().int().min(1).max(1800).optional(),
      oledFps: z.number().min(0.2).max(2).optional(),
      gpioHz: z.number().min(0.2).max(10).optional(),
    })
    .strict(),
  'snapshot.get': z.object({}).strict(),
  'device.restart': z.object({}).strict(),
  'config.get': z.object({}).strict(),
  'mqtt.config.get': z.object({}).strict(),
  'sensor.config.get': z.object({}).strict(),
  'mqtt.config.set': z
    .object({ revision: bytes, values: MqttConfigValuesSchema.omit({ ca_file: true }) })
    .strict(),
  'sensor.config.set': z.object({ revision: bytes, values: SensorConfigValuesSchema }).strict(),
  'config.set': z
    .object({
      revision: z.number().int().nonnegative(),
      values: z
        .object({
          SSID: z.string().min(1).max(32).optional(),
          PASSWORD: z.string().max(64).optional(),
          NTP_SERVER: z.string().min(1).max(253).optional(),
          TIMEZONE_HOURS: z.number().min(-12).max(14).optional(),
          NETWORK_TEST_HOST: z.string().min(1).max(253).optional(),
          NETWORK_TEST_PORT: z.number().int().min(1).max(65535).optional(),
          POLL_INTERVAL_MS: z.number().int().min(100).max(60000).optional(),
        })
        .strict(),
    })
    .strict(),
  'gpio.write': z
    .object({ gpio: z.number().int().min(0).max(39), value: z.union([z.literal(0), z.literal(1)]) })
    .strict(),
};
export const DeviceCommandSchema = z
  .object({
    v: z.literal(1),
    id: z.string().min(1).max(100),
    command: z.enum([
      'debug.set',
      'debug.renew',
      'snapshot.get',
      'device.restart',
      'config.get',
      'config.set',
      'mqtt.config.get',
      'mqtt.config.set',
      'sensor.config.get',
      'sensor.config.set',
      'gpio.write',
    ]),
    bootId: z.string().min(1).max(100).optional(),
    args: record.default({}),
  })
  .strict()
  .superRefine((command, ctx) => {
    if (!commandArgs[command.command].safeParse(command.args).success)
      ctx.addIssue({ code: 'custom', path: ['args'], message: 'Invalid command arguments' });
    if (
      [
        'debug.renew',
        'config.set',
        'mqtt.config.set',
        'sensor.config.set',
        'device.restart',
        'gpio.write',
      ].includes(command.command) &&
      !command.bootId
    )
      ctx.addIssue({ code: 'custom', path: ['bootId'], message: 'Current bootId is required' });
  });
export type DeviceCommand = z.infer<typeof DeviceCommandSchema>;

export interface ConnectionState {
  connected: boolean;
  mode: 'usb' | 'replay' | 'none';
  port: string | null;
  error: string | null;
}
export interface BridgeState {
  connection: ConnectionState;
  hello: DeviceEvent | null;
  status: DeviceEvent | null;
  oled: DeviceEvent | null;
  pins: DeviceEvent | null;
  debug: DeviceEvent | null;
  metrics: DeviceEvent | null;
  logs: DeviceEvent[];
  lastSeen: number | null;
}
export type BridgeMessage =
  | { type: 'snapshot'; data: BridgeState }
  | { type: 'event'; data: DeviceEvent }
  | { type: 'heartbeat'; data: { ts: number } }
  | { type: 'connection'; data: ConnectionState };

export function createBridgeState(): BridgeState {
  return {
    connection: { connected: false, mode: 'none', port: null, error: null },
    hello: null,
    status: null,
    oled: null,
    pins: null,
    debug: null,
    metrics: null,
    logs: [],
    lastSeen: null,
  };
}

export function redactSecrets(message: string): string {
  return message.replace(
    /((?:password|passwd|pwd|token|secret|authorization)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)/gi,
    '$1[redacted]',
  );
}

export function applyDeviceEvent(
  state: BridgeState,
  event: DeviceEvent,
  receivedAt = Date.now(),
): BridgeState {
  const previousBoot = state.hello?.bootId ?? state.status?.bootId ?? state.metrics?.bootId;
  const next =
    previousBoot && previousBoot !== event.bootId
      ? { ...createBridgeState(), connection: state.connection }
      : { ...state };
  next.lastSeen = receivedAt;
  if (event.type === 'log') {
    const safeEvent = {
      ...event,
      data: { ...event.data, message: redactSecrets(String(event.data.message)) },
    };
    next.logs = [...next.logs, safeEvent].slice(-MAX_LOGS);
  } else if (event.type === 'pins' && event.data.partial === true) {
    const current = (next.pins?.data.pins ?? []) as PinState[];
    const changes = event.data.pins as PinState[];
    const merged = new Map(current.map((pin) => [pin.gpio, pin]));
    for (const pin of changes) merged.set(pin.gpio, pin);
    next.pins = {
      ...event,
      data: { pins: [...merged.values()].sort((a, b) => a.gpio - b.gpio), partial: false },
    };
  } else if (event.type !== 'ack') {
    next[event.type] = event;
  }
  return next;
}

export function parseDeviceLine(line: string): DeviceEvent | null {
  if (line.length > MAX_WIRE_LINE) throw new Error('Device line exceeds maximum size');
  if (!line.startsWith(WIRE_PREFIX)) return null;
  return DeviceEventSchema.parse(JSON.parse(line.slice(WIRE_PREFIX.length)));
}

export function encodeCommand(command: DeviceCommand): string {
  return WIRE_PREFIX + JSON.stringify(DeviceCommandSchema.parse(command)) + '\n';
}

export function decodeOledPixels(frame: OledFrame): Uint8Array {
  OledFrameSchema.parse(frame);
  const pixels = new Uint8Array(128 * 64);
  if (!frame.available) return pixels;
  const bytes = atob(frame.buffer);
  if (bytes.length !== 1024) throw new Error('Invalid OLED byte count');
  for (let y = 0; y < 64; y++)
    for (let x = 0; x < 128; x++)
      pixels[y * 128 + x] = (bytes.charCodeAt((y >> 3) * 128 + x) >> (y & 7)) & 1;
  return pixels;
}
