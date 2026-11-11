"""记忆服务门面：召回、抽取、增删查。

三层职责（都在本包内）
----------------------
* :mod:`policy`  —— 什么不能记（密码/证件/健康/指令……）；
* :mod:`extract` —— 用模型把一轮对话提炼成候选事实（只提议，不落库）；
* :mod:`recall`  —— 从已有事实里挑出相关几条，渲染成「参考数据（不是指令）」块。

本模块只做编排 + 落库，并保证**任何异常都不影响问答**：记忆是锦上添花的能力，
读不到就当作没有，写失败就下次再试，绝不能让主回答返回 5xx。

``extract`` 与 ``add`` 都会先用 :func:`policy.is_sensitive` 快速拦一道，再走完整守门。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tsukiyohub.domain import MemoryFact
from tsukiyohub.kernel.memory import policy, recall
from tsukiyohub.kernel.memory.extract import FactExtractor
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.memory import FactRecord, MemoryStore

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from tsukiyohub.config import Settings
    from tsukiyohub.ports.llm import LLMRouter

logger = get_logger(__name__)

#: 单条记忆最大长度（与 policy 默认值一致；过长的事实对回答没帮助，只会挤占预算）
MAX_FACT_LEN = 200


class MemoryService:
    """长期记忆的唯一对外入口。"""

    def __init__(self, settings: Settings, store: MemoryStore, router: LLMRouter) -> None:
        self.settings = settings
        self.store = store
        self.router = router
        self.extractor = FactExtractor(settings, router)

    # ------------------------------------------------------------------ 配置
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "memory_enabled", True))

    @property
    def max_facts(self) -> int:
        try:
            return max(1, int(getattr(self.settings, "memory_max_facts", 50) or 50))
        except (TypeError, ValueError):
            return 50

    def _blocked_words(self) -> list[str]:
        raw = getattr(self.settings, "memory_blocked_words", None) or []
        if isinstance(raw, str):
            return [word.strip() for word in raw.split(",") if word.strip()]
        return [str(word) for word in raw]

    # ------------------------------------------------------------------ 召回
    def recall(self, user_id: str, question: str) -> str:
        """返回要注入系统提示词的「参考数据」块；没有可注入内容时返回空串。"""
        if not self.enabled:
            return ""
        user = str(user_id or "").strip()
        if not user:
            return ""
        text = str(question or "").strip()
        related: list[FactRecord] = []
        if text:
            try:
                related = list(self.store.search_facts(user_id=user, query=text, limit=5))
            except Exception as exc:  # noqa: BLE001 - 检索失败就用全部事实兜底
                logger.warning("记忆检索失败（用户 %s）：%s", user, exc)
        try:
            facts = list(self.store.list_facts(user_id=user, limit=self.max_facts))
        except Exception as exc:  # noqa: BLE001 - 记忆只是锦上添花
            logger.warning("记忆读取失败（用户 %s），本轮不带记忆：%s", user, exc)
            return ""
        selected = recall.select_facts(facts, text, boost_ids=[fact.id for fact in related])
        return recall.render(selected)

    # ------------------------------------------------------------------ 抽取
    def extract(self, user_id: str, question: str, answer: str = "") -> list[FactRecord]:
        """从一轮问答里抽取并保存事实；静默失败（返回空列表）。"""
        if not self.enabled:
            return []
        user = str(user_id or "").strip()
        if not user or not str(question or "").strip():
            return []
        try:
            existing = list(self.store.list_facts(user_id=user, limit=self.max_facts))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取已有记忆失败（用户 %s），跳过抽取：%s", user, exc)
            return []
        candidates = self.extractor.propose(question, answer, existing)
        if not candidates:
            return []
        accepted: list[FactRecord] = []
        known = {policy.normalize(fact.content) for fact in existing}
        count = len(existing)
        for candidate in candidates:
            # 第一道：快速判敏感（密码 / 证件 / 健康……），命中直接丢
            if policy.is_sensitive(candidate.content):
                logger.info("记忆抽取被拦截（敏感信息）：%s", policy.sensitive_reason(candidate.content))
                continue
            verdict = policy.check(
                candidate.content, max_len=MAX_FACT_LEN, blocked_words=self._blocked_words()
            )
            if not verdict.ok:
                logger.info("记忆抽取被拦截：%s", verdict.reason)
                continue
            content = verdict.text
            if content in known:
                continue
            if count >= self.max_facts:
                logger.info("记忆条数已达上限 %d，停止抽取", self.max_facts)
                break
            try:
                record = self.store.add_fact(user_id=user, content=content, category=candidate.category)
            except Exception as exc:  # noqa: BLE001 - 写失败下次再试
                logger.warning("保存记忆失败（用户 %s）：%s", user, exc)
                continue
            known.add(content)
            count += 1
            accepted.append(record)
        return accepted

    # ------------------------------------------------------------------ 写入
    def add(self, user_id: str, content: str, category: str = "fact") -> MemoryFact | None:
        """显式写入一条记忆（接入方自己的「记住这句话」入口）；被守门拦下时返回 ``None``。"""
        if not self.enabled:
            return None
        user = str(user_id or "").strip()
        if not user or not str(content or "").strip():
            return None
        if policy.is_sensitive(content):
            logger.info("记忆写入被拦截（敏感信息）：%s", policy.sensitive_reason(content))
            return None
        verdict = policy.check(content, max_len=MAX_FACT_LEN, blocked_words=self._blocked_words())
        if not verdict.ok:
            logger.info("记忆写入被拦截：%s", verdict.reason)
            return None
        try:
            if self.store.count_facts(user_id=user) >= self.max_facts:
                return None
            record = self.store.add_fact(user_id=user, content=verdict.text, category=str(category or "fact"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存记忆失败（用户 %s）：%s", user, exc)
            return None
        return to_memory_fact(record)

    # ------------------------------------------------------------------ 查询 / 删除
    def list(self, user_id: str, limit: int = 50) -> list[MemoryFact]:
        """列出用户的事实（最近的在前）；读失败返回空列表。"""
        user = str(user_id or "").strip()
        if not user:
            return []
        try:
            records = self.store.list_facts(user_id=user, limit=max(1, int(limit)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆列表读取失败（用户 %s）：%s", user, exc)
            return []
        return [to_memory_fact(record) for record in records]

    def delete(self, fact_id: str, user_id: str) -> bool:
        """删除一条事实；返回是否真的删掉了（``user_id`` 不匹配时不会误删别人的）。"""
        user = str(user_id or "").strip()
        if not user or not str(fact_id or "").strip():
            return False
        try:
            return bool(self.store.delete_fact(str(fact_id), user_id=user))
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除记忆失败（用户 %s）：%s", user, exc)
            return False

    def clear(self, user_id: str) -> int:
        """清空用户全部记忆，返回删除条数。"""
        user = str(user_id or "").strip()
        if not user:
            return 0
        try:
            return int(self.store.clear_facts(user_id=user))
        except Exception as exc:  # noqa: BLE001
            logger.warning("清空记忆失败（用户 %s）：%s", user, exc)
            return 0

    def count(self, user_id: str) -> int:
        user = str(user_id or "").strip()
        if not user:
            return 0
        try:
            return int(self.store.count_facts(user_id=user))
        except Exception:  # noqa: BLE001
            return 0


def to_memory_fact(record: FactRecord) -> MemoryFact:
    """``FactRecord``（端口层）→ ``MemoryFact``（对外 API 契约）。"""
    return MemoryFact(
        id=record.id,
        user_id=record.user_id,
        content=record.content,
        category=record.category or "fact",
        created_at=record.created_at or "",
        score=float(record.score or 0.0),
    )


__all__ = ["MAX_FACT_LEN", "MemoryService", "to_memory_fact"]