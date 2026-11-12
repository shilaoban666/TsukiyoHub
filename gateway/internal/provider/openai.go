package provider

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// JoinURL 拼接 base_url 与上游路径。
//
// 规则：去掉 base_url 末尾的 "/"，给 path 补上前导 "/"。
// 例如 base="https://api.openai.com/v1/" path="/chat/completions"
// → "https://api.openai.com/v1/chat/completions"。
func JoinURL(base, path string) string {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if path == "" {
		return base
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return base + path
}

// Do 发起一次上游 POST 请求（OpenAI 兼容）。
//
// path 形如 "/chat/completions"；body 由调用方序列化好（代理要原样透传客户端字段）。
// 自动完成：
//   - Authorization: Bearer <api_key>（azure 类型改用 api-key 头）
//   - 供应商额外 Headers（可覆盖默认头，方便做私有中转鉴权）
//   - URL 拼接、Connect/Header/Total 超时
//
// 返回值是原始 *http.Response，调用方负责读 body 并 Close。
func (r *Registry) Do(ctx context.Context, ep *Endpoint, path string, body []byte) (*http.Response, error) {
	if ep == nil {
		return nil, errors.New("provider: endpoint 为 nil")
	}
	tr, globalTotal := r.transportSnapshot()
	if tr == nil {
		return nil, errors.New("provider: 注册表未初始化")
	}

	url := JoinURL(ep.Provider.BaseURL, path)
	if url == "" {
		return nil, fmt.Errorf("provider: 供应商 %q 的 base_url 为空", ep.Provider.Name)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("provider: 构造请求 %s 失败：%w", url, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	apiKey := strings.TrimSpace(ep.Provider.APIKey)
	if apiKey != "" {
		if strings.EqualFold(ep.Provider.Type, "azure") {
			req.Header.Set("api-key", apiKey)
		} else {
			req.Header.Set("Authorization", "Bearer "+apiKey)
		}
	}
	for k, v := range ep.Provider.Headers {
		if k == "" {
			continue
		}
		req.Header.Set(k, v)
	}

	client := &http.Client{Transport: tr}
	// 单次调用总超时：供应商自己的 timeout_ms 优先，其次用全局 total_ms。
	// 流式请求建议把两者都配成 0（不限），否则长回答会被按时长切断。
	timeout := msDuration(ep.Provider.TimeoutMS)
	if timeout <= 0 {
		timeout = globalTotal
	}
	if timeout > 0 {
		client.Timeout = timeout
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("provider: 调用 %s 失败：%w", url, err)
	}
	return resp, nil
}

// ErrorSnippet 读取上游错误响应的一小段正文用于记录/排障（最多 limit 字节）。
//
// 故意不读完：错误响应可能很大，而且调用方马上要关闭 body。
func ErrorSnippet(resp *http.Response, limit int) string {
	if resp == nil || resp.Body == nil {
		return ""
	}
	if limit <= 0 {
		limit = 2048
	}
	buf := make([]byte, limit)
	n, _ := resp.Body.Read(buf)
	if n <= 0 {
		return ""
	}
	return strings.TrimSpace(string(buf[:n]))
}

// providerTimeout 返回供应商实际生效的调用超时（供应商配置优先，其次 0 表示不限）。
func providerTimeout(p model.Provider) time.Duration {
	return msDuration(p.TimeoutMS)
}