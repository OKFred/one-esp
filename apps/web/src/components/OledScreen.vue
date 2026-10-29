<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { decodeOledPixels } from '@one-esp/protocol';
import type { OledData } from '../types';

const props = defineProps<{ frame?: OledData; stale?: boolean }>();
const canvas = ref<HTMLCanvasElement>();
const decodeError = ref('');
const description = computed(
  () => props.frame?.text || (props.frame ? '设备像素画面' : '等待设备画面'),
);

function draw() {
  const context = canvas.value?.getContext('2d');
  if (!context) return;
  decodeError.value = '';
  const image = context.createImageData(128, 64);
  let pixels: Uint8Array = new Uint8Array(128 * 64);
  if (props.frame?.available) {
    try {
      pixels = decodeOledPixels(props.frame);
    } catch {
      decodeError.value = '画面数据不完整，等待下一帧';
    }
  }
  for (let index = 0; index < pixels.length; index++) {
    const lit = pixels[index] === 1;
    const yellow = Math.floor(index / 128) < 16;
    image.data[index * 4] = lit ? (yellow ? 235 : 76) : 3;
    image.data[index * 4 + 1] = lit ? (yellow ? 224 : 230) : 8;
    image.data[index * 4 + 2] = lit ? (yellow ? 73 : 244) : 10;
    image.data[index * 4 + 3] = 255;
  }
  context.imageSmoothingEnabled = false;
  context.putImageData(image, 0, 0);
}

onMounted(draw);
watch(() => props.frame, draw);
</script>

<template>
  <div class="oled-assembly" :class="{ 'oled-stale': stale }">
    <div class="oled-mount oled-mount-top"><i></i><span>SSD1306 · 0.96″ OLED</span><i></i></div>
    <div class="oled-glass">
      <canvas ref="canvas" width="128" height="64" role="img" :aria-label="description"></canvas>
      <div v-if="!frame" class="oled-waiting">
        <span>NO SIGNAL</span><small>连接设备，或载入场景回放</small>
      </div>
      <div v-else-if="!frame.available" class="oled-waiting">
        <span>DISPLAY UNAVAILABLE</span><small>设备尚未初始化显示屏</small>
      </div>
    </div>
    <div class="oled-mount oled-mount-bottom"><i></i><span>128 × 64 / MONO_VLSB</span><i></i></div>
  </div>
  <p class="oled-caption" aria-live="polite">
    <span v-if="decodeError" class="tone-warning">{{ decodeError }}</span>
    <span v-else-if="stale && frame">最后一帧 · 当前数据已过期</span>
    <span v-else>同步固件帧缓冲 · 像素原样显示</span>
  </p>
  <details v-if="frame?.text" class="screen-text">
    <summary>屏幕文本</summary>
    <pre>{{ frame.text }}</pre>
  </details>
</template>
