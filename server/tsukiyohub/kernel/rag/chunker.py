"""文本切块：递归按段落 / 句子边界切分，并把标题面包屑织进正文。

为什么自己写 splitter 而不是依赖 LangChain
------------------------------------------
``RecursiveCharacterTextSplitter`` 的算法本身只有几十行（分隔符优先级 + 合并 + 重叠窗口），
自己实现能拿到两个好处：内核继续「零外部依赖可跑」，并且切块行为完全可测。

为什么要织入面包屑（这是本模块最关键的一点）
--------------------------------------------
Markdown 标题分割器只把标题写进 metadata，**不拼进正文**，而 BM25 只看正文——
于是「4.2 审批权限」这种只在标题里出现的问法永远召回不到。所以切块时把标题路径
前置成 ``（4. 请假管理 > 4.2 审批权限）正文……``，让标题也能被关键词命中。

切块 ID 稳定性
--------------
``chunk_id = f"{doc_id}#{ordinal}"``，ordinal 从 0 连续递增。同一份文档只要内容不变，
切块与 ID 就不变，重复入库会原地覆盖（幂等），也便于按 ID 删除。
"""

from __future__ import annotations

import re

from tsukiyohub.config import Settings
from tsukiyohub.domain import Chunk, Document
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 切块算法版本：算法或默认参数一变就改它（写进索引签名，便于判断是否需要重建）
CHUNKER_VERSION = "3"
#: 小于这个长度的碎片直接丢弃（「第 3 页」「附则」这类）
MIN_CHUNK_CHARS = 10
#: parent_child 模式：父块上限 1600 字，子块 420 / 重叠 60
PARENT_MAX_CHARS = 1600
CHILD_SIZE = 420
CHILD_OVERLAP = 60
#: 表名候选：紧挨表格上一行、不超过这么长的一句
CAPTION_MAX_CHARS = 60

