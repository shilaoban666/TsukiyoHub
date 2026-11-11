"""会话与消息接口：前端会话列表、历史回放都走这里。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.api.schemas import (
    MessageOut,
    SessionCreateRequest,
    SessionDetail,
    SessionListResponse,
)
from tsukiyohub.domain import Citation, SessionInfo
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["sessions"])


def _session_info(record: Any) -> SessionInfo:
    return SessionInfo(
        id=record.id, title=record.title or "新会话", user_id=record.user_id, channel=record.channel,
        created_at=record.created_at, updated_at=record.updated_at,
        message_count=record.message_count, last_message=record.last_message,
    )


@router.get("/sessions", response_model=SessionListResponse, summary="会话列表")
async def list_sessions(
    container: ContainerDep,
    _caller: ApiKeyDep,
    user_id: str | None = Query(None, description="只看某个用户的会话"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> SessionListResponse:
    records = container.sessions.list_sessions(user_id=user_id, limit=limit, offset=offset)
    return SessionListResponse(items=[_session_info(r) for r in records], total=len(records))


@router.post("/sessions", response_model=SessionInfo, summary="新建会话")
async def create_session(payload: SessionCreateRequest, container: ContainerDep, _caller: ApiKeyDep) -> SessionInfo:
    record = container.sessions.create_session(user_id=payload.user_id, channel=payload.channel, title=payload.title)
    return _session_info(record)


@router.get("/sessions/{session_id}", response_model=SessionInfo, summary="会话详情")
async def get_session(session_id: str, container: ContainerDep, _caller: ApiKeyDep) -> SessionInfo:
    record = container.sessions.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return _session_info(record)


@router.patch("/sessions/{session_id}", response_model=SessionInfo, summary="重命名会话")
async def rename_session(
    session_id: str,
    payload: dict[str, Any],
    container: ContainerDep,
    _caller: ApiKeyDep,
) -> SessionInfo:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title 不能为空")
    if len(title) > 120:
        title = title[:120]
    record = container.sessions.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    container.sessions.touch_session(session_id, title=title)
    record = container.sessions.get_session(session_id)
    return _session_info(record)


@router.get("/sessions/{session_id}/messages", response_model=SessionDetail, summary="历史消息（时间正序）")
async def list_messages(
    session_id: str,
    container: ContainerDep,
    _caller: ApiKeyDep,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = Query(None, description="向上翻页：传当前最老一条消息的 id"),
) -> SessionDetail:
    record = container.sessions.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    rows = container.sessions.list_messages(session_id, limit=limit, before=before)
    messages = [
        MessageOut(
            id=m.id, role=m.role, content=m.content, created_at=m.created_at,
            citations=[Citation(**c) for c in (m.citations or [])],
            tool_calls=m.tool_calls or [], usage=m.usage or {}, latency_ms=m.latency_ms,
        )
        for m in rows
    ]
    return SessionDetail(session=_session_info(record), messages=messages)


@router.delete("/sessions/{session_id}", summary="删除会话")
async def delete_session(session_id: str, container: ContainerDep, _caller: ApiKeyDep) -> dict[str, Any]:
    removed = container.sessions.delete_session(session_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return {"ok": True}


__all__ = ["router"]