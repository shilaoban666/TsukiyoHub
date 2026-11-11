"""向量化缓存与降级策略（内核侧，不绑定任何厂商）。

为什么需要这一层
----------------
1. **省调用**：同一段切块在重建索引、增量导入时会被反复向量化；缓存键
   ``sha256(provider|model|dimension|text)``，换模型或维度自动失效，不会混用两种向量。
2. **降级不阻断**：索引期**严格**（失败即抛错，让入库任务重试，绝不把伪向量写进索引——
   写进去是永久污染，且因为不报错而极难发现）；查询期**宽松**（失败逐条回退伪向量，
   检索仍能跑 BM25）。这个「索引严格 / 查询宽松」的不对称是整个降级设计的关键。
3. **零依赖可跑**：``embedding_provider=hash`` 时向量化是纯本地特征哈希，无网络也能
   完成「入库 → 混合检索 → 证据闸门」全链路。

注意：:meth:`CachedEmbeddings.pseudo_vector` 产出的**不是语义向量**，只保证「同文本同向量」。
它只用于查询失败兜底；索引向量必须来自真实 provider（或显式的 hash 降级模式）。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from pathlib import Path

from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.embedding import EmbeddingProvider

logger = get_logger(__name__)

#: 查询向量进程内 LRU 条数（问题高度重复，命中即省一次外部调用）
QUERY_LRU = 4096
#: 缓存文件最多装载条数（防止历史缓存无限膨胀）
MAX_CACHE_ENTRIES = 200_000


class EmbeddingError(RuntimeError):
    """向量化失败。索引期调用方应当重试 / 标记失败，而不是写伪向量。"""


class CachedEmbeddings:
    """包装 :class:`EmbeddingProvider`，提供磁盘缓存、批量切片与降级。

    Args:
        provider: 真实向量化实现（``openai`` / ``dashscope`` / ``hash``）。
        cache_path: 磁盘缓存文件（JSONL，一行一个 ``{"k": 缓存键, "v": 向量}``）；
            ``None`` = 只做进程内缓存。
    """

    def __init__(self, provider: EmbeddingProvider, cache_path: Path | None = None) -> None:
        self.provider = provider
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._cache: dict[str, list[float]] = {}
        self._lru: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._loaded = False
        self.stats = {"index_hits": 0, "index_misses": 0, "query_hits": 0, "fallbacks": 0}

    # ------------------------------------------------------------------ 属性
    @property
    def enabled(self) -> bool:
        """是否参与稠密检索（``False`` 时检索只用 BM25，索引写入空向量）。"""
        return bool(getattr(self.provider, "enabled", False))

    @property
    def provider_name(self) -> str:
        return str(getattr(self.provider, "provider", "") or "none")

    @property
    def model(self) -> str:
        return str(getattr(self.provider, "model", "") or "")

    @property
    def dimension(self) -> int:
        return int(getattr(self.provider, "dimension", 0) or 0)

    @property
    def semantic(self) -> bool:
        """是否真实语义向量。``False`` = 本地哈希 / 占位实现（检索层不因此关闭稠密通路）。"""
        return bool(getattr(self.provider, "semantic", True))

    @property
    def batch_size(self) -> int:
        return max(1, int(getattr(self.provider, "batch_size", 10) or 10))

    # ------------------------------------------------------------ 索引（严格）
    def embed_for_index(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（索引期）。失败抛 :class:`EmbeddingError`，绝不返回伪向量。

        Returns:
            与 ``texts`` 等长、等维、L2 归一化的向量列表；向量化未启用时返回等长的空列表
            （等于告诉后端「这些切块只参与关键词检索」）。
        """
        if not texts:
            return []
        if not self.enabled:
            return [[] for _ in texts]

        keys = [self._key(text) for text in texts]
        with self._lock:
            self._ensure_loaded()
            missing: list[int] = []
            for index, key in enumerate(keys):
                vector = self._cache.get(key)
                if vector is None:
                    missing.append(index)
                else:
                    self.stats["index_hits"] += 1
            self.stats["index_misses"] += len(missing)

        if missing:
            fresh: dict[str, list[float]] = {}
            for start in range(0, len(missing), self.batch_size):
                part = missing[start:start + self.batch_size]
                vectors = self._call_provider([texts[index] for index in part])
                for index, vector in zip(part, vectors, strict=True):
                    fresh[keys[index]] = vector
            with self._lock:
                self._cache.update(fresh)
            self._append_cache(fresh)

        with self._lock:
            return [list(self._cache[key]) for key in keys]

    def lookup(self, texts: list[str]) -> dict[str, list[float]]:
        """只查缓存（重建索引时用，不调用向量化接口）：返回 ``{文本: 向量}``。"""
        with self._lock:
            self._ensure_loaded()
            found: dict[str, list[float]] = {}
            for text in texts:
                vector = self._cache.get(self._key(text))
                if vector is not None:
                    found[text] = list(vector)
            return found

    def seed(self, pairs: list[tuple[str, list[float]]]) -> int:
        """把已有向量回填进缓存（从后端取回原向量后重建索引时用）。"""
        data = {self._key(text): list(vector) for text, vector in pairs if vector}
        if not data:
            return 0
        with self._lock:
            self._cache.update(data)
        self._append_cache(data)
        return len(data)

    # ------------------------------------------------------------ 查询（宽松）
    def embed_for_query(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（查询期）。失败**逐条**回退伪向量，且回退结果不写缓存（恢复后立即走真接口）。"""
        if not texts:
            return []
        if not self.enabled:
            return [[] for _ in texts]

        out: list[list[float] | None] = []
        missing: list[int] = []
        with self._lock:
            for index, text in enumerate(texts):
                vector = self._lru.get(text)
                if vector is not None:
                    self._lru.move_to_end(text)
                    self.stats["query_hits"] += 1
                elif str(self.provider_name) == "hash":
                    vector = None  # hash provider 无需缓存：本地计算极快
                out.append(vector)
                if vector is None:
                    missing.append(index)

        if missing:
            try:
                vectors = self._call_provider([texts[index] for index in missing])
                with self._lock:
                    for index, vector in zip(missing, vectors, strict=True):
                        out[index] = vector
                        self._remember_query(texts[index], vector)
            except Exception as exc:  # noqa: BLE001 - 查询失败不能阻断检索
                self.stats["fallbacks"] += len(missing)
                logger.warning("查询向量化失败，%d 条回退伪向量（本次仅关键词检索有效）：%s", len(missing), exc)
                for index in missing:
                    out[index] = self.pseudo_vector(texts[index])
        return [vector or [] for vector in out]

    def embed_query(self, text: str) -> list[float]:
        """:meth:`embed_for_query` 的单条便捷入口。"""
        return self.embed_for_query([text])[0]

    # ------------------------------------------------------------ 伪向量兜底
    def pseudo_vector(self, text: str) -> list[float]:
        """确定性伪向量（sha256 种子的线性同余 + L2 归一化），只保证「同文本同向量」。

        优先用 provider 自己的实现（保证维度一致），它不可用时用本地算法兜底。
        """
        dimension = self.dimension or 1024
        try:
            vector = self.provider.pseudo_vector(text)
        except Exception:  # noqa: BLE001 - 第三方实现缺失 / 异常
            vector = _lcg_pseudo(text, dimension)
        return _normalize(list(vector)) if vector else _lcg_pseudo(text, dimension)

    # ---------------------------------------------------------------- 内部
    def _call_provider(self, texts: list[str]) -> list[list[float]]:
        """调用 provider 并校验条数 / 维度 / 是否空向量（空向量入库会污染整个索引）。"""
        try:
            vectors = self.provider.embed(list(texts))
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一成内核异常类型
            raise EmbeddingError(f"向量化调用失败：{exc}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(f"向量化返回条数({len(vectors)})与请求条数({len(texts)})不一致")
        expected = self.dimension
        out: list[list[float]] = []
        for vector in vectors:
            values = [float(value) for value in vector]
            if not values:
                raise EmbeddingError("向量化返回了空向量（可能模型名不受支持或网关异常）")
            if expected and len(values) != expected:
                raise EmbeddingError(f"向量维度({len(values)})与配置维度({expected})不一致，请检查 embedding 配置")
            out.append(_normalize(values))
        return out

    def _remember_query(self, text: str, vector: list[float]) -> None:
        """写入查询 LRU（调用方需持锁）。hash provider 不需要。"""
        if str(self.provider_name) == "hash":
            return
        self._lru[text] = list(vector)
        while len(self._lru) > QUERY_LRU:
            self._lru.popitem(last=False)

    def _key(self, text: str) -> str:
        raw = f"{self.provider_name}|{self.model}|{self.dimension}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _ensure_loaded(self) -> None:
        """首次使用时装载磁盘缓存（调用方需持锁）；坏行跳过。"""
        if self._loaded:
            return
        self._loaded = True
        path = self.cache_path
        if path is None or not path.exists():
            return
        loaded = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        key, vector = item["k"], item["v"]
                        if isinstance(key, str) and isinstance(vector, list) and vector:
                            self._cache[key] = [float(value) for value in vector]
                            loaded += 1
                    except (ValueError, KeyError, TypeError):
                        continue  # 坏行跳过：缓存损坏不该让服务起不来
        except OSError as exc:
            logger.warning("向量缓存读取失败（忽略）：%s", exc)
            return
        if loaded:
            logger.info("向量缓存已装载：%d 条（%s）", loaded, path)

    def _append_cache(self, data: dict[str, list[float]]) -> None:
        """追加写磁盘缓存（调用方无需持锁）。写失败不影响检索。"""
        path = self.cache_path
        if path is None or not data:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for key, vector in data.items():
                    handle.write(json.dumps({"k": key, "v": [round(value, 6) for value in vector]},
                                            ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("向量缓存写入失败（忽略）：%s", exc)

    def purge(self) -> int:
        """清空磁盘缓存（换模型后想彻底重算时用），返回删除条数。"""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._lru.clear()
        path = self.cache_path
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("向量缓存文件删除失败：%s", exc)
        return count


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化：归一化后点积 == 余弦，后端可以只做点积。"""
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm == 0 else [value / norm for value in vector]


def _lcg_pseudo(text: str, dimension: int) -> list[float]:
    """本地伪向量：sha256 作种子 + 线性同余生成，确定性、与文本无关语义。"""
    state = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    vector: list[float] = []
    for _ in range(max(1, dimension)):
        state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
        vector.append((state / 2**64) * 2.0 - 1.0)
    return _normalize(vector)


__all__ = ["CachedEmbeddings", "EmbeddingError", "MAX_CACHE_ENTRIES", "QUERY_LRU"]