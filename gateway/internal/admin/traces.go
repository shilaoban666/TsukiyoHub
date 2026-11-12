package admin

import (
	"net/http"

	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/tracing"
)

// handleTraces 返回最近的请求记录。
//
// 查询参数：limit / route / model / ok / since。
// ok 与 since 的语义见 tracing.Filter。
func (a *api) handleTraces(w http.ResponseWriter, r *http.Request) {
	if a.opts.Tracer == nil {
		writeJSON(w, http.StatusOK, map[string]any{"items": []model.RequestRecord{}, "traces": []model.RequestRecord{}, "count": 0})
		return
	}
	filter := tracing.Filter{
		Limit: queryInt(r, "limit", 100, 1, 1000),
		Route: r.URL.Query().Get("route"),
		Model: r.URL.Query().Get("model"),
		Since: parseSince(r.URL.Query().Get("since")),
		Status: queryBool(r, "ok"),
	}
	records := a.opts.Tracer.List(filter)
	if records == nil {
		records = []model.RequestRecord{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items":  records,
		"traces": records,
		"count":  len(records),
		"total":  len(records),
	})
}

// handleTraceDetail 返回单条请求记录。
func (a *api) handleTraceDetail(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		writeErr(w, http.StatusBadRequest, "缺少请求 ID")
		return
	}
	if a.opts.Tracer == nil {
		writeErr(w, http.StatusNotFound, "追踪未启用")
		return
	}
	record, ok := a.opts.Tracer.Get(id)
	if !ok {
		writeErr(w, http.StatusNotFound, "请求记录不存在："+id)
		return
	}
	writeJSON(w, http.StatusOK, record)
}