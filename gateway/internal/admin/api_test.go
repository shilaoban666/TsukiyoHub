package admin

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/config"
	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
	"tsukiyohub/gateway/internal/tracing"
)

// stubSource 测试用 ConfigSource。
type stubSource struct {
	mu  sync.Mutex
	cfg *model.Config
}

func (s *stubSource) Current() *model.Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg
}

func (s *stubSource) set(cfg *model.Config) {
	s.mu.Lock()
	s.cfg = cfg
	s.mu.Unlock()
}

// testAdmin 装配好的管理接口与依赖。
type testAdmin struct {
	handler http.Handler
	opts    Options
	source  *stubSource
	mu      sync.Mutex
	reloads []*model.Config
}

// reloaded 返回 Reload 被调用时收到的配置。
func (a *testAdmin) reloaded() []*model.Config {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]*model.Config(nil), a.reloads...)
}

// baseConfig 构造一份最小可用配置。
func baseConfig(upstreamURL string) *model.Config {
	cfg := &model.Config{
		Listen:       ":8081",
		AdminToken:   "",
		PublicURL:    "http://localhost:8081",
		MaxAttempts:  2,
		Providers: []model.Provider{{
			Name: "p1", Type: "openai", BaseURL: upstreamURL + "/v1",
			APIKey: "sk-1234567890abcdef", HealthPath: "/models", Enabled: true,
		}},
		Models: []model.ModelDef{{
			ID: "m1", Provider: "p1", Upstream: "up-m1",
			InputPrice: 1, OutputPrice: 2, Enabled: true,
		}},
		Routes: []model.Route{{
			Alias: "fast", Strategy: "priority", Enabled: true,
			Targets: []model.Target{{Model: "m1"}},
		}},
		DefaultRoute: "fast",
		Breaker: model.BreakerConfig{
			Enabled: true, FailureThreshold: 5, OpenSeconds: 30,
			HalfOpenMax: 2, WindowSeconds: 60,
		},
		Tracing:  model.TracingConfig{Enabled: true, MaxRecords: 100},
		Timeouts: model.TimeoutConfig{ConnectMS: 2000, HeaderMS: 5000},
	}
	cfg.WithDefaults()
	return cfg
}

// newTestAdmin 装配管理接口。
func newTestAdmin(t *testing.T, cfg *model.Config, mutate func(*Options)) *testAdmin {
	t.Helper()
	source := &stubSource{cfg: cfg}
	registry, err := provider.NewRegistry(cfg)
	if err != nil {
		t.Fatalf("构造 provider 注册表失败：%v", err)
	}
	admin := &testAdmin{source: source}
	opts := Options{
		ConfigPath: "config.test.json",
		Config:     source,
		Reload: func(next *model.Config) error {
			admin.mu.Lock()
			admin.reloads = append(admin.reloads, next)
			admin.mu.Unlock()
			source.set(next)
			return nil
		},
		Providers:  registry,
		Breakers:   middleware.NewBreakers(cfg.Breaker),
		Aggregator: metrics.NewAggregator(),
		Tracer:     tracing.NewRing(100),
		Metrics:    metrics.NewRegistry(),
		Version:    "test-1.0",
		StartedAt:  time.Now(),
	}
	if mutate != nil {
		mutate(&opts)
	}
	admin.opts = opts
	admin.handler = New(opts)
	return admin
}

// adminRequest 发一个管理接口请求。
func adminRequest(t *testing.T, handler http.Handler, method, path, body, remote string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("")
	} else {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	if remote != "" {
		req.RemoteAddr = remote
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	return recorder
}

// decodeMap 解析 JSON 响应。
func decodeMap(t *testing.T, recorder *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	payload := map[string]any{}
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatalf("响应不是合法 JSON：%v body=%s", err, recorder.Body.String())
	}
	return payload
}

