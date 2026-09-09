from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from cognis.core.agent_loop import AgentLoop
from cognis.core.remember_queue import RememberRetryQueue, _SessionReconciliationProgress
from cognis.core.trusted_evidence import (
    EVIDENCE_QUEUE_KIND,
    ORDINARY_ASSISTANT_QUEUE_KIND,
    ORDINARY_USER_QUEUE_KIND,
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    build_evidence_admission,
    build_evidence_event_binding,
    build_marker,
    deterministic_queue_id,
    event_hash,
)
from cognis.providers.memory.evidence import EvidenceRememberRequest
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, RememberQueueRow, Session, User

ADMISSION_KEY = b"remember-queue-reconciliation-test-key"


async def _database(
    tmp_path: Path,
    *,
    session_count: int = 1,
) -> tuple[Any, Any]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/remember-reconciliation.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Reconciliation agent",
            )
        )
        session.add(
            Conversation(
                conversation_id="conversation-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="chat",
            )
        )
        session.add_all(
            [
                Session(
                    session_id=f"session-{index}",
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    intaris_session_id=f"intaris-{index}",
                    mnemory_session_id=f"mnemory-{index}",
                )
                for index in range(1, session_count + 1)
            ]
        )
        await session.commit()
    return engine, factory


def _marked_user_event(index: int = 1) -> dict[str, Any]:
    content = "repair this append"
    intaris_session_id = f"intaris-{index}"
    cognis_session_id = f"session-{index}"
    turn_id = f"turn-{index}"
    binding = build_evidence_event_binding(
        intaris_session_id=intaris_session_id,
        cognis_session_id=cognis_session_id,
        conversation_id="conversation-1",
        turn_id=turn_id,
        user_id="user@example.com",
        owner_id="user@example.com",
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        attachment_refs_value=[],
    )
    admission = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=True,
        owner_id="user@example.com",
        policy_fingerprint="0123456789abcdef",
        event_binding=binding,
        max_attempts=8,
        max_age_seconds=3600,
    )
    marker = build_marker(
        content=content,
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        user_id="user@example.com",
        owner_id="user@example.com",
        intaris_session_id=intaris_session_id,
        cognis_session_id=cognis_session_id,
        conversation_id="conversation-1",
        turn_id=turn_id,
        attachment_refs_value=[],
        admission=admission,
        admission_key=ADMISSION_KEY,
    )
    return {
        "seq": 1,
        "type": "user_message",
        "data": {
            "source": "user_input",
            "role": "user",
            "prompt_visibility": "user_visible",
            "prompt_provenance": {"kind": "user_authored"},
            "user_visible_content": content,
            "attachments": [],
            "turn_id": turn_id,
            "trusted_evidence": marker,
        },
    }


