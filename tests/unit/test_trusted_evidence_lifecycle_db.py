from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from cognis.core.agent_loop import AgentLoop
from cognis.core.remember_queue import RememberQueueItem, RememberRetryQueue
from cognis.core.trusted_evidence import (
    EVIDENCE_QUEUE_KIND,
    ORDINARY_USER_QUEUE_KIND,
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    build_evidence_admission,
    build_evidence_event_binding,
    build_marker,
    deserialize_evidence_admission,
    deterministic_queue_id,
    event_hash,
    serialize_evidence_admission,
)
from cognis.providers.memory.evidence import EvidenceRememberRequest
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, RememberQueueRow, Session, User

ADMISSION_KEY = b"trusted-evidence-test-key"


def _event_binding(
    *,
    content: str = "hello",
    owner_id: str = "owner@example.com",
    turn_id: str = "turn-1",
    intaris_session_id: str = "intaris-1",
) -> dict[str, object]:
    return build_evidence_event_binding(
        intaris_session_id=intaris_session_id,
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id=turn_id,
        user_id="user@example.com",
        owner_id=owner_id,
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        attachment_refs_value=[],
    )


@pytest.fixture
def ADMISSION():
    # Mint at test setup, not collection: a full suite can exceed the signed TTL.
    return serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=True,
            owner_id="owner@example.com",
            policy_fingerprint="0123456789abcdef",
            event_binding=_event_binding(),
        )
    )


