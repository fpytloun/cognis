from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from cognis.api.runtime_support import _resolve_eligible_executor_config
from cognis.core.agent_management import (
    AgentManagementDependencies,
    _settings_field_names,
)
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.runtime import ExecutorConfigurationError
from cognis.core.turn_scheduler import _durable_turn_error_detail, classify_turn_error
from cognis.models.agent import AgentDefinition
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_executor,
    create_user,
    get_agent,
    get_agent_grant_for_user,
    update_agent,
    update_executor,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL, handle_agent_management_tool
from cognis.tools.introspection import validate_available_tool_call_with_context
from cognis.tools.native_validation import NativeValidationContext

OWNER = "owner@example.com"
GUEST = "guest@example.com"
EXECUTION = {
    "default_chat_mode": "plan",
    "step_agent_overrides": {"review": "reviewer"},
    "additional_executors": [{"executor_id": "auxiliary", "description": "Keep"}],
}


@pytest_asyncio.fixture
async def deps(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/executors.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        for email in (OWNER, GUEST, SYSTEM_USER_EMAIL):
            await create_user(session, email=email, name=email, password_hash="test", role="admin")
        await create_agent(
            session,
            agent_id="target",
            name="Target",
            owner_email=OWNER,
            status="active",
            execution=EXECUTION,
            permissions={"allowed_credentials": ["keep"]},
        )
        for executor_id, owner, shared in (
            ("owner-executor", OWNER, False),
            ("guest-executor", GUEST, False),
            ("shared-executor", None, True),
        ):
            await create_executor(
                session,
                executor_id=executor_id,
                name=executor_id,
                owner_email=owner,
                shared=shared,
                executor_type="websocket",
                is_default=not shared,
                labels={"host": executor_id},
            )
        await session.commit()
    try:
        yield AgentManagementDependencies(session_factory=factory)
    finally:
        await engine.dispose()


async def update(
    deps: AgentManagementDependencies, settings: dict[str, Any], *, actor: str = OWNER
) -> Any:
    return await handle_agent_management_tool(
        tool_name="manage_agents",
        deps=deps,
        user_email=actor,
        current_agent_id="manager",
        runtime_access=RuntimeAccessContext(
            user_email=actor,
            agent_id="manager",
            agent_owner_email=actor,
            agent_type="primary",
        ),
        arguments={"action": "settings_update", "agent_id": "target", "settings": settings},
    )


async def execution(deps: AgentManagementDependencies) -> dict[str, Any]:
    async with deps.session_factory() as session:
        row = await get_agent(session, "target")
        assert row is not None
        assert row.permissions == {"allowed_credentials": ["keep"]}
        assert row.status == "active"
        return dict(row.execution)


@pytest.mark.asyncio
async def test_schema_validation_and_tool_assignment_preserve_execution(deps: Any) -> None:
    settings = {"executor_id": "owner-executor"}
    validated = await validate_available_tool_call_with_context(
        [MANAGE_AGENTS_TOOL],
        "manage_agents",
        {"action": "settings_update", "agent_id": "target", "settings": settings},
        context=NativeValidationContext(
            actor_email=OWNER, current_agent_id="manager", agent_management_deps=deps
        ),
    )
    assert validated["valid"], validated
    assert await execution(deps) == EXECUTION  # Validation must not mutate.
    schema = next(
        op for op in MANAGE_AGENTS_TOOL.native_operations if op.operation == "settings_update"
    ).input_schema["properties"]["settings"]["properties"]
    assert set(schema) == _settings_field_names()
    result = await update(deps, settings)
    assert not result.is_error, result.output
    assert json.loads(result.output)["settings"]["executor"]["executor_id"] == "owner-executor"
    assert await execution(deps) == {**EXECUTION, "executor_id": "owner-executor"}
    result = await update(deps, {"executor_id": "shared-executor"})
    assert not result.is_error, result.output
    assert (await execution(deps))["executor_id"] == "shared-executor"


@pytest.mark.asyncio
async def test_selector_switch_null_and_omission_semantics(deps: Any) -> None:
    for settings, expected in (
        ({"executor_id": "owner-executor"}, {"executor_id": "owner-executor"}),
        (
            {"executor_selector": {"host": "shared-executor"}},
            {"executor_selector": {"host": "shared-executor"}},
        ),
        (
            {"executor_id": None},
            {"executor_selector": {"host": "shared-executor"}, "executor_id": None},
        ),
        (
            {"executor_id": "owner-executor", "executor_selector": None},
            {"executor_id": "owner-executor", "executor_selector": None},
        ),
        (
            {"executor_selector": {"host": "owner-executor"}, "executor_id": None},
            {"executor_selector": {"host": "owner-executor"}},
        ),
        ({"executor_selector": None}, {"executor_selector": None}),
    ):
        result = await update(deps, settings)
        assert not result.is_error, result.output
        actual = await execution(deps)
        assert {key: value for key, value in actual.items() if value is not None} == {
            **EXECUTION,
            **{key: value for key, value in expected.items() if value is not None},
        }
    result = await update(deps, {"temperature": 0.2})
    assert not result.is_error, result.output
    assert (await execution(deps))["executor_selector"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"executor_id": "guest-executor"},
        {"executor_id": "nonexistent-executor"},
        {"executor_id": ""},
        {"executor_selector": {}},
        {"executor_selector": {"": "owner-executor"}},
        {"executor_selector": {"host": " "}},
        {"executor_selector": {"host": 42}},
        {"executor_selector": {"host": "guest-executor"}},
        {"executor_id": "owner-executor", "executor_selector": {"host": "owner-executor"}},
        {"executor_selector": {"host": "owner-executor"}, "executor_id": "owner-executor"},
    ],
)
async def test_invalid_or_cross_owner_selection_is_atomic(deps: Any, settings: Any) -> None:
    result = await update(deps, {**settings, "temperature": 0.8})
    assert result.is_error
    assert await execution(deps) == EXECUTION
    async with deps.session_factory() as session:
        assert (await get_agent(session, "target")).llm_config is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["owner_executor", "grantee_executor"])