func TestAuthLoopbackOnly(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)

	// 本机可访问
	ok := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "127.0.0.1:5000", nil)
	if ok.Code != http.StatusOK {
		t.Fatalf("本机访问应 200：%d %s", ok.Code, ok.Body.String())
	}
	ipv6 := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "[::1]:5000", nil)
	if ipv6.Code != http.StatusOK {
		t.Fatalf("IPv6 本机访问应 200：%d", ipv6.Code)
	}

	// 非本机拒绝
	remote := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "8.8.8.8:5000", nil)
	if remote.Code != http.StatusForbidden {
		t.Fatalf("非本机应 403：%d %s", remote.Code, remote.Body.String())
	}
	if !strings.Contains(remote.Body.String(), "admin_token") {
		t.Fatalf("403 应说明原因：%s", remote.Body.String())
	}

	// CORS 预检不要求鉴权
	preflight := adminRequest(t, admin.handler, http.MethodOptions, "/admin/api/overview", "", "8.8.8.8:5000",
		map[string]string{"Origin": "http://localhost:5173"})
	if preflight.Code != http.StatusNoContent {
		t.Fatalf("预检应 204：%d", preflight.Code)
	}
	if preflight.Header().Get("Access-Control-Allow-Origin") != "http://localhost:5173" {
		t.Fatalf("CORS 头缺失：%v", preflight.Header())
	}
}

func TestAuthToken(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	cfg.AdminToken = "secret-token-value"
	admin := newTestAdmin(t, cfg, nil)

	noAuth := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "127.0.0.1:5000", nil)
	if noAuth.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401：%d", noAuth.Code)
	}
	wrong := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "127.0.0.1:5000",
		map[string]string{"Authorization": "Bearer wrong"})
	if wrong.Code != http.StatusUnauthorized {
		t.Fatalf("错误 token 应 401：%d", wrong.Code)
	}
	correct := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "8.8.8.8:5000",
		map[string]string{"Authorization": "Bearer secret-token-value"})
	if correct.Code != http.StatusOK {
		t.Fatalf("正确 token 应 200（外部地址也可）：%d %s", correct.Code, correct.Body.String())
	}
	headerToken := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "8.8.8.8:5000",
		map[string]string{"X-Admin-Token": "secret-token-value"})
	if headerToken.Code != http.StatusOK {
		t.Fatalf("X-Admin-Token 应支持：%d", headerToken.Code)
	}
}

func TestGetConfigRedacted(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	cfg.AdminToken = "admin-secret-123456"
	admin := newTestAdmin(t, cfg, nil)

	headers := map[string]string{"Authorization": "Bearer admin-secret-123456"}
	unauthorized := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/config", "", "127.0.0.1:5000", nil)
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("设置 admin_token 后无 token 应 401：%d", unauthorized.Code)
	}
	response := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/config", "", "127.0.0.1:5000", headers)
	if response.Code != http.StatusOK {
		t.Fatalf("GET config 应 200：%d %s", response.Code, response.Body.String())
	}
	payload := decodeMap(t, response)
	if _, ok := payload["config"]; !ok {
		t.Fatalf("响应缺少 config：%v", payload)
	}
	if hash, _ := payload["config_hash"].(string); hash == "" {
		t.Fatalf("config_hash 不能为空：%v", payload)
	}
	if needsRestart, _ := payload["needs_restart"].(bool); needsRestart {
		t.Fatalf("needs_restart 应为 false：%v", payload)
	}
	configNode, _ := payload["config"].(map[string]any)
	providers, _ := configNode["providers"].([]any)
	if len(providers) != 1 {
		t.Fatalf("providers 数量不对：%v", configNode)
	}
	providerNode := providers[0].(map[string]any)
	if providerNode["api_key"] != "sk-1****cdef" {
		t.Fatalf("api_key 应脱敏：%v", providerNode["api_key"])
	}
	if configNode["admin_token"] != "admi****3456" {
		t.Fatalf("admin_token 应脱敏：%v", configNode["admin_token"])
	}
}

