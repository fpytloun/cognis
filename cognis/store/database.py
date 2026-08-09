"""SQLAlchemy 2.x async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from prometheus_client import Counter, Gauge
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from cognis.logging import get_logger

logger = get_logger(__name__)

DB_POOL_CHECKED_OUT = Gauge(
    "cognis_db_pool_checked_out",
    "Current SQLAlchemy database connections checked out by this process",
)
DB_POOL_CONNECTIONS_TOTAL = Counter(
    "cognis_db_pool_connections_total",
    "SQLAlchemy database connections created by this process",
)
DB_POOL_CONNECTIONS_CLOSED_TOTAL = Counter(
    "cognis_db_pool_connections_closed_total",
    "SQLAlchemy database connections closed by this process",
)
DB_POOL_INVALIDATIONS_TOTAL = Counter(
    "cognis_db_pool_invalidations_total",
    "SQLAlchemy database connections invalidated by this process",
)


def pool_snapshot(engine: AsyncEngine) -> dict[str, int]:
    """Return bounded QueuePool state for diagnostics."""
    pool = engine.sync_engine.pool
    snapshot: dict[str, int] = {}
    for name in ("size", "checkedin", "checkedout", "overflow"):
        value = getattr(pool, name, None)
        if callable(value):
            snapshot[name] = int(value())
    return snapshot


def _instrument_pool(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _connection_created(_: object, __: object) -> None:
        DB_POOL_CONNECTIONS_TOTAL.inc()

    @event.listens_for(engine.sync_engine, "close")
    def _connection_closed(_: object, __: object) -> None:
        DB_POOL_CONNECTIONS_CLOSED_TOTAL.inc()

    @event.listens_for(engine.sync_engine, "checkout")
    def _connection_checked_out(_: object, __: object, ___: object) -> None:
        DB_POOL_CHECKED_OUT.inc()

    @event.listens_for(engine.sync_engine, "checkin")
    def _connection_checked_in(_: object, __: object) -> None:
        DB_POOL_CHECKED_OUT.dec()

    @event.listens_for(engine.sync_engine, "invalidate")
    def _connection_invalidated(_: object, __: object, ___: object) -> None:
        DB_POOL_INVALIDATIONS_TOTAL.inc()


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    Configures connection pooling and SQLite-specific settings.
    """
    connect_args: dict[str, object] = {}
    pool_kwargs: dict[str, object] = {}

    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        pool_kwargs["poolclass"] = NullPool
    else:
        pool_kwargs["pool_size"] = 5
        pool_kwargs["max_overflow"] = 10
        pool_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(
        database_url,
        connect_args=connect_args,
        echo=False,
        **pool_kwargs,
    )
    _instrument_pool(engine)

    # Enable WAL mode for SQLite to prevent "database is locked" errors
    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: object, _: object) -> None:
            cursor = cast(Any, dbapi_conn).cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the engine."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions with automatic rollback on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_connection(engine: AsyncEngine) -> bool:
    """Verify database connectivity."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database connection check failed")
        return False
