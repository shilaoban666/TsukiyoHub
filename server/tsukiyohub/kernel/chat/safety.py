"""轻量安全层：把敏感信息换成占位符再交给模型，输出前再还原 / 拦住新增泄露。

为什么保留在聊天内核里
--------------------
「发给模型的文本里有手机号 / 邮箱 / 身份证 / 银行卡」是任何接入方都会踩的坑，与行业无关。
但完整的风控（自定义规则、逐字暂缓、打码落库）属于运营侧能力，内核只保留最小闭环：

* 输入：命中 → 换成 ``[手机号1]`` 这样的占位符（同一原文始终同一占位符）；
* 输出：模型复述占位符 → 还原成原文（用户看得懂）；模型自己写出新的敏感信息 → 打码 ``***``；
* 拒答：``refuseTopics`` 命中的问题不进模型，直接返回礼貌话术。

**默认关闭**：只有 ``pref["safety"]["enabled"] = True`` 时生效，避免接入方没配置就被改写输入。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

#: 占位符里的类别名（同时用于还原，所以保持简短、不含数字）
BUILTINS: dict[str, str] = {"手机号": "mobile", "邮箱": "email", "身份证号": "idcard", "银行卡号": "bankcard"}

#: 打码后的样子（不可还原）
MASK = "***"

#: 默认拒答话术：不给「因为什么」的解释空间，避免用户反复试探
DEFAULT_REFUSE = "抱歉，这个问题我不方便回答。你可以换一个与知识库内容相关的问题。"

#: 还原占位符用：``[手机号1]`` / ``[邮箱2]``
_PLACEHOLDER = re.compile(r"\[([^\[\]\d]{1,8})(\d{1,3})\]")

#: 内置识别规则（顺序有意义：身份证 18 位要先于银行卡匹配）
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("身份证号", re.compile(
        r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")),
    ("手机号", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    ("银行卡号", re.compile(r"(?<!\d)(?:62|4\d|5[1-5])\d{2}(?:[ -]?\d{4}){2,3}(?:[ -]?\d{1,3})?(?!\d)")),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
)


@dataclass(frozen=True)
class SafetyPolicy:
    """一次请求生效的安全策略（从 ``pref["safety"]`` 解析，缺省全关）。"""

    enabled: bool = False
    refuse_topics: tuple[str, ...] = ()
    refuse_text: str = DEFAULT_REFUSE
    #: 输出里模型自己写出的敏感信息是否打码
    mask_output: bool = True

    @classmethod
    def parse(cls, pref: dict[str, Any] | None) -> SafetyPolicy:
        cfg = (pref or {}).get("safety") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        topics = tuple(str(t).strip() for t in (cfg.get("refuseTopics") or []) if str(t).strip())
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            refuse_topics=topics,
            refuse_text=str(cfg.get("refuseText") or DEFAULT_REFUSE),
            mask_output=bool(cfg.get("maskOutput", True)),
        )


def _placeholder_key(kind: str, value: str) -> str:
    """占位符编号按「类别 + 原文」的哈希稳定分配：同一原文在多轮里恒定，前端不会看到编号乱跳。"""
    return hashlib.sha1(f"{kind}:{value}".encode()).hexdigest()


class MaskSession:
    """一次问答内的脱敏上下文（线程内使用，不共享）。关闭时所有方法都是恒等函数。"""

    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.policy = policy or SafetyPolicy()
        self._to_placeholder: dict[str, str] = {}
        self._to_original: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.hits: dict[str, int] = {}

    # ---------------------------------------------------------------- 输入
    def mask_in(self, text: str) -> str:
        """发给模型的文本：敏感信息 → 占位符（同一原文 → 同一占位符）。"""
        if not self.policy.enabled or not text:
            return text
        out = text
        for kind, pattern in _RULES:
            out = pattern.sub(lambda m, kind=kind: self._mask_one(kind, m.group(0)), out)
        return out

    def _mask_one(self, kind: str, original: str) -> str:
        hit = self._to_placeholder.get(original)
        if hit is not None:
            return hit
        index = self._counters.get(kind, 0) + 1
        self._counters[kind] = index
        placeholder = f"[{kind}{index}]"
        self._to_placeholder[original] = placeholder
        self._to_original[placeholder] = original
        self.hits[kind] = self.hits.get(kind, 0) + 1
        return placeholder

    # ---------------------------------------------------------------- 输出
    def restore_out(self, text: str) -> str:
        """模型输出 → 用户看到的文本：先拦住模型新写的敏感信息，再还原占位符。"""
        if not text or not self.policy.enabled:
            return text
        out = text
        if self.policy.mask_output:
            for kind, pattern in _RULES:
                out = pattern.sub(lambda m, kind=kind: self._mask_new(kind, m.group(0)), out)
        if self._to_original:
            out = _PLACEHOLDER.sub(lambda m: self._to_original.get(m.group(0), m.group(0)), out)
        return out

    def _mask_new(self, kind: str, original: str) -> str:
        # 已经脱敏过、正要还原的不算新泄露（占位符不匹配这些正则，正常不会走到这里）
        if original in self._to_original:
            return original
        self.hits[kind] = self.hits.get(kind, 0) + 1
        return MASK

    def redact(self, text: str) -> str:
        """落库用：所有敏感信息一律 ``***``，不可还原。"""
        if not text:
            return text
        out = text
        for _kind, pattern in _RULES:
            out = pattern.sub(MASK, out)
        return out

    # ---------------------------------------------------------------- 观测
    def flags(self) -> dict[str, Any]:
        """落库 / 调试用的命中摘要（只记类别，不记原文）。"""
        return {"safety": dict(self.hits)} if self.hits else {}

    def reject_reason(self, question: str) -> str | None:
        """问题命中禁止话题时返回话题名（调用方据此拒答，不调模型）。"""
        if not self.policy.enabled or not question:
            return None
        lowered = question.lower()
        for topic in self.policy.refuse_topics:
            if topic.lower() in lowered:
                return topic
        return None


def build_mask(pref: dict[str, Any] | None) -> MaskSession:
    """按偏好建一次脱敏会话（``pref["safety"]["enabled"]`` 为假时是空操作）。"""
    return MaskSession(SafetyPolicy.parse(pref))


__all__ = ["BUILTINS", "DEFAULT_REFUSE", "MASK", "MaskSession", "SafetyPolicy", "build_mask"]