from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import cognis.core.workflow_engine as workflow_engine_module
from cognis.core.agent_loop import AgentLoop, PauseWaiter
from cognis.core.program_guidance import (
    PYTHON_EXECUTE_DESCRIPTION,
    PYTHON_EXECUTE_TOOL_NAME,
    render_runtime_guide,
)
from cognis.core.prompts import PromptContext, build_skill_guidance, build_system_instructions
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_composition import validate_composed_workflow
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_rendering import WorkflowRenderer
from cognis.core.workflow_step_scope import WorkflowStepScope
from cognis.executor.program_runtime import ProgramRuntime
from cognis.executor.tool_definitions_runtime import handle_python_execute
from cognis.models.agent import AgentDefinition
from cognis.models.task import TaskModel
from cognis.models.tool import EvaluationResult, ToolResult, stable_tool_id
from cognis.models.workflow import (
    ProgramStepConfig,
    StepDefinition,
    StepOutput,
    Workflow,
    WorkflowState,
)
from cognis.tools.builtin.tool_output import READ_TOOL_OUTPUT
from cognis.tools.executor.definitions import PYTHON_EXECUTE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _program_payload(**overrides: object) -> dict[str, object]:
    return {
        "language": "python",
        "description": "Count matching records and return their total.",
        "code": "await cognis.emit({'total': len(inputs['records'])})",
        "inputs": {"records": []},
        "input_schema": {
            "type": "object",
            "properties": {"records": {"type": "array"}},
            "required": ["records"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"total": {"type": "integer"}},
            "required": ["total"],
            "additionalProperties": False,
        },
        "tools": ["builtin:read_tool_output"],
        "limits": {"max_calls": 4, "concurrency": 2},
        **overrides,
    }


def _program_step(**overrides: object) -> StepDefinition:
    return StepDefinition(
        name="aggregate",
        type="program",
        program=ProgramStepConfig.model_validate(_program_payload(**overrides)),
    )


