// Package proxy 实现 OpenAI 兼容的网关入口：鉴权、选路、上游透传（含 SSE 流式）、
// 用量/成本统计与请求记录。
//
// 设计约束：
//  1. **透传优先**：客户端请求体只用 json.RawMessage 的 map 重新组装，只改写 model 字段，
//     其余参数（temperature/top_p/response_format/自定义字段）原样发给上游；
//  2. **失败切换**：按策略顺序逐个尝试，跳过已失败（Excluded）与熔断中的模型，
//     全部失败返回 502 并带上 attempts 明细；
//  3. **一次请求一条记录**：无论成功、被限流、被预算拒还是全部失败，都写 RequestRecord
//     （指标 + 聚合器 + 追踪），告警才能统计到 429/402。
package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
	"tsukiyohub/gateway/internal/strategy"
	"tsukiyohub/gateway/internal/tracing"
)

// maxRequestBody 客户端请求体上限（32MB，多模态图片也够用）。
const maxRequestBody = 32 << 20

// readyProbeMaxAge /readyz 复用探活结果的保鲜期。
const readyProbeMaxAge = 15 * time.Second

// ConfigSource 配置来源（Lead 在 cmd 里实现 atomic.Pointer[model.Config]）。
type ConfigSource interface {
	Current() *model.Config
}

// Options 代理装配参数（由 Lead 的 main.go 填充）。
type Options struct {
	Config     ConfigSource
	Providers  *provider.Registry
	Breakers   *middleware.Breakers
	Guards     *middleware.Chain
	Metrics    *metrics.Registry
	Aggregator *metrics.Aggregator
	Tracer     *tracing.Ring
	// Alerts 告警引擎（可为 nil；代理本身不触发告警，由引擎自行评估聚合器）
	Alerts  model.AlertEngine
	Version string
}

// Handler 网关主入口，实现 http.Handler。
type Handler struct {
	opts      Options
	cfg       atomic.Pointer[model.Config]
	startedAt time.Time

	// inflight 本地在途计数（与 Aggregator 分开，避免并发上限判断被聚合器抖动影响）
	inflight int64

	latency *latencyTracker
	stats   *statsView
}

// New 创建代理 handler，并完成指标/统计的装配（幂等，可重复调用）。
func New(opts Options) *Handler {
	h := &Handler{opts: opts, startedAt: time.Now(), latency: newLatencyTracker()}
	if opts.Config != nil {
		h.cfg.Store(opts.Config.Current())
	}
	h.stats = &statsView{latency: h.latency, breakers: opts.Breakers}
	if opts.Metrics != nil {
		registerMetricHelp(opts.Metrics)
		if opts.Providers != nil {
			opts.Providers.SetMetrics(opts.Metrics)
		}
		if opts.Guards != nil {
			opts.Guards.BindMetrics(opts.Metrics)
		}
	}
	if opts.Breakers != nil {
		opts.Breakers.SetMetrics(opts.Metrics, opts.Aggregator)
	}
	if cfg := h.current(); cfg != nil {
		h.preloadBreakers(cfg)
	}
	return h
}

// SetConfig 热更新配置（Lead 在 Reload 成功后调用）。
//
// 代理持有的配置指针是原子的：SetConfig 之后，正在处理的请求继续用旧配置，
// 新请求用新配置，无需重启也不需要加锁。
func (h *Handler) SetConfig(cfg *model.Config) {
	if cfg == nil {
		return
	}
	h.cfg.Store(cfg)
	h.preloadBreakers(cfg)
}

