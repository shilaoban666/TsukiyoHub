"""依赖装配：把「配置 → 端口实现 → 内核 → 会话编排」收敛到一处。

为什么要有容器：
* **一处装配，处处一致**：FastAPI 接口、脚本、测试都从同一个容器取实例，
  避免两份不同的知识库 / 两次向量化；
* **可替换**：换模型、换向量库、换存储只改这里的两个 ``build_*`` 调用，
  内核与前端都不用动；
* **可降级**：Milvus 连不上 → 退内存检索；没有模型密钥 → 退演示模型；
  Redis/OSS 未配置 → 用无操作的实现。

业务项目里的组织架构、权限、消息渠道都不在这里——它们属于「接入方」，不属于内核。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from tsukiyohub.config import Settings, get_settings
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class Container:
    """一次进程生命周期的全部依赖。字段顺序即装配顺序。"""

    settings: Settings
    # ---- 端口 ----
    router: Any = None              # LLMRouter
    embeddings: Any = None          # CachedEmbeddings
    vector: Any = None              # VectorStore
    blobs: Any = None               # BlobStore
    hot: Any = None                 # HotStore
    reranker: Any = None            # Reranker
    # ---- 持久化 ----
    db: Any = None                  # Database
    sessions: Any = None            # SessionStore
    memory_store: Any = None        # MemoryStore
    kv: Any = None                  # KVStore
    # ---- 内核 ----
    chunker: Any = None
    knowledge: Any = None           # KnowledgeStore
    skills: Any = None              # SkillRegistry
    memory: Any = None              # MemoryService
    agent: Any = None               # ChatAgent
    conversation: Any = None        # ConversationService
    skill_docs: list[Any] = field(default_factory=list)

    started_at: float = field(default_factory=time.time)
    db_error: str | None = None

    # ================================================================ 辅助
    @property
    def db_ready(self) -> bool:
        return self.sessions is not None

    def model_name(self) -> str:
        """当前主模型名（OpenAI 兼容接口里的 ``model`` 字段用它）。"""
        try:
            _provider, model, _ready = self.router.info()
            return model or "tsukiyohub"
        except Exception:  # noqa: BLE001
            return "tsukiyohub"

    def runtime_cfg(self) -> dict[str, Any]:
        """技能注册表用的运行时开关（来自 KV，前端「技能」页可改）。"""
        disabled = set()
        try:
            disabled = set(self.kv.get("skills.disabled") or [])
        except Exception:  # noqa: BLE001 - 读不到就用默认
            pass
        return {"tools": {name: False for name in disabled}, "disabled": sorted(disabled)}

    def describe(self) -> dict[str, Any]:
        provider, model, ready = ("", "", False)
        try:
            provider, model, ready = self.router.info()
        except Exception:  # noqa: BLE001
            pass
        stats = self.knowledge.stats() if self.knowledge is not None else None
        enabled_skills = 0
        if self.skills is not None:
            try:
                enabled_skills = len(self.skills.enabled(self.runtime_cfg()))
            except Exception:  # noqa: BLE001
                enabled_skills = 0
        return {
            "env": self.settings.app_env,
            "llm": f"{provider}:{model}" if ready else "not_configured",
            "embedding": (
                f"{self.embeddings.provider_name}:{self.embeddings.model}"
                if self.embeddings is not None and self.embeddings.enabled else "disabled(bm25-only)"
            ),
            "vector_store": getattr(self.vector, "name", "none"),
            "database": "sqlite" if self.settings.resolved_database_url.startswith("sqlite") else "postgresql",
            "documents": stats.document_count if stats else 0,
            "chunks": stats.chunk_count if stats else 0,
            "skills": enabled_skills,
        }


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_container(settings: Settings | None = None, *, bootstrap: bool = True) -> Container:
    """构造完整依赖图。任何一步失败都抛清晰的中文异常，方便接入方排查。"""
    settings = settings or get_settings()
    settings.ensure_dirs()
    container = Container(settings=settings)

    # ---- 持久化（SQLite 开箱即用 / PostgreSQL 生产）----
    from tsukiyohub.adapters.store_sql import build_stores

    try:
        db, sessions, memory_store, kv = build_stores(settings)
        container.db, container.sessions, container.memory_store, container.kv = db, sessions, memory_store, kv
    except Exception as exc:  # noqa: BLE001
        container.db_error = str(exc)
        logger.error("数据库初始化失败：%s。请检查 DATABASE_URL（留空则用 data/tsukiyohub.db）", exc)
        raise

    # ---- 模型 ----
    from tsukiyohub.adapters.llm_openai import build_router

    container.router = build_router(settings, container.kv)

    # ---- 向量化 + 检索后端 ----
    from tsukiyohub.adapters.embedding import build_embeddings
    from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings

    container.embeddings = CachedEmbeddings(
        build_embeddings(settings), cache_path=settings.index_dir / "embed_cache.jsonl",
    )
    container.vector = _build_vector_store(settings)

    from tsukiyohub.adapters.rerank_dashscope import build_reranker

    container.reranker = build_reranker(settings)

    # ---- 原文件存储 ----
    from tsukiyohub.ports.blob import LocalBlobStore

    container.blobs = LocalBlobStore(settings.data_dir / "files")

    # ---- 热存储（可选）----
    from tsukiyohub.adapters.hot_redis import build_hot_store

    container.hot = build_hot_store(settings)

    # ---- RAG 内核 ----
    from tsukiyohub.kernel.rag.chunker import TextChunker
    from tsukiyohub.kernel.rag.retriever import HybridRetriever
    from tsukiyohub.kernel.rag.store import KnowledgeStore

    container.chunker = TextChunker(settings)
    retriever = HybridRetriever(settings, container.vector, container.embeddings, container.reranker)
    container.knowledge = KnowledgeStore(
        settings, container.vector, container.embeddings, container.chunker, retriever,
    )
    if settings.kb_auto_load_index:
        container.knowledge.load()
    if bootstrap:
        _bootstrap_knowledge(container)

    # ---- 技能内核 ----
    _build_skills(container)

    # ---- 记忆 ----
    from tsukiyohub.kernel.memory.service import MemoryService

    container.memory = MemoryService(settings, container.memory_store, container.router)

    # ---- 聊天内核 ----
    from tsukiyohub.kernel.chat.agent import ChatAgent
    from tsukiyohub.kernel.chat.conversation import ConversationService

    container.agent = ChatAgent(settings, container.router, container.skills, container.kv)
    container.conversation = ConversationService(
        settings, container.sessions, container.memory, container.agent, container.skills, container.hot,
    )

    logger.info("依赖装配完成：%s", container.describe())
    return container


def _build_vector_store(settings: Settings) -> Any:
    """按配置构造检索后端；Milvus 连不上时自动退回内存实现（并明确告警）。"""
    if settings.vector_backend == "milvus":
        try:
            from tsukiyohub.adapters.vector_milvus import build_milvus_store

            store = build_milvus_store(settings)
            logger.info("检索后端：Milvus（%s / %s）", settings.milvus_uri, settings.milvus_collection)
            return store
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 不可用，退回内存检索后端（仅本进程有效，重启后需重建索引）：%s", exc)
    from tsukiyohub.adapters.vector_memory import build_memory_store

    store = build_memory_store(settings)
    logger.info("检索后端：内存混合检索（维度和写入磁盘：%s）", settings.index_dir)
    return store


def _build_skills(container: Container) -> None:
    """注册内置技能 + SKILL.md 技能 + MCP 工具，并应用启停状态。"""
    from tsukiyohub.kernel.skills.registry import SkillRegistry

    settings = container.settings
    registry = SkillRegistry()

    # ---- 内置技能 ----
    from tsukiyohub.kernel.skills.builtin.knowledge import KnowledgeTool
    from tsukiyohub.kernel.skills.builtin.math import CalculatorTool
    from tsukiyohub.kernel.skills.builtin.web import WebFetchTool, WebSearchTool

    registry.register(KnowledgeTool(settings, container.knowledge))
    registry.register(CalculatorTool())
    registry.register(WebFetchTool(settings))
    registry.register(WebSearchTool(settings))

    # 默认启用集合（``SKILLS_ENABLED``）**只作用于内置工具**：
    # SKILL.md 技能与 MCP 工具按各自 front matter / 配置里的 default_enabled 决定，
    # 否则用户写了一个技能却因为没写进环境变量而永远不生效。
    default_on = set(settings.skills_enabled_list)
    for skill in list(registry.all()):
        if getattr(skill, "kind", "tool") != "tool":
            continue
        if skill.name == "knowledge_search":   # 知识库检索是 RAG 内核的底座，永远启用
            continue
        if default_on and skill.name not in default_on:
            registry.set_enabled(skill.name, False)

    # ---- SKILL.md 技能 ----
    try:
        from tsukiyohub.kernel.skills.loader import build_skill_tools, discover

        container.skill_docs = discover(settings.skills_path)
        for tool in build_skill_tools(settings.skills_path, {}):
            registry.register(tool)
        if container.skill_docs:
            logger.info("发现 %d 个 SKILL.md 技能：%s",
                        len(container.skill_docs), ", ".join(s.name for s in container.skill_docs))
    except Exception as exc:  # noqa: BLE001 - 技能加载失败不影响聊天
        logger.warning("SKILL.md 技能加载失败：%s", exc)

    # ---- MCP ----
    if settings.mcp_config_file.strip():
        try:
            from tsukiyohub.kernel.skills.mcp import McpRegistry

            mcp = McpRegistry(settings)
            for tool in mcp.load():
                registry.register(tool)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP 工具加载失败（不影响其它能力）：%s", exc)

    # ---- 恢复用户上次的启停选择 ----
    container.skills = registry
    for name in container.runtime_cfg().get("disabled") or []:
        registry.set_enabled(name, False)


def _bootstrap_knowledge(container: Container) -> None:
    """首次启动时把 ``data/knowledge`` 下的文件增量入库。

    只在「索引为空」且目录里有文件时做全量扫描；已有索引时由 API 的
    ``/api/knowledge/ingest`` 或重启时的增量逻辑处理，避免每次启动都重新解析。
    """
    knowledge = container.knowledge
    try:
        if knowledge.loaded:
            logger.info("已加载知识库索引：%d 篇 / %d 个切块",
                        knowledge.stats().document_count, knowledge.chunk_count)
            return
        root = container.settings.knowledge_dir
        if not root.is_dir() or not any(root.rglob("*")):
            logger.info("知识库为空且 %s 下没有文件；可以上传文档或调用 /api/knowledge/ingest", root)
            return
        results = knowledge.ingest_directory(root)
        stats = knowledge.stats()
        failed = [r for r in results if r.error]
        logger.info("启动导入完成：%d 篇 / %d 个切块（失败 %d）",
                    stats.document_count, stats.chunk_count, len(failed))
    except Exception as exc:  # noqa: BLE001 - 导入失败不阻止服务启动
        logger.error("启动导入失败（服务继续启动，可稍后调 /api/knowledge/reindex）：%s", exc)


# ---------------------------------------------------------------------------
# 进程级单例（FastAPI 依赖注入）
# ---------------------------------------------------------------------------
_CONTAINER: Container | None = None


def get_container() -> Container:
    global _CONTAINER
    if _CONTAINER is None:
        _CONTAINER = build_container()
    return _CONTAINER


def set_container(container: Container | None) -> None:
    """替换容器（测试注入 / 热重载）。"""
    global _CONTAINER
    _CONTAINER = container


__all__ = ["Container", "build_container", "get_container", "set_container"]