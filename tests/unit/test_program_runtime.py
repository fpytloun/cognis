from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.program_plan import build_effective_program_plan
from cognis.executor.program_runtime import ProgramRuntime, executor_state_ledger_path
from cognis.executor.tool_definitions_runtime import handle_python_execute
from cognis.json_schema import offline_draft202012_validator
from cognis.models.program import (
    ProgramError,
    ProgramExecutionRequest,
    ProgramLimits,
    ProgramResult,
    ProgramRuntimeBinding,
    ProgramToolValue,
)
from cognis.tools.builtin.memory import MEMORY_FIND_TOOL, MEMORY_SEARCH_TOOL
from cognis.tools.executor.definitions import (
    PYTHON_EXECUTE_ACTIVE_TIMEOUT_SECONDS,
    PYTHON_EXECUTE_TOOL,
    WEB_SEARCH_TOOL,
)
from cognis.tools.registry import RegisteredTool, ToolRegistry


def _scope() -> str:
    return uuid.uuid4().hex


async def _unexpected_tool_call(_request: Any) -> ProgramToolValue:
    raise AssertionError("The program should not dispatch a nested tool call.")


@pytest.mark.asyncio
async def test_python_streams_flushed_stdout_and_stderr_before_cell_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real IOPub delivery and the ordinary tool chunk callback."""
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "stream-state.json"))
    runtime = ProgramRuntime(executor_id=f"stream-{uuid.uuid4().hex}")
    scope_id = _scope()
    released = tmp_path / "release-cell"
    chunks: list[tuple[str, str]] = []
    first_output = asyncio.Event()

    async def emit_output(text: str, stream: str) -> None:
        chunks.append((text, stream))
        if "initial-stdout" in text:
            first_output.set()

    request = _request(
        scope_id,
        generation=None,
        inputs={"release": str(released)},
        code=(
            "import asyncio, sys\n"
            "from pathlib import Path\n"
            "print('initial-stdout', flush=True)\n"
            "while not Path(inputs['release']).exists():\n"
            "    await asyncio.sleep(0.01)\n"
            "print('middle-stderr', file=sys.stderr, flush=True)\n"
            "await asyncio.sleep(0.05)\n"
            "print('terminal-stdout', flush=True)\n"
            "await cognis.emit({'done': True})"
        ),
    )
    binding = ProgramRuntimeBinding(
        program_id=request.program_id,
        execution_id=request.execution_id,
        scope_id=scope_id,
        description=request.description,
        tool_manifest_digest=hashlib.sha256(b"[]").hexdigest(),
        executor_id=runtime.executor_id,
        executor_instance_id=uuid.uuid4().hex,
    )
    context = SimpleNamespace(
        runtime_metadata={
            "program_runtime": binding.model_dump(mode="json"),
            "_program_runtime": runtime,
            "_program_rpc_call": _unexpected_tool_call,
        },
        output_chunk_callback=emit_output,
    )
    running = asyncio.create_task(
        handle_python_execute(
            {"description": request.description, "code": request.code, "inputs": request.inputs},
            context,
        )
    )
    waiting = asyncio.create_task(first_output.wait())
    try:
        done, _pending = await asyncio.wait(
            {waiting, running}, timeout=15, return_when=asyncio.FIRST_COMPLETED
        )
        assert waiting in done, running.result().output if running.done() else "No live output"
        assert not running.done()
        assert not released.exists()
        assert chunks == [("initial-stdout\n", "stdout")]
        released.touch()
        result = await asyncio.wait_for(running, timeout=15)
        assert not result.is_error
        assert chunks == [
            ("initial-stdout\n", "stdout"),
            ("middle-stderr\n", "stderr"),
            ("terminal-stdout\n", "stdout"),
        ]
        terminal = json.loads(result.output)
        assert terminal["stdout_preview"] == "".join(text for text, _stream in chunks)
        assert terminal["result"] == {"done": True}
    finally:
        waiting.cancel()
        running.cancel()
        await asyncio.gather(waiting, running, return_exceptions=True)
        await runtime.close()


def _request(
    scope_id: str,
    *,
    generation: int | None,
    code: str,
    inputs: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
    wall_seconds: int = 20,
    output_bytes: int | None = None,
    input_schema: dict[str, Any] | None = None,
    mode: str = "interactive",
    program_digest: str | None = None,
    code_digest: str | None = None,
    tool_manifest_digest: str | None = None,
) -> ProgramExecutionRequest:
    return ProgramExecutionRequest(
        program_id=uuid.uuid4().hex,
        execution_id=uuid.uuid4().hex,
        scope_id=scope_id,
        expected_generation=generation,
        description="Verify supervised program execution.",
        code=code,
        inputs=inputs or {},
        input_schema=input_schema,
        tools=tools or [],
        output_schema=output_schema,
        mode=mode,
        program_digest=program_digest,
        code_digest=code_digest,
        tool_manifest_digest=tool_manifest_digest,
        limits=ProgramLimits(wall_seconds=wall_seconds, output_bytes=output_bytes),
    )


def _ledger_record(ledger_path: Path, executor_id: str, scope_id: str) -> dict[str, Any]:
    scope_key = hashlib.sha256(f"{executor_id}\0{scope_id}".encode()).hexdigest()
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    return payload["scopes"][scope_key]


def _program_tool_descriptor() -> dict[str, Any]:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=WEB_SEARCH_TOOL))
    plan = build_effective_program_plan(registry)
    assert len(plan.tools) == 1
    return plan.tools[0].model_dump(mode="json")


@pytest.mark.asyncio
async def test_executor_scoped_ledgers_isolate_interleaved_restart_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_path = tmp_path / "ptc-state.json"
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(base_path))
    executor_a = f"configured-{uuid.uuid4().hex}"
    executor_b = f"configured-{uuid.uuid4().hex}"
    scope_a_active = f"conversation-{uuid.uuid4().hex}"
    scope_a_terminal = f"conversation-{uuid.uuid4().hex}"
    scope_b_active = f"conversation-{uuid.uuid4().hex}"
    scope_b_terminal = f"conversation-{uuid.uuid4().hex}"
    ledger_a = executor_state_ledger_path(executor_a)
    ledger_b = executor_state_ledger_path(executor_b)

    assert ledger_a != ledger_b
    assert ledger_a.parent == ledger_b.parent == tmp_path
    assert executor_a not in ledger_a.name
    assert executor_b not in ledger_b.name

    runtime_a = ProgramRuntime(executor_id=executor_a, ledger_path=ledger_a)
    restarted_a: ProgramRuntime | None = None
    runtime_b: ProgramRuntime | None = None
    try:
        runtime_a._persist_scope(scope_a_active, 17, active=True, reason=None)
        a_after_first_write = ledger_a.read_bytes()

        runtime_b = ProgramRuntime(executor_id=executor_b, ledger_path=ledger_b)
        assert ledger_a.read_bytes() == a_after_first_write
        assert _ledger_record(ledger_a, executor_a, scope_a_active) == {
            "generation": 17,
            "active": True,
            "state_lost_reason": None,
        }

        runtime_b._persist_scope(scope_b_active, 29, active=True, reason=None)
        await runtime_b.terminate_scope(scope_b_terminal, reason="conversation_terminal")

        runtime_a._persist_scope(scope_a_active, 18, active=True, reason=None)
        b_before_a_termination = ledger_b.read_bytes()
        b_active_before_a_termination = _ledger_record(ledger_b, executor_b, scope_b_active)
        b_terminal_before_a_termination = _ledger_record(ledger_b, executor_b, scope_b_terminal)
        await runtime_a.terminate_scope(scope_a_terminal, reason="conversation_terminal")
        assert ledger_b.read_bytes() == b_before_a_termination
        assert _ledger_record(ledger_b, executor_b, scope_b_active) == b_active_before_a_termination
        assert (
            _ledger_record(ledger_b, executor_b, scope_b_terminal)
            == b_terminal_before_a_termination
        )
        b_before_a_restart = ledger_b.read_bytes()
        b_active_before_restart = _ledger_record(ledger_b, executor_b, scope_b_active)
        b_terminal_before_restart = _ledger_record(ledger_b, executor_b, scope_b_terminal)
        a_terminal_before_restart = _ledger_record(ledger_a, executor_a, scope_a_terminal)

        await runtime_a.close()
        restarted_a = ProgramRuntime(executor_id=executor_a, ledger_path=ledger_a)

        assert ledger_b.read_bytes() == b_before_a_restart
        assert _ledger_record(ledger_b, executor_b, scope_b_active) == b_active_before_restart
        assert _ledger_record(ledger_b, executor_b, scope_b_terminal) == b_terminal_before_restart
        assert b_active_before_restart == {
            "generation": 29,
            "active": True,
            "state_lost_reason": None,
        }
        assert b_terminal_before_restart["active"] is False
        assert b_terminal_before_restart["state_lost_reason"] == "conversation_terminal"
        assert _ledger_record(ledger_a, executor_a, scope_a_active) == {
            "generation": 19,
            "active": False,
            "state_lost_reason": "executor_restart",
        }
        assert _ledger_record(ledger_a, executor_a, scope_a_terminal) == a_terminal_before_restart
        assert ledger_a.stat().st_mode & 0o777 == 0o600
        assert ledger_b.stat().st_mode & 0o777 == 0o600
    finally:
        await runtime_a.close()
        if runtime_b is not None:
            await runtime_b.close()
        if restarted_a is not None:
            await restarted_a.close()


def test_runtime_guide_adapter_passes_the_effective_plan_to_canonical_renderer() -> None:
    request = _request(_scope(), generation=None, code="await cognis.emit(None)")
    request.executor_policy = {"read_only_required": True}
    eligible_tools = [_program_tool_descriptor()]
    limits = {"max_calls": 4, "concurrency": 2}

    guide = ProgramRuntime._render_runtime_guide(
        request=request,
        generation=7,
        eligible_tools=eligible_tools,
        limits=limits,
    )
    contract_prefix = "Effective runtime contract:\n"
    assert "not a sandbox" in guide
    assert contract_prefix in guide
    rendered = json.loads(guide.split(contract_prefix, maxsplit=1)[1])

    assert rendered == {
        "discovery": {
            "enabled": True,
            "methods": [
                "cognis.tools.search",
                "cognis.tools.describe",
                "cognis.tools.load",
            ],
            "scope": "effective eligible tool plan",
        },
        "eligible_tools": eligible_tools,
        "exception_types": ["ToolExecutionError"],
        "executor_policy": {"read_only_required": True},
        "generation": "7",
        "imports": [],
        "limits": limits,
    }
    assert "await cognis.emit(json_value)" in guide


def test_program_tool_timeout_is_only_the_runtime_active_budget() -> None:
    assert "await cognis.emit(json_value)" in PYTHON_EXECUTE_TOOL.description
    assert PYTHON_EXECUTE_TOOL.timeout_seconds == PYTHON_EXECUTE_ACTIVE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_kernel_persists_state_and_preserves_falsy_structured_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    descriptor = dict(_program_tool_descriptor())
    descriptor["read_only"] = False
    calls: list[dict[str, Any]] = []

    async def tool_call(request: Any) -> ProgramToolValue:
        calls.append(request.model_dump(mode="json"))
        return ProgramToolValue(
            data=False,
            text="false",
            format="structured",
            call_id=uuid.uuid4().hex,
        )

    first = await runtime.execute(
        _request(
            scope_id,
            generation=None,
            inputs={"initial": False},
            tools=[descriptor],
            output_schema={
                "$defs": {
                    "emitted": {
                        "type": "object",
                        "properties": {
                            "counter": {"type": "integer"},
                            "value": {"type": "boolean"},
                            "truthy": {"type": "boolean"},
                        },
                        "required": ["counter", "value", "truthy"],
                    }
                },
                "$ref": "#/$defs/emitted",
            },
            code=(
                "counter = 0\n"
                f"value = await cognis.tools.{descriptor['alias']}("
                "query='read-only check')\n"
                "await cognis.emit({'counter': counter, 'value': value.data, "
                "'truthy': bool(value)})"
            ),
        ),
        tool_call_handler=tool_call,
    )
    assert first.status == "completed", (
        first.error.bounded_message if first.error is not None else first.model_dump(),
        first.stdout_preview,
        descriptor,
    )
    assert first.result == {"counter": 0, "value": False, "truthy": True}
    assert first.nested_admitted == first.nested_completed == 1
    assert len(calls) == 1

    second = await runtime.execute(
        _request(
            scope_id,
            generation=first.kernel_generation,
            code="counter += 4\nawait cognis.emit(counter)",
            output_schema={"type": "integer"},
        ),
        tool_call_handler=tool_call,
    )
    assert second.status == "completed"
    assert second.kernel_reused is True
    assert second.result == 4
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_key", ["tool_id", "alias", "name"])
async def test_documented_discovery_helpers_use_current_execution_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector_key: str
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MEMORY_FIND_TOOL))
    registry.register(RegisteredTool(definition=MEMORY_SEARCH_TOOL))
    descriptors = [
        tool.model_dump(mode="json") for tool in build_effective_program_plan(registry).tools
    ]
    descriptor = next(tool for tool in descriptors if tool["name"] == "memory_search")
    selector = json.dumps(descriptor[selector_key])
    calls: list[dict[str, Any]] = []

    async def tool_call(request: Any) -> ProgramToolValue:
        calls.append(request.model_dump(mode="json"))
        return ProgramToolValue(data=False, format="structured", call_id=uuid.uuid4().hex)

    request = _request(
        scope_id,
        generation=None,
        tools=descriptors,
        code=(
            f"matches = await cognis.tools.search({selector}, limit=1)\n"
            f"described = await cognis.tools.describe({selector})\n"
            "assert matches[0]['tool_id'] == described['tool_id']\n"
            "search = cognis.tools.load(described)\n"
            "value = await search(query='documented discovery regression')\n"
            "await cognis.emit(value.data)"
        ),
    )
    try:
        first = await runtime.execute(request, tool_call_handler=tool_call)
        assert first.status == "completed", first.error
        assert first.result is False
        assert first.nested_admitted == first.nested_completed == 1
        assert len(calls) == 1
        assert calls[0]["tool_id"] == descriptor["tool_id"]
        assert calls[0]["schema_hash"] == descriptor["schema_hash"]
        assert calls[0]["execution_id"] == request.execution_id
        assert calls[0]["program_id"] == request.program_id

        continuation = _request(
            scope_id,
            generation=first.kernel_generation,
            tools=[descriptor],
            code=(
                "value = await search(query='current execution linkage regression')\n"
                "await cognis.emit(value.data)"
            ),
        )
        second = await runtime.execute(continuation, tool_call_handler=tool_call)
        assert second.status == "completed", second.error
        assert second.kernel_reused is True
        assert second.result is False
        assert len(calls) == 2
        assert calls[1]["execution_id"] == continuation.execution_id
        assert calls[1]["program_id"] == continuation.program_id
        assert calls[1]["execution_id"] != calls[0]["execution_id"]

        third = await runtime.execute(
            _request(
                scope_id,
                generation=second.kernel_generation,
                tools=[],
                code=(
                    "try:\n"
                    f"    await cognis.tools.describe({selector})\n"
                    "except KeyError:\n"
                    "    pass\n"
                    "else:\n"
                    "    raise AssertionError('Unadmitted tool remained available')\n"
                    "try:\n"
                    f"    cognis.tools.load({json.dumps(descriptor['tool_id'])})\n"
                    "except KeyError:\n"
                    "    pass\n"
                    "else:\n"
                    "    raise AssertionError('Unadmitted tool remained loadable')\n"
                    "await cognis.emit(await cognis.tools.search('memory_search'))"
                ),
            ),
            tool_call_handler=_unexpected_tool_call,
        )
        assert third.status == "completed", third.error
        assert third.kernel_reused is True
        assert third.result == []
        assert third.nested_admitted == 0
        assert len(calls) == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unknown_nested_outcome_cannot_be_caught_and_emitted_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    descriptor = _program_tool_descriptor()
    calls: list[dict[str, Any]] = []

    async def tool_call(request: Any) -> ProgramToolValue:
        calls.append(request)
        return ProgramToolValue(
            data={"accepted": None},
            text="The operation outcome is unknown.",
            format="json",
            is_error=True,
            outcome_certainty="unknown",
        )

    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            tools=[descriptor],
            output_schema={"type": "object"},
            code=(
                "try:\n"
                f"    await cognis.tools.{descriptor['alias']}(query='uncertain mutation')\n"
                "except ToolExecutionError:\n"
                "    await cognis.emit({'caught': True})\n"
                "    print('caught-and-emitted')\n"
                "else:\n"
                "    await cognis.emit({'caught': False})"
            ),
        ),
        tool_call_handler=tool_call,
    )

    assert result.status == "uncertain"
    assert result.result is None
    assert result.error is not None
    assert result.error.code == "nested_outcome_uncertain"
    assert result.error.outcome_certainty == "unknown"
    assert "caught-and-emitted" in result.stdout_preview
    assert result.nested_admitted == 1
    assert result.nested_failed == 1
    assert len(calls) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_nested_outcome_survives_caught_error_and_output_budget_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    descriptor = _program_tool_descriptor()
    calls: list[Any] = []

    async def tool_call(request: Any) -> ProgramToolValue:
        calls.append(request)
        return ProgramToolValue(
            data={"accepted": None},
            text="The operation outcome is unknown.",
            format="structured",
            call_id=uuid.uuid4().hex,
            is_error=True,
            outcome_certainty="unknown",
        )

    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            tools=[descriptor],
            output_schema={"type": "object"},
            output_bytes=64,
            code=(
                "try:\n"
                f"    await cognis.tools.{descriptor['alias']}(query='uncertain mutation')\n"
                "except ToolExecutionError:\n"
                "    try:\n"
                f"        await cognis.tools.{descriptor['alias']}(query='must not dispatch')\n"
                "    except ToolExecutionError:\n"
                "        pass\n"
                "    print('x' * 200)\n"
                "else:\n"
                "    print('unexpected tool success')"
            ),
        ),
        tool_call_handler=tool_call,
    )

    assert result.status == "budget_exceeded"
    assert result.result is None
    assert result.error is not None
    assert result.error.code == "budget_exceeded"
    assert result.error.outcome_certainty == "unknown"
    assert "x" * 200 in result.stdout_preview
    assert result.output_bytes > 64
    assert result.nested_admitted == 1
    assert result.nested_failed == 1
    assert len(calls) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_program_runtime_successful_emit_remains_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")

    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            output_schema={"type": "object"},
            code="await cognis.emit({'completed': True})",
        ),
        tool_call_handler=_unexpected_tool_call,
    )

    assert result.status == "completed"
    assert result.error is None
    assert result.result == {"completed": True}
    assert result.state_lost_reason is None
    await runtime.close()


@pytest.mark.asyncio
async def test_program_runtime_budget_error_without_child_effect_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")

    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            output_bytes=64,
            code="print('x' * 200)",
        ),
        tool_call_handler=_unexpected_tool_call,
    )

    assert result.status == "budget_exceeded"
    assert result.error is not None
    assert result.error.code == "budget_exceeded"
    assert result.error.outcome_certainty == "known"
    assert result.output_bytes > 64
    await runtime.close()


@pytest.mark.asyncio
async def test_mutating_child_calls_are_serialized_and_schema_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    descriptor = dict(_program_tool_descriptor())
    descriptor["read_only"] = False
    calls: list[Any] = []
    active = 0
    maximum_active = 0

    async def tool_call(request: Any) -> ProgramToolValue:
        nonlocal active, maximum_active
        calls.append(request)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return ProgramToolValue(data=True, text="true", format="structured")

    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            tools=[descriptor],
            output_schema={"type": "array"},
            code=(
                f"first, second = await _ptc_asyncio.gather("
                f"cognis.tools.{descriptor['alias']}(query='first write'), "
                f"cognis.tools.{descriptor['alias']}(query='second write'))\n"
                "await cognis.emit([first.data, second.data])"
            ),
        ),
        tool_call_handler=tool_call,
    )

    assert result.status == "completed", result.error
    assert result.result == [True, True]
    assert maximum_active == 1
    assert [call.sequence for call in calls] == [1, 2]

    calls.clear()
    invalid = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            tools=[descriptor],
            code=f"await cognis.tools.{descriptor['alias']}(query=42)",
        ),
        tool_call_handler=tool_call,
    )
    assert invalid.status == "failed"
    assert invalid.error is not None
    assert "schema" in invalid.error.bounded_message
    assert calls == []
    await runtime.close()


@pytest.mark.asyncio
async def test_python_execute_adapter_preserves_unknown_output_validation_certainty() -> None:
    program_id = uuid.uuid4().hex
    result = ProgramResult(
        program_id=program_id,
        kernel_generation=1,
        status="failed",
        error=ProgramError(
            code="output_schema_invalid",
            bounded_message="The emitted result did not match the output schema.",
            outcome_certainty="unknown",
        ),
    )

    class RuntimeStub:
        async def execute(self, request: ProgramExecutionRequest, **_kwargs: Any) -> ProgramResult:
            assert request.program_id == program_id
            assert request.output_schema == {"type": "object"}
            return result

    async def rpc_call(_request: dict[str, Any]) -> ProgramToolValue:
        raise AssertionError("This program does not dispatch nested calls.")

    program_context = ProgramRuntimeBinding(
        program_id=program_id,
        execution_id=uuid.uuid4().hex,
        scope_id=f"conversation-{uuid.uuid4().hex}",
        expected_generation=1,
        description="Verify explicit unknown certainty reaches the outer tool result.",
        tool_manifest_digest="manifest",
        output_schema={"type": "object"},
        executor_id=uuid.uuid4().hex,
        executor_instance_id=uuid.uuid4().hex,
    )
    context = SimpleNamespace(
        runtime_metadata={
            "program_runtime": program_context.model_dump(mode="json"),
            "_program_runtime": RuntimeStub(),
            "_program_rpc_call": rpc_call,
        },
        output_chunk_callback=None,
    )

    tool_result = await handle_python_execute(
        {"description": program_context.description, "code": "await cognis.emit({})"},
        context,
    )

    assert tool_result.is_error is True
    assert tool_result.metadata is not None
    assert tool_result.metadata["outcome_certainty"] == "unknown"
    assert tool_result.metadata["program"]["error"]["outcome_certainty"] == "unknown"


@pytest.mark.asyncio
async def test_kernel_generation_reset_and_process_restart_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    executor_id = f"executor-{uuid.uuid4().hex}"
    scope_id = _scope()
    first_runtime = ProgramRuntime(executor_id=executor_id)
    result = await first_runtime.execute(
        _request(
            scope_id,
            generation=None,
            code="counter = 7\nawait cognis.emit(counter)",
            output_schema={"type": "integer"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert result.status == "completed"

    restarted_runtime = ProgramRuntime(executor_id=executor_id)
    status = await restarted_runtime.status(scope_id)
    assert status["state"] == "absent"
    assert status["kernel_generation"] == result.kernel_generation + 1
    assert status["state_lost_reason"] == "executor_restart"

    restarted = await restarted_runtime.execute(
        _request(
            scope_id,
            generation=status["kernel_generation"],
            code="await cognis.emit('counter' in globals())",
            output_schema={"type": "boolean"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert restarted.status == "completed"
    assert restarted.state_lost_reason == "executor_restart"
    assert restarted.result is False
    await first_runtime.close()
    await restarted_runtime.close()


@pytest.mark.asyncio
async def test_terminal_scope_cleanup_discards_state_and_rejects_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    started = await runtime.execute(
        _request(
            scope_id,
            generation=None,
            code="counter = 9\nawait cognis.emit(counter)",
            output_schema={"type": "integer"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert started.status == "completed"

    terminal = await runtime.terminate_scope(scope_id, reason="conversation_terminal")
    assert terminal == {
        "closed": 1,
        "kernel_generation": started.kernel_generation + 1,
        "state_lost_reason": "conversation_terminal",
    }
    stale = await runtime.execute(
        _request(
            scope_id,
            generation=started.kernel_generation,
            code="await cognis.emit(counter)",
            output_schema={"type": "integer"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert stale.status == "failed"
    assert stale.error is not None
    assert stale.error.code == "scope_terminal"
    assert stale.kernel_generation == terminal["kernel_generation"]
    absent_scope = _scope()
    absent_generation = (await runtime.status(absent_scope))["kernel_generation"]
    assert await runtime.terminate_scope(absent_scope, reason="conversation_terminal") == {
        "closed": 0,
        "kernel_generation": absent_generation + 1,
        "state_lost_reason": "conversation_terminal",
    }
    absent_status = await runtime.status(absent_scope)
    assert absent_status["state"] == "absent"
    assert absent_status["kernel_generation"] == absent_generation + 1
    assert absent_status["state_lost_reason"] == "conversation_terminal"
    await runtime.close()


@pytest.mark.asyncio
async def test_timeout_discards_kernel_and_advances_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    initial_generation = (await runtime.status(scope_id))["kernel_generation"]
    result = await runtime.execute(
        _request(
            scope_id,
            generation=None,
            code="while True:\n    pass",
            wall_seconds=1,
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert result.status == "timed_out"
    assert result.kernel_generation == initial_generation + 1
    assert result.state_lost_reason == "timeout"
    status = await runtime.status(scope_id)
    assert status["state"] == "absent"
    assert status["kernel_generation"] == result.kernel_generation
    await runtime.close()


@pytest.mark.asyncio
async def test_ordinary_approval_pause_does_not_spend_program_wall_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    descriptor = _program_tool_descriptor()
    request = _request(
        scope_id,
        generation=None,
        tools=[descriptor],
        output_schema={"type": "boolean"},
        wall_seconds=1,
        code=(
            f"value = await cognis.tools.{descriptor['alias']}(query='approval-gated read')\n"
            "await cognis.emit(value.data)"
        ),
    )
    statuses: list[str] = []

    async def tool_call(child_call: Any) -> ProgramToolValue:
        binding = {
            "program_id": child_call.program_id,
            "execution_id": child_call.execution_id,
            "kernel_generation": child_call.kernel_generation,
            "scope_id": scope_id,
            "sequence": child_call.sequence,
        }
        assert runtime.update_status({**binding, "status": "awaiting_approval"})
        statuses.append("awaiting_approval")
        await asyncio.sleep(1.2)
        assert runtime.update_status({**binding, "status": "running"})
        statuses.append("running")
        return ProgramToolValue(data=False, format="structured", call_id=uuid.uuid4().hex)

    result = await runtime.execute(request, tool_call_handler=tool_call)

    assert result.status == "completed", (result.error, statuses)
    assert result.result is False
    assert result.wall_ms >= 1000
    await runtime.close()


@pytest.mark.asyncio
async def test_unawaited_emit_is_not_a_workflow_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    code = "cognis.emit({'status': 'not awaited'})"
    tool_manifest = hashlib.sha256(
        json.dumps([], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            code=code,
            output_schema={"type": "object"},
            mode="workflow",
            program_digest=hashlib.sha256(b"pinned-workflow-program").hexdigest(),
            code_digest=code_digest,
            tool_manifest_digest=tool_manifest,
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "missing_emit"
    await runtime.close()


def test_json_schema_validator_resolves_local_refs_without_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import urllib.request

    def fail_external_io(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("JSON Schema validation attempted external I/O")

    monkeypatch.setattr(urllib.request, "urlopen", fail_external_io)
    monkeypatch.setattr(socket, "create_connection", fail_external_io)

    local = offline_draft202012_validator(
        {
            "$defs": {"count": {"type": "integer"}},
            "$ref": "#/$defs/count",
        }
    )
    assert list(local.iter_errors(7)) == []
    assert list(local.iter_errors("7"))

    external = offline_draft202012_validator({"$ref": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(Exception, match="Unresolvable"):
        list(external.iter_errors({"result": 7}))


def test_executor_program_runtime_runs_with_only_common_and_executor_import_roots(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    common_src = repo_root / "packages" / "common" / "src"
    executor_src = repo_root / "packages" / "executor" / "src"
    script = r"""
