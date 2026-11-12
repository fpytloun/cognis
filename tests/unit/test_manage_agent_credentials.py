from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import schema as sa_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

import cognis.core.credential_grants as grants
from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
)
from cognis.core.tool_arguments import validate_tool_arguments
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError
from cognis.providers.credentials.encrypted_db import EncryptedDBCredentialsProvider
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_session_factory
from cognis.store.models import AuditLog, Base
from cognis.store.queries import create_agent, create_user, get_agent, update_agent
from cognis.tools.builtin.agent_management import (
    MANAGE_AGENTS_TOOL,
    handle_agent_management_tool,
)

OWNER = "owner@example.com"
FOREIGN = "foreign@example.com"
CANARY = "secret-value-must-never-reach-tool-output"


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def deps(request: Any, tmp_path: Any) -> Any:
    admin = None
    schema_name = f"credential_grants_{uuid.uuid4().hex}"
    if request.param == "postgres":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(sa_schema.CreateSchema(schema_name))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": f'"{schema_name}"'}}
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/grants.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    key_path = tmp_path / "test-key"
    key_path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    provider = EncryptedDBCredentialsProvider(factory, str(key_path))
    result = AgentManagementDependencies(session_factory=factory, credentials=provider)
    async with factory() as session:
        for email in (OWNER, FOREIGN):
            await create_user(session, email=email, name=email, password_hash="hash", role="admin")
        for agent_id, owner in (("manager", OWNER), ("target", OWNER), ("foreign", FOREIGN)):
            await create_agent(
                session,
                agent_id=agent_id,
                owner_email=owner,
                name=agent_id,
                status="draft",
                permissions={"allowed_knowledgebases": ["egypt"], "allowed_credentials": ["keep"]},
            )
        await session.commit()
    for credential_id, owner in (("account", OWNER), ("foreign-account", FOREIGN)):
        await provider.upsert_credential(
            credential_id=credential_id,
            user_email=owner,
            kind="username_password",
            label=CANARY,
            description=CANARY,
            payload={"password": CANARY},
            metadata={"untrusted": CANARY},
        )
    try:
        yield result
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
            await admin.dispose()


async def action(deps: Any, operation: str, **kwargs: Any) -> dict[str, Any]:
    return await handle_agent_management_action(
        deps=deps,
        actor_email=OWNER,
        current_agent_id="manager",
        arguments={
            "action": operation,
            "agent_id": "target",
            "credential_id": "account",
            **kwargs,
        },
    )


@pytest.mark.asyncio
async def test_roundtrip_preserves_permissions_and_never_exposes_secrets(
    deps: Any, monkeypatch: Any, caplog: Any
) -> None:
    provider = deps.credentials
    resolve = provider.resolve_ref
    monkeypatch.setattr(
        provider, "resolve_ref", AsyncMock(side_effect=AssertionError("resolution"))
    )
    read = await action(deps, "credentials_get")
    assert read["assigned"] is False
    outputs = [read]
    for operation, changed in (
        ("credentials_grant", True),
        ("credentials_grant", False),
        ("credentials_revoke", True),
        ("credentials_revoke", False),
    ):
        result = await action(deps, operation, expected_updated_at=outputs[-1]["updated_at"])
        assert result["changed"] is changed
        outputs.append(result)
    assert CANARY not in json.dumps(outputs)
    async with deps.session_factory() as session:
        row = await get_agent(session, "target")
        assert row.permissions == {
            "allowed_knowledgebases": ["egypt"],
            "allowed_credentials": ["keep"],
        }
        assert row.status == "draft"
        audits = (await session.execute(select(AuditLog))).scalars().all()
        assert len(audits) == 4
        assert CANARY not in json.dumps([audit.details for audit in audits])
        agent = AgentDefinition(
            agent_id=row.agent_id,
            owner_email=row.owner_email,
            name=row.name,
            permissions=AgentPermissions.model_validate(row.permissions),
        )
    assert CANARY not in caplog.text
    with pytest.raises(CredentialAccessError, match="not allowed"):
        await resolve("$credential:account.password", agent=agent, user_email=OWNER)
    assert (await provider.get_credential("account", OWNER)).status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["credentials_get", "credentials_grant", "credentials_revoke"]
)
@pytest.mark.parametrize(
    "override",
    [{"agent_id": "foreign"}, {"credential_id": "foreign-account"}, {"credential_id": "missing"}],
)
async def test_owner_only_even_for_admin(deps: Any, operation: str, override: Any) -> None:
    revision = (await action(deps, "credentials_get"))["updated_at"]
    with pytest.raises(AgentManagementError):
        await action(deps, operation, expected_updated_at=revision, **override)
    assert (await action(deps, "credentials_get"))["assigned"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"agent_id": "manager", "scope": "agent"},
        {"scope": "agent"},
        {"scope": "unknown"},
        {"status": "revoked"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
async def test_grant_rejects_invalid_scope_or_inactive_credential(deps: Any, changes: Any) -> None:
    await deps.credentials.upsert_credential(
        credential_id="account",
        user_email=OWNER,
        kind="token",
        label="Account",
        payload={"token": CANARY},
        **changes,
    )
    revision = (await action(deps, "credentials_get"))["updated_at"]
    with pytest.raises(AgentManagementError):
        await action(deps, "credentials_grant", expected_updated_at=revision)
    assert (await action(deps, "credentials_get"))["assigned"] is False


@pytest.mark.asyncio
async def test_revoke_can_remove_inactive_scoped_credential(deps: Any) -> None:
    read = await action(deps, "credentials_get")
    grant = await action(deps, "credentials_grant", expected_updated_at=read["updated_at"])
    await deps.credentials.revoke_credential("account", OWNER)
    result = await action(deps, "credentials_revoke", expected_updated_at=grant["updated_at"])
    assert result["assigned"] is False
    assert result["credential"]["status"] == "revoked"


@pytest.mark.asyncio
async def test_revision_conflict_preserves_other_updates(deps: Any) -> None:
    revision = (await action(deps, "credentials_get"))["updated_at"]
    async with deps.session_factory() as session:
        await update_agent(
            session, "target", updates={"permissions": {"allowed_knowledgebases": ["new-kb"]}}
        )
        await session.commit()
    with pytest.raises(AgentManagementError, match="conflict"):
        await action(deps, "credentials_grant", expected_updated_at=revision)
    async with deps.session_factory() as session:
        assert (await get_agent(session, "target")).permissions == {
            "allowed_knowledgebases": ["new-kb"]
        }


@pytest.mark.asyncio
async def test_concurrent_grants_have_one_cas_winner(deps: Any, monkeypatch: Any) -> None:
    revision = (await action(deps, "credentials_get"))["updated_at"]
    original = grants.update_agent_if_updated_at
    both_ready = asyncio.Event()
    arrived = 0

    async def synchronized(*args: Any, **kwargs: Any) -> bool:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(grants, "update_agent_if_updated_at", synchronized)
    results = await asyncio.gather(
        action(deps, "credentials_grant", expected_updated_at=revision),
        action(deps, "credentials_grant", expected_updated_at=revision),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], AgentManagementError)
    assert "conflict" in str(errors[0])


