"""技能注册表：装配期注册、运行期按开关取用、按名字分发。

为什么不把开关状态放在每个技能对象上
------------------------------------
技能对象是**无状态**的（同一次进程内所有请求共用），而「这一轮允许用哪些技能」是
**每个请求**的事情：接入方可能在后台点停用某个技能、也可能在一次请求里用 ``skills``
字段限定只允许 3 个技能。所以开关以「配置字典 → 注册表里的运行期覆盖」两级解析：

1. 显式 ``set_enabled(name, False)``（后台点停用，进程内立即生效）；
2. 请求里的 ``cfg["tools"][name]``（接入方按请求传）；
3. 技能自己的 ``default_enabled``。

这样注册表本身不需要知道任何业务配置，也不需要落库（落库由接入方的 REST 层负责）。
"""

from __future__ import annotations

import threading
from typing import Any

from tsukiyohub.domain import SkillInfo
from tsukiyohub.kernel.skills.base import SKILL_KINDS, BaseSkill


class SkillRegistry:
    """线程安全的技能容器（注册顺序即解析顺序，便于提示词稳定复现）。"""

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}
        self._calls: dict[str, int] = {}
        self._switches: dict[str, bool] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 注册
    def register(self, skill: BaseSkill) -> BaseSkill:
        """注册（同名覆盖，便于热重载 SKILL.md / MCP 重连）。"""
        if not skill.name:
            raise ValueError("技能必须有 name")
        with self._lock:
            self._skills[skill.name] = skill
        return skill

    def register_many(self, skills: list[BaseSkill]) -> list[BaseSkill]:
        for skill in skills:
            self.register(skill)
        return skills

    def unregister(self, name: str) -> bool:
        with self._lock:
            self._switches.pop(name, None)
            return self._skills.pop(name, None) is not None

    def get(self, name: str) -> BaseSkill | None:
        with self._lock:
            return self._skills.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._skills)

    def all(self) -> list[BaseSkill]:
        """全部注册条目（含 ``expose=False`` 的 SKILL.md 技能），按注册顺序。

        装配期用它遍历并套用默认启停；给模型的工具列表请用 :meth:`enabled`。
        """
        with self._lock:
            return list(self._skills.values())

    def __len__(self) -> int:  # pragma: no cover - 便利方法
        return len(self._skills)

    def __contains__(self, name: object) -> bool:  # pragma: no cover - 便利方法
        return isinstance(name, str) and name in self._skills

    # ------------------------------------------------------------------ 开关
    def set_enabled(self, name: str, enabled: bool) -> bool:
        """设置运行期开关；技能不存在时返回 ``False``（REST 层据此返回 404）。"""
        with self._lock:
            if name not in self._skills:
                return False
            self._switches[name] = bool(enabled)
            return True

    def is_enabled(self, name: str, cfg: dict[str, Any] | None = None) -> bool:
        skill = self.get(name)
        if skill is None:
            return False
        return self._resolve_enabled(skill, cfg)

    def _resolve_enabled(self, skill: BaseSkill, cfg: dict[str, Any] | None) -> bool:
        with self._lock:
            if skill.name in self._switches:
                return self._switches[skill.name]
        switches = (cfg or {}).get("tools") or {}
        value = switches.get(skill.name)
        if value is not None:
            return value is not False
        return skill.default_enabled is not False

    def enabled(self, cfg: dict[str, Any] | None = None, allow: list[str] | None = None) -> list[BaseSkill]:
        """当前可用的技能（交给模型的那一份）。

        * ``cfg``：请求级配置，``cfg["tools"][name] = False`` 可临时停用；
        * ``allow``：本轮白名单（来自 ``ChatRequest.skills``），``None`` = 不限制。

        ``expose=False`` 的技能（SKILL.md）不在这里返回——它们由 ``use_skill`` 按需加载。
        """
        allowed = set(allow) if allow is not None else None
        with self._lock:
            skills = list(self._skills.values())
        out: list[BaseSkill] = []
        for skill in skills:
            if not skill.expose:
                continue
            if allowed is not None and skill.name not in allowed:
                continue
            if self._resolve_enabled(skill, cfg):
                out.append(skill)
        return out

    def specs(self, cfg: dict[str, Any] | None = None, allow: list[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI function 规格列表（直接塞进 ``chat(tools=...)``）。"""
        return [skill.spec for skill in self.enabled(cfg=cfg, allow=allow)]

    def describe(self, cfg: dict[str, Any] | None = None) -> list[SkillInfo]:
        """给前端 / 后台看的完整清单（含 SKILL.md 技能与调用次数）。"""
        with self._lock:
            items = [(skill, self._calls.get(name, 0)) for name, skill in self._skills.items()]
        out: list[SkillInfo] = []
        for skill, calls in items:
            kind = skill.kind if skill.kind in SKILL_KINDS else "tool"
            out.append(
                SkillInfo(
                    name=skill.name,
                    title=skill.title or skill.name,
                    description=skill.description,
                    kind=kind,  # type: ignore[arg-type]
                    enabled=self._resolve_enabled(skill, cfg),
                    source=skill.source,
                    parameters=skill.parameters or {"type": "object", "properties": {}},
                    rules=skill.rules,
                    calls=calls,
                )
            )
        return out

    # ------------------------------------------------------------------ 统计
    def count_call(self, name: str) -> None:
        """记一次调用（用于后台展示「哪个技能真的有用」）。"""
        with self._lock:
            self._calls[name] = self._calls.get(name, 0) + 1

    def calls(self) -> dict[str, int]:
        with self._lock:
            return dict(self._calls)


__all__ = ["SkillRegistry"]