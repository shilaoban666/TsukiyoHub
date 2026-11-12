package alert

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// ---------------------------------------------------------------------------
// 测试脚手架
// ---------------------------------------------------------------------------

// testClock 可手动推进的时钟。
//
// 引擎的时间来源是 Engine.now，测试里替换成它就能在毫秒内验证
// for / cooldown / 静默 这些本来要等几分钟的语义。
type testClock struct {
	mu sync.Mutex
	t  time.Time
}

// newTestClock 以当前真实时间为起点（与 Aggregator.Query 的 real now 对齐）。
func newTestClock() *testClock { return &testClock{t: time.Now()} }

// Now 返回当前测试时间。
func (c *testClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// Advance 推进测试时间。
func (c *testClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// captureNotifier 把收到的通知记在内存里，供断言（同步发送，结果确定）。
type captureNotifier struct {
	name string

	mu  sync.Mutex
	got []Notification
}

// Name 渠道名。
func (c *captureNotifier) Name() string { return c.name }

// Type 渠道类型。
func (c *captureNotifier) Type() string { return "capture" }

// Send 记录一条通知。
func (c *captureNotifier) Send(ctx context.Context, n Notification) error {
	c.mu.Lock()
	c.got = append(c.got, n)
	c.mu.Unlock()
	return nil
}

// count 已收到的通知条数。
func (c *captureNotifier) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.got)
}

// events 已收到的通知快照。
func (c *captureNotifier) events() []Notification {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Notification, len(c.got))
	copy(out, c.got)
	return out
}

// captureSinks 按渠道名索引测试用 capture 实例（注册表是包级全局的）。
var captureSinks sync.Map

func init() {
	Register("capture", func(cfg model.NotifierConfig, _ *log.Logger) (Notifier, error) {
		sink := &captureNotifier{name: cfg.Name}
		captureSinks.Store(cfg.Name, sink)
		return sink, nil
	})
}

// testEnv 一套测试环境。
type testEnv struct {
	engine *Engine
	agg    *metrics.Aggregator
	clock  *testClock
	logs   *bytes.Buffer
	sink   *captureNotifier
}

// newTestEnv 构造引擎：1 个 capture 渠道，时间可推进。
func newTestEnv(t *testing.T, rules []model.AlertRule, opts ...func(*model.Config)) *testEnv {
	t.Helper()
	name := t.Name()
	logs := &bytes.Buffer{}
	logger := log.New(logs, "", 0)
	cfg := &model.Config{
		PublicURL: "http://gateway.test:8081",
		Alerts: model.AlertConfig{
			Enabled:                true,
			IntervalMS:             15000,
			DefaultCooldownSeconds: 300,
			Rules:                  rules,
			Notifiers: []model.NotifierConfig{{
				Type: "capture", Name: name, Enabled: true,
			}},
		},
	}
	for _, opt := range opts {
		opt(cfg)
	}
	agg := metrics.NewAggregator()
	clock := newTestClock()
	e := New(cfg, agg, logger)
	e.now = clock.Now

	sink, ok := captureSinks.Load(name)
	if !ok {
		t.Fatalf("capture 渠道 %q 没有被构造", name)
	}
	return &testEnv{engine: e, agg: agg, clock: clock, logs: logs, sink: sink.(*captureNotifier)}
}

// recordRequests 写入 n 条请求记录，其中 errors 条失败（HTTP 500）。
//
// 记录时间用真实 now（不是测试时钟）：Aggregator.Query 内部以 time.Now() 为窗口基准，
// 这样窗口统计才符合预期；测试时钟只负责引擎的 for/cooldown/静默语义。
func (env *testEnv) recordRequests(n, errors int, route string) {
	at := time.Now()
	for i := 0; i < n; i++ {
		rec := model.RequestRecord{At: at, Route: route, Status: 200, OK: true, LatencyMS: 100}
		if i < errors {
			rec.Status = 500
			rec.OK = false
			rec.Error = "upstream 500"
		}
		env.agg.Record(rec)
	}
}

// rule 构造一条规则（减少测试里的大段字面量）。
func rule(id, metric, op string, threshold float64, window, forDur string) model.AlertRule {
	return model.AlertRule{
		ID:        id,
		Name:      id,
		Metric:    metric,
		Op:        op,
		Threshold: threshold,
		Window:    window,
		For:       forDur,
		Severity:  SeverityWarning,
		Message:   id + " 告警",
		Enabled:   true,
	}
}

// ---------------------------------------------------------------------------
// 状态机
// ---------------------------------------------------------------------------

