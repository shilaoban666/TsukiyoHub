package strategy

import (
	"sync"
	"sync/atomic"

	"tsukiyohub/gateway/internal/model"
)

// roundRobinStrategy 按目标顺序轮转（进程内计数）。
//
// 计数器是**包级**的，按路由别名区分：因为 proxy 每次请求都会 New 一个新策略实例，
// 如果计数放在实例上，轮转就永远停在第一个目标上。
var rrCounters sync.Map // alias(string) -> *atomic.Uint64

// rrFallbackKey 路由别名为空时使用的计数器键。
const rrFallbackKey = "__default__"

func rrCounter(alias string) *atomic.Uint64 {
	if alias == "" {
		alias = rrFallbackKey
	}
	if v, ok := rrCounters.Load(alias); ok {
		return v.(*atomic.Uint64)
	}
	actual, _ := rrCounters.LoadOrStore(alias, &atomic.Uint64{})
	return actual.(*atomic.Uint64)
}

type roundRobinStrategy struct{}

func init() {
	register(NameRoundRobin, func(map[string]any) Strategy { return roundRobinStrategy{} })
}

// Name 策略名。
func (roundRobinStrategy) Name() string { return NameRoundRobin }

// Order 从轮转游标处开始环形展开候选，并追加 fallbacks。
func (roundRobinStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	targets := dedupe(copyTargets(ctx.Route))
	if len(targets) == 0 {
		return appendFallbacks(ctx.Route, targets), nil
	}
	// Add(1)-1：第一次请求从 0 开始，方便测试与直觉。
	n := rrCounter(ctx.Route.Alias).Add(1) - 1
	start := int(n % uint64(len(targets)))
	ordered := make([]model.Target, 0, len(targets))
	for i := 0; i < len(targets); i++ {
		ordered = append(ordered, targets[(start+i)%len(targets)])
	}
	return appendFallbacks(ctx.Route, ordered), nil
}