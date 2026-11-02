<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import {
  Modal as AModal,
  Button as AButton,
  Input as AInput,
  InputPassword as AInputPassword,
  InputNumber as AInputNumber,
  Select as ASelect,
  Switch as ASwitch,
  Tabs as ATabs,
  TabPane as ATabPane,
} from 'ant-design-vue';
import type { DeviceCommand } from '@one-esp/protocol';
import type { AckData } from '../types';
import {
  createMqttForm,
  createSensorForm,
  readMqttForm,
  readSensorForm,
  mqttValuesForSave,
  sensorValuesForSave,
} from '../device-settings';

const props = defineProps<{
  open: boolean;
  connected: boolean;
  identity: string;
  remote?: { configured?: boolean; active?: boolean; state?: string };
  command: (name: DeviceCommand['command'], args?: Record<string, unknown>) => Promise<AckData>;
}>();
const emit = defineEmits<{
  'update:open': [value: boolean];
  applied: [domain: 'mqtt' | 'sensor'];
}>();
const mqtt = reactive(createMqttForm());
const sensor = reactive(createSensorForm());
const revisions = reactive({ mqtt: 0, sensor: 0 });
const loaded = reactive({ mqtt: false, sensor: false });
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const notice = ref('');
const passwordConfigured = ref(false);
const ca = ref<{ sha256?: string; bytes?: number } | null>(null);
let generation = 0;
const remoteLabel = computed(() =>
  props.remote?.state === 'denied'
    ? '订阅被 broker 拒绝，请检查该设备主题权限'
    : props.remote?.state === 'ready' && props.remote.active
      ? '已订阅设备专属命令主题'
      : props.remote?.configured
        ? '等待连接或订阅确认'
        : '未启用或尚未收到设备状态',
);
const protocolOptions = [
  { label: 'ASCII READ（无 CRC）', value: 'ascii_read' },
  { label: 'SHT20 · Modbus RTU（CRC16）', value: 'sht20_rtu' },
];
const baudOptions = [9600, 14400, 19200].map((value) => ({ label: String(value), value }));
const identityFields = [
  ['client_id', '客户端 ID'],
  ['site_id', '站点 ID'],
  ['gateway_id', '网关 ID'],
  ['device_id', '设备 ID'],
] as const;

function hydrate(domain: 'mqtt' | 'sensor', result: Record<string, unknown>) {
  const revision = Number(result.revision);
  if (!Number.isInteger(revision) || revision < 0 || !result.values)
    throw new Error('设备没有返回完整配置，请确认固件已更新');
  revisions[domain] = revision;
  if (domain === 'mqtt') {
    Object.assign(mqtt, readMqttForm(result));
    passwordConfigured.value =
      (result.secrets as Record<string, unknown> | undefined)?.password === true;
    ca.value = result.ca && typeof result.ca === 'object' ? (result.ca as typeof ca.value) : null;
  } else Object.assign(sensor, readSensorForm(result));
  loaded[domain] = true;
}
async function load() {
  const token = ++generation;
  saving.value = false;
  loading.value = false;
  loaded.mqtt = loaded.sensor = false;
  mqtt.password = '';
  error.value = '';
  notice.value = '';
  if (!props.connected || !props.open) return;
  loading.value = true;
  try {
    const [m, s] = await Promise.all([
      props.command('mqtt.config.get'),
      props.command('sensor.config.get'),
    ]);
    if (token !== generation) return;
    hydrate('mqtt', m.result ?? {});
    hydrate('sensor', s.result ?? {});
  } catch (cause) {
    if (token === generation)
      error.value = cause instanceof Error ? cause.message : '读取通信配置失败';
  } finally {
    if (token === generation) loading.value = false;
  }
}
async function save(domain: 'mqtt' | 'sensor') {
  const token = generation;
  error.value = '';
  notice.value = '';
  saving.value = true;
  try {
    const v =
      domain === 'mqtt'
        ? mqttValuesForSave(mqtt, passwordConfigured.value)
        : sensorValuesForSave(sensor);
    const ack = await props.command(domain === 'mqtt' ? 'mqtt.config.set' : 'sensor.config.set', {
      revision: revisions[domain],
      values: v,
    });
    if (token !== generation) return;
    if (ack.result?.values) hydrate(domain, ack.result);
    else {
      const current = await props.command(
        domain === 'mqtt' ? 'mqtt.config.get' : 'sensor.config.get',
      );
      if (token !== generation) return;
      hydrate(domain, current.result ?? {});
    }
    mqtt.password = '';
    notice.value = domain === 'mqtt' ? 'MQTT 配置已应用' : '采集配置已应用';
    emit('applied', domain);
  } catch (cause) {
    if (token === generation) {
      error.value = cause instanceof Error ? cause.message : '保存结果未确认，请重新读取实际配置';
      loaded[domain] = false;
    }
  } finally {
    if (token === generation) saving.value = false;
  }
}
watch(
  () => [props.open, props.connected, props.identity],
  () => {
    if (props.open) void load();
    else {
      ++generation;
      mqtt.password = '';
      loading.value = saving.value = false;
    }
  },
  { immediate: true },
);
</script>

