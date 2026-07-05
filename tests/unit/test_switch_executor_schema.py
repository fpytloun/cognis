"""Stage 36 schema overlay coverage:

- ``switch_executor`` controller tool is exposed only when the agent has
  >=2 USABLE assigned executors (not just >=2 assigned).
- ``target_executor`` enum overlay only appears on executor-routed tools
  observed on >=2 per-call-routable executors.
- Controller-injected tools never carry ``target_executor``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from cognis.core.agent_loop import AgentLoop
from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
)
from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _target(
    executor_id: str,
    *,
    is_primary: bool = True,
    state: ExecutorAvailability = ExecutorAvailability.USABLE,
    executor_type: str = "websocket",
    observed_tools: list[dict] | None = None,
) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type=executor_type,
        is_primary=is_primary,
        selection_source="explicit",
        description=None,
        state=state,
        enabled_tools=["*"],
        observed_tools=observed_tools or [],
    )


def _make_loop() -> AgentLoop:
    return AgentLoop.__new__(AgentLoop)


def _ctx(
    pool: ExecutorPool,
    *,
    active_executor_id: str | None = None,
    parent_session_id: str | None = None,
) -> object:
    """Build a minimal StepContext-like object."""
    return SimpleNamespace(
        session=SimpleNamespace(parent_session_id=parent_session_id),
        executor_pool=pool,
        active_executor_id=active_executor_id,
        agent=SimpleNamespace(
            agent_id="a",
            agent_type="primary",
            owner_email="user@example.com",
            tools={},
            permissions={},
            skills={},
        ),
        policy=SimpleNamespace(
            step_complete_available=False,
            require_step_complete=False,
            event_flush_strategy="default",
        ),
        orchestration_mode=MagicMock(),
        interaction_mode="step_requests",
        controller_tool_surface="workflow",
        step_definition=SimpleNamespace(allow_questions=False, metadata_contract=None),
        deliverable_step_run_id=None,
        post_deliverable_pending=False,
    )


# --------------------------------------------------------------------------
# switch_executor controller-tool exposure
# --------------------------------------------------------------------------


def _switch_tool(schemas: list[dict]) -> dict | None:
    for s in schemas:
        if s.get("function", {}).get("name") == "switch_executor":
            return s
    return None


def test_switch_executor_hidden_with_one_usable() -> None:
    """Only one usable executor → no switch_executor schema."""
    loop = _make_loop()
    # Stub the helpers used by _build_controller_tool_schemas
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(
        primary=[
            _target("exec-a"),
            _target("exec-b", state=ExecutorAvailability.OFFLINE),
        ]
    )
    schemas = loop._build_controller_tool_schemas(_ctx(pool, active_executor_id="exec-a"))
    assert _switch_tool(schemas) is None


def test_switch_executor_hidden_with_zero_usable() -> None:
    loop = _make_loop()
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(
        primary=[
            _target("exec-a", state=ExecutorAvailability.OFFLINE),
            _target("exec-b", state=ExecutorAvailability.OFFLINE),
        ]
    )
    schemas = loop._build_controller_tool_schemas(_ctx(pool))
    assert _switch_tool(schemas) is None


def test_switch_executor_visible_with_two_usable() -> None:
    """Two or more usable assigned executors → schema injected with enum."""
    loop = _make_loop()
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(
        primary=[_target("exec-a"), _target("exec-b")],
        additional=[_target("exec-add", is_primary=False)],
    )
    schemas = loop._build_controller_tool_schemas(_ctx(pool, active_executor_id="exec-a"))
    schema = _switch_tool(schemas)
    assert schema is not None
    enum_values = schema["function"]["parameters"]["properties"]["executor_id"]["enum"]
    # Must contain only the USABLE assigned executors, sorted
    assert enum_values == ["exec-a", "exec-add", "exec-b"]


def test_switch_executor_hidden_for_delegated_child_session() -> None:
    """Child sessions may switch routing locally but must not mutate the parent conversation."""
    loop = _make_loop()
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(primary=[_target("exec-a"), _target("exec-b")])

    schemas = loop._build_controller_tool_schemas(
        _ctx(pool, active_executor_id="exec-a", parent_session_id="sess-parent")
    )

    assert _switch_tool(schemas) is None


def test_child_executor_install_can_remain_session_local() -> None:
    loop = _make_loop()
    ctx = SimpleNamespace(
        active_executor_id="exec-a",
        conversation=SimpleNamespace(active_executor_id="exec-a"),
    )

    ok = loop._install_active_executor_target(
        ctx,
        _target("exec-b", executor_type="local"),
        update_conversation=False,
    )

    assert ok is False
    assert ctx.active_executor_id == "exec-b"
    assert ctx.conversation.active_executor_id == "exec-a"


def test_switch_executor_enum_excludes_offline() -> None:
    loop = _make_loop()
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(
        primary=[_target("exec-a"), _target("exec-b")],
        additional=[
            _target("exec-add", is_primary=False),
            _target("exec-down", is_primary=False, state=ExecutorAvailability.OFFLINE),
        ],
    )
    schemas = loop._build_controller_tool_schemas(_ctx(pool))
    schema = _switch_tool(schemas)
    assert schema is not None
    enum_values = schema["function"]["parameters"]["properties"]["executor_id"]["enum"]
    assert "exec-down" not in enum_values


# --------------------------------------------------------------------------
# target_executor overlay on executor-routed tools
# --------------------------------------------------------------------------


def _build_registry_with_executor_tool(name: str = "bash") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name=name,
                description="Run shell",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                source=ToolSource(type="executor"),
                category="shell",
            )
        )
    )
    return registry


def _exec_ctx(
    pool: ExecutorPool, registry: ToolRegistry, *, active_executor_id: str | None = None
) -> object:
    return SimpleNamespace(
        executor_pool=pool,
        tool_registry=registry,
        active_executor_id=active_executor_id,
    )


def test_target_executor_overlay_two_websocket_executors() -> None:
    loop = _make_loop()
    loop._get_tool_registry = lambda c: c.tool_registry
    pool = ExecutorPool(
        primary=[
            _target(
                "exec-a",
                executor_type="websocket",
                observed_tools=[{"name": "bash"}],
            ),
            _target(
                "exec-b",
                executor_type="websocket",
                observed_tools=[{"name": "bash"}],
            ),
        ]
    )
    registry = _build_registry_with_executor_tool("bash")
    schemas = loop._get_executor_tool_schemas(
        _exec_ctx(pool, registry, active_executor_id="exec-a")
    )
    bash = next(s for s in schemas if s["function"]["name"] == "bash")
    properties = bash["function"]["parameters"]["properties"]
    assert "target_executor" in properties
    assert properties["target_executor"]["enum"] == ["exec-a", "exec-b"]


def test_target_executor_overlay_single_executor_omitted() -> None:
    loop = _make_loop()
    loop._get_tool_registry = lambda c: c.tool_registry
    pool = ExecutorPool(
        primary=[
            _target(
                "exec-a",
                executor_type="websocket",
                observed_tools=[{"name": "bash"}],
            ),
        ]
    )
    registry = _build_registry_with_executor_tool("bash")
    schemas = loop._get_executor_tool_schemas(
        _exec_ctx(pool, registry, active_executor_id="exec-a")
    )
    bash = next(s for s in schemas if s["function"]["name"] == "bash")
    assert "target_executor" not in bash["function"]["parameters"]["properties"]


def test_target_executor_overlay_filters_in_process_additional() -> None:
    """Per-call routing only works for the active or websocket additionals."""
    loop = _make_loop()
    loop._get_tool_registry = lambda c: c.tool_registry
    pool = ExecutorPool(
        primary=[
            _target(
                "exec-active",
                executor_type="in_process",
                observed_tools=[{"name": "bash"}],
            ),
        ],
        additional=[
            _target(
                "exec-add-ws",
                is_primary=False,
                executor_type="websocket",
                observed_tools=[{"name": "bash"}],
            ),
            _target(
                "exec-add-ip",
                is_primary=False,
                executor_type="in_process",
                observed_tools=[{"name": "bash"}],
            ),
        ],
    )
    registry = _build_registry_with_executor_tool("bash")
    schemas = loop._get_executor_tool_schemas(
        _exec_ctx(pool, registry, active_executor_id="exec-active")
    )
    bash = next(s for s in schemas if s["function"]["name"] == "bash")
    enum_values = bash["function"]["parameters"]["properties"]["target_executor"]["enum"]
    # active (in_process) is reachable; additional websocket is reachable;
    # additional in_process is NOT.
    assert sorted(enum_values) == ["exec-active", "exec-add-ws"]


def test_target_executor_overlay_respects_observed_tools_intersection() -> None:
    """Tool observed on only one executor → no overlay even with multiple executors."""
    loop = _make_loop()
    loop._get_tool_registry = lambda c: c.tool_registry
    pool = ExecutorPool(
        primary=[
            _target(
                "exec-a",
                executor_type="websocket",
                observed_tools=[{"name": "bash"}],
            ),
            _target(
                "exec-b",
                executor_type="websocket",
                observed_tools=[{"name": "other"}],
            ),
        ]
    )
    registry = _build_registry_with_executor_tool("bash")
    schemas = loop._get_executor_tool_schemas(
        _exec_ctx(pool, registry, active_executor_id="exec-a")
    )
    bash = next(s for s in schemas if s["function"]["name"] == "bash")
    assert "target_executor" not in bash["function"]["parameters"]["properties"]


def test_no_controller_tool_carries_target_executor() -> None:
    """Regression: target_executor must never appear on a controller-injected tool."""
    loop = _make_loop()
    loop._deliverable_owner_step_run_id = lambda c: None
    pool = ExecutorPool(
        primary=[_target("exec-a"), _target("exec-b")],
    )
    schemas = loop._build_controller_tool_schemas(_ctx(pool, active_executor_id="exec-a"))
    for schema in schemas:
        properties = schema["function"]["parameters"].get("properties", {})
        assert "target_executor" not in properties, (
            f"Controller tool {schema['function']['name']} unexpectedly carries target_executor"
        )


def test_get_executor_tool_schemas_does_not_mutate_original_parameters() -> None:
    """Schema overlay must deep-copy so the registered ToolDefinition is unaffected."""
    loop = _make_loop()
    loop._get_tool_registry = lambda c: c.tool_registry
    pool = ExecutorPool(
        primary=[
            _target("exec-a", executor_type="websocket", observed_tools=[{"name": "bash"}]),
            _target("exec-b", executor_type="websocket", observed_tools=[{"name": "bash"}]),
        ]
    )
    registry = _build_registry_with_executor_tool("bash")
    original_props = dict(registry.get("bash").definition.parameters["properties"])
    loop._get_executor_tool_schemas(_exec_ctx(pool, registry, active_executor_id="exec-a"))
    after_props = registry.get("bash").definition.parameters["properties"]
    assert "target_executor" not in original_props
    assert "target_executor" not in after_props