def test_program_workflow_model_round_trips_versioned_code_and_schemas() -> None:
    workflow = Workflow(
        workflow_id="wf-program",
        name="Program workflow",
        steps=[_program_step()],
    )

    exported = workflow.model_dump(mode="json", exclude_none=True)
    restored = Workflow.model_validate(exported)

    assert restored.steps[0].program is not None
    assert restored.steps[0].program.code == _program_payload()["code"]
    assert restored.steps[0].program.input_schema["type"] == "object"
    assert restored.steps[0].program.output_schema["properties"]["total"]["type"] == "integer"
    assert restored.model_dump(mode="json", exclude_none=True) == exported


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"description": "  "}, "must not be empty"),
        ({"code": "  "}, "must not be empty"),
        ({"input_schema": {"type": "array"}}, "type 'object'"),
        (
            {"output_schema": {"type": "object", "minLength": "bad"}},
            "valid JSON Schema",
        ),
        ({"tools": ["memory_search"]}, "canonical tool IDs"),
        (
            {"tools": ["builtin:read_tool_output", "builtin:read_tool_output"]},
            "duplicate",
        ),
        ({"limits": {"max_calls": 0}}, "greater than or equal to 1"),
    ],
)
def test_program_config_rejects_invalid_contracts(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ProgramStepConfig.model_validate(_program_payload(**overrides))


def test_program_requires_exact_config_and_rejects_agent_step_fields() -> None:
    with pytest.raises(ValidationError, match="requires program configuration"):
        StepDefinition(name="aggregate", type="program")

    with pytest.raises(ValidationError, match="incompatible configuration"):
        StepDefinition(
            name="aggregate",
            type="program",
            program=ProgramStepConfig.model_validate(_program_payload()),
            tool_call={"tool": "memory_search"},
        )

    with pytest.raises(ValidationError, match="cannot use agent/input/completion/review fields"):
        StepDefinition(
            name="aggregate",
            type="program",
            program=ProgramStepConfig.model_validate(_program_payload()),
            agent_override="system:research",
        )


def test_workflow_authoring_tool_schema_exposes_program_contract() -> None:
    from cognis.tools.builtin.orchestration import _WORKFLOW_STEP_AUTHORING_SCHEMA

    properties = _WORKFLOW_STEP_AUTHORING_SCHEMA["properties"]
    assert "program" in properties
    assert "program" in _WORKFLOW_STEP_AUTHORING_SCHEMA["properties"]["type"]["enum"]
    assert set(properties["program"]["required"]) == {
        "description",
        "code",
        "input_schema",
        "output_schema",
        "tools",
    }
    assert properties["program"]["properties"]["tools"]["uniqueItems"] is True


def test_program_input_and_emitted_object_use_the_declared_json_schemas() -> None:
    config = ProgramStepConfig.model_validate(_program_payload())
    workflow_engine_module._validate_program_json_value(
        config.input_schema,
        {"records": [{"id": "a"}]},
        label="program inputs",
    )
    workflow_engine_module._validate_program_json_value(
        config.output_schema,
        {"total": 1},
        label="program output",
    )

    with pytest.raises(ValueError, match="program inputs failed JSON Schema validation"):
        workflow_engine_module._validate_program_json_value(
            config.input_schema,
            {"records": "not-an-array"},
            label="program inputs",
        )
    with pytest.raises(ValueError, match="program output failed JSON Schema validation"):
        workflow_engine_module._validate_program_json_value(
            config.output_schema,
            {"total": "one"},
            label="program output",
        )


def test_workflow_step_scope_binds_program_to_owner_pinned_definition() -> None:
    step = _program_step()
    workflow = Workflow(workflow_id="wf-program", name="Program workflow", steps=[step])
    state = WorkflowState(
        effective_workflow_definition=workflow.model_dump(mode="json", exclude_none=True)
    )
    task = TaskModel(
        task_id="task-program",
        title="Program task",
        created_by="owner@example.com",
        agent_id="agent-1",
        creation_origin="owner_configured",
    )

    scope = WorkflowStepScope.build(
        task=task,
        workflow=workflow,
        state=state,
        step=step,
        session_id="session-1",
        step_run_id="step-run-1",
        logical_call_id="call-1",
        rendered_args={
            "code": step.program.code,
            "description": step.program.description,
            "inputs": {"records": []},
        },
    )

    assert scope.tool == PYTHON_EXECUTE_TOOL_NAME
    assert scope.step_run_id == "step-run-1"
    assert scope.authorized_by == "owner_configured"
    assert scope.step_definition_digest != step.program.code


def test_program_tool_allowlist_is_canonical_and_read_only() -> None:
    allowed = SimpleNamespace(
        name="read_tool_output",
        source=SimpleNamespace(type="builtin"),
        category="context",
        read_only=True,
    )
    writable = SimpleNamespace(
        name="artifact_write",
        source=SimpleNamespace(type="builtin"),
        category="artifact",
        read_only=False,
    )
    model_backed = SimpleNamespace(
        name="memory_search",
        source=SimpleNamespace(type="builtin"),
        category="memory",
        read_only=True,
    )
    registry = SimpleNamespace(
        items=lambda: [
            SimpleNamespace(definition=allowed),
            SimpleNamespace(definition=writable),
            SimpleNamespace(definition=model_backed),
        ]
    )

    workflow_engine_module._validate_program_tool_allowlist(
        [stable_tool_id(allowed)],
        registry,
    )
    with pytest.raises(PermissionError, match="not marked read-only"):
        workflow_engine_module._validate_program_tool_allowlist(
            [stable_tool_id(writable)],
            registry,
        )
    with pytest.raises(PermissionError, match="model-backed tool"):
        workflow_engine_module._validate_program_tool_allowlist(
            [stable_tool_id(model_backed)],
            registry,
        )
    with pytest.raises(PermissionError, match="not in the active tool registry"):
        workflow_engine_module._validate_program_tool_allowlist(
            ["builtin:search_tool_output"],
            SimpleNamespace(items=lambda: [SimpleNamespace(definition=allowed)]),
        )


def test_python_execute_wrapper_remains_non_read_only_for_recovery() -> None:
    with pytest.raises(PermissionError, match="read_only=false"):
        workflow_engine_module._validate_program_tool_wrapper(
            SimpleNamespace(definition=SimpleNamespace(read_only=True))
        )

    workflow_engine_module._validate_program_tool_wrapper(
        SimpleNamespace(definition=SimpleNamespace(read_only=False))
    )


def test_ptc_prompt_and_workflow_skill_guidance_are_runtime_gated() -> None:
    assert "Programmatic tool execution" not in (
        build_system_instructions(PromptContext.CHAT, visible_tool_names={"web_search"}) or ""
    )
    enabled = build_system_instructions(
        PromptContext.CHAT,
        visible_tool_names={"python_execute"},
    )
    assert enabled is not None and "approval request suspends its call" in enabled
    assert enabled is not None and "kernel generation" in enabled
    assert enabled is not None and "Do not start nested agents." in enabled
    assert enabled is not None and "model-backed memory/knowledge tools" not in enabled
    assert enabled is not None and "inference APIs" not in enabled
    assert "Program workflow steps" not in build_skill_guidance(
        visible_tool_names={"python_execute"}
    )
    workflow_guidance = build_skill_guidance(
        visible_tool_names={"python_execute", "create_workflow"}
    )
    assert "Program workflow steps" in workflow_guidance
    assert "model-invoking memory/knowledge tools" in workflow_guidance
    assert "inference APIs" in workflow_guidance
    assert "description explaining what" in PYTHON_EXECUTE_DESCRIPTION
    assert "model-backed memory/knowledge tools" not in PYTHON_EXECUTE_DESCRIPTION


def test_runtime_guide_uses_only_supplied_effective_plan_values() -> None:
    guide = render_runtime_guide(
        generation="generation-42",
        executor_policy={"policy": "read-only"},
        imports=["json"],
        limits={"max_calls": 3},
        eligible_tools=[
            {
                "id": "builtin:read_tool_output",
                "signature": "async def read(query: str)",
                "input_schema": {"type": "object"},
            },
            {
                "id": "builtin:memory_search",
                "signature": "async def memory_search(query: str)",
                "input_schema": {"type": "object"},
            },
        ],
        discovery={"enabled": False},
        exception_types=["ToolDenied"],
    )

    assert "generation-42" in guide
    assert "builtin:read_tool_output" in guide
    assert "builtin:memory_search" in guide
    assert "max_calls" in guide
    assert "builtin:artifact_write" not in guide


def test_composer_rejects_program_when_runtime_is_disabled() -> None:
    payload = Workflow(
        workflow_id="wf-composed",
        name="Candidate",
        steps=[_program_step()],
    ).model_dump(mode="json", exclude_none=True)

    with pytest.raises(ValueError, match="enabled python_execute runtime"):
        validate_composed_workflow(payload)
    accepted = validate_composed_workflow(payload, allow_program_steps=True)
    assert accepted.steps[0].type == "program"


@pytest.mark.asyncio
async def test_recovery_fails_ambiguous_program_without_replaying_it() -> None:
    step = _program_step()
    workflow = Workflow(workflow_id="wf-program", name="Program workflow", steps=[step])
    dispatch = AsyncMock()
    engine = WorkflowEngine(
        session_factory=_SessionFactory(),
        providers=SimpleNamespace(llm=None),
        agent_loop=SimpleNamespace(execute_controller_tool=dispatch),
        step_evaluator=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        session_manager=SimpleNamespace(),
        event_bus=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
    )
    failed = workflow_engine_module._DeterministicStepResult(
        output=StepOutput(summary="ambiguous"),
        step_run_id="step-run-1",
        step_run_status="failed",
    )
    engine._fail_deterministic_step = AsyncMock(return_value=failed)  # type: ignore[method-assign]
    latest = SimpleNamespace(
        task_id="task-program",
        step_run_id="step-run-1",
        status="running",
        output=None,
        runtime_info={
            "deterministic_substate": "executing",
            "tool_read_only": False,
            "program_execution": True,
        },
    )

    result = await engine._recover_deterministic_step(
        latest,
        step_def=step,
        workflow=workflow,
    )

    assert result is failed
    message = engine._fail_deterministic_step.await_args.args[4]
    assert "was not replayed" in message
    dispatch.assert_not_awaited()


class _SessionFactory:
    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> _SessionFactory:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_program_dispatches_normally_with_step_scope_and_emitted_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    step = _program_step(
        code='await cognis.emit({"count": len(inputs["rows"])})',
        inputs={"api_key": "{{ vars.api_key }}", "rows": "{{ vars.rows }}"},
        input_schema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string"},
                "rows": {"type": "array"},
            },
            "required": ["api_key", "rows"],
        },
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    assert step.program is not None
    program_config = step.program
    workflow = Workflow(workflow_id="wf-program", name="Program workflow", steps=[step])
    state = WorkflowState(
        current_step_index=0,
        effective_workflow_definition=workflow.model_dump(mode="json", exclude_none=True),
    )
    task = TaskModel(
        task_id="task-program",
        title="Program task",
        created_by="owner@example.com",
        agent_id="agent-1",
        creation_origin="owner_configured",
    )
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="owner@example.com",
        name="Agent",
    )
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=PYTHON_EXECUTE_TOOL))
    registry.register(RegisteredTool(definition=READ_TOOL_OUTPUT))
    approval_started = asyncio.Event()
    approval_resolved = asyncio.Event()

    class _Guardrails:
        async def declare_workflow_step(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def evaluate(
            self,
            *,
            context: dict[str, Any],
            **_kwargs: Any,
        ) -> EvaluationResult:
            approval_started.set()
            await approval_resolved.wait()
            return EvaluationResult(
                call_id=str(context["tool_call_id"]),
                decision="approve",
            )

    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "program-state.json"))
    program_runtime = ProgramRuntime(executor_id="executor-workflow")

    class _InProcessExecutorConnection:
        executor_id = "executor-workflow"
        executor_instance_id = "instance-workflow"

        def __init__(self) -> None:
            self.calls: list[Any] = []
            self.program_tool_call_handler: Any = None
            self.program_binding: Any = None

        def register_program_tool_call_handler(self, binding: Any, handler: Any) -> None:
            self.program_binding = binding
            self.program_tool_call_handler = handler

        def unregister_program_tool_call_handler(self, binding: Any) -> None:
            if self.program_binding is binding:
                self.program_binding = None
                self.program_tool_call_handler = None

        async def tool_execute(self, call: Any, **kwargs: Any) -> ToolResult:
            before_send = kwargs.get("before_send")
            if callable(before_send):
                await before_send(self.executor_id, self.executor_instance_id)
            on_sent = kwargs.get("on_sent")
            if callable(on_sent):
                await on_sent(self.executor_id, self.executor_instance_id)
            self.calls.append(call)
            assert callable(self.program_tool_call_handler)
            runtime_metadata = {
                **(call.runtime_metadata or {}),
                "_program_runtime": program_runtime,
                "_program_rpc_call": self.program_tool_call_handler,
            }
            return ToolResult.model_validate(
                await handle_python_execute(
                    call.arguments,
                    SimpleNamespace(
                        runtime_metadata=runtime_metadata,
                        output_chunk_callback=kwargs.get("output_chunk_callback"),
                    ),
                ),
            )

    executor_connection = _InProcessExecutorConnection()
    agent_loop: Any = AgentLoop.__new__(AgentLoop)
    agent_loop.tool_router = ToolRouter(guardrails=_Guardrails(), pause_waiter=PauseWaiter())
    agent_loop._program_invocations = {}
    agent_loop._program_invocations_lock = asyncio.Lock()
    agent_loop._get_tool_registry = lambda _ctx: registry
    agent_loop._get_classified_tool_registry = lambda _ctx, current: current
    agent_loop._stale_session_step_output = AsyncMock(return_value=None)
    agent_loop._refresh_executor_eligibility = AsyncMock(return_value=True)
    agent_loop._refresh_active_executor_connection = AsyncMock(return_value=executor_connection)
    agent_loop._tool_runtime_metadata_for_call = lambda _ctx, _call: {}
    agent_loop._bind_tool_dispatch_fence = AsyncMock()
    agent_loop._sync_guardrails_terminal_result = AsyncMock()
    agent_loop._is_same_executor_transient_failure = lambda _result: False
    agent_loop._step_log_metadata = lambda _ctx: {}
    agent_loop.persist_controller_tool_output = AsyncMock(
        side_effect=lambda _ctx, _call, result: result
    )
    engine = WorkflowEngine(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        providers=SimpleNamespace(llm=None),
        agent_loop=agent_loop,
        step_evaluator=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_registry=SimpleNamespace(),  # type: ignore[arg-type]
        session_manager=SimpleNamespace(
            refresh_intaris_session_policy=AsyncMock(
                return_value={"evaluation_policy_version": "hybrid_v1"}
            )
        ),
        event_bus=SimpleNamespace(),  # type: ignore[arg-type]
        pause_waiter=PauseWaiter(),
    )
    conversation = SimpleNamespace(conversation_id="conversation-1")
    session = SimpleNamespace(
        session_id="session-1",
        intaris_session_id="intaris-1",
        parent_session_id=None,
        delegation_mode=None,
        user_email="owner@example.com",
        agent_id="agent-1",
        conversation_id="conversation-1",
    )
    runtime = SimpleNamespace(
        tool_registry=registry,
        executor_connection=executor_connection,
        executor_environment=SimpleNamespace(home="/home/owner", cwd="/home/owner"),
        executor_pool=None,
        active_executor_id=executor_connection.executor_id,
        refresh_executor_pool=None,
        runtime_info={"executor_runtime_marker": "preserved"},
        cleanup=AsyncMock(side_effect=program_runtime.close),
        # The selected executor has opted into the program runtime.
        program_policy=AsyncMock(return_value=True),
    )
    finalized = workflow_engine_module._DeterministicStepResult(
        output=StepOutput(summary="done"),
        step_run_id="step-run-1",
        step_run_status="approved",
    )
    engine._persist_deterministic_checkpoint = AsyncMock()  # type: ignore[method-assign]
    engine._resolve_step_agents = AsyncMock(return_value=(agent, agent))  # type: ignore[method-assign]
    engine._reuse_or_create_step_session = AsyncMock(  # type: ignore[method-assign]
        return_value=(conversation, session, False)
    )
    engine._executor_agent_for_step = lambda *_args: agent  # type: ignore[method-assign]
    engine._task_trigger_context = AsyncMock(return_value={})  # type: ignore[method-assign]
    engine._resolve_step_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    engine._report_deterministic_step_input = AsyncMock()  # type: ignore[method-assign]
    engine._record_deterministic_tool_event = AsyncMock()  # type: ignore[method-assign]
    engine._finalize_deterministic_step = AsyncMock(return_value=finalized)  # type: ignore[method-assign]
    scoped_task: dict[str, TaskModel] = {"value": task}

    async def _refresh_scope_origin(_task: TaskModel, _factory: object) -> tuple[TaskModel, None]:
        return scoped_task["value"], None

    monkeypatch.setattr(
        "cognis.core.workflow_step_scope.refresh_scope_origin",
        _refresh_scope_origin,
    )
    monkeypatch.setattr(
        "cognis.core.artifact_sharing.grant_referenced_inputs",
        AsyncMock(),
    )

    execution = asyncio.create_task(
        engine._execute_tool_call_step(
            task,
            step,
            state,
            workflow,
            "step-run-1",
            {},
            WorkflowRenderer(),
            {
                "vars": {
                    "api_key": "secret-value",
                    "rows": [1, 2, 3],
                }
            },
            cancel_event=None,
        )
    )
    await approval_started.wait()
    assert not execution.done()
    assert executor_connection.calls == []
    engine._finalize_deterministic_step.assert_not_awaited()
    approval_resolved.set()
    result = await execution

    assert result is finalized
    assert len(executor_connection.calls) == 1
    tool_call = executor_connection.calls[0]
    assert tool_call.name == PYTHON_EXECUTE_TOOL_NAME
    assert tool_call.execution_scope_id == "step-run-1"
    assert tool_call.arguments["code"] == program_config.code
    assert tool_call.arguments["inputs"] == {
        "api_key": "secret-value",
        "rows": [1, 2, 3],
    }
    assert tool_call.arguments["expected_generation"] is None
    ctx = engine._record_deterministic_tool_event.await_args_list[0].args[0]
    assert ctx.step_definition.type == "program"
    assert ctx.step_definition.program is not None
    assert ctx.step_definition.program.tools == ["builtin:read_tool_output"]
    assert ctx.step_run_id == "step-run-1"
    assert ctx.runtime_info["executor_runtime_marker"] == "preserved"
    assert ctx.runtime_info["program_digest"] == workflow_engine_module._program_digest(
        program_config
    )
    assert (
        tool_call.runtime_metadata["program_runtime"]["program_digest"]
        == ctx.runtime_info["program_digest"]
    )
    assert ctx.workflow_step_scope is not None
    assert ctx.workflow_step_scope.tool == PYTHON_EXECUTE_TOOL_NAME
    finalize_args = engine._finalize_deterministic_step.await_args
    assert finalize_args is not None
    output = finalize_args.args[4]
    assert output.outputs == {"count": 3}
    assert output.metadata["program_status"] == "completed"
    assert output.metadata["program_kernel_generation"] >= 1
    assert output.metadata["program_child_call_references"] == []
    assert output.metadata["program_stdout_preview"] == ""
    assert "program_generation" not in output.metadata
    assert "program_call_refs" not in output.metadata
    assert "program_stdout" not in output.metadata
    tool_event = engine._record_deterministic_tool_event.await_args_list[0].args[2]
    assert tool_event.data["arguments"]["code"] == program_config.code
    assert tool_event.data["arguments"]["inputs"]["api_key"] == "[redacted]"
    assert runtime.cleanup.await_count == 1

    scoped_task["value"] = task.model_copy(update={"creation_origin": "agent_composed"})
    with pytest.raises(PermissionError, match="owner-reviewed pinned workflow"):
        await engine._execute_tool_call_step(
            task,
            step,
            state,
            workflow,
            "step-run-2",
            {},
            WorkflowRenderer(),
            {"vars": {"api_key": "secret-value", "rows": [1]}},
            cancel_event=None,
        )
    assert len(executor_connection.calls) == 1
    assert runtime.cleanup.await_count == 2

    recovered_failure = workflow_engine_module._DeterministicStepResult(
        output=StepOutput(summary="ambiguous"),
        step_run_id="step-run-1",
        step_run_status="failed",
    )
    engine._fail_deterministic_step = AsyncMock(return_value=recovered_failure)  # type: ignore[method-assign]
    latest = SimpleNamespace(
        task_id="task-program",
        step_run_id="step-run-1",
        status="running",
        output=None,
        runtime_info={
            "deterministic_substate": "executing",
            "tool_read_only": False,
            "program_execution": True,
        },
    )
    recovered = await engine._recover_deterministic_step(
        latest,
        step_def=step,
        workflow=workflow,
    )
    assert recovered is recovered_failure
    assert len(executor_connection.calls) == 1
