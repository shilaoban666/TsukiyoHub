"""提示词素材：无命中话术、生成约束规则、参考资料渲染与上下文打包。

为什么把这些放在内核里而不是 chat 里
------------------------------------
「回答必须有出处」「资料没写的不补充」这类约束是**检索增强生成**的质量底线，
换任何前端、任何模型都该一样，所以它们跟检索结果一起属于 rag 内核；chat 只负责
把它们拼进自己的系统提示词（:func:`grounding_block` 就是给 chat 直接吃的片段）。

编号约定：``[n]`` 编号由调用方按检索顺序分配一次，:func:`format_context` /
:func:`build_evidence_block` 只负责渲染。编号 = 引用 ``Citation.index`` = 正文 ``[n]``，
三者必须一致，否则用户看到的就是假引用。
"""

from __future__ import annotations

from tsukiyohub.domain import Chunk, Citation, ScoredChunk

#: 未命中知识库时的标准话术（换行分段，便于任何渠道渲染）
NO_HIT_ANSWER = (
    "我在知识库里没有找到与这个问题相关的内容。\n\n"
    "你可以：\n"
    "1. 换一种说法再问一次（带上文档名称或关键词）；\n"
    "2. 确认该文档是否已经导入知识库；\n"
    "3. 换一个文档类型或标签再检索。"
)

#: AI 腔黑名单：写进生成提示词，也用于质量评估
BANNED_PHRASES: tuple[str, ...] = (
    "首先", "其次", "综上所述", "总而言之", "总之", "值得注意的是", "需要指出的是", "需要注意的是",
    "希望对你有帮助", "希望对您有帮助", "希望以上信息", "如有疑问请咨询", "如有其他问题", "根据参考资料",
    "根据提供的资料", "作为一个AI", "作为AI助手",
)

#: 知识库回答的固定版式（两条链路共用；引用列表由系统附在后面）
ANSWER_TEMPLATE = """【回答版式（知识库问题都按这个写）】
- 第一行：**一句话结论**（加粗），直接回答问题，句末标 [编号]；
- 空一行后分点展开：2~6 个编号要点，每点写成「**小标题**：内容」，把条件、数字、时限、例外、办理方式写清楚；流程题按步骤写，对比题逐项对照；
- 最后一行：「**总结**：」一两句复述最关键的条件、数字或时限（只用上面已写的内容，不加建议）；
- 每一句话（结论、要点里的每一句、总结）末尾都标 [编号]，编号只用参考资料里的；资料不支持的话不要写；
- 资料只够写一两点就只写一两点，不要为了凑要点写与问题无关的内容；
- 不要自己写「引用文档」「参考来源」，系统会附在回答后面。"""

#: 有据可查（防幻觉）：实测的幻觉几乎都是模型「好心补充」的经验、路径和建议
GROUNDING_RULES: tuple[str, ...] = (
    "涉及事实的每一句话（规定、数字、时限、入口、责任人、约束条件）都必须能在资料原文里找到，并标 [编号]。",
    "资料没写的不补充：不写「一般 / 通常 / 大概 / 实操上」之类的经验判断，"
    "不补办理路径、处理时效、联系方式，不替用户推断他属于哪种情况。",
    "只回答用户问到的：不要主动列出「资料里没写 XX」之类的补充，也不要举例、推测原因或替用户出主意；"
    "用户问的内容资料里确实没有时，才直接说「资料里没写」。",
    "不写「流程很简单」「补充两点」「也就是说」这类没有信息量的话，要点写完用「**总结**」一句收束，不加客套。",
    "多份资料说法不一致时，分别写出各自的说法和出处，不替用户选「以哪份为准」，资料写明了优先级的除外。",
    "用户本人的情况（身份、期限、权限等）只能使用对话里明确给出的信息。",
)


def grounding_block() -> str:
    """给 chat 系统提示词用的「有据可查」段落（规则编号化，便于模型逐条遵守）。"""
    lines = ["## 回答必须与资料一致"]
    lines.extend(f"{index}. {rule}" for index, rule in enumerate(GROUNDING_RULES, start=1))
    lines.append(f"\n不要出现这些词：{'、'.join(f'「{p}」' for p in BANNED_PHRASES)}。")
    return "\n".join(lines)


