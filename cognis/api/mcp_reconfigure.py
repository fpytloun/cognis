"""Helpers for refreshing executor MCP runtime state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.logging import get_logger
from cognis.store.models import MCPOAuthTransactionRow
from cognis.store.queries import (
    bump_executor_reconfigure_generation,
    list_websocket_executors_for_mcp_server,
)

logger = get_logger(__name__)


async def schedule_mcp_server_executor_reconfigure_for_app(
    app: Any,
    *,
    server_id: str,
    reason: str,
    log_context: dict[str, Any] | None = None,
    admission_guard: Any | None = None,
    terminal_cleanup_transaction_id: str | None = None,
) -> list[str] | None:
    """Bump and schedule websocket executors that reference an MCP server."""

    async with app.state.session_factory() as session:
        if admission_guard is not None and not await admission_guard(session):
            await session.rollback()
            return None
        cleanup_row = None
        bump_generation = True
        if terminal_cleanup_transaction_id is not None:
            cleanup_row = await session.scalar(
                select(MCPOAuthTransactionRow)
                .where(MCPOAuthTransactionRow.transaction_id == terminal_cleanup_transaction_id)
                .with_for_update()
            )
            if (
                cleanup_row is None
                or cleanup_row.status not in {"completed", "failed"}
                or not cleanup_row.terminal_cleanup_required
            ):
                await session.rollback()
                return []
            if cleanup_row.terminal_reconfigure_completed_at is not None:
                await session.rollback()
                return []
            bump_generation = cleanup_row.terminal_reconfigure_applied_at is None
        executors = await list_websocket_executors_for_mcp_server(session, server_id)
        scheduled_ids: list[str] = []
        ws_provider = app.state.providers.executor.websocket
        for row in executors:
            connected = ws_provider.get_connection(row.executor_id)
            if bump_generation:
                updated = await bump_executor_reconfigure_generation(
                    session,
                    row.executor_id,
                    runtime_state="reconfiguring" if connected is not None else "stale",
                )
                if updated:
                    scheduled_ids.append(row.executor_id)
            elif int(row.desired_config_version or 0) > int(row.applied_config_version or 0):
                scheduled_ids.append(row.executor_id)
        if cleanup_row is not None and bump_generation:
            cleanup_row.terminal_reconfigure_applied_at = datetime.now(UTC)
        await session.commit()
    for executor_id in scheduled_ids:
        schedule_executor_reconfigure(app, executor_id)
    recovery_ids: list[str] = []
    if terminal_cleanup_transaction_id is not None and not scheduled_ids:
        async with app.state.session_factory() as session:
            cleanup_row = await session.scalar(
                select(MCPOAuthTransactionRow)
                .where(MCPOAuthTransactionRow.transaction_id == terminal_cleanup_transaction_id)
                .with_for_update()
            )
            evidence_rows = await list_websocket_executors_for_mcp_server(
                session,
                server_id,
                for_update=True,
            )
            recovery_ids = [
                row.executor_id
                for row in evidence_rows
                if int(row.desired_config_version or 0) > int(row.applied_config_version or 0)
            ]
            if (
                cleanup_row is not None
                and cleanup_row.terminal_reconfigure_applied_at is not None
                and cleanup_row.terminal_reconfigure_completed_at is None
                and not recovery_ids
            ):
                cleanup_row.terminal_reconfigure_completed_at = datetime.now(UTC)
                await session.commit()
            else:
                await session.rollback()
        for executor_id in recovery_ids:
            schedule_executor_reconfigure(app, executor_id)
    if scheduled_ids:
        extra_data: dict[str, Any] = {
            "server_id": server_id,
            "reason": reason,
            "executor_ids": scheduled_ids,
        }
        if log_context:
            extra_data.update(log_context)
        logger.info(
            "mcp: scheduled executor reconfigure after server state change",
            extra={"extra_data": extra_data},
        )
    return scheduled_ids or recovery_ids