@pytest.mark.asyncio
async def test_self_mutation_requires_explicit_approval(deps: Any) -> None:
    with pytest.raises(AgentManagementError, match="Self mutation"):
        await action(deps, "credentials_grant", agent_id="manager")


@pytest.mark.asyncio
async def test_management_context_is_required(deps: Any) -> None:
    result = await handle_agent_management_tool(
        tool_name="manage_agents",
        arguments={"action": "credentials_get", "agent_id": "target", "credential_id": "account"},
        deps=deps,
        user_email=OWNER,
        current_agent_id="manager",
        runtime_access=RuntimeAccessContext(
            user_email=OWNER,
            agent_id="manager",
            agent_owner_email=OWNER,
            agent_type="primary",
            parent_session_id="parent-session",
        ),
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_user_credential_requested_by_another_agent_can_be_granted_and_used(
    deps: Any,
) -> None:
    await deps.credentials.upsert_credential(
        credential_id="account",
        user_email=OWNER,
        agent_id="manager",
        scope="user",
        kind="token",
        label="Account",
        payload={"token": CANARY},
    )

    async def call(operation: str, **fields: Any) -> Any:
        result = await handle_agent_management_tool(
            tool_name="manage_agents",
            arguments={
                "action": operation,
                "agent_id": "target",
                "credential_id": "account",
                **fields,
            },
            deps=deps,
            user_email=OWNER,
            current_agent_id="manager",
            runtime_access=RuntimeAccessContext(
                user_email=OWNER,
                agent_id="manager",
                agent_owner_email=OWNER,
            ),
        )
        assert not result.is_error, result.output
        assert CANARY not in result.output
        return json.loads(result.output)

    read = await call("credentials_get")
    assert read["credential"]["agent_id"] == "manager"
    await call("credentials_grant", expected_updated_at=read["updated_at"])
    async with deps.session_factory() as session:
        row = await get_agent(session, "target")
        target = AgentDefinition(
            agent_id="target",
            owner_email=OWNER,
            name="Target",
            permissions=AgentPermissions.model_validate(row.permissions),
        )
    resolved = await deps.credentials.resolve_ref(
        "$credential:account.token",
        agent=target,
        user_email=OWNER,
    )
    assert resolved.value == CANARY
    await deps.credentials.upsert_credential(
        credential_id="account",
        user_email=OWNER,
        agent_id="manager",
        scope="agent",
        kind="token",
        label="Account",
        payload={"token": CANARY},
    )
    with pytest.raises(CredentialAccessError, match="not scoped"):
        await deps.credentials.resolve_ref(
            "$credential:account.token", agent=target, user_email=OWNER
        )


def test_strict_operation_contracts() -> None:
    def valid(arguments: dict[str, Any]) -> bool:
        return (
            validate_tool_arguments(
                "manage_agents", arguments, schema=MANAGE_AGENTS_TOOL.parameters
            )
            is None
        )

    for operation in ("credentials_get", "credentials_grant", "credentials_revoke"):
        arguments: dict[str, Any] = {
            "action": operation,
            "agent_id": "target",
            "credential_id": "account",
        }
        if operation != "credentials_get":
            arguments["expected_updated_at"] = datetime.now(UTC).isoformat()
        assert valid(arguments)
        assert not valid({**arguments, "payload": CANARY})
        assert not valid({**arguments, "credential_id": None})
        if operation != "credentials_get":
            del arguments["expected_updated_at"]
            assert not valid(arguments)
