package alert

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// testNotification 构造一条通知载荷。
func testNotification(status, severity string) Notification {
	return Notification{
		Event: model.AlertEvent{
			ID:        "evt-1",
			RuleID:    "error_rate_high",
			RuleName:  "错误率过高",
			Metric:    "error_rate",
			Scope:     map[string]string{"route": "fast"},
			Value:     0.42,
			Threshold: 0.1,
			Op:        ">",
			Severity:  severity,
			Message:   "上游错误率过高",
			Status:    status,
			StartsAt:  time.Date(2026, 11, 12, 10, 0, 0, 0, time.UTC),
			Notified:  true,
		},
		Rule: model.AlertRule{
			ID:        "error_rate_high",
			Name:      "错误率过高",
			Metric:    "error_rate",
			Op:        ">",
			Threshold: 0.1,
			Window:    "1m",
			Severity:  severity,
			Message:   "上游错误率过高",
			Enabled:   true,
		},
		Gateway: "http://127.0.0.1:8081",
	}
}

// ---------------------------------------------------------------------------
// webhook
// ---------------------------------------------------------------------------

// TestWebhookPayload 验证 webhook 的 POST 方法、自定义头、JSON 载荷与 2xx 语义。
func TestWebhookPayload(t *testing.T) {
	type captured struct {
		method string
		path   string
		header http.Header
		body   []byte
	}
	var (
		mu  sync.Mutex
		got *captured
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		got = &captured{method: r.Method, path: r.URL.Path, header: r.Header.Clone(), body: body}
		mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	notifier, err := NewNotifier(model.NotifierConfig{
		Type:    "webhook",
		Name:    "hook",
		URL:     srv.URL + "/alert/hook",
		Headers: map[string]string{"X-Token": "secret-token", "X-Source": "tsukiyo"},
		Enabled: true,
	}, nil)
	if err != nil {
		t.Fatalf("构造 webhook 渠道失败：%v", err)
	}
	if err := notifier.Send(context.Background(), testNotification(statusFiring, SeverityCritical)); err != nil {
		t.Fatalf("webhook 发送失败：%v", err)
	}

	mu.Lock()
	snapshot := got
	mu.Unlock()
	if snapshot == nil {
		t.Fatal("webhook 端点没有收到请求")
	}
	if snapshot.method != http.MethodPost {
		t.Fatalf("应为 POST，实际 %s", snapshot.method)
	}
	if snapshot.path != "/alert/hook" {
		t.Fatalf("路径错误：%s", snapshot.path)
	}
	if ct := snapshot.header.Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Fatalf("Content-Type 应为 application/json，实际 %q", ct)
	}
	if token := snapshot.header.Get("X-Token"); token != "secret-token" {
		t.Fatalf("自定义头 X-Token 丢失，实际 %q", token)
	}
	if source := snapshot.header.Get("X-Source"); source != "tsukiyo" {
		t.Fatalf("自定义头 X-Source 丢失，实际 %q", source)
	}

	var payload struct {
		Event   model.AlertEvent `json:"event"`
		Rule    model.AlertRule  `json:"rule"`
		Gateway string           `json:"gateway"`
	}
	if err := json.Unmarshal(snapshot.body, &payload); err != nil {
		t.Fatalf("载荷不是合法 JSON：%v（%s）", err, string(snapshot.body))
	}
	if payload.Event.RuleID != "error_rate_high" || payload.Event.Status != statusFiring {
		t.Fatalf("event 字段不对：%+v", payload.Event)
	}
	if payload.Event.Value != 0.42 || payload.Event.Threshold != 0.1 || payload.Event.Op != ">" {
		t.Fatalf("event 指标字段不对：%+v", payload.Event)
	}
	if payload.Rule.Window != "1m" || payload.Rule.Severity != SeverityCritical {
		t.Fatalf("rule 字段不对：%+v", payload.Rule)
	}
	if payload.Gateway != "http://127.0.0.1:8081" {
		t.Fatalf("gateway 字段不对：%q", payload.Gateway)
	}

	// 非 2xx → 返回错误
	badSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("boom"))
	}))
	defer badSrv.Close()
	bad, err := NewNotifier(model.NotifierConfig{Type: "webhook", Name: "bad", URL: badSrv.URL, Enabled: true}, nil)
	if err != nil {
		t.Fatalf("构造渠道失败：%v", err)
	}
	if err := bad.Send(context.Background(), testNotification(statusFiring, SeverityWarning)); err == nil {
		t.Fatal("5xx 应返回错误")
	}

	// 缺 url 应构造失败
	if _, err := NewNotifier(model.NotifierConfig{Type: "webhook", Name: "nourl"}, nil); err == nil {
		t.Fatal("webhook 缺 url 应构造失败")
	}
}

