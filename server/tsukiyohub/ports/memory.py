"""持久化端口：会话 / 消息 / 长期记忆 / 运行期键值配置。

默认实现是 SQLAlchemy（SQLite 开箱即用，PostgreSQL 生产），见 ``tsukiyohub.adapters.store_sql``。
接入方也可以换成自己的数据库——内核只依赖这里的协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SessionRecord:
    id: str
    user_id: str | None = None
    channel: str = "api"
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    last_message: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageRecord:
    id: str
    session_id: str
    role: str
    content: str
    created_at: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    #: 单调递增的写入序号（存储层分配）：同一秒内的两条消息也能稳定排序 / 翻页
    seq: int = 0


@dataclass
class FactRecord:
    id: str
    user_id: str = ""
    content: str = ""
    category: str = "fact"
    created_at: str = ""
    last_used_at: str = ""
    score: float = 0.0


@runtime_checkable
class SessionStore(Protocol):
    """会话与消息。所有时间均为 ISO8601 字符串（UTC）。"""

    def create_session(self, *, user_id: str | None, channel: str, title: str = "") -> SessionRecord: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def touch_session(self, session_id: str, *, title: str | None = None, last_message: str | None = None) -> None: ...

    def list_sessions(self, *, user_id: str | None = None, limit: int = 20, offset: int = 0) -> list[SessionRecord]: ...

    def delete_session(self, session_id: str) -> bool: ...

    def append_message(self, message: MessageRecord) -> MessageRecord: ...

    def list_messages(self, session_id: str, *, limit: int = 50, before: str | None = None) -> list[MessageRecord]:
        """按时间正序返回；``before`` 为消息 id，用于向上翻页。"""
        ...

    def count_messages(self, session_id: str) -> int: ...

    def stats(self) -> dict[str, int]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """用户的长期事实记忆（「记住我偏好简洁回答」）。"""

    def add_fact(self, *, user_id: str, content: str, category: str = "fact") -> FactRecord: ...

    def list_facts(self, *, user_id: str, limit: int = 50) -> list[FactRecord]: ...

    def search_facts(self, *, user_id: str, query: str, limit: int = 5) -> list[FactRecord]: ...

    def delete_fact(self, fact_id: str, *, user_id: str) -> bool: ...

    def clear_facts(self, *, user_id: str) -> int: ...

    def count_facts(self, *, user_id: str) -> int: ...


@runtime_checkable
class KVStore(Protocol):
    """运行期键值配置（人设、技能开关、检索参数等可以在不改 .env 的情况下调整）。"""

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...

    def all(self, prefix: str = "") -> dict[str, Any]: ...

    def delete(self, key: str) -> None: ...


__all__ = [
    "SessionStore", "MemoryStore", "KVStore",
    "SessionRecord", "MessageRecord", "FactRecord",
]