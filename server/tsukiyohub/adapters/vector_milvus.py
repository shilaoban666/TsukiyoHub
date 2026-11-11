"""Milvus 检索后端（生产用；**pymilvus 懒加载**）。

为什么懒加载
------------
本地演示与单测不装 pymilvus，模块级 ``import pymilvus`` 会让整个应用起不来。
所以这里只在**第一次真正用到**时才导入并连接，失败抛 :class:`MilvusUnavailable`
（中文原因 + 可操作建议），其它后端不受影响。

能力声明（如实反映当前实现）
----------------------------
* ``supports_hybrid = False``：本实现只做**稠密**召回（HNSW + COSINE）。词法信号由
  :class:`~tsukiyohub.kernel.rag.retriever.HybridRetriever` 的 L1 词面重排补上。
  想升级成 Milvus 原生混合检索（BM25 稀疏向量 + 稠密向量），需要：
  1. schema 增加 ``text`` 字段（``enable_analyzer=True``）与 ``sparse`` 稀疏向量字段；
  2. 用 ``FunctionType.BM25`` 让 Milvus 自动从 ``text`` 生成稀疏向量（写入时不提供）；
  3. 给 ``sparse`` 建 ``SPARSE_INVERTED_INDEX``（``metric_type="BM25"``、``bm25_k1/b``）；
  4. 用 ``AnnSearchRequest`` + ``hybrid_search`` 融合两路，然后把 ``supports_hybrid`` 改成 ``True``。
* 分数刻度：``metric_type="COSINE"`` 返回的 distance 就是余弦，与内存后端一致
  （conformance test 比对的就是这个值，**不要**再做 ``1/(3-2cos)`` 之类的变换）。

运维约束（照源项目线上踩坑的经验）
----------------------------------
* 集合加载必须带超时，否则段损坏会把启动 / 请求永久卡死；
* 检索路径用 ``Bounded`` 一致性（毫秒级延迟），需要强一致时再升级；
* 连续失败 5 次熔断 30 秒：期间检索返回空（上层降级话术），写入直接报错；
* ``count()`` 用 ``query(filter, output_fields=["count(*)"])``，**不要**用
  ``get_collection_stats``（它只统计已 flush 的段，数字会偏小）。
"""

from __future__ import annotations

import time
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import Chunk, DocType, ScoredChunk, SourceType
from tsukiyohub.kernel.rag.fusion import chunk_matches
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.vector import IndexRow, SearchFilters

logger = get_logger(__name__)

#: VARCHAR 字段上限（按 UTF-8 字节截断，中文 3 字节，留 16 字节余量）
TEXT_MAX = 8192
TITLE_MAX = 512
BREADCRUMB_MAX = 1024
#: 集合加载超时（秒）
LOAD_TIMEOUT = 60.0
#: 连续失败多少次后熔断、熔断时长（秒）
FAILURE_THRESHOLD = 5
BREAKER_SECONDS = 30.0
#: 检索时取回的字段
OUTPUT_FIELDS = ["chunk_id", "doc_id", "doc_type", "active", "text", "content", "meta"]


class MilvusUnavailable(RuntimeError):
    """Milvus 不可用（未安装客户端 / 连不上 / 建集合失败）。异常消息是给人看的中文。"""


def _cut(text: str, limit: int = TEXT_MAX) -> str:
    """按 UTF-8 字节截断字符串（Milvus 的 VARCHAR 上限按字节算）。"""
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text or ""
    return raw[: max(0, limit - 16)].decode("utf-8", errors="ignore")


