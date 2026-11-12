// Package config 负责读取、校验、脱敏与持久化网关配置。
//
// 配置是**单一 JSON 文件**（`config.json`），支持 `${ENV_VAR}` 注入密钥，
// 这样密钥可以只存在环境变量里，配置文件可以进版本库。
//
// 热更新约定：管理接口 PUT 配置 → 校验 → 原子写回文件 → 网关整体重建路由/供应商。
// 只有 `listen` / `admin_listen` 需要重启（会返回 needs_restart=true）。
package config

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

var envPattern = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// ExpandEnv 把 `${VAR}` 替换成环境变量值；未定义时替换为空串。
//
// 为什么不用 os.ExpandEnv：它会把 `$VAR` 和 `${VAR}` 都替换，
// 而 API Key 里偶尔出现 `$` 字符（例如 base64），只认 `${...}` 更安全。
func ExpandEnv(data []byte) []byte {
	return envPattern.ReplaceAllFunc(data, func(match []byte) []byte {
		name := envPattern.FindSubmatch(match)[1]
		return []byte(os.Getenv(string(name)))
	})
}

// Load 读取并解析配置文件。
func Load(path string) (*model.Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取配置 %s 失败：%w", path, err)
	}
	cfg := &model.Config{}
	if err := json.Unmarshal(ExpandEnv(raw), cfg); err != nil {
		return nil, fmt.Errorf("解析配置 %s 失败：%w", path, err)
	}
	cfg.WithDefaults()
	if err := Validate(cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Save 原子写回配置（临时文件 + rename，避免半截文件）。
func Save(path string, cfg *model.Config) error {
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化配置失败：%w", err)
	}
	tmp := fmt.Sprintf("%s.tmp.%d", path, time.Now().UnixNano())
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("写入临时配置失败：%w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("替换配置失败：%w", err)
	}
	return nil
}

// Hash 配置指纹（管理接口用来判断「配置有没有变」）。
func Hash(cfg *model.Config) string {
	data, _ := json.Marshal(cfg)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:8])
}

// Validate 校验引用完整性。错误信息直接返回给管理员看，所以要写清楚。
func Validate(cfg *model.Config) error {
	providers := map[string]bool{}
	for _, p := range cfg.Providers {
		if strings.TrimSpace(p.Name) == "" {
			return errors.New("providers[].name 不能为空")
		}
		if providers[p.Name] {
			return fmt.Errorf("供应商 name 重复：%s", p.Name)
		}
		if strings.TrimSpace(p.BaseURL) == "" {
			return fmt.Errorf("供应商 %s 缺少 base_url", p.Name)
		}
		providers[p.Name] = true
	}

	models := map[string]bool{}
	for _, m := range cfg.Models {
		if strings.TrimSpace(m.ID) == "" {
			return errors.New("models[].id 不能为空")
		}
		if models[m.ID] {
			return fmt.Errorf("模型 id 重复：%s", m.ID)
		}
		if m.Provider != "" && !providers[m.Provider] {
			return fmt.Errorf("模型 %s 引用了不存在的供应商 %s", m.ID, m.Provider)
		}
		models[m.ID] = true
	}

	aliases := map[string]bool{}
	for _, r := range cfg.Routes {
		if strings.TrimSpace(r.Alias) == "" {
			return errors.New("routes[].alias 不能为空")
		}
		if aliases[r.Alias] {
			return fmt.Errorf("路由别名重复：%s", r.Alias)
		}
		aliases[r.Alias] = true
		if len(r.Targets) == 0 && len(r.Fallbacks) == 0 {
			return fmt.Errorf("路由 %s 没有任何候选（targets/fallbacks 都为空）", r.Alias)
		}
		for _, t := range r.Targets {
			if !models[t.Model] {
				return fmt.Errorf("路由 %s 的目标模型 %s 未定义", r.Alias, t.Model)
			}
		}
		for _, fb := range r.Fallbacks {
			if !models[fb] {
				return fmt.Errorf("路由 %s 的 fallback 模型 %s 未定义", r.Alias, fb)
			}
		}
	}
	if cfg.DefaultRoute != "" && !aliases[cfg.DefaultRoute] {
		return fmt.Errorf("default_route=%s 不是已定义的路由别名", cfg.DefaultRoute)
	}

	for _, c := range cfg.Clients {
		if strings.TrimSpace(c.Key) == "" {
			return errors.New("clients[].key 不能为空")
		}
		for _, r := range c.Routes {
			if !aliases[r] {
				return fmt.Errorf("client %s 允许的路由 %s 未定义", c.Name, r)
			}
		}
	}

	for _, rule := range cfg.Alerts.Rules {
		if strings.TrimSpace(rule.ID) == "" {
			return errors.New("alerts.rules[].id 不能为空")
		}
		if strings.TrimSpace(rule.Metric) == "" {
			return fmt.Errorf("告警规则 %s 缺少 metric", rule.ID)
		}
		switch rule.Op {
		case ">", ">=", "<", "<=", "==", "!=":
		default:
			return fmt.Errorf("告警规则 %s 的 op 非法：%q（支持 > >= < <= == !=）", rule.ID, rule.Op)
		}
		if rule.Severity == "" {
			rule.Severity = "warning"
		}
	}
	return nil
}

