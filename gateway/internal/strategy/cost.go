package strategy

import (
	"tsukiyohub/gateway/internal/model"
)

// costStrategy 按模型价格（input_price + output_price）升序：便宜的先用。
//
// 价格来自 model.ModelDef，由 proxy 通过 WithConfig 合并进 options 的
// OptionPrices 键；策略本身不 import 配置包，保持可插拔与可测试性。
type costStrategy struct {
	options map[string]any
}

func init() {
	register(NameCost, func(options map[string]any) Strategy { return &costStrategy{options: options} })
}

// Name 策略名。
func (s *costStrategy) Name() string { return NameCost }

// Order 按价格升序排序，并追加 fallbacks。
func (s *costStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	ordered := dedupe(copyTargets(ctx.Route))
	if len(ordered) == 0 {
		return appendFallbacks(ctx.Route, ordered), nil
	}
	ordered = orderCost(ordered, priceTable(s.options))
	return appendFallbacks(ctx.Route, ordered), nil
}