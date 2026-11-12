"""Owner- and agent-scoped bearer bindings for executor MCP connections."""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

from cognis import json_codec as json
from cognis.models.agent import AgentDefinition
from cognis.models.credential import CredentialAccessError, CredentialResolution
from cognis.models.tool import (
    MCP_SERVER_IDS_KEY,
    MCPAuthConfig,
    MCPServerConfig,
    effective_mcp_auth_config,
)
from cognis.ownership import is_shared_owner_email
from cognis.store.queries import get_agent, get_executor_row, get_mcp_server


def executor_supports_mcp_credentials(capabilities: Any) -> bool:
    """Require explicit current-connection support, never persisted version guesses."""
    if isinstance(capabilities, dict):
        return capabilities.get("mcp_credential_bindings") is True
    return getattr(capabilities, "mcp_credential_bindings", False) is True


async def resolve_mcp_credential(
    *,
    session_factory: Any,
    credentials: Any,
    owner_email: str,
    auth: MCPAuthConfig,
    caller_agent_id: str | None = None,
) -> CredentialResolution:
    """Resolve a binding using current durable grants, never caller-supplied permissions."""
    if (
        credentials is None
        or is_shared_owner_email(owner_email)
        or not auth.credential_agent_id
        or (caller_agent_id is not None and caller_agent_id != auth.credential_agent_id)
    ):
        raise CredentialAccessError(
            "mcp_credential_denied", "MCP credential binding is not authorized."
        )
    async with session_factory() as session:
        row = await get_agent(session, auth.credential_agent_id)
    if row is None or row.owner_email != owner_email or row.status != "active":
        raise CredentialAccessError(
            "mcp_credential_denied", "MCP credential binding is not authorized."
        )
    # Use only the durable identity and grants needed by the canonical credential resolver.
    agent = AgentDefinition(
        agent_id=row.agent_id,
        owner_email=owner_email,
        name=row.name,
        permissions=row.permissions or {},
    )
    credential_id = str(auth.credential_ref or "").removeprefix("$credential:").split(".", 1)[0]
    if agent.permissions is None or credential_id not in agent.permissions.allowed_credentials:
        raise CredentialAccessError(
            "credential_not_allowed", "MCP credential is not granted to the agent."
        )
    try:
        resolved = await credentials.resolve_ref(
            auth.credential_ref, agent=agent, user_email=owner_email
        )
    except CredentialAccessError:
        raise
    except Exception as exc:
        # Provider errors may contain secret-bearing payloads. Fail closed without echoing them.
        raise CredentialAccessError(
            "mcp_credential_unavailable", "MCP credential could not be resolved."
        ) from exc
    if not isinstance(resolved.value, str) or not re.fullmatch(
        r"[A-Za-z0-9._~+/-]+=*", resolved.value
    ):
        raise CredentialAccessError(
            "mcp_credential_invalid_token", "MCP credential is not a valid bearer token."
        )
    if not resolved.instance_id:
        raise CredentialAccessError(
            "mcp_credential_unavailable", "MCP credential generation is unavailable."
        )
    return cast(CredentialResolution, resolved)


def mcp_credential_revision(server: Any, resolved: CredentialResolution) -> dict[str, str | int]:
    """Bind the cached client to the reference, destination, headers and credential generation."""
    identity = json.dumps(
        {
            "server_id": server.server_id,
            "url": server.url,
            "transport": server.transport,
            "headers": server.headers or {},
            "auth_config": server.auth_config,
            "credential_instance": resolved.instance_id,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return {"token_id": f"credential:{digest}", "token_version": resolved.version}


async def authorize_mcp_dispatch(
    *, session_factory: Any, credentials: Any, executor_id: str, server_id: str, agent_id: str
) -> dict[str, str | int] | None:
    """Revalidate private binding and assignment at dispatch; return no secret values."""
    async with session_factory() as session:
        executor = await get_executor_row(session, executor_id)
        server = await get_mcp_server(session, server_id, include_shared=True)
    if server is None:
        raise CredentialAccessError("mcp_server_unavailable", "MCP server is unavailable.")
    auth = effective_mcp_auth_config(server.auth_config, server.headers)
    if auth.type != "credential_bearer":
        return None
    if (
        executor is None
        or executor.executor_type != "websocket"
        or executor.owner_email != server.owner_email
        or server.status != "active"
        or server_id not in (executor.config or {}).get(MCP_SERVER_IDS_KEY, [])
    ):
        raise CredentialAccessError(
            "mcp_credential_denied", "MCP credential executor is not authorized."
        )
    MCPServerConfig(
        name=server.name,
        transport=server.transport,
        url=server.url,
        headers=server.headers or {},
        auth_config=auth,
    )
    resolved = await resolve_mcp_credential(
        session_factory=session_factory,
        credentials=credentials,
        owner_email=server.owner_email,
        auth=auth,
        caller_agent_id=agent_id,
    )
    return mcp_credential_revision(server, resolved)


def mcp_credential_failure_status(server: Any, code: str) -> dict[str, Any]:
    """Return value-free runtime diagnostics for a rejected binding."""
    return {
        "server_id": server.server_id,
        "name": server.name,
        "status": "failed",
        "error": "MCP credential binding could not be authorized.",
        "error_class": code,
        "phase": "credential_resolution",
        "retryable": False,
    }
