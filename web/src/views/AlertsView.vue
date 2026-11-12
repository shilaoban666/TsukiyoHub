<script setup lang="ts">
/**
 * 告警页（/alerts）。
 *
 * 告警引擎跑在 Go 网关里（评估周期由网关配置决定），本页只做管理：
 * - 概况：规则数 / firing / pending / 最近评估时间；
 * - 规则：新增、编辑、启用停用、删除（整体 PUT，网关热更新）；
 * - 事件：按状态过滤，firing 行高亮，行内确认（Ack）并选择静默分钟数。
 *
 * 「立即评估」不是前端能触发的动作：顶部提示说明评估由后端周期执行，本页只刷新数据。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import {
  ackAlert,
  getAlertOverview,
  getGatewayHealth,
  listAlertEvents,
  listAlertRules,
  putAlertRules,
} from '@/api/gateway'
import { errorText } from '@/api/client'
import { fmtDateTime } from '@/utils/format'
import { type StatCardItem } from '@/components/gateway/meta'
import StatCards from '@/components/gateway/StatCards.vue'
import GatewayEmpty from '@/components/gateway/GatewayEmpty.vue'
import AlertRuleTable from '@/components/gateway/AlertRuleTable.vue'
import AlertRuleModal from '@/components/gateway/AlertRuleModal.vue'
import AlertEventTable from '@/components/gateway/AlertEventTable.vue'
import type { AlertEvent, AlertOverview, AlertRule, GatewayHealth } from '@/types/gateway'

// ---------------- 页面数据 ----------------
const health = ref<GatewayHealth | null>(null)
const healthError = ref('')
const overview = ref<AlertOverview | null>(null)
const rules = ref<AlertRule[]>([])
const events = ref<AlertEvent[]>([])

const loading = ref(false)
const eventsLoading = ref(false)
const loadError = ref('')
const savingRules = ref(false)

const connected = computed(() => health.value?.enabled === true && health.value?.reachable === true)

const statCards = computed<StatCardItem[]>(() => {
  const o = overview.value
  return [
    {
      label: '规则数',
      value: o ? String(o.rules) : '-',
      hint: o && !o.enabled ? '告警引擎未启用' : '已配置规则',
    },
    {
      label: 'firing',
      value: o ? String(o.firing) : '-',
      hint: '正在告警的事件',
      tone: o && o.firing > 0 ? 'error' : 'ok',
    },
    {
      label: 'pending',
      value: o ? String(o.pending) : '-',
      hint: '等待满足持续时长',
      tone: o && o.pending > 0 ? 'warn' : 'normal',
    },
    {
      label: '最近评估',
      value: o?.last_check ? fmtDateTime(o.last_check) : '-',
      hint: '由网关内置引擎周期评估',
    },
  ]
})

const emptyDesc = computed(() => {
  if (healthError.value) {
    return `TsukiyoHub 后端请求失败：${healthError.value}。请确认后端已启动后再重试。`
  }
  if (health.value && !health.value.enabled) {
    return '后端配置了 GATEWAY_ENABLED=false。启用后把 GATEWAY_ADMIN_URL 指向 Go 网关的 admin_listen（默认 http://127.0.0.1:8082），重启后端即可。'
  }
  const url = health.value?.admin_url || 'http://127.0.0.1:8082'
  return `网关管理地址：${url}。请确认 Go 网关已启动（gateway/ 下 go run ./cmd/tsukiyo-gateway），并且后端 .env 的 GATEWAY_ADMIN_URL 指向它；若网关配置了 admin_token，后端还要填相同的 GATEWAY_ADMIN_TOKEN。`
})

// ---------------- 加载 ----------------
async function loadHealth(): Promise<boolean> {
  try {
    health.value = await getGatewayHealth()
    healthError.value = ''
  } catch (e) {
    health.value = null
    healthError.value = errorText(e)
  }
  return connected.value
}

async function loadAlerts(options: { toast?: boolean } = {}) {
  loading.value = true
  try {
    const ok = await loadHealth()
    if (!ok) {
      if (options.toast) {
        if (health.value && !health.value.enabled) {
          message.warning('路由网关管理未启用（GATEWAY_ENABLED=false）')
        } else {
          message.warning('网关未连接，无法读取告警数据')
        }
      }
      return
    }

    const results = await Promise.allSettled([
      getAlertOverview({ silent: true }),
      listAlertRules({ silent: true }),
      listAlertEvents({ limit: 100, status: statusParam() }, { silent: true }),
    ])

    let firstError = ''
    const [ov, rls, evs] = results
    if (ov.status === 'fulfilled') overview.value = ov.value
    else firstError = firstError || errorText(ov.reason)

    if (rls.status === 'fulfilled') rules.value = rls.value
    else firstError = firstError || errorText(rls.reason)

    if (evs.status === 'fulfilled') events.value = evs.value
    else firstError = firstError || errorText(evs.reason)

    if (firstError) {
      const shouldToast = options.toast && !loadError.value
      loadError.value = firstError
      if (shouldToast) message.error(`部分数据加载失败：${firstError}`)
    } else {
      loadError.value = ''
    }
  } finally {
    loading.value = false
  }
}

function refresh() {
  void loadAlerts({ toast: true })
}

// ---------------- 事件状态过滤 ----------------
const eventStatus = ref('all')
const EVENT_STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'firing', label: 'firing' },
  { value: 'pending', label: 'pending' },
  { value: 'resolved', label: 'resolved' },
  { value: 'acked', label: 'acked' },
]

function statusParam(): string | undefined {
  return eventStatus.value === 'all' ? undefined : eventStatus.value
}

async function loadEvents(toastError: boolean) {
  if (!connected.value) return
  eventsLoading.value = true
  try {
    events.value = await listAlertEvents(
      { limit: 100, status: statusParam() },
      { silent: true },
    )
  } catch (e) {
    if (toastError) message.error(`事件加载失败：${errorText(e)}`)
  } finally {
    eventsLoading.value = false
  }
}

function onStatusChange(value: unknown) {
  eventStatus.value = value ? String(value) : 'all'
  void loadEvents(true)
}

// ---------------- 规则编辑 ----------------
const ruleModalOpen = ref(false)
const editingRule = ref<AlertRule | null>(null)

function openCreateRule() {
  editingRule.value = null
  ruleModalOpen.value = true
}

function openEditRule(rule: AlertRule) {
  editingRule.value = { ...rule, scope: { ...(rule.scope || {}) } }
  ruleModalOpen.value = true
}

/** 整体保存规则；成功返回 true（对话框据此决定是否关闭） */
async function persistRules(next: AlertRule[], okText: string): Promise<boolean> {
  if (savingRules.value) return false
  savingRules.value = true
  try {
    await putAlertRules(next, { silent: true })
    rules.value = next
    message.success(okText)
    void loadAlerts()
    return true
  } catch (e) {
    message.error(`保存规则失败：${errorText(e)}`)
    return false
  } finally {
    savingRules.value = false
  }
}

