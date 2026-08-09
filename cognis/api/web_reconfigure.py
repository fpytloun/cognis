"""Helpers for refreshing executor web runtime state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text

from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.logging import get_logger
from cognis.store.queries import (
    bump_executor_reconfigure_generation,
    list_active_websocket_executors,
)

logger = get_logger(__name__)
_WEB_SETTINGS_ADVISORY_LOCK_ID = 0x434F474E49535745


@asynccontextmanager
async def web_settings_distributed_lock(session_factory: Any):
    """Hold one PostgreSQL advisory lock across web settings and secret mutations."""

    async with session_factory() as lock_session:
        get_bind = getattr(lock_session, "get_bind", None)
        if not callable(get_bind):
            yield
            return
        bind = get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            yield
            return
        connection = await lock_session.connection()
        await connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _WEB_SETTINGS_ADVISORY_LOCK_ID},
        )
        try:
            yield
        finally:
            unlock_task = asyncio.create_task(
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _WEB_SETTINGS_ADVISORY_LOCK_ID},
                )
            )
            try:
                await asyncio.shield(unlock_task)
            except asyncio.CancelledError:
                try:
                    await unlock_task
                except BaseException:
                    await asyncio.shield(connection.invalidate())
                raise
            except BaseException:
                await asyncio.shield(connection.invalidate())
                raise


async def schedule_web_executor_reconfigure_for_app(
    app: Any,
    *,
    reason: str,
) -> list[str]:
    """Bump and schedule every active websocket executor after a web config change."""

    async with app.state.session_factory() as session:
        executors = await list_active_websocket_executors(session, for_update=True)
        ws_provider = app.state.providers.executor.websocket
        scheduled_ids: list[str] = []
        for row in executors:
            connected = ws_provider.get_connection(row.executor_id)
            updated = await bump_executor_reconfigure_generation(
                session,
                row.executor_id,
                runtime_state="reconfiguring" if connected is not None else "stale",
            )
            if updated:
                scheduled_ids.append(row.executor_id)
        await session.commit()

    for executor_id in scheduled_ids:
        try:
            schedule_executor_reconfigure(app, executor_id)
        except Exception:
            logger.exception(
                "web: failed to schedule executor reconfigure",
                extra={
                    "extra_data": {
                        "reason": reason,
                        "executor_id": executor_id,
                    }
                },
            )

    if scheduled_ids:
        logger.info(
            "web: scheduled executor reconfigure after settings change",
            extra={
                "extra_data": {
                    "reason": reason,
                    "executor_ids": scheduled_ids,
                }
            },
        )
    return scheduled_ids


async def finalize_web_executor_reconfigure_for_app(
    app: Any,
    *,
    reason: str,
) -> list[str]:
    """Cancellation-safely persist and schedule executor refresh after a committed mutation."""

    task = asyncio.create_task(
        schedule_web_executor_reconfigure_for_app(app, reason=reason),
        name=f"web-executor-reconfigure:{reason}",
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            logger.exception(
                "web: executor reconfigure finalization failed during cancellation",
                extra={"extra_data": {"reason": reason}},
            )
        raise


async def run_web_mutation_cancellation_safe[T](
    operation: Callable[[], Awaitable[T]],
    *,
    reason: str,
) -> T:
    """Finish a runtime-affecting web mutation before propagating caller cancellation."""

    task = asyncio.create_task(operation(), name=f"web-mutation:{reason}")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            logger.exception(
                "web: mutation finalization failed during cancellation",
                extra={"extra_data": {"reason": reason}},
            )
        raise
