import { randomUUID } from 'node:crypto';
import {
  applyDeviceEvent,
  createBridgeState,
  DeviceCommandSchema,
  parseDeviceLine,
  redactSecrets,
  WIRE_PREFIX,
  encodeCommand,
  type BridgeMessage,
  type BridgeState,
  type DeviceCommand,
  type DeviceEvent,
} from '@one-esp/protocol';
import { BoundedLineParser } from './line-parser.js';
import { createReplaySession, type ReplayScenario, type ReplaySession } from './replay.js';
import {
  createSerialTransport,
  listSerialPorts,
  type DeviceTransport,
  type PortDescription,
} from './transport.js';

export class BridgeError extends Error {
  constructor(
    message: string,
    readonly status: 400 | 403 | 409 | 429 | 503 = 400,
  ) {
    super(message);
    this.name = 'BridgeError';
  }
}

export interface BridgeControllerOptions {
  createTransport?: (path: string) => DeviceTransport;
  listPorts?: () => Promise<PortDescription[]>;
  reconnect?: boolean;
}

function safeMessage(message: string): string {
  return redactSecrets(message)
    .replace(
      /((?:["']?)(?:password|passwd|pwd|token|secret|authorization)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)/gi,
      '$1[redacted]',
    )
    .slice(0, 2048);
}

function safeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        /^(?:(?:mqtt|api)[_-]?)?(?:password|passwd|pwd|token|secret|authorization)$/i.test(key)
          ? typeof item === 'boolean'
            ? item
            : '[redacted]'
          : safeValue(item),
      ]),
    );
  }
  return value;
}

export class BridgeController {
  private state: BridgeState = createBridgeState();
  private readonly listeners = new Set<(message: BridgeMessage) => void>();
  private readonly createTransport: (path: string) => DeviceTransport;
  private readonly enumeratePorts: () => Promise<PortDescription[]>;
  private readonly reconnect: boolean;
  private transport: DeviceTransport | null = null;
  private replay: ReplaySession | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private snapshotRecoveryTimer: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;
  private attempt = 0;
  private failures = 0;
  private desiredPort: string | null = null;
  private syntheticSequence = 0;
  private activeWrites = 0;
  private debugExpiresAt = 0;
  private mutation: Promise<unknown> = Promise.resolve();

  constructor(options: BridgeControllerOptions = {}) {
    this.createTransport = options.createTransport ?? createSerialTransport;
    this.enumeratePorts = options.listPorts ?? listSerialPorts;
    this.reconnect = options.reconnect ?? true;
  }

  getState(): BridgeState {
    return structuredClone(this.state);
  }

  async listPorts(): Promise<PortDescription[]> {
    return this.enumeratePorts();
  }

  subscribe(listener: (message: BridgeMessage) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private publish(message: BridgeMessage): void {
    for (const listener of this.listeners) {
      try {
        listener(message);
      } catch {
        // A slow or closed browser must not stop device input for other viewers.
      }
    }
  }

  private accept(event: DeviceEvent): void {
    const sanitized = { ...event, data: safeValue(event.data) as DeviceEvent['data'] };
    if (sanitized.type === 'log') {
      sanitized.data = {
        ...sanitized.data,
        message: safeMessage(String(sanitized.data.message)),
      };
    }
    const previousBoot = this.state.hello?.bootId ?? this.state.status?.bootId;
    if (previousBoot && previousBoot !== sanitized.bootId) this.debugExpiresAt = 0;
    if (sanitized.type === 'debug') {
      this.debugExpiresAt =
        sanitized.data.enabled === true ? Date.now() + Number(sanitized.data.expiresInMs) : 0;
    }
    this.state = applyDeviceEvent(this.state, sanitized);
    this.publish({ type: 'event', data: sanitized });
  }

  private note(level: 'info' | 'warn' | 'error', message: string): void {
    const envelope = {
      v: 1,
      deviceId: this.state.hello?.deviceId ?? this.state.status?.deviceId ?? 'bridge',
      bootId: this.state.hello?.bootId ?? this.state.status?.bootId ?? 'bridge',
      seq: this.syntheticSequence++,
      ts: Math.floor(process.uptime() * 1000),
      type: 'log',
      data: { level, message: safeMessage(message) },
    };
    const event = parseDeviceLine(WIRE_PREFIX + JSON.stringify(envelope));
    if (event) this.accept(event);
  }

  private connection(
    connected: boolean,
    mode: 'usb' | 'replay' | 'none',
    port: string | null,
    error: string | null = null,
  ): void {
    this.state = { ...this.state, connection: { connected, mode, port, error } };
    this.publish({ type: 'connection', data: this.state.connection });
  }

  private serialized<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.mutation.then(operation, operation);
    this.mutation = result.catch(() => {});
    return result;
  }

