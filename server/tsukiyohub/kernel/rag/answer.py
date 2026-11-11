"""把检索命中渲染成「给人看的答案素材」。

三件事，都在这一层做，chat 内核不需要重复实现：

1. :func:`source_line`：一行来源标签（``《标题》v2 · 面包屑``），引用列表与证据块共用；
2. :func:`build_evidence_block`：**已编号**片段 → 证据文本。编号由调用方（``knowledge_search``
   工具 / ``ToolContext.hits``）分配，这里只按列表顺序渲染 ``[n]`` 前缀——
   这样「片段编号 = 正文 [n]」的不变式由**一处**保证；
3. :func:`extractive_answer`：模型全部不可用时的兜底——直接给最相关的原文片段，
   用户至少拿到了出处，比一句「服务不可用」有用得多。

另附 :func:`progress_found`：SSE 的 ``progress`` 事件文案。
"""

from __future__ import annotations

from tsukiyohub.domain import Chunk, ScoredChunk

#: 证据块里单段正文的默认上限（再长只会挤占模型的上下文预算）
EVIDENCE_CHARS = 1200


def source_line(chunk: Chunk) -> str:
    """来源行：``《文档标题》v2 · 4. 请假管理 > 4.2 审批权限``。

    面包屑优先用 ``extra['breadcrumb']``（切块时写入）；没有就退回 ``uri``，
    再没有就只剩标题——保证任何来源都能渲染出可读的一行。
    """
    parts = [f"《{chunk.title}》" + (f"{chunk.version} 版" if chunk.version else "")]
    breadcrumb = str(chunk.extra.get("breadcrumb") or "").replace(" / ", " > ")
    if breadcrumb:
        parts.append(breadcrumb)
    elif chunk.uri:
        parts.append(chunk.uri)
    return " · ".join(parts)


def build_evidence_block(
    chunks: list[ScoredChunk],
    *,
    start_index: int = 1,
    max_chars_per_chunk: int = EVIDENCE_CHARS,
) -> str:
    """把命中渲染成带编号的证据块。

    Args:
        chunks: 已排好序、且调用方已经（或即将）分配连续编号的命中。
        start_index: 第一段的编号，默认 1。
        max_chars_per_chunk: 单段正文上限，超出截断并加省略号。

    Returns:
        形如::

            [1] 《xx手册》v2 · 4. 请假管理 > 4.2 审批权限
            正文……

            [2] 《yy规范》
            正文……

    编号从 ``start_index`` 连续递增；调用方必须用**同一个顺序**编号写进 ``ctx.hits``，
    否则正文里的 ``[n]`` 会指错来源。
    """
    blocks: list[str] = []
    for offset, item in enumerate(chunks):
        chunk = item.chunk
        body = chunk.text.strip()
        if max_chars_per_chunk > 0 and len(body) > max_chars_per_chunk:
            body = body[:max_chars_per_chunk] + "……"
        blocks.append(f"[{start_index + offset}] {source_line(chunk)}\n{body}")
    return "\n\n".join(blocks)


def extractive_answer(chunks: list[ScoredChunk], *, max_chunks: int = 3, max_chars: int = 700) -> str:
    """模型不可用时的兜底答案：直接给最相关的原文片段（带来源）。

    Args:
        chunks: 检索命中（按分数降序）。
        max_chunks: 最多摘几段。
        max_chars: 单段正文上限。

    Returns:
        纯文本答案；没有命中时返回「知识库里没有相关内容」的标准话术。
    """
    if not chunks:
        from tsukiyohub.kernel.rag.prompt import NO_HIT_ANSWER

        return NO_HIT_ANSWER
    parts = ["当前无法调用生成模型，以下是从知识库检索到的原文片段，供你直接查阅：\n"]
    for index, item in enumerate(chunks[:max_chunks], start=1):
        body = item.chunk.text.strip().replace("\n", " ")
        if len(body) > max_chars:
            body = body[:max_chars] + "……"
        parts.append(f"[{index}] {source_line(item.chunk)}\n{body}\n")
    return "\n".join(parts)


def progress_found(titles: list[str]) -> str:
    """进度文案：``正在查找：《差旅规范》《请假办法》等 3 份资料``（去重、最多列两篇）。"""
    names = list(dict.fromkeys(title for title in titles if title))
    if not names:
        return ""
    head = "".join(f"《{name[:20]}》" for name in names[:2])
    return f"正在查找：{head}" + (f"等 {len(names)} 份资料" if len(names) > 2 else "")


__all__ = ["EVIDENCE_CHARS", "build_evidence_block", "extractive_answer", "progress_found", "source_line"]