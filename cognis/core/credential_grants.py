"""Helpers for keeping agent credential grants in sync with created credentials."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.store.queries import get_agent, update_agent


def grant_credential_to_agent_definition(
    agent: AgentDefinition,
    credential_id: str,
) -> bool:
    """Grant a credential to an in-memory agent definition."""

    normalized_id = credential_id.strip()
    if not normalized_id:
        return False
    permissions = agent.permissions or AgentPermissions()
    allowed = list(permissions.allowed_credentials or [])
    if normalized_id in allowed:
        agent.permissions = permissions
        return False
    allowed.append(normalized_id)
    agent.permissions = permissions.model_copy(update={"allowed_credentials": allowed})
    return True


async def grant_credential_to_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    credential_id: str,
    owner_email: str | None = None,
) -> bool:
    """Persistently grant a credential ID to an agent's allowed credentials."""

    normalized_id = credential_id.strip()
    if not agent_id or not normalized_id:
        return False
    row = await get_agent(session, agent_id)
    if row is None:
        return False
    if owner_email is not None and row.owner_email != owner_email:
        return False

    permissions_json: dict[str, Any] = dict(row.permissions or {})
    raw_allowed = permissions_json.get("allowed_credentials")
    allowed = [str(item) for item in raw_allowed] if isinstance(raw_allowed, list) else []
    if normalized_id in allowed:
        return False

    allowed.append(normalized_id)
    permissions_json["allowed_credentials"] = allowed
    await update_agent(session, agent_id, updates={"permissions": permissions_json})
    return True
