<script setup lang="ts">
/**
 * 空态块：网关未连接 / 未启用 / 后端不可达时用它代替白屏。
 * 默认图标是网关图标，右侧默认插槽放「重试」等操作。
 */
import type { Component } from 'vue'
import { ApiOutlined } from '@ant-design/icons-vue'

defineProps<{
  title: string
  desc?: string
  icon?: Component
}>()
</script>

<template>
  <div class="gateway-empty">
    <!-- 图标不能写成 defineProps 的默认值：Vue 会把函数型默认值当工厂调用，导致组件渲染崩溃 -->
    <component :is="icon || ApiOutlined" class="empty-icon" />
    <p class="empty-title">{{ title }}</p>
    <p v-if="desc" class="empty-desc">{{ desc }}</p>
    <div class="empty-actions">
      <slot />
    </div>
  </div>
</template>

<style scoped lang="less">
.gateway-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 48px var(--space-4);
  background: var(--color-bg-card);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-lg);
  text-align: center;
}

.empty-icon {
  font-size: 28px;
  color: var(--color-text-3);
  margin-bottom: var(--space-2);
}

.empty-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-1);
}

.empty-desc {
  margin: 0;
  max-width: 620px;
  font-size: var(--fs-xs);
  line-height: 1.8;
  color: var(--color-text-2);
}

.empty-actions {
  margin-top: var(--space-3);
}
</style>