// Ready 判断网关是否可以接流量：至少一个已启用供应商探活健康。
//
// 返回 (true, "") 表示就绪；否则返回 (false, 原因) 供 /readyz 输出。
func (h *Handler) Ready() (bool, string) {
	if h.opts.Providers == nil {
		return false, "供应商注册表未初始化"
	}
	if len(h.opts.Providers.Names()) == 0 {
		return false, "没有配置供应商"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	list := h.probeAll(ctx)
	healthy := 0
	firstErr := ""
	for _, health := range list {
		if health.Healthy {
			healthy++
			continue
		}
		if firstErr == "" {
			firstErr = health.Name + ": " + health.Error
		}
	}
	if healthy > 0 {
		return true, ""
	}
	if firstErr == "" {
		firstErr = "全部供应商不可用"
	}
	return false, "没有健康的供应商（" + firstErr + "）"
}

// current 返回当前配置（优先本地原子指针，退化到 ConfigSource）。
func (h *Handler) current() *model.Config {
	if cfg := h.cfg.Load(); cfg != nil {
		return cfg
	}
	if h.opts.Config != nil {
		if cfg := h.opts.Config.Current(); cfg != nil {
			h.cfg.Store(cfg)
			return cfg
		}
	}
	return nil
}

// ServeHTTP 实现网关的全部对外路由。
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	switch path {
	case "/healthz":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			methodNotAllowed(w)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": h.opts.Version})
	case "/readyz":
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			methodNotAllowed(w)
			return
		}
		if ok, reason := h.Ready(); ok {
			writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
		} else {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "unavailable", "reason": reason})
		}
	case "/v1/models":
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		h.handleModels(w, r)
	case "/v1/chat/completions":
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		h.handleChat(w, r)
	case "/v1/embeddings":
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		h.handleEmbeddings(w, r)
	default:
		writeAPIError(w, http.StatusNotFound, "未知路径："+path, "not_found_error")
	}
}

// handleModels 返回路由别名列表（OpenAI /v1/models 格式）。
func (h *Handler) handleModels(w http.ResponseWriter, _ *http.Request) {
	cfg := h.current()
	if cfg == nil {
		writeAPIError(w, http.StatusServiceUnavailable, "网关配置尚未就绪", "gateway_error")
		return
	}
	type modelObject struct {
		ID      string `json:"id"`
		Object  string `json:"object"`
		Created int64  `json:"created"`
		OwnedBy string `json:"owned_by"`
	}
	routes := enabledRoutes(cfg)
	data := make([]modelObject, 0, len(routes)+len(cfg.Models))
	created := h.startedAt.Unix()
	for _, route := range routes {
		data = append(data, modelObject{ID: route.Alias, Object: "model", Created: created, OwnedBy: "tsukiyohub"})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"object": "list",
		"data":   data,
	})
}

// handleChat 处理 /v1/chat/completions。
func (h *Handler) handleChat(w http.ResponseWriter, r *http.Request) {
	cfg := h.current()
	if cfg == nil {
		writeAPIError(w, http.StatusServiceUnavailable, "网关配置尚未就绪", "gateway_error")
		return
	}
	body, err := readBody(r)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error(), "invalid_request_error")
		return
	}
	raw := map[string]json.RawMessage{}
	if err := json.Unmarshal(body, &raw); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求体不是合法 JSON："+err.Error(), "invalid_request_error")
		return
	}
	chat := &model.ChatRequest{Raw: raw}
	if err := json.Unmarshal(body, chat); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求体字段类型不合法："+err.Error(), "invalid_request_error")
		return
	}
	h.serve(w, r, cfg, &inferenceRequest{
		path:         "/chat/completions",
		modelID:      chat.Model,
		stream:       chat.Stream,
		raw:          raw,
		chat:         chat,
		requestBytes: len(body),
	})
}

// handleEmbeddings 处理 /v1/embeddings（非流式）。
func (h *Handler) handleEmbeddings(w http.ResponseWriter, r *http.Request) {
	cfg := h.current()
	if cfg == nil {
		writeAPIError(w, http.StatusServiceUnavailable, "网关配置尚未就绪", "gateway_error")
		return
	}
	body, err := readBody(r)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error(), "invalid_request_error")
		return
	}
	raw := map[string]json.RawMessage{}
	if err := json.Unmarshal(body, &raw); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求体不是合法 JSON："+err.Error(), "invalid_request_error")
		return
	}
	embedding := &model.EmbeddingRequest{}
	if err := json.Unmarshal(body, embedding); err != nil {
		writeAPIError(w, http.StatusBadRequest, "请求体字段类型不合法："+err.Error(), "invalid_request_error")
		return
	}
	h.serve(w, r, cfg, &inferenceRequest{
		path:         "/embeddings",
		modelID:      embedding.Model,
		raw:          raw,
		embedding:    embedding,
		requestBytes: len(body),
	})
}

