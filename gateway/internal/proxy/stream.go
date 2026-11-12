package proxy

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// sseBufferSize SSE 逐行读取的缓冲大小（够放一行超长 tool_calls delta）。
const sseBufferSize = 32 * 1024

// streamResult 一次流式转发的统计结果。
type streamResult struct {
	// TTFTMS 首字节延迟（毫秒）
	TTFTMS int64
	// ResponseBytes 已转发字节数
	ResponseBytes int
	// Usage 流内解析到的 usage（HasUsage=false 时由调用方估算）
	Usage model.Usage
	// HasUsage 上游是否真的返回了 usage
	HasUsage bool
	// CompletionText 累积的模型输出文本（估算 token 用）
	CompletionText string
	// StreamError 流内 data 块里的 error 摘要
	StreamError string
	// ReadErr 读取上游失败（不含 io.EOF）
	ReadErr error
	// WriteErr 写客户端失败（通常是客户端断开）
	WriteErr error
}

// sseAccumulator 解析 SSE 流里的 usage / 输出文本 / 错误。
type sseAccumulator struct {
	usage           model.Usage
	hasUsage        bool
	completion      bytes.Buffer
	streamErrorText string
}

// Feed 处理一行 SSE 数据（非 data: 行与 [DONE] 直接忽略）。
func (a *sseAccumulator) Feed(line []byte) {
	line = bytes.TrimRight(line, "\r\n")
	if len(line) == 0 {
		return
	}
	if !bytes.HasPrefix(line, []byte("data:")) {
		return
	}
	payload := bytes.TrimSpace(line[len("data:"):])
	if len(payload) == 0 || bytes.Equal(payload, []byte("[DONE]")) {
		return
	}
	var chunk struct {
		Choices []struct {
			Delta struct {
				Content          json.RawMessage `json:"content"`
				ReasoningContent string          `json:"reasoning_content"`
			} `json:"delta"`
			Text string `json:"text"`
		} `json:"choices"`
		Usage *struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(payload, &chunk); err != nil {
		return
	}
	if len(chunk.Error) > 0 && !bytes.Equal(bytes.TrimSpace(chunk.Error), []byte("null")) {
		a.streamErrorText = truncate(rawText(chunk.Error), 512)
	}
	if chunk.Usage != nil {
		// OpenAI 在最后一个 data 块返回 usage（需 stream_options.include_usage）
		usage := model.Usage{
			PromptTokens:     chunk.Usage.PromptTokens,
			CompletionTokens: chunk.Usage.CompletionTokens,
			TotalTokens:      chunk.Usage.TotalTokens,
		}
		if usage.TotalTokens == 0 {
			usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
		}
		a.usage = usage
		a.hasUsage = true
	}
	for _, c := range chunk.Choices {
		if text := messageText(c.Delta.Content); text != "" {
			a.completion.WriteString(text)
		}
		a.completion.WriteString(c.Delta.ReasoningContent)
		a.completion.WriteString(c.Text)
	}
}

// forwardStream 把上游 SSE 响应逐块转发给客户端，并统计 TTFT / usage。
//
// 关键点：
//   - 每行写入后立即 Flush，否则客户端会一直等到整段结束（Nginx 下体验极差）；
//   - 不整体缓冲，长回答也不会爆内存；
//   - 客户端断开（Write 失败）时立刻停止，避免继续占用上游连接。
func (h *Handler) forwardStream(w http.ResponseWriter, resp *http.Response, startedAt time.Time) streamResult {
	var result streamResult
	setSSEHeaders(w)
	w.WriteHeader(http.StatusOK)

	flusher, _ := w.(http.Flusher)
	if flusher != nil {
		flusher.Flush()
	}

	reader := bufio.NewReaderSize(resp.Body, sseBufferSize)
	var acc sseAccumulator
	firstByte := true

	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			if firstByte {
				result.TTFTMS = ceilMillis(time.Since(startedAt))
				firstByte = false
			}
			n, werr := w.Write(line)
			result.ResponseBytes += n
			if werr != nil {
				result.WriteErr = werr
				break
			}
			if flusher != nil {
				flusher.Flush()
			}
			acc.Feed(line)
		}
		if err != nil {
			if !errors.Is(err, io.EOF) {
				result.ReadErr = err
			}
			break
		}
	}

	result.Usage = acc.usage
	result.HasUsage = acc.hasUsage
	result.CompletionText = acc.completion.String()
	result.StreamError = acc.streamErrorText
	return result
}

// setSSEHeaders 设置流式响应头。
//
// X-Accel-Buffering: no 是给 Nginx 的：禁用它的响应缓冲，否则 flush 无效。
func setSSEHeaders(w http.ResponseWriter) {
	header := w.Header()
	header.Set("Content-Type", "text/event-stream; charset=utf-8")
	header.Set("Cache-Control", "no-cache")
	header.Set("Connection", "keep-alive")
	header.Set("X-Accel-Buffering", "no")
}

// isStreamResponse 判断这次上游响应是否按 SSE 转发。
//
// 请求要的是流、上游也回了 event-stream → 流式；
// 请求要流但上游回了 JSON（部分中转站不支持流）→ 按非流式透传，别把 JSON 当 SSE 拆。
func isStreamResponse(resp *http.Response, requested bool) bool {
	if resp == nil {
		return false
	}
	contentType := resp.Header.Get("Content-Type")
	if hasEventStream(contentType) {
		return true
	}
	if !requested {
		return false
	}
	return contentType == ""
}

// hasEventStream 判断 Content-Type 是否为 SSE。
func hasEventStream(contentType string) bool {
	return strings.Contains(strings.ToLower(contentType), "text/event-stream")
}

// truncate 截断字符串（按字节，用于错误摘要）。
func truncate(s string, limit int) string {
	if limit <= 0 || len(s) <= limit {
		return s
	}
	return s[:limit] + "..."
}

// ceilMillis 把时长向上取整到毫秒。
//
// 为什么不用截断：本地/同机上游的首字常在 1ms 内到达，截断会得到 0，
// 于是 ttft_ms 指标与告警都看不到这些「极快」样本；向上取整保证 TTFT 有值。
func ceilMillis(d time.Duration) int64 {
	if d <= 0 {
		return 0
	}
	ms := d.Milliseconds()
	if d > time.Duration(ms)*time.Millisecond {
		ms++
	}
	return ms
}