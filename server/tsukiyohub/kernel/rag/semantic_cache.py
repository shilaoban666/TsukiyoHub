"""语义缓存（可选模块，**默认关闭**）：同义换说法的问题直接复用上次的好答案。

条件必须严，否则「准确」会被缓存拖垮——这是源项目线上踩过的坑：

===========  ==================================================================
条件          做法
===========  ==================================================================
匹配          问题向量余弦 ≥ ``threshold``（默认 0.95；用测评集的同义对校准后再开）
作用域一致    ``scope`` 相同才共用（调用方把「可见文档集合 / 租户」编码进 scope）
不个性化      依赖上文 / 记忆 / 附件的问题不进缓存（由 ``is_cacheable`` 决定）
只缓存好答案  有引用、未降级、非兜底答案；用户点 👎 可调 :meth:`forget` 立即删
失效          引用文档版本变化即失效（:meth:`lookup` 时核对）；``ttl_hours`` 到期
===========  ==================================================================

设计取舍
--------
* 纯内存、纯 Python（不依赖 numpy），每进程一份，按作用域最多 ``max_per_scope`` 条；
* 只在向量化启用且提供真实向量时工作——没有可比向量就没有语义匹配，宁可不缓存；
* 缓存命中不改变引用结构，答案文本原样复用；调用方拿到 :class:`Hit` 后可自行校验时效。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 默认参数（接入方按测评结果覆盖；阈值没有校准过就别开）
DEFAULTS: dict[str, Any] = {"enabled": False, "threshold": 0.95, "ttl_hours": 24.0}
#: 单作用域最多缓存条数
MAX_PER_SCOPE = 500
#: 最近命中的审计记录条数（人工抽检误命中用）
RECENT_HITS = 50


@dataclass
class Entry:
    """一条缓存记录。"""

    question: str
    vector: list[float]
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    #: ``doc_id -> 缓存时的文档版本``，用于失效核对
    doc_versions: dict[str, str] = field(default_factory=dict)
    created: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Hit:
    """缓存命中。"""

    entry: Entry
    similarity: float


class SemanticCache:
    """进程内语义缓存。

    Args:
        embeddings: 向量化器（用于把问题映射到同一向量空间）。
        enabled: 是否启用（默认关闭）。
        threshold: 余弦阈值，默认 0.95。
        ttl_hours: 过期小时数。
        is_cacheable: 额外判定「这个问题是否适合缓存」（例如依赖上文 / 个性化的返回 False）。
        max_per_scope: 单作用域最多条数。
    """

    def __init__(
        self,
        embeddings: CachedEmbeddings | None,
        *,
        enabled: bool = bool(DEFAULTS["enabled"]),
        threshold: float = float(DEFAULTS["threshold"]),
        ttl_hours: float = float(DEFAULTS["ttl_hours"]),
        is_cacheable: Callable[[str], bool] | None = None,
        max_per_scope: int = MAX_PER_SCOPE,
    ) -> None:
        self.embeddings = embeddings
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.ttl_hours = float(ttl_hours)
        self.is_cacheable = is_cacheable
        self.max_per_scope = max(1, int(max_per_scope))
        self._scopes: dict[str, list[Entry]] = {}
        self._lock = threading.Lock()
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_HITS)
        self.stats = {"lookups": 0, "hits": 0, "stored": 0, "expired": 0, "forgotten": 0}

    # ------------------------------------------------------------------ 状态
    @property
    def active(self) -> bool:
        """真正可用：启用 + 有向量化器 + 向量化器可用。"""
        return self.enabled and self.embeddings is not None and bool(self.embeddings.enabled)

    # -------------------------------------------------------------------- 读
    def lookup(
        self,
        question: str,
        *,
        scope: str = "",
        doc_versions: dict[str, str] | None = None,
        history: Sequence[Any] | None = None,
    ) -> Hit | None:
        """查找缓存。

        Args:
            question: 本次问题。
            scope: 作用域（可见文档集合 / 租户的编码）；不同作用域互不可见。
            doc_versions: 当前 ``{doc_id: version}``；给出时核对引用文档是否还是缓存时的版本。
            history: 对话历史；非空且问题很短时视为依赖上文的追问，不读缓存。

        Returns:
            :class:`Hit` 或 ``None``。
        """
        text = (question or "").strip()
        if not self.active or not text:
            return None
        if self.is_cacheable is not None and not self.is_cacheable(text):
            return None
        if history and len(text) <= 12:
            return None  # 短追问多半依赖上文，答案不可复用

        vector = self._vector(text)
        if vector is None:
            return None
        with self._lock:
            entries = list(self._scopes.get(scope) or [])
        if not entries:
            return None
        self.stats["lookups"] += 1

        best, best_similarity = None, -1.0
        for entry in entries:
            similarity = sum(a * b for a, b in zip(vector, entry.vector, strict=False))
            if similarity > best_similarity:
                best, best_similarity = entry, similarity
        if best is None or best_similarity < self.threshold:
            return None
        if self._expired(best) or not self._fresh(best, doc_versions):
            self._drop(scope, best)
            self.stats["expired"] += 1
            return None
        self.stats["hits"] += 1
        self.recent.appendleft(
            {
                "question": text[:120],
                "cached_question": best.question[:120],
                "similarity": round(best_similarity, 4),
                "at": time.time(),
            }
        )
        return Hit(best, round(best_similarity, 4))

    # -------------------------------------------------------------------- 写
    def put(
        self,
        question: str,
        answer: str,
        *,
        scope: str = "",
        citations: Sequence[dict[str, Any]] = (),
        doc_versions: dict[str, str] | None = None,
        history: Sequence[Any] | None = None,
    ) -> bool:
        """写入缓存。只在「启用 + 有引用 + 可缓存」时写入，返回是否写入。"""
        text = (question or "").strip()
        if not self.active or not text or not (answer or "").strip() or not citations:
            return False
        if self.is_cacheable is not None and not self.is_cacheable(text):
            return False
        if history and len(text) <= 12:
            return False
        vector = self._vector(text)
        if vector is None:
            return False
        entry = Entry(
            question=text,
            vector=vector,
            answer=answer,
            citations=[dict(item) for item in citations],
            doc_versions={str(doc): str(version) for doc, version in (doc_versions or {}).items() if doc},
        )
        with self._lock:
            entries = self._scopes.setdefault(scope, [])
            entries[:] = [item for item in entries if item.question != entry.question]
            entries.append(entry)
            del entries[:-self.max_per_scope]
        self.stats["stored"] += 1
        return True

    def forget(self, answers: Sequence[str]) -> int:
        """立即删除包含这些答案的缓存（用户点了 👎 时调用），返回删除条数。"""
        texts = [text for text in answers if text]
        if not texts:
            return 0
        removed = 0
        with self._lock:
            for entries in self._scopes.values():
                before = len(entries)
                entries[:] = [entry for entry in entries if not any(entry.answer in text for text in texts)]
                removed += before - len(entries)
        self.stats["forgotten"] += removed
        return removed

    def clear(self) -> None:
        """清空全部作用域。"""
        with self._lock:
            self._scopes.clear()
            self.recent.clear()

    def overview(self) -> dict[str, Any]:
        """运行状态（管理端 / 健康检查用）。"""
        lookups = self.stats["lookups"]
        return {
            "enabled": self.enabled,
            "available": self.active,
            "threshold": self.threshold,
            "ttl_hours": self.ttl_hours,
            "entries": sum(len(entries) for entries in self._scopes.values()),
            "hit_rate": round(self.stats["hits"] / lookups, 4) if lookups else 0.0,
            **self.stats,
            "recent": list(self.recent)[:20],
        }

    # ---------------------------------------------------------------- 内部
    def _vector(self, question: str) -> list[float] | None:
        assert self.embeddings is not None  # active 已经保证
        try:
            vector = self.embeddings.embed_for_query([question])[0]
        except Exception as exc:  # noqa: BLE001 - 缓存不可用不影响问答
            logger.debug("语义缓存向量化失败：%s", exc)
            return None
        if not vector:
            return None
        norm = math.sqrt(sum(value * value for value in vector))
        return None if norm == 0 else [value / norm for value in vector]

    def _expired(self, entry: Entry) -> bool:
        return time.time() - entry.created > self.ttl_hours * 3600

    @staticmethod
    def _fresh(entry: Entry, doc_versions: dict[str, str] | None) -> bool:
        """引用文档的当前版本仍是缓存时的版本；调用方没给当前版本时视为新鲜。"""
        if not entry.doc_versions or doc_versions is None:
            return True
        return all(doc_versions.get(doc) == version for doc, version in entry.doc_versions.items())

    def _drop(self, scope: str, entry: Entry) -> None:
        with self._lock:
            entries = self._scopes.get(scope) or []
            if entry in entries:
                entries.remove(entry)


__all__ = ["DEFAULTS", "Entry", "Hit", "MAX_PER_SCOPE", "SemanticCache"]