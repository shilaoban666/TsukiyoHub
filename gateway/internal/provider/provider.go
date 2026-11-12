// Package provider 维护「模型 → 供应商」注册表，并负责发起上游 HTTP 调用与探活。
//
// 设计要点：
//  1. Registry 是**可整体替换的只读快照**：配置热更新时调用 Reload 原地换掉内部索引
//     （读写锁保护），proxy / admin 持有的 *Registry 指针无需替换即可热更新。
//  2. Do 返回原始 *http.Response，由调用方决定怎么读 body——流式转发必须由代理控制读取节奏，
//     所以不能在这里把 body 读完。
//  3. 不做重试：失败切换（fallback）是路由层的职责，provider 只负责「发一次、拿到响应」。
package provider

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// Endpoint 一个可调用的「模型 + 供应商」组合。
type Endpoint struct {
	// Model 对外 model id（= ModelDef.ID，也是路由 Target.Model）
	Model string
	// Upstream 上游真实模型名（ModelDef.Upstream，空则与 Model 相同）
	Upstream string
	// Provider 供应商配置（含 base_url / api_key / headers / 超时）
	Provider model.Provider
	// Def 模型定义（价格、能力、上下文长度）
	Def model.ModelDef
}

// Registry 供应商与模型注册表（配置变更时整体重建）。
//
// 零值不可用，必须通过 NewRegistry 构造；Reload 可以并发调用（与 Resolve/Do 并发安全）。
type Registry struct {
	mu sync.RWMutex

	models        map[string]*Endpoint
	modelOrder    []string
	providers     map[string]model.Provider
	providerOrder []string
	// enabledProviders 当前被显式启用的供应商数量（用于兼容未写 enabled 的旧配置）
	enabledProviders int

	transport     *http.Transport
	connTimeout   time.Duration
	headerTimeout time.Duration
	totalTimeout  time.Duration

	// 探活缓存（Probe/ProbeAll 写入，CachedHealth 读取）
	health   map[string]model.ProviderHealth
	healthAt time.Time

	// 可选的指标注册表：绑定后探活会更新 tsukiyo_gateway_provider_up
	metrics *metrics.Registry
}

// NewRegistry 按配置构造注册表；内部等价于「空表 + Reload」。
func NewRegistry(cfg *model.Config) (*Registry, error) {
	r := &Registry{}
	if err := r.Reload(cfg); err != nil {
		return nil, err
	}
	return r, nil
}

