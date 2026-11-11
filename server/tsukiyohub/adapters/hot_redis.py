"""热存储适配器：Redis 可用就用，不可用就退化为无操作。

Redis 在这里**只是可丢弃的加速层**：会话真相在数据库里，丢了不影响正确性。
所以连接失败一律降级，不抛异常、不阻塞启动。
"""

from __future__ import annotations

import json
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.hot import HotStore, NoopHotStore

logger = get_logger(__name__)


class RedisHotStore:
    """最小封装：get / set / delete / incr，全部带超时与异常吞掉。"""

    name = "redis"
    available = True

    def __init__(self, url: str, *, prefix: str = "tsukiyo:", timeout_ms: int = 50) -> None:
        import redis  # 延迟导入：没装 redis 包时仍能用 SQLite 跑

        self._prefix = prefix
        self._timeout = max(timeout_ms, 10) / 1000
        self._client = redis.Redis.from_url(
            url,
            socket_timeout=self._timeout,
            socket_connect_timeout=self._timeout,
            decode_responses=True,
        )
        self._client.ping()

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(self._key(key))
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 - 缓存故障回源
            return None

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        try:
            self._client.set(self._key(key), json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
        except Exception:  # noqa: BLE001
            return None

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._key(key))
        except Exception:  # noqa: BLE001
            return None

    def incr(self, key: str, ttl_seconds: int = 60) -> int:
        try:
            pipe = self._client.pipeline()
            pipe.incr(self._key(key))
            pipe.expire(self._key(key), ttl_seconds)
            value, _ = pipe.execute()
            return int(value)
        except Exception:  # noqa: BLE001
            return 1

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "available": self.available, "configured": True}


def build_hot_store(settings: Settings) -> HotStore:
    """按配置构造热存储。Redis 连不上就返回空实现，并记一条 warning。"""
    url = (settings.redis_url or "").strip()
    if not url:
        return NoopHotStore()
    try:
        store = RedisHotStore(url, prefix=settings.redis_prefix, timeout_ms=settings.redis_timeout_ms)
        logger.info("热存储：Redis（%s）", url.split("@")[-1])
        return store
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 不可用，改用数据库直读（不影响功能）：%s", exc)
        return NoopHotStore()


__all__ = ["RedisHotStore", "build_hot_store"]