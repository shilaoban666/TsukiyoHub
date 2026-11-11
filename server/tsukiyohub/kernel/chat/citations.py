"""引用编号与「参考来源」：把模型写的**逐段编号**换成用户看到的**逐文档编号**。

为什么要重编号
--------------
检索为了精度按「段」召回：同一份文档可能命中 3 段，各占一个编号 ``[1] [4] [7]``。
但用户看到的来源列表是按**文档**列的，如果正文写 ``[7]`` 而列表只有 2 条，编号就对不上。
所以收口点做一次映射：正文首次出现的顺序决定文档编号，同一文档的多段合并成一条，
证据（原文）也跟着并到该文档名下，方便逐句核对与前端「点开看原文」。

三条不变量（实现别的链路时不要破坏）：
1. ``hits``（:class:`ToolContext` 里的全局编号）一旦分配不得重排——重排会让多轮工具调用的引用错位；
2. 重编号只在回答收尾做一次，正文里所有 ``[n]`` 必须同时替换，不能只改一半；
3. 没有编号的文档不再列进来源列表（拒答/闲聊下面挂一串无关资料最误导用户）。
"""

from __future__ import annotations

import re
from typing import Any

from tsukiyohub.domain import Citation

#: 全部来源最多列几篇（正文引用了的总是全列，这个数只限制「补足」部分）
MAX_SOURCES = 5
#: 一句话最多挂几篇文档的引用（超过就是堆砌，读者反而找不到重点）
MAX_REFS_PER_SENTENCE = 2
#: 「参考来源」小标题
SOURCES_TITLE = "参考来源："

_REF = re.compile(r"\[(\d{1,3})\]")
_REF_ANY = re.compile(r"\[(\d{1,3})(?:·[^\[\]]{1,16})?\]")
_DUP = re.compile(r"(\[\d{1,3}(?:·[^\[\]]{1,16})?\])(?:\s*\1)+")
_SAME_DOC = re.compile(r"\[(\d{1,3})·第([\d、]+)页\]((?:\s*\[\1·第[\d、]+页\])+)")
_PAGE = re.compile(r"第\s*(\d{1,4})\s*页")
_SENTENCE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]?")
#: 「资料里没有」一类的句子：不挂引用（没有的东西不该有出处）
NEGATIVE = re.compile(
    r"未提及|未包含|未收录|未列出|未出现|未涉及|未指定|未说明|未找到|没有找到|没有提到|没有提及|没有相关|"
    r"没有收录|并未|无法确认|无法读取|无法验证|无法直接回答|无法回答|找不到|查不到|不包含|无该|暂无|缺少|缺失"
)


# ---------------------------------------------------------------------------
# 编号映射
# ---------------------------------------------------------------------------
def doc_label(c: Citation) -> str:
    """文档级标签：``《标题》 · 版本``（不带「第 N 段」——同一文档的几段已经合并成一条）。"""
    parts: list[str] = []
    if c.title:
        parts.append(c.title if c.title.startswith("《") else f"《{c.title}》")
    if c.version and c.version not in parts:
        parts.append(c.version)
    return " · ".join(parts)


def page_of(chunk: Any) -> int | None:
    """切块所在页码：解析 PPt/PDF 时写进 ``extra["breadcrumb"]`` 的「第 N 页」，没有返回 ``None``。"""
    crumb = str((getattr(chunk, "extra", None) or {}).get("breadcrumb") or "")
    m = _PAGE.search(crumb)
    return int(m.group(1)) if m else None


def renumber(
    answer: str,
    citations: list[Citation],
    evidence: dict[int, str],
    pages: dict[int, int | None] | None = None,
) -> tuple[str, list[Citation], dict[int, str]]:
    """逐段编号 → 逐文档编号。

    * ``citations``：每段的引用（``index`` 是段编号）；
    * ``evidence``：段编号 → 该段原文；
    * ``pages``：段编号 → 页码；有页码的写成 ``[文档编号·第N页]``。

    返回 ``(新正文, 每份文档一条引用, 新编号 → 该文档被引用各段的原文)``。
    文档顺序按正文里首次出现；同一处连写的重复编号合并（``[1][1]`` → ``[1]``）。
    """
    order: dict[str, int] = {}
    by_old: dict[int, int] = {}
    docs: list[Citation] = []
    first_seen = [int(n) for n in _REF.findall(answer or "")]
    ranked = sorted(
        citations,
        key=lambda c: first_seen.index(c.index) if c.index in first_seen else len(first_seen) + c.index,
    )
    for c in ranked:
        key = c.doc_id or c.title or f"#{c.index}"
        if key not in order:
            order[key] = len(order) + 1
            docs.append(c.model_copy(update={"index": order[key], "label": doc_label(c)}))
        by_old[c.index] = order[key]

    merged: dict[int, str] = {}
    for old, new in by_old.items():
        text = evidence.get(old, "")
        if not text:
            continue
        merged[new] = f"{merged[new]}\n{text}" if new in merged else text

    def mark(m: re.Match[str]) -> str:
        old = int(m.group(1))
        if old not in by_old:
            return m.group(0)          # 无效编号原样留着，由后续清理步骤处理
        page = (pages or {}).get(old)
        return f"[{by_old[old]}·第{page}页]" if page else f"[{by_old[old]}]"

    return _DUP.sub(r"\1", _REF.sub(mark, answer or "")), docs, merged


