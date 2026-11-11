"""技能（工具）协议：模型能调用的一次动作，以及它共享的调用上下文。

为什么这样设计
--------------
* **规格即契约**：一个技能对外只有「名字 + 描述 + JSON Schema 参数 + 执行函数」。
  :attr:`BaseSkill.spec` 直接生成 OpenAI function 规格，任何支持工具调用的前端都能用，
  支持第三方在 ``data/skills/`` 里只写 SKILL.md 就注册新技能（见 :mod:`loader`）。
* **上下文共享一次请求的状态**：``ToolContext`` 在一次问答内是**唯一**的可变载体。
  知识库编号（``hits`` / ``next_index``）必须在多轮工具调用、``prefetch``、引用整理之间保持一致，
  否则模型写的 ``[3]`` 会对不上返回给用户的来源列表。
* **无副作用技能**（计算器、技能加载器）只读上下文；有副作用的技能（联网）把外部来源
  追加到 ``extra``，由聊天内核决定怎么展示——技能内核不直接产出用户可见文案。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from tsukiyohub.domain import ScoredChunk


@dataclass
class ToolContext:
    """一次问答里所有技能共享的上下文。

    字段含义（与聊天内核约定）::

        question    用户本轮问题原文
        user_ctx    接入方传入的用户上下文（user_id / user_name / 自定义字段），技能只读
        cfg         运行期可调配置（``{"tools": {name: bool}, ...}``），技能开关与检索参数在这里
        pref        用户偏好（语气、篇幅等），只读

        hits        全局引用编号 → 检索命中；**唯一**的引用来源，编号一旦分配不得重排
        next_index  下一个可用编号；知识库技能从这里连续递增
        records     每次工具调用的记录（落库 / 前端展示）
        retrieved   检索调试信息（分数、闸门判定），用于排查「为什么没命中」
        kb_called   本轮是否查过知识库（命中率统计）
        timings     各阶段累计耗时（毫秒）
        direct      工具给出的最终回复：非空时聊天内核原样返回，不经模型改写
        history     最近几轮对话（工具可能需要引用前文里的链接/条件）
        extra       各技能自己的附加输出（如网络来源 ``web_sources``），约定键见 docs/skills.md
    """

    question: str
    user_ctx: dict[str, Any]
    cfg: dict[str, Any]
    pref: dict[str, Any]
    hits: dict[int, ScoredChunk] = field(default_factory=dict)
    next_index: int = 1
    records: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    kb_called: bool = False
    timings: dict[str, int] = field(default_factory=dict)
    direct: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add_time(self, key: str, ms: float) -> None:
        """累计某阶段耗时（同一阶段被调用多次时累加，便于拆分服务端开销）。"""
        self.timings[key] = self.timings.get(key, 0) + int(ms)


#: 允许交给模型的技能种类；与 ``domain.SkillInfo.kind`` 保持一致
SKILL_KINDS: tuple[str, ...] = ("tool", "skill", "mcp")


class BaseSkill:
    """一切技能 / 工具 / MCP 包装的基类。

    子类只声明类属性（名字、描述、参数 Schema）并实现 :meth:`call`（可选 :meth:`prefetch`），
    不需要写 ``spec``——避免每个技能手写一遍 OpenAI function 结构而出错。

    ``expose`` 说明
    --------------
    SKILL.md 技能（``kind="skill"``）默认 ``expose=False``：它们的正文由 ``use_skill`` 按需加载，
    不直接作为工具塞给模型（技能多时会让工具列表爆炸、稀释模型注意力）。
    仍在注册表里，所以 REST 能列出、能启停。
    """

    #: 模型看到的函数名（``[a-zA-Z0-9_-]{1,64}``，MCP 工具带 ``mcp__server__tool`` 前缀）
    name: str = ""
    #: 后台 / 前端展示名
    title: str = ""
    #: 给模型看的能力说明（一句话说清「什么时候该用」）
    description: str = ""
    #: 追加到系统提示词「技能使用规则」的说明（可为空）
    rules: str = ""
    #: 参数 JSON Schema（子类覆盖；默认无参数）
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: 未配置开关时的默认状态
    default_enabled: bool = True
    #: tool = 内置工具；skill = SKILL.md 技能；mcp = 外部 MCP server 的工具
    kind: str = "tool"
    #: 来源标识（builtin / skills / mcp / 接入方自定义）
    source: str = "builtin"
    #: 是否把 OpenAI 规格交给模型（见类 docstring）
    expose: bool = True

    @property
    def spec(self) -> dict[str, Any]:
        """OpenAI function 规格（深拷贝，调用方改它不会污染技能定义）。"""
        parameters = copy.deepcopy(self.parameters) if self.parameters else {}
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.title or self.name,
                "parameters": parameters,
            },
        }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """执行技能，返回给模型的文本（不直接返回给用户）。"""
        raise NotImplementedError(f"技能 {self.name or type(self).__name__} 没有实现 call()")

    def prefetch(self, ctx: ToolContext) -> str | None:
        """模型不支持工具调用时的「先查再答」：返回要放进提示词的资料段。

        不适用（如计算器）时返回 ``None``；必须与 :meth:`call` 写同样的 ``ctx`` 字段。
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<{type(self).__name__} name={self.name!r} kind={self.kind!r}>"


#: 供第三方实现自定义技能时引用的导出名
SkillClass = type[BaseSkill]

__all__ = ["BaseSkill", "ToolContext", "SKILL_KINDS", "SkillClass"]