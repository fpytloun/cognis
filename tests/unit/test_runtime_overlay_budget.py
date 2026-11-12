"""Runtime producer overflow and canonical reconciliation regressions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import cognis.api.websocket as websocket_module
from cognis.api.chat_v2.schemas import MessageTimelineItem, TimelineScope
from cognis.api.websocket import AuthenticatedWebSocket, WebSocketConnectionManager
from cognis.core.chat_v2_runtime_relay import (
    MAX_PAYLOAD_BYTES,
    ChatV2RuntimeRedisRelay,
    RelayGenerationContext,
    RelayKind,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["terminal", "recoverable"])
async def test_long_turn_keeps_source_but_sends_same_bounded_local_and_remote_overlay(
    lifecycle: str,
) -> None:
    context = RelayGenerationContext(
        direct_request_id="request-1",
        turn_id="turn-1",
        session_id="session-1",
        conversation_id="conversation-1",
        owner_controller_id="controller-1",
        owner_incarnation_id="boot-1",
        fencing_token=7,
    )
    scheduler = SimpleNamespace(
        relay_generation_context=Mock(return_value=context),
        running_turn_state=Mock(return_value={"status": "running"}),
    )
    relay = ChatV2RuntimeRedisRelay(
        redis_service=Mock(),
        shared_secret="test-relay-secret",
        controller_id="controller-1",
        incarnation_id="boot-1",
        durable_validator=AsyncMock(return_value=True),
        apply_callback=AsyncMock(),
        has_subscriber=lambda _: True,
    )
    manager = WebSocketConnectionManager(
        SimpleNamespace(
            state=SimpleNamespace(turn_scheduler=scheduler, chat_v2_runtime_relay=relay)
        )
    )
    manager._fanout_chat_v2_runtime = AsyncMock()  # noqa: SLF001
    source = []
    for index in range(12):
        item = MessageTimelineItem(
            id=f"phase-{index}",
            sort_key=f"runtime:{index:04}",
            stable=False,
            role="assistant",
            content="é" * 100_000,
            message_id=f"message-{index}",
            turn_id=context.turn_id,
            status="complete",
        )
        source.append(item)
        await manager.send_chat_v2_runtime_to_conversation(
            context.conversation_id, volatile_items=[item], active_session_id=context.session_id
        )
        queued = relay._queue.items[-1].envelope  # noqa: SLF001
        local = manager._fanout_chat_v2_runtime.await_args.kwargs  # noqa: SLF001
        assert local["volatile_items"] == queued.volatile_items
        assert local["volatile_items_complete"] == queued.volatile_items_complete
        assert local["authority"] == queued.authority
        assert len(queued.raw_encoded()) <= MAX_PAYLOAD_BYTES

    assert not queued.volatile_items_complete
    assert len(manager._relay_runtime_items[context.conversation_id][1]) == 12  # noqa: SLF001
    with pytest.raises(ValueError, match="raw limit"):
        queued.model_copy(update={"volatile_items": source}).raw_encoded()

    current = source[-1].model_copy(
        update={"id": "current-small", "content": "Still making progress", "status": "running"}
    )
    await manager.send_chat_v2_runtime_to_conversation(
        context.conversation_id, volatile_items=[current], active_session_id=context.session_id
    )
    revision = relay._queue.items[-1].envelope.source_revision  # noqa: SLF001
    for _ in range(3):
        await manager.send_chat_v2_runtime_to_conversation(
            context.conversation_id,
            volatile_items=[],
            active_session_id=context.session_id,
            context_usage={"used_tokens": 123},
        )
        latest = relay._queue.items[-1].envelope  # noqa: SLF001
        assert current in latest.volatile_items
        assert latest.source_revision > revision
        revision = latest.source_revision

    await manager.send_chat_v2_runtime_to_conversation(
        context.conversation_id,
        volatile_items=[],
        active_session_id=context.session_id,
        has_active_turn=False,
        lifecycle=lifecycle,
    )
    terminal = relay._queue.items[-1].envelope  # noqa: SLF001
    assert terminal.kind == (
        RelayKind.TERMINAL if lifecycle == "terminal" else RelayKind.RELINQUISHED
    )
    assert terminal.authority.lifecycle == lifecycle
    assert not terminal.has_active_turn
    assert len(terminal.raw_encoded()) <= MAX_PAYLOAD_BYTES
    assert context.conversation_id not in manager._relay_runtime_items  # noqa: SLF001


@pytest.mark.asyncio
async def test_partial_runtime_wakes_only_its_scope_periodically_and_on_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WebSocketConnectionManager(SimpleNamespace(state=SimpleNamespace()))
    scope = TimelineScope(
        key="conversation:conversation-1", kind="conversation", conversation_id="conversation-1"
    )
    conn = SimpleNamespace(
        connection_id="connection-1",
        chat_v2_scopes={scope.key: scope},
        chat_v2_cursors={scope.key: "cursor"},
        send_text=AsyncMock(),
        send_scope_invalidation_nowait=Mock(return_value=True),
    )
    manager._connections[conn.connection_id] = conn  # noqa: SLF001
    manager._by_chat_v2_conversation["conversation-1"] = {conn.connection_id}  # noqa: SLF001
    manager._accepts_chat_v2_runtime = AsyncMock(return_value=True)  # noqa: SLF001
    clock = [10.0]
    monkeypatch.setattr(websocket_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def fanout(*, active: bool = True, complete: bool = False) -> None:
        await manager._fanout_chat_v2_runtime(  # noqa: SLF001
            "conversation-1",
            volatile_items=[],
            volatile_items_complete=complete,
            has_active_turn=active,
            active_session_id="session-1",
            context_usage=None,
            last_generation=None,
            active_turn={"turn_id": "turn-1", "session_id": "session-1", "status": "running"}
            if active
            else None,
        )

    await fanout(complete=True)
    conn.send_scope_invalidation_nowait.assert_not_called()
    await fanout()
    first = conn.send_scope_invalidation_nowait.call_args.args[0]
    assert first["scope_key"] == scope.key
    assert first["conversation_id"] == "conversation-1"
    assert first["reason"] == "chat_scope_changed"
    clock[0] = 11.0
    await fanout()
    assert conn.send_scope_invalidation_nowait.call_count == 1
    clock[0] = 20.0
    await fanout()
    assert conn.send_scope_invalidation_nowait.call_count == 2
    await fanout(active=False)
    assert conn.send_scope_invalidation_nowait.call_count == 3
    assert conn.send_scope_invalidation_nowait.call_args.args[0]["revision"] != first["revision"]
    assert manager._runtime_recovery_hint_at == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_partial_runtime_keeps_recovery_hints_for_two_scopes_in_real_outbound_queue() -> None:
    manager = WebSocketConnectionManager(SimpleNamespace(state=SimpleNamespace()))
    connection = AuthenticatedWebSocket(
        connection_id="connection-1",
        websocket=AsyncMock(),
        user_email="owner@example.com",
        role="user",
    )
    scopes = [
        TimelineScope(
            key="conversation:conversation-1",
            kind="conversation",
            conversation_id="conversation-1",
        ),
        TimelineScope(
            key="session:session-1",
            kind="session",
            conversation_id="conversation-1",
            session_id="session-1",
        ),
    ]
    for scope in scopes:
        connection.chat_v2_scopes[scope.key] = scope
        connection.chat_v2_cursors[scope.key] = "cursor"
    connection._writer_task = asyncio.create_task(asyncio.Event().wait())  # noqa: SLF001
    manager._connections[connection.connection_id] = connection  # noqa: SLF001
    manager._by_chat_v2_conversation["conversation-1"] = {connection.connection_id}  # noqa: SLF001
    manager._accepts_chat_v2_runtime = AsyncMock(return_value=True)  # noqa: SLF001
    try:
        await manager._fanout_chat_v2_runtime(  # noqa: SLF001
            "conversation-1",
            volatile_items=[],
            volatile_items_complete=False,
            has_active_turn=False,
            active_session_id="session-1",
            context_usage=None,
            last_generation=None,
        )
        hints = []
        while not connection._outbound_queue.empty():  # noqa: SLF001
            queued = connection._outbound_queue.get_nowait()  # noqa: SLF001
            if queued.msg_type == "scope_invalidated":
                hints.append(queued.payload)
            connection._outbound_queue.task_done()  # noqa: SLF001
        assert {hint["scope_key"] for hint in hints} == {scope.key for scope in scopes}
        assert len(hints) == 2
    finally:
        await connection.close()
