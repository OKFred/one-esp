<script setup lang="ts">
import type { MetricsData } from '@one-esp/protocol';

const props = defineProps<{
  metrics?: MetricsData;
  active: boolean;
  connected: boolean;
  uptimeMs?: number;
  tcpMs?: number | null;
  tcpOk?: boolean | null;
}>();

function bytes(value: number | null | undefined) {
  if (value === null || value === undefined) return '未提供';
  return value >= 1024 * 1024
    ? `${(value / (1024 * 1024)).toFixed(2)} MiB`
    : `${(value / 1024).toFixed(1)} KiB`;
}
function age(sampledAtMs: number | undefined) {
  if (sampledAtMs === undefined || props.uptimeMs === undefined) return '采样时间未知';
  return `${Math.floor(Math.max(0, props.uptimeMs - sampledAtMs) / 1000)} 秒前采样`;
}
</script>

<template>
  <section class="panel metrics-panel" aria-label="设备资源指标">
    <div class="panel-heading metrics-heading">
      <div>
        <h2>设备资源</h2>
        <span class="panel-subtitle">SYSTEM METRICS</span>
      </div>
      <span class="metrics-freshness" :class="{ 'tone-warning': !active || !connected }">
        {{ !connected ? '设备未连接' : !active ? '采集已暂停' : '低频采集中' }}
        <template v-if="metrics"> · {{ age(metrics.sampledAtMs) }}</template>
      </span>
    </div>
    <p v-if="!metrics" class="metrics-empty">尚无资源指标，开启调试或获取快照后查看。</p>
    <template v-else>
      <div class="metrics-grid">
        <div class="metric-item">
          <span class="metric-label">CPU 频率</span>
          <strong>{{
            metrics.cpuHz === null ? '未提供' : `${metrics.cpuHz / 1_000_000} MHz`
          }}</strong>
          <small>CPU 占用率：固件未提供</small>
        </div>
        <div class="metric-item">
          <span class="metric-label">Python 可用内存</span>
          <strong>{{ bytes(metrics.memory.freeBytes) }}</strong>
          <small>已分配 {{ bytes(metrics.memory.allocatedBytes) }}</small>
          <small>可用预算 {{ bytes(metrics.memory.totalBytes) }}</small>
        </div>
        <div class="metric-item">
          <span class="metric-label">ESP-IDF 可用堆</span>
          <strong>{{ bytes(metrics.idfHeap?.freeBytes) }}</strong>
          <small>最大连续块 {{ bytes(metrics.idfHeap?.largestBlockBytes) }}</small>
          <small>总容量 {{ bytes(metrics.idfHeap?.totalBytes) }}</small>
        </div>
        <div class="metric-item">
          <span class="metric-label">文件系统可用空间</span>
          <strong>{{ bytes(metrics.storage?.freeBytes) }}</strong>
          <small>
            已用
            {{
              metrics.storage
                ? bytes(Math.max(0, metrics.storage.totalBytes - metrics.storage.freeBytes))
                : '未提供'
            }}
            / 共 {{ bytes(metrics.storage?.totalBytes) }}
          </small>
          <small
            >物理 Flash {{ bytes(metrics.flashBytes) }} ·
            {{ age(metrics.storage?.sampledAtMs) }}</small
          >
        </div>
        <div class="metric-item">
          <span class="metric-label">芯片温度</span>
          <strong>{{
            metrics.temperature ? `${metrics.temperature.celsius.toFixed(1)} °C` : '未提供'
          }}</strong>
          <small>芯片内部参考值，非环境温度</small>
          <small>{{ age(metrics.temperature?.sampledAtMs) }}</small>
        </div>
        <div class="metric-item">
          <span class="metric-label">Wi-Fi 信号</span>
          <strong>{{ metrics.rssiDbm === null ? '未提供' : `${metrics.rssiDbm} dBm` }}</strong>
          <small
            >TCP 连接：{{
              tcpOk === false
                ? '失败'
                : tcpMs === null || tcpMs === undefined
                  ? '未提供'
                  : `${tcpMs} ms`
            }}</small
          >
          <small>Wi-Fi 吞吐率：固件未提供</small>
        </div>
      </div>
      <p class="metrics-note">
        核心指标 {{ metrics.intervalMs / 1000 }} 秒 · 温度 30 秒 · 空间 60 秒；Python 与 ESP-IDF
        堆有重叠，不相加。
        <span v-if="metrics.sampleDurationUs !== null"
          >本次读取 {{ (metrics.sampleDurationUs / 1000).toFixed(2) }} ms。</span
        >
      </p>
    </template>
  </section>
</template>

<style scoped>
.metrics-panel {
  margin-bottom: 20px;
}
.metrics-heading {
  flex-wrap: wrap;
  padding-top: 12px;
  padding-bottom: 12px;
}
.metrics-freshness {
  font-size: 11px;
  color: #7ca497;
  overflow-wrap: anywhere;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.metric-item {
  min-width: 0;
  padding: 16px 18px;
  border-bottom: 1px solid #27343d;
}
.metric-label {
  display: block;
  font-size: 11px;
  color: #839aa6;
}
.metric-item strong {
  display: block;
  margin: 8px 0;
  font-size: 18px;
  color: #cce4e7;
  font-variant-numeric: tabular-nums;
}
.metric-item small {
  display: block;
  color: #7d929f;
  font-size: 10px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.metrics-note,
.metrics-empty {
  margin: 0;
  padding: 12px 18px;
  color: #8297a2;
  font-size: 11px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .metric-item {
    padding: 14px;
  }
  .metrics-heading {
    gap: 8px;
  }
}
</style>
