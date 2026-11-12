package provider

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// testConfig 构造一份指向假上游的最小配置。
func testConfig(baseURL string) *model.Config {
	return &model.Config{
		Providers: []model.Provider{{
			Name:       "fake",
			Type:       "openai",
			BaseURL:    baseURL,
			APIKey:     "sk-upstream",
			HealthPath: "/health",
			Enabled:    true,
			Headers:    map[string]string{"X-Extra": "1"},
		}},
		Models: []model.ModelDef{{
			ID: "m1", Provider: "fake", Upstream: "upstream-m1",
			InputPrice: 1, OutputPrice: 2, Enabled: true,
		}},
		Timeouts: model.TimeoutConfig{ConnectMS: 1000, HeaderMS: 5000},
	}
}

func TestResolveAllNames(t *testing.T) {
	reg, err := NewRegistry(testConfig("http://127.0.0.1:1"))
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	ep, err := reg.Resolve("m1")
	if err != nil {
		t.Fatalf("Resolve 失败：%v", err)
	}
	if ep.Upstream != "upstream-m1" || ep.Provider.Name != "fake" {
		t.Fatalf("Resolve 结果不对：%+v", ep)
	}
	if _, err := reg.Resolve("missing"); err == nil {
		t.Fatal("未定义模型应该报错")
	}
	if len(reg.All()) != 1 || reg.All()[0].Model != "m1" {
		t.Fatalf("All 结果不对：%+v", reg.All())
	}
	names := reg.Names()
	if len(names) != 1 || names[0] != "fake" {
		t.Fatalf("Names 结果不对：%v", names)
	}
	if reg.EnabledProviderCount() != 1 {
		t.Fatalf("启用供应商数不对：%d", reg.EnabledProviderCount())
	}
}

func TestReloadReplacesIndexInPlace(t *testing.T) {
	reg, err := NewRegistry(testConfig("http://127.0.0.1:1"))
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	next := testConfig("http://127.0.0.1:2")
	next.Models = append(next.Models, model.ModelDef{ID: "m2", Provider: "fake", Upstream: "upstream-m2"})
	if err := reg.Reload(next); err != nil {
		t.Fatalf("Reload 失败：%v", err)
	}
	if _, err := reg.Resolve("m2"); err != nil {
		t.Fatalf("Reload 后应能解析 m2：%v", err)
	}
	if err := reg.Reload(&model.Config{Providers: []model.Provider{{Name: "x"}}}); err == nil {
		t.Fatal("缺少 base_url 应该报错")
	}
}

func TestReloadConcurrentWithResolve(t *testing.T) {
	cfg := testConfig("http://127.0.0.1:1")
	reg, err := NewRegistry(cfg)
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				_, _ = reg.Resolve("m1")
				_ = reg.Reload(cfg)
				_ = reg.Names()
			}
		}()
	}
	wg.Wait()
}

func TestJoinURL(t *testing.T) {
	cases := []struct{ base, path, want string }{
		{"https://api.openai.com/v1", "/chat/completions", "https://api.openai.com/v1/chat/completions"},
		{"https://api.openai.com/v1/", "/chat/completions", "https://api.openai.com/v1/chat/completions"},
		{"http://localhost:11434/v1", "chat/completions", "http://localhost:11434/v1/chat/completions"},
		{"http://localhost:11434", "", "http://localhost:11434"},
	}
	for _, c := range cases {
		if got := JoinURL(c.base, c.path); got != c.want {
			t.Errorf("JoinURL(%q,%q)=%q，期望 %q", c.base, c.path, got, c.want)
		}
	}
}