<template>
  <AModal
    :open="open"
    title="MQTT 与 RS485 采集"
    :footer="null"
    :width="680"
    :mask-closable="!saving"
    :closable="!saving"
    @cancel="emit('update:open', false)"
  >
    <div class="device-settings">
      <p class="hint">
        配置保存在设备上。采集与上报独立于网页调试开关；修改连接参数会测试后应用，失败则回退。
      </p>
      <p v-if="!connected" role="status">请先连接真实设备。</p>
      <p v-if="loading" role="status">正在读取设备配置…</p>
      <p v-if="error" class="settings-error" role="alert">{{ error }}</p>
      <p v-if="notice" class="settings-notice" role="status">{{ notice }}</p>
      <AButton v-if="error && connected" :disabled="saving || loading" @click="load"
        >重新读取配置</AButton
      >
      <ATabs default-active-key="mqtt">
        <ATabPane key="mqtt" tab="MQTT 上报">
          <form class="settings-fields" @submit.prevent="save('mqtt')">
            <label class="switch-field"
              >启用 MQTT 上报
              <ASwitch v-model:checked="mqtt.enabled" :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >服务器
              <AInput
                v-model:value="mqtt.host"
                placeholder="broker.example.com"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >端口（TLS）
              <AInputNumber
                v-model:value="mqtt.port"
                :min="1"
                :max="65535"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >账号
              <AInput
                v-model:value="mqtt.username"
                autocomplete="off"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >密码 <small>{{ passwordConfigured ? '已配置 · 留空保留' : '尚未配置' }}</small>
              <AInputPassword
                v-model:value="mqtt.password"
                autocomplete="new-password"
                placeholder="留空保留现有密码"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >上报周期（秒）
              <AInputNumber
                v-model:value="mqtt.interval_seconds"
                :min="60"
                :max="86400"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label
              >保活间隔（秒）
              <AInputNumber
                v-model:value="mqtt.keepalive"
                :min="15"
                :max="300"
                :disabled="!loaded.mqtt || saving"
            /></label>
            <label class="switch-field wide"
              >允许 MQTT 下发 RS485 设置
              <ASwitch v-model:checked="mqtt.commands_enabled" :disabled="!loaded.mqtt || saving"
            /></label>
            <p class="hint wide">
              下发仅接收此设备专属命令主题中的采集配置，需要 broker 允许订阅和回执发布。
            </p>
            <p class="hint wide" aria-label="MQTT 下行状态">最近设备回报：{{ remoteLabel }}</p>
            <details class="wide advanced">
              <summary>设备身份与 TLS</summary>
              <div class="settings-fields">
                <label v-for="[key, label] in identityFields" :key="key"
                  >{{ label }} <AInput v-model:value="mqtt[key]" :disabled="!loaded.mqtt || saving"
                /></label>
                <label class="wide"
                  >TLS 主机名（留空跟随服务器）
                  <AInput v-model:value="mqtt.server_hostname" :disabled="!loaded.mqtt || saving"
                /></label>
                <label class="wide"
                  >固定 IPv4（可选，留空使用 DNS）
                  <AInput v-model:value="mqtt.broker_ip" :disabled="!loaded.mqtt || saving"
                /></label>
                <p class="hint wide">证书验证始终开启，沿用设备现有根证书。</p>
                <p v-if="ca" class="fingerprint wide">
                  CA SHA256 · {{ ca.sha256 }} <small>({{ ca.bytes }} bytes)</small>
                </p>
                <p v-else class="hint wide">设备尚未提供 CA 信息。</p>
              </div>
            </details>
            <div class="settings-actions wide">
              <small>配置版本 {{ revisions.mqtt }}</small
              ><AButton
                type="primary"
                html-type="submit"
                :loading="saving"
                :disabled="!connected || !loaded.mqtt || loading || saving"
                >测试并保存 MQTT</AButton
              >
            </div>
          </form>
        </ATabPane>
        <ATabPane key="sensor" tab="RS485 采集">
          <form class="settings-fields" @submit.prevent="save('sensor')">
            <label class="switch-field wide"
              >启用温湿度采集
              <ASwitch v-model:checked="sensor.enabled" :disabled="!loaded.sensor || saving"
            /></label>
            <label class="wide"
              >通信协议
              <ASelect
                v-model:value="sensor.protocol"
                :options="protocolOptions"
                :disabled="!loaded.sensor || saving"
            /></label>
            <label
              >采集周期（秒）
              <AInputNumber
                v-model:value="sensor.interval_seconds"
                :min="5"
                :max="3600"
                :disabled="!loaded.sensor || saving"
            /></label>
            <label
              >波特率
              <ASelect
                v-model:value="sensor.baudrate"
                :options="baudOptions"
                :disabled="!loaded.sensor || saving"
            /></label>
            <label v-if="sensor.protocol === 'sht20_rtu'"
              >从站地址
              <AInputNumber
                v-model:value="sensor.slave_address"
                :min="1"
                :max="247"
                :disabled="!loaded.sensor || saving"
            /></label>
            <label
              >响应超时（毫秒）
              <AInputNumber
                v-model:value="sensor.timeout_ms"
                :min="200"
                :max="5000"
                :disabled="!loaded.sensor || saving"
            /></label>
            <p class="hint wide">UART1 · RX14 / TX15 / DE13 · 8N1。这里只修改 ESP32 的采集设置。</p>
            <p class="integrity wide">
              {{
                sensor.protocol === 'sht20_rtu'
                  ? '强制 CRC16 校验 · 功能码 04 · 温度寄存器 0x0001、湿度 0x0002 · 原始值 ÷10'
                  : 'ASCII READ 协议没有 CRC 校验字节；仅验证报文格式和数值范围。'
              }}
            </p>
            <div class="settings-actions wide">
              <small>配置版本 {{ revisions.sensor }}</small
              ><AButton
                type="primary"
                html-type="submit"
                :loading="saving"
                :disabled="!connected || !loaded.sensor || loading || saving"
                >测试并保存采集设置</AButton
              >
            </div>
          </form>
        </ATabPane>
      </ATabs>
    </div>
  </AModal>
</template>

<style scoped>
.device-settings {
  color: #c9d8df;
}
.hint {
  color: #94aab7;
  font-size: 12px;
  line-height: 1.8;
}
.settings-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.settings-fields label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  font-size: 12px;
}
.settings-fields :deep(.ant-input-number),
.settings-fields :deep(.ant-select) {
  width: 100%;
}
.settings-fields .switch-field {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
}
.wide {
  grid-column: 1 / -1;
}
.advanced summary {
  cursor: pointer;
  margin-bottom: 16px;
  font-size: 12px;
}
.fingerprint {
  font: 11px monospace;
  overflow-wrap: anywhere;
}
.integrity {
  margin: 0;
  padding: 12px;
  background: #142530;
  font-size: 12px;
  line-height: 1.8;
}
.settings-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.settings-error {
  color: #f0a594;
  overflow-wrap: anywhere;
}
.settings-notice {
  color: #8fd6bb;
}
@media (max-width: 520px) {
  .settings-fields {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