// TestPendingThenFiring 验证 pending → firing 与 for 持续时长。
func TestPendingThenFiring(t *testing.T) {
	r := rule("error_rate_high", "error_rate", ">", 0.5, "1m", "30s")
	r.Severity = SeverityCritical
	env := newTestEnv(t, []model.AlertRule{r})

	// 10 条请求 8 条失败 → error_rate = 0.8
	env.recordRequests(10, 8, "")

	env.engine.EvaluateOnce()
	events := env.engine.ListEvents(10, "")
	if len(events) != 1 {
		t.Fatalf("期望 1 条 pending 事件，实际 %d 条", len(events))
	}
	if events[0].Status != statusPending {
		t.Fatalf("条件刚满足应为 pending，实际 %q", events[0].Status)
	}
	if events[0].StartsAt.IsZero() {
		t.Fatal("pending 事件应记录 StartsAt")
	}
	if got := env.sink.count(); got != 0 {
		t.Fatalf("for 未到不应发通知，实际发了 %d 条", got)
	}
	firstID := events[0].ID

	// 20s < for(30s)：仍然是 pending，不通知
	env.clock.Advance(20 * time.Second)
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 0 {
		t.Fatalf("for 未满足时不应通知，实际 %d 条", got)
	}
	if got := len(env.engine.ListEvents(10, statusFiring)); got != 0 {
		t.Fatalf("for 未满足时不应有 firing 事件，实际 %d 条", got)
	}

	// 再 +11s（累计 31s > for）：转 firing 并通知一次
	env.clock.Advance(11 * time.Second)
	env.engine.EvaluateOnce()
	notes := env.sink.events()
	if len(notes) != 1 {
		t.Fatalf("期望 1 条 firing 通知，实际 %d 条", len(notes))
	}
	n := notes[0]
	if n.Event.Status != statusFiring {
		t.Fatalf("通知事件状态应为 firing，实际 %q", n.Event.Status)
	}
	if n.Event.ID != firstID {
		t.Fatalf("pending→firing 应是同一个事件，id 从 %s 变成 %s", firstID, n.Event.ID)
	}
	if math.Abs(n.Event.Value-0.8) > 1e-9 {
		t.Fatalf("通知里的指标值应为 0.8，实际 %v", n.Event.Value)
	}
	if n.Event.Threshold != 0.5 || n.Event.Op != ">" {
		t.Fatalf("通知应带上阈值与运算符：%+v", n.Event)
	}
	if n.Event.Severity != SeverityCritical || n.Event.RuleName != "error_rate_high" {
		t.Fatalf("通知应带上级别与规则名：%+v", n.Event)
	}
	if n.Gateway != "http://gateway.test:8081" {
		t.Fatalf("通知应带上网关地址，实际 %q", n.Gateway)
	}
	if n.Rule.Window != "1m" {
		t.Fatalf("通知应带上窗口，实际 %q", n.Rule.Window)
	}
	if !n.Event.Notified {
		t.Fatal("已发送的事件 Notified 应为 true")
	}

	ov := env.engine.Overview()
	if ov.Firing != 1 || ov.Pending != 0 {
		t.Fatalf("概览计数不对：firing=%d pending=%d", ov.Firing, ov.Pending)
	}
	if !ov.Enabled || ov.Rules != 1 || ov.LastCheck.IsZero() {
		t.Fatalf("概览字段不对：%+v", ov)
	}
}

// TestResolved 验证条件恢复 → resolved，并再发一条「已恢复」。
func TestResolved(t *testing.T) {
	env := newTestEnv(t, []model.AlertRule{rule("error_rate_high", "error_rate", ">", 0.5, "1m", "")})

	// 6/6 失败 → 1.0 > 0.5，for 为空 → 当轮就地 firing
	env.recordRequests(6, 6, "")
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 1 {
		t.Fatalf("期望首轮就 firing 并通知 1 次，实际 %d 次", got)
	}

	// 再来 20 条成功 → 6/26 ≈ 0.23 < 0.5
	env.recordRequests(20, 0, "")
	env.engine.EvaluateOnce()

	notes := env.sink.events()
	if len(notes) != 2 {
		t.Fatalf("恢复也应通知一次（共 2 条），实际 %d 条", len(notes))
	}
	rec := notes[1]
	if rec.Event.Status != statusResolved {
		t.Fatalf("恢复通知的状态应为 resolved，实际 %q", rec.Event.Status)
	}
	if rec.Event.EndsAt == nil {
		t.Fatal("resolved 事件应有 EndsAt")
	}
	if rec.Event.Value >= 0.5 {
		t.Fatalf("恢复通知应带上回落后的值，实际 %v", rec.Event.Value)
	}
	if ov := env.engine.Overview(); ov.Firing != 0 {
		t.Fatalf("恢复后 firing 计数应为 0，实际 %d", ov.Firing)
	}
	if got := len(env.engine.ListEvents(10, statusResolved)); got != 1 {
		t.Fatalf("历史里应保留 1 条 resolved 事件，实际 %d 条", got)
	}
}

