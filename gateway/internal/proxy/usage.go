package proxy

import (
	"encoding/json"
	"strings"

	"tsukiyohub/gateway/internal/model"
)

// EstimateTokens 估算一段文本的 token 数。
//
// 经验规则（无需 tokenizer，够告警与预算兜底用）：
//   - 中文/日文/韩文等表意文字：1 字 ≈ 1 token；
//   - 其余字符（英文、数字、符号）：4 字符 ≈ 1 token；
//   - 空白字符不计。
func EstimateTokens(text string) int {
	if text == "" {
		return 0
	}
	var cjk, rest int
	for _, r := range text {
		switch {
		case isCJK(r):
			cjk++
		case r == ' ' || r == '\n' || r == '\r' || r == '\t':
			// 空白不计 token
		default:
			rest++
		}
	}
	tokens := cjk + (rest+3)/4
	if tokens == 0 {
		tokens = 1
	}
	return tokens
}

// isCJK 判断字符是否属于中日韩表意文字/假名/全角符号。
func isCJK(r rune) bool {
	return (r >= 0x3000 && r <= 0x303F) || // CJK 标点
		(r >= 0x3040 && r <= 0x30FF) || // 日文假名
		(r >= 0x3400 && r <= 0x4DBF) || // CJK 扩展 A
		(r >= 0x4E00 && r <= 0x9FFF) || // CJK 基本区
		(r >= 0xF900 && r <= 0xFAFF) || // CJK 兼容
		(r >= 0xFF00 && r <= 0xFFEF) // 全角字符
}

// EstimateChatUsage 上游没返回 usage 时按字符数估算。
func EstimateChatUsage(req *model.ChatRequest, completionText string) model.Usage {
	prompt := 0
	if req != nil {
		for _, m := range req.Messages {
			prompt += EstimateTokens(messageText(m.Content))
			prompt += EstimateTokens(m.Name)
			prompt += 4 // 每条消息的 role/格式开销，粗略补偿
		}
		for _, tool := range req.Tools {
			prompt += EstimateTokens(string(tool))
		}
	}
	completion := EstimateTokens(completionText)
	return model.Usage{
		PromptTokens:     prompt,
		CompletionTokens: completion,
		TotalTokens:      prompt + completion,
		Estimated:        true,
	}
}

// EstimateEmbeddingUsage 向量请求没有 usage 时的估算（只有 input 计入 prompt）。
func EstimateEmbeddingUsage(req *model.EmbeddingRequest) model.Usage {
	prompt := 0
	if req != nil && len(req.Input) > 0 {
		prompt = EstimateTokens(rawText(req.Input))
	}
	return model.Usage{PromptTokens: prompt, TotalTokens: prompt, Estimated: true}
}

// messageText 从消息 content 里提取纯文本。
//
// content 可能是：字符串 / 多模态数组（[{"type":"text","text":"..."}]）/ null。
// 数组形式把所有 text 字段拼起来；解析不出来时退回原始 JSON 文本。
func messageText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return text
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &parts); err == nil {
		var b strings.Builder
		for _, p := range parts {
			b.WriteString(p.Text)
		}
		return b.String()
	}
	return rawText(raw)
}

// rawText 把任意 JSON 值转成可估算的文本（字符串去引号，其它原样）。
func rawText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return text
	}
	return string(raw)
}

// parseUsage 从（非流式）响应体里解析 usage 字段。
func parseUsage(body []byte) (model.Usage, bool) {
	var payload struct {
		Usage *struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(body, &payload); err != nil || payload.Usage == nil {
		return model.Usage{}, false
	}
	usage := model.Usage{
		PromptTokens:     payload.Usage.PromptTokens,
		CompletionTokens: payload.Usage.CompletionTokens,
		TotalTokens:      payload.Usage.TotalTokens,
	}
	if usage.TotalTokens == 0 {
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	}
	return usage, true
}

// completionText 从非流式响应体里取出模型输出文本（用于 usage 估算）。
func completionText(body []byte) string {
	var payload struct {
		Choices []struct {
			Message struct {
				Content          json.RawMessage `json:"content"`
				ReasoningContent string          `json:"reasoning_content"`
			} `json:"message"`
			Text string `json:"text"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return ""
	}
	var b strings.Builder
	for _, c := range payload.Choices {
		b.WriteString(messageText(c.Message.Content))
		b.WriteString(c.Message.ReasoningContent)
		b.WriteString(c.Text)
	}
	return b.String()
}