<script setup lang="ts">
import { computed } from 'vue';
import type { EnvironmentData } from '@one-esp/protocol';
import type { MqttStatusData } from '../types';

const props = defineProps<{
  environment?: EnvironmentData;
  mqtt?: MqttStatusData;
  connected: boolean;
  telemetryActive: boolean;
  deviceStale: boolean;
  uptimeMs?: number;
  statusUptimeMs?: number;
}>();

const statusAgeMs = computed(() =>
  props.uptimeMs === undefined || props.statusUptimeMs === undefined
    ? null
    : Math.max(0, props.uptimeMs - props.statusUptimeMs),
);
const ageMs = computed(() => {
  const reading = props.environment;
  if (!reading) return null;
  const reported = reading.ageMs === null ? null : reading.ageMs + (statusAgeMs.value ?? 0);
  const fromUptime =
    reading.readAtMs === null || props.uptimeMs === undefined
      ? null
      : Math.max(0, props.uptimeMs - reading.readAtMs);
  if (reported === null) return fromUptime;
  return fromUptime === null ? reported : Math.max(reported, fromUptime);
});
const hasReading = computed(
  () =>
    Number.isFinite(props.environment?.temperatureC) &&
    Number.isFinite(props.environment?.relativeHumidityPercent),
);
const readingStale = computed(
  () =>
    props.environment?.stale ||
    ageMs.value === null ||
    ageMs.value > Math.max(5000, (props.environment?.intervalMs ?? 60000) * 2),
);
const freshness = computed(() => {
  if (!props.environment?.enabled) return '未启用';
  if (!props.connected) return '设备未连接 · 最后读数';
  if (!props.telemetryActive) return '网页更新已暂停 · 最后读数';
  if (props.deviceStale || statusAgeMs.value === null || statusAgeMs.value > 10000)
    return '遥测更新中断 · 最后读数';
  if (props.environment.lastError) return '最近读取失败 · 等待重试';
  if (!hasReading.value) return '等待首次读取';
  if (readingStale.value) return '读数已过期 · 等待新数据';
  return '定时采集中';
});
const healthy = computed(() => freshness.value === '定时采集中');
const mqttCached = computed(
  () =>
    !props.connected ||
    !props.telemetryActive ||
    props.deviceStale ||
    statusAgeMs.value === null ||
    statusAgeMs.value > 10000,
);
const mqttDelivery = computed(() => {
  const mqtt = props.mqtt;
  if (!mqtt) return 'MQTT状态未提供';
  let label: string;
  if (mqtt.enabled === false || mqtt.state === 'disabled') {
    label = '未开启MQTT';
  } else if (
    mqtt.state === 'waiting_network' ||
    mqtt.state === 'waiting_clock' ||
    mqtt.transport?.phase === 'waiting_network' ||
    mqtt.transport?.phase === 'waiting_clock'
  ) {
    label = 'MQTT 等待网络或时钟';
  } else if (mqtt.state === 'configuration_error') {
    label = 'MQTT 配置异常';
  } else if (mqtt.transport?.connected === true) {
    const daily = Object.prototype.hasOwnProperty.call(mqtt, 'dailyAcknowledged');
    const count = daily ? mqtt.dailyAcknowledged : mqtt.acknowledged;
    const knownCount = typeof count === 'number' && Number.isSafeInteger(count) && count >= 0;
    const confirmation = knownCount
      ? `${daily ? '今日' : '本次启动'}已确认 ${count} 条`
      : daily
        ? '今日确认数未提供'
        : '确认数未提供';
    label = `MQTT 已连接 · ${confirmation}`;
    if (mqtt.pending === true) label += ' · 等待确认';
  } else if (mqtt.enabled === true || mqtt.state || mqtt.transport?.phase) {
    label = `MQTT 重连中${mqtt.pending === true ? ' · 待发送' : ''}`;
  } else {
    return 'MQTT状态未提供';
  }
  return mqttCached.value ? `上次MQTT状态：${label}` : label;
});
const mqttWarning = computed(() => mqttCached.value || props.mqtt?.transport?.connected !== true);
const ageLabel = computed(() =>
  ageMs.value === null ? '读取时间未知' : `${Math.floor(ageMs.value / 1000)} 秒前读取`,
);
function value(number: number | null | undefined) {
  return number === null || number === undefined || !Number.isFinite(number)
    ? '—'
    : number.toFixed(1);
}
</script>

