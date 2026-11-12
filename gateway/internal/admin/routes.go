package admin

import (
	"encoding/json"
	"io"
	"net/http"

	"tsukiyohub/gateway/internal/config"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/strategy"
)

// handleGetRoutes 返回路由与可用策略。
func (a *api) handleGetRoutes(w http.ResponseWriter, r *http.Request) {
	cfg := a.config()
	if cfg == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关配置尚未就绪")
		return
	}
	routes := cfg.Routes
	if routes == nil {
		routes = []model.Route{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items":      routes,
		"routes":     routes,
		"total":      len(routes),
		"strategies": strategy.Names(),
	})
}

// handlePutRoutes 只替换 routes 并走 Reload（其它配置保持不变）。
//
// 兼容两种请求体：`{"routes":[...]}` 与裸数组 `[...]`。
func (a *api) handlePutRoutes(w http.ResponseWriter, r *http.Request) {
	cfg := a.config()
	if cfg == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关配置尚未就绪")
		return
	}
	if a.opts.Reload == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关未提供热更新回调（Reload）")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 4<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "读取请求体失败："+err.Error())
		return
	}
	defer r.Body.Close()

	var routes []model.Route
	if trimmed := trimSpaceBytes(body); len(trimmed) > 0 && trimmed[0] == '[' {
		if err := json.Unmarshal(trimmed, &routes); err != nil {
			writeErr(w, http.StatusBadRequest, "解析 routes 数组失败："+err.Error())
			return
		}
	} else {
		var payload struct {
			Routes []model.Route `json:"routes"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			writeErr(w, http.StatusBadRequest, "解析请求体失败："+err.Error())
			return
		}
		routes = payload.Routes
	}

	next := *cfg // 浅拷贝：下面只替换 Routes 字段
	next.Routes = routes
	// 默认路由可能被删掉了：自动指向第一个路由，避免「改路由顺手把网关配坏」。
	if next.DefaultRoute != "" && !routeAliasExists(routes, next.DefaultRoute) {
		if len(routes) > 0 {
			next.DefaultRoute = routes[0].Alias
		} else {
			next.DefaultRoute = ""
		}
	}
	if err := config.Validate(&next); err != nil {
		writeErr(w, http.StatusBadRequest, "路由校验失败："+err.Error())
		return
	}
	if err := a.opts.Reload(&next); err != nil {
		writeErr(w, http.StatusBadRequest, "路由应用失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items":         routes,
		"routes":        routes,
		"total":         len(routes),
		"config_hash":   config.Hash(&next),
		"needs_restart": false,
	})
}

// routeAliasExists 判断新路由列表里是否还有该别名。
func routeAliasExists(routes []model.Route, alias string) bool {
	for _, route := range routes {
		if route.Alias == alias {
			return true
		}
	}
	return false
}

// trimSpaceBytes 去掉前后空白（不引入 strings 只为一次裁剪）。
func trimSpaceBytes(b []byte) []byte {
	start := 0
	for start < len(b) && isSpaceByte(b[start]) {
		start++
	}
	end := len(b)
	for end > start && isSpaceByte(b[end-1]) {
		end--
	}
	return b[start:end]
}

func isSpaceByte(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r'
}