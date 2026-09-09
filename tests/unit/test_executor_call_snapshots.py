"""Tests for executor call-liveness snapshots."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cognis.api import executor_runtime
from cognis.executor.runner import ExecutorRunner
from cognis.models.executor_calls import (
    EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE,
    normalize_executor_call_snapshot,
)
from cognis.models.tool import ExecutorConfig, ToolResult


def _snapshot(instance_id: str, seq: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "executor_instance_id": instance_id,
        "snapshot_seq": seq,
        "complete": True,
        "active": [],
        "recent_terminal": [],
    }


def test_registry_lifecycle_sequence_redaction_ttl_and_capacity() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="token"))
    runner._register_call(
        "call-1",
        "read",
        turn_id="turn-1",
    )
    running = runner._build_call_snapshot()
    assert running["snapshot_seq"] == 1
    assert running["active"] == [
        {
            "call_id": "call-1",
            "tool_name": "read",
            "turn_id": "turn-1",
            "started_at": running["active"][0]["started_at"],
            "updated_at": running["active"][0]["updated_at"],
            "state": "running",
        }
    ]
    assert not {"arguments", "output", "command", "path", "pid", "rss"} & set(running["active"][0])

    runner._settle_call("call-1", "completed")
    terminal = runner._build_call_snapshot()
    assert terminal["snapshot_seq"] == 2
    assert terminal["active"] == []
    assert terminal["recent_terminal"][0]["state"] == "completed"

    runner._terminal_call_records["call-1"]["terminal_at"] = (
        datetime.now(UTC) - timedelta(minutes=11)
    ).isoformat()
    assert runner._build_call_snapshot()["recent_terminal"] == []

    for index in range(257):
        runner._register_call(f"call-{index}", "read")
        runner._settle_call(f"call-{index}", "failed")
    capped = runner._build_call_snapshot()
    assert len(capped["recent_terminal"]) == 256
    assert capped["recent_terminal"][0]["call_id"] == "call-1"


def test_ready_snapshot_uses_process_instance_and_monotonic_sequence() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="token"))

    ready = runner._build_ready_params(None)
    heartbeat = runner._build_call_snapshot()

    assert ready["call_snapshot"]["executor_instance_id"] == runner.executor_instance_id
    assert ready["call_snapshot"]["snapshot_seq"] == 1
    assert heartbeat["snapshot_seq"] == 2
    assert ready["call_snapshot"]["complete"] is True


def test_local_snapshot_bounds_active_calls_and_marks_it_incomplete() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="token"))
    for index in range(EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE + 1):
        runner._register_call(f"call-{index}", "read")

    snapshot = runner._build_call_snapshot()

    assert len(runner._call_records) == EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE + 1
    assert len(snapshot["active"]) == EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE
    assert snapshot["complete"] is False
    assert snapshot["active"][0]["call_id"] == "call-0"
    assert snapshot["active"][-1]["call_id"] == (f"call-{EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE - 1}")


def test_inbound_snapshot_rejects_oversized_active_list() -> None:
    payload = _snapshot("instance-a", 1)
    now = datetime.now(UTC).isoformat()
    payload["active"] = [
        {
            "call_id": f"call-{index}",
            "tool_name": "read",
            "started_at": now,
            "updated_at": now,
            "state": "running",
        }
        for index in range(EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE + 1)
    ]
    payload["complete"] = False

    assert normalize_executor_call_snapshot(payload) is None


@pytest.mark.asyncio
async def test_tool_cancellation_creates_cancelled_tombstone() -> None:
    runner = ExecutorRunner(ExecutorConfig(executor_id="remote", controller_token="token"))
    runner._configured = True
    runner._runtime_state = "active"
    started = asyncio.Event()

    async def _handler(_: dict[str, object], __: object) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult(output="unreachable")

    runner._tool_handlers["read"] = _handler
    ws = SimpleNamespace()

    async def _send_rpc_result(*_: object, **__: object) -> None:
        return None

    runner._send_rpc_result = _send_rpc_result  # type: ignore[method-assign]
    task = asyncio.create_task(
        runner._handle_tool_execute(
            ws,
            "rpc-1",
            {
                "call_id": "call-1",
                "tool_name": "read",
                "arguments": {},
                "runtime_metadata": {"turn_id": "turn-1"},
            },
        )
    )
    await started.wait()
    task.cancel()
    await task

    snapshot = runner._build_call_snapshot()
    assert snapshot["active"] == []
    assert snapshot["recent_terminal"][0]["state"] == "cancelled"


class _Session:
    async def commit(self) -> None:
        return None


class _SessionContext:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_controller_rejects_duplicate_out_of_order_and_wrong_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(executor_id="remote-1", executor_instance_id="instance-a")
    row = SimpleNamespace(executor_id="remote-1", runtime_metadata={})
    updates: list[dict[str, object]] = []
    provider = SimpleNamespace(get_connection=lambda _: connection)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: _SessionContext(),
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=provider)),
        )
    )

    async def _get_row(*_: object) -> SimpleNamespace:
        return row

    async def _update(_session: object, _executor_id: str, **values: object) -> SimpleNamespace:
        updates.append(values)
        row.runtime_metadata = values["runtime_metadata"]
        return row

    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_row)
    monkeypatch.setattr(executor_runtime, "update_executor_runtime_state", _update)

    assert await executor_runtime.persist_executor_call_snapshot(
        app, "remote-1", _snapshot("instance-a", 2), connection=connection
    )
    assert not await executor_runtime.persist_executor_call_snapshot(
        app, "remote-1", _snapshot("instance-a", 2), connection=connection
    )
    assert not await executor_runtime.persist_executor_call_snapshot(
        app, "remote-1", _snapshot("instance-a", 1), connection=connection
    )
    assert not await executor_runtime.persist_executor_call_snapshot(
        app, "remote-1", _snapshot("instance-b", 2), connection=connection
    )
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_controller_accepts_instance_replacement_without_resource_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(executor_id="remote-1", executor_instance_id="instance-b")
    row = SimpleNamespace(
        executor_id="remote-1",
        runtime_metadata={
            "call_snapshot": _snapshot("instance-a", 500),
            "resource_snapshot_received_at": datetime.now(UTC).isoformat(),
        },
    )
    updates: list[dict[str, object]] = []
    provider = SimpleNamespace(get_connection=lambda _: connection)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: _SessionContext(),
            providers=SimpleNamespace(executor=SimpleNamespace(websocket=provider)),
        )
    )

    async def _get_row(*_: object) -> SimpleNamespace:
        return row

    async def _update(_session: object, _executor_id: str, **values: object) -> SimpleNamespace:
        updates.append(values)
        return row

    monkeypatch.setattr(executor_runtime, "get_executor_row", _get_row)
    monkeypatch.setattr(executor_runtime, "update_executor_runtime_state", _update)

    accepted = await executor_runtime.persist_executor_call_snapshot(
        app,
        "remote-1",
        _snapshot("instance-b", 1),
        connection=connection,
    )

    assert accepted is True
    assert len(updates) == 1
    metadata = updates[0]["runtime_metadata"]
    assert metadata["call_snapshot"]["executor_instance_id"] == "instance-b"
    assert "last_observed_at" not in updates[0]
