"""全链路与可选模块测试：入库 → 检索 → 证据闸门 → 证据块 → 兜底答案。

这一组是「无外部依赖可跑」这条硬约束的验收：
``vector_backend=memory`` + ``embedding_provider=hash`` + 不联网，全链路必须工作。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from test_rag_support import (  # noqa: E402
    SAMPLE_LEAVE,
    SAMPLE_TRAVEL,
    build_settings,
    build_stack,
    document_of,
    enabled_hash_embeddings,
    ingest_text,
)
from tsukiyohub.kernel.rag.answer import (  # noqa: E402
    build_evidence_block,
    extractive_answer,
    progress_found,
    source_line,
)
from tsukiyohub.kernel.rag.grounding import evaluate_grounding  # noqa: E402
from tsukiyohub.kernel.rag.multi_query import MultiQueryRetriever, RetrievalRequest, SubQuery  # noqa: E402
from tsukiyohub.kernel.rag.prompt import (  # noqa: E402
    NO_HIT_ANSWER,
    build_citations,
    format_context,
    pack_context,
)
from tsukiyohub.kernel.rag.semantic_cache import SemanticCache  # noqa: E402


def test_full_chain_offline(tmp_path: Path) -> None:
    """memory + hash（演示模式）+ 无网络：入库 → 检索 → 证据闸门 → 证据块 → 兜底答案。

    演示模式下 ``provider=hash`` 的工厂实现是 ``enabled=False``：**检索只走关键词（BM25）**，
    因此命中上没有 ``vector_score``，闸门走 BM25 分支（分数 > 0 且问题实词有交集）。
    这正是「无外部依赖可跑」的验收形态——不需要语义向量也能完整回答。
    """
    stack = build_stack(build_settings(tmp_path))
    first = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    ingest_text(stack.store, "travel.md", SAMPLE_TRAVEL)
    assert first.error is None

    hits = stack.store.retrieve("年假由谁审批")
    assert hits, "关键词检索必须命中"
    assert hits[0].vector_score is None  # 演示模式不产生稠密分
    assert hits[0].bm25_score and hits[0].bm25_score > 0

    # BM25 分支不看余弦阈值（阈值与 embedding 模型绑定，见 docs/rag.md）
    gate = evaluate_grounding(hits, stack.settings.grounding_min_score, question="年假由谁审批")
    assert gate.grounded is True, gate.reason
    assert "BM25" in gate.reason
    assert gate.score > 0

    evidence = build_evidence_block(hits)
    assert evidence.startswith("[1] 《")
    assert "年假" in evidence

    fallback = extractive_answer(hits)
    assert "《请假管理规范》" in fallback
    assert "[1]" in fallback

    line = source_line(hits[0].chunk)
    assert line.startswith("《请假管理规范》")
    assert "4. 请假管理" in line

    citations = build_citations(hits)
    assert [citation.index for citation in citations] == list(range(1, len(hits) + 1))
    assert citations[0].doc_id == hits[0].chunk.doc_id
    assert citations[0].snippet


def test_full_chain_with_opt_in_hash_vectors(tmp_path: Path) -> None:
    """显式打开哈希向量时：混合检索（稠密 + BM25 + RRF）与余弦闸门也能跑通。

    这是「后端支持混合检索」的能力验证。生产**不要**把 hash 当语义向量：
    它的余弦分布与语义无关（相关 0.30~0.51 / 无关 0.298），阈值不可标定。
    """
    settings = build_settings(tmp_path)
    stack = build_stack(settings, embeddings=enabled_hash_embeddings(settings))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    hits = stack.store.retrieve("年假由谁审批")
    assert hits and hits[0].vector_score is not None
    assert hits[0].bm25_score is not None

    gate = evaluate_grounding(hits, 0.2, question="年假由谁审批")
    assert gate.grounded is True, gate.reason
    assert "余弦" in gate.reason


def test_no_hit_chain_returns_standard_text(tmp_path: Path) -> None:
    """不相关问题：闸门判 miss，兜底答案给标准「没有找到」话术。

    这里不依赖阈值（hash 模式下 ``config`` 会把阈值适配成 0）：问题实词在片段里
    一个都没出现 → 闸门的词面交集否决必须生效。
    """
    stack = build_stack(build_settings(tmp_path))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    question = "火星殖民计划怎么报名"
    hits = stack.store.retrieve(question)
    gate = evaluate_grounding(hits, 0.0, question=question)
    assert gate.grounded is False, gate.reason
    assert extractive_answer([]) == NO_HIT_ANSWER
    assert extractive_answer(hits, max_chunks=0) != ""  # 有命中时不会说谎


def test_format_context_and_pack_context(tmp_path: Path) -> None:
    """参考资料渲染：编号从 1 连续；pack_context 按预算装入。"""
    stack = build_stack(build_settings(tmp_path))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    hits = stack.store.retrieve("年假", top_k=2)

    context = format_context(hits)
    assert context.startswith("[1] 《请假管理规范》")

    numbered = list(enumerate(hits, start=1))
    packed = pack_context(numbered, budget_tokens=1000)
    assert "[1]" in packed
    assert "《请假管理规范》" in packed


def test_parent_child_chunking_is_optional(tmp_path: Path) -> None:
    """parent_child 模式：子块带 parent_id，父块正文可回填。"""
    settings = build_settings(tmp_path)
    stack = build_stack(settings)
    long_text = "\n\n".join(f"## 第 {index} 节\n\n这一节讲的是年假与补卡的办理条件与时限。" for index in range(6))
    document = document_of("d1", "长制度", long_text)

    chunks, parents = stack.chunker.split_parent_child(document)

    assert chunks and parents
    assert all(chunk.extra.get("parent_id") in parents for chunk in chunks)
    # article 模式仍是默认（无 parent_id）
    assert all("parent_id" not in chunk.extra for chunk in stack.chunker.split(document))


def test_multi_query_optional_module(tmp_path: Path) -> None:
    """多路查询是可选模块：多说法并行召回后按 RRF 融合，能正常返回。"""
    stack = build_stack(build_settings(tmp_path))
    ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    retriever = MultiQueryRetriever(stack.settings, stack.backend, stack.embeddings)

    result = retriever.retrieve(
        RetrievalRequest(
            queries=[SubQuery("年假由谁审批"), SubQuery("补卡提交期限", kind="rewrite", weight=0.8)],
            top_k=5,
            per_query_k=10,
        )
    )

    assert result.items
    assert len(result.per_query) == 2
    assert result.partial is False
    assert result.timings["total"] >= 0
    assert result.items[0].score >= result.items[-1].score


def test_semantic_cache_is_off_by_default_and_scoped(tmp_path: Path) -> None:
    """语义缓存默认关闭；显式打开后同问题命中、不同问题不命中、可 forget。

    语义缓存必须有可用向量（``enabled=True``），所以这里注入打开的哈希向量化器。
    """
    settings = build_settings(tmp_path)
    embeddings = enabled_hash_embeddings(settings)
    disabled = SemanticCache(embeddings)
    assert disabled.active is False
    assert disabled.put("年假有几天", "满一年 5 天", citations=[{"index": 1}]) is False
    assert disabled.lookup("年假有几天") is None

    cache = SemanticCache(embeddings, enabled=True, threshold=0.95)
    assert cache.active is True
    assert cache.put("年假有几天", "满一年 5 天 [1]", citations=[{"index": 1, "doc_id": "d1"}]) is True

    hit = cache.lookup("年假有几天")
    assert hit is not None
    assert hit.entry.answer == "满一年 5 天 [1]"
    assert hit.similarity >= 0.95

    assert cache.lookup("差旅住宿标准是什么") is None
    assert cache.lookup("年假有几天", scope="other") is None  # 作用域隔离

    assert cache.forget(["满一年 5 天 [1]"]) == 1
    assert cache.lookup("年假有几天") is None
    assert cache.overview()["entries"] == 0


def test_semantic_cache_invalidation_by_doc_version(tmp_path: Path) -> None:
    """引用文档版本变化 → 缓存失效（避免用旧版本的回答）。"""
    settings = build_settings(tmp_path)
    cache = SemanticCache(enabled_hash_embeddings(settings), enabled=True, threshold=0.95)
    cache.put("年假有几天", "满一年 5 天 [1]", citations=[{"index": 1}], doc_versions={"d1": "v1"})

    assert cache.lookup("年假有几天", doc_versions={"d1": "v1"}) is not None
    assert cache.lookup("年假有几天", doc_versions={"d1": "v2"}) is None


def test_progress_found_text() -> None:
    assert progress_found([]) == ""
    assert progress_found(["请假管理规范"]) == "正在查找：《请假管理规范》"
    text = progress_found(["请假管理规范", "差旅管理规定", "报销制度"])
    assert "等 3 份资料" in text