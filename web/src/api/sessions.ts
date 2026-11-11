/**
 * 会话接口。
 *
 * 后端契约：
 * - `GET /api/sessions?user_id=&limit=&offset=` → `{items: SessionInfo[], total}`
 * - `POST /api/sessions` body `{user_id, channel, title}` → SessionInfo
 * - `PATCH /api/sessions/{id}` body `{title}` → SessionInfo
 * - `DELETE /api/sessions/{id}` → `{ok: true}`
 * - `GET /api/sessions/{id}/messages?limit=&before=` → `{session, messages: [...]}`
 *   `messages` 按时间**正序**（老 → 新），`before` 传当前最老一条的 message_id 可翻更早。
 */
import { http } from './client'
import type { ListResult, SessionInfo, SessionMessagesResult } from '@/types/api'

/** 列表接口兜底：后端约定是 `{items,total}`，但也兼容裸数组，避免版本差异导致白屏 */
export function normalizeList<T>(data: unknown): ListResult<T> {
  if (Array.isArray(data)) return { items: data as T[], total: data.length }
  const obj = (data ?? {}) as { items?: T[]; total?: number; data?: T[] }
  if (Array.isArray(obj.items)) return { items: obj.items, total: obj.total ?? obj.items.length }
  if (Array.isArray(obj.data)) return { items: obj.data, total: obj.total ?? obj.data.length }
  return { items: [], total: 0 }
}

export async function listSessions(params: {
  user_id?: string
  limit?: number
  offset?: number
} = {}): Promise<ListResult<SessionInfo>> {
  const data = await http.get<unknown>('/sessions', {
    user_id: params.user_id || undefined,
    limit: params.limit ?? 50,
    offset: params.offset ?? 0,
  })
  return normalizeList<SessionInfo>(data)
}

/** 新建会话（后端可能返回空标题，由前端补默认名） */
export async function createSession(payload: {
  user_id?: string | null
  channel?: string
  title?: string
} = {}): Promise<SessionInfo> {
  return http.post<SessionInfo>('/sessions', {
    user_id: payload.user_id ?? null,
    channel: payload.channel ?? 'web',
    title: payload.title ?? '',
  })
}

/** 重命名会话（返回更新后的 SessionInfo） */
export async function renameSession(id: string, title: string): Promise<SessionInfo> {
  return http.patch<SessionInfo>(`/sessions/${encodeURIComponent(id)}`, { title }, { silent: true })
}

/** 删除会话 */
export async function deleteSession(id: string): Promise<void> {
  await http.del<unknown>(`/sessions/${encodeURIComponent(id)}`)
}

/**
 * 拉取历史消息。
 * @param before 传当前最老一条 message_id 表示「再往前翻一页」
 *
 * 顺序修正：契约说 `messages` 是**时间正序**（老 → 新），但实测后端可能按倒序返回，
 * 且同一轮的 user / assistant 时间戳相同（同一秒）。这里做一次稳定排序：
 * 先按 `created_at` 升序，时间相同时「用户提问」排在「助手回答」前面。
 * 这样无论后端正序还是倒序，渲染出来都是正常对话。
 */
export async function listMessages(
  id: string,
  params: { limit?: number; before?: string } = {},
): Promise<SessionMessagesResult> {
  const data = await http.get<unknown>(`/sessions/${encodeURIComponent(id)}/messages`, {
    limit: params.limit ?? 50,
    before: params.before || undefined,
  })
  const obj = (data ?? {}) as Partial<SessionMessagesResult>
  const messages = Array.isArray(obj.messages) ? [...obj.messages] : []
  messages.sort((a, b) => {
    const ta = Date.parse(a.created_at || '') || 0
    const tb = Date.parse(b.created_at || '') || 0
    if (ta !== tb) return ta - tb
    if (a.role === b.role) return 0
    return a.role === 'user' ? -1 : 1
  })
  return {
    session: (obj.session ?? { id, title: '' }) as SessionInfo,
    messages,
  }
}