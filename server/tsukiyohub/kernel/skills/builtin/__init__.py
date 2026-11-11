"""内置技能集合：知识库检索、网页读取、联网搜索、计算器。

这些都是「与业务无关」的能力：任何接入方都能直接用，也可以在 ``data/skills/`` 里
用 SKILL.md 叠加自己的技能。构造入口是 :func:`build_builtin_skills`，
容器只需要传 ``Settings`` 与知识库实例。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tsukiyohub.kernel.skills.base import BaseSkill
from tsukiyohub.kernel.skills.builtin.knowledge import KnowledgeTool
from tsukiyohub.kernel.skills.builtin.math import CalculatorTool
from tsukiyohub.kernel.skills.builtin.web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from tsukiyohub.config import Settings


def build_builtin_skills(settings: Settings, knowledge: Any = None) -> list[BaseSkill]:
    """按「有知识库就带知识库工具」的规则创建内置技能列表。

    顺序即注册顺序：知识库在最前（模型更容易优先想到它）。
    """
    skills: list[BaseSkill] = []
    if knowledge is not None:
        skills.append(KnowledgeTool(settings, knowledge))
    skills.append(WebFetchTool(settings))
    skills.append(WebSearchTool(settings))
    skills.append(CalculatorTool())
    return skills


__all__ = [
    "CalculatorTool",
    "KnowledgeTool",
    "WebFetchTool",
    "WebSearchTool",
    "build_builtin_skills",
]