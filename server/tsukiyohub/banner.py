"""启动横幅：终端上只留结论，细节进日志。"""

from __future__ import annotations

_BOX_WIDTH = 64


def _line(char: str = "─", width: int = _BOX_WIDTH) -> str:
    return char * width


def _row(text: str, width: int = _BOX_WIDTH) -> str:
    """按显示宽度补齐（中文按 2 列算，避免框线错位）。"""
    visible = _display_width(text)
    padding = max(0, width - visible - 1)
    return f"│ {text}{' ' * padding}│"


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if _is_wide(ch) else 1
    return width


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAFF
    )


def print_banner(
    *,
    app_name: str,
    version: str,
    env: str,
    host: str,
    port: int,
    documents: int,
    chunks: int,
    vector_index: str,
    embedding: str,
    llm_provider: str,
    llm_model: str,
    llm_ready: bool,
    skills: int,
    database: str,
    web_url: str,
) -> None:
    """打印启动横幅（只到 stdout，不写日志文件）。"""
    llm_state = f"{llm_provider}:{llm_model}" if llm_ready else "未配置（降级为演示模型 / 原文摘录）"
    embedding_state = embedding if embedding else "关闭（纯关键词检索 BM25）"
    lines = [
        "",
        f"╭{_line()}╮",
        _row(f"🌙  {app_name}  v{version}   ·   env={env}"),
        _row("   与业务无关的 聊天 / 技能 / RAG 内核服务"),
        f"├{_line()}┤",
        _row(f"接口      http://127.0.0.1:{port}/        （文档 /docs）"),
        _row(f"前端      {web_url}"),
        _row(f"模型      {llm_state}"),
        _row(f"向量化    {embedding_state}"),
        _row(f"检索后端  {vector_index}   数据库  {database}"),
        _row(f"知识库    {documents} 篇文档 / {chunks} 个切块"),
        _row(f"技能      {skills} 个可用"),
        f"╰{_line()}╯",
        "",
    ]
    print("\n".join(lines), flush=True)


def print_shutdown(*, documents: int, chunks: int) -> None:
    print(f"\n索引已落盘：{documents} 篇文档 / {chunks} 个切块。再见 👋\n", flush=True)


__all__ = ["print_banner", "print_shutdown"]