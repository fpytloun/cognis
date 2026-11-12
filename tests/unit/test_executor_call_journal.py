from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from cognis.executor.call_journal import CallJournal, JournalCapacityError
from cognis.executor.runner import ExecutorRunner
from cognis.executor_transport import JSONMessageReceiver
from cognis.models.tool import ExecutorConfig


def _call() -> dict:
    return {
        "call_id": uuid4().hex,
        "tool_name": "bash",
        "arguments": {"command": "printf 'receipt'"},
        "execution_scope_id": uuid4().hex,
        "runtime_metadata": {"session_id": uuid4().hex, "turn_id": uuid4().hex},
    }


class _WireSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["duplicate", "not_ready"])
async def test_admitted_rejections_remain_terminal_after_runner_replacement(
    tmp_path, monkeypatch, rejection
):
    path = tmp_path / "calls.sqlite3"
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=uuid4().hex, controller_token=uuid4().hex),
        call_journal=CallJournal(path),
    )
    params = _call()
    if rejection == "duplicate":
        original = {**params, "call_id": uuid4().hex}
        assert runner._same_turn_tool_call_deduplicator.original_call_id(original) is None
    socket = _WireSocket()

    async def receive(instance, wire):
        receiver = JSONMessageReceiver()
        try:
            await instance._receive_messages(wire, receiver)
        finally:
            await receiver.close()

    async def incoming(_socket, _receiver):
        yield json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tool.execute",
                "id": uuid4().hex,
                "params": params,
            }
        )

    monkeypatch.setattr(runner, "_transport_messages", incoming)
    await receive(runner, socket)
    await asyncio.gather(*list(runner._durable_tool_tasks.values()))
    response = json.loads(socket.messages[-1])["result"]
    assert response["is_error"] is True
    assert (
        "duplicate_tool_call_same_turn" if rejection == "duplicate" else "not configured"
    ) in response["output"]

    replacement = ExecutorRunner(runner.config, call_journal=CallJournal(path))
    assert replacement.executor_instance_id != runner.executor_instance_id
    recovered_socket = _WireSocket()

    async def fetch(_socket, _receiver):
        yield json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tool.result_fetch",
                "id": uuid4().hex,
                "params": {"call_id": params["call_id"], "scope": params["runtime_metadata"]},
            }
        )

    monkeypatch.setattr(replacement, "_transport_messages", fetch)
    await receive(replacement, recovered_socket)
    report = json.loads(recovered_socket.messages[-1])["result"]
    assert report["state"] == "terminal"
    assert report["result"] == response
    assert report["accepted_executor_instance_id"] == runner.executor_instance_id


@pytest.mark.asyncio
async def test_conflicting_rpc_rejection_cannot_settle_an_inflight_original(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3")
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=uuid4().hex, controller_token=uuid4().hex),
        call_journal=journal,
    )
    original = _call()
    instance = runner.executor_instance_id
    await journal.accept(original, instance)
    runner._register_call(
        original["call_id"], original["tool_name"], turn_id=original["runtime_metadata"]["turn_id"]
    )
    conflict = {**original, "arguments": {"command": "printf 'conflict'"}}
    with pytest.raises(ValueError, match="Conflicting"):
        await journal.accept(conflict, instance)
    socket = _WireSocket()
    await runner._send_rpc_result(
        socket,
        uuid4().hex,
        {
            "call_id": original["call_id"],
            "is_error": True,
            "output": "Rejected conflicting duplicate before execution",
        },
    )
    assert (await journal.fetch(original["call_id"], instance))["state"] == "indeterminate"
    assert original["call_id"] in runner._call_records
    with pytest.raises(ValueError, match="accepting execution"):
        await runner._send_tool_execution_result(
            socket,
            uuid4().hex,
            conflict,
            {"call_id": original["call_id"], "output": "wrong result", "is_error": False},
        )
    real_result = {"call_id": original["call_id"], "output": "real effect", "is_error": False}
    await runner._send_tool_execution_result(socket, uuid4().hex, original, real_result)
    assert (await journal.fetch(original["call_id"], instance))["result"] == real_result


