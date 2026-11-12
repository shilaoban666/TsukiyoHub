package alert

// slack 渠道：Incoming Webhook，请求体是最简单的 {"text": "..."}。
//
// Slack 的 text 支持轻度 markdown（*粗体* / 换行），这里用纯文本多行，
// 避免不同客户端把 ### 渲染成一堆井号。

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"

	"tsukiyohub/gateway/internal/model"
)

// slackPayload Slack Incoming Webhook 请求体。
type slackPayload struct {
	Text string `json:"text"`
}

// slackNotifier Slack 渠道。
type slackNotifier struct {
	name string
	url  string
}

// Name 渠道名。
func (s *slackNotifier) Name() string { return s.name }

// Type 渠道类型。
func (s *slackNotifier) Type() string { return "slack" }

// Send POST {"text": "..."}。
func (s *slackNotifier) Send(ctx context.Context, n Notification) error {
	payload := slackPayload{Text: NotificationText(n)}
	body, status, err := PostJSON(ctx, HTTPClient, s.url, nil, payload)
	if err != nil {
		if status > 0 {
			return fmt.Errorf("slack 通知失败（HTTP %d）%s", status, bodySnippet(body))
		}
		return fmt.Errorf("slack 通知失败：%w", err)
	}
	return nil
}

func init() {
	Register("slack", func(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error) {
		rawURL := strings.TrimSpace(cfg.URL)
		if rawURL == "" {
			return nil, errors.New("slack 渠道缺少 url（Incoming Webhook 地址）")
		}
		if cfg.Name == "" {
			cfg.Name = "slack"
		}
		return &slackNotifier{name: cfg.Name, url: rawURL}, nil
	})
}
