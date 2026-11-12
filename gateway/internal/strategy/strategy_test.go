package strategy

import (
	"testing"

	"tsukiyohub/gateway/internal/model"
)

// fakeStats 测试用统计视图。
type fakeStats struct {
	latency  map[string]float64
	breakers map[string]string
}

func (f *fakeStats) LatencyEWMA(modelID string) float64 {
	if f == nil || f.latency == nil {
		return 0
	}
	return f.latency[modelID]
}

func (f *fakeStats) BreakerState(modelID string) string {
	if f == nil || f.breakers == nil {
		return "closed"
	}
	if state, ok := f.breakers[modelID]; ok {
		return state
	}
	return "closed"
}

// testRoute 构造带三个目标 + 两个 fallback 的路由。
func testRoute(alias, name string) model.Route {
	return model.Route{
		Alias:    alias,
		Strategy: name,
		Targets: []model.Target{
			{Model: "a", Weight: 1, Priority: 20},
			{Model: "b", Weight: 8, Priority: 10},
			{Model: "c", Weight: 1, Priority: 30},
		},
		Fallbacks: []string{"f1", "f2"},
	}
}

func modelNames(targets []model.Target) []string {
	out := make([]string, len(targets))
	for i, t := range targets {
		out[i] = t.Model
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestPriorityOrder(t *testing.T) {
	strat, err := New(NamePriority, nil)
	if err != nil {
		t.Fatalf("New 失败：%v", err)
	}
	route := testRoute("r1", NamePriority)
	ordered, err := strat.Order(&PickContext{Route: route})
	if err != nil {
		t.Fatalf("Order 失败：%v", err)
	}
	// b(10) → a(20) → c(30) → fallbacks
	want := []string{"b", "a", "c", "f1", "f2"}
	if got := modelNames(ordered); !equalStrings(got, want) {
		t.Fatalf("顺序不对：got=%v want=%v", got, want)
	}
}

func TestPriorityDoesNotMutateConfig(t *testing.T) {
	route := testRoute("r2", NamePriority)
	strat, _ := New(NamePriority, nil)
	if _, err := strat.Order(&PickContext{Route: route}); err != nil {
		t.Fatalf("Order 失败：%v", err)
	}
	if route.Targets[0].Model != "a" {
		t.Fatal("策略不应就地修改配置里的 Targets")
	}
}

func TestWeightedReturnsPermutation(t *testing.T) {
	strat, _ := New(NameWeighted, nil)
	route := testRoute("r3", NameWeighted)
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		ordered, err := strat.Order(&PickContext{Route: route})
		if err != nil {
			t.Fatalf("Order 失败：%v", err)
		}
		if len(ordered) != 5 {
			t.Fatalf("候选数不对：%v", modelNames(ordered))
		}
		seen[ordered[0].Model] = true
	}
	if len(seen) < 2 {
		t.Fatalf("加权随机应该出现多个首选目标，实际只有 %v", seen)
	}
}

func TestWeightedFavorsHeavyWeight(t *testing.T) {
	strat, _ := New(NameWeighted, nil)
	route := testRoute("r4", NameWeighted)
	counts := map[string]int{}
	const rounds = 400
	for i := 0; i < rounds; i++ {
		ordered, _ := strat.Order(&PickContext{Route: route})
		counts[ordered[0].Model]++
	}
	// b 权重 8，a/c 权重 1：b 应该明显占多数。
	if counts["b"] < rounds/3 {
		t.Fatalf("权重 8 的目标没有被优先选中：%v", counts)
	}
}

func TestRoundRobinRotation(t *testing.T) {
	strat, _ := New(NameRoundRobin, nil)
	route := testRoute("rr-unique", NameRoundRobin)
	firsts := make([]string, 0, 3)
	for i := 0; i < 3; i++ {
		ordered, err := strat.Order(&PickContext{Route: route})
		if err != nil {
			t.Fatalf("Order 失败：%v", err)
		}
		firsts = append(firsts, ordered[0].Model)
	}
	if !equalStrings(firsts, []string{"a", "b", "c"}) {
		t.Fatalf("轮转顺序不对：%v", firsts)
	}
}

func TestLeastLatencyOrder(t *testing.T) {
	strat, _ := New(NameLeastLatency, nil)
	route := testRoute("r5", NameLeastLatency)
	route.Fallbacks = nil
	stats := &fakeStats{latency: map[string]float64{"a": 300, "b": 100, "c": 200}}
	ordered, err := strat.Order(&PickContext{Route: route, Stats: stats})
	if err != nil {
		t.Fatalf("Order 失败：%v", err)
	}
	if got := modelNames(ordered); !equalStrings(got, []string{"b", "c", "a"}) {
		t.Fatalf("延迟排序不对：%v", got)
	}
}

func TestLeastLatencyNoDataFirst(t *testing.T) {
	strat, _ := New(NameLeastLatency, nil)
	route := testRoute("r6", NameLeastLatency)
	route.Fallbacks = nil
	stats := &fakeStats{latency: map[string]float64{"a": 50}}
	ordered, _ := strat.Order(&PickContext{Route: route, Stats: stats})
	// b、c 无数据，应排在 a 前面。
	if ordered[len(ordered)-1].Model != "a" {
		t.Fatalf("有数据但慢的模型应排最后：%v", modelNames(ordered))
	}
}

func TestCostOrder(t *testing.T) {
	strat, err := New(NameCost, map[string]any{
		OptionPrices: map[string]Price{
			"a": {Input: 5, Output: 5},
			"b": {Input: 1, Output: 1},
			"c": {Input: 3, Output: 3},
		},
	})
	if err != nil {
		t.Fatalf("New 失败：%v", err)
	}
	route := testRoute("r7", NameCost)
	route.Fallbacks = nil
	ordered, _ := strat.Order(&PickContext{Route: route})
	if got := modelNames(ordered); !equalStrings(got, []string{"b", "c", "a"}) {
		t.Fatalf("价格排序不对：%v", got)
	}
}

func TestCanaryPercentBoundaries(t *testing.T) {
	all, _ := New(NameCanary, map[string]any{"percent": 100})
	route := testRoute("r8", NameCanary)
	for i := 0; i < 10; i++ {
		ordered, _ := all.Order(&PickContext{Route: route})
		if ordered[0].Model != "a" {
			t.Fatalf("percent=100 时第一组应打头：%v", modelNames(ordered))
		}
	}
	none, _ := New(NameCanary, map[string]any{"percent": 0})
	plainRoute := testRoute("r8b", NameCanary)
	plainRoute.Fallbacks = nil
	for i := 0; i < 10; i++ {
		ordered, _ := none.Order(&PickContext{Route: plainRoute})
		if ordered[0].Model == "a" {
			t.Fatalf("percent=0 时第一组应垫底：%v", modelNames(ordered))
		}
		if ordered[len(ordered)-1].Model != "a" {
			t.Fatalf("percent=0 时第一组应排在最后：%v", modelNames(ordered))
		}
	}
}

func TestCanaryDefaultsToTen(t *testing.T) {
	strat, err := New(NameCanary, nil)
	if err != nil {
		t.Fatalf("New 失败：%v", err)
	}
	route := testRoute("r9", NameCanary)
	hit := 0
	for i := 0; i < 500; i++ {
		ordered, _ := strat.Order(&PickContext{Route: route})
		if ordered[0].Model == "a" {
			hit++
		}
	}
	// 默认 10%：500 次里应该远小于一半。
	if hit > 200 {
		t.Fatalf("默认灰度比例看起来不是 10%%：%d/500", hit)
	}
}

func TestStickyStablePerUser(t *testing.T) {
	strat, _ := New(NameSticky, nil)
	route := testRoute("r10", NameSticky)
	route.Fallbacks = nil
	first := ""
	for i := 0; i < 20; i++ {
		ordered, err := strat.Order(&PickContext{
			Route:   route,
			Request: &model.ChatRequest{User: "user-42"},
		})
		if err != nil {
			t.Fatalf("Order 失败：%v", err)
		}
		if first == "" {
			first = ordered[0].Model
		}
		if ordered[0].Model != first {
			t.Fatalf("同一用户应稳定命中同一目标：%s != %s", ordered[0].Model, first)
		}
	}
}

func TestStickyUsesFirstMessage(t *testing.T) {
	strat, _ := New(NameSticky, nil)
	route := testRoute("r11", NameSticky)
	route.Fallbacks = nil
	stable := func() string {
		ordered, _ := strat.Order(&PickContext{
			Route: route,
			Request: &model.ChatRequest{Messages: []model.ChatMessage{
				{Role: "system", Content: []byte(`"同一段会话"`)},
			}},
		})
		return ordered[0].Model
	}
	first := stable()
	for i := 0; i < 10; i++ {
		if got := stable(); got != first {
			t.Fatalf("相同首条消息应稳定命中：%s != %s", got, first)
		}
	}
}

func TestCapabilityFilters(t *testing.T) {
	options := map[string]any{
		OptionCapabilities: map[string]Capability{
			"a": {Context: 8000, Tools: false, Vision: false},
			"b": {Context: 128000, Tools: true, Vision: true},
			"c": {Context: 32000, Tools: true, Vision: false},
		},
	}
	strat, err := New(NameCapability, options)
	if err != nil {
		t.Fatalf("New 失败：%v", err)
	}
	route := testRoute("r12", NameCapability)
	route.Fallbacks = nil
	route.RequiredCapabilities = []string{"tools"}
	ordered, _ := strat.Order(&PickContext{Route: route})
	if len(ordered) != 2 {
		t.Fatalf("tools 过滤后应剩 2 个：%v", modelNames(ordered))
	}
	for _, target := range ordered {
		if target.Model == "a" {
			t.Fatalf("a 不支持 tools 不应入选：%v", modelNames(ordered))
		}
	}

	route.RequiredCapabilities = []string{"context:64000"}
	ordered, _ = strat.Order(&PickContext{Route: route})
	if len(ordered) != 1 || ordered[0].Model != "b" {
		t.Fatalf("context:64000 过滤不对：%v", modelNames(ordered))
	}

	route.RequiredCapabilities = []string{"vision"}
	ordered, _ = strat.Order(&PickContext{Route: route})
	if len(ordered) != 1 || ordered[0].Model != "b" {
		t.Fatalf("vision 过滤不对：%v", modelNames(ordered))
	}
}

func TestCapabilityFallsBackWhenAllFiltered(t *testing.T) {
	options := map[string]any{
		OptionCapabilities: map[string]Capability{
			"a": {Context: 1000},
			"b": {Context: 1000},
			"c": {Context: 1000},
		},
	}
	strat, _ := New(NameCapability, options)
	route := testRoute("r13", NameCapability)
	route.Fallbacks = nil
	route.RequiredCapabilities = []string{"vision"}
	ordered, _ := strat.Order(&PickContext{Route: route})
	if len(ordered) != 3 {
		t.Fatalf("全部被过滤时应退回原始候选，实际：%v", modelNames(ordered))
	}
}

func TestNewUnknownAndNames(t *testing.T) {
	if _, err := New("nope", nil); err == nil {
		t.Fatal("未知策略应报错")
	}
	names := Names()
	want := []string{
		NameCanary, NameCapability, NameCost, NameLeastLatency,
		NamePriority, NameRoundRobin, NameSticky, NameWeighted,
	}
	// Names 按字母序：canary < capability < cost < least_latency < priority < round_robin < sticky < weighted
	if !equalStrings(names, want) {
		t.Fatalf("Names 不对：got=%v want=%v", names, want)
	}
	// 空名字退化为 priority
	strat, err := New("", nil)
	if err != nil || strat.Name() != NamePriority {
		t.Fatalf("空策略名应退化为 priority：%v %v", strat, err)
	}
}

func TestWithConfigInjectsRuntimeData(t *testing.T) {
	cfg := &model.Config{
		Models: []model.ModelDef{
			{ID: "a", InputPrice: 9, OutputPrice: 9, Context: 1000},
			{ID: "b", InputPrice: 1, OutputPrice: 1, Context: 200000, SupportsTools: true},
		},
	}
	options := WithConfig(cfg, nil)
	prices := priceTable(options)
	if prices["a"].Input != 9 || prices["b"].Output != 1 {
		t.Fatalf("价格注入不对：%+v", prices)
	}
	caps := capabilityTable(options)
	if !caps["b"].Tools || caps["b"].Context != 200000 {
		t.Fatalf("能力注入不对：%+v", caps)
	}

	// 基于注入的数据跑 cost / capability 策略
	costStrat, _ := New(NameCost, options)
	route := testRoute("r14", NameCost)
	route.Fallbacks = nil
	ordered, _ := costStrat.Order(&PickContext{Route: route})
	if ordered[0].Model != "b" {
		t.Fatalf("cost 应把便宜的排前面：%v", modelNames(ordered))
	}
}

func TestOrderEmptyRoute(t *testing.T) {
	strat, _ := New(NamePriority, nil)
	if _, err := strat.Order(&PickContext{Route: model.Route{Alias: "empty"}}); err == nil {
		t.Fatal("没有候选时应报错")
	}
	if _, err := strat.Order(nil); err == nil {
		t.Fatal("nil 上下文应报错")
	}
}

func TestDedupeTargets(t *testing.T) {
	route := model.Route{
		Alias: "dup", Strategy: NamePriority,
		Targets: []model.Target{
			{Model: "a", Priority: 1},
			{Model: "a", Priority: 2},
			{Model: "b", Priority: 3},
		},
	}
	strat, _ := New(NamePriority, nil)
	ordered, _ := strat.Order(&PickContext{Route: route})
	if got := modelNames(ordered); !equalStrings(got, []string{"a", "b"}) {
		t.Fatalf("重复目标应去重：%v", got)
	}
}