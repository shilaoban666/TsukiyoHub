"""意图规则：纯正则快路径，决定「直接回答 / 查知识库 / 反问澄清」。

为什么用规则而不是先调一次模型
------------------------------
路由在**每一次**问答的入口，调用模型最贵（一次 300~800ms + token）。
而「你好」「谢谢」「帮我翻译这句话」这类问题根本不需要检索，用规则挡住最划算；
规则拿不准的一律倾向「查知识库」——多查一次的代价远小于漏查（用户问制度却拿到寒暄式回答）。

本模块只做判定，不做检索；``direct`` / ``knowledge`` / ``clarify`` 三种结论由
:class:`~tsukiyohub.kernel.chat.agent.ChatAgent` 与 :mod:`agentic` 消费。
与业务无关：词表都是通用疑问词，没有公司/行业名。
"""

from __future__ import annotations

import re
from typing import Literal

#: 路由结论：直接回答 / 查知识库 / 反问澄清
Route = Literal["direct", "knowledge", "clarify"]

#: 纯问候 / 自我介绍 / 致谢（整句匹配，宁漏勿错：「你好，请问年假几天」不是寒暄）
_CHITCHAT = frozenset(
    """你好 您好 在吗 在么 哈喽 hello hi 嗨 早 早上好 下午好 晚上好 晚安
    你是谁 介绍一下你 你叫什么 你能做什么 你会做什么 你有什么功能 你能帮我做什么
    谢谢 多谢 感谢 再见 拜拜 好的 收到 嗯 哦 ok""".split()
)

#: 通用任务（改写 / 生成 / 计算 / 代码）：用户要的是生成能力，不是知识库里的资料
_GENERAL_TASK = re.compile(
    r"翻译|润色|改写|续写|缩写|起个?名字|写一[篇封段个首]|写代码|写个函数|写一个?正则|"
    r"计算|算一下|算算|解方程|代码|正则|SQL|python|java|javascript|typescript|"
    r"笑话|段子|情书|诗|作文|祝福语|周报模板",
    re.I,
)

#: 需要翻资料的问题特征：疑问词 + 文档/制度/流程类名词（后者更可靠）
_KNOWLEDGE_HINT = re.compile(
    r"文档|资料|知识库|手册|规范|制度|流程|办法|政策|条款|规定|标准|要求|细则|说明|指南|"
    r"怎么(办|做|申请|操作|提交|报|走|开|处理)|如何|为什么|为何|多少|多久|多长|几天|"
    r"哪些|哪个|是否|能否|可不可以|能不能|有没有|要不要|需不需要|条件|材料|时限|金额|费用|审批|步骤"
)

#: 指代不明的短追问：「那这个呢」「还有吗」「然后呢」
_COREF = re.compile(r"^(那|这|它|这个|那个|还有|上面|刚才|然后|接着)|呢[?？]?$")

_PUNCT = "？?！!。.，,、~～ \t\n"


def _compact(text: str) -> str:
    """去掉标点与空白后比较，避免「你好！」「你好~」被漏掉。"""
    out = (text or "").strip().lower()
    for ch in _PUNCT:
        out = out.replace(ch, "")
    return out


def is_chitchat(question: str) -> bool:
    """整句就是寒暄 / 自我介绍 / 致谢。"""
    compact = _compact(question)
    return bool(compact) and compact in _CHITCHAT


def describe(question: str, history: list[dict[str, str]] | None = None) -> tuple[Route, str]:
    """返回（路由，原因）。原因只用于日志与调试，不参与逻辑。"""
    q = (question or "").strip()
    if not q:
        return "clarify", "empty"
    if is_chitchat(q):
        return "direct", "chitchat"
    if _GENERAL_TASK.search(q):
        return "direct", "general_task"
    if _COREF.search(q) and len(q) <= 10:
        # 有历史时「那这个呢」多半是接着问上一轮的资料 —— 查库更安全
        return ("knowledge", "coref_with_history") if history else ("clarify", "coref_without_history")
    if _KNOWLEDGE_HINT.search(q):
        return "knowledge", "knowledge_hint"
    if len(q) >= 12:
        return "knowledge", "long_question"
    return "direct", "short_unknown"


def route(question: str, history: list[dict[str, str]] | None = None) -> Route:
    """判定本次问答走哪条路（见 :data:`Route`）。"""
    return describe(question, history)[0]


def needs_knowledge(question: str, history: list[dict[str, str]] | None = None) -> bool:
    """是否值得先查一次知识库（``ChatAgent`` 的 ``kb_prefetch`` 用它省掉一轮模型决策）。"""
    return route(question, history) == "knowledge"


__all__ = ["Route", "describe", "is_chitchat", "needs_knowledge", "route"]