@pytest.mark.asyncio
async def test_acknowledged_payload_expiry_keeps_nonreplayable_tombstone(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3")
    call, instance = _call(), uuid4().hex
    await journal.accept(call, instance)
    await journal.complete(call["call_id"], {"output": "settled"})
    await journal.acknowledge(call["call_id"])
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE receipts SET settled_at=1 WHERE call_id=?", (call["call_id"],))
    report = await journal.fetch(call["call_id"], instance)
    assert report["state"] == "terminal_result_unavailable"
    assert not await journal.accept(call, instance)


@pytest.mark.asyncio
async def test_receipt_survives_process_replacement_and_acknowledgement(tmp_path: Path) -> None:
    path = tmp_path / "private" / "calls.sqlite3"
    call = _call()
    origin, replacement = uuid4().hex, uuid4().hex
    journal = CallJournal(path)
    assert await journal.accept(call, origin)
    result = {"output": "completed", "is_error": False, "attachments": []}
    await journal.complete(call["call_id"], result)
    restarted = CallJournal(path)
    report = await restarted.fetch(call["call_id"], replacement)
    assert report is not None
    assert report["accepted_executor_instance_id"] == origin
    assert report["executor_instance_id"] == replacement
    assert report["durable_receipt"]
    assert report["result"] == result
    assert not await restarted.accept(call, replacement)
    await restarted.acknowledge(call["call_id"])
    await restarted.acknowledge(call["call_id"])
    assert (await restarted.fetch(call["call_id"], replacement))["result"] == result
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_acceptance_without_terminal_is_indeterminate_after_restart(tmp_path: Path) -> None:
    call = _call()
    journal = CallJournal(tmp_path / "calls.sqlite3")
    assert await journal.accept(call, uuid4().hex)
    restarted = CallJournal(journal.path)
    report = await restarted.fetch(call["call_id"], uuid4().hex)
    assert report["state"] == "indeterminate"
    assert not await restarted.accept(call, uuid4().hex)


@pytest.mark.asyncio
async def test_concurrent_identical_admissions_have_one_owner(tmp_path: Path) -> None:
    call = _call()
    journal = CallJournal(tmp_path / "calls.sqlite3")
    instance = uuid4().hex
    results = await asyncio.gather(*(journal.accept(call, instance) for _ in range(4)))
    assert results.count(True) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["arguments", "execution_scope_id", "runtime_metadata"])
async def test_conflicting_execution_identity_is_rejected(tmp_path: Path, field: str) -> None:
    call = _call()
    journal = CallJournal(tmp_path / "calls.sqlite3")
    assert await journal.accept(call, uuid4().hex)
    replacement = _call()
    conflict = {**call, field: replacement[field]}
    if field == "arguments":
        conflict[field] = {"command": "printf 'different'"}
    with pytest.raises(ValueError, match="Conflicting"):
        await journal.accept(conflict, uuid4().hex)


@pytest.mark.asyncio
async def test_pressure_rejects_new_calls_without_evicting_old_receipts(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=1)
    call = _call()
    await journal.accept(call, uuid4().hex)
    await journal.complete(call["call_id"], {"output": "preserved", "is_error": False})
    with pytest.raises(OSError, match="full"):
        await journal.accept(_call(), uuid4().hex)
    assert (await journal.fetch(call["call_id"], uuid4().hex))["result"]["output"] == "preserved"


@pytest.mark.asyncio
async def test_terminal_receipt_is_immutable_and_cannot_exist_without_acceptance(
    tmp_path: Path,
) -> None:
    journal = CallJournal(tmp_path / "calls.sqlite3")
    call = _call()
    with pytest.raises(ValueError, match="acceptance"):
        await journal.complete(call["call_id"], {"output": "unaccepted"})
    await journal.accept(call, uuid4().hex)
    await journal.complete(call["call_id"], {"output": "first"})
    await journal.complete(call["call_id"], {"output": "first"})
    with pytest.raises(ValueError, match="Conflicting"):
        await journal.complete(call["call_id"], {"output": "second"})


@pytest.mark.asyncio
async def test_journal_does_not_store_arguments(tmp_path: Path) -> None:
    journal = CallJournal(tmp_path / "calls.sqlite3")
    call = _call()
    call["arguments"]["sensitive"] = uuid4().hex
    await journal.accept(call, uuid4().hex)
    assert call["arguments"]["sensitive"].encode() not in journal.path.read_bytes()
    assert json.dumps(call["arguments"]).encode() not in journal.path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["missing", "other_session", "other_turn"])
