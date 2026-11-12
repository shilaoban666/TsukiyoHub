package middleware

import (
	"context"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// BudgetView 单个客户端的预算使用视图（管理接口展示用）。
type BudgetView struct {
	Client      string    `json:"client"`
	SpentUSD    float64   `json:"spent_usd"`
	LimitUSD    float64   `json:"limit_usd"`
	SpentTokens int64     `json:"spent_tokens"`
	WindowStart time.Time `json:"window_start"`
	Over        bool      `json:"over"`
}

// budgetEntry 一个客户端的累计账目。
type budgetEntry struct {
	Name        string
	SpentUSD    float64
	SpentTokens int64
	LimitUSD    float64
	WindowStart time.Time
}

// BudgetStore 按客户端累计花费（内存实现，进程重启清零）。
//
// 之所以抽成独立类型并暴露一个默认实例：管理接口（admin）只需要读账目，
// 不必反向依赖 proxy 的装配细节；测试也可以构造独立 store 避免互相污染。
type BudgetStore struct {
	mu      sync.Mutex
	entries map[string]*budgetEntry
}

// NewBudgetStore 创建空账本。
func NewBudgetStore() *BudgetStore {
	return &BudgetStore{entries: map[string]*budgetEntry{}}
}

var defaultBudgetStore = NewBudgetStore()

// DefaultBudgetStore 返回进程级默认账本（网关只有一个，够用）。
func DefaultBudgetStore() *BudgetStore { return defaultBudgetStore }

// Add 累加一次请求的花费与 token 数。
func (s *BudgetStore) Add(c model.ClientKey, usd float64, tokens int, windowDays int) {
	if s == nil {
		return
	}
	now := time.Now()
	key := clientKeyOf(c)
	s.mu.Lock()
	defer s.mu.Unlock()
	e := s.entryLocked(key, c.Name, now, windowDays)
	e.LimitUSD = c.MonthlyUSD
	e.SpentUSD += usd
	if tokens > 0 {
		e.SpentTokens += int64(tokens)
	}
}

// Spent 返回客户端当前累计花费（自动处理统计窗口滚动）。
func (s *BudgetStore) Spent(c model.ClientKey, windowDays int) (usd float64, tokens int64) {
	if s == nil {
		return 0, 0
	}
	now := time.Now()
	key := clientKeyOf(c)
	s.mu.Lock()
	defer s.mu.Unlock()
	e := s.entryLocked(key, c.Name, now, windowDays)
	e.LimitUSD = c.MonthlyUSD
	return e.SpentUSD, e.SpentTokens
}

// Entry 返回客户端账目视图。
func (s *BudgetStore) Entry(c model.ClientKey, windowDays int) BudgetView {
	usd, tokens := s.Spent(c, windowDays)
	return BudgetView{
		Client:      ClientLabel(c),
		SpentUSD:    usd,
		LimitUSD:    c.MonthlyUSD,
		SpentTokens: tokens,
		Over:        c.MonthlyUSD > 0 && usd >= c.MonthlyUSD,
	}
}

// Snapshot 返回全部账目（按客户端名排序），管理接口用。
func (s *BudgetStore) Snapshot() []BudgetView {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]BudgetView, 0, len(s.entries))
	// map 顺序不稳定，但对管理界面无所谓；排序留给调用方。
	for _, e := range s.entries {
		out = append(out, BudgetView{
			Client:      e.Name,
			SpentUSD:    e.SpentUSD,
			LimitUSD:    e.LimitUSD,
			SpentTokens: e.SpentTokens,
			WindowStart: e.WindowStart,
			Over:        e.LimitUSD > 0 && e.SpentUSD >= e.LimitUSD,
		})
	}
	return out
}

// Reset 清空某个客户端（按名字或 API Key 匹配）的账目。
func (s *BudgetStore) Reset(name string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, e := range s.entries {
		if key == name || e.Name == name {
			delete(s.entries, key)
		}
	}
}