// inferenceRequest 归一化后的推理请求（chat 与 embeddings 共用同一条转发链路）。
type inferenceRequest struct {
	// path 上游路径（/chat/completions 或 /embeddings）
	path string
	// modelID 客户端请求的 model 字段（路由别名或模型 ID）
	modelID string
	stream  bool
	// raw 客户端原始字段（透传用，只改写 model）
	raw map[string]json.RawMessage
	// chat / embedding 二选一
	chat         *model.ChatRequest
	embedding    *model.EmbeddingRequest
	requestBytes int
}

// serve 是网关的核心链路：鉴权 → 限流/预算 → 选路 → 逐候选尝试 → 转发 → 记录。
func (h *Handler) serve(w http.ResponseWriter, r *http.Request, cfg *model.Config, inf *inferenceRequest) {
	startedAt := time.Now()
	rec := model.RequestRecord{
		ID:           newRequestID(),
		At:           startedAt,
		Route:        inf.modelID,
		Stream:       inf.stream,
		ClientIP:     clientIP(r),
		RequestBytes: inf.requestBytes,
	}
	in := &middleware.GuardInput{Request: r, Config: cfg, Client: model.ClientKey{}}

	// 在途计数（并发上限 + inflight 指标 + 聚合器）
	inflight := h.enter()
	defer h.exit()

	// finish 收尾：写响应 + 落记录（每个请求只调用一次）
	finishReject := func(status int, message, errType string) {
		rec.Status = status
		rec.OK = false
		rec.Error = message
		rec.LatencyMS = time.Since(startedAt).Milliseconds()
		rec.ClientName = middleware.ClientLabel(in.Client)
		writeAPIError(w, status, message, errType)
		h.record(rec, cfg, in.Client)
	}

	if cfg.Concurrency > 0 && inflight > int64(cfg.Concurrency) {
		finishReject(http.StatusServiceUnavailable,
			fmt.Sprintf("网关并发已满（上限 %d）", cfg.Concurrency), "gateway_overloaded")
		return
	}
	if h.opts.Providers == nil {
		finishReject(http.StatusServiceUnavailable, "供应商注册表未初始化", "gateway_error")
		return
	}

	// 1) 鉴权 → 限流 → 预算
	if err := h.opts.Guards.Run(r.Context(), in); err != nil {
		if rej, ok := middleware.AsReject(err); ok {
			finishReject(rej.Status, rej.Message, errorTypeForStatus(rej.Status))
			return
		}
		finishReject(http.StatusInternalServerError, "网关中间件错误："+err.Error(), "gateway_error")
		return
	}
	rec.ClientName = middleware.ClientLabel(in.Client)

	// 2) 解析路由（别名 → 直接模型 ID → 默认路由）
	route, err := resolveRoute(cfg, inf.modelID)
	if err != nil {
		finishReject(http.StatusBadRequest, err.Error(), "invalid_request_error")
		return
	}
	rec.Route = route.Alias
	if !routeAllowed(in.Client, route.Alias) {
		finishReject(http.StatusForbidden,
			fmt.Sprintf("客户端无权访问路由 %s", route.Alias), "permission_error")
		return
	}

	// 3) 选路
	order, err := h.order(cfg, route, inf, in)
	if err != nil {
		finishReject(http.StatusBadRequest, err.Error(), "invalid_request_error")
		return
	}

	// 4) 逐候选尝试
	maxAttempts := cfg.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = 1
	}
	excluded := map[string]bool{}
	attempts := make([]model.Attempt, 0, maxAttempts)
	tried := 0
	var (
		resp       *http.Response
		endpoint   *provider.Endpoint
		lastErr    string
		lastStatus int
	)

	for _, target := range order {
		if tried >= maxAttempts {
			break
		}
		if excluded[target.Model] {
			continue
		}
		candidate, resolveErr := h.opts.Providers.Resolve(target.Model)
		if resolveErr != nil {
			excluded[target.Model] = true
			lastErr = resolveErr.Error()
			attempts = append(attempts, model.Attempt{Model: target.Model, Error: resolveErr.Error()})
			continue
		}
		if !h.breakerAllow(target.Model) {
			// 熔断中：跳过这个候选，不占用 attempts 预算，继续看下一个。
			excluded[target.Model] = true
			lastErr = fmt.Sprintf("模型 %s 熔断中，已跳过", target.Model)
			attempts = append(attempts, model.Attempt{
				Model: target.Model, Provider: candidate.Provider.Name, Error: "熔断中，跳过",
			})
			continue
		}

		upstreamBody, buildErr := buildUpstreamBody(inf.raw, candidate, inf.stream && candidate.Provider.StreamUsage)
		if buildErr != nil {
			h.breakerFailure(target.Model)
			excluded[target.Model] = true
			lastErr = buildErr.Error()
			attempts = append(attempts, model.Attempt{
				Model: target.Model, Provider: candidate.Provider.Name, Error: buildErr.Error(),
			})
			continue
		}

		tried++
		attemptStart := time.Now()
		candidateResp, doErr := h.opts.Providers.Do(r.Context(), candidate, inf.path, upstreamBody)
		if doErr != nil {
			h.breakerFailure(target.Model)
			excluded[target.Model] = true
			lastErr = doErr.Error()
			attempts = append(attempts, model.Attempt{
				Model: target.Model, Provider: candidate.Provider.Name,
				Status: 0, LatencyMS: msSince(attemptStart), Error: doErr.Error(),
			})
			continue
		}
		if candidateResp.StatusCode < 200 || candidateResp.StatusCode > 299 {
			snippet := provider.ErrorSnippet(candidateResp, 2048)
			_ = candidateResp.Body.Close()
			h.breakerFailure(target.Model)
			excluded[target.Model] = true
			lastStatus = candidateResp.StatusCode
			lastErr = fmt.Sprintf("上游返回 %d：%s", candidateResp.StatusCode, snippet)
			attempts = append(attempts, model.Attempt{
				Model: target.Model, Provider: candidate.Provider.Name,
				Status: candidateResp.StatusCode, LatencyMS: msSince(attemptStart),
				Error: truncate(snippet, 512),
			})
			continue
		}
		// 成功：固定这次选中的端点与响应
		endpoint, resp = candidate, candidateResp
		attempts = append(attempts, model.Attempt{
			Model: target.Model, Provider: candidate.Provider.Name,
			Status: candidateResp.StatusCode, LatencyMS: msSince(attemptStart),
		})
		rec.Model = candidate.Model
		rec.Provider = candidate.Provider.Name
		break
	}

	// 5) 全部失败 → 502 + attempts 明细
	if resp == nil || endpoint == nil {
		rec.Status = http.StatusBadGateway
		rec.OK = false
		rec.Error = lastErr
		rec.Attempts = tried
		rec.AttemptLog = attempts
		rec.LatencyMS = time.Since(startedAt).Milliseconds()
		writeUpstreamError(w, attempts, lastErr, lastStatus)
		h.record(rec, cfg, in.Client)
		return
	}
	defer resp.Body.Close()

	// 6) 转发响应
	if isStreamResponse(resp, inf.stream) {
		h.finishStream(w, resp, inf, endpoint, &rec, startedAt)
	} else {
		h.finishJSON(w, resp, inf, endpoint, &rec)
	}
	rec.LatencyMS = time.Since(startedAt).Milliseconds()
	rec.CostUSD = Cost(endpoint, rec.Usage)
	rec.Attempts = tried
	rec.AttemptLog = attempts
	if rec.OK {
		h.breakerSuccess(endpoint.Model)
	} else {
		h.breakerFailure(endpoint.Model)
	}
	h.record(rec, cfg, in.Client)
}

