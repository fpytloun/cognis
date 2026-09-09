from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.trusted_evidence import (
    EVIDENCE_QUEUE_KIND,
    ORDINARY_ASSISTANT_QUEUE_KIND,
    ORDINARY_USER_QUEUE_KIND,
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    build_evidence_admission,
    build_evidence_event_binding,
    deterministic_queue_id,
    serialize_evidence_admission,
)
from cognis.core.trusted_evidence import (
    event_hash as calculate_event_hash,
)
from cognis.providers.memory.evidence import EvidenceRememberRequest
from cognis.store.database import create_session_factory
from cognis.store.models import Agent, Conversation, RememberQueueRow, Session, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]
ADMISSION_KEY = b"trusted-evidence-postgres-test-key"


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return (
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://")
        else url
    )


def _upgrade(sync_connection: Any) -> None:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", _url())
    config.config_file_name = None
    config.attributes["connection"] = sync_connection
    command.upgrade(config, "head")


@asynccontextmanager
async def _database() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    suffix = uuid4().hex
    schema_name = f"trusted_evidence_queue_{suffix}"
    user_email = f"trusted-evidence-{suffix}@test.local"
    admin = create_async_engine(_url())
    try:
        async with admin.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema_name))
        engine = create_async_engine(
            _url(),
            connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
        )
        try:
            async with engine.connect() as connection:
                await connection.run_sync(_upgrade)
            factory = create_session_factory(engine)
            async with factory() as session:
                session.add(User(email=user_email))
                await session.flush()
                session.add(
                    Agent(agent_id="agent", owner_email=user_email, name="Queue test agent")
                )
                session.add(
                    Conversation(
                        conversation_id="conversation",
                        user_email=user_email,
                        agent_id="agent",
                        context_type="chat",
                    )
                )
                session.add(
                    Session(
                        session_id="cognis-session",
                        conversation_id="conversation",
                        user_email=user_email,
                        agent_id="agent",
                        intaris_session_id="intaris-session",
                        mnemory_session_id="mnemory-session",
                    )
                )
                await session.commit()
            yield factory, user_email
        finally:
            await engine.dispose()
    finally:
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


def _evidence_payload(
    event_hash: str,
    user_email: str,
    *,
    max_attempts: int = 8,
) -> dict[str, Any]:
    return {
        "item_id": deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash),
        "queue_kind": EVIDENCE_QUEUE_KIND,
        "event_hash": event_hash,
        "event_seq": 7,
        "session_id": "mnemory-session",
        "cognis_session_id": "cognis-session",
        "intaris_session_id": "intaris-session",
        "conversation_id": "conversation",
        "turn_id": "turn",
        "user_email": user_email,
        "owner_email": user_email,
        "agent_owner_email": user_email,
        "agent_id": "agent",
        "policy_agent_id": "agent",
        TRUSTED_EVIDENCE_ADMISSION_KEY: _admission(
            user_email,
            max_attempts=max_attempts,
        ),
        "messages": [{"role": "user", "content": "must not persist"}],
    }


def _admission(owner_id: str, *, max_attempts: int = 8) -> dict[str, Any]:
    return serialize_evidence_admission(
        build_evidence_admission(
            key=ADMISSION_KEY,
            admitted=True,
            owner_id=owner_id,
            policy_fingerprint="0123456789abcdef",
            event_binding=build_evidence_event_binding(
                intaris_session_id="intaris",
                cognis_session_id="cognis",
                conversation_id="conversation",
                turn_id="turn",
                user_id=owner_id,
                owner_id=owner_id,
                source="user_input",
                role="user",
                prompt_visibility="user_visible",
                prompt_provenance={"kind": "user_authored"},
                content_hash=hashlib.sha256(b"event").hexdigest(),
                attachment_refs_value=[],
            ),
            max_attempts=max_attempts,
        )
    )


