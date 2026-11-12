from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Update

from cognis.bootstrap import run_schema_bootstrap
from cognis.core.trusted_evidence import (
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    build_evidence_admission,
    build_evidence_event_binding,
    serialize_evidence_admission,
)
from cognis.models.channel import ChannelDeliveryDescriptor
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import (
    DirectTurnAdmissionRejected,
    DirectTurnConflictError,
    DirectTurnRecoveryConflict,
    DirectTurnRecoverySnapshot,
    DirectTurnStatus,
    DirectTurnStore,
    PermanentDirectTurnPayloadError,
    conversation_lease_key,
)
from cognis.store.models import ArtifactRecordRow, AuditLog
from cognis.store.queries import create_agent, create_conversation, create_user

ADMISSION_KEY = b"trusted-evidence-direct-turn-test-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed_seconds", [0, 1])
async def test_scheduler_successor_uses_real_admission_at_capacity(
    tmp_path, monkeypatch, elapsed_seconds
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cognis.core import message_envelope
    from cognis.core.agent_loop import PauseWaiter
    from cognis.core.events import EventBus
    from cognis.core.followups import MID_STREAM_FAILURE_CONTINUATION_REASON
    from cognis.core.turn_scheduler import TurnScheduler
    from cognis.models.session import SessionStatus

    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(message_envelope, "datetime", ControlledDatetime)

    harness = await _harness(tmp_path)
    try:
        predecessor = (await _admit(harness, key="parent", content="request")).request
        lease = await _lease(harness)
        await harness.store.claim(
            predecessor.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        scheduler = TurnScheduler(
            session_factory=harness.session_factory,
            workflow_engine=SimpleNamespace(),
            decision_engine=SimpleNamespace(),
            task_queue=SimpleNamespace(),
            session_manager=SimpleNamespace(),
            session_cache=SimpleNamespace(),
            compaction_strategy=SimpleNamespace(),
            agent_loop=SimpleNamespace(),
            pause_waiter=PauseWaiter(),
            notification_service=SimpleNamespace(),
            providers=SimpleNamespace(),
            artifact_store=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            event_bus=EventBus(),
        )
        scheduler._direct_turn_store = harness.store
        scheduler._durable_request_by_conversation["conv-a"] = predecessor.request_id
        scheduler._durable_fences[predecessor.request_id] = SimpleNamespace(lease=lease)
        scheduler._load_conversation_runtime = AsyncMock(
            return_value=(
                SimpleNamespace(
                    conversation_id="conv-a", user_email="user@example.com", status="active"
                ),
                SimpleNamespace(session_id=None, status=SessionStatus.ACTIVE),
                SimpleNamespace(agent_id="agent-1"),
                False,
            )
        )
        scheduler._resolve_attachments_for_turn = AsyncMock(return_value=([], None))
        scheduler._build_attachment_notice = AsyncMock(return_value=None)
        scheduler._load_turn_limits = AsyncMock(return_value=(1, 0))
        scheduler._notify_queue_updated = AsyncMock()
        scheduler._publish_durable_turn_change = AsyncMock()
        result = await scheduler._schedule_automatic_continuation(
            conversation_id="conv-a",
            session_id="session",
            turn_id=predecessor.turn_id,
            user_email="user@example.com",
            metadata={"continuation_reason": MID_STREAM_FAILURE_CONTINUATION_REASON},
            prior_follow_up=None,
            turn_observers=(),
        )
        assert result.terminal_error is None
        assert result.successor_turn_id is not None
        parent = await harness.store.get(predecessor.request_id)
        assert parent.status == "completed"
        pending = await harness.store.list_conversation_pending("conv-a")
        assert [row.turn_id for row in pending] == [result.successor_turn_id]
        clock[0] += timedelta(seconds=elapsed_seconds)
        replay = await scheduler._schedule_automatic_continuation(
            conversation_id="conv-a",
            session_id="session",
            turn_id=predecessor.turn_id,
            user_email="user@example.com",
            metadata={"continuation_reason": MID_STREAM_FAILURE_CONTINUATION_REASON},
            prior_follow_up=None,
            turn_observers=(),
        )
        assert replay.successor_turn_id == result.successor_turn_id
        assert len(await harness.store.list_conversation_pending("conv-a")) == 1
        assert pending[0].payload["metadata"]["user_message_metadata"]["ts"] == (
            "2026-01-01T00:00:00Z"
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
@pytest.mark.parametrize("clear_queue", [False, True])
async def test_continuation_handoff_stop_orderings(tmp_path, cancel_first, clear_queue):
    harness = await _harness(tmp_path)
    try:
        predecessor = (await _admit(harness, key="parent", content="request")).request
        lease = await _lease(harness)
        assert await harness.store.claim(
            predecessor.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        unrelated = (await _admit(harness, key="unrelated", content="later")).request

        async def participant(session, successor, created):
            await harness.store.handoff(
                session,
                request_id=predecessor.request_id,
                turn_id=predecessor.turn_id,
                lease=lease,
                successor=successor,
            )

        async def admit_successor():
            return await harness.store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="continuation",
                idempotency_key=predecessor.turn_id,
                turn_id=f"{predecessor.turn_id}-successor",
                payload={"schema_version": 1, "content": "", "attachments": []},
                transaction_participant=participant,
            )

        if cancel_first:
            await harness.store.cancel_conversation(
                "conv-a", clear_queue=clear_queue, active_request_id=predecessor.request_id
            )
            with pytest.raises(DirectTurnAdmissionRejected):
                await admit_successor()
            pending = await harness.store.list_conversation_pending("conv-a")
            assert all(row.turn_id != f"{predecessor.turn_id}-successor" for row in pending)
        else:
            first = await admit_successor()
            replay = await admit_successor()
            assert replay.request.request_id == first.request.request_id
            assert replay.created is False
            parent = await harness.store.get(predecessor.request_id)
            assert parent.status == "completed"
            assert parent.outcome["successor_request_id"] == first.request.request_id
            # No local predecessor cache: another controller must find the chain.
            await harness.store.cancel_conversation("conv-a", clear_queue=clear_queue)
            assert (await harness.store.get(first.request.request_id)).status == "cancelled"
        assert (await harness.store.get(unrelated.request_id)).status == (
            "cancelled" if clear_queue else "queued"
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_handoff_participant_failure_rolls_back_both_rows(tmp_path):
    harness = await _harness(tmp_path)
    try:
        predecessor = (await _admit(harness, key="parent", content="request")).request
        lease = await _lease(harness)
        await harness.store.claim(
            predecessor.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )

        async def participant(session, successor, created):
            await harness.store.handoff(
                session,
                request_id=predecessor.request_id,
                turn_id=predecessor.turn_id,
                lease=lease,
                successor=successor,
            )
            raise RuntimeError("managed link fence rejected")

        with pytest.raises(RuntimeError, match="managed link fence rejected"):
            await harness.store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="continuation",
                idempotency_key=predecessor.turn_id,
                payload={"schema_version": 1, "content": "", "attachments": []},
                transaction_participant=participant,
            )
        pending = await harness.store.list_conversation_pending("conv-a")
        assert [row.request_id for row in pending] == [predecessor.request_id]
        assert pending[0].status == "claimed"
    finally:
        await harness.engine.dispose()


@dataclass
class _Harness:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    store: DirectTurnStore
    leases: DatabaseLeaseStore


class _PausingUpdateSession:
    def __init__(
        self,
        session: AsyncSession,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._session = session
        self._entered = entered
        self._release = release
        self._paused = False

    def __getattr__(self, name: str):
        return getattr(self._session, name)

    async def execute(self, statement: object, *args: object, **kwargs: object):
        if isinstance(statement, Update) and not self._paused:
            self._paused = True
            self._entered.set()
            await self._release.wait()
        return await self._session.execute(statement, *args, **kwargs)


class _PausingUpdateContext:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._context = session_factory()
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> _PausingUpdateSession:
        session = await self._context.__aenter__()
        return _PausingUpdateSession(session, self._entered, self._release)

    async def __aexit__(self, *args: object) -> object:
        return await self._context.__aexit__(*args)


class _PausingUpdateFactory:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._session_factory = session_factory
        self._entered = entered
        self._release = release

    def __call__(self) -> _PausingUpdateContext:
        return _PausingUpdateContext(
            self._session_factory,
            self._entered,
            self._release,
        )


async def _harness(tmp_path: Path) -> _Harness:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct-turns.db'}")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
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
        for conversation_id in ("conv-a", "conv-b"):
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                conversation_id=conversation_id,
            )
        await session.commit()
    return _Harness(
        engine=engine,
        session_factory=session_factory,
        store=DirectTurnStore(session_factory),
        leases=DatabaseLeaseStore(session_factory),
    )


@pytest.mark.asyncio
async def test_retry_wait_is_visible_in_public_queries_until_cancelled(tmp_path: Path) -> None:
    from cognis.core.turn_scheduler import TurnScheduler

    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="retry-wait", content="Wait for provider")
        async with harness.session_factory() as session:
            model = type(admitted.request)
            row = (
                await session.execute(
                    select(model).where(model.request_id == admitted.request.request_id)
                )
            ).scalar_one()
            assert row is not None
            row.status = DirectTurnStatus.RECOVERABLE.value
            await session.commit()
        scheduler = object.__new__(TurnScheduler)
        scheduler._direct_turn_store = harness.store
        scheduler.running_turn_state = lambda _: None
        # The lease/execution queries deliberately still exclude retry-wait.
        assert await harness.store.get_conversation_active("conv-a") is None
        assert await harness.store.list_conversations_active(["conv-a"]) == {}
        for _ in range(3):
            single = await scheduler.durable_running_turn_state("conv-a")
            batch = await scheduler.durable_running_turn_states(["conv-a", "conv-b"])
            snapshot = await scheduler.durable_runtime_context("conv-a")
            assert single is not None
            assert single["status"] == "waiting"
            assert batch == {"conv-a": single}
            assert snapshot["running"] == single
        await harness.store.request_cancel(admitted.request.request_id)
        assert await scheduler.durable_running_turn_state("conv-a") is None
        assert await scheduler.durable_running_turn_states(["conv-a"]) == {}
        assert (await scheduler.durable_runtime_context("conv-a"))["running"] is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_active_reads_reuse_caller_session_without_factory_checkout(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:

        def _forbidden_factory():
            raise AssertionError("caller-session read opened a nested checkout")

        harness.store._session_factory = _forbidden_factory  # type: ignore[assignment]
        async with harness.session_factory() as session:
            assert (
                await harness.store.get_conversation_active(
                    "conv-a",
                    session=session,
                )
                is None
            )
            assert (
                await harness.store.list_conversations_active(
                    ["conv-a", "conv-b"],
                    session=session,
                )
                == {}
            )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("changed_field", [None, "ts", "sender", "content", "origin"])
async def test_metadata_origin_controls_admission_identity(tmp_path, generated, changed_field):
    harness = await _harness(tmp_path)
    try:
        metadata = {"ts": "2026-01-01T00:00:00Z", "sender": "Alice"}
        payload = {
            "schema_version": 1,
            "content": "request",
            "attachments": [],
            "metadata": {"user_message_metadata": metadata},
        }
        args = {
            "conversation_id": "conv-a",
            "session_id": None,
            "agent_id": "agent-1",
            "user_id": "user@example.com",
            "idempotency_scope": "web:conv-a:user@example.com",
            "idempotency_key": "metadata-replay",
            "generated_user_message_metadata": generated,
        }
        first = await harness.store.admit(**args, payload=payload)
        changed_metadata = dict(metadata)
        changed_payload = {**payload, "metadata": {"user_message_metadata": changed_metadata}}
        if changed_field == "ts":
            changed_metadata["ts"] = "2026-01-01T00:00:01Z"
        elif changed_field == "sender":
            changed_metadata["sender"] = "Bob"
        elif changed_field == "content":
            changed_payload["content"] = "different request"
        elif changed_field == "origin":
            args["generated_user_message_metadata"] = not generated
        if changed_field is None or (generated and changed_field == "ts"):
            replay = await harness.store.admit(**args, payload=changed_payload)
            assert not replay.created
            assert replay.request.request_id == first.request.request_id
            assert replay.request.payload == first.request.payload
            assert replay.request.payload_hash == first.request.payload_hash
        else:
            with pytest.raises(DirectTurnConflictError):
                await harness.store.admit(**args, payload=changed_payload)
        assert len(await harness.store.list_conversation_pending("conv-a")) == 1
        assert metadata["ts"] == "2026-01-01T00:00:00Z"
    finally:
        await harness.engine.dispose()


async def _admit(
    harness: _Harness,
    *,
    conversation_id: str = "conv-a",
    key: str,
    content: str,
):
    return await harness.store.admit(
        conversation_id=conversation_id,
        session_id=None,
        agent_id="agent-1",
        user_id="user@example.com",
        idempotency_scope=f"web:{conversation_id}:user@example.com",
        idempotency_key=key,
        payload={"schema_version": 1, "content": content, "attachments": []},
    )


def _evidence_admission(*, admitted: bool, fingerprint: str) -> dict[str, object]:
    return serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=admitted,
            owner_id="user@example.com",
            policy_fingerprint=fingerprint,
            event_binding=build_evidence_event_binding(
                intaris_session_id="stream-1",
                cognis_session_id="session-1",
                conversation_id="conv-a",
                turn_id="turn-1",
                user_id="user@example.com",
                owner_id="user@example.com",
                source="user_input",
                role="user",
                prompt_visibility="user_visible",
                prompt_provenance={"kind": "user_authored"},
                content_hash=hashlib.sha256(b"same event").hexdigest(),
                attachment_refs_value=[],
            ),
        )
    )


@pytest.mark.asyncio
async def test_duplicate_admission_keeps_first_frozen_evidence_decision(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        positive = _evidence_admission(
            admitted=True,
            fingerprint="0123456789abcdef",
        )
        negative = _evidence_admission(admitted=False, fingerprint="")
        common = {
            "schema_version": 1,
            "content": "same event",
            "attachments": [],
        }
        first = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="frozen-decision",
            payload={
                **common,
                "metadata": {TRUSTED_EVIDENCE_ADMISSION_KEY: positive},
            },
        )
        replay = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="frozen-decision",
            payload={
                **common,
                "metadata": {TRUSTED_EVIDENCE_ADMISSION_KEY: negative},
            },
        )

        assert first.created is True
        assert replay.created is False
        assert replay.request.payload["metadata"][TRUSTED_EVIDENCE_ADMISSION_KEY] == positive
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_edit_rejects_bound_content_and_preserves_frozen_evidence_decision(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        positive = _evidence_admission(
            admitted=True,
            fingerprint="0123456789abcdef",
        )
        admission = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="immutable-decision",
            payload={
                "schema_version": 1,
                "content": "before",
                "attachments": [],
                "metadata": {TRUSTED_EVIDENCE_ADMISSION_KEY: positive},
            },
        )
        from cognis.store.direct_turns import direct_turn_content_is_editable

        assert direct_turn_content_is_editable(admission.request) is False
        with pytest.raises(ValueError, match="content and attachments are immutable"):
            await harness.store.edit(
                admission.request.request_id,
                payload={
                    "schema_version": 1,
                    "content": "after",
                    "attachments": [],
                    "metadata": {},
                },
                payload_version=1,
                expected_payload_hash=admission.request.payload_hash,
            )

        edited = await harness.store.edit(
            admission.request.request_id,
            payload={
                "schema_version": 1,
                "content": "before",
                "attachments": [],
                "metadata": {"editor_note": "metadata-only"},
            },
            payload_version=1,
            expected_payload_hash=admission.request.payload_hash,
        )
        assert edited is not None
        assert edited.payload["metadata"][TRUSTED_EVIDENCE_ADMISSION_KEY] == positive
        with pytest.raises(ValueError, match="admission is immutable"):
            await harness.store.edit(
                edited.request_id,
                payload={
                    "schema_version": 1,
                    "content": "before",
                    "attachments": [],
                    "metadata": {
                        TRUSTED_EVIDENCE_ADMISSION_KEY: _evidence_admission(
                            admitted=False,
                            fingerprint="",
                        )
                    },
                },
                payload_version=1,
                expected_payload_hash=edited.payload_hash,
            )
    finally:
        await harness.engine.dispose()


