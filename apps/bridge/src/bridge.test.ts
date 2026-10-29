import { afterEach, describe, expect, it, vi } from 'vitest';
import { WebSocket } from 'ws';
import { WIRE_PREFIX, type DeviceCommand } from '@one-esp/protocol';
import { BoundedLineParser } from './line-parser.js';
import { createBridgeController, type BridgeController } from './controller.js';
import { createBridgeApp } from './app.js';
import { startBridgeServer } from './server.js';
import type { DeviceTransport, TransportListeners } from './transport.js';

class FakeTransport implements DeviceTransport {
  listeners: TransportListeners | undefined;
  writes: string[] = [];
  closeCount = 0;
  openError: Error | undefined;

  async open(listeners: TransportListeners): Promise<void> {
    this.listeners = listeners;
    if (this.openError) throw this.openError;
  }
  async write(data: string): Promise<void> {
    this.writes.push(data);
  }
  async close(): Promise<void> {
    this.closeCount += 1;
  }
  receive(data: string): void {
    this.listeners?.data(Buffer.from(data));
  }
}

const controllers: BridgeController[] = [];
const servers: Awaited<ReturnType<typeof startBridgeServer>>[] = [];
afterEach(async () => {
  for (const server of servers.splice(0)) await server.close();
  for (const controller of controllers.splice(0)) await controller.close();
  vi.useRealTimers();
});

function setup() {
  const transports: FakeTransport[] = [];
  const controller = createBridgeController({
    reconnect: false,
    createTransport: () => {
      const transport = new FakeTransport();
      transports.push(transport);
      return transport;
    },
    listPorts: async () => [{ path: 'COM-test', manufacturer: 'Test fixture' }],
  });
  controllers.push(controller);
  return { controller, transports, app: createBridgeApp(controller) };
}

function eventLine(
  type = 'hello',
  data: Record<string, unknown> = {
    firmware: 'fixture',
    board: 'esp32-oled-096',
    capabilities: ['oled'],
  },
  bootId = 'test-boot',
): string {
  return (
    WIRE_PREFIX +
    JSON.stringify({
      v: 1,
      deviceId: 'test-device',
      bootId,
      seq: 0,
      ts: 1,
      type,
      data,
    }) +
    '\n'
  );
}

