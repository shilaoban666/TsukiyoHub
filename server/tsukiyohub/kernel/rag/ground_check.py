"""回答核对（防幻觉最后一道关）：逐句核对回答是否被资料支持，删掉资料不支持的句子。

实测幻觉几乎都是「好心补充」：资料没写的办理路径、时效、经验判断、责任分工。
生成提示词已经禁止（:mod:`tsukiyohub.kernel.rag.prompt` 的 ``GROUNDING_RULES``），
这里再用一次温度 0 的模型核对兜底：

* 触发：``strict`` / ``lenient`` 有资料且回答引用了资料就核对；``auto`` 只在发现回答里
  有资料外的数字时核对；``off`` 不核对；
* 处理：``strict`` 把资料不支持的句子**直接删除**（比重写快，也不会引入新错误），
  ``lenient`` 只记录不删（灰度观察用）；删完没有剩下有引用的内容时保留原答案并标记
  ``usable=False``，由调用方决定是否重写；
* 失败 / 超时 / 解析不了都不影响回答：按原答案返回，``error`` 记录原因。

为什么把 ``complete`` 当参数注入
--------------------------------
内核不认识任何模型客户端。调用方传 ``complete(prompt) -> str``（一次温度 0 的模型调用，
自己负责超时与记账），这个模块就是纯算法、无全局状态、可单测（测试里传假函数即可）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

#: 核对提示词（只输出 JSON；证据截断到 EVIDENCE_CHARS 以免超上下文）
CHECK = """【核对任务】你是严格的事实核对员。逐句检查「回答」能否在「参考资料」原文中找到依据，以下句子都要列出来：
1. 资料里找不到的规定、数字、时限、入口、责任人、约束条件；
2. 资料之外的举例、推测、经验判断、建议（如「一般当天就能」「建议先问一下」「通常由某部门负责」）；
3. 回答主体之外附加的「资料里没写 XX」「XX 可以问某部门」之类的补充说明（整条回答都在说资料没有时除外）；
4. 「流程很简单」「补充两点」这类没有信息量的句子；
5. 把资料没写的适用范围、条件说死的句子（资料只列了各档标准，回答却写「只适用于」「一律」「所有人」）。
按资料列出的名单 / 条件直接推出的结论不算无依据（资料列了一线城市名单、没有成都，回答说「成都按其他标准」）。
开头的一句话结论和结尾的「总结」句，只是概括回答里其他有依据的内容、没有新的事实时，不算无依据。
只输出 JSON：
{{"unsupported": [{{"sentence": "回答中的原句", "reason": "≤20字"}}], "ok": true|false}}

参考资料：
{evidence}

