"""系统接口：运行时配置（人设 / 风格 / 检索参数）、服务信息。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from tsukiyohub import __version__
from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.api.schemas import SystemConfig, SystemConfigUpdate
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/system", tags=["system"])

#: 运行期可覆盖项的 KV 键（不改 .env，改完下一条消息生效）
PREF_KEY = "pref"


def _merged_pref(container: ContainerDep) -> dict[str, Any]:
    """settings 默认值 + KV 覆盖（前端「设置」页保存到这里）。"""
    stored = container.kv.get(PREF_KEY) or {}
    settings = container.settings
    return {
        "bot_name": stored.get("bot_name") or settings.bot_name,
        "persona_prompt": stored.get("persona_prompt", settings.persona_prompt),
        "tone": stored.get("tone") or settings.default_tone,
        "length": stored.get("length") or settings.default_length,
        "markdown": stored.get("markdown", settings.default_markdown),
        "fallback_text": stored.get("fallback_text") or settings.fallback_text,
        "memory_enabled": stored.get("memory_enabled", settings.memory_enabled),
    }


def build_config(container: ContainerDep) -> SystemConfig:
    settings = container.settings
    pref = _merged_pref(container)
    provider, model, ready = container.router.info()
    top_k = int(container.kv.get("retrieval.top_k") or settings.retrieval_top_k)
    skills = [s.name for s in container.skills.enabled(container.runtime_cfg())]
    return SystemConfig(
        app_name=settings.app_name,
        version=__version__,
        env=settings.app_env,
        bot_name=str(pref["bot_name"]),
        model=model,
        provider=provider,
        llm_ready=ready,
        fallback_model=settings.llm_fallback_model,
        embedding_model=settings.embedding_model if settings.embedding_enabled else "",
        embedding_ready=settings.embedding_ready,
        vector_backend=container.vector.name,
        database="sqlite" if settings.resolved_database_url.startswith("sqlite") else "postgresql",
        retrieval={
            "top_k": top_k,
            "vector_k": settings.retrieval_vector_k,
            "bm25_k": settings.retrieval_bm25_k,
            "fusion_k": settings.retrieval_fusion_k,
            "vector_weight": settings.retrieval_vector_weight,
            "bm25_weight": settings.retrieval_bm25_weight,
            "min_score": settings.retrieval_min_score,
            "rerank_enabled": settings.rerank_enabled,
            "grounding_min_score": settings.grounding_min_score,
        },
        style={
            "tone": str(pref["tone"]),
            "length": str(pref["length"]),
            "markdown": bool(pref["markdown"]),
        },
        features={
            "memory": bool(pref["memory_enabled"]),
            "mcp": any(s.kind == "mcp" for s in container.skills.describe(container.runtime_cfg())),
            "streaming": True,
            "skill_docs": settings.skills_path.is_dir(),
        },
        skills=skills,
    )


@router.get("/config", response_model=SystemConfig, summary="读取运行时配置（不含密钥）")
async def get_config(container: ContainerDep, _caller: ApiKeyDep) -> SystemConfig:
    return build_config(container)


@router.put("/config", response_model=SystemConfig, summary="修改运行时配置")
async def update_config(
    payload: SystemConfigUpdate,
    container: ContainerDep,
    _caller: ApiKeyDep,
) -> SystemConfig:
    """只更新传了的字段；落库到 KV，重启后仍生效（优先于 .env 默认值）。"""
    stored = dict(container.kv.get(PREF_KEY) or {})
    mapping = {
        "bot_name": "bot_name",
        "persona_prompt": "persona_prompt",
        "default_tone": "tone",
        "default_length": "length",
        "default_markdown": "markdown",
        "fallback_text": "fallback_text",
        "memory_enabled": "memory_enabled",
    }
    for field, key in mapping.items():
        value = getattr(payload, field)
        if value is not None:
            stored[key] = value
    container.kv.set(PREF_KEY, stored)

    if payload.retrieval_top_k is not None:
        container.kv.set("retrieval.top_k", int(payload.retrieval_top_k))

    if payload.skills_enabled is not None:
        wanted = set(payload.skills_enabled)
        disabled: list[str] = []
        for skill in container.skills.describe(container.runtime_cfg()):
            enable = skill.name in wanted
            container.skills.set_enabled(skill.name, enable)
            if not enable:
                disabled.append(skill.name)
        container.kv.set("skills.disabled", sorted(disabled))

    logger.info("运行时配置已更新：%s", sorted(f for f in mapping if getattr(payload, f) is not None))
    return build_config(container)


@router.get("/persona", summary="读取人设与风格（设置页回显用）")
async def get_persona(container: ContainerDep, _caller: ApiKeyDep) -> dict[str, Any]:
    pref = _merged_pref(container)
    return {
        "persona_prompt": str(pref["persona_prompt"] or ""),
        "bot_name": str(pref["bot_name"]),
        "default_tone": str(pref["tone"]),
        "default_length": str(pref["length"]),
        "default_markdown": bool(pref["markdown"]),
        "fallback_text": str(pref["fallback_text"]),
    }


@router.get("/info", summary="服务信息（版本、依赖状态、运行时长）")
async def info(container: ContainerDep) -> dict[str, Any]:
    """不带鉴权：给探活面板 / 部署检查用。"""
    provider, model, ready = container.router.info()
    stats = container.knowledge.stats()
    return {
        "app": container.settings.app_name,
        "version": __version__,
        "env": container.settings.app_env,
        "uptime_seconds": round(time.time() - container.started_at, 1),
        "llm": {"provider": provider, "model": model, "ready": ready},
        "embedding": {"model": container.settings.embedding_model, "ready": container.settings.embedding_ready},
        "vector_store": container.vector.name,
        "knowledge": {"documents": stats.document_count, "chunks": stats.chunk_count},
        "skills": len(container.skills.describe(container.runtime_cfg())),
        "sessions": container.sessions.stats(),
        "features": {
            "mcp": bool(container.settings.mcp_config_file),
            "memory": container.settings.memory_enabled,
            "streaming": True,
        },
    }


__all__ = ["router", "build_config", "PREF_KEY"]