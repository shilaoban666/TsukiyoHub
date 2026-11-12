package strategy

import (
	"math/rand"
	"sort"
	"strconv"
	"strings"

	"tsukiyohub/gateway/internal/model"
)

// capabilityStrategy 能力路由：先按 Route.RequiredCapabilities 过滤，
// 再按 least_latency 排序。
//
// 支持的能力写法（大小写不敏感）：
//
//	tools           模型必须支持 function calling
//	vision          模型必须支持图片输入
//	context:32000   模型上下文长度必须 >= 32000
//
// 能力数据来自 model.ModelDef，由 proxy 通过 WithConfig 合并进 options 的
// OptionCapabilities 键。无法识别的能力项按「满足」处理（避免配置笔误让流量全灭）。
type capabilityStrategy struct {
	options map[string]any
}

func init() {
	register(NameCapability, func(options map[string]any) Strategy { return &capabilityStrategy{options: options} })
}

// Name 策略名。
func (s *capabilityStrategy) Name() string { return NameCapability }

// Order 过滤 + 按延迟排序，并追加 fallbacks。
func (s *capabilityStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	targets := dedupe(copyTargets(ctx.Route))
	if len(targets) == 0 {
		return appendFallbacks(ctx.Route, targets), nil
	}

	caps := capabilityTable(s.options)
	if caps != nil && len(ctx.Route.RequiredCapabilities) > 0 {
		filtered := make([]model.Target, 0, len(targets))
		for _, t := range targets {
			if supportsAll(caps, t.Model, ctx.Route.RequiredCapabilities) {
				filtered = append(filtered, t)
			}
		}
		// 全部被过滤掉时不做「硬失败」：退回原始候选，让上游自己报错，
		// 否则一个写错的能力要求会让整条路由 100% 不可用。
		if len(filtered) > 0 {
			targets = filtered
		}
	}

	sort.SliceStable(targets, func(i, j int) bool {
		return latencyOf(ctx.Stats, targets[i].Model) < latencyOf(ctx.Stats, targets[j].Model)
	})
	return appendFallbacks(ctx.Route, targets), nil
}

// supportsAll 判断模型是否满足全部能力要求。
func supportsAll(caps map[string]Capability, modelID string, required []string) bool {
	capability, ok := caps[modelID]
	if !ok {
		// 模型没能力数据时保守放行：没有数据的模型也常是刚加进来的新模型。
		return true
	}
	for _, req := range required {
		if !supports(capability, req) {
			return false
		}
	}
	return true
}

// supports 判断单个能力项是否满足。
func supports(capability Capability, requirement string) bool {
	req := strings.ToLower(strings.TrimSpace(requirement))
	if req == "" {
		return true
	}
	if strings.HasPrefix(req, "context:") {
		want, err := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(req, "context:")))
		if err != nil {
			return true // 写法不对，按「无法识别」放行
		}
		return capability.Context >= want
	}
	switch req {
	case "tools", "function_calling", "functions":
		return capability.Tools
	case "vision", "image", "multimodal":
		return capability.Vision
	}
	return true
}

// randUint64 sticky 策略无粘性键时用的随机起点。
func randUint64() uint64 {
	return uint64(rand.Int63()) //nolint:gosec // 仅用于打散流量
}