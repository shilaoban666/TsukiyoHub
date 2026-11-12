// Package admin 提供网关的管理接口（/admin/api/... 与 /metrics）。
//
// 约定：
//   - 全部返回 JSON；失败返回 {"error":"..."} + 合适状态码；
//   - 鉴权：AdminToken 非空时要求 `Authorization: Bearer <token>`；
//     AdminToken 为空时只允许本机（127.0.0.1 / ::1）访问；
//   - 配置类接口返回脱敏视图（config.Redacted），写配置时用 config.MergeSecrets
//     把「****」占位还原成真实密钥，避免前端把密钥改坏；
//   - 告警接口只依赖 model.AlertEngine 接口；未启用（nil）时返回 {"enabled":false}。
package admin

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"net"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
	"tsukiyohub/gateway/internal/tracing"
)

// healthCacheAge 管理接口复用探活结果的保鲜期。
const healthCacheAge = 15 * time.Second

// ConfigSource 配置来源（与 proxy 共用同一个实现）。
type ConfigSource interface {
	Current() *model.Config
}

// Options 管理接口装配参数（由 Lead 的 main.go 填充）。
type Options struct {
	// ConfigPath 配置文件路径（重新加载 / 展示用）
	ConfigPath string
	// Config 当前配置来源（与 proxy 共用）
	Config ConfigSource
	// Reload 热更新回调：校验 + 落盘 + 重建（由 Lead 在 cmd 里实现）
	Reload func(next *model.Config) error
	// Providers 供应商注册表
	Providers *provider.Registry
	// Breakers 熔断管理器
	Breakers *middleware.Breakers
	// Aggregator 窗口统计
	Aggregator *metrics.Aggregator
	// Tracer 请求追踪
	Tracer *tracing.Ring
	// Alerts 告警引擎（可为 nil）
	Alerts model.AlertEngine
	// Metrics Prometheus 注册表
	Metrics *metrics.Registry
	// Version 网关版本
	Version string
	// StartedAt 启动时间
	StartedAt time.Time
	// Guards 可选的中间件链：用于查询预算/限流实时用量（不填则预算接口仍可查默认账本）
	Guards *middleware.Chain
}

// api 管理接口实现。
type api struct {
	opts Options

	healthMu sync.Mutex
}

// New 创建管理接口 handler。只需挂在一个 HTTP 服务器上（Lead 的 cmd 负责监听）。
func New(opts Options) http.Handler {
	if opts.StartedAt.IsZero() {
		opts.StartedAt = time.Now()
	}
	a := &api{opts: opts}

	// 装配期幂等注入指标（即使 main 忘了调，管理接口也会把 breaker_state 写进 /metrics）
	if opts.Breakers != nil {
		opts.Breakers.SetMetrics(opts.Metrics, opts.Aggregator)
	}
	if opts.Providers != nil && opts.Metrics != nil {
		opts.Providers.SetMetrics(opts.Metrics)
	}

	mux := http.NewServeMux()
	// 只读
	mux.HandleFunc("GET /admin/api/overview", a.auth(a.handleOverview))
	mux.HandleFunc("GET /admin/api/config", a.auth(a.handleGetConfig))
	mux.HandleFunc("GET /admin/api/providers", a.auth(a.handleProviders))
	mux.HandleFunc("GET /admin/api/routes", a.auth(a.handleGetRoutes))
	mux.HandleFunc("GET /admin/api/traces", a.auth(a.handleTraces))
	mux.HandleFunc("GET /admin/api/traces/{id}", a.auth(a.handleTraceDetail))
	mux.HandleFunc("GET /admin/api/stats", a.auth(a.handleStats))
	mux.HandleFunc("GET /admin/api/budget", a.auth(a.handleBudget))
	mux.HandleFunc("GET /metrics", a.handleMetrics)
	// 写操作
	mux.HandleFunc("PUT /admin/api/config", a.auth(a.handlePutConfig))
	mux.HandleFunc("POST /admin/api/config/reload", a.auth(a.handleReloadConfig))
	mux.HandleFunc("POST /admin/api/providers/{name}/probe", a.auth(a.handleProbeProvider))
	mux.HandleFunc("PUT /admin/api/routes", a.auth(a.handlePutRoutes))
	mux.HandleFunc("POST /admin/api/budget/reset", a.auth(a.handleResetBudget))
	// 告警
	mux.HandleFunc("GET /admin/api/alerts/rules", a.auth(a.handleAlertRules))
	mux.HandleFunc("PUT /admin/api/alerts/rules", a.auth(a.handleReplaceAlertRules))
	mux.HandleFunc("GET /admin/api/alerts/events", a.auth(a.handleAlertEvents))
	mux.HandleFunc("POST /admin/api/alerts/{id}/ack", a.auth(a.handleAlertAck))
	mux.HandleFunc("GET /admin/api/alerts/overview", a.auth(a.handleAlertOverview))
	// 兜底
	mux.HandleFunc("/admin/", a.auth(a.handleNotFound))
	mux.HandleFunc("/", a.handleNotFound)

	// CORS 在外层：web 前端可能跑在不同源（Vite dev server），管理接口需要允许跨域；
	// OPTIONS 预检不带 Authorization，必须在鉴权之前返回。
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/admin/") || r.URL.Path == "/metrics" {
			setCORS(w, r)
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
		}
		mux.ServeHTTP(w, r)
	})
}

// ---------------------------------------------------------------------------
// 鉴权
// ---------------------------------------------------------------------------

