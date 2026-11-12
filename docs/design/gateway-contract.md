# LLM 路由网关 + 告警：实现契约（Lead 定义，实现者必须遵守）

> 目标：在 `E:\Project\TsukiyoHub\gateway` 下实现一个**可用的、可插拔的** OpenAI 兼容 LLM 路由网关，
> 带 Prometheus 指标、请求追踪、熔断/限流/预算，以及**内置告警引擎**（同时提供 Prometheus/Alertmanager 配置）。
> 这是 TsukiyoHub 的「路由网关 + 告警」能力，管理界面在 `web/`（路由页 / 告警页）。

## 技术选型（已定，别改）

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | **Go 1.22+，只用标准库** | 单静态二进制、零依赖、离线可构建；Go 1.27 工具链已装在 `E:\Project\HQDingtalkRobotUI\.toolchains\go` |
| 可观测 | **Prometheus 文本格式 `/metrics`**（手写 exposition）+ 内置窗口聚合 | 不引入 client_golang，部署零依赖；同时提供 Prometheus/Alertmanager 配置做外部告警 |
| eBPF | **不使用** | 网关是应用层（HTTP/SSE）转发，eBPF 只对 L4 观测有意义且仅 Linux；在 `docs/gateway.md` 的「可选扩展」里写清接入点 |
| Rust | 不使用 | 单语言降低运维成本；性能瓶颈在上游模型，不在网关 |

## 目录与所有权

```
gateway/
├─ go.mod                                   [Lead]（module tsukiyohub/gateway，无第三方依赖）
├─ cmd/tsukiyo-gateway/main.go              [Lead] 装配与启动
├─ internal/
│  ├─ model/types.go                        [Lead] 数据契约（已完成，别改）
│  ├─ config/config.go                      [Lead] 配置加载/校验/脱敏/保存（已完成）
│  ├─ metrics/metrics.go                    [Lead] Prometheus + 窗口聚合（已完成）
│  ├─ provider/                             [gateway-core] 上游供应商与 HTTP 客户端
│  ├─ strategy/                             [gateway-core] 选路策略（可插拔）
│  ├─ proxy/                                [gateway-core] /v1/chat/completions 等入口
│  ├─ middleware/                            [gateway-core] 鉴权/限流/预算/熔断/观测
│  ├─ tracing/                              [gateway-core] 请求追踪环形缓冲
│  ├─ admin/                                [gateway-core] 管理接口（overview/config/providers/routes/traces/metrics）
│  └─ alert/                                [alert-engine] 告警引擎 + 通知渠道
├─ deploy/prometheus/                       [alert-engine] prometheus.yml + alerts.yml
├─ deploy/alertmanager/                     [alert-engine] alertmanager.yml
├─ config.example.json                      [Lead]
└─ docs/                                    [各自] gateway.md [Lead] / alerts.md [alert-engine]
```

**只改自己所有权内的文件。** 需要改 `model` / `config` / `metrics` 契约时，发消息给 Lead，不要自己动。

## 必须实现的包 API（签名固定，便于 Lead 装配 main.go）

### provider（`internal/provider`）

```go
package provider

// Endpoint 一个可调用的「模型 + 供应商」组合。
type Endpoint struct {
    Model    string          // 对外 model id（ModelDef.ID）
    Upstream string          // 上游真实模型名
    Provider model.Provider  // 供应商配置
    Def      model.ModelDef
}

// Registry 供应商与模型注册表（配置变更时整体重建）。
type Registry struct { /* ... */ }

func NewRegistry(cfg *model.Config) (*Registry, error)
func (r *Registry) Resolve(modelID string) (*Endpoint, error)
func (r *Registry) All() []*Endpoint
// Do 发起一次上游请求：path 形如 "/chat/completions"；url 由 base_url 拼接；
// 自动注入 Authorization/额外 Headers、超时（connect/header）、SSE 透传所需的 DisableCompression。
// 返回值是原始 *http.Response，调用方负责读 body/关闭。
func (r *Registry) Do(ctx context.Context, ep *Endpoint, path string, body []byte) (*http.Response, error)
// Probe 探活：GET base_url + health_path，200 视为健康。
func (r *Registry) Probe(ctx context.Context, name string) model.ProviderHealth
func (r *Registry) ProbeAll(ctx context.Context) []model.ProviderHealth
```

### strategy（`internal/strategy`）