// TestDispatchTimeout 验证单个渠道的超时（sendTimeout）不会阻塞其它渠道。
func TestDispatchTimeout(t *testing.T) {
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(300 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer slow.Close()

	old := sendTimeout
	sendTimeout = 50 * time.Millisecond
	defer func() { sendTimeout = old }()

	logs := &bytes.Buffer{}
	cfg := &model.Config{
		Alerts: model.AlertConfig{
			Enabled: true,
			Notifiers: []model.NotifierConfig{
				{Type: "webhook", Name: "slow", URL: slow.URL, Enabled: true},
			},
		},
	}
	engine := New(cfg, nil, log.New(logs, "", 0))
	out := []outgoing{{event: testNotification(statusFiring, SeverityCritical).Event, rule: testNotification(statusFiring, SeverityCritical).Rule}}

	start := time.Now()
	engine.dispatch(out, engine.notifiers, "http://127.0.0.1:8081")
	elapsed := time.Since(start)

	if elapsed > 2*time.Second {
		t.Fatalf("发送超时未生效，dispatch 花了 %s", elapsed)
	}
	if !strings.Contains(logs.String(), "发送失败") {
		t.Fatalf("超时应记录失败日志，实际：%s", logs.String())
	}
}

// ---------------------------------------------------------------------------
// 钉钉
// ---------------------------------------------------------------------------

// TestDingtalkSignAndPayload 验证 markdown 载荷与加签参数。
func TestDingtalkSignAndPayload(t *testing.T) {
	const secret = "SECtestsecret"
	var (
		mu    sync.Mutex
		query url.Values
		body  []byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		mu.Lock()
		query = r.URL.Query()
		body = raw
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"errcode":0,"errmsg":"ok"}`))
	}))
	defer srv.Close()

	notifier, err := NewNotifier(model.NotifierConfig{
		Type:    "dingtalk",
		Name:    "dt",
		URL:     srv.URL + "/robot/send?access_token=abcdef",
		Secret:  secret,
		Enabled: true,
	}, nil)
	if err != nil {
		t.Fatalf("构造钉钉渠道失败：%v", err)
	}
	if err := notifier.Send(context.Background(), testNotification(statusFiring, SeverityCritical)); err != nil {
		t.Fatalf("钉钉发送失败：%v", err)
	}

	mu.Lock()
	gotQuery, gotBody := query, append([]byte(nil), body...)
	mu.Unlock()

	if gotQuery.Get("access_token") != "abcdef" {
		t.Fatalf("原有查询参数应保留，实际 %q", gotQuery.Get("access_token"))
	}
	tsRaw := gotQuery.Get("timestamp")
	if tsRaw == "" {
		t.Fatal("加签模式应带 timestamp")
	}
	ts, err := strconv.ParseInt(tsRaw, 10, 64)
	if err != nil {
		t.Fatalf("timestamp 不是毫秒时间戳：%q", tsRaw)
	}
	if delta := time.Since(time.UnixMilli(ts)); delta > time.Minute || delta < -time.Minute {
		t.Fatalf("timestamp 偏离当前时间太多：%s", delta)
	}
	sign := gotQuery.Get("sign")
	if sign == "" {
		t.Fatal("加签模式应带 sign")
	}
	wantSign, err := dingtalkSign(secret, ts)
	if err != nil {
		t.Fatalf("计算签名失败：%v", err)
	}
	if sign != wantSign {
		t.Fatalf("签名不匹配：got %q want %q", sign, wantSign)
	}

	var payload struct {
		MsgType  string `json:"msgtype"`
		Markdown struct {
			Title string `json:"title"`
			Text  string `json:"text"`
		} `json:"markdown"`
	}
	if err := json.Unmarshal(gotBody, &payload); err != nil {
		t.Fatalf("钉钉载荷不是合法 JSON：%v（%s）", err, string(gotBody))
	}
	if payload.MsgType != "markdown" {
		t.Fatalf("msgtype 应为 markdown，实际 %q", payload.MsgType)
	}
	if !strings.Contains(payload.Markdown.Title, "[CRITICAL]") || !strings.Contains(payload.Markdown.Title, "错误率过高") {
		t.Fatalf("markdown.title 不对：%q", payload.Markdown.Title)
	}
	for _, want := range []string{"error_rate", "1m", "fast", "http://127.0.0.1:8081"} {
		if !strings.Contains(payload.Markdown.Text, want) {
			t.Errorf("markdown.text 应包含 %q，实际：%s", want, payload.Markdown.Text)
		}
	}
}

// TestDingtalkWithoutSecret 验证未配置 secret 时不加签。
func TestDingtalkWithoutSecret(t *testing.T) {
	var (
		mu    sync.Mutex
		query url.Values
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		query = r.URL.Query()
		mu.Unlock()
		_, _ = w.Write([]byte(`{"errcode":0}`))
	}))
	defer srv.Close()

	notifier, err := NewNotifier(model.NotifierConfig{
		Type: "dingtalk", Name: "dt", URL: srv.URL + "/robot/send?access_token=abc", Enabled: true,
	}, nil)
	if err != nil {
		t.Fatalf("构造渠道失败：%v", err)
	}
	if err := notifier.Send(context.Background(), testNotification(statusResolved, SeverityWarning)); err != nil {
		t.Fatalf("发送失败：%v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if query.Get("timestamp") != "" || query.Get("sign") != "" {
		t.Fatalf("未配置 secret 时不应加签，query=%v", query)
	}
}

// TestDingtalkErrCode 验证「HTTP 200 但 errcode != 0」被视为失败。
func TestDingtalkErrCode(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"errcode":310000,"errmsg":"sign not match"}`))
	}))
	defer srv.Close()

	notifier, err := NewNotifier(model.NotifierConfig{
		Type: "dingtalk", Name: "dt", URL: srv.URL, Secret: "SECx", Enabled: true,
	}, nil)
	if err != nil {
		t.Fatalf("构造渠道失败：%v", err)
	}
	err = notifier.Send(context.Background(), testNotification(statusFiring, SeverityCritical))
	if err == nil {
		t.Fatal("errcode != 0 应返回错误")
	}
	if !strings.Contains(err.Error(), "310000") {
		t.Fatalf("错误信息应包含 errcode，实际：%v", err)
	}
}

