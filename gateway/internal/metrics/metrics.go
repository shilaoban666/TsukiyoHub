// Package metrics 负责两件事：
//
//  1. **Prometheus 指标**：零依赖手写 exposition 文本（标准格式，Prometheus 直接可抓）。
//  2. **窗口聚合**：为「概览接口」和「告警引擎」提供最近 N 秒的请求统计
//     （QPS / 错误率 / P95 / TTFT / token / 花费），支持按 route / model / provider 过滤。
//
// 为什么自己写而不用 prometheus/client_golang：
// 网关要求单文件、零第三方依赖即可编译（内网/离线环境友好）；我们只需要 counters、
// gauges、histograms 三种类型，刻意不实现 exemplar / native histogram 等高级特性。
package metrics

import (
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// 默认直方图桶（秒）：覆盖 5ms ~ 60s，流式首字与整体延迟都能落进去。
var defaultBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60}

type sample struct {
	labels string
	value  float64
}

type histogram struct {
	labels string
	counts []uint64 // len(buckets)+1（最后一个为 +Inf）
	sum    float64
	count  uint64
}

// Registry 指标注册表（并发安全）。
type Registry struct {
	mu        sync.RWMutex
	counters  map[string]map[string]*sample  // name -> labelsKey -> sample
	gauges    map[string]map[string]*sample
	hists     map[string]map[string]*histogram
	buckets   []float64
	help      map[string]string
	startedAt time.Time
}

// NewRegistry 创建注册表。
func NewRegistry() *Registry {
	return &Registry{
		counters:  map[string]map[string]*sample{},
		gauges:    map[string]map[string]*sample{},
		hists:     map[string]map[string]*histogram{},
		buckets:   defaultBuckets,
		help:      map[string]string{},
		startedAt: time.Now(),
	}
}

// Help 登记指标说明（可选，出现在 # HELP 行）。
func (r *Registry) Help(name, text string) {
	r.mu.Lock()
	r.help[name] = text
	r.mu.Unlock()
}

func labelsKey(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(k)
		b.WriteString(`="`)
		b.WriteString(escapeLabel(labels[k]))
		b.WriteByte('"')
	}
	return b.String()
}

func escapeLabel(v string) string {
	v = strings.ReplaceAll(v, `\`, `\\`)
	v = strings.ReplaceAll(v, "\n", `\n`)
	v = strings.ReplaceAll(v, `"`, `\"`)
	return v
}

// Inc 计数器 +value。
func (r *Registry) Inc(name string, labels map[string]string, value float64) {
	key := labelsKey(labels)
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.counters[name]
	if m == nil {
		m = map[string]*sample{}
		r.counters[name] = m
	}
	if s := m[key]; s != nil {
		s.value += value
		return
	}
	m[key] = &sample{labels: key, value: value}
}

// Set 设置 gauge 当前值。
func (r *Registry) Set(name string, labels map[string]string, value float64) {
	key := labelsKey(labels)
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.gauges[name]
	if m == nil {
		m = map[string]*sample{}
		r.gauges[name] = m
	}
	m[key] = &sample{labels: key, value: value}
}

// Observe 直方图打点。
func (r *Registry) Observe(name string, labels map[string]string, value float64) {
	key := labelsKey(labels)
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.hists[name]
	if m == nil {
		m = map[string]*histogram{}
		r.hists[name] = m
	}
	h := m[key]
	if h == nil {
		h = &histogram{labels: key, counts: make([]uint64, len(r.buckets)+1)}
		m[key] = h
	}
	h.sum += value
	h.count++
	for i, bound := range r.buckets {
		if value <= bound {
			h.counts[i]++
			return
		}
	}
	h.counts[len(r.buckets)]++
}

// WritePrometheus 输出标准 Prometheus 文本格式。
func (r *Registry) WritePrometheus(w io.Writer) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	writeMeta := func(name, typ string) {
		if help := r.help[name]; help != "" {
			fmt.Fprintf(w, "# HELP %s %s\n", name, help)
		}
		fmt.Fprintf(w, "# TYPE %s %s\n", name, typ)
	}
	writeSample := func(name, labels string, value float64) {
		if labels == "" {
			fmt.Fprintf(w, "%s %g\n", name, value)
			return
		}
		fmt.Fprintf(w, "%s{%s} %g\n", name, labels, value)
	}

	for name, series := range r.counters {
		writeMeta(name, "counter")
		for _, s := range series {
			writeSample(name, s.labels, s.value)
		}
	}
	for name, series := range r.gauges {
		writeMeta(name, "gauge")
		for _, s := range series {
			writeSample(name, s.labels, s.value)
		}
	}
	for name, series := range r.hists {
		writeMeta(name, "histogram")
		for _, h := range series {
			var cumulative uint64
			for i, bound := range r.buckets {
				cumulative += h.counts[i]
				writeSample(name+"_bucket", mergeLabels(h.labels, "le", strconv.FormatFloat(bound, 'f', -1, 64)), float64(cumulative))
			}
			cumulative += h.counts[len(r.buckets)]
			writeSample(name+"_bucket", mergeLabels(h.labels, "le", "+Inf"), float64(cumulative))
			writeSample(name+"_sum", h.labels, h.sum)
			writeSample(name+"_count", h.labels, float64(h.count))
		}
	}

	// 进程自观测
	writeMeta("tsukiyo_gateway_uptime_seconds", "gauge")
	fmt.Fprintf(w, "tsukiyo_gateway_uptime_seconds %g\n", time.Since(r.startedAt).Seconds())
}

