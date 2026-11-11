# 内核契约（Lead 定义，实现者必须遵守）

本项目从 `E:\Project\HQDingTalkrobot`（Python 后端）与 `E:\Project\HQDingtalkRobotUI`（Vue 3 前端）中抽取出
**与钉钉 / 公司业务无关**的三个内核：聊天（chat）、技能（skills）、检索增强生成（rag）。
任何前端（Web / 桌面 / IDE / IM / Agent 平台）都通过 HTTP + SSE + OpenAI 兼容协议接入。

## 目录与所有权

```
E:\Project\TsukiyoHub
├─ server/tsukiyohub/
│  ├─ config.py            [Lead]   Settings（已完成）
│  ├─ domain.py            [Lead]   数据契约（已完成）
│  ├─ logging_setup.py     [Lead]   日志（已完成）
│  ├─ ports/               [Lead]   端口协议（已完成）
│  ├─ adapters/            [各实现者]  端口的具体实现
│  │  ├─ llm_openai.py     [chat]   OpenAI 兼容模型 + fake 模型 + 路由器
│  │  ├─ embedding.py      [rag]    OpenAI / DashScope / hash 向量化
│  │  ├─ vector_memory.py  [rag]    内存混合检索（BM25 + 余弦，零依赖，落盘 JSON）
│  │  ├─ vector_milvus.py  [rag]    Milvus 后端
│  │  ├─ store_sql.py      [skills] SQLAlchemy 会话/消息/记忆/键值
│  │  └─ rerank_dashscope.py [rag]  可选重排
│  ├─ kernel/
│  │  ├─ chat/             [chat]   agent 循环、会话编排、提示词、引用整理、流式事件
│  │  ├─ rag/              [rag]    解析、切块、向量化缓存、检索、融合、证据闸门、逐句核对
│  │  ├─ skills/           [skills] 工具协议、注册表、SKILL.md 发现、MCP、内置技能
│  │  └─ memory/           [skills] 事实记忆抽取/召回、长对话摘要
│  ├─ api/                 [Lead]   路由与 schema
│  ├─ container.py         [Lead]   依赖装配
│  ├─ app.py / __main__.py [Lead]   FastAPI 应用与启动
│  └─ banner.py            [Lead]
├─ web/                    [frontend] Vue3 + TS + Vite + Ant Design Vue 聊天工作台
├─ tests/                  [各自 + Lead]
└─ docs/                   [各自]
```

**只改自己所有权范围内的文件。** 需要别人改契约时，通过 `send_message` 告诉 lead，不要自行改 `domain.py` / `ports/` / `config.py`。

## 全局约定

1. 语言：代码注释与文档中文；标识符英文。
2. Python 3.11+；一律 `from __future__ import annotations`；类型标注齐全。
3. 不引入钉钉、部门、员工、公司等业务概念。任何企业专属逻辑都属于「接入方」。
4. 无外部依赖必须能跑：`vector_backend=memory` + SQLite + `llm_provider=fake` 时，`pytest` 与
   `uvicorn tsukiyohub.app:app` 应完全可用（不连 Milvus / PostgreSQL / 外部模型）。
5. 日志：`from tsukiyohub.logging_setup import get_logger`；不要 print。
6. 所有对外文案用「知识库 / 技能 / 会话 / 用户」，不要出现「员工 / 部门 / 钉钉」。
7. 每个模块顶部写中文 docstring，说明职责与「为什么这么设计」。

## 必须实现的类与方法（跨模块契约）

### rag

