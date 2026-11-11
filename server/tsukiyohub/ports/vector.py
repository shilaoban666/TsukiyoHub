"""向量 / 混合检索端口。

内核通过这个端口做「写入索引 → 向量召回 + 关键词召回 → 融合 → 过滤」，
具体实现可以是 Milvus（生产）、进程内内存索引（演示 / 单测），或接入方自己的 ES / pgvector。

**成熟的检索后端应当自己完成融合**（Milvus 原生支持稠密向量 + 稀疏 BM25 混合检索）；
``tsukiyohub.kernel.rag.retriever`` 只负责发起查询、过滤与重排。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from tsukiyohub.domain import Chunk, ScoredChunk


@dataclass
class SearchFilters:
    """检索过滤条件（只做「缩小范围」，不做打分）。"""

    doc_types: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: 是否只返回启用中的文档（停用版本切换）
    active_only: bool = True

    def is_empty(self) -> bool:
        return not (self.doc_types or self.doc_ids or self.tags)

    def to_milvus(self) -> str:
        """翻译成 Milvus 过滤表达式。"""
        parts: list[str] = []
        if self.doc_types:
            quoted = ", ".join(f'"{_escape(v)}"' for v in self.doc_types)
            parts.append(f"doc_type in [{quoted}]")
        if self.doc_ids:
            quoted = ", ".join(f'"{_escape(v)}"' for v in self.doc_ids)
            parts.append(f"doc_id in [{quoted}]")
        if self.tags:
            quoted = ", ".join(f'"{_escape(v)}"' for v in self.tags)
            parts.append(f"ARRAY_CONTAINS_ANY(tags, [{quoted}])")
        if self.active_only:
            parts.append("active == true")
        return " and ".join(parts)


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class IndexRow:
    """一行待写入索引的切块（切块 + 向量）。"""

    chunk: Chunk
    vector: list[float]


@runtime_checkable
class VectorStore(Protocol):
    """检索后端的统一契约。"""

    #: ``milvus`` / ``memory`` / 自定义
    name: str
    #: 是否支持「稠密向量 + 关键词」混合召回（False 时内核退化为纯向量）
    supports_hybrid: bool
    #: 是否支持重启后自恢复（Milvus 为 True；内存实现落盘 JSON 也为 True）
    persistent: bool

    def upsert(self, rows: list[IndexRow]) -> int:
        """写入 / 覆盖切块，返回写入条数。"""
        ...

    def remove(self, *, doc_id: str | None = None, chunk_ids: list[str] | None = None,
               version_id: str | None = None) -> int:
        """删除切块，返回删除条数。"""
        ...

    def search(self, *, vector: list[float] | None, query_text: str, top_k: int = 20,
               filters: SearchFilters | None = None) -> list[ScoredChunk]:
        """混合检索：``vector`` 为空时退化为关键词检索。"""
        ...

    def count(self) -> int:
        ...

    def get(self, chunk_ids: list[str]) -> list[Chunk]:
        ...

    def set_flags(self, **flags: Any) -> int:
        """切换 active / current 等标记（不重新向量化）。"""
        ...

    def persist(self) -> None:
        """落盘（无状态后端可为空实现）。"""
        ...

    def clear(self) -> None:
        ...


__all__ = ["VectorStore", "IndexRow", "SearchFilters", "Literal"]