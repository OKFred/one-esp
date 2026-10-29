import {
  parseDeviceLine,
  WIRE_PREFIX,
  type DeviceCommand,
  type DeviceEvent,
} from '@one-esp/protocol';

export type ReplayScenario = 'normal' | 'wifi-error' | 'offline';

const glyphs: Record<string, string[]> = {
  A: ['01110', '10001', '10001', '11111', '10001', '10001', '10001'],
  B: ['11110', '10001', '10001', '11110', '10001', '10001', '11110'],
  C: ['01111', '10000', '10000', '10000', '10000', '10000', '01111'],
  D: ['11110', '10001', '10001', '10001', '10001', '10001', '11110'],
  E: ['11111', '10000', '10000', '11110', '10000', '10000', '11111'],
  F: ['11111', '10000', '10000', '11110', '10000', '10000', '10000'],
  G: ['01111', '10000', '10000', '10111', '10001', '10001', '01111'],
  H: ['10001', '10001', '10001', '11111', '10001', '10001', '10001'],
  I: ['11111', '00100', '00100', '00100', '00100', '00100', '11111'],
  J: ['00111', '00010', '00010', '00010', '00010', '10010', '01100'],
  K: ['10001', '10010', '10100', '11000', '10100', '10010', '10001'],
  L: ['10000', '10000', '10000', '10000', '10000', '10000', '11111'],
  M: ['10001', '11011', '10101', '10101', '10001', '10001', '10001'],
  N: ['10001', '11001', '11001', '10101', '10011', '10011', '10001'],
  O: ['01110', '10001', '10001', '10001', '10001', '10001', '01110'],
  P: ['11110', '10001', '10001', '11110', '10000', '10000', '10000'],
  Q: ['01110', '10001', '10001', '10001', '10101', '10010', '01101'],
  R: ['11110', '10001', '10001', '11110', '10100', '10010', '10001'],
  S: ['01111', '10000', '10000', '01110', '00001', '00001', '11110'],
  T: ['11111', '00100', '00100', '00100', '00100', '00100', '00100'],
  U: ['10001', '10001', '10001', '10001', '10001', '10001', '01110'],
  V: ['10001', '10001', '10001', '10001', '10001', '01010', '00100'],
  W: ['10001', '10001', '10001', '10101', '10101', '11011', '10001'],
  X: ['10001', '10001', '01010', '00100', '01010', '10001', '10001'],
  Y: ['10001', '10001', '01010', '00100', '00100', '00100', '00100'],
  Z: ['11111', '00001', '00010', '00100', '01000', '10000', '11111'],
  '0': ['01110', '10001', '10011', '10101', '11001', '10001', '01110'],
  '1': ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
  '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'],
  '3': ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
  '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'],
  '5': ['11111', '10000', '10000', '11110', '00001', '00001', '11110'],
  '6': ['01110', '10000', '10000', '11110', '10001', '10001', '01110'],
  '7': ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
  '8': ['01110', '10001', '10001', '01110', '10001', '10001', '01110'],
  '9': ['01110', '10001', '10001', '01111', '00001', '00001', '01110'],
  '.': ['00000', '00000', '00000', '00000', '00000', '00110', '00110'],
  '/': ['00001', '00010', '00010', '00100', '01000', '01000', '10000'],
  '-': ['00000', '00000', '00000', '11111', '00000', '00000', '00000'],
  ':': ['00000', '00110', '00110', '00000', '00110', '00110', '00000'],
};

export function renderReplayOled(lines: string[]): string {
  const buffer = Buffer.alloc(1024);
  for (const [lineIndex, line] of lines.slice(0, 4).entries()) {
    for (const [column, letter] of [...line.slice(0, 16).toUpperCase()].entries()) {
      const glyph = glyphs[letter];
      if (!glyph) continue;
      for (const [row, pixels] of glyph.entries()) {
        for (let offset = 0; offset < pixels.length; offset += 1) {
          if (pixels[offset] !== '1') continue;
          const x = 2 + column * 8 + offset;
          const y = 4 + lineIndex * 16 + row;
          const index = x + (y >> 3) * 128;
          buffer[index] = (buffer[index] ?? 0) | (1 << (y & 7));
        }
      }
    }
  }
  return buffer.toString('base64');
}

export interface ReplaySession {
  start(): void;
  command(command: DeviceCommand): void;
  stop(): void;
}

