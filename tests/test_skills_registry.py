"""技能注册表与工具协议的单测：注册、启停优先级、规格生成、统计。"""

from __future__ import annotations

from typing import Any

from tsukiyohub.kernel.skills import BaseSkill, SkillRegistry, ToolContext
from tsukiyohub.kernel.skills.builtin import CalculatorTool


class EchoSkill(BaseSkill):
    name = "echo"
    title = "回声"
    description = "把输入原样返回"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要回显的文字"}},
        "required": ["text"],
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.records.append({"tool": self.name, "args": args})
        return str(args.get("text") or "")


class OffByDefaultSkill(BaseSkill):
    name = "off_tool"
    title = "默认停用"
    description = "默认关闭的技能"
    default_enabled = False

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:  # pragma: no cover - 不调用
        return ""


class HiddenSkill(BaseSkill):
    """expose=False：只登记、不交给模型（模拟 SKILL.md 技能）。"""

    name = "hidden_skill"
    title = "隐藏技能"
    description = "不直接给模型"
    kind = "skill"
    expose = False

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:  # pragma: no cover - 不调用
        return "body"


def make_ctx(cfg: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(question="测试问题", user_ctx={}, cfg=cfg or {}, pref={})


def test_spec_is_openai_function_format() -> None:
    spec = EchoSkill().spec
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "echo"
    assert spec["function"]["description"] == "把输入原样返回"
    assert spec["function"]["parameters"]["properties"]["text"]["type"] == "string"
    assert spec["function"]["parameters"]["required"] == ["text"]


def test_spec_is_deepcopied() -> None:
    skill = EchoSkill()
    spec = skill.spec
    spec["function"]["parameters"]["properties"]["text"]["type"] = "integer"
    assert skill.spec["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_skill_without_parameters_gets_empty_schema() -> None:
    class NoArgs(BaseSkill):
        name = "noargs"
        description = "无参数"

    spec = NoArgs().spec
    assert spec["function"]["parameters"] == {"type": "object", "properties": {}}


def test_register_get_all_and_unregister() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(OffByDefaultSkill())
    assert registry.get("echo") is not None
    assert registry.get("missing") is None
    assert registry.names() == ["echo", "off_tool"]
    assert [skill.name for skill in registry.all()] == ["echo", "off_tool"]
    assert registry.unregister("echo") is True
    assert registry.unregister("echo") is False
    assert "echo" not in registry


def test_register_requires_name() -> None:
    class Nameless(BaseSkill):
        pass

    registry = SkillRegistry()
    try:
        registry.register(Nameless())
    except ValueError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover - 理论上走不到
        raise AssertionError("没有 name 的技能应当被拒绝")


def test_enabled_respects_default_and_cfg_and_allow() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(OffByDefaultSkill())
    assert [skill.name for skill in registry.enabled()] == ["echo"]
    assert [skill.name for skill in registry.enabled(cfg={"tools": {"off_tool": True}})] == ["echo", "off_tool"]
    assert [skill.name for skill in registry.enabled(cfg={"tools": {"echo": False}})] == []
    assert [skill.name for skill in registry.enabled(allow=["off_tool"], cfg={"tools": {"off_tool": True}})] == [
        "off_tool"
    ]


def test_set_enabled_beats_request_cfg() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    assert registry.set_enabled("echo", False) is True
    assert registry.enabled(cfg={"tools": {"echo": True}}) == []
    assert registry.set_enabled("echo", True) is True
    assert [skill.name for skill in registry.enabled(cfg={"tools": {"echo": False}})] == ["echo"]
    assert registry.set_enabled("missing", False) is False


def test_hidden_skill_not_exposed_but_listed() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(HiddenSkill())
    assert [skill.name for skill in registry.enabled()] == ["echo"]
    assert {skill.name for skill in registry.all()} == {"echo", "hidden_skill"}
    described = {info.name: info for info in registry.describe()}
    assert described["hidden_skill"].kind == "skill"
    assert described["hidden_skill"].enabled is True


def test_describe_fields_and_calls() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(CalculatorTool())
    registry.count_call("echo")
    registry.count_call("echo")
    described = {info.name: info for info in registry.describe()}
    assert described["echo"].title == "回声"
    assert described["echo"].source == "builtin"
    assert described["echo"].kind == "tool"
    assert described["echo"].calls == 2
    assert described["echo"].parameters["properties"]["text"]["type"] == "string"
    assert described["calculator"].calls == 0
    assert described["calculator"].enabled is True
    assert registry.calls() == {"echo": 2}


def test_specs_only_include_enabled() -> None:
    registry = SkillRegistry()
    registry.register(EchoSkill())
    registry.register(OffByDefaultSkill())
    names = [spec["function"]["name"] for spec in registry.specs()]
    assert names == ["echo"]
    assert [spec["function"]["name"] for spec in registry.specs(cfg={"tools": {"off_tool": True}})] == [
        "echo",
        "off_tool",
    ]


def test_tool_context_defaults_and_add_time() -> None:
    ctx = make_ctx()
    assert ctx.hits == {}
    assert ctx.next_index == 1
    assert ctx.records == []
    assert ctx.retrieved == []
    assert ctx.kb_called is False
    assert ctx.timings == {}
    assert ctx.direct == ""
    assert ctx.history == []
    assert ctx.extra == {}
    ctx.add_time("retrieve", 12.0)
    ctx.add_time("retrieve", 8.0)
    assert ctx.timings["retrieve"] == 20
    ctx.add_time("retrieve", 0.6)  # 毫秒会取整累加
    assert ctx.timings["retrieve"] == 20


def test_skill_call_can_use_context() -> None:
    ctx = make_ctx()
    assert EchoSkill().call({"text": "你好"}, ctx) == "你好"
    assert ctx.records == [{"tool": "echo", "args": {"text": "你好"}}]
    assert EchoSkill().prefetch(ctx) is None