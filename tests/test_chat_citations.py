"""引用编号与来源列表单测：``renumber`` / ``tidy_refs`` / ``with_citations`` 的语义。

这几条不变量一旦破坏，用户看到的正文编号与来源列表就对不上（最容易被反馈为「引用错乱」），
所以单独用一堆小用例钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tsukiyohub.domain import Citation  # noqa: E402
from tsukiyohub.kernel.chat.citations import (  # noqa: E402
    MAX_SOURCES,
    merge_pages,
    renumber,
    source_lines,
    tidy_refs,
    unique_sources,
    with_citations,
)


def _cite(index: int, doc_id: str = "doc-1", title: str = "示例手册", version: str = "") -> Citation:
    return Citation(index=index, doc_id=doc_id, title=title, version=version)


# ---------------------------------------------------------------------------
# renumber：逐段编号 → 逐文档编号
# ---------------------------------------------------------------------------
def test_renumber_merges_segments_of_same_document() -> None:
    citations = [_cite(1), _cite(2)]
    answer, docs, evidence = renumber(
        "年假 5 天[1]，需提前 3 天申请[2]。", citations, {1: "年假 5 天", 2: "需提前 3 天申请"},
    )

    assert len(docs) == 1 and docs[0].index == 1
    assert answer == "年假 5 天[1]，需提前 3 天申请[1]。"
    assert evidence[1] == "年假 5 天\n需提前 3 天申请"


def test_renumber_orders_documents_by_first_appearance() -> None:
    citations = [_cite(1, "doc-a", "A 文档"), _cite(2, "doc-b", "B 文档")]
    answer, docs, _evidence = renumber("先 B[2]，再 A[1]。", citations, {1: "a", 2: "b"})

    assert [doc.index for doc in docs] == [1, 2]
    assert [doc.doc_id for doc in docs] == ["doc-b", "doc-a"]
    assert answer == "先 B[1]，再 A[2]。"


def test_renumber_keeps_unknown_reference_untouched() -> None:
    answer, docs, _evidence = renumber("有依据[1]，无效编号[9]。", [_cite(1)], {1: "a"})

    assert "[9]" in answer
    assert [doc.index for doc in docs] == [1]


def test_renumber_with_pages_then_merge() -> None:
    citations = [_cite(1), _cite(2)]
    answer, _docs, _evidence = renumber("要点[1][2]。", citations, {1: "x", 2: "y"}, {1: 2, 2: 37})

    assert answer == "要点[1·第2页][1·第37页]。"
    assert merge_pages(answer) == "要点[1·第2、37页]。"
    assert merge_pages("[1][1]") == "[1]"


def test_renumber_sets_document_label() -> None:
    _answer, docs, _evidence = renumber("依据[1]。", [_cite(1, version="v2")], {1: "a"})

    assert docs[0].label == "《示例手册》 · v2"


# ---------------------------------------------------------------------------
# tidy_refs：否定句不挂引用、每句最多两篇
# ---------------------------------------------------------------------------
def test_tidy_refs_drops_reference_in_negative_sentence() -> None:
    out = tidy_refs("资料中未提及此事[1]。年假为 5 天[1]。", {1: "doc-1"})

    assert "此事[1]" not in out
    assert "年假为 5 天[1]" in out


def test_tidy_refs_limits_documents_per_sentence() -> None:
    out = tidy_refs("甲乙丙[1][2][3]。", {1: "a", 2: "b", 3: "c"})

    assert out.count("[") == 2
    assert "[3]" not in out


def test_tidy_refs_keeps_source_section_untouched() -> None:
    text = "年假 5 天[1]。\n\n参考来源：\n1、《示例手册》[9]"
    assert tidy_refs(text, {1: "doc-1"}).endswith("1、《示例手册》[9]")


# ---------------------------------------------------------------------------
# with_citations / 来源列表
# ---------------------------------------------------------------------------
def test_with_citations_appends_source_block() -> None:
    text = with_citations("年假 5 天[1]。", [_cite(1, version="v2")], markdown=False, limit=3)

    assert text.startswith("年假 5 天[1]。")
    assert "参考来源：" in text
    assert "1、《示例手册》 · v2" in text


def test_with_citations_returns_answer_when_no_citations() -> None:
    assert with_citations("你好", []) == "你好"


def test_unique_sources_dedupes_by_document() -> None:
    citations = [_cite(2, "doc-a", "A"), _cite(1, "doc-a", "A"), _cite(3, "doc-b", "B")]

    rows = unique_sources(citations)

    assert [doc.index for doc in rows] == [1, 3]
    assert len(rows) <= MAX_SOURCES


def test_source_lines_marks_cited_documents() -> None:
    lines = source_lines([_cite(1), _cite(2, "doc-2", "另一份")], markdown=False)

    assert lines[0].startswith("1、《示例手册》")
    assert lines[1].startswith("2、《另一份》")