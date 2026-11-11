"""SQL 存储单测：会话 / 消息 / 分页 / 统计 / 键值 / 记忆（SQLite 临时目录）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from tsukiyohub.adapters.store_sql import build_stores
from tsukiyohub.config import Settings
from tsukiyohub.ports.memory import KVStore, MemoryStore, MessageRecord, SessionStore


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("TSUKIYO_DATA_ROOT", str(tmp_path))
    settings = Settings()
    database, sessions, memory, kv = build_stores(settings)
    try:
        yield {
            "database": database,
            "sessions": sessions,
            "memory": memory,
            "kv": kv,
            "settings": settings,
        }
    finally:
        database.dispose()


def assert_utc_iso(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)


def make_message(
    session_id: str,
    content: str,
    *,
    role: str = "user",
    created_at: str | None = None,
) -> MessageRecord:
    return MessageRecord(
        id="",
        session_id=session_id,
        role=role,
        content=content,
        created_at=created_at or "",
    )


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def test_database_uses_sqlite_in_data_root(stores: dict[str, Any], tmp_path: Path) -> None:
    assert stores["database"].url.startswith("sqlite")
    assert (tmp_path / "tsukiyohub.db").exists()
    assert stores["database"].health() is True


def test_session_lifecycle(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="web", title="第一次会话")
    assert session.id
    assert session.user_id == "u1"
    assert session.channel == "web"
    assert session.title == "第一次会话"
    assert session.message_count == 0
    assert_utc_iso(session.created_at)
    assert_utc_iso(session.updated_at)

    loaded = sessions.get_session(session.id)
    assert loaded is not None and loaded.id == session.id
    assert sessions.get_session("no-such-id") is None

    sessions.touch_session(session.id, title="改了标题", last_message="最新一句")
    touched = sessions.get_session(session.id)
    assert touched is not None
    assert touched.title == "改了标题"
    assert touched.last_message == "最新一句"

    assert sessions.delete_session(session.id) is True
    assert sessions.delete_session(session.id) is False
    assert sessions.get_session(session.id) is None


def test_list_sessions_orders_by_updated_at_and_filters_user(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    first = sessions.create_session(user_id="u1", channel="api")
    second = sessions.create_session(user_id="u2", channel="api")
    sessions.touch_session(first.id)  # 让 u1 的会话更新得更晚
    listed = sessions.list_sessions(limit=10)
    assert [session.id for session in listed] == [first.id, second.id]
    assert [session.id for session in sessions.list_sessions(user_id="u2")] == [second.id]
    assert [session.id for session in sessions.list_sessions(limit=1, offset=1)] == [second.id]


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
def test_append_and_list_messages_updates_session(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    first = sessions.append_message(make_message(session.id, "你好，年假怎么算？"))
    second = sessions.append_message(make_message(session.id, "满一年 5 天。", role="assistant"))

    assert first.id and second.id and first.id != second.id
    assert_utc_iso(first.created_at)

    messages = sessions.list_messages(session.id)
    assert [message.content for message in messages] == ["你好，年假怎么算？", "满一年 5 天。"]
    assert [message.role for message in messages] == ["user", "assistant"]

    refreshed = sessions.get_session(session.id)
    assert refreshed is not None
    assert refreshed.message_count == 2
    assert refreshed.last_message == "满一年 5 天。"
    assert refreshed.title == "你好，年假怎么算？"  # 首条用户消息自动成为标题
    assert sessions.count_messages(session.id) == 2


def test_list_messages_pagination_with_before(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    for index in range(1, 6):
        sessions.append_message(
            make_message(session.id, f"m{index}", created_at=f"2026-01-01T00:00:0{index}+00:00")
        )

    latest = sessions.list_messages(session.id, limit=2)
    assert [message.content for message in latest] == ["m4", "m5"]

    older = sessions.list_messages(session.id, limit=2, before=latest[0].id)
    assert [message.content for message in older] == ["m2", "m3"]

    oldest = sessions.list_messages(session.id, limit=2, before=older[0].id)
    assert [message.content for message in oldest] == ["m1"]

    assert sessions.list_messages(session.id, limit=2, before=oldest[0].id) == []
    assert sessions.list_messages(session.id, limit=2, before="unknown") == []


def test_message_seq_is_monotonic_and_returned(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    first = sessions.append_message(make_message(session.id, "第一条"))
    second = sessions.append_message(make_message(session.id, "第二条"))
    assert first.seq > 0 and second.seq > first.seq
    assert [message.seq for message in sessions.list_messages(session.id)] == [first.seq, second.seq]


def test_same_second_messages_keep_insertion_order(stores: dict[str, Any]) -> None:
    """同一轮 user / assistant 的 created_at 可能只到秒，顺序必须靠 seq 而不是时间。"""
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    user_message = sessions.append_message(
        make_message(session.id, "问题", created_at="2026-01-01T00:00:00+00:00")
    )
    assistant_message = sessions.append_message(
        make_message(session.id, "回答", role="assistant", created_at="2026-01-01T00:00:00+00:00")
    )
    assert user_message.created_at == assistant_message.created_at
    messages = sessions.list_messages(session.id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == ["问题", "回答"]


def test_seq_ordering_wins_over_created_at(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    first = sessions.append_message(
        make_message(session.id, "先写的", created_at="2026-01-01T00:00:10+00:00")
    )
    second = sessions.append_message(
        make_message(session.id, "后写的", created_at="2026-01-01T00:00:05+00:00")
    )
    assert first.seq < second.seq
    assert [message.content for message in sessions.list_messages(session.id)] == ["先写的", "后写的"]


def test_created_at_is_normalized_to_microseconds(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    message = sessions.append_message(make_message(session.id, "你好"))
    assert message.created_at.endswith("+00:00")
    assert "." in message.created_at
    assert_utc_iso(message.created_at)


def test_message_fields_roundtrip(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    message = sessions.append_message(
        MessageRecord(
            id="",
            session_id=session.id,
            role="assistant",
            content="答案",
            citations=[{"index": 1, "title": "手册"}],
            tool_calls=[{"tool": "knowledge_search", "hits": 2}],
            usage={"total_tokens": 42},
            latency_ms=123,
            meta={"degraded": False},
        )
    )
    loaded = sessions.list_messages(session.id)[0]
    assert loaded.id == message.id
    assert loaded.citations == [{"index": 1, "title": "手册"}]
    assert loaded.tool_calls == [{"tool": "knowledge_search", "hits": 2}]
    assert loaded.usage == {"total_tokens": 42}
    assert loaded.latency_ms == 123
    assert loaded.meta == {"degraded": False}


def test_append_message_requires_existing_session(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    with pytest.raises(ValueError):
        sessions.append_message(make_message("missing-session", "hi"))


def test_delete_session_removes_messages(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    session = sessions.create_session(user_id="u1", channel="api")
    sessions.append_message(make_message(session.id, "hi"))
    assert sessions.stats()["messages"] == 1
    sessions.delete_session(session.id)
    assert sessions.count_messages(session.id) == 0
    assert sessions.stats()["messages"] == 0


def test_stats_counts_everything(stores: dict[str, Any]) -> None:
    sessions: SessionStore = stores["sessions"]
    memory: MemoryStore = stores["memory"]
    kv: KVStore = stores["kv"]
    session = sessions.create_session(user_id="u1", channel="api")
    sessions.append_message(make_message(session.id, "hi"))
    memory.add_fact(user_id="u1", content="喜欢简洁回答", category="preference")
    kv.set("skills.disabled", ["web_fetch"])
    stats = sessions.stats()
    assert stats["sessions"] == 1
    assert stats["messages"] == 1
    assert stats["facts"] == 1
    assert stats["kv"] == 1


# ---------------------------------------------------------------------------
# 键值
# ---------------------------------------------------------------------------
def test_kv_roundtrip_and_prefix(stores: dict[str, Any]) -> None:
    kv: KVStore = stores["kv"]
    assert kv.get("missing") is None
    kv.set("pref", {"tone": "friendly", "markdown": True})
    kv.set("retrieval.top_k", 6)
    kv.set("skills.disabled", ["web_search"])
    assert kv.get("pref") == {"tone": "friendly", "markdown": True}
    assert kv.get("retrieval.top_k") == 6
    assert kv.get("skills.disabled") == ["web_search"]
    assert kv.all() == {
        "pref": {"tone": "friendly", "markdown": True},
        "retrieval.top_k": 6,
        "skills.disabled": ["web_search"],
    }
    assert kv.all("retrieval.") == {"retrieval.top_k": 6}
    kv.set("retrieval.top_k", 8)  # 覆盖写
    assert kv.get("retrieval.top_k") == 8
    kv.delete("pref")
    assert kv.get("pref") is None
    kv.delete("missing")  # 删除不存在的键不报错


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------
def test_memory_facts_crud(stores: dict[str, Any]) -> None:
    memory: MemoryStore = stores["memory"]
    assert memory.count_facts(user_id="u1") == 0
    fact = memory.add_fact(user_id="u1", content="负责华东区渠道", category="profile")
    assert fact.id
    assert fact.category == "profile"
    assert_utc_iso(fact.created_at)

    duplicate = memory.add_fact(user_id="u1", content="负责华东区渠道", category="profile")
    assert duplicate.id == fact.id  # 幂等：同一句话不重复存
    assert memory.count_facts(user_id="u1") == 1

    other = memory.add_fact(user_id="u2", content="喜欢简洁回答", category="preference")
    assert memory.count_facts(user_id="u1") == 1
    assert memory.count_facts(user_id="u2") == 1

    listed = memory.list_facts(user_id="u1")
    assert [item.id for item in listed] == [fact.id]
    assert memory.list_facts(user_id="u-unknown") == []

    with pytest.raises(ValueError):
        memory.add_fact(user_id="", content="没有用户")
    with pytest.raises(ValueError):
        memory.add_fact(user_id="u1", content="   ")

    assert memory.delete_fact(fact.id, user_id="u2") is False  # 不能删别人的
    assert memory.delete_fact(fact.id, user_id="u1") is True
    assert memory.delete_fact(fact.id, user_id="u1") is False

    assert memory.clear_facts(user_id="u2") == 1
    assert other.id  # 保持引用，避免 lint 误报
    assert memory.count_facts(user_id="u2") == 0


def test_memory_search_facts_ranks_by_overlap(stores: dict[str, Any]) -> None:
    memory: MemoryStore = stores["memory"]
    memory.add_fact(user_id="u1", content="负责华东区渠道销售", category="profile")
    memory.add_fact(user_id="u1", content="常驻杭州", category="profile")
    memory.add_fact(user_id="u1", content="喜欢简洁回答", category="preference")
    hits = memory.search_facts(user_id="u1", query="华东区的渠道情况", limit=2)
    assert hits
    assert "华东区" in hits[0].content
    assert memory.search_facts(user_id="u1", query="", limit=2) == []
    assert memory.search_facts(user_id="u2", query="华东区", limit=2) == []