<script setup lang="ts">
import { computed, nextTick, reactive, ref, shallowRef, watch } from 'vue';
import {
  Button as AButton,
  ConfigProvider as AConfigProvider,
  Input as AInput,
  InputNumber as AInputNumber,
  InputPassword as AInputPassword,
  Modal as AModal,
  Select as ASelect,
  Switch as ASwitch,
  theme,
} from 'ant-design-vue';
import { Icon } from '@iconify/vue';
import cpuIcon from '@iconify-icons/lucide/cpu';
import plugIcon from '@iconify-icons/lucide/plug';
import refreshIcon from '@iconify-icons/lucide/refresh-cw';
import settingsIcon from '@iconify-icons/lucide/settings-2';
import restartIcon from '@iconify-icons/lucide/rotate-cw';
import cameraIcon from '@iconify-icons/lucide/scan-line';
import playIcon from '@iconify-icons/lucide/play';
import pauseIcon from '@iconify-icons/lucide/pause';
import trashIcon from '@iconify-icons/lucide/trash-2';
import terminalIcon from '@iconify-icons/lucide/terminal';
import wifiIcon from '@iconify-icons/lucide/wifi';
import memoryIcon from '@iconify-icons/lucide/memory-stick';
import timerIcon from '@iconify-icons/lucide/timer';
import arrowIcon from '@iconify-icons/lucide/arrow-up-right';
import type { DeviceEvent } from '@one-esp/protocol';
import BoardView from './components/BoardView.vue';
import OledScreen from './components/OledScreen.vue';
import ClockQuality from './components/ClockQuality.vue';
import SystemMetrics from './components/SystemMetrics.vue';
import AmbientReadings from './components/AmbientReadings.vue';
import { useDevice } from './composables/useDevice';
import { configValuesForSave, createConfigForm, readConfigValues } from './config';
import { debugCommandArgs, debugProfileLimits, normalizeDebugSettings } from './debug-settings';
import type { PinData } from './types';

