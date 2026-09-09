from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.trusted_evidence import (
    EVIDENCE_QUEUE_KIND,
    ORDINARY_ASSISTANT_QUEUE_KIND,
    deterministic_queue_id,
)
from cognis.store.database import create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    RememberQueueRow,
    Session,
    User,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _asyncpg_url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("COGNIS_TEST_POSTGRES_URL must use PostgreSQL with asyncpg")
    return url


@pytest.mark.asyncio
async def test_concurrent_reconciliation_repairs_one_pre_dispatch_assistant_row() -> None:
    url = _asyncpg_url()
    schema_name = f"cognis_remember_queue_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        event_hash = "evidence-event-hash"
        assistant_hash = "assistant-event-hash"
        evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, event_hash)
        item_id = deterministic_queue_id(
            ORDINARY_ASSISTANT_QUEUE_KIND,
            event_hash,
            assistant_hash,
        )
        payload = {
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
            "item_id": item_id,
            "event_hash": event_hash,
            "depends_on": evidence_id,
            "include_user_message": False,
            "user_event_seq": None,
            "assistant_event_seq": 2,
            "assistant_event_hash": assistant_hash,
        }
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
            session.add(User(email="user@example.com"))
            await session.flush()
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
                        session_id="mnemory-1",
                        user_email="user@example.com",
                        agent_id="agent-1",
                        payload=malformed_payload,
                        status="failed",
                        attempts=1,
                        next_retry_at=datetime.now(UTC),
                        last_error="ordinary remember assertion conflict",
                    ),
                ]
            )
            await session.commit()

        first = RememberRetryQueue(object(), session_factory=factory)
        second = RememberRetryQueue(object(), session_factory=factory)
        await asyncio.gather(first.enqueue(payload), second.enqueue(payload))

        async with factory() as session:
            row = await session.get(RememberQueueRow, item_id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error is None
        assert row.payload["event_hash"] == event_hash
        assert row.payload["depends_on"] == evidence_id
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_rotation_repair_updates_one_fenced_row() -> None:
    url = _asyncpg_url()
    schema_name = f"cognis_remember_rotation_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))

    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        evidence_hash = "evidence-event-hash"
        assistant_hash = "assistant-event-hash"
        evidence_id = deterministic_queue_id(EVIDENCE_QUEUE_KIND, evidence_hash)
        item_id = deterministic_queue_id(
            ORDINARY_ASSISTANT_QUEUE_KIND,
            evidence_hash,
            assistant_hash,
        )
        ownership = {
            "conversation_id": "conversation-1",
            "turn_id": "turn-1",
            "user_email": "user@example.com",
            "owner_email": "user@example.com",
            "agent_owner_email": "user@example.com",
            "agent_id": "agent-1",
            "policy_agent_id": "agent-1",
        }
        evidence_payload = {
            **ownership,
            "queue_kind": EVIDENCE_QUEUE_KIND,
            "session_id": "mnemory-1",
            "cognis_session_id": "session-1",
            "intaris_session_id": "intaris-1",
            "event_seq": 1,
            "event_hash": evidence_hash,
        }
        assistant_payload = {
            **ownership,
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
            session.add(User(email="user@example.com"))
            await session.flush()
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="user@example.com",
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
            session.add_all(
                [
                    Session(
                        session_id="session-1",
                        conversation_id="conversation-1",
                        user_email="user@example.com",
                        agent_id="agent-1",
                        intaris_session_id="intaris-1",
                        mnemory_session_id="mnemory-1",
                    ),
                    Session(
                        session_id="session-2",
                        conversation_id="conversation-1",
                        user_email="user@example.com",
                        agent_id="agent-1",
                        intaris_session_id="intaris-2",
                        mnemory_session_id="mnemory-2",
                        previous_session_id="session-1",
                    ),
                ]
            )
            await session.flush()
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

        first = RememberRetryQueue(object(), session_factory=factory)
        second = RememberRetryQueue(object(), session_factory=factory)
        loaded = await first._load_rotated_assistant_candidate(item_id)
        assert loaded is not None
        loaded_payload, loaded_evidence, _owner = loaded
        results = await asyncio.gather(
            first._commit_rotated_assistant_repair(
                item_id,
                assistant_payload=loaded_payload,
                evidence_payload=loaded_evidence,
            ),
            second._commit_rotated_assistant_repair(
                item_id,
                assistant_payload=loaded_payload,
                evidence_payload=loaded_evidence,
            ),
        )

        assert sorted(results) == [False, True]
        async with factory() as session:
            row = await session.get(RememberQueueRow, item_id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error is None
        assert row.payload["event_hash"] == evidence_hash
        assert row.payload["depends_on"] == evidence_id
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()