import asyncio
import sys
import uuid
from importlib.abc import MetaPathFinder

class RejectController(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "cognis.core" or fullname.startswith("cognis.core."):
            raise AssertionError(f"controller-only import attempted: {fullname}")
        return None

sys.meta_path.insert(0, RejectController())
from cognis.executor.program_runtime import ProgramRuntime
from cognis.models.program import ProgramExecutionRequest, ProgramLimits

async def main():
    runtime = ProgramRuntime(executor_id=uuid.uuid4().hex)
    request = ProgramExecutionRequest(
        program_id=uuid.uuid4().hex,
        execution_id=uuid.uuid4().hex,
        scope_id=uuid.uuid4().hex,
        description="Run an isolated executor kernel.",
        code="value = 20 + 22\nawait cognis.emit(value)",
        output_schema={"type": "integer"},
        limits=ProgramLimits(wall_seconds=10),
    )
    result = await runtime.execute(request, tool_call_handler=lambda _call: None)
    assert result.status == "completed", result.model_dump()
    assert result.result == 42
    await runtime.close()

asyncio.run(main())
assert "cognis.core" not in sys.modules
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(common_src), str(executor_src)))
    env["COGNIS_PTC_STATE_PATH"] = str(tmp_path / "isolated-state.json")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.asyncio
