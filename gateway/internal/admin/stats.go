package admin

import (
	"net/http"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

// handleStats 返回窗口统计（StatsSnapshot）。
//
// 查询参数：window（秒，默认 300，最大 900）、route、model、provider、client。
func (a *api) handleStats(w http.ResponseWriter, r *http.Request) {
	if a.opts.Aggregator == nil {
		writeJSON(w, http.StatusOK, model.StatsSnapshot{})
		return
	}
	window := queryInt(r, "window", 300, 1, 900)
	scope := metrics.Scope{}
	for _, key := range []string{"route", "model", "provider", "client"} {
		if value := r.URL.Query().Get(key); value != "" {
			scope[key] = value
		}
	}
	if len(scope) == 0 {
		scope = nil
	}
	writeJSON(w, http.StatusOK, a.opts.Aggregator.Query(window, scope))
}