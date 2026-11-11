"""``ConversationService`` 单测：会话解析、历史拼接、摘要压缩、记忆注入、落库、事件转发。

``answer`` 是 async 的（内部把同步内核搬到线程），单测用 ``asyncio.run`` 驱动而不是依赖
pytest-asyncio 的版本行为——这样测试在任何插件组合下都稳定。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_chat_fakes import (  # noqa: E402
    FakeMemory,
    FakeRegistry,
    FakeRouter,
    FakeSessionStore,
    FakeSkill,
    ScriptedLLM,
    StubAgent,
    install_fake_summary,
    install_module,
    make_settings,
)
from tsukiyohub.domain import ChatRequest, Citation, Intent  # noqa: E402
from tsukiyohub.kernel.chat.agent import AgentResult, ChatAgent, UsageRecord  # noqa: E402
from tsukiyohub.kernel.chat.conversation import ConversationService  # noqa: E402
from tsukiyohub.ports.memory import MessageRecord  # noqa: E402


def _result(**overrides: Any) -> AgentResult:
    """一个「正常的知识库回答」结果，测试按需覆盖个别字段。"""
    base: dict[str, Any] = {
        "answer": "年假为每年 5 天。",
        "citations": [Citation(index=1, doc_id="doc-1", title="示例手册")],
        "kb_called": True,
        "hit": True,
        "grounded": True,
        "grounding_score": 0.8,
        "model": "scripted-1",
        "provider": "scripted",
        "latency_ms": 12,
        "tool_calls": [{"name": "knowledge_search", "args": {"query": "年假"}, "ok": True}],
        "usages": [UsageRecord(model="scripted-1", provider="scripted", call_type="chat",
                               prompt_tokens=10, completion_tokens=5, total_tokens=15)],
    }
    base.update(overrides)
    return AgentResult(**base)


def _build(
    *, settings: Any = None, agent: Any = None, sessions: FakeSessionStore | None = None,
    memory: Any = None, hot: Any = None,
) -> tuple[ConversationService, FakeSessionStore, Any]:
    service = ConversationService(
        settings or make_settings(),
        sessions or FakeSessionStore(),
        memory,
        agent or StubAgent(_result()),
        FakeRegistry([FakeSkill()]),
        hot,
    )
    return service, service.sessions, service.agent  # type: ignore[return-value]


def _ask(service: ConversationService, request: ChatRequest, on_event: Any = None) -> Any:
    return asyncio.run(service.answer(request, on_event=on_event))


# ---------------------------------------------------------------------------
# 1. 新会话：建会话 + 落库 + 出参字段
# ---------------------------------------------------------------------------
def test_answer_creates_session_persists_and_fills_response() -> None:
    service, sessions, _agent = _build()

    response = _ask(service, ChatRequest(question="年假有几天", user_id="u1", user_name="张三"))

    assert response.session_id and response.message_id
    assert response.answer == "年假为每年 5 天。"
    assert response.intent is Intent.KNOWLEDGE
    assert response.skills_used == ["knowledge_search"]
    assert response.citations and response.citations[0].doc_id == "doc-1"
    assert response.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert response.grounded is True and response.grounding_score == pytest.approx(0.8)
    assert response.trace_id

    session = sessions.sessions[response.session_id]
    assert session.title == "年假有几天", "新会话标题取首条消息前 30 字"
    assert session.last_message
    messages = sessions.messages[response.session_id]
    assert [message.role for message in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant.citations and assistant.tool_calls
    assert assistant.usage["total_tokens"] == 15
    assert assistant.meta["mode"] == "agent"


# ---------------------------------------------------------------------------
# 2. 复用会话 + 历史拼接
# ---------------------------------------------------------------------------
def test_answer_reuses_session_and_passes_history() -> None:
    sessions = FakeSessionStore()
    session = sessions.create_session(user_id="u1", channel="api", title="旧会话")
    sessions.append_message(MessageRecord(id="m1", session_id=session.id, role="user", content="上一问"))
    sessions.append_message(MessageRecord(id="m2", session_id=session.id, role="assistant", content="上一答"))
    service = ConversationService(make_settings(), sessions, None, StubAgent(_result()), FakeRegistry([]), None)

    response = _ask(service, ChatRequest(question="继续问", session_id=session.id, user_id="u1"))

    assert response.session_id == session.id
    assert service.agent.runs[0]["history"] == [
        {"role": "user", "content": "上一问"}, {"role": "assistant", "content": "上一答"},
    ]
    assert sessions.sessions[session.id].title == "旧会话", "已有标题不该被覆盖"


def test_session_of_other_user_is_not_reused() -> None:
    sessions = FakeSessionStore()
    session = sessions.create_session(user_id="u1", channel="api", title="别人的会话")
    service = ConversationService(make_settings(), sessions, None, StubAgent(_result()), FakeRegistry([]), None)

    response = _ask(service, ChatRequest(question="年假", session_id=session.id, user_id="u2"))

    assert response.session_id != session.id


# ---------------------------------------------------------------------------
# 3. 长对话：窗口外压成摘要；摘要失败退化为截断
# ---------------------------------------------------------------------------
def _seed_long_session(sessions: FakeSessionStore, turns: int = 6) -> Any:
    session = sessions.create_session(user_id="u1", channel="api", title="长对话")
    for index in range(turns):
        sessions.append_message(MessageRecord(id=f"u{index}", session_id=session.id, role="user",
                                              content=f"问题 {index}"))
        sessions.append_message(MessageRecord(id=f"a{index}", session_id=session.id, role="assistant",
                                              content=f"回答 {index}"))
    return session


def test_long_history_is_summarized(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    install_fake_summary(monkeypatch, "更早的对话摘要", calls)
    sessions = FakeSessionStore()
    session = _seed_long_session(sessions)
    settings = make_settings(history_max_turns=2, history_summary_after=2)
    service = ConversationService(settings, sessions, None, StubAgent(_result()), FakeRegistry([]), None)

    response = _ask(service, ChatRequest(question="继续问", session_id=session.id, user_id="u1"))

    run = service.agent.runs[0]
    assert run["user_ctx"]["summary"] == "更早的对话摘要"
    assert len(run["history"]) == 4, "窗口内只保留 history_max_turns 轮"
    assert calls and calls[0]["max_chars"] == 200
    assert response.answer


def test_summary_failure_falls_back_to_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(messages: list[dict[str, str]], *, complete: Any, previous: str = "",
                max_chars: int = 200) -> str:
        raise RuntimeError("摘要模型挂了")

    install_module(monkeypatch, "tsukiyohub.kernel.memory.summary", {"summarize": _broken})
    sessions = FakeSessionStore()
    session = _seed_long_session(sessions)
    settings = make_settings(history_max_turns=2, history_summary_after=2)
    service = ConversationService(settings, sessions, None, StubAgent(_result()), FakeRegistry([]), None)

    response = _ask(service, ChatRequest(question="继续问", session_id=session.id, user_id="u1"))

    run = service.agent.runs[0]
    assert "summary" not in run["user_ctx"], "摘要失败就当没有摘要，不能把异常抛给用户"
    assert len(run["history"]) == 4
    assert response.answer


# ---------------------------------------------------------------------------
# 4. 记忆：召回注入 + 回答后异步抽取
# ---------------------------------------------------------------------------
def test_memory_recall_injected_and_extract_runs_after_answer() -> None:
    memory = FakeMemory(recall_text="用户偏好简洁回答")
    service, _sessions, _agent = _build(settings=make_settings(memory_enabled=True), memory=memory)

    async def scenario() -> Any:
        response = await service.answer(ChatRequest(question="年假有几天", user_id="u1"))
        await asyncio.sleep(0.3)   # 给异步抽取线程一点时间
        return response

    response = asyncio.run(scenario())

    assert service.agent.runs[0]["user_ctx"]["memory"] == "用户偏好简洁回答"
    assert memory.recalled == [("u1", "年假有几天")]
    assert memory.extracted and memory.extracted[0][0] == "u1"
    assert response.answer


def test_memory_disabled_does_not_recall() -> None:
    memory = FakeMemory(recall_text="不该出现")
    service, _sessions, _agent = _build(settings=make_settings(memory_enabled=False), memory=memory)

    _ask(service, ChatRequest(question="年假", user_id="u1"))

    assert memory.recalled == [] and memory.extracted == []


# ---------------------------------------------------------------------------
# 5. 偏好装配 / Agentic 路由 / 空问题 / 事件转发
# ---------------------------------------------------------------------------
def test_request_fields_are_translated_into_preference() -> None:
    service, _sessions, _agent = _build()

    _ask(service, ChatRequest(question="年假", user_id="u1", top_k=5, skills=["knowledge_search"]))

    pref = service.agent.runs[0]["preference"]
    assert pref["retrieval"]["top_k"] == 5
    assert pref["allow_skills"] == ["knowledge_search"]


def test_agentic_request_clarifies_without_calling_agent() -> None:
    service, _sessions, _agent = _build()

    response = _ask(service, ChatRequest(question="那这个呢", user_id="u1", extra={"agentic": True}))

    assert "补充" in response.answer
    assert response.intent is Intent.CLARIFY
    assert service.agent.runs == [], "指代不明时不该调模型"


def test_empty_question_does_not_call_agent() -> None:
    service, _sessions, _agent = _build()

    response = _ask(service, ChatRequest(question="   ", user_id="u1"))

    assert response.answer == ""
    assert response.intent is Intent.UNKNOWN
    assert service.agent.runs == []


def test_on_event_is_relayed_from_worker_thread() -> None:
    """真实 ChatAgent + 脚本模型：事件要能从 asyncio.to_thread 的工作线程传回调用方。"""
    settings = make_settings()
    agent = ChatAgent(settings, FakeRouter(ScriptedLLM([("你好呀", [])])), FakeRegistry([FakeSkill()]))
    service = ConversationService(settings, FakeSessionStore(), None, agent, FakeRegistry([]), None)
    events: list[tuple[str, Any]] = []

    _ask(service, ChatRequest(question="你好", user_id="u1"),
         on_event=lambda kind, value: events.append((kind, value)))

    assert ("delta", "你好呀") in events


def test_debug_payload_only_outside_production() -> None:
    service, _sessions, _agent = _build()

    debug = _ask(service, ChatRequest(question="年假", user_id="u1", debug=True)).debug
    assert debug and debug["mode"] == "agent"

    prod_service = ConversationService(
        make_settings(app_env="prod"), FakeSessionStore(), None, StubAgent(_result()), FakeRegistry([]), None,
    )
    assert _ask(prod_service, ChatRequest(question="年假", user_id="u1", debug=True)).debug is None


def test_chat_intent_when_no_knowledge_used() -> None:
    plain = AgentResult(answer="你好", grounded=False)
    service, _sessions, _agent = _build(agent=StubAgent(plain))

    response = _ask(service, ChatRequest(question="你好", user_id="u1"))

    assert response.intent is Intent.CHAT
    assert response.skills_used == []