func TestPutConfigMergesSecretsAndDetectsRestart(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	cfg.AdminToken = "admin-secret-123456"
	admin := newTestAdmin(t, cfg, nil)

	// 模拟前端：先 GET 脱敏配置，改一点东西再 PUT 回来
	headers := map[string]string{"Authorization": "Bearer admin-secret-123456"}
	getResponse := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/config", "", "127.0.0.1:5000", headers)
	if getResponse.Code != http.StatusOK {
		t.Fatalf("GET config 应 200：%d %s", getResponse.Code, getResponse.Body.String())
	}
	payload := decodeMap(t, getResponse)
	nextConfig := payload["config"].(map[string]any)
	nextConfig["listen"] = ":9090" // 触发 needs_restart
	routes := nextConfig["routes"].([]any)
	routes = append(routes, map[string]any{
		"alias": "new-route", "strategy": "priority", "enabled": true,
		"targets": []any{map[string]any{"model": "m1", "weight": 1}},
	})
	nextConfig["routes"] = routes
	body, err := json.Marshal(nextConfig)
	if err != nil {
		t.Fatalf("序列化失败：%v", err)
	}

	putResponse := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/config", string(body), "127.0.0.1:5000", headers)
	if putResponse.Code != http.StatusOK {
		t.Fatalf("PUT config 应 200：%d %s", putResponse.Code, putResponse.Body.String())
	}
	putPayload := decodeMap(t, putResponse)
	if needsRestart, _ := putPayload["needs_restart"].(bool); !needsRestart {
		t.Fatalf("listen 变化应 needs_restart=true：%v", putPayload)
	}

	reloads := admin.reloaded()
	if len(reloads) != 1 {
		t.Fatalf("Reload 应被调用 1 次：%d", len(reloads))
	}
	applied := reloads[0]
	if applied.Providers[0].APIKey != "sk-1234567890abcdef" {
		t.Fatalf("脱敏密钥应被还原：%q", applied.Providers[0].APIKey)
	}
	if applied.AdminToken != "admin-secret-123456" {
		t.Fatalf("admin_token 应被还原：%q", applied.AdminToken)
	}
	if applied.Listen != ":9090" {
		t.Fatalf("listen 应更新：%q", applied.Listen)
	}
	if len(applied.Routes) != 2 {
		t.Fatalf("新增路由未生效：%+v", applied.Routes)
	}
	if current := admin.source.Current(); current.Listen != ":9090" {
		t.Fatalf("ConfigSource 未更新：%+v", current)
	}
}

func TestPutConfigValidationError(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)

	broken := cfg
	broken.Routes = []model.Route{{
		Alias: "bad", Strategy: "priority", Enabled: true,
		Targets: []model.Target{{Model: "not-exist"}},
	}}
	body, _ := json.Marshal(broken)
	response := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/config", string(body), "127.0.0.1:5000", nil)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("非法配置应 400：%d %s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), "校验失败") {
		t.Fatalf("400 应说明校验失败：%s", response.Body.String())
	}
	if len(admin.reloaded()) != 0 {
		t.Fatal("校验失败不应触发 Reload")
	}
}

func TestPutConfigBadJSON(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)
	response := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/config", "{not json", "127.0.0.1:5000", nil)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应 400：%d", response.Code)
	}
}

func TestPutRoutes(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)

	routesBody := `[{"alias":"cheap","strategy":"cost","enabled":true,"targets":[{"model":"m1","weight":1}]}]`
	response := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/routes", routesBody, "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("PUT routes 应 200：%d %s", response.Code, response.Body.String())
	}
	reloads := admin.reloaded()
	if len(reloads) != 1 || len(reloads[0].Routes) != 1 || reloads[0].Routes[0].Alias != "cheap" {
		t.Fatalf("routes 未正确替换：%+v", reloads)
	}
	// 默认路由被删掉时应自动指向第一个路由（而不是让网关配置失效）
	if reloads[0].DefaultRoute != "cheap" {
		t.Fatalf("default_route 应自动修复：%q", reloads[0].DefaultRoute)
	}
	// 其它配置保持不变
	if reloads[0].Providers[0].Name != "p1" || reloads[0].Listen != ":8081" {
		t.Fatalf("不应改动其它字段：%+v", reloads[0])
	}

	// GET 应能看到新路由与策略列表
	getResponse := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/routes", "", "127.0.0.1:5000", nil)
	if getResponse.Code != http.StatusOK {
		t.Fatalf("GET routes 应 200：%d", getResponse.Code)
	}
	payload := decodeMap(t, getResponse)
	if !strings.Contains(getResponse.Body.String(), `"alias":"cheap"`) {
		t.Fatalf("GET routes 未返回新路由：%s", getResponse.Body.String())
	}
	strategies, _ := payload["strategies"].([]any)
	if len(strategies) < 8 {
		t.Fatalf("应返回全部策略：%v", strategies)
	}

	// 对象形式也支持
	objectBody := `{"routes":[{"alias":"fast","strategy":"priority","enabled":true,"targets":[{"model":"m1"}]}]}`
	if resp := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/routes", objectBody, "127.0.0.1:5000", nil); resp.Code != http.StatusOK {
		t.Fatalf("对象形式的 routes 也应支持：%d %s", resp.Code, resp.Body.String())
	}
}