async def _lease(
    harness: _Harness,
    *,
    conversation_id: str = "conv-a",
    controller_id: str = "controller-a",
    incarnation_id: str = "boot-a",
) -> Lease:
    lease = await harness.leases.acquire(
        conversation_lease_key(conversation_id),
        f"{controller_id}:{incarnation_id}",
        ttl_seconds=60,
    )
    assert lease is not None
    return lease


@pytest.mark.asyncio
async def test_admission_disposition_distinguishes_fifo_head_from_successor(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        head = await _admit(harness, key="head", content="run now")
        successor = await _admit(harness, key="successor", content="wait")

        assert head.request.status == DirectTurnStatus.QUEUED.value
        assert head.queued_behind_predecessor is False
        assert successor.request.status == DirectTurnStatus.QUEUED.value
        assert successor.queued_behind_predecessor is True

        replay = await _admit(harness, key="head", content="run now")
        assert replay.created is False
        assert replay.queued_behind_predecessor is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_operator_recovery_fences_stale_tool_effect_and_is_idempotent(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="operator-recovery", content="secret payload")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        checkpointed = await harness.store.checkpoint(
            admitted.request.request_id,
            lease=lease,
            phase="tool_in_flight",
            metadata={
                "call_id": "call-1",
                "tool_args": {"token": "must-not-leak"},
            },
        )
        assert checkpointed is not None
        expected = DirectTurnRecoverySnapshot(
            conversation_id="conv-a",
            status=checkpointed.status,
            phase="tool_in_flight",
            owner_controller_id="controller-a",
            owner_incarnation_id="boot-a",
            fencing_token=lease.fencing_token,
            updated_at=checkpointed.updated_at,
            phase_started_at=datetime.fromisoformat(checkpointed.outcome["phase_started_at"]),
        )
        with pytest.raises(DirectTurnRecoveryConflict, match="still live"):
            await harness.store.resolve_stale_tool_ambiguous(
                admitted.request.request_id,
                actor_email="admin@example.com",
                reason="Controller is gone; external effect is uncertain",
                client_transaction_id="txn-1",
                expected=expected,
            )
        assert await harness.leases.release(lease)

        result = await harness.store.resolve_stale_tool_ambiguous(
            admitted.request.request_id,
            actor_email="admin@example.com",
            reason="Controller is gone; external effect is uncertain",
            client_transaction_id="txn-1",
            expected=expected,
        )
        assert result.changed is True
        assert result.status == DirectTurnStatus.AMBIGUOUS.value
        assert (
            await harness.store.mark_terminal(
                admitted.request.request_id,
                lease=lease,
                status=DirectTurnStatus.COMPLETED,
            )
            is None
        )
        replay = await harness.store.resolve_stale_tool_ambiguous(
            admitted.request.request_id,
            actor_email="admin@example.com",
            reason="Controller is gone; external effect is uncertain",
            client_transaction_id="txn-1",
            expected=expected,
        )
        assert replay.changed is False
        for actor, transaction_id in [
            ("admin@example.com", "txn-2"),
            ("other-admin@example.com", "txn-1"),
        ]:
            with pytest.raises(DirectTurnRecoveryConflict) as replay_conflict:
                await harness.store.resolve_stale_tool_ambiguous(
                    admitted.request.request_id,
                    actor_email=actor,
                    reason="Controller is gone; external effect is uncertain",
                    client_transaction_id=transaction_id,
                    expected=expected,
                )
            assert replay_conflict.value.code == "idempotency_conflict"
        async with harness.session_factory() as session:
            audits = list(
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.event_type == "direct_turn_operator_recovery"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(audits) == 1
        audit_text = str(audits[0].details)
        assert "secret payload" not in audit_text
        assert "must-not-leak" not in audit_text
        assert "call-1" not in audit_text
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_operator_recovery_cannot_preempt_lease_acquired_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="operator-race", content="hello")
        stale_lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=stale_lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        checkpointed = await harness.store.checkpoint(
            admitted.request.request_id,
            lease=stale_lease,
            phase="tool_in_flight",
        )
        assert checkpointed is not None
        assert await harness.leases.release(stale_lease)
        expected = DirectTurnRecoverySnapshot(
            conversation_id="conv-a",
            status=checkpointed.status,
            phase="tool_in_flight",
            owner_controller_id="controller-a",
            owner_incarnation_id="boot-a",
            fencing_token=stale_lease.fencing_token,
            updated_at=checkpointed.updated_at,
            phase_started_at=datetime.fromisoformat(checkpointed.outcome["phase_started_at"]),
        )
        original_acquire = DatabaseLeaseStore.acquire_in_session
        competing_lease: Lease | None = None

        async def _race_acquire(
            lease_store: DatabaseLeaseStore,
            session: AsyncSession,
            resource_key: str,
            owner_id: str,
            *,
            ttl_seconds: float,
        ) -> Lease | None:
            nonlocal competing_lease
            competing_lease = await harness.leases.acquire(
                resource_key,
                "controller-b:boot-b",
                ttl_seconds=60,
            )
            assert competing_lease is not None
            return await original_acquire(
                lease_store,
                session,
                resource_key,
                owner_id,
                ttl_seconds=ttl_seconds,
            )

        monkeypatch.setattr(DatabaseLeaseStore, "acquire_in_session", _race_acquire)

        with pytest.raises(DirectTurnRecoveryConflict) as conflict:
            await harness.store.resolve_stale_tool_ambiguous(
                admitted.request.request_id,
                actor_email="admin@example.com",
                reason="Controller is gone",
                client_transaction_id="txn-race",
                expected=expected,
            )
        assert conflict.value.code == "lease_live"
        row = await harness.store.get(admitted.request.request_id)
        assert row is not None
        assert row.status == checkpointed.status
        assert row.fencing_token == stale_lease.fencing_token
        assert competing_lease is not None
        assert await harness.leases.is_current(competing_lease)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_operator_recovery_rejects_snapshot_mismatch(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="snapshot-mismatch", content="hello")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        checkpointed = await harness.store.checkpoint(
            admitted.request.request_id,
            lease=lease,
            phase="tool_in_flight",
        )
        assert checkpointed is not None
        assert await harness.leases.release(lease)
        with pytest.raises(DirectTurnRecoveryConflict) as exc:
            await harness.store.resolve_stale_tool_ambiguous(
                admitted.request.request_id,
                actor_email="admin@example.com",
                reason="operator recovery",
                client_transaction_id="txn-mismatch",
                expected=DirectTurnRecoverySnapshot(
                    conversation_id="conv-b",
                    status=checkpointed.status,
                    phase="tool_in_flight",
                    owner_controller_id="controller-a",
                    owner_incarnation_id="boot-a",
                    fencing_token=lease.fencing_token,
                    updated_at=checkpointed.updated_at,
                ),
            )
        assert exc.value.code == "snapshot_mismatch"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_admission_is_idempotent_editable_and_cancellable(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        first = await _admit(harness, key="message-1", content="original")
        replay = await _admit(harness, key="message-1", content="original")

        assert first.created is True
        assert replay.created is False
        assert replay.request.request_id == first.request.request_id
        assert replay.request.turn_id == first.request.turn_id
        assert replay.request.admission_order == first.request.admission_order

        with pytest.raises(DirectTurnConflictError, match="different request"):
            await _admit(harness, key="message-1", content="conflict")

        rotated_replay = await harness.store.admit(
            conversation_id="conv-a",
            session_id="sess-after-rotation",
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="message-1",
            payload={
                "schema_version": 1,
                "content": "original",
                "attachments": [],
            },
        )
        assert rotated_replay.created is False
        assert rotated_replay.request.request_id == first.request.request_id

        edited = await harness.store.edit(
            first.request.request_id,
            payload={"schema_version": 1, "content": "edited", "attachments": []},
            payload_version=1,
            expected_payload_hash=first.request.payload_hash,
        )
        assert edited is not None
        assert edited.payload["content"] == "edited"
        assert edited.admission_hash == first.request.admission_hash
        assert edited.payload_hash != first.request.payload_hash

        cancelled = await harness.store.request_cancel(first.request.request_id)
        assert cancelled is not None
        assert cancelled.cancellation_requested is False
        assert cancelled.request.status == DirectTurnStatus.CANCELLED.value
        assert cancelled.request.terminal_at is not None
        assert (
            await harness.store.edit(
                first.request.request_id,
                payload={"schema_version": 1, "content": "late", "attachments": []},
                payload_version=1,
            )
            is None
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_transient_settlement_preserves_fence_for_pending_cancellation(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(
            harness,
            key="transient-cancel",
            content="hello",
        )
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert await harness.store.mark_running(
            admitted.request.request_id,
            lease=lease,
        )
        cancelled = await harness.store.request_cancel(admitted.request.request_id)
        assert cancelled is not None
        assert cancelled.cancellation_requested is True

        pending = await harness.store.settle_transient_failure(
            admitted.request.request_id,
            lease=lease,
            outcome={"phase": "transient_turn_error"},
        )

        assert pending is not None
        assert pending.status == DirectTurnStatus.RUNNING.value
        assert pending.cancel_requested_at is not None
        assert pending.fencing_token == lease.fencing_token
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_phase", ["final", "error"])
async def test_fenced_channel_delivery_is_idempotent_and_reply_anchored(
    tmp_path: Path,
    terminal_phase: str,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="channel-message", content="hello")
        lease = await _lease(
            harness,
            controller_id="controller-b",
            incarnation_id="boot-b",
        )
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-b",
            incarnation_id="boot-b",
        )
        descriptor = ChannelDeliveryDescriptor(
            channel_type="matrix",
            account_id="account-a",
            chat_id="room-a",
            thread_id="thread-a",
            reply_to_id="$inbound",
        )
        first = await harness.store.create_fenced_channel_delivery(
            request_id=admitted.request.request_id,
            lease=lease,
            delivery_id=f"direct-turn:{admitted.request.request_id}:{terminal_phase}",
            descriptor=descriptor.model_dump(mode="json"),
            content="answer",
            attachments=[{"artifact_id": "artifact-a"}],
        )
        second = await harness.store.create_fenced_channel_delivery(
            request_id=admitted.request.request_id,
            lease=lease,
            delivery_id=first.delivery_id if first is not None else "missing",
            descriptor=descriptor.model_dump(mode="json"),
            content="answer",
            attachments=[{"artifact_id": "artifact-a"}],
        )
        assert first is not None
        assert second is not None
        assert second.delivery_id == first.delivery_id
        assert second.reply_to_id == "$inbound"
        assert second.thread_id == "thread-a"
        assert second.direct_turn_request_id == admitted.request.request_id

        stale = Lease(
            resource_key=lease.resource_key,
            owner_id="controller-a:boot-a",
            fencing_token=lease.fencing_token,
            lease_expires_at=lease.lease_expires_at,
        )
        assert (
            await harness.store.create_fenced_channel_delivery(
                request_id=admitted.request.request_id,
                lease=stale,
                delivery_id=first.delivery_id,
                descriptor=descriptor.model_dump(mode="json"),
                content="stale answer",
                attachments=None,
            )
            is None
        )

        other = await _admit(
            harness,
            conversation_id="conv-b",
            key="other-channel-message",
            content="other",
        )
        other_lease = await _lease(
            harness,
            conversation_id="conv-b",
            controller_id="controller-b",
            incarnation_id="boot-b",
        )
        assert other_lease.fencing_token == lease.fencing_token
        assert await harness.store.claim(
            other.request.request_id,
            lease=other_lease,
            controller_id="controller-b",
            incarnation_id="boot-b",
        )
        assert (
            await harness.store.create_fenced_channel_delivery(
                request_id=admitted.request.request_id,
                lease=other_lease,
                delivery_id=f"direct-turn:{admitted.request.request_id}:{terminal_phase}",
                descriptor=descriptor.model_dump(mode="json"),
                content="wrong conversation",
                attachments=None,
            )
            is None
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("absorbed", [False, True])
@pytest.mark.parametrize("terminal_error", [False, True])
async def test_immediate_progress_uses_shared_outbox_on_adapter_owner(
    tmp_path: Path, absorbed: bool, terminal_error: bool
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cognis.channels.delivery import ChannelDeliveryService
    from cognis.channels.source_delivery import pending_sources, progress_delivery_id
    from cognis.core.events import Event, EventBus, EventType
    from cognis.core.turn_scheduler import TurnScheduler
    from cognis.models.channel import ChannelCapabilities
    from cognis.store.queries import (
        create_or_get_channel_delivery_outbox,
        create_session,
        ensure_follow_up_result_delivery,
        get_channel_delivery_outbox,
    )

    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as db:
            source_session = await create_session(
                db, conversation_id="conv-a", user_email="user@example.com", agent_id="agent-1"
            )
            await db.commit()
        descriptor = ChannelDeliveryDescriptor(
            channel_type="matrix",
            account_id="account-a",
            chat_id="room-a",
        )
        admission = await harness.store.admit(
            conversation_id="conv-a",
            session_id=source_session.session_id,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="matrix:account-a:room-a",
            idempotency_key="immediate-message",
            payload={
                "schema_version": 1,
                "content": "request",
                "attachments": [],
                "channel_delivery": None if absorbed else descriptor.model_dump(mode="json"),
                "metadata": (
                    {"system_initiated": True}
                    if absorbed
                    else {"channel_assistant_delivery_mode": "immediate"}
                ),
            },
        )
        request = admission.request
        lease = await _lease(harness, controller_id="controller-b", incarnation_id="boot-b")
        assert await harness.store.claim(
            request.request_id, lease=lease, controller_id="controller-b", incarnation_id="boot-b"
        )
        if absorbed:
            participant = await harness.store.admit(
                conversation_id="conv-a",
                session_id=source_session.session_id,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="matrix:account-a:room-a",
                idempotency_key="participant",
                payload={
                    "schema_version": 1,
                    "content": "Continue here.",
                    "attachments": [],
                    "channel_delivery": descriptor.model_dump(mode="json"),
                    "metadata": {"channel_assistant_delivery_mode": "immediate"},
                },
            )
            assert await harness.store.begin_absorb(
                participant.request.request_id,
                lease=lease,
                controller_id="controller-b",
                incarnation_id="boot-b",
                absorbed_by_turn_id=request.turn_id,
            )
            # Uncommitted absorption must not change the effective route.
            assert await harness.store.effective_channel_delivery(request.request_id) == (
                None,
                None,
            )
            assert await harness.store.mark_absorbed(participant.request.request_id, lease=lease)
        # A fresh store instance resolves adoption without any runtime control cache.
        recovered_store = DirectTurnStore(harness.session_factory)
        assert await recovered_store.effective_channel_delivery(request.request_id) == (
            descriptor,
            "immediate",
        )
        unchanged = await harness.store.get(request.request_id)
        assert unchanged.payload == admission.request.payload
        adapter = SimpleNamespace(
            capabilities=ChannelCapabilities(supports_idempotent_send=True),
            send_message=AsyncMock(return_value="$progress"),
        )
        owner_manager = SimpleNamespace(
            owns_account=lambda _: True,
            find_adapter_for_channel=lambda *args: (adapter, object()),
        )
        cluster = SimpleNamespace(publish=AsyncMock())
        worker_delivery = ChannelDeliveryService(
            session_factory=harness.session_factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: SimpleNamespace(owns_account=lambda _: False),
            cluster_signals=cluster,
        )
        owner_delivery = ChannelDeliveryService(
            session_factory=harness.session_factory,
            event_bus=EventBus(),
            channel_manager_ref=lambda: owner_manager,
        )
        events = [
            {
                "type": "lifecycle",
                "seq": 1,
                "data": {
                    "event": "channel_source_delivery",
                    "state": "tracking",
                    "turn_id": request.turn_id,
                },
            },
            {
                "type": "assistant_message",
                "seq": 2,
                "data": {"turn_id": request.turn_id, "content": "Checking now."},
            },
        ]
        stream = SimpleNamespace(
            read_events=AsyncMock(return_value=SimpleNamespace(events=events, has_more=False))
        )
        scheduler = object.__new__(TurnScheduler)
        scheduler._turn_controls = {}
        scheduler._durable_request_by_conversation = {"conv-a": request.request_id}
        scheduler._durable_fences = {
            request.request_id: SimpleNamespace(lease=lease, assert_current=AsyncMock())
        }
        scheduler._direct_turn_store = recovered_store
        scheduler._channel_delivery = worker_delivery
        scheduler.channel_pending_sources = lambda *args, **kwargs: pending_sources(
            stream,
            [source_session.session_id],
            request.turn_id,
            is_queued=worker_delivery.source_is_queued,
            **kwargs,
        )
        scheduler._reset_active_stream = AsyncMock()
        scheduler._bump_assistant_phase_for_tool = lambda *args: 0
        scheduler._effective_turn_cycle_for_tool = lambda *args: 0
        scheduler._record_active_tool_arguments = AsyncMock()
        scheduler._iter_observers = lambda *args, **kwargs: []
        on_tool_call = scheduler._build_callbacks(
            "conv-a", source_session.session_id, request.turn_id, request.turn_id
        )[2]
        await on_tool_call("read", "call-read", {})
        adapter.send_message.assert_not_awaited()
        _, sources = await pending_sources(stream, [source_session.session_id], request.turn_id)
        delivery_id = progress_delivery_id(sources[0])
        async with harness.session_factory() as db:
            progress = await get_channel_delivery_outbox(db, delivery_id)
        assert progress is not None and progress.status == "pending"
        signal = cluster.publish.await_args
        assert signal.kwargs["scope"].direct_request_id == request.request_id
        assert signal.kwargs["revision"] == progress.delivery_id
        await owner_delivery._event_bus.publish(
            Event(
                type=EventType.CLUSTER_SCOPE_INVALIDATED,
                data={"kind": signal.args[0]},
            )
        )
        assert owner_delivery._retry_wakeup.is_set()
        await owner_delivery.recover_pending_deliveries()
        assert adapter.send_message.await_args.args[0].content == "Checking now."
        await scheduler._deliver_durable_channel_progress("conv-a")
        await owner_delivery.recover_pending_deliveries()
        adapter.send_message.assert_awaited_once()
        assert await scheduler.channel_pending_sources(
            "conv-a", source_session.session_id, request.turn_id
        ) == ("", [])
        async with harness.session_factory() as db:
            await create_or_get_channel_delivery_outbox(
                db,
                delivery_id="grace-follow-up",
                user_email="user@example.com",
                conversation_id="conv-a",
                session_id=source_session.session_id,
                source_type="follow_up",
                source_id="scheduled-notification",
                channel_type="matrix",
                account_id="account-a",
                chat_id="room-a",
                thread_id=None,
                fallback_text="Notification.",
            )
            await db.commit()
        events.append(
            {
                "type": "assistant_message",
                "seq": 3,
                "data": {"turn_id": request.turn_id, "content": "Done.", "turn_cycle_index": 1},
            }
        )
        await scheduler._persist_direct_turn_terminal_delivery(
            request_id=request.request_id,
            lease=lease,
            descriptor=None,
            content="Failed." if terminal_error else "Done.",
            attachments=None,
            error=terminal_error,
        )
        terminal_id = f"direct-turn:{request.request_id}:{'error' if terminal_error else 'final'}"
        if not terminal_error:
            # The scheduler persists the follow-up result before the event handler.
            # Both must reuse the already queued terminal row, not create another.
            async with harness.session_factory() as db:
                row = await ensure_follow_up_result_delivery(
                    db,
                    grace_delivery_id="grace-follow-up",
                    conversation_id="conv-a",
                    session_id=source_session.session_id,
                    turn_id=request.turn_id,
                    final_content="Done.",
                    attachments=None,
                    deliverable_id=None,
                )
                assert row.delivery_id == terminal_id
                await db.commit()
        completed = Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-a",
                "session_id": source_session.session_id,
                "turn_id": request.turn_id,
                "delivery_id": "grace-follow-up",
                "channel_deliverable": True,
                "final_content": "Done.",
            },
        )
        if terminal_error:
            failed = Event(type=EventType.TURN_ERROR, data=completed.data)
            await owner_delivery._handle_turn_error_event(failed)
            await owner_delivery._handle_turn_error_event(failed)
        else:
            await owner_delivery._handle_turn_completed_event(completed)
            await owner_delivery._handle_turn_completed_event(completed)
        await owner_delivery.recover_pending_deliveries()
        assert [call.args[0].content for call in adapter.send_message.await_args_list] == [
            "Checking now.",
            "Done.\n\nFailed." if terminal_error else "Done.",
        ]
        async with harness.session_factory() as db:
            grace = await get_channel_delivery_outbox(db, "grace-follow-up")
            assert grace.status == "suppressed"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_effective_channel_route_uses_absorbed_admission_order(tmp_path: Path) -> None:
    from sqlalchemy import update

    from cognis.store.models import DirectTurnRequestRow

    harness = await _harness(tmp_path)
    try:
        active = (await _admit(harness, key="active", content="Start")).request
        lease = await _lease(harness)
        assert await harness.store.claim(
            active.request_id, lease=lease, controller_id="controller-a", incarnation_id="boot-a"
        )
        absorbed_rows = []
        for anchor, mode in (("first-anchor", "concatenated"), ("last-anchor", "immediate")):
            descriptor = ChannelDeliveryDescriptor(
                channel_type="matrix", account_id="account-a", chat_id="room-a", reply_to_id=anchor
            )
            admitted = await harness.store.admit(
                conversation_id="conv-a",
                session_id=active.session_id,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="matrix:account-a:room-a",
                idempotency_key=anchor,
                payload={
                    "schema_version": 1,
                    "content": anchor,
                    "attachments": [],
                    "channel_delivery": descriptor.model_dump(mode="json"),
                    "metadata": {"channel_assistant_delivery_mode": mode},
                },
            )
            absorbed_rows.append(admitted.request)
            assert await harness.store.begin_absorb(
                admitted.request.request_id,
                lease=lease,
                controller_id="controller-a",
                incarnation_id="boot-a",
                absorbed_by_turn_id=active.turn_id,
            )
            assert await harness.store.mark_absorbed(admitted.request.request_id, lease=lease)
        # Timestamps can tie; admission_order, not random request IDs, is authoritative.
        async with harness.session_factory() as db:
            await db.execute(
                update(DirectTurnRequestRow)
                .where(DirectTurnRequestRow.request_id.in_([r.request_id for r in absorbed_rows]))
                .values(created_at=active.created_at)
            )
            await db.commit()
        assert await DirectTurnStore(harness.session_factory).effective_channel_delivery(
            active.request_id
        ) == (descriptor, "immediate")
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_survives_queued_to_claim_race(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="hello")
        lease = await _lease(harness)
        entered = asyncio.Event()
        release = asyncio.Event()

        cancelling_store = DirectTurnStore(  # type: ignore[arg-type]
            _PausingUpdateFactory(harness.session_factory, entered, release)
        )
        cancel_task = asyncio.create_task(
            cancelling_store.request_cancel(admitted.request.request_id)
        )
        await entered.wait()
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        release.set()
        cancelled = await cancel_task

        assert claimed is not None
        assert claimed.status == DirectTurnStatus.CLAIMED.value
        assert cancelled is not None
        assert cancelled.request.status == DirectTurnStatus.CLAIMED.value
        assert cancelled.cancellation_requested is True
        stored = await harness.store.get(admitted.request.request_id)
        assert stored is not None
        assert stored.status == DirectTurnStatus.CLAIMED.value
        assert stored.cancel_requested_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_survives_claimed_to_running_race(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="hello")
        lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelling_store = DirectTurnStore(  # type: ignore[arg-type]
            _PausingUpdateFactory(harness.session_factory, entered, release)
        )
        cancel_task = asyncio.create_task(
            cancelling_store.request_cancel(admitted.request.request_id)
        )
        await entered.wait()
        running = await harness.store.mark_running(
            admitted.request.request_id,
            lease=lease,
        )
        release.set()
        cancelled = await cancel_task

        assert running is not None
        assert running.status == DirectTurnStatus.RUNNING.value
        assert cancelled is not None
        assert cancelled.request.status == DirectTurnStatus.RUNNING.value
        assert cancelled.cancellation_requested is True
        stored = await harness.store.get(admitted.request.request_id)
        assert stored is not None
        assert stored.status == DirectTurnStatus.RUNNING.value
        assert stored.cancel_requested_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_can_terminally_win_before_claim(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="hello")
        lease = await _lease(harness)
        cancelled = await harness.store.request_cancel(admitted.request.request_id)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )

        assert cancelled is not None
        assert cancelled.request.status == DirectTurnStatus.CANCELLED.value
        assert cancelled.cancellation_requested is False
        assert claimed is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_admission_rejects_invalid_or_mismatched_payload_versions(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        with pytest.raises(ValueError, match="unsupported direct-turn payload version"):
            await harness.store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="web:conv-a:user@example.com",
                idempotency_key="future",
                payload={"schema_version": 2, "content": "future"},
                payload_version=2,
            )
        with pytest.raises(ValueError, match="schema_version"):
            await harness.store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="web:conv-a:user@example.com",
                idempotency_key="mismatch",
                payload={"schema_version": 2, "content": "mismatch"},
                payload_version=1,
            )
        with pytest.raises(ValueError, match="validation error"):
            await harness.store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="web:conv-a:user@example.com",
                idempotency_key="missing-content",
                payload={"schema_version": 1},
            )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_agent", [False, True])