const {
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
  deviceId,
  oledAge,
  screenStale,
  screenRecoveryPending,
  screenRecoveryError,
  samplingPaused,
  stale,
  autoRenewEnabled,
  autoRenewStatus,
  autoRenewError,
  lastRenewedAt,
  leaseRemainingMs,
  refreshPorts,
  connect,
  disconnect,
  replay,
  command,
  perform,
} = useDevice();
const selectedPort = ref<string>();
const selectedPin = ref<number | null>(null);
const scenario = ref('normal');
const profile = ref<'full' | 'lite'>('full');
const ttl = ref(600);
const oledFps = ref(2);
const gpioHz = ref(10);
const profileLimits = computed(() => debugProfileLimits[profile.value]);
const autoRenewLabel = computed(() => {
  if (!autoRenewEnabled.value) return '自动续期已关闭';
  if (autoRenewStatus.value === 'unsupported') return '当前固件不支持自动续期';
  if (autoRenewStatus.value === 'error' || autoRenewError.value) return '续期失败，请检查连接';
  if (autoRenewStatus.value === 'renewing') return '正在续期…';
  if (!clientReady.value) return '等待设备连接';
  if (debug.value?.enabled && leaseRemainingMs.value === 0) return '会话已到期，请重新开启采集';
  return debug.value?.enabled ? '自动续期已开启' : '开启采集后自动续期';
});
const logLevel = ref('all');
const paused = ref(false);
const pausedLogs = shallowRef<DeviceEvent[]>([]);
const hiddenLogs = shallowRef(new Set<string>());
const logStream = ref<HTMLElement>();
const configOpen = ref(false);
const configLoading = ref(false);
const configLoaded = ref(false);
const configSaving = ref(false);
const configConfirm = ref(false);
const restartOpen = ref(false);
const gpioConfirm = ref<{ gpio: number; value: 0 | 1 }>();
const passwordConfigured = ref(false);
const configRevision = ref(0);
const form = reactive(createConfigForm());
const configError = ref('');
const clientReady = computed(() => channelConnected.value && state.value.connection.connected);
const canMutate = computed(
  () => clientReady.value && !!debug.value?.enabled && pendingCount.value === 0,
);
const portOptions = computed(() =>
  ports.value.map((port) => ({
    value: port.path,
    label: `${port.path}${port.manufacturer ? ` · ${port.manufacturer}` : ''}`,
  })),
);
const profileOptions = [
  { label: 'FULL · USB 完整', value: 'full' },
  { label: 'LITE · 轻量采集', value: 'lite' },
];
const scenarioOptions = [
  { label: '正常启动', value: 'normal' },
  { label: 'Wi-Fi 密码错误', value: 'wifi-error' },
  { label: '设备离线', value: 'offline' },
];
const levelOptions = [
  { label: '全部级别', value: 'all' },
  { label: 'INFO', value: 'info' },
  { label: 'WARN', value: 'warn' },
  { label: 'ERROR', value: 'error' },
  { label: 'DEBUG', value: 'debug' },
];
const isReplay = computed(() => state.value.connection.mode === 'replay');
const modeLabel = computed(() =>
  isReplay.value
    ? 'REPLAY / 模拟数据'
    : state.value.connection.connected
      ? 'LIVE / USB'
      : '未连接设备',
);
const hardwareReference = computed<PinData[]>(() => {
  const rows = [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 25, 26, 36, 39];
  return rows.map((gpio) => {
    const oledPin = gpio === 4 || gpio === 5;
    const serialPin = gpio === 1 || gpio === 3;
    const flashPin = [6, 7, 8, 11].includes(gpio);
    return {
      gpio,
      label: `GPIO${gpio}`,
      mode: oledPin ? 'i2c' : serialPin ? 'uart' : flashPin ? 'reserved' : 'unknown',
      owner: oledPin ? 'OLED' : serialPin ? 'USB console' : flashPin ? 'Flash' : null,
      value: null,
      source: 'configured',
      writable: false,
      reason: oledPin
        ? gpio === 4
          ? 'SCL · 固件配置参考'
          : 'SDA · 固件配置参考'
        : serialPin
          ? 'UART0 · 控制台'
          : flashPin
            ? '芯片保留，不采样'
            : '等待设备报告；未确认连接不代表空闲',
    };
  });
});
const displayPins = computed(() => (pins.value.length ? pins.value : hardwareReference.value));
const filteredPins = computed(() =>
  selectedPin.value === null
    ? displayPins.value
    : displayPins.value.filter((pin) => pin.gpio === selectedPin.value),
);
const sourceNames = {
  sampled: '输入采样',
  commanded: '程序设定',
  configured: '配置记录',
  unavailable: '不可用',
};
const modeNames = {
  input: 'INPUT',
  output: 'OUTPUT',
  i2c: 'I²C',
  uart: 'UART',
  reserved: 'RESERVED',
  unknown: 'UNKNOWN',
};
const logKey = (entry: DeviceEvent) => `${entry.bootId}:${entry.seq}`;
const visibleLogs = computed(() =>
  (paused.value ? pausedLogs.value : state.value.logs).filter(
    (entry) =>
      !hiddenLogs.value.has(logKey(entry)) &&
      (logLevel.value === 'all' || String(entry.data.level) === logLevel.value),
  ),
);
const screenAge = computed(() =>
  !state.value.connection.connected || !channelConnected.value
    ? '设备未连接'
    : samplingPaused.value
      ? '采集已暂停'
      : screenStale.value
        ? '画面停更'
        : oledAge.value === null
          ? '尚无数据'
          : oledAge.value < 1000
            ? '刚刚更新'
            : `${Math.floor(oledAge.value / 1000)} 秒前更新`,
);
const firmwareVersion = computed(() => String(state.value.hello?.data.firmware ?? '等待识别'));
const formatUptime = (milliseconds: number | undefined) => {
  if (milliseconds === undefined) return '—';
  const seconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
};
const logTime = (event: DeviceEvent) => formatUptime(event.ts);

