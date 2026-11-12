// Package model 定义网关的共享数据契约。
//
// 设计原则：
//  1. **只用标准库类型**（不引入第三方 SDK）：Provider / Strategy / Middleware / Notifier
//     全部是接口，实现在各自包里，通过注册表装配——换实现不改这里。
//  2. **配置即契约**：Config 就是 config.json 的结构，热更新时整体替换；
//     所有实现只读 Config 的副本，不持有可变指针。
//  3. **可观测性一等公民**：每个请求都落 RequestRecord，指标聚合与告警都从它派生。
package model

import (
	"encoding/json"
	"time"
)

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

// Config 是网关的完整配置（对应 config.json）。
type Config struct {
	// Listen 客户端入口（OpenAI 兼容 HTTP / SSE），例如 ":8081"
	Listen string `json:"listen"`
	// AdminListen 管理接口，例如 ":8082"；留空则与 Listen 同端口（路径不冲突）
	AdminListen string `json:"admin_listen"`
	// AdminToken 管理接口鉴权；留空 = 仅本机可访问（127.0.0.1）
	AdminToken string `json:"admin_token"`
	// PublicURL 对外可访问的网关地址（写进 /v1/models 与文档）
	PublicURL string `json:"public_url"`

	// Clients 调用方 API Key 与配额
	Clients []ClientKey `json:"clients"`
	// Providers 上游供应商（任何 OpenAI 兼容端点）
	Providers []Provider `json:"providers"`
	// Models 可被路由的模型（供应商 + 上游模型名 + 能力/价格）
	Models []ModelDef `json:"models"`
	// Routes 路由别名：客户端请求的 model 字段 → 一组候选模型 + 选路策略
	Routes []Route `json:"routes"`

	// DefaultRoute 未匹配到别名时使用的路由（可用 "priority" 策略链）
	DefaultRoute string `json:"default_route"`
	// MaxAttempts 单次请求最多尝试几个上游（含首次）
	MaxAttempts int `json:"max_attempts"`
	// Concurrency 网关全局在途请求上限（0 = 不限）
	Concurrency int `json:"concurrency"`

	// Breaker 熔断参数（按模型维度）
	Breaker BreakerConfig `json:"breaker"`
	// Budget 预算（按 client 累计 token 与金额）
	Budget BudgetConfig `json:"budget"`
	// Tracing 请求追踪环形缓冲
	Tracing TracingConfig `json:"tracing"`
	// Alerts 告警引擎与通知渠道
	Alerts AlertConfig `json:"alerts"`
	// Timeouts 超时（毫秒）
	Timeouts TimeoutConfig `json:"timeouts"`
}

// ClientKey 一个调用方凭证与配额。
type ClientKey struct {
	Key      string   `json:"key"`      // 客户端携带的 sk-...（可被 ${ENV} 注入）
	Name     string   `json:"name"`     // 展示名
	Enabled  bool     `json:"enabled"`  // 默认 true
	Routes   []string `json:"routes"`   // 允许的路由别名；空 = 全部
	RPM      int      `json:"rpm"`      // 每分钟请求上限（0 = 不限）
	TPM      int      `json:"tpm"`      // 每分钟 token 上限（0 = 不限）
	MonthlyUSD float64 `json:"monthly_usd"` // 每月预算（0 = 不限）
}

// Provider 一个 OpenAI 兼容上游。
type Provider struct {
	Name     string            `json:"name"`
	Type     string            `json:"type"`      // openai / azure / ollama / vllm / custom
	BaseURL  string            `json:"base_url"`  // 不含 /chat/completions
	APIKey   string            `json:"api_key"`   // 支持 ${ENV}
	Headers  map[string]string `json:"headers"`   // 额外请求头
	TimeoutMS int              `json:"timeout_ms"` // 单次调用超时（0 = 用全局）
	HealthPath string          `json:"health_path"`// 探活路径，默认 /models
	Enabled  bool              `json:"enabled"`
	// StreamUsage 上游流式响应是否带 usage（OpenAI 需 stream_options.include_usage）
	StreamUsage bool `json:"stream_usage"`
}

// ModelDef 一个可路由的模型。
type ModelDef struct {
	ID          string   `json:"id"`          // 对外模型名（= 路由候选名）
	Provider    string   `json:"provider"`    // Provider.Name
	Upstream    string   `json:"upstream"`    // 上游真实模型名，空 = 同名
	Context     int      `json:"context"`     // 上下文长度（能力路由用）
	SupportsTools   bool `json:"supports_tools"`
	SupportsVision  bool `json:"supports_vision"`
	InputPrice  float64  `json:"input_price"` // 每 1K tokens 美元
	OutputPrice float64  `json:"output_price"`
	Tags        []string `json:"tags"`
	Enabled     bool     `json:"enabled"`
}

