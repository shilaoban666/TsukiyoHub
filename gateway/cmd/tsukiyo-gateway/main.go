// TsukiyoHub LLM 路由网关：进程入口。
//
// 装配顺序：
//
//	配置 → 指标/聚合 → 供应商注册表 → 熔断/限流/预算 → 追踪 → 告警引擎
//	     → 代理入口（:8081）→ 管理接口（:8082）→ 后台巡检（探活/熔断扫描/告警评估）
//
// 热更新：管理接口 PUT /admin/api/config → 校验 → 原子写回文件 → 供应商注册表 Reload
// → 代理/管理接口读取新配置（ConfigSource 原子指针）。只有 listen 变化需要重启。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"tsukiyohub/gateway/internal/admin"
	"tsukiyohub/gateway/internal/alert"
	"tsukiyohub/gateway/internal/config"
	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/middleware"
	"tsukiyohub/gateway/internal/model"
	"tsukiyohub/gateway/internal/provider"
	"tsukiyohub/gateway/internal/proxy"
	"tsukiyohub/gateway/internal/tracing"
)

// Version 由 -ldflags "-X main.Version=x.y.z" 注入。
var Version = "0.1.0"

// configStore 是配置的原子容器：代理与管理接口每次请求读当前值。
type configStore struct {
	ptr atomic.Pointer[model.Config]
}

func (s *configStore) Current() *model.Config { return s.ptr.Load() }
func (s *configStore) Store(cfg *model.Config) { s.ptr.Store(cfg) }

func main() {
	var (
		configPath = flag.String("config", "config.json", "配置文件路径")
		addr       = flag.String("addr", "", "覆盖监听地址（默认取配置 listen）")
		adminAddr  = flag.String("admin-addr", "", "覆盖管理接口地址（默认取配置 admin_listen）")
		showVer    = flag.Bool("version", false, "打印版本号后退出")
	)
	flag.Parse()

	if *showVer {
		fmt.Println("tsukiyo-gateway", Version)
		return
	}

	logger := log.New(os.Stdout, "[gateway] ", log.LstdFlags|log.Lmsgprefix)

	cfg, err := config.Load(*configPath)
	if err != nil {
		logger.Fatalf("启动失败：%v", err)
	}
	if *addr != "" {
		cfg.Listen = *addr
	}
	if *adminAddr != "" {
		cfg.AdminListen = *adminAddr
	}

	startedAt := time.Now()
	store := &configStore{}
	store.Store(cfg)

	// ---- 观测 ----
	registry := metrics.NewRegistry()
	registerMetricHelp(registry)
	aggregator := metrics.NewAggregator()
	tracer := tracing.NewRing(cfg.Tracing.MaxRecords)

	// ---- 上游与保护 ----
	providers, err := provider.NewRegistry(cfg)
	if err != nil {
		logger.Fatalf("供应商初始化失败：%v", err)
	}
	breakers := middleware.NewBreakers(cfg.Breaker)
	guards := middleware.NewChain(
		middleware.NewAPIKeyGuard(),
		middleware.NewRateLimitGuard(),
		middleware.NewBudgetGuard(),
	)

	// ---- 告警 ----
	var alerts model.AlertEngine
	if cfg.Alerts.Enabled {
		engine := alert.New(cfg, aggregator, logger)
		// 管理接口 PUT 规则后持久化回 config.json（否则重启丢规则）
		engine.SetPersist(func(rules []model.AlertRule) error {
			current := store.Current()
			next := *current
			next.Alerts = current.Alerts
			next.Alerts.Rules = rules
			return config.Save(*configPath, &next)
		})
		alerts = engine
		alerts.Start()
		logger.Printf("告警引擎已启动：%d 条规则 / %d 个通知渠道", len(cfg.Alerts.Rules), len(cfg.Alerts.Notifiers))
	} else {
		logger.Printf("告警引擎未启用（alerts.enabled=false）")
	}

	// ---- 代理 ----
	handler := proxy.New(proxy.Options{
		Config:     store,
		Providers:  providers,
		Breakers:   breakers,
		Guards:     guards,
		Metrics:    registry,
		Aggregator: aggregator,
		Tracer:     tracer,
		Alerts:     alerts,
		Version:    Version,
	})

	// ---- 热更新 ----
	reload := func(next *model.Config) error {
		next.WithDefaults()
		if err := config.Validate(next); err != nil {
			return err
		}
		if err := config.Save(*configPath, next); err != nil {
			return err
		}
		if err := providers.Reload(next); err != nil {
			return err
		}
		store.Store(next)
		handler.SetConfig(next)
		if alerts != nil {
			// 优先用整份配置热更新（含通知渠道/间隔/冷却）；不支持时退回只换规则
			if applier, ok := alerts.(interface{ ApplyConfig(*model.Config) error }); ok {
				if err := applier.ApplyConfig(next); err != nil {
					logger.Printf("告警配置热更新失败（保留旧配置）：%v", err)
				}
			} else if err := alerts.ReplaceRules(next.Alerts.Rules); err != nil {
				logger.Printf("告警规则热更新失败（保留旧规则）：%v", err)
			}
		}
		logger.Printf("配置已热更新：%d 供应商 / %d 模型 / %d 路由", len(next.Providers), len(next.Models), len(next.Routes))
		return nil
	}

	// ---- 管理接口 ----
	adminHandler := admin.New(admin.Options{
		ConfigPath: *configPath,
		Config:     store,
		Reload:     reload,
		Providers:  providers,
		Breakers:   breakers,
		Aggregator: aggregator,
		Tracer:     tracer,
		Alerts:     alerts,
		Metrics:    registry,
		Version:    Version,
		StartedAt:  startedAt,
	})

	// ---- HTTP 服务 ----
	publicMux := http.NewServeMux()
	publicMux.Handle("/v1/", handler)
	publicMux.Handle("/healthz", handler)
	publicMux.Handle("/readyz", handler)
	publicMux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		registry.WritePrometheus(w)
	})

	adminMux := http.NewServeMux()
	adminMux.Handle("/admin/api/", adminHandler)
	adminMux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		registry.WritePrometheus(w)
	})
	adminMux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	// 未单独配置管理端口时，管理接口挂到主端口（路径不冲突即可）
	serveAdminSeparately := strings.TrimSpace(cfg.AdminListen) != "" && cfg.AdminListen != cfg.Listen
	if !serveAdminSeparately {
		publicMux.Handle("/admin/api/", adminHandler)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	servers := make([]*http.Server, 0, 2)
	publicServer := &http.Server{Addr: cfg.Listen, Handler: publicMux, ReadHeaderTimeout: 10 * time.Second}
	servers = append(servers, publicServer)
	go func() {
		logger.Printf("网关入口监听 %s（OpenAI 兼容：%s/v1）", cfg.Listen, cfg.PublicURL)
		if err := publicServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("入口服务异常：%v", err)
		}
	}()
	if serveAdminSeparately {
		adminServer := &http.Server{Addr: cfg.AdminListen, Handler: adminMux, ReadHeaderTimeout: 10 * time.Second}
		servers = append(servers, adminServer)
		go func() {
			logger.Printf("管理接口监听 %s（/admin/api/*、/metrics）", cfg.AdminListen)
			if err := adminServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				logger.Fatalf("管理服务异常：%v", err)
			}
		}()
	}

	// ---- 后台巡检：探活 / 熔断扫描 / 指标刷新 ----
	go backgroundLoop(ctx, logger, store, providers, breakers, aggregator, registry, alerts)

	// ---- 优雅退出 ----
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	logger.Printf("收到退出信号，正在关闭……")
	if alerts != nil {
		alerts.Stop()
	}
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	for _, srv := range servers {
		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Printf("关闭 %s 失败：%v", srv.Addr, err)
		}
	}
	logger.Printf("已退出")
}

