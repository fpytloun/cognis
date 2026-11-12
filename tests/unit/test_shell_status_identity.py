from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.shell_target_evidence import shell_target_evidence
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.forwarding import ForwardedExecutorConnection
from cognis.providers.executor.websocket import WebSocketExecutorConnection
from tests.unit.test_shell_target_evidence import row


@pytest.fixture(params=[WebSocketExecutorConnection, ForwardedExecutorConnection])
def connection(request):
    if request.param is WebSocketExecutorConnection:
        transport = WebSocketExecutorConnection(
            ws=SimpleNamespace(),
            executor_id="fixture-executor",
            capabilities=ExecutorCapabilities(),
        )
    else:
        transport = ForwardedExecutorConnection(
            executor_id="fixture-executor",
            capabilities=ExecutorCapabilities(),
            owner_id="controller-b",
            epoch=1,
            owner_internal_url="http://controller-b:8080",
            requester_owner_id="controller-a",
            auth_provider=SimpleNamespace(),
        )
    transport.rpc_call = AsyncMock()
    return transport


async def lookup(connection):
    return await shell_target_evidence(
        connection,
        shell_id="shell_monitor",
        user_id="owner@example.com",
        session_id="session_after_compaction",
        conversation_id="conversation_monitor",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("local_id", ["remote", "fixture-executor", "another-local-label"])
async def test_real_transport_projects_connection_identity(connection, local_id):
    raw = {"shells": [row(executor_id=local_id)]}
    connection.rpc_call.return_value = raw
    snapshot = await connection.background_shell_status(include_completed=True)
    assert snapshot["shells"][0]["executor_id"] == "fixture-executor"
    assert snapshot["shells"][0]["reported_executor_id"] == local_id
    assert raw["shells"][0]["executor_id"] == local_id
    result = await lookup(connection)
    assert result["coverage"] == "complete"
    assert result["executor_id"] == "fixture-executor"
    assert result["target"]["call_id"] == "start_monitor"
    assert "output_tail" not in result["target"]
    assert connection.rpc_call.call_args.args == (
        "shell.background_status",
        {"include_completed": True},
    )
    assert connection.rpc_call.call_args.kwargs == (
        {"timeout": 10.0}
        if isinstance(connection, WebSocketExecutorConnection)
        else {"replay_safe": True}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_email", "conversation_id"])
async def test_connection_identity_does_not_bypass_owner_checks(connection, field):
    connection.rpc_call.return_value = {"shells": [row(executor_id="remote", **{field: "foreign"})]}
    result = await lookup(connection)
    assert result["reason"] == "not_found_or_not_owned"
    assert "target" not in result


@pytest.mark.asyncio
async def test_status_from_different_connection_cannot_claim_selected_executor(connection):
    connection.executor_id = "other_executor"
    connection.rpc_call.return_value = {"shells": [row(executor_id="fixture-executor")]}
    selected = SimpleNamespace(
        executor_id="fixture-executor",
        background_shell_status=connection.background_shell_status,
    )
    result = await lookup(selected)
    assert result["reason"] == "not_found_or_not_owned"
    assert "target" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [{"shells": []}, {"shells": None}, {"shells": [None]}])
async def test_missing_or_malformed_status_stays_unknown(connection, snapshot):
    connection.rpc_call.return_value = snapshot
    assert (await lookup(connection))["coverage"] == "unknown"


@pytest.mark.asyncio
async def test_failed_rpc_cannot_manufacture_provenance(connection):
    connection.rpc_call.side_effect = TimeoutError
    assert (await lookup(connection))["reason"] == "lookup_unavailable"


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry(connection):
    import asyncio

    connection.rpc_call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await lookup(connection)
    connection.rpc_call.assert_awaited_once()
