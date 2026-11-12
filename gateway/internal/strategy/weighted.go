package strategy

import (
	"math/rand"

	"tsukiyohub/gateway/internal/model"
)

// weightedStrategy 按 Target.Weight 加权随机排序。
//
// 实现方式是「按权重无放回抽样」：每次都按剩余目标的权重抽一个，抽完继续，
// 于是返回的整个列表既是随机顺序，又保证权重大的更容易排到前面。
// Weight <= 0 视为 1，避免配置漏写导致该目标永远选不到。
type weightedStrategy struct{}

func init() {
	register(NameWeighted, func(map[string]any) Strategy { return weightedStrategy{} })
}

// Name 策略名。
func (weightedStrategy) Name() string { return NameWeighted }

// Order 加权随机排序，并追加 fallbacks。
func (weightedStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	remaining := dedupe(copyTargets(ctx.Route))
	if len(remaining) == 0 {
		return appendFallbacks(ctx.Route, remaining), nil
	}

	ordered := make([]model.Target, 0, len(remaining))
	for len(remaining) > 0 {
		total := 0
		for _, t := range remaining {
			total += weightOf(t)
		}
		pick := rand.Intn(total) //nolint:gosec // 负载均衡无需密码学随机
		idx := 0
		for i, t := range remaining {
			w := weightOf(t)
			if pick < w {
				idx = i
				break
			}
			pick -= w
		}
		ordered = append(ordered, remaining[idx])
		remaining = append(remaining[:idx], remaining[idx+1:]...)
	}
	return appendFallbacks(ctx.Route, ordered), nil
}

func weightOf(t model.Target) int {
	if t.Weight <= 0 {
		return 1
	}
	return t.Weight
}