回答：
{answer}"""

#: 句子切分：句末标点、换行（列表项各算一句）
_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n")
_REF = re.compile(r"\[\d{1,3}\]")
_JSON = re.compile(r"\{.*\}", re.S)
_SENTENCE_FIELD = re.compile(r'"sentence"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EMPTY = re.compile(r'"unsupported"\s*:\s*\[\s*\]')
#: 编号 / 列表项以「**小标题**」开头
_ITEM_HEAD = re.compile(r"^\s*(?:\d+[.、]|[-•*])\s*\*\*")
_ORDERED = re.compile(r"^(\s*)\d+([.、]\s*)")
_MARK = re.compile(r"^[\s>*\-•\d.、)）]+|[*_`]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: 原句匹配：模型复述原句时常有出入（去掉编号、改个标点），相似度达到这个值就认定是同一句
MATCH = 0.72
#: 证据单段上限（字符）
EVIDENCE_CHARS = 600


@dataclass
class CheckResult:
    """核对结果。

    Attributes:
        answer: 处理后的答案（未核对 / 核对通过时与原答案一致）。
        checked: 是否真的核对过（没触发或失败时为 ``False``）。
        unsupported: 核对模型判定为无依据的句子。
        removed: 实际从答案里删掉的句子。
        error: 核对没做成的原因（``TimeoutError`` / ``unparsable`` …）；成功为 ``None``。
        usable: 删完后是否还剩有引用的实质内容；``False`` 时调用方应重写或保留原答案。
    """

    answer: str
    checked: bool = False
    unsupported: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    error: str | None = None
    usable: bool = True

    @property
    def changed(self) -> bool:
        """答案是否被改动过。"""
        return any(self.removed)


def numbers_of(text: str) -> set[str]:
    """文本里出现的数字（忽略 ``0`` / ``1`` 这类无信息量的序号）。

    用于 ``auto`` 模式：回答里出现了资料和问题都没有的数字 → 很可能在编数字。
    """
    return {match.group(0) for match in _NUMBER.finditer(text or "")} - {"0", "1"}


def should_check(mode: str, answer: str, evidence: list[tuple[int, str]], question: str = "") -> bool:
    """是否需要核对：``strict`` / ``lenient`` 有资料且回答引用了资料就核对；``auto`` 还要发现资料外的数字。"""
    if mode == "off" or not evidence or not _REF.search(answer or ""):
        return False
    if mode in ("strict", "lenient"):
        return True
    allowed = numbers_of(question)
    for _index, text in evidence:
        allowed |= numbers_of(text)
    return bool(numbers_of(answer) - allowed)


def build_prompt(answer: str, evidence: list[tuple[int, str]]) -> str:
    """拼核对提示词（证据按证据块顺序编号）。"""
    refs = "\n".join(f"[{index}] {text[:EVIDENCE_CHARS]}" for index, text in evidence)
    return CHECK.format(evidence=refs, answer=answer)


def parse(text: str) -> list[str] | None:
    """核对输出 → 资料不支持的句子；解析不了返回 ``None``（当作没核对）。

    输出被截断或夹带说明文字时按 ``"sentence": "…"`` 逐条捞回；
    明确写了 ``"unsupported": []`` 视为全部通过。
    """
    raw = text or ""
    match = _JSON.search(raw)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict):
            found = [str(item.get("sentence") or "").strip() for item in data.get("unsupported") or []
                     if isinstance(item, dict)]
            return [sentence for sentence in found if sentence]
    found = [sentence.strip() for sentence in _SENTENCE_FIELD.findall(raw) if sentence.strip()]
    if found:
        return found
    return [] if _EMPTY.search(raw) else None


def drop(answer: str, unsupported: list[str]) -> tuple[str, list[str]]:
    """删掉与 ``unsupported`` 对得上的句子（按相似度认原句）；返回（新答案，被删的原句）。"""
    if not unsupported:
        return answer, []
    targets = [_plain(sentence) for sentence in unsupported if _plain(sentence)]
    removed: list[str] = []
    lines_out: list[str] = []
    first = _first(answer)
    for line in answer.split("\n"):
        if _framing(line, first):  # 版式的结论行 / 总结行只是概括，不删
            lines_out.append(line)
            continue
        parts = [part for part in _SPLIT.split(line) if part]
        kept: list[str] = []
        for part in parts:
            body = _plain(part)
            if body and any(_similar(body, target) for target in targets):
                removed.append(part.strip())
            else:
                kept.append(part)
        text = "".join(kept)
        headed = bool(_ITEM_HEAD.match(line)) and bool(parts) and bool(kept) and kept[0] is not parts[0]
        if parts and (not _plain(text) or headed):  # 整项都删了、或删的是「**小标题**：」那句：整项去掉
            removed += [item.strip() for item in kept] if headed else []
            continue
        lines_out.append(text)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines_out)).strip()
    return _renumber_list(out), removed


def apply(answer: str, verdict: list[str] | None) -> CheckResult:
    """按核对结论处理回答（``None`` 表示没核对成，原样返回）。"""
    if verdict is None:
        return CheckResult(answer=answer)
    framing = [_plain(line) for line in answer.split("\n") if _framing(line, _first(answer))]
    verdict = [item for item in verdict if not any(_similar(_plain(item), frame) for frame in framing if frame)]
    if not verdict:
        return CheckResult(answer=answer, checked=True)
    cleaned, removed = drop(answer, verdict)
    # 删完必须还剩「有引用的实质内容」，否则宁可保留原答案（避免把答案削成一句空话）
    usable = bool(removed) and bool(_REF.search(cleaned)) and len(_plain(cleaned)) >= 10
    return CheckResult(
        answer=cleaned if usable else answer,
        checked=True,
        unsupported=verdict,
        removed=removed,
        usable=usable,
    )


def check(
    answer: str,
    evidence: list[tuple[int, str]],
    complete: Callable[[str], str],
    *,
    mode: str = "strict",
    question: str = "",
) -> CheckResult:
    """逐句核对并按结论删句。

    Args:
        answer: 待核对的回答（应包含 ``[n]`` 引用）。
        evidence: ``[(编号, 片段原文), ...]``，编号必须与回答里的 ``[n]`` 一致。
        complete: ``complete(prompt) -> str``，一次温度 0 的模型调用；抛异常视为核对失败。
        mode: ``strict`` 删掉无依据句；``lenient`` 只记录不删；``auto`` 仅发现资料外数字时核对；``off`` 不核对。
        question: 用户问题原始文本（``auto`` 模式用它的数字集合做白名单）。

    Returns:
        :class:`CheckResult`；核对失败时 ``answer`` 是原答案、``error`` 说明原因。
    """
    if not should_check(mode, answer, evidence, question):
        return CheckResult(answer=answer)
    try:
        verdict = parse(complete(build_prompt(answer, evidence)))
    except Exception as exc:  # noqa: BLE001 - 核对失败不影响回答
        return CheckResult(answer=answer, error=type(exc).__name__)
    if verdict is None:
        return CheckResult(answer=answer, error="unparsable")
    if mode == "lenient":
        # 宽松模式只做审计：把可疑句列出来，但绝不改动答案（接入方用它做灰度观察）
        return CheckResult(answer=answer, checked=True, unsupported=verdict)
    return apply(answer, verdict)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _plain(text: str) -> str:
    return _MARK.sub("", _REF.sub("", text)).strip(" ：:，,。")


def _first(answer: str) -> str:
    return next((line for line in answer.split("\n") if line.strip()), "")


def _framing(line: str, first: str) -> bool:
    """固定版式的开头「**一句话结论**」与结尾「**总结**：」：只是概括上面有依据的要点，不删。"""
    text = line.strip()
    return text.startswith("**总结") or (text == first.strip() and text.startswith("**"))


def _similar(left: str, right: str) -> bool:
    if left in right or right in left:
        return min(len(left), len(right)) >= 6
    return SequenceMatcher(None, left, right).ratio() >= MATCH


def _renumber_list(text: str) -> str:
    """删掉列表项后把编号重新排成 1、2、3（空行或非列表行之间的同级编号算一组）。"""
    out: list[str] = []
    number = 0
    for line in text.split("\n"):
        match = _ORDERED.match(line)
        if match:
            number += 1
            line = f"{match.group(1)}{number}{match.group(2)}{line[match.end():]}"
        elif line.strip() and not line.startswith((" ", "\t")):
            number = 0
        out.append(line)
    return "\n".join(out)


__all__ = [
    "CHECK", "EVIDENCE_CHARS", "MATCH", "CheckResult", "apply", "build_prompt", "check",
    "drop", "numbers_of", "parse", "should_check",
]