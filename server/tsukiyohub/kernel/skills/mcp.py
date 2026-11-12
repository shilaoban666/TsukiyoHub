"""MCP 接入：把外部 MCP server 暴露的工具包装成与内置技能同构的 :class:`BaseSkill`。

为什么这么包装
--------------
MCP（Model Context Protocol）是「工具来源」的标准协议，但对内核来说，一个 MCP 工具和
一个内置工具没有区别：都要有名字、描述、JSON Schema，都要能同步调用、能统计耗时。
包成 ``BaseSkill`` 之后，注册表 / 提示词 / 工具循环 / 前端技能列表**一行都不用改**。

配置文件格式（``settings.mcp_config_file`` 指向的 JSON）::

    {
      "servers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
          "env": {"LOG_LEVEL": "info"},
          "transport": "stdio"
        },
        "search": {"url": "http://127.0.0.1:7801/sse", "transport": "sse"}
      }
    }

优雅降级
--------
MCP 是可选能力：官方 ``mcp`` SDK 没装、配置没写、某个 server 起不来，都必须只是
**记 warning + 少几个工具**，绝不能让内核 import 崩掉或启动失败。所以 SDK 一律懒加载，
单个 server 失败只跳过它自己。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsukiyohub.kernel.skills.base import BaseSkill, ToolContext
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 工具名前缀：``mcp__<server>__<tool>``，与 OpenAI function 命名习惯一致
MCP_PREFIX = "mcp__"
MAX_TOOL_NAME = 64
MAX_RESULT_CHARS = 8000
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_sdk_warned = False


def _safe(text: str) -> str:
    return _NAME_RE.sub("_", str(text or "")).strip("_") or "tool"


def tool_name(server_name: str, raw_name: str) -> str:
    """``mcp__server__tool``，并压到 64 字符以内（OpenAI function 名限制）。"""
    name = f"{MCP_PREFIX}{_safe(server_name)}__{_safe(raw_name)}"
    return name[:MAX_TOOL_NAME]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class McpServerConfig:
    """一个 MCP server 的连接配置。"""

    name: str
    transport: str = "stdio"          # stdio / sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    enabled: bool = True
    timeout: float = 20.0

    @classmethod
    def from_raw(cls, name: str, raw: dict[str, Any], default_timeout: float = 20.0) -> McpServerConfig:
        url = str(raw.get("url") or "").strip()
        command = str(raw.get("command") or "").strip()
        transport = str(raw.get("transport") or "").strip().lower()
        if transport not in ("stdio", "sse"):
            transport = "sse" if url and not command else "stdio"
        args = raw.get("args") or []
        if isinstance(args, str):
            args = [args]
        env = raw.get("env") or {}
        try:
            timeout = float(raw.get("timeout") or default_timeout or 20.0)
        except (TypeError, ValueError):
            timeout = default_timeout or 20.0
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=[str(item) for item in args],
            env={str(key): str(value) for key, value in dict(env).items()},
            url=url,
            enabled=raw.get("enabled", True) is not False,
            timeout=timeout,
        )

    def problem(self) -> str:
        """配置缺陷（空串 = 可用）。"""
        if self.transport == "stdio" and not self.command:
            return "stdio 模式缺少 command"
        if self.transport == "sse" and not self.url:
            return "sse 模式缺少 url"
        return ""


def load_config(path: Path | str) -> list[McpServerConfig]:
    """读 MCP 配置文件；文件不存在 / 内容非法时返回空列表并记 warning。"""
    file = Path(path)
    if not file.is_file():
        logger.warning("MCP 配置文件不存在：%s", file)
        return []
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("MCP 配置读取失败（%s）：%s", file, exc)
        return []
    items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, dict) and isinstance(raw.get("servers"), dict):
        items = [(str(name), cfg) for name, cfg in raw["servers"].items() if isinstance(cfg, dict)]
    elif isinstance(raw, list):  # 兼容旧格式：[{"name": ..., ...}]
        items = [(str(cfg.get("name") or f"server{index}"), cfg) for index, cfg in enumerate(raw)
                 if isinstance(cfg, dict)]
    else:
        logger.warning("MCP 配置格式不对（需要 {\"servers\": {...}}）：%s", file)
        return []
    servers: list[McpServerConfig] = []
    for name, cfg in items:
        server = McpServerConfig.from_raw(name, cfg)
        problem = server.problem()
        if problem:
            logger.warning("MCP server「%s」配置不完整（%s），已跳过", name, problem)
            continue
        servers.append(server)
    return servers


# ---------------------------------------------------------------------------
# 官方 SDK（懒加载；没装就优雅降级）
# ---------------------------------------------------------------------------
@dataclass
class _Sdk:
    ClientSession: Any
    StdioServerParameters: Any
    stdio_client: Any
    sse_client: Any


def _sdk() -> _Sdk | None:
    """懒加载官方 ``mcp`` SDK；没装时只记一次 warning 并返回 ``None``。"""
    global _sdk_warned
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
    except Exception:  # noqa: BLE001 - 任何导入失败都按「未安装」处理
        if not _sdk_warned:
            _sdk_warned = True
            logger.warning(
                "未安装 mcp SDK，MCP 技能不可用（需要时执行 pip install mcp 并重启）；其它技能不受影响"
            )
        return None
    return _Sdk(
        ClientSession=ClientSession,
        StdioServerParameters=StdioServerParameters,
        stdio_client=stdio_client,
        sse_client=sse_client,
    )


def _run(coro: Any, timeout: float) -> Any:
    """在同步上下文里跑一段异步 MCP 会话（调用方在线程池里，不会与已有事件循环冲突）。"""

    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout=max(1.0, float(timeout)))

    return asyncio.run(_wrapped())


@asynccontextmanager
async def _session(sdk: _Sdk, server: McpServerConfig) -> AsyncIterator[Any]:
    """打开一个 MCP 会话（stdio 或 sse），用完自动关闭子进程 / 连接。"""
    if server.transport == "sse":
        async with sdk.sse_client(server.url) as (read, write):
            async with sdk.ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return
    parameters = sdk.StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env={**os.environ, **server.env} if server.env else None,
    )
    async with sdk.stdio_client(parameters) as (read, write):
        async with sdk.ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _render_result(result: Any) -> str:
    """把 MCP 调用结果（content 列表）拼成纯文本。图片等非文本内容只留占位说明。"""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
            continue
        kind = getattr(item, "type", "") or type(item).__name__
        parts.append(f"[{kind}]")
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        text = str(result)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        return "【MCP 错误】" + text[:MAX_RESULT_CHARS]
    return text[:MAX_RESULT_CHARS]


async def _list_tools_async(sdk: _Sdk, server: McpServerConfig) -> list[tuple[str, str, dict[str, Any]]]:
    async with _session(sdk, server) as session:
        result = await session.list_tools()
        tools = getattr(result, "tools", None) or []
        out: list[tuple[str, str, dict[str, Any]]] = []
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            description = str(getattr(tool, "description", "") or "").strip()
            schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}}
            out.append((name, description, schema))
        return out


class McpTool(BaseSkill):
    """一个 MCP 工具（``call`` 时按需重连 server 再调用，避免长期持有子进程）。"""

    kind = "mcp"

    def __init__(
        self,
        server: McpServerConfig,
        raw_name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> None:
        self.server = server
        self.raw_name = raw_name
        self.name = tool_name(server.name, raw_name)
        self.title = f"{server.name} / {raw_name}"
        self.description = description or f"外部 MCP 工具 {raw_name}（来自 {server.name}）"
        self.parameters = parameters
        self.source = f"mcp:{server.name}"

    async def _invoke(self, args: dict[str, Any]) -> str:
        sdk = _sdk()
        if sdk is None:
            raise RuntimeError("未安装 mcp SDK")
        async with _session(sdk, self.server) as session:
            result = await session.call_tool(self.raw_name, arguments=args)
            return _render_result(result)

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        started = time.perf_counter()
        try:
            text = _run(self._invoke(dict(args or {})), self.server.timeout)
        except Exception as exc:  # noqa: BLE001 - 外部服务出错不能影响整轮问答
            logger.warning("MCP 工具调用失败（%s / %s）：%s", self.server.name, self.raw_name, exc)
            ctx.records.append({"tool": self.name, "kind": "mcp", "status": "error", "error": str(exc)[:200]})
            return f"【MCP:{self.server.name}】工具 {self.raw_name} 调用失败（{type(exc).__name__}）。请如实说明。"
        finally:
            ctx.add_time(f"mcp:{self.server.name}", (time.perf_counter() - started) * 1000)
        ctx.records.append({"tool": self.name, "kind": "mcp", "args": dict(args or {})})
        return text


class McpRegistry:
    """读取 MCP 配置并批量连接，返回包装好的 :class:`BaseSkill` 列表。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.tools: dict[str, BaseSkill] = {}
        #: 最近一次 load 的失败原因（前端 / 健康检查可以展示）
        self.errors: list[str] = []

    @property
    def config_path(self) -> Path | None:
        raw = str(getattr(self.settings, "mcp_config_file", "") or "").strip()
        return Path(raw) if raw else None

    @property
    def sdk_available(self) -> bool:
        return _sdk() is not None

    def load(self) -> list[BaseSkill]:
        """连接所有启用的 server 并列出工具；任何失败都只跳过对应 server。"""
        path = self.config_path
        if path is None:
            return []
        servers = load_config(path)
        if not servers:
            return []
        sdk = _sdk()
        if sdk is None:
            self.errors = ["mcp SDK 未安装"]
            return []
        default_timeout = float(getattr(self.settings, "skill_timeout_seconds", 0) or 0) or 20.0
        loaded: dict[str, BaseSkill] = {}
        errors: list[str] = []
        for server in servers:
            if not server.enabled:
                logger.info("MCP server「%s」在配置里是停用状态，跳过", server.name)
                continue
            try:
                specs = _run(_list_tools_async(sdk, server), server.timeout or default_timeout)
            except Exception as exc:  # noqa: BLE001 - 单个 server 失败不影响其它
                logger.warning("连接 MCP server「%s」失败：%s", server.name, exc)
                errors.append(f"{server.name}: {type(exc).__name__}")
                continue
            for raw_name, description, parameters in specs:
                tool = McpTool(server, raw_name, description, parameters)
                if tool.name in loaded:  # 名字冲突时加后缀，保证可调用
                    tool.name = f"{tool.name[:60]}_{len(loaded):02d}"
                loaded[tool.name] = tool
            logger.info("MCP server「%s」已接入 %d 个工具", server.name, len(specs))
        self.tools = loaded
        self.errors = errors
        return list(loaded.values())

    def close(self) -> None:
        self.tools = {}


__all__ = [
    "MCP_PREFIX",
    "McpRegistry",
    "McpServerConfig",
    "McpTool",
    "load_config",
    "tool_name",
]