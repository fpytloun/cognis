from __future__ import annotations

import asyncio
import base64
import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.api.executor_ws import _resolve_executor_mcp_payload
from cognis.api.runtime_support import _merge_remote_runtime_inventory
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.executor_management import ExecutorManagementDependencies, provision_executor_token
from cognis.core.management_access import ManagementActor
from cognis.core.mcp_credentials import authorize_mcp_dispatch, resolve_mcp_credential
from cognis.core.mcp_management import (
    MCPManagementDependencies,
    MCPManagementError,
    handle_mcp_management_action,
)
from cognis.models.agent import AgentDefinition
from cognis.models.credential import CredentialAccessError
from cognis.models.tool import ExecutorCapabilities, MCPAuthConfig, MCPServerConfig
from cognis.providers.credentials.encrypted_db import EncryptedDBCredentialsProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_executor,
    create_mcp_server,
    create_user,
    get_agent,
    get_executor_row,
    get_mcp_server,
    update_agent,
)

OWNER = "owner@example.com"
AUTH = {
    "type": "credential_bearer",
    "credential_ref": "$credential:music-token.token",
    "credential_agent_id": "music-agent",
}


@pytest.fixture
async def binding(tmp_path: Path):
    postgres_url = os.environ.get("COGNIS_MCP_TEST_POSTGRES_URL")
    admin = None
    schema_name = f"mcp_credentials_{uuid.uuid4().hex}"
    if postgres_url:
        admin = create_async_engine(postgres_url)
        async with admin.begin() as connection:
            await connection.execute(sa_schema.CreateSchema(schema_name))
        engine = create_async_engine(
            postgres_url, connect_args={"server_settings": {"search_path": schema_name}}
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    else:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/binding.db")
        await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    (tmp_path / "secrets.key").write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    credentials = EncryptedDBCredentialsProvider(factory, str(tmp_path / "secrets.key"))
    async with factory() as session:
        await create_user(session, email=OWNER, name="Owner", password_hash="hash")
        await create_user(session, email="other@example.com", name="Other", password_hash="hash")
        await create_agent(
            session,
            agent_id="music-agent",
            owner_email=OWNER,
            name="Music",
            status="active",
            permissions={"allowed_credentials": ["music-token"]},
        )
        await create_executor(
            session,
            executor_id="music-executor",
            name="Music",
            executor_type="websocket",
            owner_email=OWNER,
            config={"mcp_server_ids": ["mcp_music"]},
        )
        await create_mcp_server(
            session,
            server_id="mcp_music",
            name="Music",
            transport="streamable_http",
            url="https://music.test/mcp",
            auth_config=AUTH,
            owner_email=OWNER,
        )
        await session.commit()
    await credentials.upsert_credential(
        credential_id="music-token",
        user_email=OWNER,
        kind="token",
        label="Music",
        payload={"token": "secret-music-token"},
    )
    providers = SimpleNamespace(
        _session_factory=factory,
        credentials=credentials,
        executor=SimpleNamespace(
            websocket=SimpleNamespace(
                get_connection=lambda _: SimpleNamespace(
                    capabilities=ExecutorCapabilities(mcp_credential_bindings=True)
                )
            )
        ),
    )
    yield factory, credentials, providers
    await engine.dispose()
    if admin is not None:
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


async def _resolve(binding, *, owner=OWNER, caller="music-agent"):
    factory, credentials, _ = binding
    return await resolve_mcp_credential(
        session_factory=factory,
        credentials=credentials,
        owner_email=owner,
        auth=MCPAuthConfig.model_validate(AUTH),
        caller_agent_id=caller,
    )


@pytest.mark.asyncio
async def test_binding_resolution_owner_agent_revocation_and_generation(binding):
    factory, credentials, _ = binding
    first = await _resolve(binding)
    assert first.value == "secret-music-token"
    assert first.version == 1
    for owner, caller in [("other@example.com", "music-agent"), (OWNER, "other-agent")]:
        with pytest.raises(CredentialAccessError):
            await _resolve(binding, owner=owner, caller=caller)
    await credentials.upsert_credential(
        credential_id="music-token",
        user_email=OWNER,
        kind="token",
        label="Music",
        payload={"token": "rotated-token"},
    )
    assert (await _resolve(binding)).version == 2
    async with factory() as session:
        await update_agent(
            session, "music-agent", updates={"permissions": {"allowed_credentials": []}}
        )
        await session.commit()
    with pytest.raises(CredentialAccessError, match="not granted"):
        await _resolve(binding)


@pytest.mark.asyncio
async def test_binding_missing_revoked_and_invalid_tokens_fail_closed(binding):
    _, credentials, _ = binding
    await credentials.revoke_credential("music-token", OWNER)
    with pytest.raises(CredentialAccessError):
        await _resolve(binding)
    for token in ["abc\r\nInjected: value", "abc def", "", 123, {"token": "abc"}]:
        await credentials.upsert_credential(
            credential_id="music-token",
            user_email=OWNER,
            kind="token",
            label="Music",
            payload={"token": token},
        )
        with pytest.raises(CredentialAccessError):
            await _resolve(binding)


@pytest.mark.asyncio
async def test_management_uses_reference_only_and_checks_caller(binding):
    factory, credentials, _ = binding
    deps = MCPManagementDependencies(
        session_factory=factory, credentials=credentials, caller_agent_id="music-agent"
    )
    args = {
        "action": "servers_create",
        "server_id": "mcp_created",
        "name": "Created",
        "transport": "streamable_http",
        "url": "https://music.test/mcp",
        "auth_config": AUTH,
    }
    result = await handle_mcp_management_action(deps=deps, actor_email=OWNER, arguments=args)
    assert "secret-music-token" not in str(result)
    async with factory() as session:
        server = await get_mcp_server(session, "mcp_created")
    assert server.auth_config["credential_ref"] == AUTH["credential_ref"]
    assert "secret-music-token" not in str(server.auth_config)
    deps.caller_agent_id = "other-agent"
    with pytest.raises(MCPManagementError):
        await handle_mcp_management_action(
            deps=deps, actor_email=OWNER, arguments={**args, "server_id": "mcp_denied"}
        )


async def _configurator_deps(binding):
    factory, credentials, _ = binding
    async with factory() as session:
        await create_agent(
            session,
            agent_id="config-agent",
            owner_email=OWNER,
            name="Configurator",
            status="active",
            permissions={"allowed_credentials": ["music-token"]},
            tools={"opt_in_builtin_tools": ["manage_mcp"]},
        )
        await session.commit()
    return MCPManagementDependencies(
        session_factory=factory, credentials=credentials, caller_agent_id="config-agent"
    )


async def _configuration_arguments(binding, action):
    factory, _, _ = binding
    if action == "servers_create":
        return {
            "action": action,
            "server_id": "mcp_consumer",
            "name": "Consumer Music",
            "transport": "streamable_http",
            "url": "https://music.test/mcp",
            "auth_config": dict(AUTH),
        }
    async with factory() as session:
        server = await get_mcp_server(session, "mcp_music")
    return {
        "action": action,
        "server_id": server.server_id,
        "expected_updated_at": server.updated_at.isoformat(),
        "description": "Configured for the consumer",
        "auth_config": dict(AUTH),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["servers_create", "servers_update"])
async def test_management_configurator_can_bind_an_independently_granted_consumer(
    binding, action, caplog
):
    factory, _, _ = binding
    deps = await _configurator_deps(binding)
    arguments = await _configuration_arguments(binding, action)
    original_auth = dict(arguments["auth_config"])
    result = await handle_mcp_management_action(deps=deps, actor_email=OWNER, arguments=arguments)
    assert not result.get("is_error")
    assert arguments["auth_config"] == original_auth == AUTH
    assert "secret-music-token" not in str(result) + caplog.text
    async with factory() as session:
        server = await get_mcp_server(session, arguments["server_id"])
        issuer = await get_agent(session, "config-agent")
        consumer = await get_agent(session, "music-agent")
    assert server.auth_config["credential_agent_id"] == "music-agent"
    assert server.auth_config["credential_ref"] == AUTH["credential_ref"]
    assert "secret-music-token" not in str(server.auth_config) + str(server.headers)
    assert issuer.permissions == consumer.permissions == {"allowed_credentials": ["music-token"]}
    assert not (consumer.tools or {}).get("opt_in_builtin_tools")
    # Granting the configurator does not make it a runtime consumer.
    with pytest.raises(CredentialAccessError):
        await resolve_mcp_credential(
            session_factory=factory,
            credentials=deps.credentials,
            owner_email=OWNER,
            auth=MCPAuthConfig.model_validate(server.auth_config),
            caller_agent_id="config-agent",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["servers_create", "servers_update"])
@pytest.mark.parametrize(
    ("principal", "updates"),
    [
        ("config-agent", {"permissions": {"allowed_credentials": []}}),
        ("music-agent", {"permissions": {"allowed_credentials": []}}),
        ("config-agent", {"status": "inactive"}),
        ("music-agent", {"status": "inactive"}),
        ("config-agent", {"owner_email": "other@example.com"}),
        ("music-agent", {"owner_email": "other@example.com"}),
    ],
)
async def test_management_requires_both_current_same_owner_active_grants(
    binding, action, principal, updates
):
    factory, _, _ = binding
    deps = await _configurator_deps(binding)
    arguments = await _configuration_arguments(binding, action)
    async with factory() as session:
        await update_agent(session, principal, updates=updates)
        await session.commit()
        before = await get_agent(session, principal)
        before_permissions = dict(before.permissions)
        original = await get_mcp_server(session, arguments["server_id"])
        original_auth = dict(original.auth_config) if original else None
        original_revision = original.updated_at if original else None
    with pytest.raises(MCPManagementError) as caught:
        await handle_mcp_management_action(deps=deps, actor_email=OWNER, arguments=arguments)
    assert "secret-music-token" not in str(caught.value)
    async with factory() as session:
        after = await get_mcp_server(session, arguments["server_id"])
        principal_row = await get_agent(session, principal)
        consumer = await get_agent(session, "music-agent")
    assert principal_row.permissions == before_permissions
    assert not (consumer.tools or {}).get("opt_in_builtin_tools")
    if original_auth is None:
        assert after is None
    else:
        assert after.auth_config == original_auth
        assert after.updated_at == original_revision
        assert after.description is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["servers_create", "servers_update"])
