"""SQL 持久化适配器：会话 / 消息 / 长期记忆 / 键值配置（SQLAlchemy 2.0）。

为什么三个端口放在一个模块
--------------------------
它们共享同一个数据库句柄与同一套「行对象 ↔ 端口记录」的转换；拆三个文件只会让
事务边界和字段映射分散。默认 SQLite、生产 PostgreSQL，代码完全一致（见 :mod:`tsukiyohub.db.engine`）。

几个不显然的取舍
----------------
* **时间是 UTC ISO 字符串**（毫秒精度）：同一秒可能有多条消息，纯秒精度会让 ``before``
  翻页出现重复/漏项；
* **``before`` 用消息 id 定位**：先查锚点消息的时间，再取它之前的 ``limit`` 条并按时间正序返回；
  锚点不存在时返回空列表（宁可让调用方少一页，也不返回重复内容）；
* **会话计数冗余在会话行上**：列表页只查一张表；写消息时同步累加，同一事务内保证一致；
* **事实内容按用户去重**：重复写入返回已有记录，避免模型每次抽取都追加同一条。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from tsukiyohub.db.engine import Database, build_database
from tsukiyohub.db.models import FactRow, KVRow, MessageRow, SessionRow, utc_now_iso
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.memory import (
    FactRecord,
    KVStore,
    MemoryStore,
    MessageRecord,
    SessionRecord,
    SessionStore,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from tsukiyohub.config import Settings

logger = get_logger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
SESSION_TITLE_CHARS = 60
LAST_MESSAGE_CHARS = 200


# ---------------------------------------------------------------------------
# 行对象 → 端口记录
# ---------------------------------------------------------------------------
def _session_record(row: SessionRow) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel or "api",
        title=row.title or "",
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
        message_count=int(row.message_count or 0),
        last_message=row.last_message or "",
        meta=dict(row.meta or {}),
    )


def _message_record(row: MessageRow) -> MessageRecord:
    return MessageRecord(
        id=row.id,
        session_id=row.session_id,
        role=row.role or "user",
        content=row.content or "",
        created_at=row.created_at or "",
        citations=list(row.citations or []),
        tool_calls=list(row.tool_calls or []),
        usage=dict(row.usage or {}),
        latency_ms=int(row.latency_ms or 0),
        meta=dict(row.meta or {}),
        seq=int(row.seq or 0),
    )


def _normalize_time(value: str | None) -> str:
    """把调用方给的时间统一成 UTC ISO 字符串（微秒精度）；解析不了就原样保留。"""
    text = str(value or "").strip()
    if not text:
        return utc_now_iso()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _fact_record(row: FactRow) -> FactRecord:
    return FactRecord(
        id=row.id,
        user_id=row.user_id or "",
        content=row.content or "",
        category=row.category or "fact",
        created_at=row.created_at or "",
        last_used_at=row.last_used_at or "",
        score=float(row.score or 0.0),
    )


def _tokens(text: str) -> set[str]:
    """轻量分词：英文按单词、中文按二字片段（不引 jieba，记忆检索够用）。"""
    raw = (text or "").lower()
    result = set(_WORD_RE.findall(raw))
    for run in _CJK_RE.findall(raw):
        if len(run) <= 2:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


# ---------------------------------------------------------------------------
# 会话与消息
# ---------------------------------------------------------------------------
class SqlSessionStore:
    """``SessionStore`` 的 SQLAlchemy 实现。"""

    def __init__(self, database: Database) -> None:
        self.db = database

    # ------------------------------------------------------------------ 会话
    def create_session(self, *, user_id: str | None, channel: str, title: str = "") -> SessionRecord:
        now = utc_now_iso()
        row = SessionRow(
            id=uuid.uuid4().hex,
            user_id=(str(user_id).strip() or None) if user_id else None,
            channel=(channel or "api")[:16],
            title=(title or "")[:200],
            created_at=now,
            updated_at=now,
            message_count=0,
            last_message="",
            meta={},
        )
        with self.db.session() as session:
            session.add(row)
        return _session_record(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        if not session_id:
            return None
        with self.db.session() as session:
            row = session.get(SessionRow, str(session_id))
            return _session_record(row) if row is not None else None

    def touch_session(self, session_id: str, *, title: str | None = None,
                      last_message: str | None = None) -> None:
        with self.db.session() as session:
            row = session.get(SessionRow, str(session_id))
            if row is None:
                return
            row.updated_at = utc_now_iso()
            if title is not None:
                row.title = str(title)[:200]
            if last_message is not None:
                row.last_message = str(last_message)[:LAST_MESSAGE_CHARS]

    def list_sessions(self, *, user_id: str | None = None, limit: int = 20,
                      offset: int = 0) -> list[SessionRecord]:
        statement = select(SessionRow).order_by(SessionRow.updated_at.desc())
        if user_id:
            statement = statement.where(SessionRow.user_id == str(user_id))
        statement = statement.limit(max(1, int(limit))).offset(max(0, int(offset)))
        with self.db.session() as session:
            rows = session.execute(statement).scalars().all()
        return [_session_record(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self.db.session() as session:
            row = session.get(SessionRow, str(session_id))
            if row is None:
                return False
            session.execute(delete(MessageRow).where(MessageRow.session_id == str(session_id)))
            session.delete(row)
        return True

    # ------------------------------------------------------------------ 消息
    def append_message(self, message: MessageRecord) -> MessageRecord:
        created_at = _normalize_time(message.created_at)
        message_id = message.id or uuid.uuid4().hex
        with self.db.session() as session:
            session_row = session.get(SessionRow, str(message.session_id))
            if session_row is None:
                raise ValueError(f"会话不存在：{message.session_id}")
            row = MessageRow(
                id=message_id,
                session_id=str(message.session_id),
                role=(message.role or "user")[:16],
                content=message.content or "",
                created_at=created_at,
                citations=list(message.citations or []),
                tool_calls=list(message.tool_calls or []),
                usage=dict(message.usage or {}),
                latency_ms=int(message.latency_ms or 0),
                meta=dict(message.meta or {}),
            )
            session.add(row)
            # flush 一下才能拿到数据库分配的自增 seq（对外返回时需要）
            session.flush()
            session_row.message_count = int(session_row.message_count or 0) + 1
            session_row.updated_at = created_at
            session_row.last_message = (message.content or "")[:LAST_MESSAGE_CHARS]
            if not session_row.title and row.role == "user" and message.content:
                session_row.title = message.content[:SESSION_TITLE_CHARS]
            result = _message_record(row)
        return result

    def list_messages(self, session_id: str, *, limit: int = 50,
                      before: str | None = None) -> list[MessageRecord]:
        """按写入顺序（``seq`` 升序）返回；``before`` 是消息 id，用于向上翻页。

        为什么不用 ``created_at`` 排序：调用方可能只给到秒精度，同一轮的用户提问与助手回答
        会撞时间，按时间排会出现「回答排在提问前面」。``seq`` 是存储层分配的自增序号，
        只增不减、不受时钟影响，天然给出稳定全序。
        """
        size = max(1, int(limit))
        with self.db.session() as session:
            statement = select(MessageRow).where(MessageRow.session_id == str(session_id))
            if before:
                anchor = session.execute(
                    select(MessageRow.seq).where(
                        MessageRow.id == str(before),
                        MessageRow.session_id == str(session_id),
                    )
                ).first()
                if anchor is None:
                    return []
                statement = statement.where(MessageRow.seq < int(anchor[0]))
            statement = statement.order_by(MessageRow.seq.desc()).limit(size)
            rows = list(session.execute(statement).scalars().all())
        rows.reverse()  # 取的是「最近的 size 条」，返回时改回正序
        return [_message_record(row) for row in rows]

    def count_messages(self, session_id: str) -> int:
        with self.db.session() as session:
            total = session.execute(
                select(func.count()).select_from(MessageRow).where(MessageRow.session_id == str(session_id))
            ).scalar_one()
        return int(total or 0)

    def stats(self) -> dict[str, int]:
        """会话数 / 消息数 / 事实数（键库数量顺带给出，健康检查用）。"""
        with self.db.session() as session:
            sessions = session.execute(select(func.count()).select_from(SessionRow)).scalar_one()
            messages = session.execute(select(func.count()).select_from(MessageRow)).scalar_one()
            facts = session.execute(select(func.count()).select_from(FactRow)).scalar_one()
            entries = session.execute(select(func.count()).select_from(KVRow)).scalar_one()
        return {
            "sessions": int(sessions or 0),
            "messages": int(messages or 0),
            "facts": int(facts or 0),
            "kv": int(entries or 0),
        }


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------
class SqlMemoryStore:
    """``MemoryStore`` 的 SQLAlchemy 实现。"""

    def __init__(self, database: Database) -> None:
        self.db = database

    def add_fact(self, *, user_id: str, content: str, category: str = "fact") -> FactRecord:
        user = str(user_id or "").strip()
        if not user:
            raise ValueError("user_id 不能为空")
        text = str(content or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        with self.db.session() as session:
            existing = session.execute(
                select(FactRow).where(FactRow.user_id == user, FactRow.content == text)
            ).scalars().first()
            if existing is not None:  # 幂等：同一句话不重复存
                return _fact_record(existing)
            row = FactRow(
                id=uuid.uuid4().hex,
                user_id=user,
                content=text,
                category=(category or "fact")[:32],
                created_at=utc_now_iso(),
                last_used_at="",
                score=0.0,
            )
            session.add(row)
            result = _fact_record(row)
        return result

    def list_facts(self, *, user_id: str, limit: int = 50) -> list[FactRecord]:
        with self.db.session() as session:
            rows = session.execute(
                select(FactRow)
                .where(FactRow.user_id == str(user_id))
                .order_by(FactRow.created_at.desc())
                .limit(max(1, int(limit)))
            ).scalars().all()
        return [_fact_record(row) for row in rows]

    def search_facts(self, *, user_id: str, query: str, limit: int = 5) -> list[FactRecord]:
        """按词重叠打分（不引向量库：事实量级最多几十条，精确匹配足够）。"""
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        rows = self.list_facts(user_id=user_id, limit=200)
        scored: list[tuple[float, FactRecord]] = []
        for record in rows:
            content_tokens = _tokens(record.content)
            if not content_tokens:
                continue
            overlap = len(query_tokens & content_tokens) / len(content_tokens)
            if overlap > 0:
                scored.append((overlap, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _score, record in scored[:max(1, int(limit))]]

    def delete_fact(self, fact_id: str, *, user_id: str) -> bool:
        with self.db.session() as session:
            row = session.get(FactRow, str(fact_id))
            if row is None or row.user_id != str(user_id):
                return False
            session.delete(row)
        return True

    def clear_facts(self, *, user_id: str) -> int:
        with self.db.session() as session:
            count = session.execute(delete(FactRow).where(FactRow.user_id == str(user_id))).rowcount or 0
        return int(count)

    def count_facts(self, *, user_id: str) -> int:
        with self.db.session() as session:
            total = session.execute(
                select(func.count()).select_from(FactRow).where(FactRow.user_id == str(user_id))
            ).scalar_one()
        return int(total or 0)


# ---------------------------------------------------------------------------
# 键值配置
# ---------------------------------------------------------------------------
class SqlKVStore:
    """``KVStore`` 的 SQLAlchemy 实现：值是任意 JSON。

    约定键（与 API / 聊天内核共享）：

    * ``pref`` → dict（人设、语气、篇幅、记忆开关……）；
    * ``retrieval.top_k`` → int；
    * ``skills.disabled`` → list[str]（停用的技能名）。
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    def get(self, key: str) -> Any | None:
        with self.db.session() as session:
            row = session.get(KVRow, str(key))
            return None if row is None else row.value

    def set(self, key: str, value: Any) -> None:
        now = utc_now_iso()
        with self.db.session() as session:
            row = session.get(KVRow, str(key))
            if row is None:
                session.add(KVRow(key=str(key)[:128], value=value, updated_at=now))
            else:
                row.value = value
                row.updated_at = now

    def all(self, prefix: str = "") -> dict[str, Any]:
        statement = select(KVRow).order_by(KVRow.key)
        if prefix:
            statement = statement.where(KVRow.key.startswith(str(prefix)))
        with self.db.session() as session:
            rows = session.execute(statement).scalars().all()
        return {row.key: row.value for row in rows}

    def delete(self, key: str) -> None:
        with self.db.session() as session:
            row = session.get(KVRow, str(key))
            if row is not None:
                session.delete(row)


def build_stores(settings: Settings) -> tuple[Database, SessionStore, MemoryStore, KVStore]:
    """按配置建库建表并返回三个端口的实现（供 ``container`` 装配）。"""
    database = build_database(settings)
    return (
        database,
        SqlSessionStore(database),
        SqlMemoryStore(database),
        SqlKVStore(database),
    )


__all__ = [
    "Database",
    "SqlKVStore",
    "SqlMemoryStore",
    "SqlSessionStore",
    "build_database",
    "build_stores",
]