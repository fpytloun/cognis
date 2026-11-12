"""Interrupted output must be durable or explicitly remain unconfirmed."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.api.chat_v2.item_keys import assistant_message_item_id
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_loop import PauseWaiter
from cognis.core.assistant_stream_checkpoint import AssistantStreamCheckpoint
from cognis.core.direct_turn_runtime import (
    DirectTurnExecutionFence,
    DurableDirectTurnRuntime,
)
from cognis.core.events import EventBus, EventType
from cognis.core.turn_scheduler import TurnError, TurnScheduler, _TurnControl
from cognis.models.agent import AgentDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    EventAppendResult,
    SessionModel,
)
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import (
    DirectTurnStatus,
    DirectTurnStore,
    MaterializedDirectTurnPayload,
    PermanentDirectTurnPayloadError,
)
from cognis.store.models import DirectTurnRequestRow
from cognis.store.queries import create_agent, create_conversation, create_user


class _EventStore:
    """Apply the actual receiver key contract, including to lifecycle writes."""

    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.fail_markers = False
        self.attempts: list[str] = []
        self.accepted: dict[str, list[Any]] = {}

    async def record_events(
        self, *, session_id: str, events: list[Any], idempotency_key: str, **_: Any
    ) -> EventAppendResult:
        assert idempotency_key.startswith(f"{session_id}:")
        assert re.fullmatch(r"[A-Za-z0-9_.:@-]{1,256}", idempotency_key)
        self.attempts.append(idempotency_key)
        if self.failure is not None and (
            events[0].type == "assistant_message" or self.fail_markers
        ):
            raise self.failure
        self.accepted.setdefault(idempotency_key, events)
        return EventAppendResult(ok=True, count=len(events), first_seq=450, last_seq=450)


@dataclass
class _Case:
    scheduler: TurnScheduler
    events: _EventStore
    store: Any
    fence: DirectTurnExecutionFence
    control: _TurnControl
    session: SessionModel
    agent: AgentDefinition

    async def seed(self) -> None:
        self.scheduler._assistant_phase_by_turn[("conv-checkpoint", "turn-checkpoint")] = 47
        await self.scheduler._append_active_stream_chunk(
            conversation_id="conv-checkpoint",
            session_id=self.session.session_id,
            message_id="turn-checkpoint",
            turn_id="turn-checkpoint",
            delta="Interrupted response.",
            turn_cycle_index=19,
        )

    async def checkpoint(self, finish_reason: str = "user_cancelled") -> AssistantStreamCheckpoint:
        return await self.scheduler._persist_cancelled_active_stream(
            conversation_id="conv-checkpoint",
            session=self.session,
            message_id="turn-checkpoint",
            turn_id="turn-checkpoint",
            user_email="user@example.com",
            agent=self.agent,
            finish_reason=finish_reason,
            execution_fence=self.fence,
        )

    async def run(self) -> None:
        await self.scheduler._run_turn(
            conversation=ConversationModel(
                conversation_id="conv-checkpoint",
                user_email="user@example.com",
                agent_id=self.agent.agent_id,
                context=ConversationContext(type="web"),
            ),
            session=self.session,
            agent=self.agent,
            content="Continue",
            user_email="user@example.com",
            attachments=[],
            outbound_attachments=None,
            attachment_notice=None,
            attachment_context=None,
            system_initiated=True,
            channel_deliverable=False,
            delivery_id=None,
            delivery_fallback_text=None,
            bootstrap_wait_for_intention=False,
            cancel_event=self.control.cancel_event,
            turn_control=self.control,
            turn_id="turn-checkpoint",
            durable_request_id=self.fence.request_id,
            durable_lease=self.fence.lease,
        )


@pytest.fixture
def case() -> _Case:
    events = _EventStore()
    row = SimpleNamespace(
        status=DirectTurnStatus.RUNNING.value, cancel_requested_at=None, attempt_count=1
    )
    store = SimpleNamespace(
        has_fence=AsyncMock(return_value=True),
        checkpoint=AsyncMock(return_value=row),
        get=AsyncMock(return_value=row),
        mark_terminal=AsyncMock(return_value=row),
        settle_transient_failure=AsyncMock(
            return_value=SimpleNamespace(status=DirectTurnStatus.RECOVERABLE.value)
        ),
    )
    scheduler = TurnScheduler(
        session_factory=SimpleNamespace(),
        workflow_engine=SimpleNamespace(run_direct_turn=AsyncMock()),
        decision_engine=SimpleNamespace(
            decide=AsyncMock(return_value=SimpleNamespace(decision="inline"))
        ),
        task_queue=SimpleNamespace(),
        session_manager=SimpleNamespace(refresh_intaris_session_policy=AsyncMock()),
        session_cache=SimpleNamespace(
            append_recorded_events=AsyncMock(), invalidate_canonical=AsyncMock()
        ),
        compaction_strategy=SimpleNamespace(),
        agent_loop=SimpleNamespace(),
        pause_waiter=PauseWaiter(),
        notification_service=SimpleNamespace(),
        providers=SimpleNamespace(guardrails=events),
        artifact_store=SimpleNamespace(),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
    )
    lease = Lease(
        resource_key="conversation:conv-checkpoint",
        owner_id="controller-checkpoint",
        fencing_token=63,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    fence = DirectTurnExecutionFence(
        store, "request-checkpoint", lease, user_append_phase="user_appended"
    )
    control = _TurnControl(turn_id="turn-checkpoint")
    scheduler._direct_turn_store = store
    scheduler._durable_request_by_conversation["conv-checkpoint"] = fence.request_id
    scheduler._durable_fences[fence.request_id] = fence
    scheduler._turn_controls["conv-checkpoint"] = control
    scheduler._touch_conversation = AsyncMock()
    scheduler._clear_follow_up_pending = AsyncMock()
    scheduler._notify_managed_turn_error = AsyncMock(return_value=False)
    scheduler._settle_durable_direct_turn = AsyncMock(return_value=None)
    session = SessionModel(
        session_id="sess-checkpoint",
        intaris_session_id="intaris-checkpoint",
        conversation_id="conv-checkpoint",
        user_email="user@example.com",
        agent_id="agent-checkpoint",
    )
    agent = AgentDefinition(
        agent_id="agent-checkpoint", owner_email="owner@example.com", name="Agent"
    )
    scheduler._load_conversation_runtime = AsyncMock(
        return_value=(
            ConversationModel(
                conversation_id="conv-checkpoint",
                user_email="user@example.com",
                agent_id=agent.agent_id,
                context=ConversationContext(type="web"),
            ),
            session,
            agent,
            None,
        )
    )
    return _Case(scheduler, events, store, fence, control, session, agent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason", ["user_cancelled", "controller_interrupted", "turn_interrupted"]
)
async def test_checkpoint_preserves_runtime_phase_identity_and_receiver_contract(
    case: _Case, finish_reason: str
) -> None:
    await case.seed()
    result = await case.checkpoint(finish_reason)
    assert result.status == "persisted"
    assert (result.content, result.assistant_phase_index, result.turn_cycle_index) == (
        "Interrupted response.",
        47,
        19,
    )
    event = next(iter(case.events.accepted.values()))[0]
    assert (
        assistant_message_item_id(
            message_id=event.data["message_id"], phase=event.data["assistant_phase_index"]
        )
        == "message:turn-checkpoint:phase:47"
    )
    assert event.data["turn_cycle_index"] == 19
    assert not case.fence.stream_checkpoint_pending
    assert await case.scheduler.active_stream_snapshots("conv-checkpoint") == []


@pytest.mark.asyncio
async def test_uncertain_checkpoint_reuses_key_and_preserves_text_until_acknowledged(
    case: _Case,
) -> None:
    await case.seed()
    case.events.failure = httpx.ReadTimeout("transport interrupted")
    first = await case.checkpoint()
    assert first.status == "uncertain"
    assert case.fence.stream_checkpoint_pending
    assert case.scheduler._active_streams["conv-checkpoint"].content == "Interrupted response."
    case.events.failure = None
    assert (await case.checkpoint()).status == "persisted"
    assert case.events.attempts[0] == case.events.attempts[1]
    assert len(case.events.accepted) == 1
    assert not case.fence.stream_checkpoint_pending


@pytest.mark.asyncio
async def test_cache_failure_does_not_reclassify_an_acknowledged_append(case: _Case) -> None:
    await case.seed()
    case.scheduler._session_cache.append_recorded_events.side_effect = RuntimeError(
        "cache unavailable"
    )
    assert (await case.checkpoint()).status == "persisted"
    case.scheduler._session_cache.invalidate_canonical.assert_awaited_once_with(
        case.session.session_id
    )
    assert not case.fence.stream_checkpoint_pending


@pytest.mark.asyncio
async def test_lost_owner_does_not_append_or_discard_stream(case: _Case) -> None:
    await case.seed()
    case.store.has_fence.return_value = False
    assert (await case.checkpoint()).failed
    assert case.events.attempts == []
    assert case.fence.stream_checkpoint_pending
    assert case.scheduler._active_streams["conv-checkpoint"].content == "Interrupted response."


@pytest.mark.asyncio
async def test_terminal_error_checkpoints_before_clearing_visible_output(case: _Case) -> None:
    await case.seed()
    await case.scheduler._publish_turn_error(
        "conv-checkpoint",
        case.session.session_id,
        TurnError(code="turn_failed", message="Turn failed", recoverable=False),
        turn_id="turn-checkpoint",
        durable_session=case.session,
        durable_agent=case.agent,
        durable_user_email="user@example.com",
    )
    accepted = [event for batch in case.events.accepted.values() for event in batch]
    assert [event.type for event in accepted] == ["assistant_message", "lifecycle"]
    assert accepted[0].data["content"] == "Interrupted response."
    assert accepted[0].data["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_marker", [False, True])
async def test_rejected_checkpoint_requires_a_durable_explicit_loss_marker(
    case: _Case, fail_marker: bool
) -> None:
    await case.seed()
    case.events.failure = httpx.HTTPStatusError(
        "rejected",
        request=httpx.Request("POST", "https://intaris.test/events"),
        response=httpx.Response(400),
    )
    case.events.fail_markers = fail_marker
    kwargs = dict(
        conversation_id="conv-checkpoint",
        session=case.session,
        message_id="turn-checkpoint",
        turn_id="turn-checkpoint",
        user_email="user@example.com",
        agent=case.agent,
        execution_fence=case.fence,
    )
    if fail_marker:
        with pytest.raises(httpx.HTTPStatusError):
            await case.scheduler._checkpoint_stream_before_transition(**kwargs)
        assert case.fence.stream_checkpoint_pending
        assert case.scheduler._active_streams["conv-checkpoint"].content
    else:
        result = await case.scheduler._checkpoint_stream_before_transition(**kwargs)
        assert result.status == "rejected"
        marker = next(iter(case.events.accepted.values()))[0]
        assert marker.type == "lifecycle"
        assert marker.data["error_code"] == "assistant_output_unpersisted"
        assert marker.data["output_unpersisted"] is True
        assert marker.data["assistant_phase_index"] == 47
        assert marker.data["turn_cycle_index"] == 19
        assert marker.data["stream_chunk_count"] == 1
        assert "content" not in marker.data
        assert not case.fence.stream_checkpoint_pending


@pytest.mark.asyncio
async def test_retry_cleanup_checkpoints_nonempty_output_exactly_once(case: _Case) -> None:
    await case.seed()
    kwargs = dict(
        conversation_id="conv-checkpoint",
        session=case.session,
        turn_id="turn-checkpoint",
        message_id="turn-checkpoint",
        user_email="user@example.com",
        agent=case.agent,
        execution_fence=case.fence,
    )
    assert await case.scheduler._cleanup_durable_retry_attempt(**kwargs)
    assert await case.scheduler._cleanup_durable_retry_attempt(**kwargs)
    assert len(case.events.accepted) == 1
    assert await case.scheduler.active_stream_snapshots("conv-checkpoint") == []


@pytest.mark.asyncio
async def test_cancellation_during_error_classification_preserves_stream(case: _Case) -> None:
    await case.seed()
    case.scheduler._workflow_engine.run_direct_turn.side_effect = RuntimeError("turn failed")
    case.scheduler._classify_turn_error = AsyncMock(side_effect=asyncio.CancelledError)
    await case.run()
    assistant = [
        event
        for batch in case.events.accepted.values()
        for event in batch
        if event.type == "assistant_message"
    ]
    assert len(assistant) == 1
    assert assistant[0].data["content"] == "Interrupted response."
    assert assistant[0].data["assistant_phase_index"] == 47


@pytest.mark.asyncio
async def test_cancel_during_retry_settlement_keeps_checkpointed_output(case: _Case) -> None:
    await case.seed()
    case.scheduler._workflow_engine.run_direct_turn.side_effect = RuntimeError("temporary failure")
    case.scheduler._classify_turn_error = AsyncMock(
        return_value=TurnError(
            code="provider_unavailable", message="Unavailable", recoverable=True, transient=True
        )
    )
    case.scheduler._settle_durable_direct_turn.return_value = DirectTurnStatus.CANCELLED
    await case.run()
    assistant = [
        event
        for batch in case.events.accepted.values()
        for event in batch
        if event.type == "assistant_message"
    ]
    assert len(assistant) == 1
    assert assistant[0].data["content"] == "Interrupted response."
    case.store.mark_terminal.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_shutdown_checkpoint_survives_later_teardown_cancellation(case: _Case) -> None:
    await case.seed()
    started = asyncio.Event()

    async def wait_for_shutdown(**_: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    case.scheduler._workflow_engine.run_direct_turn.side_effect = wait_for_shutdown
    case.events.failure = httpx.ReadTimeout("event store unavailable")
    errors: list[Any] = []

    async def observe_error(event: Any) -> None:
        errors.append(event)

    case.scheduler._event_bus.subscribe(EventType.TURN_ERROR, observe_error)
    task = asyncio.create_task(case.run())
    case.scheduler._active_turns["conv-checkpoint"] = task
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        result = await case.scheduler.interrupt_active_turns_and_wait(
            reason="controller_restart", timeout_seconds=1
        )
        assert result["abandoned"] == 1
        assert case.fence.stream_checkpoint_pending
        assert not task.done()
        case.store.settle_transient_failure.assert_not_awaited()
        task.cancel()
        await task
        case.scheduler._settle_durable_direct_turn.assert_not_awaited()
        assert errors == []
        assert case.scheduler._active_streams["conv-checkpoint"].content == "Interrupted response."
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_shutdown_check_is_a_barrier_against_late_published_chunks(case: _Case) -> None:
    observed = SimpleNamespace(on_token=AsyncMock())
    case.scheduler._observers["conv-checkpoint"] = [observed]
    on_token, *_ = case.scheduler._build_callbacks(
        "conv-checkpoint", case.session.session_id, "turn-checkpoint", "turn-checkpoint"
    )
    late: list[asyncio.Task[None]] = []

    async def settle(*_: Any, **__: Any) -> Any:
        late.append(asyncio.create_task(on_token("late fragment")))
        await asyncio.sleep(0)
        assert observed.on_token.await_count == 0
        return SimpleNamespace(status=DirectTurnStatus.RECOVERABLE.value)

    case.store.settle_transient_failure.side_effect = settle
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    case.scheduler._active_turns["conv-checkpoint"] = task
    result = await case.scheduler.interrupt_active_turns_and_wait(
        reason="controller_restart", timeout_seconds=1
    )
    await asyncio.gather(*late)
    assert result["interrupted"] == 1
    assert case.events.accepted == {}
    assert await case.scheduler.active_stream_snapshots("conv-checkpoint") == []
    observed.on_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_exit", ["cancelled", "exception", "permanent_error"])
async def test_real_worker_shutdown_preserves_unconfirmed_request_for_new_owner(
    case: _Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_exit: str
) -> None:
    # Exercise the controller-error handler at its terminal attempt as well.
    monkeypatch.setattr("cognis.core.direct_turn_runtime.DIRECT_TURN_CONTROLLER_MAX_ATTEMPTS", 1)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(db, email="user@example.com", name="User", password_hash="hash")
        await create_user(db, email="owner@example.com", name="Owner", password_hash="hash")
        await create_agent(
            db,
            agent_id=case.agent.agent_id,
            owner_email=case.agent.owner_email,
            name="Agent",
            status="active",
        )
        await create_conversation(
            db,
            conversation_id="conv-checkpoint",
            user_email="user@example.com",
            agent_id=case.agent.agent_id,
            context_type="web",
        )
        await db.commit()
    store, leases = DirectTurnStore(factory), DatabaseLeaseStore(factory)
    admitted = await store.admit(
        user_id="user@example.com",
        agent_id=case.agent.agent_id,
        conversation_id="conv-checkpoint",
        session_id="sess-checkpoint",
        idempotency_scope="test:conv-checkpoint",
        idempotency_key="checkpoint-admission",
        turn_id="turn-checkpoint",
        payload={"schema_version": 1, "content": "Continue", "attachments": [], "metadata": {}},
    )
    request = admitted.request
    case.scheduler._direct_turn_store = store
    started = asyncio.Event()

    async def wait_for_shutdown(**_: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    async def submit(*_: Any, **__: Any) -> None:
        task = asyncio.create_task(case.run())
        case.scheduler._active_turns["conv-checkpoint"] = task

    async def execute(
        row: DirectTurnRequestRow,
        payload: MaterializedDirectTurnPayload,
        fence: DirectTurnExecutionFence,
    ) -> None:
        case.fence = fence
        try:
            await case.scheduler._execute_claimed_direct_turn_unscoped(row, payload, fence)
        except asyncio.CancelledError:
            if worker_exit == "exception":
                raise RuntimeError("teardown failed") from None
            if worker_exit == "permanent_error":
                raise PermanentDirectTurnPayloadError("teardown payload failure") from None
            raise

    case.scheduler.submit_turn = AsyncMock(side_effect=submit)
    case.scheduler._workflow_engine.run_direct_turn.side_effect = wait_for_shutdown
    worker = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        execute_claimed_turn=execute,
        controller_id="checkpoint-owner",
        incarnation_id="checkpoint-boot",
        artifact_store=SimpleNamespace(),
        simple_mode=False,
    )
    recovered = asyncio.Event()

    async def resume(
        row: DirectTurnRequestRow,
        _payload: MaterializedDirectTurnPayload,
        fence: DirectTurnExecutionFence,
    ) -> None:
        assert row.request_id == request.request_id
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            status=DirectTurnStatus.COMPLETED,
            lease=fence.lease,
        )
        recovered.set()

    next_owner = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        execute_claimed_turn=resume,
        controller_id="replacement-owner",
        incarnation_id="replacement-boot",
        artifact_store=SimpleNamespace(),
        simple_mode=False,
    )
    try:
        await worker.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        await case.seed()
        case.events.failure = httpx.ReadTimeout("checkpoint store unavailable")
        result = await case.scheduler.interrupt_active_turns_and_wait(
            reason="controller_restart",
            timeout_seconds=1,
        )
        assert result["abandoned"] == 1
        assert case.fence.stream_checkpoint_pending
        await worker.stop()
        unresolved = await store.get(request.request_id)
        assert unresolved is not None
        assert unresolved.status in {
            DirectTurnStatus.CLAIMED.value,
            DirectTurnStatus.RUNNING.value,
            DirectTurnStatus.RECOVERABLE.value,
        }
        assert unresolved.cancel_requested_at is None
        case.scheduler._settle_durable_direct_turn.assert_not_awaited()
        assert case.scheduler._active_streams["conv-checkpoint"].content == "Interrupted response."
        await next_owner.start()
        await asyncio.wait_for(recovered.wait(), timeout=1)
        completed = await store.get(request.request_id)
        assert completed is not None and completed.status == DirectTurnStatus.COMPLETED.value
        assert completed.attempt_count == 2
    finally:
        await worker.stop()
        await next_owner.stop()
        await engine.dispose()
