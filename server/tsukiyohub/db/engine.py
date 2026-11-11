"""数据库引擎与会话管理（SQLAlchemy 2.0）。

为什么默认 SQLite
-----------------
「不装数据库也能跑」是本项目的硬约束：本地演示、单测、Docker 单容器部署都不该被迫先起
PostgreSQL。``Settings.resolved_database_url`` 在 ``DATABASE_URL`` 为空时回落到
``data/tsukiyohub.db``；生产环境填一个 ``postgresql+psycopg://...`` 即可，代码不用改。

为什么不引入 Alembic
--------------------
表结构只有四张、且是全局可重建的「状态」而非「用户数据主库」，本项目用 ``create_all``
启动建表；接成生产系统时再引入迁移工具，不在内核里提前付出复杂度。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tsukiyohub.db.models import Base, MessageRow
from tsukiyohub.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from tsukiyohub.config import Settings

logger = get_logger(__name__)


class Database:
    """一个数据库句柄：引擎 + 会话工厂 + 建表 / 健康检查。

    同一个进程只应该有一个实例（由 ``container`` 持有），
    :class:`~tsukiyohub.adapters.store_sql.SqlSessionStore` 等共享它。
    """

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        max_overflow: int = 20,
    ) -> None:
        self.url = url
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            # SQLite：允许跨线程使用（FastAPI 的线程池），并确保目录存在
            kwargs["connect_args"] = {"check_same_thread": False}
            self._ensure_sqlite_dir(url)
        else:
            kwargs.update(pool_size=max(1, int(pool_size)), max_overflow=max(0, int(max_overflow)),
                          pool_pre_ping=True)
        self.engine: Engine = create_engine(url, **kwargs)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @staticmethod
    def _ensure_sqlite_dir(url: str) -> None:
        """``sqlite:///C:/x/y.db`` → 创建 ``C:/x``（否则首次连接会报表不存在）。"""
        raw = url.split("sqlite:///", 1)[-1]
        if not raw or raw == ":memory:":
            return
        try:
            Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - 只读文件系统
            logger.warning("创建 SQLite 目录失败（%s）：%s", raw, exc)

    # ------------------------------------------------------------------ 会话
    @contextmanager
    def session(self) -> Iterator[Session]:
        """事务边界：正常结束提交，异常回滚，最后一定关闭。"""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ 建表
    def create_all(self) -> None:
        self._rebuild_outdated_message_table()
        Base.metadata.create_all(self.engine)

    def _rebuild_outdated_message_table(self) -> None:
        """开发期兼容：把没有 ``seq`` 列的旧消息表重建一次。

        消息表主键从「字符串 id」改成「自增 seq」是为了稳定排序（见 models.MessageRow），
        SQLite 不支持用 ALTER 改主键；本项目未发布、会话数据可重建，因此这里直接重建该表。
        生产环境引入迁移工具后应删除这段逻辑。
        """
        try:
            inspector = inspect(self.engine)
            if "tsukiyo_message" not in inspector.get_table_names():
                return
            columns = {column["name"] for column in inspector.get_columns("tsukiyo_message")}
            if "seq" in columns:
                return
            logger.warning("检测到旧版 tsukiyo_message 表（缺少 seq 列），重建该表以启用稳定排序")
            MessageRow.__table__.drop(self.engine, checkfirst=True)
        except Exception as exc:  # noqa: BLE001 - 探测失败就让 create_all 自己报错
            logger.warning("检查消息表结构失败（继续建表）：%s", exc)

    def drop_all(self) -> None:
        """仅测试 / 开发用。"""
        Base.metadata.drop_all(self.engine)

    def health(self) -> bool:
        """``SELECT 1`` 探活；异常时返回 False（``/readyz`` 用）。"""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("数据库健康检查失败：%s", exc)
            return False

    def dispose(self) -> None:
        self.engine.dispose()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<Database url={self.url!r}>"


def build_database(settings: Settings) -> Database:
    """按配置创建数据库句柄并建表（幂等）。"""
    url = settings.resolved_database_url
    database = Database(
        url,
        echo=bool(settings.db_echo),
        pool_size=int(settings.db_pool_size),
        max_overflow=int(settings.db_max_overflow),
    )
    database.create_all()
    logger.info("数据库就绪：%s", "SQLite" if url.startswith("sqlite") else "外部数据库")
    return database


__all__ = ["Database", "build_database"]