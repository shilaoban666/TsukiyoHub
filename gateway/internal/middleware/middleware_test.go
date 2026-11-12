package middleware

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// recordingGuard 记录调用顺序的假 Guard。
type recordingGuard struct {
	name  string
	calls *[]string
	err   error
}

func (g recordingGuard) Name() string { return g.name }

func (g recordingGuard) Check(_ context.Context, _ *GuardInput) error {
	*g.calls = append(*g.calls, g.name)
	return g.err
}

// testConfig 构造带客户端/预算/限流的最小配置。
func testConfig() *model.Config {
	return &model.Config{
		Clients: []model.ClientKey{
			{Key: "sk-client-a", Name: "app-a", Enabled: true, RPM: 0, TPM: 0},
			{Key: "sk-client-b", Name: "app-b", Enabled: false},
		},
		Budget: model.BudgetConfig{Enabled: true, WindowDays: 0},
	}
}

func TestChainRunsGuardsInOrder(t *testing.T) {
	calls := []string{}
	chain := NewChain(
		recordingGuard{name: "apikey", calls: &calls},
		recordingGuard{name: "ratelimit", calls: &calls},
		recordingGuard{name: "budget", calls: &calls},
	)
	if err := chain.Run(context.Background(), &GuardInput{Request: httptest.NewRequest(http.MethodPost, "/", nil)}); err != nil {
		t.Fatalf("Run 失败：%v", err)
	}
	if strings.Join(calls, ",") != "apikey,ratelimit,budget" {
		t.Fatalf("Guard 顺序不对：%v", calls)
	}
}

func TestChainStopsAtFirstError(t *testing.T) {
	calls := []string{}
	boom := Reject(429, "ratelimit", "太频繁")
	chain := NewChain(
		recordingGuard{name: "apikey", calls: &calls},
		recordingGuard{name: "ratelimit", calls: &calls, err: boom},
		recordingGuard{name: "budget", calls: &calls},
	)
	err := chain.Run(context.Background(), &GuardInput{Request: httptest.NewRequest(http.MethodPost, "/", nil)})
	rej, ok := AsReject(err)
	if !ok || rej.Status != 429 || rej.Message != "太频繁" {
		t.Fatalf("拒绝错误不对：%+v", err)
	}
	if strings.Join(calls, ",") != "apikey,ratelimit" {
		t.Fatalf("出错后不应继续执行：%v", calls)
	}
}

func TestNilChainAllows(t *testing.T) {
	var chain *Chain
	if err := chain.Run(context.Background(), &GuardInput{}); err != nil {
		t.Fatalf("nil 链应放行：%v", err)
	}
	if chain.Find("budget") != nil || chain.Guards() != nil {
		t.Fatal("nil 链查询应返回空")
	}
}

func TestAPIKeyGuard(t *testing.T) {
	cfg := testConfig()
	guard := NewAPIKeyGuard()

	// 1) 缺少 Authorization
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	in := &GuardInput{Request: req, Config: cfg}
	err := guard.Check(context.Background(), in)
	rej, ok := AsReject(err)
	if !ok || rej.Status != http.StatusUnauthorized {
		t.Fatalf("缺少 Key 应 401：%+v", err)
	}

	// 2) 正确 Key：写入 Client
	req.Header.Set("Authorization", "Bearer sk-client-a")
	in = &GuardInput{Request: req, Config: cfg}
	if err := guard.Check(context.Background(), in); err != nil {
		t.Fatalf("正确 Key 应通过：%v", err)
	}
	if in.Client.Name != "app-a" || in.Client.Key != "sk-client-a" {
		t.Fatalf("Client 未正确写入：%+v", in.Client)
	}

	// 3) 错误 Key
	badReq := httptest.NewRequest(http.MethodPost, "/", nil)
	badReq.Header.Set("Authorization", "Bearer sk-wrong")
	if err := guard.Check(context.Background(), &GuardInput{Request: badReq, Config: cfg}); err == nil {
		t.Fatal("错误 Key 应被拒绝")
	}

	// 4) 已禁用的客户端（存在其它启用客户端时）
	disabledReq := httptest.NewRequest(http.MethodPost, "/", nil)
	disabledReq.Header.Set("Authorization", "Bearer sk-client-b")
	err = guard.Check(context.Background(), &GuardInput{Request: disabledReq, Config: cfg})
	if rej, ok := AsReject(err); !ok || !strings.Contains(rej.Message, "禁用") {
		t.Fatalf("禁用客户端应提示禁用：%+v", err)
	}

	// 5) X-Api-Key 兼容
	headerReq := httptest.NewRequest(http.MethodPost, "/", nil)
	headerReq.Header.Set("X-Api-Key", "sk-client-a")
	in = &GuardInput{Request: headerReq, Config: cfg}
	if err := guard.Check(context.Background(), in); err != nil {
		t.Fatalf("X-Api-Key 应支持：%v", err)
	}

	// 6) 未配置客户端 = 开发模式放行
	devIn := &GuardInput{Request: req, Config: &model.Config{}}
	if err := guard.Check(context.Background(), devIn); err != nil {
		t.Fatalf("空 Clients 应放行：%v", err)
	}
	if devIn.Client.Name != "anonymous" {
		t.Fatalf("开发模式客户端名不对：%+v", devIn.Client)
	}
}

