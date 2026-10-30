import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
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
  automatic: boolean;
  epoch: number;
  stopVersion: number;
  debugSettings?: { enabled?: unknown; ttlSeconds?: unknown };
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
  const autoRenewEnabled = ref(true);
  const autoRenewError = ref('');
  const lastRenewedAt = ref<number | null>(null);
  const leaseExpiresAt = ref(0);
  const renewalInFlight = ref(false);
  const manuallyStopped = ref(false);
  const now = ref(Date.now());
  const pending = new Map<string, PendingCommand>();
  const secrets = new Set<string>();
  let socket: WebSocket | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let clockTimer: ReturnType<typeof setInterval> | undefined;
  let stopped = false;
  let retryDelay = 1000;
  let renewalEpoch = 0;
  let stopVersion = 0;
  let renewalTtlSeconds = 600;
  let appliedLeaseIdentity: string | null = null;
  let awaitingReconnectSnapshot = false;
  let retryRenewalAt = 0;

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
  const oledAge = computed(() =>
    state.value.oled ? Math.max(0, now.value - eventReceivedAt(state.value.oled)) : null,
  );
  const samplingPaused = computed(
    () =>
      channelConnected.value && state.value.connection.connected && debug.value?.enabled === false,
  );
  const stale = computed(
    () =>
      !channelConnected.value ||
      !state.value.connection.connected ||
      (!samplingPaused.value && age.value !== null && age.value > 10000),
  );
  const received = new Map<string, number>();
  function eventReceivedAt(event: DeviceEvent) {
    return received.get(event.type) ?? state.value.lastSeen ?? now.value;
  }

  const leaseRemainingMs = computed(() => Math.max(0, leaseExpiresAt.value - now.value));
  const supportsRenewal = computed(() => {
    const capabilities = state.value.hello?.data.capabilities;
    return Array.isArray(capabilities) && capabilities.includes('debug-renew');
  });
  const autoRenewStatus = computed<'idle' | 'waiting' | 'renewing' | 'error' | 'unsupported'>(
    () => {
      if (
        !autoRenewEnabled.value ||
        stopped ||
        !channelConnected.value ||
        !state.value.connection.connected ||
        !debug.value?.enabled ||
        manuallyStopped.value ||
        leaseRemainingMs.value === 0
      )
        return 'idle';
      if (!supportsRenewal.value) return 'unsupported';
      if (renewalInFlight.value) return 'renewing';
      return autoRenewError.value ? 'error' : 'waiting';
    },
  );

  function invalidateRenewal(resetHistory = false, clearFailure = true) {
    renewalEpoch += 1;
    renewalInFlight.value = false;
    retryRenewalAt = 0;
    if (clearFailure) autoRenewError.value = '';
    if (resetHistory) {
      leaseExpiresAt.value = 0;
      lastRenewedAt.value = null;
    }
  }

  function leaseIdentity(snapshot: BridgeState) {
    const source = snapshot.hello ?? snapshot.status ?? snapshot.metrics ?? snapshot.debug;
    return source
      ? JSON.stringify([
          snapshot.connection.mode,
          snapshot.connection.port,
          source.deviceId,
          source.bootId,
        ])
      : null;
  }

  function syncAppliedDuration() {
    const identity = leaseIdentity(state.value);
    if (identity && appliedLeaseIdentity && identity !== appliedLeaseIdentity) {
      renewalTtlSeconds = 600;
      appliedLeaseIdentity = null;
    }
  }

  function recordLease(data: Record<string, unknown>, receivedAt: number) {
    if (
      data.enabled === true &&
      typeof data.expiresInMs === 'number' &&
      Number.isFinite(data.expiresInMs) &&
      data.expiresInMs > 0
    ) {
      leaseExpiresAt.value = receivedAt + data.expiresInMs;
    } else if (data.enabled === false) {
      leaseExpiresAt.value = 0;
      invalidateRenewal(false, false);
      manuallyStopped.value = false;
    }
  }

  function updatePendingCount() {
    // A user must still be able to stop sampling while a renewal ACK is pending.
    pendingCount.value = [...pending.values()].filter((item) => !item.automatic).length;
  }

  function maybeRenew() {
    if (
      !autoRenewEnabled.value ||
      stopped ||
      awaitingReconnectSnapshot ||
      renewalInFlight.value ||
      manuallyStopped.value ||
      !channelConnected.value ||
      !state.value.connection.connected ||
      !bootId.value ||
      !debug.value?.enabled ||
      !supportsRenewal.value ||
      pending.size > 0 ||
      busy.value ||
      Date.now() < retryRenewalAt ||
      leaseRemainingMs.value <= 0
    )
      return;
    const debugEvent = state.value.debug;
    if (
      !debugEvent ||
      Date.now() - eventReceivedAt(debugEvent) > 15000 ||
      age.value === null ||
      age.value > 10000
    )
      return;
    const margin = Math.min(120000, (renewalTtlSeconds * 1000) / 3);
    if (leaseRemainingMs.value > margin) return;
    const epoch = renewalEpoch;
    renewalInFlight.value = true;
    void sendCommand('debug.renew', { ttlSeconds: renewalTtlSeconds }, true)
      .then(() => {
        if (stopped || epoch !== renewalEpoch) return;
        lastRenewedAt.value = Date.now();
        autoRenewError.value = '';
        retryRenewalAt = 0;
      })
      .catch((cause: unknown) => {
        if (stopped || epoch !== renewalEpoch) return;
        autoRenewError.value = clean(cause instanceof Error ? cause.message : '自动续期失败');
        retryRenewalAt = Date.now() + 30000;
      })
      .finally(() => {
        if (epoch === renewalEpoch) renewalInFlight.value = false;
      });
  }

  watch(autoRenewEnabled, () => invalidateRenewal(), { flush: 'sync' });

  function acceptSnapshot(snapshot: BridgeState) {
    received.clear();
    const events = [
      snapshot.hello,
      snapshot.status,
      snapshot.oled,
      snapshot.pins,
      snapshot.debug,
      snapshot.metrics,
    ].filter((event): event is DeviceEvent => !!event);
    const currentBoot =
      snapshot.hello?.bootId ?? snapshot.status?.bootId ?? snapshot.metrics?.bootId;
    const current = events.filter((event) => !currentBoot || event.bootId === currentBoot);
    const latestTick = Math.max(0, ...current.map((event) => event.ts));
    // A cached bridge snapshot may contain an older OLED frame than its latest
    // status/metrics event. Preserve that age rather than restarting it at zero.
    for (const event of current)
      received.set(
        event.type,
        (snapshot.lastSeen ?? Date.now()) - Math.max(0, latestTick - event.ts),
      );
    state.value = { ...snapshot, metrics: snapshot.metrics ?? null };
    syncAppliedDuration();
    if (snapshot.debug) recordLease(snapshot.debug.data, eventReceivedAt(snapshot.debug));
    awaitingReconnectSnapshot = false;
  }

  function rejectPending(reason: string) {
    for (const command of pending.values()) {
      clearTimeout(command.timer);
      command.reject(new Error(reason));
    }
    pending.clear();
    updatePendingCount();
  }

  function acceptEvent(event: DeviceEvent) {
    const previousBoot = bootId.value;
    if (
      event.type === 'ack' &&
      previousBoot &&
      event.bootId !== previousBoot &&
      !pending.has(String(event.data.id))
    )
      return;
    if (event.type === 'log') {
      const data = event.data as Record<string, unknown>;
      event = {
        ...event,
        data: { ...data, message: clean(String(data.message ?? '')) },
      } as DeviceEvent;
    }
    if (previousBoot && event.bootId !== previousBoot && event.type !== 'log') {
      invalidateRenewal(true);
      rejectPending('设备已重启，旧命令已取消');
      state.value = { ...createBridgeState(), connection: state.value.connection };
      received.clear();
    }
    received.set(event.type, Date.now());
    state.value = applyDeviceEvent(state.value, event, Date.now());
    syncAppliedDuration();
    if (event.type === 'debug') recordLease(event.data, Date.now());
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
      updatePendingCount();
      if (ack.ok) {
        const settings = command.debugSettings;
        if (
          settings &&
          command.stopVersion === stopVersion &&
          (!command.automatic || command.epoch === renewalEpoch)
        ) {
          if (settings.enabled === true || command.name === 'debug.renew') {
            const ttl = settings.ttlSeconds;
            if (typeof ttl === 'number' && Number.isFinite(ttl) && ttl >= 1 && ttl <= 1800) {
              renewalTtlSeconds = Math.trunc(ttl);
              appliedLeaseIdentity = JSON.stringify([
                state.value.connection.mode,
                state.value.connection.port,
                event.deviceId,
                event.bootId,
              ]);
            }
            if (command.name === 'debug.set') {
              manuallyStopped.value = false;
              retryRenewalAt = 0;
              autoRenewError.value = '';
            }
            if (ack.result) recordLease(ack.result, Date.now());
          }
        }
        command.resolve(ack);
      } else command.reject(new Error(clean(ack.error || '设备拒绝了操作')));
    }
  }

  function connectSocket() {
    if (stopped) return;
    const currentSocket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`,
    );
    socket = currentSocket;
    currentSocket.onopen = () => {
      if (stopped || socket !== currentSocket) return;
      channelConnected.value = true;
      retryDelay = 1000;
      error.value = '';
    };
    currentSocket.onmessage = (message) => {
      if (stopped || socket !== currentSocket) return;
      try {
        const packet = JSON.parse(String(message.data)) as {
          type: string;
          data: BridgeState | DeviceEvent | BridgeState['connection'];
        };
        if (packet.type === 'snapshot') {
          const snapshot = packet.data as BridgeState;
          if (bootId.value && snapshot.hello?.bootId && snapshot.hello.bootId !== bootId.value) {
            invalidateRenewal(true);
            rejectPending('设备会话已变化，请重新操作');
          }
          acceptSnapshot(snapshot);
        } else if (packet.type === 'event') acceptEvent(packet.data as DeviceEvent);
        else if (packet.type === 'connection') {
          const connection = packet.data as BridgeState['connection'];
          const changed =
            connection.port !== state.value.connection.port ||
            connection.mode !== state.value.connection.mode;
          if (!connection.connected || changed) rejectPending('设备连接已变化，命令已取消');
          if (!connection.connected || changed) invalidateRenewal(true);
          if (changed) received.clear();
          state.value = changed
            ? { ...createBridgeState(), connection }
            : { ...state.value, connection, ...(!connection.connected ? { metrics: null } : {}) };
        }
      } catch {
        error.value = '收到无法解析的 bridge 消息';
      }
    };
    currentSocket.onerror = () => {
      if (stopped || socket !== currentSocket) return;
      error.value = '无法连接本地 bridge，请确认服务已启动';
    };
    currentSocket.onclose = () => {
      if (socket !== currentSocket) return;
      awaitingReconnectSnapshot = true;
      invalidateRenewal(true);
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
    invalidateRenewal(true);
    busy.value = true;
    error.value = '';
    try {
      await request(path, body);
      acceptSnapshot(await request<BridgeState>('/state'));
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '连接操作失败';
    } finally {
      busy.value = false;
    }
  }

  function command(name: DeviceCommand['command'], args: Record<string, unknown> = {}) {
    error.value = '';
    notice.value = '';
    if (name === 'debug.set' && args.enabled === false) {
      stopVersion += 1;
      manuallyStopped.value = true;
      invalidateRenewal();
    }
    return sendCommand(name, args, false);
  }

  function sendCommand(
    name: DeviceCommand['command'],
    args: Record<string, unknown>,
    automatic: boolean,
  ) {
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
          updatePendingCount();
          reject(new Error('等待设备回执超时，请检查实际状态后再试'));
        },
        name === 'config.set' ? 90000 : 18000,
      );
      pending.set(id, {
        name,
        automatic,
        epoch: renewalEpoch,
        stopVersion,
        ...(['debug.set', 'debug.renew'].includes(name)
          ? { debugSettings: { enabled: args.enabled, ttlSeconds: args.ttlSeconds } }
          : {}),
        resolve,
        reject,
        timer,
      });
      updatePendingCount();
    });
    void request('/command', payload).catch((cause: unknown) => {
      const item = pending.get(id);
      if (!item) return;
      clearTimeout(item.timer);
      pending.delete(id);
      updatePendingCount();
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
      maybeRenew();
    }, 1000);
  });
  onBeforeUnmount(() => {
    stopped = true;
    invalidateRenewal(true);
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
    autoRenewEnabled,
    autoRenewStatus,
    autoRenewError,
    lastRenewedAt,
    leaseRemainingMs,
    status,
    debug,
    oled,
    metrics,
    deviceUptimeMs,
    pins,
    bootId,
    deviceId,
    age,
    oledAge,
    samplingPaused,
    stale,
    refreshPorts,
    command,
    perform,
    connect: (port: string) => changeConnection('/connect', { port }),
    disconnect: () => changeConnection('/disconnect', {}),
    replay: (scenario: string) => changeConnection('/replay', { scenario }),
  };
}