async function onRuleSave(rule: AlertRule) {
  const isEdit = Boolean(editingRule.value)
  const editingId = editingRule.value?.id
  const next = isEdit
    ? rules.value.map((item) => (item.id === editingId ? rule : item))
    : [...rules.value, rule]
  const ok = await persistRules(
    next,
    isEdit ? `规则「${rule.name}」已更新` : `规则「${rule.name}」已创建`,
  )
  if (ok) ruleModalOpen.value = false
}

function onToggleRule(rule: AlertRule) {
  const next = rules.value.map((item) =>
    item.id === rule.id ? { ...item, enabled: !item.enabled } : item,
  )
  void persistRules(next, `${rule.name} ${rule.enabled ? '已停用' : '已启用'}`)
}

function onRemoveRule(rule: AlertRule) {
  const next = rules.value.filter((item) => item.id !== rule.id)
  void persistRules(next, `已删除规则「${rule.name}」`)
}

// ---------------- 确认（Ack） ----------------
const ackOpen = ref(false)
const ackEvent = ref<AlertEvent | null>(null)
const ackMinutes = ref(30)
const acking = ref('')

const ACK_MINUTE_OPTIONS = [5, 15, 30, 60]

function openAck(event: AlertEvent) {
  ackEvent.value = event
  ackMinutes.value = 30
  ackOpen.value = true
}

async function submitAck() {
  const event = ackEvent.value
  if (!event || acking.value) return
  acking.value = event.id
  try {
    await ackAlert(event.id, ackMinutes.value, 'web', { silent: true })
    message.success(`已确认告警，静默 ${ackMinutes.value} 分钟`)
    ackOpen.value = false
    await loadAlerts()
  } catch (e) {
    message.error(`确认失败：${errorText(e)}`)
  } finally {
    acking.value = ''
  }
}

onMounted(() => {
  void loadAlerts()
})
</script>