func TestRateLimitRPM(t *testing.T) {
	cfg := testConfig()
	cfg.Clients[0].RPM = 2
	reg := metrics.NewRegistry()
	guard := NewRateLimitGuard()
	chain := NewChain(guard)
	chain.BindMetrics(reg)

	client := cfg.Clients[0]
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest(http.MethodPost, "/", nil)
		if err := chain.Run(context.Background(), &GuardInput{
			Request: req, Config: cfg, Client: client,
		}); err != nil {
			t.Fatalf("第 %d 次应放行：%v", i+1, err)
		}
	}
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	err := chain.Run(context.Background(), &GuardInput{Request: req, Config: cfg, Client: client})
	rej, ok := AsReject(err)
	if !ok || rej.Status != http.StatusTooManyRequests {
		t.Fatalf("超过 RPM 应 429：%+v", err)
	}

	var buf strings.Builder
	reg.WritePrometheus(&buf)
	if !strings.Contains(buf.String(), `tsukiyo_gateway_ratelimit_rejected_total{client="app-a"} 1`) {
		t.Fatalf("限流指标未记录：\n%s", buf.String())
	}
}

func TestRateLimitTPMTokenBucket(t *testing.T) {
	cfg := testConfig()
	cfg.Clients[0].TPM = 10
	guard := NewRateLimitGuard().(*RateLimitGuard)
	client := cfg.Clients[0]

	req := httptest.NewRequest(http.MethodPost, "/", nil)
	if err := guard.Check(context.Background(), &GuardInput{Request: req, Config: cfg, Client: client}); err != nil {
		t.Fatalf("初始应有 10 token：%v", err)
	}
	if remaining := guard.Remaining(client); remaining <= 0 {
		t.Fatalf("初始剩余 token 应 > 0：%v", remaining)
	}
	// 回报一次 20 token 的超大用量：桶容量 10 → 透支到 -10
	guard.CommitTokens(client, 20)
	if remaining := guard.Remaining(client); remaining > 0 {
		t.Fatalf("透支后剩余 token 应 <= 0：%v", remaining)
	}
	req2 := httptest.NewRequest(http.MethodPost, "/", nil)
	err := guard.Check(context.Background(), &GuardInput{Request: req2, Config: cfg, Client: client})
	if rej, ok := AsReject(err); !ok || rej.Status != http.StatusTooManyRequests {
		t.Fatalf("TPM 超额应 429：%+v", err)
	}
}

func TestRateLimitDisabledWhenZero(t *testing.T) {
	cfg := testConfig()
	guard := NewRateLimitGuard()
	client := cfg.Clients[0] // RPM/TPM 都是 0
	for i := 0; i < 100; i++ {
		req := httptest.NewRequest(http.MethodPost, "/", nil)
		if err := guard.Check(context.Background(), &GuardInput{Request: req, Config: cfg, Client: client}); err != nil {
			t.Fatalf("未配置限额应放行：%v", err)
		}
	}
}

