package config

import (
	"os"
	"path/filepath"
	"testing"

	"tsukiyohub/gateway/internal/model"
)

func baseConfig() *model.Config {
	return &model.Config{
		Listen:      ":8081",
		AdminListen: ":8082",
		AdminToken:  "super-secret-token",
		Providers: []model.Provider{
			{Name: "p1", BaseURL: "http://localhost:1/v1", APIKey: "sk-abcdef123456", Enabled: true},
		},
		Models: []model.ModelDef{{ID: "m1", Provider: "p1"}},
		Routes: []model.Route{{
			Alias: "chat", Strategy: "priority", Enabled: true,
			Targets: []model.Target{{Model: "m1", Priority: 1}},
		}},
		Clients: []model.ClientKey{{Key: "sk-client-123456", Name: "demo", Enabled: true}},
	}
}

func TestExpandEnv(t *testing.T) {
	t.Setenv("TSUKIYO_TEST_KEY", "sk-from-env")
	got := string(ExpandEnv([]byte(`{"api_key":"${TSUKIYO_TEST_KEY}"}`)))
	if got != `{"api_key":"sk-from-env"}` {
		t.Fatalf("ExpandEnv=%s", got)
	}
	// 未定义变量替换为空串；非 ${} 形式的 $ 不被处理
	got = string(ExpandEnv([]byte(`{"a":"${NOT_DEFINED}","b":"$HOME"}`)))
	if got != `{"a":"","b":"$HOME"}` {
		t.Fatalf("ExpandEnv 未定义变量处理错误：%s", got)
	}
}

func TestValidateRejectsBrokenReferences(t *testing.T) {
	cfg := baseConfig()
	cfg.Routes[0].Targets = []model.Target{{Model: "missing"}}
	if err := Validate(cfg); err == nil {
		t.Fatal("目标模型不存在时应报错")
	}

	cfg = baseConfig()
	cfg.DefaultRoute = "nope"
	if err := Validate(cfg); err == nil {
		t.Fatal("default_route 指向不存在的别名时应报错")
	}

	cfg = baseConfig()
	cfg.Routes = append(cfg.Routes, cfg.Routes[0])
	if err := Validate(cfg); err == nil {
		t.Fatal("路由别名重复时应报错")
	}

	cfg = baseConfig()
	cfg.Alerts.Rules = []model.AlertRule{{ID: "r1", Metric: "error_rate", Op: "~~"}}
	if err := Validate(cfg); err == nil {
		t.Fatal("非法运算符应报错")
	}

	if err := Validate(baseConfig()); err != nil {
		t.Fatalf("合法配置不应报错：%v", err)
	}
}

func TestRedactedAndMergeSecrets(t *testing.T) {
	cfg := baseConfig()
	masked := Redacted(cfg)
	if masked.Providers[0].APIKey == cfg.Providers[0].APIKey {
		t.Fatal("provider key 未脱敏")
	}
	if masked.Clients[0].Key == cfg.Clients[0].Key {
		t.Fatal("client key 未脱敏")
	}
	if masked.AdminToken == cfg.AdminToken {
		t.Fatal("admin token 未脱敏")
	}

	// 前端把脱敏值原样提交回来时，应沿用旧密钥
	merged := MergeSecrets(masked, cfg)
	if merged.Providers[0].APIKey != cfg.Providers[0].APIKey {
		t.Fatalf("MergeSecrets 未恢复 provider key：%s", merged.Providers[0].APIKey)
	}
	if merged.Clients[0].Key != cfg.Clients[0].Key {
		t.Fatalf("MergeSecrets 未恢复 client key：%s", merged.Clients[0].Key)
	}
}

func TestLoadSaveRoundtrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	t.Setenv("TSUKIYO_TEST_PROVIDER_KEY", "sk-env-123456")
	content := `{
	  "listen": ":18081",
	  "providers": [{"name":"p1","base_url":"http://localhost:1/v1","api_key":"${TSUKIYO_TEST_PROVIDER_KEY}","enabled":true}],
	  "models": [{"id":"m1","provider":"p1"}],
	  "routes": [{"alias":"chat","strategy":"priority","enabled":true,"targets":[{"model":"m1","priority":1}]}]
	}`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load 失败：%v", err)
	}
	if cfg.Providers[0].APIKey != "sk-env-123456" {
		t.Fatalf("环境变量注入失败：%s", cfg.Providers[0].APIKey)
	}
	if cfg.MaxAttempts <= 0 || cfg.Timeouts.ConnectMS <= 0 {
		t.Fatalf("默认值未填充：%+v", cfg)
	}
	if err := Save(path, cfg); err != nil {
		t.Fatalf("Save 失败：%v", err)
	}
	again, err := Load(path)
	if err != nil {
		t.Fatalf("二次 Load 失败：%v", err)
	}
	if Hash(again) != Hash(cfg) {
		t.Fatalf("保存前后配置指纹不一致")
	}
}