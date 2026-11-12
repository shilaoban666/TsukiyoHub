// Package middleware 实现请求进入代理前的横切检查（鉴权 / 限流 / 预算）与熔断器。
//
// 三个 Guard 的顺序就是安全顺序：鉴权 → 限流 → 预算。
// Guard 只做判断并把拒绝原因（*RejectError）返回给 proxy，由 proxy 统一写响应、
// 统一记录 RequestRecord——这样限流/预算被拒也能进指标、进告警统计。
package middleware

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// Guard 一次请求进入代理前的检查；返回错误表示拒绝。
//
// 返回 *RejectError 时 proxy 会据此写出对应的 JSON 错误响应并记录请求；
// 返回其它错误按 500 处理。
type Guard interface {
	Name() string
	Check(ctx context.Context, in *GuardInput) error
}

// GuardInput 一次 Guard 检查的输入。
//
// Client 是**值类型**字段：APIKeyGuard 会把匹配到的客户端写进来，
// 后续 Guard 和 proxy 在同一份 *GuardInput 上读到它。
type GuardInput struct {
	Request *http.Request
	Config  *model.Config
	Client  model.ClientKey
}

// RejectError 表示请求被某个 Guard 拒绝（已决定状态码与错误消息）。
type RejectError struct {
	// Status HTTP 状态码（401 / 403 / 429 / 402 ...）
	Status int
	// Message 给客户端看的错误消息
	Message string
	// Guard 拒绝它的 Guard 名（排障用）
	Guard string
}

// Error 实现 error 接口。
func (e *RejectError) Error() string {
	if e == nil {
		return ""
	}
	if e.Guard != "" {
		return fmt.Sprintf("%s: %s", e.Guard, e.Message)
	}
	return e.Message
}

// Reject 构造一个拒绝错误。
func Reject(status int, guard, message string) *RejectError {
	return &RejectError{Status: status, Message: message, Guard: guard}
}

// Rejectf 构造一个带格式化的拒绝错误。
func Rejectf(status int, guard, format string, args ...any) *RejectError {
	return &RejectError{Status: status, Message: fmt.Sprintf(format, args...), Guard: guard}
}

// AsReject 从错误链里取出 *RejectError。
func AsReject(err error) (*RejectError, bool) {
	var rej *RejectError
	if errors.As(err, &rej) {
		return rej, true
	}
	return nil, false
}

// Chain 依次执行 Guard（顺序即安全顺序：鉴权 → 限流 → 预算）。
type Chain struct {
	guards []Guard
}

// NewChain 创建 Guard 链；nil 链等价于「全部放行」，方便测试与关闭中间件。
func NewChain(guards ...Guard) *Chain {
	return &Chain{guards: guards}
}

// Run 依次执行 Guard，遇到第一个错误立即返回。
func (c *Chain) Run(ctx context.Context, in *GuardInput) error {
	if c == nil {
		return nil
	}
	for _, g := range c.guards {
		if g == nil {
			continue
		}
		if err := g.Check(ctx, in); err != nil {
			return err
		}
	}
	return nil
}

// Guards 返回链上全部 Guard（proxy 用它做类型断言，例如回报 token/花费）。
func (c *Chain) Guards() []Guard {
	if c == nil {
		return nil
	}
	return append([]Guard(nil), c.guards...)
}

// Find 按名字查找 Guard；找不到返回 nil。
func (c *Chain) Find(name string) Guard {
	if c == nil {
		return nil
	}
	for _, g := range c.guards {
		if g != nil && g.Name() == name {
			return g
		}
	}
	return nil
}

// MetricsBinder 由实现了该接口的 Guard 接收指标注册表。
//
// 之所以用「可选接口 + 装配期注入」，是为了保持 NewXxxGuard() 的无参签名
// （Lead 的 main.go 直接调用），同时让被拒次数能进 Prometheus。
type MetricsBinder interface {
	SetMetrics(reg *metrics.Registry)
}

// BindMetrics 把指标注册表注入链上所有支持它的 Guard（幂等，可重复调用）。
func (c *Chain) BindMetrics(reg *metrics.Registry) {
	if c == nil || reg == nil {
		return
	}
	for _, g := range c.guards {
		if binder, ok := g.(MetricsBinder); ok {
			binder.SetMetrics(reg)
		}
	}
}

// Guard 名常量（proxy 用它做类型断言 / 记录）。
const (
	GuardNameAPIKey    = "apikey"
	GuardNameRateLimit = "ratelimit"
	GuardNameBudget    = "budget"
)