func TestTracesEndpoints(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)

	now := time.Now()
	admin.opts.Tracer.Add(model.RequestRecord{ID: "ok-1", At: now, Route: "fast", Model: "m1", Status: 200, OK: true})
	admin.opts.Tracer.Add(model.RequestRecord{ID: "bad-1", At: now, Route: "fast", Model: "m1", Status: 502, OK: false})

	all := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/traces?limit=10", "", "127.0.0.1:5000", nil)
	if all.Code != http.StatusOK {
		t.Fatalf("traces 应 200：%d", all.Code)
	}
	if !strings.Contains(all.Body.String(), `"bad-1"`) || !strings.Contains(all.Body.String(), `"ok-1"`) {
		t.Fatalf("traces 返回不全：%s", all.Body.String())
	}

	onlyFail := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/traces?ok=false", "", "127.0.0.1:5000", nil)
	if !strings.Contains(onlyFail.Body.String(), `"bad-1"`) || strings.Contains(onlyFail.Body.String(), `"ok-1"`) {
		t.Fatalf("ok=false 过滤不对：%s", onlyFail.Body.String())
	}

	detail := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/traces/ok-1", "", "127.0.0.1:5000", nil)
	if detail.Code != http.StatusOK || !strings.Contains(detail.Body.String(), `"status":200`) {
		t.Fatalf("单条记录不对：%d %s", detail.Code, detail.Body.String())
	}
	missing := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/traces/nope", "", "127.0.0.1:5000", nil)
	if missing.Code != http.StatusNotFound {
		t.Fatalf("不存在的记录应 404：%d", missing.Code)
	}
}

func TestStatsEndpoint(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)
	admin.opts.Aggregator.Record(model.RequestRecord{
		ID: "x", At: time.Now(), Route: "fast", Model: "m1", Provider: "p1",
		Status: 200, OK: true, LatencyMS: 120, Usage: model.Usage{TotalTokens: 7},
	})
	response := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/stats?window=300", "", "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("stats 应 200：%d", response.Code)
	}
	var snapshot model.StatsSnapshot
	if err := json.Unmarshal(response.Body.Bytes(), &snapshot); err != nil {
		t.Fatalf("解析 stats 失败：%v", err)
	}
	if snapshot.Requests != 1 || snapshot.Tokens != 7 || snapshot.P95MS != 120 {
		t.Fatalf("stats 数据不对：%+v", snapshot)
	}
	// 按 route 过滤
	filtered := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/stats?route=nope", "", "127.0.0.1:5000", nil)
	var empty model.StatsSnapshot
	_ = json.Unmarshal(filtered.Body.Bytes(), &empty)
	if empty.Requests != 0 {
		t.Fatalf("不匹配的 scope 应返回 0：%+v", empty)
	}
}

func TestOverviewEndpoint(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer upstream.Close()

	cfg := baseConfig(upstream.URL)
	admin := newTestAdmin(t, cfg, nil)
	admin.opts.Aggregator.Record(model.RequestRecord{
		ID: "x", At: time.Now(), Route: "fast", Model: "m1", Provider: "p1",
		Status: 200, OK: true, LatencyMS: 50, Usage: model.Usage{TotalTokens: 3},
	})

	response := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/overview", "", "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("overview 应 200：%d %s", response.Code, response.Body.String())
	}
	var overview model.Overview
	if err := json.Unmarshal(response.Body.Bytes(), &overview); err != nil {
		t.Fatalf("解析 overview 失败：%v", err)
	}
	if overview.Version != "test-1.0" {
		t.Fatalf("version 不对：%q", overview.Version)
	}
	if overview.Routes != 1 || overview.Models != 1 {
		t.Fatalf("路由/模型数不对：%+v", overview)
	}
	if overview.ConfigHash == "" || overview.ConfigPath != "config.test.json" {
		t.Fatalf("配置信息不对：%+v", overview)
	}
	if overview.Metrics.Requests != 1 {
		t.Fatalf("overview 统计不对：%+v", overview.Metrics)
	}
	if len(overview.Providers) != 1 || !overview.Providers[0].Healthy {
		t.Fatalf("供应商健康信息不对：%+v", overview.Providers)
	}
	if overview.Breakers == nil {
		t.Fatal("breakers 字段不应为 null")
	}
	if overview.Alerts.Enabled {
		t.Fatalf("未启用告警时 Alerts.Enabled 应为 false：%+v", overview.Alerts)
	}
}

