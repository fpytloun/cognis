"""SQLAlchemy 2.x async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cognis.logging import get_logger

logger = get_logger(__name__)


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    Configures connection pooling and SQLite-specific settings.
    """
    connect_args: dict[str, object] = {}
    pool_kwargs: dict[str, object] = {}

    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        pool_kwargs["pool_size"] = 1
        pool_kwargs["max_overflow"] = 0
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