async def test_management_denies_a_foreign_credential_and_redacts_provider_errors(
    binding, action, caplog
):
    _, credentials, _ = binding
    deps = await _configurator_deps(binding)
    await credentials.revoke_credential("music-token", OWNER)
    await credentials.upsert_credential(
        credential_id="music-token",
        user_email="other@example.com",
        kind="token",
        label="Foreign Music",
        payload={"token": "foreign-music-token"},
    )
    arguments = await _configuration_arguments(binding, action)
    with pytest.raises(MCPManagementError):
        await handle_mcp_management_action(deps=deps, actor_email=OWNER, arguments=arguments)
    deps.credentials = SimpleNamespace(
        resolve_ref=AsyncMock(side_effect=RuntimeError("reflected secret-music-token"))
    )
    with pytest.raises(MCPManagementError) as caught:
        await handle_mcp_management_action(deps=deps, actor_email=OWNER, arguments=arguments)
    assert "secret-music-token" not in str(caught.value) + caplog.text
    assert "foreign-music-token" not in str(caught.value) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"parent_session_id": "parent-session"},
        {"workflow_step": True},
        {"agent_type": "secondary"},
        {"user_email": "other@example.com"},
    ],
)
async def test_cross_agent_binding_keeps_owner_root_management_gate(binding, changes):
    from cognis.runtime_context import RuntimeAccessContext
    from cognis.tools.builtin.mcp_management import handle_mcp_management_tool

    factory, credentials, _ = binding
    deps = await _configurator_deps(binding)
    resolve = AsyncMock(wraps=credentials.resolve_ref)
    deps.credentials = SimpleNamespace(resolve_ref=resolve)
    access = RuntimeAccessContext(
        user_email=OWNER, agent_id="config-agent", agent_owner_email=OWNER
    )
    result = await handle_mcp_management_tool(
        arguments=await _configuration_arguments(binding, "servers_create"),
        deps=deps,
        user_email=OWNER,
        runtime_access=replace(access, **changes),
    )
    assert result.is_error
    resolve.assert_not_awaited()
    async with factory() as session:
        assert await get_mcp_server(session, "mcp_consumer") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_principal", ["config-agent", "music-agent"])