// TestPendingRecoveredSilently 验证「还没 for 到就恢复」不会发恢复通知。
func TestPendingRecoveredSilently(t *testing.T) {
	env := newTestEnv(t, []model.AlertRule{rule("error_rate_high", "error_rate", ">", 0.5, "1m", "5m")})

	env.recordRequests(4, 4, "")
	env.engine.EvaluateOnce()
	if got := len(env.engine.ListEvents(10, statusPending)); got != 1 {
		t.Fatalf("期望 1 条 pending，实际 %d", got)
	}

	env.recordRequests(20, 0, "")
	env.engine.EvaluateOnce()

	if got := env.sink.count(); got != 0 {
		t.Fatalf("从未 firing 过的告警恢复时不应发通知，实际 %d 条", got)
	}
	events := env.engine.ListEvents(10, statusResolved)
	if len(events) != 1 {
		t.Fatalf("应为 1 条 resolved 历史，实际 %d", len(events))
	}
}

// TestCooldownSuppressesRepeat 验证 cooldown 内的重复通知被抑制，到期后再次提醒。
func TestCooldownSuppressesRepeat(t *testing.T) {
	r := rule("traffic", "requests", ">", 0, "10m", "")
	r.Cooldown = "5m"
	env := newTestEnv(t, []model.AlertRule{r})

	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 1 {
		t.Fatalf("首发应通知 1 次，实际 %d", got)
	}

	// 条件持续满足，但仍在 cooldown 内 → 不重复通知
	env.clock.Advance(time.Minute)
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 1 {
		t.Fatalf("cooldown 内不应重复通知，实际 %d 条", got)
	}

	// 超过 5m cooldown → 再提醒一次
	env.clock.Advance(5 * time.Minute)
	env.engine.EvaluateOnce()
	notes := env.sink.events()
	if len(notes) != 2 {
		t.Fatalf("cooldown 到期后应再通知一次（共 2 条），实际 %d 条", len(notes))
	}
	if notes[1].Event.Status != statusFiring {
		t.Fatalf("重复提醒仍是 firing，实际 %q", notes[1].Event.Status)
	}
	if notes[1].Event.ID != notes[0].Event.ID {
		t.Fatal("重复提醒应复用同一个事件")
	}
}

// TestAckSilencesRule 验证 Ack 后静默期内不通知、状态保留 acked，到期后恢复 firing。
func TestAckSilencesRule(t *testing.T) {
	r := rule("inflight_high", "inflight", ">", 0, "", "")
	r.Cooldown = "1m"
	env := newTestEnv(t, []model.AlertRule{r})

	env.agg.AddInflight(3)
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 1 {
		t.Fatalf("期望 1 条通知，实际 %d", got)
	}
	events := env.engine.ListEvents(10, "")
	if len(events) != 1 {
		t.Fatalf("期望 1 条事件，实际 %d", len(events))
	}
	id := events[0].ID

	if err := env.engine.Ack(id, "web", 10*time.Minute); err != nil {
		t.Fatalf("Ack 失败：%v", err)
	}
	acked := env.engine.ListEvents(10, statusAcked)
	if len(acked) != 1 {
		t.Fatalf("Ack 后应为 acked 状态，实际 %d 条", len(acked))
	}
	if acked[0].AckedBy != "web" || acked[0].AckedAt == nil || acked[0].SilencedUntil == nil {
		t.Fatalf("Ack 字段不完整：%+v", acked[0])
	}

	// 静默期内：条件仍满足，但状态保持 acked、不发通知
	env.clock.Advance(2 * time.Minute)
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 1 {
		t.Fatalf("静默期内不应通知，实际 %d 条", got)
	}
	if got := len(env.engine.ListEvents(10, statusAcked)); got != 1 {
		t.Fatalf("静默期内状态应保留 acked，实际 %d 条", got)
	}

	// 静默到期：恢复 firing 并再通知
	env.clock.Advance(10 * time.Minute)
	env.engine.EvaluateOnce()
	if got := env.sink.count(); got != 2 {
		t.Fatalf("静默到期后应再通知一次（共 2 条），实际 %d 条", got)
	}
	if got := len(env.engine.ListEvents(10, statusFiring)); got != 1 {
		t.Fatalf("静默到期后应回到 firing，实际 %d 条", got)
	}

	// 条件恢复 → resolved
	env.agg.SetInflight(0)
	env.engine.EvaluateOnce()
	if got := len(env.engine.ListEvents(10, statusResolved)); got != 1 {
		t.Fatalf("条件恢复后应有 1 条 resolved，实际 %d", got)
	}

	// 已恢复的事件不能再 Ack
	if err := env.engine.Ack(id, "web", time.Minute); err == nil {
		t.Fatal("已恢复（resolved）的事件 Ack 应返回错误")
	}
	if err := env.engine.Ack("", "web", time.Minute); err == nil {
		t.Fatal("空 id 的 Ack 应返回错误")
	}
	if err := env.engine.Ack("not-exist", "web", time.Minute); err == nil {
		t.Fatal("不存在的事件 Ack 应返回错误")
	}
}