```python
# tsukiyohub/kernel/rag/chunker.py
class TextChunker:
    def __init__(self, settings: Settings) -> None: ...
    def split(self, document: Document) -> list[Chunk]: ...

# tsukiyohub/kernel/rag/parser.py
SUPPORTED_SUFFIXES: set[str]
def build_document_meta(path: Path, *, root: Path, source_type: SourceType = ..., doc_type: DocType = ...) -> DocumentMeta: ...
class DocumentParser:
    def __init__(self, settings: Settings) -> None: ...
    def parse(self, path: Path, meta: DocumentMeta) -> Document: ...

# tsukiyohub/kernel/rag/embeddings.py
class CachedEmbeddings:
    def __init__(self, provider: EmbeddingProvider, cache_path: Path | None = None) -> None: ...
    # 属性：enabled / provider_name / model / dimension
    def embed_for_index(self, texts: list[str]) -> list[list[float]]: ...
    def embed_for_query(self, texts: list[str]) -> list[list[float]]: ...
    def pseudo_vector(self, text: str) -> list[float]: ...

# tsukiyohub/kernel/rag/retriever.py
class HybridRetriever:
    def __init__(self, settings: Settings, backend: VectorStore, embeddings: CachedEmbeddings,
                 reranker: Reranker | None = None) -> None: ...
    def retrieve(self, query: str, *, top_k: int | None = None, filters: SearchFilters | None = None,
                 doc_types: list[str] | None = None) -> list[ScoredChunk]: ...

# tsukiyohub/kernel/rag/store.py
@dataclass
class DocumentEntry:  # doc_id/title/uri/file_name/content_hash/chunk_ids/source_type/doc_type/version/updated_at/ingested_at
    ...
class KnowledgeStore:
    def __init__(self, settings: Settings, backend: VectorStore, embeddings: CachedEmbeddings,
                 chunker: TextChunker, retriever: HybridRetriever) -> None: ...
    documents: dict[str, DocumentEntry]
    def upsert_document(self, document: Document, force: bool = False) -> IngestResult: ...
    def ingest_path(self, path: Path, *, source_type: SourceType = ..., doc_type: DocType = ...) -> IngestResult: ...
    def ingest_bytes(self, file_name: str, data: bytes, *, doc_type: DocType = ...) -> IngestResult: ...
    def ingest_directory(self, root: Path, *, force: bool = False) -> list[IngestResult]: ...
    def delete_document(self, doc_id: str) -> int: ...
    def clear(self) -> None: ...
    def retrieve(self, query: str, *, top_k: int | None = None, filters: SearchFilters | None = None,
                 doc_types: list[str] | None = None) -> list[ScoredChunk]: ...
    def stats(self) -> KnowledgeStats: ...
    def list_documents(self) -> list[DocumentInfo]: ...
    def get_chunk(self, chunk_id: str) -> Chunk | None: ...
    def persist(self) -> None: ...
    def load(self) -> bool: ...
    @property
    def loaded(self) -> bool: ...
    @property
    def chunk_count(self) -> int: ...

# tsukiyohub/kernel/rag/answer.py
def source_line(chunk: Chunk) -> str: ...
def extractive_answer(chunks: list[ScoredChunk]) -> str: ...
def build_evidence_block(chunks: list[ScoredChunk]) -> str: ...
def progress_found(titles: list[str]) -> str: ...

# tsukiyohub/kernel/rag/grounding.py
@dataclass
class GroundingResult: grounded: bool; score: float; reason: str
def evaluate_grounding(chunks: list[ScoredChunk], min_score: float) -> GroundingResult: ...

# tsukiyohub/kernel/rag/ground_check.py
@dataclass
class CheckResult: answer: str; checked: bool; unsupported: list[str]; removed: list[str]; error: str | None
def check(answer: str, evidence: list[tuple[int, str]], complete: Callable[[str], str], *,
          mode: str = "strict", question: str = "") -> CheckResult: ...
```

### skills

