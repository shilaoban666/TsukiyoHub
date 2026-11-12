<script setup lang="ts">
/**
 * 告警规则表：名称 / 指标 / 作用域 / 条件 / 窗口 / 持续 / 级别 / 启用 / 操作。
 *
 * 启用停用与删除都由页面整体 `PUT /api/gateway/alerts/rules`（网关是整体替换语义），
 * 所以这里只 emit 事件，不自己改数据——失败时页面保留旧规则，switch 会回弹。
 */
import { conditionText, metricLabel, scopeText, severityMeta } from './meta'
import type { AlertRule } from '@/types/gateway'

defineProps<{
  rules: AlertRule[]
  loading: boolean
  /** 规则保存中（switch 按钮 loading，避免连点） */
  saving: boolean
}>()

const emit = defineEmits<{
  edit: [rule: AlertRule]
  toggle: [rule: AlertRule]
  remove: [rule: AlertRule]
}>()

const columns = [
  { title: '名称', key: 'name', dataIndex: 'name', width: 180 },
  { title: '指标', key: 'metric', dataIndex: 'metric', width: 120 },
  { title: '作用域', key: 'scope', width: 180 },
  { title: '条件', key: 'condition', width: 160 },
  { title: '窗口', key: 'window', dataIndex: 'window', width: 80 },
  { title: '持续', key: 'for', dataIndex: 'for', width: 80 },
  { title: '级别', key: 'severity', dataIndex: 'severity', width: 90 },
  { title: '启用', key: 'enabled', dataIndex: 'enabled', width: 80 },
  { title: '操作', key: 'action', width: 130 },
]
</script>

<template>
  <a-table
    :columns="columns"
    :data-source="rules"
    :loading="loading"
    :pagination="false"
    row-key="id"
    size="middle"
    :scroll="{ x: 1100 }"
  >
    <template #bodyCell="{ column, record }">
      <template v-if="column.key === 'name'">
        <div class="rule-name">{{ record.name || record.id }}</div>
        <div v-if="record.message" class="rule-message text-3" :title="record.message">
          {{ record.message }}
        </div>
      </template>
      <template v-else-if="column.key === 'metric'">
        <a-tag>{{ metricLabel(record.metric) }}</a-tag>
      </template>
      <template v-else-if="column.key === 'scope'">
        <span class="scope" :class="{ global: scopeText(record.scope) === '全局' }">
          {{ scopeText(record.scope) }}
        </span>
      </template>
      <template v-else-if="column.key === 'condition'">
        <span class="mono condition">{{ conditionText(record) }}</span>
      </template>
      <template v-else-if="column.key === 'window'">
        <span class="mono">{{ record.window || '-' }}</span>
      </template>
      <template v-else-if="column.key === 'for'">
        <span class="mono">{{ record.for || '立即' }}</span>
      </template>
      <template v-else-if="column.key === 'severity'">
        <a-tag :color="severityMeta(record.severity).color">
          {{ severityMeta(record.severity).label }}
        </a-tag>
      </template>
      <template v-else-if="column.key === 'enabled'">
        <a-switch
          size="small"
          :checked="record.enabled"
          :loading="saving"
          @change="emit('toggle', record)"
        />
      </template>
      <template v-else-if="column.key === 'action'">
        <a-button type="link" size="small" @click="emit('edit', record)">编辑</a-button>
        <a-popconfirm
          :title="`删除规则「${record.name}」？`"
          ok-text="删除"
          cancel-text="取消"
          @confirm="emit('remove', record)"
        >
          <a-button type="link" size="small" danger>删除</a-button>
        </a-popconfirm>
      </template>
    </template>
    <template #emptyText>
      <div class="table-empty">还没有告警规则，点右上角「新增规则」创建一条。</div>
    </template>
  </a-table>
</template>

<style scoped lang="less">
.rule-name {
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--color-text-1);
}

.rule-message {
  margin-top: 2px;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
}

.scope {
  font-size: var(--fs-xs);
  color: var(--color-text-2);
  word-break: break-word;

  &.global {
    color: var(--color-text-3);
  }
}

.condition {
  font-size: 12px;
}

.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}
</style>