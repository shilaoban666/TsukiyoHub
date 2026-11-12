/**
 * 路由网关管理接口。
 *
 * 前端只连 TsukiyoHub 后端（`/api/gateway/*`），由后端代理到独立的 Go 网关
 * （网关管理接口默认只允许本机访问，见 `gateway/internal/admin`）。
 *
 * 约定：
 * - 列表接口统一 `{items: [...]}`，这里用 `normalizeList` 兼容裸数组；
 * - 错误统一非 2xx + `{"detail": "中文"}`，由 `api/client.ts` 归一化成 `ApiError`；
 * - `/gateway/health` 在网关不可达时仍返回 200 + `reachable:false`，**不是错误**；
 * - 页面里需要「失败弹 toast 但保留旧数据」时传 `{silent:true}` 自己控制提示，
 *   避免一次刷新里多个请求失败弹一屏 toast。
 */
import { http, type RequestOptions } from './client'
import { normalizeList } from './sessions'
import type {
  AlertAckResult,
  AlertEvent,
  AlertOverview,
  AlertRule,
  AlertRulesSaveResult,
  GatewayConfig,
  GatewayConfigSaveResult,
  GatewayHealth,
  Overview,
  ProviderHealth,
  RouteConfig,
  RoutesSaveResult,
  StatsSnapshot,
  TraceRecord,
} from '@/types/gateway'

const BASE = '/gateway'

/**
 * 列表兜底：后端约定 `{items: [...]}`，但网关 admin 各实现也可能直接返回数组、
 * 或按资源名包一层（`{providers: [...]}` 等）。三种都兼容，避免版本差异白屏。
 */
function listItems<T>(data: unknown, namedKey: string): T[] {
  const base = normalizeList<T>(data)
  if (base.items.length) return base.items
  if (Array.isArray(data)) return data as T[]
  const obj = (data ?? {}) as Record<string, unknown>
  const named = obj[namedKey]
  if (Array.isArray(named)) return named as T[]
  return base.items
}

// ---------------------------------------------------------------------------
// 连通性 / 概况
// ---------------------------------------------------------------------------
/**
 * 网关连通性。**静默**调用：不可达是正常状态（页面显示空态），不该弹错误。
 * 超时设得短一点，避免后端没起时刷新按钮卡 60s。
 */
export async function getGatewayHealth(): Promise<GatewayHealth> {
  return http.get<GatewayHealth>(`${BASE}/health`, undefined, { silent: true, timeout: 6000 })
}

/** 网关概况（版本 / uptime / 窗口指标 / 供应商健康 / 熔断器 / 告警概况） */
export async function getOverview(opt?: RequestOptions): Promise<Overview> {
  return http.get<Overview>(`${BASE}/overview`, undefined, opt)
}

/** 窗口统计；`window` 后端限制 10~900 秒 */
export async function getStats(
  query: { window?: number; route?: string; model?: string; provider?: string } = {},
  opt?: RequestOptions,
): Promise<StatsSnapshot> {
  const params: Record<string, unknown> = { window: query.window ?? 300 }
  if (query.route) params.route = query.route
  if (query.model) params.model = query.model
  if (query.provider) params.provider = query.provider
  return http.get<StatsSnapshot>(`${BASE}/stats`, params, opt)
}

// ---------------------------------------------------------------------------
// 配置（页面只读取供应商类型/base_url 与模型清单）
// ---------------------------------------------------------------------------
/** 完整配置（密钥已脱敏） */
export async function getGatewayConfig(opt?: RequestOptions): Promise<GatewayConfig> {
  return http.get<GatewayConfig>(`${BASE}/config`, undefined, opt)
}

/** 保存完整配置（本页暂不提供编辑入口，接口保留给后续配置页） */
export async function putGatewayConfig(
  config: GatewayConfig,
  opt?: RequestOptions,
): Promise<GatewayConfigSaveResult> {
  return http.put<GatewayConfigSaveResult>(`${BASE}/config`, config, { timeout: 30000, ...opt })
}

/** 让网关从自己的配置文件重新加载 */
export async function reloadGatewayConfig(opt?: RequestOptions): Promise<{ ok: boolean }> {
  return http.post<{ ok: boolean }>(`${BASE}/config/reload`, {}, opt)
}

