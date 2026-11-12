package middleware

import (
	"sync"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// 熔断器状态（对外字符串，与 strategy.Stats.BreakerState 的约定一致）。
const (
	// StateClosed 正常放行。
	StateClosed = "closed"
	// StateOpen 熔断中，直接拒绝。
	StateOpen = "open"
	// StateHalfOpen 半开：放行少量探活请求，成功则恢复。
	StateHalfOpen = "half_open"
)

// 熔断参数默认值（配置缺省时兜底，避免 0 值把阈值判成「永不熔断」）。
const (
	defaultFailureThreshold = 5
	defaultOpenSeconds      = 30
	defaultHalfOpenMax      = 2
	defaultWindowSeconds    = 60
	// errorRateMinSamples 错误率熔断的最小样本数：样本太少时错误率没有统计意义。
	errorRateMinSamples = 5
)

// outcome 窗口内的一次调用结果。
type outcome struct {
	at time.Time
	ok bool
}

// breaker 单个模型的熔断状态。
type breaker struct {
	state               string
	consecutiveFailures int
	openedAt            time.Time
	halfOpenInFlight    int
	window              []outcome
}

// Breakers 熔断管理器（按模型维度）。
//
// 触发规则（任一满足即 open，持续 OpenSeconds）：
//   - 连续失败 >= FailureThreshold；
//   - 窗口错误率 >= ErrorRateThreshold（窗口内样本数 >= 5）。
//
// 半开：open 到期后放行 HalfOpenMax 个请求，全部成功回 closed，任一失败重新 open。
type Breakers struct {
	cfg model.BreakerConfig

	mu       sync.Mutex
	states   map[string]*breaker
	openCnt  int64
	metrics  *metrics.Registry
	agg      *metrics.Aggregator
	lastSync time.Time
}

// NewBreakers 创建熔断管理器。
func NewBreakers(cfg model.BreakerConfig) *Breakers {
	return &Breakers{cfg: cfg, states: map[string]*breaker{}}
}

// Config 返回当前熔断配置（副本）。
func (b *Breakers) Config() model.BreakerConfig {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cfg
}

// SetMetrics 绑定指标与聚合器（可选）：绑定后状态变化会同步
// tsukiyo_gateway_breaker_state 与 Aggregator.SetBreakerOpen。
func (b *Breakers) SetMetrics(reg *metrics.Registry, agg *metrics.Aggregator) {
	b.mu.Lock()
	b.metrics = reg
	b.agg = agg
	b.mu.Unlock()
	b.SyncMetrics()
}

// Allow 判断是否放行一次对 modelID 的调用。
func (b *Breakers) Allow(modelID string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.cfg.Enabled {
		return true
	}
	br := b.getLocked(modelID)
	now := time.Now()
	switch br.state {
	case StateOpen:
		if now.Sub(br.openedAt) < b.openDuration() {
			return false
		}
		// 到期：转半开，这次请求算第一个探活请求。
		br.state = StateHalfOpen
		br.halfOpenInFlight = 1
		b.syncLocked()
		return true
	case StateHalfOpen:
		if br.halfOpenInFlight >= b.halfOpenMax() {
			return false
		}
		br.halfOpenInFlight++
		return true
	default:
		return true
	}
}

// Success 记录一次成功调用。
func (b *Breakers) Success(modelID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.cfg.Enabled {
		return
	}
	br := b.getLocked(modelID)
	now := time.Now()
	br.window = append(b.trimWindow(br.window, now), outcome{at: now, ok: true})
	switch br.state {
	case StateHalfOpen:
		if br.halfOpenInFlight > 0 {
			br.halfOpenInFlight--
		}
		// 半开探活成功：直接恢复 closed（保守起见不要求全部探活都成功）。
		br.state = StateClosed
		br.consecutiveFailures = 0
		b.syncLocked()
	case StateClosed:
		br.consecutiveFailures = 0
	case StateOpen:
		// open 期间（可能是熔断前发出的请求）成功，不改变状态。
	}
}

// Failure 记录一次失败调用，必要时打开熔断。
func (b *Breakers) Failure(modelID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.cfg.Enabled {
		return
	}
	br := b.getLocked(modelID)
	now := time.Now()
	br.window = append(b.trimWindow(br.window, now), outcome{at: now, ok: false})

	if br.state == StateHalfOpen {
		// 半开探活失败：重新 open，等下一个周期。
		br.state = StateOpen
		br.openedAt = now
		br.halfOpenInFlight = 0
		b.syncLocked()
		return
	}
	if br.state != StateClosed {
		return
	}

	br.consecutiveFailures++
	if b.cfg.FailureThreshold > 0 && br.consecutiveFailures >= b.cfg.FailureThreshold {
		br.state = StateOpen
		br.openedAt = now
		br.halfOpenInFlight = 0
		b.syncLocked()
		return
	}
	// 错误率熔断：窗口内失败占比过高也打开（避免「成功率 50% 但永远凑不满连续失败」）。
	if b.cfg.ErrorRateThreshold > 0 {
		failures, total := countOutcomes(br.window)
		if total >= errorRateMinSamples &&
			float64(failures)/float64(total) >= b.cfg.ErrorRateThreshold {
			br.state = StateOpen
			br.openedAt = now
			br.halfOpenInFlight = 0
			b.syncLocked()
		}
	}
}

// State 返回模型当前熔断状态（closed / open / half_open）。
func (b *Breakers) State(modelID string) string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.cfg.Enabled {
		return StateClosed
	}
	br := b.getLocked(modelID)
	if br.state == StateOpen && time.Since(br.openedAt) >= b.openDuration() {
		// 读了就顺便推进状态，让 Snapshot/告警看到 half_open。
		br.state = StateHalfOpen
		br.halfOpenInFlight = 0
		b.syncLocked()
	}
	return br.state
}