@pytest.mark.asyncio
async def test_blocked_reconciliation_does_not_delay_ordinary_work_and_stops_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    reconcile_started = asyncio.Event()
    reconcile_cancelled = asyncio.Event()
    remember_called = asyncio.Event()

    class Worker:
        async def remember(self, **_kwargs: object) -> None:
            remember_called.set()

    class Reader:
        async def read_events(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                events=[
                    {
                        "seq": 1,
                        "type": "user_message",
                        "data": {"content": "question", "attachments": []},
                    },
                    {
                        "seq": 2,
                        "type": "assistant_message",
                        "data": {"content": "answer", "attachments": []},
                    },
                ]
            )

    queue = RememberRetryQueue(
        Worker(),
        session_factory=factory,
        event_reader=Reader(),
        max_concurrent=1,
        recovery_interval_seconds=60,
    )

    async def block_reconciliation() -> None:
        reconcile_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reconcile_cancelled.set()

    monkeypatch.setattr(queue, "_run_scheduled_reconciliation_cycle", block_reconciliation)
    await queue.start()
    await asyncio.wait_for(reconcile_started.wait(), timeout=1)
    await queue.enqueue(
        {
            "session_id": "mnemory-1",
            "cognis_session_id": "session-1",
            "intaris_session_id": "intaris-1",
            "user_email": "user@example.com",
            "agent_id": "agent-1",
            "agent_owner_email": "user@example.com",
            "user_event_seq": 1,
            "assistant_event_seq": 2,
        }
    )

    await asyncio.wait_for(remember_called.wait(), timeout=1)
    await queue.stop()

    assert reconcile_cancelled.is_set()
    assert queue._task is None
    assert queue._reconciliation_task is None
    assert queue._ledger_repair_task is None
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count(RememberQueueRow.item_id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_deferred_ordinary_repair_does_not_wait_for_blocked_intaris(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    event = _marked_user_event()
    evidence_admission = event["data"]["trusted_evidence"][TRUSTED_EVIDENCE_ADMISSION_KEY]
    evidence_hash = event_hash("intaris-1", 1, event)
    reconcile_started = asyncio.Event()
    reconcile_cancelled = asyncio.Event()
    remember_called = asyncio.Event()

    class Auth:
        @staticmethod
        def sign_user_event_jwt(_body: object) -> str:
            return "user-event-token"

    class Worker:
        auth_provider = Auth()

        async def remember_user_event(self, _body: object, token: str) -> None:
            assert token == "user-event-token"
            remember_called.set()

        async def remember(self, **_kwargs: object) -> None:
            raise AssertionError("paired ordinary user used generic remember")

    class Reader:
        async def read_events(self, **_kwargs: object) -> object:
            return SimpleNamespace(events=[event])

    queue = RememberRetryQueue(
        Worker(),
        session_factory=factory,
        event_reader=Reader(),
        max_depth=1,
        max_concurrent=1,
        recovery_interval_seconds=60,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    body = EvidenceRememberRequest.model_validate(
        {
            "version": 1,
            "actor": {
                "user_id": "user@example.com",
                "owner_id": "user@example.com",
            },
            "event": {
                "id": "intaris-1:1",
                "event_hash": evidence_hash,
                "cognis_session_id": "session-1",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
            },
            "messages": [{"role": "user", "content": "repair this append"}],
        }
    )

    async def resolve_body(_payload: dict[str, Any]) -> EvidenceRememberRequest:
        return body

    async def revalidate(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(body=body, token="user-event-token")

    monkeypatch.setattr(queue, "_resolve_evidence_body", resolve_body)
    monkeypatch.setattr(queue, "_revalidate_evidence_signing_inputs", revalidate)
    await queue.enqueue(
        {
            "item_id": "capacity-holder",
            "session_id": "mnemory-1",
            "user_email": "user@example.com",
        }
    )
    async with factory() as session:
        holder = await session.get(RememberQueueRow, "capacity-holder")
        assert holder is not None
        holder.next_retry_at = datetime(2100, 1, 1, tzinfo=UTC)
        await session.commit()
    await queue.enqueue_after_user_append(
        session=SimpleNamespace(
            session_id="session-1",
            mnemory_session_id="mnemory-1",
            intaris_session_id="intaris-1",
            user_email="user@example.com",
            agent_profile_id=None,
        ),
        agent=SimpleNamespace(agent_id="agent-1"),
        conversation_id="conversation-1",
        turn_id="turn-1",
        event_seq=1,
        event_hash_value=evidence_hash,
        owner_email="user@example.com",
        evidence_admission=evidence_admission,
    )

    async def block_reconciliation() -> None:
        reconcile_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reconcile_cancelled.set()

    monkeypatch.setattr(queue, "_run_scheduled_reconciliation_cycle", block_reconciliation)
    await queue.start()
    await asyncio.wait_for(reconcile_started.wait(), timeout=1)
    async with factory() as session:
        holder = await session.get(RememberQueueRow, "capacity-holder")
        assert holder is not None
        holder.status = "completed"
        await queue._update_durable_depth_metric(session)
        await session.commit()

    await asyncio.wait_for(remember_called.wait(), timeout=1)
    await queue.stop()

    assert reconcile_cancelled.is_set()
    async with factory() as session:
        ordinary = (
            await session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == ORDINARY_USER_QUEUE_KIND
                )
            )
        ).scalar_one()
        assert ordinary.status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_reconciliation_isolates_stage_timeout_and_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = RememberRetryQueue(object(), session_factory=lambda: None)
    queue._reconciliation_stage_timeout_seconds = 0.01
    queue._reconciliation_cycle_timeout_seconds = 0.2
    intaris_cancelled = asyncio.Event()
    intaris_calls = 0

    async def blocked_intaris(*_args: object, **_kwargs: object) -> tuple[int, bool]:
        nonlocal intaris_calls
        intaris_calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            intaris_cancelled.set()
        return 0, False

    async def failed_intaris(*_args: object, **_kwargs: object) -> tuple[int, bool]:
        nonlocal intaris_calls
        intaris_calls += 1
        raise RuntimeError("Intaris reconciliation failed")

    monkeypatch.setattr(queue, "_reconcile_intaris_evidence_batch", blocked_intaris)
    with caplog.at_level("WARNING"):
        await queue._run_scheduled_reconciliation_cycle()

    assert intaris_cancelled.is_set()
    assert intaris_calls == 1
    assert queue._reconciliation_pass_ok is False

    monkeypatch.setattr(queue, "_reconcile_intaris_evidence_batch", failed_intaris)
    await queue._run_scheduled_reconciliation_cycle()

    assert intaris_calls == 2
    assert any(
        "Trusted evidence reconciliation stage failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_claims_alternate_between_ready_ordinary_and_evidence_work(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    queue = RememberRetryQueue(object(), session_factory=factory, max_concurrent=1)
    await queue.enqueue(
        {
            "item_id": "evidence-item",
            "queue_kind": EVIDENCE_QUEUE_KIND,
            "session_id": "mnemory-1",
            "user_email": "user@example.com",
        }
    )
    await queue.enqueue(
        {
            "item_id": "ordinary-item",
            "session_id": "mnemory-1",
            "user_email": "user@example.com",
        }
    )

    ordinary = await queue._claim_due_durable_items(1)
    assert [item.item_id for item in ordinary] == ["ordinary-item"]
    async with factory() as session:
        row = await session.get(RememberQueueRow, "ordinary-item")
        assert row is not None
        row.status = "completed"
        row.lease_token = None
        row.lease_expires_at = None
        await session.commit()

    evidence = await queue._claim_due_durable_items(1)
    assert [item.item_id for item in evidence] == ["evidence-item"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_timeout_and_error_skip_then_retry_after_sweep(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, session_count=3)
    event = _marked_user_event()

    class Reader:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.attempts: dict[str, int] = {}

        async def read_events(self, **kwargs: object) -> object:
            session_id = str(kwargs["session_id"])
            self.calls.append(session_id)
            self.attempts[session_id] = self.attempts.get(session_id, 0) + 1
            if session_id == "intaris-1" and self.attempts[session_id] == 1:
                await asyncio.Event().wait()
            if session_id == "intaris-2" and self.attempts[session_id] == 1:
                raise RuntimeError("Intaris page failed")
            return SimpleNamespace(events=[event] if session_id == "intaris-1" else [])

    reader = Reader()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        event_reader=reader,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    queue._reconciliation_read_timeout_seconds = 0.01
    state = queue._reconciliation_state

    for _ in range(4):
        await queue._reconcile_intaris_evidence_batch(
            state,
            session_limit=1,
            page_limit=1,
        )
    assert reader.calls == ["intaris-1", "intaris-2", "intaris-3"]
    assert queue._reconciliation_pass_ok is False

    await queue._reconcile_intaris_evidence_batch(
        state,
        session_limit=1,
        page_limit=1,
    )
    assert reader.calls == ["intaris-1", "intaris-2", "intaris-3", "intaris-1"]
    async with factory() as session:
        kinds = set(
            (await session.execute(sa.select(RememberQueueRow.payload["queue_kind"].as_string())))
            .scalars()
            .all()
        )
    assert kinds == {EVIDENCE_QUEUE_KIND, ORDINARY_USER_QUEUE_KIND}
    await engine.dispose()


@pytest.mark.asyncio
async def test_growing_session_does_not_starve_later_session_reconciliation(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, session_count=2)
    first_event = _marked_user_event(1)
    later_event = _marked_user_event(2)

    class Reader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def read_events(self, **kwargs: object) -> object:
            session_id = str(kwargs["session_id"])
            after_seq = int(kwargs["after_seq"])
            self.calls.append(session_id)
            if session_id == "intaris-1":
                if after_seq == 0:
                    return SimpleNamespace(
                        events=[
                            first_event,
                            {
                                "seq": 2,
                                "type": "assistant_message",
                                "data": {"turn_id": "turn-1", "content": "first answer"},
                            },
                            *[
                                {
                                    "seq": seq,
                                    "type": "user_message",
                                    "data": {"turn_id": f"growing-{seq}"},
                                }
                                for seq in range(3, 101)
                            ],
                        ]
                    )
                return SimpleNamespace(
                    events=[
                        {
                            "seq": after_seq + offset,
                            "type": "user_message",
                            "data": {"turn_id": f"growing-{after_seq + offset}"},
                        }
                        for offset in range(1, 101)
                    ]
                )
            return SimpleNamespace(events=[later_event])

    reader = Reader()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        event_reader=reader,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )

    await queue._reconcile_intaris_evidence_batch(
        queue._reconciliation_state,
        session_limit=2,
        page_limit=2,
    )

    assert reader.calls == ["intaris-1", "intaris-2"]
    assert "session-1" in queue._reconciliation_state.session_progress
    async with factory() as session:
        repaired_later = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(
                RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                RememberQueueRow.payload["intaris_session_id"].as_string() == "intaris-2",
            )
        )
        repaired_assistant = await session.scalar(
            sa.select(sa.func.count())
            .select_from(RememberQueueRow)
            .where(
                RememberQueueRow.payload["queue_kind"].as_string() == ORDINARY_ASSISTANT_QUEUE_KIND,
                RememberQueueRow.payload["intaris_session_id"].as_string() == "intaris-1",
            )
        )
    assert repaired_later == 1
    assert repaired_assistant == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_assistant_finalization_checkpoints_completed_handoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = RememberRetryQueue(object())
    progress = _SessionReconciliationProgress(
        pending_markers={
            f"turn-{index}": (
                {"event_hash": f"event-{index}"},
                f"evidence-{index}",
                index,
                f"assistant-{index}",
            )
            for index in range(1, 4)
        }
    )
    second_started = asyncio.Event()
    calls: list[str] = []

    async def cancel_during_second(payload: dict[str, Any]) -> None:
        calls.append(str(payload["item_id"]))
        if len(calls) == 2:
            second_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(queue, "enqueue", cancel_during_second)
    finalization = asyncio.create_task(queue._finish_reconciliation_session(progress))
    await asyncio.wait_for(second_started.wait(), timeout=1)
    finalization.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await finalization

    first_id = deterministic_queue_id(
        ORDINARY_ASSISTANT_QUEUE_KIND,
        "event-1",
        "assistant-1",
    )
    assert calls.count(first_id) == 1
    assert set(progress.pending_markers) == {"turn-2", "turn-3"}

    async def complete(payload: dict[str, Any]) -> None:
        calls.append(str(payload["item_id"]))

    monkeypatch.setattr(queue, "enqueue", complete)
    await queue._finish_reconciliation_session(progress)

    assert calls.count(first_id) == 1
    assert progress.pending_markers == {}


@pytest.mark.asyncio
async def test_terminal_ledger_cursor_stops_at_capacity_blocked_row(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    queue = RememberRetryQueue(object(), session_factory=factory, max_depth=1)
    await queue.enqueue(
        {
            "item_id": "capacity-holder",
            "session_id": "mnemory-1",
            "user_email": "user@example.com",
        }
    )
    async with factory() as session:
        session.add_all(
            [
                RememberQueueRow(
                    item_id=f"evidence-{suffix}",
                    session_id="mnemory-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="unavailable",
                    next_retry_at=datetime.now(UTC),
                    payload={
                        "queue_kind": EVIDENCE_QUEUE_KIND,
                        "event_hash": f"event-{suffix}",
                        "ordinary_work_needed": True,
                        "session_id": "mnemory-1",
                        "user_email": "user@example.com",
                        "agent_id": "agent-1",
                    },
                )
                for suffix in ("a", "b")
            ]
        )
        await session.commit()

    assert await asyncio.wait_for(queue.reconcile_trusted_evidence(), timeout=1) == 0
    state = queue._reconciliation_state
    rebuilt, complete = await queue._reconcile_terminal_evidence_ledger_batch(state, 1)
    assert (rebuilt, complete) == (0, False)
    assert state.terminal_cursor is None

    async with factory() as session:
        holder = await session.get(RememberQueueRow, "capacity-holder")
        assert holder is not None
        holder.status = "completed"
        await session.commit()
    rebuilt, complete = await queue._reconcile_terminal_evidence_ledger_batch(state, 1)

    assert (rebuilt, complete) == (1, False)
    assert state.terminal_cursor == "evidence-a"
    async with factory() as session:
        ordinary = await session.get(
            RememberQueueRow,
            deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "event-a"),
        )
        assert ordinary is not None
        assert ordinary.status == "pending"
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_preserves_marker_state_across_page_budget(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    marked = _marked_user_event()
    first_page = [
        marked,
        *[
            {
                "seq": seq,
                "type": "user_message",
                "data": {"turn_id": f"other-{seq}"},
            }
            for seq in range(2, 101)
        ],
    ]
    assistant = {
        "seq": 101,
        "type": "assistant_message",
        "data": {"turn_id": "turn-1", "content": "repaired answer"},
    }

    class Reader:
        def __init__(self) -> None:
            self.after_seqs: list[int] = []

        async def read_events(self, **kwargs: object) -> object:
            after_seq = int(kwargs["after_seq"])
            self.after_seqs.append(after_seq)
            return SimpleNamespace(events=first_page if after_seq == 0 else [assistant])

    reader = Reader()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        event_reader=reader,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    state = queue._reconciliation_state

    _, complete = await queue._reconcile_intaris_evidence_batch(
        state,
        session_limit=1,
        page_limit=1,
    )
    assert complete is False
    progress = state.session_progress["session-1"]
    assert progress.after_seq == 100
    assert progress.pending_markers

    _, complete = await queue._reconcile_intaris_evidence_batch(
        state,
        session_limit=1,
        page_limit=1,
    )
    assert complete is True
    _, complete = await queue._reconcile_intaris_evidence_batch(
        state,
        session_limit=1,
        page_limit=1,
    )
    assert complete is False
    assert "session-1" not in state.session_progress
    assert reader.after_seqs == [0, 100]
    async with factory() as session:
        kinds = set(
            (await session.execute(sa.select(RememberQueueRow.payload["queue_kind"].as_string())))
            .scalars()
            .all()
        )
    assert kinds == {
        EVIDENCE_QUEUE_KIND,
        ORDINARY_USER_QUEUE_KIND,
        ORDINARY_ASSISTANT_QUEUE_KIND,
    }
    await engine.dispose()


def _assistant_identity_payload(
    *,
    evidence_hash: str,
    assistant_hash: str,
) -> dict[str, Any]:
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, evidence_hash)
    return {
        "session_id": "mnemory-1",
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "user@example.com",
        "agent_owner_email": "user@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        "originating_memory_backend": "mnemory",
        "originating_agent_profile_id": None,
        "memory_policy_fingerprint": "policy-fingerprint",
        "queue_kind": ORDINARY_ASSISTANT_QUEUE_KIND,
        "item_id": deterministic_queue_id(
            ORDINARY_ASSISTANT_QUEUE_KIND,
            evidence_hash,
            assistant_hash,
        ),
        "event_hash": evidence_hash,
        "depends_on": evidence_id,
        "include_user_message": False,
        "user_event_seq": None,
        "assistant_event_seq": 2,
        "assistant_event_hash": assistant_hash,
    }


async def _seed_failed_pre_dispatch_assistant(
    factory: Any,
    payload: dict[str, Any],
) -> None:
    evidence_id = str(payload["depends_on"])
    evidence_payload = {
        key: value
        for key, value in payload.items()
        if key
        in {
            "session_id",
            "cognis_session_id",
            "intaris_session_id",
            "conversation_id",
            "turn_id",
            "user_email",
            "owner_email",
            "agent_owner_email",
            "agent_id",
            "policy_agent_id",
            "originating_memory_backend",
            "originating_agent_profile_id",
            "memory_policy_fingerprint",
            "event_hash",
        }
    }
    evidence_payload["queue_kind"] = EVIDENCE_QUEUE_KIND
    malformed_payload = dict(payload)
    malformed_payload.pop("event_hash")
    async with factory() as session:
        session.add_all(
            [
                RememberQueueRow(
                    item_id=evidence_id,
                    session_id=str(payload["session_id"]),
                    user_email=str(payload["user_email"]),
                    agent_id=str(payload["agent_id"]),
                    payload=evidence_payload,
                    status="accepted",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                ),
                RememberQueueRow(
                    item_id=str(payload["item_id"]),
                    session_id=str(payload["session_id"]),
                    user_email=str(payload["user_email"]),
                    agent_id=str(payload["agent_id"]),
                    payload=malformed_payload,
                    status="failed",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                    last_error="ordinary remember assertion conflict",
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_dispatch_remember_persists_assistant_source_event_hash() -> None:
    event = _marked_user_event()
    admission = event["data"]["trusted_evidence"][TRUSTED_EVIDENCE_ADMISSION_KEY]
    remember_queue = SimpleNamespace(enqueue=AsyncMock())
    loop = SimpleNamespace(
        remember_queue=remember_queue,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    ctx = SimpleNamespace(
        memory_policy=SimpleNamespace(
            auto_remember=True,
            backend_id="mnemory",
            profile_id=None,
            policy_fingerprint="policy-fingerprint",
        ),
        session=SimpleNamespace(
            mnemory_session_id="mnemory-1",
            session_id="session-1",
            intaris_session_id="intaris-1",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        conversation=SimpleNamespace(conversation_id="conversation-1"),
        agent=SimpleNamespace(owner_email="user@example.com"),
        turn_id="turn-1",
        trusted_evidence_admission=admission,
        remember_evidence_event_hash="evidence-event-hash",
        remember_user_event_seq=1,
        remember_assistant_event_seq=2,
        remember_assistant_event_hash="assistant-event-hash",
    )

    await AgentLoop._dispatch_remember(loop, ctx, ["answer"])

    payload = remember_queue.enqueue.await_args.args[0]
    assert payload["event_hash"] == "evidence-event-hash"
    assert payload["item_id"] == deterministic_queue_id(
        ORDINARY_ASSISTANT_QUEUE_KIND,
        "evidence-event-hash",
        "assistant-event-hash",
    )


@pytest.mark.asyncio
async def test_reconciliation_repairs_proved_pre_dispatch_assistant_row(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    assistant_event = {
        "seq": 2,
        "type": "assistant_message",
        "data": {"turn_id": "turn-1", "content": "answer"},
    }
    assistant_hash = event_hash("intaris-1", 2, assistant_event)
    payload = _assistant_identity_payload(
        evidence_hash="evidence-event-hash",
        assistant_hash=assistant_hash,
    )
    await _seed_failed_pre_dispatch_assistant(factory, payload)

    class Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def remember(self, **_kwargs: object) -> None:
            self.calls += 1

    class Reader:
        async def read_events(self, **_kwargs: object) -> object:
            return SimpleNamespace(events=[assistant_event])

    worker = Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        event_reader=Reader(),
    )

    await queue.enqueue(payload)
    await queue.enqueue(payload)
    claimed = await queue._claim_due_durable_items(1)
    assert len(claimed) == 1
    await queue._process(claimed[0], asyncio.Semaphore(1))

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(RememberQueueRow).where(
                        RememberQueueRow.item_id == payload["item_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "completed"
    assert rows[0].attempts == 1
    assert rows[0].last_error is None
    assert rows[0].payload["event_hash"] == "evidence-event-hash"
    assert rows[0].payload["depends_on"] == payload["depends_on"]
    assert worker.calls == 1
    assert await queue._claim_due_durable_items(1) == []
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cognis_session_id", "other-cognis-session"),
        ("intaris_session_id", "other-intaris-session"),
        ("assistant_event_seq", 3),
        ("owner_email", "other@example.com"),
        ("depends_on", "rq_evidence_unrecognized"),
    ],
)
async def test_reconciliation_rejects_unproved_assistant_identity_repairs(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    engine, factory = await _database(tmp_path)
    payload = _assistant_identity_payload(
        evidence_hash="evidence-event-hash",
        assistant_hash="assistant-event-hash",
    )
    await _seed_failed_pre_dispatch_assistant(factory, payload)
    incoming = {**payload, field: value}
    queue = RememberRetryQueue(object(), session_factory=factory)

    with pytest.raises(ValueError, match="deterministic queue identity payload conflict"):
        await queue.enqueue(incoming)

    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "failed"
    assert row.attempts == 1
    assert "event_hash" not in row.payload
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence_session_id",
    [None, "mnemory-before-adoption", "other-mnemory-session"],
)
async def test_reconciliation_accepts_mutable_evidence_memory_destination(
    tmp_path: Path,
    evidence_session_id: str | None,
) -> None:
    engine, factory = await _database(tmp_path)
    payload = _assistant_identity_payload(
        evidence_hash="evidence-event-hash",
        assistant_hash="assistant-event-hash",
    )
    await _seed_failed_pre_dispatch_assistant(factory, payload)
    async with factory() as session:
        dependency = await session.get(RememberQueueRow, str(payload["depends_on"]))
        assert dependency is not None
        dependency.payload = {
            **dependency.payload,
            "session_id": evidence_session_id,
        }
        dependency.session_id = evidence_session_id or "session-1"
        await session.commit()
    queue = RememberRetryQueue(object(), session_factory=factory)

    await queue.enqueue(payload)

    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.payload["event_hash"] == "evidence-event-hash"
    assert row.payload["session_id"] == "mnemory-1"
    await engine.dispose()


async def _seed_rotated_assistant(
    factory: Any,
    *,
    linked_predecessor: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    evidence_event = _marked_user_event()
    evidence_hash = event_hash("intaris-1", 1, evidence_event)
    assistant_event = {
        "seq": 2,
        "type": "assistant_message",
        "data": {"turn_id": "turn-1", "content": "answer"},
    }
    assistant_hash = event_hash("intaris-2", 2, assistant_event)
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, evidence_hash)
    item_id = deterministic_queue_id(
        ORDINARY_ASSISTANT_QUEUE_KIND,
        evidence_hash,
        assistant_hash,
    )
    common = {
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "user@example.com",
        "agent_owner_email": "user@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
    }
    evidence_payload = {
        **common,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "session_id": "mnemory-1",
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "event_seq": 1,
        "event_hash": evidence_hash,
    }
    assistant_payload = {
        **common,
        "queue_kind": ORDINARY_ASSISTANT_QUEUE_KIND,
        "item_id": item_id,
        "session_id": "mnemory-2",
        "cognis_session_id": "session-2",
        "intaris_session_id": "intaris-2",
        "depends_on": evidence_id,
        "include_user_message": False,
        "user_event_seq": None,
        "assistant_event_seq": 2,
        "assistant_event_hash": assistant_hash,
    }
    async with factory() as session:
        successor = await session.get(Session, "session-2")
        assert successor is not None
        successor.previous_session_id = "session-1" if linked_predecessor else None
        session.add_all(
            [
                RememberQueueRow(
                    item_id=evidence_id,
                    session_id="mnemory-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    payload=evidence_payload,
                    status="accepted",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                ),
                RememberQueueRow(
                    item_id=item_id,
                    session_id="mnemory-2",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    payload=assistant_payload,
                    status="failed",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                    last_error="ordinary remember assertion conflict",
                ),
            ]
        )
        await session.commit()
    return assistant_payload, evidence_event, assistant_event


@pytest.mark.asyncio
async def test_bounded_rotation_scan_repairs_verified_predecessor_handoff(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, session_count=2)
    payload, evidence_event, assistant_event = await _seed_rotated_assistant(factory)

    class Reader:
        async def read_events(self, **kwargs: object) -> object:
            session_id = str(kwargs["session_id"])
            return SimpleNamespace(
                events=[evidence_event if session_id == "intaris-1" else assistant_event]
            )

    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        event_reader=Reader(),
    )

    repaired, complete = await queue._reconcile_rotated_assistant_batch(
        queue._reconciliation_state,
        1,
    )

    assert (repaired, complete) == (1, False)
    assert queue._reconciliation_state.rotation_repair_cursor == payload["item_id"]
    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.payload["event_hash"] == event_hash("intaris-1", 1, evidence_event)
    assert row.payload["depends_on"] == payload["depends_on"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_scan_retries_source_read_without_advancing_cursor(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, session_count=2)
    payload, evidence_event, assistant_event = await _seed_rotated_assistant(factory)

    class Reader:
        def __init__(self) -> None:
            self.fail = True

        async def read_events(self, **kwargs: object) -> object:
            if self.fail:
                raise RuntimeError("source unavailable")
            session_id = str(kwargs["session_id"])
            return SimpleNamespace(
                events=[evidence_event if session_id == "intaris-1" else assistant_event]
            )

    reader = Reader()
    queue = RememberRetryQueue(object(), session_factory=factory, event_reader=reader)

    assert await queue._reconcile_rotated_assistant_batch(queue._reconciliation_state, 1) == (
        0,
        False,
    )
    assert queue._reconciliation_state.rotation_repair_cursor is None
    reader.fail = False
    assert await queue._reconcile_rotated_assistant_batch(queue._reconciliation_state, 1) == (
        1,
        False,
    )
    assert queue._reconciliation_state.rotation_repair_cursor == payload["item_id"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_scan_rejects_unlinked_session_handoff(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, session_count=2)
    payload, _evidence_event, _assistant_event = await _seed_rotated_assistant(
        factory,
        linked_predecessor=False,
    )

    class Reader:
        async def read_events(self, **_kwargs: object) -> object:
            raise AssertionError("unproved rotation must not read source events")

    queue = RememberRetryQueue(object(), session_factory=factory, event_reader=Reader())

    assert await queue._reconcile_rotated_assistant_batch(queue._reconciliation_state, 20) == (
        0,
        True,
    )
    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "failed"
    assert "event_hash" not in row.payload
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_scan_keeps_source_hash_mismatch_failed(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, session_count=2)
    payload, evidence_event, assistant_event = await _seed_rotated_assistant(factory)
    assistant_event = {
        **assistant_event,
        "data": {**assistant_event["data"], "content": "different answer"},
    }

    class Reader:
        async def read_events(self, **kwargs: object) -> object:
            session_id = str(kwargs["session_id"])
            return SimpleNamespace(
                events=[evidence_event if session_id == "intaris-1" else assistant_event]
            )

    queue = RememberRetryQueue(object(), session_factory=factory, event_reader=Reader())

    assert await queue._reconcile_rotated_assistant_batch(queue._reconciliation_state, 20) == (
        0,
        True,
    )
    assert queue._reconciliation_state.pass_ok is False
    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "failed"
    assert row.attempts == 1
    assert "event_hash" not in row.payload
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_updates",
    [
        {"status": "completed"},
        {"status": "ambiguous"},
        {"attempts": 2},
        {"last_error": "provider outcome unknown"},
        {"lease_token": "active-lease"},
    ],
)
async def test_reconciliation_does_not_retry_completed_or_uncertain_assistant_rows(
    tmp_path: Path,
    row_updates: dict[str, object],
) -> None:
    engine, factory = await _database(tmp_path)
    payload = _assistant_identity_payload(
        evidence_hash="evidence-event-hash",
        assistant_hash="assistant-event-hash",
    )
    await _seed_failed_pre_dispatch_assistant(factory, payload)
    async with factory() as session:
        await session.execute(
            sa.update(RememberQueueRow)
            .where(RememberQueueRow.item_id == payload["item_id"])
            .values(**row_updates)
        )
        await session.commit()
    queue = RememberRetryQueue(object(), session_factory=factory)

    with pytest.raises(ValueError, match="deterministic queue identity payload conflict"):
        await queue.enqueue(payload)

    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == row_updates.get("status", "failed")
    assert row.attempts == row_updates.get("attempts", 1)
    assert row.last_error == row_updates.get("last_error", "ordinary remember assertion conflict")
    assert row.lease_token == row_updates.get("lease_token")
    assert "event_hash" not in row.payload
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cognis_session_id", "other-cognis-session"),
        ("intaris_session_id", "other-intaris-session"),
    ],
)
async def test_reconciliation_rejects_evidence_dependency_source_session_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    engine, factory = await _database(tmp_path)
    payload = _assistant_identity_payload(
        evidence_hash="evidence-event-hash",
        assistant_hash="assistant-event-hash",
    )
    await _seed_failed_pre_dispatch_assistant(factory, payload)
    async with factory() as session:
        dependency = await session.get(RememberQueueRow, str(payload["depends_on"]))
        assert dependency is not None
        dependency.payload = {**dependency.payload, field: value}
        await session.commit()
    queue = RememberRetryQueue(object(), session_factory=factory)

    with pytest.raises(ValueError, match="deterministic queue identity payload conflict"):
        await queue.enqueue(payload)

    async with factory() as session:
        row = await session.get(RememberQueueRow, str(payload["item_id"]))
    assert row is not None
    assert row.status == "failed"
    assert row.attempts == 1
    assert "event_hash" not in row.payload
    await engine.dispose()
