from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.runtime_support import select_static_tools
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_loop import AgentLoop
from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    _apply_tools_setting,
    handle_agent_management_action,
)
from cognis.core.tool_router import ToolRouter, caller_assignable_tools
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.session import SessionModel
from cognis.models.tool import EvaluationResult, ToolCall, stable_tool_id
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_agent, create_user, get_agent
from cognis.tools.builtin.agent_management import (
    MANAGE_AGENTS_TOOL,
    handle_agent_management_tool,
)
from cognis.tools.builtin.system_management import SYSTEM_MANAGEMENT_TOOLS
from cognis.tools.introspection import describe_available_tool
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _agent(**updates: object) -> AgentDefinition:
    tools = updates.pop("tools", {"opt_in_builtin_tools": ["manage_agents"]})
    assert isinstance(tools, dict)
    return AgentDefinition.model_validate(
        {
            "agent_id": "bootstrap-owner",
            "owner_email": "owner@example.com",
            "name": "Bootstrap owner",
            "tools": {"builtin_tools": [], "delegation_tools": False, **tools},
            **updates,
        }
    )


def _access(**updates: object) -> RuntimeAccessContext:
    return replace(
        RuntimeAccessContext(
            user_email="owner@example.com",
            agent_id="bootstrap-owner",
            agent_owner_email="owner@example.com",
        ),
        **updates,
    )


def _registry(agent: AgentDefinition, access: RuntimeAccessContext) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in select_static_tools(agent, access_context=access):
        registry.register(RegisteredTool(definition=tool))
    return registry


def test_assignment_candidates_do_not_enable_execution() -> None:
    agent, access = _agent(), _access()
    registry = _registry(agent, access)
    assert registry.get("manage_settings") is None
    token = current_runtime_access_context.set(access)
    try:
        inventory = caller_assignable_tools(registry, agent)
    finally:
        current_runtime_access_context.reset(token)
    assert {tool.name for tool in inventory} >= SYSTEM_MANAGEMENT_TOOLS
    assert registry.get("manage_settings") is None
    # No unrelated static tool may escape the agent's allowlist.
    narrow = _agent(tools={"opt_in_builtin_tools": ["manage_agents"], "builtin_tools": []})
    candidates = caller_assignable_tools(_registry(narrow, access), narrow, access_context=access)
    assert {tool.name for tool in candidates} == SYSTEM_MANAGEMENT_TOOLS | {
        "manage_agents",
        "manage_mcp",
    }


@pytest.mark.asyncio
async def test_dynamic_assignment_options_include_unassigned_settings() -> None:
    agent, access = _agent(), _access()
    registry = _registry(agent, access)
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_factory = None
    token = current_runtime_access_context.set(access)
    try:
        resolved = await loop._resolve_introspection_dynamic_options(
            SimpleNamespace(
                session=SimpleNamespace(user_email=agent.owner_email),
                agent=agent,
                tool_registry=registry,
            ),
            [MANAGE_AGENTS_TOOL],
        )
        expected = {stable_tool_id(tool) for tool in caller_assignable_tools(registry, agent)}
    finally:
        current_runtime_access_context.reset(token)
    described = describe_available_tool(resolved, "manage_agents")
    for operation in described["descriptor"]["operations"]:
        if operation["operation"] in {"tools_add", "tools_set", "tools_remove"}:
            for option in operation["dynamic_options"]:
                if option["source"] == "agent_management.assignable_tools":
                    assert set(option["values"]) == expected
                    assert "builtin:manage_settings" in option["values"]


@pytest.mark.parametrize(
    "access",
    [
        _access(user_email="other@example.com"),
        _access(agent_id="another-agent"),
        _access(parent_session_id="parent-session"),
        _access(delegation_mode="delegate"),
        _access(workflow_step=True),
        _access(task_id="task-with-root-context"),
        _access(agent_type="secondary"),
    ],
)
def test_assignment_candidates_require_owner_root_context(access: RuntimeAccessContext) -> None:
    agent = _agent()
    # Even a stale registry containing manage_agents cannot authorize bootstrap.
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MANAGE_AGENTS_TOOL))
    assert "manage_settings" not in {
        tool.name for tool in caller_assignable_tools(registry, agent, access_context=access)
    }


@pytest.mark.parametrize(
    "updates",
    [
        {
            "tools": {
                "opt_in_builtin_tools": ["manage_agents"],
                "deny_tools": ["builtin:manage_settings"],
            }
        },
        {
            "tools": {
                "opt_in_builtin_tools": ["manage_agents"],
                "disabled_tools": ["manage_settings"],
            }
        },
        {"permissions": AgentPermissions(denied_tools=["manage_settings"])},
        {"agent_type": "secondary"},
    ],
)
def test_assignment_candidates_respect_denials_and_agent_type(updates: dict) -> None:
    agent, access = _agent(**updates), _access()
    registry = _registry(agent, access)
    assert "manage_settings" not in {
        tool.name for tool in caller_assignable_tools(registry, agent, access_context=access)
    }


