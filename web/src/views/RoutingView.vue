<script setup lang="ts">
/**
 * 路由网关页（/routing）。
 *
 * 数据全部来自 TsukiyoHub 后端 `/api/gateway/*`（后端再代理到 Go 网关）：
 * - 首屏：连通性 → 概况 / 窗口统计 / 配置 / 供应商 / 路由 / 追踪；
 * - 网关未连接是**正常状态**：顶部醒目提示 + 空态配置指引，页面不白屏、不弹错误；
 * - 任一请求失败都保留旧数据并给一次 toast（自动刷新时只内联提示，避免刷屏）；
 * - 路由编辑是「整体替换」语义：前端拿当前全量路由，改一条后整包 PUT。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import {
  getGatewayConfig,
  getGatewayHealth,
  getOverview,
  getStats,
  getTrace,
  listProviders,
  listRoutes,
  listTraces,
  probeProvider,
  putRoutes,
  type TraceQuery,
} from '@/api/gateway'
import { errorText } from '@/api/client'
import { fmtLatency, fmtNumber, fmtPercent } from '@/utils/format'
import { fmtMoney, startOfToday, type StatCardItem, type TraceFilters } from '@/components/gateway/meta'
import StatCards from '@/components/gateway/StatCards.vue'
import GatewayStatusBar from '@/components/gateway/GatewayStatusBar.vue'
import GatewayEmpty from '@/components/gateway/GatewayEmpty.vue'
import ProviderTable from '@/components/gateway/ProviderTable.vue'
import RouteTable from '@/components/gateway/RouteTable.vue'
import RouteEditModal from '@/components/gateway/RouteEditModal.vue'
import TraceTable from '@/components/gateway/TraceTable.vue'
import TraceDrawer from '@/components/gateway/TraceDrawer.vue'
import type {
  GatewayConfig,
  GatewayHealth,
  Overview,
  ProviderHealth,
  RouteConfig,
  StatsSnapshot,
  TraceRecord,
} from '@/types/gateway'

/** 追踪表一次拉多少条（后端上限 500，分页在前端切片） */
const TRACE_LIMIT = 200
/** 今日累计最多看最近 500 条追踪（网关追踪是环形缓冲，没有按天聚合接口） */
const DAY_TRACE_LIMIT = 500

// ---------------- 页面数据 ----------------
const health = ref<GatewayHealth | null>(null)
const healthError = ref('')
const overview = ref<Overview | null>(null)
const config = ref<GatewayConfig | null>(null)
const stats = ref<StatsSnapshot | null>(null)
const providers = ref<ProviderHealth[]>([])
const routes = ref<RouteConfig[]>([])
const traces = ref<TraceRecord[]>([])
const dayTotals = ref<{ tokens: number; cost: number; samples: number } | null>(null)

const loading = ref(false)
const traceLoading = ref(false)
const loadError = ref('')
const traceError = ref('')
const probing = ref('')
const autoMs = ref(0)

/** 网关已启用且可达：只有这时才有数据区 */
const connected = computed(() => health.value?.enabled === true && health.value?.reachable === true)

/** 概况里的窗口指标优先；overview 失败时退回 /stats */
const metrics = computed<StatsSnapshot | null>(() => overview.value?.metrics ?? stats.value)

