"""回答表达规范：问题形态 → 篇幅区间、说话方式、版式与禁用词。

为什么单独一个模块
------------------
「回答多长、什么语气、用不用 Markdown」如果散落在提示词、前端设置、测评脚本三处，
同一档位会在不同回答里给出完全不同的字数，用户体感是「这个助手不稳定」。
所以这里只放**一份**口径：提示词拼装（:mod:`prompt`）、聊天内核（:mod:`agent`）、
文档都从这里取，改规则只改一处。

与业务无关：没有公司、部门、制度名称，只有「问题形态」与「篇幅档位」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 问题形态（感知 / 规则给出的 ``answer_shape``）
SHAPES: tuple[str, ...] = ("fact", "steps", "compare", "open", "chat")

#: AI 腔：出现即计为「不像人在说话」，也写进生成提示词的禁用清单
BANNED_PHRASES: tuple[str, ...] = (
    "首先", "其次", "综上所述", "总而言之", "总之", "值得注意的是", "需要指出的是", "需要注意的是",
    "希望对你有帮助", "希望对您有帮助", "希望以上信息", "如有疑问请咨询", "如有其他问题",
    "根据参考资料", "根据提供的资料", "作为一个AI", "作为AI助手",
)

#: 正文硬上限（字）：超过就截断在句末并给「详细说说」。群聊场景更短，避免刷屏
HARD_LIMIT: dict[bool, int] = {False: 1500, True: 800}

#: 人设锚点：任何接入方都可以用 ``pref["style"]["voice"]`` 整段替换
VOICE = """你是最懂这些资料的那个人，说话直接、靠谱、有温度：
- 先给结论，再把资料里的条件、数字、例外、办理方式讲全；资料里写了入口或步骤，再给下一步；
- 不确定就明说「这条资料里没写」，不用「可能」「建议咨询相关部门」和稀泥；
- 说人话，不说官话；用户着急时先给资料里写明的、马上能做的那一步。"""

#: 有据可查（防幻觉）：实测的幻觉几乎都是模型「好心补充」的经验、路径和建议
GROUNDING_RULES: tuple[str, ...] = (
    "涉及资料原文的每一句话（规定、数字、时限、入口、联系人）都必须能在资料里找到，并标 [编号]。",
    "资料没写的不补充：不写「一般 / 通常 / 大概 / 实操上」这类经验判断，不补路径、时效、联系方式。",
    "只回答用户问到的：不要主动列「资料里没写 XX」「建议问某部门」之类的补充，也不要举例或推测原因；"
    "确实没有时直接说「资料里没写」。",
    "不写「流程很简单」「补充两点」「也就是说」这类没有信息量的话，要点写完用「**总结**」一句收束。",
    "多份资料说法不一致时，分别写出各自的说法与出处，不替用户选「以哪份为准」，资料写明优先级的除外。",
)


@dataclass(frozen=True)
class LengthRange:
    """一个形态在单聊 / 群聊下的字数区间。"""

    low: int
    high: int


#: (形态, 是否群聊) → 字数区间。知识库回答按「结论 + 要点 + 总结」写，区间比闲聊宽
_LENGTH: dict[tuple[str, bool], LengthRange] = {
    ("fact", False): LengthRange(120, 400), ("fact", True): LengthRange(80, 250),
    ("steps", False): LengthRange(300, 800), ("steps", True): LengthRange(150, 400),
    ("compare", False): LengthRange(300, 800), ("compare", True): LengthRange(150, 400),
    ("open", False): LengthRange(400, 1000), ("open", True): LengthRange(150, 450),
    ("chat", False): LengthRange(2, 120), ("chat", True): LengthRange(2, 80),
}

#: 档位（前端「回答长度」）在类型区间上的缩放系数
_TIER: dict[str, float] = {"short": 0.6, "medium": 1.0, "long": 1.6}

_REF = re.compile(r"\[\d{1,3}(?:·[^\[\]]{1,16})?\]")
_SPACE = re.compile(r"\s+")
_STEPS = re.compile(r"怎么(办|弄|申请|操作|提交|报|开|走|处理)|如何|流程|步骤|手续|去哪(里)?(办|申请|提交)")
_COMPARE = re.compile(r"区别|不同|差别|对比|比较|哪个(好|更)|还是.{1,12}[?？]?$|新旧|变化")
_FACT = re.compile(r"几|多少|多久|多长|是否|能不能|可不可以|可以吗|能吗|有没有|什么时候|哪天|截止|期限|标准是")
_CHAT = re.compile(r"^(你好|您好|嗨|在吗|谢谢|多谢|好的|收到|辛苦|早上好|晚安|哈+)[!！。.~～]*$")
_DETAIL = re.compile(r"详细(说|讲|介绍|一[点些]|点)|展开(说|讲)?|具体(说|讲)(一下|说)?|说细一?点|再多说")


def normalize_shape(shape: str | None) -> str:
    """把外部传来的形态收敛到 :data:`SHAPES`（未知值按 ``open`` 处理，不报错）。"""
    value = {"table": "compare", "smalltalk": "chat"}.get(shape or "", shape or "")
    return value if value in SHAPES else "open"


def length_range(shape: str, group: bool) -> LengthRange:
    return _LENGTH.get((normalize_shape(shape), bool(group)), _LENGTH[("open", bool(group))])


def visible_length(text: str) -> int:
    """用户看到的字数：去掉引用编号与空白（测评口径与视觉一致）。"""
    return len(_SPACE.sub("", _REF.sub("", text or "")))


def banned_hits(text: str) -> list[str]:
    """命中了几条 AI 腔（测评「AI 腔率」用）。"""
    return [p for p in BANNED_PHRASES if p in (text or "")]


def guess_shape(question: str) -> str:
    """没有感知节点时按问题文字猜形态（纯规则，0 次模型调用，宁可猜「open」）。"""
    q = (question or "").strip()
    if not q or _CHAT.match(q):
        return "chat"
    if _COMPARE.search(q):
        return "compare"
    if _STEPS.search(q):
        return "steps"
    if _FACT.search(q) and len(q) <= 40:
        return "fact"
    return "open"


def wants_detail(question: str) -> bool:
    """用户明说「详细说说 / 展开讲讲」→ 直接用硬上限，不受形态区间约束。"""
    return bool(_DETAIL.search(question or ""))


def length_instruction(shape: str, group: bool, length: str, *, detail: bool = False) -> str:
    """生成提示词里的一句篇幅要求。

    * ``shape``：问题形态（见 :data:`SHAPES`）；
    * ``group``：是否群聊（群聊更短）；
    * ``length``：档位 ``short`` / ``medium`` / ``long``；
    * ``detail``：用户要求详细展开时放宽到硬上限。
    """
    if detail:
        return f"用户要求详细说明：完整展开条件、例外和步骤，正文不超过 {HARD_LIMIT[bool(group)]} 字。"
    shape = normalize_shape(shape)
    rng = length_range(shape, group)
    scale = _TIER.get(length or "medium", 1.0)
    # 给硬上限留余量，避免写到一半被截断
    high = min(HARD_LIMIT[bool(group)] * 4 // 5, max(rng.low, int(rng.high * scale)))
    rng = LengthRange(min(rng.low, high), high)
    shape_hint = {
        "fact": "结论 + 2~3 个要点（依据、条件、例外）+ 总结",
        "steps": "结论 + 按步骤分点（最多 7 步）+ 总结",
        "compare": "结论 + 逐项对照的要点（或不超过 4 行的小表格）+ 总结",
        "open": "结论 + 3~6 个要点 + 总结",
        "chat": "一两句话自然回应，不用版式",
    }.get(shape, "结论 + 3~6 个要点 + 总结")
    if group and shape in ("steps", "compare", "open"):
        shape_hint = "结论 + 最多 4 个要点 + 总结，不用表格"
    return (f"知识库问题：{shape_hint}；正文控制在 {rng.low}~{rng.high} 字"
            "（条件、例外、时限、金额不能省略）；与知识库无关的问题按需简洁回答。")


def expression_rules(style: dict[str, Any], shape: str | None, group: bool) -> str:
    """表达层规则（拼进系统提示词）：说话方式 + 禁用清单 + 引用克制 + 版式。

    ``style`` 支持的键（都可缺省）：``voice`` / ``bannedPhrases`` / ``markdown``。
    """
    voice = str(style.get("voice") or "").strip() or VOICE
    extra = [str(p).strip() for p in (style.get("bannedPhrases") or []) if str(p).strip()]
    banned = "、".join(f"「{p}」" for p in dict.fromkeys([*BANNED_PHRASES, *extra]))
    if style.get("markdown", True):
        layout = "只用加粗、编号、短列表；标题最多 1 级；表格最多 3 列；不嵌套列表"
        if group:
            layout += "；群聊里不用表格"
    else:
        layout = "只输出纯文本，不要使用任何 Markdown 语法"
    lines = [
        "## 说话方式",
        voice,
        f"- 不要出现：{banned}；不写无关的免责声明，不说教。",
        "- 引用：涉及资料原文的每一句末尾标 [编号]，同一句不重复同一编号；结尾不写「以上信息来源于知识库」。",
        f"- 版式：{layout}。",
    ]
    return "\n".join(lines)


def grounding_rules() -> str:
    """把 :data:`GROUNDING_RULES` 渲染成提示词段落（有知识库资料时才加）。"""
    return "## 依据要求\n" + "\n".join(f"- {rule}" for rule in GROUNDING_RULES)


__all__ = [
    "BANNED_PHRASES", "GROUNDING_RULES", "HARD_LIMIT", "SHAPES", "VOICE", "LengthRange",
    "banned_hits", "expression_rules", "grounding_rules", "guess_shape", "length_instruction",
    "length_range", "normalize_shape", "visible_length", "wants_detail",
]