@pytest.mark.asyncio
async def test_agent_loop_handoff_uses_mac_bound_admission_time(ADMISSION) -> None:
    captured: dict[str, object] = {}

    class Queue:
        async def enqueue_after_user_append(self, **kwargs):
            captured.update(kwargs)

    agent_loop = AgentLoop.__new__(AgentLoop)
    agent_loop.remember_queue = Queue()
    admission = deserialize_evidence_admission(ADMISSION)
    assert admission is not None
    ctx = SimpleNamespace(
        remember_evidence_event_hash="event-hash",
        remember_user_event_seq=7,
        trusted_evidence_admission=admission,
        session=SimpleNamespace(),
        agent=SimpleNamespace(agent_id="agent-1", owner_email="owner@example.com"),
        conversation=SimpleNamespace(conversation_id="conversation-1"),
        turn_id="turn-1",
    )

    await agent_loop._enqueue_evidence_rows_after_append(ctx)

    assert captured["marker_admitted_at"] == admission.admitted_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker_admitted_at",
    [None, "2099-01-01T00:00:00+00:00"],
)
async def test_expired_mac_bound_admission_never_restarts_from_queue_time(
    tmp_path,
    monkeypatch,
    marker_admitted_at: str | None,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/expired-admission.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.commit()
    admission = serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=True,
            owner_id="owner@example.com",
            policy_fingerprint="0123456789abcdef",
            event_binding=_event_binding(),
            admitted_at="2000-01-01T00:00:00+00:00",
            max_age_seconds=60,
        )
    )

    class Auth:
        def __init__(self) -> None:
            self.sign_calls = 0

        def sign_evidence_jwt(self, _body) -> str:
            self.sign_calls += 1
            return "token"

    worker = SimpleNamespace(auth_provider=Auth())
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )

    async def memory_enabled(_payload):
        return False

    monkeypatch.setattr(queue, "_hard_memory_disable_applies", memory_enabled)
    event_hash_value = "expired-event"
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
    payload = {
        "item_id": evidence_id,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash_value,
        "event_seq": 7,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: admission,
    }
    if marker_admitted_at is not None:
        payload["marker_admitted_at"] = marker_admitted_at
    await queue.enqueue(payload)
    claimed = await queue._claim_due_durable_items(1)

    assert len(claimed) == 1
    await queue._process_evidence_item(claimed[0])
    async with factory() as session:
        row = await session.get(RememberQueueRow, evidence_id)
        assert row is not None and row.status in {"conflict", "unavailable"}
    assert worker.auth_provider.sign_calls == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_durable_evidence_enqueue_is_idempotent_and_capacity_safe(
    tmp_path, ADMISSION
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/evidence.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        max_depth=1,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    event_hash = "event-hash-1"
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash)
    payload = {
        "item_id": evidence_id,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash,
        "event_seq": 7,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }
    await queue.enqueue(payload)
    await queue.enqueue(payload)

    async with factory() as session:
        rows = (await session.execute(sa.select(RememberQueueRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "pending"

    second_hash = "event-hash-2"
    second_admission = serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=True,
            owner_id="owner@example.com",
            policy_fingerprint="0123456789abcdef",
            event_binding=_event_binding(turn_id="turn-2"),
        )
    )
    await queue.enqueue(
        {
            **payload,
            "item_id": deterministic_queue_id(EVIDENCE_QUEUE_KIND, second_hash),
            "event_hash": second_hash,
            "turn_id": "turn-2",
            TRUSTED_EVIDENCE_ADMISSION_KEY: second_admission,
        }
    )
    async with factory() as session:
        rows = (await session.execute(sa.select(RememberQueueRow))).scalars().all()
        assert {row.status for row in rows} == {"pending", "unavailable"}
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(RememberQueueRow)
                .where(RememberQueueRow.status.in_(["pending", "leased", "dispatching", "failed"]))
            )
            == 1
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_completed_ordinary_row_is_a_reconciliation_suppression_ledger(
    tmp_path, ADMISSION
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/ledger.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    event_hash = "event-hash-ledger"
    payload = {
        "item_id": deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash),
        "queue_kind": ORDINARY_USER_QUEUE_KIND,
        "event_hash": event_hash,
        "event_seq": 9,
        "depends_on": deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash),
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }
    async with factory() as session:
        session.add(
            RememberQueueRow(
                item_id=payload["item_id"],
                session_id="mnemory-1",
                user_email="user@example.com",
                agent_id="agent-1",
                payload=payload,
                status="completed",
                next_retry_at=datetime.now(UTC),
            )
        )
        await session.commit()
    await queue.enqueue(payload)
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(RememberQueueRow)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_append_handoff_is_one_group_and_uses_override_stream(tmp_path, ADMISSION) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/handoff.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db_session:
        db_session.add(User(email="user@example.com"))
        await db_session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        max_depth=4,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("owner@example.com",),
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    session = SimpleNamespace(
        session_id="session-1",
        mnemory_session_id="mnemory-1",
        intaris_session_id="stale-stream",
        user_email="user@example.com",
        agent_profile_id="profile-1",
    )
    agent = SimpleNamespace(agent_id="agent-1")
    await queue.enqueue_after_user_append(
        session=session,
        agent=agent,
        conversation_id="conversation-1",
        turn_id="turn-1",
        event_seq=12,
        event_hash_value="event-hash-handoff",
        owner_email="owner@example.com",
        intaris_session_id_override="authoritative-stream",
        evidence_admission=ADMISSION,
    )
    async with factory() as db_session:
        rows = (await db_session.execute(sa.select(RememberQueueRow))).scalars().all()
        assert len(rows) == 2
        assert {row.payload["intaris_session_id"] for row in rows} == {"authoritative-stream"}
        assert {row.payload["queue_kind"] for row in rows} == {
            EVIDENCE_QUEUE_KIND,
            ORDINARY_USER_QUEUE_KIND,
        }
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_handoff_defers_ordinary_until_capacity_opens(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/deferred.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db_session:
        db_session.add(User(email="user@example.com"))
        await db_session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        max_depth=2,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("owner@example.com",),
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    for existing_hash in ("existing-1", "existing-2"):
        existing_admission = serialize_evidence_admission(
            build_evidence_admission(
                key=ADMISSION_KEY,
                admitted=True,
                owner_id="owner@example.com",
                policy_fingerprint="0123456789abcdef",
                event_binding=_event_binding(turn_id=f"turn-{existing_hash}"),
            )
        )
        await queue.enqueue(
            {
                "item_id": deterministic_queue_id(EVIDENCE_QUEUE_KIND, existing_hash),
                "queue_kind": EVIDENCE_QUEUE_KIND,
                "event_hash": existing_hash,
                "event_seq": 1,
                "cognis_session_id": "session-existing",
                "intaris_session_id": "stream-existing",
                "conversation_id": "conversation-existing",
                "turn_id": "turn-existing",
                "user_email": "user@example.com",
                "owner_email": "owner@example.com",
                "agent_id": "agent-existing",
                "policy_agent_id": "agent-existing",
                TRUSTED_EVIDENCE_ADMISSION_KEY: existing_admission,
            }
        )
    session = SimpleNamespace(
        session_id="session-1",
        mnemory_session_id="mnemory-1",
        intaris_session_id="stream-1",
        user_email="user@example.com",
        agent_profile_id="profile-1",
    )
    agent = SimpleNamespace(agent_id="agent-1")
    kwargs = {
        "session": session,
        "agent": agent,
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "event_seq": 12,
        "event_hash_value": "event-hash-deferred",
        "owner_email": "owner@example.com",
        "intaris_session_id_override": "stream-1",
        "evidence_admission": serialize_evidence_admission(
            build_evidence_admission(
                key=ADMISSION_KEY,
                admitted=True,
                owner_id="owner@example.com",
                policy_fingerprint="0123456789abcdef",
                event_binding=_event_binding(intaris_session_id="stream-1"),
            )
        ),
    }
    await queue.enqueue_after_user_append(**kwargs)
    await queue.enqueue_after_user_append(**kwargs)
    ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "event-hash-deferred")
    async with factory() as db_session:
        assert await db_session.get(RememberQueueRow, ordinary_id) is None
        evidence = (
            await db_session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["event_hash"].as_string() == "event-hash-deferred",
                    RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                )
            )
        ).scalar_one()
        assert evidence.status == "unavailable"
        assert evidence.payload["ordinary_work_needed"] is True
        existing = (
            await db_session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["event_hash"].as_string() == "existing-1"
                )
            )
        ).scalar_one()
        existing.status = "completed"
        await db_session.commit()
    await queue.enqueue_after_user_append(**kwargs)
    async with factory() as db_session:
        ordinary = await db_session.get(RememberQueueRow, ordinary_id)
        assert ordinary is not None
        assert ordinary.status == "pending"
        evidence = (
            await db_session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["event_hash"].as_string() == "event-hash-deferred",
                    RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND,
                )
            )
        ).scalar_one()
        assert evidence.payload["ordinary_work_needed"] is False
    await engine.dispose()


