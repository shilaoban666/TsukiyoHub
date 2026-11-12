"""MCP 接入单测：配置解析、缺 SDK / 缺配置时的优雅降级、工具包装。"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
from tsukiyohub.kernel.skills import ToolContext
from tsukiyohub.kernel.skills import mcp as mcp_module
from tsukiyohub.kernel.skills.mcp import (
    McpRegistry,
    McpServerConfig,
    McpTool,
    load_config,
    tool_name,
)


def make_ctx() -> ToolContext:
    return ToolContext(question="问题", user_ctx={}, cfg={}, pref={})


def write_config(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_config_parses_stdio_and_sse(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "servers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
                    "env": {"LOG_LEVEL": "info"},
                },
                "search": {"url": "http://127.0.0.1:7801/sse"},
                "off": {"command": "sleep", "enabled": False},
            }
        },
    )
    servers = {server.name: server for server in load_config(path)}
    assert set(servers) == {"filesystem", "search", "off"}
    filesystem = servers["filesystem"]
    assert filesystem.transport == "stdio"
    assert filesystem.command == "npx"
    assert filesystem.args[-1] == "/data"
    assert filesystem.env == {"LOG_LEVEL": "info"}
    assert filesystem.enabled is True
    assert servers["search"].transport == "sse"
    assert servers["search"].url.endswith("/sse")
    assert servers["off"].enabled is False


def test_load_config_skips_incomplete_server(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"servers": {"broken": {"transport": "stdio"}}})
    assert load_config(path) == []


def test_load_config_handles_missing_and_bad_files(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_config(bad) == []
    wrong_shape = write_config(tmp_path, {"servers": ["oops"]})
    assert load_config(wrong_shape) == []


def test_tool_name_prefix_and_sanitize() -> None:
    assert tool_name("filesystem", "read_file") == "mcp__filesystem__read_file"
    assert tool_name("my server", "do/thing") == "mcp__my_server__do_thing"
    assert len(tool_name("s" * 60, "t" * 60)) <= 64


def test_registry_without_config_file() -> None:
    settings = types.SimpleNamespace(mcp_config_file="", skill_timeout_seconds=5.0)
    registry = McpRegistry(settings)
    assert registry.config_path is None
    assert registry.load() == []


def test_registry_degrades_without_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, {"servers": {"fs": {"command": "npx"}}})
    settings = types.SimpleNamespace(mcp_config_file=str(path), skill_timeout_seconds=5.0)
    monkeypatch.setattr(mcp_module, "_sdk", lambda: None)
    registry = McpRegistry(settings)
    assert registry.load() == []
    assert registry.errors == ["mcp SDK 未安装"]


def test_registry_wraps_server_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(
        tmp_path,
        {"servers": {"fs": {"command": "npx", "args": ["server"], "timeout": 3}}},
    )
    settings = types.SimpleNamespace(mcp_config_file=str(path), skill_timeout_seconds=5.0)

    async def fake_list(sdk: Any, server: McpServerConfig) -> list[tuple[str, str, dict[str, Any]]]:
        assert server.name == "fs"
        return [("read_file", "读取文件", {"type": "object", "properties": {"path": {"type": "string"}}})]

    monkeypatch.setattr(mcp_module, "_sdk", lambda: object())
    monkeypatch.setattr(mcp_module, "_list_tools_async", fake_list)
    registry = McpRegistry(settings)
    tools = registry.load()
    assert [tool.name for tool in tools] == ["mcp__fs__read_file"]
    tool = tools[0]
    assert tool.kind == "mcp"
    assert tool.source == "mcp:fs"
    assert tool.spec["function"]["name"] == "mcp__fs__read_file"
    assert tool.spec["function"]["parameters"]["properties"]["path"]["type"] == "string"
    assert registry.errors == []
    registry.close()
    assert registry.tools == {}


def test_mcp_tool_call_is_graceful_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    server = McpServerConfig(name="fs", command="npx", timeout=1.0)
    tool = McpTool(server, "read_file", "读取文件", {"type": "object", "properties": {}})
    monkeypatch.setattr(mcp_module, "_sdk", lambda: None)
    ctx = make_ctx()
    text = tool.call({"path": "/tmp/a"}, ctx)
    assert "调用失败" in text
    assert ctx.records[-1]["status"] == "error"
    assert "mcp:fs" in ctx.timings