<template>
  <div class="page">
    <div class="page-inner wide">
      <header class="page-head">
        <div>
          <h1 class="page-title">告警</h1>
          <p class="page-desc">
            网关内置告警引擎的规则与事件。指标超过阈值并满足持续时长后产生事件，可按规则配置通知渠道。
          </p>
        </div>
        <div class="head-actions">
          <a-button :loading="loading" @click="refresh">
            <template #icon><ReloadOutlined /></template>
            刷新
          </a-button>
        </div>
      </header>

      <a-alert
        v-if="healthError"
        type="error"
        show-icon
        message="无法获取网关状态"
        :description="`TsukiyoHub 后端请求失败：${healthError}`"
      >
        <template #action>
          <a-button size="small" @click="refresh">重试</a-button>
        </template>
      </a-alert>

      <a-alert
        v-else-if="health && !health.enabled"
        type="warning"
        show-icon
        message="路由网关管理未启用"
        description="后端配置了 GATEWAY_ENABLED=false。启用后把 GATEWAY_ADMIN_URL 指向 Go 网关的 admin_listen（默认 http://127.0.0.1:8082）。"
      />

      <a-alert
        v-else-if="health && !health.reachable"
        type="warning"
        show-icon
        message="路由网关未连接"
        :description="`管理地址：${health.admin_url}${health.error ? `。原因：${health.error}` : ''}`"
      />

      <a-alert v-if="loadError" type="error" show-icon message="部分数据加载失败" :description="loadError">
        <template #action>
          <a-button size="small" @click="refresh">重试</a-button>
        </template>
      </a-alert>

      <template v-if="connected">
        <StatCards :items="statCards" />

        <a-alert
          type="info"
          show-icon
          message="立即评估"
          description="告警由网关内置引擎按配置的评估间隔（默认 15s）自动执行；这里的「刷新」只重新拉取规则与事件，不会立即触发评估。"
        />

        <section class="block">
          <div class="block-head">
            <h2 class="block-title">规则（{{ rules.length }}）</h2>
            <span class="text-3 hint">保存时整体 PUT /api/gateway/alerts/rules</span>
            <a-button class="head-btn" size="small" @click="openCreateRule">
              <template #icon><PlusOutlined /></template>
              新增规则
            </a-button>
          </div>
          <div class="table-card">
            <AlertRuleTable
              :rules="rules"
              :loading="loading"
              :saving="savingRules"
              @edit="openEditRule"
              @toggle="onToggleRule"
              @remove="onRemoveRule"
            />
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2 class="block-title">事件（{{ events.length }}）</h2>
            <span class="text-3 hint">firing 行已高亮；确认后静默期内不再重复通知</span>
            <a-select
              class="status-filter"
              size="small"
              :value="eventStatus"
              :options="EVENT_STATUS_OPTIONS"
              @change="onStatusChange"
            />
          </div>
          <div class="table-card">
            <AlertEventTable
              :events="events"
              :loading="eventsLoading || loading"
              :acking="acking"
              @ack="openAck"
            />
          </div>
        </section>
      </template>

      <GatewayEmpty
        v-else-if="!loading"
        :title="health && !health.enabled ? '路由网关管理未启用' : '路由网关未连接'"
        :desc="emptyDesc"
      >
        <a-button :loading="loading" @click="refresh">
          <template #icon><ReloadOutlined /></template>
          重试连接
        </a-button>
      </GatewayEmpty>

      <a-skeleton v-else active :paragraph="{ rows: 8 }" />
    </div>

    <AlertRuleModal
      v-model:open="ruleModalOpen"
      :rule="editingRule"
      :saving="savingRules"
      @save="onRuleSave"
    />

    <a-modal
      :open="ackOpen"
      title="确认告警"
      ok-text="确认"
      cancel-text="取消"
      :confirm-loading="Boolean(acking)"
      @ok="submitAck"
      @cancel="ackOpen = false"
    >
      <p class="ack-desc">
        确认「{{ ackEvent?.rule_name || ackEvent?.rule_id }}」后，该告警在选定的静默期内不再重复通知。
      </p>
      <a-radio-group v-model:value="ackMinutes">
        <a-radio v-for="minutes in ACK_MINUTE_OPTIONS" :key="minutes" :value="minutes">
          {{ minutes }} 分钟
        </a-radio>
      </a-radio-group>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
.page-inner.wide {
  max-width: 1240px;
}

.hint {
  font-size: 11px;
}

.head-btn {
  margin-left: auto;
}

.status-filter {
  width: 132px;
  margin-left: auto;
}

.table-card {
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  overflow: hidden;
}

.ack-desc {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text-2);
}

@media (max-width: 900px) {
  .page-inner.wide {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
  .head-btn,
  .status-filter {
    margin-left: 0;
  }
}
</style>