@pytest.mark.asyncio
async def test_management_assignment_crud_and_raw_opt_in(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/bootstrap.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    agent, access = _agent(), _access()
    inventory = caller_assignable_tools(_registry(agent, access), agent, access_context=access)
    deps = AgentManagementDependencies(session_factory=factory, assignable_tools=inventory)
    try:
        async with factory() as db:
            await create_user(db, email=agent.owner_email, name="Owner", password_hash="test")
            await create_user(db, email="other@example.com", name="Other", password_hash="test")
            for agent_id, owner, agent_type in (
                (agent.agent_id, agent.owner_email, "primary"),
                ("managed-primary", agent.owner_email, "primary"),
                ("managed-secondary", agent.owner_email, "secondary"),
                ("other-owned", "other@example.com", "primary"),
            ):
                await create_agent(
                    db,
                    agent_id=agent_id,
                    owner_email=owner,
                    name=agent_id,
                    agent_type=agent_type,
                    tools=agent.tools if agent_id == agent.agent_id else {},
                )
            await db.commit()

        async def action(arguments: dict, *, approved: bool = False) -> dict:
            return await handle_agent_management_action(
                deps=deps,
                actor_email=agent.owner_email,
                current_agent_id=agent.agent_id,
                arguments=arguments,
                self_mutation_approved=approved,
            )

        grant = {
            "action": "tools_add",
            "agent_id": agent.agent_id,
            "allow_tools": ["builtin:manage_settings"],
        }
        with pytest.raises(AgentManagementError, match="explicit user approval"):
            await action(grant)
        async with factory() as db:
            row = await get_agent(db, agent.agent_id)
            assert row.tools == agent.tools

        # Real router escalation must stop before any persistence or domain dispatch.
        guardrails = AsyncMock()
        guardrails.evaluate.return_value = EvaluationResult(
            decision="escalate", call_id="bootstrap-user-review", reasoning="Review required"
        )
        router = ToolRouter(guardrails=guardrails, session_factory=factory)
        result = await router.execute(
            ToolCall(
                call_id="bootstrap-self-grant",
                name="manage_agents",
                arguments=grant,
                runtime_metadata={"runtime_access": asdict(access)},
            ),
            SessionModel(
                session_id="bootstrap-session",
                conversation_id="bootstrap-conversation",
                user_email=agent.owner_email,
                agent_id=agent.agent_id,
            ),
            agent,
            _registry(agent, access),
            AsyncMock(),
        )
        assert result.metadata["evaluation"]["decision"] == "escalate"
        assert guardrails.evaluate.await_args.kwargs["context"]["minimum_outcome"] == "escalate"
        async with factory() as db:
            row = await get_agent(db, agent.agent_id)
            assert row.tools == agent.tools

        guardrails.evaluate.return_value = EvaluationResult(
            decision="approve", call_id="bootstrap-user-review", reasoning="User approved"
        )
        approved = await router.execute(
            ToolCall(
                call_id="bootstrap-approved-grant",
                name="manage_agents",
                arguments=grant,
                runtime_metadata={
                    "runtime_access": asdict(access),
                    "approval_call_id": "bootstrap-user-review",
                },
            ),
            SessionModel(
                session_id="bootstrap-session",
                conversation_id="bootstrap-conversation",
                user_email=agent.owner_email,
                agent_id=agent.agent_id,
            ),
            agent,
            _registry(agent, access),
            AsyncMock(),
        )
        assert not approved.is_error, approved.output
        assert '"status": "updated"' in approved.output
        async with factory() as db:
            row = await get_agent(db, agent.agent_id)
            enabled_agent = _agent(tools=row.tools)
        assert "manage_settings" in {
            tool.name for tool in select_static_tools(enabled_agent, access_context=access)
        }
        assert "manage_settings" not in {
            tool.name
            for tool in select_static_tools(
                enabled_agent, access_context=_access(parent_session_id="child-parent")
            )
        }
        assert (await action({**grant, "action": "tools_remove"}, approved=True))[
            "status"
        ] == "updated"
        async with factory() as db:
            row = await get_agent(db, agent.agent_id)
            assert "manage_settings" not in {
                tool.name
                for tool in select_static_tools(_agent(tools=row.tools), access_context=access)
            }
        assert (await action({**grant, "action": "tools_set"}, approved=True))[
            "status"
        ] == "updated"
        invalid = await action({**grant, "agent_id": "managed-secondary"})
        assert invalid["status"] == "invalid"
        with pytest.raises(AgentManagementError):
            await action({**grant, "agent_id": "other-owned"})
        conflict = await action({**grant, "deny_tools": ["builtin:manage_settings"]}, approved=True)
        assert conflict["status"] == "invalid"
        created = await action({"action": "create", "name": "Default-off management"})
        assert not created["agent"]["tools"]["allow_tools"]

        # Generic settings deliberately excludes tool configuration. Keep that
        # public boundary; raw opt-in normalization still uses the same inventory.
        raw = {
            "action": "settings_update",
            "agent_id": "managed-primary",
            "settings": {"opt_in_builtin_tools": ["builtin:manage_settings"]},
        }
        with pytest.raises(AgentManagementError, match="Unsupported settings field"):
            await action(raw)
        raw_tools: dict = {}
        _apply_tools_setting(
            raw_tools,
            "opt_in_builtin_tools",
            ["builtin:manage_settings"],
            SimpleNamespace(agent_type="primary"),
            inventory,
        )
        assert raw_tools["opt_in_builtin_tools"] == ["manage_settings"]
        with pytest.raises(AgentManagementError, match="secondary"):
            _apply_tools_setting(
                {},
                "opt_in_builtin_tools",
                ["manage_settings"],
                SimpleNamespace(agent_type="secondary"),
                inventory,
            )
        deps.assignable_tools = [tool for tool in inventory if tool.name != "manage_settings"]
        with pytest.raises(AgentManagementError, match="assignable inventory"):
            _apply_tools_setting(
                {},
                "opt_in_builtin_tools",
                ["manage_settings"],
                SimpleNamespace(agent_type="primary"),
                deps.assignable_tools,
            )

        denied = await handle_agent_management_tool(
            tool_name="manage_agents",
            arguments={**grant, "agent_id": "managed-primary"},
            deps=deps,
            user_email=agent.owner_email,
            current_agent_id=agent.agent_id,
            runtime_access=_access(parent_session_id="parent-session"),
        )
        assert denied.is_error
        assert denied.metadata["code"] == "agent_management_context_denied"
    finally:
        await engine.dispose()
