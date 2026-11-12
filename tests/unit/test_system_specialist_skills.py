"""System specialist initialization uses real scoped skills and prompt composition."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api import runtime_support
from cognis.core.agent_loop import (
    AgentLoop,
    PauseWaiter,
    SessionLock,
    _delegated_system_builtin_tool_ids,
    _filter_model_inventory_tools,
)
from cognis.core.agent_registry import SYSTEM_AGENTS, AgentRegistry, compose_system_agent_prompt
from cognis.core.context import ContextAssembler
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.prompts import PromptContext
from cognis.core.system_skills import get_system_skill_default
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.config import ModelInfo
from cognis.models.tool import Permission, ToolDefinition, ToolSource, stable_tool_id
from cognis.models.workflow import StepOutput
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_skill,
    create_skill_version,
    create_user,
    set_current_version,
    upsert_system_agent_override,
)
from cognis.tools.builtin.skill_management import handle_skill_management_tool
from cognis.tools.registry import ToolRegistry
from tests.unit.test_agent_loop import (
    _NoopEventBus,
    _NoopSessionCache,
    _NoopSessionManager,
    _SessionStub,
)
from tests.unit.test_runtime_support import (
    _ReadyWebSocketProvider,
    _runtime_providers_with_ws,
)


def _override(**values):
    return SimpleNamespace(
        **{
            "disabled": False,
            "llm_config_override": None,
            "execution_override": None,
            "skills_override": None,
            "tools_override": None,
            "permissions_override": None,
            "agent_profiles_override": None,
            "default_agent_profile_id_override": None,
            **values,
        }
    )


@pytest.mark.parametrize("deny_field", ["deny_tools", "disabled_tools", "disabled_categories"])
def test_skill_eligibility_and_activation_respect_negative_tool_config(deny_field):
    tool = ToolDefinition(
        name="scoped_inspect",
        description="Inspect.",
        parameters={"type": "object", "properties": {}},
        category="skill",
        source=ToolSource(type="skill", skill_id="scoped", raw_tool_name="inspect"),
    )
    agent = SYSTEM_AGENTS["system:code-review"].model_copy(deep=True)
    agent.tools = {
        **agent.tools,
        deny_field: ["skill" if deny_field == "disabled_categories" else "inspect"],
    }
    registry = SimpleNamespace(list_tools=lambda: [tool])
    assert stable_tool_id(tool) not in _delegated_system_builtin_tool_ids(agent, registry)
    assert not _filter_model_inventory_tools(
        agent, [tool], activated_tool_ids={stable_tool_id(tool)}
    )


def test_skill_filter_does_not_change_legacy_mcp_category_semantics():
    tool = ToolDefinition(
        name="query",
        description="Query.",
        parameters={"type": "object", "properties": {}},
        category="mcp",
        source=ToolSource(type="local_mcp", server_id="observability"),
    )
    agent = AgentDefinition(
        agent_id="research-agent",
        name="Research",
        owner_email="alice@example.com",
        tools={"disabled_categories": ["mcp"]},
    )
    assert _filter_model_inventory_tools(agent, [tool]) == [tool]


@pytest.fixture
async def skill_db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/specialists.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(db, email="alice@example.com", name="Alice", password_hash="not-a-login")
        await create_user(db, email="bob@example.com", name="Bob", password_hash="not-a-login")
        for skill_id in ("cognis-coding", "cognis-web-research"):
            defaults = get_system_skill_default(skill_id)
            row = await create_skill(
                db,
                skill_id=skill_id,
                name=defaults["name"],
                description=defaults["description"],
                instructions=defaults["instructions"],
                linked_tool_ids=defaults["linked_tool_ids"],
                is_system=True,
            )
            version = await create_skill_version(
                db,
                skill_id=row.skill_id,
                version_number=1,
                content_hash=skill_id,
                instructions=defaults["instructions"],
                linked_tool_ids=defaults["linked_tool_ids"],
            )
            await set_current_version(db, row.skill_id, version.version_id)
        own = await create_skill(
            db,
            name="Scoped helper",
            instructions="Inspect a source with the helper.",
            owner_email="alice@example.com",
            tools=[{"name": "inspect", "description": "Inspect a source."}],
            linked_tool_ids=["builtin:manage_agents", "builtin:write"],
        )
        foreign = await create_skill(
            db,
            name="Foreign private procedure",
            instructions="Private procedure never visible to Alice.",
            owner_email="bob@example.com",
        )
        await db.commit()
    try:
        yield factory, own.skill_id, foreign.skill_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_override", [False, True])
@pytest.mark.parametrize("deny_executable", [False, True])
@pytest.mark.parametrize(
    ("agent_id", "expected_skill"),
    [
        ("system:implement", "cognis-coding"),
        ("system:code-review", "cognis-coding"),
        ("system:architect", "cognis-coding"),
        ("system:research", "cognis-web-research"),
        ("system:explore", None),
        ("system:committer", None),
    ],
)
async def test_specialist_initialization_prompt_and_activation(
    monkeypatch, skill_db, agent_id, expected_skill, legacy_override, deny_executable
):
    factory, own_id, foreign_id = skill_db
    base = SYSTEM_AGENTS[agent_id]
    old_skills = {
        "items": [
            {"skill_id": item["skill_id"], "enabled": True}
            for item in (base.skills or {}).get("items", [])
        ]
    }
    if legacy_override or deny_executable:
        async with factory() as db:
            await upsert_system_agent_override(
                db,
                owner_email="alice@example.com",
                agent_id=agent_id,
                skills_override=old_skills if legacy_override else None,
                tools_override={
                    **base.tools,
                    **({"deny_tools": [f"skill:{own_id}:inspect"]} if deny_executable else {}),
                },
            )
            await db.commit()
    # Only infrastructure is faked: skill DB, runtime planning, child
    # initialization, identity/prefix composition and exposure are real.
    monkeypatch.setattr(
        runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
    )
    monkeypatch.setattr(
        runtime_support,
        "_resolve_eligible_executor_config",
        AsyncMock(
            return_value={
                "executor_id": "alice_exec",
                "executor_type": "websocket",
                "enabled_tools": [],
                "enabled_tool_groups": [],
                "config": {},
                "executor_owner_email": "alice@example.com",
                "owner_email": "alice@example.com",
                "selection_source": "explicit",
                "runtime_state": "active",
                "desired_config_version": 1,
                "applied_config_version": 1,
            }
        ),
    )
    monkeypatch.setattr(
        runtime_support,
        "_resolve_web_config",
        AsyncMock(return_value={"web_available_backends": ["direct"], "web_backend": "direct"}),
    )
    providers = _runtime_providers_with_ws(_ReadyWebSocketProvider("alice_exec"))
    runtime_factory = runtime_support.build_step_runtime_factory(
        providers=providers,
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=factory,
    )
    manager = _NoopSessionManager()
    manager.session_factory = factory
    loop = AgentLoop(
        providers=providers,
        session_manager=manager,
        session_cache=_NoopSessionCache(),
        context_assembler=SimpleNamespace(),
        compaction_strategy=SimpleNamespace(),
        tool_router=SimpleNamespace(),
        remember_queue=SimpleNamespace(),
        event_bus=_NoopEventBus(),
        session_lock=SessionLock(),
        pause_waiter=PauseWaiter(),
        step_runtime_factory=runtime_factory,
    )
    inspected = []

    async def inspect_child(ctx, **_):
        inspected.append(ctx)
        assert ctx.policy.skip_memory
        summaries = ctx.agent.skills["_runtime_skill_summaries"]
        assert foreign_id not in {item["skill_id"] for item in summaries}
        assert ctx.agent.skills.get("_auto_loaded_skill_ids", []) == (
            [expected_skill] if expected_skill else []
        )
        assembler = ContextAssembler(
            memory=None,
            guardrails=None,
            llm=None,
            session_cache=None,
            session_manager=None,
            compaction_threshold=0.85,
        )
        entries = assembler._compose_prefix_entries(
            agent=ctx.agent,
            project_instructions=[],
            memory_instructions=None,
            core_memories=None,
            compaction_summary=None,
        )
        prefix = assembler._compose_immutable_prefix(
            agent=ctx.agent,
            prompt_context=PromptContext.DELEGATION,
            prefix_entries=entries,
            resolved_model="gpt-5.4",
        )
        if expected_skill:
            body = get_system_skill_default(expected_skill)["instructions"]
            assert prefix.count(body) == 1
        if agent_id == "system:implement":
            assert "Use the loaded\nCognis Coding skill" in prefix
            assert "Identify project-specific verification commands" not in prefix
            assert "Do not delegate implementation work further" in prefix
        if agent_id == "system:code-review":
            assert "Do NOT write any files" in prefix
            assert "Review ONLY" in prefix
        if agent_id == "system:architect":
            assert "Do NOT invent requirements" in prefix

        def inventory(activated):
            return loop._compile_model_tool_exposure(
                ctx,
                model="gpt-5.4",
                provider_id=None,
                model_info=ModelInfo(model_id="gpt-5.4", context_window=128_000),
                controller_tool_schemas=[],
                authorized_deferred_controller_tools=[],
                promoted_tool_ids=set(),
                activated_tool_ids=activated,
            )

        before = inventory(set())
        names = {tool.name for tool in before[4]}
        assert {"skill_load", "skill_get"} <= names
        assert "manage_agents" not in names
        assert "skill_write" not in names
        assert "list_credentials" not in names
        if agent_id != "system:implement":
            assert "write" not in names
        assert all(tool.source.type != "skill" for tool in before[4])
        result = await handle_skill_management_tool(
            "skill_load", {"skill_id": own_id}, factory, "alice@example.com"
        )
        assert not result.is_error, result.output
        active = set()
        activation = await loop._apply_skill_activation(
            ctx, metadata=result.metadata, promoted_tool_ids=set(), activated_tool_ids=active
        )
        after_ids = {stable_tool_id(tool) for tool in inventory(active)[4]}
        if deny_executable:
            assert f"skill:{own_id}:inspect" not in after_ids
            assert f"skill:{own_id}:inspect" not in (
                ctx.step_definition.step_profile.tool_overrides.include
            )
        else:
            assert f"skill:{own_id}:inspect" in after_ids
        assert "builtin:manage_agents" not in activation.tool_ids
        if agent_id != "system:implement":
            assert "builtin:write" not in activation.tool_ids
        for tool_name in ("skill_load", "skill_get"):
            denied = await handle_skill_management_tool(
                tool_name, {"skill_id": foreign_id}, factory, "alice@example.com"
            )
            assert denied.is_error
            assert "Private procedure" not in denied.output
        return StepOutput(summary="checked", content="checked", outputs={}, claims=[])

    monkeypatch.setattr(loop, "run_step", inspect_child)
    await loop._run_child_session(
        child_session=_SessionStub(
            session_id="specialist-child",
            user_email="alice@example.com",
            agent_id=agent_id,
            intaris_session_id="specialist-child",
        ),
        conversation=SimpleNamespace(conversation_id="specialist-conversation"),
        agent=AgentDefinition(
            agent_id="alice-primary",
            owner_email="alice@example.com",
            name="Primary",
            skills={"_auto_loaded_skill_contexts": ["Parent-only instructions"]},
        ),
        task_description="Check the assigned bounded scope.",
        parent_intaris_session_id="parent-session",
    )
    assert len(inspected) == 1


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"skill_id": "cognis-coding", "enabled": False}],
        [{"skill_id": "cognis-coding", "auto_load_instructions": False}],
    ],
)
def test_explicit_skill_override_keeps_fallback_prompt(items):
    base = SYSTEM_AGENTS["system:implement"]
    agent = AgentRegistry._apply_system_agent_override(
        base, _override(skills_override={"items": items}), include_disabled=False
    )
    assert compose_system_agent_prompt(agent) == base.system_prompt
    assert len(agent.skills["items"]) == len(items)
    for expected, actual in zip(items, agent.skills["items"], strict=True):
        assert expected.items() <= actual.items()


@pytest.mark.parametrize(
    "tools",
    [
        {"builtin_tools": ["read"], "deny_tools": ["skill_load", "skill_get"]},
        {"builtin_tools": ["read"], "disabled_categories": ["skill"]},
    ],
)
def test_specialist_readers_respect_explicit_tool_disable(tools):
    agent = SYSTEM_AGENTS["system:implement"].model_copy(deep=True)
    agent.tools = tools
    names = {tool.name for tool in runtime_support.select_static_tools(agent)}
    assert not ({"skill_load", "skill_get"} & names)


def test_loader_does_not_override_permissions():
    agent = SYSTEM_AGENTS["system:implement"].model_copy(deep=True)
    agent.permissions = AgentPermissions(
        tool_permissions={"*": Permission.EVALUATE, "skill_load": Permission.DENY},
        allowed_credentials=[],
    )
    from cognis.core.agent_loop import _filter_model_inventory_tools

    inventory = _filter_model_inventory_tools(agent, runtime_support.select_static_tools(agent))
    assert "skill_load" not in {tool.name for tool in inventory}