// Redacted 返回脱敏后的配置视图：密钥只显示前后 4 位。
//
// 管理接口 GET 一律用这个，避免把上游密钥发给前端。
func Redacted(cfg *model.Config) *model.Config {
	clone := *cfg
	clone.AdminToken = maskSecret(clone.AdminToken)
	clone.Clients = append([]model.ClientKey(nil), cfg.Clients...)
	for i := range clone.Clients {
		clone.Clients[i].Key = maskSecret(clone.Clients[i].Key)
	}
	clone.Providers = append([]model.Provider(nil), cfg.Providers...)
	for i := range clone.Providers {
		clone.Providers[i].APIKey = maskSecret(clone.Providers[i].APIKey)
		if len(clone.Providers[i].Headers) > 0 {
			copied := map[string]string{}
			for k, v := range clone.Providers[i].Headers {
				if strings.Contains(strings.ToLower(k), "key") || strings.Contains(strings.ToLower(k), "token") {
					copied[k] = maskSecret(v)
				} else {
					copied[k] = v
				}
			}
			clone.Providers[i].Headers = copied
		}
	}
	clone.Alerts = cfg.Alerts
	clone.Alerts.Notifiers = append([]model.NotifierConfig(nil), cfg.Alerts.Notifiers...)
	for i := range clone.Alerts.Notifiers {
		clone.Alerts.Notifiers[i].URL = maskSecret(clone.Alerts.Notifiers[i].URL)
		clone.Alerts.Notifiers[i].Secret = maskSecret(clone.Alerts.Notifiers[i].Secret)
	}
	return &clone
}

func maskSecret(value string) string {
	if value == "" {
		return ""
	}
	if len(value) <= 8 {
		return "****"
	}
	return value[:4] + "****" + value[len(value)-4:]
}

// MergeSecrets 把 GET 时脱敏的配置与旧配置合并：
// 管理员在界面上没有改密钥时，前端传回来的是 `xxxx****yyyy`，
// 直接写回会把真实密钥覆盖掉，所以这里遇到「含 ****」的字段就沿用旧值。
func MergeSecrets(next, old *model.Config) *model.Config {
	if strings.Contains(next.AdminToken, "****") {
		next.AdminToken = old.AdminToken
	}
	for i := range next.Clients {
		for j := range old.Clients {
			if next.Clients[i].Name == old.Clients[j].Name && strings.Contains(next.Clients[i].Key, "****") {
				next.Clients[i].Key = old.Clients[j].Key
			}
		}
	}
	for i := range next.Providers {
		for j := range old.Providers {
			if next.Providers[i].Name != old.Providers[j].Name {
				continue
			}
			if strings.Contains(next.Providers[i].APIKey, "****") {
				next.Providers[i].APIKey = old.Providers[j].APIKey
			}
			for k, v := range next.Providers[i].Headers {
				if strings.Contains(v, "****") {
					if oldV, ok := old.Providers[j].Headers[k]; ok {
						next.Providers[i].Headers[k] = oldV
					}
				}
			}
		}
	}
	for i := range next.Alerts.Notifiers {
		for j := range old.Alerts.Notifiers {
			if next.Alerts.Notifiers[i].Name != old.Alerts.Notifiers[j].Name {
				continue
			}
			if strings.Contains(next.Alerts.Notifiers[i].URL, "****") {
				next.Alerts.Notifiers[i].URL = old.Alerts.Notifiers[j].URL
			}
			if strings.Contains(next.Alerts.Notifiers[i].Secret, "****") {
				next.Alerts.Notifiers[i].Secret = old.Alerts.Notifiers[j].Secret
			}
		}
	}
	return next
}

// ExecPath 返回可执行文件自身的目录（用于定位相对配置/静态文件）。
func ExecPath() string {
	path, err := os.Executable()
	if err != nil {
		return "."
	}
	return filepath.Dir(path)
}