func TestBudgetGuard(t *testing.T) {
	store := NewBudgetStore()
	guard := NewBudgetGuardWithStore(store)
	cfg := testConfig()
	cfg.Clients[0].MonthlyUSD = 0.01
	client := cfg.Clients[0]

	reg := metrics.NewRegistry()
	guard.SetMetrics(reg)

	req := httptest.NewRequest(http.MethodPost, "/", nil)
	if err := guard.Check(context.Background(), &GuardInput{Request: req, Config: cfg, Client: client}); err != nil {
		t.Fatalf("未超预算应放行：%v", err)
	}
	guard.AddCost(client, 0.02, 500, 0)
	req2 := httptest.NewRequest(http.MethodPost, "/", nil)
	err := guard.Check(context.Background(), &GuardInput{Request: req2, Config: cfg, Client: client})
	rej, ok := AsReject(err)
	if !ok || rej.Status != http.StatusPaymentRequired {
		t.Fatalf("超预算应 402：%+v", err)
	}
	if rej.Message != "预算已用尽" {
		t.Fatalf("402 消息必须是「预算已用尽」：%q", rej.Message)
	}

	var buf strings.Builder
	reg.WritePrometheus(&buf)
	if !strings.Contains(buf.String(), `tsukiyo_gateway_budget_rejected_total{client="app-a"} 1`) {
		t.Fatalf("预算拒绝指标未记录：\n%s", buf.String())
	}

	// 账本视图
	views := store.Snapshot()
	if len(views) != 1 || !views[0].Over || views[0].SpentTokens != 500 {
		t.Fatalf("账本视图不对：%+v", views)
	}
	store.ResetAll()
	if spent, _ := store.Spent(client, 0); spent != 0 {
		t.Fatalf("重置后花费应为 0：%v", spent)
	}
}

func TestBudgetGuardDisabled(t *testing.T) {
	store := NewBudgetStore()
	guard := NewBudgetGuardWithStore(store)
	cfg := testConfig()
	cfg.Budget.Enabled = false
	cfg.Clients[0].MonthlyUSD = 0.01
	client := cfg.Clients[0]
	guard.AddCost(client, 1, 1000, 0)
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	if err := guard.Check(context.Background(), &GuardInput{Request: req, Config: cfg, Client: client}); err != nil {
		t.Fatalf("预算未启用应放行：%v", err)
	}
}

func TestBudgetStoreWindowRollover(t *testing.T) {
	store := NewBudgetStore()
	client := model.ClientKey{Key: "k", Name: "n", MonthlyUSD: 1}
	store.Add(client, 0.5, 10, 0)
	// 手动把窗口起点拨到上个月：应自动清零
	store.mu.Lock()
	for _, entry := range store.entries {
		entry.WindowStart = time.Now().AddDate(0, -1, 0)
	}
	store.mu.Unlock()
	if spent, tokens := store.Spent(client, 0); spent != 0 || tokens != 0 {
		t.Fatalf("跨月后应清零：spent=%v tokens=%v", spent, tokens)
	}
}

func TestBreakerOpensAfterConsecutiveFailures(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 3, OpenSeconds: 30,
		HalfOpenMax: 2, WindowSeconds: 60,
	})
	for i := 0; i < 3; i++ {
		if i < 2 && !breakers.Allow("m1") {
			t.Fatalf("第 %d 次失败前应放行", i)
		}
		breakers.Failure("m1")
	}
	if breakers.State("m1") != StateOpen {
		t.Fatalf("连续失败 3 次应 open：%s", breakers.State("m1"))
	}
	if breakers.Allow("m1") {
		t.Fatal("open 状态应拒绝")
	}
	if breakers.OpenCount() != 1 {
		t.Fatalf("OpenCount 应为 1：%d", breakers.OpenCount())
	}
	if snapshot := breakers.Snapshot(); snapshot["m1"] != StateOpen {
		t.Fatalf("Snapshot 不对：%+v", snapshot)
	}
}

func TestBreakerHalfOpenRecoversAndReopens(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 1, OpenSeconds: 30,
		HalfOpenMax: 2, WindowSeconds: 60,
	})
	breakers.Failure("m1") // → open
	if breakers.State("m1") != StateOpen {
		t.Fatalf("应 open：%s", breakers.State("m1"))
	}

	// 把打开时间拨回 31 秒前，模拟熔断到期
	breakers.mu.Lock()
	breakers.states["m1"].openedAt = time.Now().Add(-31 * time.Second)
	breakers.mu.Unlock()

	if !breakers.Allow("m1") {
		t.Fatal("半开应放行第一个")
	}
	if !breakers.Allow("m1") {
		t.Fatal("半开应放行第二个（HalfOpenMax=2）")
	}
	if breakers.Allow("m1") {
		t.Fatal("超过 HalfOpenMax 应拒绝")
	}
	if state := breakers.State("m1"); state != StateHalfOpen {
		t.Fatalf("应为 half_open：%s", state)
	}
	breakers.Success("m1")
	if state := breakers.State("m1"); state != StateClosed {
		t.Fatalf("半开成功后应 closed：%s", state)
	}

	// 再次熔断 + 到期 + 半开失败 → 重新 open
	breakers.Failure("m1")
	breakers.mu.Lock()
	breakers.states["m1"].openedAt = time.Now().Add(-31 * time.Second)
	breakers.mu.Unlock()
	if !breakers.Allow("m1") {
		t.Fatal("到期后应放行探活")
	}
	breakers.Failure("m1")
	if state := breakers.State("m1"); state != StateOpen {
		t.Fatalf("半开探活失败应重新 open：%s", state)
	}
}

