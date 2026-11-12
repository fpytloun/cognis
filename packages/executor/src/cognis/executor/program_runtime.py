"""Supervised persistent IPython kernels for controller-governed programs.

Python in this runtime is not a sandbox. The kernel inherits the executor
process's filesystem, credentials, and network reach. Cognis tool calls use a
private Unix socket solely to return through the controller's normal router.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jupyter_client.manager import AsyncKernelManager

from cognis.json_schema import offline_draft202012_validator
from cognis.models.program import (
    ProgramExecutionRequest,
    ProgramResult,
    ProgramToolCall,
    ProgramToolValue,
)

logger = logging.getLogger(__name__)

_DEFAULT_WALL_SECONDS = 120
_MAX_WALL_SECONDS = 300
_DEFAULT_CALLS = 50
_DEFAULT_CONCURRENCY = 4
_INLINE_PREVIEW_BYTES = 32 * 1024
_DEFAULT_OUTPUT_BYTES = 1024 * 1024
_MAX_CAPTURE_BYTES = 1024 * 1024
_IDLE_TTL_SECONDS = 30 * 60
_MAX_KERNELS = 64
_MAX_PENDING_CHILD_CALLS = 8
_INTERRUPT_GRACE_SECONDS = 2.0
_MAX_IPC_MESSAGE_BYTES = 1024 * 1024
_MAX_CHILD_REFERENCES = 128
_TERMINAL_SCOPE_REASONS = frozenset(
    {"conversation_terminal", "task_terminal", "workflow_terminal", "scope_terminal"}
)

ToolCallHandler = Callable[[ProgramToolCall], Awaitable[ProgramToolValue]]
OutputCallback = Callable[[str, str], Awaitable[None]]


class ProgramRuntimeError(RuntimeError):
    """Sanitized runtime protocol or state error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        outcome_certainty: str = "known",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.outcome_certainty = outcome_certainty


@dataclass(slots=True)
class _ExecutionState:
    request: ProgramExecutionRequest
    handler: ToolCallHandler
    kernel_generation: int
    max_calls: int
    max_concurrency: int
    output_budget: int
    admitted: int = 0
    completed: int = 0
    failed: int = 0
    output_bytes: int = 0
    child_call_references: list[str] = field(default_factory=list)
    child_call_overflow: bool = False
    sequence_ids: dict[int, str] = field(default_factory=dict)
    semaphore: asyncio.Semaphore = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_calls: int = 0
    pending_calls: int = 0
    emitted: Any = None
    emit_count: int = 0
    active_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    failed_error: ProgramRuntimeError | None = None
    uncertain: bool = False
    awaiting_approvals: set[int] = field(default_factory=set)
    pause_started: float | None = None
    paused_seconds: float = 0.0
    status_changed: asyncio.Event = field(default_factory=asyncio.Event)
    wall_started_at: float | None = None
    wall_paused_before: float = 0.0

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.max_concurrency)

    def reference(self, call_id: str | None) -> None:
        if not call_id:
            return
        if len(self.child_call_references) < _MAX_CHILD_REFERENCES:
            self.child_call_references.append(call_id)
        else:
            self.child_call_overflow = True


@dataclass(slots=True)
class _KernelSession:
    scope_id: str
    generation: int
    manager: AsyncKernelManager
    client: Any
    kernel_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    alive: bool = True
    socket_path: str | None = None
    socket_server: asyncio.AbstractServer | None = None
    current: _ExecutionState | None = None
    ipc_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    temporary_directory: str | None = None
    ipc_token: str | None = None
    busy: bool = False