// breakerAllow / breakerSuccess / breakerFailure 是对 nil 熔断器的安全包装。
func (h *Handler) breakerAllow(modelID string) bool {
	if h.opts.Breakers == nil {
		return true
	}
	return h.opts.Breakers.Allow(modelID)
}

func (h *Handler) breakerSuccess(modelID string) {
	if h.opts.Breakers != nil {
		h.opts.Breakers.Success(modelID)
	}
}

func (h *Handler) breakerFailure(modelID string) {
	if h.opts.Breakers != nil {
		h.opts.Breakers.Failure(modelID)
	}
}

// finishStream 转发 SSE 响应并填充记录。
func (h *Handler) finishStream(w http.ResponseWriter, resp *http.Response, inf *inferenceRequest,
	endpoint *provider.Endpoint, rec *model.RequestRecord, startedAt time.Time) {

	result := h.forwardStream(w, resp, startedAt)
	rec.Status = http.StatusOK
	rec.OK = true
	rec.Stream = true
	rec.TTFTMS = result.TTFTMS
	rec.ResponseBytes = result.ResponseBytes
	if result.WriteErr != nil {
		// 客户端提前断开不算上游失败，只记录原因。
		rec.Error = "客户端提前断开：" + result.WriteErr.Error()
	}
	if result.ReadErr != nil {
		rec.OK = false
		rec.Error = "上游流中断：" + result.ReadErr.Error()
	}
	if result.StreamError != "" {
		rec.OK = false
		rec.Error = "上游流内错误：" + result.StreamError
	}
	if result.HasUsage {
		rec.Usage = result.Usage
	} else {
		// 上游没给 usage：按字符数估算（中文 1 字 ≈ 1 token）
		rec.Usage = EstimateChatUsage(inf.chat, result.CompletionText)
	}
	_ = endpoint
}