func TestProvidersEndpoints(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer upstream.Close()
	cfg := baseConfig(upstream.URL)
	admin := newTestAdmin(t, cfg, nil)

	list := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/providers", "", "127.0.0.1:5000", nil)
	if list.Code != http.StatusOK || !strings.Contains(list.Body.String(), `"healthy":true`) {
		t.Fatalf("providers 列表不对：%d %s", list.Code, list.Body.String())
	}

	probe := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/providers/p1/probe", "", "127.0.0.1:5000", nil)
	if probe.Code != http.StatusOK || !strings.Contains(probe.Body.String(), `"healthy":true`) {
		t.Fatalf("主动探活不对：%d %s", probe.Code, probe.Body.String())
	}

	missing := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/providers/nope/probe", "", "127.0.0.1:5000", nil)
	if missing.Code != http.StatusNotFound {
		t.Fatalf("不存在的供应商应 404：%d", missing.Code)
	}
}

func TestAlertsDisabledWhenEngineNil(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil) // Alerts 为 nil

	for _, path := range []string{
		"/admin/api/alerts/rules",
		"/admin/api/alerts/events",
		"/admin/api/alerts/overview",
	} {
		response := adminRequest(t, admin.handler, http.MethodGet, path, "", "127.0.0.1:5000", nil)
		if response.Code != http.StatusOK {
			t.Fatalf("%s 应 200：%d %s", path, response.Code, response.Body.String())
		}
		if !strings.Contains(response.Body.String(), `"enabled":false`) {
			t.Fatalf("%s 应返回 enabled:false：%s", path, response.Body.String())
		}
	}

	ack := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/alerts/abc/ack", `{"by":"web"}`, "127.0.0.1:5000", nil)
	if ack.Code != http.StatusServiceUnavailable {
		t.Fatalf("未启用时确认告警应 503：%d %s", ack.Code, ack.Body.String())
	}
	if !strings.Contains(ack.Body.String(), `"enabled":false`) {
		t.Fatalf("503 响应应带 enabled:false：%s", ack.Body.String())
	}
}

// fakeAlertEngine 假的告警引擎，验证 admin 与 model.AlertEngine 的接线。
type fakeAlertEngine struct {
	mu     sync.Mutex
	rules  []model.AlertRule
	events []model.AlertEvent
	acked  map[string]string
}

func newFakeAlertEngine() *fakeAlertEngine {
	return &fakeAlertEngine{
		rules:  []model.AlertRule{{ID: "r1", Name: "错误率高", Metric: "error_rate", Op: ">", Threshold: 0.1, Enabled: true}},
		events: []model.AlertEvent{{ID: "e1", RuleID: "r1", Status: "firing", Severity: "critical"}},
		acked:  map[string]string{},
	}
}

func (f *fakeAlertEngine) Start() {}
func (f *fakeAlertEngine) Stop()  {}

func (f *fakeAlertEngine) Overview() model.AlertOverview {
	f.mu.Lock()
	defer f.mu.Unlock()
	return model.AlertOverview{
		Enabled: true, Rules: len(f.rules), Firing: 1,
		Recent: append([]model.AlertEvent(nil), f.events...),
	}
}

func (f *fakeAlertEngine) ListRules() []model.AlertRule {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]model.AlertRule(nil), f.rules...)
}

func (f *fakeAlertEngine) ReplaceRules(rules []model.AlertRule) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rules = append([]model.AlertRule(nil), rules...)
	return nil
}

func (f *fakeAlertEngine) ListEvents(limit int, status string) []model.AlertEvent {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]model.AlertEvent, 0, len(f.events))
	for _, event := range f.events {
		if status != "" && event.Status != status {
			continue
		}
		out = append(out, event)
		if len(out) >= limit {
			break
		}
	}
	return out
}

