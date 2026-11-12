package proxy

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
	"tsukiyohub/gateway/internal/tracing"
)

// chatResponseJSON 假上游的非流式响应（带 usage）。
const chatResponseJSON = `{"id":"chatcmpl-1","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"你好，世界"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}`

// captured 假上游收到的请求。
type captured struct {
	path string
	body map[string]json.RawMessage
	auth string
}

// upstreamRecorder 记录假上游被调用的次数与请求内容。
type upstreamRecorder struct {
	mu       sync.Mutex
	requests []captured
}

func (u *upstreamRecorder) record(r *http.Request) captured {
	body, _ := io.ReadAll(r.Body)
	entry := captured{path: r.URL.Path, auth: r.Header.Get("Authorization")}
	_ = json.Unmarshal(body, &entry.body)
	u.mu.Lock()
	u.requests = append(u.requests, entry)
	u.mu.Unlock()
	return entry
}

func (u *upstreamRecorder) snapshot() []captured {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]captured(nil), u.requests...)
}

func (u *upstreamRecorder) count() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return len(u.requests)
}

// newUpstream 起一个假上游（t.Cleanup 自动关闭）。
func newUpstream(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

// testConfig 构造双供应商双模型配置（priority 路由 fast：m1 → m2）。
func testConfig(primaryURL, fallbackURL string) *model.Config {
	cfg := &model.Config{
		Providers: []model.Provider{
			{Name: "p1", Type: "openai", BaseURL: primaryURL + "/v1", APIKey: "sk-1",
				HealthPath: "/models", Enabled: true, StreamUsage: true},
			{Name: "p2", Type: "openai", BaseURL: fallbackURL + "/v1", APIKey: "sk-2",
				HealthPath: "/models", Enabled: true},
		},
		Models: []model.ModelDef{
			{ID: "m1", Provider: "p1", Upstream: "up-m1", InputPrice: 1, OutputPrice: 2, SupportsTools: true},
			{ID: "m2", Provider: "p2", Upstream: "up-m2", InputPrice: 0.5, OutputPrice: 1},
		},
		Routes: []model.Route{{
			Alias: "fast", Strategy: "priority", Enabled: true,
			Targets: []model.Target{{Model: "m1", Priority: 0}, {Model: "m2", Priority: 1}},
		}},
		MaxAttempts: 2,
		Breaker: model.BreakerConfig{
			Enabled: true, FailureThreshold: 5, OpenSeconds: 30,
			HalfOpenMax: 2, ErrorRateThreshold: 0, WindowSeconds: 60,
		},
		Tracing:  model.TracingConfig{Enabled: true, MaxRecords: 200},
		Timeouts: model.TimeoutConfig{ConnectMS: 2000, HeaderMS: 5000},
	}
	cfg.WithDefaults()
	return cfg
}

// staticConfig 测试用 ConfigSource。
type staticConfig struct {
	mu  sync.Mutex
	cfg *model.Config
}

func (s *staticConfig) Current() *model.Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg
}

func (s *staticConfig) set(cfg *model.Config) {
	s.mu.Lock()
	s.cfg = cfg
	s.mu.Unlock()
}

// newTestHandler 装配被测试的 proxy.Handler。
func newTestHandler(t *testing.T, cfg *model.Config, guards *middleware.Chain) *Handler {
	t.Helper()
	registry, err := provider.NewRegistry(cfg)
	if err != nil {
		t.Fatalf("构造 provider 注册表失败：%v", err)
	}
	source := &staticConfig{cfg: cfg}
	handler := New(Options{
		Config:     source,
		Providers:  registry,
		Breakers:   middleware.NewBreakers(cfg.Breaker),
		Guards:     guards,
		Metrics:    metrics.NewRegistry(),
		Aggregator: metrics.NewAggregator(),
		Tracer:     tracing.NewRing(cfg.Tracing.MaxRecords),
		Version:    "test-1.0",
	})
	return handler
}

// doRequest 发一个请求并返回响应记录器。
func doRequest(t *testing.T, handler http.Handler, method, path, body string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	return recorder
}

// lastRecord 取最近一条追踪记录。
func lastRecord(t *testing.T, handler *Handler) model.RequestRecord {
	t.Helper()
	records := handler.opts.Tracer.List(tracing.Filter{Limit: 1})
	if len(records) == 0 {
		t.Fatal("没有写入请求记录")
	}
	return records[0]
}

