from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cognis.api import runtime_support
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.program_config import PROGRAM_RUNTIME_TOOL_NAMES
from cognis.core.program_plan import build_effective_program_plan
from cognis.core.tool_router import ToolRouter
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.program import ProgramRuntimeBinding
from cognis.models.session import SessionModel
from cognis.models.skill import ResolvedSkillSet
from cognis.models.tool import (
    ExecutorConfig,
    Permission,
    ToolCall,
)
from cognis.providers.executor.composite import CompositeExecutorProvider
from cognis.providers.executor.in_process import InProcessExecutorProvider
from cognis.security import create_password_hasher
from cognis.tools.builtin.datetime_tools import GET_CURRENT_DATETIME_TOOL
from cognis.tools.executor.definitions import ALL_EXECUTOR_TOOLS
from cognis.tools.registry import RegisteredTool, ToolRegistry


class _ApprovingGuardrails:
    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, object],
        context: dict[str, object],
    ) -> object:
        del session_id, tool_name, arguments, context
        return SimpleNamespace(
            decision="approve",
            reasoning="Provider-dispatch unit test",
            risk="low",
            path="test",
            latency_ms=0,
            call_id="ptc-provider-dispatch-evaluation",
        )


async def _allow_program(_connection: object) -> bool:
    return True


