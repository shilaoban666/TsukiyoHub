<script setup lang="ts">
/**
 * 请求追踪表：时间 / 路由 / 模型 / 状态码 / 耗时 / TTFT / tokens / 成本 / 尝试次数 / 客户端。
 *
 * 过滤条件变化时 emit 给页面重新请求（`route/model/ok` 由网关侧过滤），
 * 分页纯前端切片（网关只返回最近 N 条，足够管理页排查用）。
 */
import { computed, ref, watch } from 'vue'
import dayjs from 'dayjs'
import { fmtLatency, fmtNumber } from '@/utils/format'
import { fmtMoney, type TraceFilters } from './meta'
import type { TraceRecord } from '@/types/gateway'

const props = defineProps<{
  traces: TraceRecord[]
  loading: boolean
  routeOptions: string[]
  modelOptions: string[]
  filters: TraceFilters
}>()

const emit = defineEmits<{
  'update:filters': [value: TraceFilters]
  select: [record: TraceRecord]
}>()

const page = ref(1)
const pageSize = 20

watch(
  () => props.traces,
  () => {
    page.value = 1
  },
)

const pagination = computed(() => ({
  current: page.value,
  pageSize,
  total: props.traces.length,
  showSizeChanger: false,
  showTotal: (total: number) => `共 ${total} 条`,
  onChange: (next: number) => {
    page.value = next
  },
}))

const routeSelectOptions = computed(() => props.routeOptions.map((v) => ({ value: v, label: v })))
const modelSelectOptions = computed(() => props.modelOptions.map((v) => ({ value: v, label: v })))

const RESULT_OPTIONS = [
  { value: 'all', label: '全部结果' },
  { value: 'ok', label: '仅成功' },
  { value: 'fail', label: '仅失败' },
]

function patch(patchValue: Partial<TraceFilters>) {
  emit('update:filters', { ...props.filters, ...patchValue })
}

function onRouteChange(value: unknown) {
  patch({ route: value ? String(value) : '' })
}

function onModelChange(value: unknown) {
  patch({ model: value ? String(value) : '' })
}

function onResultChange(value: unknown) {
  patch({ result: (value === 'ok' || value === 'fail' ? value : 'all') as TraceFilters['result'] })
}

function reset() {
  emit('update:filters', { route: '', model: '', result: 'all' })
}

function onRow(record: TraceRecord) {
  return { onClick: () => emit('select', record) }
}

function fmtAt(value: string): string {
  const d = dayjs(value)
  return d.isValid() ? d.format('MM-DD HH:mm:ss') : '-'
}

const columns = [
  { title: '时间', key: 'at', dataIndex: 'at', width: 140 },
  { title: '路由', key: 'route', dataIndex: 'route', width: 120 },
  { title: '模型', key: 'model', dataIndex: 'model', width: 180 },
  { title: '状态码', key: 'status', dataIndex: 'status', width: 84 },
  { title: '耗时', key: 'latency_ms', dataIndex: 'latency_ms', width: 90 },
  { title: 'TTFT', key: 'ttft_ms', dataIndex: 'ttft_ms', width: 90 },
  { title: 'tokens', key: 'tokens', width: 100 },
  { title: '成本', key: 'cost_usd', dataIndex: 'cost_usd', width: 100 },
  { title: '尝试', key: 'attempts', dataIndex: 'attempts', width: 72 },
  { title: '客户端', key: 'client', width: 150 },
]
</script>

<template>
  <div class="trace-panel">
    <div class="filters">
      <a-select
        class="filter"
        allow-clear
        placeholder="全部路由"
        :value="filters.route || undefined"
        :options="routeSelectOptions"
        @change="onRouteChange"
      />
      <a-select
        class="filter"
        allow-clear
        placeholder="全部模型"
        :value="filters.model || undefined"
        :options="modelSelectOptions"
        @change="onModelChange"
      />
      <a-select
        class="filter result"
        :value="filters.result"
        :options="RESULT_OPTIONS"
        @change="onResultChange"
      />
      <a-button size="small" type="text" @click="reset">重置</a-button>
      <span class="count text-3">最近 {{ traces.length }} 条</span>
    </div>

    <a-table
      :columns="columns"
      :data-source="traces"
      :loading="loading"
      :pagination="pagination"
      :custom-row="onRow"
      row-key="id"
      size="middle"
      :scroll="{ x: 1150 }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'at'">
          <span class="mono time">{{ fmtAt(record.at) }}</span>
        </template>
        <template v-else-if="column.key === 'route'">
          <span class="mono">{{ record.route || '-' }}</span>
        </template>
        <template v-else-if="column.key === 'model'">
          <div class="model">{{ record.model || '-' }}</div>
          <div class="provider text-3">{{ record.provider || '' }}</div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="record.ok ? 'success' : 'error'">{{ record.status }}</a-tag>
        </template>
        <template v-else-if="column.key === 'latency_ms'">
          <span class="mono">{{ fmtLatency(record.latency_ms) }}</span>
        </template>
        <template v-else-if="column.key === 'ttft_ms'">
          <span class="mono">{{ record.stream ? fmtLatency(record.ttft_ms) : '—' }}</span>
        </template>
        <template v-else-if="column.key === 'tokens'">
          <span class="mono">{{
            record.usage ? fmtNumber(record.usage.total_tokens) : '-'
          }}</span>
        </template>
        <template v-else-if="column.key === 'cost_usd'">
          <span class="mono">{{ fmtMoney(record.cost_usd) }}</span>
        </template>
        <template v-else-if="column.key === 'attempts'">
          <a-tag :color="record.attempts > 1 ? 'warning' : 'default'">{{ record.attempts }}</a-tag>
        </template>
        <template v-else-if="column.key === 'client'">
          <span class="text-2">{{ record.client_name || record.client_ip || '-' }}</span>
        </template>
      </template>
      <template #emptyText>
        <div class="table-empty">
          没有请求记录。调用网关 <code>/v1/chat/completions</code> 后这里会出现追踪。
        </div>
      </template>
    </a-table>
  </div>
</template>

<style scoped lang="less">
.trace-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.filter {
  width: 168px;
}

.result {
  width: 128px;
}

.count {
  margin-left: auto;
  font-size: var(--fs-xs);
}

.time {
  font-size: 12px;
}

.model {
  font-size: var(--fs-xs);
  color: var(--color-text-1);
}

.provider {
  font-size: 11px;
}

.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);

  code {
    font-family: var(--font-mono);
    background: var(--color-bg-hover);
    padding: 1px 5px;
    border-radius: 4px;
  }
}

:deep(.ant-table-tbody > tr) {
  cursor: pointer;
}

@media (max-width: 900px) {
  .filter {
    width: 100%;
  }
  .result {
    width: 100%;
  }
}
</style>