// ---------------------------------------------------------------------------
// slack / log / 过滤
// ---------------------------------------------------------------------------

// TestSlackPayload 验证 slack 的 {"text": "..."} 载荷。
func TestSlackPayload(t *testing.T) {
	var (
		mu   sync.Mutex
		body []byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		mu.Lock()
		body = raw
		mu.Unlock()
		_, _ = w.Write([]byte("ok"))
	}))
	defer srv.Close()

	notifier, err := NewNotifier(model.NotifierConfig{
		Type: "slack", Name: "sl", URL: srv.URL, Enabled: true,
	}, nil)
	if err != nil {
		t.Fatalf("构造渠道失败：%v", err)
	}
	if err := notifier.Send(context.Background(), testNotification(statusFiring, SeverityWarning)); err != nil {
		t.Fatalf("发送失败：%v", err)
	}
	mu.Lock()
	raw := append([]byte(nil), body...)
	mu.Unlock()

	var payload map[string]string
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("slack 载荷不是合法 JSON：%v（%s）", err, raw)
	}
	text, ok := payload["text"]
	if !ok {
		t.Fatalf("slack 载荷应只有 text 字段，实际 %s", raw)
	}
	for _, want := range []string{"error_rate_high", "error_rate", "WARNING"} {
		if !strings.Contains(text, want) {
			t.Errorf("slack text 应包含 %q，实际：%s", want, text)
		}
	}
}

// TestLogNotifier 验证 log 渠道输出单行结构化文本。
func TestLogNotifier(t *testing.T) {
	buf := &bytes.Buffer{}
	notifier, err := NewNotifier(model.NotifierConfig{Type: "log", Name: "lg"}, log.New(buf, "", 0))
	if err != nil {
		t.Fatalf("构造 log 渠道失败：%v", err)
	}
	if err := notifier.Send(context.Background(), testNotification(statusFiring, SeverityCritical)); err != nil {
		t.Fatalf("发送失败：%v", err)
	}
	line := buf.String()
	if strings.Count(line, "\n") != 1 {
		t.Fatalf("log 渠道应只输出一行，实际：%q", line)
	}
	for _, want := range []string{
		"[alert]", "status=firing", "severity=critical", "rule=error_rate_high",
		"metric=error_rate", "threshold", "window=1m", "gateway=http://127.0.0.1:8081",
	} {
		if !strings.Contains(line, want) {
			t.Errorf("日志行应包含 %q，实际：%s", want, line)
		}
	}
}

