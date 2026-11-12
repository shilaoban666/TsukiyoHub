// Package strategy 实现可插拔的选路策略。
//
// 每个策略只负责**排序**：返回按优先级排好的 model.Target 列表，由 proxy 依次尝试
// （proxy 会跳过 Excluded 与熔断中的模型）。新增策略只需：
//
//  1. 实现 Strategy 接口；
//  2. 在自己的 init 里调用 register("名字", 构造函数)。
//
// 不需要改 proxy 或配置——这就是「选路可插拔」的落点。
package strategy

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"tsukiyohub/gateway/internal/model"
)

// Stats 供策略使用的统计视图（由 proxy 侧实现并传入）。
type Stats interface {
	// LatencyEWMA 返回模型最近的平均延迟（毫秒）；无数据返回 0
	LatencyEWMA(modelID string) float64
	// BreakerState 返回 "closed" / "open" / "half_open"
	BreakerState(modelID string) string
}

// PickContext 选路上下文。
type PickContext struct {
	Request   *model.ChatRequest
	Embedding *model.EmbeddingRequest
	Route     model.Route
	Attempt   int
	// Excluded 已失败的模型（本次请求内不再选）；策略可以忽略它，proxy 会再过滤一次
	Excluded map[string]bool
	Stats    Stats
	Client   model.ClientKey
}

// Strategy 选路策略：返回**按优先级排好序**的候选列表。
type Strategy interface {
	Name() string
	Order(ctx *PickContext) ([]model.Target, error)
}

// Factory 策略构造函数；options 来自 Route.Options。
type Factory func(options map[string]any) Strategy

// 内置策略名（供管理界面展示与校验用）。
const (
	NamePriority     = "priority"
	NameWeighted     = "weighted"
	NameRoundRobin   = "round_robin"
	NameLeastLatency = "least_latency"
	NameCost         = "cost"
	NameCanary       = "canary"
	NameSticky       = "sticky"
	NameCapability   = "capability"
)

var registry = map[string]Factory{}

// register 登记内置策略（只应在 init 中调用）。
func register(name string, f Factory) {
	registry[name] = f
}

// New 按名字构造策略；options 来自 Route.Options。
//
// name 为空或 "priority" 时使用优先级策略；未注册的名字返回错误，
// 由 proxy 决定是回退还是报错（当前实现为回退到 priority）。
func New(name string, options map[string]any) (Strategy, error) {
	if strings.TrimSpace(name) == "" {
		name = NamePriority
	}
	name = strings.TrimSpace(name)
	f, ok := registry[name]
	if !ok {
		return nil, fmt.Errorf("strategy: 未知的选路策略 %q（可选：%s）", name, strings.Join(Names(), ", "))
	}
	s := f(options)
	if s == nil {
		return nil, fmt.Errorf("strategy: 策略 %q 构造失败", name)
	}
	return s, nil
}

