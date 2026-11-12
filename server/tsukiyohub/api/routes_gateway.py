"""路由网关管理接口（代理到独立的 Go 网关）。

为什么由本服务代理：
* 前端只连一个后端，不用处理网关的 CORS / 令牌 / 端口；
* 网关管理接口默认只允许本机访问，由本服务做统一的访问控制；
* 网关没启动时返回可读的「未连接」而不是让前端白屏。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/gateway", tags=["gateway"])


def _settings(container: ContainerDep) -> Any:
    return container.settings


def _client(container: ContainerDep, timeout: float | None = None) -> httpx.AsyncClient:
    settings = _settings(container)
    token = settings.gateway_admin_token.get_secret_value().strip() if settings.gateway_admin_token else ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        base_url=settings.gateway_admin_url.rstrip("/"),
        timeout=timeout or settings.gateway_timeout_seconds,
        headers=headers,
    )


async def _proxy(
    container: ContainerDep,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float | None = None,
) -> Any:
    """转发到网关管理接口，并把网关的错误原样变成 ``{"detail": ...}``。"""
    if not _settings(container).gateway_enabled:
        raise HTTPException(status_code=503, detail="路由网关未启用（GATEWAY_ENABLED=false）")
    try:
        async with _client(container, timeout) as client:
            response = await client.request(method, path, params=params, json=body)
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"路由网关未连接（{_settings(container).gateway_admin_url}）：{exc}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"路由网关响应超时：{exc}") from exc

    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("error") or payload.get("detail") or ""
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
        except ValueError:
            detail = response.text[:300]
        raise HTTPException(status_code=response.status_code, detail=detail or f"网关返回 {response.status_code}")
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


# ---------------------------------------------------------------------------
# 健康与概况
# ---------------------------------------------------------------------------
@router.get("/health", summary="网关连通性")
async def health(container: ContainerDep) -> dict[str, Any]:
    """前端用它显示「网关是否已连接」，不可达时返回 200 + reachable=false。"""
    settings = _settings(container)
    result: dict[str, Any] = {
        "enabled": settings.gateway_enabled,
        "reachable": False,
        "admin_url": settings.gateway_admin_url,
        "public_url": settings.gateway_public_url,
    }
    if not settings.gateway_enabled:
        result["error"] = "GATEWAY_ENABLED=false"
        return result
    started = time.perf_counter()
    try:
        async with _client(container, timeout=3.0) as client:
            response = await client.get("/admin/api/overview")
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if response.status_code < 400:
            data = response.json()
            result.update({"reachable": True, "version": data.get("version"), "uptime_sec": data.get("uptime_sec")})
            return result
        result["error"] = f"网关返回 {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - 未连接是常态，不该抛 500
        result["error"] = str(exc)[:200]
    return result


@router.get("/overview", summary="网关概况（指标/供应商/熔断/告警）")
async def overview(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/overview")


@router.get("/stats", summary="窗口统计")
async def stats(
    container: ContainerDep,
    _caller: ApiKeyDep,
    window: int = Query(300, ge=10, le=900),
    route: str = "",
    model: str = "",
    provider: str = "",
) -> Any:
    params = {"window": window}
    if route:
        params["route"] = route
    if model:
        params["model"] = model
    if provider:
        params["provider"] = provider
    return await _proxy(container, "GET", "/admin/api/stats", params=params)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@router.get("/config", summary="读取网关配置（密钥已脱敏）")
async def get_config(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/config")


@router.put("/config", summary="保存网关配置（热更新）")
async def put_config(payload: dict[str, Any], container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "PUT", "/admin/api/config", body=payload, timeout=30.0)


@router.post("/config/reload", summary="从文件重新加载配置")
async def reload_config(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "POST", "/admin/api/config/reload")


# ---------------------------------------------------------------------------
# 供应商 / 路由 / 追踪
# ---------------------------------------------------------------------------
@router.get("/providers", summary="供应商健康状态")
async def providers(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/providers")


@router.post("/providers/{name}/probe", summary="主动探活某个供应商")
async def probe_provider(name: str, container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "POST", f"/admin/api/providers/{name}/probe", timeout=30.0)


@router.get("/routes", summary="路由列表")
async def routes(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/routes")


@router.put("/routes", summary="保存路由（热更新）")
async def put_routes(payload: dict[str, Any], container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "PUT", "/admin/api/routes", body=payload, timeout=30.0)


@router.get("/traces", summary="请求追踪列表")
async def traces(
    container: ContainerDep,
    _caller: ApiKeyDep,
    limit: int = Query(50, ge=1, le=500),
    route: str = "",
    model: str = "",
    ok: bool | None = Query(None, description="true=只看成功，false=只看失败"),
    since: str = "",
) -> Any:
    params: dict[str, Any] = {"limit": limit}
    if route:
        params["route"] = route
    if model:
        params["model"] = model
    if ok is not None:
        params["ok"] = "true" if ok else "false"
    if since:
        params["since"] = since
    return await _proxy(container, "GET", "/admin/api/traces", params=params)


@router.get("/traces/{trace_id}", summary="单条追踪详情")
async def trace_detail(trace_id: str, container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", f"/admin/api/traces/{trace_id}")


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------
@router.get("/alerts/overview", summary="告警概况")
async def alerts_overview(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/alerts/overview")


@router.get("/alerts/rules", summary="告警规则")
async def alerts_rules(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "GET", "/admin/api/alerts/rules")


@router.put("/alerts/rules", summary="保存告警规则（热更新）")
async def put_alerts_rules(payload: dict[str, Any], container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return await _proxy(container, "PUT", "/admin/api/alerts/rules", body=payload, timeout=30.0)


@router.get("/alerts/events", summary="告警事件")
async def alerts_events(
    container: ContainerDep,
    _caller: ApiKeyDep,
    limit: int = Query(100, ge=1, le=500),
    status: str = "",
) -> Any:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return await _proxy(container, "GET", "/admin/api/alerts/events", params=params)


@router.post("/alerts/{alert_id}/ack", summary="确认（静默）一条告警")
async def ack_alert(alert_id: str, payload: dict[str, Any], container: ContainerDep, _caller: ApiKeyDep) -> Any:
    body = {
        "by": str(payload.get("by") or "web"),
        "minutes": int(payload.get("minutes") or 30),
    }
    return await _proxy(container, "POST", f"/admin/api/alerts/{alert_id}/ack", body=body)


@router.get("/metrics", summary="Prometheus 指标（透传网关 /metrics）", response_class=PlainTextResponse)
async def metrics(container: ContainerDep, request: Request) -> PlainTextResponse:
    if not _settings(container).gateway_enabled:
        raise HTTPException(status_code=503, detail="路由网关未启用")
    try:
        async with _client(container, timeout=5.0) as client:
            response = await client.get("/metrics")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"路由网关未连接：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:200])
    return PlainTextResponse(response.text, media_type="text/plain; version=0.0.4; charset=utf-8")


__all__ = ["router"]