// ---------------------------------------------------------------------------
// 供应商
// ---------------------------------------------------------------------------
/** 供应商健康列表（从网关熔断/探活状态里取） */
export async function listProviders(opt?: RequestOptions): Promise<ProviderHealth[]> {
  const data = await http.get<unknown>(`${BASE}/providers`, undefined, opt)
  return listItems<ProviderHealth>(data, 'providers')
}

/** 主动探活一个供应商（后端超时 30s，探活本身可能慢） */
export async function probeProvider(name: string, opt?: RequestOptions): Promise<ProviderHealth> {
  return http.post<ProviderHealth>(
    `${BASE}/providers/${encodeURIComponent(name)}/probe`,
    {},
    { timeout: 45000, ...opt },
  )
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------
/** 路由列表 */
export async function listRoutes(opt?: RequestOptions): Promise<RouteConfig[]> {
  const data = await http.get<unknown>(`${BASE}/routes`, undefined, opt)
  return listItems<RouteConfig>(data, 'routes')
}

/** 整体替换路由（网关热重载，不需要重启） */
export async function putRoutes(
  items: RouteConfig[],
  opt?: RequestOptions,
): Promise<RoutesSaveResult> {
  return http.put<RoutesSaveResult>(`${BASE}/routes`, { items }, { timeout: 30000, ...opt })
}

// ---------------------------------------------------------------------------
// 请求追踪
// ---------------------------------------------------------------------------
export interface TraceQuery {
  /** 后端限制 1~500，默认 50 */
  limit?: number
  route?: string
  model?: string
  /** undefined = 全部；true 只看成功；false 只看失败 */
  ok?: boolean
  /** RFC3339 时间字符串，只取该时间之后的记录 */
  since?: string
}

/** 最近请求记录（过滤参数由网关侧做；页面分页是前端切片） */
export async function listTraces(query: TraceQuery = {}, opt?: RequestOptions): Promise<TraceRecord[]> {
  const params: Record<string, unknown> = { limit: query.limit ?? 50 }
  if (query.route) params.route = query.route
  if (query.model) params.model = query.model
  if (query.ok !== undefined) params.ok = query.ok
  if (query.since) params.since = query.since
  const data = await http.get<unknown>(`${BASE}/traces`, params, opt)
  return listItems<TraceRecord>(data, 'traces')
}

/** 单条请求追踪详情（含完整 attempt_log） */
export async function getTrace(id: string, opt?: RequestOptions): Promise<TraceRecord> {
  return http.get<TraceRecord>(`${BASE}/traces/${encodeURIComponent(id)}`, undefined, opt)
}

// ---------------------------------------------------------------------------
// 告警
// ---------------------------------------------------------------------------
/** 告警概况：规则数 / firing / pending / 最近评估时间 / 最近事件 */
export async function getAlertOverview(opt?: RequestOptions): Promise<AlertOverview> {
  return http.get<AlertOverview>(`${BASE}/alerts/overview`, undefined, opt)
}

/** 当前告警规则 */
export async function listAlertRules(opt?: RequestOptions): Promise<AlertRule[]> {
  const data = await http.get<unknown>(`${BASE}/alerts/rules`, undefined, opt)
  return listItems<AlertRule>(data, 'rules')
}

/** 整体替换告警规则（网关热更新，并持久化到网关配置文件） */
export async function putAlertRules(
  items: AlertRule[],
  opt?: RequestOptions,
): Promise<AlertRulesSaveResult> {
  return http.put<AlertRulesSaveResult>(`${BASE}/alerts/rules`, { items }, { timeout: 30000, ...opt })
}

/** 最近告警事件；`status` 为空 = 全部 */
export async function listAlertEvents(
  query: { limit?: number; status?: string } = {},
  opt?: RequestOptions,
): Promise<AlertEvent[]> {
  const params: Record<string, unknown> = { limit: query.limit ?? 100 }
  if (query.status) params.status = query.status
  const data = await http.get<unknown>(`${BASE}/alerts/events`, params, opt)
  return listItems<AlertEvent>(data, 'events')
}

/** 确认（静默）一条告警：`minutes` 内不重复通知 */
export async function ackAlert(
  id: string,
  minutes: number,
  by = 'web',
  opt?: RequestOptions,
): Promise<AlertAckResult> {
  return http.post<AlertAckResult>(
    `${BASE}/alerts/${encodeURIComponent(id)}/ack`,
    { by, minutes },
    { timeout: 15000, ...opt },
  )
}