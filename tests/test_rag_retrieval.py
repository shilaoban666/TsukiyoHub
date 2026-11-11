"""检索链路测试：分词 / BM25 / 余弦刻度 / 过滤 / 降级 / 后端契约一致性。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from test_rag_support import (  # noqa: E402
    SAMPLE_LEAVE,
    SAMPLE_TRAVEL,
    build_settings,
    build_stack,
    enabled_hash_embeddings,
    ingest_text,
)
from tsukiyohub.adapters.embedding import HashEmbeddingProvider, build_embeddings  # noqa: E402
from tsukiyohub.adapters.rerank_dashscope import build_reranker  # noqa: E402
from tsukiyohub.adapters.vector_memory import MemoryVectorStore  # noqa: E402
from tsukiyohub.adapters.vector_milvus import MilvusUnavailable, build_milvus_store  # noqa: E402
from tsukiyohub.domain import Chunk, DocType  # noqa: E402
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings  # noqa: E402
from tsukiyohub.kernel.rag.tokenize import add_words, tokenize, tokenizer_name  # noqa: E402
from tsukiyohub.ports.vector import IndexRow, SearchFilters  # noqa: E402


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------
def test_tokenize_chinese_words_and_english_tokens() -> None:
    """中文按词切分、英文 / 编号整串保留、停用词被去掉。"""
    tokens = tokenize("年假有几天？")
    assert "几天" in tokens
    assert "有" not in tokens  # 停用词
    assert all(token == token.lower() for token in tokens)
    assert tokenize("OA-2024-017 表单")[0] == "oa-2024-017"
    assert tokenize("") == []
    assert tokenizer_name() in ("jieba", "bigram")


def test_add_words_registers_domain_terms() -> None:
    """行业专有词可以登记进 jieba 词典（默认词典会把「年假」切成「年」「假」）。

    没有 jieba（或 jieba 初始化失败）时 ``add_words`` 必须**安全返回 0**，而不是抛异常。
    """
    if tokenizer_name() != "jieba":
        assert add_words(["年假"]) == 0
        return
    assert add_words(["年假", "补卡"]) == 2
    assert "年假" in tokenize("年假有几天")
    assert "补卡" in tokenize("补卡几天内提交")


def test_add_words_returns_zero_without_jieba(monkeypatch) -> None:
    """jieba 不可用时 ``add_words`` 安全返回 0（不能让入库流程崩在这里）。"""
    # 注意：包 __init__ 里的 ``tokenize`` 是函数名，会遮蔽同名子模块，所以按 sys.modules 取模块
    tokenize_module = sys.modules["tsukiyohub.kernel.rag.tokenize"]

    monkeypatch.setattr(tokenize_module, "jieba_available", lambda: False)
    assert tokenize_module.add_words(["年假"]) == 0


# ---------------------------------------------------------------------------
# BM25 / 混合
# ---------------------------------------------------------------------------
def test_bm25_finds_keyword_chunk(tmp_path: Path) -> None:
    """关键词通路必须命中：查询词出现在哪一段，哪一段就该排第一。"""
    stack = build_stack(build_settings(tmp_path))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    hits = stack.store.retrieve("补卡要几天内提交")
    assert hits
    assert "补卡" in hits[0].chunk.text
    assert hits[0].bm25_score is not None and hits[0].bm25_score > 0


def test_hybrid_returns_vector_and_bm25_scores(tmp_path: Path) -> None:
    """memory 后端 = 稠密 + BM25 + RRF：命中上应同时挂两种分数。

    生产装配里 ``provider=hash`` 是 ``enabled=False``（只走关键词），所以这里**显式注入**
    一个打开的哈希向量化器——只为验证后端混合检索与分数刻度，不代表生产推荐配置。
    """
    settings = build_settings(tmp_path)
    stack = build_stack(settings, embeddings=enabled_hash_embeddings(settings))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    hits = stack.store.retrieve("年假由谁审批")
    top = hits[0]
    assert top.vector_score is not None and top.vector_score > 0
    assert top.bm25_score is not None and top.bm25_score > 0
    assert top.retriever in ("hybrid", "vector", "bm25", "fusion")
    assert top.score > 0


def test_factory_hash_provider_is_keyword_only(tmp_path: Path) -> None:
    """``provider=hash`` 是演示模式：工厂必须返回 ``enabled=False``（检索只走关键词）。

    原因：哈希余弦与语义无关，相关 0.30~0.51 / 无关 0.298 完全重叠，阈值不可标定。
    """
    settings = build_settings(tmp_path, embedding_provider="hash")
    provider = build_embeddings(settings)
    assert provider.provider == "hash"
    assert provider.enabled is False
    assert provider.semantic is False
    # 类本身仍可用（显式 enabled=True 时才产生词面向量，供测试 / 调参）
    assert HashEmbeddingProvider(256, enabled=True).enabled is True


def test_vector_score_is_raw_cosine(tmp_path: Path) -> None:
    """刻度契约：``vector_score`` 必须是**原始余弦**（不是 1/(3-2cos)，也不做任何变换）。"""
    backend = MemoryVectorStore(dimension=8)
    chunk = Chunk(chunk_id="d1#0", doc_id="d1", text="无关正文", ordinal=0)
    backend.upsert([IndexRow(chunk=chunk, vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])])

    hits = backend.search(vector=[0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], query_text="", top_k=5)
    assert hits
    assert hits[0].vector_score is not None
    assert abs(hits[0].vector_score - 0.6) < 1e-6, "余弦 0.6 应原样出现在 vector_score 上"


def test_lexical_only_when_no_vector(tmp_path: Path) -> None:
    """向量为空（未启用 / 失败）时后端退化为纯关键词，且分数仍可比。"""
    backend = MemoryVectorStore(dimension=8)
    chunk = Chunk(chunk_id="d1#0", doc_id="d1", text="补卡需在三个工作日内提交", ordinal=0)
    backend.upsert([IndexRow(chunk=chunk, vector=[])])

    hits = backend.search(vector=None, query_text="补卡多久", top_k=5)
    assert len(hits) == 1
    assert hits[0].vector_score is None
    assert hits[0].bm25_score and hits[0].bm25_score > 0
    assert backend.search(vector=None, query_text="完全没有的词", top_k=5) == []


def test_upsert_replaces_same_chunk_id(tmp_path: Path) -> None:
    """同 chunk_id 全量覆盖，且 BM25 统计同步更新（不会残留旧词）。"""
    backend = MemoryVectorStore(dimension=8)
    backend.upsert([IndexRow(chunk=Chunk(chunk_id="d1#0", doc_id="d1", text="年假制度", ordinal=0), vector=[])])
    backend.upsert([IndexRow(chunk=Chunk(chunk_id="d1#0", doc_id="d1", text="报销制度", ordinal=0), vector=[])])

    assert backend.count() == 1
    assert backend.search(vector=None, query_text="年假", top_k=5) == []
    assert backend.search(vector=None, query_text="报销", top_k=5)


def test_remove_requires_explicit_target(tmp_path: Path) -> None:
    """remove() 不给任何条件时返回 0（绝不清库）。"""
    backend = MemoryVectorStore(dimension=8)
    backend.upsert([IndexRow(chunk=Chunk(chunk_id="d1#0", doc_id="d1", text="年假", ordinal=0), vector=[])])
    assert backend.remove() == 0
    assert backend.count() == 1
    assert backend.remove(doc_id="d1") == 1
    assert backend.count() == 0


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------
def test_filters_doc_types_and_doc_ids(tmp_path: Path) -> None:
    """doc_types / doc_ids 过滤在召回期生效（不是返回后再截断）。"""
    stack = build_stack(build_settings(tmp_path))
    leave = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE, doc_type=DocType.SPEC)
    travel = ingest_text(stack.store, "travel.md", SAMPLE_TRAVEL, doc_type=DocType.NOTE)

    spec_hits = stack.store.retrieve("住宿标准 交通标准", filters=SearchFilters(doc_types=["spec"]))
    assert spec_hits and all(hit.chunk.doc_type is DocType.SPEC for hit in spec_hits)

    only_leave = stack.store.retrieve("标准", filters=SearchFilters(doc_ids=[leave.doc_id]))
    assert only_leave and all(hit.chunk.doc_id == leave.doc_id for hit in only_leave)

    only_travel = stack.store.retrieve("标准", filters=SearchFilters(doc_ids=[travel.doc_id]))
    assert only_travel and all(hit.chunk.doc_id == travel.doc_id for hit in only_travel)

    # doc_types 便捷参数与 filters.doc_types 等价
    assert stack.store.retrieve("交通标准", doc_types=["note"])


def test_filters_tags(tmp_path: Path) -> None:
    """tags 取交集：标签对不上就是空结果。"""
    from test_rag_support import document_of

    stack = build_stack(build_settings(tmp_path))
    stack.store.upsert_document(document_of("t1", "财务制度", "住宿上限 500 元。", tags=["finance"]))
    stack.store.upsert_document(document_of("t2", "人事制度", "住宿上限 300 元。", tags=["hr"]))

    finance = stack.store.retrieve("住宿上限", filters=SearchFilters(tags=["finance"]))
    assert finance and all(hit.chunk.doc_id == "t1" for hit in finance)
    assert stack.store.retrieve("住宿上限", filters=SearchFilters(tags=["nope"])) == []


def test_top_k_and_empty_query(tmp_path: Path) -> None:
    """top_k 生效；空问题直接返回空（不打后端）。"""
    stack = build_stack(build_settings(tmp_path))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    assert len(stack.store.retrieve("年假", top_k=1)) <= 1
    assert stack.store.retrieve("   ") == []


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------
class _ToggleProvider:
    """索引期正常、查询期失败的向量化器（验证查询降级）。"""

    provider = "toggle"
    model = "toggle-v1"
    dimension = 256
    enabled = True
    semantic = True
    batch_size = 10

    def __init__(self) -> None:
        self.inner = HashEmbeddingProvider(256)
        self.fail = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("查询期网络抖动")
        return self.inner.embed(texts)

    def pseudo_vector(self, text: str) -> list[float]:
        return self.inner.pseudo_vector(text)


def test_query_vector_failure_falls_back_to_keyword(tmp_path: Path) -> None:
    """查询向量化失败 → 回退伪向量继续检索（不抛错），关键词仍然命中。"""
    settings = build_settings(tmp_path)
    provider = _ToggleProvider()
    embeddings = CachedEmbeddings(provider, cache_path=None)  # type: ignore[arg-type]
    from tsukiyohub.adapters.vector_memory import build_memory_store
    from tsukiyohub.kernel.rag.chunker import TextChunker
    from tsukiyohub.kernel.rag.retriever import HybridRetriever
    from tsukiyohub.kernel.rag.store import KnowledgeStore

    backend = build_memory_store(settings)
    store = KnowledgeStore(settings, backend, embeddings, TextChunker(settings),
                           HybridRetriever(settings, backend, embeddings))
    ingest_text(store, "leave.md", SAMPLE_LEAVE)

    provider.fail = True
    hits = store.retrieve("补卡要几天内提交")
    assert hits
    assert any(hit.bm25_score for hit in hits)


def test_milvus_backend_is_lazy_and_fails_clearly(tmp_path: Path) -> None:
    """pymilvus 未安装 / 连不上：构造不抛错，检索返回空，写入抛中文异常。"""
    settings = build_settings(tmp_path, vector_backend="milvus", milvus_timeout_seconds=0.5)
    backend = build_milvus_store(settings)  # 构造阶段不得 import pymilvus

    assert backend.name == "milvus"
    assert backend.supports_hybrid is False
    assert backend.search(vector=[0.1] * 256, query_text="年假") == []

    with pytest.raises(MilvusUnavailable) as info:
        backend.upsert([IndexRow(chunk=Chunk(chunk_id="d1#0", doc_id="d1", text="年假", ordinal=0),
                                 vector=[0.1] * 256)])
    message = str(info.value)
    assert "pymilvus" in message or "Milvus" in message


def test_reranker_defaults_to_noop(tmp_path: Path) -> None:
    """未配置重排（默认关闭）时返回 NoopReranker，而不是 None。"""
    settings = build_settings(tmp_path)
    reranker = build_reranker(settings)
    assert reranker is not None
    assert reranker.enabled is False
    assert reranker.name == "none"


def test_embedding_factory_falls_back_without_key(tmp_path: Path) -> None:
    """provider=openai 但没有密钥 → 不启动失败，降级成「不启用稠密」的哈希实现。"""
    settings = build_settings(tmp_path, embedding_provider="openai", embedding_enabled=True,
                              embedding_api_key=None, llm_api_key=None)
    provider = build_embeddings(settings)
    assert provider.provider == "hash"
    assert provider.enabled is False
    assert provider.dimension == settings.embedding_dimension


def test_hash_embeddings_are_deterministic_and_lexical() -> None:
    """哈希向量：同文本同向量；词面重合的文本余弦更高。"""
    provider = HashEmbeddingProvider(256)
    left = provider.embed(["年假有几天"])[0]
    same = provider.embed(["年假有几天"])[0]
    related = provider.embed(["年假天数"])[0]
    unrelated = provider.embed(["差旅住宿标准"])[0]

    assert left == same
    assert abs(math.sqrt(sum(value * value for value in left)) - 1.0) < 1e-6
    assert _cos(left, related) > _cos(left, unrelated)


def _cos(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))