@pytest.mark.parametrize(
    ("worker_enabled", "worker_allowlist"),
    [
        (False, ()),
        (True, ("another-owner@example.com",)),
    ],
)
@pytest.mark.asyncio
async def test_disabled_worker_dispatches_previously_admitted_evidence(
    tmp_path,
    monkeypatch,
    ADMISSION,
    worker_enabled: bool,
    worker_allowlist: tuple[str, ...],
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/deselected.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        session.add(User(email="owner@example.com"))
        await session.commit()
        session.add(Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"))
        session.add(
            Conversation(
                conversation_id="conversation-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="chat",
            )
        )
        session.add(
            Session(
                session_id="session-1",
                conversation_id="conversation-1",
                user_email="user@example.com",
                agent_id="agent-1",
                intaris_session_id="intaris-1",
                mnemory_session_id="mnemory-1",
            )
        )
        await session.commit()

    class Auth:
        def __init__(self) -> None:
            self.sign_calls = 0

        def sign_evidence_jwt(self, _body) -> str:
            self.sign_calls += 1
            return "token"

    class Worker:
        def __init__(self) -> None:
            self.auth_provider = Auth()
            self.remember_calls = 0

        async def remember_evidence(self, _body, _token):
            self.remember_calls += 1
            return SimpleNamespace(status="accepted", status_code=200)

    worker = Worker()
    event_hash = "event-hash-deselected"
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash)
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        trusted_evidence_enabled=worker_enabled,
        trusted_evidence_owner_allowlist=worker_allowlist,
        trusted_evidence_policy_fingerprint="fedcba9876543210",
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    payload = {
        "item_id": evidence_id,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash,
        "event_seq": 7,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }
    await queue.enqueue(payload)
    ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash)
    await queue.enqueue(
        {
            **payload,
            "item_id": ordinary_id,
            "queue_kind": ORDINARY_USER_QUEUE_KIND,
            "depends_on": evidence_id,
        }
    )
    assert queue.policy_mismatch_active is True
    claimed = await queue._claim_due_durable_items(1)
    assert len(claimed) == 1

    async def resolve_evidence_body(_payload):
        return _evidence_body()

    async def revalidate_evidence_signing_inputs(_item, body):
        return SimpleNamespace(
            body=body,
            token=worker.auth_provider.sign_evidence_jwt(body),
        )

    monkeypatch.setattr(queue, "_resolve_evidence_body", resolve_evidence_body)
    monkeypatch.setattr(
        queue,
        "_revalidate_evidence_signing_inputs",
        revalidate_evidence_signing_inputs,
    )
    await queue._process_evidence_item(claimed[0])

    async with factory() as session:
        evidence = await session.get(RememberQueueRow, evidence_id)
        ordinary = await session.get(RememberQueueRow, ordinary_id)
        assert evidence is not None
        assert evidence.status == "accepted"
        assert ordinary is not None
        assert ordinary.status == "pending"
    assert worker.auth_provider.sign_calls == 1
    assert worker.remember_calls == 1
    assert queue.policy_mismatch_active is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_negative_admission_cannot_expand_on_enabled_worker(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/negative-admission.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("owner@example.com",),
        trusted_evidence_policy_fingerprint="0123456789abcdef",
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    negative_admission = serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=False,
            owner_id="owner@example.com",
            policy_fingerprint="fedcba9876543210",
            event_binding=_event_binding(),
        )
    )

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
        event_seq=7,
        event_hash_value="negative-event",
        owner_email="owner@example.com",
        evidence_admission=negative_admission,
    )

    async with factory() as session:
        assert (
            int(await session.scalar(sa.select(sa.func.count(RememberQueueRow.item_id))) or 0) == 0
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_frozen_admission_cannot_expand_to_another_owner(tmp_path, ADMISSION) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cross-owner.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("other@example.com",),
        trusted_evidence_policy_fingerprint="fedcba9876543210",
        trusted_evidence_admission_key=ADMISSION_KEY,
    )

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
        event_seq=7,
        event_hash_value="cross-owner-event",
        owner_email="other@example.com",
        evidence_admission=ADMISSION,
    )

    async with factory() as session:
        assert (
            int(await session.scalar(sa.select(sa.func.count(RememberQueueRow.item_id))) or 0) == 0
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_copied_admission_at_another_sequence_keeps_one_canonical_row(
    tmp_path,
    ADMISSION,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/canonical-replay.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@example.com"))
        await session.commit()
    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    common = {
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }
    await queue.enqueue(
        {
            **common,
            "item_id": deterministic_queue_id(EVIDENCE_QUEUE_KIND, "hash-at-seq-7"),
            "event_hash": "hash-at-seq-7",
            "event_seq": 7,
        }
    )
    await queue.enqueue(
        {
            **common,
            "item_id": deterministic_queue_id(EVIDENCE_QUEUE_KIND, "hash-at-seq-8"),
            "event_hash": "hash-at-seq-8",
            "event_seq": 8,
        }
    )

    async with factory() as session:
        rows = (
            await session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND
                )
            )
        ).scalars()
        assert len(list(rows)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_keeps_assistant_on_first_canonical_replay(
    tmp_path,
    ADMISSION,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cognis.core.remember_queue._utcnow",
        lambda: datetime(2100, 1, 1, tzinfo=UTC),
    )
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/reconcile-replay.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all([User(email="user@example.com"), User(email="owner@example.com")])
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"),
                Conversation(
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="chat",
                ),
                Session(
                    session_id="session-1",
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    intaris_session_id="intaris-1",
                    mnemory_session_id="mnemory-1",
                ),
            ]
        )
        await session.commit()
    admission = deserialize_evidence_admission(ADMISSION)
    assert admission is not None
    marker = build_marker(
        content="hello",
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        user_id="user@example.com",
        owner_id="owner@example.com",
        intaris_session_id="intaris-1",
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_refs_value=[],
        admission=admission,
        admission_key=ADMISSION_KEY,
    )
    user_data = {
        "source": "user_input",
        "role": "user",
        "prompt_visibility": "user_visible",
        "prompt_provenance": {"kind": "user_authored"},
        "user_visible_content": "hello",
        "attachments": [],
        "turn_id": "turn-1",
        "trusted_evidence": marker,
    }
    events = [
        {"seq": 7, "type": "user_message", "data": user_data},
        {"seq": 8, "type": "user_message", "data": user_data},
        {
            "seq": 9,
            "type": "assistant_message",
            "data": {"turn_id": "turn-1", "content": "answer"},
        },
    ]

    class Reader:
        async def read_events(self, **_kwargs):
            return SimpleNamespace(events=events)

    queue = RememberRetryQueue(
        object(),
        session_factory=factory,
        event_reader=Reader(),
        trusted_evidence_admission_key=ADMISSION_KEY,
        max_depth=1,
    )
    preexisting_hash = event_hash("intaris-1", 8, events[1])
    preexisting_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, preexisting_hash)
    await queue.enqueue(
        {
            "item_id": preexisting_id,
            "queue_kind": EVIDENCE_QUEUE_KIND,
            "event_hash": preexisting_hash,
            "event_seq": 8,
            "cognis_session_id": "session-1",
            "intaris_session_id": "intaris-1",
            "conversation_id": "conversation-1",
            "turn_id": "turn-1",
            "user_email": "user@example.com",
            "owner_email": "owner@example.com",
            "agent_owner_email": "owner@example.com",
            "agent_id": "agent-1",
            "policy_agent_id": "agent-1",
            "marker_admitted_at": admission.admitted_at,
            TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
        }
    )
    await queue.reconcile_trusted_evidence()

    async with factory() as session:
        evidence_rows = (
            await session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == EVIDENCE_QUEUE_KIND
                )
            )
        ).scalars()
        evidence = list(evidence_rows)
        assert [row.item_id for row in evidence] == [preexisting_id]
        assert evidence[0].payload["ordinary_work_needed"] is True
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(RememberQueueRow)
                .where(RememberQueueRow.payload["queue_kind"].as_string() != EVIDENCE_QUEUE_KIND)
            )
            == 0
        )
        evidence[0].status = "unavailable"
        await session.commit()

    await queue.reconcile_trusted_evidence()
    async with factory() as session:
        ordinary_user = (
            await session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == ORDINARY_USER_QUEUE_KIND
                )
            )
        ).scalar_one()
        assert ordinary_user.status == "pending"
        ordinary_user.status = "completed"
        await session.commit()

    await queue.reconcile_trusted_evidence()
    async with factory() as session:
        assistant = (
            await session.execute(
                sa.select(RememberQueueRow).where(
                    RememberQueueRow.payload["queue_kind"].as_string() == "ordinary_assistant"
                )
            )
        ).scalar_one()
        assert assistant.status == "pending"
        assert assistant.payload["depends_on"] == preexisting_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_marker_admission_mismatch_never_mints_a_token(tmp_path, ADMISSION) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/marker-admission.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all([User(email="user@example.com"), User(email="owner@example.com")])
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"),
                Conversation(
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="chat",
                ),
                Session(
                    session_id="session-1",
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    intaris_session_id="intaris-1",
                    mnemory_session_id="mnemory-1",
                ),
            ]
        )
        await session.commit()

    marker_admission = build_evidence_admission(
        key=ADMISSION_KEY,
        admitted=True,
        owner_id="owner@example.com",
        policy_fingerprint="fedcba9876543210",
        event_binding=_event_binding(content="remember this"),
    )
    marker = build_marker(
        content="remember this",
        source="user_input",
        role="user",
        prompt_visibility="user_visible",
        prompt_provenance={"kind": "user_authored"},
        user_id="user@example.com",
        owner_id="owner@example.com",
        intaris_session_id="intaris-1",
        cognis_session_id="session-1",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_refs_value=[],
        admission=marker_admission,
        admission_key=ADMISSION_KEY,
    )
    raw_event = {
        "seq": 7,
        "type": "user_message",
        "data": {
            "source": "user_input",
            "role": "user",
            "prompt_visibility": "user_visible",
            "prompt_provenance": {"kind": "user_authored"},
            "user_visible_content": "remember this",
            "attachments": [],
            "turn_id": "turn-1",
            "trusted_evidence": marker,
        },
    }
    event_hash_value = event_hash("intaris-1", 7, raw_event)
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
    ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value)

    class Auth:
        def __init__(self) -> None:
            self.sign_calls = 0

        def sign_evidence_jwt(self, _body) -> str:
            self.sign_calls += 1
            return "token"

    class Worker:
        def __init__(self) -> None:
            self.auth_provider = Auth()
            self.remember_calls = 0

        async def remember_evidence(self, _body, _token):
            self.remember_calls += 1
            raise AssertionError("mismatched admission must not dispatch")

    class Reader:
        async def read_events(self, **_kwargs):
            return SimpleNamespace(events=[raw_event])

    worker = Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        event_reader=Reader(),
        trusted_evidence_enabled=False,
        trusted_evidence_owner_allowlist=(),
        trusted_evidence_policy_fingerprint="0000000000000000",
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    payload = {
        "item_id": evidence_id,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash_value,
        "event_seq": 7,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }
    await queue.enqueue(payload)
    await queue.enqueue(
        {
            **payload,
            "item_id": ordinary_id,
            "queue_kind": ORDINARY_USER_QUEUE_KIND,
            "depends_on": evidence_id,
        }
    )
    claimed = await queue._claim_due_durable_items(1)

    assert len(claimed) == 1
    await queue._process_evidence_item(claimed[0])
    async with factory() as session:
        evidence = await session.get(RememberQueueRow, evidence_id)
        ordinary = await session.get(RememberQueueRow, ordinary_id)
        assert evidence is not None and evidence.status == "conflict"
        assert ordinary is not None and ordinary.status == "pending"
    assert worker.auth_provider.sign_calls == 0
    assert worker.remember_calls == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_fallback_deselection_reconciles_and_completes_ordinary_memory(
    tmp_path,
    ADMISSION,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/fallback-deselection.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                User(email="user@example.com"),
                User(email="owner@example.com"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"),
                Conversation(
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="chat",
                ),
                Session(
                    session_id="session-1",
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    intaris_session_id="intaris-1",
                    mnemory_session_id="mnemory-1",
                ),
            ]
        )
        await session.commit()

    raw_event = {
        "seq": 7,
        "type": "user_message",
        "data": {"user_visible_content": "remember this", "turn_id": "turn-1"},
    }
    event_hash_value = event_hash("intaris-1", 7, raw_event)

    class Reader:
        async def read_events(self, **_kwargs):
            return SimpleNamespace(events=[raw_event])

    class Worker:
        def __init__(self) -> None:
            self.calls: list[tuple[EvidenceRememberRequest, str]] = []

        async def remember_user_event(
            self,
            body: EvidenceRememberRequest,
            token: str,
        ) -> None:
            self.calls.append((body, token))

        async def remember(self, **_kwargs: object) -> None:
            raise AssertionError("paired ordinary user used generic remember")

    worker = Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        event_reader=Reader(),
        max_depth=1,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("owner@example.com",),
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    await queue.enqueue(
        {
            "item_id": deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "existing"),
            "queue_kind": ORDINARY_USER_QUEUE_KIND,
            "session_id": "mnemory-1",
            "user_email": "user@example.com",
        }
    )
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
        event_seq=7,
        event_hash_value=event_hash_value,
        owner_email="owner@example.com",
        evidence_admission=ADMISSION,
    )
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
    ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value)
    async with factory() as session:
        existing = await session.get(
            RememberQueueRow,
            deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "existing"),
        )
        assert existing is not None
        existing.status = "completed"
        await session.commit()
    restarted_queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        event_reader=Reader(),
        max_depth=1,
        trusted_evidence_enabled=False,
        trusted_evidence_owner_allowlist=(),
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    body = EvidenceRememberRequest.model_validate(
        {
            "version": 1,
            "actor": {
                "user_id": "user@example.com",
                "owner_id": "owner@example.com",
            },
            "event": {
                "id": "intaris-1:7",
                "event_hash": event_hash_value,
                "cognis_session_id": "session-1",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
            },
            "messages": [{"role": "user", "content": "remember this"}],
        }
    )

    async def resolve_body(_payload):
        return body

    async def revalidate(*_args, **_kwargs):
        return SimpleNamespace(body=body, token="user-event-token")

    restarted_queue._resolve_evidence_body = resolve_body  # type: ignore[method-assign]
    restarted_queue._revalidate_evidence_signing_inputs = revalidate  # type: ignore[method-assign]
    await restarted_queue.reconcile_trusted_evidence()

    async with factory() as session:
        evidence = await session.get(RememberQueueRow, evidence_id)
        ordinary = await session.get(RememberQueueRow, ordinary_id)
        assert evidence is not None
        assert evidence.status == "unavailable"
        assert evidence.payload["ordinary_work_needed"] is False
        assert ordinary is not None
        assert ordinary.status == "pending"
    claimed = await restarted_queue._claim_due_durable_items(1)
    await restarted_queue._process(claimed[0], asyncio.Semaphore(1))

    assert worker.calls == [(body, "user-event-token")]
    async with factory() as session:
        ordinary = await session.get(RememberQueueRow, ordinary_id)
        assert ordinary is not None
        assert ordinary.status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_owner_mutation_before_evidence_mint_is_terminal_and_unblocks_ordinary(
    tmp_path, monkeypatch, ADMISSION
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/owner-race.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                User(email="user@example.com"),
                User(email="owner@example.com"),
                User(email="new-owner@example.com"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"),
                Conversation(
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="chat",
                ),
                Session(
                    session_id="session-1",
                    conversation_id="conversation-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    intaris_session_id="intaris-1",
                    mnemory_session_id="mnemory-1",
                ),
            ]
        )
        await session.commit()
    raw_event = {
        "seq": 7,
        "type": "user_message",
        "data": {"user_visible_content": "sensitive", "turn_id": "turn-1"},
    }
    event_hash_value = event_hash("intaris-1", 7, raw_event)
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash_value)
    ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, event_hash_value)
    payload = {
        "item_id": evidence_id,
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash_value,
        "event_seq": 7,
        "cognis_session_id": "session-1",
        "intaris_session_id": "intaris-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "user_email": "user@example.com",
        "owner_email": "owner@example.com",
        "agent_owner_email": "owner@example.com",
        "agent_id": "agent-1",
        "policy_agent_id": "agent-1",
        TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
    }

    class Auth:
        def __init__(self) -> None:
            self.sign_calls = 0

        def sign_evidence_jwt(self, _body) -> str:
            self.sign_calls += 1
            return "token"

    class Worker:
        def __init__(self) -> None:
            self.auth_provider = Auth()
            self.remember_calls = 0

        async def remember_evidence(self, _body, _token):
            self.remember_calls += 1
            raise AssertionError("owner mutation must prevent evidence dispatch")

    worker = Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        event_reader=SimpleNamespace(),
        max_depth=2,
        trusted_evidence_enabled=True,
        trusted_evidence_owner_allowlist=("owner@example.com",),
        trusted_evidence_admission_key=ADMISSION_KEY,
    )
    await queue.enqueue(payload)
    await queue.enqueue(
        {
            **payload,
            "item_id": ordinary_id,
            "queue_kind": ORDINARY_USER_QUEUE_KIND,
            "depends_on": evidence_id,
        }
    )
    claimed = await queue._claim_due_durable_items(1)
    assert len(claimed) == 1
    async with factory() as session:
        agent = await session.get(Agent, "agent-1")
        assert agent is not None
        agent.owner_email = "new-owner@example.com"
        await session.commit()

    async def resolve_evidence_body(_payload):
        return EvidenceRememberRequest.model_validate(
            {
                "version": 1,
                "actor": {"user_id": "user@example.com", "owner_id": "owner@example.com"},
                "event": {
                    "id": "intaris-1:7",
                    "event_hash": event_hash_value,
                    "cognis_session_id": "session-1",
                    "conversation_id": "conversation-1",
                    "turn_id": "turn-1",
                },
                "messages": [{"role": "user", "content": "sensitive"}],
            }
        )

    monkeypatch.setattr(queue, "_resolve_evidence_body", resolve_evidence_body)
    await queue._process_evidence_item(claimed[0])

    async with factory() as session:
        evidence = await session.get(RememberQueueRow, evidence_id)
        ordinary = await session.get(RememberQueueRow, ordinary_id)
        assert evidence is not None
        assert evidence.status == "unavailable"
        assert ordinary is not None
        assert ordinary.status == "pending"
    assert worker.auth_provider.sign_calls == 0
    assert worker.remember_calls == 0
    await engine.dispose()