watch(ports, (value) => {
  if (!selectedPort.value && value[0]) selectedPort.value = value[0].path;
});
watch(
  [() => debug.value?.profile, () => debug.value?.oledFps, () => debug.value?.gpioHz],
  ([value, fps, hz]) => {
    if (!value) return;
    const settings = normalizeDebugSettings(value, fps, hz);
    profile.value = settings.profile;
    oledFps.value = settings.oledFps;
    gpioHz.value = settings.gpioHz;
  },
  { immediate: true },
);
watch(paused, (value) => {
  if (value) pausedLogs.value = [...state.value.logs];
});
watch(visibleLogs, () => {
  const element = logStream.value;
  const atEnd = !element || element.scrollHeight - element.scrollTop - element.clientHeight < 45;
  if (!paused.value && atEnd)
    void nextTick(() => {
      if (logStream.value) logStream.value.scrollTop = logStream.value.scrollHeight;
    });
});
watch(configOpen, (value) => {
  if (!value) {
    form.PASSWORD = '';
    configError.value = '';
    configConfirm.value = false;
  }
});

function changeProfile(value: unknown) {
  const settings = normalizeDebugSettings(value);
  profile.value = settings.profile;
  oledFps.value = settings.oledFps;
  gpioHz.value = settings.gpioHz;
}

function setAutoRenew(value: boolean | string | number) {
  autoRenewEnabled.value = !!value;
}

async function setDebug(enabled: boolean | string | number) {
  const args = debugCommandArgs(!!enabled, profile.value, oledFps.value, gpioHz.value, ttl.value);
  if (args.enabled) {
    profile.value = args.profile;
    oledFps.value = args.oledFps;
    gpioHz.value = args.gpioHz;
    ttl.value = args.ttlSeconds;
  }
  await perform('debug.set', args);
}

async function openConfig() {
  configOpen.value = true;
  configLoading.value = true;
  configLoaded.value = false;
  configError.value = '';
  form.PASSWORD = '';
  try {
    const ack = await command('config.get');
    const result = ack.result || {};
    configRevision.value = Number(result.revision ?? 0);
    const secretFlags = result.secrets as Record<string, unknown> | undefined;
    passwordConfigured.value = Boolean(result.passwordConfigured ?? secretFlags?.PASSWORD);
    const values = (result.values as Record<string, unknown>) || {};
    Object.assign(form, readConfigValues(values));
    configLoaded.value = true;
  } catch (cause) {
    configError.value = cause instanceof Error ? cause.message : '读取配置失败';
  } finally {
    configLoading.value = false;
  }
}

function reviewConfig() {
  configError.value = '';
  try {
    configValuesForSave(form);
    configConfirm.value = true;
  } catch (cause) {
    configError.value = cause instanceof Error ? cause.message : '配置格式无效';
  }
}

async function saveConfig() {
  configSaving.value = true;
  try {
    const values = configValuesForSave(form);
    await command('config.set', { revision: configRevision.value, values });
    form.PASSWORD = '';
    configConfirm.value = false;
    configOpen.value = false;
    notice.value = '设备已确认配置变更';
  } catch (cause) {
    configError.value = cause instanceof Error ? cause.message : '配置保存失败';
    configConfirm.value = false;
  } finally {
    configSaving.value = false;
  }
}

async function restart() {
  const ack = await perform('device.restart');
  if (ack) {
    restartOpen.value = false;
    notice.value = '设备已接受重启命令，等待重新上线';
  }
}

async function writePin() {
  if (!gpioConfirm.value) return;
  const ack = await perform('gpio.write', gpioConfirm.value);
  if (ack) gpioConfirm.value = undefined;
}

function clearVisibleLogs() {
  hiddenLogs.value = new Set(state.value.logs.map(logKey));
  pausedLogs.value = [];
}
</script>