const statCards = computed<StatCardItem[]>(() => {
  const m = metrics.value
  const day = dayTotals.value
  const errorRate = m?.error_rate
  return [
    {
      label: 'RPS',
      value: m ? m.rps.toFixed(2) : '-',
      hint: m?.window_sec ? `${m.window_sec}s 窗口` : '窗口统计',
    },
    {
      label: '错误率',
      value: m ? fmtPercent(m.error_rate, 2) : '-',
      hint: m ? `${fmtNumber(m.errors)} 错 / ${fmtNumber(m.requests)} 请求` : undefined,
      tone:
        errorRate == null
          ? 'normal'
          : errorRate > 0.1
            ? 'error'
            : errorRate > 0.02
              ? 'warn'
              : 'ok',
    },
    {
      label: 'P95 延迟',
      value: m ? fmtLatency(m.p95_ms) : '-',
      hint: m ? `P50 ${fmtLatency(m.p50_ms)}` : undefined,
    },
    { label: 'P95 首字', value: m ? fmtLatency(m.ttft_p95_ms) : '-', hint: '流式 TTFT' },
    { label: '在途请求', value: m ? fmtNumber(m.inflight) : '-', hint: '瞬时值' },
    {
      label: '今日 token',
      value: day ? fmtNumber(day.tokens) : '-',
      hint: day ? `最近 500 条追踪内 · ${day.samples} 条` : '暂无今日追踪',
    },
    {
      label: '今日成本',
      value: day ? fmtMoney(day.cost) : '-',
      hint: '按模型单价估算（USD）',
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

// ---------------- 连通性 ----------------
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

// ---------------- 加载主数据 ----------------
function traceQuery(): TraceQuery {
  const f = traceFilters.value
  return {
    limit: TRACE_LIMIT,
    route: f.route || undefined,
    model: f.model || undefined,
    ok: f.result === 'all' ? undefined : f.result === 'ok',
  }
}

/**
 * 今日 token / 成本：网关没有按天聚合接口，用最近 500 条追踪按本地 0 点过滤后累加。
 * 追踪环形缓冲有上限，所以是「最近 N 条内」的近似值，卡片 hint 里会写清楚。
 */
async function loadDayTotals() {
  try {
    const list = await listTraces({ limit: DAY_TRACE_LIMIT }, { silent: true })
    const start = startOfToday()
    let tokens = 0
    let cost = 0
    let samples = 0
    for (const item of list) {
      const at = Date.parse(item.at)
      if (!Number.isFinite(at) || at < start) continue
      const usage = item.usage
      tokens +=
        usage?.total_tokens ?? (usage?.prompt_tokens ?? 0) + (usage?.completion_tokens ?? 0)
      cost += item.cost_usd || 0
      samples++
    }
    dayTotals.value = { tokens, cost, samples }
  } catch {
    // 今日累计是附加指标，失败静默；保留上一次的值
  }
}

async function loadDashboard(options: { toast?: boolean; quiet?: boolean } = {}) {
  if (!options.quiet) loading.value = true
  try {
    const ok = await loadHealth()
    if (!ok) {
      if (options.toast) {
        if (health.value && !health.value.enabled) {
          message.warning('路由网关管理未启用（GATEWAY_ENABLED=false）')
        } else {
          message.warning('网关未连接，请检查网关服务与 GATEWAY_ADMIN_URL')
        }
      }
      return
    }

    const results = await Promise.allSettled([
      getOverview({ silent: true }),
      getStats({ window: 300 }, { silent: true }),
      getGatewayConfig({ silent: true }),
      listProviders({ silent: true }),
      listRoutes({ silent: true }),
      listTraces(traceQuery(), { silent: true }),
    ])

    let firstError = ''
    const [ov, st, cfg, prov, rts, trs] = results
    if (ov.status === 'fulfilled') overview.value = ov.value
    else firstError = firstError || errorText(ov.reason)

    if (st.status === 'fulfilled') stats.value = st.value
    else firstError = firstError || errorText(st.reason)

    if (cfg.status === 'fulfilled') config.value = cfg.value
    else firstError = firstError || errorText(cfg.reason)

    if (prov.status === 'fulfilled') providers.value = prov.value
    else firstError = firstError || errorText(prov.reason)

    if (rts.status === 'fulfilled') routes.value = rts.value
    else firstError = firstError || errorText(rts.reason)

    if (trs.status === 'fulfilled') {
      traces.value = trs.value
      traceError.value = ''
    } else {
      traceError.value = errorText(trs.reason)
      firstError = firstError || traceError.value
    }

    void loadDayTotals()

    if (firstError) {
      const shouldToast = options.toast && !loadError.value
      loadError.value = firstError
      if (shouldToast) message.error(`部分数据加载失败：${firstError}`)
    } else {
      loadError.value = ''
    }
  } finally {
    if (!options.quiet) loading.value = false
  }
}

function refresh() {
  void loadDashboard({ toast: true })
}

// ---------------- 自动刷新 ----------------
let autoTimer = 0
watch(autoMs, (ms) => {
  window.clearInterval(autoTimer)
  if (ms > 0) {
    autoTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadDashboard({ quiet: true })
    }, ms)
  }
})
onBeforeUnmount(() => window.clearInterval(autoTimer))

// ---------------- 供应商探活 ----------------
async function onProbe(name: string) {
  if (probing.value) return
  probing.value = name
  try {
    const result = await probeProvider(name, { silent: true })
    providers.value = providers.value.map((item) => (item.name === name ? result : item))
    if (result.healthy) {
      message.success(`供应商 ${name} 探活成功（${fmtLatency(result.latency_ms)}）`)
    } else {
      message.error(`供应商 ${name} 探活失败：${result.error || '未返回健康状态'}`)
    }
  } catch (e) {
    message.error(`探活失败：${errorText(e)}`)
  } finally {
    probing.value = ''
  }
}

// ---------------- 路由编辑 ----------------
const editOpen = ref(false)
const editingRoute = ref<RouteConfig | null>(null)
const savingRoute = ref(false)

const routeOptions = computed(() => routes.value.map((r) => r.alias).filter(Boolean))

const modelOptions = computed(() => {
  const set = new Set<string>()
  for (const m of config.value?.models || []) if (m.id) set.add(m.id)
  for (const r of routes.value) for (const t of r.targets || []) if (t.model) set.add(t.model)
  return [...set].sort()
})

function openCreate() {
  editingRoute.value = null
  editOpen.value = true
}

function openEdit(route: RouteConfig) {
  // 保存一份快照：自动刷新会重建 routes 数组，不能直接引用列表里的对象，
  // 否则编辑中的表单会被后台刷新重置。
  editingRoute.value = {
    ...route,
    options: { ...(route.options || {}) },
    targets: (route.targets || []).map((t) => ({ ...t })),
    fallbacks: [...(route.fallbacks || [])],
    required_capabilities: [...(route.required_capabilities || [])],
  }
  editOpen.value = true
}

async function onSaveRoute(next: RouteConfig) {
  if (savingRoute.value) return
  const originalAlias = editingRoute.value?.alias || ''
  const list = [...routes.value]
  if (originalAlias) {
    const idx = list.findIndex((r) => r.alias === originalAlias)
    if (idx >= 0) list[idx] = next
    else list.push(next)
  } else {
    if (list.some((r) => r.alias === next.alias)) {
      message.error(`路由别名「${next.alias}」已存在`)
      return
    }
    list.push(next)
  }

  savingRoute.value = true
  try {
    const result = await putRoutes(list, { silent: true })
    routes.value = list
    editOpen.value = false
    message.success(result.reloaded === false ? '路由已保存（网关未热重载）' : '路由已保存并热重载')
    void loadDashboard({ quiet: true })
  } catch (e) {
    message.error(`保存路由失败：${errorText(e)}`)
  } finally {
    savingRoute.value = false
  }
}

// ---------------- 追踪过滤与详情 ----------------
const traceFilters = ref<TraceFilters>({ route: '', model: '', result: 'all' })

function onTraceFilters(next: TraceFilters) {
  traceFilters.value = next
  void loadTraces(true)
}

async function loadTraces(toastError: boolean) {
  if (!connected.value) return
  traceLoading.value = true
  try {
    traces.value = await listTraces(traceQuery(), { silent: true })
    traceError.value = ''
  } catch (e) {
    traceError.value = errorText(e)
    if (toastError) message.error(`追踪加载失败：${traceError.value}`)
  } finally {
    traceLoading.value = false
  }
}

const traceOpen = ref(false)
const traceDetail = ref<TraceRecord | null>(null)
const traceDetailLoading = ref(false)

async function openTrace(record: TraceRecord) {
  traceDetail.value = record
  traceOpen.value = true
  traceDetailLoading.value = true
  try {
    traceDetail.value = await getTrace(record.id, { silent: true })
  } catch (e) {
    message.warning(`读取追踪详情失败：${errorText(e)}`)
  } finally {
    traceDetailLoading.value = false
  }
}

onMounted(() => {
  void loadDashboard()
})
</script>

<template>
  <div class="page">
    <div class="page-inner wide">
      <header class="page-head">
        <div>
          <h1 class="page-title">路由网关</h1>
          <p class="page-desc">
            多供应商路由 / 降级 / 熔断 / 追踪。这里只读展示网关状态，路由修改保存后由网关热重载，不需要重启。
          </p>
        </div>
      </header>

      <GatewayStatusBar
        :health="health"
        :overview="overview"
        :auto-ms="autoMs"
        :loading="loading"
        @refresh="refresh"
        @auto-change="autoMs = $event"
      />

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

        <section class="block">
          <div class="block-head">
            <h2 class="block-title">供应商</h2>
            <span class="text-3 hint">探活会真实请求上游 health_path</span>
          </div>
          <div class="table-card">
            <ProviderTable
              :providers="providers"
              :configs="config?.providers || []"
              :loading="loading"
              :probing="probing"
              @probe="onProbe"
            />
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2 class="block-title">路由</h2>
            <span class="text-3 hint">保存时整体替换 routes 并热重载</span>
            <a-button class="head-btn" size="small" @click="openCreate">
              <template #icon><PlusOutlined /></template>
              新增路由
            </a-button>
          </div>
          <div class="table-card">
            <RouteTable :routes="routes" :loading="loading" @edit="openEdit" />
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2 class="block-title">请求追踪</h2>
            <span class="text-3 hint">点击行查看完整 attempt_log 与错误</span>
          </div>
          <div class="table-card">
            <TraceTable
              :traces="traces"
              :loading="traceLoading"
              :route-options="routeOptions"
              :model-options="modelOptions"
              :filters="traceFilters"
              @update:filters="onTraceFilters"
              @select="openTrace"
            />
          </div>
          <p v-if="traceError" class="trace-error">追踪加载失败：{{ traceError }}</p>
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

    <RouteEditModal
      v-model:open="editOpen"
      :route="editingRoute"
      :saving="savingRoute"
      :model-options="modelOptions"
      @save="onSaveRoute"
    />
    <TraceDrawer
      v-model:open="traceOpen"
      :record="traceDetail"
      :loading="traceDetailLoading"
    />
  </div>
</template>

<style scoped lang="less">
.page-inner.wide {
  max-width: 1280px;
}

.hint {
  font-size: 11px;
}

.head-btn {
  margin-left: auto;
}

.table-card {
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  overflow: hidden;
}

.trace-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-error);
}

@media (max-width: 900px) {
  .page-inner.wide {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
  .head-btn {
    margin-left: 0;
  }
}
</style>