from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from cognis.core.agent_management import AgentManagementDependencies
from cognis.core.tool_router import caller_assignable_tools
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ToolDefinition, ToolSource, stable_tool_id
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import create_agent, create_user
from cognis.tools.builtin.agent_management import handle_agent_management_tool
from cognis.tools.registry import RegisteredTool, ToolRegistry

OWNER = "owner@example.com"
ACCESS = RuntimeAccessContext(user_email=OWNER, agent_id="manager", agent_owner_email=OWNER)
MCP_TOOL = ToolDefinition(
    name="read_device",
    description="Read a device.",
    parameters={"type": "object"},
    source=ToolSource(type="mcp", server_name="network"),
)


@pytest_asyncio.fixture
async def deps(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/management.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_user(session, email=OWNER, name="Owner", password_hash="test")
        await create_user(session, email="other@example.com", name="Other", password_hash="test")
        for agent_id, owner in (("target", OWNER), ("foreign", "other@example.com")):
            await create_agent(
                session,
                agent_id=agent_id,
                owner_email=owner,
                name=agent_id,
                status="active",
                capabilities={"analytics_enabled": False},
            )
        await session.commit()
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MCP_TOOL))
    inventory = caller_assignable_tools(
        registry,
        AgentDefinition(agent_id="manager", owner_email=OWNER, name="Manager"),
        access_context=ACCESS,
    )
    try:
        yield AgentManagementDependencies(session_factory=factory, assignable_tools=inventory)
    finally:
        await engine.dispose()


async def call(deps: Any, action: str, **fields: Any) -> Any:
    return await handle_agent_management_tool(
        tool_name="manage_agents",
        arguments={"action": action, **fields},
        deps=deps,
        user_email=OWNER,
        current_agent_id="manager",
        runtime_access=ACCESS,
    )


@pytest.mark.asyncio
async def test_guardrail_create_update_roundtrip_and_validation(deps: Any) -> None:
    created = await call(
        deps,
        "create",
        name="New policy agent",
        intaris_evaluation="bounded_autonomy",
        approval_mode="unattended",
        minimum_outcome="escalate",
        maximum_outcome="approve",
    )
    assert not created.is_error, created.output
    agent = json.loads(created.output)["agent"]
    assert agent["capabilities"]["intaris_evaluation"] == "bounded_autonomy"
    settings = {
        "guardrails_backend": "intaris",
        "intaris_evaluation": "bounded_autonomy",
        "approval_mode": "unattended",
        "minimum_outcome": "escalate",
        "maximum_outcome": "approve",
    }
    updated = await call(deps, "settings_update", agent_id="target", settings=settings)
    assert not updated.is_error, updated.output
    read = await call(deps, "settings_get", agent_id="target")
    actual = json.loads(read.output)["settings"]
    assert {key: actual[key] for key in settings} == settings
    assert actual["analytics_enabled"] is False
    for patch in (
        {"guardrails_backend": "none"},
        {"intaris_evaluation": "unsupported"},
        {"approval_mode": "unsupported"},
    ):
        rejected = await call(deps, "settings_update", agent_id="target", settings=patch)
        assert rejected.is_error
    cleared = await call(
        deps,
        "settings_update",
        agent_id="target",
        settings={
            "guardrails_backend": "none",
            "approval_mode": "ask",
            "minimum_outcome": None,
            "maximum_outcome": None,
        },
    )
    assert not cleared.is_error, cleared.output
    denied = await call(deps, "settings_update", agent_id="foreign", settings=settings)
    assert denied.is_error


@pytest.mark.asyncio
async def test_mcp_assignment_roundtrip_and_default_denies(deps: Any) -> None:
    tool_id = stable_tool_id(MCP_TOOL)
    assert any(stable_tool_id(tool) == tool_id for tool in deps.assignable_tools)
    assigned = {
        "allow_tools": [tool_id],
        "disabled_mcp_servers": ["offline"],
        "intaris_mcp_servers": ["network"],
        "mcp_servers": [{"name": "legacy", "command": "mcp-server"}],
    }
    updated = await call(deps, "tools_set", agent_id="target", **assigned)
    assert not updated.is_error, updated.output
    payload = json.loads(updated.output)["tools"]
    assert payload["validation"]["valid"]
    assert tool_id in payload["effective_tools"]
    assert payload["configured"]["disabled_mcp_servers"] == ["offline"]
    assert payload["configured"]["mcp_servers"][0]["name"] == "legacy"
    denied = await call(deps, "tools_set", agent_id="target", allow_tools=[], deny_tools=[tool_id])
    assert not denied.is_error, denied.output
    assert json.loads(denied.output)["tools"]["effective_tools"] == []
    added = await call(deps, "tools_add", agent_id="target", disabled_mcp_servers=["network"])
    assert not added.is_error, added.output
    removed = await call(deps, "tools_remove", agent_id="target", disabled_mcp_servers=["offline"])
    assert not removed.is_error, removed.output
    read = await call(deps, "tools_get", agent_id="target")
    assert json.loads(read.output)["tools"]["configured"]["disabled_mcp_servers"] == ["network"]
    removed = await call(
        deps,
        "tools_remove",
        agent_id="target",
        mcp_servers=[{"name": "legacy", "command": "mcp-server"}],
    )
    assert not removed.is_error, removed.output
    assert json.loads(removed.output)["tools"]["configured"]["mcp_servers"] == []
    invalid = await call(deps, "tools_set", agent_id="target", mcp_servers=[{"name": "broken"}])
    assert invalid.is_error
    created = await call(deps, "create", name="Default agent")
    assert not created.is_error, created.output
    agent_id = json.loads(created.output)["agent"]["agent_id"]
    defaults = await call(deps, "tools_get", agent_id=agent_id)
    assert json.loads(defaults.output)["tools"]["validation"]["errors"] == []
