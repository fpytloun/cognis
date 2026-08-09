from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import JSON, TIMESTAMP, BigInteger, inspect, select
from sqlalchemy import schema as sa_schema
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.database import create_session_factory
from cognis.store.direct_turns import (
    DirectTurnRecoveryConflict,
    DirectTurnRecoverySnapshot,
    DirectTurnStatus,
    DirectTurnStore,
    conversation_lease_key,
)
from cognis.store.models import AuditLog, Base
from cognis.store.queries import create_agent, create_conversation, create_user

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
async def test_postgres_concurrent_admission_and_fencing() -> None:
    url = _asyncpg_url()
    schema_name = f"cognis_direct_turn_{uuid.uuid4().hex}"
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
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns("direct_turn_requests")
                }
            )
        assert isinstance(columns["admission_order"]["type"], BigInteger)
        assert isinstance(columns["fencing_token"]["type"], BigInteger)
        assert isinstance(columns["payload"]["type"], JSON)
        for timestamp_column in (
            "created_at",
            "updated_at",
            "claimed_at",
            "started_at",
            "terminal_at",
            "cancel_requested_at",
        ):
            column_type = columns[timestamp_column]["type"]
            assert isinstance(column_type, TIMESTAMP)
            assert column_type.timezone is True
        sequence_default = str(columns["admission_order"]["default"])
        assert sequence_default.startswith("nextval(")
        assert {
            name: column["default"] for name, column in columns.items() if name != "admission_order"
        } == {name: None for name in columns if name != "admission_order"}
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
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                conversation_id="conv-a",
            )
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                conversation_id="conv-operator",
            )
            await session.commit()

        store = DirectTurnStore(session_factory)

        async def admit():
            return await store.admit(
                conversation_id="conv-a",
                session_id=None,
                agent_id="agent-1",
                user_id="user@example.com",
                idempotency_scope="web:conv-a:user@example.com",
                idempotency_key="message-1",
                payload={"schema_version": 1, "content": "hello", "attachments": []},
            )

        first, second = await asyncio.gather(admit(), admit())
        assert sum(result.created for result in (first, second)) == 1
        assert first.request.request_id == second.request.request_id

        lease_store = DatabaseLeaseStore(session_factory)
        first_lease = await lease_store.acquire(
            conversation_lease_key("conv-a"),
            "controller-a:boot-a",
            ttl_seconds=60,
        )
        assert first_lease is not None
        claimed = await store.claim(
            first.request.request_id,
            lease=first_lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        assert await lease_store.release(first_lease)

        successor = await lease_store.acquire(
            conversation_lease_key("conv-a"),
            "controller-b:boot-b",
            ttl_seconds=60,
        )
        assert successor is not None
        assert successor.fencing_token > first_lease.fencing_token
        assert (
            await store.mark_running(
                first.request.request_id,
                lease=first_lease,
            )
            is None
        )
        recovered = await store.recover_stale_claim(
            first.request.request_id,
            lease=successor,
        )
        assert recovered is not None
        reclaimed = await store.claim(
            first.request.request_id,
            lease=successor,
            controller_id="controller-b",
            incarnation_id="boot-b",
        )
        assert reclaimed is not None
        cancel_result, running = await asyncio.gather(
            store.request_cancel(first.request.request_id),
            store.mark_running(first.request.request_id, lease=successor),
        )
        assert cancel_result is not None
        assert cancel_result.cancellation_requested is True
        assert running is not None
        stored = await store.get(first.request.request_id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.cancel_requested_at is not None

        operator_turn = await store.admit(
            conversation_id="conv-operator",
            session_id=None,
            agent_id="agent-1",
            user_id="user@example.com",
            idempotency_scope="web:conv-operator:user@example.com",
            idempotency_key="operator-recovery",
            payload={"schema_version": 1, "content": "hello", "attachments": []},
        )
        stale_lease = await lease_store.acquire(
            conversation_lease_key("conv-operator"),
            "controller-a:boot-a",
            ttl_seconds=60,
        )
        assert stale_lease is not None
        claimed = await store.claim(
            operator_turn.request.request_id,
            lease=stale_lease,
            controller_id="controller-a",
            incarnation_id="boot-a",
        )
        assert claimed is not None
        checkpointed = await store.checkpoint(
            operator_turn.request.request_id,
            lease=stale_lease,
            phase="tool_in_flight",
            metadata={"call_id": "call-postgres"},
        )
        assert checkpointed is not None
        expected = DirectTurnRecoverySnapshot(
            conversation_id="conv-operator",
            status=checkpointed.status,
            phase="tool_in_flight",
            owner_controller_id="controller-a",
            owner_incarnation_id="boot-a",
            fencing_token=stale_lease.fencing_token,
            updated_at=checkpointed.updated_at,
            phase_started_at=datetime.fromisoformat(checkpointed.outcome["phase_started_at"]),
        )
        with pytest.raises(DirectTurnRecoveryConflict) as live_conflict:
            await store.resolve_stale_tool_ambiguous(
                operator_turn.request.request_id,
                actor_email="admin@example.com",
                reason="Controller is gone",
                client_transaction_id="txn-postgres",
                expected=expected,
            )
        assert live_conflict.value.code == "lease_live"
        assert await lease_store.release(stale_lease)

        first_recovery, replay = await asyncio.gather(
            store.resolve_stale_tool_ambiguous(
                operator_turn.request.request_id,
                actor_email="admin@example.com",
                reason="Controller is gone",
                client_transaction_id="txn-postgres",
                expected=expected,
            ),
            store.resolve_stale_tool_ambiguous(
                operator_turn.request.request_id,
                actor_email="admin@example.com",
                reason="Controller is gone",
                client_transaction_id="txn-postgres",
                expected=expected,
            ),
        )
        assert {first_recovery.changed, replay.changed} == {False, True}
        assert first_recovery.status == DirectTurnStatus.AMBIGUOUS.value
        assert replay.status == DirectTurnStatus.AMBIGUOUS.value
        assert (
            await store.mark_terminal(
                operator_turn.request.request_id,
                lease=stale_lease,
                status=DirectTurnStatus.COMPLETED,
            )
            is None
        )
        async with session_factory() as session:
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
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()
