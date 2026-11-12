/**
 * 路由网关管理接口的类型定义（唯一真源）。
 *
 * 字段名**完全跟随 Go 网关 `gateway/internal/model/types.go` 的 JSON tag**（snake_case），
 * 由 TsukiyoHub 后端 `/api/gateway/*` 原样代理，前端不做驼峰转换。
 *
 * 注意：
 * 1. 这些类型只描述**管理接口**（网关 admin API），与聊天接口的 `types/api.ts` 无关；
 * 2. 所有字段都按「后端可能比前端旧」来渲染兜底，类型上给全是为了编辑器提示；
 * 3. 时间字段是 RFC3339 字符串（Go `time.Time` 默认序列化）。
 */

// ---------------------------------------------------------------------------
// 连通性 / 概况
// ---------------------------------------------------------------------------
/** `GET /api/gateway/health`：网关连通性（不可达时仍返回 200 + reachable=false） */
export interface GatewayHealth {
  /** TsukiyoHub 的 `GATEWAY_ENABLED`；false = 没接入网关 */
  enabled: boolean
  /** 管理接口能否连上（后端会顺手探一次 `/admin/api/overview`） */
  reachable: boolean
  /** 后端配置的网关管理地址，例如 http://127.0.0.1:8082 */
  admin_url: string
  /** 网关对外 OpenAI 兼容地址（可选字段，管理页展示用） */
  public_url?: string
  /** 不可达原因（后端截断后的可读文本） */
  error?: string
  /** 探活耗时（毫秒） */
  latency_ms?: number
  /** 网关版本（可达时由 overview 带回） */
  version?: string
  /** 网关已运行秒数 */
  uptime_sec?: number
}

/** 窗口统计（`GET /api/gateway/stats` 与 `Overview.metrics`） */
export interface StatsSnapshot {
  window_sec: number
  requests: number
  errors: number
  /** 0~1 */
  error_rate: number
  rps: number
  p50_ms: number
  p95_ms: number
  ttft_p95_ms: number
  tokens: number
  cost_usd: number
  rate_limited: number
  budget_rejects: number
  inflight: number
}

/** 供应商探活结果（`GET /api/gateway/providers` 的 items） */
export interface ProviderHealth {
  name: string
  healthy: boolean
  latency_ms: number
  error?: string
  checked_at: string
}

/** `GET /api/gateway/overview`：网关概况（仪表盘首屏） */
export interface Overview {
  version: string
  started_at: string
  uptime_sec: number
  public_url: string
  metrics: StatsSnapshot
  providers: ProviderHealth[]
  /** model -> closed / open / half_open */
  breakers: Record<string, string>
  routes: number
  models: number
  alerts: AlertOverview
  config_path: string
  config_hash: string
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------
/** 路由候选（对应 Go Target） */
export interface RouteTarget {
  model: string
  weight: number
  priority: number
}

/** 路由配置（`GET/PUT /api/gateway/routes` 的 items 元素） */
export interface RouteConfig {
  /** 客户端请求里的 model 字段 */
  alias: string
  /** priority / weighted / round_robin / least_latency / cost / canary / sticky / capability */
  strategy: string
  /** 策略参数（canary.percent 等，结构由策略决定） */
  options: Record<string, unknown>
  targets: RouteTarget[]
  /** 全部目标失败后按顺序再试的模型 ID */
  fallbacks: string[]
  /** tools / vision / context:32000 */
  required_capabilities: string[]
  enabled: boolean
}

// ---------------------------------------------------------------------------
// 请求追踪
// ---------------------------------------------------------------------------
/** 一次调用的用量（对应 Go Usage） */
export interface TraceUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  /** 上游没返回 usage 时为 true（网关按字符数估算） */
  estimated?: boolean
}

/** 一次上游尝试（对应 Go Attempt） */
export interface TraceAttempt {
  model: string
  provider: string
  status: number
  latency_ms: number
  error?: string
}

/** `GET /api/gateway/traces` 的 items 元素（对应 Go RequestRecord） */
export interface TraceRecord {
  id: string
  at: string
  /** 客户端请求的路由别名 */
  route: string
  /** 实际命中的模型 */
  model: string
  provider: string
  status: number
  ok: boolean
  attempts: number
  latency_ms: number
  ttft_ms: number
  stream: boolean
  /** 老版本网关可能没有 usage，渲染时兜底 */
  usage?: TraceUsage
  cost_usd: number
  client_name: string
  client_ip: string
  error?: string
  attempt_log?: TraceAttempt[]
  request_bytes: number
  response_bytes: number
}

