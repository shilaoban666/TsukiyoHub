/**
 * 聊天接口：非流式 + SSE 流式。
 *
 * **流式必须走 fetch**（不是 axios）：需要 POST body + 自定义头 + 手动中止 + ReadableStream 增量读取。
 * 后端契约（见 kernel-contract.md 的「SSE 事件」表）：
 *
 * | event    | data                                   | 语义                          |
 * |----------|----------------------------------------|-------------------------------|
 * | status   | thinking / searching / generating       | 状态条                         |
 * | progress | 纯文本                                  | 「正在查找《xx》等 N 份资料」   |
 * | delta    | **当前累积全文**                        | 整段替换（不是增量！）          |
 * | reset    | 空                                      | 换了一轮生成，清空重来          |
 * | tool     | {"name","ok"}                           | 技能调用                       |
 * | meta     | ChatResponse 的子集                     | 引用 / grounded / usage        |
 * | error    | {"message"}                             | 失败，展示后结束               |
 * | done     | [DONE]                                  | 结束                           |
 *
 * 服务端每 15 秒发 `: ping` 注释帧；解析器会忽略。
 */
import { API_BASE, ApiError, getApiKey, http } from './client'
import { createSSEParser, parseSSEData } from '@/utils/sse'
import type { ChatMeta, ChatRequest, ChatResponse } from '@/types/api'

/** 非流式对话（兜底 / 脚本调试用） */
export function chat(payload: ChatRequest, opt?: { signal?: AbortSignal; silent?: boolean }) {
  return http.post<ChatResponse>('/chat', { ...payload, stream: false }, opt)
}

export interface ChatStreamHandlers {
  /** 收到 reset：清空当前累积文本 */
  onReset?: () => void
  /** 状态：thinking / searching / generating */
  onStatus?: (phase: string) => void
  /** 过程文案：正在查找《xx》等 N 份资料 */
  onProgress?: (text: string) => void
  /** 正文更新：**累积全文**，直接整段替换 */
  onDelta?: (fullText: string) => void
  /** 技能调用 */
  onTool?: (tool: { name: string; ok?: boolean }) => void
  /** 结束后元信息（引用、grounded、usage 等） */
  onMeta?: (meta: ChatMeta) => void
  /** 后端 error 帧（流已开始后的失败只能走这条路径） */
  onError?: (text: string) => void
  /** 正常结束 */
  onDone?: () => void
}

/** 空闲超时：服务端每 15s ping，超过 60s 没有任何字节视为连接已死 */
const IDLE_TIMEOUT_MS = 60_000

/**
 * 发起流式对话。
 *
 * @returns 结束时最后一次 meta（可能为 null，例如后端没发 meta）
 * @throws  HTTP 非 2xx / 网络错误 / 空闲超时 时抛错；被 AbortController 中止时抛 DOMException(AbortError)
 */