async def test_ready_binding_rechecks_consumer_grant_not_configurator_grant(
    binding, revoked_principal
):
    factory, credentials, providers = binding
    deps = await _configurator_deps(binding)
    await handle_mcp_management_action(
        deps=deps,
        actor_email=OWNER,
        arguments=await _configuration_arguments(binding, "servers_update"),
    )
    executor = SimpleNamespace(
        executor_id="music-executor", owner_email=OWNER, config={"mcp_server_ids": ["mcp_music"]}
    )
    servers, _, metadata = await _resolve_executor_mcp_payload(executor, providers)
    assert len(servers) == 1
    ready_revision = metadata["mcp_credential_revisions"]["mcp_music"]
    async with factory() as session:
        await update_agent(
            session, revoked_principal, updates={"permissions": {"allowed_credentials": []}}
        )
        await session.commit()
    tool = {
        "name": "mcp_Music__players",
        "description": "Players",
        "source": {"type": "local_mcp", "server_id": "mcp_music", "server_name": "Music"},
    }
    inventory = await _merge_remote_runtime_inventory(
        remote_tools_data=[tool],
        agent_tools=[],
        providers=providers,
        # Deliberately retain the admitted agent's old grant: durable state must win.
        agent=AgentDefinition(
            agent_id="music-agent",
            owner_email=OWNER,
            name="Music",
            permissions={"allowed_credentials": ["music-token"]},
        ),
        disabled_categories=set(),
        disabled_tools=set(),
        intaris_result=SimpleNamespace(
            tools=[], warnings=[], collision_count=0, fallback_used=False
        ),
    )
    dispatch = dict(
        session_factory=factory,
        credentials=credentials,
        executor_id="music-executor",
        server_id="mcp_music",
        agent_id="music-agent",
    )
    if revoked_principal == "music-agent":
        assert not inventory.tools
        with pytest.raises(CredentialAccessError):
            await authorize_mcp_dispatch(**dispatch)
    else:
        assert len(inventory.tools) == 1
        assert await authorize_mcp_dispatch(**dispatch) == ready_revision
    with pytest.raises(MCPManagementError):
        await handle_mcp_management_action(
            deps=deps,
            actor_email=OWNER,
            arguments=await _configuration_arguments(binding, "servers_update"),
        )