// ---------------------------------------------------------------------------
// 指标读取
// ---------------------------------------------------------------------------

// TestInstantMetric 验证瞬时指标走 Aggregator.Instant（不受窗口影响）。
func TestInstantMetric(t *testing.T) {
	env := newTestEnv(t, []model.AlertRule{rule("breaker_open_high", "breaker_open", ">", 0, "", "")})

	env.agg.SetBreakerOpen(2)
	env.engine.EvaluateOnce()

	notes := env.sink.events()
	if len(notes) != 1 {
		t.Fatalf("瞬时指标应触发 1 条通知，实际 %d", len(notes))
	}
	if notes[0].Event.Value != 2 {
		t.Fatalf("瞬时指标值应为 2，实际 %v", notes[0].Event.Value)
	}

	// 即使时间跑很久（窗口早已过期），瞬时指标仍然有效
	env.clock.Advance(2 * time.Hour)
	env.engine.EvaluateOnce()
	if ov := env.engine.Overview(); ov.Firing != 1 {
		t.Fatalf("瞬时指标应持续 firing，实际 firing=%d", ov.Firing)
	}
}

// TestWindowMetricMapping 直接验证「指标名 → StatsSnapshot 字段」的映射表。
func TestWindowMetricMapping(t *testing.T) {
	snap := model.StatsSnapshot{
		Requests:      10,
		Errors:        3,
		ErrorRate:     0.3,
		RPS:           0.5,
		P50MS:         12,
		P95MS:         34,
		TTFTP95MS:     56,
		Tokens:        789,
		CostUSD:       1.25,
		RateLimited:   2,
		BudgetRejects: 1,
	}
	want := map[string]float64{
		"error_rate":      0.3,
		"p95_latency_ms":  34,
		"p50_latency_ms":  12,
		"ttft_ms":         56,
		"rps":             0.5,
		"requests":        10,
		"error_count":     3,
		"tokens":          789,
		"cost_usd":        1.25,
		"rate_limited":    2,
		"budget_rejected": 1,
	}
	if len(want) != len(windowMetrics) {
		t.Fatalf("窗口指标数量不一致：映射表 %d / 用例 %d", len(windowMetrics), len(want))
	}
	for metric, expected := range want {
		extract, ok := windowMetrics[metric]
		if !ok {
			t.Errorf("缺少窗口指标映射：%s", metric)
			continue
		}
		if got := extract(snap); got != expected {
			t.Errorf("指标 %s 取值错误：got %v want %v", metric, got, expected)
		}
	}
	for _, metric := range []string{"inflight", "breaker_open", "provider_down"} {
		if !instantMetrics[metric] {
			t.Errorf("缺少瞬时指标：%s", metric)
		}
	}
	if got := len(SupportedMetrics()); got != len(want)+len(instantMetrics) {
		t.Errorf("SupportedMetrics 数量不对：%d", got)
	}
	if names := SupportedMetrics(); !contains(names, "error_rate") || !contains(names, "provider_down") {
		t.Errorf("SupportedMetrics 内容不对：%v", names)
	}
}

// TestScopeFilter 验证 scope 只统计指定维度。
func TestScopeFilter(t *testing.T) {
	r := rule("fast_errors", "error_count", ">", 0, "1m", "")
	r.Scope = map[string]string{"route": "fast"}
	env := newTestEnv(t, []model.AlertRule{r})

	// slow 路由的错误不应计入
	env.recordRequests(3, 3, "slow")
	env.recordRequests(1, 0, "fast")
	env.engine.EvaluateOnce()
	if got := len(env.engine.ListEvents(10, "")); got != 0 {
		t.Fatalf("scope=fast 时 slow 的错误不应触发告警，实际 %d 条事件", got)
	}

	env.recordRequests(2, 2, "fast")
	env.engine.EvaluateOnce()
	notes := env.sink.events()
	if len(notes) != 1 {
		t.Fatalf("期望 1 条通知，实际 %d", len(notes))
	}
	if notes[0].Event.Value != 2 {
		t.Fatalf("scope=fast 的错误数应为 2（不是全部 5），实际 %v", notes[0].Event.Value)
	}
	if notes[0].Event.Scope["route"] != "fast" {
		t.Fatalf("事件应带上 scope：%+v", notes[0].Event.Scope)
	}
}

