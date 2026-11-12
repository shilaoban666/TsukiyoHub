"""FastAPI 应用装配：配置与日志 → 依赖容器 → 知识库预热 → 路由。

对外只有 HTTP：任何前端（Web / 桌面 / IDE / IM / Agent 平台）都用同一套接口。
"""

from __future__ import annotations

import contextlib
import asyncio
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from tsukiyohub import __version__ as VERSION
from tsukiyohub import metrics
from tsukiyohub.api import (
    routes_chat,
    routes_gateway,
    routes_knowledge,
    routes_memory,
    routes_sessions,
    routes_skills,
    routes_system,
)
from tsukiyohub.config import get_settings
from tsukiyohub.container import build_container, get_container, set_container
from tsukiyohub.domain import HealthResponse
from tsukiyohub.logging_setup import (
    bind_request_context,
    clear_request_context,
    get_logger,
    quiet_startup,
    setup_logging,
)

logger = get_logger(__name__)

_STARTED_AT = time.time()


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，启动横幅的框线与中文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时装配依赖并预热，关闭时落盘索引。"""
    settings = get_settings()
    setup_logging(settings)
    settings.ensure_dirs()

    with quiet_startup():
        logger.info("启动 %s v%s（env=%s）", settings.app_name, VERSION, settings.app_env)
        container = build_container(settings)
        set_container(container)
        # 中文分词预热：放在就绪之前，避免第一批检索请求排队等懒初始化
        try:
            from tsukiyohub.kernel.rag.tokenize import tokenize

            tokenize("分词预热")
        except Exception as exc:  # noqa: BLE001 - 分词失败不影响启动
            logger.warning("分词预热失败：%s", exc)

    _print_banner(container)

    # 资源类指标（文档数/会话数/记忆条数/就绪状态）每 15 秒刷新一次
    sampler = asyncio.create_task(_metrics_sampler(container))

    try:
        yield
    finally:
        sampler.cancel()
        container = get_container()
        with contextlib.suppress(Exception):
            if container.knowledge is not None:
                container.knowledge.persist()
        logger.info("服务退出")


async def _metrics_sampler(container) -> None:
    """周期刷新 gauge；失败只记日志，不影响服务。"""
    while True:
        try:
            metrics.refresh_gauges(container)
        except Exception as exc:  # noqa: BLE001
            logger.debug("指标刷新失败：%s", exc)
        await asyncio.sleep(15)


def _print_banner(container) -> None:
    """启动结论只打 stdout、不写日志文件（日志里留结构化信息，便于 grep）。"""
    try:
        from tsukiyohub.banner import print_banner

        stats = container.knowledge.stats()
        provider, model, ready = container.router.info()
        print_banner(
            app_name=container.settings.app_name,
            version=VERSION,
            env=container.settings.app_env,
            host=container.settings.app_host,
            port=container.settings.app_port,
            documents=stats.document_count,
            chunks=stats.chunk_count,
            vector_index=container.vector.name,
            embedding=getattr(container.embeddings, "model", "") if container.embeddings.enabled else "",
            llm_provider=provider,
            llm_model=model,
            llm_ready=ready,
            skills=len(container.skills.enabled(container.runtime_cfg())),
            database="sqlite" if container.settings.resolved_database_url.startswith("sqlite") else "postgresql",
            web_url=container.settings.public_base_url,
        )
    except Exception as exc:  # noqa: BLE001 - 横幅失败绝不影响启动
        logger.warning("启动横幅渲染失败（不影响服务）：%s", exc)


def create_app() -> FastAPI:
    """应用工厂：测试可以构造彼此隔离的实例。"""
    settings = get_settings()
    setup_logging(settings)
    _force_utf8_console()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=VERSION,
        description=(
            "与业务无关的「聊天 + 技能 + RAG」内核服务。\n\n"
            "- `POST /api/chat`：一次性回答；`POST /api/chat/stream`：SSE 逐字输出\n"
            "- `POST /api/v1/chat/completions`：OpenAI 兼容，任何 Agent 平台可直接接入\n"
            "- `GET/POST /api/knowledge/*`：知识库上传、导入、检索调试\n"
            "- `GET/PATCH /api/skills`：技能列表与启停\n"
            "- `GET /docs`：交互式接口文档"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes_chat.router, prefix="/api")
    app.include_router(routes_sessions.router, prefix="/api")
    app.include_router(routes_skills.router, prefix="/api")
    app.include_router(routes_knowledge.router, prefix="/api")
    app.include_router(routes_memory.router, prefix="/api")
    app.include_router(routes_system.router, prefix="/api")
    app.include_router(routes_gateway.router, prefix="/api")

    _register_meta_routes(app)
    _register_middleware(app)
    _register_exception_handlers(app)
    return app


def _register_meta_routes(app: FastAPI) -> None:
    @app.get("/", tags=["meta"], summary="服务信息")
    async def root() -> dict[str, object]:
        settings = get_settings()
        return {
            "app": settings.app_name,
            "version": VERSION,
            "env": settings.app_env,
            "description": "与业务无关的聊天 / 技能 / RAG 内核服务",
            "docs": "/docs",
            "endpoints": {
                "chat": "POST /api/chat",
                "chat_stream": "POST /api/chat/stream",
                "openai_compatible": "POST /api/v1/chat/completions",
                "knowledge": "GET /api/knowledge/stats",
                "skills": "GET /api/skills",
                "health": "GET /healthz",
            },
        }

    @app.get("/healthz", tags=["meta"], response_model=HealthResponse, summary="健康检查")
    async def healthz() -> HealthResponse:
        """探活 + 依赖就绪状态。

        任一大模型 / 向量依赖未就绪时 ``status=degraded``，但**服务仍可用**
        （降级为原文摘录 / 纯关键词检索），所以 HTTP 仍返回 200。
        不要把存活探针绑定到 ``status``，否则模型临时故障会导致容器反复重启。
        """
        container = get_container()
        stats = container.knowledge.stats()
        provider, model, llm_ready = container.router.info()
        hot = container.hot.status() if container.hot is not None else {"backend": "none", "available": False}
        return HealthResponse(
            status="ok" if (llm_ready and container.db_ready) else "degraded",
            app=container.settings.app_name,
            version=VERSION,
            env=container.settings.app_env,
            uptime_seconds=round(time.time() - _STARTED_AT, 2),
            index_loaded=container.knowledge.loaded,
            document_count=stats.document_count,
            chunk_count=stats.chunk_count,
            llm_ready=llm_ready,
            embedding_ready=container.settings.embedding_ready,
            database="ready" if container.db_ready else (container.db_error or "error"),
            vector_store=container.vector.name,
            features={
                "streaming": True,
                "memory": container.settings.memory_enabled,
                "mcp": bool(container.settings.mcp_config_file),
                "skill_docs": bool(container.skill_docs),
            },
        )

    @app.get("/metrics", tags=["meta"], summary="Prometheus 指标")
    async def prometheus_metrics() -> PlainTextResponse:
        """标准 Prometheus exposition 文本（无需鉴权，便于 Prometheus 抓取）。"""
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/readyz", tags=["meta"], summary="就绪检查（含知识库）")
    async def readyz() -> JSONResponse:
        """就绪检查：知识库为空时返回 503，供负载均衡摘流量。"""
        container = get_container()
        stats = container.knowledge.stats()
        ready = container.knowledge.loaded or stats.chunk_count > 0
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ready": ready,
                "documents": stats.document_count,
                "chunks": stats.chunk_count,
                "hint": None if ready else "知识库为空：先上传文档（POST /api/knowledge/upload）或导入目录",
            },
        )


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        trace_id = request.headers.get("x-request-id") or f"{int(time.time() * 1000):x}"
        bind_request_context(trace_id=trace_id, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            clear_request_context()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 指标：路径用路由模板（基数可控），拿不到时记为 unmatched
        route = request.scope.get("route")
        path_label = str(getattr(route, "path", "") or "unmatched")
        metrics.record_http(request.method, path_label, response.status_code, elapsed_ms / 1000)
        response.headers["X-Request-Id"] = trace_id
        response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
        if request.url.path not in ("/healthz", "/readyz", "/api/system/info"):
            logger.info("%s %s → %d（%dms）", request.method, request.url.path, response.status_code, elapsed_ms)
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "请求参数校验失败",
                "errors": [
                    {"field": ".".join(str(p) for p in err.get("loc", [])), "message": err.get("msg", "")}
                    for err in exc.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "服务内部错误，请稍后重试或查看服务端日志", "path": request.url.path},
        )


# 模块级实例：供 `uvicorn tsukiyohub.app:app` / `python -m tsukiyohub` 使用
app = create_app()


__all__ = ["app", "create_app"]