<script setup lang="ts">
/**
 * 指标卡网格：路由页与告警页共用。
 *
 * 只做展示（label / value / hint / 语义色），不做请求——数据由页面自己算好传进来。
 */
import type { StatCardItem } from './meta'

defineProps<{ items: StatCardItem[] }>()
</script>

<template>
  <div class="stat-grid">
    <div
      v-for="item in items"
      :key="item.label"
      class="stat-card"
      :class="`tone-${item.tone || 'normal'}`"
    >
      <div class="stat-value">{{ item.value }}</div>
      <div class="stat-label">{{ item.label }}</div>
      <div v-if="item.hint" class="stat-hint">{{ item.hint }}</div>
    </div>
  </div>
</template>

<style scoped lang="less">
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
  gap: var(--space-3);
}

.stat-card {
  padding: var(--space-3) var(--space-4);
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);

  &.tone-ok .stat-value {
    color: var(--color-success);
  }
  &.tone-warn .stat-value {
    color: var(--color-warning);
  }
  &.tone-error .stat-value {
    color: var(--color-error);
  }
}

.stat-value {
  font-size: var(--fs-lg);
  font-weight: 600;
  line-height: 1.25;
  color: var(--color-text-1);
  font-variant-numeric: tabular-nums;
}

.stat-label {
  margin-top: 2px;
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}

.stat-hint {
  margin-top: 2px;
  font-size: 11px;
  color: var(--color-text-3);
}

@media (max-width: 900px) {
  .stat-grid {
    grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  }
}
</style>