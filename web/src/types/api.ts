/**
 * 与后端 API 对齐的类型定义（唯一真源）。
 *
 * 命名规则：**完全跟随后端 Pydantic 模型的 snake_case 字段名**，前端不做驼峰转换。
 * 这样做的好处是「打开 devtools 看到什么，代码里就是什么」，排查链路问题时不用来回翻译。
 *
 * 所有字段在类型上尽量给全（含 `?` 可选），但实际上前端渲染仍会用可选链兜底：
 * 后端版本可能比前端旧（少字段），前端不该因此白屏。
 */

// ---------------------------------------------------------------------------
// 通用
// ---------------------------------------------------------------------------
/** 后端统一错误体（FastAPI 默认）：`{"detail": "中文可读信息"}` */
export interface ApiErrorBody {
  detail?: string
  message?: string
}

/** 健康检查（GET /healthz、GET /readyz） */
export interface HealthInfo {
  status: 'ok' | 'degraded' | string
  app: string
  version: string
  env: string
  uptime_seconds: number
  index_loaded?: boolean
  document_count?: number
  chunk_count?: number
  llm_ready?: boolean
  embedding_ready?: boolean
  database?: string
  vector_store?: string
  features?: Record<string, boolean>
}

// ---------------------------------------------------------------------------
// 聊天
// ---------------------------------------------------------------------------
/** 引用来源（对应后端 Citation） */
export interface Citation {
  /** 正文里的 [1][2] 编号 */
  index: number
  doc_id: string
  title: string
  uri?: string
  /** 已截断的原文片段 */
  snippet: string
  version?: string
  updated_at?: string
  /** 融合分（0~1） */
  score: number
  label?: string
  chunk_id?: string
  url?: string
  /** false = 召回但未被正文引用（用来补足列表的） */
  cited?: boolean
}

/** 统一入参（对应后端 ChatRequest） */
export interface ChatRequest {
  question: string
  session_id?: string | null
  user_id?: string | null
  user_name?: string | null
  channel?: string
  top_k?: number | null
  doc_types?: string[] | null
  skills?: string[] | null
  stream?: boolean
  debug?: boolean
}

/** 统一出参（对应后端 ChatResponse） */
export interface ChatResponse {
  answer: string
  session_id: string
  message_id: string
  intent?: string
  citations: Citation[]
  model?: string | null
  provider?: string | null
  latency_ms: number
  usage: Record<string, number>
  grounded: boolean
  grounding_score?: number | null
  degraded: boolean
  degraded_reason?: string | null
  tool_calls: Record<string, unknown>[]
  skills_used: string[]
  suggestions: string[]
  trace_id?: string | null
}

/** `meta` 事件携带的 ChatResponse 子集（字段名与 ChatResponse 对齐） */
export interface ChatMeta {
  session_id?: string
  message_id?: string
  citations?: Citation[]
  grounded?: boolean
  grounding_score?: number | null
  usage?: Record<string, number>
  tool_calls?: Record<string, unknown>[]
  skills_used?: string[]
  trace_id?: string | null
  model?: string | null
  provider?: string | null
  latency_ms?: number
  /** 后端额外下发的字段（当前实现在 meta 里带这三个） */
  degraded?: boolean
  intent?: string
  suggestions?: string[]
  /** 少数实现会在 done/meta 里带全文，前端拿到就以后端为准 */
  answer?: string
}

/** 会话列表项（对应后端 SessionInfo） */
export interface SessionInfo {
  id: string
  title: string
  user_id?: string | null
  channel?: string
  created_at: string
  updated_at: string
  message_count: number
  last_message: string
}

/** 历史消息（后端 `messages` 数组里的一条；比落库模型多带了统计字段） */
export interface MessageRecord {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  name?: string | null
  created_at?: string
  citations?: Citation[]
  tool_calls?: Record<string, unknown>[]
  usage?: Record<string, number>
  latency_ms?: number
  extra?: Record<string, unknown> & { citations?: Citation[] }
}

/** `GET /api/sessions/{id}/messages` 的返回（messages 按时间正序：老 → 新） */
export interface SessionMessagesResult {
  session: SessionInfo
  messages: MessageRecord[]
}

// ---------------------------------------------------------------------------
// 技能
// ---------------------------------------------------------------------------
/** 技能/工具（对应后端 SkillInfo） */
export interface SkillInfo {
  name: string
  title: string
  description: string
  kind: 'tool' | 'skill' | 'mcp'
  enabled: boolean
  source: string
  /** 模型看到的参数结构（JSON Schema） */
  parameters: Record<string, unknown>
  rules: string
  calls: number
}

// ---------------------------------------------------------------------------
// 知识库
// ---------------------------------------------------------------------------
/** 文档列表项（对应后端 DocumentInfo） */
export interface DocumentInfo {
  doc_id: string
  title: string
  uri: string
  file_name: string
  doc_type: string
  source_type: string
  version: string
  chunk_count: number
  content_hash: string
  tags: string[]
  updated_at: string
  ingested_at: string
}

/** 知识库统计（对应后端 KnowledgeStats） */
export interface KnowledgeStats {
  document_count: number
  chunk_count: number
  vector_count: number
  embedding_enabled: boolean
  vector_store: string
  embedding_model?: string | null
  sources: Record<string, number>
  doc_types: Record<string, number>
  last_ingested_at?: string | null
  index_path?: string | null
}

/** 检索命中的切块（对应后端 Chunk） */
export interface Chunk {
  chunk_id: string
  doc_id: string
  text: string
  ordinal: number
  title: string
  uri: string
  source_type: string
  doc_type: string
  version: string
  updated_at: string
  extra?: Record<string, unknown>
}