@pytest.mark.asyncio
async def test_owner_executor_share_preserves_consumer_identity_and_tool_allowlist(binding):
    from cognis.api.runtime_support import _resolve_executor_scope
    from cognis.core.tool_router import ToolRouter
    from cognis.models.tool import (
        EvaluationResult,
        ToolCall,
        ToolDefinition,
        ToolSource,
        stable_tool_id,
    )
    from cognis.tools.registry import RegisteredTool, ToolRegistry

    factory, credentials, _ = binding
    await _configurator_deps(binding)
    definitions = [
        ToolDefinition(
            name=f"mcp_Music__{name}",
            description=name,
            source=ToolSource(
                type="local_mcp", server_id="mcp_music", server_name="Music", raw_tool_name=name
            ),
            read_only=True,
        )
        for name in ("players", "delete_player")
    ]
    policy = {
        "allowed_credentials": ["music-token"],
        "tool_permissions": {stable_tool_id(definitions[0]): "evaluate", "*": "deny"},
    }
    async with factory() as session:
        await update_agent(
            session,
            "music-agent",
            updates={
                "permissions": policy,
                "execution": {"executor_id": "music-executor"},
            },
        )
        await create_agent_grant(
            session,
            agent_id="music-agent",
            grantee_user_email="other@example.com",
            executor_scope="owner_executor",
            granted_by=OWNER,
        )
        await session.commit()
        row = await get_agent(session, "music-agent")
    consumer = AgentDefinition(
        agent_id=row.agent_id,
        owner_email=row.owner_email,
        name=row.name,
        permissions=row.permissions,
        execution=row.execution,
    )
    async with factory() as session:
        execution, executor_owner, allow_default = await _resolve_executor_scope(
            session,
            consumer,
            "other@example.com",
            conversation_id=None,
            task_id=None,
        )
    assert execution["executor_id"] == "music-executor"
    assert executor_owner == OWNER
    assert not allow_default
    registry = ToolRegistry()
    for definition in definitions:
        registry.register(RegisteredTool(definition=definition))
    guardrails = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=EvaluationResult(call_id="family-approval", decision="approve")
        )
    )
    router = ToolRouter(
        guardrails=guardrails, session_factory=factory, credentials_provider=credentials
    )
    grantee_session = SimpleNamespace(
        user_email="other@example.com", session_id="family-session", intaris_session_id=None
    )
    for definition, expected in zip(definitions, ("approve", "deny"), strict=True):
        call = ToolCall(call_id=f"family-{definition.name}", name=definition.name, arguments={})
        decision = await router.evaluate_tool_call(call, consumer, grantee_session, registry)
        assert decision.decision == expected
        if expected == "approve":
            assert decision.source == "guardrails"
            assert (
                await router._prepare_http_mcp_dispatch(
                    call,
                    registered_tool=registry.get(call.name),
                    session=grantee_session,
                    agent=consumer,
                    executor_id=execution["executor_id"],
                    executor_capabilities=ExecutorCapabilities(mcp_credential_bindings=True),
                )
                is None
            )
            assert call.runtime_metadata["mcp_binding_revision"]["token_version"] == 1
        else:
            assert decision.source == "agent"
    assert guardrails.evaluate.await_count == 1
    assert not (consumer.tools or {}).get("opt_in_builtin_tools")