def _quote(value: str) -> str:
    """转义 Milvus 过滤表达式里的字符串（反斜杠与引号）。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class MilvusVectorStore:
    """Milvus 向量后端（实现 :class:`~tsukiyohub.ports.vector.VectorStore`）。

    Args:
        uri: Milvus 地址，例如 ``http://127.0.0.1:19530`` 或本地 ``./milvus.db``。
        token: 认证 token（``user:password`` 或 API Key）；无认证留空。
        collection: 集合名（单集合存全部知识库，用 ``doc_id`` / ``doc_type`` 过滤）。
        dimension: 向量维度。
        timeout: 连接 / 请求超时（秒）。
    """

    name = "milvus"
    persistent = True
    #: 当前实现只做稠密召回（见模块 docstring 的升级步骤）
    supports_hybrid = False

    def __init__(
        self,
        *,
        uri: str,
        token: str = "",
        collection: str = "tsukiyo_chunks",
        dimension: int = 1024,
        timeout: float = 5.0,
    ) -> None:
        self.uri = (uri or "").strip()
        self.token = (token or "").strip()
        self.collection = (collection or "tsukiyo_chunks").strip()
        self.dimension = max(8, int(dimension or 1024))
        self.timeout = float(timeout or 5.0)
        self._client: Any = None
        self._ready = False
        self._failures = 0
        self._open_until = 0.0

    # ============================================================ 连接管理
    def ensure_ready(self) -> None:
        """确保集合存在并已加载；失败抛 :class:`MilvusUnavailable`（幂等）。"""
        self._ensure(raise_error=True)

    def ping(self) -> bool:
        """能否连上 Milvus（不抛错）。"""
        try:
            self._ensure(raise_error=True)
        except MilvusUnavailable:
            return False
        return True

    def _ensure(self, *, raise_error: bool) -> bool:
        """建连接 / 建集合 / 加载；带熔断与一次性日志。"""
        if self._ready:
            return True
        if self._open_until > time.monotonic():
            if raise_error:
                raise MilvusUnavailable(
                    f"Milvus 已熔断 {BREAKER_SECONDS:.0f} 秒（连续失败 {self._failures} 次），稍后重试"
                )
            return False
        try:
            self._prepare()
        except MilvusUnavailable:
            self._fail()
            if raise_error:
                raise
            return False
        except Exception as exc:  # noqa: BLE001 - 任何连接异常都统一成可读错误
            self._fail()
            message = f"Milvus 连接失败（{self.uri}）：{exc}"
            if raise_error:
                raise MilvusUnavailable(message) from exc
            logger.warning(message)
            return False
        self._ready = True
        self._failures = 0
        return True

    def _prepare(self) -> None:
        """导入客户端、建集合、建索引、加载集合（只做一次）。"""
        client = self._connect()
        if client.has_collection(self.collection):
            client.load_collection(self.collection, timeout=LOAD_TIMEOUT)
            return
        datatype = self._datatype()
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", datatype.VARCHAR, max_length=96, is_primary=True)
        schema.add_field("doc_id", datatype.VARCHAR, max_length=64)
        schema.add_field("doc_type", datatype.VARCHAR, max_length=16)
        schema.add_field("active", datatype.BOOL)
        #: text 是给未来 BM25 用的原始文本（当前实现只做稠密召回）
        schema.add_field("text", datatype.VARCHAR, max_length=TEXT_MAX)
        schema.add_field("content", datatype.VARCHAR, max_length=TEXT_MAX)
        schema.add_field("meta", datatype.JSON)
        schema.add_field("dense", datatype.FLOAT_VECTOR, dim=self.dimension)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        client.create_collection(self.collection, schema=schema, index_params=index_params)
        for field_name in ("doc_id", "doc_type", "active"):
            try:  # 标量索引只是加速过滤，建不上不影响正确性
                index_params_scalar = client.prepare_index_params()
                index_params_scalar.add_index(field_name=field_name, index_type="INVERTED")
                client.create_index(self.collection, index_params_scalar)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Milvus 标量索引 %s 创建失败（忽略）：%s", field_name, exc)
        client.load_collection(self.collection, timeout=LOAD_TIMEOUT)
        logger.info("Milvus 集合已创建：%s（dim=%d）", self.collection, self.dimension)

    def _connect(self) -> Any:
        """懒加载 pymilvus 并建立客户端（失败抛中文可操作错误）。"""
        if self._client is not None:
            return self._client
        if not self.uri:
            raise MilvusUnavailable("未配置 Milvus 地址（vector_backend=milvus 时需要 milvus_uri）")
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise MilvusUnavailable(
                "未安装 pymilvus，无法使用 Milvus 后端；请执行 pip install pymilvus，"
                "或改用 vector_backend=memory（零依赖）"
            ) from exc
        try:
            self._client = MilvusClient(uri=self.uri, token=self.token, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise MilvusUnavailable(
                f"连接 Milvus 失败（{self.uri}）：{exc}；请确认服务已启动、地址与 token 正确"
            ) from exc
        return self._client

    @staticmethod
    def _datatype() -> Any:
        from pymilvus import DataType

        return DataType

    def _fail(self) -> None:
        """记一次失败；连续失败到阈值就熔断。"""
        self._failures += 1
        if self._failures >= FAILURE_THRESHOLD:
            self._open_until = time.monotonic() + BREAKER_SECONDS
            logger.error("Milvus 连续失败 %d 次，熔断 %.0f 秒", self._failures, BREAKER_SECONDS)

    # ================================================================ 写入
    def upsert(self, rows: list[IndexRow]) -> int:
        """写入 / 覆盖切块（写失败抛错，由上层重试；检索失败才降级）。"""
        if not rows:
            return 0
        self._ensure(raise_error=True)
        client = self._connect()
        data = [self._entity(row) for row in rows]
        try:
            client.upsert(self.collection, data)
        except Exception as exc:  # noqa: BLE001
            self._fail()
            raise MilvusUnavailable(f"Milvus 写入失败：{exc}") from exc
        return len(data)

    def remove(
        self,
        *,
        doc_id: str | None = None,
        chunk_ids: list[str] | None = None,
        version_id: str | None = None,
    ) -> int:
        """删除切块。**所有条件都为 None 时返回 0**（防误删全库）。"""
        expression = self._target_expr(doc_id=doc_id, chunk_ids=chunk_ids, version_id=version_id)
        if not expression:
            logger.warning("Milvus remove() 未给任何条件，按安全约定不做任何删除")
            return 0
        if not self._ensure(raise_error=False):
            return 0
        try:
            result = self._connect().delete(self.collection, filter=expression)
        except Exception as exc:  # noqa: BLE001
            self._fail()
            logger.warning("Milvus 删除失败：%s", exc)
            return 0
        return _deleted_count(result)

    def set_flags(self, *, doc_id: str | None = None, chunk_ids: list[str] | None = None,
                  version_id: str | None = None, **flags: object) -> int:
        """切换切块标记：查整行 → 改标量 → upsert 回（不重新向量化）。"""
        expression = self._target_expr(doc_id=doc_id, chunk_ids=chunk_ids, version_id=version_id)
        if not expression or not flags:
            return 0
        if not self._ensure(raise_error=False):
            return 0
        client = self._connect()
        try:
            rows = client.query(self.collection, filter=expression, output_fields=OUTPUT_FIELDS + ["dense"])
        except Exception as exc:  # noqa: BLE001
            self._fail()
            logger.warning("Milvus 查询待改标记的行失败：%s", exc)
            return 0
        updated = 0
        for row in rows or []:
            entity = dict(row)
            entity.update({key: value for key, value in flags.items() if key in ("active", "doc_type")})
            try:
                client.upsert(self.collection, [entity])
                updated += 1
            except Exception as exc:  # noqa: BLE001
                self._fail()
                logger.warning("Milvus 更新标记失败：%s", exc)
                break
        return updated

    def clear(self) -> None:
        """删除并重建集合（仅测试 / 重建索引用；线上慎用）。"""
        self._ensure(raise_error=True)
        client = self._connect()
        try:
            client.drop_collection(self.collection)
        except Exception as exc:  # noqa: BLE001
            raise MilvusUnavailable(f"删除 Milvus 集合失败：{exc}") from exc
        self._ready = False
        self.ensure_ready()

    # ================================================================ 检索
    def search(
        self,
        *,
        vector: list[float] | None,
        query_text: str,
        top_k: int = 20,
        filters: SearchFilters | None = None,
    ) -> list[ScoredChunk]:
        """稠密检索（``vector`` 为空时没有可用通路，返回空列表）。

        Returns:
            ``list[ScoredChunk]``（``vector_score`` 是 Milvus 返回的**原始余弦**）。
            任何失败都返回空列表——上层据此降级话术，而不是 500。
        """
        if not vector or len(vector) != self.dimension:
            logger.debug("Milvus 后端未收到有效向量，本次返回空（词法通路见 retriever 的 L1 重排）")
            return []
        if not self._ensure(raise_error=False):
            return []
        expression = self._filter_expr(filters)
        try:
            result = self._connect().search(
                self.collection,
                data=[[float(value) for value in vector]],
                anns_field="dense",
                filter=expression,
                limit=max(1, int(top_k)),
                output_fields=OUTPUT_FIELDS,
                search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                consistency_level="Bounded",
            )
        except Exception as exc:  # noqa: BLE001 - 检索失败降级
            self._fail()
            logger.warning("Milvus 检索失败，本次返回空：%s", exc)
            return []

        items: list[Chunk] = []
        for hit in (result[0] if result else []):
            entity = hit.get("entity", hit)
            try:
                chunk = self._chunk(entity)
            except (ValueError, TypeError) as exc:
                logger.warning("Milvus 返回行无法解析，已跳过：%s", exc)
                continue
            if not chunk_matches(chunk, filters):  # tags / active 的本地兜底过滤
                continue
            score = float(hit.get("distance", 0.0))
            items.append(ScoredChunk(chunk=chunk, score=round(score, 6), vector_score=round(score, 6),
                                     retriever="vector"))
        return items

    def count(self) -> int:
        """切块总数（用 ``count(*)`` 查询，避免 flush 延迟导致数字偏小）。"""
        if not self._ensure(raise_error=False):
            return 0
        try:
            rows = self._connect().query(self.collection, filter=self._filter_expr(None),
                                         output_fields=["count(*)"])
        except Exception as exc:  # noqa: BLE001
            self._fail()
            logger.warning("Milvus 计数失败：%s", exc)
            return 0
        if not rows:
            return 0
        row = dict(rows[0])
        return int(row.get("count(*)", 0) or 0)

    def get(self, chunk_ids: list[str]) -> list[Chunk]:
        """按 ID 取切块。"""
        if not chunk_ids:
            return []
        if not self._ensure(raise_error=False):
            return []
        expression = self._target_expr(chunk_ids=chunk_ids)
        try:
            rows = self._connect().query(self.collection, filter=expression, output_fields=OUTPUT_FIELDS,
                                         limit=len(chunk_ids))
        except Exception as exc:  # noqa: BLE001
            self._fail()
            logger.warning("Milvus 取切块失败：%s", exc)
            return []
        chunks: list[Chunk] = []
        for row in rows or []:
            try:
                chunks.append(self._chunk(dict(row)))
            except (ValueError, TypeError):
                continue
        return chunks

    def persist(self) -> None:
        """Milvus 自己负责持久化，这里无需动作。"""
        return None

    def stats(self) -> dict[str, Any]:
        """后端状态（健康检查用）。"""
        return {
            "name": self.name,
            "uri": self.uri,
            "collection": self.collection,
            "dimension": self.dimension,
            "ready": self._ready,
            "count": self.count() if self._ready else 0,
            "supports_hybrid": self.supports_hybrid,
            "failures": self._failures,
        }

    # ---------------------------------------------------------------- 内部
    def _entity(self, row: IndexRow) -> dict[str, Any]:
        """切块 → Milvus 行。"""
        chunk = row.chunk
        extra = chunk.extra or {}
        vector = [float(value) for value in row.vector] if row.vector else []
        if len(vector) != self.dimension:
            # 没有稠密向量时用占位单位向量：Milvus 不接受空向量，且该行会因分数极低被自然排在后面
            vector = [1.0] + [0.0] * (self.dimension - 1)
        return {
            "chunk_id": _cut(chunk.chunk_id, 96),
            "doc_id": _cut(chunk.doc_id, 64),
            "doc_type": _cut(chunk.doc_type.value, 16),
            "active": bool(extra.get("active", True)),
            "text": _cut(chunk.text),
            "content": _cut(chunk.text),
            "meta": {
                "ordinal": chunk.ordinal,
                "uri": chunk.uri,
                "source_type": chunk.source_type.value,
                "version": chunk.version,
                "updated_at": chunk.updated_at,
                "title": _cut(chunk.title, TITLE_MAX),
                "breadcrumb": _cut(str(extra.get("breadcrumb") or ""), BREADCRUMB_MAX),
                "tags": list(extra.get("tags") or []),
                "parent_id": str(extra.get("parent_id") or ""),
            },
            "dense": vector,
        }

    @staticmethod
    def _chunk(entity: dict[str, Any]) -> Chunk:
        """Milvus 行 → 切块。"""
        meta = entity.get("meta") or {}
        if isinstance(meta, str):  # 某些版本会把 JSON 列回成字符串
            import json

            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        extra: dict[str, Any] = {
            "active": bool(entity.get("active", True)),
            "breadcrumb": str(meta.get("breadcrumb") or ""),
            "tags": list(meta.get("tags") or []),
        }
        if meta.get("parent_id"):
            extra["parent_id"] = str(meta["parent_id"])
        return Chunk(
            chunk_id=str(entity.get("chunk_id") or ""),
            doc_id=str(entity.get("doc_id") or ""),
            text=str(entity.get("content") or entity.get("text") or ""),
            ordinal=int(meta.get("ordinal") or 0),
            title=str(meta.get("title") or ""),
            uri=str(meta.get("uri") or ""),
            source_type=SourceType.parse(meta.get("source_type", SourceType.LOCAL.value)),
            doc_type=DocType.parse(entity.get("doc_type") or DocType.DOC.value),
            version=str(meta.get("version") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            extra=extra,
        )

    @staticmethod
    def _target_expr(*, doc_id: str | None = None, chunk_ids: list[str] | None = None,
                     version_id: str | None = None) -> str:
        """删除 / 查询的目标表达式；没有条件时返回空串。"""
        parts: list[str] = []
        if chunk_ids:
            quoted = ", ".join(f'"{_quote(item)}"' for item in chunk_ids)
            parts.append(f"chunk_id in [{quoted}]")
        if doc_id is not None:
            parts.append(f'doc_id == "{_quote(doc_id)}"')
        if version_id is not None:
            # 当前 schema 没有独立 version_id 列，用 meta JSON 里的值过滤（仅用于兼容调用）
            parts.append(f'meta["version_id"] == "{_quote(version_id)}"')
        return " and ".join(parts)

    @staticmethod
    def _filter_expr(filters: SearchFilters | None) -> str:
        """过滤表达式：``active`` + 文档类型 + 文档 ID。``tags`` 留到本地二次过滤。"""
        parts: list[str] = []
        if filters is None:
            return 'active == true'
        if filters.active_only:
            parts.append("active == true")
        if filters.doc_types:
            quoted = ", ".join(f'"{_quote(value)}"' for value in filters.doc_types)
            parts.append(f"doc_type in [{quoted}]")
        if filters.doc_ids:
            quoted = ", ".join(f'"{_quote(value)}"' for value in filters.doc_ids)
            parts.append(f"doc_id in [{quoted}]")
        return " and ".join(parts)


def _deleted_count(result: Any) -> int:
    """Milvus ``delete`` 的返回值在不同版本里形态不同，这里宽松取数。"""
    for key in ("delete_count", "delete_cnt"):
        try:
            return int(result[key])
        except (TypeError, KeyError, IndexError):
            continue
    return 0


def build_milvus_store(settings: Settings) -> MilvusVectorStore:
    """按配置构造 Milvus 后端（构造阶段不连接；首次使用才懒加载 pymilvus）。"""
    token = settings.milvus_token.get_secret_value() if settings.milvus_token is not None else ""
    return MilvusVectorStore(
        uri=settings.milvus_uri,
        token=token,
        collection=settings.milvus_collection,
        dimension=int(settings.embedding_dimension or 1024),
        timeout=float(settings.milvus_timeout_seconds or 5.0),
    )


__all__ = ["BREAKER_SECONDS", "FAILURE_THRESHOLD", "MilvusUnavailable", "MilvusVectorStore",
           "build_milvus_store"]