def _evidence_body() -> EvidenceRememberRequest:
    return EvidenceRememberRequest.model_validate(
        {
            "version": 1,
            "actor": {"user_id": "user@example.com", "owner_id": "owner@example.com"},
            "event": {
                "id": "intaris-1:7",
                "event_hash": "0" * 64,
                "cognis_session_id": "session-1",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
            },
            "messages": [{"role": "user", "content": "hello"}],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_paired_ordinary_user_uses_body_bound_shared_ingestion(
    tmp_path,
    ADMISSION,
    monkeypatch,
    rejected,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/shared-user-handoff.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                User(email="user@example.com"),
                User(email="owner@example.com"),
            ]
        )
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
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
        session.add(
            Session(
                session_id="session-1",
                conversation_id="conversation-1",
                user_email="user@example.com",
                agent_id="agent-1",
                intaris_session_id="intaris-1",
                mnemory_session_id="mnemory-1",
            )
        )
        await session.commit()

    body = _evidence_body()
    body.messages[0].content = "Ž" * 40_000
    user_event_calls: list[tuple[EvidenceRememberRequest, str]] = []

    class Auth:
        evidence_calls = 0
        user_event_calls = 0

        def sign_evidence_jwt(self, _body) -> str:
            self.evidence_calls += 1
            return "wrong-token"

        def sign_user_event_jwt(self, signed_body) -> str:
            self.user_event_calls += 1
            assert signed_body.actor.user_id == "user@example.com"
            assert signed_body.actor.owner_id == "owner@example.com"
            return "user-event-token"

    class Worker:
        def __init__(self) -> None:
            self.auth_provider = Auth()

        async def remember_user_event(self, signed_body, token) -> None:
            user_event_calls.append((signed_body, token))
            if rejected:
                from cognis.providers.memory.evidence import (
                    TrustedEventRejectedError,
                    TrustedEventRejection,
                )

                raise TrustedEventRejectedError(
                    TrustedEventRejection(
                        status="rejected",
                        outcome="rejected_before_write",
                        reason="input_budget_exceeded",
                        terminal=True,
                        retryable=False,
                        fallback_allowed=False,
                        semantic_effects="none",
                        source_retention="caller_queue",
                        operation_id="b72ca2d1-27cb-4c1e-810e-57df94088021",
                    ),
                    signed_body,
                )

        async def remember(self, **_kwargs) -> None:
            raise AssertionError("paired ordinary user used generic remember")

    worker = Worker()
    queue = RememberRetryQueue(
        worker,
        session_factory=factory,
        trusted_evidence_admission_key=ADMISSION_KEY,
    )

    async def resolve_body(_payload):
        return body

    token_kinds: list[str] = []

    async def revalidate(_item, signed_body, *, token_kind):
        token_kinds.append(token_kind)
        return SimpleNamespace(body=signed_body, token="user-event-token")

    monkeypatch.setattr(queue, "_resolve_evidence_body", resolve_body)
    monkeypatch.setattr(queue, "_revalidate_evidence_signing_inputs", revalidate)

    async def memory_enabled(_payload):
        return False

    monkeypatch.setattr(queue, "_hard_memory_disable_applies", memory_enabled)
    evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, "0" * 64)
    item = RememberQueueItem(
        payload={
            "queue_kind": ORDINARY_USER_QUEUE_KIND,
            "user_email": "user@example.com",
            "owner_email": "owner@example.com",
            "agent_owner_email": "owner@example.com",
            "agent_id": "agent-1",
            "policy_agent_id": "agent-1",
            "session_id": "mnemory-1",
            "cognis_session_id": "session-1",
            "intaris_session_id": "intaris-1",
            "conversation_id": "conversation-1",
            "turn_id": "turn-1",
            "event_seq": 7,
            "event_hash": "0" * 64,
            "depends_on": evidence_id,
            TRUSTED_EVIDENCE_ADMISSION_KEY: ADMISSION,
        },
        item_id=deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "0" * 64),
        lease_token="lease-token",
    )
    async with factory() as session:
        session.add_all(
            [
                RememberQueueRow(
                    item_id=evidence_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="mnemory-1",
                    status="skipped",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                    payload={"queue_kind": EVIDENCE_QUEUE_KIND},
                ),
                RememberQueueRow(
                    item_id=item.item_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="mnemory-1",
                    status="leased",
                    attempts=1,
                    next_retry_at=datetime.now(UTC),
                    lease_token=item.lease_token,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    payload=item.payload,
                ),
            ]
        )
        await session.commit()

    await queue._process(item, asyncio.Semaphore(1))

    assert len(user_event_calls) == 1
    assert user_event_calls[0][0].model_dump(mode="json") == body.model_dump(mode="json")
    assert user_event_calls[0][1] == "user-event-token"
    assert token_kinds == ["user_event"]
    async with factory() as session:
        completed = await session.get(RememberQueueRow, item.item_id)
        assert completed is not None
        assert completed.status == ("failed" if rejected else "completed")
        if rejected:
            assert completed.payload["rejected_source"] == body.model_dump(mode="json")
            assert "input_budget_exceeded" in completed.last_error
            assert "user-event-token" not in str(completed.payload)
    if rejected:
        assert await queue._claim_due_durable_items(10) == []
        from cognis.providers.memory.evidence import (
            TrustedEventRejectedError,
            TrustedEventRejection,
        )

        async with factory() as session:
            evidence_row = await session.get(RememberQueueRow, evidence_id)
            evidence_row.status = "dispatching"
            evidence_row.lease_token = "evidence-lease"
            await session.commit()
        evidence_item = RememberQueueItem(
            payload={**item.payload, "queue_kind": EVIDENCE_QUEUE_KIND},
            item_id=evidence_id,
            lease_token="evidence-lease",
        )
        await queue._fail_trusted_rejection(
            evidence_item,
            TrustedEventRejectedError(
                TrustedEventRejection.model_validate(completed.payload["trusted_rejection"]),
                body,
            ),
        )
        async with factory() as session:
            evidence_row = await session.get(RememberQueueRow, evidence_id)
            assert evidence_row.status == "rejected"
            assert evidence_row.payload["rejected_source"] == body.model_dump(mode="json")
            assert "input_budget_exceeded" in evidence_row.last_error
        assert await queue._dependency_is_terminal(evidence_id)
        await queue.enqueue({**item.payload, "item_id": item.item_id})
        await queue.enqueue({**evidence_item.payload, "item_id": evidence_id})
        assert await queue._claim_due_durable_items(10) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_ordinary_assistant_remains_agent_scoped(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Worker:
        async def remember(self, **kwargs) -> None:
            calls.append(dict(kwargs))

        async def remember_user_event(self, *_args) -> None:
            raise AssertionError("assistant used trusted user-event ingestion")

    queue = RememberRetryQueue(Worker())
    event_hash_value = "1" * 64
    assistant_hash = "2" * 64
    item = RememberQueueItem(
        payload={
            "queue_kind": "ordinary_assistant",
            "event_hash": event_hash_value,
            "assistant_event_seq": 9,
            "assistant_event_hash": assistant_hash,
            "agent_id": "agent-1",
            "policy_agent_id": "agent-1",
            "agent_owner_email": "owner@example.com",
            "owner_email": "owner@example.com",
        },
        item_id=deterministic_queue_id(
            "ordinary_assistant",
            event_hash_value,
            assistant_hash,
        ),
    )
    queue._items.append(item)

    async def memory_enabled(_payload):
        return False

    async def resolve_payload(_payload):
        return {
            "session_id": "mnemory-1",
            "messages": [{"role": "assistant", "content": "answer"}],
            "user_email": "user@example.com",
            "agent_id": "agent-1",
            "agent_owner_email": "owner@example.com",
        }

    monkeypatch.setattr(queue, "_hard_memory_disable_applies", memory_enabled)
    monkeypatch.setattr(queue, "_resolve_payload", resolve_payload)

    await queue._process(item, asyncio.Semaphore(1))

    assert calls == [
        {
            "session_id": "mnemory-1",
            "messages": [{"role": "assistant", "content": "answer"}],
            "user_email": "user@example.com",
            "agent_id": "agent-1",
            "agent_owner_email": "owner@example.com",
        }
    ]