// finishJSON 转发非流式响应（body 原样回传）并填充记录。
func (h *Handler) finishJSON(w http.ResponseWriter, resp *http.Response, inf *inferenceRequest,
	endpoint *provider.Endpoint, rec *model.RequestRecord) {

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		rec.Status = http.StatusBadGateway
		rec.OK = false
		rec.Error = "读取上游响应失败：" + err.Error()
		writeUpstreamError(w, nil, rec.Error, resp.StatusCode)
		return
	}
	copyUpstreamHeaders(w, resp)
	w.WriteHeader(resp.StatusCode)
	if _, err := w.Write(body); err != nil {
		rec.Error = "写响应失败：" + err.Error()
	}
	rec.Status = resp.StatusCode
	rec.OK = resp.StatusCode < 400
	rec.ResponseBytes = len(body)
	if usage, ok := parseUsage(body); ok {
		rec.Usage = usage
	} else if inf.chat != nil {
		rec.Usage = EstimateChatUsage(inf.chat, completionText(body))
	} else {
		rec.Usage = EstimateEmbeddingUsage(inf.embedding)
	}
	_ = endpoint
}

// order 按路由策略给出候选顺序。
func (h *Handler) order(cfg *model.Config, route model.Route, inf *inferenceRequest, in *middleware.GuardInput) ([]model.Target, error) {
	options := strategy.WithConfig(cfg, route.Options)
	pick, err := strategy.New(route.Strategy, options)
	if err != nil {
		// 策略名写错不应该让整条路由不可用：回退到 priority。
		pick, err = strategy.New(strategy.NamePriority, options)
		if err != nil {
			return nil, err
		}
	}
	ordered, err := pick.Order(&strategy.PickContext{
		Request:   inf.chat,
		Embedding: inf.embedding,
		Route:     route,
		Attempt:   1,
		Excluded:  map[string]bool{},
		Stats:     h.stats,
		Client:    in.Client,
	})
	if err != nil {
		return nil, err
	}
	if len(ordered) == 0 {
		return nil, fmt.Errorf("路由 %s 没有可用候选模型", route.Alias)
	}
	return ordered, nil
}

// enter 在途 +1，返回当前在途数。
func (h *Handler) enter() int64 {
	n := atomic.AddInt64(&h.inflight, 1)
	if reg := h.opts.Metrics; reg != nil {
		reg.Set(MetricInflight, nil, float64(n))
	}
	if agg := h.opts.Aggregator; agg != nil {
		agg.AddInflight(1)
	}
	return n
}

// exit 在途 -1。
func (h *Handler) exit() {
	n := atomic.AddInt64(&h.inflight, -1)
	if n < 0 {
		n = 0
		atomic.StoreInt64(&h.inflight, 0)
	}
	if reg := h.opts.Metrics; reg != nil {
		reg.Set(MetricInflight, nil, float64(n))
	}
	if agg := h.opts.Aggregator; agg != nil {
		agg.AddInflight(-1)
	}
}

// preloadBreakers 把当前配置里的模型都登记进熔断器，保证 breaker_state 指标从启动就有序列。
func (h *Handler) preloadBreakers(cfg *model.Config) {
	if h.opts.Breakers == nil || cfg == nil {
		return
	}
	ids := make([]string, 0, len(cfg.Models))
	for _, m := range cfg.Models {
		if m.ID != "" {
			ids = append(ids, m.ID)
		}
	}
	h.opts.Breakers.Preload(ids)
}

