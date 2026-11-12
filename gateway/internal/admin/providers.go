package admin

import (
	"net/http"

	"tsukiyohub/gateway/internal/model"
)

// handleProviders 返回全部供应商的探活健康列表。
func (a *api) handleProviders(w http.ResponseWriter, r *http.Request) {
	list := a.healthList(r.Context())
	if list == nil {
		list = []model.ProviderHealth{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": list, "providers": list, "total": len(list)})
}

// handleProbeProvider 主动探活单个供应商（POST /admin/api/providers/{name}/probe）。
func (a *api) handleProbeProvider(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" {
		writeErr(w, http.StatusBadRequest, "缺少供应商名")
		return
	}
	if a.opts.Providers == nil {
		writeErr(w, http.StatusServiceUnavailable, "供应商注册表未初始化")
		return
	}
	health := a.opts.Providers.Probe(r.Context(), name)
	if health.Error == "供应商不存在" {
		writeErr(w, http.StatusNotFound, "供应商不存在："+name)
		return
	}
	// 探活会更新 provider_up 指标（Registry.SetMetrics 已在 New 里绑定），
	// 这里再同步一下「不可用供应商数」给告警用的瞬时指标。
	if a.opts.Aggregator != nil {
		if cached, ok := a.opts.Providers.CachedHealth(healthCacheAge); ok {
			var down int64
			for _, item := range cached {
				if !item.Healthy {
					down++
				}
			}
			a.opts.Aggregator.SetProvidersDown(down)
		}
	}
	writeJSON(w, http.StatusOK, health)
}