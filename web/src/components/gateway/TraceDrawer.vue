<script setup lang="ts">
/**
 * 请求追踪详情抽屉：完整字段 + 逐次上游尝试（attempt_log）+ 错误 + 原始 JSON。
 *
 * 列表里拿到的是网关环形缓冲的摘要记录；打开抽屉时页面会再请求
 * `/api/gateway/traces/{id}`，保证 attempt_log / 错误信息是最完整的。
 */
import { fmtBytes, fmtDateTime, fmtLatency, fmtNumber } from '@/utils/format'
import { fmtMoney, safeJsonText } from './meta'
import type { TraceAttempt, TraceRecord } from '@/types/gateway'

defineProps<{
  open: boolean
  record: TraceRecord | null
  loading: boolean
}>()

const emit = defineEmits<{ 'update:open': [value: boolean] }>()

const attemptColumns = [
  { title: '模型', key: 'model', dataIndex: 'model', width: 160 },
  { title: '供应商', key: 'provider', dataIndex: 'provider', width: 100 },
  { title: '状态码', key: 'status', dataIndex: 'status', width: 80 },
  { title: '耗时', key: 'latency_ms', dataIndex: 'latency_ms', width: 80 },
  { title: '错误', key: 'error', dataIndex: 'error' },
]

/** 同一次请求可能对同一个模型重试，key 里带上状态/耗时尽量唯一 */
function attemptKey(attempt: TraceAttempt): string {
  return `${attempt.model}|${attempt.provider}|${attempt.status}|${attempt.latency_ms}`
}

function tokenText(record: TraceRecord): string {
  const u = record.usage
  if (!u) return '-'
  return `${fmtNumber(u.prompt_tokens)} / ${fmtNumber(u.completion_tokens)} / ${fmtNumber(
    u.total_tokens,
  )}`
}
</script>

<template>
  <a-drawer
    :open="open"
    title="请求详情"
    placement="right"
    :width="680"
    @close="emit('update:open', false)"
  >
    <a-skeleton v-if="loading && !record" active :paragraph="{ rows: 10 }" />

    <template v-else-if="record">
      <div class="drawer-head">
        <span class="mono trace-id" :title="record.id">{{ record.id }}</span>
        <span v-if="loading" class="loading-note">刷新中…</span>
      </div>

      <dl class="meta-grid">
        <div class="meta-item">
          <dt>时间</dt>
          <dd>{{ fmtDateTime(record.at) }}</dd>
        </div>
        <div class="meta-item">
          <dt>状态</dt>
          <dd>
            <a-tag :color="record.ok ? 'success' : 'error'">
              {{ record.status }} {{ record.ok ? '成功' : '失败' }}
            </a-tag>
          </dd>
        </div>
        <div class="meta-item">
          <dt>路由 / 模型</dt>
          <dd class="mono">{{ record.route || '-' }} → {{ record.model || '-' }}</dd>
        </div>
        <div class="meta-item">
          <dt>供应商</dt>
          <dd class="mono">{{ record.provider || '-' }}</dd>
        </div>
        <div class="meta-item">
          <dt>耗时 / TTFT</dt>
          <dd class="mono">
            {{ fmtLatency(record.latency_ms) }} /
            {{ record.stream ? fmtLatency(record.ttft_ms) : '—' }}
          </dd>
        </div>
        <div class="meta-item">
          <dt>流式 / 尝试次数</dt>
          <dd>{{ record.stream ? '是' : '否' }} / {{ record.attempts }}</dd>
        </div>
        <div class="meta-item">
          <dt>客户端</dt>
          <dd>{{ record.client_name || '-' }} {{ record.client_ip || '' }}</dd>
        </div>
        <div class="meta-item">
          <dt>tokens（prompt / completion / total）</dt>
          <dd class="mono">{{ tokenText(record) }}</dd>
        </div>
        <div class="meta-item">
          <dt>成本</dt>
          <dd class="mono">{{ fmtMoney(record.cost_usd) }}</dd>
        </div>
        <div class="meta-item">
          <dt>请求 / 响应字节</dt>
          <dd class="mono">
            {{ fmtBytes(record.request_bytes) }} / {{ fmtBytes(record.response_bytes) }}
          </dd>
        </div>
      </dl>

      <section class="section">
        <h3 class="section-title">逐次上游尝试（attempt_log）</h3>
        <a-table
          :columns="attemptColumns"
          :data-source="record.attempt_log || []"
          :pagination="false"
          :row-key="attemptKey"
          size="small"
          :scroll="{ x: 560 }"
        >
          <template #bodyCell="{ column, record: attempt }">
            <template v-if="column.key === 'status'">
              <a-tag :color="attempt.status >= 200 && attempt.status < 300 ? 'success' : 'error'">
                {{ attempt.status }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'latency_ms'">
              <span class="mono">{{ fmtLatency(attempt.latency_ms) }}</span>
            </template>
            <template v-else-if="column.key === 'error'">
              <span v-if="attempt.error" class="attempt-error">{{ attempt.error }}</span>
              <span v-else class="text-3">—</span>
            </template>
          </template>
          <template #emptyText>
            <div class="table-empty">没有逐次尝试记录（可能只尝试了一次且成功）。</div>
          </template>
        </a-table>
      </section>

      <section v-if="record.error" class="section">
        <h3 class="section-title">错误</h3>
        <pre class="pre error-pre">{{ record.error }}</pre>
      </section>

      <section class="section">
        <details class="raw">
          <summary>原始记录 JSON</summary>
          <pre class="pre mono">{{ safeJsonText(record) }}</pre>
        </details>
      </section>
    </template>

    <div v-else class="table-empty">没有选中请求。</div>
  </a-drawer>
</template>

<style scoped lang="less">
.drawer-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.trace-id {
  font-size: 11px;
  color: var(--color-text-3);
  word-break: break-all;
}

.loading-note {
  font-size: 11px;
  color: var(--color-text-3);
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3) var(--space-4);
  margin: 0 0 var(--space-4);
  padding: var(--space-3) var(--space-4);
  background: var(--color-bg-sunken);
  border-radius: var(--radius-md);
}

.meta-item {
  min-width: 0;

  dt {
    font-size: 11px;
    color: var(--color-text-3);
    margin-bottom: 2px;
  }
  dd {
    margin: 0;
    font-size: var(--fs-xs);
    color: var(--color-text-1);
    word-break: break-word;
  }
}

.section {
  margin-top: var(--space-4);
}

.section-title {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--color-text-2);
}

.pre {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-sunken);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-1);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow: auto;
}

.error-pre {
  color: var(--color-error);
  background: var(--color-error-bg);
  border-color: transparent;
}

.attempt-error {
  font-size: 11px;
  color: var(--color-error);
  word-break: break-word;
}

.raw {
  summary {
    cursor: pointer;
    font-size: var(--fs-xs);
    color: var(--color-text-2);
    margin-bottom: var(--space-2);
  }
}

.table-empty {
  padding: var(--space-4) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}

@media (max-width: 700px) {
  .meta-grid {
    grid-template-columns: 1fr;
  }
}
</style>