def _bounded_limit(requested: int | None, default: int, ceiling: int) -> int:
    if requested is None:
        return default
    return max(1, min(requested, ceiling))


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bootstrap_code(socket_path: str, token: str) -> str:
    """Install the small async API in the kernel namespace."""
    return f"""
import asyncio as _ptc_asyncio
import json as _ptc_json
import os as _ptc_os
import inspect as _ptc_inspect
import hashlib as _ptc_hashlib
import typing as _ptc_typing
import keyword as _ptc_keyword

_ptc_socket_path = {socket_path!r}
_ptc_token = {token!r}
_ptc_sequence = 0
_ptc_tools = {{}}
_ptc_aliases = {{}}
_ptc_inputs = {{}}
_ptc_managed_tasks = set()
_ptc_current = {{"program_id": "", "execution_id": "", "generation": 0}}

class ToolExecutionError(RuntimeError):
    def __init__(self, value):
        self.value = value
        self.outcome_certainty = value.get("outcome_certainty", "known")
        super().__init__(value.get("text") or "Cognis tool call failed")

class ToolValue:
    def __init__(self, value):
        self.data = value.get("data")
        self.text = value.get("text", "")
        self.format = value.get("format", "text")
        self.call_id = value.get("call_id")
        self.attachments = value.get("attachments", [])
        self.truncated = value.get("truncated", False)
        self.recovery_reference = value.get("recovery_reference")
        self.is_error = value.get("is_error", False)
        self.outcome_certainty = value.get("outcome_certainty", "known")
    def __repr__(self):
        return f"ToolValue(format={{self.format!r}}, call_id={{self.call_id!r}}, truncated={{self.truncated!r}})"

class _ToolStub:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.__name__ = descriptor["alias"]
        self.__doc__ = descriptor["description"]
        self.__signature__ = _ptc_signature(descriptor["input_schema"])
    async def __call__(self, **arguments):
        global _ptc_sequence
        _ptc_sequence += 1
        return await _ptc_request({{
            "kind": "tool_call",
            "sequence": _ptc_sequence,
            "tool_id": self.descriptor["tool_id"],
            "schema_hash": self.descriptor["schema_hash"],
            "arguments": arguments,
        }})

def _ptc_signature(schema):
    props = schema.get("properties", {{}})
    required = set(schema.get("required", []))
    parameters = []
    for name, spec in props.items():
        if not name.isidentifier() or name.startswith("_") or _ptc_keyword.iskeyword(name):
            continue
        default = _ptc_inspect.Parameter.empty if name in required else None
        parameters.append(_ptc_inspect.Parameter(name, _ptc_inspect.Parameter.KEYWORD_ONLY, default=default))
    return _ptc_inspect.Signature(parameters)

async def _ptc_request(payload):
    reader, writer = await _ptc_asyncio.open_unix_connection(_ptc_socket_path, limit={_MAX_IPC_MESSAGE_BYTES})
    try:
        message = {{
            "token": _ptc_token,
            "program_id": _ptc_current["program_id"],
            "execution_id": _ptc_current["execution_id"],
            "kernel_generation": _ptc_current["generation"],
            **payload,
        }}
        writer.write(_ptc_json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\\n")
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise RuntimeError("Cognis program service disconnected")
        result = _ptc_json.loads(line)
        if not isinstance(result, dict):
            raise RuntimeError("Invalid Cognis program response")
        if result.get("is_error"):
            raise ToolExecutionError(result)
        if result.get("kind") in ("emit_ack", "cell_tasks_ack"):
            return None
        return ToolValue(result)
    finally:
        writer.close()
        await writer.wait_closed()

class _ToolNamespace:
    async def search(self, query, limit=10):
        text = str(query).casefold()
        matches = [
            descriptor
            for descriptor in _ptc_aliases.values()
            if text in descriptor["name"].casefold()
            or text in descriptor["tool_id"].casefold()
            or text in descriptor["alias"].casefold()
            or text in descriptor["description"].casefold()
        ]
        def rank(descriptor):
            identifiers = tuple(
                descriptor[key].casefold() for key in ("tool_id", "name", "alias")
            )
            if text in identifiers:
                return 0
            if text in identifiers[1] or text in identifiers[2]:
                return 1
            return 2
        matches.sort(key=rank)
        return matches[:max(0, min(int(limit), 50))]
    async def describe(self, tool_id):
        for descriptor in _ptc_aliases.values():
            if tool_id in (descriptor["tool_id"], descriptor["alias"], descriptor["name"]):
                return dict(descriptor)
        raise KeyError("Tool is not in this execution plan")
    def load(self, descriptor):
        tool_id = descriptor.get("tool_id") if isinstance(descriptor, dict) else descriptor
        for item in _ptc_aliases.values():
            if item["tool_id"] == tool_id:
                return _ToolStub(item)
        raise KeyError("Tool is not in this execution plan")
    def __getattr__(self, alias):
        descriptor = _ptc_aliases.get(alias)
        if descriptor is None:
            raise AttributeError(alias)
        stub = _ptc_tools.get(alias)
        if stub is None:
            stub = _ToolStub(descriptor)
            _ptc_tools[alias] = stub
        return stub

class _Cognis:
    tools = _ToolNamespace()
    async def emit(self, value):
        await _ptc_request({{"kind": "emit", "value": value}})
    def create_task(self, coro, *, name=None):
        task = _ptc_asyncio.create_task(coro, name=name)
        _ptc_managed_tasks.add(task)
        return task

cognis = _Cognis()
inputs = {{}}
"""


def _configured_state_ledger_path() -> Path:
    configured_path = os.environ.get("COGNIS_PTC_STATE_PATH")
    return Path(configured_path or str(Path.home() / ".cognis" / "program_runtime_state.json"))


def executor_state_ledger_path(executor_id: str) -> Path:
    """Return a stable, non-identifying sibling ledger path for one executor."""
    if not executor_id.strip():
        raise ValueError("Program runtime executor identity must not be empty")

    base_path = _configured_state_ledger_path()
    if not base_path.name or (base_path.exists() and base_path.is_dir()):
        raise ValueError("COGNIS_PTC_STATE_PATH must name a ledger file")

    identity_digest = hashlib.sha256(executor_id.encode("utf-8")).hexdigest()
    suffix = base_path.suffix
    stem = base_path.name[: -len(suffix)] if suffix else base_path.name
    return base_path.with_name(f"{stem}.{identity_digest}{suffix}")


