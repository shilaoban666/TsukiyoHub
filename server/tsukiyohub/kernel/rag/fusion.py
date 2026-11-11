"""检索融合与轻量重排（纯算法，零外部依赖）。

为什么自己实现 RRF，而不用现成框架
----------------------------------
1. 向量分数（余弦，0~1）与 BM25 分数（无上界）量纲不可比，加权求和必须先归一化，
   而归一化尺度随查询漂移；
2. RRF 只依赖**名次**：``score(d) = Σ_r w_r / (k + rank_r(d))``，无需归一化，
   且「两路都排前」的片段天然得分最高；
3. 逻辑只有二十行，放在内核里可以同时被内存后端、多路检索、重排复用，
   行为完全可测（不同后端因此能对齐）。

本模块的职责边界
----------------
* :func:`reciprocal_rank_fusion` / :func:`weighted_rrf`：多路名次 → 融合分；
* :func:`l1_rerank`：在融合分上做**乘法**词面加权（零成本、始终可跑）；
* :func:`chunk_matches`：本地过滤兜底（后端过滤 + 本地二次过滤双保险）；
* 去冗余工具：:func:`dedupe_parents` / :func:`dedupe_versions` / :func:`cap_per_doc`。

注意：这里**不做**分数刻度转换。``ScoredChunk.vector_score`` 一律保存**原始余弦**，
证据闸门也在余弦刻度上比较（见 :mod:`tsukiyohub.kernel.rag.grounding`）。
"""

from __future__ import annotations

import re
from collections import defaultdict

from tsukiyohub.domain import Chunk, ScoredChunk
from tsukiyohub.kernel.rag.tokenize import token_set, tokenize
from tsukiyohub.ports.vector import SearchFilters

#: RRF 平滑常数（60 是文献里的通用取值，越大越平权）
DEFAULT_FUSION_K = 60
#: 关键词型问题的判定：编号 / 型号 / 金额 / 英文缩写 / 引号专名
_KEYWORDISH = re.compile(r"[A-Za-z]{2,}|\d{3,}|[￥$]\s*\d|\d+(?:\.\d+)?\s*(?:元|万|%|个?月|天|小时)")
_NORM = re.compile(r"[\s，。？！?!,.、；;：:]+")


def reciprocal_rank_fusion(
    ranked_lists: list[tuple[list[Chunk], float]],
    k: int = DEFAULT_FUSION_K,
) -> list[ScoredChunk]:
    """加权 RRF 融合。

    Args:
        ranked_lists: ``[(有序切块列表, 权重), ...]``；权重 ``<= 0`` 的路直接跳过。
        k: RRF 平滑常数。

    Returns:
        按融合分降序的 :class:`ScoredChunk` 列表（``retriever="fusion"``）。
        同一 ``chunk_id`` 只保留首次出现的切块对象（后续路只加分）。
    """
    fused: dict[str, float] = defaultdict(float)
    registry: dict[str, Chunk] = {}
    for chunks, weight in ranked_lists:
        if not chunks or weight <= 0:
            continue
        for rank, chunk in enumerate(chunks, start=1):
            fused[chunk.chunk_id] += weight / (k + rank)
            registry.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    return [
        ScoredChunk(chunk=registry[chunk_id], score=round(score, 6), retriever="fusion")
        for chunk_id, score in ordered
    ]


def weighted_rrf(ranked: list[tuple[list[Chunk], float]], k: int = DEFAULT_FUSION_K) -> list[ScoredChunk]:
    """:func:`reciprocal_rank_fusion` 的别名（多路检索里沿用这个名字更顺手）。"""
    return reciprocal_rank_fusion(ranked, k=k)


def l1_rerank(
    query: str,
    items: list[ScoredChunk],
    *,
    boost_docs: frozenset[str] | tuple[str, ...] = frozenset(),
    categories: frozenset[str] | tuple[str, ...] = frozenset(),
    history: bool = False,
) -> list[ScoredChunk]:
    """L1 词面重排：在融合分上乘一个「问句实词覆盖 + 元数据偏好」因子。

    为什么是乘法而不是加权求和：融合分只反映名次，量纲很小（~0.01），
    乘法能在**不打乱同一档名次**的前提下把「问到的词真的出现在片段里」的候选顶上来。

    因子（沿用源项目实测值）::

        factor  = 1 + 0.3 * |问句实词 ∩ 片段实词| / |问句实词|
        factor *= 1.15   命中标题 / 面包屑
        factor *= 0.70   片段被停用（active=False）且没有历史意图
        factor *= 1.10   doc_type 落在调用方给的偏好分类里
        factor *= 1.20   doc_id 在 boost_docs 里
        factor *= 0.80   片段过短（< 40 字，多半是表格碎片）

    注意：参数 ``items`` 会被**原地**修改 ``score``（它们由后端刚刚构造，不会被别处引用）。

    Args:
        query: 用户问题原文。
        items: 融合后的命中。
        boost_docs: 调用方希望优先的文档 ID。
        categories: 调用方希望优先的文档类型（``DocType.value``）。
        history: 有「历史版本」意图时不惩罚停用片段。

    Returns:
        按新分数降序、截断到原长度的列表。
    """
    tokens = {t for t in tokenize(query) if len(t) >= 2} or set(tokenize(query))
    if not tokens or not items:
        return items
    boost = set(boost_docs)
    cats = set(categories)
    total = len(tokens)
    for item in items:
        chunk = item.chunk
        factor = 1.0 + 0.3 * len(tokens & token_set(chunk.text)) / total
        breadcrumb = str(chunk.extra.get("breadcrumb") or "")
        if any(token in f"{chunk.title} {breadcrumb}" for token in tokens):
            factor *= 1.15
        if chunk.extra.get("active") is False and not history:
            factor *= 0.70
        if cats and chunk.doc_type.value in cats:
            factor *= 1.10
        if chunk.doc_id in boost:
            factor *= 1.20
        if len(chunk.text.strip()) < 40:
            factor *= 0.80
        item.score = round(item.score * factor, 6)
    return sorted(items, key=lambda x: x.score, reverse=True)