```go
package strategy

// Stats 供策略使用的统计视图（由 proxy 侧实现并传入）。
type Stats interface {
    // LatencyEWMA 返回模型最近的平均延迟（毫秒）；无数据返回 0
    LatencyEWMA(modelID string) float64
    // BreakerState 返回 "closed" / "open" / "half_open"
    BreakerState(modelID string) string
}

// PickContext 选路上下文。
type PickContext struct {
    Request  *model.ChatRequest
    Embedding *model.EmbeddingRequest
    Route    model.Route
    Attempt  int                 // 从 1 开始
    Excluded map[string]bool     // 已失败的模型（本次请求内不再选）
    Stats    Stats
    Client   model.ClientKey
}

// Strategy 选路策略：返回**按优先级排好序**的候选列表；proxy 会跳过 Excluded / 熔断中的模型。
type Strategy interface {
    Name() string
    Order(ctx *PickContext) ([]model.Target, error)
}

// New 按名字构造策略；options 来自 Route.Options。
func New(name string, options map[string]any) (Strategy, error)
func Names() []string
```

必须实现的内置策略：

| name | 行为 |
|---|---|
| `priority` | 按 `Target.Priority` 升序（相同则按配置顺序），失败逐个降级 |
| `weighted` | 按 `Target.Weight` 加权随机 |
| `round_robin` | 按目标顺序轮转（进程内计数） |
| `least_latency` | 按 `Stats.LatencyEWMA` 升序；无数据的排前面 |
| `cost` | 按模型价格（input+output）升序 |
| `canary` | `Options.percent`（默认 10）概率把流量给第一个目标，其余给第二组 |
| `sticky` | 按 `Request.User`（空则用会话首条消息 hash）稳定哈希选一个目标 |
| `capability` | 先按路由 `RequiredCapabilities`（tools / vision / context:32000）过滤，再按 least_latency |

新增策略只需实现 `Strategy` 并在 `New` 的注册表里登记——**这就是「可插拔」的落点**。

### middleware（`internal/middleware`）

```go
package middleware

// Guard 一次请求进入代理前的检查；返回错误表示拒绝（已写好响应）。
type Guard interface {
    Name() string
    Check(ctx context.Context, in *GuardInput) error
}

type GuardInput struct {
    Request *http.Request
    Config  *model.Config
    Client  model.ClientKey
}

// Chain 依次执行 Guard（顺序即安全顺序：鉴权 → 限流 → 预算）。
type Chain struct{ /* ... */ }
func NewChain(guards ...Guard) *Chain
func (c *Chain) Run(ctx context.Context, in *GuardInput) error

// NewBreakers 熔断管理器（按模型维度）。
func NewBreakers(cfg model.BreakerConfig) *Breakers
func (b *Breakers) Allow(modelID string) bool
func (b *Breakers) Success(modelID string)
func (b *Breakers) Failure(modelID string)
func (b *Breakers) State(modelID string) string   // closed / open / half_open
func (b *Breakers) Snapshot() map[string]string
func (b *Breakers) OpenCount() int64
func (b *Breakers) Sweep()                        // 后台定期调用，用于 half-open 恢复

// 内置 Guard：APIKeyGuard（鉴权 + 找到 ClientKey）、RateLimitGuard（RPM/TPM 令牌桶）、
// BudgetGuard（累计金额，超预算返回 402）。
```

### tracing（`internal/tracing`）

```go
package tracing

// Ring 请求追踪环形缓冲。
type Ring struct{ /* ... */ }
func NewRing(capacity int) *Ring
func (r *Ring) Add(rec model.RequestRecord)
// List 过滤：status 为空 = 全部；ok 为 nil = 全部；since 为零值 = 不限。
func (r *Ring) List(filter Filter) []model.RequestRecord
func (r *Ring) Get(id string) (model.RequestRecord, bool)

type Filter struct {
    Limit  int
    Status *bool      // true=只看成功，false=只看失败
    Route  string
    Model  string
    Since  time.Time
}
```

### proxy（`internal/proxy`）

```go
package proxy

type Options struct {
    Config     ConfigSource        // interface{ Current() *model.Config }
    Providers  *provider.Registry
    Breakers   *middleware.Breakers
    Guards     *middleware.Chain
    Metrics    *metrics.Registry
    Aggregator *metrics.Aggregator
    Tracer     *tracing.Ring
    Alerts     model.AlertEngine   // 可为 nil
    Version    string
}

// ConfigSource 由 Lead 在 cmd 里实现（atomic.Pointer[model.Config]）。
type ConfigSource interface{ Current() *model.Config }

func New(opts Options) *Handler
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request)
```

