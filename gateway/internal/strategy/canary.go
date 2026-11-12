package strategy

import (
	"math/rand"

	"tsukiyohub/gateway/internal/model"
)

// canaryStrategy 灰度发布：按概率把流量给「第一组」（canary），其余给第二组。
//
// Options:
//
//	percent: 命中第一组的概率（0~100，默认 10）
//
// 排序结果有两种：命中 canary 时第一个目标打头，其余照配置顺序；
// 未命中时其余目标打头、第一组垫底。这样即使 canary 失败，proxy 也会自动
// 切换到稳定组（无需额外 fallback 配置）。
type canaryStrategy struct {
	options map[string]any
}

// 默认灰度比例（百分比）。
const defaultCanaryPercent = 10

func init() {
	register(NameCanary, func(options map[string]any) Strategy { return &canaryStrategy{options: options} })
}

// Name 策略名。
func (s *canaryStrategy) Name() string { return NameCanary }

// Order 按灰度概率决定第一组是否打头，并追加 fallbacks。
func (s *canaryStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	targets := dedupe(copyTargets(ctx.Route))
	if len(targets) == 0 {
		return appendFallbacks(ctx.Route, targets), nil
	}
	if len(targets) == 1 {
		return appendFallbacks(ctx.Route, targets), nil
	}

	percent := optionFloat(s.options, "percent", defaultCanaryPercent)
	if percent < 0 {
		percent = 0
	}
	if percent > 100 {
		percent = 100
	}
	hitCanary := percent >= 100 || (percent > 0 && rand.Float64()*100 < percent) //nolint:gosec // 灰度不需要密码学随机

	ordered := make([]model.Target, 0, len(targets))
	if hitCanary {
		ordered = append(ordered, targets[0])
		ordered = append(ordered, targets[1:]...)
	} else {
		ordered = append(ordered, targets[1:]...)
		ordered = append(ordered, targets[0])
	}
	return appendFallbacks(ctx.Route, ordered), nil
}