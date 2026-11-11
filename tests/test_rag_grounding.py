"""证据闸门与逐句核对测试（防幻觉两段式）。

覆盖：
* ``evaluate_grounding`` 的**原始余弦**阈值语义（不是 RRF 分、不是 1/(3-2cos)）；
* 闲聊短语 / 词面交集的否决；
* ``ground_check.check`` 用**假 complete 函数**驱动：删句、失败保留、解析失败、auto 模式、可用性回退。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from tsukiyohub.domain import Chunk, ScoredChunk  # noqa: E402
from tsukiyohub.kernel.rag.ground_check import CheckResult, check, numbers_of  # noqa: E402
from tsukiyohub.kernel.rag.grounding import evaluate_grounding, has_lexical_overlap, looks_like_chitchat  # noqa: E402


def scored(
    text: str = "年假天数按工龄计算，满一年 5 天。",
    *,
    vector: float | None = None,
    bm25: float | None = None,
    rerank: float | None = None,
) -> ScoredChunk:
    """构造一条命中（分数可控，便于精确验证阈值）。"""
    chunk = Chunk(
        chunk_id="d1#0",
        doc_id="d1",
        text=text,
        ordinal=0,
        title="请假管理规范",
        extra={"breadcrumb": "4. 请假管理 > 4.2 审批权限"},
    )
    return ScoredChunk(chunk=chunk, score=0.01, vector_score=vector, bm25_score=bm25, rerank_score=rerank)


# ---------------------------------------------------------------------------
# 证据闸门
# ---------------------------------------------------------------------------
def test_no_chunks_is_not_grounded() -> None:
    result = evaluate_grounding([], 0.35)
    assert result.grounded is False
    assert result.score == 0.0
    assert "没有检索到" in result.reason


def test_threshold_compares_raw_cosine() -> None:
    """阈值比较的是原始余弦：0.36 ≥ 0.35 命中，0.34 < 0.35 不命中。"""
    hit = evaluate_grounding([scored(vector=0.36)], 0.35)
    miss = evaluate_grounding([scored(vector=0.34)], 0.35)

    assert hit.grounded is True
    assert abs(hit.score - 0.36) < 1e-9
    assert miss.grounded is False
    assert "阈值" in miss.reason


def test_max_score_across_chunks_is_used() -> None:
    """取各片段分数的最大值，而不是最后一条 / 平均值。"""
    result = evaluate_grounding([scored(vector=0.10), scored(vector=0.80)], 0.35)
    assert result.grounded is True
    assert abs(result.score - 0.80) < 1e-9


def test_rerank_score_takes_priority() -> None:
    """有重排分时优先用重排分（0~1，跨问题可比）。"""
    result = evaluate_grounding([scored(vector=0.10, rerank=0.92)], 0.35)
    assert result.grounded is True
    assert "重排分" in result.reason


def test_bm25_mode_needs_positive_score() -> None:
    """只有 BM25 分数时：> 0 即视为命中（无上界，不能拿余弦阈值比）。"""
    hit = evaluate_grounding([scored(vector=None, bm25=4.2)], 0.35)
    miss = evaluate_grounding([scored(vector=None, bm25=0.0)], 0.35)
    assert hit.grounded is True
    assert miss.grounded is False
    assert "BM25" in hit.reason


def test_no_scores_is_not_grounded() -> None:
    result = evaluate_grounding([scored()], 0.35)
    assert result.grounded is False
    assert "分数字段" in result.reason


def test_chitchat_is_never_grounded() -> None:
    """「你好」即使余弦很高也不该拿文档作答。"""
    assert looks_like_chitchat("你好") is True
    assert looks_like_chitchat("你好，请问年假有几天") is False

    result = evaluate_grounding([scored(vector=0.99)], 0.35, question="你好")
    assert result.grounded is False
    assert "闲聊" in result.reason


def test_lexical_overlap_veto() -> None:
    """分数达标但问题实词一个都没出现在片段里 → 不算命中（泛化相似不是命中）。"""
    chunks = [scored(vector=0.55)]
    assert has_lexical_overlap("年假有几天", chunks) is True
    assert has_lexical_overlap("火星殖民计划怎么报名", chunks) is False

    result = evaluate_grounding(chunks, 0.35, question="火星殖民计划怎么报名")
    assert result.grounded is False
    assert "实词" in result.reason


# ---------------------------------------------------------------------------
# 逐句核对
# ---------------------------------------------------------------------------
EVIDENCE = [(1, "年假天数按工龄计算，满一年 5 天。补卡需在 3 个工作日内提交。")]

ANSWER = (
    "**结论**：年假满一年 5 天 [1]\n\n"
    "1. **要点**：年假按工龄计算 [1]\n"
    "2. **提示**：一般当天就能办好 [1]"
)


def _complete_removing_one(prompt: str) -> str:
    assert "[1]" in prompt and "回答" in prompt  # 提示词里要有编号证据
    return '{"unsupported": [{"sentence": "一般当天就能办好", "reason": "资料没写"}], "ok": false}'


def test_strict_check_removes_unsupported_sentence() -> None:
    result = check(ANSWER, EVIDENCE, _complete_removing_one, mode="strict", question="年假几天")

    assert isinstance(result, CheckResult)
    assert result.checked is True
    assert result.error is None
    assert result.removed
    assert "当天就能办好" not in result.answer
    assert "年假满一年 5 天" in result.answer
    assert result.usable is True


def test_lenient_mode_records_but_keeps_answer() -> None:
    """lenient 只记录可疑句、不改答案（chat 的 ``answer_judge=lenient`` 走这条）。"""
    result = check(ANSWER, EVIDENCE, _complete_removing_one, mode="lenient", question="年假几天")
    assert result.checked is True
    assert result.answer == ANSWER
    assert result.removed == []
    assert result.unsupported  # 结论仍然记录下来了


def test_check_skips_without_reference_marks() -> None:
    """回答里没有 [n] 引用 → 不核对（免得无谓模型调用）。"""
    result = check("年假满一年 5 天", EVIDENCE, _complete_removing_one, mode="strict")
    assert result.checked is False
    assert result.answer == "年假满一年 5 天"


def test_check_off_mode_never_calls_model() -> None:
    def _boom(prompt: str) -> str:  # pragma: no cover - 不该被调用
        raise AssertionError("off 模式不应调用模型")

    result = check(ANSWER, EVIDENCE, _boom, mode="off")
    assert result.checked is False
    assert result.answer == ANSWER


def test_check_failure_keeps_answer() -> None:
    """核对调用失败 / 超时：按原答案返回并记录原因。"""
    def _fail(prompt: str) -> str:
        raise TimeoutError("模型超时")

    result = check(ANSWER, EVIDENCE, _fail, mode="strict")
    assert result.answer == ANSWER
    assert result.checked is False
    assert result.error == "TimeoutError"


def test_check_unparsable_output_keeps_answer() -> None:
    result = check(ANSWER, EVIDENCE, lambda prompt: "抱歉，我做不到", mode="strict")
    assert result.answer == ANSWER
    assert result.error == "unparsable"


def test_check_no_unsupported_means_pass() -> None:
    result = check(ANSWER, EVIDENCE, lambda prompt: '{"unsupported": [], "ok": true}', mode="strict")
    assert result.checked is True
    assert result.answer == ANSWER
    assert result.unsupported == []


def test_check_keeps_original_when_everything_removed() -> None:
    """删完没有剩下有引用的内容时保留原答案（避免把答案削成空话）。"""
    answer = "一般当天就能办好 [1]"
    result = check(
        answer,
        EVIDENCE,
        lambda prompt: '{"unsupported": [{"sentence": "一般当天就能办好", "reason": "无依据"}]}',
        mode="strict",
    )
    assert result.checked is True
    assert result.usable is False
    assert result.answer == answer


def test_auto_mode_only_checks_unknown_numbers() -> None:
    """auto 模式：回答里出现资料 / 问题都没有的数字才核对。"""
    evidence = [(1, "年假满一年 5 天。")]
    result = check("年假一共 999 天 [1]", evidence, lambda prompt: '{"unsupported": []}',
                   mode="auto", question="年假几天")
    assert result.checked is True

    clean = check("年假满一年 5 天 [1]", evidence, lambda prompt: '{"unsupported": []}',
                  mode="auto", question="年假几天")
    assert clean.checked is False


def test_numbers_of_extracts_meaningful_numbers() -> None:
    assert numbers_of("满一年 5 天，最多 15 天，3 个工作日内") == {"5", "15", "3"}
    assert numbers_of("第一步、第二步") == set()  # 0/1 这类序号不算信息量
    assert numbers_of("") == set()