def merge_pages(answer: str) -> str:
    """同一篇文档挨着的几处页码合并：``[2·第2页][2·第37页]`` → ``[2·第2、37页]``。"""
    def merge(m: re.Match[str]) -> str:
        pages = [m.group(2), *re.findall(r"·第([\d、]+)页", m.group(3))]
        flat = list(dict.fromkeys(p for group in pages for p in group.split("、")))
        return f"[{m.group(1)}·第{'、'.join(flat)}页]"

    prev = None
    while prev != answer:
        prev, answer = answer, _SAME_DOC.sub(merge, answer)
    # [2][2·第3页] 这类紧邻重复：只留带页码的那一个
    answer = _DUP.sub(r"\1", answer)
    return re.sub(r"\[(\d{1,3})\]((?:\s*\[\1(?:·[^\[\]]{1,16})?\])+)", lambda m: m.group(2).strip(), answer)


def tidy_refs(answer: str, doc_ids: dict[int, str]) -> str:
    """整理正文引用（重编号**之前**做）：

    * 否定句（「资料中未提及…」）去掉引用；
    * 一句话最多引用 :data:`MAX_REFS_PER_SENTENCE` 篇文档（按出现顺序保留）；
    * 「参考来源」之后的版式原样保留，不再改。
    """
    if not answer or not _REF_ANY.search(answer):
        return answer
    out_lines: list[str] = []
    tail = False
    for line in answer.split("\n"):
        tail = tail or line.lstrip().startswith(("**参考来源", "参考来源", "**引用文档", "引用文档"))
        if tail or not _REF_ANY.search(line):
            out_lines.append(line)
            continue
        parts: list[str] = []
        for sent in _SENTENCE.findall(line):
            plain = _REF_ANY.sub("", sent)
            if NEGATIVE.search(plain):
                parts.append(_tidy_spaces(plain))
                continue
            docs: list[str] = []

            def keep(m: re.Match[str], docs: list[str] = docs) -> str:
                doc = doc_ids.get(int(m.group(1)), m.group(1))
                if doc in docs:
                    return m.group(0)
                if len(docs) >= MAX_REFS_PER_SENTENCE:
                    return ""
                docs.append(doc)
                return m.group(0)

            kept = _REF_ANY.sub(keep, sent)
            parts.append(_tidy_spaces(kept) if kept != sent else kept)
        out_lines.append("".join(parts) if parts else line)
    return "\n".join(out_lines)


def _tidy_spaces(text: str) -> str:
    return re.sub(r"\s+([。！？；，,.!?;])", r"\1", re.sub(r"[ \t]{2,}", " ", text))


# ---------------------------------------------------------------------------
# 来源列表渲染
# ---------------------------------------------------------------------------
def unique_sources(citations: list[Citation], limit: int = MAX_SOURCES) -> list[Citation]:
    """同一文档只留一条（按编号顺序），最多 ``limit`` 条；正文引用了的总是全部列上。"""
    limit = max(limit, sum(1 for c in citations if c.cited))
    seen: set[str] = set()
    out: list[Citation] = []
    for c in sorted(citations, key=lambda x: x.index):
        key = c.doc_id or c.title or str(c.index)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:limit]


def source_line(c: Citation, markdown: bool = True) -> str:
    """一条来源（不含编号）：``《标题》 · 版本``，有链接时标题可点。"""
    title = c.title if c.title.startswith("《") else f"《{c.title}》"
    if not c.title:
        title = "《未命名文档》"
    url = c.url or (c.uri if str(c.uri).startswith(("http://", "https://")) else "")
    if url:
        title = f"[{title}]({url})" if markdown else f"{title}（{url}）"
    parts = [title]
    if c.version and c.version != (c.updated_at or "")[:10]:
        parts.append(c.version)
    return " · ".join(p for p in parts if p)


def source_lines(citations: list[Citation], markdown: bool = True, limit: int = MAX_SOURCES) -> list[str]:
    """来源列表的每一行：``1、[《标题》](链接) · 版本``。"""
    return [f"{c.index}、{source_line(c, markdown)}" for c in unique_sources(citations, limit)]


def sources_block(lines: list[str], markdown: bool = True) -> str:
    """回答末尾的来源区（空列表返回空串）。Markdown 下用两个空格换行，避免被渲染成一行。"""
    if not lines:
        return ""
    head = f"**{SOURCES_TITLE}**" if markdown else SOURCES_TITLE
    sep = "  \n" if markdown else "\n"
    return sep.join([head, *lines])


def with_citations(answer: str, citations: list[Citation], *, markdown: bool = True, limit: int = 3) -> str:
    """回答正文 + 末尾「参考来源」。给不支持富文本的渠道与 OpenAI 兼容接口用。"""
    block = sources_block(source_lines(citations, markdown, limit), markdown)
    if not block:
        return answer or ""
    return f"{answer or ''}\n\n{block}"


def has_refs(answer: str) -> bool:
    """正文（来源列表之前）还有没有引用编号。"""
    body = re.split(r"\n\s*(?:\*\*)?(?:参考来源|引用文档)", answer or "", maxsplit=1)[0]
    return bool(_REF_ANY.search(body))


def related(cited: list[Citation], ranked: list[Citation], limit: int = MAX_SOURCES) -> list[Citation]:
    """把来源补足到 ``limit`` 篇：按检索排名取没被引用的文档（``cited=False``），编号接在后面。"""
    seen = {key for c in cited for key in (c.doc_id, c.title) if key}
    out: list[Citation] = []
    for c in ranked:
        if len(cited) + len(out) >= limit:
            break
        keys = {key for key in (c.doc_id, c.title) if key}
        if keys and not keys & seen:
            seen |= keys
            out.append(c.model_copy(update={"index": len(cited) + len(out) + 1,
                                            "label": doc_label(c), "cited": False}))
    return out


__all__ = [
    "MAX_REFS_PER_SENTENCE", "MAX_SOURCES", "NEGATIVE", "SOURCES_TITLE",
    "doc_label", "has_refs", "merge_pages", "page_of", "related", "renumber", "source_line",
    "source_lines", "sources_block", "tidy_refs", "unique_sources", "with_citations",
]