async def test_attachment_urls_are_transient_and_refreshed_after_recovery(
    tmp_path: Path, shared_agent: bool
) -> None:
    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as session:
            if shared_agent:
                from cognis.store.models import Agent, AgentGrantRow

                await create_user(
                    session, "agent-owner@example.com", "Agent owner", password_hash="test-hash"
                )
                agent = await session.get(Agent, "agent-1")
                assert agent is not None
                agent.owner_email = "agent-owner@example.com"
                session.add(
                    AgentGrantRow(
                        grant_id="grant-shared-agent",
                        agent_id=agent.agent_id,
                        grantee_user_email="user@example.com",
                        granted_by=agent.owner_email,
                    )
                )
            session.add(
                ArtifactRecordRow(
                    artifact_id="art_input",
                    namespace="attachments",
                    object_id="obj_input",
                    filename="input.pdf",
                    owner_email="user@example.com",
                    conversation_id="conv-a",
                    purpose="chat_input",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=123,
                    status="attached",
                )
            )
            await session.commit()

        def payload(url: str) -> dict[str, object]:
            return {
                "schema_version": 1,
                "content": "review this",
                "attachments": [
                    {
                        "artifact_id": "art_input",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "input.pdf",
                        "size_bytes": 123,
                        "url": url,
                    }
                ],
            }

        first = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="message-with-attachment",
            payload=payload("https://files.invalid/expired?signature=old"),
        )
        replay = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="message-with-attachment",
            payload=payload("https://files.invalid/refreshed?signature=new"),
        )

        assert first.created is True
        assert replay.created is False
        assert replay.request.admission_hash == first.request.admission_hash
        assert replay.request.payload_hash == first.request.payload_hash
        assert "url" not in first.request.payload["attachments"][0]

        class RotatingArtifactStore:
            def __init__(self) -> None:
                self.calls = 0

            @property
            def signed_url_ttl_seconds(self) -> int:
                return 3600

            async def async_get_public_url(
                self,
                namespace: str,
                object_id: str,
                filename: str,
                *,
                ttl_seconds: int | None = None,
                mode: str = "download",
                expires_at: datetime | None = None,
            ) -> str:
                del ttl_seconds, mode, expires_at
                self.calls += 1
                return (
                    f"https://files.invalid/{namespace}/{object_id}/{filename}"
                    f"?generation={self.calls}"
                )

        artifact_store = RotatingArtifactStore()
        first_lease = await _lease(harness)
        claimed = await harness.store.claim(
            first.request.request_id,
            lease=first_lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        materialized = await harness.store.materialize_claimed_payload(
            first.request.request_id,
            lease=first_lease,
            artifact_store=artifact_store,
        )
        assert materialized is not None
        assert materialized.attachments[0].url.endswith("generation=1")
        assert await harness.leases.release(first_lease)

        successor = await _lease(
            harness,
            controller_id="controller-a",
            incarnation_id="boot-b",
        )
        recovered = await harness.store.recover_stale_claim(
            first.request.request_id,
            lease=successor,
            outcome={"reason": "signed URL expired during owner restart"},
        )
        assert recovered is not None
        reclaimed = await harness.store.claim(
            first.request.request_id,
            lease=successor,
            controller_id="controller-a",
            incarnation_id="boot-b",
        )
        assert reclaimed is not None
        rematerialized = await harness.store.materialize_claimed_payload(
            first.request.request_id,
            lease=successor,
            artifact_store=artifact_store,
        )
        assert rematerialized is not None
        assert rematerialized.attachments[0].url.endswith("generation=2")
        stored = await harness.store.get(first.request.request_id)
        assert stored is not None
        assert stored.payload_hash == first.request.payload_hash
        assert "url" not in stored.payload["attachments"][0]

        entered = asyncio.Event()
        release = asyncio.Event()
        connection_events: list[str] = []

        @event.listens_for(harness.engine.sync_engine, "checkout")
        def _checkout(*_args: object) -> None:
            connection_events.append("checkout")

        @event.listens_for(harness.engine.sync_engine, "checkin")
        def _checkin(*_args: object) -> None:
            connection_events.append("checkin")

        class BlockingArtifactStore:
            @property
            def signed_url_ttl_seconds(self) -> int:
                return 3600

            async def async_get_public_url(
                self,
                namespace: str,
                object_id: str,
                filename: str,
                *,
                ttl_seconds: int | None = None,
                mode: str = "download",
                expires_at: datetime | None = None,
            ) -> str:
                del namespace, object_id, filename, ttl_seconds, mode, expires_at
                assert connection_events[-1] == "checkin"
                entered.set()
                await release.wait()
                return "https://files.invalid/fresh-after-fence-loss"

        materialize_task = asyncio.create_task(
            harness.store.materialize_claimed_payload(
                first.request.request_id,
                lease=successor,
                artifact_store=BlockingArtifactStore(),
            )
        )
        await entered.wait()
        assert await harness.leases.release(successor)
        replacement = await _lease(
            harness,
            controller_id="controller-a",
            incarnation_id="boot-c",
        )
        assert replacement.fencing_token > successor.fencing_token
        release.set()
        assert await materialize_task is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_agent", [False, True])
async def test_attachment_materialization_enforces_agent_scope(
    tmp_path: Path, same_agent: bool
) -> None:
    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as session:
            await create_agent(
                session, agent_id="agent-other", owner_email="user@example.com", name="Other"
            )
            source = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1" if same_agent else "agent-other",
                context_type="web",
                context_ref=None,
                context_data={},
            )
            session.add(
                ArtifactRecordRow(
                    artifact_id="art_sibling",
                    namespace="attachments",
                    object_id="obj_sibling",
                    filename="sibling.pdf",
                    owner_email="user@example.com",
                    owner_agent_id="agent-1" if same_agent else "agent-other",
                    conversation_id=source.conversation_id,
                    purpose="chat_input",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=42,
                    status="attached",
                )
            )
            await session.commit()
        admitted = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="sibling-attachment",
            payload={
                "schema_version": 1,
                "content": "read sibling",
                "attachments": [
                    {
                        "artifact_id": "art_sibling",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "sibling.pdf",
                        "size_bytes": 42,
                    }
                ],
            },
        )
        lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None

        class ArtifactStore:
            @property
            def signed_url_ttl_seconds(self) -> int:
                return 3600

            async def async_get_public_url(
                self,
                namespace: str,
                object_id: str,
                filename: str,
                *,
                ttl_seconds: int | None = None,
                mode: str = "download",
                expires_at: datetime | None = None,
            ) -> str:
                del namespace, object_id, filename, ttl_seconds, mode, expires_at
                return "https://files.invalid/forbidden"

        if same_agent:
            assert await harness.store.materialize_claimed_payload(
                admitted.request.request_id,
                lease=lease,
                artifact_store=ArtifactStore(),
            )
        else:
            with pytest.raises(PermanentDirectTurnPayloadError, match="Attachment access denied"):
                await harness.store.materialize_claimed_payload(
                    admitted.request.request_id,
                    lease=lease,
                    artifact_store=ArtifactStore(),
                )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expires_at", "deleted_at", "error"),
    [
        (
            "attached",
            datetime.now(UTC) - timedelta(minutes=1),
            None,
            "Attachment access denied",
        ),
        (
            "deleted",
            None,
            None,
            "Attachment is unavailable",
        ),
        (
            "attached",
            None,
            datetime.now(UTC) - timedelta(minutes=1),
            "Attachment is unavailable",
        ),
    ],
)
async def test_attachment_materialization_rejects_unusable_artifacts(
    tmp_path: Path,
    status: str,
    expires_at: datetime | None,
    deleted_at: datetime | None,
    error: str,
) -> None:
    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as session:
            session.add(
                ArtifactRecordRow(
                    artifact_id="art_unusable",
                    namespace="attachments",
                    object_id="obj_unusable",
                    filename="unusable.pdf",
                    owner_email="user@example.com",
                    conversation_id="conv-a",
                    purpose="chat_input",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=42,
                    status=status,
                    expires_at=expires_at,
                    deleted_at=deleted_at,
                )
            )
            await session.commit()
        admitted = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="unusable-attachment",
            payload={
                "schema_version": 1,
                "content": "read unusable",
                "attachments": [
                    {
                        "artifact_id": "art_unusable",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "unusable.pdf",
                        "size_bytes": 42,
                    }
                ],
            },
        )
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )

        class ArtifactStore:
            calls = 0

            @property
            def signed_url_ttl_seconds(self) -> int:
                return 3600

            async def async_get_public_url(
                self,
                namespace: str,
                object_id: str,
                filename: str,
                *,
                ttl_seconds: int | None = None,
                mode: str = "download",
                expires_at: datetime | None = None,
            ) -> str:
                del namespace, object_id, filename, ttl_seconds, mode, expires_at
                self.calls += 1
                return "https://files.invalid/unusable"

        artifact_store = ArtifactStore()
        with pytest.raises(PermanentDirectTurnPayloadError, match=error):
            await harness.store.materialize_claimed_payload(
                admitted.request.request_id,
                lease=lease,
                artifact_store=artifact_store,
            )
        assert artifact_store.calls == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_attachment_url_ttl_is_clamped_to_near_expiry(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        expires_at = datetime.now(UTC) + timedelta(seconds=30)
        async with harness.session_factory() as session:
            session.add(
                ArtifactRecordRow(
                    artifact_id="art_near_expiry",
                    namespace="attachments",
                    object_id="obj_near_expiry",
                    filename="near-expiry.pdf",
                    owner_email="user@example.com",
                    conversation_id="conv-a",
                    purpose="chat_input",
                    kind="pdf",
                    mime_type="application/pdf",
                    size_bytes=42,
                    status="attached",
                    expires_at=expires_at,
                )
            )
            await session.commit()
        admitted = await harness.store.admit(
            conversation_id="conv-a",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-a:user@example.com",
            idempotency_key="near-expiry-attachment",
            payload={
                "schema_version": 1,
                "content": "read soon",
                "attachments": [
                    {
                        "artifact_id": "art_near_expiry",
                        "kind": "pdf",
                        "mime_type": "application/pdf",
                        "filename": "near-expiry.pdf",
                        "size_bytes": 42,
                    }
                ],
            },
        )
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )

        class ArtifactStore:
            captured_ttl: int | None = None
            captured_expiry: datetime | None = None

            @property
            def signed_url_ttl_seconds(self) -> int:
                return 3600

            async def async_get_public_url(
                self,
                namespace: str,
                object_id: str,
                filename: str,
                *,
                ttl_seconds: int | None = None,
                mode: str = "download",
                expires_at: datetime | None = None,
            ) -> str:
                del namespace, object_id, filename, mode
                self.captured_ttl = ttl_seconds
                self.captured_expiry = expires_at
                return "https://files.invalid/near-expiry"

        artifact_store = ArtifactStore()
        materialized = await harness.store.materialize_claimed_payload(
            admitted.request.request_id,
            lease=lease,
            artifact_store=artifact_store,
        )
        assert materialized is not None
        assert artifact_store.captured_ttl is not None
        assert 1 <= artifact_store.captured_ttl <= 30
        assert artifact_store.captured_expiry == expires_at
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_claimable_heads_preserve_fifo_per_conversation(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        first = await _admit(harness, key="a-1", content="first")
        second = await _admit(harness, key="a-2", content="second")
        other = await _admit(
            harness,
            conversation_id="conv-b",
            key="b-1",
            content="other",
        )

        heads = await harness.store.list_claimable_heads()
        assert [row.request_id for row in heads] == [
            first.request.request_id,
            other.request.request_id,
        ]

        lease = await _lease(harness)
        assert (
            await harness.store.claim(
                second.request.request_id,
                lease=lease,
                controller_id="controller-a",
                incarnation_id="boot-a",
            )
            is None
        )
        claimed = await harness.store.claim(
            first.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            session_id="sess-1",
        )
        assert claimed is not None
        assert claimed.status == DirectTurnStatus.CLAIMED.value
        assert claimed.session_id == "sess-1"
        assert claimed.attempt_count == 1
        assert await harness.store.has_fence(first.request.request_id, lease=lease)

        running = await harness.store.mark_running(first.request.request_id, lease=lease)
        assert running is not None
        completed = await harness.store.mark_terminal(
            first.request.request_id,
            lease=lease,
            status=DirectTurnStatus.COMPLETED,
            outcome={"last_seq": 9},
        )
        assert completed is not None
        assert completed.terminal_at is not None

        heads = await harness.store.list_claimable_heads()
        assert [row.request_id for row in heads] == [
            second.request.request_id,
            other.request.request_id,
        ]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_runtime_authority_retains_terminal_fence_without_authorizing_work(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="authority-terminal", content="hello")
        lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            session_id="sess-1",
        )
        assert claimed is not None
        terminal = await harness.store.mark_terminal(
            admitted.request.request_id,
            lease=lease,
            status=DirectTurnStatus.COMPLETED,
        )
        assert terminal is not None
        assert terminal.fencing_token == lease.fencing_token
        assert terminal.owner_controller_id is not None
        authority_row = await harness.store.get_conversation_runtime_authority_row("conv-a")
        assert authority_row is not None
        assert authority_row.fencing_token == lease.fencing_token
        assert await harness.store.mark_running(admitted.request.request_id, lease=lease) is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_runtime_authority_survives_absorption_retry_and_stop(
    tmp_path: Path, retry: bool
) -> None:
    harness = await _harness(tmp_path)
    try:
        first = (await _admit(harness, key="authority-head", content="first")).request
        lease = await _lease(harness)
        assert await harness.store.claim(
            first.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            session_id="sess-1",
        )
        second = (await _admit(harness, key="authority-absorbed", content="second")).request
        assert await harness.store.begin_absorb(
            second.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            absorbed_by_turn_id=first.turn_id,
        )
        assert await harness.store.mark_absorbed(second.request_id, lease=lease)
        if retry:
            assert await harness.store.settle_transient_failure(
                first.request_id,
                lease=lease,
                outcome={
                    "phase": "user_appended",
                    "retry_target": {
                        "scope": "provider_model",
                        "runtime_revision": 0,
                        "provider_id": "anthropic",
                        "model": "claude-fable-5-1",
                    },
                },
                retry_after_seconds=60,
            )
            waiting = await harness.store.get_conversation_runtime_authority_row("conv-a")
            assert waiting is not None
            assert waiting.request_id == first.request_id
            assert waiting.status == DirectTurnStatus.RECOVERABLE.value
            assert await harness.store.expedite_provider_retry(
                "conv-a", session_id="sess-1", previous_runtime_revision=0
            )
            assert await harness.leases.release(lease)
            lease = await _lease(harness, controller_id="controller-b", incarnation_id="boot-b")
            assert await harness.store.claim(
                first.request_id,
                lease=lease,
                controller_id="controller-b",
                incarnation_id="boot-b",
            )
        assert await harness.store.request_cancel(first.request_id)
        assert await harness.store.mark_terminal(
            first.request_id, lease=lease, status=DirectTurnStatus.CANCELLED
        )
        # An unclaimed queued cancellation must not hide the last execution fence.
        third = (await _admit(harness, key="authority-unclaimed", content="third")).request
        assert await harness.store.request_cancel(third.request_id)
        terminal = await harness.store.get_conversation_runtime_authority_row("conv-a")
        assert terminal is not None
        assert terminal.request_id == first.request_id
        assert terminal.status == DirectTurnStatus.CANCELLED.value
        assert terminal.fencing_token == lease.fencing_token
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_runtime_authority_lookup_uses_one_bounded_select(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    statements: list[str] = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(harness.engine.sync_engine, "before_cursor_execute", record_select)
    try:
        admitted = await _admit(harness, key="authority-query", content="hello")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        statements.clear()
        row = await harness.store.get_conversation_runtime_authority_row("conv-a")
        assert row is not None
        assert len(statements) == 1
    finally:
        event.remove(harness.engine.sync_engine, "before_cursor_execute", record_select)
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_claimable_heads_wait_for_persisted_retry_deadline(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="retry-delay", content="hello")
        lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        recovered = await harness.store.settle_transient_failure(
            admitted.request.request_id,
            lease=lease,
            outcome={"phase": "user_appended"},
            retry_after_seconds=60,
        )
        assert recovered is not None
        assert recovered.next_attempt_at is not None
        assert await harness.store.list_claimable_heads() == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_stop_terminally_cancels_recoverable_head_and_releases_successor(
    tmp_path: Path,
) -> None:
    harness = await _harness(tmp_path)
    try:
        first = await _admit(harness, key="retry-head", content="first")
        second = await _admit(harness, key="retry-successor", content="second")
        lease = await _lease(harness)
        assert await harness.store.claim(
            first.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert await harness.store.settle_transient_failure(
            first.request.request_id,
            lease=lease,
            outcome={"phase": "user_appended"},
            retry_after_seconds=60,
        )

        cancelled = await harness.store.cancel_conversation("conv-a", clear_queue=False)
        assert len(cancelled) == 1
        assert cancelled[0].request.status == DirectTurnStatus.CANCELLED.value
        assert cancelled[0].request.next_attempt_at is None
        assert cancelled[0].request.terminal_at is not None
        heads = await harness.store.list_claimable_heads()
        assert [row.request_id for row in heads] == [second.request.request_id]

        repeated = await harness.store.request_cancel(first.request.request_id)
        assert repeated is not None
        assert repeated.request.status == DirectTurnStatus.CANCELLED.value
        assert (
            await harness.store.claim(
                first.request.request_id,
                lease=lease,
                controller_id="controller-a",
                incarnation_id="boot-a",
            )
            is None
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_runtime_target_change_expedites_provider_retry(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="provider-retry", content="hello")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert await harness.store.settle_transient_failure(
            admitted.request.request_id,
            lease=lease,
            outcome={
                "phase": "user_appended",
                "session_id": "sess-a",
                "retry_target": {
                    "scope": "provider_model",
                    "runtime_revision": 3,
                    "reason_class": "rate_limit",
                },
            },
            retry_after_seconds=60,
        )
        assert await harness.store.list_claimable_heads() == []

        expedited = await harness.store.expedite_provider_retry(
            "conv-a",
            session_id="sess-a",
            previous_runtime_revision=3,
        )
        assert expedited is not None
        heads = await harness.store.list_claimable_heads()
        assert [row.request_id for row in heads] == [admitted.request.request_id]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_new_incarnation_fences_and_recovers_stale_claim(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="hello")
        first_lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=first_lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        assert await harness.leases.release(first_lease)

        successor = await _lease(
            harness,
            controller_id="controller-a",
            incarnation_id="boot-b",
        )
        assert successor.fencing_token > first_lease.fencing_token
        assert (
            await harness.store.mark_running(admitted.request.request_id, lease=first_lease) is None
        )
        assert not await harness.store.has_fence(admitted.request.request_id, lease=first_lease)

        recovered = await harness.store.recover_stale_claim(
            admitted.request.request_id,
            lease=successor,
            outcome={"reason": "owner lease expired before execution"},
        )
        assert recovered is not None
        assert recovered.status == DirectTurnStatus.RECOVERABLE.value

        reclaimed = await harness.store.claim(
            admitted.request.request_id,
            lease=successor,
            controller_id="controller-a",
            incarnation_id="boot-b",
        )
        assert reclaimed is not None
        assert reclaimed.attempt_count == 2
        assert reclaimed.fencing_token == successor.fencing_token
        assert await harness.store.mark_running(
            admitted.request.request_id,
            lease=successor,
        )
        assert await harness.leases.release(successor)

        final_owner = await _lease(
            harness,
            controller_id="controller-a",
            incarnation_id="boot-c",
        )
        ambiguous = await harness.store.mark_stale_ambiguous(
            admitted.request.request_id,
            lease=final_owner,
            outcome={"reason": "tool outcome unknown"},
        )
        assert ambiguous is not None
        assert ambiguous.status == DirectTurnStatus.AMBIGUOUS.value
        assert ambiguous.terminal_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_absorb_and_active_cancel_are_fenced(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="follow-up")
        lease = await _lease(harness)
        absorbing = await harness.store.begin_absorb(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            absorbed_by_turn_id="turn-active",
        )
        assert absorbing is not None
        assert absorbing.status == DirectTurnStatus.ABSORBING.value
        assert absorbing.absorbed_by_turn_id == "turn-active"

        cancellation = await harness.store.request_cancel(admitted.request.request_id)
        assert cancellation is not None
        assert cancellation.cancellation_requested is True
        absorbed = await harness.store.mark_absorbed(
            admitted.request.request_id,
            lease=lease,
        )
        assert absorbed is not None
        assert absorbed.status == DirectTurnStatus.ABSORBED.value
        assert absorbed.cancel_requested_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_recoverable_request_can_resume_absorption(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-recovered", content="follow-up")
        lease = await _lease(harness)
        claimed = await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        recoverable = await harness.store.mark_recoverable(
            admitted.request.request_id,
            lease=lease,
            outcome={"phase": "recovered_uncommitted_absorb"},
        )
        assert recoverable is not None

        absorbing = await harness.store.begin_absorb(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
            absorbed_by_turn_id="turn-active",
        )

        assert absorbing is not None
        assert absorbing.status == DirectTurnStatus.ABSORBING.value
        assert absorbing.absorbed_by_turn_id == "turn-active"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_admission_creates_one_request(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        first, second = await asyncio.gather(
            _admit(harness, key="message-1", content="hello"),
            _admit(harness, key="message-1", content="hello"),
        )
        assert sum(result.created for result in (first, second)) == 1
        assert first.request.request_id == second.request.request_id
        assert first.request.admission_order == second.request.admission_order
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_cannot_claim(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="message-1", content="hello")
        expired = await harness.leases.acquire(
            conversation_lease_key("conv-a"),
            "controller-a:boot-a",
            ttl_seconds=0,
        )
        assert expired is not None
        assert (
            await harness.store.claim(
                admitted.request.request_id,
                lease=expired,
                controller_id="controller-a",
                incarnation_id="boot-a",
            )
            is None
        )
    finally:
        await harness.engine.dispose()


_PAYLOAD_COLUMN = re.compile(r"\bpayload\b(?!_)")


def _capture_statements(engine: AsyncEngine) -> list[str]:
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    return statements


@pytest.mark.asyncio
async def test_has_fence_is_a_single_statement_without_payload(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="fence", content="payload body")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        statements = _capture_statements(harness.engine)
        assert await harness.store.has_fence(admitted.request.request_id, lease=lease)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1, selects
        assert not _PAYLOAD_COLUMN.search(selects[0])

        wrong_token = Lease(
            resource_key=lease.resource_key,
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token + 1,
            lease_expires_at=lease.lease_expires_at,
        )
        assert not await harness.store.has_fence(admitted.request.request_id, lease=wrong_token)
        other_owner = Lease(
            resource_key=lease.resource_key,
            owner_id="controller-b:boot-b",
            fencing_token=lease.fencing_token,
            lease_expires_at=lease.lease_expires_at,
        )
        assert not await harness.store.has_fence(admitted.request.request_id, lease=other_owner)
        assert not await harness.store.has_fence("dtr_missing", lease=lease)

        # Unclaimed rows have NULL owner columns: the SQL concat must yield false.
        unclaimed = await _admit(harness, conversation_id="conv-b", key="unclaimed", content="x")
        unclaimed_lease = await _lease(harness, conversation_id="conv-b")
        assert not await harness.store.has_fence(
            unclaimed.request.request_id, lease=unclaimed_lease
        )

        assert await harness.leases.release(lease)
        assert not await harness.store.has_fence(admitted.request.request_id, lease=lease)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_get_cancel_state_and_checkpoint_skip_payload(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        admitted = await _admit(harness, key="cancel", content="payload body")
        lease = await _lease(harness)
        assert await harness.store.claim(
            admitted.request.request_id,
            lease=lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert await harness.store.get_cancel_state("dtr_missing") is None
        status, cancel_requested_at = await harness.store.get_cancel_state(
            admitted.request.request_id
        )
        assert status == DirectTurnStatus.CLAIMED.value
        assert cancel_requested_at is None

        statements = _capture_statements(harness.engine)
        checkpointed = await harness.store.checkpoint(
            admitted.request.request_id,
            lease=lease,
            phase="model_wait",
            metadata={},
        )
        assert checkpointed is not None
        selects = [s for s in statements if "direct_turn_requests" in s and "SELECT" in s.upper()]
        assert selects and not any(_PAYLOAD_COLUMN.search(s) for s in selects), selects
        updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
        assert updates and not any(_PAYLOAD_COLUMN.search(u) for u in updates), updates

        # The durable payload is still intact for readers that need it.
        materialized = await harness.store.materialize_claimed_payload(
            admitted.request.request_id, lease=lease, artifact_store=None
        )
        assert materialized is not None
        assert materialized.content == "payload body"
    finally:
        await harness.engine.dispose()