// Snapshot 返回全部模型的熔断状态。
func (b *Breakers) Snapshot() map[string]string {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make(map[string]string, len(b.states))
	now := time.Now()
	for id, br := range b.states {
		if br.state == StateOpen && now.Sub(br.openedAt) >= b.openDuration() {
			br.state = StateHalfOpen
			br.halfOpenInFlight = 0
		}
		out[id] = br.state
	}
	b.syncLocked()
	return out
}

// OpenCount 当前处于 open / half_open 的模型数（告警的 breaker_open 指标）。
func (b *Breakers) OpenCount() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.countOpenLocked()
}

// Sweep 后台定期调用：把到期的 open 推进到 half_open、修剪统计窗口。
//
// 之所以需要它：没有流量时状态不会自然推进，告警与 overview 会一直看到
// 「open」；由 main 起一个 ticker 调它即可（契约要求）。
func (b *Breakers) Sweep() {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := time.Now()
	for _, br := range b.states {
		br.window = b.trimWindow(br.window, now)
		if br.state == StateOpen && now.Sub(br.openedAt) >= b.openDuration() {
			br.state = StateHalfOpen
			br.halfOpenInFlight = 0
		}
	}
	b.syncLocked()
}

// SyncMetrics 把当前状态写入 Prometheus 与聚合器（幂等，装配/概览接口调用）。
func (b *Breakers) SyncMetrics() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.syncLocked()
}

// Reset 清空全部熔断状态（管理/测试用）。
func (b *Breakers) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.states = map[string]*breaker{}
	b.syncLocked()
}

// Preload 预先登记模型（不做任何状态变更），让 breaker_state 指标从启动就有序列。
//
// 没有这一步时，Prometheus 要等到第一次请求才会出现该模型的 breaker_state，
// 外部告警规则在冷启动阶段会看不到「模型是否熔断」。
func (b *Breakers) Preload(modelIDs []string) {
	if !b.cfg.Enabled || len(modelIDs) == 0 {
		return
	}
	b.mu.Lock()
	for _, id := range modelIDs {
		if id == "" {
			continue
		}
		b.getLocked(id)
	}
	b.syncLocked()
	b.mu.Unlock()
}

// ---------------------------------------------------------------------------
// 内部实现
// ---------------------------------------------------------------------------

// getLocked 取（或创建）模型的熔断状态；调用方需持有锁。
func (b *Breakers) getLocked(modelID string) *breaker {
	if b.states == nil {
		b.states = map[string]*breaker{}
	}
	br := b.states[modelID]
	if br == nil {
		br = &breaker{state: StateClosed}
		b.states[modelID] = br
	}
	return br
}

// openDuration 熔断持续时间。
func (b *Breakers) openDuration() time.Duration {
	sec := b.cfg.OpenSeconds
	if sec <= 0 {
		sec = defaultOpenSeconds
	}
	return time.Duration(sec) * time.Second
}

// halfOpenMax 半开放行数。
func (b *Breakers) halfOpenMax() int {
	if b.cfg.HalfOpenMax <= 0 {
		return defaultHalfOpenMax
	}
	return b.cfg.HalfOpenMax
}

// windowDuration 错误率统计窗口。
func (b *Breakers) windowDuration() time.Duration {
	sec := b.cfg.WindowSeconds
	if sec <= 0 {
		sec = defaultWindowSeconds
	}
	return time.Duration(sec) * time.Second
}

// trimWindow 丢掉窗口外的调用结果。
func (b *Breakers) trimWindow(window []outcome, now time.Time) []outcome {
	cutoff := now.Add(-b.windowDuration())
	idx := 0
	for idx < len(window) && window[idx].at.Before(cutoff) {
		idx++
	}
	if idx == 0 {
		return window
	}
	return append(window[:0], window[idx:]...)
}

// countOpenLocked 统计 open/half_open 的模型数；调用方需持有锁。
func (b *Breakers) countOpenLocked() int64 {
	var n int64
	for _, br := range b.states {
		if br.state == StateOpen || br.state == StateHalfOpen {
			n++
		}
	}
	return n
}

// syncLocked 把状态同步到指标与聚合器；调用方需持有锁。
func (b *Breakers) syncLocked() {
	n := b.countOpenLocked()
	b.openCnt = n
	if b.metrics != nil {
		for id, br := range b.states {
			b.metrics.Set("tsukiyo_gateway_breaker_state",
				map[string]string{"model": id}, breakerStateValue(br.state))
		}
	}
	if b.agg != nil {
		b.agg.SetBreakerOpen(n)
	}
	b.lastSync = time.Now()
}

// breakerStateValue 指标值：0=closed,1=half_open,2=open。
func breakerStateValue(state string) float64 {
	switch state {
	case StateOpen:
		return 2
	case StateHalfOpen:
		return 1
	default:
		return 0
	}
}

// countOutcomes 统计窗口内失败数与总数。
func countOutcomes(window []outcome) (failures, total int) {
	for _, o := range window {
		total++
		if !o.ok {
			failures++
		}
	}
	return failures, total
}