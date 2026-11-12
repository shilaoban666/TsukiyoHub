package strategy

import (
	"hash/fnv"

	"tsukiyohub/gateway/internal/model"
)

// stickyStrategy 会话粘性：同一个用户（或同一段会话）稳定命中同一个目标。
//
// 键的优先级：Request.User > 会话首条消息内容 > Embedding.Input > 客户端 Key。
// 用 FNV-1a 哈希取模选中一个目标，再把它作为排序起点环形展开，
// 这样既稳定，又保留了「粘性目标失败时自动切到下一个」的能力。
type stickyStrategy struct{}

func init() {
	register(NameSticky, func(map[string]any) Strategy { return stickyStrategy{} })
}

// Name 策略名。
func (s stickyStrategy) Name() string { return NameSticky }

// Order 按粘性键稳定选路，并追加 fallbacks。
func (s stickyStrategy) Order(ctx *PickContext) ([]model.Target, error) {
	if ctx == nil {
		return nil, errNoCandidates
	}
	targets := dedupe(copyTargets(ctx.Route))
	if len(targets) == 0 {
		return appendFallbacks(ctx.Route, targets), nil
	}
	key := stickyKey(ctx)
	var h uint64
	if key == "" {
		// 没有可用的粘性键时退化为随机起点，避免所有匿名请求压到同一个目标。
		h = randUint64()
	} else {
		fnvHash := fnv.New64a()
		_, _ = fnvHash.Write([]byte(key))
		h = fnvHash.Sum64()
	}
	start := int(h % uint64(len(targets)))
	ordered := make([]model.Target, 0, len(targets))
	for i := 0; i < len(targets); i++ {
		ordered = append(ordered, targets[(start+i)%len(targets)])
	}
	return appendFallbacks(ctx.Route, ordered), nil
}

// stickyKey 计算粘性键；空串表示没有可用的稳定信息。
func stickyKey(ctx *PickContext) string {
	if ctx.Request != nil {
		if ctx.Request.User != "" {
			return "user:" + ctx.Request.User
		}
		for _, m := range ctx.Request.Messages {
			if len(m.Content) == 0 {
				continue
			}
			content := string(m.Content)
			// 首条消息可能是超长 system prompt，截断避免哈希大对象。
			if len(content) > 256 {
				content = content[:256]
			}
			return "msg:" + content
		}
	}
	if ctx.Embedding != nil {
		if ctx.Embedding.User != "" {
			return "user:" + ctx.Embedding.User
		}
		if len(ctx.Embedding.Input) > 0 {
			input := string(ctx.Embedding.Input)
			if len(input) > 256 {
				input = input[:256]
			}
			return "emb:" + input
		}
	}
	if ctx.Client.Key != "" {
		return "client:" + ctx.Client.Key
	}
	return ""
}