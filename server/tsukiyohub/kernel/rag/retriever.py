"""检索编排：一次查询 → 后端混合召回 → 过滤 → 可选重排 → 每文档上限。

职责边界（很重要）
------------------
* **召回**交给 :class:`~tsukiyohub.ports.vector.VectorStore`：成熟后端（Milvus）自己就做
  稠密 + 稀疏 BM25 + 融合；内存后端用 :mod:`tsukiyohub.kernel.rag.fusion` 做同样的事。
  所以本模块只发起**一次** ``backend.search``，不拆成两路——这是 Lead 冻结的端口语义。
* **过滤**下推给后端，返回后再用 :func:`chunk_matches` 兜一层（防止某些后端忽略字段）。
  越权 / 串库这类问题，双保险比单点信任便宜。
* **重排**分两级：L1 是零成本的词面加权（只有后端不做词法融合时才需要，例如纯稠密后端）；
  L2 是可选的模型重排（:class:`~tsukiyohub.ports.rerank.Reranker`，默认关闭）。

分数语义：``score`` 是融合分（只反映名次，跨查询不可比），``vector_score`` 是**原始余弦**
（跨查询可比，证据闸门用它）。两者不要混用。
"""

from __future__ import annotations

from tsukiyohub.config import Settings
from tsukiyohub.domain import ScoredChunk
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings
from tsukiyohub.kernel.rag.fusion import cap_per_doc, chunk_matches, l1_rerank
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.rerank import Reranker
from tsukiyohub.ports.vector import SearchFilters, VectorStore

logger = get_logger(__name__)


class HybridRetriever:
    """向量 + 关键词混合检索器。

    Args:
        settings: 全局配置（召回数、权重、阈值、每文档上限都从这里取）。
        backend: 检索后端（``memory`` / ``milvus`` / 接入方自研）。
        embeddings: 带缓存的向量化器（``enabled=False`` 时自动退化为纯关键词检索）。
        reranker: 可选 L2 重排；``None`` 或 ``enabled=False`` 时不重排。
    """

    def __init__(
        self,
        settings: Settings,
        backend: VectorStore,
        embeddings: CachedEmbeddings,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.embeddings = embeddings
        self.reranker = reranker

    # ------------------------------------------------------------------ 公开
    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: SearchFilters | None = None,
        doc_types: list[str] | None = None,
    ) -> list[ScoredChunk]:
        """检索最相关的切块。

        Args:
            query: 用户问题原文（空白直接返回空列表，不打扰后端）。
            top_k: 返回条数；缺省用 ``settings.retrieval_top_k``。
            filters: 过滤条件（文档类型 / 文档 ID / 标签 / 是否只含启用中的）。
            doc_types: 便捷参数，等价于往 ``filters.doc_types`` 里塞值；两者都给时以它为并集。

        Returns:
            按融合分降序的命中列表；后端不可用时返回空列表（上层走「知识库暂无」话术）。
        """
        text = (query or "").strip()
        if not text:
            return []
        top_k = max(1, int(top_k or self.settings.retrieval_top_k))
        filters = self._merge_filters(filters, doc_types)
        recall = max(int(self.settings.retrieval_vector_k), int(self.settings.retrieval_bm25_k), top_k)

        vector: list[float] | None = None
        if self.embeddings.enabled:
            try:
                candidate = self.embeddings.embed_for_query([text])[0]
                vector = candidate or None
            except Exception as exc:  # noqa: BLE001 - 向量失败不阻断关键词检索
                logger.warning("查询向量化失败，本次只用关键词检索：%s", exc)

        try:
            items = self.backend.search(vector=vector, query_text=text, top_k=recall, filters=filters)
        except Exception as exc:  # noqa: BLE001 - 检索失败返回空，由上层降级
            logger.warning("检索后端 %s 查询失败：%s", getattr(self.backend, "name", "?"), exc)
            return []

        items = [item for item in items if chunk_matches(item.chunk, filters)]
        if not items:
            logger.info("检索无命中：query=%r（后端=%s）", text[:50], getattr(self.backend, "name", "?"))
            return []

        # 纯稠密后端没有词法通路：用零成本的 L1 词面加权补上「问到的词是否真的出现」
        if not bool(getattr(self.backend, "supports_hybrid", True)):
            items = l1_rerank(text, items)

        items = self._maybe_rerank(text, items)
        items = [item for item in items if item.score >= float(self.settings.retrieval_min_score)]
        if not items:
            return []

        if bool(getattr(self.settings, "rerank_enabled", False)):
            items = cap_per_doc(items, int(self.settings.rerank_max_chunks), top_k)
        else:
            items = items[:top_k]
        logger.info(
            "检索完成：query=%r 返回=%d（后端=%s 向量=%s）",
            text[:40], len(items), getattr(self.backend, "name", "?"), "on" if vector else "off",
        )
        return items

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _merge_filters(filters: SearchFilters | None, doc_types: list[str] | None) -> SearchFilters:
        """合并过滤条件，且**不修改**调用方传进来的对象（它可能被复用）。"""
        base = filters or SearchFilters()
        wanted = [str(value).strip() for value in (doc_types or []) if str(value).strip()]
        if not wanted:
            return base
        merged = sorted({*[str(value).strip() for value in base.doc_types], *wanted})
        return SearchFilters(
            doc_types=merged,
            doc_ids=list(base.doc_ids),
            tags=list(base.tags),
            active_only=base.active_only,
        )

    def _maybe_rerank(self, query: str, items: list[ScoredChunk]) -> list[ScoredChunk]:
        """L2 模型重排（可选）。失败时保留原顺序，绝不让重排拖垮检索。"""
        reranker = self.reranker
        if reranker is None or not bool(getattr(reranker, "enabled", False)) or len(items) < 2:
            return items
        try:
            reranked = reranker.rerank(query, items, top_n=min(len(items), 50))
        except Exception as exc:  # noqa: BLE001
            logger.warning("重排失败，保留融合顺序：%s", exc)
            return items
        return reranked or items


__all__ = ["HybridRetriever"]