def format_context(chunks: list[ScoredChunk], *, start_index: int = 1, max_chars_per_chunk: int = 1200) -> str:
    """渲染成 ``[n] 《标题》（版本 · 更新日期）\\n正文`` 的参考资料块（简单版，无面包屑）。"""
    blocks: list[str] = []
    for offset, item in enumerate(chunks):
        chunk = item.chunk
        body = chunk.text.strip()
        if len(body) > max_chars_per_chunk:
            body = body[:max_chars_per_chunk] + "……"
        meta = " · ".join(value for value in (chunk.version, (chunk.updated_at or "")[:10]) if value)
        blocks.append(f"[{start_index + offset}] 《{chunk.title}》{('（' + meta + '）') if meta else ''}\n{body}")
    return "\n\n".join(blocks)


def pack_context(
    numbered: list[tuple[int, ScoredChunk]],
    parents: dict[str, str] | None = None,
    *,
    budget_tokens: int = 3000,
) -> str:
    """父块回填 + token 预算打包（生成阶段的上下文）。

    * 命中的子块属于某个父块时，用**父块正文**（同一父块只出现一次），信息更完整；
    * 按分数从高到低装入，装不下的父块退回子块正文；
    * 预算按 ``tokens / 0.7`` 折算成字符（中文约 0.7 token / 字），超预算就停；
    * 编号沿用调用方给的全局编号（不要在这里重新编号）。

    Args:
        numbered: ``[(编号, 命中), ...]``，编号从 1 连续。
        parents: ``{parent_id: 父块正文}``，来自 ``TextChunker.split_parent_child``。
        budget_tokens: 上下文预算（token）。
    """
    from tsukiyohub.kernel.rag.answer import source_line

    parents = parents or {}
    budget = int(budget_tokens / 0.7)
    used: set[str] = set()
    blocks: list[tuple[int, str]] = []
    for index, item in sorted(numbered, key=lambda pair: pair[1].score, reverse=True):
        chunk = item.chunk
        parent_id = str(chunk.extra.get("parent_id") or "")
        body = chunk.text.strip()
        if parent_id and parent_id in used:
            continue
        parent = parents.get(parent_id, "") if parent_id else ""
        if parent and len(parent) <= budget:
            body = parent.strip()
            used.add(parent_id)
        if len(body) > budget:
            if blocks:
                continue
            body = body[:budget] + "……"
        budget -= len(body)
        blocks.append((index, f"[{index}] {source_line(chunk)}\n{body}"))
        if budget <= 0:
            break
    return "\n\n".join(text for _index, text in sorted(blocks))


def build_citation(item: ScoredChunk, index: int, *, snippet_length: int = 160) -> Citation:
    """单条引用（:class:`Citation`）：``snippet`` 是给人看的摘要，``index`` 必须等于正文编号。"""
    chunk: Chunk = item.chunk
    snippet = chunk.text.strip().replace("\n", " ")
    if len(snippet) > snippet_length:
        snippet = snippet[:snippet_length] + "…"
    return Citation(
        index=index,
        doc_id=chunk.doc_id,
        title=chunk.title,
        uri=chunk.uri,
        snippet=snippet,
        version=chunk.version,
        updated_at=chunk.updated_at,
        score=item.score,
        label=chunk.citation,
        chunk_id=chunk.chunk_id,
    )


def build_citations(chunks: list[ScoredChunk], *, snippet_length: int = 160) -> list[Citation]:
    """按顺序给每个命中编号（1 起）并生成引用列表。"""
    return [build_citation(item, index, snippet_length=snippet_length) for index, item in enumerate(chunks, start=1)]


__all__ = [
    "ANSWER_TEMPLATE", "BANNED_PHRASES", "GROUNDING_RULES", "NO_HIT_ANSWER",
    "build_citation", "build_citations", "format_context", "grounding_block", "pack_context",
]