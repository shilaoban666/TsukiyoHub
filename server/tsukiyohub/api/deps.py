"""FastAPI 依赖：容器注入、鉴权、限流、请求上下文。"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from tsukiyohub.container import Container, get_container
from tsukiyohub.logging_setup import bind_request_context, clear_request_context


def container_dep() -> Container:
    """取进程级容器（应用启动时装配好；测试可 set_container 注入）。"""
    return get_container()


ContainerDep = Annotated[Container, Depends(container_dep)]


async def verify_api_key(
    container: ContainerDep,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """可选的调用方鉴权。

    ``API_KEY`` 为空 = 不鉴权（本地开发 / 内网直连）。
    填了则接受 ``Authorization: Bearer <key>`` 或 ``X-API-Key: <key>`` 两种写法，
    方便浏览器前端与脚本 / OpenAI 兼容客户端各自用顺手的头。
    """
    expected = container.settings.api_key.get_secret_value().strip() if container.settings.api_key else ""
    if not expected:
        return "anonymous"
    supplied = (x_api_key or "").strip()
    if not supplied and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = token.strip()
    if supplied != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API Key 无效或缺失")
    return "authenticated"


async def request_context(request: Request) -> None:
    """给每条请求打 trace_id，日志里能串起来。"""
    trace_id = request.headers.get("x-request-id") or f"{int(time.time() * 1000):x}"
    bind_request_context(trace_id=trace_id, path=request.url.path)
    request.state.trace_id = trace_id
    try:
        yield
    finally:
        clear_request_context()


async def rate_limit(container: ContainerDep, request: Request) -> None:
    """按客户端 IP 的每分钟限流（``RATE_LIMIT_PER_MINUTE=0`` 关闭）。

    优先用热存储的原子自增（多副本一致）；没有 Redis 时退化为进程内计数。
    """
    limit = container.settings.rate_limit_per_minute
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    key = f"rl:{client}:{int(time.time() // 60)}"
    try:
        hits = container.hot.incr(key, ttl_seconds=90)
        if hits > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail=f"请求过于频繁（每分钟上限 {limit} 次）")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 限流失败不能影响正常问答
        return


def current_trace_id(request: Request) -> str:
    return str(getattr(request.state, "trace_id", "") or "")


__all__ = [
    "ContainerDep", "container_dep", "verify_api_key", "request_context", "rate_limit", "current_trace_id",
]


# 供路由里做类型标注用的小别名
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
RateLimitDep = Annotated[None, Depends(rate_limit)]
ContextDep = Annotated[None, Depends(request_context)]


def json_error(message: str, *, status_code: int = 400) -> HTTPException:
    """统一的业务错误：``{"detail": "中文可读信息"}``。"""
    return HTTPException(status_code=status_code, detail=message)


def safe_debug(container: Container, payload: dict[str, Any]) -> dict[str, Any] | None:
    """仅非生产环境返回调试信息。"""
    return payload if not container.settings.is_production else None