// probeAll 探活全部供应商（带 15 秒缓存，避免 /readyz 与概览接口反复打上游）。
func (h *Handler) probeAll(ctx context.Context) []model.ProviderHealth {
	if h.opts.Providers == nil {
		return nil
	}
	if cached, ok := h.opts.Providers.CachedHealth(readyProbeMaxAge); ok {
		return cached
	}
	list := h.opts.Providers.ProbeAll(ctx)
	if agg := h.opts.Aggregator; agg != nil {
		var down int64
		for _, health := range list {
			if !health.Healthy {
				down++
			}
		}
		agg.SetProvidersDown(down)
	}
	return list
}

// registerMetricHelp 登记指标说明（出现在 /metrics 的 # HELP 行）。
func registerMetricHelp(reg *metrics.Registry) {
	reg.Help(MetricRequests, "网关请求总数（route/model/provider/status 维度）")
	reg.Help(MetricDuration, "网关请求总耗时（秒）")
	reg.Help(MetricTTFT, "流式首字延迟（秒）")
	reg.Help(MetricTokens, "token 消耗量（direction=prompt|completion）")
	reg.Help(MetricCost, "模型费用累计（美元）")
	reg.Help(MetricInflight, "当前在途请求数")
	reg.Help("tsukiyo_gateway_breaker_state", "熔断器状态（0=closed,1=half_open,2=open）")
	reg.Help("tsukiyo_gateway_provider_up", "供应商探活结果（1=健康,0=不可用）")
	reg.Help("tsukiyo_gateway_ratelimit_rejected_total", "因限流被拒的请求数")
	reg.Help("tsukiyo_gateway_budget_rejected_total", "因预算被拒的请求数")
}

// ---------------------------------------------------------------------------
// 路由 / 配置辅助
// ---------------------------------------------------------------------------

// enabledRoutes 返回启用的路由。
//
// 兼容性：老配置常忘写 route.enabled=true（JSON 零值是 false），
// 若一个都没启用则视为全部启用，避免「配了路由但网关说没有」。
func enabledRoutes(cfg *model.Config) []model.Route {
	if cfg == nil {
		return nil
	}
	anyEnabled := false
	for _, route := range cfg.Routes {
		if route.Enabled {
			anyEnabled = true
			break
		}
	}
	out := make([]model.Route, 0, len(cfg.Routes))
	for _, route := range cfg.Routes {
		if route.Enabled || !anyEnabled {
			out = append(out, route)
		}
	}
	return out
}

// resolveRoute 把客户端 model 字段解析成路由。
//
// 顺序：路由别名 → 直接模型 ID（合成单目标路由）→ default_route。
// 这样既支持「别名抽象」，也支持客户端直接点名某个模型（很多 SDK 写死了模型名）。
func resolveRoute(cfg *model.Config, alias string) (model.Route, error) {
	alias = strings.TrimSpace(alias)
	if alias == "" {
		return model.Route{}, errors.New("请求缺少 model 字段")
	}
	routes := enabledRoutes(cfg)
	for _, route := range routes {
		if route.Alias == alias {
			return route, nil
		}
	}
	for _, m := range cfg.Models {
		if m.ID == alias {
			return model.Route{
				Alias:    m.ID,
				Strategy: strategy.NamePriority,
				Targets:  []model.Target{{Model: m.ID, Weight: 1}},
				Enabled:  true,
			}, nil
		}
	}
	if cfg.DefaultRoute != "" {
		for _, route := range routes {
			if route.Alias == cfg.DefaultRoute {
				return route, nil
			}
		}
	}
	return model.Route{}, fmt.Errorf("未知的模型或路由别名：%q", alias)
}

