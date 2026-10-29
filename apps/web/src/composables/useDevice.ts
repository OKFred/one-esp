import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue';
import {
  applyDeviceEvent,
  createBridgeState,
  type BridgeState,
  type DeviceCommand,
  type DeviceEvent,
  type MetricsData,
} from '@one-esp/protocol';
import type { AckData, DebugData, OledData, PinData, PortInfo, StatusData } from '../types';

interface PendingCommand {
  name: DeviceCommand['command'];
  resolve: (ack: AckData) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export function useDevice() {
  const state = shallowRef<BridgeState>(createBridgeState());
  const ports = ref<PortInfo[]>([]);
  const channelConnected = ref(false);
  const busy = ref(false);
  const error = ref('');
  const notice = ref('');
  const pendingCount = ref(0);
  const now = ref(Date.now());
  const pending = new Map<string, PendingCommand>();
  const secrets = new Set<string>();
  let socket: WebSocket | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let clockTimer: ReturnType<typeof setInterval> | undefined;
  let stopped = false;
  let retryDelay = 1000;

  const clean = (value: string) => {
    let result = value;
    for (const secret of secrets) result = result.split(secret).join('[已隐藏]');
    return result;
  };

  const status = computed(() => state.value.status?.data as unknown as StatusData | undefined);
  const debug = computed(() => state.value.debug?.data as unknown as DebugData | undefined);
  const oled = computed(() => state.value.oled?.data as unknown as OledData | undefined);
  const metrics = computed(() => state.value.metrics?.data as unknown as MetricsData | undefined);
  const deviceUptimeMs = computed(() => {
    const event = state.value.status ?? state.value.metrics;
    if (!event) return undefined;
    const uptime = event.type === 'status' ? Number(event.data.uptimeMs) : event.ts;
    // Continue ageing cached samples after debug stops or the transport closes.
    return uptime + Math.max(0, now.value - eventReceivedAt(event));
  });
  const pins = computed(() => {
    const value = state.value.pins?.data as unknown as { pins?: PinData[] } | undefined;
    return value?.pins ?? [];
  });
  const bootId = computed(
    () => state.value.hello?.bootId ?? state.value.status?.bootId ?? state.value.metrics?.bootId,
  );
  const deviceId = computed(() => state.value.hello?.deviceId ?? state.value.status?.deviceId);
  const age = computed(() =>
    state.value.lastSeen === null ? null : Math.max(0, now.value - state.value.lastSeen),
  );
  const stale = computed(
    () => !state.value.connection.connected || (age.value !== null && age.value > 10000),
  );
  const received = new Map<string, number>();
  function eventReceivedAt(event: DeviceEvent) {
    return received.get(event.type) ?? state.value.lastSeen ?? now.value;
  }

  function rejectPending(reason: string) {
    for (const command of pending.values()) {
      clearTimeout(command.timer);
      command.reject(new Error(reason));
    }
    pending.clear();
    pendingCount.value = 0;
  }

  function acceptEvent(event: DeviceEvent) {
    const previousBoot = bootId.value;
    if (event.type === 'log') {
      const data = event.data as Record<string, unknown>;
      event = {
        ...event,
        data: { ...data, message: clean(String(data.message ?? '')) },
      } as DeviceEvent;
    }
    if (previousBoot && event.bootId !== previousBoot && event.type !== 'log') {
      rejectPending('设备已重启，旧命令已取消');
      state.value = { ...createBridgeState(), connection: state.value.connection };
      received.clear();
    }
    received.set(event.type, Date.now());
    state.value = applyDeviceEvent(state.value, event, Date.now());
    if (event.type === 'ack') {
      const ack = event.data as unknown as AckData;
      const command = pending.get(ack.id);
      if (!command) return;
      if (command.name === 'config.set' && ack.ok && ack.result?.stage === 'testing') {
        notice.value = '设备正在测试新 Wi-Fi，等待最终结果（最多 60 秒）';
        return;
      }
      clearTimeout(command.timer);
      pending.delete(ack.id);
      pendingCount.value = pending.size;
      if (ack.ok) command.resolve(ack);
      else command.reject(new Error(clean(ack.error || '设备拒绝了操作')));
    }
  }

  function connectSocket() {
    if (stopped) return;
    socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`,
    );
    socket.onopen = () => {
      channelConnected.value = true;
      retryDelay = 1000;
      error.value = '';
    };
    socket.onmessage = (message) => {
      try {
        const packet = JSON.parse(String(message.data)) as {
          type: string;
          data: BridgeState | DeviceEvent | BridgeState['connection'];
        };
        if (packet.type === 'snapshot') {
          const snapshot = packet.data as BridgeState;
          if (bootId.value && snapshot.hello?.bootId && snapshot.hello.bootId !== bootId.value) {
            rejectPending('设备会话已变化，请重新操作');
          }
          state.value = { ...snapshot, metrics: snapshot.metrics ?? null };
          received.clear();
        } else if (packet.type === 'event') acceptEvent(packet.data as DeviceEvent);
        else if (packet.type === 'connection') {
          const connection = packet.data as BridgeState['connection'];
          const changed =
            connection.port !== state.value.connection.port ||
            connection.mode !== state.value.connection.mode;
          if (!connection.connected || changed) rejectPending('设备连接已变化，命令已取消');
          state.value = changed
            ? { ...createBridgeState(), connection }
            : { ...state.value, connection, ...(!connection.connected ? { metrics: null } : {}) };
        }
      } catch {
        error.value = '收到无法解析的 bridge 消息';
      }
    };
    socket.onerror = () => {
      error.value = '无法连接本地 bridge，请确认服务已启动';
    };
    socket.onclose = () => {
      channelConnected.value = false;
      state.value = { ...state.value, metrics: null };
      rejectPending('调试通道已断开');
      if (!stopped) {
        retryTimer = setTimeout(connectSocket, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 10000);
      }
    };
  }

  async function request<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(`/api${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await response.json()) as T & { error?: string };
    if (!response.ok)
      throw new Error(
        clean(typeof data.error === 'string' ? data.error : `请求失败 (${response.status})`),
      );
    return data;
  }

  async function refreshPorts() {
    try {
      const result = await request<{ ports: PortInfo[] }>('/ports');
      ports.value = result.ports;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '读取串口列表失败';
    }
  }

  async function changeConnection(path: string, body: unknown) {
    busy.value = true;
    error.value = '';
    try {
      await request(path, body);
      state.value = await request<BridgeState>('/state');
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '连接操作失败';
    } finally {
      busy.value = false;
    }
  }

  function command(name: DeviceCommand['command'], args: Record<string, unknown> = {}) {
    error.value = '';
    notice.value = '';
    if (!state.value.connection.connected || !channelConnected.value)
      return Promise.reject(new Error('请先连接设备或打开回放'));
    for (const [key, value] of Object.entries((args.values as Record<string, unknown>) || {})) {
      if (/password|secret|token/i.test(key) && typeof value === 'string' && value)
        secrets.add(value);
    }
    const id = crypto.randomUUID();
    const payload: DeviceCommand = {
      v: 1,
      id,
      command: name,
      args,
      ...(bootId.value ? { bootId: bootId.value } : {}),
    };
    const result = new Promise<AckData>((resolve, reject) => {
      const timer = setTimeout(
        () => {
          pending.delete(id);
          pendingCount.value = pending.size;
          reject(new Error('等待设备回执超时，请检查实际状态后再试'));
        },
        name === 'config.set' ? 90000 : 18000,
      );
      pending.set(id, { name, resolve, reject, timer });
      pendingCount.value = pending.size;
    });
    void request('/command', payload).catch((cause: unknown) => {
      const item = pending.get(id);
      if (!item) return;
      clearTimeout(item.timer);
      pending.delete(id);
      pendingCount.value = pending.size;
      item.reject(cause instanceof Error ? cause : new Error('命令发送失败'));
    });
    return result;
  }

  async function perform(name: DeviceCommand['command'], args: Record<string, unknown> = {}) {
    try {
      const ack = await command(name, args);
      notice.value = '设备已确认操作';
      return ack;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '设备操作失败';
      return undefined;
    }
  }

  onMounted(() => {
    void refreshPorts();
    connectSocket();
    clockTimer = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  });
  onBeforeUnmount(() => {
    stopped = true;
    clearTimeout(retryTimer);
    clearInterval(clockTimer);
    rejectPending('页面已关闭');
    socket?.close();
  });

  return {
    state,
    ports,
    channelConnected,
    busy,
    error,
    notice,
    pendingCount,
    status,
    debug,
    oled,
    metrics,
    deviceUptimeMs,
    pins,
    bootId,
    deviceId,
    age,
    stale,
    refreshPorts,
    command,
    perform,
    connect: (port: string) => changeConnection('/connect', { port }),
    disconnect: () => changeConnection('/disconnect', {}),
    replay: (scenario: string) => changeConnection('/replay', { scenario }),
  };
}