async def test_managed_background_tasks_are_cancelled_at_cell_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    result = await runtime.execute(
        _request(
            _scope(),
            generation=None,
            code=(
                "import asyncio\n"
                "async def pending_work():\n"
                "    await asyncio.sleep(60)\n"
                "cognis.create_task(pending_work())\n"
                "await cognis.emit('finished')"
            ),
            output_schema={"type": "string"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "detached_tasks"
    await runtime.close()


@pytest.mark.asyncio
async def test_workflow_program_is_pinned_emits_once_and_discards_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    code = "await cognis.emit({'accepted': inputs['accepted']})"
    manifest_digest = hashlib.sha256(
        json.dumps([], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = await runtime.execute(
        _request(
            scope_id,
            generation=None,
            code=code,
            inputs={"accepted": True},
            input_schema={"type": "object", "required": ["accepted"]},
            output_schema={
                "type": "object",
                "required": ["accepted"],
                "properties": {"accepted": {"type": "boolean"}},
                "additionalProperties": False,
            },
            mode="workflow",
            program_digest=hashlib.sha256(code.encode()).hexdigest(),
            code_digest=hashlib.sha256(code.encode()).hexdigest(),
            tool_manifest_digest=manifest_digest,
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )

    assert result.status == "completed", result.error
    assert result.result == {"accepted": True}
    assert result.state_lost_reason == "workflow_terminal"
    status = await runtime.status(scope_id)
    assert status["state"] == "absent"
    assert status["kernel_generation"] == result.kernel_generation
    assert status["state_lost_reason"] == "workflow_terminal"
    await runtime.close()


@pytest.mark.asyncio
async def test_lost_state_ledger_does_not_reuse_an_old_kernel_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "state-ledger.json"
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(ledger_path))
    monkeypatch.setattr("cognis.executor.program_runtime.secrets.randbelow", lambda _upper: 77)
    executor_id = f"executor-{uuid.uuid4().hex}"
    scope_id = _scope()

    original_runtime = ProgramRuntime(executor_id=executor_id)
    old_generation = original_runtime._known_generation(scope_id)
    original_runtime._persist_scope(scope_id, old_generation, active=False, reason=None)
    ledger_path.unlink()

    recovered_runtime = ProgramRuntime(executor_id=executor_id)
    recovered_generation = (await recovered_runtime.status(scope_id))["kernel_generation"]
    assert recovered_generation == old_generation

    descriptor = _program_tool_descriptor()
    execution_marker = tmp_path / "program-code-executed"
    nested_calls: list[Any] = []
    code = (
        "from pathlib import Path\n"
        f"Path({str(execution_marker)!r}).write_text('executed')\n"
        f"tool_value = await cognis.tools.{descriptor['alias']}(query='post-reset')\n"
        "await cognis.emit(tool_value.data)"
    )

    async def tool_call(request: Any) -> ProgramToolValue:
        nested_calls.append(request)
        return ProgramToolValue(
            data="recovered",
            text="recovered",
            format="structured",
            call_id=uuid.uuid4().hex,
        )

    stale_request = _request(
        scope_id,
        generation=old_generation,
        code=code,
        tools=[descriptor],
        output_schema={"type": "string"},
    )
    for _ in range(2):
        stale = await recovered_runtime.execute(stale_request, tool_call_handler=tool_call)
        assert stale.status == "failed"
        assert stale.error is not None
        assert stale.error.code == "kernel_state_lost"
        assert stale.kernel_generation == recovered_generation
        assert not execution_marker.exists()
        assert nested_calls == []

    reset = await recovered_runtime.reset(scope_id, expected_generation=recovered_generation)
    assert reset["state"] == "reset"
    assert reset["kernel_generation"] == recovered_generation + 1

    resumed = await recovered_runtime.execute(
        _request(
            scope_id,
            generation=reset["kernel_generation"],
            code=code,
            tools=[descriptor],
            output_schema={"type": "string"},
        ),
        tool_call_handler=tool_call,
    )
    assert resumed.status == "completed", resumed.error
    assert resumed.result == "recovered"
    assert execution_marker.read_text(encoding="utf-8") == "executed"
    assert len(nested_calls) == 1

    await original_runtime.close()
    await recovered_runtime.close()


@pytest.mark.asyncio
async def test_input_schema_rejection_happens_before_kernel_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_PTC_STATE_PATH", str(tmp_path / "state-ledger.json"))
    runtime = ProgramRuntime(executor_id=f"executor-{uuid.uuid4().hex}")
    scope_id = _scope()
    result = await runtime.execute(
        _request(
            scope_id,
            generation=None,
            code="await cognis.emit(1)",
            inputs={"count": "one"},
            input_schema={
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer"}},
            },
            output_schema={"type": "integer"},
        ),
        tool_call_handler=lambda _request: _no_tool_call(),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "input_schema_invalid"
    assert (await runtime.status(scope_id))["state"] == "absent"
    await runtime.close()


async def _no_tool_call() -> ProgramToolValue:
    raise AssertionError("No tool calls are expected.")
