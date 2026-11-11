/**
 * 会话 + 消息 + 流式状态（工作台的核心 store）。
 *
 * 设计取舍：
 * 1. **消息按会话分桶**（`messages: Record<sessionId, UiMessage[]>`）：切换会话不丢已加载数据，
 *    也不会因为列表重排触发整列渲染。
 * 2. **流式只改「正在流式的那一个对象」的 `content` 字段**，绝不整对象/整数组替换——
 *    这是旧前端踩过的坑（`panes.value = {...panes.value}` 会触发全量 diff）。
 * 3. **AbortController 放模块作用域**，不进响应式状态：controller 被 Proxy 包住会出怪问题。
 * 4. `delta` 是**累积全文**，所以渲染时整段替换是幂等的；50ms 节流只是降低渲染频率，
 *    不改变语义（最后一次 content 一定等于服务端最后一次 delta）。
 */
import { defineStore } from 'pinia'
import { streamChat, type ChatStreamHandlers } from '@/api/chat'
import {
  createSession as apiCreateSession,
  deleteSession as apiDeleteSession,
  listMessages as apiListMessages,
  listSessions as apiListSessions,
  renameSession as apiRenameSession,
} from '@/api/sessions'
import { errorText } from '@/api/client'
import type { Citation, MessageRecord, SessionInfo, UiMessage } from '@/types/api'

/** 生成中的运行时状态（不进 store state，避免被响应式代理） */
interface StreamRuntime {
  controller: AbortController | null
  timer: number
  buffer: string
  abortedByUser: boolean
}

const runtime: StreamRuntime = { controller: null, timer: 0, buffer: '', abortedByUser: false }

/** 临时会话 id 前缀：这类 id 不发给后端（后端会自建并回 meta.session_id） */
const TEMP_PREFIX = 'tmp-'

function isTempId(id: string | null): boolean {
  return !!id && id.startsWith(TEMP_PREFIX)
}

function nowIso(): string {
  return new Date().toISOString()
}

function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

/** 抽一个短标题：取问题前 20 个字符 */
function shortTitle(text: string): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > 20 ? `${t.slice(0, 20)}…` : t
}

