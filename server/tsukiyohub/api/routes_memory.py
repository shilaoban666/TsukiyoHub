"""记忆接口：查看 / 删除某个用户的长期事实记忆。

记忆是「模型自己记住的东西」（偏好、身份、长期目标），由内核在对话后自动抽取；
这里提供人工查看与纠正的入口——**用户有权知道自己被记住了什么，也有权忘掉**。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.api.schemas import MemoryListResponse
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/{user_id}", response_model=MemoryListResponse, summary="查看某个用户的记忆")
async def list_memory(
    user_id: str,
    container: ContainerDep,
    _caller: ApiKeyDep,
    limit: int = Query(50, ge=1, le=200),
) -> MemoryListResponse:
    items = container.memory.list(user_id, limit=limit)
    return MemoryListResponse(user_id=user_id, items=items, total=len(items))


@router.delete("/{user_id}", summary="清空某个用户的记忆")
async def clear_memory(user_id: str, container: ContainerDep, _caller: ApiKeyDep) -> dict[str, Any]:
    removed = container.memory.clear(user_id)
    logger.info("已清空用户 %s 的记忆（%d 条）", user_id, removed)
    return {"ok": True, "removed": removed}


@router.delete("/{user_id}/{fact_id}", summary="删除一条记忆")
async def delete_fact(user_id: str, fact_id: str, container: ContainerDep, _caller: ApiKeyDep) -> dict[str, Any]:
    removed = container.memory.delete(fact_id, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"记忆不存在：{fact_id}")
    return {"ok": True}


__all__ = ["router"]