<template>
  <AConfigProvider
    :theme="{
      algorithm: theme.darkAlgorithm,
      token: {
        colorPrimary: '#52d6b7',
        colorBgBase: '#0b1015',
        colorTextBase: '#dce7ec',
        borderRadius: 5,
        fontSize: 13,
        controlHeight: 34,
        fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif',
      },
    }"
    component-size="middle"
  >
    <div class="console-shell">
      <header class="app-header">
        <a href="/" class="brand" aria-label="one-esp 首页"
          ><span class="brand-mark"><Icon :icon="cpuIcon" /></span
          ><span>one<span class="brand-dash">—</span>esp<small>DEVICE WORKBENCH</small></span></a
        >
        <div class="header-context">
          <span class="header-divider"></span><span>设备调试台</span
          ><span class="hardware-label">ESP32-WROOM-32</span>
        </div>
        <div class="header-right">
          <span :class="['channel-status', { active: channelConnected }]"
            ><i></i>{{ channelConnected ? 'BRIDGE ONLINE' : 'BRIDGE OFFLINE' }}</span
          ><span class="version-label">LOCAL / v0.1</span>
        </div>
      </header>

      <main>
        <div class="page-heading">
          <div>
            <div class="eyebrow">OBSERVE. INSPECT. UNDERSTAND.</div>
            <h1>看见设备正在发生什么<span class="heading-cursor">_</span></h1>
            <p>屏幕、引脚与运行日志，汇集在一个工作台。</p>
          </div>
          <span
            :class="[
              'mode-badge',
              { 'mode-replay': isReplay, 'mode-live': !isReplay && state.connection.connected },
            ]"
            ><i></i>{{ modeLabel }}</span
          >
        </div>

        <section class="connection-bar" aria-label="设备连接">
          <div class="connection-title"><Icon :icon="plugIcon" /><span>连接设备</span></div>
          <label class="sr-only" for="usb-port">USB 串口</label>
          <ASelect
            id="usb-port"
            v-model:value="selectedPort"
            :options="portOptions"
            placeholder="选择 USB 串口"
            aria-label="USB 串口"
            class="port-select"
            :disabled="busy || state.connection.connected"
          />
          <AButton
            class="icon-button"
            aria-label="刷新串口列表"
            title="刷新串口列表"
            :disabled="busy"
            @click="refreshPorts"
            ><Icon :icon="refreshIcon"
          /></AButton>
          <AButton
            v-if="!state.connection.connected"
            type="primary"
            :loading="busy"
            :disabled="!selectedPort"
            @click="selectedPort && connect(selectedPort)"
            >连接串口</AButton
          >
          <AButton v-else :loading="busy" @click="disconnect">断开连接</AButton>
          <span class="bar-divider"></span>
          <label class="sr-only" for="replay-scenario">回放场景</label>
          <ASelect
            id="replay-scenario"
            v-model:value="scenario"
            :options="scenarioOptions"
            aria-label="回放场景"
            class="scenario-select"
          />
          <AButton :disabled="busy" @click="replay(scenario)"
            ><template #icon><Icon :icon="playIcon" /></template>场景回放</AButton
          >
          <span class="connection-hint">{{
            isReplay ? '模拟场景 · 不操作实物' : 'USB 115200 · 8N1'
          }}</span>
        </section>

        <div v-if="error || state.connection.error" class="feedback feedback-error" role="alert">
          <strong>操作未完成</strong><span>{{ error || state.connection.error }}</span
          ><button aria-label="关闭提示" @click="error = ''">×</button>
        </div>
        <div v-else-if="notice" class="feedback feedback-success" role="status">
          <span>{{ notice }}</span
          ><button aria-label="关闭提示" @click="notice = ''">×</button>
        </div>
        <div v-if="isReplay" class="replay-banner">
          <Icon :icon="playIcon" /><strong>场景回放模式</strong
          ><span>此处为模拟遥测和模拟回执，不代表真实设备状态。</span>
        </div>

        <section class="stats-grid" aria-label="设备概览">
          <div class="stat">
            <span class="stat-icon"><Icon :icon="wifiIcon" /></span>
            <div>
              <span class="stat-label">网络连接</span
              ><strong :class="{ 'tone-green': status?.wifi.connected }">{{
                status ? (status.wifi.connected ? 'Wi-Fi 已连接' : 'Wi-Fi 未连接') : '等待设备'
              }}</strong
              ><small>{{ status?.wifi.ip || status?.wifi.ssid || '暂无网络信息' }}</small>
            </div>
          </div>
          <div class="stat">
            <span class="stat-icon"><Icon :icon="memoryIcon" /></span>
            <div>
              <span class="stat-label">可用内存</span
              ><strong
                >{{
                  status?.memFree !== null && status?.memFree !== undefined
                    ? (status.memFree / 1024).toFixed(1)
                    : '—'
                }}<em v-if="status?.memFree !== null && status?.memFree !== undefined"
                  >KB</em
                ></strong
              ><small>MicroPython heap</small>
            </div>
          </div>
          <div class="stat">
            <span class="stat-icon"><Icon :icon="timerIcon" /></span>
            <div>
              <span class="stat-label">运行时间</span
              ><strong class="mono">{{ formatUptime(status?.uptimeMs) }}</strong
              ><small>{{ deviceId || '设备尚未识别' }}</small>
            </div>
          </div>
          <div class="stat stat-session">
            <div>
              <span class="stat-label">调试会话</span
              ><strong>{{
                debug?.enabled ? `${debug.profile.toUpperCase()} / 采集中` : '未开启'
              }}</strong
              ><small>{{
                debug?.enabled
                  ? `${autoRenewEnabled && ['waiting', 'renewing'].includes(autoRenewStatus) ? '自动续期 · ' : ''}剩余 ${Math.ceil(leaseRemainingMs / 1000)} 秒`
                  : '连接后开启调试以获取遥测'
              }}</small>
            </div>
            <span :class="['session-dot', { enabled: debug?.enabled }]"></span>
          </div>
        </section>

        <div class="workspace-grid">
          <div class="main-column">
            <section class="panel display-panel">
              <div class="panel-heading">
                <div>
                  <span class="section-number">01</span>
                  <h2>屏幕镜像</h2>
                  <span class="panel-subtitle">DISPLAY</span>
                </div>
                <span :class="['freshness', { expired: screenStale }]"><i></i>{{ screenAge }}</span>
              </div>
              <div class="display-content">
                <OledScreen :frame="oled" :stale="screenStale" :paused="samplingPaused" />
                <p
                  v-if="(screenStale && (oled || debug?.enabled)) || screenRecoveryPending"
                  class="screen-recovery tone-warning"
                  role="status"
                  aria-label="屏幕恢复状态"
                >
                  <span v-if="screenStale">画面停更 · </span>
                  {{ screenRecoveryPending ? '正在恢复画面' : '等待设备的新画面，自动尝试恢复' }}
                  <span v-if="screenRecoveryError"> · {{ screenRecoveryError }}</span>
                </p>
                <ClockQuality v-if="status?.clock" :clock="status.clock" />
              </div>
              <div class="display-toolbar">
                <span class="mono"
                  >{{ debug?.oledFps ?? '—' }} FPS LIMIT <span class="toolbar-dot">·</span> 128 × 64
                  PX</span
                ><AButton
                  v-if="samplingPaused"
                  size="small"
                  :disabled="!clientReady || pendingCount > 0"
                  @click="setDebug(true)"
                  >开启实时采集</AButton
                ><AButton
                  size="small"
                  :disabled="!clientReady || pendingCount > 0"
                  @click="perform('snapshot.get')"
                  ><template #icon><Icon :icon="cameraIcon" /></template>获取快照</AButton
                >
              </div>
            </section>

            <AmbientReadings
              :environment="status?.environment"
              :connected="state.connection.connected && channelConnected"
              :telemetry-active="!!debug?.enabled"
              :device-stale="stale"
              :uptime-ms="deviceUptimeMs"
              :status-uptime-ms="status?.uptimeMs"
            />

            <SystemMetrics
              :metrics="metrics"
              :active="!!debug?.enabled && channelConnected"
              :connected="state.connection.connected && channelConnected"
              :uptime-ms="deviceUptimeMs"
              :tcp-ms="status?.maintenance?.tcpMs"
              :tcp-ok="status?.maintenance?.tcpOk"
            />

            <section class="panel pins-panel">
              <div class="panel-heading">
                <div>
                  <span class="section-number">02</span>
                  <h2>引脚状态</h2>
                  <span class="panel-subtitle">GPIO INSPECTOR</span>
                </div>
                <button v-if="selectedPin !== null" class="text-button" @click="selectedPin = null">
                  显示全部 ×</button
                ><span v-else class="subtle-label">{{
                  pins.length ? '设备报告' : '配置参考 / 等待遥测'
                }}</span>
              </div>
              <div class="pin-table-wrap">
                <table class="pin-table">
                  <thead>
                    <tr>
                      <th>引脚</th>
                      <th>功能 / 占用</th>
                      <th>状态</th>
                      <th>数据来源</th>
                      <th class="pin-actions">输出控制</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="pin in filteredPins"
                      :key="pin.gpio"
                      :class="{ 'row-selected': selectedPin === pin.gpio }"
                    >
                      <td>
                        <button
                          class="gpio-label"
                          @click="selectedPin = selectedPin === pin.gpio ? null : pin.gpio"
                        >
                          GPIO {{ pin.gpio }}
                        </button>
                      </td>
                      <td>
                        <span :class="['function-tag', `function-${pin.mode}`]">{{
                          modeNames[pin.mode]
                        }}</span
                        ><span class="pin-owner">{{ pin.owner || '未确认' }}</span
                        ><small v-if="pin.reason" class="pin-reason">{{ pin.reason }}</small>
                      </td>
                      <td>
                        <span
                          :class="['pin-value', { high: pin.value === 1, low: pin.value === 0 }]"
                          >{{ pin.value === null ? '—' : pin.value === 1 ? 'HIGH' : 'LOW' }}</span
                        >
                      </td>
                      <td>
                        <span class="source-label">{{ sourceNames[pin.source] }}</span>
                      </td>
                      <td class="pin-actions">
                        <div class="gpio-control">
                          <AButton
                            size="small"
                            :disabled="!pin.writable || !canMutate"
                            :aria-label="`GPIO${pin.gpio} 设为低电平`"
                            @click="gpioConfirm = { gpio: pin.gpio, value: 0 }"
                            >0</AButton
                          ><AButton
                            size="small"
                            :disabled="!pin.writable || !canMutate"
                            :aria-label="`GPIO${pin.gpio} 设为高电平`"
                            @click="gpioConfirm = { gpio: pin.gpio, value: 1 }"
                            >1</AButton
                          >
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="panel-note">
                <span class="small-dot"></span
                >输入采样与程序设定值分别标识。输出白名单默认为空，已占用及保留引脚不开放控制。
              </div>
            </section>
          </div>

          <aside class="side-column">
            <section class="panel board-panel">
              <div class="panel-heading">
                <div>
                  <span class="section-number">03</span>
                  <h2>开发板</h2>
                </div>
                <span class="subtle-label">PIN MAP</span>
              </div>
              <BoardView
                :pins="displayPins"
                :selected="selectedPin"
                :screen-text="oled?.text"
                @select="selectedPin = selectedPin === $event ? null : $event"
              />
            </section>
            <section class="panel controls-panel">
              <div class="panel-heading">
                <div><h2>实时调试采集</h2></div>
                <ASwitch
                  :checked="debug?.enabled ?? false"
                  :disabled="!clientReady || pendingCount > 0"
                  aria-label="实时调试采集"
                  @change="setDebug"
                />
              </div>
              <div class="controls-body">
                <p class="control-help">
                  控制屏幕镜像、引脚和资源指标的实时更新。关闭后仍可获取快照，MQTT
                  温度上报继续运行。
                </p>
                <div class="auto-renew-control">
                  <div class="auto-renew-heading">
                    <span>自动续期</span>
                    <ASwitch
                      :checked="autoRenewEnabled"
                      aria-label="自动续期"
                      @change="setAutoRenew"
                    />
                  </div>
                  <p class="control-help" aria-live="polite">{{ autoRenewLabel }}</p>
                  <p v-if="autoRenewError" class="control-help tone-warning">
                    {{ autoRenewError }}
                  </p>
                  <p v-if="lastRenewedAt" class="control-help">
                    上次成功续期 {{ new Date(lastRenewedAt).toLocaleTimeString() }}
                  </p>
                  <p class="control-help">
                    本页连接时自动续期。关闭本页后不再续期；没有其他页面续期时，到期后暂停。电脑休眠时可能到期。
                  </p>
                </div>
                <label class="field-label" for="profile-select">采集档位</label
                ><ASelect
                  id="profile-select"
                  :value="profile"
                  :options="profileOptions"
                  class="full-width"
                  aria-label="采集档位"
                  @change="changeProfile"
                />
                <div class="control-fields">
                  <div>
                    <label class="field-label" for="oled-fps">屏幕 / FPS</label
                    ><AInputNumber
                      id="oled-fps"
                      v-model:value="oledFps"
                      :min="0.2"
                      :max="profileLimits.oledFps"
                      :step="0.5"
                      aria-label="屏幕帧率"
                    />
                  </div>
                  <div>
                    <label class="field-label" for="gpio-hz">GPIO / Hz</label
                    ><AInputNumber
                      id="gpio-hz"
                      v-model:value="gpioHz"
                      :min="0.2"
                      :max="profileLimits.gpioHz"
                      :step="0.2"
                      aria-label="GPIO 采样频率"
                    />
                  </div>
                </div>
                <label class="field-label" for="session-ttl">会话有效期 / 秒</label
                ><AInputNumber
                  id="session-ttl"
                  v-model:value="ttl"
                  :min="10"
                  :max="1800"
                  :step="60"
                  class="full-width"
                  aria-label="调试会话有效期"
                /><AButton
                  class="full-width apply-button"
                  :disabled="!canMutate"
                  @click="setDebug(true)"
                  >应用参数 / 续期</AButton
                >
                <p class="control-help">
                  参数以设备回执为准。慢连接时保留最新画面，并按实际负载限流。
                </p>
                <div class="device-actions">
                  <AButton :disabled="!clientReady || pendingCount > 0" @click="openConfig"
                    ><template #icon><Icon :icon="settingsIcon" /></template>设备配置</AButton
                  ><AButton :disabled="!canMutate" @click="restartOpen = true"
                    ><template #icon><Icon :icon="restartIcon" /></template>重启</AButton
                  >
                </div>
              </div>
            </section>
          </aside>
        </div>

        <section class="panel logs-panel">
          <div class="panel-heading">
            <div>
              <span class="terminal-symbol"><Icon :icon="terminalIcon" /></span>
              <h2>运行日志</h2>
              <span class="panel-subtitle">DEVICE STREAM</span
              ><span class="log-count">{{ visibleLogs.length }}</span>
            </div>
            <div class="log-tools">
              <label class="sr-only" for="log-level">日志级别筛选</label>
              <ASelect
                id="log-level"
                v-model:value="logLevel"
                :options="levelOptions"
                size="small"
                aria-label="日志级别筛选"
              /><AButton
                size="small"
                :aria-label="paused ? '继续日志滚动' : '暂停日志滚动'"
                :title="paused ? '继续' : '暂停'"
                @click="paused = !paused"
                ><Icon :icon="paused ? playIcon : pauseIcon" /></AButton
              ><AButton
                size="small"
                aria-label="清空当前日志显示"
                title="清空显示"
                @click="clearVisibleLogs"
                ><Icon :icon="trashIcon"
              /></AButton>
            </div>
          </div>
          <div
            ref="logStream"
            class="log-stream"
            role="log"
            :aria-live="paused ? 'off' : 'polite'"
            tabindex="0"
          >
            <div v-for="entry in visibleLogs" :key="logKey(entry)" class="log-line">
              <time>{{ logTime(entry) }}</time
              ><span :class="['log-level', `log-${String(entry.data.level)}`]">{{
                String(entry.data.level || 'info').toUpperCase()
              }}</span
              ><span>{{ entry.data.message }}</span>
            </div>
            <div v-if="!visibleLogs.length" class="log-empty">
              <span class="prompt-sign">›</span>
              {{
                state.connection.connected
                  ? '等待设备日志…'
                  : '连接设备或载入场景，开始观察运行日志。'
              }}
            </div>
          </div>
          <div class="log-status">
            <span>{{ paused ? '日志显示已暂停 · 后台仍接收' : '最多保留最近 200 条日志' }}</span
            ><span>{{ debug?.droppedLogs ?? 0 }} 条设备日志已丢弃</span>
          </div>
        </section>

        <footer class="app-footer">
          <div>
            <span class="footer-dot"></span><span>{{ firmwareVersion }}</span
            ><span class="footer-separator">/</span
            ><span>{{ state.connection.port || (isReplay ? 'REPLAY FIXTURE' : 'NO DEVICE') }}</span>
          </div>
          <div>
            <span>MQTT / 独立于网页调试</span><span class="footer-separator">·</span
            ><span>环境温湿度 / RS485</span><Icon :icon="arrowIcon" />
          </div>
        </footer>
      </main>
    </div>

    <AModal
      v-model:open="configOpen"
      title="设备配置"
      :width="600"
      :mask-closable="!configSaving"
      :closable="!configSaving"
      @cancel="form.PASSWORD = ''"
    >
      <p class="modal-description">
        读取设备当前配置。密码不会回传；留空将保留原密码。修改网络配置可能短暂断开
        Wi-Fi，失败时由设备回退。
      </p>
      <div v-if="configLoading" class="modal-loading">正在等待设备返回配置…</div>
      <form v-else class="config-form" @submit.prevent="reviewConfig">
        <div>
          <label for="wifi-ssid">Wi-Fi 名称</label
          ><AInput id="wifi-ssid" v-model:value="form.SSID" autocomplete="off" />
        </div>
        <div>
          <label for="wifi-password"
            >Wi-Fi 密码 <small>{{ passwordConfigured ? '已配置' : '未配置 / 未知' }}</small></label
          ><AInputPassword
            id="wifi-password"
            v-model:value="form.PASSWORD"
            autocomplete="new-password"
            placeholder="留空保留原密码"
          />
        </div>
        <div>
          <label for="ntp-server">NTP 服务器</label
          ><AInput id="ntp-server" v-model:value="form.NTP_SERVER" />
        </div>
        <div>
          <label for="timezone">时区偏移 / 小时</label
          ><AInputNumber id="timezone" v-model:value="form.TIMEZONE_HOURS" :min="-12" :max="14" />
        </div>
        <div>
          <label for="test-host">网络测试目标</label
          ><AInput id="test-host" v-model:value="form.NETWORK_TEST_HOST" />
        </div>
        <div>
          <label for="test-port">网络测试端口</label
          ><AInputNumber
            id="test-port"
            v-model:value="form.NETWORK_TEST_PORT"
            :min="1"
            :max="65535"
          />
        </div>
        <div>
          <label for="poll-interval">业务轮询间隔 / 毫秒</label
          ><AInputNumber
            id="poll-interval"
            v-model:value="form.POLL_INTERVAL_MS"
            :min="100"
            :max="60000"
          />
        </div>
        <div class="config-revision">
          配置版本 <strong class="mono">{{ configRevision }}</strong
          ><small>MQTT 参数在后续接入阶段启用</small>
        </div>
      </form>
      <p v-if="configError" class="inline-error" role="alert">{{ configError }}</p>
      <p v-if="!debug?.enabled" class="control-help">开启调试会话后才可保存配置。</p>
      <template #footer
        ><AButton :disabled="configSaving" @click="configOpen = false">关闭</AButton
        ><AButton
          type="primary"
          :disabled="configLoading || !configLoaded || !canMutate"
          @click="reviewConfig"
          >检查并保存</AButton
        ></template
      >
    </AModal>
    <AModal
      v-model:open="configConfirm"
      title="确认修改设备配置"
      ok-text="确认保存"
      cancel-text="返回检查"
      :confirm-loading="configSaving"
      :mask-closable="!configSaving"
      @ok="saveConfig"
      ><p>
        将新配置发送到当前{{ isReplay ? '模拟' : '真实' }}设备。Wi-Fi
        变更需要连接验证，请等待设备的最终回执。
      </p>
      <p class="control-help">新密码不会显示在日志和配置回读中。</p></AModal
    >
    <AModal
      v-model:open="restartOpen"
      title="确认重启设备"
      ok-text="确认重启"
      cancel-text="取消"
      :confirm-loading="pendingCount > 0"
      @ok="restart"
      ><p>
        设备将中断当前运行并重新启动，调试会话会结束。{{
          isReplay ? '当前为回放模式，不会重启实物。' : '请确认已保存需要保留的配置。'
        }}
      </p></AModal
    >
    <AModal
      :open="!!gpioConfirm"
      title="确认修改 GPIO 输出"
      ok-text="确认输出"
      cancel-text="取消"
      :confirm-loading="pendingCount > 0"
      @cancel="gpioConfirm = undefined"
      @ok="writePin"
      ><p>
        将 GPIO{{ gpioConfirm?.gpio }} 的程序设定值改为
        {{ gpioConfirm?.value }}。此操作仅对设备白名单中的输出脚开放。
      </p></AModal
    >
  </AConfigProvider>
</template>