// TestUnknownMetricSkipped 验证未知 metric 只跳过该规则并记一次 warning，引擎不崩。
func TestUnknownMetricSkipped(t *testing.T) {
	bad := rule("typo_rule", "nope_metric", ">", 0, "1m", "")
	good := rule("good_rule", "requests", ">", 0, "1m", "")
	env := newTestEnv(t, []model.AlertRule{bad, good})

	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()
	env.engine.EvaluateOnce() // 第二轮：warning 不应重复刷屏

	events := env.engine.ListEvents(10, "")
	if len(events) != 1 {
		t.Fatalf("未知指标的规则应被跳过，只有 good_rule 产生事件，实际 %d 条", len(events))
	}
	if events[0].RuleID != "good_rule" {
		t.Fatalf("事件应来自 good_rule，实际 %q", events[0].RuleID)
	}
	if got := env.sink.count(); got != 1 {
		t.Fatalf("只应有 good_rule 的通知，实际 %d 条", got)
	}
	logs := env.logs.String()
	if !strings.Contains(logs, "未知指标") || !strings.Contains(logs, "nope_metric") {
		t.Fatalf("应记录未知指标 warning，日志：%s", logs)
	}
	if got := strings.Count(logs, "nope_metric"); got != 1 {
		t.Fatalf("未知指标 warning 只应记一次，实际 %d 次", got)
	}
	// 规则本身仍在列表里（只是被跳过），便于管理员修正
	if got := len(env.engine.ListRules()); got != 2 {
		t.Fatalf("未知指标的规则不应被删除，实际规则数 %d", got)
	}
}

// ---------------------------------------------------------------------------
// 规则热替换
// ---------------------------------------------------------------------------

// TestReplaceRulesKeepsStateForUnchangedRules 验证热替换的保留/重建语义。
func TestReplaceRulesKeepsStateForUnchangedRules(t *testing.T) {
	ruleA := rule("a", "requests", ">", 0, "5m", "")
	env := newTestEnv(t, []model.AlertRule{ruleA})

	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()
	fired := env.engine.ListEvents(10, "")
	if len(fired) != 1 {
		t.Fatalf("期望 1 条 firing 事件，实际 %d", len(fired))
	}
	originalID := fired[0].ID

	// 新增一条规则：未变规则 A 的状态与事件都要保留
	ruleB := rule("b", "inflight", ">", 1000, "", "")
	if err := env.engine.ReplaceRules([]model.AlertRule{ruleA, ruleB}); err != nil {
		t.Fatalf("ReplaceRules 失败：%v", err)
	}
	if got := len(env.engine.ListRules()); got != 2 {
		t.Fatalf("替换后应有 2 条规则，实际 %d", got)
	}
	events := env.engine.ListEvents(10, "")
	if len(events) != 1 || events[0].ID != originalID || events[0].Status != statusFiring {
		t.Fatalf("未变规则的事件应被保留（id=%s status=firing），实际 %+v", originalID, events)
	}

	// 变更 A 的级别：旧事件收尾为 resolved（不通知），运行状态重建
	changed := ruleA
	changed.Severity = SeverityCritical
	if err := env.engine.ReplaceRules([]model.AlertRule{changed}); err != nil {
		t.Fatalf("ReplaceRules 失败：%v", err)
	}
	events = env.engine.ListEvents(10, "")
	if len(events) != 1 || events[0].Status != statusResolved {
		t.Fatalf("规则变更后旧活动事件应就地收尾为 resolved，实际 %+v", events)
	}
	if !events[0].Notified {
		t.Fatal("原事件已经通知过，Notified 应保持 true")
	}

	// 新规则条件下重新产生事件（新 id）
	env.engine.EvaluateOnce()
	newest := env.engine.ListEvents(1, statusFiring)
	if len(newest) != 1 {
		t.Fatalf("重建状态后应重新产生 firing 事件，实际 %d", len(newest))
	}
	if newest[0].ID == originalID {
		t.Fatal("重建状态后的事件 id 不应复用")
	}
	if got := env.engine.ListRules(); len(got) != 1 || got[0].Severity != SeverityCritical {
		t.Fatalf("规则内容应为变更后的版本：%+v", got)
	}

	// 非法规则：整体拒绝，规则保持不变
	err := env.engine.ReplaceRules([]model.AlertRule{
		{ID: "bad", Metric: "requests", Op: "=>", Threshold: 1, Enabled: true},
	})
	if err == nil {
		t.Fatal("非法 op 应导致 ReplaceRules 返回错误")
	}
	if got := len(env.engine.ListRules()); got != 1 {
		t.Fatalf("拒绝后规则不应变化，实际 %d 条", got)
	}
	if got := len(env.engine.ListEvents(10, "")); got != 2 {
		t.Fatalf("拒绝后事件不应变化，实际 %d 条", got)
	}
}