func (f *fakeAlertEngine) Ack(id, by string, _ time.Duration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.acked[id] = by
	return nil
}

func (f *fakeAlertEngine) Notifiers() []model.NotifierView { return nil }

func TestAlertsWithEngine(t *testing.T) {
	engine := newFakeAlertEngine()
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, func(opts *Options) {
		opts.Alerts = engine
	})

	rules := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/alerts/rules", "", "127.0.0.1:5000", nil)
	if rules.Code != http.StatusOK || !strings.Contains(rules.Body.String(), `"enabled":true`) || !strings.Contains(rules.Body.String(), `"r1"`) {
		t.Fatalf("rules 响应不对：%d %s", rules.Code, rules.Body.String())
	}

	replaceBody := `{"rules":[{"id":"r2","name":"延迟高","metric":"p95_latency_ms","op":">","threshold":5000,"enabled":true}]}`
	replace := adminRequest(t, admin.handler, http.MethodPut, "/admin/api/alerts/rules", replaceBody, "127.0.0.1:5000", nil)
	if replace.Code != http.StatusOK || !strings.Contains(replace.Body.String(), `"r2"`) {
		t.Fatalf("替换规则失败：%d %s", replace.Code, replace.Body.String())
	}
	if len(engine.ListRules()) != 1 || engine.ListRules()[0].ID != "r2" {
		t.Fatalf("规则未替换：%+v", engine.ListRules())
	}

	events := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/alerts/events?status=firing", "", "127.0.0.1:5000", nil)
	if events.Code != http.StatusOK || !strings.Contains(events.Body.String(), `"e1"`) {
		t.Fatalf("events 响应不对：%d %s", events.Code, events.Body.String())
	}

	ack := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/alerts/e1/ack", `{"by":"tester","minutes":15}`, "127.0.0.1:5000", nil)
	if ack.Code != http.StatusOK || !strings.Contains(ack.Body.String(), `"ok":true`) {
		t.Fatalf("ack 失败：%d %s", ack.Code, ack.Body.String())
	}
	if engine.acked["e1"] != "tester" {
		t.Fatalf("ack 未传递给引擎：%+v", engine.acked)
	}

	overview := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/alerts/overview", "", "127.0.0.1:5000", nil)
	if overview.Code != http.StatusOK || !strings.Contains(overview.Body.String(), `"firing":1`) {
		t.Fatalf("alerts overview 不对：%d %s", overview.Code, overview.Body.String())
	}
}

func TestBudgetEndpoints(t *testing.T) {
	store := middleware.NewBudgetStore()
	guard := middleware.NewBudgetGuardWithStore(store)
	guards := middleware.NewChain(middleware.NewAPIKeyGuard(), guard)
	cfg := baseConfig("http://127.0.0.1:1")
	cfg.Budget = model.BudgetConfig{Enabled: true}
	cfg.Clients = []model.ClientKey{{Key: "sk-a", Name: "client-a", Enabled: true, MonthlyUSD: 5}}
	admin := newTestAdmin(t, cfg, func(opts *Options) { opts.Guards = guards })
	store.Add(cfg.Clients[0], 1.25, 1000, 0)

	response := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/budget", "", "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("budget 应 200：%d %s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), `"enabled":true`) || !strings.Contains(response.Body.String(), `"client":"client-a"`) {
		t.Fatalf("budget 响应不对：%s", response.Body.String())
	}

	reset := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/budget/reset", `{"client":"client-a"}`, "127.0.0.1:5000", nil)
	if reset.Code != http.StatusOK {
		t.Fatalf("reset 应 200：%d", reset.Code)
	}
	if spent, _ := store.Spent(cfg.Clients[0], 0); spent != 0 {
		t.Fatalf("reset 后花费应为 0：%v", spent)
	}
}

