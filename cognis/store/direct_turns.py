"""Durable direct-turn admission and fenced state transitions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, exists, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import ChannelDeliveryDescriptor
from cognis.models.retry import RetryReason
from cognis.store.coordination import Lease, database_now, database_now_expression
from cognis.store.models import (
    ArtifactRecordRow,
    AuditLog,
    ChannelDeliveryOutboxRow,
    CoordinationLeaseRow,
    DirectTurnRequestRow,
)


class DirectTurnStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    ABSORBING = "absorbing"
    RECOVERABLE = "recoverable"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABSORBED = "absorbed"
    AMBIGUOUS = "ambiguous"


CLAIMABLE_STATUSES = frozenset({DirectTurnStatus.QUEUED, DirectTurnStatus.RECOVERABLE})
ACTIVE_STATUSES = frozenset(
    {DirectTurnStatus.CLAIMED, DirectTurnStatus.RUNNING, DirectTurnStatus.ABSORBING}
)
TERMINAL_STATUSES = frozenset(
    {
        DirectTurnStatus.COMPLETED,
        DirectTurnStatus.FAILED,
        DirectTurnStatus.CANCELLED,
        DirectTurnStatus.ABSORBED,
        DirectTurnStatus.AMBIGUOUS,
    }
)
NONTERMINAL_STATUSES = CLAIMABLE_STATUSES | ACTIVE_STATUSES
OPERATOR_RECOVERY_LEASE_SECONDS = 30


def _outcome_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    normalized_left = left if left.tzinfo is not None else left.replace(tzinfo=UTC)
    normalized_right = right if right.tzinfo is not None else right.replace(tzinfo=UTC)
    return normalized_left.astimezone(UTC) == normalized_right.astimezone(UTC)


class DirectTurnRecoveryConflict(RuntimeError):
    """The requested operator recovery does not match recoverable state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DirectTurnRecoverySnapshot:
    conversation_id: str
    status: str
    phase: str
    owner_controller_id: str
    owner_incarnation_id: str
    fencing_token: int
    updated_at: datetime
    phase_started_at: datetime | None = None


@dataclass(frozen=True)
class DirectTurnRecoveryResult:
    request_id: str
    conversation_id: str
    status: str
    phase: str
    fencing_token: int | None
    changed: bool


class DurableAttachmentRefV1(BaseModel):  # type: ignore[misc]
    """Stable attachment identity and metadata; signed URLs are never durable."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: ArtifactKind
    mime_type: str
    filename: str
    size_bytes: int
    url: str | None = Field(default=None, exclude=True)


class DirectTurnPayloadV1(BaseModel):  # type: ignore[misc]
    """Strict durable envelope for reproducing one admitted direct message."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    content: str
    attachments: list[DurableAttachmentRefV1] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    channel_delivery: ChannelDeliveryDescriptor | None = None
    retry_reason: RetryReason | None = None

    @field_validator("retry_reason", mode="before")
    @classmethod
    def _normalize_retry_reason(cls, value: Any) -> RetryReason | None:
        if value is None:
            return None
        try:
            return RetryReason(value)
        except (TypeError, ValueError):
            return RetryReason.TRANSIENT_RUNTIME