```python
# tsukiyohub/kernel/skills/base.py
@dataclass
class ToolContext:
    question: str
    user_ctx: dict[str, Any]
    cfg: dict[str, Any]
    pref: dict[str, Any]
    hits: dict[int, ScoredChunk]
    next_index: int = 1
    records: list[dict[str, Any]] = ...
    retrieved: list[dict[str, Any]] = ...
    kb_called: bool = False
    timings: dict[str, int] = ...
    direct: str = ""
    history: list[dict[str, Any]] = ...
    extra: dict[str, Any] = ...
    def add_time(self, key: str, ms: float) -> None: ...

class BaseSkill:
    name: str; title: str; description: str; rules: str
    parameters: dict[str, Any]          # JSON Schema
    default_enabled: bool = True
    kind: str = "tool"                  # tool / skill / mcp
    source: str = "builtin"
    @property
    def spec(self) -> dict[str, Any]: ...   # OpenAI function 规格
    def call(self, args: dict[str, Any], ctx: ToolContext) -> str: ...
    def prefetch(self, ctx: ToolContext) -> str | None: return None

# tsukiyohub/kernel/skills/registry.py
class SkillRegistry:
    def register(self, skill: BaseSkill) -> BaseSkill: ...
    def get(self, name: str) -> BaseSkill | None: ...
    def enabled(self, cfg: dict[str, Any] | None = None, allow: list[str] | None = None) -> list[BaseSkill]: ...
    def describe(self, cfg: dict[str, Any] | None = None) -> list[SkillInfo]: ...
    def set_enabled(self, name: str, enabled: bool) -> bool: ...
    def count_call(self, name: str) -> None: ...

# tsukiyohub/kernel/skills/loader.py
@dataclass(frozen=True)
class SkillDoc: name: str; description: str; path: Path; body: str; metadata: dict[str, str]
def discover(root: Path) -> list[SkillDoc]: ...
def catalog_prompt(skills: list[SkillDoc]) -> str: ...
def build_skill_tools(root: Path, cfg: dict[str, Any]) -> list[BaseSkill]: ...   # 生成 use_skill 等

# tsukiyohub/kernel/skills/mcp.py
class McpRegistry:
    def __init__(self, settings: Settings) -> None: ...
    def load(self) -> list[BaseSkill]: ...    # 按 mcp_config_file 连接 stdio / sse server，包装成 BaseSkill

# tsukiyohub/kernel/skills/builtin/knowledge.py
class KnowledgeTool(BaseSkill):
    name = "knowledge_search"
    def __init__(self, settings: Settings, store: KnowledgeStore) -> None: ...
    def filters(self, ctx: ToolContext) -> SearchFilters: ...
    def call(self, args: dict[str, Any], ctx: ToolContext) -> str: ...
    def prefetch(self, ctx: ToolContext) -> str | None: ...

# tsukiyohub/kernel/skills/builtin/web.py
class WebFetchTool(BaseSkill): ...   # 抓取公开网页正文
class WebSearchTool(BaseSkill): ...  # 可选：配置了搜索 API 才启用

# tsukiyohub/kernel/skills/builtin/math.py
class CalculatorTool(BaseSkill): ...  # 安全四则运算（演示「无副作用技能」）

# tsukiyohub/kernel/memory/service.py
class MemoryService:
    def __init__(self, settings: Settings, store: MemoryStore, router: LLMRouter) -> None: ...
    def recall(self, user_id: str, question: str) -> str: ...
    def extract(self, user_id: str, question: str, answer: str) -> list[FactRecord]: ...
    def list(self, user_id: str, limit: int = 50) -> list[FactRecord]: ...
    def delete(self, fact_id: str, user_id: str) -> bool: ...
    def clear(self, user_id: str) -> int: ...
```

### chat

```python
# tsukiyohub/kernel/chat/agent.py
EventSink = Callable[[str, Any], None]        # ("delta", 全文) / ("status", "thinking"|"searching") / ("progress", 文案)
@dataclass
class UsageRecord: model; provider; call_type; prompt_tokens; completion_tokens; total_tokens; latency_ms; success; error
@dataclass
class AgentResult: answer; citations; kb_called; hit; tool_calls; usages; model; provider; degraded; degraded_reason;
                   latency_ms; mode; route; llm_ms; retrieve_ms; retrieved; evidence; context; suggestions; grounding_score
class ChatAgent:
    def __init__(self, settings: Settings, router: LLMRouter, tools: SkillRegistry, kv: KVStore | None = None) -> None: ...
    def run(self, question: str, history: list[dict[str, str]] | None = None,
            user_ctx: dict[str, Any] | None = None, *, preference: dict[str, Any] | None = None,
            on_event: EventSink | None = None) -> AgentResult: ...
    def build_system_prompt(self, pref: dict[str, Any], user_ctx: dict[str, Any],
                            tools: list[BaseSkill] | None = None) -> str: ...
    def simple_completion(self, prompt: str, *, call_type: str = "utility",
                          params: dict[str, Any] | None = None) -> tuple[str, list[UsageRecord]]: ...

# tsukiyohub/kernel/chat/conversation.py
class ConversationService:
    def __init__(self, settings: Settings, sessions: SessionStore, memory: MemoryService,
                 agent: ChatAgent, skills: SkillRegistry, hot: HotStore) -> None: ...
    async def answer(self, request: ChatRequest, *, on_event: EventSink | None = None) -> ChatResponse: ...
    def resolve_session(self, request: ChatRequest) -> SessionRecord: ...
    def history(self, session: SessionRecord) -> list[dict[str, str]]: ...

# tsukiyohub/kernel/chat/citations.py
def with_citations(answer: str, citations: list[Citation], *, markdown: bool = True, limit: int = 3) -> str: ...
def renumber(answer: str, citations: list[Citation], evidence: dict[int, str],
             pages: dict[int, int | None] | None = None) -> tuple[str, list[Citation], dict[int, str]]: ...
def tidy_refs(answer: str, doc_ids: dict[int, str]) -> str: ...

# tsukiyohub/kernel/chat/prompt.py
def build_persona_prompt(settings: Settings, pref: dict[str, Any], user_ctx: dict[str, Any]) -> str: ...
def build_tool_rules(tools: list[BaseSkill]) -> str: ...

# tsukiyohub/kernel/chat/answer_style.py
def guess_shape(question: str) -> str: ...
def length_instruction(shape: str, group: bool, length: str, *, detail: bool = False) -> str: ...
def expression_rules(style: dict[str, Any], shape: str | None, group: bool) -> str: ...
```

