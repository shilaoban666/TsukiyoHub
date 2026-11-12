package alert

// 本文件是通知层的公共部分：
//   - Notifier 接口与注册表（新增渠道 = 实现接口 + init 里 Register，不动引擎）；
//   - 按 MinSeverity 过滤 + 并发发送 + 单渠道 5s 超时（失败只记日志，不影响其它渠道）；
//   - 通知文案的统一格式化（单行日志版 / 多行 markdown 版）。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// sendTimeout 单个渠道一次发送的超时。测试会把它临时改小。
var sendTimeout = 5 * time.Second

// HTTPClient 通知专用 HTTP 客户端。
//
// 刻意不设置 Proxy：钉钉机器人 / 内网 webhook 一般要求直连，
// 跟随 HTTP_PROXY 反而在容器里经常连不通。连接超时由 ctx 控制。
//
// 外部包实现自定义渠道时应复用它，保持超时/连接池行为一致：
//
//	alert.PostJSON(ctx, alert.HTTPClient, url, headers, payload)
var HTTPClient = &http.Client{
	Transport: &http.Transport{
		MaxIdleConns:        8,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	},
}

// Notification 一次通知的渠道无关载荷。
//
// 各渠道自己决定怎么渲染：log 输出单行、钉钉用 markdown、slack 用 text、
// webhook 直接序列化成 {"event":..,"rule":..,"gateway":..}。
type Notification struct {
	// Event 事件快照（Status 为 firing 或 resolved）
	Event model.AlertEvent
	// Rule 触发该事件的规则定义
	Rule model.AlertRule
	// Gateway 网关对外地址（config.public_url），便于通知里带上入口链接
	Gateway string
}

// Notifier 通知渠道接口：实现它并在 init 里 Register 就能接入新渠道。
type Notifier interface {
	// Name 渠道展示名（配置里的 name）
	Name() string
	// Type 渠道类型（log / webhook / dingtalk / slack / ...）
	Type() string
	// Send 发送一次通知；返回错误表示失败（引擎只记日志，不影响其它渠道）
	Send(ctx context.Context, n Notification) error
}

// NotifierFactory 渠道构造函数；logger 用于 log 渠道与内部警告。
type NotifierFactory func(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error)

var (
	factoriesMu sync.RWMutex
	factories   = map[string]NotifierFactory{}
)

// Register 注册一种通知渠道类型（同名后注册覆盖先注册）。
//
// 只允许在 init 或单测里调用，不要运行期并发注册；注册表用读写锁保护，
// 因此与「引擎正在发送通知」并发也是安全的。
func Register(typ string, factory NotifierFactory) {
	typ = strings.ToLower(strings.TrimSpace(typ))
	if typ == "" || factory == nil {
		return
	}
	factoriesMu.Lock()
	factories[typ] = factory
	factoriesMu.Unlock()
}

// NewNotifier 按配置构造渠道；类型未知或配置缺字段时返回错误。
func NewNotifier(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error) {
	if logger == nil {
		logger = log.Default()
	}
	typ := strings.ToLower(strings.TrimSpace(cfg.Type))
	if typ == "" {
		return nil, errors.New("通知渠道缺少 type")
	}
	factoriesMu.RLock()
	factory := factories[typ]
	factoriesMu.RUnlock()
	if factory == nil {
		return nil, fmt.Errorf("不支持的通知渠道类型 %q（支持：%s）",
			typ, strings.Join(SupportedNotifierTypes(), ", "))
	}
	n, err := factory(cfg, logger)
	if err != nil {
		return nil, err
	}
	if n == nil {
		return nil, fmt.Errorf("通知渠道 %q 构造结果为空", typ)
	}
	return n, nil
}

// SupportedNotifierTypes 返回已注册的渠道类型（排序后）。
func SupportedNotifierTypes() []string {
	factoriesMu.RLock()
	out := make([]string, 0, len(factories))
	for typ := range factories {
		out = append(out, typ)
	}
	factoriesMu.RUnlock()
	sort.Strings(out)
	return out
}

// notifierMount 一个已构造的渠道 + 发送策略（是否启用、最低级别）。
type notifierMount struct {
	notifier Notifier
	cfg      model.NotifierConfig
	minRank  int
}

