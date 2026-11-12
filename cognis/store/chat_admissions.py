"""Content-free reconciliation of accepted Chat v2 message admissions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.store.direct_turns import DirectTurnStatus
from cognis.store.models import ChatClientTransactionRow, DirectTurnRequestRow


@dataclass(frozen=True)
class MessageAdmissionState:
    """An HTTP receipt's durable execution outcome, not canonical message history."""

    client_txn_id: str
    client_message_id: str
    turn_id: str
    status: str
    started: bool
    admission_order: int
    before_client_message_id: str | None = None
    session_id: str | None = None


async def read_message_admission(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    principal_id: str,
    client_txn_id: str | None = None,
    client_message_id: str | None = None,
    canonical_client_message_ids: Sequence[str] = (),
) -> MessageAdmissionState | None:
    """Resolve an owned receipt without loading queued input or Intaris content."""
    if (client_txn_id is None) == (client_message_id is None):
        raise ValueError("Exactly one message admission identity is required")
    txn = ChatClientTransactionRow
    turn = DirectTurnRequestRow
    statement = (
        select(
            turn.turn_id,
            turn.status,
            turn.started_at,
            turn.admission_order,
            turn.session_id,
            txn.client_txn_id,
            txn.result,
        )
        .join(
            txn,
            (txn.client_txn_id == turn.idempotency_key)
            & (txn.conversation_id == turn.conversation_id),
        )
        .where(
            turn.conversation_id == conversation_id,
            turn.user_id == principal_id,
            turn.idempotency_scope == f"chat-v2:{conversation_id}:{principal_id}",
            txn.principal_id == principal_id,
            txn.operation == "send_message",
            txn.status.in_(("accepted", "queued")),
        )
    )
    async with session_factory() as session:
        identity = (
            txn.client_txn_id == client_txn_id
            if client_txn_id is not None
            else txn.result["client_message_id"].as_string() == client_message_id
        )
        rows = (await session.execute(statement.where(identity).limit(2))).all()
        # Legacy device records may lack their transaction ID. An ambiguous
        # client ID cannot establish which request the device is recovering.
        if len(rows) != 1:
            return None
        row = rows[0]
        receipt = row.result or {}
        client_id = receipt.get("client_message_id")
        if not isinstance(client_id, str) or not client_id:
            return None
        before = None
        if row.started_at is None and row.status in {
            DirectTurnStatus.FAILED.value,
            DirectTurnStatus.CANCELLED.value,
        }:
            # Admission order proves this unstarted submission preceded a later
            # admitted input, not a canonical event position. The reader must
            # still prove that successor's canonical client ID.
            successors = statement.where(
                turn.admission_order > row.admission_order,
                turn.started_at.is_not(None),
            )
            if canonical_client_message_ids:
                successors = successors.where(
                    txn.result["client_message_id"].as_string().in_(canonical_client_message_ids)
                )
            successor = (
                await session.execute(successors.order_by(turn.admission_order).limit(1))
            ).one_or_none()
            if successor is not None:
                value = (successor.result or {}).get("client_message_id")
                if isinstance(value, str) and value and value != client_id:
                    before = value
        return MessageAdmissionState(
            client_txn_id=row.client_txn_id,
            client_message_id=client_id,
            turn_id=row.turn_id,
            status=row.status,
            started=row.started_at is not None,
            admission_order=row.admission_order,
            before_client_message_id=before,
            session_id=row.session_id,
        )
