"""RAG 内核测试的公共装配（不是测试用例本身）。

约定
----
* 数据目录一律用 pytest 的 ``tmp_path``（并通过 ``TSUKIYO_DATA_ROOT`` 指向它），
  **绝不污染项目 ``data/``**；
* 默认组合 = ``vector_backend=memory`` + ``embedding_provider=hash``：不联网、不装 Milvus
  也能跑完整链路——这正是「无外部依赖可跑」这条硬约束的验收方式。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: 让测试不依赖「包已安装」：把 server/ 放进 sys.path（与自检命令里的 PYTHONPATH 等价）
_SERVER = Path(__file__).resolve().parents[1] / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from tsukiyohub.adapters.embedding import HashEmbeddingProvider, build_embeddings  # noqa: E402
from tsukiyohub.adapters.vector_memory import MemoryVectorStore, build_memory_store  # noqa: E402
from tsukiyohub.config import Settings  # noqa: E402
from tsukiyohub.domain import DocType, Document, DocumentMeta, IngestResult, SourceType  # noqa: E402
from tsukiyohub.kernel.rag.chunker import TextChunker  # noqa: E402
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings  # noqa: E402
from tsukiyohub.kernel.rag.retriever import HybridRetriever  # noqa: E402
from tsukiyohub.kernel.rag.store import KnowledgeStore  # noqa: E402


def build_settings(tmp_path: Path, **overrides: object) -> Settings:
    """构造一份「指向临时目录 + memory/hash + 无网络」的配置。"""
    data_root = Path(tmp_path) / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["TSUKIYO_DATA_ROOT"] = str(data_root)
    values: dict[str, object] = {
        "data_root": data_root,
        "embedding_enabled": True,
        "embedding_provider": "hash",
        "embedding_dimension": 256,
        "vector_backend": "memory",
        "kb_auto_load_index": False,
        "chunk_size": 700,
        "chunk_overlap": 120,
        "retrieval_top_k": 8,
        "grounding_min_score": 0.35,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@dataclass
class Stack:
    """一套装配好的 RAG 组件（每个测试自己一份，互不干扰）。"""

    settings: Settings
    embeddings: CachedEmbeddings
    backend: MemoryVectorStore
    chunker: TextChunker
    retriever: HybridRetriever
    store: KnowledgeStore


def build_stack(settings: Settings, *, embeddings: CachedEmbeddings | None = None) -> Stack:
    """按 container.py 的顺序装配（与生产装配保持一致，测的才是真链路）。

    Args:
        settings: 配置。
        embeddings: 可选注入（例如显式 ``enabled=True`` 的哈希向量化器，用于验证稠密通路）。
            缺省用 :func:`build_embeddings` 的结果——``provider=hash`` 时它是
            ``enabled=False``（演示模式只走关键词检索）。
    """
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    if embeddings is None:
        embeddings = CachedEmbeddings(build_embeddings(settings), cache_path=settings.index_dir / "embed_cache.jsonl")
    backend = build_memory_store(settings)
    chunker = TextChunker(settings)
    retriever = HybridRetriever(settings, backend, embeddings)
    store = KnowledgeStore(settings, backend, embeddings, chunker, retriever)
    return Stack(settings=settings, embeddings=embeddings, backend=backend, chunker=chunker,
                 retriever=retriever, store=store)


def enabled_hash_embeddings(settings: Settings) -> CachedEmbeddings:
    """显式打开的哈希向量化器（**只用于测试**稠密通路 / 分数刻度）。

    生产装配里 ``provider=hash`` 走的是 ``enabled=False``：哈希余弦与语义无关，
    阈值不可标定，会让闸门把「知识库没有」误判成「有」。
    """
    provider = HashEmbeddingProvider(settings.embedding_dimension, enabled=True)
    return CachedEmbeddings(provider, cache_path=None)


def ingest_text(store: KnowledgeStore, name: str, text: str, **kwargs: object) -> IngestResult:
    """把一段文本当成「上传的文件」入库。"""
    return store.ingest_bytes(name, text.encode("utf-8"), **kwargs)


def document_of(doc_id: str, title: str, content: str, *, doc_type: DocType = DocType.DOC,
                tags: list[str] | None = None) -> Document:
    """直接构造 ``Document``（绕开文件解析，用于精细控制切块输入）。"""
    meta = DocumentMeta(
        doc_id=doc_id,
        title=title,
        source_type=SourceType.API,
        doc_type=doc_type,
        uri=f"{doc_id}.md",
        file_name=f"{doc_id}.md",
        tags=list(tags or []),
    )
    return Document(meta=meta, content=content)


#: 两份结构清晰的中文样例文档（标题层级 + 表格），多个测试共用
SAMPLE_LEAVE = """# 请假管理规范

## 4. 请假管理

### 4.2 审批权限

年假由直属主管审批，连续请假超过 3 天需要二级审批。年假天数按工龄计算，满一年 5 天。

### 4.3 补卡

补卡需在 3 个工作日内提交，逾期不予受理。

## 5. 报销

差旅住宿标准如下：

| 城市 | 上限 |
| --- | --- |
| 一线城市 | 500 元 |
| 其他城市 | 350 元 |
"""

SAMPLE_TRAVEL = """# 差旅管理规定

## 1. 适用范围

本规定适用于全体员工出差。

## 2. 交通标准

高铁二等座、飞机经济舱；单程超过 4 小时可乘高铁一等座。
"""


__all__ = [
    "SAMPLE_LEAVE", "SAMPLE_TRAVEL", "Stack", "build_settings", "build_stack", "document_of",
    "enabled_hash_embeddings", "ingest_text",
]