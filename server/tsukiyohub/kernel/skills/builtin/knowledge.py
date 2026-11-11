"""内置技能：``knowledge_search`` —— 查内部知识库。

为什么它是「最该被先想到」的技能
--------------------------------
模型不知道接入方有哪些资料，也不知道资料里写了什么；直接凭常识回答公司/项目规定就是幻觉来源。
所以这个技能把检索结果**带编号**地交给模型，并要求模型用 ``[n]`` 标注出处——编号与
:class:`~tsukiyohub.kernel.skills.base.ToolContext` 的 ``hits`` 对齐，聊天内核据此生成
给用户看的「参考来源」列表。

编号连续性的约定
----------------
``ctx.next_index`` 是全局游标：同一轮里第二次、第三次检索接着往后编号，绝不重排。
``prefetch`` 与 ``call`` 必须写同样的 ``ctx`` 字段（``hits`` / ``retrieved`` / ``kb_called`` /
``timings``），这样「先查再答」链路与工具调用链路的引用行为完全一致。

证据文本的拼装
--------------
来源行复用 rag 内核的 ``source_line``（拿不到时用等价兜底），保证与「参考来源」列表同一套文案；
但证据块本身按**全局编号**自己拼，不用 ``build_evidence_block``——那个函数固定从 1 开始编号，
多轮检索时会与 ``ctx.next_index`` 错位。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from tsukiyohub.domain import ScoredChunk
from tsukiyohub.kernel.skills.base import BaseSkill, ToolContext
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.vector import SearchFilters

if TYPE_CHECKING:  # 只做类型提示：rag 内核未就绪时本模块仍可导入
    from tsukiyohub.config import Settings
    from tsukiyohub.kernel.rag.store import KnowledgeStore

logger = get_logger(__name__)

#: 每段证据最多带多少字（控制提示词体积；检索片段本身通常 500~900 字）
MAX_SNIPPET = 900
#: 一次检索最多返回多少片段（模型传 top_k 时的硬上限）
MAX_TOP_K = 20


def source_line(chunk: Any) -> str:
    """来源行：优先用 rag 内核的实现，保证与引用列表文案一致。"""
    try:
        from tsukiyohub.kernel.rag.answer import source_line as rag_source_line

        line = str(rag_source_line(chunk)).strip()
        if line:
            return line
    except Exception:  # noqa: BLE001 - rag 未就绪 / 实现差异时退回本地文案
        pass
    parts = [f"《{chunk.title}》" if getattr(chunk, "title", "") else "《未命名文档》"]
    version = getattr(chunk, "version", "") or ""
    if version:
        parts.append(version)
    uri = getattr(chunk, "uri", "") or ""
    if uri:
        parts.append(uri)
    updated = (getattr(chunk, "updated_at", "") or "")[:10]
    if updated:
        parts.append(f"更新：{updated}")
    return " · ".join(parts)


def format_evidence(numbered: list[tuple[int, ScoredChunk]]) -> str:
    """按全局编号拼证据块：``[n] 来源行`` + 片段正文。"""
    blocks: list[str] = []
    for index, item in numbered:
        chunk = item.chunk
        text = (chunk.text or "").strip()
        if len(text) > MAX_SNIPPET:
            text = text[:MAX_SNIPPET] + "…"
        blocks.append(f"[{index}] {source_line(chunk)}\n{text}")
    return "\n\n".join(blocks)


class KnowledgeTool(BaseSkill):
    """``knowledge_search(query, top_k?)``：检索知识库并返回带编号的证据。"""

    name = "knowledge_search"
    title = "知识库检索"
    kind = "tool"
    source = "builtin"
    #: 知识库是内核的核心能力：即使接入方把开关写成空列表也应保持可用（container 里另有兜底）
    default_enabled = True
    description = (
        "检索内部知识库（文档、规范、手册、常见问答等）。"
        "只要问题可能涉及内部规定、流程、产品说明、项目资料，就必须先调用本工具再回答；"
        "不要凭常识编造内部信息。"
    )
    rules = (
        "- 涉及内部资料的问题先调用 knowledge_search，回答时在对应句末用 [1]、[2] 标注片段编号，"
        "编号只能来自工具返回的内容；工具说没找到时如实说明，不要编造。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索用的关键词或完整问题，使用用户的语言",
            },
            "top_k": {
                "type": "integer",
                "description": f"返回片段数（1~{MAX_TOP_K}，默认按服务端配置）",
            },
        },
        "required": ["query"],
    }

    def __init__(self, settings: Settings, store: KnowledgeStore) -> None:
        self.settings = settings
        self.store = store

    # ------------------------------------------------------------------ 过滤
    def filters(self, ctx: ToolContext) -> SearchFilters:
        """检索范围：请求里限定的文档类型（``doc_types``）或接入方直接注入的过滤器。

        没有可见范围概念的内核只做「类型过滤」；接入方若要按租户/项目隔离，
        可以往 ``ctx.cfg["filters"]`` 塞一个 :class:`SearchFilters`，本方法原样透传。
        """
        injected = (ctx.cfg or {}).get("filters")
        if isinstance(injected, SearchFilters):
            return injected
        raw = ctx.extra.get("doc_types") or (ctx.cfg or {}).get("doc_types") or []
        if isinstance(raw, str):
            raw = [raw]
        doc_types = [str(item).strip() for item in raw if str(item).strip()]
        return SearchFilters(doc_types=doc_types)

    def _top_k(self, args: dict[str, Any]) -> int | None:
        raw = args.get("top_k")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return max(1, min(value, MAX_TOP_K))

    # ------------------------------------------------------------------ 检索
    def _retrieve(self, query: str, ctx: ToolContext, top_k: int | None = None) -> list[ScoredChunk]:
        started = time.perf_counter()
        ctx.kb_called = True
        try:
            filters = self.filters(ctx)
            kwargs: dict[str, Any] = {}
            if top_k:
                kwargs["top_k"] = top_k
            if not filters.is_empty():
                kwargs["filters"] = filters
            chunks = list(self.store.retrieve(query, **kwargs))
        finally:
            ctx.add_time("retrieve", (time.perf_counter() - started) * 1000)
        ctx.retrieved.extend(
            {
                "query": query,
                "title": item.chunk.title,
                "score": round(float(item.score), 4),
                "vector_score": item.vector_score,
                "bm25_score": item.bm25_score,
                "rerank_score": item.rerank_score,
            }
            for item in chunks
        )
        return chunks

    def _grounded(self, chunks: list[ScoredChunk]) -> bool:
        """置信度闸门：低于阈值视为「知识库里没有」。rag 未就绪时不拦。"""
        if not chunks:
            return False
        try:
            from tsukiyohub.kernel.rag.grounding import evaluate_grounding

            threshold = float(getattr(self.settings, "grounding_min_score", 0.0) or 0.0)
            return bool(evaluate_grounding(chunks, threshold).grounded)
        except Exception:  # noqa: BLE001 - 判定实现缺失不该让检索失败
            return True

    def _number(self, chunks: list[ScoredChunk], ctx: ToolContext) -> list[tuple[int, ScoredChunk]]:
        start = ctx.next_index
        numbered = [(start + offset, item) for offset, item in enumerate(chunks)]
        ctx.next_index = start + len(numbered)
        for index, item in numbered:
            ctx.hits[index] = item
        return numbered

    def search(self, query: str, ctx: ToolContext, top_k: int | None = None) -> tuple[list[tuple[int, ScoredChunk]], str]:
        """检索 + 闸门 + 编号；返回（带编号命中，给模型的证据文本）。"""
        try:
            chunks = self._retrieve(query, ctx, top_k)
        except Exception as exc:  # noqa: BLE001 - 检索挂了也要给出可读提示，不中断问答
            logger.error("知识库检索失败（%s）：%s", query, exc)
            return [], f"知识库检索暂时不可用，没有找到与「{query}」相关的内容。请如实说明暂时查不到。"
        if not chunks:
            return [], f"知识库中没有找到与「{query}」相关的内容。"
        if not self._grounded(chunks):
            return [], f"知识库中没有找到与「{query}」相关的内容（检索结果相关度不足）。"
        numbered = self._number(chunks, ctx)
        return numbered, f"以下是与「{query}」相关的知识库片段，回答时用 [编号] 标注出处：\n\n" + format_evidence(numbered)

    # ------------------------------------------------------------------ 入口
    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip() or (ctx.question or "").strip()
        numbered, content = self.search(query, ctx, self._top_k(args))
        ctx.records.append({"tool": self.name, "args": {"query": query[:120]}, "hits": len(numbered)})
        return content

    def prefetch(self, ctx: ToolContext) -> str | None:
        """模型不支持工具调用时的「先查再答」：把检索结果直接放进提示词。"""
        numbered, content = self.search(ctx.question, ctx)
        ctx.records.append({"tool": self.name, "args": {"query": (ctx.question or "")[:120]}, "prefetch": True,
                            "hits": len(numbered)})
        if not numbered:
            return None
        return "## 参考资料\n" + format_evidence(numbered)


__all__ = ["KnowledgeTool", "MAX_SNIPPET", "MAX_TOP_K", "format_evidence", "source_line"]