`ServeHTTP` 负责（**必须全部实现**）：

| 路径 | 行为 |
|---|---|
| `GET /v1/models` | 返回路由别名列表（OpenAI 格式） |
| `POST /v1/chat/completions` | 路由 → 上游 → 透传（含 SSE 流式）；记录 usage/成本/耗时/TTFT |
| `POST /v1/embeddings` | 同上（非流式） |
| `GET /healthz` | `{"status":"ok"}` |
| `GET /readyz` | 至少一个供应商健康才 200 |
| 其它 | 404 |

核心逻辑：
1. **鉴权**（Guards）→ 2. 选路（strategy.New + Order）→ 3. 逐候选尝试（跳过熔断/Excluded）：
   `Providers.Do` → 2xx 成功（流式则边转发边统计 TTFT）→ 非 2xx 记失败、熔断 Failure、换下一个；
4. 全部失败返回 `502` + 结构化错误（含 attempts）；
5. 结束记录 `model.RequestRecord`：写入 `Tracer`、`Aggregator.Record`、Prometheus 指标；
6. 流式响应头必须带 `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`；
   转发时**逐块 flush**；解析流内 `data: {...}` 的 `usage`（没有则按字符数估算）；
7. 请求/响应体大小、错误摘要写入记录。

### admin（`internal/admin`）

```go
package admin

type Options struct {
    ConfigPath string
    Config     ConfigSource  // 与 proxy 共用同一个 source
    Reload     func(next *model.Config) error   // 由 Lead 在 cmd 里实现（校验+落盘+重建 provider/proxy 配置）
    Providers  *provider.Registry
    Breakers   *middleware.Breakers
    Aggregator *metrics.Aggregator
    Tracer     *tracing.Ring
    Alerts     model.AlertEngine
    Metrics    *metrics.Registry
    Version    string
    StartedAt  time.Time
}

func New(opts Options) http.Handler
```

前缀 `/admin/api`：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/overview` | `model.Overview`（含 StatsSnapshot、providers 健康、断熔器状态、告警概况） |
| GET | `/admin/api/config` | `config.Redacted(cfg)` + `config_hash` + `needs_restart` |
| PUT | `/admin/api/config` | body 完整 Config；`MergeSecrets` → `Reload` → 返回新配置；**listen/admin_listen 变更返回 needs_restart=true** |
| POST | `/admin/api/config/reload` | 从文件重新加载 |
| GET | `/admin/api/providers` | 健康列表 |
| POST | `/admin/api/providers/{name}/probe` | 主动探活 |
| GET | `/admin/api/routes` | 路由 + 目标 + 策略 |
| PUT | `/admin/api/routes` | 只替换 routes 并走 Reload |
| GET | `/admin/api/traces?limit&route&model&ok&since` | 最近请求记录 |
| GET | `/admin/api/traces/{id}` | 单条 |
| GET | `/admin/api/stats?window=300&route=&model=&provider=` | StatsSnapshot |
| GET | `/metrics` | Prometheus 文本（由 Lead 的 cmd 挂，或 admin 暴露均可） |
| GET | `/admin/api/alerts/rules` | 规则 |
| PUT | `/admin/api/alerts/rules` | 整体替换规则（调 `Alerts.ReplaceRules`） |
| GET | `/admin/api/alerts/events?limit&status` | 事件 |
| POST | `/admin/api/alerts/{id}/ack` | 确认（body `{"by":"web","minutes":30}`） |
| GET | `/admin/api/alerts/overview` | `model.AlertOverview` |

管理接口鉴权：`AdminToken` 非空时要求 `Authorization: Bearer <token>`；为空时只允许 `127.0.0.1/::1` 访问。
返回统一 JSON；错误 `{"error":"..."}` + 合适状态码。

### alert（`internal/alert`，alert-engine 负责）

```go
package alert

type Engine struct{ /* ... */ }
// New 构造：cfg 来自 model.Config；agg 提供窗口统计；logger 写标准库 log。
func New(cfg *model.Config, agg *metrics.Aggregator, logger *log.Logger) *Engine