class ProgramRuntime:
    """Owns executor-local kernels and their controller-governed IPC."""

    def __init__(self, *, executor_id: str, ledger_path: Path | None = None) -> None:
        self.executor_id = executor_id
        self._sessions: dict[str, _KernelSession] = {}
        self._generations: dict[str, int] = {}
        self._ledger_path = ledger_path or _configured_state_ledger_path()
        self._ledger: dict[str, dict[str, Any]] = {}
        self._state_lost_reasons: dict[str, str] = {}
        self._ledger_healthy = True
        # A lost ledger must not make stale generation 1 requests valid again.
        # Keep the fallback within JavaScript's exact-integer range.
        self._initial_generation = secrets.randbelow(1 << 52) + 1
        self._load_state_ledger()
        self._lock = asyncio.Lock()
        self._closed = False

    async def execute(
        self,
        request: ProgramExecutionRequest,
        *,
        tool_call_handler: ToolCallHandler,
        output_callback: OutputCallback | None = None,
    ) -> ProgramResult:
        """Execute one cell without replay; persist state only for its scope."""
        if self._closed:
            return self._failure(request, "runtime_closed", "Program runtime is closed.")
        if not self._ledger_healthy:
            return self._failure(
                request,
                "state_ledger_unavailable",
                "Program state recovery metadata is unavailable; execution is disabled.",
            )
        try:
            if request.input_schema is not None:
                offline_draft202012_validator(request.input_schema).check_schema(
                    request.input_schema
                )
            if request.output_schema is not None:
                offline_draft202012_validator(request.output_schema).check_schema(
                    request.output_schema
                )
            for descriptor in request.tools:
                offline_draft202012_validator(descriptor.input_schema).check_schema(
                    descriptor.input_schema
                )
                if descriptor.output_schema is not None:
                    offline_draft202012_validator(descriptor.output_schema).check_schema(
                        descriptor.output_schema
                    )
            json.dumps(request.inputs, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            return self._failure(
                request,
                "invalid_program_schema",
                f"Program tool or output schema is invalid ({type(exc).__name__}).",
            )
        code_digest = hashlib.sha256(request.code.encode("utf-8")).hexdigest()
        manifest = [item.model_dump(mode="json") for item in request.tools]
        manifest_digest = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if request.code_digest is not None and request.code_digest != code_digest:
            return self._failure(
                request, "program_digest_mismatch", "Program digest does not match."
            )
        if (
            request.tool_manifest_digest is not None
            and request.tool_manifest_digest != manifest_digest
        ):
            return self._failure(
                request, "tool_manifest_mismatch", "Program tool manifest does not match."
            )
        if request.mode == "workflow" and (
            request.program_digest is None
            or request.code_digest is None
            or request.tool_manifest_digest is None
        ):
            return self._failure(
                request,
                "workflow_digest_required",
                "Workflow programs require pinned code and tool-manifest digests.",
            )
        if request.input_schema is not None:
            failures = list(
                offline_draft202012_validator(request.input_schema).iter_errors(request.inputs)
            )
            if failures:
                return self._failure(
                    request,
                    "input_schema_invalid",
                    f"Program inputs do not match input_schema at {failures[0].json_path}.",
                )
        started = time.monotonic()
        limits = request.limits
        wall_seconds = _bounded_limit(limits.wall_seconds, _DEFAULT_WALL_SECONDS, _MAX_WALL_SECONDS)
        output_budget = _bounded_limit(
            limits.output_bytes, _DEFAULT_OUTPUT_BYTES, _MAX_CAPTURE_BYTES
        )
        max_calls = _bounded_limit(limits.max_calls, _DEFAULT_CALLS, _DEFAULT_CALLS)
        concurrency = _bounded_limit(limits.concurrency, _DEFAULT_CONCURRENCY, _DEFAULT_CONCURRENCY)
        if any(not descriptor.read_only for descriptor in request.tools):
            # Mutating child calls are serialized in v1. This bounds the
            # reconciliation surface when a connection is interrupted.
            concurrency = 1
        self._known_generation(request.scope_id)
        session: _KernelSession | None = None
        reused = False
        state: _ExecutionState | None = None
        stdout: list[str] = []
        captured_bytes = 0
        status = "failed"
        error_code: str | None = None
        error_message = ""
        error_certainty = "known"
        state_lost_reason: str | None = self._state_lost_reasons.get(request.scope_id)
        program_started = False
        try:
            session, reused = await self._get_or_start_session(request)
            async with session.lock:
                if not session.alive:
                    raise ProgramRuntimeError("kernel_state_lost", "Kernel state was lost.")
                state = _ExecutionState(
                    request=request,
                    handler=tool_call_handler,
                    kernel_generation=session.generation,
                    max_calls=max_calls,
                    max_concurrency=concurrency,
                    output_budget=output_budget,
                )
                session.current = state
                await self._serve_ipc(session, state)
                descriptors = [item.model_dump(mode="json") for item in request.tools]
                input_json = json.dumps(
                    request.inputs, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                descriptors_json = json.dumps(
                    descriptors, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                runtime_guide = self._render_runtime_guide(
                    request=request,
                    generation=session.generation,
                    eligible_tools=descriptors,
                    limits={
                        "wall_seconds": wall_seconds,
                        "max_calls": max_calls,
                        "concurrency": concurrency,
                        "output_bytes": output_budget,
                    },
                )
                bind = (
                    "_ptc_current.update("
                    + repr(
                        {
                            "program_id": request.program_id,
                            "execution_id": request.execution_id,
                            "generation": session.generation,
                        }
                    )
                    + "); "
                    + "inputs = _ptc_json.loads("
                    + repr(input_json)
                    + ")"
                    + "; "
                    + "_ptc_aliases = {item['alias']: item for item in "
                    + "_ptc_json.loads("
                    + repr(descriptors_json)
                    + ")}; "
                    + "cognis.runtime_guide = "
                    + repr(runtime_guide)
                    + "; "
                    + "_ptc_managed_tasks = set()"
                )
                binding_result = await self._execute_cell(session, bind, wall_seconds=10)
                if binding_result.get("error"):
                    raise ProgramRuntimeError(
                        "program_binding_error",
                        f"Program bindings could not be installed ({binding_result['error']}).",
                    )
                state.wall_started_at = time.monotonic()
                state.wall_paused_before = state.paused_seconds
                program_started = True
                output = await self._execute_cell(
                    session,
                    request.code,
                    wall_seconds=self._remaining(state, wall_seconds),
                    output=stdout,
                    output_callback=output_callback,
                )
                cleanup = await self._execute_cell(
                    session,
                    (
                        "_ptc_live_tasks = [task for task in _ptc_managed_tasks "
                        "if not task.done() and task is not _ptc_asyncio.current_task()]; "
                        "[task.cancel() for task in _ptc_live_tasks]; "
                        "await _ptc_asyncio.gather(*_ptc_live_tasks, return_exceptions=True); "
                        "await _ptc_request({'kind': 'cell_tasks', "
                        "'count': len(_ptc_live_tasks)})"
                    ),
                    wall_seconds=self._remaining(state, wall_seconds),
                )
                if cleanup.get("error"):
                    raise ProgramRuntimeError(
                        "cell_cleanup_failed",
                        f"Program cell cleanup failed ({cleanup['error']}).",
                    )
                captured_bytes = sum(len(item.encode("utf-8")) for item in stdout)
                if state.output_bytes > output_budget:
                    raise ProgramRuntimeError(
                        "budget_exceeded", "Program output exceeded its configured byte budget."
                    )
                if state.failed_error is not None:
                    raise state.failed_error
                if state.uncertain:
                    raise ProgramRuntimeError(
                        "nested_outcome_uncertain",
                        "A nested tool call has an unresolved delivery outcome.",
                        outcome_certainty="unknown",
                    )
                if output.get("error"):
                    error_message = str(output["error"])[:512]
                    raise ProgramRuntimeError("execution_error", error_message)
                if request.mode == "workflow" and state.failed:
                    raise ProgramRuntimeError(
                        "workflow_child_failed",
                        "A workflow program child call failed or was denied.",
                    )
                if state.emit_count > 1:
                    raise ProgramRuntimeError("invalid_emit", "cognis.emit() may be called once.")
                if request.mode == "workflow" and state.emit_count != 1:
                    raise ProgramRuntimeError(
                        "missing_emit", "Workflow programs must emit exactly one JSON value."
                    )
                result_value = state.emitted if state.emit_count == 1 else None
                if state.emit_count == 1 and request.output_schema is not None:
                    failures = list(
                        offline_draft202012_validator(request.output_schema).iter_errors(
                            result_value
                        )
                    )
                    if failures:
                        raise ProgramRuntimeError(
                            "output_schema_invalid",
                            f"Program result does not match output_schema at {failures[0].json_path}.",
                        )
                status = "completed"
        except asyncio.CancelledError:
            status = "cancelled"
            error_code = "cancelled"
            error_message = "Program execution was cancelled."
            error_certainty = "unknown"
            if session is not None:
                state_lost_reason = "cancelled"
                await self._interrupt_and_discard(session, reason="cancelled")
        except TimeoutError:
            status = "timed_out"
            error_code = "program_timeout"
            error_message = "Program execution exceeded its wall-time limit."
            error_certainty = "unknown" if program_started else "known"
            if session is not None:
                state_lost_reason = "timeout"
                await self._interrupt_and_discard(session, reason="timeout")
        except ProgramRuntimeError as exc:
            error_code = exc.code
            error_message = str(exc)
            error_certainty = (
                "unknown" if state is not None and state.uncertain else exc.outcome_certainty
            )
            if program_started and exc.code in {
                "execution_error",
                "output_schema_invalid",
                "workflow_child_failed",
                "missing_emit",
                "invalid_emit",
                "program_timeout",
            }:
                error_certainty = "unknown"
            state_lost_reason = self._state_lost_reasons.get(request.scope_id, state_lost_reason)
            if exc.code == "budget_exceeded":
                status = "budget_exceeded"
            elif exc.code == "nested_outcome_uncertain":
                status = "uncertain"
            else:
                status = "failed"
            if session is not None and exc.code in {"budget_exceeded", "execution_error"}:
                state_lost_reason = exc.code
                await self._interrupt_and_discard(session, reason=exc.code)
        except Exception as exc:
            status = "failed"
            error_code = "program_runtime_error"
            error_message = f"Program runtime failed ({type(exc).__name__})."
            error_certainty = "unknown" if program_started else "known"
            logger.warning(
                "Program runtime failed",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "program_id": request.program_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            if session is not None:
                state_lost_reason = "runtime_failure"
                await self._interrupt_and_discard(session, reason="runtime_failure")
        finally:
            if session is not None:
                if state is not None:
                    await self._finish_ipc(session, state)
                    if session.current is state:
                        session.current = None
                    session.last_used = time.monotonic()
                session.busy = False
                if request.mode == "workflow" and session.alive:
                    state_lost_reason = "workflow_terminal"
                    await self._discard_session(session, reason="workflow_terminal")

        generation = (
            session.generation
            if session is not None and session.alive
            else self._known_generation(request.scope_id)
        )
        result_value = state.emitted if state is not None and state.emit_count == 1 else None
        combined = "".join(stdout)
        preview = combined.encode("utf-8")[:_INLINE_PREVIEW_BYTES].decode("utf-8", errors="ignore")
        return ProgramResult(
            program_id=request.program_id,
            program_digest=request.program_digest,
            code_digest=code_digest,
            tool_manifest_digest=manifest_digest,
            kernel_generation=generation,
            kernel_reused=reused,
            state_lost_reason=state_lost_reason,
            status=status,
            result=result_value if status == "completed" else None,
            error=(
                {
                    "code": error_code or "program_failed",
                    "bounded_message": error_message[:1024],
                    "retryable": False,
                    "outcome_certainty": error_certainty,
                }
                if status != "completed"
                else None
            ),
            stdout_preview=preview,
            nested_admitted=state.admitted if state is not None else 0,
            nested_completed=state.completed if state is not None else 0,
            nested_failed=state.failed if state is not None else 0,
            wall_ms=max(0, int((time.monotonic() - started) * 1000)),
            output_bytes=state.output_bytes if state is not None else captured_bytes,
            cost_complete=False,
            child_call_references=state.child_call_references if state is not None else [],
            child_call_overflow=state.child_call_overflow if state is not None else False,
        )

    async def status(self, scope_id: str) -> dict[str, Any]:
        """Return kernel metadata without exposing namespace values."""
        if not self._ledger_healthy:
            raise ProgramRuntimeError(
                "state_ledger_unavailable",
                "Program state recovery metadata is unavailable.",
            )
        session = self._sessions.get(scope_id)
        if session is None:
            return {
                "state": "absent",
                "kernel_generation": self._known_generation(scope_id),
                "state_lost_reason": self._state_lost_reasons.get(scope_id),
            }
        return {
            "state": "running" if session.busy or session.current is not None else "idle",
            "kernel_generation": session.generation,
            "kernel_id": session.kernel_id,
            "last_used_monotonic": session.last_used,
            "active_program_id": (
                session.current.request.program_id if session.current is not None else None
            ),
        }

    async def terminate_scope(
        self, scope_id: str, *, reason: str = "scope_terminal"
    ) -> dict[str, Any]:
        """Discard one conversation or workflow scope when its owner is terminal."""
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("A program scope ID is required.")
        if reason not in _TERMINAL_SCOPE_REASONS:
            reason = "scope_terminal"
        async with self._lock:
            session = self._sessions.get(scope_id)
            if session is None:
                existing = self._ledger.get(self._scope_key(scope_id))
                if (
                    existing is None
                    or existing.get("state_lost_reason") not in _TERMINAL_SCOPE_REASONS
                ):
                    generation = max(self._known_generation(scope_id), 1) + 1
                    self._persist_scope(scope_id, generation, active=False, reason=reason)
                else:
                    generation = self._known_generation(scope_id)
                return {
                    "closed": 0,
                    "kernel_generation": generation,
                    "state_lost_reason": self._state_lost_reasons.get(scope_id),
                }
            if session.current is not None or session.busy:
                await self._interrupt_and_discard(session, reason=reason)
            else:
                await self._discard_session(session, reason=reason)
        return {
            "closed": 1,
            "kernel_generation": self._known_generation(scope_id),
            "state_lost_reason": self._state_lost_reasons.get(scope_id),
        }

    def update_status(self, params: dict[str, Any]) -> bool:
        """Pause/resume the active-cell wall clock for ordinary approval waits."""
        scope_id = params.get("scope_id")
        session = self._sessions.get(scope_id) if isinstance(scope_id, str) else None
        state = session.current if session is not None else None
        if state is None:
            return False
        request = state.request
        if (
            params.get("program_id") != request.program_id
            or params.get("execution_id") != request.execution_id
            or params.get("kernel_generation") != state.kernel_generation
        ):
            return False
        sequence = params.get("sequence")
        status = params.get("status")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            return False
        if status == "awaiting_approval":
            state.awaiting_approvals.add(sequence)
            if state.pause_started is None:
                state.pause_started = time.monotonic()
        elif status == "running":
            state.awaiting_approvals.discard(sequence)
            if not state.awaiting_approvals and state.pause_started is not None:
                state.paused_seconds += time.monotonic() - state.pause_started
                state.pause_started = None
        else:
            return False
        state.status_changed.set()
        return True

    async def reset(self, scope_id: str, expected_generation: int) -> dict[str, Any]:
        """Explicitly discard one matching idle kernel and advance its generation."""
        if not self._ledger_healthy:
            raise ProgramRuntimeError(
                "state_ledger_unavailable",
                "Program state recovery metadata is unavailable.",
            )
        if self._state_lost_reasons.get(scope_id) in _TERMINAL_SCOPE_REASONS:
            raise ProgramRuntimeError("scope_terminal", "A terminal program scope cannot be reset.")
        session = self._sessions.get(scope_id)
        if session is not None:
            if session.generation != expected_generation:
                raise ProgramRuntimeError("stale_generation", "Kernel generation is stale.")
            if session.busy or session.current is not None:
                raise ProgramRuntimeError("kernel_busy", "An active program cannot be reset.")
            await self._discard_session(session, reason="explicit_reset")
        elif expected_generation != self._known_generation(scope_id):
            raise ProgramRuntimeError("stale_generation", "Kernel generation is stale.")
        generation = (
            self._generations[scope_id]
            if session is not None
            else max(expected_generation, self._known_generation(scope_id)) + 1
        )
        self._persist_scope(scope_id, generation, active=False, reason="explicit_reset")
        return {
            "state": "reset",
            "kernel_generation": generation,
            "state_lost_reason": "explicit_reset",
        }

    async def close(self) -> None:
        """Stop all managed kernels during executor shutdown."""
        self._closed = True
        sessions = list(self._sessions.values())
        await asyncio.gather(
            *(self._discard_session(session, reason="executor_shutdown") for session in sessions),
            return_exceptions=True,
        )

    async def _get_or_start_session(
        self, request: ProgramExecutionRequest
    ) -> tuple[_KernelSession, bool]:
        await self._reap_idle()
        async with self._lock:
            current = self._sessions.get(request.scope_id)
            if current is not None:
                if request.expected_generation is None:
                    raise ProgramRuntimeError(
                        "expected_generation_required",
                        "Existing kernel calls require the current expected_generation.",
                    )
                if current.generation != request.expected_generation:
                    raise ProgramRuntimeError("stale_generation", "Kernel generation is stale.")
                if not current.alive:
                    raise ProgramRuntimeError("kernel_state_lost", "Kernel state was lost.")
                if current.busy or current.current is not None:
                    raise ProgramRuntimeError("kernel_busy", "A program is already active.")
                current.busy = True
                return current, True
            scope_record = self._ledger.get(self._scope_key(request.scope_id))
            if request.expected_generation is not None and scope_record is None:
                raise ProgramRuntimeError(
                    "kernel_state_lost",
                    "Program state metadata is missing; reset is required before reuse.",
                )
            generation = self._known_generation(request.scope_id)
            if (
                scope_record is not None
                and scope_record.get("state_lost_reason") in _TERMINAL_SCOPE_REASONS
            ):
                raise ProgramRuntimeError(
                    "scope_terminal",
                    "A terminal program scope cannot be executed.",
                )
            if request.expected_generation is None and scope_record is not None:
                raise ProgramRuntimeError(
                    "expected_generation_required",
                    "Existing kernel state requires the current expected_generation.",
                )
            if (
                request.expected_generation is not None
                and request.expected_generation != generation
            ):
                raise ProgramRuntimeError(
                    "stale_generation",
                    f"Kernel generation mismatch; expected {generation}.",
                )
            if len(self._sessions) >= _MAX_KERNELS:
                raise ProgramRuntimeError(
                    "kernel_capacity",
                    "Executor program kernel capacity is exhausted.",
                    retryable=True,
                )
            self._persist_scope(request.scope_id, generation, active=True, reason=None)
            try:
                session = await self._start_session(request.scope_id, generation)
            except Exception:
                self._persist_scope(
                    request.scope_id, generation + 1, active=False, reason="kernel_start_failed"
                )
                raise
            session.busy = True
            self._sessions[request.scope_id] = session
            self._generations[request.scope_id] = generation
            return session, False

    async def _start_session(self, scope_id: str, generation: int) -> _KernelSession:
        manager = AsyncKernelManager(kernel_name="python3")
        temporary_directory = tempfile.mkdtemp(prefix="cognis-ptc-")
        socket_path = os.path.join(temporary_directory, "rpc.sock")
        token = secrets.token_urlsafe(32)
        try:
            await manager.start_kernel(
                cwd=temporary_directory,
                env={
                    **os.environ,
                    "COGNIS_PTC_SOCKET": socket_path,
                    "COGNIS_PTC_TOKEN": token,
                },
            )
            client = manager.client()
            client.start_channels()
            await client.wait_for_ready(timeout=30)
            session = _KernelSession(
                scope_id=scope_id,
                generation=generation,
                manager=manager,
                client=client,
                kernel_id=str(manager.kernel_id),
                socket_path=socket_path,
                temporary_directory=temporary_directory,
            )
            bootstrap_result = await self._execute_cell(
                session, _bootstrap_code(socket_path, token), wall_seconds=30
            )
            if bootstrap_result.get("error"):
                raise ProgramRuntimeError(
                    "kernel_bootstrap_error",
                    f"Kernel program API setup failed ({bootstrap_result['error']}).",
                )
            return session
        except BaseException:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise

    async def _execute_cell(
        self,
        session: _KernelSession,
        code: str,
        *,
        wall_seconds: float,
        output: list[str] | None = None,
        output_callback: OutputCallback | None = None,
    ) -> dict[str, Any]:
        client = session.client
        message_id = client.execute(code, allow_stdin=False, stop_on_error=True)
        output = output if output is not None else []
        error: str | None = None
        state = session.current
        paused_before = state.paused_seconds if state is not None else 0.0
        cell_started = time.monotonic()
        message_task = asyncio.create_task(client.get_iopub_msg())
        status_task: asyncio.Task[bool] | None = None
        try:
            while True:
                if state is not None and state.status_changed.is_set():
                    state.status_changed.clear()
                if state is not None and state.awaiting_approvals:
                    remaining: float | None = None
                else:
                    now = time.monotonic()
                    paused = state.paused_seconds - paused_before if state is not None else 0.0
                    if state is not None and state.pause_started is not None:
                        paused += now - state.pause_started
                    remaining = wall_seconds - (now - cell_started - paused)
                    if remaining <= 0:
                        raise TimeoutError
                if state is not None:
                    status_task = asyncio.create_task(state.status_changed.wait())
                    done, _pending = await asyncio.wait(
                        {message_task, status_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        raise TimeoutError
                    if status_task in done and message_task not in done:
                        state.status_changed.clear()
                        status_task = None
                        continue
                    status_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await status_task
                    status_task = None
                else:
                    try:
                        await asyncio.wait_for(message_task, timeout=remaining)
                    except TimeoutError:
                        raise
                message = message_task.result()
                message_task = asyncio.create_task(client.get_iopub_msg())
                if message.get("parent_header", {}).get("msg_id") != message_id:
                    continue
                kind = message.get("header", {}).get("msg_type")
                content = message.get("content", {})
                if kind == "stream":
                    text = content.get("text")
                    if isinstance(text, str):
                        output.append(text)
                        if state is not None:
                            state.output_bytes += len(text.encode("utf-8"))
                            if state.output_bytes > _MAX_CAPTURE_BYTES:
                                raise ProgramRuntimeError(
                                    "budget_exceeded",
                                    "Program output capture exceeded its hard limit.",
                                )
                        if output_callback is not None:
                            with contextlib.suppress(Exception):
                                stream = "stderr" if content.get("name") == "stderr" else "stdout"
                                await output_callback(text, stream)
                elif kind == "error":
                    error = (
                        f"{content.get('ename') or 'PythonError'}: "
                        f"{content.get('evalue') or 'program failed'}"
                    )[:512]
                elif kind == "execute_result":
                    data = content.get("data")
                    text = data.get("text/plain") if isinstance(data, dict) else None
                    if isinstance(text, str):
                        output.append(text)
                        if state is not None:
                            state.output_bytes += len(text.encode("utf-8"))
                            if state.output_bytes > _MAX_CAPTURE_BYTES:
                                raise ProgramRuntimeError(
                                    "budget_exceeded",
                                    "Program output capture exceeded its hard limit.",
                                )
                        if output_callback is not None:
                            with contextlib.suppress(Exception):
                                await output_callback(text, "stdout")
                elif kind == "status" and content.get("execution_state") == "idle":
                    break
        finally:
            for task in (message_task, status_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (message_task, status_task) if task is not None),
                return_exceptions=True,
            )
        return {"error": error}

    async def _serve_ipc(self, session: _KernelSession, state: _ExecutionState) -> None:
        if session.socket_path is None:
            raise ProgramRuntimeError("ipc_unavailable", "Program IPC is unavailable.")

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                session.ipc_tasks.add(task)
            try:
                raw = await reader.readline()
                if not raw or len(raw) > _MAX_IPC_MESSAGE_BYTES:
                    raise ProgramRuntimeError("invalid_ipc", "Program request is too large.")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ProgramRuntimeError("invalid_ipc", "Invalid program request.")
                if message.get("token") != session.ipc_token:
                    # The random socket path is the primary private capability;
                    # the token also rejects stale or accidental local clients.
                    raise ProgramRuntimeError(
                        "invalid_ipc", "Program request authentication failed."
                    )
                response = await self._dispatch_ipc(state, message)
                writer.write(
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )
                await writer.drain()
            except ProgramRuntimeError as exc:
                response = {
                    "is_error": True,
                    "text": str(exc),
                    "code": exc.code,
                    "outcome_certainty": exc.outcome_certainty,
                }
                with contextlib.suppress(Exception):
                    writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                    await writer.drain()
            except (ValueError, TypeError, json.JSONDecodeError):
                response = {"is_error": True, "text": "Invalid program IPC payload."}
                with contextlib.suppress(Exception):
                    writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                    await writer.drain()
            except Exception as exc:
                logger.warning(
                    "Program IPC request failed",
                    extra={"extra_data": {"error_type": type(exc).__name__}},
                )
                with contextlib.suppress(Exception):
                    writer.write(
                        json.dumps(
                            {
                                "is_error": True,
                                "text": "Program tool call failed.",
                                "outcome_certainty": ("unknown" if state.uncertain else "known"),
                            },
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )
                    await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                if task is not None:
                    session.ipc_tasks.discard(task)

        # Kernel started with the token in its environment. The path is private
        # to the executor-owned temporary directory (0700 from mkdtemp).
        # Each supervisor invocation rotates the kernel-side token so retained
        # helpers cannot dispatch a completed execution.
        token = secrets.token_urlsafe(32)
        session.ipc_token = token
        await self._execute_cell(
            session,
            f"_ptc_token = {token!r}",
            wall_seconds=10,
        )
        session.socket_server = await asyncio.start_unix_server(
            accept, path=session.socket_path, limit=_MAX_IPC_MESSAGE_BYTES
        )
        os.chmod(session.socket_path, 0o600)

    async def _dispatch_ipc(
        self, state: _ExecutionState, message: dict[str, Any]
    ) -> dict[str, Any]:
        request = state.request
        if (
            message.get("program_id") != request.program_id
            or message.get("execution_id") != request.execution_id
            or message.get("kernel_generation") != state.kernel_generation
        ):
            raise ProgramRuntimeError("stale_execution", "Program execution binding is stale.")
        kind = message.get("kind")
        if kind == "emit":
            state.emit_count += 1
            if state.emit_count != 1:
                raise ProgramRuntimeError("invalid_emit", "cognis.emit() may be called once.")
            try:
                emitted_bytes = _json_size(message.get("value"))
                json.dumps(message.get("value"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ProgramRuntimeError(
                    "invalid_emit", "cognis.emit() requires a JSON value."
                ) from exc
            state.output_bytes += emitted_bytes
            if state.output_bytes > state.output_budget:
                state.failed_error = ProgramRuntimeError(
                    "budget_exceeded", "Program result exceeded its configured byte budget."
                )
                raise state.failed_error
            state.emitted = message.get("value")
            return {"kind": "emit_ack"}
        if kind == "cell_tasks":
            count = message.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProgramRuntimeError("invalid_task_count", "Invalid program task count.")
            if count:
                state.failed_error = ProgramRuntimeError(
                    "detached_tasks", "Background tasks were stopped at the cell boundary."
                )
            return {"kind": "cell_tasks_ack"}
        if kind != "tool_call":
            raise ProgramRuntimeError("invalid_ipc", "Unsupported program IPC operation.")
        if state.uncertain:
            raise ProgramRuntimeError(
                "nested_outcome_uncertain",
                "A nested tool call has an unresolved delivery outcome.",
                outcome_certainty="unknown",
            )
        sequence = message.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ProgramRuntimeError("invalid_sequence", "Invalid program call sequence.")
        tool_id = message.get("tool_id")
        schema_hash = message.get("schema_hash")
        descriptor = next(
            (
                item
                for item in request.tools
                if item.tool_id == tool_id and item.schema_hash == schema_hash
            ),
            None,
        )
        if descriptor is None:
            raise ProgramRuntimeError("tool_not_in_plan", "Tool is not in this execution plan.")
        arguments = message.get("arguments")
        if not isinstance(arguments, dict):
            raise ProgramRuntimeError("invalid_arguments", "Tool arguments must be an object.")
        failures = list(
            offline_draft202012_validator(descriptor.input_schema).iter_errors(arguments)
        )
        if failures:
            raise ProgramRuntimeError(
                "invalid_arguments",
                f"Tool arguments do not match the declared schema at {failures[0].json_path}.",
            )
        fingerprint = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        async with state.lock:
            previous = state.sequence_ids.get(sequence)
            if previous is not None:
                if previous != fingerprint:
                    raise ProgramRuntimeError(
                        "sequence_conflict", "Program sequence was reused with different arguments."
                    )
                raise ProgramRuntimeError(
                    "duplicate_delivery", "Duplicate program tool delivery was not re-executed."
                )
            if state.admitted >= state.max_calls:
                state.failed += 1
                raise ProgramRuntimeError("budget_exceeded", "Program tool-call budget exhausted.")
            state.sequence_ids[sequence] = fingerprint
            state.admitted += 1
        waiting_for_slot = False
        active_slot = False
        async with state.admission_lock:
            if state.active_calls < state.max_concurrency:
                state.active_calls += 1
                active_slot = True
            elif state.pending_calls < _MAX_PENDING_CHILD_CALLS:
                state.pending_calls += 1
                waiting_for_slot = True
            else:
                state.failed += 1
                raise ProgramRuntimeError(
                    "pending_call_capacity",
                    "Program child-call queue is full.",
                    retryable=True,
                )
        try:
            async with state.semaphore:
                if waiting_for_slot:
                    async with state.admission_lock:
                        state.pending_calls -= 1
                        state.active_calls += 1
                        waiting_for_slot = False
                        active_slot = True
                if state.uncertain:
                    raise ProgramRuntimeError(
                        "nested_outcome_uncertain",
                        "A nested tool call has an unresolved delivery outcome.",
                        outcome_certainty="unknown",
                    )
                call = ProgramToolCall(
                    program_id=request.program_id,
                    execution_id=request.execution_id,
                    kernel_generation=state.kernel_generation,
                    sequence=sequence,
                    tool_id=descriptor.tool_id,
                    schema_hash=descriptor.schema_hash,
                    arguments=arguments,
                )
                task = asyncio.current_task()
                if task is not None:
                    state.active_tasks.add(task)
                try:
                    value = await state.handler(call)
                    if value.outcome_certainty == "unknown":
                        state.uncertain = True
                    state.reference(value.call_id)
                    if value.is_error:
                        state.failed += 1
                    else:
                        state.completed += 1
                    byte_count = len(value.text.encode("utf-8")) + _json_size(value.data)
                    state.output_bytes += byte_count
                    if state.output_bytes > state.output_budget:
                        state.failed_error = ProgramRuntimeError(
                            "budget_exceeded",
                            "Program tool results exceeded their byte budget.",
                        )
                        raise state.failed_error
                    return value.model_dump(mode="json")
                except asyncio.CancelledError:
                    state.failed += 1
                    state.uncertain = True
                    state.failed_error = ProgramRuntimeError(
                        "nested_outcome_uncertain",
                        "A dispatched child call was cancelled before its outcome was reconciled.",
                        outcome_certainty="unknown",
                    )
                    raise
                except Exception as exc:
                    state.failed += 1
                    if getattr(exc, "outcome_certainty", "unknown") == "unknown":
                        state.uncertain = True
                        state.failed_error = ProgramRuntimeError(
                            "nested_outcome_uncertain",
                            "A nested tool call has an unresolved delivery outcome.",
                            outcome_certainty="unknown",
                        )
                    raise
                finally:
                    if task is not None:
                        state.active_tasks.discard(task)
        finally:
            async with state.admission_lock:
                if active_slot:
                    state.active_calls -= 1
                elif waiting_for_slot:
                    state.pending_calls -= 1

    async def _finish_ipc(self, session: _KernelSession, state: _ExecutionState) -> None:
        if session.socket_server is not None:
            session.socket_server.close()
            with contextlib.suppress(Exception):
                await session.socket_server.wait_closed()
            session.socket_server = None
        if session.socket_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(session.socket_path)
        if state.active_tasks:
            for task in tuple(state.active_tasks):
                task.cancel()
            await asyncio.gather(*state.active_tasks, return_exceptions=True)
            state.active_tasks.clear()

    async def _interrupt_and_discard(
        self, session: _KernelSession, *, reason: str = "execution_interrupted"
    ) -> None:
        if not session.alive:
            return
        with contextlib.suppress(Exception):
            await session.manager.interrupt_kernel()
            await asyncio.sleep(_INTERRUPT_GRACE_SECONDS)
        await self._discard_session(session, reason=reason)

    async def _discard_session(self, session: _KernelSession, *, reason: str) -> None:
        if not session.alive:
            return
        session.alive = False
        if session.socket_server is not None:
            session.socket_server.close()
            with contextlib.suppress(Exception):
                await session.socket_server.wait_closed()
            session.socket_server = None
        for task in tuple(session.ipc_tasks):
            if task is not asyncio.current_task():
                task.cancel()
        if session.ipc_tasks:
            await asyncio.gather(
                *(task for task in session.ipc_tasks if task is not asyncio.current_task()),
                return_exceptions=True,
            )
        with contextlib.suppress(Exception):
            session.client.stop_channels()
        with contextlib.suppress(Exception):
            await session.manager.shutdown_kernel(now=True)
        if session.temporary_directory:
            shutil.rmtree(session.temporary_directory, ignore_errors=True)
        self._sessions.pop(session.scope_id, None)
        next_generation = (
            max(
                self._generations.get(session.scope_id, session.generation),
                session.generation,
            )
            + 1
        )
        with contextlib.suppress(ProgramRuntimeError):
            self._persist_scope(
                session.scope_id,
                next_generation,
                active=False,
                reason=reason,
            )
        logger.info(
            "Program kernel discarded",
            extra={
                "extra_data": {
                    "executor_id": self.executor_id,
                    "kernel_id": session.kernel_id,
                    "kernel_generation": session.generation,
                    "reason": reason,
                }
            },
        )

    async def _reap_idle(self) -> None:
        now = time.monotonic()
        expired = [
            session
            for session in self._sessions.values()
            if not session.busy
            and session.current is None
            and now - session.last_used >= _IDLE_TTL_SECONDS
        ]
        if expired:
            await asyncio.gather(
                *(self._discard_session(session, reason="idle_expired") for session in expired),
                return_exceptions=True,
            )

    def _scope_key(self, scope_id: str) -> str:
        value = f"{self.executor_id}\0{scope_id}".encode()
        return hashlib.sha256(value).hexdigest()

    def _known_generation(self, scope_id: str) -> int:
        generation = self._generations.get(scope_id)
        if generation is not None:
            return generation
        record = self._ledger.get(self._scope_key(scope_id))
        if record is None:
            generation = self._initial_generation
        else:
            generation = int(record["generation"])
            reason = record.get("state_lost_reason")
            if isinstance(reason, str) and reason:
                self._state_lost_reasons[scope_id] = reason
        self._generations[scope_id] = generation
        return generation

    def _load_state_ledger(self) -> None:
        """Recover generations without storing code, arguments, or kernel state."""
        try:
            self._ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self._ledger_path.exists():
                return
            payload = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported PTC state ledger")
            raw_records = payload.get("scopes")
            if not isinstance(raw_records, dict):
                raise ValueError("malformed PTC state ledger")
            for key, value in raw_records.items():
                if (
                    not isinstance(key, str)
                    or len(key) != 64
                    or not isinstance(value, dict)
                    or isinstance(value.get("generation"), bool)
                    or not isinstance(value.get("generation"), int)
                    or value["generation"] < 1
                    or not isinstance(value.get("active"), bool)
                ):
                    raise ValueError("malformed PTC scope ledger record")
                record = dict(value)
                if record["active"]:
                    record["generation"] += 1
                    record["active"] = False
                    record["state_lost_reason"] = "executor_restart"
                self._ledger[key] = record
            self._persist_ledger()
        except Exception as exc:
            self._ledger_healthy = False
            logger.error(
                "Program state ledger could not be loaded",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )

    def _persist_scope(
        self,
        scope_id: str,
        generation: int,
        *,
        active: bool,
        reason: str | None,
    ) -> None:
        if not self._ledger_healthy:
            raise ProgramRuntimeError(
                "state_ledger_unavailable",
                "Program state recovery metadata is unavailable.",
            )
        record = {
            "generation": generation,
            "active": active,
            "state_lost_reason": reason,
        }
        self._ledger[self._scope_key(scope_id)] = record
        self._generations[scope_id] = generation
        if reason:
            self._state_lost_reasons[scope_id] = reason
        elif active:
            self._state_lost_reasons.pop(scope_id, None)
        try:
            self._persist_ledger()
        except Exception as exc:
            self._ledger_healthy = False
            logger.error(
                "Program state ledger could not be persisted",
                extra={
                    "extra_data": {
                        "executor_id": self.executor_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            raise ProgramRuntimeError(
                "state_ledger_unavailable",
                "Program state recovery metadata could not be persisted.",
            ) from exc

    def _persist_ledger(self) -> None:
        payload = json.dumps(
            {"version": 1, "scopes": self._ledger},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="program-state-", dir=self._ledger_path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._ledger_path)
            os.chmod(self._ledger_path, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)

    def _failure(self, request: ProgramExecutionRequest, code: str, message: str) -> ProgramResult:
        return ProgramResult(
            program_id=request.program_id,
            program_digest=request.program_digest,
            code_digest=request.code_digest,
            tool_manifest_digest=request.tool_manifest_digest,
            kernel_generation=self._known_generation(request.scope_id),
            status="failed",
            error={
                "code": code,
                "bounded_message": message[:1024],
                "retryable": False,
                "outcome_certainty": "known",
            },
        )

    @staticmethod
    def _render_runtime_guide(
        *,
        request: ProgramExecutionRequest,
        generation: int,
        eligible_tools: list[dict[str, Any]],
        limits: dict[str, Any],
    ) -> str:
        from cognis.program_guidance import render_runtime_guide

        return render_runtime_guide(
            generation=str(generation),
            executor_policy=request.executor_policy,
            imports=[],
            limits=limits,
            eligible_tools=eligible_tools,
            discovery={
                "enabled": True,
                "methods": [
                    "cognis.tools.search",
                    "cognis.tools.describe",
                    "cognis.tools.load",
                ],
                "scope": "effective eligible tool plan",
            },
            exception_types=["ToolExecutionError"],
        )

    @staticmethod
    def _remaining(state: _ExecutionState, wall_seconds: float) -> float:
        if state.wall_started_at is None:
            return wall_seconds
        now = time.monotonic()
        paused = state.paused_seconds - state.wall_paused_before
        if state.pause_started is not None:
            paused += now - state.pause_started
        remaining = wall_seconds - (now - state.wall_started_at - paused)
        if remaining <= 0:
            raise TimeoutError
        return remaining


__all__ = ["ProgramRuntime", "ProgramRuntimeError", "ToolCallHandler", "OutputCallback"]