// Reload 用新配置重建内部索引（供应商/模型/HTTP 传输层），保持 *Registry 指针不变。
//
// 并发安全：先在新结构上完成构建，再在一把写锁里整体交换；正在进行的请求
// 已经通过 Resolve 拿到了 Endpoint 值拷贝，不受影响。
func (r *Registry) Reload(cfg *model.Config) error {
	if cfg == nil {
		return errors.New("provider: 配置为 nil")
	}

	providers := make(map[string]model.Provider, len(cfg.Providers))
	providerOrder := make([]string, 0, len(cfg.Providers))
	enabled := 0
	for _, p := range cfg.Providers {
		name := strings.TrimSpace(p.Name)
		if name == "" {
			return errors.New("provider: providers[].name 不能为空")
		}
		if _, dup := providers[name]; dup {
			return fmt.Errorf("provider: 供应商 name 重复：%s", name)
		}
		if strings.TrimSpace(p.BaseURL) == "" {
			return fmt.Errorf("provider: 供应商 %s 缺少 base_url", name)
		}
		p.Name = name
		if p.HealthPath == "" {
			p.HealthPath = "/models"
		}
		providers[name] = p
		providerOrder = append(providerOrder, name)
		if p.Enabled {
			enabled++
		}
	}
	// 兼容性：老配置常忘写 provider.enabled=true（JSON 零值是 false），
	// 若一个都没启用则视为全部启用，避免「配置看起来对但网关不工作」。
	if enabled == 0 && len(providers) > 0 {
		for name, p := range providers {
			p.Enabled = true
			providers[name] = p
		}
		enabled = len(providers)
	}

	models := make(map[string]*Endpoint, len(cfg.Models))
	modelOrder := make([]string, 0, len(cfg.Models))
	for _, m := range cfg.Models {
		id := strings.TrimSpace(m.ID)
		if id == "" {
			return errors.New("provider: models[].id 不能为空")
		}
		if _, dup := models[id]; dup {
			return fmt.Errorf("provider: 模型 id 重复：%s", id)
		}
		prov, ok := providers[m.Provider]
		if !ok {
			return fmt.Errorf("provider: 模型 %s 引用了不存在的供应商 %q", id, m.Provider)
		}
		upstream := m.Upstream
		if upstream == "" {
			upstream = id
		}
		m.ID = id
		models[id] = &Endpoint{Model: id, Upstream: upstream, Provider: prov, Def: m}
		modelOrder = append(modelOrder, id)
	}

	connTimeout := msDuration(cfg.Timeouts.ConnectMS)
	headerTimeout := msDuration(cfg.Timeouts.HeaderMS)
	totalTimeout := msDuration(cfg.Timeouts.TotalMS)

	transport := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 64,
		IdleConnTimeout:     90 * time.Second,
		// DisableCompression：SSE 流式响应如果被 gzip，代理必须解压才能按块 flush，
		// 某些中转站还会在压缩后整块缓冲；直接禁用压缩最稳。
		DisableCompression:    true,
		ForceAttemptHTTP2:     true,
		ExpectContinueTimeout: time.Second,
		ResponseHeaderTimeout: headerTimeout,
	}
	dialer := &net.Dialer{KeepAlive: 30 * time.Second}
	if connTimeout > 0 {
		dialer.Timeout = connTimeout
		transport.TLSHandshakeTimeout = connTimeout
	} else {
		transport.TLSHandshakeTimeout = 10 * time.Second
	}
	transport.DialContext = dialer.DialContext

	r.mu.Lock()
	old := r.transport
	r.models = models
	r.modelOrder = modelOrder
	r.providers = providers
	r.providerOrder = providerOrder
	r.enabledProviders = enabled
	r.transport = transport
	r.connTimeout = connTimeout
	r.headerTimeout = headerTimeout
	r.totalTimeout = totalTimeout
	r.health = map[string]model.ProviderHealth{}
	r.healthAt = time.Time{}
	r.mu.Unlock()

	// 旧传输层里的长连接已经没有归属，主动关掉避免 fd 泄漏。
	if old != nil {
		old.CloseIdleConnections()
	}
	return nil
}

// SetMetrics 绑定指标注册表（可选）：绑上后探活会写入 tsukiyo_gateway_provider_up。
func (r *Registry) SetMetrics(reg *metrics.Registry) {
	r.mu.Lock()
	r.metrics = reg
	r.mu.Unlock()
}

// Resolve 按对外 model id 查找可用端点，返回**值拷贝**（调用方可以安全长期持有）。
func (r *Registry) Resolve(modelID string) (*Endpoint, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.models == nil {
		return nil, errors.New("provider: 注册表未初始化")
	}
	ep, ok := r.models[modelID]
	if !ok {
		return nil, fmt.Errorf("provider: 模型 %q 未在 models 中定义", modelID)
	}
	if !ep.Provider.Enabled {
		return nil, fmt.Errorf("provider: 模型 %q 的供应商 %q 已禁用", modelID, ep.Provider.Name)
	}
	clone := *ep
	return &clone, nil
}

// All 返回全部端点（按 models 配置顺序，值为拷贝）。
func (r *Registry) All() []*Endpoint {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Endpoint, 0, len(r.modelOrder))
	for _, id := range r.modelOrder {
		ep := r.models[id]
		if ep == nil {
			continue
		}
		clone := *ep
		out = append(out, &clone)
	}
	return out
}

// Names 返回全部供应商名（按 providers 配置顺序），探活巡检用。
func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return append([]string(nil), r.providerOrder...)
}

// EnabledProviderCount 当前启用的供应商数量。
func (r *Registry) EnabledProviderCount() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.enabledProviders
}

// transport 返回当前 HTTP 传输层与全局超时设置。
func (r *Registry) transportSnapshot() (*http.Transport, time.Duration) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.transport, r.totalTimeout
}

// providerConfig 返回供应商配置拷贝。
func (r *Registry) providerConfig(name string) (model.Provider, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	p, ok := r.providers[name]
	return p, ok
}

// msDuration 把毫秒配置转成 time.Duration（<=0 视为 0，即「不限」）。
func msDuration(ms int) time.Duration {
	if ms <= 0 {
		return 0
	}
	return time.Duration(ms) * time.Millisecond
}