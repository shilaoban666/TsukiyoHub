package alert

// 本文件负责「规则编译」：把配置里的字符串（window / for / cooldown / severity / scope）
// 解析成评估循环里不用再做字符串处理的形式，并集中定义「指标名 → 取值」的映射。
//
// 指标分两类：
//  1. 窗口指标：来自 metrics.Aggregator.Query，取最近 window 秒的统计量；
//  2. 瞬时指标：来自 metrics.Aggregator.Instant，取当前 gauge 值。
//
// 未知指标不是致命错误：规则保留但永远跳过评估，只在第一次遇到时记一条 warning
// （见 evaluate.go 的 resolveLocked），避免一条写错 metric 的规则把整个引擎拖垮。

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

const (
	// maxWindow 窗口上限：Aggregator 只保留最近 900 秒的请求记录，
	// 比它更大的窗口没有数据支撑，直接判为配置错误。
	maxWindow = 900 * time.Second
	// defaultWindow 规则没写 window 时的默认窗口（1 分钟）。
	defaultWindow = time.Minute
)

// 支持的比较运算符（与 model.AlertRule 的注释一致）。
var validOps = map[string]bool{
	">": true, ">=": true, "<": true, "<=": true, "==": true, "!=": true,
}

// scopeKeys 允许的作用域维度，必须与 metrics.Scope.match 的实现保持一致。
var scopeKeys = map[string]bool{
	"route": true, "model": true, "provider": true, "client": true,
}

// windowMetrics 窗口指标名 → StatsSnapshot 取值函数。
//
// 注意 error_rate / rps 等比率是 Aggregator 在窗口内算好的，这里只做取值，
// 不要在引擎里重新算——两处口径不一致会让「面板和告警对不上」。
var windowMetrics = map[string]func(model.StatsSnapshot) float64{
	"error_rate":      func(s model.StatsSnapshot) float64 { return s.ErrorRate },
	"p95_latency_ms":  func(s model.StatsSnapshot) float64 { return s.P95MS },
	"p50_latency_ms":  func(s model.StatsSnapshot) float64 { return s.P50MS },
	"ttft_ms":         func(s model.StatsSnapshot) float64 { return s.TTFTP95MS },
	"rps":             func(s model.StatsSnapshot) float64 { return s.RPS },
	"requests":        func(s model.StatsSnapshot) float64 { return float64(s.Requests) },
	"error_count":     func(s model.StatsSnapshot) float64 { return float64(s.Errors) },
	"tokens":          func(s model.StatsSnapshot) float64 { return float64(s.Tokens) },
	"cost_usd":        func(s model.StatsSnapshot) float64 { return s.CostUSD },
	"rate_limited":    func(s model.StatsSnapshot) float64 { return float64(s.RateLimited) },
	"budget_rejected": func(s model.StatsSnapshot) float64 { return float64(s.BudgetRejects) },
}

// instantMetrics 瞬时指标：走 Aggregator.Instant，window 对它没有意义。
var instantMetrics = map[string]bool{
	"inflight":      true,
	"breaker_open":  true,
	"provider_down": true,
}