### adapters

```python
# tsukiyohub/adapters/llm_openai.py
class OpenAIChatClient:      # implements LLMClient（langchain_openai.ChatOpenAI；流式 + 工具）
    def __init__(self, *, model: str, base_url: str, api_key: str, provider: str,
                 timeout: float, max_retries: int, thinking: bool, temperature: float, max_tokens: int) -> None: ...
class FakeLLMClient:         # 无密钥演示：识别「知识库」提问并调用工具；其余返回固定回复
class ConfiguredLLMRouter:   # implements LLMRouter
    def __init__(self, settings: Settings, kv: KVStore | None = None) -> None: ...
def build_router(settings: Settings, kv: KVStore | None = None) -> LLMRouter: ...

# tsukiyohub/adapters/embedding.py
def build_embeddings(settings: Settings) -> EmbeddingProvider: ...
class OpenAIEmbeddingProvider: ...
class HashEmbeddingProvider: ...

# tsukiyohub/adapters/vector_memory.py
class MemoryVectorStore:   # implements VectorStore；BM25(jieba) + 余弦 + JSONL 落盘
    def __init__(self, *, dimension: int, persist_path: Path | None = None) -> None: ...
def build_memory_store(settings: Settings) -> MemoryVectorStore: ...

# tsukiyohub/adapters/vector_milvus.py
class MilvusVectorStore:   # implements VectorStore
    def __init__(self, *, uri: str, token: str, collection: str, dimension: int, timeout: float) -> None: ...
def build_milvus_store(settings: Settings) -> MilvusVectorStore: ...

# tsukiyohub/adapters/store_sql.py
class Database:  # engine/session/init
class SqlSessionStore  # SessionStore
class SqlMemoryStore   # MemoryStore
class SqlKVStore       # KVStore
def build_database(settings: Settings) -> Database: ...
def build_stores(settings: Settings) -> tuple[Database, SessionStore, MemoryStore, KVStore]: ...
```

### API / container（Lead 负责，其他人只读）

- `POST /api/chat`、`POST /api/chat/stream`（SSE）、`POST /api/v1/chat/completions`（OpenAI 兼容）
- `GET/POST/DELETE /api/sessions...`、`GET /api/skills`、`PATCH /api/skills/{name}`、
  `POST /api/knowledge/ingest`、`POST /api/knowledge/upload`、`GET /api/knowledge/documents`、
  `DELETE /api/knowledge/documents/{doc_id}`、`POST /api/knowledge/search`、`GET /api/knowledge/stats`、
  `GET /api/memory/{user_id}`、`DELETE /api/memory/{user_id}`、`GET /healthz`、`GET /readyz`。

SSE 事件（`text/event-stream`，`Cache-Control: no-cache`、`X-Accel-Buffering: no`，每 15 秒发一次注释帧 `: ping` 防网关掐流）：

| event | data | 说明 |
|---|---|---|
| `status` | `thinking` / `searching` / `generating` | 状态提示 |
| `progress` | 纯文本 | 「正在查找：《xx》等 N 份资料」 |
| `tool` | `{"name": "...", "ok": true}` | 调用了哪个技能（用于过程展示） |
| `delta` | **当前完整文本**（累积，不是增量） | 前端整段替换，天然幂等；`reset` 后从空开始 |
| `reset` | 空 | 换了一轮生成，清空重来 |
| `meta` | `{"session_id", "citations", "grounded", "grounding_score", "usage", "tool_calls", "skills_used", "trace_id"}` | 生成结束后 |
| `error` | `{"message": "..."}` | 服务端异常（已转成可读中文） |
| `done` | `[DONE]` | 结束 |

> 为什么 `delta` 推「累积全文」而不是增量片段：模型可能中途换一轮（工具调用后重新生成），
> 前端拿到全文后直接整段替换即可，不需要自己拼接、也不会因为丢帧而错位。

## 实现顺序（避免互相等待）

1. rag 先做 `tokenize / chunker / parser / embeddings / vector_memory / retriever / store / answer / grounding / ground_check`；
2. skills 同时做 `base / registry / loader / builtin(knowledge,web,math)` + `store_sql` + `memory/service`；
3. chat 依赖 1、2 的签名**已定死的接口**，可以先按契约写，再联调；
4. frontend 只依赖 HTTP 契约，可并行。