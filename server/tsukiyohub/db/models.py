"""数据库表定义（SQLAlchemy 2.0 声明式）。

只有四张表，全部与业务无关：

======================  ==========================================================
``tsukiyo_session``     会话（一次持续对话的容器）
``tsukiyo_message``     会话里的每条消息（含引用、工具调用、用量）
``tsukiyo_fact``        用户的长期事实记忆
``tsukiyo_kv``          运行期键值配置（人设、技能开关、检索参数）
======================  ==========================================================

约定
----
* **时间是 UTC ISO 字符串**：SQLite / PostgreSQL 都按字符串原样存，排序即时间序，
  跨时区不会因为数据库驱动把 tz 丢掉；毫秒精度足够区分同一秒内的连续消息。
* **JSON 用 SQLAlchemy 通用 ``JSON`` 类型**：SQLite 存 TEXT、PostgreSQL 存 JSON，
  不绑定 ``JSONB``，这样默认的 SQLite 也能开箱即用。
* 不引入 Alembic：启动时 ``create_all`` 建表；表结构变更只在开发期（本项目未发布）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now_iso() -> str:
    """当前 UTC 时间（ISO8601 字符串，微秒精度）。

    为什么用微秒：同一毫秒内连续写入多条消息很常见，``before`` 翻页靠时间比较，
    精度不够会漏项或重复；微秒 + id 兜底足以形成稳定全序。
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Base(DeclarativeBase):
    """所有表的基类（统一命名规约，索引名可控）。"""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class SessionRow(Base):
    """会话。``message_count`` / ``last_message`` 冗余在会话上，列表页不用再聚合消息表。"""

    __tablename__ = "tsukiyo_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="api")
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=utc_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=utc_now_iso, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MessageRow(Base):
    """一条消息。``citations`` / ``tool_calls`` / ``usage`` 原样存 JSON，便于回放与统计。

    ``seq`` 是自增主键，也是**唯一的排序依据**：``created_at`` 由调用方传入时可能只到秒精度
    （同一轮 user/assistant 两条消息会撞时间），靠时间排序会出现「回答排在提问前面」。
    字符串主键 ``id`` 仍然是会话翻页与引用的对外标识，改成唯一索引。
    """

    __tablename__ = "tsukiyo_message"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("tsukiyo_session.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=utc_now_iso)
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_tsukiyo_message_session_seq", "session_id", "seq"),
        Index("ix_tsukiyo_message_session_created", "session_id", "created_at"),
    )


class FactRow(Base):
    """一条长期记忆。同一用户的具体内容唯一（重复写入直接返回已有记录）。"""

    __tablename__ = "tsukiyo_fact"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="fact")
    created_at: Mapped[str] = mapped_column(String(32), default=utc_now_iso)
    last_used_at: Mapped[str] = mapped_column(String(32), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (Index("ix_tsukiyo_fact_user_created", "user_id", "created_at"),)


class KVRow(Base):
    """运行期键值配置（值为任意 JSON）。"""

    __tablename__ = "tsukiyo_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), default=utc_now_iso)


__all__ = ["Base", "FactRow", "KVRow", "MessageRow", "SessionRow", "utc_now_iso"]