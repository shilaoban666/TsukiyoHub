"""多路查询检索（可选模块，默认不启用）：同一个问题用多种说法并行召回，再融合。

为什么需要它
------------
口语提问和文档用词经常不一致（「酒店一晚能报多少」vs「住宿费标准」）。单路检索靠向量
能缓解，但**词形差异大**时仍然会漏。多路查询把「改写、拆分、术语书面化、HyDE」等变体
各召回一次，用 RRF 融合——一条路漏掉的，另一条路通常能补上。

它为什么是可选模块
------------------
每次多路召回都会放大检索开销；主链路（:class:`~tsukiyohub.kernel.rag.retriever.HybridRetriever`）
已经能覆盖大多数场景。接入方在**确认召回不足**时再打开它，并注意：

* 子查询最多 6 条（去重后），再多收益递减、延迟上升；
* ``deadline`` 到达时已完成的路照常融合，未完成的放弃并标记 ``partial=True``；
* ``kind="hyde"`` 的子查询只用词法召回（假设答案文本不走向量匹配）。

本模块不依赖任何模型客户端：改写 / 扩写由调用方（chat 或接入方）产出子查询文本。
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from tsukiyohub.config import Settings
from tsukiyohub.domain import Chunk, ScoredChunk
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings
from tsukiyohub.kernel.rag.fusion import (
    cap_per_doc,
    dedupe_parents,
    dedupe_versions,
    keyword_heavy,
    l1_rerank,
    norm_query,
    reciprocal_rank_fusion,
)
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.rerank import Reranker
from tsukiyohub.ports.vector import SearchFilters, VectorStore

logger = get_logger(__name__)

#: 多路召回的线程池（模块级单例；检索是 IO / 外部服务等待型任务）
MAX_WORKERS = 16
#: 子查询上限
MAX_QUERIES = 6

_POOL: ThreadPoolExecutor | None = None


def pool() -> ThreadPoolExecutor:
    """取出（或惰性创建）检索线程池。"""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="rag")
    return _POOL


@dataclass(frozen=True)
class SubQuery:
    """一路子查询。

    Attributes:
        text: 查询文本。
        kind: ``original`` / ``rewrite`` / ``sub`` / ``keyword`` / ``hyde`` / ``expand``（只用于观测与权重）。
        weight: 这一路在融合时的权重；``<= 0`` 表示不参与。
        bm25_only: 只走关键词召回（编号、型号类问题用）。
    """

    text: str
    kind: str = "original"
    weight: float = 1.0
    bm25_only: bool = False


@dataclass
class RetrievalRequest:
    """一次多路检索请求。"""

    queries: list[SubQuery]
    top_k: int = 8
    per_query_k: int = 20
    filters: SearchFilters = field(default_factory=SearchFilters)
    max_per_doc: int = 3
    #: ``time.monotonic()`` 截止时刻；``None`` = 不限制
    deadline: float | None = None
    #: 已算好的查询向量（例如 HyDE 文本的向量），省一次向量化
    vectors: dict[str, list[float]] | None = None


@dataclass
class RetrievalResult:
    """一次多路检索的结果。"""

    items: list[ScoredChunk] = field(default_factory=list)
    #: 每路召回条数（``kind:文本前 24 字`` → 条数），调参用
    per_query: dict[str, int] = field(default_factory=dict)
    timings: dict[str, int] = field(default_factory=dict)
    #: 有路被截止时间取消（结果是部分召回）
    partial: bool = False
    rerank: str = "L1"
    #: 融合后的前 10 个（重排前的分数快照），逐层排障用
    pre_rerank: list[tuple[str, float]] = field(default_factory=list)


class MultiQueryRetriever:
    """多路查询检索器（可选）。

    Args:
        settings: 全局配置。
        backend: 检索后端（每次子查询调用一次 ``search``，由后端自己融合两路召回）。
        embeddings: 向量化器（``enabled=False`` 时子查询全部退化为纯关键词）。
        reranker: 可选 L2 重排。
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
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """并行召回多路 → 加权 RRF → 去冗余 → 每文档上限。"""
        started = time.perf_counter()
        result = RetrievalResult()
        queries = self._dedupe(request.queries)[:MAX_QUERIES]
        if not queries:
            return result

        filters = request.filters or SearchFilters()
        per_query_k = max(1, int(request.per_query_k))
        vectors: dict[str, list[float]] = dict(request.vectors or {})
        t0 = time.perf_counter()
        use_dense = self.embeddings.enabled
        pending_vectors = [q for q in queries if not q.bm25_only and q.text not in vectors]
        if use_dense and pending_vectors:
            for sub, vector in zip(
                pending_vectors,
                self.embeddings.embed_for_query([sub.text for sub in pending_vectors]),
                strict=True,
            ):
                if vector:
                    vectors[sub.text] = vector
        result.timings["embed"] = int((time.perf_counter() - t0) * 1000)

        jobs: dict[Future[list[ScoredChunk]], tuple[SubQuery, float]] = {}
        for sub in queries:
            if sub.weight <= 0:
                continue
            weight = float(sub.weight)
            if keyword_heavy(sub.text) and not sub.bm25_only:
                weight *= 1.1  # 编号 / 金额类问题，词法通路本来就该更重一点
            vector = None if sub.bm25_only or sub.kind == "hyde" else vectors.get(sub.text)
            jobs[pool().submit(self.backend.search, vector=vector, query_text=sub.text,
                               top_k=per_query_k, filters=filters)] = (sub, weight)

        timeout = None if request.deadline is None else max(0.05, request.deadline - time.monotonic())
        done, pending = wait(jobs, timeout=timeout)
        for future in pending:
            future.cancel()
        result.partial = bool(pending)
        t1 = time.perf_counter()
        result.timings["search"] = int((t1 - t0) * 1000)

        ranked: list[tuple[list[Chunk], float]] = []
        for future in done:
            sub, weight = jobs[future]
            try:
                hits = future.result()
            except Exception as exc:  # noqa: BLE001 - 一路失败不影响其它路
                logger.warning("多路检索中 %r 失败：%s", sub.text[:30], exc)
                continue
            key = f"{sub.kind}:{sub.text[:24]}"
            result.per_query[key] = result.per_query.get(key, 0) + len(hits)
            ranked.append(([item.chunk for item in hits], weight))

        fused = reciprocal_rank_fusion(ranked, k=int(self.settings.retrieval_fusion_k))
        if not fused:
            return result
        if not bool(getattr(self.backend, "supports_hybrid", True)):
            fused = l1_rerank(request.queries[0].text, fused)
        fused = dedupe_versions(fused)
        fused = dedupe_parents(fused)
        result.pre_rerank = [(item.chunk.chunk_id, item.score) for item in fused[:10]]
        result.rerank = "L1"

        reranked = self._maybe_rerank(request.queries[0].text, fused)
        result.items = cap_per_doc(reranked, request.max_per_doc, request.top_k)
        result.timings["total"] = int((time.perf_counter() - started) * 1000)
        logger.info(
            "多路检索完成：%d 路 / 融合 %d 条 / 返回 %d 条（%dms）",
            len(ranked), len(fused), len(result.items), result.timings["total"],
        )
        return result

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _dedupe(queries: list[SubQuery]) -> list[SubQuery]:
        """按「去标点小写」合并重复子查询：权重相加，保留首个的文本与类型。"""
        merged: dict[str, SubQuery] = {}
        for sub in queries:
            if not (sub.text or "").strip():
                continue
            key = norm_query(sub.text) + ("#bm25" if sub.bm25_only else "")
            previous = merged.get(key)
            merged[key] = sub if previous is None else SubQuery(
                previous.text, previous.kind, previous.weight + sub.weight, previous.bm25_only
            )
        return list(merged.values())

    def _maybe_rerank(self, query: str, items: list[ScoredChunk]) -> list[ScoredChunk]:
        reranker = self.reranker
        if reranker is None or not bool(getattr(reranker, "enabled", False)) or len(items) < 2:
            return items
        try:
            reranked = reranker.rerank(query, items, top_n=min(len(items), 50))
        except Exception as exc:  # noqa: BLE001
            logger.warning("多路检索重排失败，保留融合顺序：%s", exc)
            return items
        return reranked or items


__all__ = [
    "MAX_QUERIES", "MAX_WORKERS", "MultiQueryRetriever", "RetrievalRequest", "RetrievalResult",
    "SubQuery", "pool",
]