@pytest.mark.asyncio
async def test_executor_payload_and_dispatch_authorization(binding):
    factory, credentials, providers = binding
    row = SimpleNamespace(
        executor_id="music-executor", owner_email=OWNER, config={"mcp_server_ids": ["mcp_music"]}
    )
    servers, secrets, metadata = await _resolve_executor_mcp_payload(row, providers)
    assert len(servers) == 1
    assert servers[0].headers == {"Authorization": "Bearer secret-music-token"}
    assert not secrets
    assert "secret-music-token" not in str(metadata)
    revision = await authorize_mcp_dispatch(
        session_factory=factory,
        credentials=credentials,
        executor_id="music-executor",
        server_id="mcp_music",
        agent_id="music-agent",
    )
    assert revision == metadata["mcp_credential_revisions"]["mcp_music"]
    with pytest.raises(CredentialAccessError):
        await authorize_mcp_dispatch(
            session_factory=factory,
            credentials=credentials,
            executor_id="music-executor",
            server_id="mcp_music",
            agent_id="other-agent",
        )
    await credentials.revoke_credential("music-token", OWNER)
    servers, _, metadata = await _resolve_executor_mcp_payload(row, providers)
    assert not servers
    assert metadata["mcp_servers"][0]["phase"] == "credential_resolution"
    assert "secret-music-token" not in str(metadata)


@pytest.mark.parametrize(
    "updates",
    [
        {"url": "http://music.test/mcp"},
        {"url": "https://user:password@music.test/mcp"},
        {"headers": {"authorization": "literal"}},
        {"transport": "stdio", "command": "python"},
        {"auth_config": {**AUTH, "credential_ref": "$credential:music-token.password"}},
    ],
)
def test_binding_contract_rejects_unsafe_configuration(updates):
    with pytest.raises(ValueError):
        MCPServerConfig.model_validate(
            {
                "name": "Music",
                "transport": "streamable_http",
                "url": "https://music.test/mcp",
                "auth_config": AUTH,
                **updates,
            }
        )


@pytest.mark.asyncio
async def test_discovery_hides_bindings_from_other_agents(binding):
    _, _, providers = binding
    tool = {
        "name": "mcp_Music__players",
        "description": "Players",
        "source": {"type": "local_mcp", "server_id": "mcp_music", "server_name": "Music"},
    }
    intaris = SimpleNamespace(tools=[], warnings=[], collision_count=0, fallback_used=False)
    for agent_id, expected in [("music-agent", 1), ("other-agent", 0)]:
        merged = await _merge_remote_runtime_inventory(
            remote_tools_data=[tool],
            agent_tools=[],
            providers=providers,
            agent=AgentDefinition(agent_id=agent_id, owner_email=OWNER, name="Music"),
            disabled_categories=set(),
            disabled_tools=set(),
            intaris_result=intaris,
        )
        assert len(merged.tools) == expected


def _provision_deps(binding):
    factory, credentials, _ = binding
    return ExecutorManagementDependencies(
        session_factory=factory,
        credentials=credentials,
        websocket=SimpleNamespace(get_connection=lambda _: None),
        fence=AsyncMock(),
        reconfigure=Mock(),
        trigger_reconcile=Mock(),
        sign_token=Mock(return_value="signed-executor-token"),
    )


async def _provision(
    deps, *, actor=OWNER, agent="music-agent", operation="create-executor", expected=None
):
    async with deps.session_factory() as session:
        row = await get_executor_row(session, "music-executor")
        revision = row.updated_at.isoformat()
    return await provision_executor_token(
        deps,
        ManagementActor(actor, "admin"),
        "music-executor",
        agent_id=agent,
        operation_id=operation,
        expected_updated_at=expected or revision,
    )


