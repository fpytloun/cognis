from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Update

from cognis.bootstrap import run_schema_bootstrap
from cognis.models.channel import ChannelDeliveryDescriptor
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.database import create_engine, create_session_factory
from cognis.store.direct_turns import (
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
async def test_attachment_urls_are_transient_and_refreshed_after_recovery(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as session:
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
async def test_attachment_materialization_enforces_conversation_scope(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    try:
        async with harness.session_factory() as session:
            session.add(
                ArtifactRecordRow(
                    artifact_id="art_sibling",
                    namespace="attachments",
                    object_id="obj_sibling",
                    filename="sibling.pdf",
                    owner_email="user@example.com",
                    conversation_id="conv-b",
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
            "Attachment is expired",
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
