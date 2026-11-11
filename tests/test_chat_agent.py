"""``ChatAgent`` 主路径单测：无工具、工具轮、预取、主备降级、摘录兜底、plain 模式、核对开关。

全部用脚本化模型 + 假技能，不连任何外部服务；断言集中在「产物语义」上：
回答正文、引用编号、事件序列、用量记录、降级原因——这些是前端与 API 真正依赖的部分。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_chat_fakes import (  # noqa: E402
    FakeKV,
    FakeRegistry,
    FakeRouter,
    FakeSkill,
    ScriptedLLM,
    install_fake_ground_check,
    install_fake_grounding,
    install_module,
    make_settings,
)
from tsukiyohub.kernel.chat.agent import ChatAgent  # noqa: E402
from tsukiyohub.ports.llm import LLMUnavailable, ToolCall  # noqa: E402


class LLMToolUnsupported(LLMUnavailable):
    """与 adapters 里同名异常：内核只按类名 / 属性识别，不 import adapters。"""

    tool_unsupported = True


def _agent(
    *clients: ScriptedLLM, settings: Any = None, skills: list[FakeSkill] | None = None,
    kv: FakeKV | None = None,
) -> tuple[ChatAgent, FakeRegistry]:
    registry = FakeRegistry(skills if skills is not None else [FakeSkill()])
    return ChatAgent(settings or make_settings(), FakeRouter(*clients), registry, kv), registry


def _collect(agent: ChatAgent, question: str, **kwargs: Any) -> tuple[Any, list[tuple[str, Any]]]:
    events: list[tuple[str, Any]] = []
    result = agent.run(question, on_event=lambda kind, value: events.append((kind, value)), **kwargs)
    return result, events


# ---------------------------------------------------------------------------
# 1. 直接回答（不查库）
# ---------------------------------------------------------------------------
def test_single_turn_without_tools() -> None:
    llm = ScriptedLLM([("你好，我可以帮你查资料。", [])])
    agent, registry = _agent(llm)

    result, events = _collect(agent, "你好")

    assert result.answer == "你好，我可以帮你查资料。"
    assert result.citations == []
    assert result.kb_called is False and result.hit is False
    assert result.mode == "agent" and result.degraded is False
    assert result.model == "scripted-1" and result.provider == "scripted"
    assert ("delta", "你好，我可以帮你查资料。") in events
    # 寒暄走 direct 路由：不该有检索状态 / 进度
    assert not [kind for kind, _ in events if kind in ("status", "progress")]
    assert registry.calls == {}


# ---------------------------------------------------------------------------
# 2. 工具调用一轮后回答
# ---------------------------------------------------------------------------
def test_tool_call_one_round_then_answer() -> None:
    settings = make_settings(kb_prefetch=False)
    skill = FakeSkill()
    llm = ScriptedLLM([
        ("", [ToolCall(id="call-1", name="knowledge_search", arguments={"query": "年假"})]),
        ("年假为每年 5 天，需提前 3 天申请 [1]。", []),
    ])
    agent, registry = _agent(llm, settings=settings, skills=[skill])

    result, events = _collect(agent, "年假有几天")

    assert result.kb_called is True and result.hit is True
    assert [citation.index for citation in result.citations] == [1]
    assert result.citations[0].doc_id == "doc-1"
    assert result.evidence and "年假为每年 5 天" in result.evidence[1]
    # 工具真的被调用过（不是 prefetch 路径）
    assert skill.calls == [{"query": "年假"}]
    assert registry.calls.get("knowledge_search") == 1
    assert ("status", "searching") in events
    assert any(kind == "tool" and payload["name"] == "knowledge_search" for kind, payload in events)
    # 第二轮请求里带着工具结果，模型据此作答
    second = llm.requests[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "示例手册" in second[-1]["content"]
    assert result.tool_calls and result.tool_calls[0]["name"] == "knowledge_search"


# ---------------------------------------------------------------------------
# 3. kb_prefetch：规则判定要查库时先检索，省掉一轮模型决策
# ---------------------------------------------------------------------------
def test_kb_prefetch_saves_a_model_round() -> None:
    settings = make_settings(kb_prefetch=True)
    skill = FakeSkill()
    llm = ScriptedLLM([("年假为每年 5 天 [1]。", [])])
    agent, _registry = _agent(llm, settings=settings, skills=[skill])

    result, events = _collect(agent, "年假有几天")

    assert len(llm.requests) == 1, "预取后只该调用模型一次"
    assert skill.prefetches == 1 and skill.calls == []
    roles = [message["role"] for message in llm.requests[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert ("status", "searching") in events
    progress = [payload for kind, payload in events if kind == "progress"]
    assert progress and "正在查找" in progress[0]
    assert result.hit is True


def test_kb_prefetch_respects_switch() -> None:
    settings = make_settings(kb_prefetch=False)
    skill = FakeSkill()
    llm = ScriptedLLM([("年假为每年 5 天。", [])])
    agent, _registry = _agent(llm, settings=settings, skills=[skill])

    _collect(agent, "年假有几天")

    assert skill.prefetches == 0
    assert len(llm.requests) == 1


# ---------------------------------------------------------------------------
# 4. 模型降级：主 → 备
# ---------------------------------------------------------------------------
def test_primary_failure_falls_back_to_secondary() -> None:
    primary = ScriptedLLM([RuntimeError("主模型超时")], name="primary", model="m-primary")
    fallback = ScriptedLLM([("备用的回答", [])], name="fallback", model="m-fallback")
    agent, _registry = _agent(primary, fallback, settings=make_settings(kb_prefetch=False))

    result, _events = _collect(agent, "帮我写一段祝福语")

    assert result.answer == "备用的回答"
    assert result.model == "m-fallback" and result.provider == "fallback"
    assert result.degraded is False
    assert str(result.degraded_reason).startswith("fallback:")
    assert len(result.usages) == 2
    assert result.usages[0].success is False and result.usages[0].model == "m-primary"
    assert result.usages[1].success is True


# ---------------------------------------------------------------------------
# 5. 全部失败：直接检索给原文摘录
# ---------------------------------------------------------------------------
def test_all_models_failed_returns_extractive_answer() -> None:
    primary = ScriptedLLM([RuntimeError("boom-1")], name="primary", model="m-primary")
    fallback = ScriptedLLM([RuntimeError("boom-2")], name="fallback", model="m-fallback")
    skill = FakeSkill()
    agent, _registry = _agent(primary, fallback, settings=make_settings(), skills=[skill])

    result, _events = _collect(agent, "年假有几天")

    assert result.degraded is True and result.mode == "degraded"
    assert str(result.degraded_reason).startswith("all_models_failed:")
    assert result.kb_called is True
    assert result.citations, "兜底也要给来源，用户至少能顺着原文看"
    assert result.answer
    assert "年假" in result.answer or "示例手册" in result.answer
    assert [usage.success for usage in result.usages] == [False, False]


# ---------------------------------------------------------------------------
# 6. 模型不支持工具 → plain 模式（先查再答）
# ---------------------------------------------------------------------------
def test_tool_unsupported_falls_back_to_plain_mode() -> None:
    settings = make_settings(kb_prefetch=False)
    skill = FakeSkill()
    llm = ScriptedLLM([
        LLMToolUnsupported("400 Bad Request: tools not supported"),
        ("根据资料：年假为每年 5 天 [1]。", []),
    ])
    agent, _registry = _agent(llm, settings=settings, skills=[skill])

    result, _events = _collect(agent, "年假有几天")

    assert result.mode == "plain"
    assert llm.supports_tools is False, "确认不支持后要回写到客户端，下次直接走 plain"
    assert skill.prefetches == 1
    assert "参考资料使用规则" in llm.requests[1]["messages"][0]["content"]
    assert result.hit is True
    assert len(result.usages) == 1, "失败的工具轮不计用量，只有 plain 的那次"


# ---------------------------------------------------------------------------
# 7. 逐句核对：off 不调用；strict 会改写答案
# ---------------------------------------------------------------------------
def test_answer_judge_off_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    install_fake_ground_check(monkeypatch, calls)
    llm = ScriptedLLM([("年假为每年 5 天 [1]。", [])])
    agent, _registry = _agent(llm, settings=make_settings(kb_prefetch=True, answer_judge="off"))

    result, _events = _collect(agent, "年假有几天")

    assert calls == [], "answer_judge=off 时不该调用核对"
    assert result.answer == "年假为每年 5 天 [1]。"


def test_answer_judge_strict_rewrites_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    install_fake_ground_check(monkeypatch, calls, answer="年假为每年 5 天 [1]。", removed=["无依据的一句。"])
    install_fake_grounding(monkeypatch, score=0.9, grounded=True)
    llm = ScriptedLLM([("年假为每年 5 天 [1]。无依据的一句。", []), ("{}", [])])
    agent, _registry = _agent(llm, settings=make_settings(kb_prefetch=True, answer_judge="strict"))

    result, _events = _collect(agent, "年假有几天")

    assert result.answer == "年假为每年 5 天 [1]。"
    assert len(calls) == 1
    assert calls[0]["mode"] == "strict"
    assert calls[0]["evidence"] and calls[0]["evidence"][0][0] == 1
    assert result.grounding_score == pytest.approx(0.9)
    assert result.grounded is True
    assert [usage.call_type for usage in result.usages] == ["chat", "judge"]


def test_grounding_falls_back_to_vector_score(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("rag 内核不可用")

    install_module(monkeypatch, "tsukiyohub.kernel.rag.grounding", {"evaluate_grounding": _boom})
    llm = ScriptedLLM([("年假为每年 5 天 [1]。", [])])
    agent, _registry = _agent(llm, settings=make_settings(kb_prefetch=True))

    result, _events = _collect(agent, "年假有几天")

    assert result.grounding_score == pytest.approx(0.82)
    assert result.grounded is True


# ---------------------------------------------------------------------------
# 8. 提示词 / 偏好
# ---------------------------------------------------------------------------
def test_system_prompt_contains_persona_profile_memory_rules() -> None:
    settings = make_settings(persona_prompt="你是 {bot_name}，今天是 {current_date}。", bot_name="Tsukiyo")
    skill = FakeSkill(rich=True, rules="1. 需要资料时调用知识库。\n2. 不要编造引用编号。")
    llm = ScriptedLLM([("好的", [])])
    agent, _registry = _agent(llm, settings=settings, skills=[skill])

    prompt = agent.build_system_prompt({}, {"name": "张三", "question": "年假几天", "memory": "偏好简洁回答"})

    assert "Tsukiyo" in prompt and "{current_date}" not in prompt
    assert "张三" in prompt and "偏好简洁回答" in prompt
    assert "技能使用规则" in prompt
    assert "1. " in prompt and "6. " in prompt, "技能规则与通用规则要连续编号"


def test_preference_reads_kv_and_merges_request_override() -> None:
    kv = FakeKV({
        "pref": {"bot_name": "小夜", "tone": "concise", "markdown": False, "fallback_text": "问点别的吧"},
        "retrieval.top_k": 5,
        "skills.disabled": ["web_fetch"],
    })
    llm = ScriptedLLM([("好的", [])])
    agent, _registry = _agent(llm, kv=kv)

    pref = agent.effective_preference()

    assert pref["persona"]["name"] == "小夜"
    assert pref["style"]["tone"] == "concise" and pref["style"]["markdown"] is False
    assert pref["fallback"]["text"] == "问点别的吧"
    assert pref["retrieval"]["top_k"] == 5
    assert agent.harness_config()["tools"]["web_fetch"] is False
    # 请求级覆盖只改传入的那一项，其余沿用 KV
    merged = agent.effective_preference({"style": {"length": "short"}})
    assert merged["style"]["length"] == "short" and merged["style"]["tone"] == "concise"


def test_disabled_skill_is_not_offered_to_model() -> None:
    skill = FakeSkill()
    llm = ScriptedLLM([("年假为每年 5 天。", [])])
    agent, _registry = _agent(llm, settings=make_settings(kb_prefetch=True), skills=[skill])

    _collect(agent, "年假有几天", preference={"tools": {"knowledge_search": False}})

    assert llm.requests[0]["tools"] is None, "技能被停用时不该把规格交给模型"
    assert skill.prefetches == 0 and skill.calls == []


def test_simple_completion_uses_fallback_model() -> None:
    primary = ScriptedLLM([RuntimeError("辅助调用失败")], name="primary", model="m-primary")
    fallback = ScriptedLLM([("抽取结果", [])], name="fallback", model="m-fallback")
    agent, _registry = _agent(primary, fallback)

    text, usages = agent.simple_completion("把这句话抽成事实：我喜欢简洁回答")

    assert text == "抽取结果"
    assert [usage.success for usage in usages] == [False, True]
    assert usages[-1].call_type == "utility"


# ---------------------------------------------------------------------------
# 9. 空问题 / 拒答（安全层默认关闭，打开才生效）
# ---------------------------------------------------------------------------
def test_safety_refuse_short_circuits_model() -> None:
    llm = ScriptedLLM([("不该被调用", [])])
    agent, _registry = _agent(llm)

    result, events = _collect(
        agent, "随便聊聊内部资料",
        preference={"safety": {"enabled": True, "refuseTopics": ["内部资料"]}},
    )

    assert result.mode == "refuse" and result.route == "refuse"
    assert llm.requests == []
    assert ("delta", result.answer) in events


def test_empty_question_returns_fallback_text() -> None:
    llm = ScriptedLLM([("不该被调用", [])])
    agent, _registry = _agent(llm)

    result = agent.run("   ")

    assert result.answer == make_settings().fallback_text
    assert result.mode == "empty"
    assert llm.requests == []