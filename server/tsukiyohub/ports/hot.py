"""热存储端口（Redis 可选）：会话状态、近期消息、去重、限流。

设计成「可丢弃的加速层」：读写失败由内核吞掉并回源数据库，
因此没有 Redis 也能完整工作（:class:`NoopHotStore` 就是空实现）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HotStore(Protocol):
    name: str
    available: bool

    def get(self, key: str) -> Any | None:
        ...

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        ...

    def delete(self, key: str) -> None:
        ...

    def incr(self, key: str, ttl_seconds: int = 60) -> int:
        """计数（限流用），返回自增后的值。"""
        ...

    def status(self) -> dict[str, Any]:
        ...


class NoopHotStore:
    """不缓存：所有读都回源数据库。"""

    name = "none"
    available = False

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        return None

    def delete(self, key: str) -> None:
        return None

    def incr(self, key: str, ttl_seconds: int = 60) -> int:
        return 1

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "available": False, "configured": False}


__all__ = ["HotStore", "NoopHotStore"]