<template>
  <section class="panel ambient-panel" aria-label="环境温湿度">
    <div class="panel-heading ambient-heading">
      <div>
        <h2>环境温湿度</h2>
        <span class="panel-subtitle">RS485 SENSOR</span>
      </div>
      <span class="ambient-freshness" :class="{ 'tone-warning': !healthy }">
        {{ freshness }}
      </span>
    </div>
    <div class="ambient-grid">
      <div>
        <span>环境温度</span>
        <strong>{{ value(environment?.temperatureC) }} <small>°C</small></strong>
      </div>
      <div>
        <span>相对湿度</span>
        <strong>{{ value(environment?.relativeHumidityPercent) }} <small>%RH</small></strong>
      </div>
    </div>
    <p class="ambient-delivery" :class="{ 'tone-warning': mqttWarning }" aria-label="MQTT 投递状态">
      {{ mqttDelivery }}
    </p>
    <p class="ambient-note">
      <template v-if="environment?.enabled">
        每 {{ environment.intervalMs / 1000 }} 秒读取 · {{ ageLabel }}。
      </template>
      <template v-else>设备尚未启用环境传感器。</template>
      环境温度与下方芯片内部温度分别显示；设备采样和 MQTT 上报独立于网页调试开关。
    </p>
    <p class="ambient-note" aria-label="采集协议与校验">
      {{
        environment?.protocol === 'sht20_rtu'
          ? 'SHT20 Modbus RTU'
          : environment?.protocol === 'ascii_read'
            ? 'ASCII READ'
            : '协议待设备报告'
      }}
      ·
      {{
        environment?.protocol === 'sht20_rtu' && environment?.integrity === 'crc_verified'
          ? '最近报文 CRC16 校验通过'
          : environment?.protocol === 'ascii_read'
            ? '协议未提供 CRC · 仅格式/范围校验'
            : '校验结果待确认'
      }}
      <template v-if="environment?.crcFailures !== undefined">
        · CRC 错误 {{ environment.crcFailures }}</template
      >
    </p>
    <details v-if="environment?.lastFrameHex" class="ambient-note">
      <summary>最近原始报文（十六进制）</summary>
      <code class="raw-frame">{{ environment.lastFrameHex }}</code>
    </details>
  </section>
</template>

<style scoped>
.raw-frame {
  overflow-wrap: anywhere;
  white-space: normal;
}
.ambient-panel {
  margin-bottom: 20px;
}
.ambient-heading {
  flex-wrap: wrap;
  gap: 10px;
  padding-top: 12px;
  padding-bottom: 12px;
}
.ambient-freshness {
  color: #7ca497;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.ambient-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.ambient-grid > div {
  min-width: 0;
  padding: 16px 18px;
}
.ambient-grid > div + div {
  border-left: 1px solid #27343d;
}
.ambient-grid span {
  color: #839aa6;
  font-size: 11px;
}
.ambient-grid strong {
  display: block;
  margin-top: 8px;
  color: #cce4e7;
  font-size: clamp(20px, 3vw, 28px);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.ambient-grid small {
  color: #839aa6;
  font-size: 12px;
  font-weight: normal;
}
.ambient-note {
  margin: 0;
  padding: 12px 18px;
  border-top: 1px solid #27343d;
  color: #8297a2;
  font-size: 11px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.ambient-delivery {
  margin: 0;
  padding: 10px 18px;
  border-top: 1px solid #27343d;
  color: #7ca497;
  font-size: 11px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.ambient-delivery.tone-warning {
  color: #e1bc70;
}
@media (max-width: 360px) {
  .ambient-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .ambient-grid > div + div {
    border-top: 1px solid #27343d;
    border-left: 0;
  }
}
</style>
