/**
 * 网关管理页共用的小工具：指标字典、策略/运算符选项、级别与状态配色、格式化。
 *
 * 集中放这里的原因：RoutingView / AlertsView 与 8 个子组件都要用同一套文案和颜色，
 * 散在各处很容易出现「同一个 firing 两种红」。
 */
import type { AlertEventStatus, AlertRule } from '@/types/gateway'

export interface SelectOption {
  value: string
  label: string
}

/** 指标卡条目（StatCards 组件与页面共用的形状） */
export interface StatCardItem {
  label: string
  value: string
  hint?: string
  /** 数值语义色：正常 / 健康 / 警告 / 错误 */
  tone?: 'normal' | 'ok' | 'warn' | 'error'
}

/** 请求追踪的过滤条件（route/model/ok 由网关过滤） */
export interface TraceFilters {
  route: string
  model: string
  result: 'all' | 'ok' | 'fail'
}

/** 14 个内置指标（与 `gateway/internal/model/types.go` 的 AlertRule 注释一一对应） */
export const METRIC_OPTIONS: Array<SelectOption & { hint: string }> = [
  { value: 'error_rate', label: '错误率', hint: '窗口错误率 0~1' },
  { value: 'p95_latency_ms', label: 'P95 延迟', hint: '窗口 P95 延迟（ms）' },
  { value: 'p50_latency_ms', label: 'P50 延迟', hint: '窗口 P50 延迟（ms）' },
  { value: 'ttft_ms', label: '首字延迟', hint: '窗口 P95 首字延迟（ms）' },
  { value: 'rps', label: '每秒请求数', hint: 'RPS' },
  { value: 'inflight', label: '在途请求数', hint: '瞬时值' },
  { value: 'breaker_open', label: '熔断模型数', hint: '瞬时值' },
  { value: 'rate_limited', label: '限流请求数', hint: '窗口计数' },
  { value: 'budget_rejected', label: '预算拒绝数', hint: '窗口计数' },
  { value: 'provider_down', label: '异常供应商数', hint: '瞬时值' },
  { value: 'requests', label: '请求数', hint: '窗口计数' },
  { value: 'error_count', label: '错误数', hint: '窗口计数' },
  { value: 'tokens', label: 'Token 数', hint: '窗口累计' },
  { value: 'cost_usd', label: '费用', hint: '窗口累计（USD）' },
]

const METRIC_LABEL = new Map(METRIC_OPTIONS.map((m) => [m.value, m.label]))

export function metricLabel(metric?: string): string {
  if (!metric) return '-'
  return METRIC_LABEL.get(metric) || metric
}

/** 选路策略（与 `internal/strategy.New` 注册表一致） */
export const STRATEGY_OPTIONS: SelectOption[] = [
  { value: 'priority', label: 'priority · 按优先级降级' },
  { value: 'weighted', label: 'weighted · 加权随机' },
  { value: 'round_robin', label: 'round_robin · 轮询' },
  { value: 'least_latency', label: 'least_latency · 最低延迟' },
  { value: 'cost', label: 'cost · 最低成本' },
  { value: 'canary', label: 'canary · 灰度' },
  { value: 'sticky', label: 'sticky · 会话粘滞' },
  { value: 'capability', label: 'capability · 能力过滤' },
]

export const OP_OPTIONS: SelectOption[] = [
  { value: '>', label: '>' },
  { value: '>=', label: '>=' },
  { value: '<', label: '<' },
  { value: '<=', label: '<=' },
  { value: '==', label: '==' },
  { value: '!=', label: '!=' },
]

export const WINDOW_OPTIONS: SelectOption[] = [
  { value: '30s', label: '30 秒' },
  { value: '1m', label: '1 分钟' },
  { value: '5m', label: '5 分钟' },
  { value: '10m', label: '10 分钟' },
  { value: '15m', label: '15 分钟' },
  { value: '1h', label: '1 小时' },
]

export const FOR_OPTIONS: SelectOption[] = [
  { value: '', label: '立即触发' },
  { value: '30s', label: '持续 30 秒' },
  { value: '1m', label: '持续 1 分钟' },
  { value: '2m', label: '持续 2 分钟' },
  { value: '5m', label: '持续 5 分钟' },
]

export const COOLDOWN_OPTIONS: SelectOption[] = [
  { value: '1m', label: '1 分钟' },
  { value: '5m', label: '5 分钟' },
  { value: '15m', label: '15 分钟' },
  { value: '30m', label: '30 分钟' },
  { value: '1h', label: '1 小时' },
]

