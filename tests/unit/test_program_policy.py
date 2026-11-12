from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.runtime_support import _merge_remote_runtime_inventory
from cognis.bootstrap import run_schema_bootstrap
from cognis.core import program_policy
from cognis.core.program_config import PROGRAM_RUNTIME_TOOL_NAMES, program_runtime_executor_enabled
from cognis.core.program_policy import ProgramPolicyResolver
from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionModel
from cognis.models.tool import ToolCall, ToolDefinition, ToolSource
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_executor, create_user, get_executor_row
from cognis.tools.executor.definitions import ALL_EXECUTOR_TOOLS
from cognis.tools.registry import RegisteredTool, ToolRegistry


@pytest.mark.parametrize(
    ("labels", "config", "expected"),
    [
        ({}, {}, False),
        ({}, {"program_runtime": {"host_enabled": True}}, True),
        ({}, {"program_runtime": {"host_enabled": True, "enabled": False}}, False),
        ({"cognis_runtime_profile": "pod"}, {}, True),
        ({"cognis_runtime_profile": "pod"}, {"program_runtime": {"enabled": True}}, True),
        ({"cognis_runtime_profile": "pod"}, {"program_runtime": {"enabled": False}}, False),
        ({"cognis_runtime_profile": "pod"}, {"program_runtime": "unknown"}, False),
    ],
)
def test_config_truth_table(labels, config, expected):
    assert (
        program_runtime_executor_enabled(
            {"executor_id": "configured", "labels": labels, "config": config}
        )
        is expected
    )
    assert not program_runtime_executor_enabled(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_remote_manifest_cannot_grant_program_runtime(enabled):
    ordinary = next(tool for tool in ALL_EXECUTOR_TOOLS if tool.name == "bash")
    mcp = ToolDefinition(
        name="mcp_search", description="search", source=ToolSource(type="local_mcp")
    )
    program = [tool for tool in ALL_EXECUTOR_TOOLS if tool.name in PROGRAM_RUNTIME_TOOL_NAMES]
    result = await _merge_remote_runtime_inventory(
        remote_tools_data=[tool.model_dump(mode="json") for tool in [*program, ordinary, mcp]],
        agent_tools=[],
        providers=SimpleNamespace(),
        agent=AgentDefinition(agent_id="agent", owner_email="owner", name="agent"),
        disabled_categories=set(),
        disabled_tools=set(),
        intaris_result=SimpleNamespace(
            tools=[], warnings=[], collision_count=0, fallback_used=False
        ),
        program_enabled=enabled,
    )
    names = {tool.name for tool in result.tools}
    assert {"bash", "mcp_search"} <= names
    assert (names & PROGRAM_RUNTIME_TOOL_NAMES) == (
        PROGRAM_RUNTIME_TOOL_NAMES if enabled else set()
    )


@pytest.mark.asyncio
async def test_opted_in_legacy_manifest_does_not_invent_program_tools():
    ordinary = next(tool for tool in ALL_EXECUTOR_TOOLS if tool.name == "bash")
    result = await _merge_remote_runtime_inventory(
        remote_tools_data=[ordinary.model_dump(mode="json")],
        agent_tools=[],
        providers=SimpleNamespace(),
        agent=AgentDefinition(agent_id="agent", owner_email="owner", name="agent"),
        disabled_categories=set(),
        disabled_tools=set(),
        intaris_result=SimpleNamespace(
            tools=[], warnings=[], collision_count=0, fallback_used=False
        ),
        program_enabled=True,
    )
    assert {tool.name for tool in result.tools} == {"bash"}


@pytest.mark.asyncio
async def test_resolver_uses_binding_scope_and_current_configuration(monkeypatch):
    row = SimpleNamespace(
        executor_id="configured", labels={}, config={"program_runtime": {"host_enabled": True}}
    )
    lookup = AsyncMock(return_value=row)
    monkeypatch.setattr(program_policy, "get_executor_row", lookup)

    @asynccontextmanager
    async def sessions():
        yield object()

    resolver = ProgramPolicyResolver(sessions, "owner", {"run-id": "configured"})
    assert await resolver(SimpleNamespace(executor_id="run-id"))
    assert lookup.await_args.kwargs["owner_email"] == "owner"
    assert lookup.await_args.args[1] == "configured"
    row.config = {"program_runtime": {"enabled": False, "host_enabled": True}}
    assert not await resolver(SimpleNamespace(executor_id="run-id"))
    lookup.return_value = None
    assert not await resolver(SimpleNamespace(executor_id="other"))
    assert not await resolver(None)
    lookup.side_effect = RuntimeError("Policy store unavailable")
    assert not await resolver(SimpleNamespace(executor_id="run-id"))


@pytest.mark.asyncio
async def test_resolver_reads_real_scoped_rows_and_revocation(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/policy.db")
    await run_schema_bootstrap(engine)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            for email in ("owner@example.com", "other@example.com"):
                await create_user(db, email=email, name=email, password_hash="hash")
            for executor_id, owner, enabled in (
                ("primary", "owner@example.com", True),
                ("secondary", "owner@example.com", False),
                ("foreign", "other@example.com", True),
            ):
                await create_executor(
                    db,
                    executor_id=executor_id,
                    name=executor_id,
                    executor_type="websocket",
                    owner_email=owner,
                    config={"program_runtime": {"host_enabled": enabled}},
                )
            await db.commit()
        resolver = ProgramPolicyResolver(sessions, "owner@example.com", {"run-id": "primary"})
        assert await resolver(SimpleNamespace(executor_id="run-id"))
        assert not await resolver(SimpleNamespace(executor_id="secondary"))
        assert not await resolver(SimpleNamespace(executor_id="foreign"))
        registry = ToolRegistry()
        program = next(tool for tool in ALL_EXECUTOR_TOOLS if tool.name == "python_execute")
        registry.register(RegisteredTool(definition=program))
        secondary = SimpleNamespace(executor_id="secondary", execute=AsyncMock())
        result = await ToolRouter(guardrails=SimpleNamespace()).execute(
            ToolCall(call_id="targeted", name="python_execute", arguments={}),
            SessionModel(
                session_id="session",
                conversation_id="conversation",
                user_email="owner@example.com",
                agent_id="agent",
            ),
            AgentDefinition(agent_id="agent", owner_email="owner@example.com", name="agent"),
            registry,
            secondary,
            program_policy=resolver,
        )
        assert result.is_error and result.metadata["code"] == "program_runtime_disabled"
        secondary.execute.assert_not_awaited()
        async with sessions() as db:
            row = await get_executor_row(db, "primary")
            row.config = {"program_runtime": {"enabled": False, "host_enabled": True}}
            await db.commit()
        assert not await resolver(SimpleNamespace(executor_id="run-id"))
        stale_primary = SimpleNamespace(executor_id="run-id", execute=AsyncMock())
        result = await ToolRouter(guardrails=SimpleNamespace()).execute(
            ToolCall(call_id="stale", name="python_execute", arguments={}),
            SessionModel(
                session_id="session",
                conversation_id="conversation",
                user_email="owner@example.com",
                agent_id="agent",
            ),
            AgentDefinition(agent_id="agent", owner_email="owner@example.com", name="agent"),
            registry,
            stale_primary,
            program_policy=resolver,
        )
        assert result.is_error and result.metadata["code"] == "program_runtime_disabled"
        stale_primary.execute.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_program_dispatch_denies_without_side_effects():
    registry = ToolRegistry()
    for tool in ALL_EXECUTOR_TOOLS:
        if tool.name in PROGRAM_RUNTIME_TOOL_NAMES:
            registry.register(RegisteredTool(definition=tool))
    executor = SimpleNamespace(executor_id="disabled", execute=AsyncMock())
    router = ToolRouter(guardrails=SimpleNamespace())
    session = SessionModel(
        session_id="session", conversation_id="conversation", user_email="owner", agent_id="agent"
    )
    agent = AgentDefinition(agent_id="agent", owner_email="owner", name="agent")
    for name in PROGRAM_RUNTIME_TOOL_NAMES:
        result = await router.execute(
            ToolCall(call_id=name, name=name, arguments={}), session, agent, registry, executor
        )
        assert result.is_error and result.metadata["code"] == "program_runtime_disabled"
    executor.execute.assert_not_awaited()
