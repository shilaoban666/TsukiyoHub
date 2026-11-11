"""对话 Agent：模型主导 + 工具循环 + 引用编号 + 三级降级。

一次问答的骨架
--------------
::

    system(人设 + 技能规则 + 表达规范 + 用户/记忆)
      + 历史 + 当前问题
      → 规则判定「要查知识库」？先 prefetch 一次（省掉一轮模型决策）
      → 模型（带工具）── 要调技能 → 回填结果 → 再来一轮（≤ settings.max_tool_rounds）
      → 引用挑选 → 逐段编号转逐文档编号 → 逐句核对 → grounding 评分 → AgentResult

为什么这样切
------------
* **模型主导、规则兜底**：要不要查库由规则快路径先判（0 次调用），拿不准才交给模型，
  既省一轮延迟又不漏查；
* **编号只在收尾分配**：:class:`ToolContext.hits` 是全局编号总线，多轮工具调用期间只追加不改写，
  收尾时一次性 ``renumber`` 成用户看到的逐文档编号——这样正文 ``[n]`` 与来源列表永远对得上；
* **降级不抛异常**：主模型失败 → 备用模型 → 直接给检索原文摘录；模型不支持工具 → 先查再答
  （``plain`` 模式）。调用方（HTTP / IM / CLI）永远拿到一个能展示的 ``AgentResult``；
* **不 import adapters**：内核只依赖 ``ports.llm``，模型异常通过类名/文本特征识别
  （``adapters.llm_openai.LLMToolUnsupported`` 带 ``tool_unsupported=True`` 属性），
  避免 kernel → adapters 的反向依赖。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import Citation, ScoredChunk
from tsukiyohub.kernel.chat import citations as citations_mod
from tsukiyohub.kernel.chat import prompt as prompt_mod
from tsukiyohub.kernel.chat import router_rules
from tsukiyohub.kernel.chat.answer_style import expression_rules, grounding_rules, guess_shape
from tsukiyohub.kernel.chat.safety import MaskSession, build_mask
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.llm import LLMClient, LLMReply, LLMRouter
from tsukiyohub.ports.memory import KVStore

logger = get_logger(__name__)

#: 内核事件回调：``("delta", 当前全文)`` / ``("status", "thinking"|"searching")`` /
#: ``("progress", 文案)`` / ``("tool", {"name": ..., "ok": ...})``。
#: ``delta`` 的 payload 是**累积全文**（不是增量），前端整段替换即可，天然幂等。
EventSink = Callable[[str, Any], None]

#: 知识库技能名（技能内核的约定名，改变时要同步 skills 侧）
KNOWLEDGE_TOOL = "knowledge_search"

#: 送逐句核对的资料段数上限（多了会把核对自己的上下文撑爆）
CHECK_EVIDENCE = 10

_REF_RE = re.compile(r"\[(\d{1,3})\]")

#: 「模型不支持工具调用」的异常特征（400 / bad request + tool 字样）
_TOOL_HINTS = ("tool", "function", "tool_choice", "tools")

#: 模型不支持工具时的「先查再答」规则（拼进系统提示词）
PLAIN_RULES = """## 参考资料使用规则
下面「参考资料」是从知识库检索到的片段。问题与知识库内容相关时，只依据参考资料回答，
并在句末用 [编号] 标注出处；参考资料里没有答案时如实说明，不要编造。
与知识库无关的问题正常回答，不要标注编号。"""


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------
@dataclass
class UsageRecord:
    """一次模型调用的用量（落库 / 成本统计用）。"""

    model: str = ""
    provider: str = ""
    call_type: str = "chat"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    success: bool = True
    error: str | None = None


@dataclass
class AgentResult:
    """一次问答的完整结果（聊天内核的对外契约，字段顺序即契约顺序）。"""

    answer: str
    citations: list[Citation] = field(default_factory=list)
    kb_called: bool = False
    hit: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usages: list[UsageRecord] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    degraded: bool = False
    degraded_reason: str | None = None
    latency_ms: int = 0
    mode: str = "agent"                      # agent / plain / degraded / clarify / refuse / agentic
    route: str = ""                          # direct / knowledge / clarify / refuse / fallback
    llm_ms: int = 0
    retrieve_ms: int = 0
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    #: 被引用片段的原文（编号 → 正文）；不返回给前端，用于核对与测评
    evidence: dict[int, str] = field(default_factory=dict)
    #: 本轮交给模型的全部片段（带来源行），用于「忠实度」对照
    context: dict[int, str] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    grounding_score: float | None = None
    # ---- 以下为契约之外的补充（都有默认值，按关键字构造不受影响）----
    grounded: bool = False                   # 是否「有据可答」（grounding 判定，比 hit 严格）
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def prompt_tokens(self) -> int:
        return sum(u.prompt_tokens for u in self.usages)

    @property
    def completion_tokens(self) -> int:
        return sum(u.completion_tokens for u in self.usages)

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usages)

    def usage_dict(self) -> dict[str, int]:
        """ChatResponse.usage 的形状（input/output/total 三键，兼容 OpenAI 命名）。"""
        return {
            "input_tokens": self.prompt_tokens,
            "output_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _ToolUnsupported(RuntimeError):
    """内部信号：模型不支持工具调用 → 同一模型改走 ``plain`` 模式。"""


# ---------------------------------------------------------------------------
# 技能上下文（懒加载：skills 内核可能尚未安装/未实现，此时用等价的最小实现）
# ---------------------------------------------------------------------------
@dataclass
class _FallbackToolContext:
    """与 ``kernel.skills.base.ToolContext`` 字段一致的最小实现（仅作兜底）。"""

    question: str
    user_ctx: dict[str, Any] = field(default_factory=dict)
    cfg: dict[str, Any] = field(default_factory=dict)
    pref: dict[str, Any] = field(default_factory=dict)
    hits: dict[int, ScoredChunk] = field(default_factory=dict)
    next_index: int = 1
    records: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    kb_called: bool = False
    timings: dict[str, int] = field(default_factory=dict)
    direct: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add_time(self, key: str, ms: float) -> None:
        self.timings[key] = self.timings.get(key, 0) + int(ms)


def _tool_context_cls() -> type:
    """技能上下文的真实实现；技能内核不可用时退回最小实现。"""
    try:
        from tsukiyohub.kernel.skills.base import ToolContext

        return ToolContext
    except Exception:  # noqa: BLE001 - 技能内核可能还没写完 / 被裁剪
        return _FallbackToolContext


def _new_tool_context(
    question: str, user_ctx: dict[str, Any], cfg: dict[str, Any], pref: dict[str, Any],
    history: list[dict[str, Any]],
) -> Any:
    cls = _tool_context_cls()
    kwargs = {"question": question, "user_ctx": user_ctx, "cfg": cfg, "pref": pref, "history": history}
    try:
        return cls(**kwargs)
    except TypeError:
        # 真实 ToolContext 的实现可能与契约有细微差异：必需字段先建，其余尽力赋值
        ctx = cls(question=question, user_ctx=user_ctx, cfg=cfg, pref=pref)
        for key, value in kwargs.items():
            try:
                setattr(ctx, key, value)
            except Exception:  # noqa: BLE001
                pass
        return ctx


def _progress_text(titles: list[str]) -> str:
    """「正在查找：《X》等 N 份资料」：优先用 rag 内核的实现，缺失时用等价兜底。"""
    try:
        from tsukiyohub.kernel.rag.answer import progress_found

        return str(progress_found(list(titles)) or "")
    except Exception:  # noqa: BLE001
        names = list(dict.fromkeys(t for t in titles if t))
        if not names:
            return ""
        head = "".join(f"《{n[:20]}》" for n in names[:2])
        return f"正在查找：{head}" + (f"等 {len(names)} 份资料" if len(names) > 2 else "")


def _is_tool_unsupported(exc: BaseException) -> bool:
    """识别「模型不支持工具调用」：适配器打的标记优先，其次看类名与报错文本。"""
    if getattr(exc, "tool_unsupported", False):
        return True
    if type(exc).__name__ in ("LLMToolUnsupported", "_ToolUnsupported"):
        return True
    text = str(exc).lower()
    if not any(hint in text for hint in _TOOL_HINTS):
        return False
    return any(mark in text for mark in ("400", "bad request", "not support", "unsupported", "invalid"))


# ---------------------------------------------------------------------------
# ChatAgent
# ---------------------------------------------------------------------------
class ChatAgent:
    """一次问答的执行者（无状态，可被多线程并发调用）。"""

    def __init__(self, settings: Settings, router: LLMRouter, tools: Any, kv: KVStore | None = None) -> None:
        self.settings = settings
        self.router = router
        self.tools = tools
        self.kv = kv

    # ================================================================ 偏好
    def _preference(self) -> dict[str, Any]:
        """``settings`` + KV 运行期偏好 → 统一的 preference 结构。

        KV 里的 ``pref`` 是**扁平**的（前端只改几个开关）：
        ``bot_name / persona_prompt / tone / length / markdown / fallback_text / memory_enabled``；
        也允许直接给嵌套结构（``persona`` / ``style`` / ``fallback`` / ``safety`` / ``tools`` / ``retrieval``）。
        缺省字段不填——由 :mod:`prompt` 与 :mod:`answer_style` 回落到 ``settings`` 默认值。
        """
        raw: dict[str, Any] = {}
        if self.kv is not None:
            try:
                value = self.kv.get("pref")
                raw = value if isinstance(value, dict) else {}
            except Exception:  # noqa: BLE001 - 读不到配置不影响回答
                raw = {}
        pref: dict[str, Any] = {
            "persona": _section(raw, "persona"),
            "style": _section(raw, "style"),
            "fallback": _section(raw, "fallback"),
            "safety": _section(raw, "safety"),
            "tools": _section(raw, "tools"),
            "retrieval": _section(raw, "retrieval"),
        }
        # 扁平键优先映射进来（前端「设置」页只暴露这几个）
        if raw.get("bot_name"):
            pref["persona"]["name"] = str(raw["bot_name"])
        if raw.get("persona_prompt"):
            pref["persona"]["systemPrompt"] = str(raw["persona_prompt"])
        for key in ("tone", "length"):
            if raw.get(key):
                pref["style"][key] = str(raw[key])
        if raw.get("markdown") is not None:
            pref["style"]["markdown"] = bool(raw["markdown"])
        if raw.get("fallback_text"):
            pref["fallback"]["text"] = str(raw["fallback_text"])
        if "memory_enabled" in raw:
            pref["memory_enabled"] = bool(raw["memory_enabled"])
        # 检索参数可以在 KV 单独调（不改 .env）
        if pref["retrieval"].get("top_k") in (None, "") and self.kv is not None:
            try:
                top_k = self.kv.get("retrieval.top_k")
            except Exception:  # noqa: BLE001
                top_k = None
            if top_k:
                try:
                    pref["retrieval"]["top_k"] = int(top_k)
                except (TypeError, ValueError):
                    pass
        return pref

    def effective_preference(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        """KV 默认偏好 + 本次请求的覆盖（一层字典合并，便于前端只传改动项）。"""
        base = self._preference()
        if not override:
            return base
        merged = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged[key] = {**base[key], **value}
            else:
                merged[key] = value
        return merged

    def harness_config(self) -> dict[str, Any]:
        """技能开关（KV 里 ``skills.disabled`` → 注册表认识的 ``{"tools": {...}}``）。"""
        disabled: set[str] = set()
        if self.kv is not None:
            try:
                disabled = {str(name) for name in (self.kv.get("skills.disabled") or [])}
            except Exception:  # noqa: BLE001
                disabled = set()
        return {"tools": {name: False for name in disabled}, "disabled": sorted(disabled)}

    # ================================================================ 入口
    def run(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        user_ctx: dict[str, Any] | None = None,
        *,
        preference: dict[str, Any] | None = None,
        on_event: EventSink | None = None,
    ) -> AgentResult:
        """回答一个问题。**永不抛业务异常**：最差也返回可展示的降级结果。"""
        started = time.perf_counter()
        text = (question or "").strip()
        history = list(history or [])
        user_ctx = dict(user_ctx or {})
        pref = self.effective_preference(preference)
        user_ctx.setdefault("question", text)
        mask = build_mask(pref)

        # ---- 安全拒答：命中禁止话题不进模型（默认关闭）
        refused = mask.reject_reason(text)
        if refused:
            answer = str((pref.get("safety") or {}).get("refuseText") or "") or "抱歉，这个问题我不方便回答。"
            self._emit(on_event, "delta", answer)
            result = AgentResult(answer=answer, mode="refuse", route="refuse",
                                 latency_ms=_ms_since(started))
            logger.info("问题命中禁止话题「%s」，已拒答", refused)
            return result
        if not text:
            result = AgentResult(answer=self._fallback_text(pref), mode="empty", route="empty",
                                 latency_ms=_ms_since(started))
            self._emit(on_event, "delta", result.answer)
            return result

        tools = self._enabled_tools(pref)
        candidates = self._candidates()
        if not candidates:
            result = self._degraded_answer(text, user_ctx, pref, on_event)
            result.degraded_reason = "no_model_configured"
            result.latency_ms = _ms_since(started)
            return result

        errors: list[str] = []
        failures: list[UsageRecord] = []
        for client in candidates:
            try:
                result = self._run_with_client(client, text, history, user_ctx, pref, tools, on_event, mask)
            except Exception as exc:  # noqa: BLE001 - 任何模型错误都尝试下一个
                logger.warning("模型 %s 调用失败：%s", _client_name(client), exc)
                errors.append(str(exc))
                failures.append(UsageRecord(model=getattr(client, "model", ""), provider=getattr(client, "name", ""),
                                            call_type="chat", success=False, error=str(exc)[:500]))
                continue
            result.usages = failures + result.usages
            if errors:
                result.degraded_reason = f"fallback: {errors[-1][:120]}"
            result.latency_ms = _ms_since(started)
            return result

        # ---- 全部模型失败：直接检索给原文摘录
        result = self._degraded_answer(text, user_ctx, pref, on_event)
        result.usages = failures
        result.degraded_reason = f"all_models_failed: {(errors[-1] if errors else 'unknown')[:200]}"
        result.latency_ms = _ms_since(started)
        logger.error("全部模型失败，返回原文摘录：%s", result.degraded_reason)
        return result

    def _run_with_client(
        self, client: LLMClient, question: str, history: list[dict[str, str]], user_ctx: dict[str, Any],
        pref: dict[str, Any], tools: list[Any], on_event: EventSink | None, mask: MaskSession,
    ) -> AgentResult:
        """单个模型的一次完整尝试（可能内部降级为 plain 模式）。"""
        if getattr(client, "supports_tools", None) is False:
            logger.info("模型 %s 已知不支持工具，直接走「先查再答」", _client_name(client))
            return self._run_plain(client, question, history, user_ctx, pref, tools, on_event, mask)
        try:
            result = self._run_agent(client, question, history, user_ctx, pref, tools, on_event, mask)
        except _ToolUnsupported as exc:
            logger.warning("模型 %s 不支持工具调用（%s），改走「先查再答」", _client_name(client), exc)
            try:
                client.supports_tools = False  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            return self._run_plain(client, question, history, user_ctx, pref, tools, on_event, mask)
        if getattr(client, "supports_tools", None) is None:
            try:
                client.supports_tools = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return result

    # ================================================================ 带工具
    def _run_agent(
        self, client: LLMClient, question: str, history: list[dict[str, str]], user_ctx: dict[str, Any],
        pref: dict[str, Any], tools: list[Any], on_event: EventSink | None, mask: MaskSession,
    ) -> AgentResult:
        result = self._new_result(client, "agent")
        ctx = self._new_ctx(question, user_ctx, pref, history)
        messages = self._messages(pref, user_ctx, history, question, mask, tools)
        specs = [tool.spec for tool in tools]

        # ---- 规则预取：明确要查资料时先查一次，省掉一轮「模型决定要不要调工具」
        kb = _find_tool(tools, KNOWLEDGE_TOOL)
        if kb is not None and self._kb_prefetch(pref) and router_rules.needs_knowledge(question, history):
            self._emit(on_event, "status", "searching")
            content = self._prefetch_tool(kb, ctx, on_event)
            if content:
                args = {"query": question}
                self._append_tool_exchange(messages, kb.name, args, content, mask)
                self._emit_progress(on_event, ctx)

        max_rounds = self._max_tool_rounds(pref)
        for round_no in range(max_rounds + 1):
            final_round = round_no >= max_rounds or not tools
            started = time.perf_counter()
            try:
                reply = self._call(client, messages, None if final_round else specs, pref, on_event, mask)
            except Exception as exc:  # noqa: BLE001
                if round_no == 0 and not final_round and _is_tool_unsupported(exc):
                    raise _ToolUnsupported(str(exc)[:200]) from exc
                raise
            latency = _ms_since(started)
            result.llm_ms += latency
            result.usages.append(self._usage(client, reply, "chat" if round_no == 0 else "tool_round", latency))

            calls = list(reply.tool_calls or [])
            if not calls or final_round:
                result.answer = (reply.content or "").strip()
                break

            self._emit(on_event, "status", "searching")
            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.name, "arguments": _json(call.arguments)}}
                    for call in calls
                ],
            })
            for call in calls:
                tool = _find_tool(tools, call.name)
                if tool is None:
                    content = f"技能 {call.name} 不可用。"
                    self._emit(on_event, "tool", {"name": call.name, "ok": False})
                else:
                    content = self._call_tool(tool, call.arguments or {}, ctx, on_event)
                    if tool.name == KNOWLEDGE_TOOL:
                        self._emit_progress(on_event, ctx)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id or uuid.uuid4().hex[:12],
                    "content": mask.mask_in(content),
                })
        return self._finish(result, ctx, pref, mask, client)

    # ================================================================ 无工具
    def _run_plain(
        self, client: LLMClient, question: str, history: list[dict[str, str]], user_ctx: dict[str, Any],
        pref: dict[str, Any], tools: list[Any], on_event: EventSink | None, mask: MaskSession,
    ) -> AgentResult:
        """模型不支持工具调用：先让各技能按问题取材料，拼进提示词，再调一次模型。"""
        result = self._new_result(client, "plain")
        ctx = self._new_ctx(question, user_ctx, pref, history)
        sections: list[str] = []
        for tool in tools:
            content = self._prefetch_tool(tool, ctx, on_event)
            if content:
                sections.append(str(content))
        if sections and getattr(ctx, "hits", None):
            self._emit_progress(on_event, ctx)
        extra = ""
        if sections:
            extra = PLAIN_RULES + "\n\n" + "\n\n".join(mask.mask_in(section) for section in sections)
        messages = self._messages(pref, user_ctx, history, question, mask, [], extra_system=extra)
        started = time.perf_counter()
        reply = self._call(client, messages, None, pref, on_event, mask)
        result.llm_ms = _ms_since(started)
        result.usages.append(self._usage(client, reply, "chat", result.llm_ms))
        result.answer = (reply.content or "").strip()
        return self._finish(result, ctx, pref, mask, client)

    # ================================================================ 降级
    def _degraded_answer(
        self, question: str, user_ctx: dict[str, Any], pref: dict[str, Any], on_event: EventSink | None,
    ) -> AgentResult:
        """全部模型不可用：直接检索知识库，把最相关的原文摘录给用户（至少拿到出处）。"""
        ctx = self._new_ctx(question, user_ctx, pref, [])
        kb = _find_tool(self._enabled_tools(pref), KNOWLEDGE_TOOL)
        if kb is not None:
            self._emit(on_event, "status", "searching")
            self._prefetch_tool(kb, ctx, on_event)
            self._emit_progress(on_event, ctx)
        result = AgentResult(answer="", degraded=True, mode="degraded", route="fallback",
                             trace_id=uuid.uuid4().hex[:12])
        return self._finish(result, ctx, pref, build_mask(pref), None)

    # ================================================================ 收尾
    def _finish(
        self, result: AgentResult, ctx: Any, pref: dict[str, Any], mask: MaskSession, client: LLMClient | None,
    ) -> AgentResult:
        """引用编号 / 核对 / grounding 的唯一收口点（所有链路都必须走这里）。"""
        hits: dict[int, ScoredChunk] = dict(getattr(ctx, "hits", None) or {})
        result.kb_called = bool(getattr(ctx, "kb_called", False) or hits)
        result.tool_calls = list(getattr(ctx, "records", None) or [])
        result.retrieved = list(getattr(ctx, "retrieved", None) or [])
        result.retrieve_ms = int((getattr(ctx, "timings", None) or {}).get("retrieve", 0))

        answer = (result.answer or "").strip()
        if answer:
            # 核对要在还原脱敏占位符之前做：模型看到的回答与资料都是脱敏后的
            if hits and result.mode in ("agent", "plain") and self._answer_judge_mode(pref) != "off":
                answer = self._check_answer(result, answer, ctx, pref, mask, client)
            answer = mask.restore_out(answer)
            if hits:
                answer = citations_mod.tidy_refs(answer, {i: (hit.chunk.doc_id or str(i)) for i, hit in hits.items()})
            result.citations = self._pick_citations(answer, hits)
            answer, result.citations, result.evidence = citations_mod.renumber(
                answer,
                result.citations,
                {c.index: hits[c.index].chunk.text for c in result.citations if c.index in hits},
                {i: citations_mod.page_of(hit.chunk) for i, hit in hits.items()},
            )
            answer = citations_mod.merge_pages(answer)
            # 删掉无效编号后中文标点前会留下空格（「结构 。」）
            answer = re.sub(r"[ \t]+([。，；：！？、])", r"\1", answer)
        else:
            # 模型没给正文：原文摘录兜底（有引用列表，用户仍能顺着来源看）
            answer = self._extractive(hits) if hits else self._fallback_text(pref)
            result.citations = self._pick_citations("", hits, fallback=True)
            result.degraded = True
            result.degraded_reason = result.degraded_reason or "empty_answer"
            result.mode = "degraded"

        result.answer = answer
        result.context = {
            i: f"{_source_line(hit.chunk)}\n{hit.chunk.text}" for i, hit in sorted(hits.items())
        }
        self._grade(result, hits)
        result.hit = bool(result.kb_called and result.citations)
        return result

    def _grade(self, result: AgentResult, hits: dict[int, ScoredChunk]) -> None:
        """写 grounding_score / grounded：优先用 rag 内核的两档闸门，缺失时按向量分兜底。"""
        chunks = [hits[i] for i in sorted(hits)]
        if not chunks:
            result.grounding_score = None
            result.grounded = False
            return
        try:
            from tsukiyohub.kernel.rag.grounding import evaluate_grounding

            verdict = evaluate_grounding(chunks, float(self.settings.grounding_min_score))
            score = float(getattr(verdict, "score", 0.0) or 0.0)
            result.grounding_score = round(score, 4)
            result.grounded = bool(getattr(verdict, "grounded", False))
            return
        except Exception as exc:  # noqa: BLE001 - rag 内核缺失或阈值实现不同
            logger.debug("grounding 判定不可用，改按最高向量分：%s", exc)
        scores = [hit.vector_score for hit in chunks if hit.vector_score is not None]
        if scores:
            best = max(scores)
            result.grounding_score = round(float(best), 4)
            result.grounded = float(best) >= float(self.settings.grounding_min_score)
        else:
            result.grounded = True   # 只有 BM25 分时无法比较阈值，按「有检索结果」处理

    def _check_answer(
        self, result: AgentResult, answer: str, ctx: Any, pref: dict[str, Any],
        mask: MaskSession, client: LLMClient | None,
    ) -> str:
        """逐句核对：删掉资料不支持的句子（``settings.answer_judge`` = strict / lenient / off）。"""
        if client is None:
            return answer
        try:
            from tsukiyohub.kernel.rag import ground_check
        except Exception as exc:  # noqa: BLE001 - rag 内核还没实现时跳过核对
            logger.debug("逐句核对不可用（%s），跳过", exc)
            return answer
        hits: dict[int, ScoredChunk] = dict(getattr(ctx, "hits", None) or {})
        evidence = [(i, mask.mask_in(hits[i].chunk.text)) for i in sorted(hits)][:CHECK_EVIDENCE]
        if not evidence:
            return answer

        def complete(prompt: str) -> str:
            started = time.perf_counter()
            reply = client.chat([{"role": "user", "content": prompt}],
                                params={"temperature": 0, "max_tokens": 256})
            latency = _ms_since(started)
            result.llm_ms += latency
            result.usages.append(self._usage(client, reply, "judge", latency))
            return reply.content or ""

        try:
            checked = ground_check.check(
                answer, evidence, complete, mode=self._answer_judge_mode(pref),
                question=str(getattr(ctx, "question", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 核对失败不影响回答
            logger.warning("逐句核对失败（沿用原回答）：%s", exc)
            return answer
        error = getattr(checked, "error", None)
        removed = list(getattr(checked, "removed", None) or [])
        if error:
            logger.debug("逐句核对面返回错误：%s", error)
        if removed:
            logger.info("核对删除 %d 句无依据内容：%s", len(removed), "｜".join(removed)[:200])
        return str(getattr(checked, "answer", "") or answer)

    # ================================================================ 提示词
    def build_system_prompt(
        self, pref: dict[str, Any], user_ctx: dict[str, Any], tools: list[Any] | None = None,
    ) -> str:
        """人设 + 技能规则 + 表达规范（+ 有知识库时附加「依据要求」）。"""
        enabled = self._enabled_tools(pref) if tools is None else list(tools)
        style = pref.get("style") or {}
        parts = [
            prompt_mod.build_persona_prompt(self.settings, pref, user_ctx),
            prompt_mod.build_tool_rules(enabled) if enabled else "",
        ]
        if style.get("expression", True):
            question = str(user_ctx.get("question") or "")
            shape = str(user_ctx.get("answer_shape") or guess_shape(question))
            parts.append(expression_rules(style, shape, bool(user_ctx.get("group"))))
        if any(getattr(tool, "name", "") == KNOWLEDGE_TOOL for tool in enabled):
            parts.append(grounding_rules())
        return "\n\n".join(part for part in parts if part)

    # ================================================================ 其它调用
    def simple_completion(
        self, prompt: str, *, call_type: str = "utility", params: dict[str, Any] | None = None,
    ) -> tuple[str, list[UsageRecord]]:
        """不带工具的一次性模型调用（记忆抽取 / 摘要 / 轻判定）。

        主模型失败就换备用；全失败返回 ``("", 失败记录)``，调用方自行决定怎么兜底。
        """
        usages: list[UsageRecord] = []
        payload = params or {"temperature": 0.2, "max_tokens": 600}
        for client in self._candidates():
            started = time.perf_counter()
            try:
                reply = client.chat([{"role": "user", "content": prompt}], params=payload)
            except Exception as exc:  # noqa: BLE001
                usages.append(UsageRecord(model=getattr(client, "model", ""), provider=getattr(client, "name", ""),
                                          call_type=call_type, success=False, error=str(exc)[:500]))
                logger.warning("辅助调用失败（%s）：%s", _client_name(client), exc)
                continue
            usages.append(self._usage(client, reply, call_type, _ms_since(started)))
            return (reply.content or "").strip(), usages
        return "", usages

    # ================================================================ 内部工具
    def _call(
        self, client: LLMClient, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
        pref: dict[str, Any], on_event: EventSink | None, mask: MaskSession,
    ) -> LLMReply:
        """调模型；有事件回调时走流式，把「当前全文」推给前端（脱敏后）。"""
        params = pref.get("params") if isinstance(pref.get("params"), dict) else None
        sink = None
        if on_event is not None:
            def _sink(text: str) -> None:
                # 适配器给的是「当前累积全文」，脱敏还原后直接推给前端（前端整段替换）
                self._emit(on_event, "delta", mask.restore_out(text))

            sink = _sink
        return client.chat(messages, tools=tools, params=params, on_delta=sink)

    def _call_tool(self, tool: Any, args: dict[str, Any], ctx: Any, on_event: EventSink | None) -> str:
        """执行一个技能；异常转成给模型看的错误文本（不让一次技能失败终止整轮问答）。"""
        name = str(getattr(tool, "name", "") or "")
        started = time.perf_counter()
        ok = True
        try:
            self._count_call(name)
            content = tool.call(args, ctx)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning("技能 %s 执行失败：%s", name, exc)
            content = f"技能 {name} 执行失败：{exc}"
        finally:
            _add_time(ctx, name, _ms_since(started))
        self._emit(on_event, "tool", {"name": name, "ok": ok})
        return "" if content is None else str(content)

    def _prefetch_tool(self, tool: Any, ctx: Any, on_event: EventSink | None) -> str | None:
        """技能的「先查再答」路径；返回要放进提示词的材料，无材料返回 ``None``。"""
        name = str(getattr(tool, "name", "") or "")
        started = time.perf_counter()
        ok = True
        try:
            self._count_call(name)
            content = tool.prefetch(ctx)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning("技能 %s 预取失败：%s", name, exc)
            content = None
        finally:
            _add_time(ctx, name, _ms_since(started))
        self._emit(on_event, "tool", {"name": name, "ok": ok})
        return str(content) if content else None

    def _messages(
        self, pref: dict[str, Any], user_ctx: dict[str, Any], history: list[dict[str, str]],
        question: str, mask: MaskSession, tools: list[Any], extra_system: str = "",
    ) -> list[dict[str, Any]]:
        system = self.build_system_prompt(pref, user_ctx, tools)
        if extra_system:
            system = f"{system}\n\n{extra_system}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for turn in history:
            role = "user" if str(turn.get("role") or "") == "user" else "assistant"
            content = mask.mask_in(str(turn.get("content") or ""))
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": mask.mask_in(question)})
        return messages

    def _append_tool_exchange(
        self, messages: list[dict[str, Any]], name: str, args: dict[str, Any], content: str, mask: MaskSession,
    ) -> None:
        """把「预取」伪装成一次模型发起的工具调用，让模型知道资料是从工具来的。"""
        call_id = f"prefetch_{uuid.uuid4().hex[:8]}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": _json(args)}}],
        })
        messages.append({"role": "tool", "tool_call_id": call_id, "content": mask.mask_in(content)})

    def _new_ctx(self, question: str, user_ctx: dict[str, Any], pref: dict[str, Any],
                 history: list[dict[str, Any]]) -> Any:
        cfg = dict(pref.get("retrieval") or {})
        cfg.setdefault("tools", dict(pref.get("tools") or {}))
        return _new_tool_context(question, user_ctx, cfg, pref, list(history))

    def _enabled_tools(self, pref: dict[str, Any]) -> list[Any]:
        """本轮交给模型的技能（请求级开关 > KV 开关 > 技能默认值，由注册表解析）。"""
        harness = self.harness_config()
        switches = pref.get("tools") if isinstance(pref.get("tools"), dict) else {}
        cfg = {**harness, "tools": {**harness.get("tools", {}), **switches}}
        allow = pref.get("allow_skills")
        allow = list(allow) if isinstance(allow, list) else None
        try:
            return list(self.tools.enabled(cfg, allow))
        except TypeError:  # 注册表实现更严格/更宽松时逐级降级
            try:
                return list(self.tools.enabled(cfg))
            except Exception:  # noqa: BLE001
                return list(self.tools.enabled())

    def _candidates(self) -> list[LLMClient]:
        try:
            return [client for client in self.router.candidates() if client is not None]
        except Exception as exc:  # noqa: BLE001 - 模型未配置
            logger.warning("没有可用模型：%s", exc)
            return []

    def _new_result(self, client: LLMClient | None, mode: str) -> AgentResult:
        return AgentResult(
            answer="", mode=mode,
            model=str(getattr(client, "model", "") or ""),
            provider=str(getattr(client, "name", "") or ""),
        )

    def _usage(self, client: LLMClient, reply: LLMReply, call_type: str, latency: int) -> UsageRecord:
        return UsageRecord(
            model=str(reply.model or getattr(client, "model", "") or ""),
            provider=str(reply.provider or getattr(client, "name", "") or ""),
            call_type=call_type,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            total_tokens=reply.total_tokens,
            latency_ms=latency,
        )

    def _max_tool_rounds(self, pref: dict[str, Any]) -> int:
        value = pref.get("max_tool_rounds")
        try:
            rounds = int(value) if value is not None else int(self.settings.max_tool_rounds)
        except (TypeError, ValueError):
            rounds = int(self.settings.max_tool_rounds)
        return max(0, rounds)

    def _kb_prefetch(self, pref: dict[str, Any]) -> bool:
        value = pref.get("kb_prefetch")
        return bool(self.settings.kb_prefetch if value is None else value)

    def _answer_judge_mode(self, pref: dict[str, Any]) -> str:
        mode = str(pref.get("answer_judge") or self.settings.answer_judge or "off")
        return mode if mode in ("strict", "lenient", "off") else "off"

    def _fallback_text(self, pref: dict[str, Any]) -> str:
        text = str((pref.get("fallback") or {}).get("text") or "").strip()
        return text or str(self.settings.fallback_text or "抱歉，我暂时无法回答这个问题。")

    def _extractive(self, hits: dict[int, ScoredChunk]) -> str:
        """模型不可用时的原文摘录（优先用 rag 内核的实现，缺失时本地渲染）。"""
        chunks = [hits[i] for i in sorted(hits)]
        if not chunks:
            return ""
        try:
            from tsukiyohub.kernel.rag.answer import extractive_answer

            return str(extractive_answer(chunks) or "")
        except Exception:  # noqa: BLE001
            parts = ["模型暂时不可用，以下是从知识库检索到的原文片段，供你直接查阅：\n"]
            for index, item in enumerate(chunks[:3], start=1):
                body = item.chunk.text.strip().replace("\n", " ")
                parts.append(f"[{index}] {_source_line(item.chunk)}\n{body[:700]}\n")
            return "\n".join(parts)

    @staticmethod
    def _pick_citations(
        answer: str, hits: dict[int, ScoredChunk], *, fallback: bool = False,
    ) -> list[Citation]:
        """正文引用了哪些片段就列哪些；没有引用不挂来源（拒答下面列一串资料最误导）。

        ``fallback``：模型不可用时的原文摘录，按检索顺序列前 3 段。
        """
        if not hits:
            return []
        referenced = [int(n) for n in _REF_RE.findall(answer or "") if int(n) in hits]
        order = list(dict.fromkeys(referenced)) or (sorted(hits)[:3] if fallback else [])
        return [_build_citation(hits[index], index) for index in order]

    def _emit(self, on_event: EventSink | None, kind: str, value: Any) -> None:
        """推一个内核事件；回调抛异常不能影响回答本身。"""
        if on_event is None:
            return
        try:
            on_event(kind, value)
        except Exception as exc:  # noqa: BLE001 - 前端断开 / 序列化失败
            logger.debug("事件推送失败（%s）：%s", kind, exc)

    def _emit_progress(self, on_event: EventSink | None, ctx: Any) -> None:
        hits: dict[int, ScoredChunk] = dict(getattr(ctx, "hits", None) or {})
        if not hits:
            return
        text = _progress_text([hits[i].chunk.title for i in sorted(hits)])
        if text:
            self._emit(on_event, "progress", text)

    def _count_call(self, name: str) -> None:
        try:
            self.tools.count_call(name)
        except Exception:  # noqa: BLE001 - 计数失败不影响调用
            pass


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------
def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _find_tool(tools: list[Any], name: str) -> Any | None:
    return next((tool for tool in tools if getattr(tool, "name", "") == name), None)


def _client_name(client: LLMClient) -> str:
    return f"{getattr(client, 'name', '')}:{getattr(client, 'model', '')}"


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _add_time(ctx: Any, key: str, ms: int) -> None:
    try:
        ctx.add_time(key, ms)
    except Exception:  # noqa: BLE001 - 上下文实现可能有差异
        timings = getattr(ctx, "timings", None)
        if isinstance(timings, dict):
            timings[key] = timings.get(key, 0) + ms


def _json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 参数理论上一定是 JSON 可序列化的
        return "{}"


def _source_line(chunk: Any) -> str:
    """来源行：``《标题》版本 · 面包屑``（与 rag 内核的 source_line 对齐，缺失时本地拼）。"""
    try:
        from tsukiyohub.kernel.rag.answer import source_line

        return str(source_line(chunk))
    except Exception:  # noqa: BLE001
        title = getattr(chunk, "title", "") or "未命名文档"
        version = getattr(chunk, "version", "") or ""
        crumb = str((getattr(chunk, "extra", None) or {}).get("breadcrumb") or "")
        parts = [f"《{title}》" + (f"{version} 版" if version else "")]
        if crumb:
            parts.append(crumb.replace(" / ", " > "))
        return " · ".join(parts)


def _build_citation(item: ScoredChunk, index: int, snippet_length: int = 160) -> Citation:
    """检索命中 → 引用（编号用段编号，稍后由 ``citations.renumber`` 换成文档编号）。"""
    chunk = item.chunk
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


__all__ = ["AgentResult", "ChatAgent", "EventSink", "KNOWLEDGE_TOOL", "PLAIN_RULES", "UsageRecord"]