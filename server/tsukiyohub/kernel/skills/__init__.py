"""技能内核：工具协议（base）、注册表（registry）、SKILL.md 发现（loader）、MCP（mcp）、内置技能。

对上层只有两个入口：

* :class:`SkillRegistry` —— 注册 / 启停 / 取工具规格 / 统计；
* :func:`build_builtin_skills` + :func:`build_skill_tools` —— 装配期把内置技能与
  ``data/skills/*/SKILL.md`` 技能都注册进来。

技能与聊天内核的耦合点只有 ``BaseSkill.spec``（OpenAI function 规格）与
``BaseSkill.call(args, ctx)``；换任何支持工具调用的模型/前端都成立。
"""

from __future__ import annotations

from tsukiyohub.kernel.skills.base import SKILL_KINDS, BaseSkill, ToolContext
from tsukiyohub.kernel.skills.builtin import (
    CalculatorTool,
    KnowledgeTool,
    WebFetchTool,
    WebSearchTool,
    build_builtin_skills,
)
from tsukiyohub.kernel.skills.loader import (
    SkillDoc,
    SkillDocTool,
    UseSkillTool,
    build_skill_tools,
    catalog_prompt,
    discover,
    doc_enabled,
)
from tsukiyohub.kernel.skills.mcp import McpRegistry, McpServerConfig, McpTool
from tsukiyohub.kernel.skills.registry import SkillRegistry

__all__ = [
    "BaseSkill",
    "SKILL_KINDS",
    "ToolContext",
    "SkillRegistry",
    "SkillDoc",
    "SkillDocTool",
    "UseSkillTool",
    "build_skill_tools",
    "catalog_prompt",
    "discover",
    "doc_enabled",
    "McpRegistry",
    "McpServerConfig",
    "McpTool",
    "CalculatorTool",
    "KnowledgeTool",
    "WebFetchTool",
    "WebSearchTool",
    "build_builtin_skills",
]