// metricsText 导出 Prometheus 文本。
func metricsText(handler *Handler) string {
	var buf strings.Builder
	handler.opts.Metrics.WritePrometheus(&buf)
	return buf.String()
}

// alwaysJSON 假上游：任意路径都返回固定 JSON。
func alwaysJSON(t *testing.T, rec *upstreamRecorder, body string) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		rec.record(r)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}
}

func TestChatCompletionsNonStream(t *testing.T) {
	recorder := &upstreamRecorder{}
	upstream := newUpstream(t, alwaysJSON(t, recorder, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)

	body := `{"model":"fast","messages":[{"role":"user","content":"hi"}],"temperature":0.2,"top_p":0.9,"custom_field":{"a":1}}`
	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions", body, nil)

	if response.Code != http.StatusOK {
		t.Fatalf("状态码应为 200：%d body=%s", response.Code, response.Body.String())
	}
	// 上游响应原样回传
	if response.Body.String() != chatResponseJSON {
		t.Fatalf("非流式响应应原样透传，实际：%s", response.Body.String())
	}

	// 上游收到：model 被改写、其余字段透传
	requests := recorder.snapshot()
	if len(requests) != 1 {
		t.Fatalf("上游应被调用 1 次：%d", len(requests))
	}
	got := requests[0]
	if got.path != "/v1/chat/completions" {
		t.Fatalf("上游路径不对：%s", got.path)
	}
	if got.auth != "Bearer sk-1" {
		t.Fatalf("上游鉴权头不对：%s", got.auth)
	}
	if string(got.body["model"]) != `"up-m1"` {
		t.Fatalf("model 应被改写为上游模型名：%s", got.body["model"])
	}
	if string(got.body["top_p"]) != "0.9" || string(got.body["temperature"]) != "0.2" {
		t.Fatalf("其余字段应原样透传：%+v", got.body)
	}
	if _, ok := got.body["custom_field"]; !ok {
		t.Fatalf("未知字段也应透传：%+v", got.body)
	}
	if _, ok := got.body["stream_options"]; ok {
		t.Fatal("非流式请求不应注入 stream_options")
	}

	// 记录：usage / 成本 / 指标
	record := lastRecord(t, handler)
	if record.Status != 200 || !record.OK {
		t.Fatalf("记录状态不对：%+v", record)
	}
	if record.Model != "m1" || record.Provider != "p1" || record.Route != "fast" {
		t.Fatalf("记录维度不对：%+v", record)
	}
	if record.Usage.PromptTokens != 10 || record.Usage.CompletionTokens != 4 || record.Usage.TotalTokens != 14 {
		t.Fatalf("usage 解析不对：%+v", record.Usage)
	}
	if record.Usage.Estimated {
		t.Fatal("上游有 usage 时不应标记估算")
	}
	// 10/1000*1 + 4/1000*2 = 0.018
	if record.CostUSD < 0.0179 || record.CostUSD > 0.0181 {
		t.Fatalf("成本计算不对：%v", record.CostUSD)
	}
	if record.Attempts != 1 || record.RequestBytes == 0 || record.ResponseBytes == 0 {
		t.Fatalf("记录大小/尝试数不对：%+v", record)
	}

	snapshot := handler.opts.Aggregator.Query(300, nil)
	if snapshot.Requests != 1 || snapshot.Tokens != 14 {
		t.Fatalf("聚合器统计不对：%+v", snapshot)
	}

	text := metricsText(handler)
	wantLabels := `model="m1",provider="p1",route="fast",status="200"`
	if !strings.Contains(text, wantLabels) {
		t.Fatalf("requests_total 标签缺失：\n%s", text)
	}
	if !strings.Contains(text, `tsukiyo_gateway_tokens_total{direction="prompt",model="m1"} 10`) {
		t.Fatalf("prompt token 指标缺失：\n%s", text)
	}
	if !strings.Contains(text, `tsukiyo_gateway_tokens_total{direction="completion",model="m1"} 4`) {
		t.Fatalf("completion token 指标缺失：\n%s", text)
	}
	if !strings.Contains(text, "tsukiyo_gateway_cost_usd_total{model=\"m1\"} 0.018") {
		t.Fatalf("cost 指标缺失：\n%s", text)
	}
	if !strings.Contains(text, "tsukiyo_gateway_request_duration_seconds_sum{") {
		t.Fatalf("耗时直方图缺失：\n%s", text)
	}
	if !strings.Contains(text, "tsukiyo_gateway_inflight 0") {
		t.Fatalf("inflight 指标缺失：\n%s", text)
	}
	if !strings.Contains(text, `tsukiyo_gateway_breaker_state{model="m1"} 0`) {
		t.Fatalf("breaker_state 指标缺失：\n%s", text)
	}
}

func TestChatCompletionsStream(t *testing.T) {
	var sawIncludeUsage bool
	upstream := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			_, _ = w.Write([]byte(`{"data":[]}`))
			return
		}
		body, _ := io.ReadAll(r.Body)
		sawIncludeUsage = strings.Contains(string(body), `"include_usage":true`)
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		time.Sleep(10 * time.Millisecond) // 让 TTFT 可测量
		chunks := []string{
			`data: {"choices":[{"delta":{"content":"你"}}]}`,
			`data: {"choices":[{"delta":{"content":"好"}}]}`,
			`data: {"choices":[{"delta":{},"finish_reason":"stop"}]}`,
			`data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14}}`,
			`data: [DONE]`,
		}
		for _, chunk := range chunks {
			_, _ = fmt.Fprintf(w, "%s\n\n", chunk)
			if flusher != nil {
				flusher.Flush()
			}
		}
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)

	body := `{"model":"fast","stream":true,"messages":[{"role":"user","content":"hi"}]}`
	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions", body, nil)

	if response.Code != http.StatusOK {
		t.Fatalf("状态码应为 200：%d", response.Code)
	}
	if contentType := response.Header().Get("Content-Type"); !strings.Contains(contentType, "text/event-stream") {
		t.Fatalf("流式响应 Content-Type 不对：%q", contentType)
	}
	if response.Header().Get("Cache-Control") != "no-cache" {
		t.Fatalf("Cache-Control 不对：%q", response.Header().Get("Cache-Control"))
	}
	if response.Header().Get("X-Accel-Buffering") != "no" {
		t.Fatalf("X-Accel-Buffering 不对：%q", response.Header().Get("X-Accel-Buffering"))
	}
	streamBody := response.Body.String()
	for _, chunk := range []string{"你", "好", "[DONE]", `"usage"`} {
		if !strings.Contains(streamBody, chunk) {
			t.Fatalf("流式响应缺少 %q：%s", chunk, streamBody)
		}
	}
	if !response.Flushed {
		t.Fatal("流式响应必须逐块 Flush")
	}
	if !sawIncludeUsage {
		t.Fatal("Provider.StreamUsage=true 时应注入 stream_options.include_usage")
	}

	record := lastRecord(t, handler)
	if !record.Stream {
		t.Fatalf("记录应标记为流式：%+v", record)
	}
	if record.TTFTMS <= 0 {
		t.Fatalf("TTFT 应大于 0：%d", record.TTFTMS)
	}
	if record.Usage.PromptTokens != 12 || record.Usage.CompletionTokens != 2 || record.Usage.Estimated {
		t.Fatalf("流式 usage 解析不对：%+v", record.Usage)
	}
	if record.Status != 200 || !record.OK {
		t.Fatalf("记录状态不对：%+v", record)
	}

	text := metricsText(handler)
	if !strings.Contains(text, "tsukiyo_gateway_ttft_seconds_bucket{model=\"m1\",le=") {
		t.Fatalf("TTFT 直方图缺失：\n%s", text)
	}
}

