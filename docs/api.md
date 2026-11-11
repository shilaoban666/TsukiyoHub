# TsukiyoHub API

所有接口都在 `/api` 前缀下，响应为 JSON；错误统一是 `{"detail": "中文可读信息"}` + 非 2xx。
服务启动后交互式文档在 <http://127.0.0.1:8080/docs>。

- 鉴权：`.env` 里 `API_KEY` 留空 = 不鉴权；填了则请求需带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。
- 编码：UTF-8。
- 时间：ISO8601（UTC）。

---

## 1. 聊天

### `POST /api/chat`

一次返回完整回答。

```json
{
  "question": "怎么配置 Milvus 向量库？",
  "session_id": null,
  "user_id": "u-1",
  "user_name": "小明",
  "channel": "web",
  "top_k": 8,
  "doc_types": ["doc"],
  "skills": ["knowledge_search", "web_fetch"]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `question` | string | 必填。用户问题（上限 8000 字符） |
| `session_id` | string \| null | 会话 ID；不传则新建，多轮续聊必须带回来 |
| `user_id` | string \| null | 接入方自己的用户标识（记忆 / 会话隔离用） |
| `user_name` | string \| null | 展示用昵称 |
| `channel` | string | `api` / `web` / `cli` / `openai` / `plugin`（只影响日志与统计） |
| `top_k` | int \| null | 本次检索取几条（默认取服务配置） |
| `doc_types` | string[] \| null | 限定文档类型（`doc` / `faq` / `spec` / `note` / `other`） |
| `skills` | string[] \| null | 本轮允许使用的技能名；null = 全部启用的技能 |
| `stream` | bool | 仅供 OpenAI 兼容接口使用；本接口固定非流式 |

响应：

```json
{
  "answer": "在 .env 里设置 VECTOR_BACKEND=milvus，并填 MILVUS_URI... [1]",
  "session_id": "f3a1c2...",
  "message_id": "9b8e7d...",
  "intent": "knowledge",
  "citations": [
    {
      "index": 1,
      "doc_id": "ab12cd34ef56",
      "chunk_id": "ab12cd34ef56#0",
      "title": "部署说明",
      "uri": "deploy.md",
      "snippet": "向量检索默认使用内存实现...",
      "version": "v1",
      "score": 0.82,
      "label": "《部署说明》",
      "url": "",
      "cited": true
    }
  ],
  "model": "gpt-4o-mini",
  "provider": "openai",
  "latency_ms": 2310,
  "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
  "grounded": true,
  "grounding_score": 0.82,
  "degraded": false,
  "degraded_reason": null,
  "tool_calls": [{"name": "knowledge_search", "args": {"query": "配置 Milvus"}, "ok": true}],
  "skills_used": ["knowledge_search"],
  "suggestions": [],
  "trace_id": "1a2b3c"
}
```

关键字段：
- `grounded`：回答是否有知识库依据。`false` 时**不要**把 `citations` 当制度依据展示。
- `degraded`：未走大模型（没配 Key / 模型全挂），`answer` 是资料原文摘录或兜底话术。
- `citations[].cited`：`true` 表示回答正文里真的引用了它；`false` 是补足列表的相近文档。

### `POST /api/chat/stream`

SSE 逐字输出。**必须用 POST**（EventSource 不支持自定义 body/header，请用 `fetch` + `ReadableStream`）。

请求体同 `POST /api/chat`；响应头 `Content-Type: text/event-stream`、`X-Accel-Buffering: no`。

事件表：

| event | data | 说明 |
|---|---|---|
| `status` | `thinking` / `searching` / `generating` | 状态提示，可忽略 |
| `progress` | 纯文本 | 如「正在查找：《部署说明》等 3 份资料」 |
| `tool` | `{"name":"knowledge_search","ok":true}` | 调用了哪个技能 |
| `delta` | **当前累积全文** | 每次都是从头到现在的完整文本，整段替换即可 |
| `reset` | 空 | 换了一轮生成，先清空再继续 |
| `meta` | JSON | 生成结束后的元信息（见下） |
| `error` | `{"message":"..."}` | 出错（收到即结束，不要重试） |
| `done` | `[DONE]` | 结束 |

每 15 秒会有一条注释帧 `: ping`，解析器忽略以 `:` 开头的行即可。

`meta` 示例：

```json
{
  "session_id": "f3a1c2",
  "message_id": "9b8e7d",
  "citations": [ ... ],
  "grounded": true,
  "grounding_score": 0.82,
  "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
  "tool_calls": [ ... ],
  "skills_used": ["knowledge_search"],
  "suggestions": [],
  "trace_id": "1a2b3c",
  "degraded": false,
  "intent": "knowledge"
}
```

最小前端实现：

```ts
const res = await fetch("/api/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
  body: JSON.stringify({ question, session_id: sessionId, user_id }),
  signal: controller.signal,          // 停止生成：controller.abort()
});

