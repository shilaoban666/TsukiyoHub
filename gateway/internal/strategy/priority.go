package strategy

import (
	"sort"

	"tsukiyohub/gateway/internal/model"
)

// priorityStrategy 按 Target.Priority 升序（相同则保持配置顺序），失败逐个降级。
//
// 这是最常用、也最好预测的策略：想「主用 A，A 挂了用 B」就配优先级。
type priorityStrategy struct{}

func init() {
	register(NamePriority, func(map[string]any) Strategy { return priorityStrategy{} })
}

// Name 策略名。
func (priorityStrategy) Name() string { return NamePriority }

// Order 按 Priority 升序稳定排序，并追加 fallbacks。
func (priorityStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	ordered := dedupe(copyTargets(ctx.Route))
	if len(ordered) == 0 {
		ordered = appendFallbacks(ctx.Route, ordered)
		if len(ordered) == 0 {
			return nil, errNoCandidates
		}
		return ordered, nil
	}
	sort.SliceStable(ordered, func(i, j int) bool {
		return ordered[i].Priority < ordered[j].Priority
	})
	return appendFallbacks(ctx.Route, ordered), nil
}