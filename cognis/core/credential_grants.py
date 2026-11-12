"""Helpers for keeping agent credential grants in sync with created credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.store.queries import get_agent, update_agent, update_agent_if_updated_at


class CredentialGrantError(ValueError):
    """An owner-scoped credential assignment was denied."""


def credential_assignment_metadata(credential: Any, *, owner_email: str) -> dict[str, Any]:
    """Project only identity and policy fields, never secret payload or metadata."""
    if credential is None or credential.user_email != owner_email:
        raise CredentialGrantError("Credential not found or not owned")
    return {
        "credential_id": credential.credential_id,
        "kind": credential.kind,
        "scope": credential.scope,
        "agent_id": credential.agent_id,
        "status": credential.status,
        "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
    }


async def update_credential_assignment(
    session: AsyncSession,
    *,
    agent_id: str,
    owner_email: str,
    credential: Any,
    grant: bool,
    expected_updated_at: str,
) -> dict[str, Any]:
    """Change only the allowlist using CAS; cancellation rolls back the caller transaction.

    Values are never resolved. Scope/status remain enforced at every use by the
    provider, including when they change after this metadata check.
    A revision conflict requires a fresh read, not an automatic retry.
    """
    metadata = credential_assignment_metadata(credential, owner_email=owner_email)
    row = await get_agent(session, agent_id)
    if row is None or row.owner_email != owner_email or row.is_system:
        raise CredentialGrantError("Agent not found or not owned")
    try:
        expected = datetime.fromisoformat(expected_updated_at)
    except (TypeError, ValueError) as exc:
        raise CredentialGrantError("expected_updated_at must be an ISO-8601 timestamp") from exc
    actual = row.updated_at
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=UTC)
    if expected != actual:
        raise CredentialGrantError("Credential assignment conflict; re-read and retry")
    if grant:
        if credential.scope not in {"user", "agent"}:
            raise CredentialGrantError("Unsupported credential scope")
        if credential.scope == "agent" and credential.agent_id != agent_id:
            raise CredentialGrantError("Credential agent scope does not match")
        if credential.status != "active" or credential.revoked_at is not None:
            raise CredentialGrantError("Credential is inactive")
        expires_at = credential.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise CredentialGrantError("Credential is expired")
    permissions = dict(row.permissions or {})
    allowed = list(permissions.get("allowed_credentials") or [])
    credential_id = credential.credential_id
    changed = (credential_id not in allowed) if grant else (credential_id in allowed)
    if grant and changed:
        allowed.append(credential_id)
    elif not grant:
        allowed = [item for item in allowed if item != credential_id]
    permissions["allowed_credentials"] = allowed
    # CAS even for a no-op: a concurrent opposite operation must not report success.
    if not await update_agent_if_updated_at(
        session,
        agent_id,
        expected_updated_at=row.updated_at,
        updates={"permissions": permissions},
    ):
        raise CredentialGrantError("Credential assignment conflict; re-read and retry")
    await session.refresh(row)
    return {
        "agent_id": agent_id,
        "credential": metadata,
        "assigned": grant,
        "changed": changed,
        "updated_at": row.updated_at.isoformat(),
    }


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