// Route 路由别名与候选。
type Route struct {
	Alias     string      `json:"alias"`      // 客户端 model 字段
	Strategy  string      `json:"strategy"`   // priority / weighted / round_robin / least_latency / cost / canary / sticky / capability
	Options   map[string]any `json:"options"` // 策略参数
	Targets   []Target    `json:"targets"`
	Fallbacks []string    `json:"fallbacks"`  // 全部失败后按顺序再试的模型 ID
	// RequiredCapabilities 能力过滤（tools / vision / context:32000）
	RequiredCapabilities []string `json:"required_capabilities"`
	Enabled   bool        `json:"enabled"`
}

// Target 路由候选。
type Target struct {
	Model    string `json:"model"`
	Weight   int    `json:"weight"`
	Priority int    `json:"priority"`
}

// BreakerConfig 熔断。
type BreakerConfig struct {
	Enabled            bool    `json:"enabled"`
	FailureThreshold   int     `json:"failure_threshold"`   // 连续失败次数
	OpenSeconds        int     `json:"open_seconds"`        // 熔断持续时间
	HalfOpenMax        int     `json:"half_open_max"`       // 半开状态放行数
	ErrorRateThreshold float64 `json:"error_rate_threshold"` // 窗口错误率（0~1，0 = 不看）
	WindowSeconds      int     `json:"window_seconds"`
}

// BudgetConfig 预算。
type BudgetConfig struct {
	Enabled     bool `json:"enabled"`
	WindowDays  int  `json:"window_days"`   // 统计窗口（0 = 当月）
	RejectOnOver bool `json:"reject_on_over"`
}

// TracingConfig 追踪。
type TracingConfig struct {
	Enabled bool `json:"enabled"`
	MaxRecords int `json:"max_records"` // 环形缓冲容量，默认 2000
}

// AlertConfig 告警。
type AlertConfig struct {
	Enabled        bool             `json:"enabled"`
	IntervalMS     int              `json:"interval_ms"`      // 评估间隔，默认 15000
	Rules          []AlertRule      `json:"rules"`
	Notifiers      []NotifierConfig `json:"notifiers"`
	DefaultCooldownSeconds int      `json:"default_cooldown_seconds"`
}

// AlertRule 一条告警规则。
//
// 内置指标（Metric）：
//   error_rate        窗口错误率（0~1）
//   p95_latency_ms    窗口 P95 延迟
//   p50_latency_ms    窗口 P50 延迟
//   ttft_ms           窗口 P95 首字延迟
//   rps               每秒请求数
//   inflight          在途请求数（瞬时）
//   breaker_open      熔断中的模型数（瞬时）
//   rate_limited      窗口内被限流的请求数
//   budget_rejected   窗口内因预算被拒的请求数
//   provider_down     探活失败的供应商数（瞬时）
//   requests          窗口内请求数
//   error_count       窗口内错误数
//   tokens            窗口内 token 数
//   cost_usd          窗口内费用
//
// Scope 形如 {"route":"fast"} / {"model":"gpt-4o-mini"} / {"provider":"openai"}；空 = 全局。
type AlertRule struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	Metric    string            `json:"metric"`
	Scope     map[string]string `json:"scope"`
	Op        string            `json:"op"`       // > >= < <= == !=
	Threshold float64           `json:"threshold"`
	Window    string            `json:"window"`   // "1m" "5m" "1h"
	For       string            `json:"for"`      // 连续满足多久才 firing（"30s"），空 = 立即
	Severity  string            `json:"severity"` // critical / warning / info
	Message   string            `json:"message"`
	Cooldown  string            `json:"cooldown"` // 重复通知冷却
	Enabled   bool              `json:"enabled"`
}

// NotifierConfig 通知渠道。
type NotifierConfig struct {
	Type    string            `json:"type"`    // webhook / dingtalk / slack / log
	Name    string            `json:"name"`
	URL     string            `json:"url"`     // webhook / dingtalk robot / slack 
	Secret  string            `json:"secret"`  // 钉钉加签密钥（可选）
	Headers map[string]string `json:"headers"`
	Enabled bool              `json:"enabled"`
	// MinSeverity 只有达到该级别才发（info/warning/critical），空 = 全部
	MinSeverity string `json:"min_severity"`
}