export function createReplaySession(
  scenario: ReplayScenario,
  emit: (event: DeviceEvent) => void,
  offline: () => void,
): ReplaySession {
  const started = Date.now();
  const bootId = `replay-${crypto.randomUUID()}`;
  let sequence = 0;
  let enabled = true;
  let profile: 'full' | 'lite' = 'full';
  let expiresAt = started + 600_000;
  let timer: ReturnType<typeof setInterval> | undefined;
  let offlineTimer: ReturnType<typeof setTimeout> | undefined;
  const send = (type: DeviceEvent['type'], data: Record<string, unknown>) => {
    const event = parseDeviceLine(
      WIRE_PREFIX +
        JSON.stringify({
          v: 1,
          deviceId: 'demo-esp32',
          bootId,
          seq: sequence++,
          ts: Date.now() - started,
          type,
          data,
        }),
    );
    if (!event) throw new Error('Invalid replay event');
    emit(event);
  };
  const debug = () =>
    send('debug', {
      enabled,
      profile,
      expiresInMs: Math.max(0, expiresAt - Date.now()),
      oledFps: profile === 'full' ? 2 : 1,
      gpioHz: profile === 'full' ? 10 : 1,
      droppedLogs: 0,
    });
  const status = () =>
    send('status', {
      uptimeMs: Date.now() - started,
      memFree: 112_640,
      wifi: {
        connected: scenario === 'normal',
        status: scenario === 'normal' ? 1010 : scenario === 'wifi-error' ? 202 : 1000,
        ssid: 'DEMO NETWORK',
        ...(scenario === 'normal' ? { ip: '192.0.2.100', rssi: -47 } : {}),
      },
      sensorEnabled: false,
    });
  const snapshot = () => {
    send('hello', {
      firmware: 'one-esp / replay',
      board: 'esp32-oled-096',
      capabilities: ['oled', 'pins', 'logs', 'config.read', 'replay'],
    });
    status();
    debug();
    const lines =
      scenario === 'wifi-error'
        ? ['WiFi failed', 'Check password', 'Edit .env', 'Restart to retry']
        : scenario === 'offline'
          ? ['DEVICE OFFLINE', 'LAST KNOWN FRAME', 'CHECK CONNECTION', 'SENSOR DISABLED']
          : ['WiFi Connected!', '192.0.2.100', 'Debug / USB', 'Sensor disabled'];
    send('oled', {
      width: 128,
      height: 64,
      format: 'MONO_VLSB',
      buffer: renderReplayOled(lines),
      available: true,
      text: lines.join('\n'),
    });
    send('pins', {
      pins: [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 25, 26, 36, 39].map((gpio) => {
        const owner = [4, 5].includes(gpio)
          ? 'OLED I2C'
          : [1, 3].includes(gpio)
            ? 'USB UART0'
            : [6, 7, 8, 11].includes(gpio)
              ? 'Flash'
              : null;
        return {
          gpio,
          label: gpio === 39 ? 'SVN' : gpio === 36 ? 'SVP' : `GPIO${gpio}`,
          mode: [4, 5].includes(gpio)
            ? 'i2c'
            : [1, 3].includes(gpio)
              ? 'uart'
              : owner
                ? 'reserved'
                : 'unknown',
          owner,
          value: null,
          source: owner ? 'configured' : 'unavailable',
          writable: false,
          reason: owner ? '外设占用' : '未登记；不修改引脚配置',
        };
      }),
    });
  };
  return {
    start() {
      snapshot();
      send('log', {
        level: 'info',
        message: '离线演示模式：全部数据来自模拟场景，未连接或控制真实设备。',
      });
      if (scenario === 'wifi-error') {
        send('log', { level: 'warn', message: '模拟 Wi-Fi 认证失败：请检查密码。' });
      }
      if (scenario === 'offline') {
        offlineTimer = setTimeout(offline, 1200);
        offlineTimer.unref();
      } else {
        timer = setInterval(() => {
          if (enabled && Date.now() >= expiresAt) {
            enabled = false;
            debug();
          }
          if (enabled) status();
        }, 1000);
        timer.unref();
      }
    },
    command(command) {
      if (command.command === 'debug.set') {
        enabled = command.args.enabled === true;
        profile = command.args.profile === 'lite' ? 'lite' : 'full';
        expiresAt = Date.now() + Math.min(1800, Number(command.args.ttlSeconds) || 600) * 1000;
        debug();
      } else if (command.command === 'snapshot.get') {
        snapshot();
      } else if (command.command === 'config.get') {
        send('ack', {
          id: command.id,
          ok: true,
          result: {
            revision: 0,
            values: { SSID: 'DEMO NETWORK', TIMEZONE_HOURS: 8 },
            secrets: { PASSWORD: false },
            simulated: true,
          },
        });
        return;
      } else {
        send('ack', {
          id: command.id,
          ok: false,
          error: '演示模式不执行重启、配置修改或 GPIO 输出；请连接真实设备。',
        });
        return;
      }
      send('ack', { id: command.id, ok: true, result: { simulated: true } });
    },
    stop() {
      if (timer) clearInterval(timer);
      if (offlineTimer) clearTimeout(offlineTimer);
    },
  };
}