const reader = res.body!.getReader();
const decoder = new TextDecoder();
let buffer = "";
let event = "message", data: string[] = [];

const dispatch = () => {
  const payload = data.join("\n");    // SSE 规范：多行 data 用 \n 拼接
  if (event === "delta") render(payload);              // 累积全文，直接渲染
  if (event === "reset") render("");
  if (event === "meta") applyMeta(JSON.parse(payload));
  if (event === "error") showError(JSON.parse(payload).message);
  event = "message"; data = [];
};

for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const lines = buffer.split("\n");
  buffer = lines.pop() ?? "";
  for (const line of lines) {
    if (line.startsWith(":")) continue;               // 心跳注释
    if (line === "") { dispatch(); continue; }
    const [field, ...rest] = line.split(":");
    const text = rest.join(":").replace(/^ /, "");
    if (field === "event") event = text;
    if (field === "data") data.push(text);
  }
}
```

### `POST /api/v1/chat/completions`（OpenAI 兼容）

标准 OpenAI Chat Completions 协议，`stream=true` 时返回 `data: {...}` 分片。
`model` 字段被忽略（模型由服务端配置决定）。额外返回 `citations` / `session_id` / `grounded`。

```
Base URL: http://127.0.0.1:8080/api/v1
```

### `GET /api/v1/models`

返回一个模型条目，满足部分客户端启动时的模型探测。

### `POST /api/feedback`

```json
{"session_id": "f3a1c2", "message_id": "9b8e7d", "rating": 1, "comment": "很有用"}
```

写入热存储（未配置 Redis 时只返回成功、不持久化）。

---

## 2. 会话

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/sessions?user_id=&limit=20&offset=0` | 会话列表 → `{items, total}` |
| `POST` | `/api/sessions` | 新建 → `{user_id, channel, title}` → `SessionInfo` |
| `GET` | `/api/sessions/{id}` | 会话详情 |
| `PATCH` | `/api/sessions/{id}` | 重命名 → `{"title": "..."}` |
| `GET` | `/api/sessions/{id}/messages?limit=50&before=` | 历史消息 → `{session, messages}`（时间正序） |
| `DELETE` | `/api/sessions/{id}` | 删除会话（连带消息） |

`SessionInfo`：

```json
{"id":"f3a1c2","title":"怎么配置 Milvus","user_id":"u-1","channel":"web",
 "created_at":"2026-01-01T10:00:00+00:00","updated_at":"2026-01-01T10:02:00+00:00",
 "message_count":4,"last_message":"在 .env 里设置..."}
```

消息：

```json
{"id":"9b8e7d","role":"assistant","content":"...","created_at":"...",
 "citations":[...],"tool_calls":[...],
 "usage":{"prompt_tokens":1200,"completion_tokens":180,"total_tokens":1380},
 "latency_ms":2310}
```

---

## 3. 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/knowledge/stats` | 文档数 / 切块数 / 向量数 / 后端 / 上次导入时间 |
| `GET` | `/api/knowledge/documents` | 文档列表 → `{items, total}` |
| `POST` | `/api/knowledge/upload` | multipart，字段名 `file`（可多个）→ 上传即入库 |
| `POST` | `/api/knowledge/ingest` | 导入服务器目录/文件 → `{"path": "knowledge", "doc_type": "doc", "force": false}` |
| `DELETE` | `/api/knowledge/documents/{doc_id}` | 删除文档及其切块 |
| `POST` | `/api/knowledge/search` | 只检索不调模型，返回分数明细（调参用） |
| `POST` | `/api/knowledge/reindex` | 清空索引并重新导入 `data/knowledge` |

上传响应：

```json
{"ok": true, "count": 1, "results": [
  {"file_name": "部署说明.md", "ok": true, "doc_id": "ab12cd34ef56",
   "title": "部署说明", "chunk_count": 12, "skipped": false, "reason": null, "error": null}
]}
```

检索响应：

```json
{"query": "Milvus", "latency_ms": 42, "grounded": true, "grounding_score": 0.82,
 "hits": [{"chunk_id":"ab12#0","doc_id":"ab12","title":"部署说明","text":"...",
           "score":0.031,"vector_score":0.82,"bm25_score":3.1,"rerank_score":null,
           "retriever":"fusion","uri":"deploy.md"}]}
```

