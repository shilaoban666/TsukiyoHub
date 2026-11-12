package middleware

import (
	"context"
	"crypto/subtle"
	"strings"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// APIKeyGuard 客户端鉴权：从 Authorization 头解析 API Key，在 Config.Clients 里匹配。
//
// 语义：
//   - 支持 `Authorization: Bearer sk-xxx`，也兼容 `X-Api-Key: sk-xxx`；
//   - 匹配成功把完整 ClientKey 写进 in.Client（后续限流/预算 Guard 直接读）；
//   - 找不到返回 401；
//   - **Clients 为空时不鉴权**（开发模式），Client.Name 记为 "anonymous"——
//     否则本地起个网关还要先配 key，体验太差。
type APIKeyGuard struct {
	metrics *metrics.Registry
}

// NewAPIKeyGuard 创建鉴权 Guard。
func NewAPIKeyGuard() Guard {
	return &APIKeyGuard{}
}

// Name 返回 Guard 名。
func (g *APIKeyGuard) Name() string { return GuardNameAPIKey }

// SetMetrics 绑定指标注册表（鉴权失败不进 Prometheus，仅保留接口一致性）。
func (g *APIKeyGuard) SetMetrics(reg *metrics.Registry) {
	g.metrics = reg
}

// Check 执行鉴权。
func (g *APIKeyGuard) Check(_ context.Context, in *GuardInput) error {
	if in == nil {
		return Reject(401, GuardNameAPIKey, "缺少请求上下文")
	}
	if in.Config == nil || len(in.Config.Clients) == 0 {
		in.Client = model.ClientKey{Name: "anonymous", Enabled: true}
		return nil
	}

	token := bearerToken(headerValue(in, "Authorization"))
	if token == "" {
		token = strings.TrimSpace(headerValue(in, "X-Api-Key"))
	}
	if token == "" {
		return Reject(401, GuardNameAPIKey, "缺少 API Key（Authorization: Bearer <key>）")
	}

	// 兼容性：若一个客户端都没显式启用（老配置常忘写 enabled:true），视为全部启用。
	anyEnabled := false
	for _, c := range in.Config.Clients {
		if c.Enabled {
			anyEnabled = true
			break
		}
	}
	matched := false
	for _, c := range in.Config.Clients {
		if subtle.ConstantTimeCompare([]byte(c.Key), []byte(token)) != 1 {
			continue
		}
		matched = true
		if c.Enabled || !anyEnabled {
			in.Client = c
			return nil
		}
	}
	if matched {
		return Reject(401, GuardNameAPIKey, "该 API Key 已被禁用")
	}
	return Reject(401, GuardNameAPIKey, "无效的 API Key")
}

// headerValue 安全读取请求头（in 或 Request 为 nil 时返回空串）。
func headerValue(in *GuardInput, name string) string {
	if in == nil || in.Request == nil {
		return ""
	}
	return in.Request.Header.Get(name)
}

// bearerToken 解析 `Bearer xxx` / `bearer xxx`，不是该格式时原样返回。
func bearerToken(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) >= 7 && strings.EqualFold(value[:7], "bearer ") {
		return strings.TrimSpace(value[7:])
	}
	if strings.EqualFold(value, "bearer") {
		return ""
	}
	return value
}