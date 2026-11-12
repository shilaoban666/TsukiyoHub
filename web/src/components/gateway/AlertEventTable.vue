<script setup lang="ts">
/**
 * 告警事件表：时间 / 规则 / 级别 / 指标值 / 阈值 / 状态 / 持续时间 + 行内确认（Ack）。
 *
 * firing 行整行高亮（只做背景色，不加动画）；确认弹窗由页面统一处理。
 */
import dayjs from 'dayjs'
import {
  eventStatusMeta,
  fmtDurationBetween,
  fmtThreshold,
  metricLabel,
  severityMeta,
} from './meta'
import type { AlertEvent } from '@/types/gateway'

defineProps<{
  events: AlertEvent[]
  loading: boolean
  /** 正在确认的事件 id */
  acking: string
}>()

const emit = defineEmits<{ ack: [event: AlertEvent] }>()

const columns = [
  { title: '时间', key: 'starts_at', dataIndex: 'starts_at', width: 140 },
  { title: '规则', key: 'rule', width: 200 },
  { title: '级别', key: 'severity', dataIndex: 'severity', width: 90 },
  { title: '指标值', key: 'value', dataIndex: 'value', width: 120 },
  { title: '阈值', key: 'threshold', dataIndex: 'threshold', width: 100 },
  { title: '状态', key: 'status', dataIndex: 'status', width: 100 },
  { title: '持续时间', key: 'duration', width: 110 },
  { title: '操作', key: 'action', width: 100 },
]

function fmtAt(value: string): string {
  const d = dayjs(value)
  return d.isValid() ? d.format('MM-DD HH:mm:ss') : '-'
}

function rowClass(record: AlertEvent): string {
  return record.status === 'firing' ? 'row-firing' : ''
}
</script>

<template>
  <a-table
    :columns="columns"
    :data-source="events"
    :loading="loading"
    :pagination="events.length > 20 ? { pageSize: 20, showSizeChanger: false } : false"
    :row-class-name="rowClass"
    row-key="id"
    size="middle"
    :scroll="{ x: 1050 }"
  >
    <template #bodyCell="{ column, record }">
      <template v-if="column.key === 'starts_at'">
        <span class="mono time">{{ fmtAt(record.starts_at) }}</span>
      </template>
      <template v-else-if="column.key === 'rule'">
        <div class="rule-name">{{ record.rule_name || record.rule_id }}</div>
        <div v-if="record.message" class="rule-message text-3" :title="record.message">
          {{ record.message }}
        </div>
      </template>
      <template v-else-if="column.key === 'severity'">
        <a-tag :color="severityMeta(record.severity).color">
          {{ severityMeta(record.severity).label }}
        </a-tag>
      </template>
      <template v-else-if="column.key === 'value'">
        <span class="mono" :title="metricLabel(record.metric)">
          {{ fmtThreshold(record.value) }}
        </span>
      </template>
      <template v-else-if="column.key === 'threshold'">
        <span class="mono text-2">{{ record.op }} {{ fmtThreshold(record.threshold) }}</span>
      </template>
      <template v-else-if="column.key === 'status'">
        <a-tag :color="eventStatusMeta(record.status).color">
          {{ eventStatusMeta(record.status).label }}
        </a-tag>
      </template>
      <template v-else-if="column.key === 'duration'">
        <span class="mono text-2">
          {{ fmtDurationBetween(record.starts_at, record.ends_at || undefined) }}
        </span>
      </template>
      <template v-else-if="column.key === 'action'">
        <a-button
          size="small"
          :loading="acking === record.id"
          :disabled="record.status === 'resolved'"
          @click="emit('ack', record)"
        >
          确认
        </a-button>
      </template>
    </template>
    <template #emptyText>
      <div class="table-empty">暂无告警事件。指标超过规则阈值并满足持续时长后才会产生事件。</div>
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

.time {
  font-size: 12px;
}

:deep(.row-firing) > td {
  background: var(--color-error-bg) !important;
}

.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}
</style>