"""Helpers for refreshing executor MCP runtime state."""

from __future__ import annotations

from typing import Any

from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.logging import get_logger
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
) -> list[str]:
    """Bump and schedule websocket executors that reference an MCP server."""

    async with app.state.session_factory() as session:
        executors = await list_websocket_executors_for_mcp_server(session, server_id)
        scheduled_ids: list[str] = []
        ws_provider = app.state.providers.executor.websocket
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
    return scheduled_ids
