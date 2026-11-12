package metrics

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/model"
)

func TestRegistryPrometheusFormat(t *testing.T) {
	reg := NewRegistry()
	reg.Help("demo_total", "演示计数器")
	reg.Inc("demo_total", map[string]string{"route": "chat"}, 2)
	reg.Set("demo_gauge", nil, 7)
	reg.Observe("demo_seconds", map[string]string{"model": "m1"}, 0.03)

	var buf bytes.Buffer
	reg.WritePrometheus(&buf)
	text := buf.String()

	for _, want := range []string{
		"# HELP demo_total 演示计数器",
		"# TYPE demo_total counter",
		`demo_total{route="chat"} 2`,
		`demo_gauge 7`,
		"# TYPE demo_seconds histogram",
		`demo_seconds_bucket{model="m1",le="0.05"} 1`,
		`demo_seconds_bucket{model="m1",le="+Inf"} 1`,
		`demo_seconds_count{model="m1"} 1`,
		"tsukiyo_gateway_uptime_seconds",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("Prometheus 文本缺少 %q\n%s", want, text)
		}
	}
}

func TestAggregatorQueryScopeAndPercentiles(t *testing.T) {
	agg := NewAggregator()
	now := time.Now()
	appendRecord := func(route, modelID string, status int, latency int64, tokens int) {
		agg.Record(model.RequestRecord{
			At: now.Add(-time.Second), Route: route, Model: modelID, Provider: "p",
			Status: status, OK: status < 400, LatencyMS: latency,
			Usage: model.Usage{TotalTokens: tokens}, CostUSD: 0.001,
		})
	}
	appendRecord("chat", "m1", 200, 100, 10)
	appendRecord("chat", "m1", 500, 300, 10)
	appendRecord("other", "m2", 200, 900, 20)

	all := agg.Query(300, Scope{})
	if all.Requests != 3 {
		t.Fatalf("requests=%d，期望 3", all.Requests)
	}
	if all.Errors != 1 {
		t.Fatalf("errors=%d，期望 1", all.Errors)
	}
	if all.Tokens != 40 {
		t.Fatalf("tokens=%d，期望 40", all.Tokens)
	}
	if all.P95MS < 300 || all.P95MS > 900 {
		t.Fatalf("p95=%.1f，落在不合理区间", all.P95MS)
	}

	chat := agg.Query(300, Scope{"route": "chat"})
	if chat.Requests != 2 || chat.Errors != 1 {
		t.Fatalf("按 route 过滤错误：%+v", chat)
	}
	if chat.ErrorRate <= 0.4 || chat.ErrorRate >= 0.6 {
		t.Fatalf("error_rate=%.3f，期望 0.5", chat.ErrorRate)
	}

	limited := agg.Query(300, Scope{"route": "chat", "model": "m1"})
	if limited.Requests != 2 {
		t.Fatalf("多维度过滤失败：%+v", limited)
	}

	// 瞬时 gauge
	agg.AddInflight(1)
	agg.SetBreakerOpen(2)
	agg.SetProvidersDown(1)
	if got := agg.Instant("inflight"); got != 1 {
		t.Fatalf("inflight=%v", got)
	}
	if got := agg.Instant("breaker_open"); got != 2 {
		t.Fatalf("breaker_open=%v", got)
	}
	if got := agg.Instant("provider_down"); got != 1 {
		t.Fatalf("provider_down=%v", got)
	}
	if got := agg.Instant("unknown"); got != 0 {
		t.Fatalf("未知指标应返回 0，得到 %v", got)
	}
}

func TestAggregatorRateLimitAndBudgetCounters(t *testing.T) {
	agg := NewAggregator()
	agg.Record(model.RequestRecord{At: time.Now(), Route: "chat", Status: 429, OK: false})
	agg.Record(model.RequestRecord{At: time.Now(), Route: "chat", Status: 402, OK: false})
	snap := agg.Query(60, Scope{})
	if snap.RateLimited != 1 || snap.BudgetRejects != 1 {
		t.Fatalf("限流/预算计数错误：%+v", snap)
	}
}

func TestAggregatorRecent(t *testing.T) {
	agg := NewAggregator()
	for i := 0; i < 5; i++ {
		agg.Record(model.RequestRecord{At: time.Now(), Route: "r", Status: 200, OK: true})
	}
	if got := len(agg.Recent(3)); got != 3 {
		t.Fatalf("Recent(3)=%d", got)
	}
	if got := len(agg.Recent(100)); got != 5 {
		t.Fatalf("Recent(100)=%d", got)
	}
}