#: 分隔符优先级：段落 → 换行 → 中文句末 → 子句 → 空格 → 硬切
#: 越靠前越优先作为切分点，保证尽量在自然语义边界断开。
SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", "；", "！", "？", "，", " ", "")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FAQ_Q = re.compile(r"^\s*(?:Q\s*[:：]|问\s*[:：]|问题\s*[:：]|\d+[.、]\s*.+[?？]\s*$|#{1,6}\s+.+[?？]\s*$)", re.I)
_FAQ_A = re.compile(r"^\s*(?:A\s*[:：]|答\s*[:：]|回答\s*[:：])", re.I)


# ---------------------------------------------------------------------------
# 递归切分器（零依赖，行为等价于 LangChain 的 RecursiveCharacterTextSplitter）
# ---------------------------------------------------------------------------
class RecursiveSplitter:
    """按分隔符优先级递归切分，并把相邻小块合并到 ``size``，保留 ``overlap`` 重叠。

    语义与 ``langchain_text_splitters.RecursiveCharacterTextSplitter`` 一致：
    ``keep_separator=True``（分隔符留在前一块末尾，保留句末标点）、
    ``strip_whitespace=True``（每块首尾去空白）、``length_function=len``。
    """

    def __init__(self, size: int, overlap: int, separators: tuple[str, ...] = SEPARATORS) -> None:
        self.size = max(1, int(size))
        self.overlap = max(0, min(int(overlap), self.size // 2))
        self.separators = tuple(separators)

    def split_text(self, text: str) -> list[str]:
        """切分文本，返回去掉首尾空白的块列表。"""
        if not text or not text.strip():
            return []
        pieces = self._split(text, self.separators)
        return [piece.strip() for piece in pieces if piece.strip()]

    # ------------------------------------------------------------------ 内部
    def _split(self, text: str, separators: tuple[str, ...]) -> list[str]:
        if len(text) <= self.size:
            return [text]
        for index, separator in enumerate(separators):
            if separator == "":
                return self._hard_split(text)
            if separator in text:
                parts = self._split_keep(text, separator)
                splits: list[str] = []
                for part in parts:
                    if len(part) > self.size:
                        splits.extend(self._split(part, separators[index + 1:]))
                    else:
                        splits.append(part)
                return self._merge(splits)
        return [text]

    def _merge(self, splits: list[str]) -> list[str]:
        """把小块合并到 size 以内；超长时先兑现当前块，再从头部丢弃到只剩 overlap。"""
        docs: list[str] = []
        current: list[str] = []
        total = 0
        for piece in splits:
            length = len(piece)
            if total + length > self.size and current:
                docs.append("".join(current))
                while total > self.overlap or (total + length > self.size and total > 0):
                    total -= len(current.pop(0))
                    if not current:
                        break
            current.append(piece)
            total += length
            if total > self.size:  # 单个片段本身就超过 size（超长段 / 硬切结果）：单独成块
                docs.append("".join(current))
                current, total = [], 0
        rest = "".join(current)
        if rest.strip():
            docs.append(rest)
        return docs

    @staticmethod
    def _split_keep(text: str, separator: str) -> list[str]:
        """按分隔符切分，并把它保留在**前一段**末尾（保住句号 / 问号）。"""
        parts = text.split(separator)
        out = [part + separator for part in parts[:-1]]
        if parts[-1]:
            out.append(parts[-1])
        return [part for part in out if part]

    def _hard_split(self, text: str) -> list[str]:
        step = max(1, self.size - self.overlap)
        return [text[start:start + self.size] for start in range(0, len(text), step)]


# ---------------------------------------------------------------------------
# 表格保护：表格不在行中间断开
# ---------------------------------------------------------------------------
def _is_table_line(line: str) -> bool:
    return " | " in line or line.strip().startswith("|")


def split_tables(text: str) -> list[tuple[str, str]]:
    """把正文拆成 ``[(kind, text)]``：kind = ``prose`` / ``table``。

    连续 ≥ 2 行含「 | 」视为表格（Markdown 表 / 制表符对齐后的表）。
    """
    blocks: list[tuple[str, str]] = []
    prose: list[str] = []
    table: list[str] = []

    def flush_prose() -> None:
        if prose and "\n".join(prose).strip():
            blocks.append(("prose", "\n".join(prose).strip()))
        prose.clear()

    def flush_table() -> None:
        if len(table) >= 2:
            flush_prose()
            blocks.append(("table", "\n".join(table)))
        else:
            prose.extend(table)
        table.clear()

    for line in text.split("\n"):
        if _is_table_line(line):
            table.append(line)
            continue
        if table:
            flush_table()
        prose.append(line)
    flush_table()
    flush_prose()
    return blocks


def _table_head(lines: list[str]) -> tuple[list[str], list[str]]:
    """（表头行，数据行）：Markdown 表的表头含分隔行 ``| --- |``。"""
    count = 2 if len(lines) > 1 and _MD_RULE.match(lines[1].strip()) else 1
    return lines[:count], lines[count:]


def split_table(table: str, limit: int, lead: str = "") -> list[str]:
    """表格超长时按行组切分，每块重复表名（``lead``）与表头。"""
    header, rows = _table_head(table.split("\n"))
    head = "\n".join(([lead] if lead else []) + header)
    if len(table) + len(lead) + 1 <= limit or not rows:
        return [(lead + "\n" if lead else "") + table]
    out: list[str] = []
    group: list[str] = []
    size = len(head)
    for row in rows:
        if group and size + len(row) + 1 > limit:
            out.append(head + "\n" + "\n".join(group))
            group, size = [], len(head)
        group.append(row)
        size += len(row) + 1
    if group:
        out.append(head + "\n" + "\n".join(group))
    return out


def table_summary(table: str, caption: str) -> str:
    """大表的「表摘要」块：这张表讲什么、有哪些列、多少行。

    问「有哪些列 / 标准表在哪」时，摘要块能命中整张表的主题。
    """
    header, rows = _table_head(table.split("\n"))
    cols = [col.strip() for col in re.split(r"(?<!\\)\|", header[0].strip().strip("|")) if col.strip()]
    return f"表格摘要：{caption or '本节表格'}，共 {len(rows)} 行，列：{'、'.join(cols[:30])}"


def _caption(prose: str) -> str:
    line = prose.rstrip().rsplit("\n", 1)[-1].strip()
    return line if 0 < len(line) <= CAPTION_MAX_CHARS and not _HEADING_RE.match(line) else ""


def split_blocks(text: str, limit: int, split_prose) -> list[str]:
    """正文按 ``split_prose`` 切、表格按行组切。

    表格上一行的短句作为表名，随表头重复进每一块；拆成多块的大表另加一个表摘要块；
    表名从正文块里移走，避免同一句出现两次。
    """
    blocks = split_tables(text)
    out: list[str] = []
    for index, (kind, block) in enumerate(blocks):
        if kind == "prose":
            if index + 1 < len(blocks) and _caption(block):
                block = block.rstrip().rsplit("\n", 1)[0] if "\n" in block.strip() else ""
            if block.strip():
                out.extend(split_prose(block))
            continue
        caption = _caption(blocks[index - 1][1]) if index and blocks[index - 1][0] == "prose" else ""
        parts = split_table(block, limit, caption)
        if len(parts) > 1:
            out.append(table_summary(block, caption))
        out.extend(parts)
    return out


# ---------------------------------------------------------------------------
# 标题层级 → 面包屑
# ---------------------------------------------------------------------------
class _Section:
    """一个「标题 + 该标题下的正文」单元。"""

    __slots__ = ("path", "text", "start_line")

    def __init__(self, path: list[str], text: str, start_line: int) -> None:
        self.path = path
        self.text = text
        self.start_line = start_line

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.path)


def split_by_headings(text: str) -> list[_Section]:
    """按 Markdown 标题（代码围栏外的 ``^#{1,6} ``）把文本切成若干单元。

    每个单元的 ``path`` 是该标题的完整层级路径（如 ``['4. 请假管理', '4.2 审批权限']``），
    用于在切块时织入面包屑。表格、列表、多级标题都不影响此逻辑。
    """
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    current_path: list[str] = []
    current_start = 0
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(_Section(list(current_path), body, current_start))
        buffer.clear()

    for line_number, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(stripped)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            stack = [(lv, hd) for lv, hd in stack if lv < level]
            stack.append((level, title))
            current_path = [hd for _, hd in stack]
            current_start = line_number
            continue
        buffer.append(line)

    flush()
    return sections


def parent_blocks(text: str) -> list[str]:
    """把一节正文切成 ≤ ``PARENT_MAX_CHARS`` 的父块（按空行累积）。"""
    if len(text) <= PARENT_MAX_CHARS:
        return [text]
    blocks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) + 2 > PARENT_MAX_CHARS:
            blocks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        blocks.append(current)
    return blocks


def _faq_pairs(text: str) -> list[tuple[str, str]]:
    """识别「Q: / 问： / 1. xxx？」+ 答案段落；识别不出时整段作为一条。"""
    pairs: list[tuple[str, str]] = []
    question, answer = "", []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _FAQ_Q.match(stripped) and not _FAQ_A.match(stripped):
            if question:
                pairs.append((question, "\n".join(answer)))
            question = re.sub(r"^(?:#{1,6}\s+|Q\s*[:：]|问题?\s*[:：])\s*", "", stripped, flags=re.I)
            answer = []
        else:
            answer.append(_FAQ_A.sub("", stripped).strip())
    if question:
        pairs.append((question, "\n".join(answer)))
    return pairs or [("", text)]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
class TextChunker:
    """把 :class:`Document` 切成带引用元数据的 :class:`Chunk`。

    ``chunk_id`` 形如 ``<doc_id>#<ordinal>``：ordinal 从 0 连续递增，同一文档重复入库时
    ID 稳定（幂等覆盖），也便于按 ID 删除。
    """

    def __init__(self, settings: Settings) -> None:
        self.chunk_size = max(100, int(settings.chunk_size or 700))
        self.chunk_overlap = max(0, min(int(settings.chunk_overlap or 0), self.chunk_size // 2))
        self.splitter = RecursiveSplitter(self.chunk_size, self.chunk_overlap)

    # ------------------------------------------------------------------ 公开
    def split(self, document: Document, *, mode: str = "article") -> list[Chunk]:
        """按配置切分文档（默认 ``article`` 模式），返回的正文已包含标题面包屑。

        Args:
            document: 解析后的文档。
            mode: ``article``（按标题分段 + 递归切块）或 ``parent_child``
                （父块 ≤1600 字、子块 420/60，子块 ``extra['parent_id']`` 指回父块）。
                父块正文不在返回值里——需要回填时用 :meth:`split_parent_child`。
        """
        chunks, _parents = self._build(document, mode=mode)
        return chunks

    def split_parent_child(self, document: Document) -> tuple[list[Chunk], dict[str, str]]:
        """``parent_child`` 模式：返回 ``(子块, {parent_id: 父块正文})``，供父块回填使用。"""
        return self._build(document, mode="parent_child")

    # ---------------------------------------------------------------- 内部
    def _build(self, document: Document, *, mode: str) -> tuple[list[Chunk], dict[str, str]]:
        text = (document.content or "").strip()
        if not text:
            return [], {}
        meta = document.meta
        parents: dict[str, str] = {}
        pieces: list[tuple[str, str, str]] = []  # (正文, 面包屑, parent_id)

        if mode == "faq":
            for question, answer in _faq_pairs(text):
                pieces.append((f"{question}\n{answer}".strip(), question, ""))
        elif mode == "fixed":
            pieces.extend((piece, "", "") for piece in split_blocks(text, self.chunk_size, self.splitter.split_text))
        else:
            child = RecursiveSplitter(CHILD_SIZE, CHILD_OVERLAP)
            sections = split_by_headings(text) or [_Section([], text, 0)]
            for section in sections:
                breadcrumb = section.breadcrumb
                if mode == "parent_child":
                    for body in parent_blocks(section.text):
                        parent_id = f"{meta.doc_id}#p{len(parents)}"
                        parents[parent_id] = f"（{breadcrumb}）{body}" if breadcrumb else body
                        pieces.extend(
                            (piece, breadcrumb, parent_id)
                            for piece in split_blocks(body, CHILD_SIZE, child.split_text)
                        )
                else:
                    pieces.extend(
                        (piece, breadcrumb, "")
                        for piece in split_blocks(section.text, self.chunk_size, self.splitter.split_text)
                    )

        chunks: list[Chunk] = []
        for body, breadcrumb, parent_id in pieces:
            clean = body.strip()
            if len(clean) < MIN_CHUNK_CHARS:
                continue
            ordinal = len(chunks)
            extra: dict[str, object] = {**meta.extra, "tags": list(meta.tags), "breadcrumb": breadcrumb, "active": True}
            if parent_id:
                extra["parent_id"] = parent_id
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.doc_id}#{ordinal}",
                    doc_id=meta.doc_id,
                    # 面包屑织进正文：BM25 只认正文，标题里的词也要能被检索到
                    text=f"（{breadcrumb}）{clean}" if breadcrumb and mode != "faq" else clean,
                    ordinal=ordinal,
                    title=meta.title,
                    uri=meta.uri,
                    source_type=meta.source_type,
                    doc_type=meta.doc_type,
                    version=meta.version,
                    updated_at=meta.updated_at,
                    extra=extra,
                )
            )
        logger.debug("文档 %s 切分为 %d 块（mode=%s size=%d）", meta.doc_id, len(chunks), mode, self.chunk_size)
        return chunks, parents


__all__ = [
    "CHILD_OVERLAP", "CHILD_SIZE", "CHUNKER_VERSION", "MIN_CHUNK_CHARS", "PARENT_MAX_CHARS",
    "SEPARATORS", "RecursiveSplitter", "TextChunker", "parent_blocks", "split_blocks",
    "split_by_headings", "split_table", "split_tables", "table_summary",
]