func TestStreamWithoutUsageEstimatesTokens(t *testing.T) {
	upstream := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			_, _ = w.Write([]byte(`{"data":[]}`))
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		// 供应商没声明 stream_usage，也没有 usage 块
		_, _ = fmt.Fprintf(w, "data: %s\n\n", `{"choices":[{"delta":{"content":"你好世界"}}]}`)
		if flusher != nil {
			flusher.Flush()
		}
		_, _ = fmt.Fprintf(w, "data: %s\n\n", "[DONE]")
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Providers[0].StreamUsage = false // 不注入 include_usage
	handler := newTestHandler(t, cfg, nil)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","stream":true,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if response.Code != http.StatusOK {
		t.Fatalf("状态码应为 200：%d", response.Code)
	}
	record := lastRecord(t, handler)
	if !record.Usage.Estimated {
		t.Fatalf("没有 usage 时应标记估算：%+v", record.Usage)
	}
	if record.Usage.CompletionTokens < 3 {
		t.Fatalf("中文 4 字应估算 >= 3 token：%+v", record.Usage)
	}
}

func TestFailoverToFallback(t *testing.T) {
	primaryRecorder := &upstreamRecorder{}
	primary := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		primaryRecorder.record(r)
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"primary down"}`))
	})
	fallbackRecorder := &upstreamRecorder{}
	fallback := newUpstream(t, alwaysJSON(t, fallbackRecorder, chatResponseJSON))

	cfg := testConfig(primary.URL, fallback.URL)
	handler := newTestHandler(t, cfg, nil)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, nil)
	if response.Code != http.StatusOK {
		t.Fatalf("应切换到 fallback 并返回 200：%d %s", response.Code, response.Body.String())
	}
	if primaryRecorder.count() != 1 || fallbackRecorder.count() != 1 {
		t.Fatalf("调用次数不对：primary=%d fallback=%d", primaryRecorder.count(), fallbackRecorder.count())
	}
	record := lastRecord(t, handler)
	if record.Attempts != 2 {
		t.Fatalf("应记录 2 次尝试：%d", record.Attempts)
	}
	if len(record.AttemptLog) != 2 || record.AttemptLog[0].Status != 500 {
		t.Fatalf("attempt log 不对：%+v", record.AttemptLog)
	}
	if record.Model != "m2" || record.Provider != "p2" {
		t.Fatalf("应命中 m2/p2：%+v", record)
	}
}

func TestAllUpstreamsFailReturns502(t *testing.T) {
	failing := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte("upstream exploded"))
	}
	primary := newUpstream(t, failing)
	fallback := newUpstream(t, failing)
	cfg := testConfig(primary.URL, fallback.URL)
	handler := newTestHandler(t, cfg, nil)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, nil)
	if response.Code != http.StatusBadGateway {
		t.Fatalf("应返回 502：%d", response.Code)
	}
	var payload struct {
		Error struct {
			Message  string         `json:"message"`
			Type     string         `json:"type"`
			Attempts []model.Attempt `json:"attempts"`
		} `json:"error"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("502 响应不是合法 JSON：%v", err)
	}
	if payload.Error.Type != "upstream_error" {
		t.Fatalf("error.type 应为 upstream_error：%q", payload.Error.Type)
	}
	if len(payload.Error.Attempts) != 2 {
		t.Fatalf("attempts 应有 2 条：%+v", payload.Error.Attempts)
	}
	if !strings.Contains(payload.Error.Message, "所有上游均失败") {
		t.Fatalf("错误消息不对：%q", payload.Error.Message)
	}
	record := lastRecord(t, handler)
	if record.Status != 502 || record.OK || record.Attempts != 2 {
		t.Fatalf("失败记录不对：%+v", record)
	}
}

