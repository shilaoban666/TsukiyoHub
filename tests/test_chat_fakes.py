"""聊天内核单测用的假实现（**不是测试用例**，被其它 ``test_chat_*.py`` 导入）。

为什么单独放一个文件
--------------------
``ChatAgent`` / ``ConversationService`` 只依赖端口（``LLMRouter`` / ``SessionStore`` /
``MemoryStore`` / ``SkillRegistry`` 的形状），所以单测不需要任何真实模型、数据库、知识库：
脚本化模型 + 内存会话库 + 假技能就能覆盖全部主路径，且每次结果确定、毫秒级。
"""

from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import Chunk, ScoredChunk
from tsukiyohub.ports.llm import LLMReply, ToolCall
from tsukiyohub.ports.memory import FactRecord, MessageRecord, SessionRecord

#: 测试里用的最小可用配置：演示模型、关闭逐句核对（避免依赖 rag 内核是否已就绪）
TEST_DATA_ROOT = Path(__file__).resolve().parent / ".data"


def make_settings(**overrides: Any) -> Settings:
    """构造 ``Settings``：不读 .env、数据目录放在 tests 下（不污染项目 data/）。"""
    base: dict[str, Any] = {
        "app_env": "test",
        "debug": False,
        "llm_provider": "fake",
        "llm_model": "fake-chat",
        "answer_judge": "off",
        "memory_enabled": False,
        "kb_prefetch": True,
        "max_tool_rounds": 3,
        "history_max_turns": 10,
        "history_summary_after": 12,
        "max_concurrent_chats": 4,
        "grounding_min_score": 0.35,
        "fallback_text": "抱歉，我暂时无法回答这个问题。",
        "data_root": TEST_DATA_ROOT,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def make_hit(
    doc_id: str = "doc-1", title: str = "示例手册", text: str = "年假为每年 5 天，需提前 3 天申请。",
    *, ordinal: int = 0, score: float = 0.82,
) -> ScoredChunk:
    """一条检索命中（带向量分，grounding 判定要用）。"""
    chunk = Chunk(chunk_id=f"{doc_id}#{ordinal}", doc_id=doc_id, text=text, ordinal=ordinal, title=title)
    return ScoredChunk(chunk=chunk, score=score, vector_score=score)


class FakeSkill:
    """最小技能：``call`` 与 ``prefetch`` 都往 ``ctx.hits`` 里塞同一批命中。"""

    def __init__(
        self, name: str = "knowledge_search", *, hits: list[ScoredChunk] | None = None,
        text: str = "## 参考资料\n[1] 《示例手册》\n年假为每年 5 天，需提前 3 天申请。",
        rules: str = "需要资料时调用知识库检索。", rich: bool = False,
    ) -> None:
        self.name = name
        self.title = name
        self.description = "检索知识库"
        self.rules = rules if rich else ""
        self.default_enabled = True
        self.parameters = {"type": "object", "properties": {"query": {"type": "string"}}}
        self.kind = "tool"
        self.source = "fake"
        self._hits = hits or [make_hit()]
        self._text = text
        self.calls: list[dict[str, Any]] = []
        self.prefetches = 0

    @property
    def spec(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def _fill(self, ctx: Any) -> None:
        index = int(getattr(ctx, "next_index", 1) or 1)
        for offset, item in enumerate(self._hits):
            ctx.hits[index + offset] = item
        ctx.next_index = index + len(self._hits)
        ctx.kb_called = True
        ctx.timings["retrieve"] = ctx.timings.get("retrieve", 0) + 3
        for offset, item in enumerate(self._hits):
            ctx.retrieved.append({"index": index + offset, "score": item.score, "retriever": item.retriever})

    def call(self, args: dict[str, Any], ctx: Any) -> str:
        self.calls.append(dict(args))
        self._fill(ctx)
        ctx.records.append({"name": self.name, "args": dict(args), "ok": True})
        return self._text

    def prefetch(self, ctx: Any) -> str | None:
        self.prefetches += 1
        self._fill(ctx)
        ctx.records.append({"name": self.name, "args": {"query": getattr(ctx, "question", "")}, "ok": True})
        return self._text


class FakeRegistry:
    """``SkillRegistry`` 的最小实现（只实现内核真正调用的 4 个方法）。"""

    def __init__(self, skills: list[FakeSkill] | None = None) -> None:
        self._skills = {skill.name: skill for skill in (skills or [])}
        self.calls: dict[str, int] = {}

    def get(self, name: str) -> FakeSkill | None:
        return self._skills.get(name)

    def enabled(self, cfg: dict[str, Any] | None = None, allow: list[str] | None = None) -> list[FakeSkill]:
        allowed = set(allow) if allow is not None else None
        switches = (cfg or {}).get("tools") or {}
        out: list[FakeSkill] = []
        for skill in self._skills.values():
            if allowed is not None and skill.name not in allowed:
                continue
            if switches.get(skill.name) is False:
                continue
            if skill.default_enabled is False:
                continue
            out.append(skill)
        return out

    def count_call(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def describe(self, cfg: dict[str, Any] | None = None) -> list[Any]:
        return []


class ScriptedLLM:
    """按脚本逐次返回的假模型：脚本项是 ``LLMReply`` / ``(正文, 工具调用)`` / ``Exception``。"""

    def __init__(self, script: list[Any], *, name: str = "scripted", model: str = "scripted-1",
                 supports_tools: bool | None = None) -> None:
        self.name = name
        self.model = model
        self.supports_tools = supports_tools
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self._pos = 0

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None, on_delta: Any = None,
    ) -> LLMReply:
        self.requests.append({"messages": list(messages), "tools": tools, "params": params})
        step = self.script[min(self._pos, len(self.script) - 1)] if self.script else LLMReply()
        self._pos += 1
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, LLMReply):
            reply = step
        else:
            content, tool_calls = step
            reply = LLMReply(
                content=content,
                tool_calls=[call if isinstance(call, ToolCall) else ToolCall(**call) for call in tool_calls],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model=self.model,
                provider=self.name,
            )
        if on_delta is not None and reply.content:
            on_delta(reply.content)
        return reply


class FakeRouter:
    """``LLMRouter`` 的最小实现（主 → 备）。"""

    def __init__(self, *clients: ScriptedLLM) -> None:
        self.clients = list(clients)
        self.primary_calls = 0

    def primary(self) -> ScriptedLLM:
        self.primary_calls += 1
        return self.clients[0]

    def fallback(self) -> ScriptedLLM | None:
        return self.clients[1] if len(self.clients) > 1 else None

    def candidates(self) -> list[ScriptedLLM]:
        return list(self.clients)

    def info(self) -> tuple[str, str, bool]:
        client = self.clients[0]
        return client.name, client.model, True


class FakeKV:
    """内存 KV（``KVStore`` 端口）。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key: str) -> Any | None:
        return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def all(self, prefix: str = "") -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if k.startswith(prefix)}

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class FakeSessionStore:
    """内存会话库（``SessionStore`` 端口）。"""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.messages: dict[str, list[MessageRecord]] = {}
        self.titles: list[str] = []
        self.last_messages: list[str] = []

    def create_session(self, *, user_id: str | None, channel: str, title: str = "") -> SessionRecord:
        session = SessionRecord(id=uuid.uuid4().hex, user_id=user_id, channel=channel, title=title,
                                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
        self.sessions[session.id] = session
        self.messages[session.id] = []
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def touch_session(self, session_id: str, *, title: str | None = None,
                      last_message: str | None = None) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        if title:
            session.title = title
            self.titles.append(title)
        if last_message:
            session.last_message = last_message
            self.last_messages.append(last_message)

    def list_sessions(self, *, user_id: str | None = None, limit: int = 20,
                      offset: int = 0) -> list[SessionRecord]:
        rows = [s for s in self.sessions.values() if user_id is None or s.user_id == user_id]
        return rows[offset:offset + limit]

    def delete_session(self, session_id: str) -> bool:
        self.messages.pop(session_id, None)
        return self.sessions.pop(session_id, None) is not None

    def append_message(self, message: MessageRecord) -> MessageRecord:
        if not message.id:
            message.id = uuid.uuid4().hex
        self.messages.setdefault(message.session_id, []).append(message)
        return message

    def list_messages(self, session_id: str, *, limit: int = 50,
                      before: str | None = None) -> list[MessageRecord]:
        rows = list(self.messages.get(session_id, []))
        if before:
            index = next((i for i, row in enumerate(rows) if row.id == before), len(rows))
            rows = rows[:index]
        return rows[-limit:]

    def count_messages(self, session_id: str) -> int:
        return len(self.messages.get(session_id, []))

    def stats(self) -> dict[str, int]:
        return {"sessions": len(self.sessions), "messages": sum(len(rows) for rows in self.messages.values())}


class FakeMemory:
    """``MemoryService`` 的最小实现（可记录召回与抽取）。"""

    def __init__(self, recall_text: str = "") -> None:
        self.recall_text = recall_text
        self.recalled: list[tuple[str, str]] = []
        self.extracted: list[tuple[str, str, str]] = []

    def recall(self, user_id: str, question: str) -> str:
        self.recalled.append((user_id, question))
        return self.recall_text

    def extract(self, user_id: str, question: str, answer: str) -> list[FactRecord]:
        self.extracted.append((user_id, question, answer))
        return []


class StubAgent:
    """``ChatAgent`` 的替身：只记录入参、返回预设 ``AgentResult``。"""

    def __init__(self, result: Any, *, preference: dict[str, Any] | None = None) -> None:
        self.result = result
        self.preference = preference or {}
        self.runs: list[dict[str, Any]] = []

    def run(self, question: str, history: list[dict[str, str]] | None = None,
            user_ctx: dict[str, Any] | None = None, *, preference: dict[str, Any] | None = None,
            on_event: Any = None) -> Any:
        self.runs.append({"question": question, "history": list(history or []),
                          "user_ctx": dict(user_ctx or {}), "preference": dict(preference or {})})
        return self.result

    def simple_completion(self, prompt: str, *, call_type: str = "utility",
                          params: dict[str, Any] | None = None) -> tuple[str, list[Any]]:
        return "", []

    def effective_preference(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**self.preference, **(override or {})}


# ---------------------------------------------------------------------------
# 假内核模块：rag / memory 可能还在开发中，单测用假模块钉住交互（也保证结果确定）
# ---------------------------------------------------------------------------
def install_module(monkeypatch: Any, dotted: str, attrs: dict[str, Any]) -> types.ModuleType:
    """把 ``dotted`` 换成一个只有 ``attrs`` 的假模块（同时挂到父包属性上，兼容 ``from X import Y``）。"""
    module = types.ModuleType(dotted)
    for key, value in attrs.items():
        setattr(module, key, value)
    parent_name = dotted.rpartition(".")[0]
    parent = sys.modules.get(parent_name)
    if parent is None:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined] - 让 import 把它当成包
        monkeypatch.setitem(sys.modules, parent_name, parent)
    monkeypatch.setitem(sys.modules, dotted, module)
    monkeypatch.setattr(parent, dotted.rpartition(".")[2], module, raising=False)
    return module


def install_fake_grounding(monkeypatch: Any, *, score: float = 0.77, grounded: bool = True) -> None:
    """假 ``rag.grounding.evaluate_grounding``：返回固定的（分数，是否命中）。"""

    class _GroundingResult:
        def __init__(self) -> None:
            self.grounded = grounded
            self.score = score
            self.reason = "test"

    def evaluate_grounding(chunks: list[Any], min_score: float) -> _GroundingResult:
        return _GroundingResult()

    install_module(monkeypatch, "tsukiyohub.kernel.rag.grounding", {"evaluate_grounding": evaluate_grounding})


def install_fake_ground_check(
    monkeypatch: Any, calls: list[dict[str, Any]], *, answer: str | None = None,
    removed: list[str] | None = None,
) -> None:
    """假 ``rag.ground_check.check``：记录调用参数，并可改写返回的答案。"""

    class _CheckResult:
        def __init__(self) -> None:
            self.answer = answer if answer is not None else ""
            self.checked = True
            self.unsupported: list[str] = list(removed or [])
            self.removed = list(removed or [])
            self.error = None

    def check(
        text: str, evidence: list[tuple[int, str]], complete: Any, *, mode: str = "strict",
        question: str = "",
    ) -> _CheckResult:
        calls.append({"answer": text, "evidence": list(evidence), "mode": mode,
                      "question": question, "complete": complete})
        result = _CheckResult()
        result.answer = answer if answer is not None else text
        if complete is not None:
            # 真实实现会逐句问一次模型；这里也调一次，保证「judge 用量」路径被覆盖
            complete("【核对任务】下面这句话是否被资料支持？")
        return result

    install_module(monkeypatch, "tsukiyohub.kernel.rag.ground_check", {"check": check, "CheckResult": _CheckResult})


def install_fake_summary(monkeypatch: Any, text: str = "更早的对话摘要", calls: list[Any] | None = None) -> None:
    """假 ``memory.summary.summarize``（签名与 skills-engineer 确认的一致）。"""

    def summarize(messages: list[dict[str, str]], *, complete: Any, previous: str = "",
                  max_chars: int = 200) -> str:
        if calls is not None:
            calls.append({"messages": list(messages), "previous": previous, "max_chars": max_chars})
        return text

    install_module(monkeypatch, "tsukiyohub.kernel.memory.summary", {"summarize": summarize})


__all__ = [
    "FakeKV", "FakeMemory", "FakeRegistry", "FakeRouter", "FakeSessionStore", "FakeSkill", "ScriptedLLM",
    "StubAgent", "install_fake_ground_check", "install_fake_grounding", "install_fake_summary",
    "install_module", "make_hit", "make_settings",
]