  private cancelSnapshotRecovery(): void {
    if (this.snapshotRecoveryTimer) clearTimeout(this.snapshotRecoveryTimer);
    this.snapshotRecoveryTimer = undefined;
  }

  private async release(): Promise<void> {
    this.generation += 1;
    this.attempt += 1;
    this.desiredPort = null;
    this.failures = 0;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = undefined;
    this.cancelSnapshotRecovery();
    this.replay?.stop();
    this.replay = null;
    const transport = this.transport;
    this.transport = null;
    if (transport) {
      try {
        await transport.close();
      } catch {
        // Keep the handle available for a later close attempt. Do not silently
        // claim that a failed close made the port available to an uploader.
        this.transport = transport;
        this.connection(false, 'usb', this.state.connection.port, '串口关闭失败，请重试断开连接。');
        throw new BridgeError('串口关闭失败，请重试断开连接。', 503);
      }
    }
  }

  private clearState(): void {
    this.state = createBridgeState();
    this.debugExpiresAt = 0;
    this.publish({ type: 'snapshot', data: this.getState() });
  }

  async connect(path: string): Promise<void> {
    if (!path || path.length > 256 || /[\r\n\0]/.test(path)) {
      throw new BridgeError('请选择有效串口。');
    }
    return this.serialized(async () => {
      await this.release();
      this.clearState();
      this.desiredPort = path;
      this.connection(false, 'usb', path);
      await this.openUsb(path, this.generation);
    });
  }

  private async openUsb(path: string, generation: number): Promise<void> {
    if (generation !== this.generation || this.desiredPort !== path) return;
    this.cancelSnapshotRecovery();
    const attempt = ++this.attempt;
    let transport: DeviceTransport;
    try {
      transport = this.createTransport(path);
    } catch (error) {
      const message = safeMessage(error instanceof Error ? error.message : '无法创建串口连接。');
      this.connection(false, 'usb', path, message);
      this.scheduleRetry(path, generation);
      throw new BridgeError(message, 503);
    }
    this.transport = transport;
    let recoveryRequests = 0;
    const isCurrentSession = () =>
      attempt === this.attempt &&
      generation === this.generation &&
      this.transport === transport &&
      this.desiredPort === path;
    const recoverSnapshot = () => {
      if (!isCurrentSession() || this.snapshotRecoveryTimer || recoveryRequests >= 3) return;
      const timer = setTimeout(() => {
        if (this.snapshotRecoveryTimer !== timer) return;
        this.snapshotRecoveryTimer = undefined;
        if (!isCurrentSession()) return;
        recoveryRequests += 1;
        this.note('warn', `串口消息损坏，正在重新获取只读快照（${recoveryRequests}/3）。`);
        void transport
          .write(
            encodeCommand({
              v: 1,
              id: `bridge-recovery-${randomUUID()}`,
              command: 'snapshot.get',
              args: {},
            }),
          )
          .catch(() => {
            if (isCurrentSession()) this.note('warn', '恢复快照请求发送失败，请检查串口连接。');
          });
      }, 2000);
      this.snapshotRecoveryTimer = timer;
      timer.unref();
    };
    const parser = new BoundedLineParser(
      (line) => {
        if (attempt !== this.attempt || generation !== this.generation) return;
        if (!line.trim()) return;
        try {
          const event = parseDeviceLine(line);
          if (event) this.accept(event);
          else this.note('info', line);
        } catch {
          this.note('warn', '已忽略格式错误的设备遥测消息。');
          recoverSnapshot();
        }
      },
      () => {
        if (!isCurrentSession()) return;
        this.note('warn', '已丢弃超过 8 KiB 的串口行；等待下一行恢复。');
        recoverSnapshot();
      },
    );
    const lost = (message: string) => {
      if (attempt !== this.attempt || generation !== this.generation) return;
      this.attempt += 1;
      this.cancelSnapshotRecovery();
      this.transport = null;
      this.connection(false, 'usb', path, safeMessage(message));
      void transport.close().catch(() => {});
      this.scheduleRetry(path, generation);
    };
    try {
      await transport.open({
        data: (chunk) => {
          if (attempt === this.attempt && generation === this.generation) parser.push(chunk);
        },
        error: (error) => lost(error.message),
        close: () => lost('串口已断开，等待设备重新连接。'),
      });
      if (attempt !== this.attempt || generation !== this.generation) return;
      // A running device defaults to quiet debug mode and may have emitted its
      // boot hello before this port opened. Read its state without enabling debug.
      await transport.write(
        encodeCommand({
          v: 1,
          id: `bridge-snapshot-${randomUUID()}`,
          command: 'snapshot.get',
          args: {},
        }),
      );
      if (attempt !== this.attempt || generation !== this.generation) return;
      this.failures = 0;
      this.connection(true, 'usb', path);
      this.note('info', `串口 ${path} 已连接：115200 / 8N1，温湿度采集未启用。`);
    } catch (error) {
      const message = error instanceof Error ? error.message : '无法打开串口。';
      lost(message);
      throw new BridgeError(safeMessage(message), 503);
    }
  }