func TestBreakerSkipsOpenModel(t *testing.T) {
	failing := func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	primaryRecorder := &upstreamRecorder{}
	primary := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		primaryRecorder.record(r)
		failing(w, r)
	})
	fallbackRecorder := &upstreamRecorder{}
	fallback := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		fallbackRecorder.record(r)
		failing(w, r)
	})

	cfg := testConfig(primary.URL, fallback.URL)
	cfg.Breaker.FailureThreshold = 1 // 一次失败就熔断
	handler := newTestHandler(t, cfg, nil)

	// 第一次：两个模型都失败 → 都进入 open
	first := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, nil)
	if first.Code != http.StatusBadGateway {
		t.Fatalf("第一次应 502：%d", first.Code)
	}
	if handler.opts.Breakers.State("m1") != middleware.StateOpen {
		t.Fatalf("m1 应熔断：%s", handler.opts.Breakers.State("m1"))
	}

	// 第二次：熔断中的模型被跳过，不再打上游
	second := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, nil)
	if second.Code != http.StatusBadGateway {
		t.Fatalf("第二次应 502：%d", second.Code)
	}
	if primaryRecorder.count() != 1 || fallbackRecorder.count() != 1 {
		t.Fatalf("熔断后不应再请求上游：primary=%d fallback=%d",
			primaryRecorder.count(), fallbackRecorder.count())
	}
	record := lastRecord(t, handler)
	if record.Attempts != 0 {
		t.Fatalf("全部跳过时 attempts 应为 0：%d", record.Attempts)
	}
	if len(record.AttemptLog) == 0 || !strings.Contains(record.AttemptLog[0].Error, "熔断") {
		t.Fatalf("attempt log 应说明熔断跳过：%+v", record.AttemptLog)
	}
}

