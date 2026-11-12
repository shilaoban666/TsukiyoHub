package middleware

import (
	"context"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// rateWindowSeconds RPM/TPM 的统计窗口固定为 60 秒（「每分钟」上限）。
const rateWindowSeconds = 60

// staleWindow 超过该时长没有任何请求的客户端窗口会被清理，避免 map 无限增长。
const staleWindow = 10 * time.Minute

// rateWindow 单个客户端的限流状态。
type rateWindow struct {
	// reqs RPM 滑动窗口：最近 60 秒内的请求时间戳
	reqs []time.Time
	// tokens TPM 令牌桶：容量 = TPM，按 TPM/60 每秒回填
	tokens     float64
	lastRefill time.Time
	lastSeen   time.Time
}

// RateLimitGuard RPM/TPM 限流（滑动窗口 + 令牌桶，标准库实现）。
//
//   - RPM 用 60 秒滑动窗口：窗口内请求数达到上限即 429；
//   - TPM 用令牌桶：容量 = TPM，按 TPM/60 token/秒回填；请求通过后由 proxy
//     调用 CommitTokens 扣掉真实用量（拿不到真实用量时按估算值扣）。
//
// 两个上限都是 0 表示不限。
type RateLimitGuard struct {
	mu      sync.Mutex
	windows map[string]*rateWindow
	checks  int
	metrics *metrics.Registry
}

// NewRateLimitGuard 创建限流 Guard。
func NewRateLimitGuard() Guard {
	return &RateLimitGuard{windows: map[string]*rateWindow{}}
}

// Name 返回 Guard 名。
func (g *RateLimitGuard) Name() string { return GuardNameRateLimit }

// SetMetrics 绑定指标注册表（被拒时累加 ratelimit_rejected_total）。
func (g *RateLimitGuard) SetMetrics(reg *metrics.Registry) {
	g.mu.Lock()
	g.metrics = reg
	g.mu.Unlock()
}

// Check 检查 RPM/TPM 是否超额。
func (g *RateLimitGuard) Check(_ context.Context, in *GuardInput) error {
	if in == nil || in.Config == nil {
		return nil
	}
	c := in.Client
	if c.RPM <= 0 && c.TPM <= 0 {
		return nil
	}
	now := time.Now()
	key := clientKeyOf(c)

	g.mu.Lock()
	if g.windows == nil {
		g.windows = map[string]*rateWindow{}
	}
	w := g.windows[key]
	if w == nil {
		w = &rateWindow{tokens: float64(c.TPM), lastRefill: now}
		g.windows[key] = w
	}
	w.lastSeen = now
	w.pruneRequests(now)

	// RPM：滑动窗口
	if c.RPM > 0 && len(w.reqs) >= c.RPM {
		g.mu.Unlock()
		g.reject(c)
		return Rejectf(429, GuardNameRateLimit, "请求过于频繁：RPM 上限 %d", c.RPM)
	}
	// TPM：令牌桶
	if c.TPM > 0 {
		w.refill(now, float64(c.TPM)/rateWindowSeconds, float64(c.TPM))
		if w.tokens <= 0 {
			g.mu.Unlock()
			g.reject(c)
			return Rejectf(429, GuardNameRateLimit, "token 速率超限：TPM 上限 %d", c.TPM)
		}
	}
	w.reqs = append(w.reqs, now)

	// 顺便清理长期不活跃的客户端窗口（每 128 次检查一次，成本可忽略）。
	g.checks++
	if g.checks%128 == 0 {
		g.pruneStale(now)
	}
	g.mu.Unlock()
	return nil
}

// CommitTokens 回报一次请求的真实 token 用量（由 proxy 在请求结束后调用）。
func (g *RateLimitGuard) CommitTokens(c model.ClientKey, tokens int) {
	if tokens <= 0 || c.TPM <= 0 {
		return
	}
	now := time.Now()
	key := clientKeyOf(c)
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.windows == nil {
		g.windows = map[string]*rateWindow{}
	}
	w := g.windows[key]
	if w == nil {
		w = &rateWindow{tokens: float64(c.TPM), lastRefill: now}
		g.windows[key] = w
	}
	w.lastSeen = now
	w.refill(now, float64(c.TPM)/rateWindowSeconds, float64(c.TPM))
	w.tokens -= float64(tokens)
	// 允许透支，但最多透支一个完整桶容量：一次超大请求不会让客户端被锁死几小时。
	if floor := -float64(c.TPM); w.tokens < floor {
		w.tokens = floor
	}
}

// Remaining 返回客户端当前剩余 TPM token（调试/测试用）。
func (g *RateLimitGuard) Remaining(c model.ClientKey) float64 {
	if c.TPM <= 0 {
		return 0
	}
	now := time.Now()
	key := clientKeyOf(c)
	g.mu.Lock()
	defer g.mu.Unlock()
	w := g.windows[key]
	if w == nil {
		return float64(c.TPM)
	}
	w.refill(now, float64(c.TPM)/rateWindowSeconds, float64(c.TPM))
	return w.tokens
}

// reject 记录被限流并返回统一错误（调用方构造错误）。
func (g *RateLimitGuard) reject(c model.ClientKey) {
	g.mu.Lock()
	reg := g.metrics
	g.mu.Unlock()
	if reg == nil {
		return
	}
	reg.Inc("tsukiyo_gateway_ratelimit_rejected_total", map[string]string{"client": ClientLabel(c)}, 1)
}

// pruneRequests 丢掉 60 秒之前的请求时间戳。
func (w *rateWindow) pruneRequests(now time.Time) {
	cutoff := now.Add(-rateWindowSeconds * time.Second)
	idx := 0
	for idx < len(w.reqs) && w.reqs[idx].Before(cutoff) {
		idx++
	}
	if idx > 0 {
		w.reqs = append(w.reqs[:0], w.reqs[idx:]...)
	}
}

// refill 按经过时间把令牌桶补满（不超过容量）。
func (w *rateWindow) refill(now time.Time, ratePerSec, capacity float64) {
	if ratePerSec <= 0 {
		return
	}
	if w.lastRefill.IsZero() {
		w.lastRefill = now
		return
	}
	elapsed := now.Sub(w.lastRefill).Seconds()
	if elapsed <= 0 {
		return
	}
	w.tokens += elapsed * ratePerSec
	if w.tokens > capacity {
		w.tokens = capacity
	}
	w.lastRefill = now
}

// pruneStale 清理长时间无请求的窗口（调用方需持有锁）。
func (g *RateLimitGuard) pruneStale(now time.Time) {
	for key, w := range g.windows {
		if now.Sub(w.lastSeen) > staleWindow {
			delete(g.windows, key)
		}
	}
}

// clientKeyOf 限流/预算的内部键：优先用 API Key（唯一），退化到名字。
func clientKeyOf(c model.ClientKey) string {
	if c.Key != "" {
		return c.Key
	}
	if c.Name != "" {
		return c.Name
	}
	return "anonymous"
}

// ClientLabel 指标里的 client 标签值。
func ClientLabel(c model.ClientKey) string {
	if c.Name != "" {
		return c.Name
	}
	return "anonymous"
}