// TimeoutConfig 超时（毫秒）。
type TimeoutConfig struct {
	// Connect 建连超时
	ConnectMS int `json:"connect_ms"`
	// Header 首字节超时（流式首字）
	HeaderMS int `json:"header_ms"`
	// Total 单次上游调用总超时（0 = 不限，流式建议 0）
	TotalMS int `json:"total_ms"`
}

// ---------------------------------------------------------------------------
// 请求 / 响应（只建模网关关心的字段，其余透传）
// ---------------------------------------------------------------------------

// ChatMessage OpenAI 格式消息。
type ChatMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content,omitempty"`
	Name    string          `json:"name,omitempty"`
}

// ChatRequest 客户端请求（/v1/chat/completions）。
type ChatRequest struct {
	Model       string        `json:"model"`
	Messages    []ChatMessage `json:"messages"`
	Stream      bool          `json:"stream"`
	Temperature *float64      `json:"temperature,omitempty"`
	MaxTokens   *int          `json:"max_tokens,omitempty"`
	Tools       []json.RawMessage `json:"tools,omitempty"`
	User        string        `json:"user,omitempty"`
	// 透传原始字段（保留未知参数）
	Raw map[string]json.RawMessage `json:"-"`
}

// EmbeddingRequest 向量请求（用途统计需要，路由同 Chat）。
type EmbeddingRequest struct {
	Model string          `json:"model"`
	Input json.RawMessage `json:"input"`
	User  string          `json:"user,omitempty"`
}

// Usage 一次调用的用量。
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
	// Estimated 上游没返回 usage 时为 true（按字符数估算）
	Estimated bool `json:"estimated"`
}

// RequestRecord 一次请求的完整记录（指标、追踪、告警都从它派生）。
type RequestRecord struct {
	ID        string    `json:"id"`
	At        time.Time `json:"at"`
	Route     string    `json:"route"`   // 客户端模型别名
	Model     string    `json:"model"`   // 实际命中的模型
	Provider  string    `json:"provider"`
	Status    int       `json:"status"`  // HTTP 状态码
	OK        bool      `json:"ok"`
	Attempts  int       `json:"attempts"`
	LatencyMS int64     `json:"latency_ms"`
	TTFTMS    int64     `json:"ttft_ms"`
	Stream    bool      `json:"stream"`
	Usage     Usage     `json:"usage"`
	CostUSD   float64   `json:"cost_usd"`
	ClientName string   `json:"client_name"`
	ClientIP  string    `json:"client_ip"`
	Error     string    `json:"error,omitempty"`
	AttemptLog []Attempt `json:"attempt_log,omitempty"`
	// 请求/响应体尺寸（排查用）
	RequestBytes  int `json:"request_bytes"`
	ResponseBytes int `json:"response_bytes"`
}

// Attempt 一次上游尝试。
type Attempt struct {
	Model     string `json:"model"`
	Provider  string `json:"provider"`
	Status    int    `json:"status"`
	LatencyMS int64  `json:"latency_ms"`
	Error     string `json:"error,omitempty"`
}

// ProviderHealth 供应商探活结果。
type ProviderHealth struct {
	Name      string    `json:"name"`
	Healthy   bool      `json:"healthy"`
	LatencyMS int64     `json:"latency_ms"`
	Error     string    `json:"error,omitempty"`
	CheckedAt time.Time `json:"checked_at"`
}

// ---------------------------------------------------------------------------
// 告警事件
// ---------------------------------------------------------------------------

// AlertEvent 一条告警事件。
type AlertEvent struct {
	ID        string            `json:"id"`
	RuleID    string            `json:"rule_id"`
	RuleName  string            `json:"rule_name"`
	Metric    string            `json:"metric"`
	Scope     map[string]string `json:"scope"`
	Value     float64           `json:"value"`
	Threshold float64           `json:"threshold"`
	Op        string            `json:"op"`
	Severity  string            `json:"severity"`
	Message   string            `json:"message"`
	Status    string            `json:"status"` // pending / firing / resolved / acked
	StartsAt  time.Time         `json:"starts_at"`
	EndsAt    *time.Time        `json:"ends_at,omitempty"`
	AckedBy   string            `json:"acked_by,omitempty"`
	AckedAt   *time.Time        `json:"acked_at,omitempty"`
	SilencedUntil *time.Time    `json:"silenced_until,omitempty"`
	Notified  bool              `json:"notified"`
}