async def test_remote_lookup_and_ack_are_bound_to_session_and_turn(
    tmp_path: Path, scope_kind
) -> None:
    journal = CallJournal(tmp_path / "calls.sqlite3")
    call = _call()
    instance = uuid4().hex
    await journal.accept(call, instance)
    await journal.complete(call["call_id"], {"output": "private outcome"})
    scope = dict(call["runtime_metadata"])
    if scope_kind == "missing":
        scope = None
    else:
        scope["session_id" if scope_kind == "other_session" else "turn_id"] = uuid4().hex
    report = await journal.fetch(call["call_id"], instance, scope=scope, require_scope=True)
    assert report["state"] == "indeterminate"
    assert "result" not in report
    assert not await journal.acknowledge(call["call_id"], scope=scope, require_scope=True)
    assert (
        await journal.fetch(
            call["call_id"], instance, scope=call["runtime_metadata"], require_scope=True
        )
    )["result"]["output"] == "private outcome"


@pytest.mark.asyncio
async def test_byte_pressure_reclaims_only_acknowledged_payload_and_preserves_identity(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_bytes=2 * 1024 * 1024)
    original, instance = _call(), uuid4().hex
    await journal.accept(original, instance)
    result = {"output": "évidence", "is_error": False}
    await journal.complete(original["call_id"], result)
    next_call = _call()
    with pytest.raises(JournalCapacityError) as error:
        await journal.accept(next_call, instance)
    assert error.value.capacity["unacknowledged_calls"] == 1
    assert error.value.capacity["unacknowledged_bytes"] > 0
    assert (await journal.fetch(original["call_id"], instance))["result"] == result
    assert await journal.acknowledge(
        original["call_id"], scope=original["runtime_metadata"], require_scope=True
    )
    assert await journal.accept(next_call, instance)

    restarted = CallJournal(journal.path, max_bytes=journal.max_bytes)
    report = await restarted.fetch(original["call_id"], uuid4().hex)
    assert report["state"] == "terminal_result_unavailable"
    assert "result" not in report
    assert not await restarted.accept(original, uuid4().hex)
    with pytest.raises(ValueError, match="Conflicting"):
        await restarted.accept({**original, "arguments": {}}, uuid4().hex)
    with pytest.raises(ValueError, match="reclaimed"):
        await restarted.complete(original["call_id"], result)
    assert await restarted.acknowledge(original["call_id"])


@pytest.mark.asyncio
async def test_lifetime_tombstones_do_not_consume_retained_call_capacity(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=1)
    originals = []
    for _ in range(8):
        call, instance = _call(), uuid4().hex
        assert await journal.accept(call, instance)
        await journal.complete(call["call_id"], {"output": "settled"})
        assert await journal.acknowledge(call["call_id"])
        originals.append(call)
    for call in originals:
        assert not await journal.accept(call, uuid4().hex)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == len(originals)
        assert db.execute("SELECT count(result) FROM receipts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_pressure_reclaims_oldest_acknowledged_payload_only(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=2)
    calls = [_call(), _call()]
    for index, call in enumerate(calls):
        await journal.accept(call, uuid4().hex)
        await journal.complete(call["call_id"], {"output": str(index)})
        await journal.acknowledge(call["call_id"])
    with sqlite3.connect(journal.path) as db:
        # Both are inside the grace period; order must be settlement age, not call ID.
        db.execute(
            "UPDATE receipts SET settled_at=cast(strftime('%s','now') AS REAL)-10 WHERE call_id=?",
            (calls[0]["call_id"],),
        )
    await journal.accept(_call(), uuid4().hex)
    assert (await journal.fetch(calls[0]["call_id"], uuid4().hex))["state"] == (
        "terminal_result_unavailable"
    )
    assert (await journal.fetch(calls[1]["call_id"], uuid4().hex))["result"]["output"] == "1"


@pytest.mark.asyncio
async def test_pending_receipts_remain_reserved_and_cannot_be_acknowledged(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_bytes=2 * 1024 * 1024)
    call = _call()
    await journal.accept(call, uuid4().hex)
    assert not await journal.acknowledge(call["call_id"])
    with pytest.raises(JournalCapacityError):
        await journal.accept(_call(), uuid4().hex)
    assert (await journal.fetch(call["call_id"], uuid4().hex))["state"] == "indeterminate"


@pytest.mark.asyncio
async def test_concurrent_pressure_admission_has_one_owner_and_preserves_tombstone(tmp_path):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=1)
    original, instance = _call(), uuid4().hex
    await journal.accept(original, instance)
    await journal.complete(original["call_id"], {"output": "settled"})
    await journal.acknowledge(original["call_id"])
    contenders = [_call(), _call()]
    outcomes = await asyncio.gather(
        *(journal.accept(call, instance) for call in contenders), return_exceptions=True
    )
    assert outcomes.count(True) == 1
    assert sum(isinstance(outcome, JournalCapacityError) for outcome in outcomes) == 1
    assert not await journal.accept(original, instance)


