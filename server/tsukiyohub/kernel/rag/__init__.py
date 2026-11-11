"""RAG 内核：解析 → 切块 → 向量化 → 混合检索 → 证据闸门 → 逐句核对。

分层（依赖方向只能向下）
------------------------
::

    store / retriever / multi_query / semantic_cache   编排
    prompt / answer / grounding / ground_check         纯算法（无 IO）
    chunker / parser / tokenize / fusion / embeddings  基础能力
    ports.*                                          端口协议（VectorStore / EmbeddingProvider / Reranker）

三条硬约束（改代码时别破坏）
----------------------------
1. **无外部依赖可跑**：``vector_backend=memory`` + ``embedding_provider=hash`` 时，
   「入库 → 检索 → 证据闸门」全链路不联网即可完成（测试覆盖）。
2. **降级不阻断**：向量化关闭 → 纯关键词；检索后端失败 → 返回空、上层走「知识库暂无」；
   重排失败 → 保留融合顺序。任何一环失败都只是**质量下降**，不是 500。
3. **索引期严格、查询期宽松**：索引期向量化失败要重试、仍失败就整篇返回 ``error``
   （绝不写伪向量污染索引）；查询期失败才回退伪向量。

分数刻度：``ScoredChunk.score`` 是融合分（只反映名次），``vector_score`` 是**原始余弦**
（证据闸门用它）。两者不可混比。
"""

from __future__ import annotations

from tsukiyohub.kernel.rag.answer import build_evidence_block, extractive_answer, progress_found, source_line
from tsukiyohub.kernel.rag.chunker import CHUNKER_VERSION, TextChunker
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings, EmbeddingError
from tsukiyohub.kernel.rag.fusion import (
    cap_per_doc,
    chunk_matches,
    dedupe_parents,
    dedupe_versions,
    l1_rerank,
    reciprocal_rank_fusion,
)
from tsukiyohub.kernel.rag.ground_check import CheckResult, check, numbers_of
from tsukiyohub.kernel.rag.grounding import GroundingResult, evaluate_grounding, grounding_conf
from tsukiyohub.kernel.rag.multi_query import MultiQueryRetriever, RetrievalRequest, SubQuery
from tsukiyohub.kernel.rag.parser import SUPPORTED_SUFFIXES, DocumentParser, ParseError, build_document_meta
from tsukiyohub.kernel.rag.prompt import (
    ANSWER_TEMPLATE,
    GROUNDING_RULES,
    NO_HIT_ANSWER,
    build_citations,
    format_context,
    grounding_block,
    pack_context,
)
from tsukiyohub.kernel.rag.retriever import HybridRetriever
from tsukiyohub.kernel.rag.semantic_cache import SemanticCache
from tsukiyohub.kernel.rag.store import DocumentEntry, KnowledgeStore
from tsukiyohub.kernel.rag.tokenize import add_words, prewarm, token_set, tokenize, tokenizer_name

__all__ = [
    # 分词 / 切块 / 解析
    "tokenize", "token_set", "tokenizer_name", "prewarm", "add_words",
    "TextChunker", "CHUNKER_VERSION",
    "SUPPORTED_SUFFIXES", "DocumentParser", "ParseError", "build_document_meta",
    # 向量化与检索
    "CachedEmbeddings", "EmbeddingError",
    "HybridRetriever", "MultiQueryRetriever", "RetrievalRequest", "SubQuery",
    "KnowledgeStore", "DocumentEntry",
    "reciprocal_rank_fusion", "l1_rerank", "chunk_matches", "dedupe_versions", "dedupe_parents", "cap_per_doc",
    # 证据与答案
    "GroundingResult", "evaluate_grounding", "grounding_conf",
    "check", "CheckResult", "numbers_of",
    "source_line", "build_evidence_block", "extractive_answer", "progress_found",
    "format_context", "pack_context", "build_citations",
    "NO_HIT_ANSWER", "ANSWER_TEMPLATE", "GROUNDING_RULES", "grounding_block",
    # 可选模块
    "SemanticCache",
]