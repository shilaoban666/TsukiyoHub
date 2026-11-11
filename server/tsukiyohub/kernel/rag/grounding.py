"""证据闸门：检索到的片段**是否真的能回答问题**（防幻觉第一道关）。

为什么不能只看融合分
--------------------
融合分（RRF）只反映名次，实测「相关」0.0163 / 「无关」0.0098，跨问题不可比，
拿它跟阈值比毫无意义。所以这里的置信度取片段上**跨查询可比的原始分**：

1. 有 ``rerank_score``（0~1）→ 用它；
2. 否则用 ``vector_score``（**原始余弦**）；
3. 否则用 ``bm25_score``（无上界，只能判「> 0 且词面有交集」）。

三道额外判定（都是实测踩出来的）
--------------------------------
* **闲聊短语**：「你好」的向量余弦可能高于阈值，只看分数会把打招呼当业务问题；
* **词面交集**：语义泛化型的无关问题余弦也可能不低，但问题里的实词在片段里一个都没出现，
  说明只是「泛似」而非命中；``GENERIC_TERMS``（规定 / 制度 / 流程这类每篇都有的词）不参与判断；
* **阈值刻度**：``min_score`` 在**原始余弦**刻度上，默认 ``settings.grounding_min_score``。
  换 embedding 模型必须重新标定——不同模型的余弦分布差异很大（这条要写进接入文档）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tsukiyohub.domain import ScoredChunk
from tsukiyohub.kernel.rag.tokenize import token_set, tokenize

#: 纯问候 / 自我介绍类问题（精确匹配短名单，宁漏勿错：「你好，请问年假有几天」不算闲聊）
CHITCHAT_PATTERNS: frozenset[str] = frozenset(
    """你好 您好 在吗 在么 哈喽 hello hi 嗨 早 早上好 下午好 晚上好
    你是谁 介绍一下你 你叫什么 你能做什么 你会做什么 你有什么功能
    谢谢 多谢 感谢 再见 拜拜 好的 收到""".split()
)

#: 泛词：判断「问题的实词是否出现在片段里」时不算数（几乎任何文档都会出现）
GENERIC_TERMS: frozenset[str] = frozenset(
    """规定 制度 办法 要求 标准 流程 政策 文件 内容 一般 具体 平时 每天 怎样 咋 怎么办 手续
    今天 现在 目前 能 想 知道 告诉 问问 帮 帮忙 申请 办理 提交 提前 多久 多长时间 什么时候""".split()
)

#: 模式名（写进 reason，便于调试与日志）
MODE_RERANK, MODE_VECTOR, MODE_BM25, MODE_NONE = "rerank", "vector", "bm25", "none"

_STRIP = re.compile(r"[\s？?！!。.，,、~～]")


@dataclass(frozen=True)
class GroundingResult:
    """闸门结论。

    Attributes:
        grounded: 是否可以基于这批片段作答。
        score: 判定所用的原始分（余弦 / 重排分 / BM25）；无命中时为 0。
        reason: 中文短句，说明为什么这么判（日志、``debug=true``、评估报告都用它）。
    """

    grounded: bool
    score: float
    reason: str


def grounding_conf(chunks: list[ScoredChunk], *, use_rerank: bool = True) -> tuple[float, str]:
    """置信度与口径：``rerank`` / ``vector`` / ``bm25`` / ``none``。

    取**各片段对应分数字段的最大值**（不是融合分）。
    """
    if use_rerank:
        rerank_scores = [item.rerank_score for item in chunks if item.rerank_score is not None]
        if rerank_scores:
            return max(rerank_scores), MODE_RERANK
    vector_scores = [item.vector_score for item in chunks if item.vector_score is not None]
    if vector_scores:
        return max(vector_scores), MODE_VECTOR
    bm25_scores = [item.bm25_score for item in chunks if item.bm25_score is not None]
    if bm25_scores:
        return max(bm25_scores), MODE_BM25
    return 0.0, MODE_NONE


def looks_like_chitchat(question: str) -> bool:
    """纯问候 / 自我介绍类问题（去标点后精确匹配，避免误伤「你好，请问…」）。"""
    compact = _STRIP.sub("", (question or "").strip().lower())
    return bool(compact) and compact in CHITCHAT_PATTERNS


def has_lexical_overlap(question: str, chunks: list[ScoredChunk], top_n: int = 3) -> bool:
    """问题的实词是否至少有一个出现在前 ``top_n`` 个片段里。

    分词可能切偏（「补卡要在几天内提交」→ 问句词切碎了），所以额外补一层二字片段：
    实词里的任一二字片段作为子串出现在原文里也算命中。
    问题全是停用词 / 泛词时返回 ``True``，把决定权交回分数。
    """
    query_tokens = set(tokenize(question))
    if not query_tokens:
        return True
    query_tokens = (query_tokens - GENERIC_TERMS) or query_tokens
    grams = {token[i:i + 2] for token in query_tokens if len(token) > 2 for i in range(len(token) - 1)} - GENERIC_TERMS
    for item in chunks[:top_n]:
        text = item.chunk.text
        if query_tokens & token_set(text):
            return True
        if any(gram in text for gram in grams):
            return True
    return False


def evaluate_grounding(
    chunks: list[ScoredChunk],
    min_score: float,
    *,
    question: str = "",
) -> GroundingResult:
    """证据闸门：这批片段是否足以支撑回答。

    Args:
        chunks: 检索命中（按融合分降序）。
        min_score: 余弦阈值（**原始余弦刻度**，默认 ``settings.grounding_min_score``）。
            重排分存在时同样用它比较（两者都是 0~1 且跨查询可比）。
        question: 可选。给了就额外做「闲聊」与「词面交集」判定，误判更少。

    Returns:
        :class:`GroundingResult`；``score`` 是判定所用的原始分。

    Examples:
        >>> evaluate_grounding([], 0.35).grounded
        False
    """
    if not chunks:
        return GroundingResult(False, 0.0, "没有检索到任何片段")
    if question and looks_like_chitchat(question):
        # 打招呼不是知识库问题：即使向量相近也不该拿文档来回答
        return GroundingResult(False, 0.0, "这是闲聊短语，不需要检索知识库")

    score, mode = grounding_conf(chunks)
    if mode == MODE_NONE:
        return GroundingResult(False, 0.0, "片段上没有可用的分数字段（后端未返回分数）")
    if mode == MODE_BM25:
        # BM25 无上界，阈值语义不适用；只要求「有交集且分数 > 0」
        if score > 0 and (not question or has_lexical_overlap(question, chunks)):
            return GroundingResult(True, float(score), f"关键词命中（BM25 {score:.2f}）")
        return GroundingResult(False, float(score), "关键词分数为 0 或问题实词未出现")

    passed = score >= float(min_score)
    if passed and question and not has_lexical_overlap(question, chunks):
        return GroundingResult(False, float(score), f"{mode} 分数 {score:.3f} 达标，但问题实词未出现在片段中")
    if passed:
        label = "重排分" if mode == MODE_RERANK else "向量余弦"
        return GroundingResult(True, float(score), f"{label} {score:.3f} ≥ 阈值 {float(min_score):.2f}")
    label = "重排分" if mode == MODE_RERANK else "向量余弦"
    return GroundingResult(False, float(score), f"{label} {score:.3f} < 阈值 {float(min_score):.2f}")


def is_grounded(chunks: list[ScoredChunk], min_score: float, *, question: str = "") -> bool:
    """:func:`evaluate_grounding` 的布尔便捷入口。"""
    return evaluate_grounding(chunks, min_score, question=question).grounded


__all__ = [
    "CHITCHAT_PATTERNS", "GENERIC_TERMS", "GroundingResult", "evaluate_grounding",
    "grounding_conf", "has_lexical_overlap", "is_grounded", "looks_like_chitchat",
]