function post(path: string, body: unknown, origin = 'http://127.0.0.1:5173') {
  return new Request(`http://localhost${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: origin },
    body: JSON.stringify(body),
  });
}

describe('bounded serial framing', () => {
  it('keeps split UTF-8 and CRLF intact across arbitrary chunks', () => {
    const lines: string[] = [];
    const overflow = vi.fn();
    const parser = new BoundedLineParser((line) => lines.push(line), overflow);
    const bytes = Buffer.from('中文\r\nsecond\n');
    for (const byte of bytes) parser.push(Uint8Array.of(byte));
    expect(lines).toEqual(['中文', 'second']);
    expect(overflow).not.toHaveBeenCalled();
  });

  it('discards an oversized unterminated line once and recovers at the next newline', () => {
    const lines: string[] = [];
    const overflow = vi.fn();
    const parser = new BoundedLineParser((line) => lines.push(line), overflow, 16);
    parser.push(Buffer.from('x'.repeat(100_000)));
    parser.push(Buffer.from('tail\nvalid\r\n'));
    expect(lines).toEqual(['valid']);
    expect(overflow).toHaveBeenCalledTimes(1);
  });
});

describe('bridge controller without hardware', () => {
  it('does not open a serial port until explicitly connected', () => {
    const { controller, transports } = setup();
    expect(transports).toHaveLength(0);
    expect(controller.getState().connection).toEqual({
      connected: false,
      mode: 'none',
      port: null,
      error: null,
    });
  });

  it('identifies an already running, debug-disabled device by requesting a snapshot on connect', async () => {
    const transport = new FakeTransport();
    const write = vi.spyOn(transport, 'write').mockImplementation(async (data) => {
      transport.writes.push(data);
      const command = JSON.parse(data.slice(WIRE_PREFIX.length)) as DeviceCommand;
      if (command.command === 'snapshot.get') {
        transport.receive(eventLine());
        transport.receive(
          eventLine('debug', {
            enabled: false,
            profile: 'full',
            expiresInMs: 0,
            oledFps: 2,
            gpioHz: 10,
            droppedLogs: 0,
          }),
        );
      }
    });
    const controller = createBridgeController({
      reconnect: false,
      createTransport: () => transport,
    });
    controllers.push(controller);

    expect(controller.getState().hello).toBeNull();
    await controller.connect('COM-test');

    expect(write).toHaveBeenCalledOnce();
    expect(transport.writes[0]).toMatch(/^@one-esp .*\n$/);
    expect(JSON.parse(transport.writes[0]!.slice(WIRE_PREFIX.length))).toEqual({
      v: 1,
      id: expect.stringMatching(/^bridge-snapshot-/),
      command: 'snapshot.get',
      args: {},
    });
    expect(controller.getState().hello?.deviceId).toBe('test-device');
    expect(controller.getState().debug?.data.enabled).toBe(false);
    expect(controller.getState().connection.connected).toBe(true);
  });

  it('parses split device messages and reports malformed input without crashing', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const transport = transports[0]!;
    const line = eventLine();
    transport.receive(line.slice(0, 12));
    transport.receive(line.slice(12));
    expect(controller.getState().hello?.deviceId).toBe('test-device');
    transport.receive(WIRE_PREFIX + '{broken}\n');
    expect(controller.getState().logs.at(-1)?.data.level).toBe('warn');
    expect(controller.getState().hello?.deviceId).toBe('test-device');
  });

  it('recovers a damaged pin snapshot by requesting a read-only snapshot after two seconds', async () => {
    vi.useFakeTimers();
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const transport = transports[0]!;
    const pins = [
      ...Array.from({ length: 20 }, (_, gpio) => gpio),
      21,
      22,
      23,
      25,
      26,
      27,
      ...Array.from({ length: 8 }, (_, index) => index + 32),
    ].map((gpio) => ({
      gpio,
      label: `GPIO${gpio}`,
      mode: 'unknown',
      owner: null,
      value: null,
      source: 'unavailable',
      writable: false,
    }));
    transport.receive(eventLine());
    transport.receive(eventLine('pins', { pins: pins.slice(0, 28), partial: false }));
    transport.receive(WIRE_PREFIX + '{damaged-pin-chunk}\n');
    expect(controller.getState().logs.at(-1)?.data.message).toContain('格式错误');
    expect(controller.getState().pins?.data.pins).toHaveLength(28);
    vi.spyOn(transport, 'write').mockImplementation(async (data) => {
      transport.writes.push(data);
      transport.receive(eventLine('pins', { pins, partial: false }));
    });

    await vi.advanceTimersByTimeAsync(1999);
    expect(transport.writes).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(transport.writes).toHaveLength(2);
    const recovery = JSON.parse(transport.writes[1]!.slice(WIRE_PREFIX.length));
    expect(recovery).toEqual({
      v: 1,
      id: expect.stringMatching(/^bridge-recovery-/),
      command: 'snapshot.get',
      args: {},
    });
    expect(controller.getState().pins?.data.pins).toHaveLength(34);
    expect(
      controller.getState().logs.some((event) => String(event.data.message).includes('格式错误')),
    ).toBe(true);
  });

  it('coalesces malformed and oversized frames and limits automatic recovery to three requests per open', async () => {
    vi.useFakeTimers();
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const transport = transports[0]!;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      transport.receive(WIRE_PREFIX + '{broken}\n');
      transport.receive('x'.repeat(9000) + '\n');
      await vi.advanceTimersByTimeAsync(1999);
      expect(transport.writes).toHaveLength(1 + Math.min(attempt, 3));
      await vi.advanceTimersByTimeAsync(1);
      expect(transport.writes).toHaveLength(1 + Math.min(attempt + 1, 3));
    }
    const commands = transport.writes.map(
      (line) => JSON.parse(line.slice(WIRE_PREFIX.length)) as DeviceCommand,
    );
    expect(new Set(commands.map((command) => command.id)).size).toBe(4);
    expect(commands.every((command) => command.command === 'snapshot.get')).toBe(true);
    expect(commands.every((command) => command.bootId === undefined)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(
      controller.getState().logs.some((event) => String(event.data.message).includes('8 KiB')),
    ).toBe(true);
  });

  it('cancels recovery on release and prevents old callbacks from requesting a new session snapshot', async () => {
    vi.useFakeTimers();
    const { controller, transports } = setup();
    await controller.connect('COM-old');
    const old = transports[0]!;
    old.receive(WIRE_PREFIX + '{broken}\n');
    expect(vi.getTimerCount()).toBe(1);
    await controller.connect('COM-new');
    expect(vi.getTimerCount()).toBe(0);
    old.receive(WIRE_PREFIX + '{still-broken}\n');
    old.receive('x'.repeat(9000) + '\n');
    await vi.advanceTimersByTimeAsync(3000);
    expect(old.writes).toHaveLength(1);
    expect(transports[1]!.writes).toHaveLength(1);
    expect(controller.getState().connection.port).toBe('COM-new');
  });

  it('redacts credentials from startup logs and device event payloads', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    transports[0]!.receive(eventLine());
    transports[0]!.receive('{"PASSWORD":"super-sensitive-password"}\n');
    transports[0]!.receive(
      eventLine('log', { level: 'warn', message: "token='test-secret-token'" }),
    );
    transports[0]!.receive(
      eventLine('ack', { id: 'bad-firmware', ok: true, result: { PASSWORD: 'must-not-leak' } }),
    );
    const state = JSON.stringify(controller.getState());
    expect(state).not.toContain('super-sensitive-password');
    expect(state).not.toContain('test-secret-token');
    expect(state).not.toContain('must-not-leak');
    expect(state).toContain('[redacted]');
  });

  it('clears old state when switching source and ignores stale transport callbacks', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const old = transports[0]!;
    old.receive(eventLine());
    await controller.startReplay('normal');
    old.receive(eventLine('hello', { firmware: 'old', board: 'old', capabilities: [] }));
    old.receive('x'.repeat(9000));
    old.listeners?.close();
    expect(controller.getState().hello?.deviceId).toBe('demo-esp32');
    expect(controller.getState().connection.mode).toBe('replay');
    expect(
      controller.getState().logs.some((line) => String(line.data.message).includes('8 KiB')),
    ).toBe(false);
    expect(old.closeCount).toBe(1);
  });

  it('sends only validated, newline-terminated structured commands', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const initialWrites = [...transports[0]!.writes];
    const command: DeviceCommand = { v: 1, id: 'snapshot-1', command: 'snapshot.get', args: {} };
    await expect(controller.command(command)).resolves.toEqual({
      id: 'snapshot-1',
      accepted: true,
    });
    expect(transports[0]!.writes).toEqual([
      ...initialWrites,
      WIRE_PREFIX + JSON.stringify(command) + '\n',
    ]);
    await expect(
      controller.command({ ...command, command: 'exec', args: { code: 'bad' } }),
    ).rejects.toThrow('无效');
    await expect(
      controller.command({ v: 1, id: 'restart', command: 'device.restart', args: {} }),
    ).rejects.toThrow('无效');
    expect(transports[0]!.writes).toHaveLength(initialWrites.length + 1);
  });

  it('shows replay as simulated and never performs mutation commands', async () => {
    const { controller } = setup();
    await controller.startReplay('wifi-error');
    expect(controller.getState().connection.mode).toBe('replay');
    expect(controller.getState().oled?.data.text).toContain('Check password');
    const messages: unknown[] = [];
    controller.subscribe((message) => messages.push(message));
    await controller.command({
      v: 1,
      id: 'demo-restart',
      bootId: 'replay',
      command: 'device.restart',
      args: {},
    });
    expect(JSON.stringify(messages)).toContain('演示模式不执行');
    expect(controller.getState().connection.connected).toBe(true);
  });

  it('rejects mutations with stale boot identifiers, missing leases, or expired leases', async () => {
    vi.useFakeTimers();
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const transport = transports[0]!;
    transport.receive(eventLine());
    const command = {
      v: 1,
      id: 'restart',
      bootId: 'old-boot',
      command: 'device.restart',
      args: {},
    };
    await expect(controller.command(command)).rejects.toThrow('已重启');
    command.bootId = 'test-boot';
    await expect(controller.command(command)).rejects.toThrow('未开启或已过期');
    transport.receive(
      eventLine('debug', {
        enabled: true,
        profile: 'full',
        expiresInMs: 1000,
        oledFps: 2,
        gpioHz: 10,
        droppedLogs: 0,
      }),
    );
    await controller.command(command);
    expect(transport.writes).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1001);
    await expect(controller.command({ ...command, id: 'expired' })).rejects.toThrow(
      '未开启或已过期',
    );
    expect(transport.writes).toHaveLength(2);
  });

  it('never forwards GPIO writes outside the reported output whitelist', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const transport = transports[0]!;
    transport.receive(eventLine());
    transport.receive(
      eventLine('debug', {
        enabled: true,
        profile: 'full',
        expiresInMs: 600_000,
        oledFps: 2,
        gpioHz: 10,
        droppedLogs: 0,
      }),
    );
    const command = {
      v: 1,
      id: 'gpio',
      bootId: 'test-boot',
      command: 'gpio.write',
      args: { gpio: 5, value: 1 },
    };
    await expect(controller.command(command)).rejects.toThrow('白名单');
    expect(transport.writes).toHaveLength(1);
  });

  it('marks a lost serial device and releases all port resources on disconnect', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    transports[0]!.listeners?.close();
    expect(controller.getState().connection.connected).toBe(false);
    expect(controller.getState().connection.error).toContain('断开');
    await controller.disconnect();
    expect(controller.getState().connection.mode).toBe('none');
  });

  it('reports a failed port close without falsely keeping the connection online', async () => {
    const { controller, transports } = setup();
    await controller.connect('COM-test');
    const close = vi.spyOn(transports[0]!, 'close');
    close.mockRejectedValueOnce(new Error('close failed'));
    await expect(controller.disconnect()).rejects.toThrow('关闭失败');
    expect(controller.getState().connection.connected).toBe(false);
    expect(controller.getState().connection.error).toContain('关闭失败');
    await controller.disconnect();
    expect(close).toHaveBeenCalledTimes(2);
    expect(controller.getState().connection.mode).toBe('none');
  });

  it('reports native serial construction failures as an unavailable connection', async () => {
    const controller = createBridgeController({
      reconnect: false,
      createTransport: () => {
        throw new Error('Binding unavailable');
      },
    });
    controllers.push(controller);
    await expect(controller.connect('COM-test')).rejects.toThrow('Binding unavailable');
    expect(controller.getState().connection.connected).toBe(false);
    expect(controller.getState().connection.error).toBe('Binding unavailable');
  });

  it('closes the port and reports failure if the initial snapshot cannot be sent', async () => {
    const transport = new FakeTransport();
    vi.spyOn(transport, 'write').mockRejectedValue(new Error('snapshot write failed'));
    const controller = createBridgeController({
      reconnect: false,
      createTransport: () => transport,
    });
    controllers.push(controller);

    await expect(controller.connect('COM-test')).rejects.toThrow('snapshot write failed');
    expect(controller.getState().connection.connected).toBe(false);
    expect(controller.getState().connection.error).toBe('snapshot write failed');
    expect(transport.closeCount).toBe(1);
  });

  it('retries a lost port and cancels retry when the user disconnects', async () => {
    vi.useFakeTimers();
    const transports: FakeTransport[] = [];
    const controller = createBridgeController({
      createTransport: () => {
        const transport = new FakeTransport();
        transports.push(transport);
        return transport;
      },
    });
    controllers.push(controller);
    await controller.connect('COM-test');
    const initialSnapshot = JSON.parse(transports[0]!.writes[0]!.slice(WIRE_PREFIX.length));
    expect(initialSnapshot.command).toBe('snapshot.get');
    expect(initialSnapshot.bootId).toBeUndefined();
    transports[0]!.receive(WIRE_PREFIX + '{broken-before-loss}\n');
    transports[0]!.listeners?.close();
    await vi.advanceTimersByTimeAsync(1000);
    expect(transports).toHaveLength(2);
    expect(controller.getState().connection.connected).toBe(true);
    expect(transports[1]!.writes).toHaveLength(1);
    const reconnectSnapshot = JSON.parse(transports[1]!.writes[0]!.slice(WIRE_PREFIX.length));
    expect(reconnectSnapshot.command).toBe('snapshot.get');
    expect(reconnectSnapshot.bootId).toBeUndefined();
    expect(reconnectSnapshot.id).not.toBe(initialSnapshot.id);
    await vi.advanceTimersByTimeAsync(1000);
    expect(transports[0]!.writes).toHaveLength(1);
    expect(transports[1]!.writes).toHaveLength(1);
    transports[1]!.listeners?.close();
    await controller.disconnect();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(transports).toHaveLength(2);
  });
});

describe('Hono HTTP and WebSocket interface', () => {
  it('lists ports and exposes state without opening hardware', async () => {
    const { app, transports } = setup();
    const response = await app.request('/api/ports');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      ports: [{ path: 'COM-test', manufacturer: 'Test fixture' }],
    });
    expect(transports).toHaveLength(0);
  });

  it('blocks remote origins, remote hostnames, and oversized requests', async () => {
    const { app, transports } = setup();
    expect(
      (await app.request(post('/api/connect', { port: 'COM-test' }, 'https://attacker.invalid')))
        .status,
    ).toBe(403);
    expect((await app.request('http://attacker.invalid/api/state')).status).toBe(403);
    expect((await app.request(post('/api/command', { excess: 'x'.repeat(9000) }))).status).toBe(
      413,
    );
    expect(transports).toHaveLength(0);
  });

  it('returns clean errors for invalid JSON, invalid commands, and disconnected devices', async () => {
    const { app } = setup();
    const malformed = new Request('http://localhost/api/connect', {
      method: 'POST',
      body: '{nope',
    });
    expect((await app.request(malformed)).status).toBe(400);
    expect(
      (await app.request(post('/api/command', { v: 1, id: 'a', command: 'exec', args: {} })))
        .status,
    ).toBe(400);
    expect(
      (
        await app.request(
          post('/api/command', { v: 1, id: 'a', command: 'snapshot.get', args: {} }),
        )
      ).status,
    ).toBe(409);
    expect((await app.request(post('/api/replay', { scenario: 'invalid' }))).status).toBe(400);
  });

  it('provides a real WebSocket snapshot and broadcasts replay events', async () => {
    const { controller } = setup();
    const server = await startBridgeServer({ port: 0, controller });
    servers.push(server);
    const socket = new WebSocket(`ws://127.0.0.1:${server.port}/ws`, {
      origin: 'http://127.0.0.1:5173',
    });
    const received: { type: string; data: unknown }[] = [];
    const initial = await new Promise<{ type: string }>((resolve, reject) => {
      socket.once('error', reject);
      socket.on('message', (buffer) => {
        const message = JSON.parse(buffer.toString()) as { type: string; data: unknown };
        received.push(message);
        if (message.type === 'snapshot') resolve(message);
      });
    });
    expect(initial.type).toBe('snapshot');
    await controller.startReplay('normal');
    await vi.waitFor(() => expect(received.some((message) => message.type === 'event')).toBe(true));
    expect(JSON.stringify(received)).toContain('demo-esp32');
    socket.close();
  });
});