// 实现 model.AlertEngine 全部方法：Start/Stop/Overview/ListRules/ReplaceRules/ListEvents/Ack/Notifiers。
```

评估语义：
* 每 `Alerts.IntervalMS` 评估一次所有启用的规则；
* 窗口 = `rule.Window`（`"30s" "1m" "5m" "10m"`），从 `Aggregator.Query(window, scope)` 取值；
* 瞬时指标（`inflight` / `breaker_open` / `provider_down`）走 `Aggregator.Instant()`；
* `for` 内的连续满足 → `status=pending`；满足 `for` 时长后 → `firing` 并**发通知**；
* 指标恢复（不再满足）→ `resolved`，也发一条「已恢复」；
* 同规则在 `cooldown` 内不重复通知；`Ack` 后 `silence` 期间不发；
* 事件环形保存最近 200 条；返回给管理接口；
* 通知渠道实现 `Notifier` 接口并注册：`log` / `webhook`（POST JSON）/ `dingtalk`（机器人 markdown + 可选加签）/ `slack`（text）；
* 通知内容包含：规则名、指标值、阈值、窗口、级别、发生时间、网关地址。

**内置规则**（写进 `config.example.json`，开箱即用）：
1. `error_rate_high`：全局 error_rate > 0.1 持续 1m，critical
2. `latency_p95_high`：全局 p95_latency_ms > 5000 持续 2m，warning
3. `ttft_slow`：全局 ttft_ms > 8000 持续 2m，warning
4. `breaker_open`：breaker_open > 0 持续 30s，critical
5. `provider_down`：provider_down > 0 持续 1m，critical
6. `budget_rejected`：budget_rejected > 0 持续 1m，critical
7. `rate_limited_spike`：rate_limited > 20 持续 1m，warning

### Prometheus 部署物（alert-engine 负责）

* `deploy/prometheus/prometheus.yml`：抓 `gateway:8081/metrics`（或 8082），30s 间隔
* `deploy/prometheus/alerts.yml`：基于网关指标的告警规则（error rate / p95 / breaker / down），
  使用 `tsukiyo_gateway_requests_total`、`tsukiyo_gateway_request_duration_seconds_bucket`、
  `tsukiyo_gateway_breaker_state`、`tsukiyo_gateway_provider_up` 等**网关实现时必须暴露的指标名**
* `deploy/alertmanager/alertmanager.yml`：webhook 示例
* `gateway/docs/alerts.md`：内置告警引擎与外部 Prometheus 两条链路怎么用、怎么加规则/渠道

## 网关必须暴露的 Prometheus 指标名（gateway-core 实现，alert-engine 写规则时引用）

```
tsukiyo_gateway_requests_total{route,model,provider,status}
tsukiyo_gateway_request_duration_seconds_bucket{route,model,provider,le}
tsukiyo_gateway_request_duration_seconds_sum / _count
tsukiyo_gateway_ttft_seconds_bucket{model}
tsukiyo_gateway_tokens_total{model,direction="prompt|completion"}
tsukiyo_gateway_cost_usd_total{model}
tsukiyo_gateway_inflight
tsukiyo_gateway_breaker_state{model}        // 0=closed,1=half_open,2=open
tsukiyo_gateway_provider_up{provider}       // 1/0
tsukiyo_gateway_ratelimit_rejected_total{client}
tsukiyo_gateway_budget_rejected_total{client}
tsukiyo_gateway_alerts_firing
```

## 验收标准（Lead 会实测）

1. `go build ./...` 与 `go vet ./...` 通过；`go test ./...` 通过（每个包至少 1 个测试）。
2. 起一个**假上游**（Python 或 Go 都行，Lead 提供脚本），验证：
   - 非流式请求经网关透传，usage/成本/指标正确；
   - 流式请求逐块转发、TTFT 有值、客户端能正常结束；
   - 上游 500 时自动切换 fallback 模型（attempts>1）；
   - 熔断：连续失败后该模型被跳过；
   - 限流：超过 RPM 返回 429；预算：超预算返回 402；
   - `/metrics` 可被 Prometheus 解析；`/admin/api/overview` 数据合理；
   - 触发一条告警（阈值设 0）→ `/admin/api/alerts/events` 里出现 firing，且 notifier 收到请求；
3. 配置热更新：PUT 一个新增路由 → 不重启即可调用。
4. 代码注释中文、`go vet` 干净；不要引入任何第三方依赖。