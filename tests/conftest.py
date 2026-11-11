"""pytest 公共夹具：隔离数据目录 + 零依赖配置（假模型 / 内存检索 / SQLite）。

原则：**测试不连任何外部服务**，也不读写仓库里的 ``data/`` 目录。

两个环境适配（很关键）：
1. 一些受限环境（CI 容器 / Windows 沙箱）系统临时目录不可写，``tmp_path`` 会直接报错，
   所以这里用 ``mkdtemp`` 自己找位置，并把 ``TMPDIR/TEMP/TMP`` 指过去——jieba 之类
   会把缓存写临时目录，指错了会静默退化成二元组分词，检索测试会莫名其妙失败。
2. 容器以 **应用生命周期里构建的那个** 为准（``client`` 先起，``container`` 从 ``get_container()`` 取），
   否则 ``sample_docs`` 会写进另一个容器，接口读不到。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))


def _make_temp_dir() -> Path:
    """找一个**可写**的临时目录，并把它设成进程级临时目录。"""
    configured = os.environ.get("TSUKIYO_TEST_TMPDIR", "").strip()
    if configured:
        base = Path(configured)
        base.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="tsukiyo-test-", dir=base))
    else:
        try:
            path = Path(tempfile.mkdtemp(prefix="tsukiyo-test-"))
        except (OSError, FileNotFoundError):
            base = ROOT / ".pytest-tmp"
            base.mkdir(parents=True, exist_ok=True)
            path = Path(tempfile.mkdtemp(prefix="tsukiyo-test-", dir=base))
    # 让后续所有临时文件（jieba 缓存、pytest、库内部）都落在同一个可写目录
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(path)
    tempfile.tempdir = str(path)
    return path


_DATA_DIR = _make_temp_dir()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest 钩子签名
    """会话结束后清理临时数据目录（Windows 上文件句柄可能延迟释放，忽略失败）。"""
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


def pytest_configure(config):
    """把 pytest 自带的 ``--basetemp`` 指到可写目录（其它测试文件会用 ``tmp_path``）。"""
    if not getattr(config.option, "basetemp", None):
        config.option.basetemp = str(_DATA_DIR / "pytest-basetemp")


@pytest.fixture(scope="session", autouse=True)
def _isolated_env():
    """在导入应用之前把环境变量钉死，避免读到开发者本地的 .env。"""
    os.environ.update({
        "TSUKIYO_DATA_ROOT": str(_DATA_DIR / "data"),
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        # 无外部依赖：演示模型 + 确定性向量 + 内存检索 + SQLite
        "LLM_PROVIDER": "fake",
        "LLM_API_KEY": "",
        "EMBEDDING_ENABLED": "true",
        "EMBEDDING_PROVIDER": "hash",
        "EMBEDDING_DIMENSION": "256",
        "VECTOR_BACKEND": "memory",
        "DATABASE_URL": "",
        "REDIS_URL": "",
        "API_KEY": "",
        "MEMORY_ENABLED": "true",
        "ANSWER_JUDGE": "off",          # 假模型下跳过逐句核对，测试更快更稳
        "KB_AUTO_LOAD_INDEX": "false",  # 由测试显式入库
        "SCHEDULER_ENABLED": "false",
        "RATE_LIMIT_PER_MINUTE": "0",
    })
    from tsukiyohub.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def client(_isolated_env):
    """FastAPI 测试客户端（会执行 lifespan，容器在此时构建）。"""
    from fastapi.testclient import TestClient

    from tsukiyohub.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def container(client):  # noqa: ARG001 - 依赖 client 以触发 lifespan
    """与接口共用的那个容器（从 ``get_container()`` 取，保证是同一实例）。"""
    from tsukiyohub.container import get_container

    return get_container()


@pytest.fixture()
def sample_docs(container):
    """往知识库里放两篇小文档，返回 doc_id 列表。"""
    from tsukiyohub.domain import DocType

    results = [
        container.knowledge.ingest_bytes(
            "检索说明.md",
            "# 检索说明\n\nTsukiyoHub 使用向量召回与 BM25 关键词召回，再用加权 RRF 融合两路结果。".encode(),
            doc_type=DocType.DOC,
        ),
        container.knowledge.ingest_bytes(
            "部署说明.md",
            "# 部署说明\n\n生产环境把 VECTOR_BACKEND 设为 milvus，DATABASE_URL 指向 PostgreSQL。".encode(),
            doc_type=DocType.DOC,
        ),
    ]
    return [r.doc_id for r in results]