// ResetAll 清空全部账目（管理接口重置用）。
func (s *BudgetStore) ResetAll() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.entries = map[string]*budgetEntry{}
	s.mu.Unlock()
}

// entryLocked 取（或创建）账目并处理窗口滚动；调用方需持有锁。
func (s *BudgetStore) entryLocked(key, name string, now time.Time, windowDays int) *budgetEntry {
	if s.entries == nil {
		s.entries = map[string]*budgetEntry{}
	}
	e := s.entries[key]
	if e == nil {
		e = &budgetEntry{Name: name, WindowStart: now}
		s.entries[key] = e
	}
	if name != "" {
		e.Name = name
	}
	if windowExpired(e.WindowStart, now, windowDays) {
		e.SpentUSD = 0
		e.SpentTokens = 0
		e.WindowStart = now
	}
	return e
}

// windowExpired 判断统计窗口是否已过期：windowDays > 0 按天数滚动，否则按自然月。
func windowExpired(start, now time.Time, windowDays int) bool {
	if start.IsZero() {
		return false
	}
	if windowDays > 0 {
		return !now.Before(start.AddDate(0, 0, windowDays))
	}
	// 自然月：跨月（含跨年）即重置。
	return now.Year() != start.Year() || now.Month() != start.Month()
}

// BudgetGuard 预算检查：按 client 累计 cost_usd，超额返回 402。
//
// 语义：
//   - 只有 Config.Budget.Enabled 且客户端配了 MonthlyUSD 才生效；
//   - 已花费 >= 预算即拒绝（402 + 「预算已用尽」）；
//   - 被拒也由 proxy 记 RequestRecord（status=402），供告警统计 budget_rejected。
type BudgetGuard struct {
	store   *BudgetStore
	metrics *metrics.Registry
	mu      sync.RWMutex
}

// NewBudgetGuard 创建预算 Guard（使用进程级默认账本）。
func NewBudgetGuard() Guard {
	return &BudgetGuard{store: DefaultBudgetStore()}
}

// NewBudgetGuardWithStore 用独立账本创建预算 Guard（测试用，避免全局状态污染）。
func NewBudgetGuardWithStore(store *BudgetStore) *BudgetGuard {
	if store == nil {
		store = NewBudgetStore()
	}
	return &BudgetGuard{store: store}
}

// Name 返回 Guard 名。
func (g *BudgetGuard) Name() string { return GuardNameBudget }

// SetMetrics 绑定指标注册表（超预算累加 budget_rejected_total）。
func (g *BudgetGuard) SetMetrics(reg *metrics.Registry) {
	g.mu.Lock()
	g.metrics = reg
	g.mu.Unlock()
}

// Store 返回底层账本。
func (g *BudgetGuard) Store() *BudgetStore { return g.store }

// Check 检查是否已超预算。
func (g *BudgetGuard) Check(_ context.Context, in *GuardInput) error {
	if in == nil || in.Config == nil {
		return nil
	}
	if !in.Config.Budget.Enabled {
		return nil
	}
	c := in.Client
	if c.MonthlyUSD <= 0 {
		return nil
	}
	spent, _ := g.store.Spent(c, in.Config.Budget.WindowDays)
	if spent < c.MonthlyUSD {
		return nil
	}
	g.mu.RLock()
	reg := g.metrics
	g.mu.RUnlock()
	if reg != nil {
		reg.Inc("tsukiyo_gateway_budget_rejected_total", map[string]string{"client": ClientLabel(c)}, 1)
	}
	return Reject(402, GuardNameBudget, "预算已用尽")
}

// AddCost 累加一次请求的真实花费（由 proxy 在请求结束后调用）。
//
// windowDays 来自 Config.Budget.WindowDays（0 = 自然月），用于统计窗口滚动。
func (g *BudgetGuard) AddCost(c model.ClientKey, usd float64, tokens, windowDays int) {
	if g == nil || g.store == nil {
		return
	}
	g.store.Add(c, usd, tokens, windowDays)
}