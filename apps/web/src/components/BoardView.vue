<script setup lang="ts">
import { computed } from 'vue';
import type { PinData } from '../types';

const props = defineProps<{ pins: PinData[]; selected: number | null; screenText?: string }>();
const emit = defineEmits<{ select: [gpio: number] }>();
const byGpio = computed(() => new Map(props.pins.map((pin) => [pin.gpio, pin])));
const left = [36, 25, 26, 8, 11, 7, 6, '3V3', 'GND', '5V'] as const;
const right = [39, 16, 5, 4, 0, 2, 14, 12, 13, 15, 3, 1, '3V3', 'GND', 'GND', '5V'] as const;
const lines = computed(() =>
  (props.screenText || 'one-esp\nWAITING FOR DEVICE').split('\n').slice(0, 4),
);
function mode(gpio: number | string) {
  if (typeof gpio !== 'number') return 'power';
  return byGpio.value.get(gpio)?.mode || 'unknown';
}
function title(gpio: number | string) {
  if (typeof gpio !== 'number') return `${gpio} · 电源标识，非实时测量`;
  const pin = byGpio.value.get(gpio);
  return `GPIO${gpio} · ${pin?.owner || pin?.reason || '状态未知'}${pin?.value !== null && pin?.value !== undefined ? ` · ${pin.value}` : ''}`;
}
function activate(gpio: number | string) {
  if (typeof gpio === 'number') emit('select', gpio);
}
</script>

<template>
  <svg
    class="board-svg"
    viewBox="0 0 344 482"
    role="img"
    aria-label="ESP32 带 OLED 开发板引脚参考图"
  >
    <defs>
      <pattern id="pcb-grid" width="10" height="10" patternUnits="userSpaceOnUse">
        <circle cx="1" cy="1" r="0.5" fill="#253d3c" />
      </pattern>
    </defs>
    <rect x="82" y="10" width="180" height="460" rx="13" fill="#162b2b" stroke="#36554e" />
    <rect x="83" y="11" width="178" height="458" rx="12" fill="url(#pcb-grid)" />
    <circle v-for="y in [24, 455]" :key="y" cx="95" :cy="y" r="5" fill="#080d11" stroke="#66756d" />
    <circle
      v-for="y in [24, 455]"
      :key="`r${y}`"
      cx="249"
      :cy="y"
      r="5"
      fill="#080d11"
      stroke="#66756d"
    />
    <path
      d="M137 55V30h15v25h13V30h15v25h13V30h14v37"
      fill="none"
      stroke="#d1ae5d"
      stroke-width="4"
    />
    <rect x="106" y="93" width="132" height="150" rx="3" fill="#73817d" stroke="#a0aaa1" />
    <path d="M111 98h122v140H111z" fill="none" stroke="#94a298" stroke-width="1" />
    <text
      x="172"
      y="142"
      text-anchor="middle"
      fill="#33443e"
      font-size="16"
      font-weight="700"
      font-family="monospace"
    >
      ESP32
    </text>
    <text
      x="172"
      y="164"
      text-anchor="middle"
      fill="#33443e"
      font-size="10"
      font-family="monospace"
    >
      WROOM-32
    </text>
    <text x="172" y="210" text-anchor="middle" fill="#43574c" font-size="9" font-family="monospace">
      Wi-Fi · Bluetooth
    </text>
    <rect x="101" y="279" width="142" height="97" rx="3" fill="#0c1725" stroke="#4a6971" />
    <rect x="109" y="287" width="126" height="79" fill="#03090c" />
    <text
      v-for="(line, i) in lines"
      :key="i"
      x="115"
      :y="302 + i * 17"
      :fill="i === 0 ? '#d9d96b' : '#58d7df'"
      font-size="8"
      font-family="monospace"
    >
      {{ line.slice(0, 18) }}
    </text>
    <rect x="144" y="416" width="56" height="24" rx="3" fill="#4f6667" stroke="#97a8a1" />
    <rect x="153" y="420" width="38" height="13" rx="2" fill="#101b20" />
    <text x="172" y="398" fill="#97aaa4" font-size="8" text-anchor="middle" font-family="monospace">
      0.96″ OLED
    </text>
    <g
      v-for="(pin, index) in left"
      :key="`l${index}`"
      :class="['board-pin', `pin-${mode(pin)}`, { 'pin-selected': selected === pin }]"
      :role="typeof pin === 'number' ? 'button' : undefined"
      :tabindex="typeof pin === 'number' ? 0 : undefined"
      :aria-label="title(pin)"
      @click="activate(pin)"
      @keydown.enter="activate(pin)"
      @keydown.space.prevent="activate(pin)"
    >
      <title>{{ title(pin) }}</title>
      <rect x="65" :y="79 + index * 22" width="27" height="9" rx="1" />
      <circle cx="87" :cy="83.5 + index * 22" r="2" fill="#0b1418" />
      <text x="56" :y="87 + index * 22" text-anchor="end">
        {{ typeof pin === 'number' ? `IO${pin}` : pin }}
      </text>
    </g>
    <g
      v-for="(pin, index) in right"
      :key="`r${index}`"
      :class="['board-pin', `pin-${mode(pin)}`, { 'pin-selected': selected === pin }]"
      :role="typeof pin === 'number' ? 'button' : undefined"
      :tabindex="typeof pin === 'number' ? 0 : undefined"
      :aria-label="title(pin)"
      @click="activate(pin)"
      @keydown.enter="activate(pin)"
      @keydown.space.prevent="activate(pin)"
    >
      <title>{{ title(pin) }}</title>
      <rect x="252" :y="79 + index * 22" width="27" height="9" rx="1" />
      <circle cx="257" :cy="83.5 + index * 22" r="2" fill="#0b1418" />
      <text x="288" :y="87 + index * 22">{{ typeof pin === 'number' ? `IO${pin}` : pin }}</text>
    </g>
  </svg>
  <div class="pin-legend">
    <span><i class="legend-i2c"></i>OLED</span><span><i class="legend-uart"></i>串口</span
    ><span><i class="legend-reserved"></i>保留</span><span><i class="legend-unknown"></i>未知</span>
  </div>
  <p class="board-footnote">
    按商品引脚图绘制 · 电源标签不是电压测量<br />GPIO13 / 14 / 15 为传感器后续计划，当前断开
  </p>
</template>