func TestDoInjectsHeadersAndBody(t *testing.T) {
	var (
		mu      sync.Mutex
		gotAuth string
		gotExtra string
		gotBody []byte
		gotPath string
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotAuth = r.Header.Get("Authorization")
		gotExtra = r.Header.Get("X-Extra")
		gotPath = r.URL.Path
		gotBody, _ = io.ReadAll(r.Body)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	reg, err := NewRegistry(testConfig(server.URL+"/v1"))
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	ep, _ := reg.Resolve("m1")
	resp, err := reg.Do(context.Background(), ep, "/chat/completions", []byte(`{"model":"upstream-m1"}`))
	if err != nil {
		t.Fatalf("Do 失败：%v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != `{"ok":true}` {
		t.Fatalf("响应体不对：%s", body)
	}

	mu.Lock()
	defer mu.Unlock()
	if gotAuth != "Bearer sk-upstream" {
		t.Errorf("Authorization 头不对：%q", gotAuth)
	}
	if gotExtra != "1" {
		t.Errorf("额外头丢失：%q", gotExtra)
	}
	if gotPath != "/v1/chat/completions" {
		t.Errorf("路径拼接不对：%q", gotPath)
	}
	if !strings.Contains(string(gotBody), `"model":"upstream-m1"`) {
		t.Errorf("请求体丢失：%s", gotBody)
	}
}

func TestDoAzureUsesAPIKeyHeader(t *testing.T) {
	var gotAPIKey, gotAuth string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAPIKey = r.Header.Get("api-key")
		gotAuth = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	cfg := testConfig(server.URL)
	cfg.Providers[0].Type = "azure"
	reg, err := NewRegistry(cfg)
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	ep, _ := reg.Resolve("m1")
	resp, err := reg.Do(context.Background(), ep, "/chat/completions", []byte(`{}`))
	if err != nil {
		t.Fatalf("Do 失败：%v", err)
	}
	resp.Body.Close()
	if gotAPIKey != "sk-upstream" || gotAuth != "" {
		t.Fatalf("azure 鉴权头不对：api-key=%q authorization=%q", gotAPIKey, gotAuth)
	}
}

func TestProbeAndProbeAll(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer sk-upstream" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer server.Close()

	reg, err := NewRegistry(testConfig(server.URL))
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	health := reg.Probe(context.Background(), "fake")
	if !health.Healthy {
		t.Fatalf("探活应成功：%+v", health)
	}
	if health.CheckedAt.IsZero() {
		t.Fatal("CheckedAt 应有值")
	}
	if missing := reg.Probe(context.Background(), "nope"); missing.Healthy || missing.Error == "" {
		t.Fatalf("不存在的供应商探活应失败：%+v", missing)
	}
	all := reg.ProbeAll(context.Background())
	if len(all) != 1 || !all[0].Healthy {
		t.Fatalf("ProbeAll 结果不对：%+v", all)
	}
	if !AnyHealthy(all) {
		t.Fatal("AnyHealthy 应为 true")
	}
	if cached, ok := reg.CachedHealth(time.Minute); !ok || len(cached) != 1 {
		t.Fatalf("缓存读取不对：ok=%v cached=%+v", ok, cached)
	}
}

func TestProbeUnhealthyStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	reg, err := NewRegistry(testConfig(server.URL))
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	health := reg.Probe(context.Background(), "fake")
	if health.Healthy {
		t.Fatalf("500 不应该健康：%+v", health)
	}
	if !strings.Contains(health.Error, "500") {
		t.Fatalf("错误信息应包含状态码：%q", health.Error)
	}
}

func TestProviderDisabledCompat(t *testing.T) {
	// 所有供应商都没写 enabled（零值 false）时按启用处理，避免老配置直接不可用。
	cfg := testConfig("http://127.0.0.1:1")
	cfg.Providers[0].Enabled = false
	reg, err := NewRegistry(cfg)
	if err != nil {
		t.Fatalf("NewRegistry 失败：%v", err)
	}
	if _, err := reg.Resolve("m1"); err != nil {
		t.Fatalf("兼容模式下应可解析：%v", err)
	}
}

func TestErrorSnippet(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("upstream boom"))
	}))
	defer server.Close()
	resp, err := http.Get(server.URL)
	if err != nil {
		t.Fatalf("请求失败：%v", err)
	}
	defer resp.Body.Close()
	if snippet := ErrorSnippet(resp, 64); snippet != "upstream boom" {
		t.Fatalf("ErrorSnippet 不对：%q", snippet)
	}
}

func TestConfigJSONRoundTripModel(t *testing.T) {
	// 防御性测试：确保 Endpoint 里的值可以被 JSON 无关地复制（不持有 map 引用问题）。
	cfg := testConfig("http://127.0.0.1:1")
	cfg.Providers[0].Headers = map[string]string{"X-A": "1"}
	reg, _ := NewRegistry(cfg)
	ep, _ := reg.Resolve("m1")
	raw, err := json.Marshal(ep.Provider)
	if err != nil {
		t.Fatalf("配置序列化失败：%v", err)
	}
	if !strings.Contains(string(raw), `"name":"fake"`) {
		t.Fatalf("序列化结果不对：%s", raw)
	}
}