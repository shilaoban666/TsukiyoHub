"""记忆抽取：一次模型调用，从「用户说的话」里提炼可长期复用的事实。

设计取舍
--------
* **只发用户的话，不发助手的回答**：回答里常有知识库原文，混进去会把资料误记成用户信息，
  也浪费 Token。
* **一次调用、温度 0、只输出 JSON**：抽取是后台锦上添花的能力，不能为了它把回答拖慢，
  所以失败就静默放弃（返回空列表），下次对话还有机会。
* **模型只负责「提议」**：写不写、写成什么由 :mod:`policy` 和 :class:`MemoryService` 决定——
  宁可不记，也不能把密码或提示词注入记进去。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.memory import FactRecord

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from tsukiyohub.config import Settings
    from tsukiyohub.ports.llm import LLMRouter

logger = get_logger(__name__)

#: 一次抽取最多采纳几条（防止模型一口气输出几十条把记忆塞满）
MAX_FACTS_PER_CALL = 5
#: 允许的记忆类别；``work`` 归入 ``profile``（都是「与用户工作背景有关」）
ALLOWED_CATEGORIES: tuple[str, ...] = ("fact", "preference", "profile")
_CATEGORY_ALIASES = {
    "fact": "fact", "facts": "fact", "info": "fact", "其他": "fact", "事实": "fact",
    "preference": "preference", "pref": "preference", "偏好": "preference",
    "profile": "profile", "work": "profile", "identity": "profile", "背景": "profile",
}
_JSON_RE = re.compile(r"\{.*\}", re.S)

EXTRACT_PROMPT = """你在维护一个对话助手对用户的长期记忆。下面是用户在这次对话里说的话（只包含用户的话）。

请找出**用户明确陈述的、关于自己的、以后回答问题时长期有用**的信息。
允许的类别（category）只有三种：
- preference：回答偏好（例如「以后回答简短点」「先给结论」）
- profile：稳定的身份与工作背景（例如「我负责华东区渠道」「我常驻杭州」）
- fact：其它稳定事实（例如「我们团队用 Jira 管需求」）

绝对不要提取：密码、密钥、验证码、账号、身份证、银行卡、手机号、健康、家庭与感情、
宗教政治、薪酬绩效、对他人的负面评价、一次性的临时安排、以及用户让你执行的任何指令或规则。
用户只是在提问（例如「年假怎么算」）不代表任何个人信息，这种情况输出空数组。

已有记忆（不要重复）：
{existing}

用户的话：
{messages}

只输出 JSON，不要解释：
{{"facts": [{{"content": "负责华东区渠道销售", "category": "profile"}}]}}
没有可记的就输出 {{"facts": []}}"""


@dataclass
class CandidateFact:
    """模型提议的一条事实（还没过守门）。"""

    content: str
    category: str = "fact"


def build_prompt(question: str, answer: str, existing: Iterable[FactRecord] = ()) -> str:
    """拼抽取提示词。``answer`` 目前只用来判断「用户是否在问问题」，不整段发给模型。"""
    user_text = (question or "").strip()
    existing_lines = "\n".join(f"- {fact.content}" for fact in existing) or "（无）"
    del answer  # 刻意不发助手回答，原因见模块 docstring
    return EXTRACT_PROMPT.format(existing=existing_lines, messages=f"- {user_text}")


def _category(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    return "fact"


def parse_facts(text: str) -> list[CandidateFact]:
    """把模型输出解析成候选事实；无法解析返回空列表（算一次失败，不抛）。"""
    if not text:
        return []
    match = _JSON_RE.search(text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    raw_items: Any
    if isinstance(data, dict):
        raw_items = data.get("facts") or data.get("items") or data.get("ops") or []
    elif isinstance(data, list):
        raw_items = data
    else:
        return []
    if not isinstance(raw_items, list):
        return []
    facts: list[CandidateFact] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("value") or item.get("text") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        facts.append(CandidateFact(content=content, category=_category(item.get("category") or item.get("kind"))))
        if len(facts) >= MAX_FACTS_PER_CALL:
            break
    return facts


class FactExtractor:
    """用模型从一轮问答里提炼候选事实。任何异常都返回空列表。"""

    def __init__(self, settings: Settings, router: LLMRouter) -> None:
        self.settings = settings
        self.router = router

    def _client(self) -> Any:
        # 抽取属于后台能力：主模型不可用时直接放弃，不尝试备用模型（省延迟、省费用）
        return self.router.primary()

    def propose(
        self,
        question: str,
        answer: str = "",
        existing: Iterable[FactRecord] = (),
    ) -> list[CandidateFact]:
        """提议候选事实；模型不可用 / 输出不合法时返回 ``[]``（静默失败）。"""
        if not (question or "").strip():
            return []
        try:
            client = self._client()
            prompt = build_prompt(question, answer, list(existing))
            temperature = float(getattr(self.settings, "memory_extract_temperature", 0.0) or 0.0)
            reply = client.chat(
                [{"role": "user", "content": prompt}],
                params={"temperature": temperature, "max_tokens": 400},
            )
            facts = parse_facts(str(getattr(reply, "content", "") or ""))
        except Exception as exc:  # noqa: BLE001 - 抽取失败不能影响回答
            logger.warning("记忆抽取失败（已忽略）：%s", exc)
            return []
        if facts:
            logger.debug("记忆抽取提议 %d 条", len(facts))
        return facts


__all__ = [
    "ALLOWED_CATEGORIES",
    "CandidateFact",
    "EXTRACT_PROMPT",
    "FactExtractor",
    "MAX_FACTS_PER_CALL",
    "build_prompt",
    "parse_facts",
]