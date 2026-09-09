from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import cognis.core.direct_turn_runtime as direct_turn_runtime_module
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.controller_runtime import ControllerLifecycleState, ControllerRuntime
from cognis.core.direct_turn_runtime import (
    DirectTurnExecutionFence,
    DurableDirectTurnRuntime,
    StaleDirectTurnOwner,
)
from cognis.core.executor_recovery import (
    EXECUTOR_RECOVERY_WINDOW_SECONDS,
    ExecutorRecoveryTimeout,
    begin_executor_recovery,
    clear_executor_recovery,
)
from cognis.core.turn_scheduler import TurnScheduler
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import WebSocketExecutorConnection
from cognis.store.coordination import DatabaseLeaseStore, Lease, database_now
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import (
    DirectTurnStatus,
    DirectTurnStore,
    PermanentDirectTurnPayloadError,
    conversation_lease_key,
)
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_managed_conversation_link,
    create_user,
    get_conversation,
    mark_executor_unavailable,
    set_conversation_active_executor,
)


class _ArtifactStore:
    signed_url_ttl_seconds = 3600

    async def async_get_public_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://artifacts.invalid/input"


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)


async def _stores(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hash",
        )
        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            status="active",
        )
        await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-1",
            context_type="web",
            conversation_id="conv-a",
        )
        await session.commit()
    return engine, DirectTurnStore(factory), DatabaseLeaseStore(factory)


async def _admit(
    store: DirectTurnStore,
    key: str,
    content: str,
    *,
    metadata: dict[str, Any] | None = None,
):
    return await store.admit(
        conversation_id="conv-a",
        session_id="sess-a",
        agent_id="agent-1",
        user_id="user@example.com",
        idempotency_scope="test:conv-a",
        idempotency_key=key,
        payload={
            "schema_version": 1,
            "content": content,
            "attachments": [],
            "metadata": metadata or {},
        },
    )


@pytest.mark.asyncio
async def test_child_materializes_parent_owned_artifact(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    factory = store._session_factory  # noqa: SLF001
    async with factory() as session:
        await create_agent(
            session,
            agent_id="agent-child",
            owner_email="user@example.com",
            name="Child",
            status="active",
        )
        child = await create_conversation(
            session,
            user_email="user@example.com",
            agent_id="agent-child",
            context_type="agent_work",
            conversation_id="conv-child",
        )
        await create_managed_conversation_link(
            session,
            user_email="user@example.com",
            controller_agent_id="agent-1",
            controller_conversation_id="conv-a",
            controller_session_id="sess-a",
            target_agent_id="agent-child",
            target_conversation_id=child.conversation_id,
            target_session_id="sess-child",
            title="Child",
        )
        await create_artifact_record(
            session,
            artifact_id="att-parent",
            namespace="attachments",
            object_id="att-parent",
            filename="context.txt",
            owner_email="user@example.com",
            conversation_id="conv-a",
            purpose="chat_input",
            kind="file",
            mime_type="text/plain",
            size_bytes=7,
            status="attached",
        )
        await session.commit()

    admitted = await store.admit(
        conversation_id="conv-child",
        session_id="sess-child",
        agent_id="agent-child",
        user_id="user@example.com",
        idempotency_scope="test:conv-child",
        idempotency_key="parent-artifact",
        payload={
            "schema_version": 1,
            "content": "Use parent context",
            "attachments": [
                {
                    "artifact_id": "att-parent",
                    "kind": "file",
                    "mime_type": "text/plain",
                    "filename": "context.txt",
                    "size_bytes": 7,
                }
            ],
            "metadata": {},
        },
    )
    lease = await leases.acquire(
        conversation_lease_key("conv-child"),
        "controller:boot",
        ttl_seconds=60,
    )
    assert lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=lease,
        controller_id="controller",
        incarnation_id="boot",
    )

    materialized = await store.materialize_claimed_payload(
        admitted.request.request_id,
        lease=lease,
        artifact_store=_ArtifactStore(),
    )

    assert materialized is not None
    assert materialized.attachments[0].artifact_id == "att-parent"
    assert materialized.attachments[0].url == "https://artifacts.invalid/input"
    await engine.dispose()