def test_journal_environment_limits_are_explicit_and_validated(tmp_path, monkeypatch):
    monkeypatch.delenv("COGNIS_EXECUTOR_RECEIPT_MAX_BYTES", raising=False)
    monkeypatch.delenv("COGNIS_EXECUTOR_RECEIPT_MAX_CALLS", raising=False)
    path = tmp_path / "calls.sqlite3"
    assert CallJournal.from_environment(path).max_bytes == 1024**3
    monkeypatch.setenv("COGNIS_EXECUTOR_RECEIPT_MAX_BYTES", str(4 * 1024 * 1024))
    monkeypatch.setenv("COGNIS_EXECUTOR_RECEIPT_MAX_CALLS", "5")
    configured = CallJournal.from_environment(path)
    assert configured.max_bytes == 4 * 1024 * 1024
    assert configured.max_calls == 5
    for invalid in ("0", "-1", "not-a-number"):
        monkeypatch.setenv("COGNIS_EXECUTOR_RECEIPT_MAX_CALLS", invalid)
        with pytest.raises(ValueError):
            CallJournal.from_environment(path)
    monkeypatch.setenv("COGNIS_EXECUTOR_RECEIPT_MAX_CALLS", "5")
    monkeypatch.setenv("COGNIS_EXECUTOR_RECEIPT_MAX_BYTES", "1")
    with pytest.raises(ValueError):
        CallJournal.from_environment(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capacity", "sqlite", "filesystem", "identity_conflict"])
async def test_admission_failure_is_actionable_logged_and_never_executes(
    tmp_path, monkeypatch, caplog, failure
):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=1)
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=uuid4().hex, controller_token=uuid4().hex),
        call_journal=journal,
    )
    secret = uuid4().hex
    if failure == "capacity":
        await journal.accept(_call(), runner.executor_instance_id)
    else:

        async def reject(*args):
            if failure == "sqlite":
                raise sqlite3.OperationalError(secret)
            if failure == "filesystem":
                raise OSError(28, secret)
            raise ValueError(secret)

        monkeypatch.setattr(journal, "accept", reject)

    call, socket = _call(), _WireSocket()
    call["arguments"]["private"] = secret

    async def incoming(_socket, _receiver):
        yield json.dumps(
            {"jsonrpc": "2.0", "method": "tool.execute", "id": uuid4().hex, "params": call}
        )

    monkeypatch.setattr(runner, "_transport_messages", incoming)
    receiver = JSONMessageReceiver()
    try:
        await runner._receive_messages(socket, receiver)
    finally:
        await receiver.close()
    result = json.loads(socket.messages[-1])["result"]
    assert result["is_error"]
    assert result["metadata"]["reason"] == failure
    assert result["metadata"]["delivery_state"] == "not_sent"
    assert result["metadata"]["code"] == "executor_receipt_admission_failed"
    assert not runner._durable_tool_tasks
    assert await journal.fetch(call["call_id"], runner.executor_instance_id) is None
    assert f"reason={failure}" in caplog.text
    assert secret not in caplog.text
    assert secret not in json.dumps(result)
    if failure == "capacity":
        assert result["metadata"]["max_calls"] == 1
        assert "unacknowledged" in result["output"]
    elif failure == "filesystem":
        assert result["metadata"]["storage_error_code"] == 28