// TestDeleteRuleDropsItsEvents 验证删除规则会清掉它的历史事件。
func TestDeleteRuleDropsItsEvents(t *testing.T) {
	ruleA := rule("a", "requests", ">", 0, "5m", "")
	env := newTestEnv(t, []model.AlertRule{ruleA})
	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()
	if got := len(env.engine.ListEvents(10, "")); got != 1 {
		t.Fatalf("期望 1 条事件，实际 %d", got)
	}

	if err := env.engine.ReplaceRules(nil); err != nil {
		t.Fatalf("清空规则失败：%v", err)
	}
	if got := len(env.engine.ListRules()); got != 0 {
		t.Fatalf("规则应被清空，实际 %d", got)
	}
	if got := len(env.engine.ListEvents(10, "")); got != 0 {
		t.Fatalf("被删除规则的事件应被清理，实际 %d", got)
	}
}

// TestRulesValidation 验证编译期校验（窗口/for/op/severity/scope）。
func TestRulesValidation(t *testing.T) {
	cases := []struct {
		name string
		rule model.AlertRule
		want string
	}{
		{"空 id", model.AlertRule{Metric: "error_rate", Op: ">"}, "id 不能为空"},
		{"缺 metric", model.AlertRule{ID: "x", Op: ">"}, "缺少 metric"},
		{"非法 op", model.AlertRule{ID: "x", Metric: "error_rate", Op: "=>"}, "op 非法"},
		{"非法 window", model.AlertRule{ID: "x", Metric: "error_rate", Op: ">", Window: "2h"}, "window 非法"},
		{"无法解析 window", model.AlertRule{ID: "x", Metric: "error_rate", Op: ">", Window: "soon"}, "window 非法"},
		{"非法 for", model.AlertRule{ID: "x", Metric: "error_rate", Op: ">", For: "abc"}, "for 非法"},
		{"非法 severity", model.AlertRule{ID: "x", Metric: "error_rate", Op: ">", Severity: "panic"}, "severity 非法"},
		{"未知 scope", model.AlertRule{ID: "x", Metric: "error_rate", Op: ">", Scope: map[string]string{"region": "cn"}}, "scope 键"},
	}
	env := newTestEnv(t, nil)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := env.engine.ReplaceRules([]model.AlertRule{tc.rule})
			if err == nil {
				t.Fatalf("期望报错（%s）", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("错误信息应包含 %q，实际：%v", tc.want, err)
			}
		})
	}

	// 合法的窗口写法都应接受，并被规范化
	env2 := newTestEnv(t, nil)
	valid := []model.AlertRule{
		{ID: "s30", Metric: "error_rate", Op: ">", Window: "30s", Enabled: true},
		{ID: "m1", Metric: "error_rate", Op: ">", Window: "1m", Enabled: true},
		{ID: "m5", Metric: "error_rate", Op: ">", Window: "5m", Enabled: true},
		{ID: "m10", Metric: "error_rate", Op: ">", Window: "10m", Enabled: true},
		{ID: "sec", Metric: "error_rate", Op: ">", Window: "900", Enabled: true},
	}
	if err := env2.engine.ReplaceRules(valid); err != nil {
		t.Fatalf("合法规则被拒绝：%v", err)
	}
	rules := env2.engine.ListRules()
	wantWindows := []string{"30s", "1m", "5m", "10m", "15m"}
	for i, want := range wantWindows {
		if rules[i].Window != want {
			t.Errorf("规则 %s 的窗口应规范化为 %q，实际 %q", rules[i].ID, want, rules[i].Window)
		}
	}
}

// ---------------------------------------------------------------------------
// 生命周期 / 其他
// ---------------------------------------------------------------------------

// TestDisabledEngine 验证 alerts.enabled=false 时引擎不评估。
func TestDisabledEngine(t *testing.T) {
	env := newTestEnv(t, []model.AlertRule{rule("a", "requests", ">", 0, "1m", "")}, func(cfg *model.Config) {
		cfg.Alerts.Enabled = false
	})
	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()
	env.engine.Start()
	defer env.engine.Stop()

	if got := len(env.engine.ListEvents(10, "")); got != 0 {
		t.Fatalf("未启用时不应产生事件，实际 %d 条", got)
	}
	if ov := env.engine.Overview(); ov.Enabled {
		t.Fatal("Overview.Enabled 应为 false")
	}
	if !strings.Contains(env.logs.String(), "未启用") {
		t.Fatalf("应记录未启用警告，日志：%s", env.logs.String())
	}
}