func TestBreakerErrorRateOpens(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 100, OpenSeconds: 30,
		HalfOpenMax: 1, ErrorRateThreshold: 0.5, WindowSeconds: 60,
	})
	for i := 0; i < 5; i++ {
		breakers.Allow("m2")
		breakers.Failure("m2")
	}
	if state := breakers.State("m2"); state != StateOpen {
		t.Fatalf("错误率 100%% 应 open：%s", state)
	}
}

func TestBreakerSuccessResetsConsecutive(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 3, OpenSeconds: 30, HalfOpenMax: 2, WindowSeconds: 60,
	})
	breakers.Failure("m3")
	breakers.Failure("m3")
	breakers.Success("m3") // 连续失败计数清零
	breakers.Failure("m3")
	breakers.Failure("m3")
	if state := breakers.State("m3"); state != StateClosed {
		t.Fatalf("中间成功应重置连续失败：%s", state)
	}
}

func TestBreakerSweepPromotesExpired(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 1, OpenSeconds: 30, HalfOpenMax: 1, WindowSeconds: 60,
	})
	breakers.Failure("m4")
	breakers.mu.Lock()
	breakers.states["m4"].openedAt = time.Now().Add(-31 * time.Second)
	breakers.mu.Unlock()

	breakers.Sweep()
	breakers.mu.Lock()
	state := breakers.states["m4"].state
	breakers.mu.Unlock()
	if state != StateHalfOpen {
		t.Fatalf("Sweep 应把到期 open 推进为 half_open：%s", state)
	}
}

func TestBreakerDisabled(t *testing.T) {
	breakers := NewBreakers(model.BreakerConfig{Enabled: false, FailureThreshold: 1})
	for i := 0; i < 10; i++ {
		breakers.Failure("m5")
	}
	if !breakers.Allow("m5") {
		t.Fatal("熔断未启用时应始终放行")
	}
	if state := breakers.State("m5"); state != StateClosed {
		t.Fatalf("未启用时状态应 closed：%s", state)
	}
}

func TestBreakerMetricsAndAggregator(t *testing.T) {
	reg := metrics.NewRegistry()
	agg := metrics.NewAggregator()
	breakers := NewBreakers(model.BreakerConfig{
		Enabled: true, FailureThreshold: 1, OpenSeconds: 30, HalfOpenMax: 1, WindowSeconds: 60,
	})
	breakers.SetMetrics(reg, agg)
	breakers.Preload([]string{"m6", "m7"}) // 让指标序列提前存在
	breakers.Failure("m6")

	var buf strings.Builder
	reg.WritePrometheus(&buf)
	text := buf.String()
	if !strings.Contains(text, `tsukiyo_gateway_breaker_state{model="m6"} 2`) {
		t.Fatalf("breaker_state 指标不对：\n%s", text)
	}
	if !strings.Contains(text, `tsukiyo_gateway_breaker_state{model="m7"} 0`) {
		t.Fatalf("Preload 的模型也应有序列：\n%s", text)
	}
	if got := agg.Instant("breaker_open"); got != 1 {
		t.Fatalf("聚合器 breaker_open 应为 1：%v", got)
	}
	if breakers.Config().FailureThreshold != 1 {
		t.Fatalf("Config 返回不对：%+v", breakers.Config())
	}
}

func TestRejectErrorAsReject(t *testing.T) {
	err := Rejectf(403, "apikey", "路由 %s 不允许", "fast")
	rej, ok := AsReject(err)
	if !ok || rej.Status != 403 || !strings.Contains(rej.Message, "fast") {
		t.Fatalf("AsReject 不对：%+v", rej)
	}
	if _, ok := AsReject(errors.New("other")); ok {
		t.Fatal("普通错误不应识别为 RejectError")
	}
	if rej.Error() == "" {
		t.Fatal("Error() 不应为空")
	}
	var nilRej *RejectError
	if nilRej.Error() != "" {
		t.Fatal("nil RejectError 的 Error() 应返回空串")
	}
}