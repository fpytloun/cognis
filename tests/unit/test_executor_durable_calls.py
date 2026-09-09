"""Tool executions survive executor websocket reconnects.

An unstable link (mobile executor, NAT, proxy timeout) must not destroy work in
progress or turn a completed command into an unknown outcome. The executor keeps
tool tasks alive across sockets and retains terminal results so the controller
can fetch the real outcome after reconnecting to the same process.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from cognis.executor.runner import ExecutorRunner
from cognis.executor.unary_dedup import is_replay_safe_unary_method
from cognis.models.tool import ExecutorConfig


class _DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _runner() -> ExecutorRunner:
    return ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="token"))


def test_tool_result_fetch_is_replay_safe() -> None:
    assert is_replay_safe_unary_method("tool.result_fetch") is True


@pytest.mark.asyncio
async def test_terminal_tool_result_is_fetchable_after_reconnect() -> None:
    runner = _runner()
    runner._register_call("call-1", "bash", turn_id="turn-1")
    runner._settle_call(
        "call-1",
        "completed",
        result={
            "call_id": "call-1",
            "output": "side effect done",
            "is_error": False,
            "duration_ms": 12,
            "metadata": None,
            "attachments": None,
        },
    )

    # A brand new socket stands in for the post-reconnect connection.
    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "call-1"})

    result = ws.sent[-1]["result"]
    assert result["state"] == "terminal"
    assert result["result"]["output"] == "side effect done"
    assert result["executor_instance_id"] == runner.executor_instance_id


@pytest.mark.asyncio
async def test_running_tool_reports_active_and_unknown_call_reports_unknown() -> None:
    runner = _runner()
    runner._register_call("call-running", "bash", turn_id="turn-1")

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "call-running"})
    assert ws.sent[-1]["result"]["state"] == "active"

    await runner._handle_tool_result_fetch(ws, "rpc-2", {"call_id": "never-dispatched"})
    assert ws.sent[-1]["result"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_durable_tool_task_survives_connection_teardown() -> None:
    runner = _runner()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def _long_tool() -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    task = runner._create_durable_tool_task(_long_tool(), call_id="call-1", msg_id="rpc-1")
    runner._active_calls["call-1"] = task
    await asyncio.wait_for(started.wait(), timeout=1)

    # Connection teardown must not cancel tool work.
    await runner._cancel_connection_handler_tasks()
    assert task.cancelled() is False

    await asyncio.wait_for(finished.wait(), timeout=1)
    await task
    await asyncio.sleep(0)  # let the done-callback drain the registry
    assert "call-1" not in runner._durable_tool_tasks


@pytest.mark.asyncio
async def test_tool_cancel_still_stops_a_durable_task() -> None:
    runner = _runner()
    started = asyncio.Event()

    async def _long_tool() -> None:
        started.set()
        await asyncio.sleep(30)

    task = runner._create_durable_tool_task(_long_tool(), call_id="call-1", msg_id="rpc-1")
    runner._active_calls["call-1"] = task
    await asyncio.wait_for(started.wait(), timeout=1)

    runner._active_calls["call-1"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() is True


def test_retained_results_are_bounded() -> None:
    from cognis.executor.runner import _MAX_RETAINED_CALL_RESULTS

    runner = _runner()
    for index in range(_MAX_RETAINED_CALL_RESULTS + 10):
        runner._register_call(f"call-{index}", "bash")
        runner._settle_call(
            f"call-{index}",
            "completed",
            result={"call_id": f"call-{index}", "output": "x", "is_error": False},
        )

    assert len(runner._completed_call_results) == _MAX_RETAINED_CALL_RESULTS
    assert "call-0" not in runner._completed_call_results


@pytest.mark.asyncio
async def test_evicted_result_never_claims_the_call_did_not_run() -> None:
    """Safety: an evicted result must not look like proof the tool never ran."""

    from cognis.executor.runner import _MAX_RETAINED_CALL_RESULTS

    runner = _runner()
    runner._register_call("call-target", "bash")
    runner._settle_call(
        "call-target",
        "completed",
        result={"call_id": "call-target", "output": "side effect", "is_error": False},
    )
    # Push the retained result out of the bounded window.
    for index in range(_MAX_RETAINED_CALL_RESULTS + 5):
        runner._register_call(f"filler-{index}", "bash")
        runner._settle_call(
            f"filler-{index}",
            "completed",
            result={"call_id": f"filler-{index}", "output": "x", "is_error": False},
        )
    assert "call-target" not in runner._completed_call_results

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "call-target"})

    # It ran; the result is simply gone. It must never be reported as unknown.
    assert ws.sent[-1]["result"]["state"] == "terminal_result_unavailable"


@pytest.mark.asyncio
async def test_truncated_accept_history_reports_indeterminate() -> None:
    runner = _runner()
    runner._accepted_call_ids_overflowed = True

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "long-forgotten"})

    assert ws.sent[-1]["result"]["state"] == "indeterminate"


@pytest.mark.asyncio
async def test_cancel_before_start_does_not_leak_a_running_call() -> None:
    """A task cancelled before its body runs must still settle its registries."""

    runner = _runner()
    runner._register_call("call-1", "bash", turn_id="turn-1")

    async def _never_starts() -> None:  # pragma: no cover - cancelled first
        await asyncio.sleep(0)

    task = runner._create_durable_tool_task(_never_starts(), call_id="call-1", msg_id="rpc-1")
    runner._active_calls["call-1"] = task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert "call-1" not in runner._call_records
    assert "call-1" not in runner._active_calls
    assert "call-1" not in runner._durable_tool_tasks

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-2", {"call_id": "call-1"})
    assert ws.sent[-1]["result"]["state"] != "active"


@pytest.mark.asyncio
async def test_configure_can_reapply_the_current_version() -> None:
    """A reconnect may re-send the same version; rejecting it would deadlock."""

    runner = _runner()
    ws = _DummyWebSocket()
    await runner._handle_configure(ws, "cfg-1", {"config_version": 3, "config": {}})
    assert runner._config_version == 3
    assert runner._configured is True
    ws.sent.clear()

    await runner._handle_configure(ws, "cfg-2", {"config_version": 3, "config": {}})

    assert "error" not in ws.sent[-1]
    assert ws.sent[-1]["result"]["applied_version"] == 3

    # An older version is still rejected as stale.
    ws.sent.clear()
    await runner._handle_configure(ws, "cfg-3", {"config_version": 2, "config": {}})
    assert ws.sent[-1]["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_oversized_result_is_not_retained_but_still_proves_it_ran() -> None:
    from cognis.executor.runner import _MAX_RETAINED_RESULT_BYTES

    runner = _runner()
    runner._register_call("call-big", "bash")
    runner._settle_call(
        "call-big",
        "completed",
        result={
            "call_id": "call-big",
            "output": "x" * (_MAX_RETAINED_RESULT_BYTES + 1),
            "is_error": False,
        },
    )

    assert "call-big" not in runner._completed_call_results

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "call-big"})
    # Dropping the payload is safe, claiming it never ran would not be.
    assert ws.sent[-1]["result"]["state"] == "terminal_result_unavailable"


def test_retained_results_respect_aggregate_byte_ceiling() -> None:
    from cognis.executor.runner import _MAX_RETAINED_RESULTS_TOTAL_BYTES

    runner = _runner()
    chunk = "y" * 1_000_000
    for index in range(60):
        runner._register_call(f"call-{index}", "bash")
        runner._settle_call(
            f"call-{index}",
            "completed",
            result={"call_id": f"call-{index}", "output": chunk, "is_error": False},
        )

    assert sum(runner._retained_result_bytes.values()) <= _MAX_RETAINED_RESULTS_TOTAL_BYTES
    assert set(runner._retained_result_bytes) == set(runner._completed_call_results)


def _registered_stub():
    """Minimal registered-tool shape used by the recovery finalization path."""

    from types import SimpleNamespace

    return SimpleNamespace(
        definition=SimpleNamespace(
            max_result_size=1000,
            content_trust="untrusted",
            category="shell",
            source=SimpleNamespace(type="executor"),
        )
    )


def _recovery_router(*, persist_raises: bool = False):
    """Build a ToolRouter stub exercising only the recovery finalization path."""

    from cognis.core.tool_router import ToolRouter
    from cognis.models.credential import CredentialAccessError
    from cognis.models.tool import ToolResult

    router = ToolRouter.__new__(ToolRouter)
    calls: list[str] = []

    async def _persist(result, session, agent):
        del session, agent
        calls.append("persist")
        if persist_raises:
            raise CredentialAccessError("credential_unavailable", "credential unavailable")
        metadata = dict(result.metadata or {})
        metadata.pop("browser_auth_state", None)
        metadata["credential_persisted"] = True
        return result.model_copy(update={"metadata": metadata})

    async def _materialize(result, session, tool_name):
        del session, tool_name
        calls.append("materialize")
        return result

    async def _postprocess(result, tool_call, session):
        del tool_call, session
        calls.append("postprocess")
        return result

    def _sanitize(tool_name, result, max_size, **kwargs):
        del tool_name, max_size, kwargs
        calls.append("sanitize")
        return result.model_copy(update={"output": f"[wrapped]{result.output}"})

    def _credential_error(exc):
        return ToolResult(output=f"credential error: {exc}", is_error=True)

    router._persist_browser_auth_state_if_needed = _persist  # type: ignore[method-assign]
    router._materialize_inline_attachments = _materialize  # type: ignore[method-assign]
    router._postprocess_tool_result = _postprocess  # type: ignore[method-assign]
    router._sanitize_result = _sanitize  # type: ignore[method-assign]
    router._credential_error_result = _credential_error  # type: ignore[method-assign]
    return router, calls


@pytest.mark.asyncio
async def test_recovered_result_crosses_the_full_post_execution_pipeline() -> None:
    """Recovered results must be treated exactly like directly returned ones."""

    from types import SimpleNamespace

    from cognis.models.tool import ToolCall, ToolResult

    router, calls = _recovery_router()
    registered = _registered_stub()

    finalized = await router.finalize_recovered_executor_result(
        result=ToolResult(
            output="secret page",
            is_error=False,
            metadata={
                "browser_auth_state": {"cookies": "sensitive"},
                "evaluation": {"decision": "allow"},
            },
        ),
        tool_call=ToolCall(call_id="call-1", name="browser_login", arguments={}),
        registered_tool=registered,
        session=SimpleNamespace(user_email="u@example.com"),
        agent=SimpleNamespace(agent_id="agent-1"),
    )

    assert calls == ["persist", "materialize", "postprocess", "sanitize"]
    # Credential state encrypted away, output sanitized, audit metadata kept.
    assert "browser_auth_state" not in (finalized.metadata or {})
    assert finalized.metadata["credential_persisted"] is True
    assert finalized.metadata["evaluation"] == {"decision": "allow"}
    assert finalized.output == "[wrapped]secret page"


@pytest.mark.asyncio
async def test_recovered_credential_failure_keeps_audit_metadata_and_sanitizes() -> None:
    from types import SimpleNamespace

    from cognis.models.tool import ToolCall, ToolResult

    router, calls = _recovery_router(persist_raises=True)
    registered = _registered_stub()

    finalized = await router.finalize_recovered_executor_result(
        result=ToolResult(
            output="secret page",
            is_error=False,
            metadata={"evaluation": {"decision": "allow"}},
        ),
        tool_call=ToolCall(call_id="call-1", name="browser_login", arguments={}),
        registered_tool=registered,
        session=SimpleNamespace(user_email="u@example.com"),
        agent=SimpleNamespace(agent_id="agent-1"),
    )

    assert finalized.is_error is True
    assert "sanitize" in calls
    # Controller-owned audit metadata survives the failure path.
    assert finalized.metadata["evaluation"] == {"decision": "allow"}
    assert finalized.output.startswith("[wrapped]credential error")


@pytest.mark.asyncio
async def test_retained_result_is_physically_removed_after_expiry() -> None:
    """Sensitive payloads must not outlive the reconnect window, even when idle."""

    runner = _runner()
    runner._register_call("call-1", "browser_login")
    runner._settle_call(
        "call-1",
        "completed",
        result={
            "call_id": "call-1",
            "output": "logged in",
            "is_error": False,
            "metadata": {"browser_auth_state": {"cookies": "sensitive"}},
        },
    )
    assert "call-1" in runner._completed_call_results

    # Expire the retention window without any other activity.
    runner._retained_result_expiry["call-1"] = 0.0

    # A heartbeat snapshot is the periodic tick that always runs while alive.
    runner._build_call_snapshot()

    assert "call-1" not in runner._completed_call_results
    assert "call-1" not in runner._retained_result_bytes
    assert "call-1" not in runner._retained_result_expiry

    ws = _DummyWebSocket()
    await runner._handle_tool_result_fetch(ws, "rpc-1", {"call_id": "call-1"})
    # Expiry must never be reported as proof the call never ran.
    assert ws.sent[-1]["result"]["state"] == "terminal_result_unavailable"


@pytest.mark.asyncio
async def test_retained_result_expires_even_while_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long outage produces no heartbeats, so expiry must not depend on them."""

    runner = _runner()
    runner._register_call("call-1", "browser_login")
    runner._settle_call(
        "call-1",
        "completed",
        result={
            "call_id": "call-1",
            "output": "logged in",
            "is_error": False,
            "metadata": {"browser_auth_state": {"cookies": "sensitive"}},
        },
    )
    runner._retained_result_expiry["call-1"] = 0.0

    attempts = 0
    observed_while_running: list[bool] = []

    async def _always_fails() -> None:
        nonlocal attempts
        attempts += 1
        # Observed inside the reconnect loop, before run()'s shutdown cleanup.
        observed_while_running.append("call-1" in runner._completed_call_results)
        if attempts >= 2:
            runner._running = False
        raise ConnectionError("network unavailable")

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner, "_connect_and_serve", _always_fails)
    monkeypatch.setattr("cognis.executor.runner.asyncio.sleep", _no_sleep)

    await runner.run()

    # Expired before the first connection attempt, while still disconnected.
    assert observed_while_running == [False, False]