// allows 判断该渠道是否愿意接收这个级别的事件。
func (m *notifierMount) allows(severity string) bool {
	return severityRank(severity) >= m.minRank
}

// parseMinSeverity 解析 min_severity；空 = 不过滤（返回 0）。
func parseMinSeverity(s string) (int, error) {
	if strings.TrimSpace(s) == "" {
		return 0, nil
	}
	severity, err := normalizeSeverity(s)
	if err != nil {
		return 0, err
	}
	return severityRank(severity), nil
}

// buildNotifiers 按配置构建全部渠道；坏的渠道（未知类型、缺 url）只记 warning 并跳过。
//
// 即使 Enabled=false 也构建并保留在列表里，这样管理界面能展示「有渠道但被关掉」。
func buildNotifiers(cfgs []model.NotifierConfig, logger *log.Logger) []*notifierMount {
	if logger == nil {
		logger = log.Default()
	}
	out := make([]*notifierMount, 0, len(cfgs))
	for i, cfg := range cfgs {
		if strings.TrimSpace(cfg.Name) == "" {
			cfg.Name = fmt.Sprintf("%s-%d", strings.ToLower(cfg.Type), i+1)
		}
		n, err := NewNotifier(cfg, logger)
		if err != nil {
			logger.Printf("[alert] 通知渠道 %s(%s) 配置无效，已忽略：%v", cfg.Name, cfg.Type, err)
			continue
		}
		minRank, err := parseMinSeverity(cfg.MinSeverity)
		if err != nil {
			logger.Printf("[alert] 通知渠道 %s 的 min_severity 非法（%v），按不过滤处理", cfg.Name, err)
			minRank = 0
		}
		out = append(out, &notifierMount{notifier: n, cfg: cfg, minRank: minRank})
	}
	return out
}

// dispatch 把通知并发发给所有启用的渠道，每个渠道独立 5s 超时。
//
// 之所以同步等待（而不是 fire-and-forget）：事件状态（Notified/状态流转）与
// 实际发送顺序一致，单测也能直接断言；评估在后台 goroutine 里跑，等几秒无妨。
func (e *Engine) dispatch(outs []outgoing, mounts []*notifierMount, gateway string) {
	for _, out := range outs {
		note := Notification{Event: out.event, Rule: out.rule, Gateway: gateway}
		var wg sync.WaitGroup
		for _, m := range mounts {
			if !m.cfg.Enabled || !m.allows(out.event.Severity) {
				continue
			}
			wg.Add(1)
			go func(m *notifierMount) {
				defer wg.Done()
				ctx, cancel := context.WithTimeout(context.Background(), sendTimeout)
				defer cancel()
				if err := m.notifier.Send(ctx, note); err != nil {
					e.logger.Printf("[alert] 通知渠道 %s(%s) 发送失败：%v", m.cfg.Name, m.cfg.Type, err)
				}
			}(m)
		}
		wg.Wait()
	}
}

