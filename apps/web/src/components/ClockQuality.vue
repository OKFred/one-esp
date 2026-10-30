<script setup lang="ts">
import type { StatusData } from '../types';
defineProps<{ clock: NonNullable<StatusData['clock']> }>();
</script>

<template>
  <p
    class="clock-quality"
    :class="{ 'tone-warning': !clock.withinTarget || clock.withinTargetAtNextSync === false }"
    aria-label="时间校准状态"
  >
    <span v-if="!clock.synced">等待网络校时</span>
    <span v-else-if="clock.estimatedErrorMs !== null">
      估计误差 ≤ {{ Math.ceil(clock.estimatedErrorMs) }} ms
      <span v-if="!clock.withinTarget"> · 精度待恢复</span>
    </span>
    <span v-else>校时精度未知</span>
    <span> · 目标 {{ clock.targetMs }} ms</span>
    <span v-if="clock.withinTarget && clock.withinTargetAtNextSync === false">
      · 当前漂移可能使下次校准前超出目标
    </span>
    <span v-if="clock.lastSyncAgeMs !== null">
      · {{ Math.floor(clock.lastSyncAgeMs / 1000) }} 秒前校准
    </span>
  </p>
</template>