export const SEVERITY_OPTIONS: Array<SelectOption & { color: string }> = [
  { value: 'critical', label: 'critical（严重）', color: 'error' },
  { value: 'warning', label: 'warning（警告）', color: 'warning' },
  { value: 'info', label: 'info（提示）', color: 'blue' },
]

/** 作用域维度（空 = 全局） */
export const SCOPE_OPTIONS: SelectOption[] = [
  { value: '', label: '全局（不限）' },
  { value: 'route', label: 'route（路由别名）' },
  { value: 'model', label: 'model（模型 ID）' },
  { value: 'provider', label: 'provider（供应商名）' },
]

interface TagMeta {
  label: string
  color: string
}

const SEVERITY_META: Record<string, TagMeta> = {
  critical: { label: '严重', color: 'error' },
  warning: { label: '警告', color: 'warning' },
  info: { label: '提示', color: 'blue' },
}

export function severityMeta(value?: string): TagMeta {
  return SEVERITY_META[value || ''] || { label: value || '-', color: 'default' }
}

const EVENT_STATUS_META: Record<AlertEventStatus, TagMeta> = {
  pending: { label: 'pending', color: 'warning' },
  firing: { label: 'firing', color: 'error' },
  resolved: { label: 'resolved', color: 'success' },
  acked: { label: 'acked', color: 'blue' },
}

export function eventStatusMeta(value?: string): TagMeta {
  return EVENT_STATUS_META[value as AlertEventStatus] || { label: value || '-', color: 'default' }
}

/** 作用域对象 → 可读文本 */
export function scopeText(scope?: Record<string, string> | null): string {
  if (!scope) return '全局'
  const parts = Object.entries(scope)
    .filter(([, v]) => v !== undefined && v !== null && `${v}` !== '')
    .map(([k, v]) => `${k}=${v}`)
  return parts.length ? parts.join(' · ') : '全局'
}

/** 规则条件文本：`metric op threshold` */
export function conditionText(rule: Pick<AlertRule, 'metric' | 'op' | 'threshold'>): string {
  return `${metricLabel(rule.metric)} ${rule.op || '>'} ${fmtThreshold(rule.threshold)}`
}

/** 阈值/指标值：整数不加小数，小数最多保留 3 位 */
export function fmtThreshold(n?: number | null): string {
  if (n == null || !Number.isFinite(n)) return '-'
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
}

/** 金额：小于 1 美元保留 4 位，避免成本卡显示 $0.00 */
export function fmtMoney(n?: number | null): string {
  if (n == null || !Number.isFinite(n)) return '-'
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`
}

/** 运行时长：1 天 2 小时 / 3 小时 12 分 / 5 分 */
export function fmtUptime(sec?: number | null): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '-'
  const total = Math.floor(sec)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days} 天 ${hours} 小时`
  if (hours > 0) return `${hours} 小时 ${minutes} 分`
  if (minutes > 0) return `${minutes} 分`
  return `${total} 秒`
}

/** 持续时间：从 start 到 end（end 为空则算到此刻） */
export function fmtDurationBetween(start?: string | null, end?: string | null): string {
  if (!start) return '-'
  const from = Date.parse(start)
  if (!Number.isFinite(from)) return '-'
  const to = end ? Date.parse(end) : Date.now()
  if (!Number.isFinite(to)) return '-'
  const sec = Math.max(0, Math.round((to - from) / 1000))
  if (sec < 60) return `${sec}s`
  const minutes = Math.floor(sec / 60)
  const restSec = sec % 60
  if (minutes < 60) return restSec ? `${minutes}m${restSec}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const restMin = minutes % 60
  return restMin ? `${hours}h${restMin}m` : `${hours}h`
}

/** 配置指纹短展示（完整值放 tooltip） */
export function shortHash(hash?: string | null, len = 12): string {
  if (!hash) return '-'
  return hash.length > len ? `${hash.slice(0, len)}…` : hash
}

/** 安全 JSON 文本（用于 <pre> 展示，永不抛错） */
export function safeJsonText(value: unknown): string {
  try {
    if (value === undefined || value === null) return '{}'
    return JSON.stringify(value, null, 2)
  } catch {
    return '（无法序列化）'
  }
}

/** 逗号/空格分隔的输入 → 去重数组（降级链、能力要求用） */
export function parseList(text: string): string[] {
  return Array.from(
    new Set(
      (text || '')
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  )
}

/** 今天 00:00 的时间戳（本地时区），用于「今日 token / 成本」统计 */
export function startOfToday(): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}