// SupportedMetrics 返回引擎认识的全部指标名（已排序，供文档与警告文案使用）。
func SupportedMetrics() []string {
	out := make([]string, 0, len(windowMetrics)+len(instantMetrics))
	for name := range windowMetrics {
		out = append(out, name)
	}
	for name := range instantMetrics {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// 告警级别（min_severity 过滤与排序用）。
const (
	SeverityInfo     = "info"
	SeverityWarning  = "warning"
	SeverityCritical = "critical"
)

// severityRank 级别排序：info(1) < warning(2) < critical(3)；空/未知为 0（不过滤）。
func severityRank(severity string) int {
	switch strings.ToLower(strings.TrimSpace(severity)) {
	case SeverityCritical:
		return 3
	case SeverityWarning:
		return 2
	case SeverityInfo:
		return 1
	}
	return 0
}

// normalizeSeverity 规范化级别名：空 = warning；同时接受几个常见别名；
// 未知级别返回错误（宁可拒绝也不静默降级，否则拼错的消息会悄悄变成 warning）。
func normalizeSeverity(severity string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(severity)) {
	case "":
		return SeverityWarning, nil
	case SeverityInfo, "notice":
		return SeverityInfo, nil
	case SeverityWarning, "warn":
		return SeverityWarning, nil
	case SeverityCritical, "crit", "fatal":
		return SeverityCritical, nil
	}
	return "", fmt.Errorf("severity 非法：%q（支持 info / warning / critical）", severity)
}

// compiledRule 编译后的规则。
//
// 字段说明：
//   - rule      原始规则（规范化后的副本，返回给管理接口时用）
//   - windowSec 秒数，直接传给 Aggregator.Query
//   - instant   是否为瞬时指标（此时窗口不参与取值）
//   - extract   窗口指标取值函数；instant 或未知指标时为 nil
//   - unknown   未知指标：规则保留但跳过，只记一次 warning
type compiledRule struct {
	rule model.AlertRule

	metric    string
	op        string
	threshold float64
	window    time.Duration
	windowSec int
	forDur    time.Duration
	cooldown  time.Duration
	severity  string
	sevRank   int
	scope     metrics.Scope

	instant bool
	extract func(model.StatsSnapshot) float64
	unknown bool
}

// compare 用规则的运算符比较当前值与阈值。
func (cr *compiledRule) compare(value float64) bool {
	switch cr.op {
	case ">":
		return value > cr.threshold
	case ">=":
		return value >= cr.threshold
	case "<":
		return value < cr.threshold
	case "<=":
		return value <= cr.threshold
	case "==":
		return value == cr.threshold
	case "!=":
		return value != cr.threshold
	}
	return false
}

// compileRules 批量编译；返回能用的规则和全部错误。
//
// 调用方要自己决定错误的处理方式：New 记日志后跳过坏的（尽量启动），
// ReplaceRules 则整体拒绝（原子替换，避免半套规则生效）。
func compileRules(rules []model.AlertRule) ([]*compiledRule, []error) {
	compiled := make([]*compiledRule, 0, len(rules))
	var errs []error
	seen := make(map[string]bool, len(rules))
	for i, r := range rules {
		cr, err := compileRule(r)
		if err != nil {
			errs = append(errs, fmt.Errorf("alerts.rules[%d]：%w", i, err))
			continue
		}
		if seen[cr.rule.ID] {
			errs = append(errs, fmt.Errorf("alerts.rules[%d]：告警规则 id 重复：%s", i, cr.rule.ID))
			continue
		}
		seen[cr.rule.ID] = true
		compiled = append(compiled, cr)
	}
	return compiled, errs
}

// compileRule 编译单条规则：规范化字段、解析时长、校验 scope 与指标。
func compileRule(r model.AlertRule) (*compiledRule, error) {
	if strings.TrimSpace(r.ID) == "" {
		return nil, errors.New("告警规则 id 不能为空")
	}
	r.ID = strings.TrimSpace(r.ID)
	if strings.TrimSpace(r.Name) == "" {
		r.Name = r.ID
	}

	metric := strings.ToLower(strings.TrimSpace(r.Metric))
	if metric == "" {
		return nil, fmt.Errorf("规则 %s 缺少 metric", r.ID)
	}
	r.Metric = metric

	op := strings.TrimSpace(r.Op)
	if op == "" {
		op = ">"
	}
	if !validOps[op] {
		return nil, fmt.Errorf("规则 %s 的 op 非法：%q（支持 > >= < <= == !=）", r.ID, op)
	}
	r.Op = op

	severity, err := normalizeSeverity(r.Severity)
	if err != nil {
		return nil, fmt.Errorf("规则 %s：%w", r.ID, err)
	}
	r.Severity = severity

	window, err := parseWindow(r.Window)
	if err != nil {
		return nil, fmt.Errorf("规则 %s 的 window 非法：%w", r.ID, err)
	}
	r.Window = formatDuration(window)

	forDur, err := parseDuration(r.For)
	if err != nil {
		return nil, fmt.Errorf("规则 %s 的 for 非法：%w", r.ID, err)
	}
	r.For = formatDuration(forDur)

	cooldown, err := parseDuration(r.Cooldown)
	if err != nil {
		return nil, fmt.Errorf("规则 %s 的 cooldown 非法：%w", r.ID, err)
	}
	r.Cooldown = formatDuration(cooldown)

	scope, err := compileScope(r.Scope)
	if err != nil {
		return nil, fmt.Errorf("规则 %s：%w", r.ID, err)
	}

	cr := &compiledRule{
		rule:      r,
		metric:    metric,
		op:        op,
		threshold: r.Threshold,
		window:    window,
		windowSec: int(window / time.Second),
		forDur:    forDur,
		cooldown:  cooldown,
		severity:  severity,
		sevRank:   severityRank(severity),
		scope:     scope,
	}
	if extract, ok := windowMetrics[metric]; ok {
		cr.extract = extract
	} else if instantMetrics[metric] {
		cr.instant = true
	} else {
		cr.unknown = true
	}
	return cr, nil
}

// compileScope 把配置里的 scope map 转成 metrics.Scope；
// 未知维度直接报错——否则该规则永远不会命中（match 对未知键返回 false），
// 这种「配了但永远不响」的问题最难排查。
func compileScope(scope map[string]string) (metrics.Scope, error) {
	if len(scope) == 0 {
		return nil, nil
	}
	out := metrics.Scope{}
	for k, v := range scope {
		key := strings.ToLower(strings.TrimSpace(k))
		if !scopeKeys[key] {
			return nil, fmt.Errorf("scope 键 %q 不受支持（支持 route / model / provider / client）", k)
		}
		if strings.TrimSpace(v) == "" {
			continue
		}
		out[key] = v
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// parseDuration 解析时长：支持 Go 时长（30s / 1m / 5m / 10m / 1h）和纯数字秒（"300"）。
func parseDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		if f < 0 {
			return 0, errors.New("不能为负数")
		}
		return time.Duration(f * float64(time.Second)), nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("无法解析 %q（示例：30s / 1m / 5m / 10m）", s)
	}
	if d < 0 {
		return 0, errors.New("不能为负数")
	}
	return d, nil
}

// parseWindow 解析聚合窗口：空 = 1m；最小 1s；最大 maxWindow。
func parseWindow(s string) (time.Duration, error) {
	d, err := parseDuration(s)
	if err != nil {
		return 0, err
	}
	if d == 0 {
		return defaultWindow, nil
	}
	if d < time.Second {
		return 0, errors.New("窗口至少 1 秒")
	}
	if d > maxWindow {
		return 0, fmt.Errorf("窗口最大 %s（聚合器只保留最近 900 秒记录）", formatDuration(maxWindow))
	}
	return d, nil
}

// formatDuration 输出紧凑时长文本：30s / 1m / 90s → 1m30s / 1h。
//
// 用自定义格式而不是 time.Duration.String()，是因为后者会把 1m 打成 "1m0s"，
// 写进通知和配置回显里很难看。
func formatDuration(d time.Duration) string {
	if d <= 0 {
		return "0s"
	}
	if d%time.Hour == 0 {
		return fmt.Sprintf("%dh", d/time.Hour)
	}
	if d%time.Minute == 0 {
		return fmt.Sprintf("%dm", d/time.Minute)
	}
	if d >= time.Minute {
		return fmt.Sprintf("%dm%ds", d/time.Minute, (d%time.Minute)/time.Second)
	}
	if d%time.Second == 0 {
		return fmt.Sprintf("%ds", d/time.Second)
	}
	return d.String()
}

// cloneScope 复制 scope map，避免管理接口/通知拿到引擎内部可变数据。
func cloneScope(scope map[string]string) map[string]string {
	if len(scope) == 0 {
		return nil
	}
	out := make(map[string]string, len(scope))
	for k, v := range scope {
		out[k] = v
	}
	return out
}

// cloneRule 复制规则（深拷贝 scope），用于返回给管理接口。
func cloneRule(r model.AlertRule) model.AlertRule {
	out := r
	out.Scope = cloneScope(r.Scope)
	return out
}
