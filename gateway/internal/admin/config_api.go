package admin

import (
	"net/http"

	"tsukiyohub/gateway/internal/config"
	"tsukiyohub/gateway/internal/model"
)

// configResponse 配置接口的返回结构（GET / PUT / reload 一致）。
type configResponse struct {
	// Config 脱敏后的配置（密钥只显示前后 4 位）
	Config *model.Config `json:"config"`
	// ConfigHash 配置指纹（前端用来判断有没有脏数据）
	ConfigHash string `json:"config_hash"`
	// NeedsRestart 只有 listen / admin_listen 变化才需要重启进程
	NeedsRestart bool `json:"needs_restart"`
}

// handleGetConfig 返回脱敏配置。
func (a *api) handleGetConfig(w http.ResponseWriter, _ *http.Request) {
	cfg := a.config()
	if cfg == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关配置尚未就绪")
		return
	}
	writeJSON(w, http.StatusOK, configResponse{
		Config:     config.Redacted(cfg),
		ConfigHash: config.Hash(cfg),
	})
}

// handlePutConfig 用请求体整体替换配置：校验 → 合并密钥 → 热更新。
func (a *api) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	old := a.config()
	if old == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关配置尚未就绪")
		return
	}
	next := &model.Config{}
	if !decodeJSON(w, r, next) {
		return
	}
	next.WithDefaults()
	if err := config.Validate(next); err != nil {
		writeErr(w, http.StatusBadRequest, "配置校验失败："+err.Error())
		return
	}
	if a.opts.Reload == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关未提供热更新回调（Reload）")
		return
	}
	// 前端拿到的是脱敏配置，没改动的密钥是 `ab****yz`；这里还原成真实值。
	merged := config.MergeSecrets(next, old)
	if err := a.opts.Reload(merged); err != nil {
		writeErr(w, http.StatusBadRequest, "配置应用失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, configResponse{
		Config:       config.Redacted(merged),
		ConfigHash:   config.Hash(merged),
		NeedsRestart: listenChanged(old, merged),
	})
}

// handleReloadConfig 从配置文件重新加载。
func (a *api) handleReloadConfig(w http.ResponseWriter, r *http.Request) {
	if a.opts.ConfigPath == "" {
		writeErr(w, http.StatusBadRequest, "未配置 config_path，无法从文件重新加载")
		return
	}
	if a.opts.Reload == nil {
		writeErr(w, http.StatusServiceUnavailable, "网关未提供热更新回调（Reload）")
		return
	}
	old := a.config()
	next, err := config.Load(a.opts.ConfigPath)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "重新加载配置失败："+err.Error())
		return
	}
	if err := a.opts.Reload(next); err != nil {
		writeErr(w, http.StatusBadRequest, "配置应用失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, configResponse{
		Config:       config.Redacted(next),
		ConfigHash:   config.Hash(next),
		NeedsRestart: old != nil && listenChanged(old, next),
	})
}

// listenChanged 判断监听地址是否变化（这类变更只能重启进程）。
func listenChanged(old, next *model.Config) bool {
	if old == nil || next == nil {
		return false
	}
	return old.Listen != next.Listen || old.AdminListen != next.AdminListen
}