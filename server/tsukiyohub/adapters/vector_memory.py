"""内存向量后端：BM25（jieba 分词）+ 余弦 + 加权 RRF 融合，JSONL 落盘。

为什么要一个内存后端
--------------------
1. **零依赖可跑**：不装 Milvus / 不连数据库也能完成「入库 → 混合检索 → 证据闸门」，
   本地演示、单测、离线环境全靠它；
2. **行为基准**：它用内核自己的 :mod:`tsukiyohub.kernel.rag.fusion` 做融合，所以
   「标准融合行为」由它定义，Milvus 后端只要对齐同一套分数刻度（原始余弦、RRF）即可；
3. **可持久化**：``persist_path`` 每行一条 JSON（切块 + 向量），重启自动恢复，坏行跳过。

融合与刻度（与 Milvus 后端必须一致，conformance test 覆盖）
-----------------------------------------------------------
* 稠密分 = **原始余弦**（向量在写入与查询时都 L2 归一化，点积即余弦），不做任何非线性变换；
* 词法分 = BM25（``k1=1.5, b=0.75``，中文按 jieba 分词、英文按词）；
* 融合分 = 加权 RRF（``k=60``，默认 ``0.6/0.4``），与 :func:`fusion.reciprocal_rank_fusion` 同一实现；
* 只有一路召回时按单路 RRF 计分（保持与两路同一量级，便于 ``min_score`` 统一比较）。
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tsukiyohub.config import Settings
from tsukiyohub.domain import Chunk, ScoredChunk
from tsukiyohub.kernel.rag.fusion import chunk_matches, reciprocal_rank_fusion
from tsukiyohub.kernel.rag.tokenize import tokenize
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.vector import IndexRow, SearchFilters

logger = get_logger(__name__)

#: BM25 参数（1.5/0.75 是通用默认值；中文短文用 1.2 也常见，这里不追求调参）
BM25_K1 = 1.5
BM25_B = 0.75
#: 落盘格式版本
PERSIST_VERSION = 1
#: 融合默认权重 / 平滑常数（与 ``settings.retrieval_*`` 默认一致）
DEFAULT_VECTOR_WEIGHT = 0.6
DEFAULT_BM25_WEIGHT = 0.4
DEFAULT_FUSION_K = 60


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm == 0 else [value / norm for value in vector]


@dataclass
class _Row:
    """索引里的一行：切块 + 归一化向量 + 词法统计。"""

    chunk: Chunk
    dense: list[float]
    tf: dict[str, int]
    length: int


class MemoryVectorStore:
    """进程内混合检索后端（实现 :class:`~tsukiyohub.ports.vector.VectorStore`）。

    Args:
        dimension: 向量维度（与向量化器一致；不一致的向量按「无稠密向量」处理）。
        persist_path: 落盘文件（JSONL）；``None`` = 不落盘。
        vector_weight: 稠密一路的 RRF 权重。
        bm25_weight: 词法一路的 RRF 权重。
        fusion_k: RRF 平滑常数。
        use_numpy: 用 numpy 加速稠密点积（大索引时可开；默认纯 Python，零依赖）。
    """

    name = "memory"
    supports_hybrid = True

    def __init__(
        self,
        *,
        dimension: int = 1024,
        persist_path: Path | None = None,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        bm25_weight: float = DEFAULT_BM25_WEIGHT,
        fusion_k: int = DEFAULT_FUSION_K,
        use_numpy: bool = False,
    ) -> None:
        self.dimension = max(8, int(dimension or 1024))
        self.persist_path = Path(persist_path) if persist_path is not None else None
        self.vector_weight = float(vector_weight)
        self.bm25_weight = float(bm25_weight)
        self.fusion_k = int(fusion_k)
        self.use_numpy = bool(use_numpy)
        self._rows: dict[str, _Row] = {}
        self._df: dict[str, int] = defaultdict(int)
        self._total_length = 0
        self._lock = threading.RLock()
        self._dirty = False
        if self.persist_path is not None:
            self._load()

    @property
    def persistent(self) -> bool:
        """是否支持重启自恢复。"""
        return self.persist_path is not None

    # ================================================================ 写入
    def upsert(self, rows: list[IndexRow]) -> int:
        """写入 / 覆盖切块（同 ``chunk_id`` 全量覆盖，并维护 BM25 统计）。"""
        if not rows:
            return 0
        with self._lock:
            for row in rows:
                chunk_id = row.chunk.chunk_id
                if chunk_id in self._rows:
                    self._drop_row(chunk_id)
                vector = [float(value) for value in row.vector] if row.vector else []
                if vector and len(vector) != self.dimension:
                    logger.warning("切块 %s 的向量维度 %d 与索引维度 %d 不一致，按无稠密向量处理",
                                   chunk_id, len(vector), self.dimension)
                    vector = []
                tokens = tokenize(row.chunk.text)
                counts: dict[str, int] = defaultdict(int)
                for token in tokens:
                    counts[token] += 1
                tf = dict(counts)
                self._rows[chunk_id] = _Row(
                    chunk=row.chunk,
                    dense=_normalize(vector) if vector else [],
                    tf=tf,
                    length=len(tokens),
                )
                for token in tf:
                    self._df[token] += 1
                self._total_length += len(tokens)
            self._dirty = True
        return len(rows)

    def remove(
        self,
        *,
        doc_id: str | None = None,
        chunk_ids: list[str] | None = None,
        version_id: str | None = None,
    ) -> int:
        """删除切块，返回删除条数。

        安全约束（与源实现一致）：**所有条件都为 None 时返回 0，绝不清库**——
        一次参数漏传不该把整个知识库删掉。
        """
        if doc_id is None and chunk_ids is None and version_id is None:
            logger.warning("remove() 未给任何条件，按安全约定不做任何删除")
            return 0
        targets: list[str] = []
        with self._lock:
            if chunk_ids:
                targets.extend(chunk_id for chunk_id in chunk_ids)
            if doc_id is not None:
                targets.extend(cid for cid, row in self._rows.items() if row.chunk.doc_id == doc_id)
            if version_id is not None:
                targets.extend(
                    cid for cid, row in self._rows.items()
                    if str(row.chunk.extra.get("version_id") or "") == str(version_id)
                )
            removed = 0
            for chunk_id in dict.fromkeys(targets):
                if chunk_id in self._rows:
                    self._drop_row(chunk_id)
                    removed += 1
            if removed:
                self._dirty = True
        return removed

    def set_flags(self, *, doc_id: str | None = None, chunk_ids: list[str] | None = None,
                  version_id: str | None = None, **flags: object) -> int:
        """切换切块标记（``active`` / ``current`` …），不重新向量化。"""
        if not flags:
            return 0
        with self._lock:
            updated = 0
            for chunk_id, row in self._rows.items():
                if chunk_ids and chunk_id not in set(chunk_ids):
                    continue
                if doc_id is not None and row.chunk.doc_id != doc_id:
                    continue
                if version_id is not None and str(row.chunk.extra.get("version_id") or "") != str(version_id):
                    continue
                row.chunk.extra = {**row.chunk.extra, **flags}
                updated += 1
            if updated:
                self._dirty = True
        return updated

    def clear(self) -> None:
        """清空索引（显式调用才清）。"""
        with self._lock:
            self._rows.clear()
            self._df.clear()
            self._total_length = 0
            self._dirty = True

    # ================================================================ 检索
    def search(
        self,
        *,
        vector: list[float] | None,
        query_text: str,
        top_k: int = 20,
        filters: SearchFilters | None = None,
    ) -> list[ScoredChunk]:
        """混合检索：稠密（余弦）+ 词法（BM25）→ 加权 RRF。

        Args:
            vector: 查询向量；``None`` / 空 / 维度不符时退化为纯关键词检索。
            query_text: 查询原文（BM25 输入，与索引期同一分词函数）。
            top_k: 返回条数。
            filters: 过滤条件（本地逐条判断）。

        Returns:
            按融合分降序的命中；两路都空时返回 ``[]``（上层据此走「暂无」话术）。
        """
        limit = max(1, int(top_k))
        query = (query_text or "").strip()
        matching = self._matching_rows(filters)

        dense_hits: list[tuple[Chunk, float]] = []
        if vector and len(vector) == self.dimension:
            query_vector = _normalize([float(value) for value in vector])
            dense_hits = sorted(
                ((row.chunk, self._dot(query_vector, row.dense)) for row in matching if row.dense),
                key=lambda item: item[1],
                reverse=True,
            )[:limit]

        lexical_hits: list[tuple[Chunk, float]] = []
        if query:
            lexical_hits = self._lexical(query, matching, limit)

        if not dense_hits and not lexical_hits:
            return []

        vector_scores = {chunk.chunk_id: score for chunk, score in dense_hits}
        bm25_scores = {chunk.chunk_id: score for chunk, score in lexical_hits}
        ranked: list[tuple[list[Chunk], float]] = []
        if dense_hits:
            ranked.append(([chunk for chunk, _ in dense_hits], self.vector_weight))
        if lexical_hits:
            ranked.append(([chunk for chunk, _ in lexical_hits], self.bm25_weight))

        fused = reciprocal_rank_fusion(ranked, k=self.fusion_k)
        for item in fused:
            chunk_id = item.chunk.chunk_id
            item.vector_score = vector_scores.get(chunk_id)
            item.bm25_score = bm25_scores.get(chunk_id)
            if chunk_id in vector_scores and chunk_id in bm25_scores:
                item.retriever = "hybrid"
            elif chunk_id in vector_scores:
                item.retriever = "vector"
            else:
                item.retriever = "bm25"
        return fused[:limit]

    def count(self) -> int:
        """索引里的切块总数。"""
        with self._lock:
            return len(self._rows)

    def get(self, chunk_ids: list[str]) -> list[Chunk]:
        """按 ID 取切块（顺序按入参，找不到的跳过）。"""
        if not chunk_ids:
            return []
        with self._lock:
            return [self._rows[chunk_id].chunk for chunk_id in chunk_ids if chunk_id in self._rows]

    def stats(self) -> dict[str, object]:
        """后端自身状态（健康检查 / 调试用）。"""
        with self._lock:
            return {
                "name": self.name,
                "rows": len(self._rows),
                "dimension": self.dimension,
                "avg_doc_length": round(self._total_length / len(self._rows), 2) if self._rows else 0.0,
                "vocabulary": len(self._df),
                "persist_path": str(self.persist_path) if self.persist_path else None,
            }

    # ================================================================ 持久化
    def persist(self) -> None:
        """整库写 JSONL（原子替换）；失败只记日志。"""
        path = self.persist_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                with self._lock:
                    rows = list(self._rows.values())
                for row in rows:
                    payload = {
                        "version": PERSIST_VERSION,
                        "chunk": row.chunk.model_dump(mode="json"),
                        "dense": [round(value, 6) for value in row.dense],
                    }
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            os.replace(temporary, path)
            self._dirty = False
            logger.debug("内存索引已落盘：%d 行 → %s", len(rows), path)
        except OSError as exc:
            logger.error("内存索引落盘失败：%s", exc)

    # ---------------------------------------------------------------- 内部
    def _matching_rows(self, filters: SearchFilters | None) -> list[_Row]:
        with self._lock:
            rows = list(self._rows.values())
        if filters is None:
            return rows
        return [row for row in rows if chunk_matches(row.chunk, filters)]

    def _lexical(self, query: str, matching: list[_Row], limit: int) -> list[tuple[Chunk, float]]:
        """BM25 打分：``idf(t) * tf*(k1+1) / (tf + k1*(1-b+b*dl/avgdl))``。"""
        query_tokens = list(dict.fromkeys(tokenize(query)))
        if not query_tokens:
            return []
        with self._lock:
            total = len(self._rows)
            avgdl = (self._total_length / total) if total else 0.0
            df = {token: self._df.get(token, 0) for token in query_tokens}
        if avgdl <= 0:
            return []
        scored: list[tuple[Chunk, float]] = []
        for row in matching:
            score = 0.0
            for token in query_tokens:
                frequency = row.tf.get(token, 0)
                if not frequency:
                    continue
                token_df = df.get(token, 0)
                idf = math.log(1 + (total - token_df + 0.5) / (token_df + 0.5)) if token_df else 0.0
                denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * row.length / avgdl)
                score += idf * frequency * (BM25_K1 + 1) / denominator
            if score > 0:
                scored.append((row.chunk, round(score, 6)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        """点积（两侧都已 L2 归一化，等价余弦）。"""
        if not left or not right or len(left) != len(right):
            return 0.0
        return float(sum(a * b for a, b in zip(left, right, strict=True)))

    def _drop_row(self, chunk_id: str) -> None:
        """删除一行并回退 BM25 统计（调用方需持锁）。"""
        row = self._rows.pop(chunk_id, None)
        if row is None:
            return
        for token in row.tf:
            remaining = self._df.get(token, 0) - 1
            if remaining > 0:
                self._df[token] = remaining
            else:
                self._df.pop(token, None)
        self._total_length = max(0, self._total_length - row.length)

    def _load(self) -> None:
        """从 JSONL 恢复；文件缺失 / 坏行 / 版本不符都只跳过，不抛错。"""
        path = self.persist_path
        assert path is not None
        if not path.exists():
            return
        loaded = 0
        skipped = 0
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("内存索引读取失败：%s", exc)
            return
        for line_number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
                if int(item.get("version") or 0) != PERSIST_VERSION:
                    skipped += 1
                    continue
                chunk = Chunk.model_validate(item["chunk"])
                vector = [float(value) for value in item.get("dense") or []]
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("索引文件第 %d 行无法解析，已跳过：%s", line_number, exc)
                skipped += 1
                continue
            if vector and len(vector) != self.dimension:
                logger.warning("切块 %s 的向量维度 %d 与索引维度 %d 不一致，忽略其稠密向量",
                               chunk.chunk_id, len(vector), self.dimension)
                vector = []
            tokens = tokenize(chunk.text)
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            self._rows[chunk.chunk_id] = _Row(
                chunk=chunk,
                dense=_normalize(vector) if vector else [],
                tf=dict(counts),
                length=len(tokens),
            )
            for token in counts:
                self._df[token] += 1
            self._total_length += len(tokens)
            loaded += 1
        if loaded:
            logger.info("内存索引已恢复：%d 行（跳过 %d 行）← %s", loaded, skipped, path)


def build_memory_store(settings: Settings) -> MemoryVectorStore:
    """按配置构造内存后端。

    落盘文件固定为 ``<data>/index/kb_index_vectors.jsonl``（与 :class:`KnowledgeStore` 的
    清单 / 切块文件同目录），方便整体迁移与备份。
    """
    persist_path = settings.index_path.with_name(settings.index_path.name + "_vectors.jsonl")
    store = MemoryVectorStore(
        dimension=int(settings.embedding_dimension or 1024),
        persist_path=persist_path,
        vector_weight=float(settings.retrieval_vector_weight),
        bm25_weight=float(settings.retrieval_bm25_weight),
        fusion_k=int(settings.retrieval_fusion_k),
    )
    logger.info("检索后端就绪：memory（%d 行，落盘 %s）", store.count(), persist_path)
    return store


__all__ = [
    "BM25_B", "BM25_K1", "MemoryVectorStore", "PERSIST_VERSION", "build_memory_store",
]