@pytest.mark.asyncio
async def test_provisioning_is_encrypted_scoped_and_idempotent(binding):
    deps = _provision_deps(binding)
    result = await _provision(deps)
    assert result["persisted"] and result["fenced"]
    assert "signed-executor-token" not in str(result)
    replay = await _provision(deps, expected="2000-01-01T00:00:00")
    assert result == replay
    assert deps.sign_token.call_count == 1
    assert deps.fence.await_count == 1
    async with deps.session_factory() as session:
        agent = await get_agent(session, "music-agent")
        row = await get_executor_row(session, "music-executor")
    assert row.token_version == 1
    resolved = await deps.credentials.resolve_ref(
        result["token_ref"],
        agent=AgentDefinition(
            agent_id=agent.agent_id,
            owner_email=OWNER,
            name=agent.name,
            permissions=agent.permissions,
        ),
        user_email=OWNER,
    )
    assert resolved.value == "signed-executor-token"
    with pytest.raises(CredentialAccessError):
        await deps.credentials.resolve_ref(
            result["token_ref"],
            agent=AgentDefinition(
                agent_id="other-agent",
                owner_email=OWNER,
                name="Other",
                permissions={"allowed_credentials": [result["credential_id"]]},
            ),
            user_email=OWNER,
        )


@pytest.mark.asyncio
async def test_provisioning_cancellation_after_commit_resumes_fence(binding):
    deps = _provision_deps(binding)
    deps.fence.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _provision(deps)
    deps.fence.side_effect = None
    result = await _provision(deps, expected="2000-01-01T00:00:00")
    assert result["token_version"] == 1
    assert deps.sign_token.call_count == 1
    assert deps.fence.await_count == 2