// AlertEngine 告警引擎接口：网关只依赖它，实现放在 internal/alert。
//
// 这样 admin API 可以在没有告警模块时也能编译（返回“未启用”）。
type AlertEngine interface {
	// Start 启动评估循环（阻塞的 goroutine 由实现自己管理）。
	Start()
	// Stop 停止评估。
	Stop()
	// Overview 返回引擎概况（规则数、firing 数、最近事件）。
	Overview() AlertOverview
	// ListRules 当前规则。
	ListRules() []AlertRule
	// ReplaceRules 热替换规则并持久化到配置文件。
	ReplaceRules(rules []AlertRule) error
	// ListEvents 最近事件；status 为空 = 全部。
	ListEvents(limit int, status string) []AlertEvent
	// Ack 确认一条告警；duration 内不再重复通知。
	Ack(id, by string, duration time.Duration) error
	// Notifiers 当前通知渠道（脱敏）。
	Notifiers() []NotifierView
}

// AlertOverview 告警概况。
type AlertOverview struct {
	Enabled   bool         `json:"enabled"`
	Rules     int          `json:"rules"`
	Firing    int          `json:"firing"`
	Pending   int          `json:"pending"`
	LastCheck time.Time    `json:"last_check"`
	Recent    []AlertEvent `json:"recent"`
}

// NotifierView 通知渠道展示视图。
type NotifierView struct {
	Name        string `json:"name"`
	Type        string `json:"type"`
	Enabled     bool   `json:"enabled"`
	MinSeverity string `json:"min_severity"`
}

// ---------------------------------------------------------------------------
// 管理接口响应
// ---------------------------------------------------------------------------

// Overview 网关概况（前端仪表盘）。
type Overview struct {
	Version     string            `json:"version"`
	StartedAt   time.Time         `json:"started_at"`
	UptimeSec   int64             `json:"uptime_sec"`
	PublicURL   string            `json:"public_url"`
	Metrics     StatsSnapshot     `json:"metrics"`
	Providers   []ProviderHealth  `json:"providers"`
	Breakers    map[string]string `json:"breakers"` // model -> closed/open/half_open
	Routes      int               `json:"routes"`
	Models      int               `json:"models"`
	Alerts      AlertOverview     `json:"alerts"`
	ConfigPath  string            `json:"config_path"`
	ConfigHash  string            `json:"config_hash"`
}

// StatsSnapshot 窗口统计。
type StatsSnapshot struct {
	WindowSec     int     `json:"window_sec"`
	Requests      int64   `json:"requests"`
	Errors        int64   `json:"errors"`
	ErrorRate     float64 `json:"error_rate"`
	RPS           float64 `json:"rps"`
	P50MS         float64 `json:"p50_ms"`
	P95MS         float64 `json:"p95_ms"`
	TTFTP95MS     float64 `json:"ttft_p95_ms"`
	Tokens        int64   `json:"tokens"`
	CostUSD       float64 `json:"cost_usd"`
	RateLimited   int64   `json:"rate_limited"`
	BudgetRejects int64   `json:"budget_rejects"`
	Inflight      int64   `json:"inflight"`
}

// ---------------------------------------------------------------------------
// 默认值
// ---------------------------------------------------------------------------

// WithDefaults 给缺失字段填默认值（一次性，避免各处重复判断）。
func (c *Config) WithDefaults() {
	if c.Listen == "" {
		c.Listen = ":8081"
	}
	if c.MaxAttempts <= 0 {
		c.MaxAttempts = 2
	}
	if c.Breaker.FailureThreshold <= 0 {
		c.Breaker = BreakerConfig{
			Enabled: true, FailureThreshold: 5, OpenSeconds: 30,
			HalfOpenMax: 2, ErrorRateThreshold: 0.5, WindowSeconds: 60,
		}
	}
	if c.Tracing.MaxRecords <= 0 {
		c.Tracing.MaxRecords = 2000
	}
	if c.Alerts.IntervalMS <= 0 {
		c.Alerts.IntervalMS = 15000
	}
	if c.Alerts.DefaultCooldownSeconds <= 0 {
		c.Alerts.DefaultCooldownSeconds = 300
	}
	if c.Timeouts.ConnectMS <= 0 {
		c.Timeouts.ConnectMS = 5000
	}
	if c.Timeouts.HeaderMS <= 0 {
		c.Timeouts.HeaderMS = 60000
	}
	if c.DefaultRoute == "" && len(c.Routes) > 0 {
		c.DefaultRoute = c.Routes[0].Alias
	}
	for i := range c.Providers {
		if c.Providers[i].HealthPath == "" {
			c.Providers[i].HealthPath = "/models"
		}
	}
	for i := range c.Models {
		if !c.Models[i].Enabled && c.Models[i].ID != "" {
			// 未显式 enabled 的模型视为启用：JSON 里很难区分 false 与缺省，
			// 因此约定「模型条目存在即启用」，要停用请用 enabled:false + 从 routes 里移除。
		}
	}
}