/** 历史消息 → 渲染层消息 */
function toUiMessage(m: MessageRecord, index: number): UiMessage {
  const citations = m.citations ?? m.extra?.citations ?? []
  return {
    id: m.id || uid(`hist-${index}`),
    role: m.role === 'assistant' ? 'assistant' : 'user',
    content: m.content || '',
    created_at: m.created_at || nowIso(),
    citations: Array.isArray(citations) ? citations : [],
    status: 'done',
    latency_ms: m.latency_ms,
    usage: m.usage,
  }
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    // ---------------- 会话列表 ----------------
    sessions: [] as SessionInfo[],
    sessionsLoading: false,
    sessionsError: '',
    /** 后端是否支持列表接口（false 时说明后端未连接，页面给重试入口） */
    sessionsLoaded: false,

    // ---------------- 消息 ----------------
    activeId: null as string | null,
    messages: {} as Record<string, UiMessage[]>,
    messagesLoading: false,
    messagesError: '',

    // ---------------- 流式 ----------------
    streaming: {
      sessionId: null as string | null,
      messageId: null as string | null,
      /** '' | 'thinking' | 'searching' | 'generating' */
      phase: '',
      /** 「正在查找《xx》等 N 份资料」之类的过程文案 */
      progress: '',
      running: false,
      tool: '' as string,
    },

    // ---------------- 右侧来源面板 ----------------
    /** 当前展示在右侧面板的引用（点击气泡里的引用时写入） */
    panelCitations: [] as Citation[],
    panelMessageId: null as string | null,
    panelOpen: false,
  }),

  getters: {
    /** 当前会话的消息列表 */
    activeMessages(state): UiMessage[] {
      return state.activeId ? (state.messages[state.activeId] ?? []) : []
    },
    activeSession(state): SessionInfo | null {
      return state.sessions.find((s) => s.id === state.activeId) ?? null
    },
    /** 是否有正在生成的回答 */
    busy(state): boolean {
      return state.streaming.running
    },
  },

  actions: {
    // =====================================================================
    // 会话
    // =====================================================================
    /** 按更新时间倒序（最近的会话在最上面） */
    sortSessions() {
      this.sessions.sort((a, b) => {
        const ta = Date.parse(a.updated_at || a.created_at || '') || 0
        const tb = Date.parse(b.updated_at || b.created_at || '') || 0
        return tb - ta
      })
    },

    async loadSessions(opts: { silent?: boolean } = {}) {
      if (this.sessionsLoading) return
      this.sessionsLoading = true
      this.sessionsError = ''
      try {
        const { items } = await apiListSessions({ limit: 100 })
        this.sessions = items.map((s) => ({ ...s, title: s.title || '未命名会话' }))
        this.sortSessions()
        this.sessionsLoaded = true
      } catch (e) {
        this.sessionsError = errorText(e)
        if (!opts.silent) throw e
      } finally {
        this.sessionsLoading = false
      }
    },

    /**
     * 新建会话。后端不可用时降级为本地临时会话：
     * 用户仍然可以先提问，首条回答的 meta.session_id 回来后再绑定真实 id。
     */
    async createSession(title = '新会话'): Promise<SessionInfo> {
      let session: SessionInfo
      try {
        const created = await apiCreateSession({ title })
        session = {
          ...created,
          id: created?.id || uid('s'),
          title: created?.title || title,
          created_at: created?.created_at || nowIso(),
          updated_at: created?.updated_at || nowIso(),
          message_count: created?.message_count ?? 0,
          last_message: created?.last_message ?? '',
        }
      } catch {
        // 后端未连接：本地占位，等首条消息的 meta 回来再替换 id
        session = {
          id: uid('tmp'),
          title,
          channel: 'web',
          created_at: nowIso(),
          updated_at: nowIso(),
          message_count: 0,
          last_message: '',
        }
      }
      this.sessions.unshift(session)
      this.messages[session.id] = []
      this.activeId = session.id
      this.sortSessions()
      return session
    },

    async renameSession(id: string, title: string) {
      const next = title.trim()
      if (!next) return
      const target = this.sessions.find((s) => s.id === id)
      const prev = target?.title ?? ''
      if (target) target.title = next // 乐观更新
      if (isTempId(id)) return
      try {
        const updated = await apiRenameSession(id, next)
        if (target && updated?.title) target.title = updated.title
      } catch (e) {
        if (target) target.title = prev
        throw e
      }
    },

    async removeSession(id: string) {
      if (!isTempId(id)) await apiDeleteSession(id)
      this.sessions = this.sessions.filter((s) => s.id !== id)
      delete this.messages[id]
      if (this.activeId === id) {
        this.activeId = this.sessions[0]?.id ?? null
        if (this.activeId) await this.loadMessages(this.activeId)
        else this.panelOpen = false
      }
    },

    // =====================================================================
    // 消息
    // =====================================================================
    /** 选中会话并（首次）加载历史 */
    async selectSession(id: string) {
      this.activeId = id
      this.panelOpen = false
      this.panelCitations = []
      this.panelMessageId = null
      if (!this.messages[id]) await this.loadMessages(id)
    },

    async loadMessages(id: string, opts: { force?: boolean } = {}) {
      if (this.messagesLoading) return
      if (this.messages[id] && !opts.force) return
      this.messagesLoading = true
      this.messagesError = ''
      try {
        const { session, messages } = await apiListMessages(id, { limit: 100 })
        this.messages[id] = messages.map(toUiMessage)
        // 后端回来的会话信息可能比列表更新（标题由首条消息生成）
        const target = this.sessions.find((s) => s.id === id)
        if (target && session?.title) target.title = session.title
      } catch (e) {
        this.messagesError = errorText(e)
      } finally {
        this.messagesLoading = false
      }
    },

    /** 追加一条消息到当前会话 */
    pushMessage(sessionId: string, msg: UiMessage) {
      if (!this.messages[sessionId]) this.messages[sessionId] = []
      this.messages[sessionId].push(msg)
    },

    /** 找到正在流式的那条消息对象（直接返回引用，改它的标量字段即可响应） */
    streamingMessage(): UiMessage | null {
      const sid = this.streaming.sessionId
      const mid = this.streaming.messageId
      if (!sid || !mid) return null
      return (this.messages[sid] ?? []).find((m) => m.id === mid) ?? null
    },

    // =====================================================================
    // 发送 / 停止 / 重新生成
    // =====================================================================
    async send(text: string, opts: { sessionId?: string | null } = {}) {
      const question = (text || '').trim()
      if (!question || this.streaming.running) return

      // 1) 确保有会话
      let sessionId = opts.sessionId ?? this.activeId
      if (!sessionId) {
        const created = await this.createSession(shortTitle(question))
        sessionId = created.id
      }
      // 临时会话不发临时 id 给后端（后端会自建会话并用 meta.session_id 回来）
      const sendableId = isTempId(sessionId) ? null : sessionId

      // 2) 先落两条消息：用户消息 + 助手占位（占位先出现，流式往里填）
      const userMsg: UiMessage = {
        id: uid('u'),
        role: 'user',
        content: question,
        created_at: nowIso(),
        citations: [],
        status: 'done',
      }
      const assistantMsg: UiMessage = {
        id: uid('a'),
        role: 'assistant',
        content: '',
        created_at: nowIso(),
        citations: [],
        status: 'streaming',
        question,
      }
      this.pushMessage(sessionId, userMsg)
      this.pushMessage(sessionId, assistantMsg)

      // 3) 更新会话摘要（本地先更新，避免等接口回来列表跳一下）
      const target = this.sessions.find((s) => s.id === sessionId)
      if (target) {
        target.last_message = question
        target.updated_at = nowIso()
        target.message_count = (target.message_count || 0) + 2
        if (target.title === '新会话' || target.title === '未命名会话' || !target.title) {
          target.title = shortTitle(question)
        }
        this.sortSessions()
      }

      // 4) 开流
      runtime.abortedByUser = false
      runtime.buffer = ''
      const controller = new AbortController()
      runtime.controller = controller

      this.streaming = {
        sessionId,
        messageId: assistantMsg.id,
        phase: 'thinking',
        progress: '',
        running: true,
        tool: '',
      }

      /** 50ms 节流：把最新累积全文刷进响应式状态 */
      const flush = () => {
        const msg = this.streamingMessage()
        if (msg) msg.content = runtime.buffer
      }
      const scheduleFlush = () => {
        if (runtime.timer) return
        runtime.timer = window.setTimeout(() => {
          runtime.timer = 0
          flush()
        }, 50)
      }

      const toolLabel = (name: string) => {
        const label = name.replace(/_/g, ' ')
        return `正在调用技能：${label}`
      }

      const handlers: ChatStreamHandlers = {
        onReset: () => {
          runtime.buffer = ''
          flush()
        },
        onStatus: (phase) => {
          this.streaming.phase = phase
          if (phase && phase !== 'generating') this.streaming.progress = ''
        },
        onProgress: (progress) => {
          this.streaming.progress = progress
        },
        onDelta: (fullText) => {
          // 累积全文：直接替缓冲，保证幂等
          runtime.buffer = fullText
          scheduleFlush()
        },
        onTool: (tool) => {
          this.streaming.tool = toolLabel(tool.name)
          if (tool.ok) window.setTimeout(() => (this.streaming.tool = ''), 1200)
        },
        onMeta: (meta) => {
          const msg = this.streamingMessage()
          if (!msg) return
          if (Array.isArray(meta.citations)) msg.citations = meta.citations
          if (typeof meta.grounded === 'boolean') msg.grounded = meta.grounded
          if (meta.usage) msg.usage = meta.usage
          if (meta.model) msg.model = meta.model
          if (meta.provider) msg.provider = meta.provider
          if (typeof meta.latency_ms === 'number') msg.latency_ms = meta.latency_ms
          if (Array.isArray(meta.skills_used)) msg.skills_used = meta.skills_used
          if (meta.message_id) msg.id = meta.message_id

          // 首次对话：把临时会话绑定到后端真实 id
          if (meta.session_id && meta.session_id !== sessionId) {
            this.adoptSessionId(sessionId as string, meta.session_id)
            sessionId = meta.session_id
          }
        },
        onDone: () => {
          const msg = this.streamingMessage()
          if (msg) msg.status = 'done'
        },
        onError: (text) => {
          const msg = this.streamingMessage()
          if (msg) {
            msg.status = 'error'
            msg.error = text
          }
        },
      }

      try {
        const meta = await streamChat(
          {
            question,
            session_id: sendableId,
            channel: 'web',
          },
          handlers,
          controller.signal,
        )

        // meta 帧可能比 done 先到，也可能一次都没到（后端只发 done）
        if (meta) handlers.onMeta?.(meta)

        // 结束：把节流器里没刷出去的内容补一次
        if (runtime.timer) {
          window.clearTimeout(runtime.timer)
          runtime.timer = 0
        }
        flush()

        const msg = this.streamingMessage()
        if (msg) {
          if (msg.status === 'streaming') msg.status = 'done'
          // 兜底：服务端一个字都没给
          if (!msg.content) msg.content = '（本次没有返回内容）'
        }

        // 让会话标题/摘要与后端对齐
        this.syncSessionSummary(sessionId, question)
      } catch (e) {
        const aborted = runtime.abortedByUser || controller.signal.aborted
        const msg = this.streamingMessage()
        if (msg) {
          if (aborted) {
            msg.status = 'aborted'
            if (!msg.content) msg.error = '已停止生成'
          } else {
            msg.status = 'error'
            msg.error = errorText(e)
          }
        }
        if (!aborted) {
          // 错误已由页面统一 toast（这里只记录，避免重复弹两次）
          this.streaming.progress = ''
        }
      } finally {
        if (runtime.timer) {
          window.clearTimeout(runtime.timer)
          runtime.timer = 0
        }
        flush()
        runtime.controller = null
        this.streaming.running = false
        this.streaming.phase = ''
        this.streaming.progress = ''
        this.streaming.tool = ''
      }
    },

    /** 停止生成（服务端会检测到客户端断开并取消任务） */
    stop() {
      runtime.abortedByUser = true
      runtime.controller?.abort()
    },

    /** 重新生成：删掉这条回答及其对应的用户消息，然后把同一个问题重发一次 */
    async regenerate(messageId: string) {
      if (this.streaming.running) return
      const sid = this.activeId
      if (!sid) return
      const list = this.messages[sid] ?? []
      const idx = list.findIndex((m) => m.id === messageId)
      if (idx < 0) return
      const question =
        list[idx].question ?? (list[idx - 1]?.role === 'user' ? list[idx - 1].content : '')
      if (!question) return
      const start = list[idx - 1]?.role === 'user' ? idx - 1 : idx
      list.splice(start, list.length - start)
      await this.send(question, { sessionId: sid })
    },

    /** 临时会话 id → 后端真实 id（迁移消息桶） */
    adoptSessionId(oldId: string, newId: string) {
      if (oldId === newId) return
      this.messages[newId] = this.messages[oldId] ?? []
      delete this.messages[oldId]
      const session = this.sessions.find((s) => s.id === oldId)
      if (session) session.id = newId
      if (this.activeId === oldId) this.activeId = newId
      if (this.streaming.sessionId === oldId) this.streaming.sessionId = newId
    },

    /** 流结束后静默刷新会话列表（后端可能更新了标题/消息数） */
    async syncSessionSummary(sessionId: string | null, question: string) {
      const target = this.sessions.find((s) => s.id === sessionId)
      if (target) {
        target.last_message = question
        target.updated_at = nowIso()
        target.message_count = (this.messages[target.id] ?? []).length
      }
      this.sortSessions()
      // 不阻塞 UI；失败也不提示（列表会在下次进入时再刷新）
      try {
        const { items } = await apiListSessions({ limit: 100 })
        const merged = items.map((s) => ({ ...s, title: s.title || '未命名会话' }))
        // 保留本地占位会话（后端还没有的）
        const localOnly = this.sessions.filter((s) => isTempId(s.id))
        this.sessions = [...localOnly, ...merged]
        this.sortSessions()
      } catch {
        /* 忽略：离线时保持本地状态 */
      }
    },

    // =====================================================================
    // 右侧来源面板
    // =====================================================================
    openCitationPanel(messageId: string, citations: Citation[]) {
      this.panelMessageId = messageId
      this.panelCitations = citations
      this.panelOpen = true
    },
    closeCitationPanel() {
      this.panelOpen = false
    },
  },
})