@pytest.mark.asyncio
async def test_provisioning_storage_failure_rolls_back_rotation_and_grant(binding):
    deps = _provision_deps(binding)
    broken = replace(
        deps,
        credentials=SimpleNamespace(
            create_credential_in_transaction=AsyncMock(side_effect=asyncio.CancelledError())
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await _provision(broken)
    async with deps.session_factory() as session:
        row = await get_executor_row(session, "music-executor")
        agent = await get_agent(session, "music-agent")
    assert row.token_version == 0
    assert agent.permissions["allowed_credentials"] == ["music-token"]
    deps.fence.assert_not_awaited()
    result = await _provision(deps)
    assert result["token_version"] == 1


@pytest.mark.asyncio
async def test_provisioning_cross_user_agent_and_stale_retry_denied(binding):
    from fastapi import HTTPException

    deps = _provision_deps(binding)
    for actor, agent in [("other@example.com", "music-agent"), (OWNER, "missing-agent")]:
        with pytest.raises(HTTPException):
            await _provision(deps, actor=actor, agent=agent)
    first = await _provision(deps)
    await _provision(deps, operation="rotate-executor")
    with pytest.raises(HTTPException, match="stale"):
        await _provision(deps)
    assert first["token_version"] == 1


@pytest.mark.asyncio
async def test_concurrent_provisioning_replay_signs_once(binding):
    deps = _provision_deps(binding)
    results = await asyncio.gather(_provision(deps), _provision(deps))
    assert results[0] == results[1]
    assert deps.sign_token.call_count == 1


@pytest.mark.asyncio
async def test_executor_checks_live_binding_revision_before_call():
    from cognis.executor.runner import ExecutorRunner, runtime_mcp_server_key
    from cognis.models.tool import ToolDefinition, ToolSource

    tool = ToolDefinition(
        name="mcp_Music__players",
        description="Players",
        source=ToolSource(type="local_mcp", server_id="mcp_music", server_name="Music"),
    )
    runner = object.__new__(ExecutorRunner)
    client = SimpleNamespace(call_tool=AsyncMock(return_value={"players": []}))
    runner._mcp_clients = {runtime_mcp_server_key(tool.source): client}
    runner._mcp_client_active_calls = {}
    revision = {"token_id": "credential:binding-hash", "token_version": 1}
    runner._mcp_credential_revisions = {"mcp_music": revision}
    handler = runner._build_mcp_handler(tool)
    for metadata in [{}, {"mcp_binding_revision": {**revision, "token_version": 2}}]:
        result = await handler({}, SimpleNamespace(runtime_metadata=metadata))
        assert result.is_error
    client.call_tool.assert_not_awaited()
    assert await handler(
        {}, SimpleNamespace(runtime_metadata={"mcp_binding_revision": revision})
    ) == {"players": []}


def test_http_bearer_redaction_preserves_non_secret_content():
    from cognis.mcp_runtime import StreamableHTTPMCPClient

    client = StreamableHTTPMCPClient(
        MCPServerConfig(name="Music", transport="streamable_http", url="https://music.test/mcp"),
        headers={"Authorization": "Bearer secret-music-token"},
    )
    value = {
        "description": "Authorization: Bearer secret-music-token",
        "items": ["secret-music-token", 23],
    }
    assert client._redact_bearer(value) == {
        "description": "Authorization: Bearer [REDACTED]",
        "items": ["[REDACTED]", 23],
    }


@pytest.mark.asyncio
async def test_credential_recreation_changes_cached_client_identity(binding):
    factory, credentials, _ = binding
    kwargs = dict(
        session_factory=factory,
        credentials=credentials,
        executor_id="music-executor",
        server_id="mcp_music",
        agent_id="music-agent",
    )
    first = await authorize_mcp_dispatch(**kwargs)
    await credentials.delete_credential("music-token", OWNER)
    with pytest.raises(CredentialAccessError):
        await authorize_mcp_dispatch(**kwargs)
    await credentials.upsert_credential(
        credential_id="music-token",
        user_email=OWNER,
        kind="token",
        label="Music",
        payload={"token": "replacement-token"},
    )
    recreated = await authorize_mcp_dispatch(**kwargs)
    assert first["token_version"] == recreated["token_version"] == 1
    assert first["token_id"] != recreated["token_id"]


@pytest.mark.asyncio
async def test_agent_grant_cas_conflict_rolls_back_provisioning(binding, monkeypatch):
    from fastapi import HTTPException

    deps = _provision_deps(binding)
    conflict = AsyncMock(return_value=False)
    monkeypatch.setattr("cognis.core.executor_management.update_agent_if_updated_at", conflict)
    with pytest.raises(HTTPException, match="permissions changed"):
        await _provision(deps)
    conflict.assert_awaited_once()
    async with deps.session_factory() as session:
        row = await get_executor_row(session, "music-executor")
        agent = await get_agent(session, "music-agent")
    assert row.token_version == 0
    assert agent.permissions["allowed_credentials"] == ["music-token"]
    assert len(await deps.credentials.list_credentials(OWNER)) == 1
    deps.fence.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["initialize", "list_tools", "call_tool"])
async def test_http_bearer_is_redacted_from_error_paths(operation, monkeypatch):
    from cognis.mcp_runtime import MCPClientError, StreamableHTTPMCPClient

    token = "secret-music-token"
    client = StreamableHTTPMCPClient(
        MCPServerConfig(name="Music", transport="streamable_http", url="https://music.test/mcp"),
        headers={"Authorization": f"Bearer {token}"},
    )
    error = RuntimeError(f"Remote error reflected {token}")
    if operation == "initialize":
        monkeypatch.setattr(client, "_enter_transport", AsyncMock(side_effect=error))
        invoke = client.connect
    else:

        class ErrorQueue:
            async def put(self, request):
                request[2].set_exception(error)

        client._requests = ErrorQueue()
        client._task = object()
        invoke = (
            client.list_tools
            if operation == "list_tools"
            else lambda: client.call_tool("players", {})
        )
    with pytest.raises(MCPClientError) as caught:
        await invoke()
    assert token not in str(caught.value)
    assert token not in str(vars(caught.value))
    if operation == "initialize":
        await client.close(suppress_cancelled=True)


@pytest.mark.asyncio
async def test_router_prepares_binding_without_oauth_and_rechecks_grants(binding):
    from cognis.core.tool_router import ToolRouter
    from cognis.models.tool import ToolCall, ToolDefinition, ToolSource
    from cognis.tools.registry import RegisteredTool

    factory, credentials, _ = binding
    router = ToolRouter(
        guardrails=Mock(), session_factory=factory, credentials_provider=credentials
    )
    registered = RegisteredTool(
        definition=ToolDefinition(
            name="mcp_Music__players",
            description="Players",
            source=ToolSource(type="local_mcp", server_id="mcp_music", server_name="Music"),
        )
    )
    call = ToolCall(call_id="binding-dispatch", name=registered.definition.name, arguments={})
    agent = AgentDefinition(agent_id="music-agent", owner_email=OWNER, name="Music")
    session = SimpleNamespace(user_email=OWNER)
    with pytest.raises(CredentialAccessError, match="upgraded"):
        await router._prepare_http_mcp_dispatch(
            call,
            registered_tool=registered,
            session=session,
            agent=agent,
            executor_id="music-executor",
        )
    result = await router._prepare_http_mcp_dispatch(
        call,
        registered_tool=registered,
        session=session,
        agent=agent,
        executor_id="music-executor",
        executor_capabilities=ExecutorCapabilities(mcp_credential_bindings=True),
    )
    assert result is None
    assert call.runtime_metadata["mcp_binding_revision"]["token_version"] == 1
    await credentials.revoke_credential("music-token", OWNER)
    with pytest.raises(CredentialAccessError):
        await router._prepare_http_mcp_dispatch(
            call,
            registered_tool=registered,
            session=session,
            agent=agent,
            executor_id="music-executor",
            executor_capabilities=ExecutorCapabilities(mcp_credential_bindings=True),
        )


@pytest.mark.asyncio
async def test_provisioning_tool_returns_resolvable_ref_only_and_denies_child_context(binding):
    from cognis import json_codec as json
    from cognis.core.system_management import handle_system_management_tool
    from cognis.core.tool_arguments import validate_tool_arguments
    from cognis.runtime_context import RuntimeAccessContext
    from cognis.tools.builtin.system_management import system_management_tools

    deps = _provision_deps(binding)
    async with deps.session_factory() as session:
        executor = await get_executor_row(session, "music-executor")
    arguments = {
        "action": "provision_token",
        "executor_id": "music-executor",
        "operation_id": "tool-create-executor",
        "expected_updated_at": executor.updated_at.isoformat(),
    }
    definition = next(tool for tool in system_management_tools() if tool.name == "manage_executors")
    operation = next(op for op in definition.native_operations if op.operation == "provision_token")
    assert (
        validate_tool_arguments("manage_executors", arguments, schema=operation.input_schema)
        is None
    )
    access = RuntimeAccessContext(
        user_email=OWNER,
        agent_id="music-agent",
        agent_owner_email=OWNER,
    )
    dependencies = SimpleNamespace(session_factory=deps.session_factory, executors=deps)
    denied = await handle_system_management_tool(
        name="manage_executors",
        arguments=arguments,
        deps=dependencies,
        access=replace(access, parent_session_id="parent-session"),
    )
    assert denied.is_error
    deps.sign_token.assert_not_called()
    result = await handle_system_management_tool(
        name="manage_executors",
        arguments=arguments,
        deps=dependencies,
        access=access,
    )
    assert not result.is_error
    output = json.loads(result.output)
    assert output["token_ref"].startswith("$credential:executor-token-")
    assert output["token_ref"].endswith(".token")
    assert "signed-executor-token" not in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [None, {}, {"mcp_credential_bindings": False}])