async def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_executor_recovery_deadline_survives_controller_restart(tmp_path: Path) -> None:
    engine, store, _leases = await _stores(tmp_path)
    factory = store._session_factory  # noqa: SLF001
    first_observation = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    try:
        async with factory() as session:
            await set_conversation_active_executor(
                session,
                "conv-a",
                "maitrea_riker",
                source="explicit_primary",
            )
            await session.commit()

        first = await begin_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
            observed_at=first_observation,
        )
        recovered = await begin_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
            observed_at=first_observation + timedelta(minutes=5),
        )

        assert recovered.unavailable_since == first.unavailable_since
        assert recovered.deadline == first_observation + timedelta(
            seconds=EXECUTOR_RECOVERY_WINDOW_SECONDS
        )
        assert recovered.remaining_seconds(now=recovered.deadline) == 0
        timeout = ExecutorRecoveryTimeout(recovered, phase="runtime_admission")
        assert timeout.detail == {
            "code": "executor_recovery_timeout",
            "executor_id": "maitrea_riker",
            "unavailable_since": first_observation.isoformat(),
            "deadline": recovered.deadline.isoformat(),
            "waited_seconds": EXECUTOR_RECOVERY_WINDOW_SECONDS,
            "phase": "runtime_admission",
        }

        await clear_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
        )
        async with factory() as session:
            conversation = await get_conversation(session, "conv-a")
            assert conversation is not None
            assert conversation.active_executor_unavailable_since is None
        second_observation = first_observation + timedelta(hours=1)
        second = await begin_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
            observed_at=second_observation,
        )
        assert second.unavailable_since == second_observation
        assert second.deadline == second_observation + timedelta(
            seconds=EXECUTOR_RECOVERY_WINDOW_SECONDS
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_executor_recovery_first_observation_uses_database_clock(tmp_path: Path) -> None:
    engine, store, _leases = await _stores(tmp_path)
    factory = store._session_factory  # noqa: SLF001
    try:
        async with factory() as session:
            await set_conversation_active_executor(
                session,
                "conv-a",
                "maitrea_riker",
                source="explicit_primary",
            )
            before = await database_now(session)
            await session.commit()

        window = await begin_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
        )
        async with factory() as session:
            after = await database_now(session)

        assert before <= window.unavailable_since <= after
        assert window.database_remaining_seconds <= EXECUTOR_RECOVERY_WINDOW_SECONDS
        assert window.database_remaining_seconds > EXECUTOR_RECOVERY_WINDOW_SECONDS - 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_unavailable_cas_returns_winning_timestamp(tmp_path: Path) -> None:
    engine, store, _leases = await _stores(tmp_path)
    factory = store._session_factory  # noqa: SLF001
    earlier = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    later = earlier + timedelta(seconds=30)
    try:
        async with factory() as session:
            await set_conversation_active_executor(
                session,
                "conv-a",
                "maitrea_riker",
                source="selector_primary",
            )
            await session.commit()

        async def observe(at: datetime) -> tuple[bool, datetime | None]:
            async with factory() as session:
                result = await mark_executor_unavailable(
                    session,
                    conversation_id="conv-a",
                    task_id=None,
                    expected_executor_id="maitrea_riker",
                    expected_generation=1,
                    observed_at=at,
                )
                await session.commit()
                return result

        results = await asyncio.gather(observe(earlier), observe(later))
        winning = next(timestamp for changed, timestamp in results if changed)
        assert winning is not None
        assert all(timestamp == winning for _changed, timestamp in results)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_controllers_execute_one_conversation_in_fifo_order(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    first = await _admit(store, "message-1", "first")
    second = await _admit(store, "message-2", "first")
    started: list[str] = []
    active = 0
    max_active = 0
    release_first = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        nonlocal active, max_active
        assert await store.mark_running(row.request_id, lease=fence.lease)
        active += 1
        max_active = max(max_active, active)
        started.append(row.request_id)
        if row.request_id == first.request.request_id:
            await release_first.wait()
        active -= 1
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    runtimes = [
        DurableDirectTurnRuntime(
            store=store,
            lease_store=leases,
            controller_id=f"controller-{index}",
            incarnation_id=f"boot-{index}",
            artifact_store=_ArtifactStore(),
            execute_claimed_turn=execute,
            simple_mode=False,
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.gather(*(runtime.start() for runtime in runtimes))
        await _wait_for(lambda: len(started) == 1)
        await asyncio.sleep(0.1)
        assert started == [first.request.request_id]
        assert max_active == 1
        release_first.set()
        await _wait_for(lambda: len(started) == 2)
        await _wait_for(
            lambda: all(
                task.done()
                for runtime in runtimes
                for task in runtime._active.values()  # noqa: SLF001
            )
        )
        assert started == [first.request.request_id, second.request.request_id]
        assert max_active == 1
    finally:
        await asyncio.gather(*(runtime.stop() for runtime in runtimes))
        await engine.dispose()


@pytest.mark.asyncio
async def test_reclaimed_wait_reuses_deadline_and_projects_one_terminal_outcome(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-recovery", "wait")
    factory = store._session_factory  # noqa: SLF001
    unavailable_since = datetime(2026, 9, 3, 21, 30, tzinfo=UTC)
    async with factory() as session:
        await set_conversation_active_executor(
            session,
            "conv-a",
            "maitrea_riker",
            source="selector_primary",
        )
        await session.commit()
    original_window = await begin_executor_recovery(
        factory,
        conversation_id="conv-a",
        task_id=None,
        executor_id="maitrea_riker",
        observed_at=unavailable_since,
    )

    old_started = asyncio.Event()
    old_fence_rejected = asyncio.Event()
    dispatch_boundary = asyncio.Event()
    old_fence: DirectTurnExecutionFence | None = None
    dispatch_attempts = 0
    successful_dispatches = 0
    terminal_projections = 0
    reclaimed_window = None

    async def shared_tool_dispatch(fence: DirectTurnExecutionFence) -> None:
        nonlocal dispatch_attempts, successful_dispatches
        dispatch_attempts += 1
        await fence.assert_current()
        successful_dispatches += 1

    async def old_execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        nonlocal old_fence
        assert await store.mark_running(row.request_id, lease=fence.lease)
        old_fence = fence
        old_started.set()
        try:
            await dispatch_boundary.wait()
            await shared_tool_dispatch(fence)
        except StaleDirectTurnOwner:
            old_fence_rejected.set()
            raise

    async def new_execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        nonlocal reclaimed_window, terminal_projections
        assert await store.mark_running(row.request_id, lease=fence.lease)
        reclaimed_window = await begin_executor_recovery(
            factory,
            conversation_id="conv-a",
            task_id=None,
            executor_id="maitrea_riker",
            observed_at=unavailable_since + timedelta(minutes=10),
        )
        dispatch_boundary.set()
        await asyncio.sleep(0)
        await shared_tool_dispatch(fence)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.FAILED,
            outcome={
                "phase": "executor_recovery_timeout",
                "error_code": "executor_recovery_timeout",
                "error": "deadline expired",
            },
        )
        terminal_projections += 1

    old_runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-old",
        incarnation_id="boot-old",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=old_execute,
        simple_mode=False,
    )
    new_runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=new_execute,
        simple_mode=False,
    )
    try:
        await old_runtime.start()
        await asyncio.wait_for(old_started.wait(), timeout=2)
        assert old_fence is not None
        await old_runtime.stop_claiming()
        assert await leases.renew(old_fence.lease, ttl_seconds=-1) is not None
        recovery_lease = await leases.acquire(
            conversation_lease_key("conv-a"),
            "controller-new:recovery",
            ttl_seconds=60,
        )
        assert recovery_lease is not None
        assert await store.recover_stale_running(
            admitted.request.request_id,
            lease=recovery_lease,
            outcome={"phase": "executor_waiting"},
        )
        assert await leases.release(recovery_lease)

        await new_runtime.start()
        await _wait_for(lambda: terminal_projections == 1)
        await asyncio.wait_for(old_fence_rejected.wait(), timeout=2)
        terminal = await store.get(admitted.request.request_id)
        assert terminal is not None
        assert terminal.status == DirectTurnStatus.FAILED.value
        assert terminal.outcome["error_code"] == "executor_recovery_timeout"
        assert reclaimed_window is not None
        assert reclaimed_window.unavailable_since == original_window.unavailable_since
        assert reclaimed_window.deadline == original_window.deadline
        assert terminal_projections == 1
        assert dispatch_attempts == 2
        assert successful_dispatches == 1
    finally:
        await old_runtime.stop()
        await new_runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model_response", "intaris_append", "canonical_user_append"])
async def test_failed_stale_reconciliation_does_not_block_later_fifo_turn(
    tmp_path: Path, phase: str
) -> None:
    engine, store, leases = await _stores(tmp_path)
    first = await _admit(store, "stale-message", "first")
    second = await _admit(store, "later-message", "second")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"), "controller-old:boot-old", ttl_seconds=60
    )
    assert stale_lease is not None
    assert await store.claim(
        first.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(first.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        first.request.request_id,
        lease=stale_lease,
        phase=phase,
        metadata={
            "session_id": "intaris-sess-a",
            "phase_started_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert await leases.release(stale_lease)
    executed: list[str] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(row.request_id)
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    async def reconcile(_row: Any) -> bool:
        raise RuntimeError("Intaris authorization failed")

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        reconcile_canonical_append=reconcile,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await _wait_for(lambda: second.request.request_id in executed)
        stale = await store.get(first.request.request_id)
        assert stale is not None and stale.status == DirectTurnStatus.AMBIGUOUS.value
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "metadata"),
    [
        ("tool_in_flight", {"call_id": "call-1"}),
        (
            "intaris_append",
            {"event_types": ["tool_call"], "call_ids": ["call-1"]},
        ),
        (
            "intaris_append",
            {"event_types": ["tool_result"], "call_ids": ["call-1"]},
        ),
    ],
)
async def test_stale_tool_in_flight_becomes_ambiguous_without_redispatch(
    tmp_path: Path,
    phase: str,
    metadata: dict[str, Any],
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "tool")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase=phase,
        metadata={
            **metadata,
            "phase_started_at": (datetime.now(UTC) - timedelta(minutes=3)).isoformat(),
        },
    )
    assert await leases.release(stale_lease)
    executed = False

    async def execute(*_args: Any) -> None:
        nonlocal executed
        executed = True

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            row = await store.get(admitted.request.request_id)
            if row is not None and row.status == DirectTurnStatus.AMBIGUOUS.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("stale tool turn was not quarantined")
            await asyncio.sleep(0.01)
        assert row is not None
        assert row.status == DirectTurnStatus.AMBIGUOUS.value
        assert executed is False
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_tool_recovery_preserves_turn_and_continues_without_redispatch(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "tool-recovery", "tool")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase="tool_in_flight",
        metadata={
            "session_id": "sess-a",
            "turn_id": admitted.request.turn_id,
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "tool_name": "bash",
                    "frozen": True,
                    "dispatch_state": "pending",
                }
            ],
        },
    )
    assert await leases.release(stale_lease)
    recovered: list[dict[str, Any]] = []
    executed: list[tuple[str, dict[str, Any]]] = []

    async def recover(row, _lease: Lease) -> None:
        recovered.append(dict(row.outcome or {}))

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append((row.turn_id, dict(row.outcome or {})))
        await fence.complete({"phase": "completed"})

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        recover_tool_calls=recover,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await _wait_for(lambda: bool(executed))
        assert len(recovered) == 1
        started = datetime.fromisoformat(recovered[0]["recovery_started_at"])
        deadline = datetime.fromisoformat(recovered[0]["recovery_deadline_at"])
        assert deadline - started == timedelta(seconds=60)
        assert executed[0][0] == admitted.request.turn_id
        assert executed[0][1]["phase"] == "user_appended"
        assert executed[0][1]["tool_recovery_result_persisted"] is True
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_dispatch_binding_is_fenced_and_merged_before_send(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "tool-binding", "tool")
    lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller:boot",
        ttl_seconds=60,
    )
    assert lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=lease,
        controller_id="controller",
        incarnation_id="boot",
    )
    assert await store.mark_running(admitted.request.request_id, lease=lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=lease,
        phase="tool_in_flight",
        metadata={
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "tool_name": "bash",
                    "frozen": True,
                    "dispatch_state": "pending",
                }
            ]
        },
    )
    fence = DirectTurnExecutionFence(
        store=store,
        request_id=admitted.request.request_id,
        lease=lease,
    )

    await fence.bind_tool_dispatch(
        "call-1",
        "executor-1",
        "instance-1",
        dispatch_state="dispatching",
    )

    row = await store.get(admitted.request.request_id)
    assert row is not None
    assert row.outcome["tool_calls"] == [
        {
            "call_id": "call-1",
            "tool_name": "bash",
            "frozen": True,
            "dispatch_state": "dispatching",
            "executor_id": "executor-1",
            "executor_instance_id": "instance-1",
        }
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_immediately_before_binding_aborts_tool_frame(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "tool-binding-cancel", "tool")
    lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller:boot",
        ttl_seconds=60,
    )
    assert lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=lease,
        controller_id="controller",
        incarnation_id="boot",
    )
    assert await store.mark_running(admitted.request.request_id, lease=lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=lease,
        phase="tool_in_flight",
        metadata={
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "tool_name": "bash",
                    "frozen": False,
                    "dispatch_state": "pending",
                }
            ]
        },
    )
    fence = DirectTurnExecutionFence(
        store=store,
        request_id=admitted.request.request_id,
        lease=lease,
    )
    cancelled = await store.request_cancel(admitted.request.request_id)
    assert cancelled is not None and cancelled.cancellation_requested
    websocket = _RecordingWebSocket()
    connection = WebSocketExecutorConnection(
        websocket,  # type: ignore[arg-type]
        "executor-1",
        ExecutorCapabilities(),
    )

    async def bind_before_send(executor_id: str, instance_id: str | None) -> None:
        await fence.bind_tool_dispatch(
            "call-1",
            executor_id,
            instance_id,
            dispatch_state="dispatching",
        )

    with pytest.raises(StaleDirectTurnOwner):
        await connection.rpc_call(
            "tool.execute",
            {"call_id": "call-1"},
            timeout=1,
            before_send=bind_before_send,
        )

    assert websocket.sent == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_during_tool_recovery_settles_cancelled(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "tool-recovery-cancel", "tool")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase="tool_in_flight",
        metadata={
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "tool_name": "bash",
                    "frozen": True,
                    "dispatch_state": "pending",
                }
            ]
        },
    )
    assert await leases.release(stale_lease)
    executed = False

    async def recover(_row, _lease: Lease) -> None:
        result = await store.request_cancel(admitted.request.request_id)
        assert result is not None and result.cancellation_requested

    async def execute(*_args: Any) -> None:
        nonlocal executed
        executed = True

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        recover_tool_calls=recover,
        simple_mode=False,
    )
    try:
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            current = await store.get(admitted.request.request_id)
            if current is not None and current.status == DirectTurnStatus.CANCELLED.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("tool recovery cancellation was not settled")
            await asyncio.sleep(0.01)
        assert executed is False
        assert current.outcome["reason"] == "cancelled during tool recovery"
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_and_executed_once(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "recover")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await leases.release(stale_lease)
    executed: list[str] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(row.request_id)
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await _wait_for(lambda: executed == [admitted.request.request_id])
        row = await store.get(admitted.request.request_id)
        assert row is not None
        assert row.attempt_count == 2
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_stale_cancelled_turn_is_terminalized_without_execution(
    tmp_path: Path,
    running: bool,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", f"cancel-stale-{running}")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    if running:
        assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    cancel_result = await store.request_cancel(admitted.request.request_id)
    assert cancel_result is not None
    assert cancel_result.cancellation_requested is True
    assert await leases.release(stale_lease)
    executed = False

    async def execute(_row, _payload, _fence: DirectTurnExecutionFence) -> None:
        nonlocal executed
        executed = True

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            row = await store.get(admitted.request.request_id)
            if row is not None and row.status == DirectTurnStatus.CANCELLED.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("stale cancellation was not terminalized")
            await asyncio.sleep(0.01)
        assert row.owner_controller_id is None
        assert row.owner_incarnation_id is None
        assert row.fencing_token is None
        assert row.terminal_at is not None
        assert row.outcome == {
            "phase": "cancelled",
            "reason": "stale owner cancellation recovered",
        }
        assert executed is False
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_model_wait_resumes_from_durable_boundary(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "model")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase="model_wait",
    )
    assert await leases.release(stale_lease)
    executed: list[dict[str, Any]] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(dict(row.outcome or {}))
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await _wait_for(lambda: len(executed) == 1)
        assert executed[0]["phase"] == "recovered_model_boundary"
        assert executed[0]["source_phase"] == "model_wait"
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "metadata"),
    [
        ("model_response", {"session_id": "intaris-sess-a"}),
        (
            "intaris_append",
            {
                "session_id": "intaris-sess-a",
                "event_types": ["assistant_message"],
            },
        ),
    ],
)
async def test_stale_model_response_reconciles_without_rerun(
    tmp_path: Path,
    phase: str,
    metadata: dict[str, Any],
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "model")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.mark_running(admitted.request.request_id, lease=stale_lease)
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase=phase,
        metadata={
            **metadata,
            "phase_started_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert await leases.release(stale_lease)
    executed = False

    async def execute(*_args: Any) -> None:
        nonlocal executed
        executed = True

    async def reconcile(_row: Any) -> bool:
        return True

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        reconcile_canonical_append=reconcile,
        simple_mode=False,
    )
    try:
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            row = await store.get(admitted.request.request_id)
            if row is not None and row.status == DirectTurnStatus.COMPLETED.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("model response was not reconciled")
            await asyncio.sleep(0.01)
        assert row.outcome == {"phase": "reconciled_terminal_assistant"}
        assert executed is False
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_absorbed_append_is_reconciled_without_standalone_execution(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    parent = await _admit(store, "message-1", "parent")
    child = await _admit(store, "message-2", "absorbed")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        parent.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.begin_absorb(
        child.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
        absorbed_by_turn_id=parent.request.turn_id,
        session_id="sess-a",
    )
    assert await store.checkpoint(
        child.request.request_id,
        lease=stale_lease,
        phase="canonical_user_append",
        metadata={
            "session_id": "sess-a",
            "phase_started_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert await leases.release(stale_lease)
    executed: list[str] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(row.request_id)
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    async def reconcile(row) -> bool:
        return row.request_id == child.request.request_id

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        reconcile_canonical_append=reconcile,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await _wait_for(lambda: executed == [parent.request.request_id])
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            child_row = await store.get(child.request.request_id)
            if child_row is not None and child_row.status == DirectTurnStatus.ABSORBED.value:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("absorbed request was not reconciled")
            await asyncio.sleep(0.01)
        assert executed == [parent.request.request_id]
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_phase", "append_was_accepted", "expected_phase", "reconcile_count"),
    [
        ("user_append_uncertain", True, "user_appended", 1),
        ("user_append_uncertain", False, "user_append_pending", 1),
        ("user_appended", False, "user_appended", 0),
    ],
)
async def test_stale_uncertain_user_append_is_reconciled_before_execution(
    tmp_path: Path,
    initial_phase: str,
    append_was_accepted: bool,
    expected_phase: str,
    reconcile_count: int,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "uncertain-message", "hello")
    stale_lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-old:boot-old",
        ttl_seconds=60,
    )
    assert stale_lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=stale_lease,
        controller_id="controller-old",
        incarnation_id="boot-old",
    )
    assert await store.checkpoint(
        admitted.request.request_id,
        lease=stale_lease,
        phase=initial_phase,
        metadata={
            "session_id": "isess-a",
            "phase_started_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert await leases.release(stale_lease)
    executed_phase: list[str] = []
    executed_session: list[str | None] = []
    completed = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed_phase.append(str((row.outcome or {}).get("phase")))
        executed_session.append((row.outcome or {}).get("session_id"))
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        completed.set()

    reconcile = AsyncMock(return_value=append_was_accepted)
    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-new",
        incarnation_id="boot-new",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        reconcile_canonical_append=reconcile,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await asyncio.wait_for(completed.wait(), timeout=1)
        assert executed_phase == [expected_phase]
        assert executed_session == ["isess-a"]
        row = await store.get(admitted.request.request_id)
        assert row is not None
        assert row.status == DirectTurnStatus.COMPLETED.value
        assert reconcile.await_count == reconcile_count
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated_by", ["cancellation", "stale_lease"])
async def test_invalid_fence_cannot_cross_user_append_boundary(
    tmp_path: Path,
    invalidated_by: str,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, f"invalid-{invalidated_by}", "hello")
    lease = await leases.acquire(
        conversation_lease_key("conv-a"),
        "controller-a:boot-a",
        ttl_seconds=60,
    )
    assert lease is not None
    assert await store.claim(
        admitted.request.request_id,
        lease=lease,
        controller_id="controller-a",
        incarnation_id="boot-a",
    )
    fence = DirectTurnExecutionFence(store, admitted.request.request_id, lease)
    if invalidated_by == "cancellation":
        cancelled = await store.request_cancel(admitted.request.request_id)
        assert cancelled is not None and cancelled.cancellation_requested
        expected_error = asyncio.CancelledError
    else:
        assert await leases.release(lease)
        expected_error = StaleDirectTurnOwner
    append_calls: list[str] = []

    try:
        with pytest.raises(expected_error):
            await fence.checkpoint(
                "user_append_uncertain",
                session_id="isess-a",
            )
            append_calls.append("user_message")
        assert append_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint_phase", "append_phase"),
    [
        ("user_append_pending", "user_append_pending"),
        ("user_append_uncertain", "user_append_uncertain"),
        ("user_appended", "user_appended"),
        ("model_wait", "user_appended"),
    ],
)
async def test_controller_error_preserves_append_phase_for_reclaim(
    tmp_path: Path,
    checkpoint_phase: str,
    append_phase: str,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    await _admit(store, f"controller-error-{append_phase}", "hello")
    attempts: list[dict[str, Any]] = []
    completed = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        attempts.append(dict(row.outcome or {}))
        if len(attempts) == 1:
            fence.set_user_append_state(
                append_phase,
                session_id="isess-a",
            )
            await fence.checkpoint(
                checkpoint_phase,
                session_id="isess-a",
            )
            raise RuntimeError("controller failed")
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        completed.set()

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        reconcile_canonical_append=AsyncMock(return_value=False),
        simple_mode=False,
    )
    try:
        await runtime.start()
        await asyncio.wait_for(completed.wait(), timeout=10)
        assert len(attempts) == 2
        assert attempts[1]["phase"] == "controller_error"
        assert attempts[1]["user_append_phase"] == append_phase
        assert attempts[1]["user_append_session_id"] == "isess-a"
        assert attempts[1]["session_id"] == "isess-a"
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_deterministic_controller_failure_is_bounded_and_releases_fifo(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    first = await _admit(store, "controller-failure", "first")
    second = await _admit(store, "after-controller-failure", "second")
    completed = asyncio.Event()
    attempts: list[str] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        attempts.append(row.request_id)
        if row.request_id == first.request.request_id:
            raise RuntimeError("secret internal detail")
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        completed.set()

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await asyncio.wait_for(completed.wait(), timeout=3)
        failed = await store.get(first.request.request_id)
        succeeded = await store.get(second.request.request_id)
        assert failed is not None
        assert failed.status == DirectTurnStatus.FAILED.value
        assert (
            failed.attempt_count == direct_turn_runtime_module.DIRECT_TURN_CONTROLLER_MAX_ATTEMPTS
        )
        assert failed.outcome == {
            "phase": "permanent_payload_visibility_pending",
            "failure_kind": "controller",
            "error": "Direct turn controller execution failed.",
        }
        assert succeeded is not None
        assert succeeded.status == DirectTurnStatus.COMPLETED.value
        assert attempts.count(first.request.request_id) == 3
        assert attempts[-1] == second.request.request_id
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_simple_mode_wake_claims_without_poll_delay(tmp_path: Path) -> None:
    engine, store, leases = await _stores(tmp_path)
    executed = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        executed.set()

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=True,
    )
    try:
        await runtime.start()
        await _admit(store, "message-1", "immediate")
        await runtime.wake()
        await asyncio.wait_for(executed.wait(), timeout=2.0)
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_is_gated_until_schema_ready_but_starts_before_ready(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    controller = ControllerRuntime("controller-a", incarnation_id="boot-a")
    executed = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        executed.set()

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id=controller.controller_id,
        incarnation_id=controller.incarnation_id,
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        can_claim_turn=lambda _row: (
            controller.schema_compatible and controller.state is ControllerLifecycleState.READY
        ),
        simple_mode=True,
    )
    try:
        await runtime.start()
        await _admit(store, "message-1", "gated")
        await runtime.wake()
        await asyncio.sleep(0.05)
        assert not executed.is_set()
        controller.mark_schema_compatible()
        await runtime.wake()
        await asyncio.sleep(0.05)
        assert not executed.is_set()
        assert controller.state is ControllerLifecycleState.STARTING
        controller.mark_ready()
        await runtime.wake()
        await asyncio.wait_for(executed.wait(), timeout=2.0)
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_escalation_blocked_request_survives_restart_and_takeover(
    tmp_path: Path,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "blocked")
    pause_waiter = PauseWaiter()
    pause_waiter.register(
        PendingPause(
            pause_id="esc-1",
            pause_type="escalation",
            conversation_id="conv-a",
            session_id="sess-a",
        )
    )
    executed: list[str] = []

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(row.request_id)
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    def can_claim(_row: Any) -> bool:
        return (
            pause_waiter.find_pending(
                pause_type="escalation",
                conversation_id="conv-a",
            )
            is None
        )

    first = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        can_claim_turn=can_claim,
        simple_mode=True,
    )
    second = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-b",
        incarnation_id="boot-b",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        can_claim_turn=can_claim,
        simple_mode=True,
    )
    try:
        await first.start()
        await first.wake()
        await asyncio.sleep(0.05)
        assert executed == []
        await first.stop()
        await second.start()
        assert (await store.get(admitted.request.request_id)).status == "queued"
        assert pause_waiter.resolve(
            "esc-1",
            PauseResolution(decision="approve"),
        )
        await second.wake()
        await _wait_for(lambda: executed == [admitted.request.request_id])
    finally:
        await first.stop()
        await second.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_permanent_attachment_failure_advances_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    poison = await _admit(store, "message-1", "poison")
    healthy = await _admit(store, "message-2", "healthy")
    original = store.materialize_claimed_payload
    executed: list[str] = []
    visible_failures: list[str] = []

    async def materialize(request_id: str, **kwargs: Any):
        if request_id == poison.request.request_id:
            raise PermanentDirectTurnPayloadError("Attachment is expired")
        return await original(request_id, **kwargs)

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        executed.append(row.request_id)
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )

    visibility_attempts = 0

    async def on_permanent_failure(row, exc: Exception) -> None:
        nonlocal visibility_attempts
        visibility_attempts += 1
        if visibility_attempts == 1:
            raise RuntimeError("Intaris temporarily unavailable")
        visible_failures.append(f"{row.request_id}:{exc}")

    monkeypatch.setattr(store, "materialize_claimed_payload", materialize)
    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        on_permanent_failure=on_permanent_failure,
        simple_mode=True,
    )
    try:
        await runtime.start()
        await runtime.wake()
        await _wait_for(lambda: executed == [healthy.request.request_id])
        poison_row = await store.get(poison.request.request_id)
        assert poison_row is not None
        assert poison_row.status == DirectTurnStatus.FAILED.value
        await _wait_for(lambda: len(visible_failures) == 1)
        assert visible_failures == [f"{poison.request.request_id}:Attachment is expired"]
        completed_visibility = await store.get(poison.request.request_id)
        assert completed_visibility is not None
        assert completed_visibility.outcome == {"phase": "permanent_payload_visibility_complete"}
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_start_delay", [0.0, 0.1])
async def test_post_admission_wake_failure_is_retried_without_rejecting_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_start_delay: float,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    executed = asyncio.Event()

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        assert await store.mark_running(row.request_id, lease=fence.lease)
        assert await store.mark_terminal(
            row.request_id,
            lease=fence.lease,
            status=DirectTurnStatus.COMPLETED,
        )
        executed.set()

    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=leases,
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=True,
    )
    original_acquire = leases.acquire
    failures = 0
    failure_observed = asyncio.Event()
    original_run = runtime._run  # noqa: SLF001

    async def delayed_run() -> None:
        await asyncio.sleep(worker_start_delay)
        await original_run()

    monkeypatch.setattr(runtime, "_run", delayed_run)

    async def flaky_acquire(*args: Any, **kwargs: Any):
        nonlocal failures
        if failures == 0:
            failures += 1
            failure_observed.set()
            raise RuntimeError("temporary database failure")
        return await original_acquire(*args, **kwargs)

    try:
        await runtime.start()
        await _admit(store, "message-1", "accepted")
        monkeypatch.setattr(leases, "acquire", flaky_acquire)
        await runtime.wake()
        # The second wake must follow the failed iteration, not an assumed
        # scheduling delay. Pre-start wakes legitimately coalesce.
        await asyncio.wait_for(failure_observed.wait(), timeout=1)
        assert runtime._worker is not None and not runtime._worker.done()  # noqa: SLF001
        await runtime.wake()
        await asyncio.wait_for(executed.wait(), timeout=1)
        assert failures == 1
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_wake_during_claim_iteration_is_not_lost() -> None:
    runtime = object.__new__(DurableDirectTurnRuntime)
    runtime._accepting_claims = True  # noqa: SLF001
    runtime._stop = asyncio.Event()  # noqa: SLF001
    runtime._wake = asyncio.Event()  # noqa: SLF001
    runtime._wake_generation = 0  # noqa: SLF001
    entered = asyncio.Event()
    release = asyncio.Event()
    second_iteration = asyncio.Event()
    iterations = 0

    async def run_once() -> None:
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            entered.set()
            await release.wait()
        else:
            second_iteration.set()

    runtime.run_once = run_once  # type: ignore[method-assign]
    worker = asyncio.create_task(runtime._run())  # noqa: SLF001
    try:
        await entered.wait()
        await runtime.wake()
        release.set()
        await asyncio.wait_for(second_iteration.wait(), timeout=0.1)
    finally:
        runtime._stop.set()  # noqa: SLF001
        runtime._wake.set()  # noqa: SLF001
        await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_renewal_loss_cancels_executing_owner(
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    class LeaseStore:
        async def renew(self, _lease: Lease, *, ttl_seconds: float) -> None:
            del ttl_seconds
            if raises:
                raise RuntimeError("renew failed")
            return None

    runtime = object.__new__(DurableDirectTurnRuntime)
    runtime._lease_store = LeaseStore()  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(direct_turn_runtime_module, "DIRECT_TURN_LEASE_SECONDS", 0.03)
    cancelled = asyncio.Event()

    async def owner() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    owner_task = asyncio.create_task(owner())
    lease = Lease(
        resource_key="direct-turn:conversation:conv-a",
        owner_id="controller-a:boot-a",
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    ownership_lost = asyncio.Event()
    renew_task = asyncio.create_task(  # noqa: SLF001
        runtime._renew(lease, owner_task, ownership_lost)
    )
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    await renew_task
    assert ownership_lost.is_set()
    with pytest.raises(asyncio.CancelledError):
        await owner_task


@pytest.mark.asyncio
async def test_durable_cancel_watch_interrupts_owner_without_cluster_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(cancel_requested_at=datetime.now(UTC))
    runtime = object.__new__(DurableDirectTurnRuntime)
    runtime.store = SimpleNamespace(get=AsyncMock(return_value=row))
    monkeypatch.setattr(
        direct_turn_runtime_module,
        "DIRECT_TURN_CANCELLATION_POLL_SECONDS",
        0.01,
    )
    cancelled = asyncio.Event()

    async def owner() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    owner_task = asyncio.create_task(owner())
    watcher = asyncio.create_task(  # noqa: SLF001
        runtime._watch_cancellation("request-1", owner_task)
    )

    await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    await watcher
    with pytest.raises(asyncio.CancelledError):
        await owner_task


@pytest.mark.asyncio
async def test_renewal_exception_leaves_request_for_recovery_not_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "renewal")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class LeaseStore:
        async def acquire(self, *args: Any, **kwargs: Any):
            kwargs["ttl_seconds"] = 60
            return await leases.acquire(*args, **kwargs)

        async def renew(self, _lease: Lease, *, ttl_seconds: float) -> None:
            del ttl_seconds
            await started.wait()
            raise RuntimeError("database unavailable")

        async def release(self, lease: Lease) -> bool:
            return await leases.release(lease)

    async def execute(row, _payload, fence: DirectTurnExecutionFence) -> None:
        assert await store.mark_running(row.request_id, lease=fence.lease)
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(direct_turn_runtime_module, "DIRECT_TURN_LEASE_SECONDS", 0.3)
    runtime = DurableDirectTurnRuntime(
        store=store,
        lease_store=LeaseStore(),  # type: ignore[arg-type]
        controller_id="controller-a",
        incarnation_id="boot-a",
        artifact_store=_ArtifactStore(),
        execute_claimed_turn=execute,
        simple_mode=False,
    )
    try:
        await runtime.start()
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await runtime.stop_claiming()
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        row = await store.get(admitted.request.request_id)
        assert row is not None
        assert row.status == DirectTurnStatus.RUNNING.value
        assert row.cancel_requested_at is None
    finally:
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_queue_projection_edit_and_cancel_use_durable_store(
    tmp_path: Path,
) -> None:
    engine, store, _leases = await _stores(tmp_path)
    admitted = await _admit(store, "message-1", "original")
    scheduler = object.__new__(TurnScheduler)
    scheduler._direct_turn_store = store  # noqa: SLF001
    scheduler._durable_queue_cache = {}  # noqa: SLF001
    notifications: list[str] = []

    async def notify(conversation_id: str, **_kwargs: Any) -> None:
        notifications.append(conversation_id)

    scheduler._notify_queue_updated = notify  # type: ignore[method-assign]  # noqa: SLF001
    try:
        queue = await scheduler.get_queued_messages("conv-a")
        assert queue[0]["content"] == "original"
        edited = await scheduler.update_queued_message(
            "conv-a",
            admitted.request.request_id,
            content="final content",
        )
        assert edited is not None
        assert edited["content"] == "final content"
        assert await scheduler.cancel_queued_message(
            "conv-a",
            admitted.request.request_id,
        )
        assert await scheduler.get_queued_messages("conv-a") == []
        stored = await store.get(admitted.request.request_id)
        assert stored is not None
        assert stored.status == DirectTurnStatus.CANCELLED.value
        assert notifications == ["conv-a", "conv-a"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_queue_projection_types_durable_automatic_continuations(
    tmp_path: Path,
) -> None:
    engine, store, _leases = await _stores(tmp_path)
    admitted = await _admit(
        store,
        "automatic-continuation",
        "",
        metadata={
            "follow_up": {
                "origin_kind": "continuation",
                "reason": "llm_cycle_ceiling_reached",
            }
        },
    )
    scheduler = object.__new__(TurnScheduler)
    scheduler._direct_turn_store = store  # noqa: SLF001
    scheduler._durable_queue_cache = {}  # noqa: SLF001
    try:
        queue = await scheduler.get_queued_messages("conv-a")
        assert len(queue) == 1
        assert queue[0]["queue_id"] == admitted.request.request_id
        assert queue[0]["content"] == ""
        assert queue[0]["kind"] == "automatic_continuation"
        assert queue[0]["continuation_reason"] == "llm_cycle_ceiling_reached"
    finally:
        await engine.dispose()
