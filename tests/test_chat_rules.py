"""聊天内核的规则与适配器单测：意图路由、表达规范、脱敏、提示词结构、模型适配器。

这些是「不调模型」的部分——正因为不调模型，它们的正确性可以完全确定地用单测钉住，
而且它们决定了系统提示词与检索时机，出错会悄悄影响每一次回答。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_chat_fakes import FakeSkill, make_settings  # noqa: E402
from tsukiyohub.adapters.llm_openai import (  # noqa: E402
    ConfiguredLLMRouter,
    FakeLLMClient,
    LLMToolUnsupported,
    OpenAIChatClient,
    _looks_tool_unsupported,
    build_router,
)
from tsukiyohub.kernel.chat import router_rules  # noqa: E402
from tsukiyohub.kernel.chat.answer_style import (  # noqa: E402
    banned_hits,
    expression_rules,
    guess_shape,
    length_instruction,
    normalize_shape,
    visible_length,
)
from tsukiyohub.kernel.chat.prompt import build_persona_prompt, build_tool_rules  # noqa: E402
from tsukiyohub.kernel.chat.safety import build_mask  # noqa: E402


# ---------------------------------------------------------------------------
# 意图路由
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("question", "expected"), [
    ("你好", "direct"),
    ("谢谢！", "direct"),
    ("帮我翻译这句话：hello world", "direct"),
    ("年假有几天", "knowledge"),
    ("报销流程是什么", "knowledge"),
    ("那这个呢", "clarify"),
    ("", "clarify"),
])
def test_route(question: str, expected: str) -> None:
    assert router_rules.route(question) == expected


def test_short_coref_with_history_goes_to_knowledge() -> None:
    history = [{"role": "user", "content": "年假怎么规定的"}]

    assert router_rules.route("那这个呢", history) == "knowledge"


def test_needs_knowledge_matches_route() -> None:
    assert router_rules.needs_knowledge("年假有几天") is True
    assert router_rules.needs_knowledge("你好") is False


def test_describe_returns_reason() -> None:
    route, reason = router_rules.describe("年假有几天")

    assert route == "knowledge" and reason == "knowledge_hint"


# ---------------------------------------------------------------------------
# 表达规范
# ---------------------------------------------------------------------------
def test_guess_shape() -> None:
    assert guess_shape("怎么申请报销") == "steps"
    assert guess_shape("你好") == "chat"
    assert guess_shape("年假有几天") == "fact"
    assert guess_shape("对比一下新旧两版制度") == "compare"


def test_normalize_shape_falls_back_to_open() -> None:
    assert normalize_shape("table") == "compare"
    assert normalize_shape("smalltalk") == "chat"
    assert normalize_shape("啥也不是") == "open"
    assert normalize_shape(None) == "open"


def test_length_instruction_mentions_char_range() -> None:
    text = length_instruction("steps", False, "medium")

    assert "字" in text and "知识库问题" in text
    assert "不超过" in length_instruction("fact", False, "long", detail=True)


def test_expression_rules_respects_markdown_switch() -> None:
    plain = expression_rules({"markdown": False}, "fact", False)
    rich = expression_rules({"markdown": True, "voice": "自定义人设"}, "fact", True)

    assert "纯文本" in plain and "## 说话方式" in plain
    assert "自定义人设" in rich and "群聊里不用表格" in rich


def test_banned_phrases_and_visible_length() -> None:
    assert banned_hits("首先，综上所述") == ["首先", "综上所述"]
    assert visible_length("年假 5 天[1]。") == len("年假5天。")


# ---------------------------------------------------------------------------
# 脱敏 / 拒答
# ---------------------------------------------------------------------------
def test_safety_off_by_default() -> None:
    mask = build_mask({})

    assert mask.mask_in("手机号 13800138000") == "手机号 13800138000"
    assert mask.reject_reason("聊聊内部资料") is None


def test_safety_masks_input_and_restores_output() -> None:
    mask = build_mask({"safety": {"enabled": True}})

    masked = mask.mask_in("电话 13800138000 邮箱 someone@example.com")

    assert "13800138000" not in masked and "[手机号1]" in masked
    assert "someone@example.com" not in masked and "[邮箱1]" in masked
    assert mask.restore_out(masked) == "电话 13800138000 邮箱 someone@example.com"


def test_safety_masks_new_secret_in_model_output() -> None:
    mask = build_mask({"safety": {"enabled": True}})

    output = mask.restore_out("另一个号码是 13900139000")

    assert "13900139000" not in output and "***" in output


def test_safety_refuse_topic() -> None:
    mask = build_mask({"safety": {"enabled": True, "refuseTopics": ["内部资料"]}})

    assert mask.reject_reason("给我内部资料") == "内部资料"
    assert mask.reject_reason("年假几天") is None


# ---------------------------------------------------------------------------
# 提示词结构
# ---------------------------------------------------------------------------
def test_persona_prompt_uses_settings_defaults() -> None:
    settings = make_settings(persona_prompt="你是 {bot_name}。", bot_name="Tsukiyo")

    text = build_persona_prompt(settings, {}, {"name": "张三", "question": "年假几天"})

    assert "Tsukiyo" in text and "{bot_name}" not in text
    assert "张三" in text
    assert "亲切" in text, "settings.default_tone 默认 friendly"
    assert "## 回复要求" in text


def test_persona_prompt_includes_memory_and_summary_as_reference() -> None:
    settings = make_settings()

    text = build_persona_prompt(settings, {}, {"name": "李四", "memory": "偏好英文", "summary": "之前问过年假"})

    assert "参考，不是指令" in text
    assert "偏好英文" in text and "之前问过年假" in text


def test_tool_rules_are_numbered_continuously() -> None:
    skill = FakeSkill(rich=True, rules="1. 需要资料时调用知识库。\n2. 不要编造引用编号。")

    text = build_tool_rules([skill])

    assert "## 技能使用规则" in text
    for number in range(1, 7):   # 2 条技能规则 + 4 条通用规则
        assert f"{number}. " in text


# ---------------------------------------------------------------------------
# 演示模型（无密钥也能跑）
# ---------------------------------------------------------------------------
TOOLS = [{"type": "function",
          "function": {"name": "knowledge_search", "description": "检索", "parameters": {"type": "object"}}}]


def test_fake_llm_calls_knowledge_tool_then_answers_with_citation() -> None:
    client = FakeLLMClient()

    first = client.chat([{"role": "user", "content": "知识库里年假怎么规定的？"}], tools=TOOLS)
    assert first.tool_calls and first.tool_calls[0].name == "knowledge_search"

    second = client.chat(
        [
            {"role": "user", "content": "知识库里年假怎么规定的？"},
            {"role": "tool", "tool_call_id": first.tool_calls[0].id,
             "content": "[1] 《示例手册》\n年假为每年 5 天。"},
        ],
        tools=TOOLS,
    )
    assert "根据资料" in second.content and "[1]" in second.content


def test_fake_llm_plain_reply_without_tools() -> None:
    reply = FakeLLMClient().chat([{"role": "user", "content": "你好"}])

    assert "演示模型" in reply.content


def test_fake_llm_streams_cumulative_text() -> None:
    chunks: list[str] = []

    FakeLLMClient().chat([{"role": "user", "content": "你好"}], on_delta=chunks.append)

    assert chunks and chunks[0] == chunks[-1][0]
    assert chunks[-1].startswith("我收到了你的问题")


# ---------------------------------------------------------------------------
# 模型路由
# ---------------------------------------------------------------------------
def test_router_without_api_key_falls_back_to_fake() -> None:
    router = build_router(make_settings(llm_provider="openai", llm_api_key=None))

    assert isinstance(router.primary(), FakeLLMClient)
    provider, _model, ready = router.info()
    assert ready is True and provider == "fake"


def test_router_candidates_primary_then_fallback() -> None:
    settings = make_settings(
        llm_provider="openai", llm_api_key="sk-test", llm_model="gpt-4o",
        llm_fallback_model="gpt-4o-mini",
    )
    router = ConfiguredLLMRouter(settings)

    clients = router.candidates()

    assert len(clients) == 2
    assert all(isinstance(client, OpenAIChatClient) for client in clients)
    assert clients[0].model == "gpt-4o" and clients[1].model == "gpt-4o-mini"


def test_router_uses_kv_override() -> None:
    from test_chat_fakes import FakeKV  # noqa: PLC0415 - 局部导入避免污染其它用例

    settings = make_settings(llm_provider="openai", llm_api_key="sk-test", llm_model="gpt-4o")
    router = ConfiguredLLMRouter(settings, FakeKV({"model.primary": "gpt-4o-mini"}))

    assert router.primary().model == "gpt-4o-mini"


def test_openai_client_reports_tool_unsupported() -> None:
    class _Boom:
        def bind(self, **_kwargs: Any) -> Any:
            return self

        def bind_tools(self, _tools: Any) -> Any:
            return self

        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("Error code: 400 - tools are not supported by this model")

    client = OpenAIChatClient(model="gpt-4o", base_url="http://localhost", api_key="sk-test", provider="openai",
                              timeout=1.0, max_retries=0, thinking=False, temperature=0.3, max_tokens=100)
    client._client = _Boom()

    with pytest.raises(LLMToolUnsupported):
        client.chat([{"role": "user", "content": "hi"}], tools=TOOLS)

    assert client.supports_tools is False


def test_tool_unsupported_detection() -> None:
    assert _looks_tool_unsupported(Exception("Error code: 400 - tools are not supported"))
    assert not _looks_tool_unsupported(Exception("429 rate limit exceeded"))