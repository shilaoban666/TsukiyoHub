package provider

import (
	"context"
	"net/http"
	"sync"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// probeTimeout 单次探活的兜底超时（供应商没配 timeout_ms 时使用）。
const probeTimeout = 5 * time.Second

// Probe 探活单个供应商：GET base_url + health_path，2xx 视为健康。
func (r *Registry) Probe(ctx context.Context, name string) model.ProviderHealth {
	now := time.Now()
	p, ok := r.providerConfig(name)
	if !ok {
		return model.ProviderHealth{
			Name:      name,
			Healthy:   false,
			Error:     "供应商不存在",
			CheckedAt: now,
		}
	}
	if !p.Enabled {
		return model.ProviderHealth{
			Name:      name,
			Healthy:   false,
			Error:     "供应商已禁用",
			CheckedAt: now,
		}
	}

	tr, _ := r.transportSnapshot()
	path := p.HealthPath
	if path == "" {
		path = "/models"
	}
	url := JoinURL(p.BaseURL, path)

	// 探活给自己一个上限，避免拖住 /readyz 与后台巡检。
	timeout := providerTimeout(p)
	if timeout <= 0 || timeout > probeTimeout {
		timeout = probeTimeout
	}
	pctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(pctx, http.MethodGet, url, nil)
	if err != nil {
		return r.finishProbe(model.ProviderHealth{Name: name, Error: err.Error()}, now)
	}
	if p.APIKey != "" {
		if p.Type == "azure" {
			req.Header.Set("api-key", p.APIKey)
		} else {
			req.Header.Set("Authorization", "Bearer "+p.APIKey)
		}
	}
	for k, v := range p.Headers {
		if k != "" {
			req.Header.Set(k, v)
		}
	}

	start := time.Now()
	client := &http.Client{Transport: tr, Timeout: timeout}
	resp, err := client.Do(req)
	latency := time.Since(start).Milliseconds()
	if err != nil {
		return r.finishProbe(model.ProviderHealth{Name: name, Error: err.Error(), LatencyMS: latency}, now)
	}
	defer resp.Body.Close()
	// 探活只需要状态码，body 丢弃但读一点点帮助连接复用。
	buf := make([]byte, 512)
	_, _ = resp.Body.Read(buf)

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return r.finishProbe(model.ProviderHealth{
			Name:      name,
			Healthy:   false,
			LatencyMS: latency,
			Error:     "探活返回 " + resp.Status,
		}, now)
	}
	return r.finishProbe(model.ProviderHealth{
		Name:      name,
		Healthy:   true,
		LatencyMS: latency,
	}, now)
}

// finishProbe 补全公共字段、写入缓存并更新 provider_up 指标。
func (r *Registry) finishProbe(h model.ProviderHealth, at time.Time) model.ProviderHealth {
	h.CheckedAt = at

	r.mu.Lock()
	if r.health == nil {
		r.health = map[string]model.ProviderHealth{}
	}
	r.health[h.Name] = h
	r.healthAt = at
	reg := r.metrics
	r.mu.Unlock()

	if reg != nil {
		up := 0.0
		if h.Healthy {
			up = 1
		}
		reg.Set("tsukiyo_gateway_provider_up", map[string]string{"provider": h.Name}, up)
	}
	return h
}

// ProbeAll 并发探活全部供应商（结果顺序与 providers 配置一致）。
func (r *Registry) ProbeAll(ctx context.Context) []model.ProviderHealth {
	names := r.Names()
	out := make([]model.ProviderHealth, len(names))
	var wg sync.WaitGroup
	for i, name := range names {
		wg.Add(1)
		go func(i int, name string) {
			defer wg.Done()
			out[i] = r.Probe(ctx, name)
		}(i, name)
	}
	wg.Wait()
	return out
}

// CachedHealth 返回最近一次探活结果；距上次探活超过 maxAge 时返回 ok=false。
//
// 供 /readyz 与 admin overview 使用：既不想每次请求都真去探活，又要在
// 数据过期时主动刷新。
func (r *Registry) CachedHealth(maxAge time.Duration) ([]model.ProviderHealth, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.healthAt.IsZero() || time.Since(r.healthAt) > maxAge {
		return nil, false
	}
	out := make([]model.ProviderHealth, 0, len(r.providerOrder))
	for _, name := range r.providerOrder {
		if h, ok := r.health[name]; ok {
			out = append(out, h)
			continue
		}
		out = append(out, model.ProviderHealth{Name: name, Healthy: false, Error: "尚未探活", CheckedAt: r.healthAt})
	}
	return out, true
}

// AnyHealthy 判断给定探活结果里是否至少有一个健康供应商。
func AnyHealthy(list []model.ProviderHealth) bool {
	for _, h := range list {
		if h.Healthy {
			return true
		}
	}
	return false
}