// routeAllowed 判断客户端是否允许访问该路由（ClientKey.Routes 为空 = 全部允许）。
func routeAllowed(client model.ClientKey, alias string) bool {
	if len(client.Routes) == 0 {
		return true
	}
	for _, allowed := range client.Routes {
		if allowed == alias {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// JSON / HTTP 辅助
// ---------------------------------------------------------------------------

// readBody 读取客户端请求体（带上限）。
func readBody(r *http.Request) ([]byte, error) {
	if r.Body == nil {
		return nil, errors.New("请求体为空")
	}
	defer r.Body.Close()
	body, err := io.ReadAll(io.LimitReader(r.Body, maxRequestBody+1))
	if err != nil {
		return nil, fmt.Errorf("读取请求体失败：%w", err)
	}
	if len(body) == 0 {
		return nil, errors.New("请求体为空")
	}
	if len(body) > maxRequestBody {
		return nil, fmt.Errorf("请求体超过上限 %d 字节", maxRequestBody)
	}
	return body, nil
}

// buildUpstreamBody 组装上游请求体：只改写 model（以及可选 stream_options），其余字段原样透传。
func buildUpstreamBody(raw map[string]json.RawMessage, ep *provider.Endpoint, includeUsage bool) ([]byte, error) {
	clone := make(map[string]json.RawMessage, len(raw)+1)
	for key, value := range raw {
		clone[key] = value
	}
	upstream, err := json.Marshal(ep.Upstream)
	if err != nil {
		return nil, fmt.Errorf("序列化上游模型名失败：%w", err)
	}
	clone["model"] = upstream

	// 只有供应商声明支持流式 usage 时才注入 stream_options.include_usage，
	// 否则某些中转站会因为不认识该字段直接 400。
	if includeUsage {
		streamOptions := map[string]json.RawMessage{}
		if old, ok := clone["stream_options"]; ok && len(old) > 0 {
			_ = json.Unmarshal(old, &streamOptions)
			if streamOptions == nil {
				streamOptions = map[string]json.RawMessage{}
			}
		}
		streamOptions["include_usage"] = json.RawMessage("true")
		encoded, err := json.Marshal(streamOptions)
		if err == nil {
			clone["stream_options"] = encoded
		}
	}
	return json.Marshal(clone)
}

// copyUpstreamHeaders 透传上游响应头（跳过 hop-by-hop 与长度相关头）。
func copyUpstreamHeaders(w http.ResponseWriter, resp *http.Response) {
	if resp == nil {
		return
	}
	for key, values := range resp.Header {
		switch strings.ToLower(key) {
		case "content-length", "transfer-encoding", "connection", "keep-alive",
			"proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade":
			continue
		}
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
}

// writeJSON 写 JSON 响应。
func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// writeAPIError 写统一格式的 OpenAI 风格错误。
func writeAPIError(w http.ResponseWriter, status int, message, errType string) {
	if errType == "" {
		errType = "gateway_error"
	}
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"message": message,
			"type":    errType,
		},
	})
}

// writeUpstreamError 全部候选失败时的 502（带每次尝试的明细，便于排查）。
func writeUpstreamError(w http.ResponseWriter, attempts []model.Attempt, lastErr string, lastStatus int) {
	message := "所有上游均失败"
	if lastErr != "" {
		if lastStatus > 0 {
			message = fmt.Sprintf("所有上游均失败（最后状态码 %d）：%s", lastStatus, lastErr)
		} else {
			message = "所有上游均失败：" + lastErr
		}
	}
	if attempts == nil {
		attempts = []model.Attempt{}
	}
	writeJSON(w, http.StatusBadGateway, map[string]any{
		"error": map[string]any{
			"message":  message,
			"type":     "upstream_error",
			"attempts": attempts,
		},
	})
}

// methodNotAllowed 统一 405 响应。
func methodNotAllowed(w http.ResponseWriter) {
	w.Header().Set("Allow", "GET, POST")
	writeAPIError(w, http.StatusMethodNotAllowed, "请求方法不支持", "invalid_request_error")
}

// errorTypeForStatus 把拒绝状态码映射成 OpenAI 风格的错误类型。
func errorTypeForStatus(status int) string {
	switch status {
	case http.StatusUnauthorized:
		return "authentication_error"
	case http.StatusForbidden:
		return "permission_error"
	case http.StatusPaymentRequired:
		return "insufficient_quota"
	case http.StatusTooManyRequests:
		return "rate_limit_error"
	default:
		return "invalid_request_error"
	}
}

// clientIP 取客户端 IP（去掉端口）。只用 RemoteAddr，避免 X-Forwarded-For 被伪造。
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// msSince 返回从 t 到现在的毫秒数。
func msSince(t time.Time) int64 {
	return time.Since(t).Milliseconds()
}

// durationSeconds 秒字符串（便于日志/调试）。
func durationSeconds(d time.Duration) string {
	return strconv.FormatFloat(d.Seconds(), 'f', 3, 64) + "s"
}