def test_accepted_call_ledger_is_bounded_and_flags_overflow() -> None:
    from cognis.executor.runner import _MAX_ACCEPTED_CALL_IDS

    runner = _runner()
    assert runner._accepted_call_ids_overflowed is False
    for index in range(_MAX_ACCEPTED_CALL_IDS + 5):
        runner._register_call(f"call-{index}", "bash")

    assert len(runner._accepted_call_ids) == _MAX_ACCEPTED_CALL_IDS
    assert runner._accepted_call_ids_overflowed is True


def test_ready_params_report_fast_resume_state() -> None:
    runner = _runner()

    cold = runner._build_ready_params(None)
    assert cold["configured"] is False
    assert cold["config_version"] == 0

    runner._configured = True
    runner._config_version = 7
    runner._runtime_state = "active"
    warm = runner._build_ready_params(None)
    assert warm["configured"] is True
    assert warm["config_version"] == 7
    assert warm["runtime_state"] == "active"


def test_fast_resume_requires_configured_executor_at_known_version() -> None:
    from cognis.api.executor_ws import _fast_resume_runtime_state

    assert _fast_resume_runtime_state({}) is None
    assert (
        _fast_resume_runtime_state(
            {"configured": False, "config_version": 7, "runtime_state": "active"}
        )
        is None
    )
    assert (
        _fast_resume_runtime_state(
            {"configured": True, "config_version": 0, "runtime_state": "active"}
        )
        is None
    )
    assert (
        _fast_resume_runtime_state(
            {"configured": True, "config_version": 7, "runtime_state": "offline"}
        )
        is None
    )
    assert (
        _fast_resume_runtime_state(
            {"configured": True, "config_version": 7, "runtime_state": "active"}
        )
        == "active"
    )
    assert (
        _fast_resume_runtime_state(
            {"configured": True, "config_version": 7, "runtime_state": "degraded"}
        )
        == "degraded"
    )
