<script setup lang="ts">
/**
 * 网关状态条：地址 / 健康状态 / 版本 / uptime / 配置指纹 + 刷新与自动刷新开关。
 *
 * 只负责展示和把用户操作 emit 出去；请求与轮询都由 RoutingView 统一管理，
 * 这样自动刷新关掉时不会残留定时器。
 */
import { computed } from 'vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { fmtUptime, shortHash } from './meta'
import type { GatewayHealth, Overview } from '@/types/gateway'

const props = defineProps<{
  health: GatewayHealth | null
  overview: Overview | null
  autoMs: number
  loading?: boolean
}>()

const emit = defineEmits<{
  refresh: []
  'auto-change': [value: number]
}>()

/** 状态灯：idle=未知/未启用，ok=已连接，down=未连接 */
const stateTone = computed<'idle' | 'ok' | 'down'>(() => {
  const h = props.health
  if (!h) return 'idle'
  if (!h.enabled) return 'idle'
  return h.reachable ? 'ok' : 'down'
})

const stateText = computed(() => {
  const h = props.health
  if (!h) return '网关状态未知'
  if (!h.enabled) return '网关管理未启用'
  return h.reachable ? '网关已连接' : '网关未连接'
})

const version = computed(() => props.overview?.version || props.health?.version || '-')
const uptime = computed(() =>
  fmtUptime(props.overview?.uptime_sec ?? props.health?.uptime_sec ?? null),
)
const configHash = computed(() => props.overview?.config_hash || '')

const AUTO_OPTIONS = [
  { value: 0, label: '自动刷新：关闭' },
  { value: 5000, label: '自动刷新：5 秒' },
  { value: 15000, label: '自动刷新：15 秒' },
]

function onAutoChange(value: unknown) {
  emit('auto-change', Number(value) || 0)
}
</script>

<template>
  <div class="status-bar" :class="stateTone">
    <div class="left">
      <span class="dot" />
      <span class="state">{{ stateText }}</span>

      <code v-if="health" class="addr">{{ health.admin_url }}</code>

      <span v-if="health?.reachable" class="meta">
        <span>版本 {{ version }}</span>
        <span>运行 {{ uptime }}</span>
        <span class="hash">
          配置
          <code :title="configHash">{{ shortHash(configHash) }}</code>
        </span>
      </span>
      <span v-else-if="health?.error" class="reason" :title="health.error">{{ health.error }}</span>
    </div>

    <div class="right">
      <a-select
        class="auto"
        size="small"
        :value="autoMs"
        :options="AUTO_OPTIONS"
        @change="onAutoChange"
      />
      <a-button size="small" :loading="loading" @click="emit('refresh')">
        <template #icon><ReloadOutlined /></template>
        刷新
      </a-button>
    </div>
  </div>
</template>

<style scoped lang="less">
.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
}

.left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-warning);
  flex: 0 0 auto;
}

&.ok .dot {
  background: var(--color-success);
  box-shadow: 0 0 0 3px var(--color-success-bg);
}
&.down .dot {
  background: var(--color-error);
  box-shadow: 0 0 0 3px var(--color-error-bg);
}

.state {
  font-weight: 500;
  color: var(--color-text-1);
}

.addr {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--color-bg-sunken);
  color: var(--color-text-2);
}

.meta {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  color: var(--color-text-3);
}

.hash code {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--color-text-2);
}

.reason {
  color: var(--color-error);
  max-width: 460px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.right {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}

.auto {
  width: 148px;
}

@media (max-width: 900px) {
  .reason {
    max-width: 100%;
  }
}
</style>