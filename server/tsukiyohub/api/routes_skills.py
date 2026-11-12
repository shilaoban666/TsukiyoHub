"""技能接口：列出、启停、查看参数。

「技能」是模型可调用的能力单元的统称：内置工具、SKILL.md 技能、MCP 工具都从这里看。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.api.schemas import SkillListResponse, SkillToggleRequest
from tsukiyohub.domain import SkillInfo
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillListResponse, summary="技能列表")
async def list_skills(container: ContainerDep, _caller: ApiKeyDep) -> SkillListResponse:
    items = container.skills.describe(container.runtime_cfg())
    return SkillListResponse(items=items, total=len(items))


@router.patch("/{name}", response_model=SkillInfo, summary="启用 / 停用技能")
async def toggle_skill(
    name: str,
    payload: SkillToggleRequest,
    container: ContainerDep,
    _caller: ApiKeyDep,
) -> SkillInfo:
    """启停结果写进 KV（不改 .env），下一条消息立即生效。"""
    skill = container.skills.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{name}")
    container.skills.set_enabled(name, payload.enabled)
    # 持久化：把「被停用的技能」存成列表，重启后仍是停用状态
    disabled = set(container.kv.get("skills.disabled") or [])
    if payload.enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    container.kv.set("skills.disabled", sorted(disabled))

    for info in container.skills.describe(container.runtime_cfg()):
        if info.name == name:
            return info
    raise HTTPException(status_code=500, detail=f"技能状态更新失败：{name}")


@router.post("/{name}/test", summary="直接调用技能（调试）")
async def test_skill(
    name: str,
    payload: dict[str, Any],
    container: ContainerDep,
    _caller: ApiKeyDep,
) -> dict[str, Any]:
    """不经过模型，直接把参数喂给技能，看它返回什么——写技能 / 排查时很有用。"""
    from tsukiyohub.kernel.skills.base import ToolContext

    skill = container.skills.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{name}")
    ctx = ToolContext(
        question=str(payload.get("question") or ""),
        user_ctx={"user_id": payload.get("user_id") or "debug"},
        cfg=container.runtime_cfg(),
        pref={},
    )
    try:
        content = skill.call(dict(payload.get("args") or {}), ctx)
        ok = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("技能 %s 调用失败", name)
        ok = False
        content = ""
        from tsukiyohub.metrics import record_skill_call

        record_skill_call(name, False)
        raise HTTPException(status_code=500, detail=f"技能调用失败：{exc}") from exc
    from tsukiyohub.metrics import record_skill_call

    record_skill_call(name, ok)
    return {
        "name": name,
        "content": content,
        "hits": {str(k): v.chunk.title for k, v in ctx.hits.items()},
        "records": ctx.records,
        "timings": ctx.timings,
    }


__all__ = ["router"]