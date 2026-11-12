<script setup lang="ts">
/**
 * 路由表：别名 / 策略 / 目标 / 降级链 / 能力要求 / 启用状态，展开行看策略参数。
 *
 * 行内只做展示与「编辑」入口；真正的保存由页面统一 `PUT /api/gateway/routes`（整体替换）。
 */
import { safeJsonText } from './meta'
import type { RouteConfig } from '@/types/gateway'

defineProps<{
  routes: RouteConfig[]
  loading: boolean
}>()

const emit = defineEmits<{ edit: [route: RouteConfig] }>()

const columns = [
  { title: '别名', key: 'alias', dataIndex: 'alias', width: 160 },
  { title: '策略', key: 'strategy', dataIndex: 'strategy', width: 130 },
  { title: '目标', key: 'targets', width: 240 },
  { title: '降级链', key: 'fallbacks', width: 180 },
  { title: '能力要求', key: 'required_capabilities', width: 160 },
  { title: '启用', key: 'enabled', dataIndex: 'enabled', width: 80 },
  { title: '操作', key: 'action', width: 80 },
]
</script>

<template>
  <a-table
    :columns="columns"
    :data-source="routes"
    :loading="loading"
    :pagination="false"
    row-key="alias"
    size="middle"
    :scroll="{ x: 1000 }"
  >
    <template #expandedRowRender="{ record }">
      <div class="options">
        <div class="options-label">strategy options（JSON）</div>
        <pre class="options-pre mono">{{ safeJsonText(record.options) }}</pre>
      </div>
    </template>

    <template #bodyCell="{ column, record }">
      <template v-if="column.key === 'alias'">
        <span class="mono alias">{{ record.alias }}</span>
      </template>
      <template v-else-if="column.key === 'strategy'">
        <a-tag color="blue">{{ record.strategy || '-' }}</a-tag>
      </template>
      <template v-else-if="column.key === 'targets'">
        <span v-if="!record.targets?.length" class="text-3">-</span>
        <span v-else class="tags">
          <a-tag v-for="t in record.targets" :key="t.model" class="target-tag">
            <span class="mono">{{ t.model }}</span>
            <span class="target-meta">p{{ t.priority }} · w{{ t.weight }}</span>
          </a-tag>
        </span>
      </template>
      <template v-else-if="column.key === 'fallbacks'">
        <span v-if="!record.fallbacks?.length" class="text-3">-</span>
        <span v-else class="tags">
          <a-tag v-for="m in record.fallbacks" :key="m" class="mono">{{ m }}</a-tag>
        </span>
      </template>
      <template v-else-if="column.key === 'required_capabilities'">
        <span v-if="!record.required_capabilities?.length" class="text-3">-</span>
        <span v-else class="tags">
          <a-tag v-for="c in record.required_capabilities" :key="c">{{ c }}</a-tag>
        </span>
      </template>
      <template v-else-if="column.key === 'enabled'">
        <a-tag :color="record.enabled ? 'success' : 'default'">
          {{ record.enabled ? '启用' : '停用' }}
        </a-tag>
      </template>
      <template v-else-if="column.key === 'action'">
        <a-button type="link" size="small" @click="emit('edit', record)">编辑</a-button>
      </template>
    </template>
    <template #emptyText>
      <div class="table-empty">网关还没有配置路由。</div>
    </template>
  </a-table>
</template>

<style scoped lang="less">
.alias {
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--color-text-1);
}

.tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}

.target-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0;
}

.target-meta {
  color: var(--color-text-3);
  font-size: 10px;
}

.options {
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-sunken);
  border-radius: var(--radius-sm);
}

.options-label {
  font-size: 11px;
  color: var(--color-text-3);
  margin-bottom: 4px;
}

.options-pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-1);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}

.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}
</style>