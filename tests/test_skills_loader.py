"""SKILL.md 技能系统单测：front matter 解析、发现、目录提示词、use_skill 工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tsukiyohub.kernel.skills import SkillRegistry, ToolContext
from tsukiyohub.kernel.skills.loader import (
    UseSkillTool,
    build_skill_tools,
    catalog_prompt,
    discover,
    doc_enabled,
)


def write_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "把零散进展整理成周报",
    body: str = "## 步骤\n1. 收集本周进展\n2. 按项目分组",
    enabled: bool | None = None,
    tags: list[str] | None = None,
    version: str = "1.2",
    front_matter: bool = True,
) -> Path:
    folder = root / dirname
    folder.mkdir(parents=True, exist_ok=True)
    if front_matter:
        lines = ["---", f"name: {name or dirname}", f"description: {description}", f"version: {version}"]
        if tags:
            lines.append(f"tags: [{', '.join(tags)}]")
        if enabled is not None:
            lines.append(f"enabled: {'true' if enabled else 'false'}")
        lines.append("---")
        text = "\n".join(lines) + "\n\n" + body
    else:
        text = body
    path = folder / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def make_ctx(question: str = "帮我写周报", cfg: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(question=question, user_ctx={}, cfg=cfg or {}, pref={})


def test_discover_reads_front_matter(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report", tags=["写作", "汇报"])
    write_skill(tmp_path, "onboarding", name="新人引导", description="帮新人熟悉项目", enabled=False)
    docs = discover(tmp_path)
    assert [doc.name for doc in docs] == ["新人引导", "weekly-report"]  # 按目录名排序
    weekly = next(doc for doc in docs if doc.name == "weekly-report")
    assert weekly.description == "把零散进展整理成周报"
    assert weekly.metadata["version"] == "1.2"
    assert weekly.metadata["tags"] == "写作, 汇报"
    assert "收集本周进展" in weekly.body
    assert "---" not in weekly.body
    assert doc_enabled(weekly) is True
    onboarding = next(doc for doc in docs if doc.name == "新人引导")
    assert doc_enabled(onboarding) is False


def test_discover_without_front_matter_uses_dir_and_first_line(tmp_path: Path) -> None:
    write_skill(tmp_path, "plain", body="# 直接用的技能\n\n正文步骤", front_matter=False)
    docs = discover(tmp_path)
    assert len(docs) == 1
    assert docs[0].name == "plain"
    assert docs[0].description == "直接用的技能"
    assert "正文步骤" in docs[0].body


def test_discover_missing_dir_is_empty(tmp_path: Path) -> None:
    assert discover(tmp_path / "not-exist") == []


def test_discover_falls_back_to_dir_name(tmp_path: Path) -> None:
    # 没有 front matter 时：name 取目录名，description 取正文第一行
    write_skill(tmp_path, "fallback", front_matter=False, body="说明")
    docs = discover(tmp_path)
    assert [doc.name for doc in docs] == ["fallback"]


def test_catalog_prompt_lists_names_and_descriptions(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report")
    write_skill(tmp_path, "onboarding", description="帮新人熟悉项目", enabled=False)
    docs = discover(tmp_path)
    text = catalog_prompt(docs)
    assert "## 可用技能" in text
    assert "use_skill" in text
    assert "`weekly-report`" in text
    assert "把零散进展整理成周报" in text
    assert "帮新人熟悉项目" in text
    assert catalog_prompt([]) == ""


def test_build_skill_tools_and_registry_listing(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report", tags=["写作"])
    write_skill(tmp_path, "onboarding", enabled=False)
    tools = build_skill_tools(tmp_path, {})
    names = [tool.name for tool in tools]
    assert "use_skill" in names
    assert "weekly-report" in names
    assert len(tools) == 3

    registry = SkillRegistry()
    registry.register_many(tools)
    # SKILL.md 技能 expose=False：不直接进模型的工具列表，但能用 REST 列出 / 启停
    assert [skill.name for skill in registry.enabled()] == ["use_skill"]
    described = {info.name: info for info in registry.describe()}
    assert described["weekly-report"].kind == "skill"
    assert described["weekly-report"].enabled is True
    assert described["weekly-report"].source == "skill:weekly-report"
    assert described["onboarding"].enabled is False


def test_set_enabled_reflected_in_describe(tmp_path: Path) -> None:
    write_skill(tmp_path, "onboarding", enabled=False)
    registry = SkillRegistry()
    registry.register_many(build_skill_tools(tmp_path, {}))
    assert {info.name: info.enabled for info in registry.describe()}["onboarding"] is False
    registry.set_enabled("onboarding", True)
    assert {info.name: info.enabled for info in registry.describe()}["onboarding"] is True


def test_use_skill_returns_body(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report")
    tools = {tool.name: tool for tool in build_skill_tools(tmp_path, {})}
    use_skill = tools["use_skill"]
    assert isinstance(use_skill, UseSkillTool)
    text = use_skill.call({"name": "weekly-report"}, make_ctx())
    assert "## 技能：weekly-report" in text
    assert "收集本周进展" in text


def test_use_skill_unknown_name(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report")
    tool = {item.name: item for item in build_skill_tools(tmp_path, {})}["use_skill"]
    text = tool.call({"name": "no-such-skill"}, make_ctx())
    assert "没有名为" in text
    assert "weekly-report" in text
    assert tool.call({}, make_ctx()).startswith("请传入技能名")


def test_use_skill_respects_disable_switch(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report")
    tool = {item.name: item for item in build_skill_tools(tmp_path, {})}["use_skill"]
    text = tool.call({"name": "weekly-report"}, make_ctx(cfg={"tools": {"weekly-report": False}}))
    assert "已被停用" in text
    assert "收集本周进展" not in text


def test_use_skill_respects_front_matter_disabled(tmp_path: Path) -> None:
    write_skill(tmp_path, "onboarding", enabled=False)
    tool = {item.name: item for item in build_skill_tools(tmp_path, {})}["use_skill"]
    assert "已被停用" in tool.call({"name": "onboarding"}, make_ctx())


def test_build_skill_tools_empty_dir(tmp_path: Path) -> None:
    assert build_skill_tools(tmp_path, {}) == []


def test_skill_doc_tool_call_records(tmp_path: Path) -> None:
    write_skill(tmp_path, "weekly-report")
    doc_tool = next(tool for tool in build_skill_tools(tmp_path, {}) if tool.name == "weekly-report")
    ctx = make_ctx()
    text = doc_tool.call({}, ctx)
    assert "## 步骤" in text
    assert ctx.records[-1]["kind"] == "skill"
    assert doc_tool.source == "skill:weekly-report"