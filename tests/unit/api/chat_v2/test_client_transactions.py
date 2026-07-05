"""Chat v2 client transaction ledger tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, User
from cognis.store.queries import (
    claim_chat_client_transaction,
    complete_chat_client_transaction,
)


def test_chat_client_transaction_migration_chains_after_current_head() -> None:
    migration = Path("cognis/store/migrations/versions/071_chat_client_transactions.py").read_text()

    assert 'down_revision = "7a9390c1ea82"' in migration


async def _factory(tmp_path: object) -> tuple[Any, Any]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/chat-v2-ledger.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@test.com", name="User", role="admin"))
        await session.flush()
        session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="Agent"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@test.com",
                agent_id="agent-1",
                context_type="web",
            )
        )
        await session.commit()
    return engine, factory


@pytest.mark.asyncio
async def test_chat_client_transaction_claim_is_idempotent(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            first, first_created = await claim_chat_client_transaction(
                session,
                conversation_id="conv-1",
                principal_id="user@test.com",
                client_txn_id="txn-1",
                operation="send_message",
                payload_hash="hash-a",
            )
            await session.commit()

        async with factory() as session:
            second, second_created = await claim_chat_client_transaction(
                session,
                conversation_id="conv-1",
                principal_id="user@test.com",
                client_txn_id="txn-1",
                operation="send_message",
                payload_hash="hash-a",
            )

        assert first_created is True
        assert second_created is False
        assert second.transaction_id == first.transaction_id
        assert second.payload_hash == "hash-a"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chat_client_transaction_completion_persists_result(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            row, created = await claim_chat_client_transaction(
                session,
                conversation_id="conv-1",
                principal_id="user@test.com",
                client_txn_id="txn-1",
                operation="send_message",
                payload_hash="hash-a",
            )
            assert created is True
            await complete_chat_client_transaction(
                session,
                row,
                status="accepted",
                result={"status": "accepted", "client_txn_id": "txn-1"},
            )
            await session.commit()

        async with factory() as session:
            row, created = await claim_chat_client_transaction(
                session,
                conversation_id="conv-1",
                principal_id="user@test.com",
                client_txn_id="txn-1",
                operation="send_message",
                payload_hash="hash-a",
            )

        assert created is False
        assert row.status == "accepted"
        assert row.result == {"status": "accepted", "client_txn_id": "txn-1"}
        assert row.completed_at is not None
    finally:
        await engine.dispose()
