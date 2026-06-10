"""Executor policy and user-scope enforcement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.tool import MCP_SERVER_IDS_KEY
from cognis.ownership import is_shared_owner_email
from cognis.store.queries import get_mcp_server, get_setting_value

logger = get_logger(__name__)


@dataclass(frozen=True)
class ExecutorPolicy:
    allow_in_process: bool = True
    allow_subprocess: bool = True


async def load_executor_policy(
    session_factory: async_sessionmaker[AsyncSession],
) -> ExecutorPolicy:
    async with session_factory() as session:
        allow_in_process = bool(
            await get_setting_value(session, "executors.allow_in_process", True)
        )
        allow_subprocess = bool(
            await get_setting_value(session, "executors.allow_subprocess", True)
        )
    return ExecutorPolicy(
        allow_in_process=allow_in_process,
        allow_subprocess=allow_subprocess,
    )


def is_executor_type_allowed(executor_type: str, policy: ExecutorPolicy) -> bool:
    if executor_type == "in_process":
        return policy.allow_in_process
    if executor_type == "subprocess":
        return policy.allow_subprocess
    return True


def is_executor_row_usable(
    row: Any, policy: ExecutorPolicy, *, owner_email: str | None = None
) -> bool:
    if row is None:
        return False
    row_owner_email = getattr(row, "owner_email", None)
    if (
        owner_email is not None
        and row_owner_email != owner_email
        and not is_shared_owner_email(row_owner_email)
    ):
        return False
    if getattr(row, "status", None) != "active":
        return False
    return is_executor_type_allowed(getattr(row, "executor_type", ""), policy)


def ensure_executor_type_allowed(executor_type: str, policy: ExecutorPolicy) -> None:
    if not is_executor_type_allowed(executor_type, policy):
        msg = f"Executor type '{executor_type}' is disabled by deployment policy"
        raise ValueError(msg)


async def validate_executor_mcp_scope(
    session: AsyncSession,
    *,
    owner_email: str,
    config: dict[str, Any] | None,
) -> None:
    ids = (config or {}).get(MCP_SERVER_IDS_KEY, [])
    if not isinstance(ids, list):
        msg = f"{MCP_SERVER_IDS_KEY} must be a list"
        raise ValueError(msg)
    for server_id in ids:
        row = await get_mcp_server(
            session,
            str(server_id),
            owner_email=owner_email,
            include_shared=not is_shared_owner_email(owner_email),
        )
        if row is None:
            msg = f"MCP server '{server_id}' is not available for this user"
            raise ValueError(msg)
        if is_shared_owner_email(owner_email) and not is_shared_owner_email(
            getattr(row, "owner_email", None)
        ):
            msg = f"MCP server '{server_id}' is private and cannot be assigned to a shared executor"
            raise ValueError(msg)
