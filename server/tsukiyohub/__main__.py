"""``python -m tsukiyohub`` —— 开发环境启动入口。"""

from __future__ import annotations

import argparse

import uvicorn

from tsukiyohub import __version__
from tsukiyohub.config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=f"TsukiyoHub v{__version__}")
    parser.add_argument("--host", default=settings.app_host, help="监听地址")
    parser.add_argument("--port", type=int, default=settings.app_port, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")
    args = parser.parse_args()

    uvicorn.run(
        "tsukiyohub.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()