"""知识库门面：入库、删除、检索、统计、清单持久化。

为什么要有这一层（而不是让 API 直接操作后端）
--------------------------------------------
* **幂等与增量**是产品语义，不是存储语义：同一份文档内容没变就不该重建切块与向量
  （``content_hash`` 相同直接跳过）；
* **写顺序**有硬约束：**先 upsert 新切块、再删旧切块**，否则重建瞬间会出现「检索空窗」，
  用户正好在这时提问就会得到「知识库暂无」；
* **失败语义**要统一：单文档解析失败 / 向量化失败只让**这一篇**返回 ``error``，
  不中断整批，也不污染索引（索引期绝不写伪向量）；
* **落盘格式**是可恢复的：``kb_index_docs.json``（清单）+ ``kb_index_chunks.jsonl``（切块），
  带版本号、坏行跳过；向量本身由后端自己持久化（内存后端写 ``kb_index_vectors.jsonl``）。

数据流::

    文件/字节 → DocumentParser → Document → TextChunker → Chunk[]
              → CachedEmbeddings.embed_for_index（严格）→ VectorStore.upsert
              → 删旧切块 → 更新清单 → persist()

公开给 API 的方法都在契约里：``upsert_document/ingest_path/ingest_bytes/ingest_directory/
delete_document/clear/retrieve/stats/list_documents/get_chunk/persist/load``。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import (
    Chunk,
    DocType,
    Document,
    DocumentInfo,
    IngestResult,
    KnowledgeStats,
    ScoredChunk,
    SourceType,
)
from tsukiyohub.kernel.rag.chunker import TextChunker
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings
from tsukiyohub.kernel.rag.parser import SUPPORTED_SUFFIXES, DocumentParser, ParseError, build_document_meta
from tsukiyohub.kernel.rag.retriever import HybridRetriever
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.vector import IndexRow, SearchFilters, VectorStore

logger = get_logger(__name__)

#: 清单 / 切块文件格式版本（结构一改就加一，``load`` 会拒绝更新的版本）
INDEX_FORMAT_VERSION = 1


def sha256_text(text: str) -> str:
    """文本内容哈希（幂等判据）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class DocumentEntry:
    """清单里的一条文档记录（检索结果的引用骨架来自这些字段）。"""

    doc_id: str
    title: str
    uri: str = ""
    file_name: str = ""
    content_hash: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    source_type: str = SourceType.LOCAL.value
    doc_type: str = DocType.DOC.value
    version: str = ""
    updated_at: str = ""
    ingested_at: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_document(cls, document: Document, chunk_ids: list[str], content_hash: str) -> DocumentEntry:
        """从 :class:`Document` 构造清单项。"""
        meta = document.meta
        return cls(
            doc_id=meta.doc_id,
            title=meta.title,
            uri=meta.uri,
            file_name=meta.file_name,
            content_hash=content_hash,
            chunk_ids=list(chunk_ids),
            source_type=meta.source_type.value,
            doc_type=meta.doc_type.value,
            version=meta.version,
            updated_at=meta.updated_at,
            ingested_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tags=list(meta.tags),
        )

    def to_info(self) -> DocumentInfo:
        """转成 API 使用的 :class:`DocumentInfo`。"""
        return DocumentInfo(
            doc_id=self.doc_id,
            title=self.title,
            uri=self.uri,
            file_name=self.file_name,
            doc_type=self.doc_type,
            source_type=self.source_type,
            version=self.version,
            chunk_count=len(self.chunk_ids),
            content_hash=self.content_hash,
            tags=list(self.tags),
            updated_at=self.updated_at,
            ingested_at=self.ingested_at,
        )


