"""跨模块共享的数据契约（Pydantic 模型 / 枚举）。

**全项目只有这一处**定义「消息、文档、切块、检索命中、问答结果、技能、会话」的结构，
保证 API 层、聊天内核、检索内核、技能内核、任何前端看到的是同一套模型。

与业务无关：这里没有钉钉、部门、员工、公司，只有「用户 / 会话 / 技能 / 知识」。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class Channel(str, Enum):
    """请求来源渠道。仅用于日志、统计与差异化策略，不绑定任何具体产品。"""

    API = "api"          # 直接调 REST
    WEB = "web"          # 自带聊天前端
    CLI = "cli"          # 命令行 / 脚本
    OPENAI = "openai"    # OpenAI 兼容协议（Dify / FastGPT / 任何 IDE / 任何 App）
    PLUGIN = "plugin"    # 自定义渠道插件（接入方自己实现）

    @classmethod
    def parse(cls, value: Any) -> Channel:
        """宽松解析：无法识别时回落到 ``API``，避免渠道字段写错直接 400。"""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.API
        text = str(value).strip().lower()
        aliases = {
            "api": cls.API, "rest": cls.API, "http": cls.API,
            "web": cls.WEB, "browser": cls.WEB, "ui": cls.WEB,
            "cli": cls.CLI, "script": cls.CLI,
            "openai": cls.OPENAI, "compatible": cls.OPENAI, "chat/completions": cls.OPENAI,
            "plugin": cls.PLUGIN, "custom": cls.PLUGIN,
        }
        return aliases.get(text, cls.API)


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class DocType(str, Enum):
    """知识文档类型（通用分类，可按需扩展）。"""

    DOC = "doc"          # 普通文档
    FAQ = "faq"          # 常见问答
    SPEC = "spec"        # 规范 / 手册
    NOTE = "note"        # 笔记 / 公告
    OTHER = "other"

    @classmethod
    def parse(cls, value: Any) -> DocType:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.OTHER


class SourceType(str, Enum):
    """文档来源：本地目录 / 上传 / API 推送 / 渠道插件。"""

    LOCAL = "local"
    UPLOAD = "upload"
    API = "api"
    PLUGIN = "plugin"

    @classmethod
    def parse(cls, value: Any) -> SourceType:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.LOCAL


class Intent(str, Enum):
    """一次问答的意图（用于观测与分流，不影响对外契约）。"""

    CHAT = "chat"
    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    CLARIFY = "clarify"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 文档与切块
# ---------------------------------------------------------------------------
class DocumentMeta(BaseModel):
    """文档级元数据。**检索结果必须能回溯到人可读的来源**，这些字段就是引用的骨架。"""

    doc_id: str = Field(..., description="文档稳定 ID（默认取相对路径 / 文件名的哈希）")
    title: str = Field(..., description="文档标题")
    source_type: SourceType = SourceType.LOCAL
    doc_type: DocType = DocType.DOC
    uri: str = Field("", description="原始位置：本地相对路径 / 对象存储 key / 外部 URL")
    file_name: str = ""
    version: str = Field("", description="文档版本，如 v2.1")
    effective_date: str = Field("", description="生效日期 ISO8601")
    tags: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    extra: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    """一篇原始文档（解析后、切块前的纯文本形态）。"""

    meta: DocumentMeta
    content: str
    content_hash: str = ""


class Chunk(BaseModel):
    """检索的最小单元。``citation`` 是给用户看的引用标签。"""

    chunk_id: str
    doc_id: str
    text: str
    ordinal: int = 0
    title: str = ""
    uri: str = ""
    source_type: SourceType = SourceType.LOCAL
    doc_type: DocType = DocType.DOC
    version: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def citation(self) -> str:
        """人类可读引用，例如「《员工手册》v2.1 · 第 3 段」。"""
        parts = [f"《{self.title}》" if self.title else "《未命名文档》"]
        if self.version:
            parts.append(self.version)
        parts.append(f"第 {self.ordinal + 1} 段")
        return " · ".join(parts)


class ScoredChunk(BaseModel):
    """带分数的检索命中。"""

    chunk: Chunk
    score: float = 0.0
    vector_score: float | None = None
    bm25_score: float | None = None
    #: 重排模型给的相关度（0 ~ 1，跨问题可比；只有经过 L2 模型重排的候选才有）
    rerank_score: float | None = None
    retriever: Literal["vector", "bm25", "hybrid", "fusion", "none"] = "hybrid"


class Citation(BaseModel):
    """返回给前端的引用来源（可直接渲染成「参考来源」列表）。"""

    index: int
    doc_id: str
    title: str
    uri: str = ""
    snippet: str = ""
    version: str = ""
    updated_at: str = ""
    score: float = 0.0
    label: str = ""
    chunk_id: str = ""
    url: str = ""
    #: 回答里真的引用了它（``False`` = 检索靠前、用来补足列表的文档）
    cited: bool = True


# ---------------------------------------------------------------------------
# 消息与统一入口
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    """一条对话消息（落库与上下文都用它）。"""

    role: Role = Role.USER
    content: str = ""
    name: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    extra: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """统一入参：任何前端 / 任何渠道都归一化成这个结构。"""

    question: str = Field("", description="用户问题原文（长度校验在 API 层）")
    messages: list[ChatMessage] = Field(default_factory=list, description="可选：完整消息列表（OpenAI 风格）")
    session_id: str | None = Field(None, description="会话 ID，用于多轮上下文；不传则新建")
    user_id: str | None = Field(None, description="用户标识（接入方自己的用户 ID）")
    user_name: str | None = None
    channel: Channel = Channel.API
    top_k: int | None = Field(None, ge=1, le=30)
    doc_types: list[DocType] | None = Field(None, description="限定文档类型过滤")
    skills: list[str] | None = Field(None, description="本轮允许使用的技能名；None = 全部启用的技能")
    stream: bool = False
    debug: bool = Field(False, description="返回检索调试信息（仅非生产环境生效）")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel", mode="before")
    @classmethod
    def _coerce_channel(cls, value: Any) -> Any:
        return Channel.parse(value)

    @property
    def has_question(self) -> bool:
        return bool(self.normalized_question())

    def normalized_question(self) -> str:
        """去掉 @机器人 之类的噪声。"""
        text = (self.question or "").strip()
        if not text and self.messages:
            last_user = [m for m in self.messages if m.role == Role.USER]
            text = (last_user[-1].content if last_user else "").strip()
        for token in ("@机器人", "@小助手", "/", "/ask"):
            if text.startswith(token):
                text = text[len(token):].strip()
        return text


class ChatResponse(BaseModel):
    """统一出参。``answer`` 是主字段，``text`` / ``content`` 为渠道兼容别名。"""

    answer: str
    session_id: str
    message_id: str = ""
    intent: Intent = Intent.CHAT
    citations: list[Citation] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    latency_ms: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    grounded: bool = Field(False, description="答案是否有知识库依据")
    grounding_score: float | None = None
    degraded: bool = Field(False, description="true 表示未走大模型（无 Key 或调用失败）")
    degraded_reason: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, description="本轮用到的技能调用记录")
    skills_used: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list, description="建议的追问 / 澄清按钮")
    trace_id: str | None = None
    debug: dict[str, Any] | None = None

    # --- 渠道兼容别名 ---
    @property
    def text(self) -> str:
        return self.answer

    @property
    def content(self) -> str:
        return self.answer

    def answer_with_citations(self, max_citations: int = 3) -> str:
        """答案 + 引用来源的纯文本（给不支持富文本的渠道用）。"""
        if not self.citations or not self.grounded:
            return self.answer
        from tsukiyohub.kernel.chat.citations import with_citations

        return with_citations(self.answer, self.citations, markdown=False, limit=max_citations)


# ---------------------------------------------------------------------------
# 知识库统计 / 入库结果
# ---------------------------------------------------------------------------
class KnowledgeStats(BaseModel):
    document_count: int = 0
    chunk_count: int = 0
    vector_count: int = 0
    embedding_enabled: bool = False
    vector_store: str = "memory"
    embedding_model: str | None = None
    sources: dict[str, int] = Field(default_factory=dict)
    doc_types: dict[str, int] = Field(default_factory=dict)
    last_ingested_at: str | None = None
    index_path: str | None = None


class IngestResult(BaseModel):
    doc_id: str
    title: str
    chunk_count: int
    skipped: bool = False
    reason: str | None = None
    error: str | None = None


class DocumentInfo(BaseModel):
    """知识库列表里的一条文档。"""

    doc_id: str
    title: str
    uri: str = ""
    file_name: str = ""
    doc_type: str = "doc"
    source_type: str = "local"
    version: str = ""
    chunk_count: int = 0
    content_hash: str = ""
    tags: list[str] = Field(default_factory=list)
    updated_at: str = ""
    ingested_at: str = ""


# ---------------------------------------------------------------------------
# 会话 / 技能 / 记忆
# ---------------------------------------------------------------------------
class SessionInfo(BaseModel):
    id: str
    title: str = ""
    user_id: str | None = None
    channel: str = "api"
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    last_message: str = ""


class SkillInfo(BaseModel):
    """一个可被模型调用的能力单元（内置工具 / SKILL.md 技能 / MCP 工具）。"""

    name: str
    title: str = ""
    description: str = ""
    kind: Literal["tool", "skill", "mcp"] = "tool"
    enabled: bool = True
    source: str = "builtin"
    #: 模型看到的参数结构（JSON Schema）
    parameters: dict[str, Any] = Field(default_factory=dict)
    rules: str = ""
    calls: int = 0


class MemoryFact(BaseModel):
    id: str
    user_id: str = ""
    content: str = ""
    category: str = "fact"
    created_at: str = ""
    score: float = 0.0


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    app: str = ""
    version: str = "0.1.0"
    env: str = "local"
    uptime_seconds: float = 0.0
    index_loaded: bool = False
    document_count: int = 0
    chunk_count: int = 0
    llm_ready: bool = False
    embedding_ready: bool = False
    database: str = "ready"
    vector_store: str = "memory"
    features: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 与 LangChain 的桥接（仅在适配器层使用，避免 LangChain 类型渗透到内核）
# ---------------------------------------------------------------------------
_LC_META_FIELDS = (
    "doc_id", "title", "uri", "source_type", "doc_type", "version", "updated_at", "ordinal",
)


def chunk_to_lc_document(chunk: Chunk) -> Any:
    """``Chunk`` → ``langchain_core.documents.Document``。"""
    from langchain_core.documents import Document as LCDocument

    metadata: dict[str, Any] = {field: getattr(chunk, field) for field in _LC_META_FIELDS}
    metadata["chunk_id"] = chunk.chunk_id
    if chunk.extra:
        metadata["extra"] = chunk.extra
    return LCDocument(page_content=chunk.text, metadata=metadata)


def chunk_from_lc_document(document: Any) -> Chunk:
    """``langchain_core.documents.Document`` → ``Chunk``。"""
    metadata = dict(getattr(document, "metadata", {}) or {})
    doc_id = str(metadata.get("doc_id") or "unknown")
    ordinal = int(metadata.get("ordinal", 0) or 0)
    return Chunk(
        chunk_id=str(metadata.get("chunk_id") or f"{doc_id}#{ordinal}"),
        doc_id=doc_id,
        text=getattr(document, "page_content", "") or "",
        ordinal=ordinal,
        title=str(metadata.get("title") or ""),
        uri=str(metadata.get("uri") or ""),
        source_type=SourceType.parse(metadata.get("source_type", SourceType.LOCAL.value)),
        doc_type=DocType.parse(metadata.get("doc_type", DocType.DOC.value)),
        version=str(metadata.get("version") or ""),
        updated_at=str(metadata.get("updated_at") or ""),
        extra=metadata.get("extra") or {},
    )


__all__ = [
    "Channel", "Role", "DocType", "SourceType", "Intent",
    "DocumentMeta", "Document", "Chunk", "ScoredChunk", "Citation",
    "ChatMessage", "ChatRequest", "ChatResponse",
    "KnowledgeStats", "IngestResult", "DocumentInfo",
    "SessionInfo", "SkillInfo", "MemoryFact", "HealthResponse",
    "chunk_to_lc_document", "chunk_from_lc_document",
]