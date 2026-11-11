"""记忆守门：不记什么、怎么洗文本（任何写入路径都必须过这里）。

为什么要单独一个模块
--------------------
记忆会被**反复注入提示词**，一旦写进「忽略上面的规则」这类句子，就不是一条数据，而是
一次永久的提示词注入；写进密码、身份证号则是实打实的数据泄露。所以：

* **禁记类别**用正则拦住（健康、家庭感情、宗教政治、薪酬、对他人的评价、账号密码、
  证件银行卡手机号），宁可少记也不能记错；
* **指令特征**单独一套正则（「忽略」「你现在是」「系统提示」……）；
* **文本归一化**去掉 Markdown 与多余空白——记忆是给人看的一行字，不需要结构；
* 所有规则集中在这里，``extract``（模型抽取）与 ``service.add``（显式写入）共用同一套判断，
  不会出现「模型抽取绕过了守门」的漏洞。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 默认单条记忆最大长度（字符）
DEFAULT_MAX_LEN = 200

#: 禁记类别：命中即拒绝，``reason`` 会直接告诉用户为什么
SENSITIVE_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("账号与密码", re.compile(
        r"密码|口令|验证码|账号|帐号|登录名|api[_\-\s]?key|密钥|私钥|secret|token|助记词|授权码",
        re.I,
    )),
    ("证件与银行卡", re.compile(
        r"身份证|证件号|护照号|社保号|公积金号|银行卡|信用卡|卡号|开户行|(?!\d)\d{16,19}(?!\d)"
    )),
    ("个人联系方式", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|(?!\d)\d{15}(?!\d)|(?!\d)\d{17}[\dXx](?!\d)")),
    ("健康", re.compile(r"病|住院|手术|怀孕|孕检|抑郁|焦虑|失眠|体检|吃药|服药|过敏|残疾|心理")),
    ("家庭与感情", re.compile(r"离婚|结婚|恋爱|男朋友|女朋友|男友|女友|老公|老婆|对象|分手|家暴")),
    ("宗教与政治", re.compile(r"宗教|信仰|佛教|基督|伊斯兰|政治|党员|入党")),
    ("薪酬与绩效", re.compile(r"工资|薪资|薪水|月薪|年薪|年终奖|奖金|绩效|涨薪|降薪|股票|期权|房贷|欠款")),
    ("对他人的评价", re.compile(r"(领导|老板|上司|主管|经理|同事|组长).{0,8}(不行|差劲|讨厌|垃圾|傻|蠢|坑|不靠谱|烦)")),
]
#: 兼容旧名字（外部代码可能按这个名字引用）
BLOCKED_CATEGORIES = SENSITIVE_CATEGORIES

#: 指令 / 提示词注入特征：命中即拒绝
INSTRUCTION_RE = re.compile(
    r"忽略|无视|忘记(之前|上面|所有).{0,6}(规则|指令|设定)|系统提示|提示词|prompt|"
    r"你(必须|一定要|只能|要扮演)|扮演|不要遵守|越狱|jailbreak|"
    r"以后(都|一律).{0,4}(回答|回复|输出)|开发者模式|角色设定",
    re.I,
)
_MARKUP_RE = re.compile(r"[#*`>|\[\]{}<>]+")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class Verdict:
    """一次守门判定的结果：``ok=False`` 时 ``reason`` 可原样告诉用户。"""

    ok: bool
    text: str = ""
    reason: str = ""


def normalize(text: str) -> str:
    """去掉换行与 Markdown 符号、压缩空白，再去掉首尾标点。"""
    return _SPACE_RE.sub(" ", _MARKUP_RE.sub("", text or "")).strip(" ，。,.;；:：\t")


def is_sensitive(text: str) -> bool:
    """是否命中禁记类别（密码 / 密钥 / 证件 / 银行卡 / 健康 / 家庭 / 薪酬 / 他人评价等）。"""
    clean = normalize(text)
    if not clean:
        return False
    return any(pattern.search(clean) for _name, pattern in SENSITIVE_CATEGORIES)


def sensitive_reason(text: str) -> str:
    """命中禁记类别时返回类别名，否则空串。"""
    clean = normalize(text)
    for name, pattern in SENSITIVE_CATEGORIES:
        if pattern.search(clean):
            return name
    return ""


def check(
    text: str,
    *,
    max_len: int = DEFAULT_MAX_LEN,
    blocked_words: list[str] | None = None,
    free_text: bool = True,
) -> Verdict:
    """校验一条待写入的记忆。

    ``free_text=False`` 用于「偏好标准句」这类由内核生成的文本（不需要再查指令特征）。
    """
    clean = normalize(text)
    if not clean:
        return Verdict(False, reason="内容为空")
    name = sensitive_reason(clean)
    if name:
        return Verdict(False, reason=f"涉及{name}，这类信息我不会保存")
    for word in blocked_words or []:
        if word and str(word) in clean:
            return Verdict(False, reason="包含不能保存的内容")
    if free_text:
        if INSTRUCTION_RE.search(clean):
            return Verdict(False, reason="看起来像是给我的指令，不能作为记忆保存")
        if len(clean) > max_len:
            return Verdict(False, reason=f"太长了，请控制在 {max_len} 字以内")
    return Verdict(True, text=clean)


__all__ = [
    "BLOCKED_CATEGORIES",
    "DEFAULT_MAX_LEN",
    "INSTRUCTION_RE",
    "SENSITIVE_CATEGORIES",
    "Verdict",
    "check",
    "is_sensitive",
    "normalize",
    "sensitive_reason",
]