@pytest.mark.asyncio
async def test_program_runtime_tools_use_registered_in_process_provider_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    _, engine, session_factory, _ = await bootstrap_runtime(load_config(), create_password_hasher())
    provider = InProcessExecutorProvider(session_factory=session_factory)
    executor_runtime = pytest.importorskip("cognis.executor.tool_definitions_runtime")
    handlers = executor_runtime.executor_tool_handlers()
    scope_id = uuid4().hex
    executor_id = f"ptc-router-provider-{uuid4().hex}"
    session = SessionModel(
        session_id=f"ptc-provider-session-{scope_id}",
        conversation_id=f"ptc-provider-conversation-{scope_id}",
        user_email="ptc-provider@example.test",
        agent_id="ptc-provider-agent",
    )
    agent = AgentDefinition(
        agent_id=session.agent_id,
        owner_email=session.user_email,
        name="PTC provider dispatch test",
        permissions=AgentPermissions(
            tool_permissions={
                tool_name: Permission.EVALUATE for tool_name in PROGRAM_RUNTIME_TOOL_NAMES
            }
        ),
    )

    try:
        program_tools = [
            tool for tool in ALL_EXECUTOR_TOOLS if tool.name in PROGRAM_RUNTIME_TOOL_NAMES
        ]
        assert {tool.name for tool in program_tools} == set(PROGRAM_RUNTIME_TOOL_NAMES)
        handle = await provider.spawn(
            ExecutorConfig(
                executor_id=executor_id,
                program_runtime_executor_id=executor_id,
                tools=program_tools,
                metadata={
                    "conversation_id": session.conversation_id,
                    "session_id": session.session_id,
                    "agent_id": agent.agent_id,
                    "user_email": session.user_email,
                },
            )
        )
        connection = await provider.get_executor(handle)
        registry = ToolRegistry()
        for definition in program_tools:
            registry.register(
                RegisteredTool(
                    definition=definition,
                    handler=handlers[definition.name],
                )
            )
        plan = build_effective_program_plan(registry)
        router = ToolRouter(guardrails=_ApprovingGuardrails())

        async def dispatch(
            *,
            call_id: str,
            name: str,
            arguments: dict[str, object],
            expected_generation: int | None = None,
        ):
            binding = ProgramRuntimeBinding(
                program_id=call_id,
                execution_id=f"{call_id}-execution",
                scope_id=session.conversation_id,
                expected_generation=expected_generation,
                description="Exercise the real in-process program provider.",
                tool_manifest_digest=plan.plan_hash,
                executor_id=handle.executor_id,
                executor_instance_id=connection.executor_instance_id,
            )
            return await router.execute(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    execution_scope_id=session.session_id,
                    runtime_metadata={
                        "program_runtime": binding.model_dump(mode="json"),
                        "turn_id": "ptc-provider-turn",
                        "tool_call_id": call_id,
                        "runtime_access": {
                            "conversation_id": session.conversation_id,
                            "session_id": session.session_id,
                            "agent_id": agent.agent_id,
                            "user_email": session.user_email,
                        },
                    },
                ),
                session,
                agent,
                registry,
                connection,
                program_policy=_allow_program,
            )

        executed = await dispatch(
            call_id="ptc-provider-cell",
            name="python_execute",
            arguments={
                "description": "Run one bounded provider-dispatch cell.",
                "code": 'await cognis.emit({"provider_dispatch": "completed"})',
            },
        )
        assert executed.is_error is False, executed.output
        program_result = executed.metadata["program"]
        assert program_result["status"] == "completed"
        assert "provider_dispatch" in json.dumps(program_result["result"])
        generation = program_result["kernel_generation"]

        status = await dispatch(
            call_id="ptc-provider-status",
            name="python_kernel_status",
            arguments={},
            expected_generation=generation,
        )
        assert status.is_error is False, status.output
        assert json.loads(status.output)["kernel_generation"] == generation

        reset = await dispatch(
            call_id="ptc-provider-reset",
            name="python_kernel_reset",
            arguments={"expected_generation": generation},
            expected_generation=generation,
        )
        assert reset.is_error is False, reset.output
        assert json.loads(reset.output)["kernel_generation"] == generation + 1
    finally:
        await provider.cleanup()
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_factory_retains_program_state_across_turn_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path / "data"))
    base_ledger_path = tmp_path / "program-state.json"
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(base_ledger_path))
    _, engine, session_factory, _ = await bootstrap_runtime(load_config(), create_password_hasher())

    configured_executor_id = f"ptc-runtime-factory-{uuid4().hex}"
    from cognis.executor.program_runtime import executor_state_ledger_path

    provider = InProcessExecutorProvider(session_factory=session_factory)

    class _WebSocketProvider:
        async def list_active(self) -> list[object]:
            return []

        async def notify_browser_session_terminal(
            self, _executor_ids: list[str], _owner: dict[str, str]
        ) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    composite = CompositeExecutorProvider(
        in_process=provider,
        websocket=_WebSocketProvider(),  # type: ignore[arg-type]
    )

    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True)

    async def _executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": configured_executor_id,
            "executor_type": "in_process",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {"program_runtime": {"host_enabled": True}},
            "executor_owner_email": "ptc-runtime@example.test",
            "owner_email": "ptc-runtime@example.test",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    async def _mcp_servers(*_: object, **__: object) -> list[object]:
        return []

    async def _secrets(*_: object) -> dict[str, str]:
        return {}

    async def _unused_static_tool(_arguments: dict[str, Any], _context: Any) -> object:
        raise AssertionError("Nested tool calls must use the current RPC callback.")

    shared_registry = ToolRegistry()
    shared_registry.register(
        RegisteredTool(
            definition=GET_CURRENT_DATETIME_TOOL,
            handler=_unused_static_tool,
        )
    )

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(runtime_support, "_resolve_eligible_executor_config", _executor_config)
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)
    monkeypatch.setattr(runtime_support, "_resolve_executor_mcp_servers", _mcp_servers)

    factory = runtime_support.build_step_runtime_factory(
        providers=SimpleNamespace(
            _session_factory=session_factory,
            executor=composite,
            secrets=SimpleNamespace(resolve_for_execution=_secrets),
        ),
        shared_registry=shared_registry,
        shared_connection=None,
        session_factory=session_factory,
    )
    user_email = "ptc-runtime@example.test"
    conversation_a = f"conversation-{uuid4().hex}"
    conversation_b = f"conversation-{uuid4().hex}"
    session = SessionModel(
        session_id=f"session-{conversation_a}",
        conversation_id=conversation_a,
        user_email=user_email,
        agent_id="ptc-runtime-agent",
    )
    agent = AgentDefinition(
        agent_id=session.agent_id,
        owner_email=user_email,
        name="PTC runtime factory test",
        execution={"executor_id": configured_executor_id},
        tools={
            "builtin_tools": [
                *PROGRAM_RUNTIME_TOOL_NAMES,
                "get_current_datetime",
            ],
            "tool_groups": [],
        },
        permissions=AgentPermissions(
            tool_permissions={
                **{tool_name: Permission.EVALUATE for tool_name in PROGRAM_RUNTIME_TOOL_NAMES},
                "get_current_datetime": Permission.EVALUATE,
            }
        ),
    )
    router = ToolRouter(guardrails=_ApprovingGuardrails())

    async def _dispatch(
        runtime: object,
        *,
        scope_id: str,
        name: str,
        arguments: dict[str, object],
        expected_generation: int | None = None,
        active_session: SessionModel | None = None,
        rpc_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ):
        connection = runtime.executor_connection  # type: ignore[attr-defined]
        registry = runtime.tool_registry  # type: ignore[attr-defined]
        call_session = active_session or session
        plan = build_effective_program_plan(registry)
        call_id = f"ptc-factory-{uuid4().hex}"
        binding = ProgramRuntimeBinding(
            program_id=call_id,
            execution_id=f"execution-{uuid4().hex}",
            scope_id=scope_id,
            expected_generation=expected_generation,
            description="Exercise the provider-owned PTC runtime.",
            tool_manifest_digest=plan.plan_hash,
            tools=plan.tools,
            executor_id=connection.executor_id,
            executor_instance_id=connection.executor_instance_id,
        )

        if rpc_handler is None:

            async def _unexpected_rpc_call(_params: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("No nested tools are expected in this dispatch.")

            rpc_handler = _unexpected_rpc_call
        connection.register_program_tool_call_handler(binding, rpc_handler)
        try:
            return await router.execute(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    execution_scope_id=call_session.session_id,
                    runtime_metadata={
                        "program_runtime": binding.model_dump(mode="json"),
                        "turn_id": f"turn-{uuid4().hex}",
                        "tool_call_id": call_id,
                        "runtime_access": {
                            "conversation_id": scope_id,
                            "session_id": call_session.session_id,
                            "agent_id": agent.agent_id,
                            "user_email": user_email,
                        },
                    },
                ),
                call_session,
                agent,
                registry,
                connection,
                program_policy=_allow_program,
            )
        finally:
            connection.unregister_program_tool_call_handler(binding)

    try:
        first = await factory(
            agent=agent,
            user_email=user_email,
            conversation_id=conversation_a,
        )
        assert first.executor_connection.executor_id.startswith(f"{configured_executor_id}:run:")
        assert first.tool_registry.get("python_execute") is not None
        first_rpc_calls: list[dict[str, Any]] = []

        async def _first_turn_rpc(params: dict[str, Any]) -> dict[str, Any]:
            first_rpc_calls.append(params)
            raise AssertionError("The first turn's RPC callback must not handle the second turn.")

        first_cell = await _dispatch(
            first,
            scope_id=conversation_a,
            name="python_execute",
            arguments={
                "description": "Initialize a value in the conversation kernel.",
                "code": 'sentinel = 41\nawait cognis.emit({"sentinel": sentinel})',
            },
            rpc_handler=_first_turn_rpc,
        )
        assert first_cell.is_error is False, first_cell.output
        first_program = first_cell.metadata["program"]
        generation = first_program["kernel_generation"]
        assert first_program["status"] == "completed"
        assert first_rpc_calls == []
        per_executor_ledger = executor_state_ledger_path(configured_executor_id)
        assert per_executor_ledger.exists()
        assert not base_ledger_path.exists()
        first_connection = first.executor_connection
        await first.cleanup()
        assert provider.has_program_runtime(configured_executor_id)

        second = await factory(
            agent=agent,
            user_email=user_email,
            conversation_id=conversation_a,
        )
        assert second.executor_connection is not first_connection
        assert second.executor_connection.executor_id != first_connection.executor_id
        second_session = session.model_copy(update={"session_id": f"session-{uuid4().hex}"})
        second_context_id = f"turn-context-{uuid4().hex}"
        second_rpc_calls: list[dict[str, Any]] = []

        async def _second_turn_rpc(params: dict[str, Any]) -> dict[str, Any]:
            second_rpc_calls.append(params)
            if params.get("scope_id") != conversation_a or params.get("arguments") != {
                "timezone": "UTC"
            }:
                raise AssertionError(
                    "Nested RPC did not use the second turn's scope and tool arguments."
                )
            data = {
                "context_id": second_context_id,
                "session_id": second_session.session_id,
                "executor_handle_id": second.executor_connection.executor_id,
            }
            return {
                "data": data,
                "text": json.dumps(data),
                "format": "structured",
                "call_id": f"nested-{uuid4().hex}",
            }

        datetime_alias = next(
            tool.alias
            for tool in build_effective_program_plan(second.tool_registry).tools
            if tool.name == "get_current_datetime"
        )
        second_cell = await _dispatch(
            second,
            scope_id=conversation_a,
            name="python_execute",
            arguments={
                "description": "Continue the same conversation kernel.",
                "code": (
                    f"nested = await cognis.tools.{datetime_alias}(timezone='UTC')\n"
                    "sentinel += 1\n"
                    "await cognis.emit({'sentinel': sentinel, 'nested': nested.data})"
                ),
            },
            expected_generation=generation,
            active_session=second_session,
            rpc_handler=_second_turn_rpc,
        )
        assert second_cell.is_error is False, second_cell.output
        second_program = second_cell.metadata["program"]
        assert second_program["kernel_generation"] == generation
        assert second_program["kernel_reused"] is True
        assert second_program["result"] == {
            "sentinel": 42,
            "nested": {
                "context_id": second_context_id,
                "session_id": second_session.session_id,
                "executor_handle_id": second.executor_connection.executor_id,
            },
        }
        assert first_rpc_calls == []
        assert len(second_rpc_calls) == 1
        assert second_rpc_calls[0]["program_id"] == second_program["program_id"]
        await second.cleanup()

        isolated_session = session.model_copy(
            update={
                "session_id": f"session-{conversation_b}",
                "conversation_id": conversation_b,
            }
        )
        third = await factory(
            agent=agent,
            user_email=user_email,
            conversation_id=conversation_b,
        )
        isolated_status = await _dispatch(
            third,
            scope_id=conversation_b,
            name="python_execute",
            arguments={
                "description": "Verify conversation-specific Python isolation.",
                "code": "await cognis.emit('sentinel' in globals())",
            },
            active_session=isolated_session,
        )
        assert isolated_status.is_error is False, isolated_status.output
        assert isolated_status.metadata["program"]["result"] is False
        await third.cleanup()

        # The turn connection is gone; terminal scope cleanup must still reach
        # the runtime retained by the configured executor provider.
        await composite.terminate_program_scope(
            conversation_a,
            executor_ids=[configured_executor_id],
        )
        assert provider.has_program_runtime(configured_executor_id)
        terminal_check = await factory(
            agent=agent,
            user_email=user_email,
            conversation_id=conversation_a,
        )
        terminal_status = await _dispatch(
            terminal_check,
            scope_id=conversation_a,
            name="python_kernel_status",
            arguments={},
        )
        assert terminal_status.is_error is False, terminal_status.output
        assert terminal_status.metadata["state"] == "absent"
        assert terminal_status.metadata["state_lost_reason"] == "conversation_terminal"
        await terminal_check.cleanup()
        await composite.cleanup()
        assert not provider.has_program_runtime(configured_executor_id)
    finally:
        await composite.cleanup()
        await engine.dispose()