// backgroundLoop 每 15 秒做一轮巡检：探活供应商、扫描熔断器、刷新 gauge。
func backgroundLoop(
	ctx context.Context,
	logger *log.Logger,
	store *configStore,
	providers *provider.Registry,
	breakers *middleware.Breakers,
	aggregator *metrics.Aggregator,
	registry *metrics.Registry,
	alerts model.AlertEngine,
) {
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	probe := func() {
		cfg := store.Current()
		if cfg == nil {
			return
		}
		results := providers.ProbeAll(ctx)
		down := int64(0)
		for _, res := range results {
			up := 1.0
			if !res.Healthy {
				up = 0
				down++
			}
			registry.Set("tsukiyo_gateway_provider_up", map[string]string{"provider": res.Name}, up)
			registry.Observe("tsukiyo_gateway_provider_probe_seconds",
				map[string]string{"provider": res.Name}, float64(res.LatencyMS)/1000)
		}
		aggregator.SetProvidersDown(down)

		breakers.Sweep()
		open := int64(0)
		for modelID, state := range breakers.Snapshot() {
			value := 0.0
			switch state {
			case "open":
				value, open = 2, open+1
			case "half_open":
				value = 1
			}
			registry.Set("tsukiyo_gateway_breaker_state", map[string]string{"model": modelID}, value)
		}
		aggregator.SetBreakerOpen(open)

		registry.Set("tsukiyo_gateway_inflight", nil, float64(aggregator.Inflight()))
		if alerts != nil {
			registry.Set("tsukiyo_gateway_alerts_firing", nil, float64(alerts.Overview().Firing))
		}
		if down > 0 {
			logger.Printf("探活：%d 个供应商不可用", down)
		}
	}
	probe()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			probe()
		}
	}
}

// registerMetricHelp 登记指标说明（/metrics 的 # HELP）。
func registerMetricHelp(r *metrics.Registry) {
	r.Help("tsukiyo_gateway_requests_total", "网关处理的请求总数（按路由/模型/供应商/状态码）")
	r.Help("tsukiyo_gateway_request_duration_seconds", "请求总耗时（秒）")
	r.Help("tsukiyo_gateway_ttft_seconds", "流式首字延迟（秒）")
	r.Help("tsukiyo_gateway_tokens_total", "消耗的 token 数（prompt/completion）")
	r.Help("tsukiyo_gateway_cost_usd_total", "按模型累计的费用（美元）")
	r.Help("tsukiyo_gateway_inflight", "当前在途请求数")
	r.Help("tsukiyo_gateway_breaker_state", "熔断器状态：0=closed 1=half_open 2=open")
	r.Help("tsukiyo_gateway_provider_up", "供应商探活结果：1=up 0=down")
	r.Help("tsukiyo_gateway_ratelimit_rejected_total", "因限流被拒绝的请求数")
	r.Help("tsukiyo_gateway_budget_rejected_total", "因预算被拒绝的请求数")
	r.Help("tsukiyo_gateway_alerts_firing", "当前 firing 的告警数")
	r.Help("tsukiyo_gateway_provider_probe_seconds", "供应商探活耗时（秒）")
}