// auth 包装管理接口鉴权。
func (a *api) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cfg := a.config()
		adminToken := ""
		if cfg != nil {
			adminToken = cfg.AdminToken
		}
		if adminToken != "" {
			provided := bearerToken(r.Header.Get("Authorization"))
			if provided == "" {
				provided = r.Header.Get("X-Admin-Token")
			}
			if subtle.ConstantTimeCompare([]byte(provided), []byte(adminToken)) != 1 {
				writeErr(w, http.StatusUnauthorized, "管理接口鉴权失败（Authorization: Bearer <admin_token>）")
				return
			}
			next(w, r)
			return
		}
		// 未配置 admin_token：只允许本机访问（避免管理接口无意暴露到公网）。
		if !isLoopbackAddr(r.RemoteAddr) {
			writeErr(w, http.StatusForbidden, "管理接口未配置 admin_token，仅允许本机访问")
			return
		}
		next(w, r)
	}
}

// bearerToken 解析 `Bearer xxx`；不是该格式时原样返回。
func bearerToken(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) >= 7 && strings.EqualFold(value[:7], "bearer ") {
		return strings.TrimSpace(value[7:])
	}
	return value
}

// isLoopbackAddr 判断 RemoteAddr 是否为本机（127.0.0.1 / ::1 / localhost）。
func isLoopbackAddr(remoteAddr string) bool {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		host = remoteAddr
	}
	host = strings.Trim(host, "[]")
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback()
}

// ---------------------------------------------------------------------------
// 装配辅助
// ---------------------------------------------------------------------------

// config 返回当前配置。
func (a *api) config() *model.Config {
	if a.opts.Config == nil {
		return nil
	}
	return a.opts.Config.Current()
}

// alertEngine 返回告警引擎；nil 与「typed nil」都视为未启用。
//
// 注意：把 (*alert.Engine)(nil) 赋给接口会得到非 nil 接口，必须用 reflect 判断。
func (a *api) alertEngine() (model.AlertEngine, bool) {
	engine := a.opts.Alerts
	if engine == nil {
		return nil, false
	}
	value := reflect.ValueOf(engine)
	switch value.Kind() {
	case reflect.Ptr, reflect.Map, reflect.Slice, reflect.Func, reflect.Chan, reflect.Interface:
		if value.IsNil() {
			return nil, false
		}
	}
	return engine, true
}

// healthList 返回供应商健康列表（15 秒缓存，过期才真正探活）。
func (a *api) healthList(ctx context.Context) []model.ProviderHealth {
	if a.opts.Providers == nil {
		return nil
	}
	if cached, ok := a.opts.Providers.CachedHealth(healthCacheAge); ok {
		return cached
	}
	a.healthMu.Lock()
	defer a.healthMu.Unlock()
	if cached, ok := a.opts.Providers.CachedHealth(healthCacheAge); ok {
		return cached
	}
	list := a.opts.Providers.ProbeAll(ctx)
	if a.opts.Aggregator != nil {
		var down int64
		for _, health := range list {
			if !health.Healthy {
				down++
			}
		}
		a.opts.Aggregator.SetProvidersDown(down)
	}
	return list
}

// ---------------------------------------------------------------------------
// HTTP / JSON 辅助
// ---------------------------------------------------------------------------

// writeJSON 写 JSON 响应。
func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// writeErr 写统一错误响应 {"error":"..."}。
func writeErr(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]any{"error": message})
}

// decodeJSON 解析请求体；失败时已写 400 响应并返回 false。
func decodeJSON(w http.ResponseWriter, r *http.Request, target any) bool {
	defer r.Body.Close()
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	if err := decoder.Decode(target); err != nil {
		writeErr(w, http.StatusBadRequest, "请求体不是合法 JSON："+err.Error())
		return false
	}
	return true
}

// queryInt 读取整型查询参数。
func queryInt(r *http.Request, key string, def, min, max int) int {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return def
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	if value < min {
		value = min
	}
	if max > 0 && value > max {
		value = max
	}
	return value
}

// queryBool 读取 *bool 查询参数（空 = nil）。
func queryBool(r *http.Request, key string) *bool {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return nil
	}
	var value bool
	switch strings.ToLower(raw) {
	case "1", "true", "yes", "ok":
		value = true
	case "0", "false", "no", "fail":
		value = false
	default:
		return nil
	}
	return &value
}

// parseSince 解析 since 参数：支持 RFC3339、Unix 秒、以及「5m / 1h」这种时长。
func parseSince(raw string) time.Time {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return time.Time{}
	}
	if ts, err := time.Parse(time.RFC3339, raw); err == nil {
		return ts
	}
	if seconds, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return time.Unix(seconds, 0)
	}
	if d, err := time.ParseDuration(raw); err == nil {
		return time.Now().Add(-d)
	}
	return time.Time{}
}

// setCORS 设置管理接口的 CORS 头（反射 Origin，兼容带凭证的请求）。
func setCORS(w http.ResponseWriter, r *http.Request) {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return
	}
	header := w.Header()
	header.Set("Access-Control-Allow-Origin", origin)
	header.Set("Vary", "Origin")
	header.Set("Access-Control-Allow-Credentials", "true")
	header.Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
	header.Set("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Admin-Token, X-Api-Key")
	header.Set("Access-Control-Max-Age", "600")
}

// handleNotFound 未知路径统一 404 JSON。
func (a *api) handleNotFound(w http.ResponseWriter, r *http.Request) {
	writeErr(w, http.StatusNotFound, "未知的管理接口："+r.URL.Path)
}

// handleMetrics 输出 Prometheus 文本格式。
func (a *api) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	if a.opts.Metrics == nil {
		writeErr(w, http.StatusServiceUnavailable, "指标注册表未初始化")
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	a.opts.Metrics.WritePrometheus(w)
}