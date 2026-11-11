"""原文件存储端口：知识库原文件、上传附件放这里。

默认实现是本地目录（:class:`LocalBlobStore`）；生产可换成 OSS / S3 / MinIO。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        """写入对象，返回可长期引用的 key / 路径。"""
        ...

    def get(self, key: str) -> bytes:
        ...

    def delete(self, key: str) -> bool:
        ...

    def exists(self, key: str) -> bool:
        ...

    def url(self, key: str) -> str:
        """给前端访问的地址；本地存储返回空串（由 API 代理下载）。"""
        ...


class LocalBlobStore:
    """把对象写到本地目录，key 即相对路径。"""

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # 防目录穿越：key 只能是相对路径
        target = (self.root / key.lstrip("/\\")).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError(f"非法的存储 key：{key}")
        return target

    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            return True
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def url(self, key: str) -> str:
        return ""

    def local_path(self, key: str) -> Path:
        """本地实现的特有能力：直接拿文件路径给解析器用。"""
        return self._path(key)


__all__ = ["BlobStore", "LocalBlobStore"]