func TestMetricsEndpoint(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)
	admin.opts.Metrics.Inc("tsukiyo_gateway_requests_total",
		map[string]string{"route": "fast", "model": "m1", "provider": "p1", "status": "200"}, 3)

	response := adminRequest(t, admin.handler, http.MethodGet, "/metrics", "", "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("/metrics 应 200：%d", response.Code)
	}
	if contentType := response.Header().Get("Content-Type"); !strings.Contains(contentType, "text/plain") {
		t.Fatalf("Content-Type 不对：%q", contentType)
	}
	text := response.Body.String()
	if !strings.Contains(text, `tsukiyo_gateway_requests_total{model="m1",provider="p1",route="fast",status="200"} 3`) {
		t.Fatalf("/metrics 内容不对：\n%s", text)
	}
	if !strings.Contains(text, "tsukiyo_gateway_uptime_seconds") {
		t.Fatalf("/metrics 缺少 uptime：\n%s", text)
	}
}

func TestNotFoundAndReloadWithoutPath(t *testing.T) {
	cfg := baseConfig("http://127.0.0.1:1")
	admin := newTestAdmin(t, cfg, nil)

	notFound := adminRequest(t, admin.handler, http.MethodGet, "/admin/api/unknown", "", "127.0.0.1:5000", nil)
	if notFound.Code != http.StatusNotFound {
		t.Fatalf("未知路径应 404：%d", notFound.Code)
	}
	if !strings.Contains(notFound.Body.String(), `"error"`) {
		t.Fatalf("404 应是 JSON 错误：%s", notFound.Body.String())
	}

	// ConfigPath 为空时 reload 应 400
	admin2 := newTestAdmin(t, baseConfig("http://127.0.0.1:1"), func(opts *Options) {
		opts.ConfigPath = ""
	})
	reload := adminRequest(t, admin2.handler, http.MethodPost, "/admin/api/config/reload", "", "127.0.0.1:5000", nil)
	if reload.Code != http.StatusBadRequest {
		t.Fatalf("未配置 ConfigPath 时 reload 应 400：%d %s", reload.Code, reload.Body.String())
	}
}

func TestReloadFromFile(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/config.json"
	cfg := baseConfig("http://127.0.0.1:1")
	if err := config.Save(path, cfg); err != nil {
		t.Fatalf("写配置文件失败：%v", err)
	}
	admin := newTestAdmin(t, cfg, func(opts *Options) { opts.ConfigPath = path })

	response := adminRequest(t, admin.handler, http.MethodPost, "/admin/api/config/reload", "", "127.0.0.1:5000", nil)
	if response.Code != http.StatusOK {
		t.Fatalf("reload 应 200：%d %s", response.Code, response.Body.String())
	}
	if len(admin.reloaded()) != 1 {
		t.Fatalf("Reload 应被调用：%d", len(admin.reloaded()))
	}
}

func TestIsLoopbackAddr(t *testing.T) {
	cases := map[string]bool{
		"127.0.0.1:1234": true,
		"[::1]:1234":     true,
		"localhost:80":   true,
		"192.168.1.2:80": false,
		"8.8.8.8:443":    false,
		"not-an-addr":    false,
	}
	for addr, want := range cases {
		if got := isLoopbackAddr(addr); got != want {
			t.Errorf("isLoopbackAddr(%q)=%v，期望 %v", addr, got, want)
		}
	}
}

func TestQueryHelpers(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/x?a=5&flag=true&since=300", nil)
	if v := queryInt(req, "a", 10, 1, 100); v != 5 {
		t.Fatalf("queryInt 不对：%d", v)
	}
	if v := queryInt(req, "missing", 10, 1, 100); v != 10 {
		t.Fatalf("queryInt 默认值不对：%d", v)
	}
	if v := queryInt(req, "a", 10, 1, 3); v != 3 {
		t.Fatalf("queryInt 上限裁剪不对：%d", v)
	}
	flag := queryBool(req, "flag")
	if flag == nil || !*flag {
		t.Fatalf("queryBool 不对：%v", flag)
	}
	if queryBool(req, "nope") != nil {
		t.Fatal("queryBool 缺省应为 nil")
	}
	if since := parseSince("5m"); since.IsZero() || time.Since(since) < 4*time.Minute {
		t.Fatalf("parseSince 时长解析不对：%v", since)
	}
	if since := parseSince(""); !since.IsZero() {
		t.Fatalf("空 since 应为零值：%v", since)
	}
	if ts := parseSince("1700000000"); ts.Unix() != 1700000000 {
		t.Fatalf("parseSince Unix 秒解析不对：%v", ts)
	}
}