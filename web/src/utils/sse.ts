/**
 * SSE 帧解析器（纯函数，零依赖，可单测）。
 *
 * 为什么不用 EventSource：
 * - 我们用的是 **POST**（问题体可能很大、需要带 Authorization），EventSource 只能 GET；
 * - 需要 AbortController 精细中止；
 * - 需要在「流结束但没有 done」时给出明确判定。
 *
 * 按 SSE 规范实现的关键点（旧前端只用了 `/^data: (.*)$/m` 正则，多行 data 会被静默截断）：
 * - 帧以**空行**分隔（统一成 `\n\n`，兼容 CRLF）；
 * - `data:` 可以出现多行，按规范用 `\n` 拼接后再交给调用方；
 * - 以 `:` 开头的是注释帧（心跳 `: ping`），直接忽略；
 * - `event:` / `id:` / `retry:` 是字段名，冒号后可能有一个空格。
 */

export interface SSEFrame {
  event: string
  data: string
  id?: string
}

export type SSEFrameHandler = (frame: SSEFrame) => void

/**
 * 创建一个解析器：反复喂字符串分片即可，内部维护残余缓冲。
 * @param onFrame 每解析出一个完整帧回调一次
 * @returns feed(chunk, flush?) —— flush=true 表示流已结束（丢弃不完整残帧并告警）
 */
export function createSSEParser(onFrame: SSEFrameHandler): (chunk: string, flush?: boolean) => void {
  let buf = ''
  return (chunk: string, flush = false) => {
    buf += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

    for (;;) {
      const boundary = buf.indexOf('\n\n')
      if (boundary < 0) break
      const raw = buf.slice(0, boundary)
      buf = buf.slice(boundary + 2)
      const frame = parseFrame(raw)
      if (frame) onFrame(frame)
    }

    if (flush) {
      // 流结束仍残留半帧：说明后端没有以空行收尾。丢掉它而不是解析半截 JSON，
      // 否则会在 UI 上报一个看不懂的 SyntaxError。
      buf = ''
    }
  }
}

/** 把一段原始帧文本解析成 { event, data, id }；无 data 字段的帧（纯注释）返回 null */
function parseFrame(raw: string): SSEFrame | null {
  let event = 'message'
  let id: string | undefined
  const data: string[] = []

  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue // 空行 / 心跳注释
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
    else if (field === 'id') id = value
    // retry 不使用：重连策略由前端业务决定
  }

  // 注释帧（`: ping`）没有 data，不该触发业务回调
  if (!data.length) return null
  return { event, data: data.join('\n'), id }
}

/**
 * 尽力把 data 解析成 JSON；不是 JSON 就原样返回（progress / status 是纯文本）。
 *
 * 注意后端的两种写法都要吃下：
 * - `event: delta` 的 data 有时是 `{"text":"..."}`，有时是 **JSON 字符串字面量** `"..."`；
 * - `event: progress` 的 data 是裸中文文本。
 * 只对「看起来像 JSON」的内容尝试 parse，避免把 `123` 这种纯文本误判成数字。
 */
export function parseSSEData<T = unknown>(data: string): T | string {
  const text = data.trim()
  if (!text) return ''
  const first = text[0]
  const looksJson =
    first === '{' || first === '[' || first === '"' || text === 'null' || text === 'true' || text === 'false'
  if (!looksJson) return text
  try {
    return JSON.parse(text) as T
  } catch {
    return text
  }
}