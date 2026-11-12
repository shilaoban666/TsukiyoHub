package alert

// 钉钉群机器人渠道：markdown 消息 + 可选「加签」。
//
// 请求体：
//
//	{"msgtype":"markdown","markdown":{"title":"...","text":"..."}}
//
// 加签算法（钉钉官方）：
//
//	stringToSign = timestamp + "\n" + secret           // timestamp 是毫秒时间戳
//	sign         = base64(HmacSHA256(key=secret, stringToSign))
//	POST url?timestamp=<ts>&sign=<urlencode(sign)>
//
// 注意：钉钉对「HTTP 200 但 errcode != 0」也会返回错误（例如 sign 不匹配 310000），
// 所以这里解析响应体里的 errcode，不能只看状态码。

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/url"
	"strconv"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// dingtalkMarkdown 钉钉 markdown 消息体。
type dingtalkMarkdown struct {
	Title string `json:"title"`
	Text  string `json:"text"`
}

// dingtalkPayload 钉钉机器人请求体（只建模 markdown 消息）。
type dingtalkPayload struct {
	MsgType  string           `json:"msgtype"`
	Markdown dingtalkMarkdown `json:"markdown"`
}

// dingtalkResponse 钉钉机器人响应（errcode=0 才是成功）。
type dingtalkResponse struct {
	ErrCode *int   `json:"errcode"`
	ErrMsg  string `json:"errmsg"`
}

// dingtalkNotifier 钉钉群机器人渠道。
type dingtalkNotifier struct {
	name   string
	url    string
	secret string
}

// Name 渠道名。
func (d *dingtalkNotifier) Name() string { return d.name }

// Type 渠道类型。
func (d *dingtalkNotifier) Type() string { return "dingtalk" }

// Send 发送 markdown 消息；配置了 secret 时自动加签。
func (d *dingtalkNotifier) Send(ctx context.Context, n Notification) error {
	target, err := dingtalkTarget(d.url, d.secret, time.Now())
	if err != nil {
		return err
	}
	title := notificationTitle(n)
	payload := dingtalkPayload{
		MsgType: "markdown",
		Markdown: dingtalkMarkdown{
			Title: title,
			Text:  NotificationMarkdown(n),
		},
	}
	body, status, err := PostJSON(ctx, HTTPClient, target, nil, payload)
	if err != nil {
		if status > 0 {
			return fmt.Errorf("钉钉通知失败（HTTP %d）%s", status, bodySnippet(body))
		}
		return fmt.Errorf("钉钉通知失败：%w", err)
	}
	// 钉钉业务错误：HTTP 200 + errcode != 0
	var resp dingtalkResponse
	if json.Unmarshal(body, &resp) == nil && resp.ErrCode != nil && *resp.ErrCode != 0 {
		return fmt.Errorf("钉钉返回错误 errcode=%d errmsg=%s", *resp.ErrCode, resp.ErrMsg)
	}
	return nil
}

// dingtalkTarget 按需给 URL 追加 timestamp/sign 查询参数。
//
// secret 为空时原样返回；已有查询参数时用 & 连接（用 net/url 重排，
// 保证 base64 里的 + / = 都被正确转义）。
func dingtalkTarget(rawURL, secret string, now time.Time) (string, error) {
	if strings.TrimSpace(secret) == "" {
		return rawURL, nil
	}
	timestamp := now.UnixMilli()
	sign, err := dingtalkSign(secret, timestamp)
	if err != nil {
		return "", err
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", fmt.Errorf("钉钉 url 非法：%w", err)
	}
	query := u.Query()
	query.Set("timestamp", strconv.FormatInt(timestamp, 10))
	query.Set("sign", sign)
	u.RawQuery = query.Encode()
	return u.String(), nil
}

// dingtalkSign 计算钉钉加签串：base64(HmacSHA256(secret, timestamp+"\n"+secret))。
func dingtalkSign(secret string, timestampMS int64) (string, error) {
	stringToSign := strconv.FormatInt(timestampMS, 10) + "\n" + secret
	mac := hmac.New(sha256.New, []byte(secret))
	if _, err := mac.Write([]byte(stringToSign)); err != nil {
		return "", fmt.Errorf("钉钉加签失败：%w", err)
	}
	return base64.StdEncoding.EncodeToString(mac.Sum(nil)), nil
}

func init() {
	Register("dingtalk", func(cfg model.NotifierConfig, logger *log.Logger) (Notifier, error) {
		rawURL := strings.TrimSpace(cfg.URL)
		if rawURL == "" {
			return nil, errors.New("dingtalk 渠道缺少 url（机器人 webhook 地址）")
		}
		if cfg.Name == "" {
			cfg.Name = "dingtalk"
		}
		return &dingtalkNotifier{name: cfg.Name, url: rawURL, secret: strings.TrimSpace(cfg.Secret)}, nil
	})
}
