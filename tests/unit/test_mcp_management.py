from __future__ import annotations

from pathlib import Path

import pytest

from cognis.bootstrap import run_schema_bootstrap
from cognis.core.mcp_management import (
    MCPManagementDependencies,
    MCPManagementError,
    handle_mcp_management_action,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_executor,
    create_mcp_server,
    create_user,
    get_mcp_server,
)
from cognis.tools.builtin.mcp_management import MANAGE_MCP_TOOL


async def _deps(tmp_path: Path) -> MCPManagementDependencies:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/mcp-management.db")
    await run_schema_bootstrap(engine)
    deps = MCPManagementDependencies(session_factory=create_session_factory(engine))
    async with deps.session_factory() as session:
        await create_user(session, email="owner@example.com", name="Owner", password_hash="hash")
        await create_user(session, email="other@example.com", name="Other", password_hash="hash")
        await create_executor(
            session,
            executor_id="owner-executor",
            name="Owner executor",
            executor_type="websocket",
            owner_email="owner@example.com",
        )
        await create_mcp_server(
            session,
            server_id="mcp_oura",
            name="Oura",
            transport="streamable_http",
            url="https://mcp.oura.test",
            auth_config={
                "type": "oauth2",
                "issuer": "https://auth.oura.test",
                "client_id": "cognis",
                "client_secret_ref": "legacy-literal",
                "authorization_params": {"audience_token": "legacy-audience"},
            },
            owner_email="owner@example.com",
        )
        await create_mcp_server(
            session,
            server_id="mcp_other",
            name="Other",
            transport="streamable_http",
            url="https://mcp.other.test",
            owner_email="other@example.com",
        )
        await session.commit()
    return deps


def test_manage_mcp_uses_native_operation_descriptors() -> None:
    descriptor = MANAGE_MCP_TOOL.descriptor
    assert descriptor is not None
    assert MANAGE_MCP_TOOL.description.startswith("Manage private Cognis MCP servers")
    assert {operation.operation for operation in descriptor.operations} >= {
        "servers_create",
        "assignments_add",
        "oauth_authorize",
        "oauth_status",
        "oauth_disconnect",
    }
    authorize = next(
        operation for operation in descriptor.operations if operation.operation == "oauth_authorize"
    )
    assert authorize.mutation_kind.value == "execute"
    assert authorize.side_effects


@pytest.mark.asyncio
async def test_create_server_accepts_executor_only_timeout_fields(tmp_path: Path) -> None:
    deps = await _deps(tmp_path)
    result = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={
            "action": "servers_create",
            "server_id": "mcp_created",
            "name": "Created",
            "transport": "streamable_http",
            "url": "https://mcp.created.test",
            "timeout_seconds": 45,
        },
    )
    assert result["status"] == "created"
    assert result["server"]["server_id"] == "mcp_created"
    assert result["server"]["timeout_seconds"] == 45


@pytest.mark.asyncio
async def test_update_is_revisioned_and_preserves_redacted_oauth_values(
    tmp_path: Path,
) -> None:
    deps = await _deps(tmp_path)
    current = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={"action": "servers_get", "server_id": "mcp_oura"},
    )
    server = current["server"]
    assert server["auth_config"]["client_secret_ref"] == "***"
    assert server["auth_config"]["authorization_params"]["audience_token"] == "***"

    updated = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={
            "action": "servers_update",
            "server_id": "mcp_oura",
            "expected_updated_at": server["updated_at"],
            "description": "Updated",
            "auth_config": server["auth_config"],
        },
    )
    assert updated["server"]["description"] == "Updated"
    async with deps.session_factory() as session:
        row = await get_mcp_server(
            session,
            "mcp_oura",
            owner_email="owner@example.com",
            include_shared=False,
        )
    assert row is not None
    assert row.auth_config["client_secret_ref"] == "legacy-literal"
    assert row.auth_config["authorization_params"]["audience_token"] == "legacy-audience"

    with pytest.raises(MCPManagementError, match="changed"):
        await handle_mcp_management_action(
            deps=deps,
            actor_email="owner@example.com",
            arguments={
                "action": "servers_update",
                "server_id": "mcp_oura",
                "expected_updated_at": server["updated_at"],
                "description": "Stale",
            },
        )


@pytest.mark.asyncio
async def test_assignment_is_owner_scoped_idempotent_and_versioned(tmp_path: Path) -> None:
    deps = await _deps(tmp_path)
    result = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={
            "action": "assignments_add",
            "executor_id": "owner-executor",
            "server_ids": ["mcp_oura", "mcp_oura"],
            "expected_config_version": 0,
        },
    )
    assert result == {
        "status": "updated",
        "executor_id": "owner-executor",
        "server_ids": ["mcp_oura"],
        "desired_config_version": 1,
    }

    unchanged = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={
            "action": "assignments_add",
            "executor_id": "owner-executor",
            "server_ids": ["mcp_oura"],
            "expected_config_version": 1,
        },
    )
    assert unchanged["status"] == "unchanged"
    assert unchanged["desired_config_version"] == 1

    with pytest.raises(MCPManagementError, match="MCP server not found"):
        await handle_mcp_management_action(
            deps=deps,
            actor_email="owner@example.com",
            arguments={
                "action": "assignments_add",
                "executor_id": "owner-executor",
                "server_ids": ["mcp_other"],
                "expected_config_version": 1,
            },
        )


class _AuthorizationStart:
    authorization_url = "https://auth.oura.test/authorize"
    transaction_id = "tx_oura"
    issuer = "https://auth.oura.test"
    authorization_server = "https://auth.oura.test"
    scopes = ["daily"]
    resource = "https://mcp.oura.test"
    flow = "authorization_code"
    verification_uri = None
    verification_uri_complete = None
    user_code = None
    interval = None
    callback_mode = "controller_public"
    oauth_executor_id = None
    oauth_executor_name = None
    redirect_uri = "https://cognis.test/api/v1/mcp/oauth/callback"
    instructions = "Open the authorization URL."

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.expires_at = datetime(2030, 1, 1, tzinfo=UTC)


class _OAuthService:
    async def start_authorization(self, *, user_email: str, server_id: str) -> _AuthorizationStart:
        assert user_email == "owner@example.com"
        assert server_id == "mcp_oura"
        return _AuthorizationStart()


@pytest.mark.asyncio
async def test_oauth_authorize_returns_browser_callback_metadata(tmp_path: Path) -> None:
    deps = await _deps(tmp_path)
    deps.oauth_service = _OAuthService()
    result = await handle_mcp_management_action(
        deps=deps,
        actor_email="owner@example.com",
        arguments={"action": "oauth_authorize", "server_id": "mcp_oura"},
    )
    assert result["status"] == "authorization_pending"
    assert result["authorization_url"] == "https://auth.oura.test/authorize"
    assert result["redirect_uri"] == "https://cognis.test/api/v1/mcp/oauth/callback"
    assert result["transaction_id"] == "tx_oura"
