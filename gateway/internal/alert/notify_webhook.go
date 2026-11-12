package alert

// webhook 渠道：POST JSON 到任意 HTTP 端点。
//
// 载荷格式（与契约一致）：
//
//	{
//	  "event":   { AlertEvent 的完整字段，status = firing / resolved },
//	  "rule":    { AlertRule 的完整字段，便于接收端按规则路由 },
//	  "gateway": "http://127.0.0.1:8081"
//	}
//
// 自定义 headers 用于鉴权（例如 X-Token / Authorization）；2xx 视为成功。

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"

	"tsukiyohub/gateway/internal/model"
)

// webhookPayload webhook 渠道的请求体。
type webhookPayload struct {
	Event   model.AlertEvent `json:"event"`
	Rule    model.AlertRule  `json:"rule"`
	Gateway string           `json:"gateway"`
}

// webhookNotifier 通用 webhook 渠道。
type webhookNotifier struct {
	name    string
	url     string
	headers map[string]string
}

// Name 渠道名。
func (w *webhookNotifier) Name() string { return w.name }

// Type 渠道类型。
func (w *webhookNotifier) Type() string { return "webhook" }

// Send POST JSON 载荷；非 2xx 返回错误（引擎记日志，不影响其它渠道）。
func (w *webhookNotifier) Send(ctx context.Context, n Notification) error {
	payload := webhookPayload{Event: n.Event, Rule: n.Rule, Gateway: n.Gateway}
	body, status, err := PostJSON(ctx, HTTPClient, w.url, w.headers, payload)
	if err != nil {
		if status > 0 {
			return fmt.Errorf("webhook 通知失败（HTTP %d）%s", status, bodySnippet(body))
		}
		return fmt.Errorf("webhook 通知失败：%w", err)
	}
	return nil
}

func init() {
	Register("webhook", func(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error) {
		url := strings.TrimSpace(cfg.URL)
		if url == "" {
			return nil, errors.New("webhook 渠道缺少 url")
		}
		headers := make(map[string]string, len(cfg.Headers))
		for k, v := range cfg.Headers {
			headers[k] = v
		}
		if cfg.Name == "" {
			cfg.Name = "webhook"
		}
		return &webhookNotifier{name: cfg.Name, url: url, headers: headers}, nil
	})
}