func TestRateLimitRejects429AndRecords(t *testing.T) {
	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Clients = []model.ClientKey{{Key: "sk-test", Name: "limited", Enabled: true, RPM: 1}}

	guards := middleware.NewChain(
		middleware.NewAPIKeyGuard(),
		middleware.NewRateLimitGuard(),
		middleware.NewBudgetGuard(),
	)
	handler := newTestHandler(t, cfg, guards)

	headers := map[string]string{"Authorization": "Bearer sk-test"}
	first := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, headers)
	if first.Code != http.StatusOK {
		t.Fatalf("第一次应成功：%d %s", first.Code, first.Body.String())
	}
	second := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, headers)
	if second.Code != http.StatusTooManyRequests {
		t.Fatalf("第二次应 429：%d %s", second.Code, second.Body.String())
	}
	var payload struct {
		Error struct {
			Type string `json:"type"`
		} `json:"error"`
	}
	_ = json.Unmarshal(second.Body.Bytes(), &payload)
	if payload.Error.Type != "rate_limit_error" {
		t.Fatalf("429 的 error.type 不对：%q", payload.Error.Type)
	}

	records := handler.opts.Tracer.List(tracing.Filter{Limit: 2})
	if len(records) != 2 {
		t.Fatalf("被拒请求也应记录：%d", len(records))
	}
	if records[0].Status != 429 || records[0].OK {
		t.Fatalf("最新记录应为 429 失败：%+v", records[0])
	}
	if records[0].ClientName != "limited" {
		t.Fatalf("记录应带客户端名：%+v", records[0])
	}
	if !strings.Contains(metricsText(handler), `tsukiyo_gateway_ratelimit_rejected_total{client="limited"} 1`) {
		t.Fatalf("限流拒绝指标缺失：\n%s", metricsText(handler))
	}
	// 聚合器里也应能统计到 rate_limited
	if snapshot := handler.opts.Aggregator.Query(300, nil); snapshot.RateLimited != 1 {
		t.Fatalf("聚合器 RateLimited 应为 1：%+v", snapshot)
	}
}

func TestBudgetRejects402(t *testing.T) {
	middleware.DefaultBudgetStore().ResetAll()
	t.Cleanup(func() { middleware.DefaultBudgetStore().ResetAll() })

	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Budget = model.BudgetConfig{Enabled: true, WindowDays: 0}
	cfg.Clients = []model.ClientKey{{Key: "sk-budget", Name: "cheap", Enabled: true, MonthlyUSD: 0.001}}

	guards := middleware.NewChain(
		middleware.NewAPIKeyGuard(),
		middleware.NewRateLimitGuard(),
		middleware.NewBudgetGuard(),
	)
	handler := newTestHandler(t, cfg, guards)
	headers := map[string]string{"Authorization": "Bearer sk-budget"}

	first := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, headers)
	if first.Code != http.StatusOK {
		t.Fatalf("第一次应成功：%d %s", first.Code, first.Body.String())
	}
	// 第一次花费 ~0.018 > 0.001，第二次应被预算拒绝
	second := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, headers)
	if second.Code != http.StatusPaymentRequired {
		t.Fatalf("超预算应 402：%d %s", second.Code, second.Body.String())
	}
	if !strings.Contains(second.Body.String(), "预算已用尽") {
		t.Fatalf("402 消息不对：%s", second.Body.String())
	}
	if !strings.Contains(metricsText(handler), `tsukiyo_gateway_budget_rejected_total{client="cheap"} 1`) {
		t.Fatalf("预算拒绝指标缺失：\n%s", metricsText(handler))
	}
	records := handler.opts.Tracer.List(tracing.Filter{Limit: 1})
	if len(records) != 1 || records[0].Status != 402 {
		t.Fatalf("402 也应记录：%+v", records)
	}
	if snapshot := handler.opts.Aggregator.Query(300, nil); snapshot.BudgetRejects != 1 {
		t.Fatalf("聚合器 BudgetRejects 应为 1：%+v", snapshot)
	}
}