/** 带分数的检索命中（对应后端 ScoredChunk） */
export interface ScoredChunk {
  chunk: Chunk
  score: number
  vector_score?: number | null
  bm25_score?: number | null
  rerank_score?: number | null
  retriever?: string
}

/** `POST /api/knowledge/search` 的命中项：后端已把 chunk 拍平，不需要再嵌一层 */
export interface SearchHit {
  chunk_id: string
  doc_id: string
  title: string
  text: string
  score: number
  vector_score?: number | null
  bm25_score?: number | null
  rerank_score?: number | null
  retriever?: string
  uri?: string
}

/** `POST /api/knowledge/search` 的返回 */
export interface SearchResult {
  query: string
  hits: SearchHit[]
  latency_ms: number
  grounded: boolean
  grounding_score?: number | null
}

/** 单个文件的上传结果（后端 `results[]` 里的一条） */
export interface UploadFileResult {
  file_name: string
  ok: boolean
  doc_id?: string
  title?: string
  chunk_count?: number
  skipped?: boolean
  reason?: string | null
  error?: string | null
}

/** `POST /api/knowledge/upload` 的返回（支持一次多文件，`results` 与文件一一对应） */
export interface UploadResult {
  ok: boolean
  /** 成功入库的文件数 */
  count: number
  results: UploadFileResult[]
}

// ---------------------------------------------------------------------------
// 系统 / 配置
// ---------------------------------------------------------------------------
/** `GET /api/system/config` 的返回：后端生效配置的脱敏快照（结构由 Lead 定稿） */
export interface SystemConfig {
  app_name?: string
  version?: string
  env?: string
  bot_name?: string

  model?: string
  provider?: string
  llm_ready?: boolean
  fallback_model?: string

  embedding_model?: string
  embedding_ready?: boolean
  vector_backend?: string
  database?: string

  retrieval?: RetrievalConfig
  style?: StyleConfig
  features?: FeatureConfig
  /** 当前启用的技能名（后端是字符串列表，兼容毫分号分隔的字符串） */
  skills?: string[]
}

/** 检索参数（只读展示区） */
export interface RetrievalConfig {
  top_k?: number
  vector_k?: number
  bm25_k?: number
  fusion_k?: number
  vector_weight?: number
  bm25_weight?: number
  min_score?: number
  rerank_enabled?: boolean
  grounding_min_score?: number
}

/** 回复风格 */
export interface StyleConfig {
  tone?: string
  length?: string
  markdown?: boolean
}

/** 功能开关 */
export interface FeatureConfig {
  memory?: boolean
  mcp?: boolean
  streaming?: boolean
  skill_docs?: boolean
}

/**
 * `PUT /api/system/config` 的请求体：**只有这 9 个字段可写**（部分覆盖）。
 * 其余（模型、向量后端、检索权重）属于部署期配置，UI 只读展示，避免改完不生效造成误解。
 */
export interface SystemConfigPatch {
  bot_name?: string
  persona_prompt?: string
  default_tone?: string
  default_length?: string
  default_markdown?: boolean
  fallback_text?: string
  retrieval_top_k?: number
  skills_enabled?: string[]
  memory_enabled?: boolean
}

/** `GET /api/system/persona` 的返回：人设等可写配置的回显 */
export interface PersonaConfig {
  persona_prompt?: string
  bot_name?: string
  default_tone?: string
  default_length?: string
  default_markdown?: boolean
  fallback_text?: string
  retrieval_top_k?: number
  memory_enabled?: boolean
  skills_enabled?: string[]
}

/** 列表类接口的统一返回（后端统一 `{items, total}`） */
export interface ListResult<T> {
  items: T[]
  total: number
}

/** `GET /api/system/info`：后端自述信息（比 /healthz 丰富，用于设置页展示） */
export interface SystemInfo {
  app?: string
  version?: string
  env?: string
  python?: string
  vector_backend?: string
  llm?: Record<string, unknown>
  embedding?: Record<string, unknown>
  features?: Record<string, boolean>
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// SSE 事件联合类型
// ---------------------------------------------------------------------------
/** 事件名。`delta` 是**累积全文**，直接整段替换即可；`reset` 表示换了一轮生成。 */
export type SSEEventName =
  | 'status'
  | 'progress'
  | 'delta'
  | 'reset'
  | 'tool'
  | 'meta'
  | 'error'
  | 'done'
  | 'ping'

export type SSEPayload =
  | { event: 'status'; data: string }
  | { event: 'progress'; data: string }
  | { event: 'delta'; data: string }
  | { event: 'reset'; data: string }
  | { event: 'tool'; data: { name: string; ok?: boolean } }
  | { event: 'meta'; data: ChatMeta }
  | { event: 'error'; data: { message?: string; detail?: string } }
  | { event: 'done'; data: string }
  | { event: 'ping'; data: string }

// ---------------------------------------------------------------------------
// 前端内部使用的 UI 模型（不是后端契约）
// ---------------------------------------------------------------------------
/** 聊天消息状态机 */
export type MessageStatus = 'streaming' | 'done' | 'aborted' | 'error'

/** 渲染层的一条消息 */
export interface UiMessage {
  /** 本地临时 id 或后端 message_id */
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
  citations: Citation[]
  status: MessageStatus
  /** 是否命中知识库（false 且有回答 = 走了兜底） */
  grounded?: boolean
  degraded?: boolean
  degraded_reason?: string | null
  model?: string | null
  provider?: string | null
  latency_ms?: number
  usage?: Record<string, number>
  skills_used?: string[]
  /** 失败/中止时的可读原因 */
  error?: string
  /** 该条消息发起时的问题（重新生成用） */
  question?: string
}