// Names 返回全部已注册策略名（按字母序）。
func Names() []string {
	out := make([]string, 0, len(registry))
	for name := range registry {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------------------
// 运行时数据（价格 / 能力）
// ---------------------------------------------------------------------------

// 运行时数据的 options 键名。
//
// cost / capability 策略需要「模型价格 / 模型能力」，但这些数据在 model.ModelDef 里
// 而不在 model.Route 里；为了不修改 model 契约，proxy 在构造策略前用 WithConfig
// 把它们合并进 options。
const (
	// OptionPrices 价格表：map[string]Price
	OptionPrices = "__prices"
	// OptionCapabilities 能力表：map[string]Capability
	OptionCapabilities = "__capabilities"
)

// Price 模型价格（每 1K tokens 美元）。
type Price struct {
	Input  float64
	Output float64
}

// Capability 模型能力（能力路由用）。
type Capability struct {
	Context int
	Tools   bool
	Vision  bool
}

// WithConfig 返回合并了配置运行时数据（价格/能力）的 options 副本。
//
// 浅拷贝 + 追加两个内部键，不修改入参（route.Options 是共享配置，绝不能就地写）。
func WithConfig(cfg *model.Config, options map[string]any) map[string]any {
	out := make(map[string]any, len(options)+2)
	for k, v := range options {
		out[k] = v
	}
	if cfg == nil {
		return out
	}
	prices := make(map[string]Price, len(cfg.Models))
	caps := make(map[string]Capability, len(cfg.Models))
	for _, m := range cfg.Models {
		prices[m.ID] = Price{Input: m.InputPrice, Output: m.OutputPrice}
		caps[m.ID] = Capability{Context: m.Context, Tools: m.SupportsTools, Vision: m.SupportsVision}
	}
	out[OptionPrices] = prices
	out[OptionCapabilities] = caps
	return out
}

// priceTable 容错地读取价格表（支持 Price / map / JSON 风格数字）。
func priceTable(options map[string]any) map[string]Price {
	raw, ok := options[OptionPrices]
	if !ok || raw == nil {
		return nil
	}
	switch table := raw.(type) {
	case map[string]Price:
		return table
	case map[string]any:
		out := make(map[string]Price, len(table))
		for id, v := range table {
			switch pv := v.(type) {
			case Price:
				out[id] = pv
			case map[string]any:
				out[id] = Price{
					Input: toFloat(pv["input"]),
					Output: toFloat(pv["output"]),
				}
			case []float64:
				if len(pv) >= 2 {
					out[id] = Price{Input: pv[0], Output: pv[1]}
				}
			}
		}
		return out
	}
	return nil
}

// capabilityTable 容错地读取能力表。
func capabilityTable(options map[string]any) map[string]Capability {
	raw, ok := options[OptionCapabilities]
	if !ok || raw == nil {
		return nil
	}
	switch table := raw.(type) {
	case map[string]Capability:
		return table
	case map[string]any:
		out := make(map[string]Capability, len(table))
		for id, v := range table {
			switch cv := v.(type) {
			case Capability:
				out[id] = cv
			case map[string]any:
				out[id] = Capability{
					Context: toInt(cv["context"]),
					Tools:   toBool(cv["tools"]),
					Vision:  toBool(cv["vision"]),
				}
			}
		}
		return out
	}
	return nil
}

// ---------------------------------------------------------------------------
// 通用工具
// ---------------------------------------------------------------------------

// copyTargets 复制候选（避免策略就地修改配置里的 Targets 切片）。
func copyTargets(route model.Route) []model.Target {
	out := make([]model.Target, len(route.Targets))
	copy(out, route.Targets)
	return out
}

// appendFallbacks 把 Route.Fallbacks 追加到候选尾部（按配置顺序、去重）。
//
// 所有内置策略都用它：fallbacks 是「全部主候选都失败后再试」的兜底模型，
// 因此永远排在策略排序结果之后。
func appendFallbacks(route model.Route, ordered []model.Target) []model.Target {
	if len(route.Fallbacks) == 0 {
		return ordered
	}
	seen := make(map[string]bool, len(ordered))
	for _, t := range ordered {
		seen[t.Model] = true
	}
	for _, id := range route.Fallbacks {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ordered = append(ordered, model.Target{Model: id, Weight: 1})
	}
	return ordered
}

// dedupe 去掉重复目标（保留首次出现），避免同一模型被尝试两次。
func dedupe(targets []model.Target) []model.Target {
	out := make([]model.Target, 0, len(targets))
	seen := make(map[string]bool, len(targets))
	for _, t := range targets {
		if t.Model == "" || seen[t.Model] {
			continue
		}
		seen[t.Model] = true
		out = append(out, t)
	}
	return out
}

// latencyOf 读取模型延迟；无统计或延迟 <= 0 视为「无数据」，返回 -1 让它排前面。
func latencyOf(stats Stats, modelID string) float64 {
	if stats == nil {
		return -1
	}
	v := stats.LatencyEWMA(modelID)
	if v <= 0 {
		return -1
	}
	return v
}

func optionFloat(options map[string]any, key string, def float64) float64 {
	if options == nil {
		return def
	}
	if v, ok := options[key]; ok {
		if f, ok := toFloatOk(v); ok {
			return f
		}
	}
	return def
}

func optionInt(options map[string]any, key string, def int) int {
	if options == nil {
		return def
	}
	if v, ok := options[key]; ok {
		if i, ok := toIntOk(v); ok {
			return i
		}
	}
	return def
}

func optionString(options map[string]any, key, def string) string {
	if options == nil {
		return def
	}
	if v, ok := options[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

func toFloat(v any) float64 {
	f, _ := toFloatOk(v)
	return f
}

func toFloatOk(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case float32:
		return float64(n), true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case int32:
		return float64(n), true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(n), 64)
		return f, err == nil
	}
	return 0, false
}

func toInt(v any) int {
	i, _ := toIntOk(v)
	return i
}

func toIntOk(v any) (int, bool) {
	switch n := v.(type) {
	case float64:
		return int(n), true
	case float32:
		return int(n), true
	case int:
		return n, true
	case int64:
		return int(n), true
	case int32:
		return int(n), true
	case string:
		i, err := strconv.Atoi(strings.TrimSpace(n))
		return i, err == nil
	}
	return 0, false
}

func toBool(v any) bool {
	switch b := v.(type) {
	case bool:
		return b
	case string:
		return b == "true" || b == "1"
	case float64:
		return b != 0
	}
	return false
}

// errNoCandidates 路由没有任何候选时返回的错误（proxy 会转成 400）。
var errNoCandidates = errors.New("strategy: 路由没有任何候选模型")

// orderCost 按模型价格（input+output）升序排序，未知价格排最后。
func orderCost(targets []model.Target, prices map[string]Price) []model.Target {
	if prices == nil {
		return targets
	}
	out := make([]model.Target, len(targets))
	copy(out, targets)
	sort.SliceStable(out, func(i, j int) bool {
		return priceOf(prices, out[i].Model) < priceOf(prices, out[j].Model)
	})
	return out
}

func priceOf(prices map[string]Price, id string) float64 {
	p, ok := prices[id]
	if !ok {
		return 1e18
	}
	return p.Input + p.Output
}