def _ordinary_payload(
    event_hash: str,
    user_email: str,
    *,
    queue_kind: str = ORDINARY_USER_QUEUE_KIND,
    depends_on: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_id": deterministic_queue_id(queue_kind, event_hash),
        "queue_kind": queue_kind,
        "event_hash": event_hash,
        "event_seq": 8,
        "session_id": "mnemory-session",
        "cognis_session_id": "cognis-session",
        "intaris_session_id": "intaris-session",
        "conversation_id": "conversation",
        "turn_id": "turn",
        "user_email": user_email,
        "owner_email": user_email,
        "agent_owner_email": user_email,
        "agent_id": "agent",
        "policy_agent_id": "agent",
    }
    if depends_on is not None:
        payload["depends_on"] = depends_on
    if queue_kind == ORDINARY_ASSISTANT_QUEUE_KIND:
        payload.update(
            {
                "assistant_event_seq": 9,
                "assistant_event_hash": f"assistant-{event_hash}",
            }
        )
    return payload


async def _rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[RememberQueueRow]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(RememberQueueRow).order_by(RememberQueueRow.item_id.asc())
                )
            )
            .scalars()
            .all()
        )


def _assert_metadata_only(rows: list[RememberQueueRow]) -> None:
    forbidden = {"content", "messages", "token", "plan"}
    for row in rows:
        assert forbidden.isdisjoint(row.payload)


