<script setup lang="ts">
/**
 * 供应商表：名称 / 类型 / base_url / 健康 / 延迟 / 最近探活 + 行内「探活」。
 *
 * 健康数据来自 `/api/gateway/providers`（网关熔断与探活的结果），
 * 类型和 base_url 来自 `/api/gateway/config`（两个接口按 name 关联）。
 */
import { computed } from 'vue'
import { fmtDateTime, fmtLatency } from '@/utils/format'
import type { GatewayProviderConfig, ProviderHealth } from '@/types/gateway'

const props = defineProps<{
  providers: ProviderHealth[]
  configs: GatewayProviderConfig[]
  loading: boolean
  /** 正在探活的供应商名（按钮 loading） */
  probing: string
}>()

const emit = defineEmits<{ probe: [name: string] }>()

interface ProviderRow extends ProviderHealth {
  type: string
  base_url: string
}

const rows = computed<ProviderRow[]>(() =>
  props.providers.map((p) => {
    const cfg = props.configs.find((c) => c.name === p.name)
    return {
      ...p,
      type: cfg?.type || '-',
      base_url: cfg?.base_url || '-',
    }
  }),
)

const columns = [
  { title: '名称', key: 'name', dataIndex: 'name', width: 170 },
  { title: '类型', key: 'type', dataIndex: 'type', width: 100 },
  { title: 'base_url', key: 'base_url', dataIndex: 'base_url' },
  { title: '健康', key: 'healthy', dataIndex: 'healthy', width: 90 },
  { title: '延迟', key: 'latency_ms', dataIndex: 'latency_ms', width: 90 },
  { title: '最近探活', key: 'checked_at', dataIndex: 'checked_at', width: 170 },
  { title: '操作', key: 'action', width: 100 },
]
</script>

<template>
  <a-table
    :columns="columns"
    :data-source="rows"
    :loading="loading"
    :pagination="false"
    row-key="name"
    size="middle"
    :scroll="{ x: 860 }"
  >
    <template #bodyCell="{ column, record }">
      <template v-if="column.key === 'name'">
        <div class="name-cell">
          <span class="name">{{ record.name }}</span>
          <a-tooltip v-if="record.error" :title="record.error">
            <span class="err-dot">!</span>
          </a-tooltip>
        </div>
      </template>
      <template v-else-if="column.key === 'type'">
        <a-tag>{{ record.type }}</a-tag>
      </template>
      <template v-else-if="column.key === 'base_url'">
        <span class="mono url" :title="record.base_url">{{ record.base_url }}</span>
      </template>
      <template v-else-if="column.key === 'healthy'">
        <a-tag :color="record.healthy ? 'success' : 'error'">
          {{ record.healthy ? '健康' : '异常' }}
        </a-tag>
      </template>
      <template v-else-if="column.key === 'latency_ms'">
        <span class="mono">{{ fmtLatency(record.latency_ms) }}</span>
      </template>
      <template v-else-if="column.key === 'checked_at'">
        <span class="text-2">{{ fmtDateTime(record.checked_at) }}</span>
      </template>
      <template v-else-if="column.key === 'action'">
        <a-button
          type="link"
          size="small"
          :loading="probing === record.name"
          @click="emit('probe', record.name)"
        >
          探活
        </a-button>
      </template>
    </template>
    <template #emptyText>
      <div class="table-empty">网关还没有配置供应商。</div>
    </template>
  </a-table>
</template>

<style scoped lang="less">
.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}

.name {
  font-weight: 500;
  color: var(--color-text-1);
}

.err-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: var(--color-error-bg);
  color: var(--color-error);
  font-size: 10px;
  font-weight: 700;
  cursor: help;
}

.url {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}

.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}
</style>