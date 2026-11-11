"""会话编排：把「一个 HTTP 请求」变成「一次有上下文、有记忆、能落库的问答」。

职责边界
--------
* **解析 / 新建会话**：``session_id`` 不存在就建；``user_id`` 用来做隔离（不能续用别人的会话）；
* **上下文窗口**：默认取 ``settings.history_max_turns`` 轮；超过 ``history_summary_after``
  时把窗口外更早的轮次交给 :mod:`tsukiyohub.kernel.memory.summary` 压成摘要，失败则直接截断
  （宁可少一点上下文，也不要让请求失败）；
* **长期记忆**：``settings.memory_enabled`` 时召回用户事实注入 ``user_ctx["memory"]``，
  回答后**异步**抽取新事实（失败静默，绝不拖慢响应）；
* **落库**：用户消息 + 助手消息（引用 / 工具调用 / 用量 / 耗时 / 轨迹一起存）；
* **并发保护**：模块级 :class:`asyncio.Semaphore`，同时处理的问答数不超过
  ``settings.max_concurrent_chats``；
* **同步内核搬线程**：模型与检索都是同步阻塞调用，放进 ``asyncio.to_thread``，
  这样 SSE 的 ``on_event`` 从工作线程回调时，API 层可以用 ``call_soon_threadsafe`` 接住。

这里**不出现任何业务概念**：没有渠道鉴权、没有组织架构、没有消息去重（去重是渠道的事）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import Channel, ChatRequest, ChatResponse, Intent
from tsukiyohub.kernel.chat.agent import AgentResult, ChatAgent, EventSink
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.hot import HotStore, NoopHotStore
from tsukiyohub.ports.memory import MessageRecord, SessionRecord, SessionStore

logger = get_logger(__name__)

#: 全局并发槽位（进程级，模块级单例）。按上限值缓存，测试里改配置也能生效。
_SLOTS: asyncio.Semaphore | None = None
_SLOT_LIMIT = 0
#: 后台任务（记忆抽取）的强引用集合：不持有引用可能被 GC 提前回收
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _slots(settings: Settings) -> asyncio.Semaphore:
    """取进程级并发槽位；上限变化时重建（Semaphore 创建不需要事件循环）。"""
    global _SLOTS, _SLOT_LIMIT
    limit = max(1, int(settings.max_concurrent_chats or 1))
    if _SLOTS is None or _SLOT_LIMIT != limit:
        _SLOTS = asyncio.Semaphore(limit)
        _SLOT_LIMIT = limit
    return _SLOTS


def _now() -> str:
    """ISO8601 时间戳（**微秒**精度）。

    会话消息按 ``created_at`` 排序，同一秒内的「用户问 + 助手答」必须能区分先后，
    否则某些实现会用随机 id 兜底排序，历史顺序就会翻转（第一轮看起来像助手先说）。
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ConversationService:
    """会话编排（无状态，可被并发调用）。"""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionStore,
        memory: Any,
        agent: ChatAgent,
        skills: Any,
        hot: HotStore | None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.memory = memory          # MemoryService（可为 None）
        self.agent = agent
        self.skills = skills
        self.hot = hot or NoopHotStore()

    # ================================================================ 入口
    async def answer(self, request: ChatRequest, *, on_event: EventSink | None = None) -> ChatResponse:
        """处理一次问答请求。异常向上抛（由 HTTP 层转 5xx），业务上的降级都在内核里做完。"""
        started = time.perf_counter()
        question = request.normalized_question()
        if not question:
            # 空问题不建会话、不调模型（API 层已校验；这里兜住脚本 / 渠道直接构造的请求）
            return ChatResponse(answer="", session_id=request.session_id or "", message_id=uuid.uuid4().hex,
                                intent=Intent.UNKNOWN, latency_ms=_ms_since(started))
        async with _slots(self.settings):
            session = self.resolve_session(request)

            pref = self._preference(request)
            history, summary = self._context(session)
            user_ctx: dict[str, Any] = {
                "user_id": request.user_id or session.user_id or "",
                "name": request.user_name or "",
                "user_name": request.user_name or "",
                "channel": _channel(request),
                "question": question,
                "group": bool(request.extra.get("group")),
            }
            if summary:
                user_ctx["summary"] = summary
            memory_text, memory_enabled = self._recall(user_ctx["user_id"], question, pref)
            if memory_text:
                user_ctx["memory"] = memory_text

            result = await asyncio.to_thread(self._run_agent, question, history, user_ctx, pref, on_event)
            message_id = await self._persist(session, question, result)
            if memory_enabled:
                self._extract_async(user_ctx["user_id"], question, result.answer)
            return self._to_response(request, result, session, message_id, history, started=started)

    # ================================================================ 会话
    def resolve_session(self, request: ChatRequest) -> SessionRecord:
        """取会话；不存在（或不属于该用户）就新建一个。"""
        session: SessionRecord | None = None
        if request.session_id:
            try:
                session = self.sessions.get_session(str(request.session_id))
            except Exception as exc:  # noqa: BLE001 - 存储暂时不可用时仍然允许回答
                logger.warning("读取会话失败（%s）：%s", request.session_id, exc)
                session = None
        if session is not None and request.user_id and session.user_id and session.user_id != request.user_id:
            # 用户隔离：别人的会话不能接着用（避免把 A 的上下文喂给 B）
            logger.info("会话 %s 属于其他用户，改为新建", session.id)
            session = None
        if session is None:
            session = self.sessions.create_session(
                user_id=request.user_id or None, channel=_channel(request), title="",
            )
        return session

    def history(self, session: SessionRecord) -> list[dict[str, str]]:
        """最近 ``history_max_turns`` 轮对话（不含摘要；摘要走 :meth:`answer` 内部的上下文构建）。"""
        return self._context(session)[0]

    def _context(self, session: SessionRecord) -> tuple[list[dict[str, str]], str]:
        """构造上下文：返回（最近几轮消息，更早轮次的摘要）。

        取数上限同时覆盖「窗口」与「摘要阈值」，只有拿到足够的尾部才能判断是否超阈值。
        超阈值时：窗口内保留最近 ``history_max_turns`` 轮，窗口外交给摘要；摘要失败返回 ""（退化为截断）。
        """
        turns = max(0, int(self.settings.history_max_turns))
        after = max(0, int(self.settings.history_summary_after))
        # 多取一轮，才能判断「是否超过摘要阈值」；不足时按实际条数处理
        fetch = max(turns * 2, (after + 1) * 2, 2)
        try:
            rows = self.sessions.list_messages(session.id, limit=fetch)
        except Exception as exc:  # noqa: BLE001 - 读历史失败就当新对话
            logger.warning("读取历史消息失败（%s）：%s", session.id, exc)
            return [], ""
        messages = [
            {"role": row.role, "content": row.content}
            for row in rows
            if row.role in ("user", "assistant") and (row.content or "").strip()
        ]
        if not turns:
            return [], ""
        rounds = len(messages) // 2
        if not after or rounds <= after:
            return messages[-turns * 2:], ""
        keep = turns * 2
        dropped = messages[:-keep] if keep else messages
        recent = messages[-keep:] if keep else []
        summary = self._summarize(session, dropped) if dropped else ""
        return recent, summary

    def _summarize(self, session: SessionRecord, dropped: list[dict[str, str]]) -> str:
        """把窗口外对话压成摘要。模块缺失 / 模型失败都返回空串——调用方退化为截断。"""
        try:
            from tsukiyohub.kernel.memory.summary import summarize
        except Exception as exc:  # noqa: BLE001 - 记忆内核可能没装/裁剪
            logger.debug("对话摘要模块不可用（%s），改为截断历史", exc)
            return ""
        key = f"chat:summary:{session.id}"
        previous = ""
        try:
            previous = str(self.hot.get(key) or "")
        except Exception:  # noqa: BLE001 - 热存储是可丢弃的加速层
            previous = ""

        def complete(prompt: str) -> str:
            try:
                text, _usages = self.agent.simple_completion(prompt, call_type="summary")
                return text
            except Exception as exc:  # noqa: BLE001
                logger.warning("生成对话摘要失败：%s", exc)
                return ""

        try:
            text = summarize(dropped, complete=complete, previous=previous)
        except TypeError:
            # 兼容早期 / 简化实现：不带 previous 的签名
            try:
                text = summarize(dropped, complete=complete)
            except Exception as exc:  # noqa: BLE001
                logger.warning("生成对话摘要失败（退化为截断）：%s", exc)
                return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("生成对话摘要失败（退化为截断）：%s", exc)
            return ""
        text = str(text or "").strip()
        if text:
            try:
                self.hot.set(key, text, ttl_seconds=3600)
            except Exception:  # noqa: BLE001
                pass
        return text

    # ================================================================ 偏好 / 记忆
    def _preference(self, request: ChatRequest) -> dict[str, Any]:
        """把请求字段翻译成内核偏好（键名见 docs/chat.md「偏好注入」一节）。"""
        raw = request.extra.get("preference")
        pref: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if request.skills is not None:
            pref["allow_skills"] = list(request.skills)
        retrieval = dict(pref.get("retrieval")) if isinstance(pref.get("retrieval"), dict) else {}
        if request.top_k:
            retrieval["top_k"] = int(request.top_k)
        if request.doc_types:
            retrieval["doc_types"] = [
                getattr(doc_type, "value", str(doc_type)) for doc_type in request.doc_types
            ]
        if retrieval:
            pref["retrieval"] = retrieval
        if "agentic" in request.extra:
            pref["agentic"] = bool(request.extra["agentic"])
        return pref

    def _memory_enabled(self, pref: dict[str, Any]) -> bool:
        """记忆开关：请求偏好 > KV 偏好 > settings（任一处显式关闭即关闭）。"""
        value = pref.get("memory_enabled")
        if value is None:
            try:
                value = self.agent.effective_preference().get("memory_enabled")
            except Exception:  # noqa: BLE001 - 装配的是替身 / 精简实现时忽略
                value = None
        return bool(self.settings.memory_enabled if value is None else value)

    def _recall(self, user_id: str, question: str, pref: dict[str, Any]) -> tuple[str, bool]:
        """召回用户长期事实。失败静默（记忆只是锦上添花，不能拖垮回答）。"""
        enabled = self._memory_enabled(pref)
        if not enabled or not user_id or self.memory is None:
            return "", False
        try:
            text = self.memory.recall(user_id, question)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆召回失败（忽略）：%s", exc)
            return "", enabled
        return str(text or "").strip(), enabled

    def _extract_async(self, user_id: str, question: str, answer: str) -> None:
        """回答后异步抽取长期事实：不阻塞响应，失败静默。"""
        if not user_id or self.memory is None or not (question and answer):
            return
        try:
            task = asyncio.create_task(asyncio.to_thread(self._safe_extract, user_id, question, answer))
        except RuntimeError:  # pragma: no cover - 没有事件循环（同步调用场景）
            self._safe_extract(user_id, question, answer)
            return
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    def _safe_extract(self, user_id: str, question: str, answer: str) -> None:
        try:
            self.memory.extract(user_id, question, answer)
        except Exception as exc:  # noqa: BLE001 - 抽取失败不影响任何用户可见行为
            logger.debug("记忆抽取失败（忽略）：%s", exc)

    # ================================================================ 执行 / 落库
    def _run_agent(
        self, question: str, history: list[dict[str, str]], user_ctx: dict[str, Any],
        pref: dict[str, Any], on_event: EventSink | None,
    ) -> AgentResult:
        """跑一次问答。``pref["agentic"]`` 打开时走可选的 Agentic 管线（默认关）。"""
        if pref.get("agentic"):
            try:
                from tsukiyohub.kernel.chat.agentic import AgenticPipeline

                return AgenticPipeline(self.agent).run(
                    question, history, user_ctx, preference={**pref, "agentic": False}, on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 - Agentic 出问题不能影响基本问答
                logger.warning("Agentic 管线失败，回退默认链路：%s", exc)
        return self.agent.run(question, history, user_ctx, preference=pref, on_event=on_event)

    async def _persist(self, session: SessionRecord, question: str, result: AgentResult) -> str:
        """落库用户消息 + 助手消息，并更新会话标题 / 最后一条消息。返回助手消息 id。"""
        message_id = uuid.uuid4().hex
        try:
            if question:
                self.sessions.append_message(MessageRecord(
                    id=uuid.uuid4().hex, session_id=session.id, role="user",
                    content=question, created_at=_now(),
                ))
            stored = self.sessions.append_message(MessageRecord(
                id=message_id,
                session_id=session.id,
                role="assistant",
                content=result.answer,
                created_at=_now(),
                citations=[citation.model_dump() for citation in result.citations],
                tool_calls=result.tool_calls,
                usage=result.usage_dict(),
                latency_ms=result.latency_ms,
                meta={
                    "model": result.model, "provider": result.provider, "mode": result.mode,
                    "route": result.route, "degraded": result.degraded,
                    "degraded_reason": result.degraded_reason, "grounded": result.grounded,
                    "grounding_score": result.grounding_score, "trace_id": result.trace_id,
                },
            ))
            if stored is not None and getattr(stored, "id", ""):
                message_id = str(stored.id)
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响回答本身
            logger.error("会话落库失败（%s）：%s", session.id, exc)
        try:
            self.sessions.touch_session(
                session.id,
                title=(question[:30] if question and not session.title else None),
                last_message=(result.answer or "")[:120] if result.answer else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("更新会话摘要失败（%s）：%s", session.id, exc)
        return message_id

    # ================================================================ 出参
    def _to_response(
        self, request: ChatRequest, result: AgentResult, session: SessionRecord, message_id: str,
        history: list[dict[str, str]], *, started: float,
    ) -> ChatResponse:
        debug = None
        if request.debug and not self.settings.is_production:
            debug = {
                "mode": result.mode,
                "route": result.route,
                "tool_calls": result.tool_calls,
                "retrieved": result.retrieved,
                "llm_ms": result.llm_ms,
                "retrieve_ms": result.retrieve_ms,
                "history_turns": len(history) // 2,
            }
        return ChatResponse(
            answer=result.answer,
            session_id=session.id,
            message_id=message_id,
            intent=_intent_of(result),
            citations=result.citations,
            model=result.model or None,
            provider=result.provider or None,
            latency_ms=_ms_since(started) or result.latency_ms,
            usage=result.usage_dict(),
            grounded=bool(result.grounded),
            grounding_score=result.grounding_score,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            tool_calls=result.tool_calls,
            skills_used=_skills_used(result),
            suggestions=list(result.suggestions),
            trace_id=result.trace_id,
            debug=debug,
        )


# ---------------------------------------------------------------------------
def _channel(request: ChatRequest) -> str:
    channel = request.channel
    return channel.value if isinstance(channel, Channel) else str(channel or "api")


def _intent_of(result: AgentResult) -> Intent:
    """把内核路由映射成对外意图（观测用，不影响回答内容）。"""
    if result.mode == "clarify" or result.route == "clarify":
        return Intent.CLARIFY
    if result.citations or result.kb_called:
        return Intent.KNOWLEDGE
    if result.tool_calls:
        return Intent.SKILL
    return Intent.CHAT


def _skills_used(result: AgentResult) -> list[str]:
    """本轮用到了哪些技能（按首次调用顺序去重）。"""
    names: list[str] = []
    for record in result.tool_calls:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["ConversationService"]