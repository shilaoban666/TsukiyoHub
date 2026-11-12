package admin

import (
	"net/http"
	"time"

	"tsukiyohub/gateway/internal/config"
	"tsukiyohub/gateway/internal/model"
)

// handleOverview 返回仪表盘数据（model.Overview）。
func (a *api) handleOverview(w http.ResponseWriter, r *http.Request) {
	cfg := a.config()
	overview := model.Overview{
		Version:    a.opts.Version,
		StartedAt:  a.opts.StartedAt,
		UptimeSec:  int64(time.Since(a.opts.StartedAt).Seconds()),
		ConfigPath: a.opts.ConfigPath,
		Breakers:   map[string]string{},
	}
	if a.opts.Aggregator != nil {
		overview.Metrics = a.opts.Aggregator.Query(300, nil)
	}
	if providers := a.healthList(r.Context()); providers != nil {
		overview.Providers = providers
	} else {
		overview.Providers = []model.ProviderHealth{}
	}
	if a.opts.Breakers != nil {
		if snapshot := a.opts.Breakers.Snapshot(); snapshot != nil {
			overview.Breakers = snapshot
		}
	}
	if cfg != nil {
		overview.PublicURL = cfg.PublicURL
		overview.Routes = len(cfg.Routes)
		overview.Models = len(cfg.Models)
		overview.ConfigHash = config.Hash(cfg)
	}
	if engine, ok := a.alertEngine(); ok {
		overview.Alerts = engine.Overview()
	} else {
		overview.Alerts = model.AlertOverview{}
	}
	writeJSON(w, http.StatusOK, overview)
}