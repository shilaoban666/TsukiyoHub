package admin

import (
	"net/http"
	"time"

	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
)

// alertsDisabled 未启用告警引擎时的统一响应体。
var alertsDisabled = map[string]any{"enabled": false}

// handleAlertRules 返回告警规则。
func (a *api) handleAlertRules(w http.ResponseWriter, _ *http.Request) {
	engine, ok := a.alertEngine()
	if !ok {
		writeJSON(w, http.StatusOK, alertsDisabled)
		return
	}
	rules := engine.ListRules()
	if rules == nil {
		rules = []model.AlertRule{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"enabled": true, "items": rules, "rules": rules, "total": len(rules)})
}

// handleReplaceAlertRules 整体替换规则（调 AlertEngine.ReplaceRules）。
//
// 兼容 `{"rules":[...]}` 与裸数组两种请求体。
func (a *api) handleReplaceAlertRules(w http.ResponseWriter, r *http.Request) {
	engine, ok := a.alertEngine()
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"enabled": false,
			"error":   "告警引擎未启用",
		})
		return
	}
	var payload struct {
		Rules []model.AlertRule `json:"rules"`
	}
	if !decodeJSON(w, r, &payload) {
		return
	}
	if err := engine.ReplaceRules(payload.Rules); err != nil {
		writeErr(w, http.StatusBadRequest, "替换告警规则失败："+err.Error())
		return
	}
	rules := engine.ListRules()
	if rules == nil {
		rules = []model.AlertRule{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"enabled": true, "items": rules, "rules": rules, "total": len(rules)})
}

// handleAlertEvents 返回最近告警事件（status 为空 = 全部）。
func (a *api) handleAlertEvents(w http.ResponseWriter, r *http.Request) {
	engine, ok := a.alertEngine()
	if !ok {
		writeJSON(w, http.StatusOK, alertsDisabled)
		return
	}
	limit := queryInt(r, "limit", 50, 1, 500)
	status := r.URL.Query().Get("status")
	events := engine.ListEvents(limit, status)
	if events == nil {
		events = []model.AlertEvent{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"enabled": true, "items": events, "events": events, "total": len(events)})
}

// handleAlertAck 确认一条告警（body: {"by":"web","minutes":30}）。
func (a *api) handleAlertAck(w http.ResponseWriter, r *http.Request) {
	engine, ok := a.alertEngine()
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"enabled": false,
			"error":   "告警引擎未启用",
		})
		return
	}
	id := r.PathValue("id")
	if id == "" {
		writeErr(w, http.StatusBadRequest, "缺少告警 ID")
		return
	}
	var payload struct {
		By      string `json:"by"`
		Minutes int    `json:"minutes"`
	}
	if r.ContentLength != 0 {
		if !decodeJSON(w, r, &payload) {
			return
		}
	}
	if payload.By == "" {
		payload.By = "web"
	}
	minutes := payload.Minutes
	if minutes <= 0 {
		minutes = 30
	}
	if err := engine.Ack(id, payload.By, time.Duration(minutes)*time.Minute); err != nil {
		writeErr(w, http.StatusNotFound, "确认告警失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"enabled": true,
		"ok":      true,
		"id":      id,
		"by":      payload.By,
		"minutes": minutes,
	})
}

// handleAlertOverview 返回告警概况（model.AlertOverview）。
func (a *api) handleAlertOverview(w http.ResponseWriter, _ *http.Request) {
	engine, ok := a.alertEngine()
	if !ok {
		writeJSON(w, http.StatusOK, model.AlertOverview{Enabled: false})
		return
	}
	writeJSON(w, http.StatusOK, engine.Overview())
}

// handleBudget 返回按客户端的预算用量。
//
// 这是契约之外的补充接口（需求要求「预算累计可查」），用于前端展示与排障；
// 数据来自内存账本，进程重启清零。
func (a *api) handleBudget(w http.ResponseWriter, _ *http.Request) {
	store := a.budgetStore()
	if store == nil {
		writeJSON(w, http.StatusOK, map[string]any{"enabled": false, "clients": []any{}})
		return
	}
	views := store.Snapshot()
	if views == nil {
		views = []middleware.BudgetView{}
	}
	enabled := a.opts.Guards != nil && a.opts.Guards.Find(middleware.GuardNameBudget) != nil
	writeJSON(w, http.StatusOK, map[string]any{
		"enabled": enabled,
		"clients": views,
	})
}

// handleResetBudget 清零预算账本（body: {"client":"名称"}，空 = 全部）。
func (a *api) handleResetBudget(w http.ResponseWriter, r *http.Request) {
	store := a.budgetStore()
	if store == nil {
		writeErr(w, http.StatusServiceUnavailable, "预算账本未初始化")
		return
	}
	var payload struct {
		Client string `json:"client"`
	}
	if r.ContentLength != 0 {
		if !decodeJSON(w, r, &payload) {
			return
		}
	}
	if payload.Client == "" {
		store.ResetAll()
	} else {
		store.Reset(payload.Client)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// budgetStore 找到实际在用的预算账本（优先 Guards 链里的 BudgetGuard）。
func (a *api) budgetStore() *middleware.BudgetStore {
	if a.opts.Guards != nil {
		if guard := a.opts.Guards.Find(middleware.GuardNameBudget); guard != nil {
			if budgetGuard, ok := guard.(*middleware.BudgetGuard); ok {
				return budgetGuard.Store()
			}
		}
	}
	return middleware.DefaultBudgetStore()
}