// ---------------------------------------------------------------------------
// 告警
// ---------------------------------------------------------------------------
/** 告警级别 */
export type AlertSeverity = 'critical' | 'warning' | 'info'
/** 告警事件状态 */
export type AlertEventStatus = 'pending' | 'firing' | 'resolved' | 'acked'

/**
 * 一条告警规则（对应 Go AlertRule）。
 *
 * 指标字典见 `components/gateway/meta.ts` 的 `METRIC_OPTIONS`（14 个内置指标）。
 * `scope` 形如 `{route: "fast"}` / `{model: "gpt-4o-mini"}` / `{provider: "openai"}`，空 = 全局。
 */
export interface AlertRule {
  id: string
  name: string
  /** 内置指标名，如 error_rate / p95_latency_ms / ttft_ms / tokens … */
  metric: string
  scope: Record<string, string>
  /** > >= < <= == != */
  op: string
  threshold: number
  /** 统计窗口："30s" "1m" "5m" "1h" … */
  window: string
  /** 连续满足多久才 firing（"30s"），空 = 立即 */
  for: string
  severity: AlertSeverity
  message: string
  /** 重复通知冷却，如 "5m" */
  cooldown: string
  enabled: boolean
}

/** 一条告警事件（对应 Go AlertEvent） */
export interface AlertEvent {
  id: string
  rule_id: string
  rule_name: string
  metric: string
  scope: Record<string, string>
  value: number
  threshold: number
  op: string
  severity: AlertSeverity
  message: string
  status: AlertEventStatus
  starts_at: string
  ends_at?: string | null
  acked_by?: string
  acked_at?: string | null
  silenced_until?: string | null
  notified: boolean
}

/** `GET /api/gateway/alerts/overview`：告警概况（对应 Go AlertOverview） */
export interface AlertOverview {
  enabled: boolean
  rules: number
  firing: number
  pending: number
  last_check: string
  /** 最近事件（网关给一小段，页面事件表另有分页接口） */
  recent: AlertEvent[]
}

// ---------------------------------------------------------------------------
// 网关完整配置（只在管理页只读使用：供应商类型/base_url、模型清单）
// ---------------------------------------------------------------------------
/** 调用方凭证（密钥已脱敏） */
export interface GatewayClientConfig {
  key?: string
  name: string
  enabled: boolean
  routes?: string[]
  rpm?: number
  tpm?: number
  monthly_usd?: number
}

/** 上游供应商（api_key 已脱敏） */
export interface GatewayProviderConfig {
  name: string
  type: string
  base_url: string
  api_key?: string
  headers?: Record<string, string>
  timeout_ms?: number
  health_path?: string
  enabled: boolean
  stream_usage?: boolean
}

/** 可路由模型 */
export interface GatewayModelConfig {
  id: string
  provider: string
  upstream?: string
  context?: number
  supports_tools?: boolean
  supports_vision?: boolean
  input_price?: number
  output_price?: number
  tags?: string[]
  enabled: boolean
}

/** 告警子配置（规则 + 通知渠道） */
export interface GatewayAlertConfig {
  enabled: boolean
  interval_ms: number
  rules: AlertRule[]
  notifiers: Array<Record<string, unknown>>
  default_cooldown_seconds: number
}

/**
 * `GET /api/gateway/config`：完整网关配置（密钥已脱敏）+ `config_hash`。
 * 字段全部可选：后端版本差异不该让页面白屏，页面只读 providers/models/routes。
 */
export interface GatewayConfig {
  listen?: string
  admin_listen?: string
  admin_token?: string
  public_url?: string
  clients?: GatewayClientConfig[]
  providers?: GatewayProviderConfig[]
  models?: GatewayModelConfig[]
  routes?: RouteConfig[]
  default_route?: string
  max_attempts?: number
  concurrency?: number
  breaker?: Record<string, unknown>
  budget?: Record<string, unknown>
  tracing?: Record<string, unknown>
  alerts?: GatewayAlertConfig
  timeouts?: Record<string, unknown>
  config_hash?: string
  needs_restart?: boolean
}

/** `PUT /api/gateway/config` 的返回：新配置 + needs_restart（字段可能平铺） */
export type GatewayConfigSaveResult = GatewayConfig & { needs_restart?: boolean }

/** 路由保存结果 */
export interface RoutesSaveResult {
  ok: boolean
  reloaded?: boolean
}

/** 告警规则保存结果 */
export interface AlertRulesSaveResult {
  ok: boolean
}

/** 告警确认（Ack）结果 */
export interface AlertAckResult {
  ok: boolean
}