class KnowledgeStore:
    """知识库门面：导入、删除、检索、统计、清单持久化。

    Args:
        settings: 全局配置（索引目录、切块参数、检索参数）。
        backend: 检索后端（内存 / Milvus / 自研）。
        embeddings: 带缓存的向量化器。
        chunker: 切块器。
        retriever: 检索编排器（与本 store 共用同一个后端与向量化器）。
    """

    def __init__(
        self,
        settings: Settings,
        backend: VectorStore,
        embeddings: CachedEmbeddings,
        chunker: TextChunker,
        retriever: HybridRetriever,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.embeddings = embeddings
        self.chunker = chunker
        self.retriever = retriever
        #: ``doc_id -> DocumentEntry``（API 直接读它做列表）
        self.documents: dict[str, DocumentEntry] = {}
        self._chunks: dict[str, Chunk] = {}
        self._last_ingested_at: str | None = None
        self._parser = DocumentParser(settings)
        self._lock = threading.RLock()

    # ================================================================ 写入
    def upsert_document(self, document: Document, force: bool = False) -> IngestResult:
        """导入 / 更新一篇文档（幂等）。

        内容哈希未变且 ``force=False`` 时跳过；向量化失败先重试一次，仍失败则**不写索引**、
        返回 ``error``（保留旧版本，允许重试）——绝不把伪向量写进索引。
        """
        meta = document.meta
        content_hash = document.content_hash or sha256_text(document.content)
        document.content_hash = content_hash
        with self._lock:
            existing = self.documents.get(meta.doc_id)
            if existing is not None and existing.content_hash == content_hash and not force:
                return IngestResult(
                    doc_id=meta.doc_id,
                    title=meta.title,
                    chunk_count=len(existing.chunk_ids),
                    skipped=True,
                    reason="内容未变化，已跳过（增量导入）",
                )

            chunks = self.chunker.split(document)
            if not chunks:
                return IngestResult(
                    doc_id=meta.doc_id,
                    title=meta.title,
                    chunk_count=0,
                    skipped=True,
                    reason="解析后无有效文本",
                )

            vectors, error = self._embed(chunks)
            if error is not None or vectors is None:
                return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=error or "向量化失败")

            rows = [IndexRow(chunk=chunk, vector=vector) for chunk, vector in zip(chunks, vectors, strict=True)]
            try:
                written = self.backend.upsert(rows)
            except Exception as exc:  # noqa: BLE001 - 写失败：保留旧版本，让调用方重试
                logger.error("写入索引失败（%s）：%s", meta.title, exc)
                return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0,
                                    error=f"写入索引失败：{exc}")

            # 先写完新的再删旧的，避免重建瞬间的检索空窗
            stale = sorted(set(existing.chunk_ids if existing else []) - {chunk.chunk_id for chunk in chunks})
            if stale:
                try:
                    self.backend.remove(chunk_ids=stale)
                except Exception as exc:  # noqa: BLE001 - 旧切块删不掉只影响体积，不影响正确性
                    logger.warning("清理旧切块失败（%s）：%s", meta.title, exc)

            self.documents[meta.doc_id] = DocumentEntry.from_document(
                document, [chunk.chunk_id for chunk in chunks], content_hash
            )
            for chunk_id in existing.chunk_ids if existing else []:
                self._chunks.pop(chunk_id, None)
            self._chunks.update({chunk.chunk_id: chunk for chunk in chunks})
            self._last_ingested_at = datetime.now(UTC).isoformat(timespec="seconds")

        logger.info("文档入库完成：%s（切块 %d / 写入 %d）", meta.title, len(chunks), written)
        self._autopersist()
        return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=len(chunks))

    def ingest_path(
        self,
        path: Path,
        *,
        root: Path | None = None,
        source_type: SourceType = SourceType.LOCAL,
        doc_type: DocType = DocType.DOC,
    ) -> IngestResult:
        """解析并入库一个本地文件（解析失败只影响这一篇）。"""
        path = Path(path)
        meta = build_document_meta(path, root=root, source_type=source_type, doc_type=doc_type)
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0,
                                error=f"不支持的格式 {path.suffix or '(无扩展名)'}")
        try:
            document = self._parser.parse(path, meta)
        except ParseError as exc:
            logger.warning("解析失败（%s）：%s", path.name, exc)
            return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=str(exc))
        except OSError as exc:
            logger.warning("读取失败（%s）：%s", path.name, exc)
            return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=f"读取文件失败：{exc}")
        return self.upsert_document(document)

    def ingest_bytes(
        self,
        file_name: str,
        data: bytes,
        *,
        doc_type: DocType = DocType.DOC,
    ) -> IngestResult:
        """解析并入库一段字节（上传接口用）。

        ``doc_id`` 由文件名哈希得到（同一文件名再次上传即覆盖），
        这样前端反复传同一份文件不会堆出无数个副本。
        """
        meta = DocumentParser.meta_from_name(file_name)
        if doc_type is not DocType.DOC:
            meta.doc_type = doc_type
        try:
            document = self._parser.parse_bytes(file_name, data, meta)
        except ParseError as exc:
            logger.warning("解析失败（%s）：%s", file_name, exc)
            return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=str(exc))
        except OSError as exc:
            return IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=f"读取失败：{exc}")
        return self.upsert_document(document)

    def ingest_directory(self, root: Path, *, force: bool = False) -> list[IngestResult]:
        """递归导入一个目录（按内容哈希增量；隐藏文件 / 不支持的格式跳过）。

        注意：这里**不删除**「目录里已经消失」的文档——「本次没扫到」不等于「已被删除」，
        真正的删除请调 :meth:`delete_document`。
        """
        root = Path(root)
        if not root.exists():
            logger.warning("知识库目录不存在：%s", root)
            return []
        results: list[IngestResult] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            result = self.ingest_path(path, root=root, source_type=SourceType.LOCAL)
            if result.skipped and force:
                # force 重建：跳过判据是内容哈希，强制路径需要重新解析一次
                meta = build_document_meta(path, root=root, source_type=SourceType.LOCAL)
                try:
                    result = self.upsert_document(self._parser.parse(path, meta), force=True)
                except ParseError as exc:
                    result = IngestResult(doc_id=meta.doc_id, title=meta.title, chunk_count=0, error=str(exc))
            results.append(result)
        logger.info(
            "目录导入完成：%s（%d 篇新入库 / %d 篇跳过 / %d 篇失败）",
            root,
            sum(1 for item in results if not item.error and not item.skipped),
            sum(1 for item in results if item.skipped),
            sum(1 for item in results if item.error),
        )
        return results

    def delete_document(self, doc_id: str) -> int:
        """删除文档及其全部切块，返回删除的切块数（不存在时返回 0）。"""
        with self._lock:
            entry = self.documents.pop(doc_id, None)
            if entry is None:
                return 0
            for chunk_id in entry.chunk_ids:
                self._chunks.pop(chunk_id, None)
            try:
                removed = self.backend.remove(doc_id=doc_id)
            except Exception as exc:  # noqa: BLE001 - 后端删除失败：清单已移除，索引残留不影响正确性
                logger.error("删除索引失败（%s）：%s", doc_id, exc)
                removed = len(entry.chunk_ids)
        logger.info("文档已删除：%s（切块 %d）", entry.title, removed)
        self._autopersist()
        return removed

    def clear(self) -> None:
        """清空知识库（重建索引时用）。"""
        with self._lock:
            for doc_id in list(self.documents):
                try:
                    self.backend.remove(doc_id=doc_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("清空时删除文档 %s 失败：%s", doc_id, exc)
            self.documents.clear()
            self._chunks.clear()
            self._last_ingested_at = None
        self._autopersist()

    # ================================================================ 读取
    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: SearchFilters | None = None,
        doc_types: list[str] | None = None,
    ) -> list[ScoredChunk]:
        """检索（委托给 :class:`HybridRetriever`，本类只是门面）。"""
        return self.retriever.retrieve(query, top_k=top_k, filters=filters, doc_types=doc_types)

    def stats(self) -> KnowledgeStats:
        """知识库统计（``/api/knowledge/stats`` 与启动日志用）。"""
        sources: dict[str, int] = {}
        doc_types: dict[str, int] = {}
        for entry in self.documents.values():
            sources[entry.source_type] = sources.get(entry.source_type, 0) + 1
            doc_types[entry.doc_type] = doc_types.get(entry.doc_type, 0) + 1
        return KnowledgeStats(
            document_count=len(self.documents),
            chunk_count=len(self._chunks),
            vector_count=self._backend_count(),
            embedding_enabled=bool(self.embeddings.enabled),
            vector_store=str(getattr(self.backend, "name", "memory")),
            embedding_model=self.embeddings.model if self.embeddings.enabled else None,
            sources=sources,
            doc_types=doc_types,
            last_ingested_at=self._last_ingested_at,
            index_path=str(self.settings.index_path),
        )

    def list_documents(self) -> list[DocumentInfo]:
        """文档列表（最近入库在前）。"""
        entries = sorted(self.documents.values(), key=lambda item: item.ingested_at, reverse=True)
        return [entry.to_info() for entry in entries]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """按切块 ID 取原文（先查内存副本，再问后端）。"""
        chunk = self._chunks.get(chunk_id)
        if chunk is not None:
            return chunk
        try:
            found = self.backend.get([chunk_id])
        except Exception:  # noqa: BLE001 - 后端不可用时不该抛给前端
            return None
        return found[0] if found else None

    @property
    def chunk_count(self) -> int:
        """当前管理的切块数。"""
        return len(self._chunks)

    @property
    def loaded(self) -> bool:
        """是否已从磁盘恢复出内容（启动自检用）。"""
        return bool(self.documents) or bool(self._chunks)

    # ================================================================ 持久化
    def persist(self) -> None:
        """把清单与切块副本落盘，并让后端持久化向量。

        清单与切块是**元数据真相**（Milvus 数据丢了也能据此重建）；向量本身由后端负责。
        写失败只记日志，不阻断在线服务。
        """
        with self._lock:
            try:
                self.settings.index_dir.mkdir(parents=True, exist_ok=True)
                payload: dict[str, Any] = {
                    "version": INDEX_FORMAT_VERSION,
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "last_ingested_at": self._last_ingested_at,
                    "backend": str(getattr(self.backend, "name", "memory")),
                    "embedding_provider": self.embeddings.provider_name,
                    "embedding_model": self.embeddings.model,
                    "dimension": self.embeddings.dimension,
                    "documents": [asdict(entry) for entry in self.documents.values()],
                }
                _atomic_write_text(self.manifest_path, json.dumps(payload, ensure_ascii=False, indent=2))
                with self.chunks_path.open("w", encoding="utf-8") as handle:
                    for chunk in self._chunks.values():
                        handle.write(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n")
                self.backend.persist()
                logger.info("知识库已持久化：%d 篇文档 / %d 个切块", len(self.documents), len(self._chunks))
            except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断在线服务
                logger.error("知识库持久化失败：%s", exc)

    def load(self) -> bool:
        """恢复清单与切块副本；后端为空时按切块重建索引。返回是否恢复到内容。"""
        manifest = self.manifest_path
        if not manifest.exists():
            logger.info("未发现知识库清单，按空知识库启动：%s", manifest)
            return False
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("知识库清单读取失败，按空知识库启动：%s", exc)
            return False

        version = int(payload.get("version") or 0)
        if version > INDEX_FORMAT_VERSION:
            logger.warning(
                "知识库清单版本 %d 高于当前支持的 %d，字段可能不兼容（尽力读取）", version, INDEX_FORMAT_VERSION
            )
        known = {item.name for item in fields(DocumentEntry)}
        documents: dict[str, DocumentEntry] = {}
        for item in payload.get("documents") or []:
            if not isinstance(item, dict) or not item.get("doc_id"):
                continue
            try:
                documents[str(item["doc_id"])] = DocumentEntry(**{k: v for k, v in item.items() if k in known})
            except (TypeError, ValueError) as exc:
                logger.warning("清单中一条文档记录无法解析，已跳过：%s", exc)

        chunks = {chunk.chunk_id: chunk for chunk in self._load_chunks()}
        with self._lock:
            self.documents = documents
            self._chunks = chunks
            self._last_ingested_at = payload.get("last_ingested_at")

        logger.info(
            "知识库清单加载完成：%d 篇文档 / %d 个切块（后端=%s）",
            len(self.documents), len(self._chunks), getattr(self.backend, "name", "?"),
        )
        if chunks and self._backend_count() == 0:
            # 清单在、后端空（换了后端 / 向量文件丢失）：按切块重建索引，避免「有文档但搜不到」
            logger.warning("检索后端为空但清单有 %d 个切块，开始重建索引", len(chunks))
            self._reindex_from_chunks()
        return self.loaded

    # ---------------------------------------------------------------- 路径
    @property
    def manifest_path(self) -> Path:
        """``<data>/index/kb_index_docs.json``。"""
        return self.settings.index_path.with_name(self.settings.index_path.name + "_docs.json")

    @property
    def chunks_path(self) -> Path:
        """``<data>/index/kb_index_chunks.jsonl``。"""
        return self.settings.index_path.with_name(self.settings.index_path.name + "_chunks.jsonl")

    # ---------------------------------------------------------------- 内部
    def _embed(self, chunks: list[Chunk]) -> tuple[list[list[float]] | None, str | None]:
        """索引期向量化：严格 + 重试一次；``semantic=False``（明确降级模式）才允许伪向量。"""
        texts = [chunk.text for chunk in chunks]
        if not self.embeddings.enabled:
            # 向量化关闭：写空向量，切块仍可被关键词检索
            return [[] for _ in texts], None
        try:
            return self.embeddings.embed_for_index(texts), None
        except Exception as exc:  # noqa: BLE001 - 重试一次，外部接口偶发抖动很常见
            logger.warning("向量化失败，重试一次：%s", exc)
            try:
                return self.embeddings.embed_for_index(texts), None
            except Exception as retry_exc:  # noqa: BLE001
                if not self.embeddings.semantic:
                    logger.warning("向量化持续失败，按伪向量写入（该文档只能被关键词命中）：%s", retry_exc)
                    return [self.embeddings.pseudo_vector(text) for text in texts], None
                logger.error("向量化持续失败，本次不入库（保留原文件，可重试）：%s", retry_exc)
                return None, f"向量化失败：{retry_exc}"

    def _backend_count(self) -> int:
        try:
            return int(self.backend.count())
        except Exception as exc:  # noqa: BLE001 - 后端不可用时按内存副本数报
            logger.debug("后端计数失败：%s", exc)
            return len(self._chunks)

    def _reindex_from_chunks(self) -> int:
        """用清单里的切块重建后端索引（恢复场景；向量走缓存，失败按伪向量兜底）。"""
        chunks = sorted(self._chunks.values(), key=lambda item: (item.doc_id, item.ordinal))
        if not chunks:
            return 0
        vectors, error = self._embed(chunks)
        if error is not None or vectors is None:
            logger.error("重建索引失败（向量化不可用）：%s", error)
            return 0
        try:
            written = self.backend.upsert(
                [IndexRow(chunk=chunk, vector=vector) for chunk, vector in zip(chunks, vectors, strict=True)]
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("重建索引写入失败：%s", exc)
            return 0
        logger.info("索引重建完成：%d 个切块", written)
        return written

    def _load_chunks(self) -> list[Chunk]:
        """从 JSONL 恢复切块；文件缺失或单行损坏都不致命（跳过坏行）。"""
        path = self.chunks_path
        if not path.exists():
            return []
        chunks: list[Chunk] = []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("切块文件读取失败：%s", exc)
            return []
        for line_number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                chunks.append(Chunk.model_validate(json.loads(stripped)))
            except (ValueError, TypeError) as exc:  # 坏行跳过，保住其余切块
                logger.warning("切块文件第 %d 行解析失败，已跳过：%s", line_number, exc)
        return chunks

    def _autopersist(self) -> None:
        """入库 / 删除后自动落盘（单文档级别；大批量导入建议由调用方控制节奏）。"""
        self.persist()


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写文本：先写 ``.tmp`` 再 ``os.replace``，避免断电留下半个 JSON。"""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


__all__ = ["INDEX_FORMAT_VERSION", "DocumentEntry", "KnowledgeStore", "sha256_text"]