async def test_older_executor_never_receives_bearer(binding, capabilities):
    _, credentials, providers = binding
    providers.executor.websocket.get_connection = lambda _: SimpleNamespace(
        capabilities=capabilities
    )
    resolve = AsyncMock(wraps=credentials.resolve_ref)
    credentials.resolve_ref = resolve
    row = SimpleNamespace(
        executor_id="music-executor", owner_email=OWNER, config={"mcp_server_ids": ["mcp_music"]}
    )
    servers, _, metadata = await _resolve_executor_mcp_payload(row, providers)
    assert not servers
    resolve.assert_not_awaited()
    assert metadata["mcp_servers"][0]["error_class"] == "mcp_credential_executor_upgrade_required"
    assert "secret-music-token" not in str(metadata)


def test_executor_binding_capability_is_opt_in():
    from cognis.core.mcp_credentials import executor_supports_mcp_credentials

    assert not executor_supports_mcp_credentials(ExecutorCapabilities())
    assert not executor_supports_mcp_credentials(Mock())
    assert executor_supports_mcp_credentials(ExecutorCapabilities(mcp_credential_bindings=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_supported", [False, True])
async def test_physical_connection_rechecks_binding_support_after_admission(initially_supported):
    from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
    from cognis.providers.executor.websocket import WebSocketExecutorConnection

    ws = Mock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    connection = WebSocketExecutorConnection(
        ws,
        "music-executor",
        ExecutorCapabilities(mcp_credential_bindings=initially_supported),
    )
    connection._connected = True

    async def before_send(*_args):
        connection.capabilities.mcp_credential_bindings = False

    with pytest.raises(ExecutorDeliveryError) as caught:
        await connection.rpc_call(
            "tool.execute",
            {
                "runtime_metadata": {
                    "mcp_binding_revision": {
                        "token_id": "credential:binding-hash",
                        "token_version": 1,
                    }
                },
            },
            before_send=before_send,
        )
    assert caught.value.delivery_state == DeliveryState.NOT_SENT
    assert caught.value.code == "mcp_credential_executor_upgrade_required"
    ws.send_json.assert_not_awaited()
    ws.send_text.assert_not_awaited()
