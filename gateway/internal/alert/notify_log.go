package alert

// log 渠道：把告警写进进程日志（标准库 log）。
//
// 这是最小可用渠道，配置里只要有 {"type":"log"} 就能工作，适合：
//   - 本地开发验证「告警到底有没有触发」；
//   - 作为兜底渠道，防止 webhook 全挂时消息彻底消失（消息至少留在日志里）。

import (
	"context"
	"log"
	"sync"

	"tsukiyohub/gateway/internal/model"
)

// logNotifier 标准日志渠道。
type logNotifier struct {
	name   string
	logger *log.Logger

	// mu 保护 logger 的 Write：自定义 log.Logger 不保证并发安全。
	mu sync.Mutex
}

// Name 渠道名。
func (l *logNotifier) Name() string { return l.name }

// Type 渠道类型。
func (l *logNotifier) Type() string { return "log" }

// Send 输出一行结构化文本：status / severity / rule / metric / value / threshold / at / gateway。
func (l *logNotifier) Send(ctx context.Context, n Notification) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.logger.Printf("%s", notificationLine(n))
	return nil
}

func init() {
	Register("log", func(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error) {
		if logger == nil {
			logger = log.Default()
		}
		if cfg.Name == "" {
			cfg.Name = "log"
		}
		return &logNotifier{name: cfg.Name, logger: logger}, nil
	})
}