  private scheduleRetry(path: string, generation: number): void {
    if (!this.reconnect || generation !== this.generation || this.desiredPort !== path) return;
    const delay = Math.min(10_000, 1000 * 2 ** Math.min(this.failures++, 4));
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      this.clearState();
      this.connection(false, 'usb', path, '正在重新连接串口…');
      void this.openUsb(path, generation).catch(() => {});
    }, delay);
    this.retryTimer.unref();
  }

  async disconnect(): Promise<void> {
    return this.serialized(async () => {
      await this.release();
      this.connection(false, 'none', null);
    });
  }

  async startReplay(scenario: ReplayScenario): Promise<void> {
    if (!['normal', 'wifi-error', 'offline'].includes(scenario)) {
      throw new BridgeError('未知演示场景。');
    }
    return this.serialized(async () => {
      await this.release();
      this.clearState();
      this.connection(true, 'replay', null);
      const generation = this.generation;
      this.replay = createReplaySession(
        scenario,
        (event) => this.accept(event),
        () => {
          if (generation !== this.generation) return;
          this.replay?.stop();
          this.connection(false, 'replay', null, '模拟设备已离线；画面为最后收到的快照。');
        },
      );
      this.replay.start();
    });
  }

  async command(input: unknown): Promise<{ id: string; accepted: true }> {
    const result = DeviceCommandSchema.safeParse(input);
    if (!result.success) throw new BridgeError('调试命令或参数无效。');
    const command: DeviceCommand = result.data;
    if (!this.state.connection.connected) throw new BridgeError('设备尚未连接。', 409);
    if (this.state.connection.mode === 'replay' && this.replay) {
      this.replay.command(command);
      return { id: command.id, accepted: true };
    }
    const transport = this.transport;
    if (!transport) throw new BridgeError('串口不可用。', 409);
    if (['device.restart', 'config.set', 'gpio.write'].includes(command.command)) {
      const bootId = this.state.hello?.bootId ?? this.state.status?.bootId;
      if (!bootId || command.bootId !== bootId) {
        throw new BridgeError('设备已重启或尚未就绪，请刷新快照后重试。', 409);
      }
      if (this.state.debug?.data.enabled !== true || Date.now() >= this.debugExpiresAt) {
        throw new BridgeError('调试会话未开启或已过期，请重新开启调试。', 403);
      }
      if (command.command === 'gpio.write') {
        const pins = this.state.pins?.data.pins;
        const pin = Array.isArray(pins)
          ? (pins.find(
              (item: unknown) =>
                item !== null &&
                typeof item === 'object' &&
                'gpio' in item &&
                item.gpio === command.args.gpio,
            ) as { writable?: boolean } | undefined)
          : undefined;
        if (pin?.writable !== true) throw new BridgeError('该引脚不在可控制的输出白名单中。', 403);
      }
    }
    if (this.activeWrites >= 8) throw new BridgeError('命令发送过快，请等待设备响应。', 429);
    this.activeWrites += 1;
    try {
      await transport.write(encodeCommand(command));
    } catch {
      throw new BridgeError('串口写入失败，请检查设备连接。', 503);
    } finally {
      this.activeWrites -= 1;
    }
    return { id: command.id, accepted: true };
  }

  async close(): Promise<void> {
    try {
      await this.disconnect();
    } finally {
      this.listeners.clear();
    }
  }
}

export function createBridgeController(options: BridgeControllerOptions = {}): BridgeController {
  return new BridgeController(options);
}
