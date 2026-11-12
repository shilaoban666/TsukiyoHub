package proxy

import (
	"crypto/rand"
	"encoding/hex"
	"strconv"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
)

// Prometheus 指标名（必须与契约完全一致，alert-engine 的规则直接引用这些名字）。
const (
	// MetricRequests 请求总数 {route,model,provider,status}
	MetricRequests = "tsukiyo_gateway_requests_total"
	// MetricDuration 请求耗时直方图（秒）{route,model,provider}
	MetricDuration = "tsukiyo_gateway_request_duration_seconds"
	// MetricTTFT 首字延迟直方图（秒）{model}
	MetricTTFT = "tsukiyo_gateway_ttft_seconds"
	// MetricTokens token 数 {model,direction=prompt|completion}
	MetricTokens = "tsukiyo_gateway_tokens_total"
	// MetricCost 费用（美元）{model}
	MetricCost = "tsukiyo_gateway_cost_usd_total"
	// MetricInflight 在途请求数
	MetricInflight = "tsukiyo_gateway_inflight"
)

// latencyAlpha 延迟 EWMA 的平滑系数：越大越灵敏。
const latencyAlpha = 0.3

// newRequestID 生成请求 ID（16 位十六进制）。
func newRequestID() string {
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		// 极端情况下退化为纳秒时间戳，保证 ID 非空。
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(buf[:])
}

// Cost 按模型价格计算一次调用的费用（价格单位：美元 / 1K tokens）。
func Cost(ep *provider.Endpoint, usage model.Usage) float64 {
	if ep == nil {
		return 0
	}
	const perK = 1000.0
	cost := float64(usage.PromptTokens)/perK*ep.Def.InputPrice +
		float64(usage.CompletionTokens)/perK*ep.Def.OutputPrice
	if cost < 0 {
		return 0
	}
	return cost
}

// latencyTracker 记录每个模型最近延迟的指数加权移动平均（least_latency 策略用）。
type latencyTracker struct {
	mu     sync.RWMutex
	values map[string]float64
}

// newLatencyTracker 创建延迟表。
func newLatencyTracker() *latencyTracker {
	return &latencyTracker{values: map[string]float64{}}
}

// Observe 记录一次延迟样本（毫秒）。
func (t *latencyTracker) Observe(modelID string, ms float64) {
	if t == nil || modelID == "" || ms < 0 {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.values == nil {
		t.values = map[string]float64{}
	}
	if old, ok := t.values[modelID]; ok {
		t.values[modelID] = old*(1-latencyAlpha) + ms*latencyAlpha
		return
	}
	t.values[modelID] = ms
}

// Get 返回模型当前 EWMA 延迟（毫秒），无数据返回 0。
func (t *latencyTracker) Get(modelID string) float64 {
	if t == nil {
		return 0
	}
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.values[modelID]
}

// statsView 实现 strategy.Stats：给选路策略提供延迟与熔断状态。
type statsView struct {
	latency  *latencyTracker
	breakers *middleware.Breakers
}

// LatencyEWMA 返回模型最近的平均延迟（毫秒）；无数据返回 0。
func (s *statsView) LatencyEWMA(modelID string) float64 {
	if s == nil || s.latency == nil {
		return 0
	}
	return s.latency.Get(modelID)
}

// BreakerState 返回 "closed" / "open" / "half_open"。
func (s *statsView) BreakerState(modelID string) string {
	if s == nil || s.breakers == nil {
		return middleware.StateClosed
	}
	return s.breakers.State(modelID)
}

// record 把一次请求写进 Prometheus 指标、窗口聚合器、追踪环形缓冲，
// 并回报预算 / TPM 用量（供后续请求判断）。
func (h *Handler) record(rec model.RequestRecord, cfg *model.Config, client model.ClientKey) {
	rec.ClientName = middleware.ClientLabel(client)
	if rec.At.IsZero() {
		rec.At = time.Now()
	}

	// 1) Prometheus
	if reg := h.opts.Metrics; reg != nil {
		labels := map[string]string{
			"route":    rec.Route,
			"model":    rec.Model,
			"provider": rec.Provider,
			"status":   strconv.Itoa(rec.Status),
		}
		reg.Inc(MetricRequests, labels, 1)
		reg.Observe(MetricDuration, map[string]string{
			"route":    rec.Route,
			"model":    rec.Model,
			"provider": rec.Provider,
		}, float64(rec.LatencyMS)/1000)
		if rec.TTFTMS > 0 {
			reg.Observe(MetricTTFT, map[string]string{"model": rec.Model}, float64(rec.TTFTMS)/1000)
		}
		if rec.Usage.PromptTokens > 0 {
			reg.Inc(MetricTokens, map[string]string{"model": rec.Model, "direction": "prompt"}, float64(rec.Usage.PromptTokens))
		}
		if rec.Usage.CompletionTokens > 0 {
			reg.Inc(MetricTokens, map[string]string{"model": rec.Model, "direction": "completion"}, float64(rec.Usage.CompletionTokens))
		}
		if rec.CostUSD > 0 {
			reg.Inc(MetricCost, map[string]string{"model": rec.Model}, rec.CostUSD)
		}
	}

	// 2) 窗口聚合（概览 / 告警都从它派生）
	if agg := h.opts.Aggregator; agg != nil {
		agg.Record(rec)
	}

	// 3) 追踪环形缓冲（只在开启时写入，节省内存）
	if tracer := h.opts.Tracer; tracer != nil {
		enabled := true
		if cfg != nil {
			enabled = cfg.Tracing.Enabled
		}
		if enabled {
			tracer.Add(rec)
		}
	}

	// 4) 延迟统计（只统计成功请求，失败的高延迟值会污染选路）
	if rec.OK && rec.Model != "" && rec.LatencyMS > 0 {
		h.latency.Observe(rec.Model, float64(rec.LatencyMS))
	}

	// 5) 回报预算与 TPM 实际用量
	if guards := h.opts.Guards; guards != nil && client.Key != "" {
		windowDays := 0
		if cfg != nil {
			windowDays = cfg.Budget.WindowDays
		}
		if g := guards.Find(middleware.GuardNameBudget); g != nil {
			if bg, ok := g.(*middleware.BudgetGuard); ok {
				bg.AddCost(client, rec.CostUSD, rec.Usage.TotalTokens, windowDays)
			}
		}
		if g := guards.Find(middleware.GuardNameRateLimit); g != nil {
			if rg, ok := g.(*middleware.RateLimitGuard); ok {
				rg.CommitTokens(client, rec.Usage.TotalTokens)
			}
		}
	}
}