package strategy

import (
	"sort"

	"tsukiyohub/gateway/internal/model"
)

// leastLatencyStrategy 按 Stats.LatencyEWMA 升序；无数据的模型排最前面。
//
// 「无数据先试」是刻意的：新模型/刚重启的模型还没统计，先让它接一次流量
// 才能拿到延迟数据，否则会被老模型一直压住。
type leastLatencyStrategy struct{}

func init() {
	register(NameLeastLatency, func(map[string]any) Strategy { return leastLatencyStrategy{} })
}

// Name 策略名。
func (leastLatencyStrategy) Name() string { return NameLeastLatency }

// Order 按延迟升序稳定排序，并追加 fallbacks。
func (leastLatencyStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	ordered := dedupe(copyTargets(ctx.Route))
	if len(ordered) == 0 {
		return appendFallbacks(ctx.Route, ordered), nil
	}
	sort.SliceStable(ordered, func(i, j int) bool {
		return latencyOf(ctx.Stats, ordered[i].Model) < latencyOf(ctx.Stats, ordered[j].Model)
	})
	return appendFallbacks(ctx.Route, ordered), nil
}