func TestBadAPIKeyRejected(t *testing.T) {
	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Clients = []model.ClientKey{{Key: "sk-good", Name: "good", Enabled: true}}
	guards := middleware.NewChain(middleware.NewAPIKeyGuard())
	handler := newTestHandler(t, cfg, guards)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`,
		map[string]string{"Authorization": "Bearer sk-bad"})
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("错误 Key 应 401：%d", response.Code)
	}
	if !strings.Contains(response.Body.String(), "authentication_error") {
		t.Fatalf("401 响应体不对：%s", response.Body.String())
	}
}

func TestEmbeddings(t *testing.T) {
	embeddingsJSON := `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2]}],"usage":{"prompt_tokens":3,"total_tokens":3}}`
	upstream := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			_, _ = w.Write([]byte(`{"data":[]}`))
			return
		}
		_, _ = w.Write([]byte(embeddingsJSON))
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)

	response := doRequest(t, handler, http.MethodPost, "/v1/embeddings",
		`{"model":"fast","input":"hello"}`, nil)
	if response.Code != http.StatusOK {
		t.Fatalf("embeddings 应 200：%d %s", response.Code, response.Body.String())
	}
	if response.Body.String() != embeddingsJSON {
		t.Fatalf("embeddings 响应应原样透传：%s", response.Body.String())
	}
	record := lastRecord(t, handler)
	if record.Usage.PromptTokens != 3 || record.Status != 200 {
		t.Fatalf("embeddings 记录不对：%+v", record)
	}
}

func TestModelsHealthAndReadyz(t *testing.T) {
	upstream := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		// 探活路径：base_url(/v1) + health_path(/models) = /v1/models
		_, _ = w.Write([]byte(`{"data":[]}`))
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)

	modelsResp := doRequest(t, handler, http.MethodGet, "/v1/models", "", nil)
	if modelsResp.Code != http.StatusOK {
		t.Fatalf("/v1/models 应 200：%d", modelsResp.Code)
	}
	if !strings.Contains(modelsResp.Body.String(), `"id":"fast"`) || !strings.Contains(modelsResp.Body.String(), `"object":"list"`) {
		t.Fatalf("/v1/models 响应不对：%s", modelsResp.Body.String())
	}

	health := doRequest(t, handler, http.MethodGet, "/healthz", "", nil)
	if health.Code != http.StatusOK || !strings.Contains(health.Body.String(), `"ok"`) {
		t.Fatalf("/healthz 不对：%d %s", health.Code, health.Body.String())
	}

	ready := doRequest(t, handler, http.MethodGet, "/readyz", "", nil)
	if ready.Code != http.StatusOK {
		t.Fatalf("/readyz 应 200：%d %s", ready.Code, ready.Body.String())
	}
	if replaced, reason := handler.Ready(); !replaced {
		t.Fatalf("Ready() 应为 true：%s", reason)
	}
	if !strings.Contains(metricsText(handler), `tsukiyo_gateway_provider_up{provider="p1"} 1`) {
		t.Fatalf("provider_up 指标缺失：\n%s", metricsText(handler))
	}

	// 未知路径 → 404 JSON
	notFound := doRequest(t, handler, http.MethodGet, "/v1/nope", "", nil)
	if notFound.Code != http.StatusNotFound {
		t.Fatalf("未知路径应 404：%d", notFound.Code)
	}

	// 方法不支持 → 405
	badMethod := doRequest(t, handler, http.MethodPost, "/v1/models", `{}`, nil)
	if badMethod.Code != http.StatusMethodNotAllowed {
		t.Fatalf("方法不支持应 405：%d", badMethod.Code)
	}
}

func TestReadyzFailsWhenAllProvidersDown(t *testing.T) {
	upstream := newUpstream(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)
	ready := doRequest(t, handler, http.MethodGet, "/readyz", "", nil)
	if ready.Code != http.StatusServiceUnavailable {
		t.Fatalf("全部供应商不可用应 503：%d", ready.Code)
	}
	if ok, reason := handler.Ready(); ok || reason == "" {
		t.Fatalf("Ready() 应返回 false + 原因：%v %q", ok, reason)
	}
}

func TestUnknownModelAndDirectModelID(t *testing.T) {
	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.DefaultRoute = "" // 关闭兜底路由，才能测到「未知模型」
	handler := newTestHandler(t, cfg, nil)

	// 未知模型（没有 default_route）→ 400
	bad := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"does-not-exist","messages":[]}`, nil)
	if bad.Code != http.StatusBadRequest {
		t.Fatalf("未知模型应 400：%d %s", bad.Code, bad.Body.String())
	}

	// 直接使用模型 ID 也可以（合成单目标路由）
	direct := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"m1","messages":[{"role":"user","content":"hi"}]}`, nil)
	if direct.Code != http.StatusOK {
		t.Fatalf("直接用模型 ID 应 200：%d %s", direct.Code, direct.Body.String())
	}
	record := lastRecord(t, handler)
	if record.Route != "m1" || record.Model != "m1" {
		t.Fatalf("直接模型 ID 的记录不对：%+v", record)
	}

	// default_route 兜底
	cfg2 := testConfig(upstream.URL, upstream.URL)
	cfg2.DefaultRoute = "fast"
	handler2 := newTestHandler(t, cfg2, nil)
	fallbackRoute := doRequest(t, handler2, http.MethodPost, "/v1/chat/completions",
		`{"model":"whatever","messages":[{"role":"user","content":"hi"}]}`, nil)
	if fallbackRoute.Code != http.StatusOK {
		t.Fatalf("default_route 应兜底：%d %s", fallbackRoute.Code, fallbackRoute.Body.String())
	}
	if record := lastRecord(t, handler2); record.Route != "fast" {
		t.Fatalf("兜底后 route 应为 fast：%+v", record)
	}
}

func TestSetConfigHotReload(t *testing.T) {
	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	handler := newTestHandler(t, cfg, nil)

	// 新配置新增一条路由 new-alias
	next := testConfig(upstream.URL, upstream.URL)
	next.Routes = append(next.Routes, model.Route{
		Alias: "new-alias", Strategy: "priority", Enabled: true,
		Targets: []model.Target{{Model: "m2"}},
	})
	handler.SetConfig(next)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"new-alias","messages":[{"role":"user","content":"hi"}]}`, nil)
	if response.Code != http.StatusOK {
		t.Fatalf("热更新后新路由应可用：%d %s", response.Code, response.Body.String())
	}
	if record := lastRecord(t, handler); record.Route != "new-alias" {
		t.Fatalf("热更新后路由记录不对：%+v", record)
	}
}