> `score` 是融合分（只用于排序）；判断「像不像」请用 `vector_score`（原始余弦）。

---

## 4. 技能

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/skills` | 技能列表 → `{items, total}` |
| `PATCH` | `/api/skills/{name}` | 启停 → `{"enabled": false}` |
| `POST` | `/api/skills/{name}/test` | 不经过模型直接调技能（调试） |

`SkillInfo`：

```json
{"name":"knowledge_search","title":"知识库检索","kind":"tool","source":"builtin",
 "description":"在知识库里检索与问题相关的片段，返回带编号的证据",
 "enabled":true,"parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]},
 "rules":"", "calls":12}
```

`kind`：`tool`（代码技能）/ `skill`（SKILL.md 文档技能）/ `mcp`（外部 MCP 工具）。

---

## 5. 记忆

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/memory/{user_id}?limit=50` | 查看该用户的长期记忆 → `{user_id, items, total}` |
| `DELETE` | `/api/memory/{user_id}` | 清空该用户记忆 → `{ok, removed}` |
| `DELETE` | `/api/memory/{user_id}/{fact_id}` | 删除一条 |

`MemoryFact`：`{"id":"...","user_id":"u-1","content":"偏好简洁回答","category":"preference","created_at":"...","score":0}`

---

## 6. 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/system/config` | 运行时配置（不含密钥） |
| `PUT` | `/api/system/config` | 修改人设 / 风格 / 检索条数 / 技能启停 / 记忆开关 |
| `GET` | `/api/system/persona` | 人设与风格回显 |
| `GET` | `/api/system/info` | 版本、依赖状态、知识库规模、运行时长 |
| `GET` | `/healthz` | 健康检查（模型未就绪时 `status=degraded`，HTTP 仍 200） |
| `GET` | `/readyz` | 就绪检查（知识库为空时 503） |

`GET /api/system/config`：

```json
{
  "app_name": "TsukiyoHub", "version": "0.1.0", "env": "local", "bot_name": "Tsukiyo",
  "model": "gpt-4o-mini", "provider": "openai", "llm_ready": true, "fallback_model": "",
  "embedding_model": "text-embedding-3-small", "embedding_ready": true,
  "vector_backend": "memory", "database": "sqlite",
  "retrieval": {"top_k":8,"vector_k":20,"bm25_k":20,"fusion_k":60,"vector_weight":0.6,
                "bm25_weight":0.4,"min_score":0.0,"rerank_enabled":false,"grounding_min_score":0.35},
  "style": {"tone":"friendly","length":"medium","markdown":true},
  "features": {"memory":true,"mcp":false,"streaming":true,"skill_docs":true},
  "skills": ["knowledge_search","web_fetch"]
}
```

`PUT /api/system/config` 可写字段（只传要改的）：

```json
{"bot_name":"小月","persona_prompt":"你是...","default_tone":"friendly",
 "default_length":"medium","default_markdown":true,"fallback_text":"抱歉...",
 "retrieval_top_k":8,"skills_enabled":["knowledge_search"],"memory_enabled":true}
```

---

## 7. TypeScript 类型（可直接抄进前端）

```ts
export interface Citation {
  index: number; doc_id: string; chunk_id: string; title: string; uri: string;
  snippet: string; version: string; updated_at: string; score: number; label: string;
  url: string; cited: boolean;
}

export interface ChatResponse {
  answer: string; session_id: string; message_id: string;
  intent: "chat" | "knowledge" | "skill" | "clarify";
  citations: Citation[]; model: string | null; provider: string | null;
  latency_ms: number; usage: Record<string, number>;
  grounded: boolean; grounding_score: number | null;
  degraded: boolean; degraded_reason: string | null;
  tool_calls: { name: string; args?: unknown; ok?: boolean }[];
  skills_used: string[]; suggestions: string[]; trace_id: string | null;
}

export type SSEEvent =
  | { type: "status"; data: "thinking" | "searching" | "generating" }
  | { type: "progress"; data: string }
  | { type: "tool"; data: { name: string; ok: boolean } }
  | { type: "delta"; data: string }        // 累积全文
  | { type: "reset"; data: "" }
  | { type: "meta"; data: Pick<ChatResponse,
      "session_id" | "message_id" | "citations" | "grounded" | "grounding_score" |
      "usage" | "tool_calls" | "skills_used" | "suggestions" | "trace_id" | "degraded" | "intent"> }
  | { type: "error"; data: { message: string } }
  | { type: "done"; data: "[DONE]" };
```