class ArtifactURLResolver(Protocol):
    @property
    def signed_url_ttl_seconds(self) -> int: ...

    async def async_get_public_url(
        self,
        namespace: str,
        object_id: str,
        filename: str,
        *,
        ttl_seconds: int | None = None,
        mode: str = "download",
        expires_at: datetime | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class _MaterializedArtifact:
    artifact_id: str
    namespace: str
    object_id: str
    filename: str
    kind: ArtifactKind
    mime_type: str
    size_bytes: int
    expires_at: datetime | None
    url_ttl_seconds: int


class DirectTurnConflictError(RuntimeError):
    """An idempotency or stable-ID key was reused for a different request."""


class DirectTurnAdmissionRejected(RuntimeError):
    """A transaction-scoped admission guard rejected a new durable request."""


class DirectTurnAdmissionGuard(Protocol):
    async def __call__(self, session: AsyncSession) -> bool: ...


class PermanentDirectTurnPayloadError(RuntimeError):
    """Claimed payload cannot become executable through retry."""


@dataclass(frozen=True)
class AdmissionResult:
    request: DirectTurnRequestRow
    created: bool


@dataclass(frozen=True)
class CancelResult:
    request: DirectTurnRequestRow
    cancellation_requested: bool


@dataclass(frozen=True)
class MaterializedDirectTurnPayload:
    schema_version: Literal[1]
    content: str
    attachments: list[AttachmentRef]
    metadata: dict[str, Any]
    channel_delivery: ChannelDeliveryDescriptor | None = None


def conversation_lease_key(conversation_id: str) -> str:
    return f"direct-turn:conversation:{conversation_id}"


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _owner_id(controller_id: str, incarnation_id: str) -> str:
    return f"{controller_id}:{incarnation_id}"


def _validated_payload(payload: dict[str, Any], payload_version: int) -> dict[str, Any]:
    if payload_version != 1:
        raise ValueError(f"unsupported direct-turn payload version: {payload_version}")
    if payload.get("schema_version") != payload_version:
        raise ValueError("payload schema_version does not match payload_version")
    validated = DirectTurnPayloadV1.model_validate(payload)
    normalized = cast(dict[str, Any], validated.model_dump(mode="json"))
    if "retry_reason" not in payload:
        normalized.pop("retry_reason", None)
    return normalized


class DirectTurnStore:
    """Database authority for accepted direct-turn requests."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def admit(
        self,
        *,
        conversation_id: str,
        session_id: str | None,
        agent_id: str,
        user_id: str,
        idempotency_scope: str,
        idempotency_key: str,
        payload: dict[str, Any],
        payload_version: int = 1,
        request_id: str | None = None,
        turn_id: str | None = None,
        admission_guard: DirectTurnAdmissionGuard | None = None,
        transaction_participant: (
            Callable[[AsyncSession, DirectTurnRequestRow, bool], Awaitable[None]] | None
        ) = None,
    ) -> AdmissionResult:
        """Insert one immutable admission or replay the matching existing row."""
        payload = _validated_payload(payload, payload_version)
        request_id = request_id or f"dtr_{uuid.uuid4().hex}"
        turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        descriptor = {
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "payload_version": payload_version,
            "payload": payload,
        }
        admission_hash = _canonical_hash(descriptor)
        payload_hash = _canonical_hash(payload)

        values = {
            "request_id": request_id,
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "idempotency_scope": idempotency_scope,
            "idempotency_key": idempotency_key,
            "admission_hash": admission_hash,
            "payload_hash": payload_hash,
            "payload_version": payload_version,
            "payload": payload,
            "status": DirectTurnStatus.QUEUED.value,
            "attempt_count": 0,
        }
        for attempt in range(3):
            try:
                return await self._admit_once(
                    values=values,
                    idempotency_scope=idempotency_scope,
                    idempotency_key=idempotency_key,
                    admission_hash=admission_hash,
                    admission_guard=admission_guard,
                    transaction_participant=transaction_participant,
                )
            except OperationalError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("direct-turn admission retry loop exhausted")

    async def _admit_once(
        self,
        *,
        values: dict[str, Any],
        idempotency_scope: str,
        idempotency_key: str,
        admission_hash: str,
        admission_guard: DirectTurnAdmissionGuard | None,
        transaction_participant: (
            Callable[[AsyncSession, DirectTurnRequestRow, bool], Awaitable[None]] | None
        ),
    ) -> AdmissionResult:
        async with self._session_factory() as session:
            if admission_guard is not None and not await admission_guard(session):
                await session.rollback()
                raise DirectTurnAdmissionRejected("durable admission fence changed")
            now = await database_now(session)
            insert_values = {**values, "created_at": now, "updated_at": now}
            dialect = session.bind.dialect.name if session.bind is not None else ""
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            statement = (
                insert(DirectTurnRequestRow)
                .values(**insert_values)
                .on_conflict_do_nothing()
                .returning(DirectTurnRequestRow)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            created = row is not None
            if row is None:
                row = (
                    await session.execute(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.idempotency_scope == idempotency_scope,
                            DirectTurnRequestRow.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    await session.rollback()
                    raise DirectTurnConflictError("request_id or turn_id already exists")
                if row.admission_hash != admission_hash:
                    await session.rollback()
                    raise DirectTurnConflictError(
                        "idempotency key was reused with a different request"
                    )
            if transaction_participant is not None:
                await transaction_participant(session, row, created)
            await session.commit()
            return AdmissionResult(request=row, created=created)

    async def get(self, request_id: str) -> DirectTurnRequestRow | None:
        async with self._session_factory() as session:
            return cast(
                DirectTurnRequestRow | None,
                (
                    await session.execute(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.request_id == request_id
                        )
                    )
                ).scalar_one_or_none(),
            )

    async def list_claimable_heads(self, *, limit: int = 100) -> list[DirectTurnRequestRow]:
        """Return FIFO conversation heads that are eligible for ownership."""
        earlier = aliased(DirectTurnRequestRow)
        async with self._session_factory() as session:
            now = await database_now(session)
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(
                    DirectTurnRequestRow.status.in_(
                        [status.value for status in CLAIMABLE_STATUSES]
                    ),
                    or_(
                        DirectTurnRequestRow.next_attempt_at.is_(None),
                        DirectTurnRequestRow.next_attempt_at <= now,
                    ),
                    ~exists(
                        select(1).where(
                            earlier.conversation_id == DirectTurnRequestRow.conversation_id,
                            earlier.admission_order < DirectTurnRequestRow.admission_order,
                            earlier.status.in_([status.value for status in NONTERMINAL_STATUSES]),
                        )
                    ),
                )
                .order_by(DirectTurnRequestRow.admission_order)
                .limit(max(1, limit))
            )
            return list(result.scalars().all())

    async def list_conversation_pending(self, conversation_id: str) -> list[DirectTurnRequestRow]:
        """Return authoritative nonterminal requests in FIFO order."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(
                    DirectTurnRequestRow.conversation_id == conversation_id,
                    DirectTurnRequestRow.status.in_(
                        [status.value for status in NONTERMINAL_STATUSES]
                    ),
                )
                .order_by(DirectTurnRequestRow.admission_order)
            )
            return list(result.scalars().all())

    async def get_conversation_active(self, conversation_id: str) -> DirectTurnRequestRow | None:
        """Return the authoritative active durable turn for a conversation."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(
                    DirectTurnRequestRow.conversation_id == conversation_id,
                    DirectTurnRequestRow.status.in_([status.value for status in ACTIVE_STATUSES]),
                )
                .order_by(DirectTurnRequestRow.admission_order)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def list_conversations_active(
        self, conversation_ids: list[str]
    ) -> dict[str, DirectTurnRequestRow]:
        """Return one authoritative active durable turn per requested conversation."""

        if not conversation_ids:
            return {}
        async with self._session_factory() as session:
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(
                    DirectTurnRequestRow.conversation_id.in_(conversation_ids),
                    DirectTurnRequestRow.status.in_([status.value for status in ACTIVE_STATUSES]),
                )
                .order_by(
                    DirectTurnRequestRow.conversation_id, DirectTurnRequestRow.admission_order
                )
            )
            rows: dict[str, DirectTurnRequestRow] = {}
            for row in result.scalars():
                rows.setdefault(row.conversation_id, row)
            return rows

    async def list_stale_active(self, *, limit: int = 100) -> list[DirectTurnRequestRow]:
        """Return active rows whose exact lease tuple is no longer current."""
        async with self._session_factory() as session:
            current_lease = exists(
                select(1).where(
                    CoordinationLeaseRow.resource_key
                    == ("direct-turn:conversation:" + DirectTurnRequestRow.conversation_id),
                    CoordinationLeaseRow.owner_id
                    == (
                        DirectTurnRequestRow.owner_controller_id
                        + ":"
                        + DirectTurnRequestRow.owner_incarnation_id
                    ),
                    CoordinationLeaseRow.fencing_token == DirectTurnRequestRow.fencing_token,
                    CoordinationLeaseRow.lease_expires_at > database_now_expression(session),
                )
            )
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(
                    DirectTurnRequestRow.status.in_([status.value for status in ACTIVE_STATUSES]),
                    ~current_lease,
                )
                .order_by(DirectTurnRequestRow.admission_order)
                .limit(max(1, limit))
            )
            return list(result.scalars().all())

    async def list_stale_active_page(
        self,
        *,
        after_admission_order: int = 0,
        limit: int = 100,
    ) -> tuple[list[DirectTurnRequestRow], bool]:
        """Return a bounded operational page without loading durable payloads."""
        bounded_limit = min(max(1, limit), 100)
        async with self._session_factory() as session:
            current_lease = exists(
                select(1).where(
                    CoordinationLeaseRow.resource_key
                    == ("direct-turn:conversation:" + DirectTurnRequestRow.conversation_id),
                    CoordinationLeaseRow.owner_id
                    == (
                        DirectTurnRequestRow.owner_controller_id
                        + ":"
                        + DirectTurnRequestRow.owner_incarnation_id
                    ),
                    CoordinationLeaseRow.fencing_token == DirectTurnRequestRow.fencing_token,
                    CoordinationLeaseRow.lease_expires_at > database_now_expression(session),
                )
            )
            rows = list(
                (
                    await session.execute(
                        select(DirectTurnRequestRow)
                        .where(
                            DirectTurnRequestRow.admission_order > after_admission_order,
                            DirectTurnRequestRow.status.in_(
                                [status.value for status in ACTIVE_STATUSES]
                            ),
                            ~current_lease,
                        )
                        .order_by(DirectTurnRequestRow.admission_order)
                        .limit(bounded_limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            return rows[:bounded_limit], len(rows) > bounded_limit

    async def resolve_stale_tool_ambiguous(
        self,
        request_id: str,
        *,
        actor_email: str,
        reason: str,
        client_transaction_id: str,
        expected: DirectTurnRecoverySnapshot,
    ) -> DirectTurnRecoveryResult:
        """Fence and quarantine one stale tool effect with its audit atomically."""
        from cognis.store.coordination import DatabaseLeaseStore

        audit_digest = hashlib.sha256(
            f"{actor_email}\0{request_id}\0{client_transaction_id}".encode()
        ).hexdigest()[:24]
        audit_id = f"audit_{audit_digest}"
        recovery_owner = f"operator-recovery:{audit_digest}"
        async with self._session_factory() as session:
            existing_audit = await session.get(AuditLog, audit_id)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request is None:
                raise DirectTurnRecoveryConflict("not_found", "Direct turn not found")
            outcome = request.outcome if isinstance(request.outcome, dict) else {}
            if request.status == DirectTurnStatus.AMBIGUOUS.value:
                if outcome.get("operator_recovery") is True:
                    if (
                        outcome.get("operator_recovery_transaction_id") != client_transaction_id
                        or outcome.get("operator_recovery_actor") != actor_email
                    ):
                        raise DirectTurnRecoveryConflict(
                            "idempotency_conflict",
                            "Recovery transaction does not match the completed operation",
                        )
                    return DirectTurnRecoveryResult(
                        request_id=request.request_id,
                        conversation_id=request.conversation_id,
                        status=request.status,
                        phase=str(outcome.get("phase") or "ambiguous"),
                        fencing_token=request.fencing_token,
                        changed=False,
                    )
                raise DirectTurnRecoveryConflict(
                    "invalid_status", "Direct turn is already terminal"
                )
            if existing_audit is not None:
                raise DirectTurnRecoveryConflict(
                    "idempotency_conflict", "Recovery transaction does not match state"
                )
            current_phase = str(outcome.get("phase") or "")
            phase_started_at = _outcome_datetime(outcome.get("phase_started_at"))
            snapshot_matches = (
                request.conversation_id == expected.conversation_id
                and request.status == expected.status
                and current_phase == expected.phase
                and request.owner_controller_id == expected.owner_controller_id
                and request.owner_incarnation_id == expected.owner_incarnation_id
                and request.fencing_token == expected.fencing_token
                and _same_datetime(request.updated_at, expected.updated_at)
                and (
                    expected.phase_started_at is None
                    or _same_datetime(phase_started_at, expected.phase_started_at)
                )
            )
            if not snapshot_matches:
                raise DirectTurnRecoveryConflict(
                    "snapshot_mismatch", "Direct turn changed since it was inspected"
                )
            if (
                request.status not in {status.value for status in ACTIVE_STATUSES}
                or current_phase != "tool_in_flight"
            ):
                raise DirectTurnRecoveryConflict(
                    "invalid_phase", "Only stale tool-in-flight turns may be resolved"
                )
            resource_key = conversation_lease_key(request.conversation_id)
            now = await database_now(session)
            lease_store = DatabaseLeaseStore(self._session_factory)
            recovery_lease = await lease_store.acquire_in_session(
                session,
                resource_key,
                recovery_owner,
                ttl_seconds=OPERATOR_RECOVERY_LEASE_SECONDS,
            )
            if recovery_lease is None:
                raise DirectTurnRecoveryConflict(
                    "lease_live", "The conversation lease is still live"
                )
            previous = {
                "status": request.status,
                "owner_controller_id": request.owner_controller_id,
                "owner_incarnation_id": request.owner_incarnation_id,
                "fencing_token": request.fencing_token,
                "phase": current_phase,
                "phase_started_at": (
                    phase_started_at.isoformat() if phase_started_at is not None else None
                ),
                "updated_at": request.updated_at.isoformat(),
            }
            new_outcome = {
                "phase": "ambiguous",
                "operator_recovery": True,
                "operator_recovery_reason": reason,
                "operator_recovery_transaction_id": client_transaction_id,
                "operator_recovery_actor": actor_email,
                "call_id": outcome.get("call_id"),
                "call_ids": outcome.get("call_ids"),
                "resolved_at": now.isoformat(),
            }
            updated = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == expected.status,
                        DirectTurnRequestRow.owner_controller_id == expected.owner_controller_id,
                        DirectTurnRequestRow.owner_incarnation_id == expected.owner_incarnation_id,
                        DirectTurnRequestRow.fencing_token == expected.fencing_token,
                        DirectTurnRequestRow.updated_at == expected.updated_at,
                    )
                    .values(
                        status=DirectTurnStatus.AMBIGUOUS.value,
                        owner_controller_id="operator-recovery",
                        owner_incarnation_id=audit_digest,
                        fencing_token=recovery_lease.fencing_token,
                        outcome=new_outcome,
                        terminal_at=now,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            if updated is None:
                raise DirectTurnRecoveryConflict(
                    "snapshot_mismatch", "Direct turn changed during recovery"
                )
            await session.execute(
                update(CoordinationLeaseRow)
                .where(
                    CoordinationLeaseRow.resource_key == resource_key,
                    CoordinationLeaseRow.owner_id == recovery_lease.owner_id,
                    CoordinationLeaseRow.fencing_token == recovery_lease.fencing_token,
                )
                .values(lease_expires_at=now, updated_at=now)
            )
            session.add(
                AuditLog(
                    log_id=audit_id,
                    event_type="direct_turn_operator_recovery",
                    user_email=actor_email,
                    agent_id=None,
                    details={
                        "actor": actor_email,
                        "reason": reason,
                        "request_id": request_id,
                        "conversation_id": request.conversation_id,
                        "client_transaction_id": client_transaction_id,
                        "previous": previous,
                        "new": {
                            "status": DirectTurnStatus.AMBIGUOUS.value,
                            "owner_id": recovery_lease.owner_id,
                            "fencing_token": recovery_lease.fencing_token,
                            "phase": "ambiguous",
                        },
                    },
                )
            )
            await session.commit()
            return DirectTurnRecoveryResult(
                request_id=request_id,
                conversation_id=request.conversation_id,
                status=DirectTurnStatus.AMBIGUOUS.value,
                phase="ambiguous",
                fencing_token=recovery_lease.fencing_token,
                changed=True,
            )

    async def list_pending_failure_visibility(
        self, *, limit: int = 100
    ) -> list[DirectTurnRequestRow]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(DirectTurnRequestRow)
                .where(DirectTurnRequestRow.status == DirectTurnStatus.FAILED.value)
                .order_by(DirectTurnRequestRow.admission_order)
                .limit(max(1, limit))
            )
            return [
                row
                for row in result.scalars().all()
                if isinstance(row.outcome, dict)
                and row.outcome.get("phase") == "permanent_payload_visibility_pending"
            ]

    async def complete_failure_visibility(self, request_id: str) -> DirectTurnRequestRow | None:
        async with self._session_factory() as session:
            now = await database_now(session)
            current = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                current is None
                or current.status != DirectTurnStatus.FAILED.value
                or not isinstance(current.outcome, dict)
                or current.outcome.get("phase") != "permanent_payload_visibility_pending"
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.FAILED.value,
                        DirectTurnRequestRow.updated_at == current.updated_at,
                    )
                    .values(
                        outcome={"phase": "permanent_payload_visibility_complete"},
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def edit(
        self,
        request_id: str,
        *,
        payload: dict[str, Any],
        payload_version: int,
        expected_payload_hash: str | None = None,
    ) -> DirectTurnRequestRow | None:
        """Replace payload only while the request remains queued."""
        payload = _validated_payload(payload, payload_version)
        async with self._session_factory() as session:
            now = await database_now(session)
            predicates = [
                DirectTurnRequestRow.request_id == request_id,
                DirectTurnRequestRow.status == DirectTurnStatus.QUEUED.value,
            ]
            if expected_payload_hash is not None:
                predicates.append(DirectTurnRequestRow.payload_hash == expected_payload_hash)
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(*predicates)
                    .values(
                        payload=payload,
                        payload_hash=_canonical_hash(payload),
                        payload_version=payload_version,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return cast(DirectTurnRequestRow | None, row)

    async def request_cancel(self, request_id: str) -> CancelResult | None:
        """Atomically cancel idle work or durably flag active work."""
        async with self._session_factory() as session:
            now = await database_now(session)
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status.in_(
                            [status.value for status in NONTERMINAL_STATUSES]
                        ),
                    )
                    .values(
                        status=case(
                            (
                                DirectTurnRequestRow.status.in_(
                                    [status.value for status in CLAIMABLE_STATUSES]
                                ),
                                DirectTurnStatus.CANCELLED.value,
                            ),
                            else_=DirectTurnRequestRow.status,
                        ),
                        cancel_requested_at=now,
                        terminal_at=case(
                            (
                                DirectTurnRequestRow.status.in_(
                                    [status.value for status in CLAIMABLE_STATUSES]
                                ),
                                now,
                            ),
                            else_=DirectTurnRequestRow.terminal_at,
                        ),
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            if row is None:
                row = (
                    await session.execute(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.request_id == request_id
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
            await session.commit()
            return CancelResult(
                request=row,
                cancellation_requested=(
                    DirectTurnStatus(row.status) in ACTIVE_STATUSES
                    and row.cancel_requested_at is not None
                ),
            )

    async def checkpoint(
        self,
        request_id: str,
        *,
        lease: Lease,
        phase: str,
        metadata: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        """Persist the latest execution boundary under the exact fence."""
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=ACTIVE_STATUSES,
                values={
                    "outcome": {
                        "phase": phase,
                        "phase_started_at": now.isoformat(),
                        **(metadata or {}),
                    },
                    "updated_at": now,
                },
            )
            await session.commit()
            return row

    async def materialize_claimed_payload(
        self,
        request_id: str,
        *,
        lease: Lease,
        artifact_store: ArtifactURLResolver,
    ) -> MaterializedDirectTurnPayload | None:
        """Resolve fresh attachment URLs for a request still owned by this fence."""
        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status.in_(
                            [status.value for status in ACTIVE_STATUSES]
                        ),
                        DirectTurnRequestRow.fencing_token == lease.fencing_token,
                        self._lease_predicate(session, lease),
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.owner_controller_id is None
                or request.owner_incarnation_id is None
                or lease.owner_id
                != _owner_id(request.owner_controller_id, request.owner_incarnation_id)
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            payload = DirectTurnPayloadV1.model_validate(request.payload)
            artifact_ids = [attachment.artifact_id for attachment in payload.attachments]
            records = (
                (
                    await session.execute(
                        select(ArtifactRecordRow).where(
                            ArtifactRecordRow.artifact_id.in_(artifact_ids)
                        )
                    )
                )
                .scalars()
                .all()
                if artifact_ids
                else []
            )
            records_by_id = {record.artifact_id: record for record in records}
            prepared: list[_MaterializedArtifact] = []
            from cognis.core.artifact_access import artifact_authorized_for_conversation

            now: datetime | None = None
            for durable in payload.attachments:
                record = records_by_id.get(durable.artifact_id)
                if record is None or record.status == "deleted" or record.deleted_at is not None:
                    raise PermanentDirectTurnPayloadError(
                        f"Attachment is unavailable: {durable.artifact_id}"
                    )
                if not await artifact_authorized_for_conversation(
                    session,
                    artifact=record,
                    owner_email=request.user_id,
                    conversation_id=request.conversation_id,
                    agent_id=request.agent_id,
                ):
                    raise PermanentDirectTurnPayloadError(
                        f"Attachment access denied: {durable.artifact_id}"
                    )
                expires_at = record.expires_at
                if expires_at is not None:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                    if now is None:
                        now = await database_now(session)
                    database_remaining = int((expires_at - now).total_seconds())
                    if database_remaining < 1:
                        raise PermanentDirectTurnPayloadError(
                            f"Attachment is expired: {durable.artifact_id}"
                        )
                    url_ttl_seconds = min(
                        artifact_store.signed_url_ttl_seconds,
                        database_remaining,
                    )
                else:
                    url_ttl_seconds = artifact_store.signed_url_ttl_seconds
                prepared.append(
                    _MaterializedArtifact(
                        artifact_id=record.artifact_id,
                        namespace=record.namespace,
                        object_id=record.object_id,
                        filename=record.filename,
                        kind=ArtifactKind(record.kind),
                        mime_type=record.mime_type,
                        size_bytes=record.size_bytes,
                        expires_at=expires_at,
                        url_ttl_seconds=url_ttl_seconds,
                    )
                )
        materialized: list[AttachmentRef] = []
        for artifact in prepared:
            try:
                url = await artifact_store.async_get_public_url(
                    artifact.namespace,
                    artifact.object_id,
                    artifact.filename,
                    ttl_seconds=artifact.url_ttl_seconds,
                    expires_at=artifact.expires_at,
                )
            except ValueError as exc:
                if "expired" not in str(exc).lower():
                    raise
                raise PermanentDirectTurnPayloadError(
                    f"Attachment is expired: {artifact.artifact_id}"
                ) from exc
            materialized.append(
                AttachmentRef(
                    artifact_id=artifact.artifact_id,
                    kind=artifact.kind,
                    mime_type=artifact.mime_type,
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                    url=url,
                )
            )
        if not await self.has_fence(request_id, lease=lease):
            return None
        return MaterializedDirectTurnPayload(
            schema_version=1,
            content=payload.content,
            attachments=materialized,
            metadata=payload.metadata,
            channel_delivery=payload.channel_delivery,
        )

    async def claim(
        self,
        request_id: str,
        *,
        lease: Lease,
        controller_id: str,
        incarnation_id: str,
        session_id: str | None = None,
    ) -> DirectTurnRequestRow | None:
        """Claim eligible work only while the exact conversation lease is current."""
        if lease.owner_id != _owner_id(controller_id, incarnation_id):
            raise ValueError("lease owner does not match controller incarnation")
        async with self._session_factory() as session:
            now = await database_now(session)
            values: dict[str, Any] = {
                "status": DirectTurnStatus.CLAIMED.value,
                "owner_controller_id": controller_id,
                "owner_incarnation_id": incarnation_id,
                "fencing_token": lease.fencing_token,
                "attempt_count": DirectTurnRequestRow.attempt_count + 1,
                "claimed_at": now,
                "started_at": None,
                "terminal_at": None,
                "next_attempt_at": None,
                "updated_at": now,
            }
            if session_id is not None:
                values["session_id"] = session_id
            row = await self._fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=CLAIMABLE_STATUSES,
                values=values,
            )
            await session.commit()
            return row

    async def recover_stale_claim(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        """Requeue a pre-effect claim after a successor acquires a newer fence."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or lease.resource_key != conversation_lease_key(request.conversation_id)
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.CLAIMED.value,
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.RECOVERABLE.value,
                        owner_controller_id=None,
                        owner_incarnation_id=None,
                        fencing_token=None,
                        outcome=outcome,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return cast(DirectTurnRequestRow | None, row)

    async def mark_stale_ambiguous(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any],
    ) -> DirectTurnRequestRow | None:
        """Terminally quarantine effecting work owned by an older fence."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or lease.resource_key != conversation_lease_key(request.conversation_id)
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status.in_(
                            [
                                DirectTurnStatus.RUNNING.value,
                                DirectTurnStatus.ABSORBING.value,
                            ]
                        ),
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.AMBIGUOUS.value,
                        outcome=outcome,
                        terminal_at=now,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def reconcile_stale_absorbed(
        self,
        request_id: str,
        *,
        lease: Lease,
    ) -> DirectTurnRequestRow | None:
        """Finalize an absorbed request whose canonical event was reconciled."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status != DirectTurnStatus.ABSORBING.value
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.ABSORBING.value,
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.ABSORBED.value,
                        outcome={"phase": "reconciled_absorbed"},
                        terminal_at=now,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def reconcile_stale_completed(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any],
    ) -> DirectTurnRequestRow | None:
        """Settle stale running work whose canonical assistant output exists."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status != DirectTurnStatus.RUNNING.value
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.RUNNING.value,
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.COMPLETED.value,
                        outcome=outcome,
                        terminal_at=now,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def cancel_stale_active(
        self,
        request_id: str,
        *,
        lease: Lease,
    ) -> DirectTurnRequestRow | None:
        """Finalize requested cancellation after acquiring a newer conversation fence."""

        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status not in {status.value for status in ACTIVE_STATUSES}
                or request.cancel_requested_at is None
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status.in_(
                            [status.value for status in ACTIVE_STATUSES]
                        ),
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_not(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.CANCELLED.value,
                        owner_controller_id=None,
                        owner_incarnation_id=None,
                        fencing_token=None,
                        outcome={
                            "phase": "cancelled",
                            "reason": "stale owner cancellation recovered",
                        },
                        terminal_at=now,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return cast(DirectTurnRequestRow | None, row)

    async def recover_stale_absorbing(
        self,
        request_id: str,
        *,
        lease: Lease,
    ) -> DirectTurnRequestRow | None:
        """Return an unreconciled absorbed append to the durable FIFO."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status != DirectTurnStatus.ABSORBING.value
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.ABSORBING.value,
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.RECOVERABLE.value,
                        owner_controller_id=None,
                        owner_incarnation_id=None,
                        fencing_token=None,
                        absorbed_by_turn_id=None,
                        outcome={"phase": "recovered_uncommitted_absorb"},
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def recover_stale_running(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any],
    ) -> DirectTurnRequestRow | None:
        """Requeue stale running work only after runtime classified it retry-safe."""
        async with self._session_factory() as session:
            now = await database_now(session)
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status != DirectTurnStatus.RUNNING.value
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return None
            row = (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.RUNNING.value,
                        DirectTurnRequestRow.fencing_token == request.fencing_token,
                        DirectTurnRequestRow.cancel_requested_at.is_(None),
                        self._lease_predicate(session, lease),
                    )
                    .values(
                        status=DirectTurnStatus.RECOVERABLE.value,
                        owner_controller_id=None,
                        owner_incarnation_id=None,
                        fencing_token=None,
                        outcome=outcome,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

    async def mark_running(self, request_id: str, *, lease: Lease) -> DirectTurnRequestRow | None:
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses={DirectTurnStatus.CLAIMED},
                values={
                    "status": DirectTurnStatus.RUNNING.value,
                    "started_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()
            return row

    async def begin_absorb(
        self,
        request_id: str,
        *,
        lease: Lease,
        controller_id: str,
        incarnation_id: str,
        absorbed_by_turn_id: str,
        session_id: str | None = None,
    ) -> DirectTurnRequestRow | None:
        if lease.owner_id != _owner_id(controller_id, incarnation_id):
            raise ValueError("lease owner does not match controller incarnation")
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses={DirectTurnStatus.QUEUED},
                values={
                    "status": DirectTurnStatus.ABSORBING.value,
                    "owner_controller_id": controller_id,
                    "owner_incarnation_id": incarnation_id,
                    "fencing_token": lease.fencing_token,
                    "attempt_count": DirectTurnRequestRow.attempt_count + 1,
                    "absorbed_by_turn_id": absorbed_by_turn_id,
                    "outcome": {
                        "phase": "canonical_user_append",
                        "phase_started_at": now.isoformat(),
                        "session_id": session_id,
                    },
                    "claimed_at": now,
                    "updated_at": now,
                },
                enforce_fifo=False,
            )
            await session.commit()
            return row

    async def mark_absorbed(self, request_id: str, *, lease: Lease) -> DirectTurnRequestRow | None:
        return await self._mark_terminal(
            request_id,
            lease=lease,
            from_statuses={DirectTurnStatus.ABSORBING},
            terminal_status=DirectTurnStatus.ABSORBED,
            outcome=None,
        )

    async def mark_recoverable(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=ACTIVE_STATUSES,
                values={
                    "status": DirectTurnStatus.RECOVERABLE.value,
                    "owner_controller_id": None,
                    "owner_incarnation_id": None,
                    "fencing_token": None,
                    "outcome": outcome,
                    "updated_at": now,
                },
            )
            await session.commit()
            return row

    async def settle_transient_failure(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any],
        retry_after_seconds: float | None = None,
    ) -> DirectTurnRequestRow | None:
        """Recover only when no concurrent cancellation is already pending."""
        async with self._session_factory() as session:
            now = await database_now(session)
            next_attempt_at = (
                now + timedelta(seconds=retry_after_seconds)
                if retry_after_seconds is not None
                else None
            )
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=ACTIVE_STATUSES,
                values={
                    "status": DirectTurnStatus.RECOVERABLE.value,
                    "owner_controller_id": None,
                    "owner_incarnation_id": None,
                    "fencing_token": None,
                    "outcome": outcome,
                    "next_attempt_at": next_attempt_at,
                    "terminal_at": None,
                    "updated_at": now,
                },
                predicates=(DirectTurnRequestRow.cancel_requested_at.is_(None),),
            )
            if row is None:
                row = (
                    await session.execute(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.request_id == request_id,
                            DirectTurnRequestRow.status.in_(
                                [status.value for status in ACTIVE_STATUSES]
                            ),
                            DirectTurnRequestRow.owner_controller_id.is_not(None),
                            DirectTurnRequestRow.owner_incarnation_id.is_not(None),
                            (
                                DirectTurnRequestRow.owner_controller_id
                                + literal(":")
                                + DirectTurnRequestRow.owner_incarnation_id
                                == lease.owner_id
                            ),
                            DirectTurnRequestRow.fencing_token == lease.fencing_token,
                            DirectTurnRequestRow.cancel_requested_at.is_not(None),
                            self._lease_predicate(session, lease),
                        )
                    )
                ).scalar_one_or_none()
            await session.commit()
            return row

    async def mark_terminal(
        self,
        request_id: str,
        *,
        lease: Lease,
        status: DirectTurnStatus,
        outcome: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        if status not in {
            DirectTurnStatus.COMPLETED,
            DirectTurnStatus.FAILED,
            DirectTurnStatus.CANCELLED,
            DirectTurnStatus.AMBIGUOUS,
        }:
            raise ValueError(f"unsupported terminal status: {status}")
        return await self._mark_terminal(
            request_id,
            lease=lease,
            from_statuses=ACTIVE_STATUSES,
            terminal_status=status,
            outcome=outcome,
        )

    async def has_fence(self, request_id: str, *, lease: Lease) -> bool:
        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.owner_controller_id is None
                or request.owner_incarnation_id is None
                or lease.owner_id
                != _owner_id(request.owner_controller_id, request.owner_incarnation_id)
                or lease.resource_key != conversation_lease_key(request.conversation_id)
            ):
                return False
            statement = select(
                exists(
                    select(1).where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.owner_controller_id == request.owner_controller_id,
                        DirectTurnRequestRow.owner_incarnation_id == request.owner_incarnation_id,
                        DirectTurnRequestRow.fencing_token == lease.fencing_token,
                        DirectTurnRequestRow.status.in_(
                            [status.value for status in ACTIVE_STATUSES]
                        ),
                        self._lease_predicate(session, lease),
                    )
                )
            )
            return bool((await session.execute(statement)).scalar_one())

    async def _mark_terminal(
        self,
        request_id: str,
        *,
        lease: Lease,
        from_statuses: set[DirectTurnStatus] | frozenset[DirectTurnStatus],
        terminal_status: DirectTurnStatus,
        outcome: dict[str, Any] | None,
    ) -> DirectTurnRequestRow | None:
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=from_statuses,
                values={
                    "status": terminal_status.value,
                    "outcome": outcome,
                    "terminal_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()
            return row

    async def _fenced_update(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        lease: Lease,
        statuses: set[DirectTurnStatus] | frozenset[DirectTurnStatus],
        values: dict[str, Any],
        enforce_fifo: bool = True,
    ) -> DirectTurnRequestRow | None:
        request = (
            await session.execute(
                select(DirectTurnRequestRow).where(DirectTurnRequestRow.request_id == request_id)
            )
        ).scalar_one_or_none()
        if request is None or lease.resource_key != conversation_lease_key(request.conversation_id):
            return None
        earlier = aliased(DirectTurnRequestRow)
        predicates: list[Any] = [
            DirectTurnRequestRow.request_id == request_id,
            DirectTurnRequestRow.status.in_([status.value for status in statuses]),
            self._lease_predicate(session, lease),
        ]
        if enforce_fifo:
            predicates.append(
                ~exists(
                    select(1).where(
                        earlier.conversation_id == DirectTurnRequestRow.conversation_id,
                        earlier.admission_order < DirectTurnRequestRow.admission_order,
                        earlier.status.in_([status.value for status in NONTERMINAL_STATUSES]),
                    )
                )
            )
        return cast(
            DirectTurnRequestRow | None,
            (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(*predicates)
                    .values(**values)
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none(),
        )

    async def _owned_fenced_update(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        lease: Lease,
        statuses: set[DirectTurnStatus] | frozenset[DirectTurnStatus],
        values: dict[str, Any],
        predicates: tuple[Any, ...] = (),
    ) -> DirectTurnRequestRow | None:
        request = (
            await session.execute(
                select(DirectTurnRequestRow).where(DirectTurnRequestRow.request_id == request_id)
            )
        ).scalar_one_or_none()
        if request is None or lease.resource_key != conversation_lease_key(request.conversation_id):
            return None
        if (
            request.owner_controller_id is None
            or request.owner_incarnation_id is None
            or lease.owner_id
            != _owner_id(request.owner_controller_id, request.owner_incarnation_id)
        ):
            return None
        return cast(
            DirectTurnRequestRow | None,
            (
                await session.execute(
                    update(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status.in_([status.value for status in statuses]),
                        DirectTurnRequestRow.owner_controller_id == request.owner_controller_id,
                        DirectTurnRequestRow.owner_incarnation_id == request.owner_incarnation_id,
                        DirectTurnRequestRow.fencing_token == lease.fencing_token,
                        self._lease_predicate(session, lease),
                        *predicates,
                    )
                    .values(**values)
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none(),
        )

    @staticmethod
    def _lease_predicate(session: AsyncSession, lease: Lease) -> Any:
        return exists(
            select(1).where(
                CoordinationLeaseRow.resource_key == lease.resource_key,
                CoordinationLeaseRow.owner_id == lease.owner_id,
                CoordinationLeaseRow.fencing_token == lease.fencing_token,
                CoordinationLeaseRow.lease_expires_at > database_now_expression(session),
            )
        )

    async def create_fenced_channel_delivery(
        self,
        *,
        request_id: str,
        lease: Lease,
        delivery_id: str,
        descriptor: dict[str, Any],
        content: str,
        attachments: list[dict[str, Any]] | None,
    ) -> ChannelDeliveryOutboxRow | None:
        """Create one deterministic outbox row only under the live request fence."""
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            request = aliased(DirectTurnRequestRow)
            columns = [
                "delivery_id",
                "user_email",
                "conversation_id",
                "session_id",
                "source_type",
                "source_id",
                "channel_type",
                "account_id",
                "chat_id",
                "thread_id",
                "reply_to_id",
                "direct_turn_request_id",
                "direct_turn_fencing_token",
                "status",
                "fallback_text",
                "attachments_json",
                "next_attempt_at",
            ]
            values = select(
                literal(delivery_id),
                request.user_id,
                request.conversation_id,
                request.session_id,
                literal("direct_turn_result"),
                request.turn_id,
                literal(str(descriptor["channel_type"])),
                literal(str(descriptor["account_id"])),
                literal(str(descriptor["chat_id"])),
                literal(descriptor.get("thread_id")),
                literal(descriptor.get("reply_to_id")),
                request.request_id,
                request.fencing_token,
                literal("pending"),
                literal(content),
                literal(attachments, type_=ChannelDeliveryOutboxRow.attachments_json.type),
                database_now_expression(session),
            ).where(
                request.request_id == request_id,
                request.owner_controller_id.is_not(None),
                request.owner_incarnation_id.is_not(None),
                (
                    request.owner_controller_id + literal(":") + request.owner_incarnation_id
                    == lease.owner_id
                ),
                (
                    literal("direct-turn:conversation:") + request.conversation_id
                    == lease.resource_key
                ),
                request.fencing_token == lease.fencing_token,
                self._lease_predicate(session, lease),
            )
            statement = (
                insert(ChannelDeliveryOutboxRow)
                .from_select(columns, values)
                .on_conflict_do_nothing(index_elements=["delivery_id"])
                .returning(ChannelDeliveryOutboxRow)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                row = (
                    await session.execute(
                        select(ChannelDeliveryOutboxRow)
                        .join(
                            request,
                            request.request_id == ChannelDeliveryOutboxRow.direct_turn_request_id,
                        )
                        .where(
                            ChannelDeliveryOutboxRow.delivery_id == delivery_id,
                            ChannelDeliveryOutboxRow.direct_turn_request_id == request_id,
                            (
                                request.owner_controller_id
                                + literal(":")
                                + request.owner_incarnation_id
                                == lease.owner_id
                            ),
                            (
                                literal("direct-turn:conversation:") + request.conversation_id
                                == lease.resource_key
                            ),
                            request.fencing_token == lease.fencing_token,
                            self._lease_predicate(session, lease),
                        )
                    )
                ).scalar_one_or_none()
            await session.commit()
            return row