// TestSeverityFilter 验证 min_severity 过滤：低级别不发，高级别发。
func TestSeverityFilter(t *testing.T) {
	all := &captureNotifier{name: "all"}
	criticalOnly := &captureNotifier{name: "critical"}
	disabled := &captureNotifier{name: "disabled"}
	engine := &Engine{logger: log.New(io.Discard, "", 0)}

	mounts := []*notifierMount{
		{notifier: all, cfg: model.NotifierConfig{Name: "all", Type: "capture", Enabled: true}, minRank: 0},
		{notifier: criticalOnly, cfg: model.NotifierConfig{Name: "critical", Type: "capture", Enabled: true, MinSeverity: SeverityCritical}, minRank: severityRank(SeverityCritical)},
		{notifier: disabled, cfg: model.NotifierConfig{Name: "disabled", Type: "capture", Enabled: false}, minRank: 0},
	}

	// info 级别：只有「不过滤」的渠道收到
	note := testNotification(statusFiring, SeverityInfo)
	engine.dispatch([]outgoing{{event: note.Event, rule: note.Rule}}, mounts, note.Gateway)
	if all.count() != 1 {
		t.Fatalf("info 事件应发给不过滤的渠道，实际 %d", all.count())
	}
	if criticalOnly.count() != 0 {
		t.Fatalf("info 事件不应发给 min_severity=critical 的渠道，实际 %d", criticalOnly.count())
	}
	if disabled.count() != 0 {
		t.Fatalf("禁用的渠道不应收到通知，实际 %d", disabled.count())
	}

	// warning 级别：仍不到 critical
	warn := testNotification(statusFiring, SeverityWarning)
	engine.dispatch([]outgoing{{event: warn.Event, rule: warn.Rule}}, mounts, warn.Gateway)
	if all.count() != 2 || criticalOnly.count() != 0 {
		t.Fatalf("warning 过滤结果不对：all=%d critical=%d", all.count(), criticalOnly.count())
	}

	// critical 级别：两个启用的渠道都收到
	crit := testNotification(statusFiring, SeverityCritical)
	engine.dispatch([]outgoing{{event: crit.Event, rule: crit.Rule}}, mounts, crit.Gateway)
	if all.count() != 3 || criticalOnly.count() != 1 {
		t.Fatalf("critical 过滤结果不对：all=%d critical=%d", all.count(), criticalOnly.count())
	}
	if disabled.count() != 0 {
		t.Fatalf("禁用的渠道始终不应收到通知，实际 %d", disabled.count())
	}

	// 级别排序与解析
	if severityRank(SeverityInfo) >= severityRank(SeverityWarning) ||
		severityRank(SeverityWarning) >= severityRank(SeverityCritical) {
		t.Fatal("级别排序应为 info < warning < critical")
	}
	if rank, err := parseMinSeverity(""); err != nil || rank != 0 {
		t.Fatalf("空 min_severity 应不过滤：rank=%d err=%v", rank, err)
	}
	if _, err := parseMinSeverity("panic"); err == nil {
		t.Fatal("非法 min_severity 应返回错误")
	}
}

// TestNotifierRegistry 验证注册表：四种内置渠道 + 未知类型报错。
func TestNotifierRegistry(t *testing.T) {
	types := SupportedNotifierTypes()
	for _, want := range []string{"log", "webhook", "dingtalk", "slack"} {
		if !contains(types, want) {
			t.Errorf("内置渠道缺少 %q，实际：%v", want, types)
		}
	}
	if _, err := NewNotifier(model.NotifierConfig{Type: "email", Name: "mail"}, nil); err == nil {
		t.Fatal("未注册的类型应返回错误")
	}
	if _, err := NewNotifier(model.NotifierConfig{Name: "no-type"}, nil); err == nil {
		t.Fatal("缺 type 应返回错误")
	}
}

// TestDingtalkTargetQueryJoin 验证 URL 已有查询参数/无参数时都能正确拼接并转义。
func TestDingtalkTargetQueryJoin(t *testing.T) {
	now := time.UnixMilli(1700000000123)

	withQuery, err := dingtalkTarget("https://oapi.dingtalk.com/robot/send?access_token=abc", "SECxyz", now)
	if err != nil {
		t.Fatalf("加签失败：%v", err)
	}
	u, err := url.Parse(withQuery)
	if err != nil {
		t.Fatalf("加签后 URL 非法：%v", err)
	}
	q := u.Query()
	if q.Get("access_token") != "abc" {
		t.Fatalf("access_token 丢失：%s", withQuery)
	}
	if q.Get("timestamp") != "1700000000123" {
		t.Fatalf("timestamp 不对：%s", withQuery)
	}
	if q.Get("sign") == "" {
		t.Fatalf("sign 丢失：%s", withQuery)
	}

	plain, err := dingtalkTarget("https://example.com/robot/send", "SECxyz", now)
	if err != nil {
		t.Fatalf("加签失败：%v", err)
	}
	if !strings.Contains(plain, "?") || !strings.Contains(plain, "sign=") {
		t.Fatalf("无查询参数的 URL 也应正确拼接：%s", plain)
	}

	noSecret, err := dingtalkTarget("https://example.com/robot/send", "", now)
	if err != nil {
		t.Fatalf("无 secret 不应报错：%v", err)
	}
	if noSecret != "https://example.com/robot/send" {
		t.Fatalf("无 secret 时应原样返回：%s", noSecret)
	}
}