@pytest.mark.asyncio
async def test_legacy_journal_reclaims_multiple_batches_and_keeps_its_identity(tmp_path):
    path, journal_id, instance = tmp_path / "calls.sqlite3", uuid4().hex, uuid4().hex
    originals = [_call() for _ in range(130)]
    # Pre-upgrade private schema, deliberately without the new retained index.
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE receipts (call_id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "instance_id TEXT NOT NULL, scope TEXT NOT NULL, result TEXT, "
            "acknowledged INTEGER NOT NULL DEFAULT 0, settled_at REAL)"
        )
        db.execute("CREATE TABLE identity (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO identity VALUES (?)", (journal_id,))
        for call in originals:
            digest, scope = CallJournal._identity(call)
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,1,cast(strftime('%s','now') AS REAL))",
                (call["call_id"], digest, instance, scope, json.dumps({"output": "settled"})),
            )
    journal = CallJournal(path, max_calls=1)
    assert (await journal.fetch(originals[0]["call_id"], instance))["journal_id"] == journal_id
    assert await journal.accept(_call(), uuid4().hex)
    assert (await journal.fetch(originals[0]["call_id"], instance))["journal_id"] == journal_id
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 131
        assert db.execute("SELECT count(result) FROM receipts").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='receipts_retained'"
            ).fetchone()[0]
            == 1
        )
    assert not await journal.accept(originals[0], instance)
    assert not await journal.accept(originals[-1], instance)


@pytest.mark.asyncio
async def test_corrupt_journal_is_reported_without_dispatch_or_content_exposure(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / "calls.sqlite3"
    private_bytes = uuid4().hex.encode()
    path.write_bytes(private_bytes)
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=uuid4().hex, controller_token=uuid4().hex),
        call_journal=CallJournal(path),
    )
    call, socket, receiver = _call(), _WireSocket(), JSONMessageReceiver()

    async def incoming(_socket, _receiver):
        yield json.dumps(
            {"jsonrpc": "2.0", "method": "tool.execute", "id": uuid4().hex, "params": call}
        )

    monkeypatch.setattr(runner, "_transport_messages", incoming)
    try:
        await runner._receive_messages(socket, receiver)
    finally:
        await receiver.close()
    result = json.loads(socket.messages[-1])["result"]
    assert result["metadata"]["reason"] == "sqlite"
    assert result["metadata"]["storage_error_code"] == sqlite3.SQLITE_NOTADB
    assert result["metadata"]["delivery_state"] == "not_sent"
    assert path.read_bytes() == private_bytes
    assert private_bytes.decode() not in caplog.text
    assert not runner._durable_tool_tasks


@pytest.mark.asyncio
async def test_receive_ack_pressure_and_reconnect_never_reexecutes_reclaimed_call(
    tmp_path, monkeypatch
):
    journal = CallJournal(tmp_path / "calls.sqlite3", max_calls=1)
    runner = ExecutorRunner(
        ExecutorConfig(executor_id=uuid4().hex, controller_token=uuid4().hex),
        call_journal=journal,
    )
    original, successor = _call(), _call()
    socket, receiver = _WireSocket(), JSONMessageReceiver()
    request_ids = [uuid4().hex for _ in range(4)]

    async def incoming(_socket, _receiver):
        yield json.dumps(
            {"jsonrpc": "2.0", "method": "tool.execute", "id": request_ids[0], "params": original}
        )
        await asyncio.gather(*runner._durable_tool_tasks.values())
        yield json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tool.result_ack",
                "id": request_ids[1],
                "params": {"call_id": original["call_id"], "scope": original["runtime_metadata"]},
            }
        )
        yield json.dumps(
            {"jsonrpc": "2.0", "method": "tool.execute", "id": request_ids[2], "params": successor}
        )
        await asyncio.gather(*runner._durable_tool_tasks.values())
        yield json.dumps(
            {"jsonrpc": "2.0", "method": "tool.execute", "id": request_ids[3], "params": original}
        )

    monkeypatch.setattr(runner, "_transport_messages", incoming)
    try:
        await runner._receive_messages(socket, receiver)
    finally:
        await receiver.close()
    responses = {message["id"]: message for message in map(json.loads, socket.messages)}
    assert responses[request_ids[0]]["result"]["is_error"]
    assert "not configured" in responses[request_ids[0]]["result"]["output"]
    assert responses[request_ids[1]]["result"]["acknowledged"]
    assert responses[request_ids[2]]["result"]["is_error"]
    assert "not configured" in responses[request_ids[2]]["result"]["output"]
    assert responses[request_ids[3]]["error"]["code"] == -32000
    replacement = ExecutorRunner(runner.config, call_journal=CallJournal(journal.path))
    report = await replacement._call_journal.fetch(
        original["call_id"],
        replacement.executor_instance_id,
        scope=original["runtime_metadata"],
        require_scope=True,
    )
    assert report["state"] == "terminal_result_unavailable"
    assert not await replacement._call_journal.accept(original, replacement.executor_instance_id)
