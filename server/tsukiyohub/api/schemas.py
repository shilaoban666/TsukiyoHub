"""API 层专属的请求 / 响应模型（跨层契约在 ``domain.py``）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tsukiyohub.domain import ChatResponse, Citation, DocumentInfo, MemoryFact, ScoredChunk, SessionInfo, SkillInfo


# ---------------------------------------------------------------------------
# 技能
# ---------------------------------------------------------------------------
class SkillToggleRequest(BaseModel):
    enabled: bool


class SkillListResponse(BaseModel):
    items: list[SkillInfo] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class SessionCreateRequest(BaseModel):
    user_id: str | None = None
    channel: str = "web"
    title: str = ""


class SessionListResponse(BaseModel):
    items: list[SessionInfo] = Field(default_factory=list)
    total: int = 0


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: str = ""
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int = 0


class SessionDetail(BaseModel):
    session: SessionInfo
    messages: list[MessageOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    """导入服务器上已有的目录 / 文件（相对 data/ 或绝对路径均可）。"""

    path: str = Field(..., description="目录或文件路径；相对路径按 data/ 解析")
    doc_type: str = "doc"
    force: bool = False


class IngestResponse(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list)
    added: int = 0
    skipped: int = 0
    failed: int = 0


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(8, ge=1, le=30)
    doc_types: list[str] | None = None


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float = 0.0
    vector_score: float | None = None
    bm25_score: float | None = None
    rerank_score: float | None = None
    retriever: str = "hybrid"
    uri: str = ""


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    latency_ms: int = 0
    grounded: bool = False
    grounding_score: float | None = None


class DocumentListResponse(BaseModel):
    items: list[DocumentInfo] = Field(default_factory=list)
    total: int = 0


class DeleteDocumentResponse(BaseModel):
    doc_id: str
    removed_chunks: int = 0


class ReindexResponse(BaseModel):
    documents: int = 0
    chunks: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------
class MemoryListResponse(BaseModel):
    user_id: str
    items: list[MemoryFact] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------
class SystemConfig(BaseModel):
    """对外可见的配置（**绝不含密钥**）。"""

    app_name: str = ""
    version: str = ""
    env: str = ""
    bot_name: str = ""
    model: str = ""
    provider: str = ""
    llm_ready: bool = False
    fallback_model: str = ""
    embedding_model: str = ""
    embedding_ready: bool = False
    vector_backend: str = ""
    database: str = ""
    retrieval: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)


class SystemConfigUpdate(BaseModel):
    """可运行时修改的配置子集（落库到 KV，不改 .env）。"""

    bot_name: str | None = None
    persona_prompt: str | None = None
    default_tone: str | None = None
    default_length: str | None = None
    default_markdown: bool | None = None
    fallback_text: str | None = None
    retrieval_top_k: int | None = Field(None, ge=1, le=30)
    skills_enabled: list[str] | None = None
    memory_enabled: bool | None = None


# ---------------------------------------------------------------------------
# 通用包装
# ---------------------------------------------------------------------------
class ChatEnvelope(BaseModel):
    """``/api/chat`` 的响应包装：兼容只想拿一句话的调用方。"""

    code: int = 0
    message: str = "ok"
    data: ChatResponse


class ChunkDebug(BaseModel):
    chunk_id: str
    text: str
    score: float


__all__ = [
    "SkillToggleRequest", "SkillListResponse",
    "SessionCreateRequest", "SessionListResponse", "MessageOut", "SessionDetail",
    "IngestRequest", "IngestResponse", "SearchRequest", "SearchHit", "SearchResponse",
    "DocumentListResponse", "DeleteDocumentResponse", "ReindexResponse",
    "MemoryListResponse", "SystemConfig", "SystemConfigUpdate",
    "ChatEnvelope", "ChunkDebug", "ScoredChunk",
]