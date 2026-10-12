import logging
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

log = logging.getLogger("kidsproxy.db")

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _factory
    if _engine is not None:
        raise RuntimeError("engine already initialised; call dispose_engine() first")
    _engine = create_async_engine(url or get_settings().database_url, pool_pre_ping=True)

    @event.listens_for(_engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        # WAL: better multi-process concurrency. busy_timeout: wait instead of
        # erroring on write contention (SQLite serializes writers — see README).
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")  # durability: ledger survives power loss
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = _factory = None


def get_engine() -> AsyncEngine:
    assert _engine is not None, "call init_engine() first"
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncSession:
    assert _factory is not None, "call init_engine() first"
    async with _factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