func TestConcurrencyLimit(t *testing.T) {
	started := make(chan struct{}, 1)
	release := make(chan struct{})
	var releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }
	// 无论测试怎么退出都要放行上游，否则 httptest.Server.Close 会一直等挂起的请求。
	defer unblock()

	upstream := newUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			_, _ = w.Write([]byte(`{"data":[]}`))
			return
		}
		select {
		case started <- struct{}{}:
		default:
		}
		// 同时监听请求上下文：客户端断开/服务器关闭时立即返回，避免测试收尾时挂住。
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		_, _ = w.Write([]byte(chatResponseJSON))
	})
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Concurrency = 1
	handler := newTestHandler(t, cfg, nil)

	// 第一个请求放到 goroutine 里跑：它会被上游卡住，模拟「在途请求」。
	// 注意这里不使用 doRequest（它内部会调用 t.Fatalf，不能在子 goroutine 里用）。
	done := make(chan int, 1)
	go func() {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
			strings.NewReader(`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`))
		req.Header.Set("Content-Type", "application/json")
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, req)
		done <- recorder.Code
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("上游没有被调用（第一个请求应当进入转发阶段）")
	}

	// 第一个请求还在途，第二个应该被并发上限拒绝
	second := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`, nil)
	if second.Code != http.StatusServiceUnavailable {
		t.Fatalf("超过并发上限应 503：%d %s", second.Code, second.Body.String())
	}
	unblock()
	select {
	case code := <-done:
		if code != http.StatusOK {
			t.Fatalf("第一个请求应成功：%d", code)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("第一个请求未结束")
	}
}

func TestClientRoutePermission(t *testing.T) {
	upstream := newUpstream(t, alwaysJSON(t, &upstreamRecorder{}, chatResponseJSON))
	cfg := testConfig(upstream.URL, upstream.URL)
	cfg.Clients = []model.ClientKey{{
		Key: "sk-only-slow", Name: "slow-only", Enabled: true, Routes: []string{"slow"},
	}}
	guards := middleware.NewChain(middleware.NewAPIKeyGuard())
	handler := newTestHandler(t, cfg, guards)

	response := doRequest(t, handler, http.MethodPost, "/v1/chat/completions",
		`{"model":"fast","messages":[{"role":"user","content":"hi"}]}`,
		map[string]string{"Authorization": "Bearer sk-only-slow"})
	if response.Code != http.StatusForbidden {
		t.Fatalf("无权访问的路由应 403：%d %s", response.Code, response.Body.String())
	}
}

func TestBuildUpstreamBodyPreservesUnknownFields(t *testing.T) {
	endpoint := &provider.Endpoint{Model: "m1", Upstream: "up-m1"}
	raw := map[string]json.RawMessage{
		"model":    json.RawMessage(`"fast"`),
		"messages": json.RawMessage(`[{"role":"user","content":"hi"}]`),
		"tools":    json.RawMessage(`[{"type":"function"}]`),
		"stop":     json.RawMessage(`["\n\n"]`),
	}
	body, err := buildUpstreamBody(raw, endpoint, true)
	if err != nil {
		t.Fatalf("构造失败：%v", err)
	}
	text := string(body)
	for _, fragment := range []string{`"model":"up-m1"`, `"tools":`, `"stop":`, `"include_usage":true`} {
		if !strings.Contains(text, fragment) {
			t.Fatalf("上游请求体缺少 %q：%s", fragment, text)
		}
	}
	// 不能就地修改客户端的 raw map
	if string(raw["model"]) != `"fast"` {
		t.Fatalf("不应修改原始请求字段：%s", raw["model"])
	}
}

func TestEstimateTokens(t *testing.T) {
	cases := []struct {
		text string
		min  int
		max  int
	}{
		{"", 0, 0},
		{"hello world", 2, 4},
		{"你好世界", 4, 4},
		{"你好 world", 3, 5},
	}
	for _, c := range cases {
		got := EstimateTokens(c.text)
		if got < c.min || got > c.max {
			t.Errorf("EstimateTokens(%q)=%d，期望 %d~%d", c.text, got, c.min, c.max)
		}
	}
}

func TestParseUsageAndCompletionText(t *testing.T) {
	usage, ok := parseUsage([]byte(chatResponseJSON))
	if !ok || usage.PromptTokens != 10 || usage.CompletionTokens != 4 {
		t.Fatalf("parseUsage 不对：%+v %v", usage, ok)
	}
	if _, ok := parseUsage([]byte(`{"choices":[]}`)); ok {
		t.Fatal("没有 usage 时应返回 false")
	}
	if text := completionText([]byte(chatResponseJSON)); text != "你好，世界" {
		t.Fatalf("completionText 不对：%q", text)
	}
	// 多模态 content 数组
	multimodal := []byte(`{"choices":[{"message":{"content":[{"type":"text","text":"图里的猫"}]}}]}`)
	if text := completionText(multimodal); text != "图里的猫" {
		t.Fatalf("多模态 content 解析不对：%q", text)
	}
}

func TestCostCalculation(t *testing.T) {
	endpoint := &provider.Endpoint{Def: model.ModelDef{InputPrice: 3, OutputPrice: 15}}
	usage := model.Usage{PromptTokens: 1000, CompletionTokens: 1000}
	// 1000 prompt tokens = 1K → 3 美元；1000 completion tokens = 1K → 15 美元
	if cost := Cost(endpoint, usage); cost != 18 {
		t.Fatalf("成本计算不对：%v", cost)
	}
	if cost := Cost(nil, usage); cost != 0 {
		t.Fatalf("nil endpoint 成本应为 0：%v", cost)
	}
}

func TestErrorTypeMapping(t *testing.T) {
	cases := map[int]string{
		401: "authentication_error",
		402: "insufficient_quota",
		403: "permission_error",
		429: "rate_limit_error",
		500: "invalid_request_error",
	}
	for status, want := range cases {
		if got := errorTypeForStatus(status); got != want {
			t.Errorf("status %d 类型应为 %q，实际 %q", status, want, got)
		}
	}
}

func TestReadBodyLimit(t *testing.T) {
	big := strings.Repeat("a", maxRequestBody+10)
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(big))
	if _, err := readBody(req); err == nil {
		t.Fatal("超过上限的请求体应报错")
	}
	empty := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(""))
	if _, err := readBody(empty); err == nil {
		t.Fatal("空请求体应报错")
	}
	if body, err := readBody(httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"a":1}`))); err != nil || len(body) == 0 {
		t.Fatalf("正常请求体应可读取：%v", err)
	}
}