// PostJSON 发送 JSON POST 并读取响应体；2xx 返回 nil 错误。
//
// 返回响应体是为了让钉钉这类「HTTP 200 但 errcode != 0」的接口自己判断。
// 导出给自定义渠道复用（见 docs/alerts.md「加渠道」）。
func PostJSON(ctx context.Context, client *http.Client, rawURL string, headers map[string]string, payload any) ([]byte, int, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, fmt.Errorf("序列化通知载荷失败：%w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, rawURL, bytes.NewReader(body))
	if err != nil {
		return nil, 0, fmt.Errorf("构造通知请求失败：%w", err)
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("User-Agent", "tsukiyo-gateway-alert/1.0")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return respBody, resp.StatusCode, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return respBody, resp.StatusCode, nil
}

// bodySnippet 截断响应体，写进错误信息便于排查（避免把整页 HTML 打进日志）。
func bodySnippet(body []byte) string {
	s := strings.TrimSpace(string(body))
	s = strings.ReplaceAll(s, "\n", " ")
	if s == "" {
		return ""
	}
	if len(s) > 200 {
		s = s[:200] + "..."
	}
	return " 响应：" + s
}

// ---------------------------------------------------------------------------
// 通知文案
// ---------------------------------------------------------------------------

// statusLabel 事件状态的中文标签。
func statusLabel(status string) string {
	switch status {
	case statusFiring:
		return "触发"
	case statusResolved:
		return "已恢复"
	case statusPending:
		return "待触发"
	case statusAcked:
		return "已确认"
	}
	return status
}

// formatValue 数值的紧凑表示（避免 0.15300000000000002 这种浮点噪声）。
func formatValue(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}

// formatScope 把 scope 拍平成 "route=fast model=gpt-4o" 的形式。
func formatScope(scope map[string]string) string {
	if len(scope) == 0 {
		return ""
	}
	keys := make([]string, 0, len(scope))
	for k := range scope {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+scope[k])
	}
	return strings.Join(parts, " ")
}

// notificationTitle 通知标题，形如 "[CRITICAL] error_rate_high"。
func notificationTitle(n Notification) string {
	severity := strings.ToUpper(strings.TrimSpace(n.Event.Severity))
	if severity == "" {
		severity = "ALERT"
	}
	name := strings.TrimSpace(n.Event.RuleName)
	if name == "" {
		name = n.Event.RuleID
	}
	return fmt.Sprintf("[%s] %s", severity, name)
}

// notificationLine 单行结构化文本（log 渠道 / 日志排查用）。
func notificationLine(n Notification) string {
	ev, rule := n.Event, n.Rule
	scope := formatScope(ev.Scope)
	if scope == "" {
		scope = "全局"
	}
	window := rule.Window
	if window == "" {
		window = "instant"
	}
	message := ""
	if msg := strings.TrimSpace(ev.Message); msg != "" {
		message = " message=" + strings.ReplaceAll(msg, "\n", " ")
	}
	return fmt.Sprintf(
		"[alert] status=%s severity=%s rule=%s name=%s metric=%s value=%s threshold=%s%s window=%s scope=%s at=%s gateway=%s%s",
		ev.Status, ev.Severity, ev.RuleID, rule.Name, ev.Metric,
		formatValue(ev.Value), ev.Op, formatValue(ev.Threshold),
		window, scope, ev.StartsAt.Format(time.RFC3339), n.Gateway, message,
	)
}

// NotificationText 多行纯文本（slack / 通用 webhook 文本字段用，不用 markdown 语法）。
//
// 导出的原因：自定义渠道（飞书/企微/自建网关）通常只需要一段纯文本，
// 不用自己拼规则名、指标值、阈值、窗口这些字段。
func NotificationText(n Notification) string {
	return renderNotification(n, false)
}

// NotificationMarkdown 多行 markdown（钉钉机器人用；自定义渠道也可复用）。
func NotificationMarkdown(n Notification) string {
	return renderNotification(n, true)
}

// renderNotification 组装通知正文；markdown=true 时加标题层级与列表符号。
func renderNotification(n Notification, markdown bool) string {
	ev, rule := n.Event, n.Rule
	scope := formatScope(ev.Scope)
	if scope == "" {
		scope = "全局"
	}
	window := rule.Window
	if window == "" {
		window = "即时值"
	}
	header := fmt.Sprintf("%s %s", notificationTitle(n), statusLabel(ev.Status))

	lines := []string{
		fmt.Sprintf("规则：%s（%s）", ev.RuleID, rule.Name),
		fmt.Sprintf("指标：%s = %s（阈值 %s %s，窗口 %s）",
			ev.Metric, formatValue(ev.Value), ev.Op, formatValue(ev.Threshold), window),
		fmt.Sprintf("级别：%s", strings.ToUpper(ev.Severity)),
		fmt.Sprintf("作用域：%s", scope),
		fmt.Sprintf("时间：%s", ev.StartsAt.Format("2006-01-02 15:04:05")),
	}
	if gateway := strings.TrimSpace(n.Gateway); gateway != "" {
		lines = append(lines, "网关："+gateway)
	}
	if msg := strings.TrimSpace(ev.Message); msg != "" {
		lines = append(lines, "说明："+msg)
	}

	var b strings.Builder
	if markdown {
		fmt.Fprintf(&b, "### %s\n\n", header)
		for _, line := range lines {
			b.WriteString("- ")
			b.WriteString(line)
			b.WriteString("\n")
		}
	} else {
		b.WriteString(header)
		b.WriteString("\n")
		for _, line := range lines {
			b.WriteString(line)
			b.WriteString("\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}
