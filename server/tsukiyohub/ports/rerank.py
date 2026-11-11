"""重排端口：检索出候选后，用更贵的模型 / 规则把最相关的几条顶上来。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tsukiyohub.domain import ScoredChunk


@runtime_checkable
class Reranker(Protocol):
    #: 标识（``dashscope-gte-rerank`` / ``llm`` / ``none``）
    name: str
    enabled: bool

    def rerank(self, query: str, chunks: list[ScoredChunk], *, top_n: int | None = None) -> list[ScoredChunk]:
        """按相关度重排并截断；``rerank_score`` 为 0~1 的可比分数。"""
        ...


class NoopReranker:
    """不重排（默认）。"""

    name = "none"
    enabled = False

    def rerank(self, query: str, chunks: list[ScoredChunk], *, top_n: int | None = None) -> list[ScoredChunk]:
        return chunks[:top_n] if top_n else chunks


__all__ = ["Reranker", "NoopReranker"]