// TestStartStopLoop 验证后台循环会按间隔评估，且 Start/Stop 幂等。
func TestStartStopLoop(t *testing.T) {
	env := newTestEnv(t, []model.AlertRule{rule("inflight_high", "inflight", ">", 0, "", "")},
		func(cfg *model.Config) { cfg.Alerts.IntervalMS = 100 })
	env.agg.AddInflight(1)

	env.engine.Start()
	env.engine.Start() // 幂等：不应启动第二个 goroutine

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if !env.engine.Overview().LastCheck.IsZero() {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	env.engine.Stop()
	env.engine.Stop() // 幂等

	if env.engine.Overview().LastCheck.IsZero() {
		t.Fatal("后台循环未执行评估")
	}
	if got := env.sink.count(); got == 0 {
		t.Fatal("后台循环应至少产生一条通知")
	}
}

// TestListEventsFilter 验证事件列表的 limit / status 过滤与「最新在前」顺序。
func TestListEventsFilter(t *testing.T) {
	rules := []model.AlertRule{
		rule("r1", "requests", ">", 0, "5m", ""),
		rule("r2", "requests", ">=", 1, "5m", ""),
	}
	env := newTestEnv(t, rules)
	env.recordRequests(1, 0, "")
	env.engine.EvaluateOnce()

	all := env.engine.ListEvents(10, "")
	if len(all) != 2 {
		t.Fatalf("两条规则都应产生事件，实际 %d", len(all))
	}
	if got := len(env.engine.ListEvents(1, "")); got != 1 {
		t.Fatalf("limit=1 应只返回 1 条，实际 %d", got)
	}
	if got := len(env.engine.ListEvents(10, statusFiring)); got != 2 {
		t.Fatalf("两条都应是 firing，实际 %d", got)
	}
	if got := len(env.engine.ListEvents(10, statusResolved)); got != 0 {
		t.Fatalf("不应有 resolved 事件，实际 %d", got)
	}
}

// TestNotifiersView 验证渠道视图（脱敏：只暴露 name/type/enabled/min_severity）。
func TestNotifiersView(t *testing.T) {
	logs := &bytes.Buffer{}
	cfg := &model.Config{
		Alerts: model.AlertConfig{
			Enabled: true,
			Notifiers: []model.NotifierConfig{
				{Type: "log", Name: "logger", Enabled: true},
				{Type: "webhook", Name: "hook", URL: "https://example.com/hook?token=secret", Enabled: true, MinSeverity: "critical"},
				{Type: "slack", Name: "slack", URL: "https://hooks.slack.com/x", Enabled: false, MinSeverity: "warning"},
				{Type: "no-such-type", Name: "broken", Enabled: true},
			},
		},
	}
	engine := New(cfg, nil, log.New(logs, "", 0))

	views := engine.Notifiers()
	if len(views) != 3 {
		t.Fatalf("未知类型应被跳过，期望 3 个渠道，实际 %d：%+v", len(views), views)
	}
	if views[0].Name != "logger" || views[0].Type != "log" || !views[0].Enabled {
		t.Fatalf("log 渠道视图不对：%+v", views[0])
	}
	if views[0].MinSeverity != SeverityInfo {
		t.Fatalf("未写 min_severity 时应展示为 info，实际 %q", views[0].MinSeverity)
	}
	if views[1].MinSeverity != SeverityCritical || views[1].Name != "hook" {
		t.Fatalf("webhook 渠道视图不对：%+v", views[1])
	}
	if views[2].Enabled {
		t.Fatalf("被禁用的渠道应展示 enabled=false：%+v", views[2])
	}
	if !strings.Contains(logs.String(), "配置无效") {
		t.Fatalf("未知渠道类型应记 warning，日志：%s", logs.String())
	}
	// 脱敏：日志里不应出现渠道密钥
	if strings.Contains(logs.String(), "secret") {
		t.Fatalf("日志不应打印渠道密钥：%s", logs.String())
	}
}

// contains 判断字符串切片是否包含目标（小工具，省一个依赖）。
func contains(values []string, target string) bool {
	for _, v := range values {
		if v == target {
			return true
		}
	}
	return false
}

// TestBuiltinRulesAccepted 验证契约里的 7 条内置规则都能被引擎接受（config.example.json 即用这份）。
func TestBuiltinRulesAccepted(t *testing.T) {
	builtin := []model.AlertRule{
		{ID: "error_rate_high", Name: "错误率过高", Metric: "error_rate", Op: ">", Threshold: 0.1, Window: "1m", For: "1m", Severity: "critical", Enabled: true},
		{ID: "latency_p95_high", Name: "P95 延迟过高", Metric: "p95_latency_ms", Op: ">", Threshold: 5000, Window: "5m", For: "2m", Severity: "warning", Enabled: true},
		{ID: "ttft_slow", Name: "首字延迟过慢", Metric: "ttft_ms", Op: ">", Threshold: 8000, Window: "5m", For: "2m", Severity: "warning", Enabled: true},
		{ID: "breaker_open", Name: "模型熔断", Metric: "breaker_open", Op: ">", Threshold: 0, Window: "1m", For: "30s", Severity: "critical", Enabled: true},
		{ID: "provider_down", Name: "供应商不可用", Metric: "provider_down", Op: ">", Threshold: 0, Window: "1m", For: "1m", Severity: "critical", Enabled: true},
		{ID: "budget_rejected", Name: "预算拒绝", Metric: "budget_rejected", Op: ">", Threshold: 0, Window: "1m", For: "1m", Severity: "critical", Enabled: true},
		{ID: "rate_limited_spike", Name: "限流突增", Metric: "rate_limited", Op: ">", Threshold: 20, Window: "5m", For: "1m", Severity: "warning", Enabled: true},
	}
	env := newTestEnv(t, builtin)

	if got := len(env.engine.ListRules()); got != len(builtin) {
		t.Fatalf("内置规则应全部被接受，期望 %d 条，实际 %d 条", len(builtin), got)
	}
	compiled, errs := compileRules(builtin)
	if len(errs) != 0 {
		t.Fatalf("内置规则不应有编译错误：%v", errs)
	}
	for _, cr := range compiled {
		if cr.unknown {
			t.Errorf("内置规则 %s 的指标 %q 未被引擎支持", cr.rule.ID, cr.metric)
		}
		if cr.window > maxWindow {
			t.Errorf("内置规则 %s 的窗口超过上限：%s", cr.rule.ID, cr.window)
		}
	}
	if got := len(env.engine.Notifiers()); got != 1 {
		t.Fatalf("期望 1 个渠道，实际 %d", got)
	}

	// 一条错误请求即可让 error_rate_high（for=1m）进入 pending
	env.recordRequests(1, 1, "")
	env.engine.EvaluateOnce()
	events := env.engine.ListEvents(10, "")
	if len(events) != 1 || events[0].RuleID != "error_rate_high" || events[0].Status != statusPending {
		t.Fatalf("期望 error_rate_high 进入 pending，实际 %+v", events)
	}
}

// TestEngineNotifiesWebhookEndToEnd 端到端：引擎 → webhook 渠道 → httptest.Server。
//
// 用一个「阈值设 0」的瞬时指标规则模拟验收流程：inflight>0 立刻 firing，
// 回落到 0 时再收到一条 resolved 通知。
func TestEngineNotifiesWebhookEndToEnd(t *testing.T) {
	var (
		mu       sync.Mutex
		payloads [][]byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		payloads = append(payloads, body)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	cfg := &model.Config{
		PublicURL: "http://gateway.test:8081",
		Alerts: model.AlertConfig{
			Enabled:                true,
			IntervalMS:             15000,
			DefaultCooldownSeconds: 300,
			Rules: []model.AlertRule{{
				ID: "inflight_high", Name: "在途突增", Metric: "inflight",
				Op: ">", Threshold: 0, Severity: SeverityCritical,
				Message: "并发请求数异常", Enabled: true,
			}},
			Notifiers: []model.NotifierConfig{{
				Type: "webhook", Name: "hook", URL: srv.URL, Enabled: true,
			}},
		},
	}
	agg := metrics.NewAggregator()
	engine := New(cfg, agg, log.New(io.Discard, "", 0))

	agg.AddInflight(2)
	engine.EvaluateOnce()

	mu.Lock()
	first := append([][]byte(nil), payloads...)
	mu.Unlock()
	if len(first) != 1 {
		t.Fatalf("期望 webhook 收到 1 条通知，实际 %d 条", len(first))
	}
	var firing struct {
		Event   model.AlertEvent `json:"event"`
		Gateway string           `json:"gateway"`
	}
	if err := json.Unmarshal(first[0], &firing); err != nil {
		t.Fatalf("通知不是合法 JSON：%v（%s）", err, first[0])
	}
	if firing.Event.RuleID != "inflight_high" || firing.Event.Status != statusFiring {
		t.Fatalf("首条通知应为 firing：%+v", firing.Event)
	}
	if firing.Event.Value != 2 || firing.Event.Threshold != 0 {
		t.Fatalf("通知里的指标值/阈值不对：%+v", firing.Event)
	}
	if firing.Gateway != "http://gateway.test:8081" {
		t.Fatalf("通知里的网关地址不对：%q", firing.Gateway)
	}

	// 回落到 0 → 收到「已恢复」
	agg.SetInflight(0)
	engine.EvaluateOnce()

	mu.Lock()
	all := append([][]byte(nil), payloads...)
	mu.Unlock()
	if len(all) != 2 {
		t.Fatalf("期望 webhook 共收到 2 条通知，实际 %d 条", len(all))
	}
	var resolved struct {
		Event model.AlertEvent `json:"event"`
	}
	if err := json.Unmarshal(all[1], &resolved); err != nil {
		t.Fatalf("恢复通知不是合法 JSON：%v", err)
	}
	if resolved.Event.Status != statusResolved {
		t.Fatalf("第二条通知应为 resolved，实际 %q", resolved.Event.Status)
	}
	if resolved.Event.EndsAt == nil {
		t.Fatal("resolved 事件应带 ends_at")
	}
	if ov := engine.Overview(); ov.Firing != 0 || ov.Pending != 0 {
		t.Fatalf("恢复后概览不应有活动告警：%+v", ov)
	}
}