async def test_assignment_preserves_share_and_runtime_owner_scope(deps: Any, scope: str) -> None:
    async with deps.session_factory() as session:
        await create_agent_grant(
            session,
            agent_id="target",
            grantee_user_email=GUEST,
            executor_scope=scope,
            granted_by=OWNER,
        )
        await session.commit()
    assert not (await update(deps, {"executor_id": "owner-executor"})).is_error
    denied = await update(deps, {"executor_id": "guest-executor"}, actor=GUEST)
    assert denied.is_error
    async with deps.session_factory() as session:
        grant = await get_agent_grant_for_user(session, "target", GUEST)
        assert grant.executor_scope == scope
        assert grant.grantee_overrides is None
        row = await get_agent(session, "target")
        agent = AgentDefinition(
            agent_id="target", name="Target", owner_email=OWNER, execution=row.execution
        )
    providers = SimpleNamespace(_session_factory=deps.session_factory)
    owner = await _resolve_eligible_executor_config(providers, agent, OWNER, ExecutorPolicy())
    guest = await _resolve_eligible_executor_config(providers, agent, GUEST, ExecutorPolicy())
    assert owner["executor_id"] == "owner-executor"
    assert guest["executor_id"] == (
        "owner-executor" if scope == "owner_executor" else "guest-executor"
    )


@pytest.mark.asyncio
async def test_missing_binding_is_actionable_without_model_health_or_default_fallback(
    deps: Any,
) -> None:
    health = AsyncMock()
    providers = SimpleNamespace(
        _session_factory=deps.session_factory, llm=SimpleNamespace(health=health)
    )
    agent = AgentDefinition(agent_id="target", name="Target", owner_email=OWNER)
    with pytest.raises(ExecutorConfigurationError) as raised:
        await _resolve_eligible_executor_config(providers, agent, OWNER, ExecutorPolicy())
    classified = await classify_turn_error(providers, raised.value)
    assert classified.code == "executor_configuration_invalid"
    assert classified.transient is False
    assert classified.recoverable is False
    assert "executor_id or executor_selector" in _durable_turn_error_detail(classified)
    health.assert_not_awaited()
    # A configured but inactive primary must never fall back to a shared/default executor.
    async with deps.session_factory() as session:
        await update_agent(
            session, "target", updates={"execution": {"executor_id": "owner-executor"}}
        )
        await update_executor(session, "owner-executor", owner_email=OWNER, status="inactive")
        await session.commit()
    agent.execution = {"executor_id": "owner-executor"}
    with pytest.raises(RuntimeError, match="not active"):
        await _resolve_eligible_executor_config(providers, agent, OWNER, ExecutorPolicy())