func mergeLabels(labels, key, value string) string {
	extra := key + `="` + escapeLabel(value) + `"`
	if labels == "" {
		return extra
	}
	return labels + "," + extra
}

// ---------------------------------------------------------------------------
// 窗口聚合（概览 / 告警共用）
// ---------------------------------------------------------------------------

const (
	// 聚合保留窗口：足够覆盖 10 分钟告警窗口
	aggWindowSeconds = 900
	// 最多保留的请求记录（高 QPS 下按最近 N 条近似，够告警与概览使用）
	maxRecords = 30000
)

// Aggregator 保存最近请求记录，按窗口 + 维度过滤后算出统计量。
type Aggregator struct {
	mu       sync.RWMutex
	records  []model.RequestRecord // 环形使用（追加 + 截断）
	total    int64
	inflight int64

	breakerOpen   int64
	providersDown int64
	startedAt     time.Time
}

// NewAggregator 创建聚合器。
func NewAggregator() *Aggregator {
	return &Aggregator{records: make([]model.RequestRecord, 0, maxRecords), startedAt: time.Now()}
}

// Record 记录一次完成的请求。
func (a *Aggregator) Record(rec model.RequestRecord) {
	if rec.At.IsZero() {
		rec.At = time.Now()
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.total++
	a.records = append(a.records, rec)
	if len(a.records) > maxRecords {
		// 一次裁掉 10%，避免每次都 copy 整个切片
		drop := maxRecords / 10
		a.records = append(a.records[:0], a.records[drop:]...)
	}
}

// AddInflight 在途请求计数（+1 / -1）。
func (a *Aggregator) AddInflight(delta int64) {
	a.mu.Lock()
	a.inflight += delta
	if a.inflight < 0 {
		a.inflight = 0
	}
	a.mu.Unlock()
}

// SetInflight 直接设置（用于探活巡检校正）。
func (a *Aggregator) SetInflight(v int64) {
	a.mu.Lock()
	a.inflight = v
	a.mu.Unlock()
}

// Inflight 当前在途数。
func (a *Aggregator) Inflight() int64 {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.inflight
}

// SetBreakerOpen 当前处于 open/half_open 的模型数。
func (a *Aggregator) SetBreakerOpen(v int64) {
	a.mu.Lock()
	a.breakerOpen = v
	a.mu.Unlock()
}

// SetProvidersDown 当前探活失败的供应商数。
func (a *Aggregator) SetProvidersDown(v int64) {
	a.mu.Lock()
	a.providersDown = v
	a.mu.Unlock()
}

// Scope 过滤维度；空字段 = 不限。
type Scope map[string]string

func (s Scope) match(rec model.RequestRecord) bool {
	for key, want := range s {
		switch key {
		case "route":
			if rec.Route != want {
				return false
			}
		case "model":
			if rec.Model != want {
				return false
			}
		case "provider":
			if rec.Provider != want {
				return false
			}
		case "client":
			if rec.ClientName != want {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// Query 返回最近 windowSec 秒内、符合 scope 的统计。
//
// 注意：高 QPS 下记录会被环形截断，统计是**近似值**（用于告警与概览，
// 精确计费请看 budget 的持久化累计）。
func (a *Aggregator) Query(windowSec int, scope Scope) model.StatsSnapshot {
	if windowSec <= 0 || windowSec > aggWindowSeconds {
		windowSec = 300
	}
	cutoff := time.Now().Add(-time.Duration(windowSec) * time.Second)

	a.mu.RLock()
	records := make([]model.RequestRecord, 0, len(a.records))
	for i := len(a.records) - 1; i >= 0; i-- {
		rec := a.records[i]
		if rec.At.Before(cutoff) {
			break
		}
		if scope.match(rec) {
			records = append(records, rec)
		}
	}
	inflight := a.inflight
	breakerOpen := a.breakerOpen
	providersDown := a.providersDown
	a.mu.RUnlock()
	_ = breakerOpen
	_ = providersDown

	snap := model.StatsSnapshot{WindowSec: windowSec, Inflight: inflight}
	if len(records) == 0 {
		return snap
	}

	latencies := make([]int64, 0, len(records))
	ttfts := make([]int64, 0, len(records))
	var sumLatency int64
	for _, rec := range records {
		snap.Requests++
		snap.Tokens += int64(rec.Usage.TotalTokens)
		snap.CostUSD += rec.CostUSD
		if !rec.OK || rec.Status >= 400 {
			snap.Errors++
		}
		switch rec.Status {
		case 429:
			snap.RateLimited++
		case 402:
			snap.BudgetRejects++
		}
		latencies = append(latencies, rec.LatencyMS)
		sumLatency += rec.LatencyMS
		if rec.TTFTMS > 0 {
			ttfts = append(ttfts, rec.TTFTMS)
		}
	}
	if snap.Requests > 0 {
		snap.ErrorRate = float64(snap.Errors) / float64(snap.Requests)
		snap.RPS = float64(snap.Requests) / float64(windowSec)
	}
	snap.P50MS = float64(percentile(latencies, 50))
	snap.P95MS = float64(percentile(latencies, 95))
	if len(ttfts) > 0 {
		snap.TTFTP95MS = float64(percentile(ttfts, 95))
	}
	_ = sumLatency
	snap.CostUSD = math.Round(snap.CostUSD*1e6) / 1e6
	return snap
}

// Instant 返回瞬时 gauge 值（供告警的瞬时指标使用）。
func (a *Aggregator) Instant(metric string) float64 {
	a.mu.RLock()
	defer a.mu.RUnlock()
	switch metric {
	case "inflight":
		return float64(a.inflight)
	case "breaker_open":
		return float64(a.breakerOpen)
	case "provider_down":
		return float64(a.providersDown)
	}
	return 0
}

// Total 网关启动以来的完成请求数。
func (a *Aggregator) Total() int64 {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.total
}

// percentile 线性插值分位数（latencies 不会被外部修改）。
func percentile(values []int64, p float64) int64 {
	if len(values) == 0 {
		return 0
	}
	sorted := make([]int64, len(values))
	copy(sorted, values)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	if len(sorted) == 1 {
		return sorted[0]
	}
	rank := p / 100 * float64(len(sorted)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))
	if lower < 0 {
		lower = 0
	}
	if upper >= len(sorted) {
		upper = len(sorted) - 1
	}
	if lower == upper {
		return sorted[lower]
	}
	weight := rank - float64(lower)
	return int64(float64(sorted[lower])*(1-weight) + float64(sorted[upper])*weight)
}

// Recent 返回最近 n 条记录（最新在前），供管理接口展示。
func (a *Aggregator) Recent(n int) []model.RequestRecord {
	if n <= 0 {
		n = 50
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	out := make([]model.RequestRecord, 0, n)
	for i := len(a.records) - 1; i >= 0 && len(out) < n; i-- {
		out = append(out, a.records[i])
	}
	return out
}