export async function streamChat(
  payload: ChatRequest,
  handlers: ChatStreamHandlers,
  signal: AbortSignal,
): Promise<ChatMeta | null> {
  const key = getApiKey()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  if (key) headers.Authorization = `Bearer ${key}`

  let res: Response
  try {
    res = await fetch(`${API_BASE}/chat/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ ...payload, stream: true }),
      signal,
    })
  } catch (e) {
    if (signal.aborted) throw e
    throw new ApiError('无法连接后端，请确认服务已启动（默认 http://127.0.0.1:8080）', {
      isNetwork: true,
    })
  }

  if (!res.ok) {
    // 流还没开始就失败：此时是标准 HTTP 错误，body 是 {"detail": "..."}
    const text = await res.text().catch(() => '')
    let detail = text
    try {
      const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
      else if (typeof parsed.message === 'string') detail = parsed.message
    } catch {
      /* body 不是 JSON，直接用原文 */
    }
    throw new ApiError(detail || `请求失败（HTTP ${res.status}）`, { status: res.status, detail })
  }
  if (!res.body) throw new ApiError('当前浏览器不支持流式响应（ReadableStream 不可用）')

  let meta: ChatMeta | null = null
  let finished = false
  let serverError = ''

  /**
   * 累积文本。后端 `delta` 有两种可能语义（历史实现不一致，前端必须都能吃下）：
   * 1. **增量**：每次只给新片段（当前 server 的实现，`event: reset` 表示重新开始）；
   * 2. **累积全文**：每次给到目前为止的完整文本（旧实现）。
   *
   * 判定方式：新片段以「已有全文」开头 → 说明是累积全文，直接替换；否则按增量拼接。
   * 对外统一成「累积全文」传给 `onDelta`，上层（store / 组件）就不用关心两种协议。
   */
  let accumulated = ''

  const asText = (data: unknown): string => {
    if (typeof data === 'string') return data
    if (data && typeof data === 'object') {
      const obj = data as { text?: unknown; content?: unknown; phase?: unknown; message?: unknown }
      for (const key of ['text', 'content', 'phase', 'message'] as const) {
        const value = obj[key]
        if (typeof value === 'string') return value
      }
      return ''
    }
    return data == null ? '' : String(data)
  }

  const feed = createSSEParser((frame) => {
    switch (frame.event) {
      case 'reset':
        accumulated = ''
        handlers.onReset?.()
        break

      case 'status': {
        // 契约是纯文本；兼容后端发 {"phase":"searching"} 的情况
        const phase = asText(parseSSEData(frame.data))
        if (phase) handlers.onStatus?.(phase)
        break
      }

      case 'progress': {
        const text = asText(parseSSEData(frame.data))
        if (text) handlers.onProgress?.(text)
        break
      }

      case 'delta': {
        const piece = asText(parseSSEData(frame.data))
        if (!piece) break
        /**
         * 契约：`delta` = **当前累积全文**，整段替换即可（后端 40ms 节流推送）。
         * 这里仍然保留「增量」兼容分支，因为：
         * - 老实现（以及 OpenAI 兼容接口）推的是增量片段；
         * - 两种语义用「新文本是否以已有全文为前缀 + 更长」区分，代价只有一次字符串比较。
         */
        if (!accumulated) accumulated = piece
        else if (piece === accumulated) {
          /* 重复帧：忽略，避免累积语义下把全文拼两遍 */
        } else if (piece.length > accumulated.length && piece.startsWith(accumulated)) {
          accumulated = piece // 累积全文 → 整段替换
        } else {
          accumulated += piece // 增量片段 → 追加
        }
        handlers.onDelta?.(accumulated)
        break
      }

      case 'tool': {
        const data = parseSSEData<{ name?: string; ok?: boolean }>(frame.data)
        if (data && typeof data === 'object' && data.name) {
          handlers.onTool?.({ name: data.name, ok: data.ok })
        }
        break
      }

      case 'meta': {
        const data = parseSSEData<ChatMeta>(frame.data)
        if (data && typeof data === 'object') {
          meta = { ...(meta ?? {}), ...data }
          handlers.onMeta?.(data)
        }
        break
      }

      case 'error': {
        const data = parseSSEData<{ message?: string; detail?: string }>(frame.data)
        serverError =
          (typeof data === 'string' ? data : (data?.message ?? data?.detail)) || '生成失败'
        handlers.onError?.(serverError)
        break
      }

      case 'done': {
        finished = true
        // 契约是 [DONE]；若后端改成发完整 ChatResponse JSON，这里也顺手吸收
        if (frame.data.trim() !== '[DONE]') {
          const data = parseSSEData<ChatMeta>(frame.data)
          if (data && typeof data === 'object') {
            meta = { ...(meta ?? {}), ...data }
            handlers.onMeta?.(data)
          }
        }
        handlers.onDone?.()
        break
      }

      case 'ping':
        break

      default:
        // 未知事件不抛错：后端加新事件时老前端仍然能用
        break
    }
  })

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let lastByteAt = Date.now()

  // 空闲看门狗：网关掐流时 reader.read() 可能永远挂着，必须自己兜底
  const watchdog = window.setInterval(() => {
    if (Date.now() - lastByteAt > IDLE_TIMEOUT_MS) {
      void reader.cancel('idle timeout').catch(() => undefined)
    }
  }, 5_000)

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      lastByteAt = Date.now()
      feed(decoder.decode(value, { stream: true }))
      // 服务端发 error 帧后通常立刻关闭连接；提前退出，避免把残留帧当正文
      if (serverError) break
    }
    feed(decoder.decode(), true)
  } finally {
    window.clearInterval(watchdog)
    reader.releaseLock?.()
  }

  if (serverError) throw new ApiError(serverError, { detail: serverError })
  if (!finished && !signal.aborted) {
    throw new ApiError('连接中断：没有收到结束事件（done），回答可能不完整')
  }
  return meta
}