def keyword_heavy(text: str) -> bool:
    """问题含编号 / 型号 / 金额 / 英文缩写 → 词法权重应当调高。"""
    return bool(_KEYWORDISH.search(text or ""))


def norm_query(text: str) -> str:
    """问题归一化键（去空白与标点、小写），用于多路查询去重。"""
    return _NORM.sub("", (text or "").lower())


def chunk_matches(chunk: Chunk, filters: SearchFilters | None) -> bool:
    """本地过滤兜底：后端已经下推过滤，这里再判一次，防止某些后端忽略字段。

    语义与 :class:`SearchFilters` 一致：空列表 = 不限；多值之间是「或」。
    ``tags`` 与切块 ``extra['tags']`` 取交集；``active_only`` 时排除
    ``extra['active'] is False`` 的切块（没写过该标记的视为启用）。
    """
    if filters is None:
        return True
    if filters.active_only and chunk.extra.get("active") is False:
        return False
    if filters.doc_ids and chunk.doc_id not in set(filters.doc_ids):
        return False
    if filters.doc_types:
        wanted = {str(t).strip().lower() for t in filters.doc_types}
        if chunk.doc_type.value.lower() not in wanted:
            return False
    if filters.tags:
        wanted_tags = {str(t).strip().lower() for t in filters.tags}
        own = {str(t).strip().lower() for t in (chunk.extra.get("tags") or [])}
        if not (wanted_tags & own):
            return False
    return True


def dedupe_parents(items: list[ScoredChunk]) -> list[ScoredChunk]:
    """同一父块的多个子块只留最高分那个，其余分数的 30% 加到它身上。

    父块回填（``pack_context``）只认第一个出现的父块，留着其它子块只会占据名额。
    """
    kept: dict[str, ScoredChunk] = {}
    out: list[ScoredChunk] = []
    for item in items:
        parent_id = str(item.chunk.extra.get("parent_id") or "")
        if not parent_id:
            out.append(item)
            continue
        if parent_id in kept:
            head = kept[parent_id]
            head.score = round(head.score + item.score * 0.3, 6)
            continue
        kept[parent_id] = item
        out.append(item)
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def dedupe_versions(items: list[ScoredChunk], history: bool = False) -> list[ScoredChunk]:
    """同一文档的多个版本同时命中时只留「更新的那一版」。

    版本信息在 ``extra``：``active``（是否在用）+ ``version_date``（int）。
    有历史意图（``history=True``）时不做去重，调用方要看的正是历史版本。
    """
    if history:
        return items

    def rank(item: ScoredChunk) -> tuple[bool, int]:
        extra = item.chunk.extra
        return extra.get("active") is not False, int(extra.get("version_date") or 0)

    best: dict[str, tuple[bool, int]] = {}
    for item in items:
        key = item.chunk.doc_id
        if key not in best or rank(item) > best[key]:
            best[key] = rank(item)
    return [item for item in items if rank(item) == best[item.chunk.doc_id]]


def cap_per_doc(items: list[ScoredChunk], max_per_doc: int, top_k: int) -> list[ScoredChunk]:
    """每篇文档最多 ``max_per_doc`` 段（避免一份长文档占满名额）。

    超出的片段**不丢**：别的文档凑不满 ``top_k`` 时按原顺序补上——上限是为了多样性，
    不是为了丢掉同一份文档里的其它章节。
    """
    per_doc: dict[str, int] = defaultdict(int)
    out: list[ScoredChunk] = []
    overflow: list[ScoredChunk] = []
    for item in items:
        if per_doc[item.chunk.doc_id] >= max(1, max_per_doc):
            overflow.append(item)
            continue
        per_doc[item.chunk.doc_id] += 1
        out.append(item)
        if len(out) >= top_k:
            return out
    return out + overflow[: max(0, top_k - len(out))]


def as_retriever(items: list[ScoredChunk], kind: str) -> list[ScoredChunk]:
    """把一批命中的 ``retriever`` 字段统一改成 ``kind``（后端融合结果打标用）。"""
    return [item.model_copy(update={"retriever": kind}) for item in items]


__all__ = [
    "DEFAULT_FUSION_K", "as_retriever", "cap_per_doc", "chunk_matches", "dedupe_parents",
    "dedupe_versions", "keyword_heavy", "l1_rerank", "norm_query", "reciprocal_rank_fusion",
    "weighted_rrf",
]