@pytest.mark.asyncio
async def test_concurrent_duplicate_event_enqueue_is_idempotent() -> None:
    async with _database() as (factory, user_email):
        queue = RememberRetryQueue(
            object(),
            session_factory=factory,
            max_depth=4,
            trusted_evidence_enabled=True,
            trusted_evidence_owner_allowlist=(user_email,),
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        session = type(
            "Session",
            (),
            {
                "session_id": "cognis-session",
                "mnemory_session_id": "mnemory-session",
                "intaris_session_id": "intaris-session",
                "user_email": user_email,
                "agent_profile_id": "profile",
            },
        )()
        agent = type("Agent", (), {"agent_id": "agent"})()

        await asyncio.gather(
            *(
                queue.enqueue_after_user_append(
                    session=session,
                    agent=agent,
                    conversation_id="conversation",
                    turn_id="turn",
                    event_seq=7,
                    event_hash_value="same-event",
                    owner_email=user_email,
                    evidence_admission=_admission(user_email),
                )
                for _ in range(8)
            )
        )

        rows = await _rows(factory)
        assert len(rows) == 2
        assert {row.payload["queue_kind"] for row in rows} == {
            EVIDENCE_QUEUE_KIND,
            ORDINARY_USER_QUEUE_KIND,
        }
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 2
        _assert_metadata_only(rows)


@pytest.mark.asyncio
async def test_simultaneous_evidence_user_assistant_enqueue_respects_capacity() -> None:
    async with _database() as (factory, user_email):
        queue = RememberRetryQueue(
            object(),
            session_factory=factory,
            max_depth=2,
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        evidence = _evidence_payload("evidence-event", user_email)
        user = _ordinary_payload("user-event", user_email)
        assistant = _ordinary_payload(
            "assistant-event",
            user_email,
            queue_kind=ORDINARY_ASSISTANT_QUEUE_KIND,
        )
        start = asyncio.Event()

        async def enqueue(payload: dict[str, Any]) -> None:
            await start.wait()
            await queue.enqueue(payload)

        tasks = [asyncio.create_task(enqueue(payload)) for payload in (evidence, user, assistant)]
        start.set()
        await asyncio.gather(*tasks)

        rows = await _rows(factory)
        active = [row for row in rows if row.status in {"pending", "leased", "dispatching"}]
        assert len(active) <= queue.max_depth
        row_ids = {row.item_id for row in rows}
        assert evidence["item_id"] in row_ids
        assert row_ids - {evidence["item_id"]} <= {
            user["item_id"],
            assistant["item_id"],
        }
        assert len(rows) == 2
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 2
        _assert_metadata_only(rows)


@pytest.mark.asyncio
async def test_full_capacity_retains_evidence_suppression_then_admits_ordinary_work() -> None:
    async with _database() as (factory, user_email):
        queue = RememberRetryQueue(
            object(),
            session_factory=factory,
            max_depth=1,
            trusted_evidence_enabled=True,
            trusted_evidence_owner_allowlist=(user_email,),
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        await queue.enqueue(_ordinary_payload("existing-event", user_email))
        session = type(
            "Session",
            (),
            {
                "session_id": "cognis-session",
                "mnemory_session_id": "mnemory-session",
                "intaris_session_id": "intaris-session",
                "user_email": user_email,
                "agent_profile_id": "profile",
            },
        )()
        agent = type("Agent", (), {"agent_id": "agent"})()
        kwargs = {
            "session": session,
            "agent": agent,
            "conversation_id": "conversation",
            "turn_id": "turn",
            "event_seq": 7,
            "event_hash_value": "retained-event",
            "owner_email": user_email,
            "evidence_admission": _admission(user_email),
        }
        await asyncio.gather(
            queue.enqueue_after_user_append(**kwargs),
            queue.enqueue_after_user_append(**kwargs),
        )

        evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, "retained-event")
        ordinary_id = deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "retained-event")
        rows = await _rows(factory)
        evidence = next(row for row in rows if row.item_id == evidence_id)
        assert evidence.status == "unavailable"
        assert evidence.payload["ordinary_work_needed"] is True
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 1

        async with factory() as db:
            existing = await db.get(
                RememberQueueRow,
                deterministic_queue_id(ORDINARY_USER_QUEUE_KIND, "existing-event"),
            )
            assert existing is not None
            existing.status = "completed"
            await db.commit()
        await queue.enqueue_after_user_append(**kwargs)

        rows = await _rows(factory)
        evidence = next(row for row in rows if row.item_id == evidence_id)
        ordinary = next(row for row in rows if row.item_id == ordinary_id)
        assert evidence.status == "unavailable"
        assert evidence.payload["ordinary_work_needed"] is False
        assert ordinary.status == "pending"
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 1
        _assert_metadata_only(rows)


@pytest.mark.asyncio
async def test_postgres_processes_dependent_ordinary_user_work_with_exact_authority() -> None:
    async with _database() as (factory, user_email):
        raw_event = {
            "seq": 8,
            "type": "user_message",
            "data": {"user_visible_content": "persist this", "turn_id": "turn"},
        }
        event_hash_value = calculate_event_hash("intaris-session", 8, raw_event)
        evidence = _evidence_payload(event_hash_value, user_email)
        ordinary = _ordinary_payload(
            event_hash_value,
            user_email,
            depends_on=evidence["item_id"],
        )

        class Reader:
            async def read_events(self, **_kwargs: object) -> object:
                return SimpleNamespace(events=[raw_event])

        class Worker:
            def __init__(self) -> None:
                self.auth_provider = SimpleNamespace(
                    sign_user_event_jwt=lambda _body: "user-event-token"
                )
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
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        body = EvidenceRememberRequest.model_validate(
            {
                "version": 1,
                "actor": {"user_id": user_email, "owner_id": user_email},
                "event": {
                    "id": "intaris-session:8",
                    "event_hash": event_hash_value,
                    "cognis_session_id": "cognis-session",
                    "conversation_id": "conversation",
                    "turn_id": "turn",
                },
                "messages": [{"role": "user", "content": "persist this"}],
            }
        )

        async def resolve_body(_payload: dict[str, Any]) -> EvidenceRememberRequest:
            return body

        async def revalidate(*_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(body=body, token="user-event-token")

        queue._resolve_evidence_body = resolve_body  # type: ignore[method-assign]
        queue._revalidate_evidence_signing_inputs = revalidate  # type: ignore[method-assign]
        await queue.enqueue(evidence)
        async with factory() as session:
            row = await session.get(RememberQueueRow, evidence["item_id"])
            assert row is not None
            row.status = "unavailable"
            await session.commit()
        await queue.enqueue(ordinary)
        claimed = await queue._claim_due_durable_items(1)
        assert [item.item_id for item in claimed] == [ordinary["item_id"]]
        await queue._process(claimed[0], asyncio.Semaphore(1))

        assert worker.calls == [(body, "user-event-token")]
        async with factory() as session:
            row = await session.get(RememberQueueRow, ordinary["item_id"])
            assert row is not None
            assert row.status == "completed"


@pytest.mark.asyncio
async def test_abandoned_evidence_suppresses_reenqueue_and_unblocks_ordinary_row() -> None:
    async with _database() as (factory, user_email):
        queue = RememberRetryQueue(
            object(),
            session_factory=factory,
            max_depth=2,
            trusted_evidence_max_attempts=1,
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        evidence = _evidence_payload("abandoned-event", user_email, max_attempts=1)
        await queue.enqueue(evidence)
        claimed = await queue._claim_due_durable_items(1)
        assert len(claimed) == 1

        async with factory() as db:
            row = await db.get(RememberQueueRow, evidence["item_id"])
            assert row is not None
            row.attempts = 1
            await db.commit()
        await queue._process_evidence_item(claimed[0])
        await queue.enqueue(evidence)
        ordinary = _ordinary_payload(
            "abandoned-event",
            user_email,
            depends_on=evidence["item_id"],
        )
        await queue.enqueue(ordinary)

        rows = await _rows(factory)
        evidence_row = next(row for row in rows if row.item_id == evidence["item_id"])
        ordinary_row = next(row for row in rows if row.item_id == ordinary["item_id"])
        assert evidence_row.status == "abandoned"
        assert ordinary_row.status == "pending"
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 1
        _assert_metadata_only(rows)


@pytest.mark.asyncio
async def test_expired_evidence_dispatch_lease_is_taken_over_once() -> None:
    async with _database() as (factory, user_email):
        queue = RememberRetryQueue(
            object(),
            session_factory=factory,
            max_depth=2,
            trusted_evidence_admission_key=ADMISSION_KEY,
        )
        evidence = _evidence_payload("lease-event", user_email)
        await queue.enqueue(evidence)
        claimed = await queue._claim_due_durable_items(1)
        assert len(claimed) == 1
        item = claimed[0]

        async with factory() as db:
            row = await db.get(RememberQueueRow, item.item_id)
            assert row is not None
            row.status = "dispatching"
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

        assert await queue._claim_due_durable_items(1) == []
        rows = await _rows(factory)
        assert rows[0].status == "pending"
        retry = await queue._claim_due_durable_items(1)
        assert [retry_item.item_id for retry_item in retry] == [item.item_id]

        rows = await _rows(factory)
        assert len(rows) == 1
        assert rows[0].status == "leased"
        assert sum(row.status in {"pending", "leased", "dispatching"} for row in rows) == 1
        _assert_metadata_only(rows)


@pytest.mark.asyncio
async def test_two_controllers_claim_ordinary_once_while_reconciliation_is_blocked() -> None:
    async with _database() as (factory, user_email):
        reconcile_started = [asyncio.Event(), asyncio.Event()]
        reconcile_cancelled = [asyncio.Event(), asyncio.Event()]
        remember_called = asyncio.Event()

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

        class Worker:
            def __init__(self) -> None:
                self.calls = 0

            async def remember(self, **_kwargs: object) -> None:
                self.calls += 1
                remember_called.set()

        worker = Worker()
        queues = [
            RememberRetryQueue(
                worker,
                session_factory=factory,
                event_reader=Reader(),
                max_concurrent=1,
                recovery_interval_seconds=60,
            )
            for _ in range(2)
        ]

        def blocker(index: int) -> Any:
            async def block_reconciliation() -> None:
                reconcile_started[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    reconcile_cancelled[index].set()

            return block_reconciliation

        for index, queue in enumerate(queues):
            queue._run_scheduled_reconciliation_cycle = blocker(index)  # type: ignore[method-assign]
            await queue.start()
        await asyncio.wait_for(
            asyncio.gather(*(started.wait() for started in reconcile_started)),
            timeout=2,
        )

        await queues[0].enqueue(
            {
                "item_id": "ordinary-shared",
                "session_id": "mnemory-session",
                "cognis_session_id": "cognis-session",
                "intaris_session_id": "intaris-session",
                "user_email": user_email,
                "agent_id": "agent",
                "agent_owner_email": user_email,
                "user_event_seq": 1,
                "assistant_event_seq": 2,
            }
        )
        await asyncio.wait_for(remember_called.wait(), timeout=2)
        async with asyncio.timeout(2):
            while await _rows(factory):
                await asyncio.sleep(0.01)

        await asyncio.gather(*(queue.stop() for queue in queues))

        assert worker.calls == 1
        assert all(cancelled.is_set() for cancelled in reconcile_cancelled)
