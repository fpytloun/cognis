"""Durable direct-turn admission and fenced state transitions."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased, defer
from sqlalchemy.sql.elements import ColumnElement

from cognis import json_codec as json
from cognis.core.trusted_evidence import (
    TRUSTED_EVIDENCE_ADMISSION_KEY,
    deserialize_evidence_admission,
    deserialize_evidence_origin,
    serialize_evidence_admission,
    serialize_evidence_origin,
)
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import ChannelDeliveryDescriptor
from cognis.models.retry import RetryReason
from cognis.store.coordination import Lease, database_now, database_now_expression
from cognis.store.models import (
    ArtifactRecordRow,
    AuditLog,
    ChannelDeliveryOutboxRow,
    Conversation,
    CoordinationLeaseRow,
    DirectTurnRequestRow,
    ManagedConversationLink,
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


def direct_turn_content_is_editable(row: DirectTurnRequestRow) -> bool:
    """Project the same queued-content restriction enforced by edit."""
    metadata = row.payload.get("metadata") or {}
    admission = deserialize_evidence_admission(metadata.get(TRUSTED_EVIDENCE_ADMISSION_KEY))
    return row.status == DirectTurnStatus.QUEUED.value and not (
        admission is not None and admission.admitted
    )


TOOL_IN_FLIGHT_PHASE = "tool_in_flight"
TOOL_RESULT_PERSISTED_PHASE = "tool_result_persisted"
# One direct turn can execute tool batches from several Intaris sessions at
# once, because delegate children share the parent turn's execution fence. The
# group count is therefore bounded by the sessions holding a live batch, and
# each group is removed as its batch settles. Never bound the recorded groups or
# their descriptors while reading or writing: a dropped entry is a dispatched
# call that restart recovery can no longer settle, and silent truncation hides
# it. Tool recovery enforces this bound instead, and reports an over-long
# descriptor set as ambiguous rather than discarding the excess.
TOOL_DISPATCH_DESCRIPTOR_LIMIT = 500


@dataclass(frozen=True)
class ToolDispatchGroup:
    """One session's in-flight tool batch under a shared direct-turn fence.

    ``session_id`` and ``turn_id`` identify the canonical history that owns the
    descriptors. They are not interchangeable with the direct-turn row's own
    ``session_id``/``turn_id``: a delegate child runs its tools under the
    parent's fence but records them in its own session and turn.
    """

    session_id: str
    turn_id: str
    tool_calls: tuple[dict[str, Any], ...]

    def as_json(self) -> dict[str, Any]:
        """Return the persisted JSON form of this group."""
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "tool_calls": [dict(descriptor) for descriptor in self.tool_calls],
        }


class ToolDispatchMergeStatus(StrEnum):
    """Why a dispatch binding did or did not reach the durable record."""

    MERGED = "merged"
    FENCE_LOST = "fence_lost"
    CANCELLED = "cancelled"
    UNKNOWN_CALL = "unknown_call"


@dataclass(frozen=True)
class ToolDispatchMergeResult:
    """Outcome of merging one physical dispatch binding."""

    status: ToolDispatchMergeStatus
    row: DirectTurnRequestRow | None = None

    @property
    def fence_held(self) -> bool:
        """Return whether the caller still owns the direct-turn fence."""
        return self.status in {
            ToolDispatchMergeStatus.MERGED,
            ToolDispatchMergeStatus.UNKNOWN_CALL,
        }


def _coerce_dispatch_group(raw: Any) -> ToolDispatchGroup | None:
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    turn_id = raw.get("turn_id")
    descriptors = raw.get("tool_calls")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(turn_id, str) or not turn_id:
        return None
    if not isinstance(descriptors, list) or not descriptors:
        return None
    # Descriptor contents stay verbatim: tool recovery owns their validation and
    # must still see a malformed or over-long descriptor set instead of a
    # silently dropped call.
    return ToolDispatchGroup(
        session_id=session_id,
        turn_id=turn_id,
        tool_calls=tuple(descriptors),
    )


def read_tool_dispatch_groups(
    outcome: Any,
    *,
    fallback_session_id: str | None = None,
    fallback_turn_id: str | None = None,
) -> list[ToolDispatchGroup]:
    """Return every session's in-flight tool batch recorded on one direct turn.

    Rows written before per-session dispatch groups existed carry a single flat
    ``tool_calls`` list plus the last writer's ``session_id``/``turn_id``. That
    legacy shape is read here so a controller can still recover turns that were
    in flight across an upgrade. Remove the fallback once no pre-upgrade row can
    still be in ``tool_in_flight``.
    """
    if not isinstance(outcome, dict):
        return []
    raw_groups = outcome.get("tool_dispatch_groups")
    if isinstance(raw_groups, list):
        groups: list[ToolDispatchGroup] = []
        for raw in raw_groups:
            group = _coerce_dispatch_group(raw)
            if group is not None:
                groups.append(group)
        return groups
    legacy_descriptors = outcome.get("tool_calls")
    if not isinstance(legacy_descriptors, list) or not legacy_descriptors:
        return []
    legacy_session_id = outcome.get("session_id") or fallback_session_id
    legacy_turn_id = outcome.get("turn_id") or fallback_turn_id
    legacy = _coerce_dispatch_group(
        {
            "session_id": legacy_session_id,
            "turn_id": legacy_turn_id,
            "tool_calls": legacy_descriptors,
        }
    )
    return [legacy] if legacy is not None else []


def unreadable_tool_dispatch_groups(outcome: Any) -> list[Any]:
    """Return recorded group entries that cannot be read back as a group.

    An entry lands here when it carries no usable session or turn identity, for
    example a batch dispatched by a caller whose turn could not be resolved.
    Such an entry still represents dispatched calls, so live writers must carry
    it forward verbatim and let tool recovery report it.
    """
    if not isinstance(outcome, dict):
        return []
    raw_groups = outcome.get("tool_dispatch_groups")
    if not isinstance(raw_groups, list):
        return []
    return [raw for raw in raw_groups if _coerce_dispatch_group(raw) is None]


def write_tool_dispatch_groups(
    outcome: dict[str, Any],
    groups: list[ToolDispatchGroup],
    *,
    preserved: Sequence[Any] = (),
) -> dict[str, Any]:
    """Return ``outcome`` carrying ``groups`` plus ``preserved`` raw entries.

    ``preserved`` holds entries that could not be read back as groups. Dropping
    them here would erase dispatched calls from the durable record before tool
    recovery ever inspects it, so every live writer carries them forward.

    The flat legacy ``tool_calls`` slot is always dropped. Mirroring groups back
    into it would let an older controller attribute a delegate child's calls to
    the parent session and repair canonical history into the wrong session.
    """
    updated = {
        key: value
        for key, value in outcome.items()
        if key not in {"tool_dispatch_groups", "tool_calls"}
    }
    recorded = [group.as_json() for group in groups] + list(preserved)
    if recorded:
        updated["tool_dispatch_groups"] = recorded
    return updated


def _resolve_dispatch_turn_id(turn_id: str | None, request: DirectTurnRequestRow) -> str:
    """Return the canonical turn that owns a dispatch group.

    The caller's own turn wins. A caller without one falls back to the direct
    turn's turn, which matches where its canonical tool history is recorded. An
    unresolvable turn is recorded as empty so recovery reports missing canonical
    identity instead of silently dropping the batch.
    """
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    return request.turn_id if isinstance(request.turn_id, str) and request.turn_id else ""


def _replace_dispatch_group(
    groups: list[ToolDispatchGroup],
    replacement: ToolDispatchGroup,
) -> list[ToolDispatchGroup]:
    key = (replacement.session_id, replacement.turn_id)
    merged = [group for group in groups if (group.session_id, group.turn_id) != key]
    merged.append(replacement)
    return merged


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


class DurableAttachmentRefV1(BaseModel):
    """Stable attachment identity and metadata; signed URLs are never durable."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: ArtifactKind
    mime_type: str
    filename: str
    size_bytes: int
    url: str | None = Field(default=None, exclude=True)


class DirectTurnPayloadV1(BaseModel):
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


def foreground_admission_clause() -> ColumnElement[bool]:
    """Classify persisted v1 admissions using their canonical metadata envelope."""
    return cast(
        ColumnElement[bool],
        DirectTurnRequestRow.payload["metadata"]["system_initiated"].as_boolean().is_not(True),
    )


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
    queued_behind_predecessor: bool


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
    normalized = validated.model_dump(mode="json")
    raw_origin = normalized["metadata"].get("evidence_origin")
    if raw_origin is not None:
        origin = deserialize_evidence_origin(raw_origin)
        if origin is None:
            raise ValueError("metadata.evidence_origin is invalid")
        normalized["metadata"]["evidence_origin"] = serialize_evidence_origin(origin)
    raw_admission = normalized["metadata"].get(TRUSTED_EVIDENCE_ADMISSION_KEY)
    if raw_admission is not None:
        admission = deserialize_evidence_admission(raw_admission)
        if admission is None:
            raise ValueError(f"metadata.{TRUSTED_EVIDENCE_ADMISSION_KEY} is invalid")
        normalized["metadata"][TRUSTED_EVIDENCE_ADMISSION_KEY] = serialize_evidence_admission(
            admission
        )
    if "retry_reason" not in payload:
        normalized.pop("retry_reason", None)
    return normalized


def _admission_descriptor_payload(
    payload: dict[str, Any], *, generated_user_message_metadata: bool = False
) -> dict[str, Any]:
    """Remove server-owned policy data from the client idempotency descriptor."""
    descriptor_payload = dict(payload)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        descriptor_payload["metadata"] = {
            key: value for key, value in metadata.items() if key != TRUSTED_EVIDENCE_ADMISSION_KEY
        }
        if generated_user_message_metadata:
            envelope = metadata.get("user_message_metadata")
            if isinstance(envelope, dict):
                descriptor_payload["metadata"]["user_message_metadata"] = {
                    key: value for key, value in envelope.items() if key != "ts"
                }
    return descriptor_payload


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
        generated_user_message_metadata: bool = False,
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
            "payload": _admission_descriptor_payload(
                payload, generated_user_message_metadata=generated_user_message_metadata
            ),
        }
        # Both new branches are namespaced away from the unversioned legacy
        # descriptor. Provenance comes from the caller, never from the payload.
        descriptor["generated_user_message_metadata"] = generated_user_message_metadata
        admission_hash = _canonical_hash(descriptor)
        legacy_descriptor = {
            key: value
            for key, value in descriptor.items()
            if key != "generated_user_message_metadata"
        }
        # Exact old semantics: retain even a server-generated timestamp.
        legacy_descriptor["payload"] = _admission_descriptor_payload(payload)
        legacy_admission_hash = _canonical_hash(legacy_descriptor)
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
                    legacy_admission_hash=legacy_admission_hash,
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
        legacy_admission_hash: str,
        admission_guard: DirectTurnAdmissionGuard | None,
        transaction_participant: (
            Callable[[AsyncSession, DirectTurnRequestRow, bool], Awaitable[None]] | None
        ),
    ) -> AdmissionResult:
        async with self._session_factory() as session:
            # Serialize admission disposition per conversation. Durable rows
            # are inserted as QUEUED staging records, but the FIFO head can run
            # immediately and must not be exposed as user-visible queueing.
            await session.execute(
                select(Conversation.conversation_id)
                .where(Conversation.conversation_id == values["conversation_id"])
                .with_for_update()
            )
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
                if row.admission_hash not in (admission_hash, legacy_admission_hash):
                    await session.rollback()
                    raise DirectTurnConflictError(
                        "idempotency key was reused with a different request"
                    )
            if transaction_participant is not None:
                await transaction_participant(session, row, created)
            queued_behind_predecessor = bool(
                await session.scalar(
                    select(
                        exists().where(
                            DirectTurnRequestRow.conversation_id == row.conversation_id,
                            DirectTurnRequestRow.admission_order < row.admission_order,
                            DirectTurnRequestRow.status.in_(
                                [status.value for status in NONTERMINAL_STATUSES]
                            ),
                        )
                    )
                )
            )
            await session.commit()
            return AdmissionResult(
                request=row,
                created=created,
                queued_behind_predecessor=queued_behind_predecessor,
            )

    async def get(
        self, request_id: str, *, session: AsyncSession | None = None
    ) -> DirectTurnRequestRow | None:
        """Read a request, optionally within the caller-owned transaction."""
        if session is None:
            async with self._session_factory() as owned_session:
                return await self.get(request_id, session=owned_session)
        return (
            await session.execute(
                select(DirectTurnRequestRow)
                .where(DirectTurnRequestRow.request_id == request_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _payload_deferred() -> Any:
        """Skip the durable ``payload`` JSON on reads that only need ownership state.

        asyncpg decodes JSON columns on the event loop; the payload is only
        needed by ``materialize_claimed_payload`` and the terminal handlers.
        ``raiseload`` turns an accidental read into an explicit error.
        """

        return defer(DirectTurnRequestRow.payload, raiseload=True)

    async def get_cancel_state(self, request_id: str) -> tuple[str, datetime | None] | None:
        """Return ``(status, cancel_requested_at)`` without loading the payload."""

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        DirectTurnRequestRow.status,
                        DirectTurnRequestRow.cancel_requested_at,
                    ).where(DirectTurnRequestRow.request_id == request_id)
                )
            ).one_or_none()
            if row is None:
                return None
            return str(row[0]), row[1]

    async def list_claimable_heads(self, *, limit: int = 100) -> list[DirectTurnRequestRow]:
        """Return FIFO conversation heads that are eligible for ownership."""
        earlier = aliased(DirectTurnRequestRow)
        async with self._session_factory() as session:
            now = database_now_expression(session)
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                # Match SQLAlchemy's bound DateTime representation, including at
                # exact second boundaries, without another database-clock read.
                now = func.strftime("%Y-%m-%d %H:%M:%S.000000", now)
            result = await session.execute(
                select(DirectTurnRequestRow)
                .options(self._payload_deferred())
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

    async def get_conversation_active(
        self,
        conversation_id: str,
        *,
        session: AsyncSession | None = None,
        include_recoverable: bool = False,
    ) -> DirectTurnRequestRow | None:
        """Return the authoritative active durable turn for a conversation."""

        if session is None:
            async with self._session_factory() as owned_session:
                return await self.get_conversation_active(
                    conversation_id,
                    session=owned_session,
                    include_recoverable=include_recoverable,
                )
        result = await session.execute(
            select(DirectTurnRequestRow)
            .where(
                DirectTurnRequestRow.conversation_id == conversation_id,
                DirectTurnRequestRow.status.in_(
                    [status.value for status in ACTIVE_STATUSES]
                    + ([DirectTurnStatus.RECOVERABLE.value] if include_recoverable else [])
                ),
            )
            .order_by(DirectTurnRequestRow.admission_order)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_conversation_runtime_authority_row(
        self,
        conversation_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> DirectTurnRequestRow | None:
        """Return active state, or the latest execution generation, in one query."""

        if session is None:
            async with self._session_factory() as owned_session:
                return await self.get_conversation_runtime_authority_row(
                    conversation_id,
                    session=owned_session,
                )
        active_values = [status.value for status in ACTIVE_STATUSES]
        historical_values = [
            status.value
            for status in (
                ACTIVE_STATUSES
                | {DirectTurnStatus.RECOVERABLE}
                | (TERMINAL_STATUSES - {DirectTurnStatus.ABSORBED})
            )
        ]
        result = await session.execute(
            select(DirectTurnRequestRow)
            .where(
                DirectTurnRequestRow.conversation_id == conversation_id,
                DirectTurnRequestRow.status.in_(historical_values),
            )
            .order_by(
                case(
                    (DirectTurnRequestRow.status.in_(active_values), 0),
                    else_=1,
                ),
                # Recovery advances the fence without changing admission order.
                # Absorbed messages never own the absorbing turn's lifecycle.
                DirectTurnRequestRow.fencing_token.desc().nullslast(),
                DirectTurnRequestRow.admission_order.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_conversations_active(
        self,
        conversation_ids: list[str],
        *,
        session: AsyncSession | None = None,
        include_recoverable: bool = False,
    ) -> dict[str, DirectTurnRequestRow]:
        """Return one authoritative active durable turn per requested conversation."""

        if not conversation_ids:
            return {}
        if session is None:
            async with self._session_factory() as owned_session:
                return await self.list_conversations_active(
                    conversation_ids,
                    session=owned_session,
                    include_recoverable=include_recoverable,
                )
        result = await session.execute(
            select(DirectTurnRequestRow)
            .where(
                DirectTurnRequestRow.conversation_id.in_(conversation_ids),
                DirectTurnRequestRow.status.in_(
                    [status.value for status in ACTIVE_STATUSES]
                    + ([DirectTurnStatus.RECOVERABLE.value] if include_recoverable else [])
                ),
            )
            .order_by(DirectTurnRequestRow.conversation_id, DirectTurnRequestRow.admission_order)
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
        """Discover pending visibility work without loading admitted payloads."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(DirectTurnRequestRow)
                .options(self._payload_deferred())
                .where(
                    DirectTurnRequestRow.status == DirectTurnStatus.FAILED.value,
                    DirectTurnRequestRow.outcome["phase"].as_string()
                    == "permanent_payload_visibility_pending",
                )
                .order_by(DirectTurnRequestRow.admission_order)
                .limit(max(1, limit))
            )
            return list(result.scalars().all())

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
        async with self._session_factory() as session:
            current = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.request_id == request_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.QUEUED.value,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                await session.rollback()
                return None
            if expected_payload_hash is not None and current.payload_hash != expected_payload_hash:
                await session.rollback()
                return None
            raw_current_metadata = (
                current.payload.get("metadata") if isinstance(current.payload, dict) else None
            )
            current_metadata: dict[str, Any] = (
                dict(raw_current_metadata) if isinstance(raw_current_metadata, dict) else {}
            )
            current_admission = current_metadata.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
            if not direct_turn_content_is_editable(current):
                bound_fields_changed = any(
                    payload.get(field) != current.payload.get(field)
                    for field in ("content", "attachments")
                )
                if bound_fields_changed:
                    await session.rollback()
                    raise ValueError(
                        "content and attachments are immutable after trusted evidence admission"
                    )
            raw_proposed_metadata = payload.get("metadata")
            proposed_metadata: dict[str, Any]
            if not isinstance(raw_proposed_metadata, dict):
                proposed_metadata = {}
                payload = {**payload, "metadata": proposed_metadata}
            else:
                proposed_metadata = dict(raw_proposed_metadata)
            proposed_admission = proposed_metadata.get(TRUSTED_EVIDENCE_ADMISSION_KEY)
            if proposed_admission is not None and proposed_admission != current_admission:
                await session.rollback()
                raise ValueError("trusted evidence admission is immutable")
            if current_admission is not None:
                payload = {
                    **payload,
                    "metadata": {
                        **proposed_metadata,
                        TRUSTED_EVIDENCE_ADMISSION_KEY: current_admission,
                    },
                }
            payload = _validated_payload(payload, payload_version)
            now = await database_now(session)
            predicates = [
                DirectTurnRequestRow.request_id == request_id,
                DirectTurnRequestRow.status == DirectTurnStatus.QUEUED.value,
                DirectTurnRequestRow.payload_hash == current.payload_hash,
            ]
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
            return row

    async def handoff(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        turn_id: str,
        lease: Lease,
        successor: DirectTurnRequestRow,
    ) -> bool:
        """Complete a predecessor inside the successor admission transaction.

        The caller holds the conversation admission lock. Return False for an
        exact committed replay, True for a new handoff.
        """
        predecessor = await session.scalar(
            select(DirectTurnRequestRow).where(DirectTurnRequestRow.request_id == request_id)
        )
        if (
            predecessor is None
            or predecessor.conversation_id != successor.conversation_id
            or predecessor.turn_id != turn_id
        ):
            raise DirectTurnAdmissionRejected("continuation predecessor mismatch")
        outcome = {
            "phase": "automatic_continuation_handoff",
            "successor_request_id": successor.request_id,
            "successor_turn_id": successor.turn_id,
        }
        if predecessor.status == DirectTurnStatus.COMPLETED.value:
            if predecessor.outcome == outcome:
                return False
            raise DirectTurnAdmissionRejected("continuation predecessor already settled")
        now = await database_now(session)
        updated = await self._owned_fenced_update(
            session,
            request_id=request_id,
            lease=lease,
            statuses=ACTIVE_STATUSES,
            predicates=(DirectTurnRequestRow.cancel_requested_at.is_(None),),
            values={
                "status": DirectTurnStatus.COMPLETED.value,
                "outcome": outcome,
                "terminal_at": now,
                "updated_at": now,
            },
        )
        if updated is None:
            raise DirectTurnAdmissionRejected("continuation predecessor fence changed")
        return True

    async def cancel_conversation(
        self,
        conversation_id: str,
        *,
        clear_queue: bool,
        active_request_id: str | None = None,
    ) -> list[CancelResult]:
        """Serialize stop with admission and follow the committed active chain."""
        async with self._session_factory() as session:
            await session.execute(
                select(Conversation.conversation_id)
                .where(Conversation.conversation_id == conversation_id)
                .with_for_update()
            )
            rows = list(
                (
                    await session.scalars(
                        select(DirectTurnRequestRow)
                        .where(
                            DirectTurnRequestRow.conversation_id == conversation_id,
                            DirectTurnRequestRow.status.in_(
                                [status.value for status in NONTERMINAL_STATUSES]
                            ),
                        )
                        .order_by(DirectTurnRequestRow.admission_order)
                    )
                ).all()
            )
            if not clear_queue:
                active = (
                    await session.scalar(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.request_id == active_request_id
                        )
                    )
                    if active_request_id
                    else None
                )
                if active is None:
                    active = next(
                        (
                            row
                            for row in rows
                            if row.status
                            in {DirectTurnStatus.CLAIMED.value, DirectTurnStatus.RUNNING.value}
                        ),
                        None,
                    )
                if active is None:
                    active = await session.scalar(
                        select(DirectTurnRequestRow)
                        .where(
                            DirectTurnRequestRow.conversation_id == conversation_id,
                            DirectTurnRequestRow.status == DirectTurnStatus.COMPLETED.value,
                            DirectTurnRequestRow.outcome["phase"].as_string()
                            == "automatic_continuation_handoff",
                        )
                        .order_by(DirectTurnRequestRow.admission_order.desc())
                        .limit(1)
                    )
                seen: set[str] = set()
                while (
                    active is not None
                    and active.conversation_id == conversation_id
                    and active.request_id not in seen
                    and (active.outcome or {}).get("phase") == "automatic_continuation_handoff"
                ):
                    seen.add(active.request_id)
                    outcome = active.outcome or {}
                    successor = await session.scalar(
                        select(DirectTurnRequestRow).where(
                            DirectTurnRequestRow.request_id == outcome["successor_request_id"]
                        )
                    )
                    active = successor
                if active_request_id is not None and active is not None and active not in rows:
                    rows = []
                    active = None
                elif active is None or active not in rows:
                    active = next(
                        (row for row in rows if row.status == DirectTurnStatus.RECOVERABLE.value),
                        None,
                    )
                rows = [active] if active is not None else []
            now = await database_now(session)
            results = []
            for row in rows:
                updated_row = (
                    await session.execute(
                        update(DirectTurnRequestRow)
                        .where(
                            DirectTurnRequestRow.request_id == row.request_id,
                            DirectTurnRequestRow.status.in_(
                                [status.value for status in NONTERMINAL_STATUSES]
                            ),
                        )
                        .values(
                            cancel_requested_at=now,
                            updated_at=now,
                            status=case(
                                (
                                    DirectTurnRequestRow.status.in_(
                                        [status.value for status in CLAIMABLE_STATUSES]
                                    ),
                                    DirectTurnStatus.CANCELLED.value,
                                ),
                                else_=DirectTurnRequestRow.status,
                            ),
                            terminal_at=case(
                                (
                                    DirectTurnRequestRow.status.in_(
                                        [status.value for status in CLAIMABLE_STATUSES]
                                    ),
                                    now,
                                ),
                                else_=DirectTurnRequestRow.terminal_at,
                            ),
                            next_attempt_at=case(
                                (
                                    DirectTurnRequestRow.status.in_(
                                        [status.value for status in CLAIMABLE_STATUSES]
                                    ),
                                    None,
                                ),
                                else_=DirectTurnRequestRow.next_attempt_at,
                            ),
                        )
                        .returning(DirectTurnRequestRow)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if updated_row is None:
                    continue
                results.append(
                    CancelResult(
                        request=updated_row,
                        cancellation_requested=DirectTurnStatus(updated_row.status)
                        in ACTIVE_STATUSES,
                    )
                )
            await session.commit()
            return results

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
                        next_attempt_at=case(
                            (
                                DirectTurnRequestRow.status.in_(
                                    [status.value for status in CLAIMABLE_STATUSES]
                                ),
                                None,
                            ),
                            else_=DirectTurnRequestRow.next_attempt_at,
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

    async def expedite_provider_retry(
        self,
        conversation_id: str,
        *,
        session_id: str,
        previous_runtime_revision: int,
    ) -> DirectTurnRequestRow | None:
        """Make a provider-specific retry due after its runtime target changes."""

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.conversation_id == conversation_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.RECOVERABLE.value,
                    )
                    .order_by(DirectTurnRequestRow.admission_order)
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            outcome = row.outcome if isinstance(row.outcome, dict) else {}
            retry_session_id = row.session_id or outcome.get("session_id")
            if retry_session_id != session_id:
                return None
            retry_target = outcome.get("retry_target")
            if (
                not isinstance(retry_target, dict)
                or retry_target.get("scope") != "provider_model"
                or retry_target.get("runtime_revision") != previous_runtime_revision
            ):
                return None
            now = await database_now(session)
            if row.next_attempt_at is None:
                return None
            row.next_attempt_at = now
            row.updated_at = now
            await session.commit()
            return row

    async def checkpoint(
        self,
        request_id: str,
        *,
        lease: Lease,
        phase: str,
        metadata: dict[str, Any] | None = None,
        clear_tool_dispatch: bool = False,
    ) -> DirectTurnRequestRow | None:
        """Persist the latest execution boundary under the exact fence.

        Live tool dispatch groups survive every other phase boundary. Several
        sessions share one direct-turn fence, so a session reaching its own
        boundary must not erase a sibling's in-flight batch, and the turn must
        stay in ``tool_in_flight`` while any dispatched call can still need
        restart recovery. The requested phase is kept as ``deferred_phase``.

        ``clear_tool_dispatch`` is for the recovery-completion boundary, which
        has already settled every group and must release the phase.
        """
        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .options(self._payload_deferred())
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request is None or not self._owns_live_fence(request, lease):
                return None
            if not await session.scalar(select(self._lease_predicate(session, lease))):
                return None
            now = await database_now(session)
            previous = dict(request.outcome) if isinstance(request.outcome, dict) else {}
            live_groups = (
                []
                if clear_tool_dispatch
                else read_tool_dispatch_groups(
                    previous,
                    fallback_session_id=request.session_id,
                    fallback_turn_id=request.turn_id,
                )
            )
            preserved = [] if clear_tool_dispatch else unreadable_tool_dispatch_groups(previous)
            outcome = {
                "phase": phase,
                "phase_started_at": now.isoformat(),
                **(metadata or {}),
            }
            if live_groups or preserved:
                previous_started_at = previous.get("phase_started_at")
                outcome["phase"] = TOOL_IN_FLIGHT_PHASE
                outcome["phase_started_at"] = (
                    previous_started_at
                    if previous.get("phase") == TOOL_IN_FLIGHT_PHASE
                    and isinstance(previous_started_at, str)
                    else now.isoformat()
                )
                outcome["deferred_phase"] = phase
            request.outcome = write_tool_dispatch_groups(
                outcome,
                live_groups,
                preserved=preserved,
            )
            request.updated_at = now
            await session.commit()
            return request

    async def record_tool_dispatch(
        self,
        request_id: str,
        *,
        lease: Lease,
        session_id: str,
        turn_id: str | None,
        descriptors: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        """Record one session's in-flight tool batch, preserving sibling batches.

        Delegate children share the parent turn's fence, so several sessions can
        hold in-flight tool calls at once. Each call replaces only the group
        owned by ``(session_id, turn_id)``; other sessions' groups survive.
        """

        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .options(self._payload_deferred())
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request is None or not self._owns_live_fence(request, lease):
                return None
            if not await session.scalar(select(self._lease_predicate(session, lease))):
                return None
            outcome = dict(request.outcome) if isinstance(request.outcome, dict) else {}
            groups = read_tool_dispatch_groups(
                outcome,
                fallback_session_id=request.session_id,
                fallback_turn_id=request.turn_id,
            )
            now = await database_now(session)
            merged = _replace_dispatch_group(
                groups,
                ToolDispatchGroup(
                    session_id=session_id,
                    turn_id=_resolve_dispatch_turn_id(turn_id, request),
                    tool_calls=tuple(descriptors),
                ),
            )
            # This session's new batch supersedes any earlier unreadable entry of
            # its own, so only other sessions' entries carry forward.
            preserved = [
                raw
                for raw in unreadable_tool_dispatch_groups(outcome)
                if not isinstance(raw, dict) or raw.get("session_id") != session_id
            ]
            request.outcome = write_tool_dispatch_groups(
                {
                    "phase": TOOL_IN_FLIGHT_PHASE,
                    "phase_started_at": now.isoformat(),
                    **(metadata or {}),
                },
                merged,
                preserved=preserved,
            )
            request.updated_at = now
            await session.commit()
            return request

    async def complete_tool_dispatch(
        self,
        request_id: str,
        *,
        lease: Lease,
        session_id: str,
        turn_id: str | None,
        call_ids: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> DirectTurnRequestRow | None:
        """Release one session's settled tool batch from the shared turn record.

        The turn only leaves ``tool_in_flight`` once no session still holds an
        in-flight batch. Clearing the phase while a sibling session is mid-batch
        would make those calls invisible to restart recovery.
        """

        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .options(self._payload_deferred())
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request is None or not self._owns_live_fence(request, lease):
                return None
            if not await session.scalar(select(self._lease_predicate(session, lease))):
                return None
            outcome = dict(request.outcome) if isinstance(request.outcome, dict) else {}
            groups = read_tool_dispatch_groups(
                outcome,
                fallback_session_id=request.session_id,
                fallback_turn_id=request.turn_id,
            )
            settled_key = (session_id, _resolve_dispatch_turn_id(turn_id, request))
            remaining = [
                group for group in groups if (group.session_id, group.turn_id) != settled_key
            ]
            preserved = unreadable_tool_dispatch_groups(outcome)
            now = await database_now(session)
            previous_phase = outcome.get("phase")
            previous_started_at = outcome.get("phase_started_at")
            if remaining or preserved:
                # A sibling batch is still live: keep the in-flight phase and its
                # original clock so a settled batch cannot defer staleness
                # detection for the batch that is still running.
                phase = TOOL_IN_FLIGHT_PHASE
                phase_started_at = (
                    previous_started_at
                    if previous_phase == TOOL_IN_FLIGHT_PHASE
                    and isinstance(previous_started_at, str)
                    else now.isoformat()
                )
            else:
                phase = TOOL_RESULT_PERSISTED_PHASE
                phase_started_at = now.isoformat()
            request.outcome = write_tool_dispatch_groups(
                {
                    "phase": phase,
                    "phase_started_at": phase_started_at,
                    **(metadata or {}),
                    "call_ids": list(call_ids[:TOOL_DISPATCH_DESCRIPTOR_LIMIT]),
                },
                remaining,
                preserved=preserved,
            )
            request.updated_at = now
            await session.commit()
            return request

    async def merge_tool_dispatch(
        self,
        request_id: str,
        *,
        lease: Lease,
        session_id: str,
        turn_id: str | None,
        call_id: str,
        executor_id: str,
        executor_instance_id: str | None,
        dispatch_state: Literal["dispatching", "sent"],
    ) -> ToolDispatchMergeResult:
        """Merge one physical dispatch binding under the live direct-turn fence.

        A missing descriptor is reported as ``UNKNOWN_CALL`` rather than as a
        lost fence. The two conditions are unrelated: a binding can arrive after
        its own batch settled, while the fence itself is still held.
        """

        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .options(self._payload_deferred())
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request is None or not self._owns_live_fence(request, lease):
                return ToolDispatchMergeResult(status=ToolDispatchMergeStatus.FENCE_LOST)
            if not await session.scalar(select(self._lease_predicate(session, lease))):
                return ToolDispatchMergeResult(status=ToolDispatchMergeStatus.FENCE_LOST)
            if request.cancel_requested_at is not None:
                return ToolDispatchMergeResult(status=ToolDispatchMergeStatus.CANCELLED)
            outcome = dict(request.outcome) if isinstance(request.outcome, dict) else {}
            groups = read_tool_dispatch_groups(
                outcome,
                fallback_session_id=request.session_id,
                fallback_turn_id=request.turn_id,
            )
            owner_key = (session_id, _resolve_dispatch_turn_id(turn_id, request))
            owner = next(
                (group for group in groups if (group.session_id, group.turn_id) == owner_key),
                None,
            )
            if owner is None:
                return ToolDispatchMergeResult(status=ToolDispatchMergeStatus.UNKNOWN_CALL)
            matched = False
            merged_descriptors: list[dict[str, Any]] = []
            for raw in owner.tool_calls:
                if not isinstance(raw, dict):
                    continue
                descriptor = dict(raw)
                if descriptor.get("call_id") == call_id:
                    descriptor.update(
                        {
                            "executor_id": executor_id,
                            "executor_instance_id": executor_instance_id,
                            "dispatch_state": dispatch_state,
                        }
                    )
                    matched = True
                merged_descriptors.append(descriptor)
            if not matched:
                return ToolDispatchMergeResult(status=ToolDispatchMergeStatus.UNKNOWN_CALL)
            now = await database_now(session)
            request.outcome = write_tool_dispatch_groups(
                outcome,
                _replace_dispatch_group(
                    groups,
                    ToolDispatchGroup(
                        session_id=owner.session_id,
                        turn_id=owner.turn_id,
                        tool_calls=tuple(merged_descriptors),
                    ),
                ),
                preserved=unreadable_tool_dispatch_groups(outcome),
            )
            request.updated_at = now
            await session.commit()
            return ToolDispatchMergeResult(
                status=ToolDispatchMergeStatus.MERGED,
                row=request,
            )

    @staticmethod
    def _owns_live_fence(request: DirectTurnRequestRow, lease: Lease) -> bool:
        """Return whether ``lease`` still owns this active request's fence."""
        return (
            request.status in {status.value for status in ACTIVE_STATUSES}
            and request.owner_controller_id is not None
            and request.owner_incarnation_id is not None
            and lease.owner_id
            == _owner_id(request.owner_controller_id, request.owner_incarnation_id)
            and request.fencing_token == lease.fencing_token
            and lease.resource_key == conversation_lease_key(request.conversation_id)
        )

    async def begin_tool_recovery(
        self,
        request_id: str,
        *,
        lease: Lease,
        controller_id: str,
        incarnation_id: str,
        deadline_seconds: float,
    ) -> DirectTurnRequestRow | None:
        """Take over stale tool recovery and preserve its first DB-time deadline."""

        if lease.owner_id != _owner_id(controller_id, incarnation_id):
            raise ValueError("lease owner does not match controller incarnation")
        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status not in {status.value for status in ACTIVE_STATUSES}
                or request.fencing_token is None
                or request.fencing_token >= lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
                or not await session.scalar(select(self._lease_predicate(session, lease)))
            ):
                return None
            outcome = dict(request.outcome) if isinstance(request.outcome, dict) else {}
            if outcome.get("phase") != "tool_in_flight":
                return None
            now = await database_now(session)
            recovery_started_at = outcome.get("recovery_started_at")
            if not isinstance(recovery_started_at, str):
                recovery_started_at = now.isoformat()
            recovery_deadline_at = outcome.get("recovery_deadline_at")
            if not isinstance(recovery_deadline_at, str):
                recovery_deadline_at = (
                    now + timedelta(seconds=max(1.0, deadline_seconds))
                ).isoformat()
            request.owner_controller_id = controller_id
            request.owner_incarnation_id = incarnation_id
            request.fencing_token = lease.fencing_token
            request.outcome = {
                **outcome,
                "recovery_started_at": recovery_started_at,
                "recovery_deadline_at": recovery_deadline_at,
            }
            request.updated_at = now
            await session.commit()
            return request

    async def mark_tool_recovery_terminal(
        self,
        request_id: str,
        *,
        lease: Lease,
        status: DirectTurnStatus,
        outcome: dict[str, Any],
    ) -> DirectTurnRequestRow | None:
        """Settle a failed recovery and any matching managed link atomically."""

        if status not in {DirectTurnStatus.FAILED, DirectTurnStatus.AMBIGUOUS}:
            raise ValueError("tool recovery fallback must be failed or ambiguous")
        async with self._session_factory() as session:
            now = await database_now(session)
            row = await self._owned_fenced_update(
                session,
                request_id=request_id,
                lease=lease,
                statuses=ACTIVE_STATUSES,
                values={
                    "status": status.value,
                    "outcome": outcome,
                    "terminal_at": now,
                    "updated_at": now,
                },
            )
            if row is None:
                await session.rollback()
                return None
            await session.execute(
                update(ManagedConversationLink)
                .where(
                    ManagedConversationLink.target_conversation_id == row.conversation_id,
                    ManagedConversationLink.active_turn_id == row.turn_id,
                )
                .values(
                    turn_state="failed",
                    active_turn_id=None,
                    notify_on_completion=False,
                    last_error=str(outcome.get("error") or outcome.get("reason") or "")[:1000],
                    last_result_turn_id=row.turn_id,
                    handoff_state=None,
                    handoff_target_turn_id=None,
                    handoff_controller_session_id=None,
                    handoff_controller_turn_id=None,
                    handoff_tool_call_id=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return row

    async def finish_tool_recovery(
        self,
        request_id: str,
        *,
        lease: Lease,
        outcome: dict[str, Any],
    ) -> DirectTurnRequestRow | None:
        """Make recovered work claimable, or honor cancellation atomically."""

        async with self._session_factory() as session:
            request = (
                await session.execute(
                    select(DirectTurnRequestRow)
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                request is None
                or request.status not in {status.value for status in ACTIVE_STATUSES}
                or request.owner_controller_id is None
                or request.owner_incarnation_id is None
                or lease.owner_id
                != _owner_id(request.owner_controller_id, request.owner_incarnation_id)
                or request.fencing_token != lease.fencing_token
                or lease.resource_key != conversation_lease_key(request.conversation_id)
                or not await session.scalar(select(self._lease_predicate(session, lease)))
            ):
                return None
            now = await database_now(session)
            if request.cancel_requested_at is not None:
                request.status = DirectTurnStatus.CANCELLED.value
                request.outcome = {
                    "phase": "cancelled",
                    "reason": "cancelled during tool recovery",
                }
                request.terminal_at = now
            else:
                request.status = DirectTurnStatus.RECOVERABLE.value
                request.outcome = outcome
            request.owner_controller_id = None
            request.owner_incarnation_id = None
            request.fencing_token = None
            request.updated_at = now
            await session.commit()
            return request

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
            if request.payload_hash != _canonical_hash(request.payload):
                raise PermanentDirectTurnPayloadError("Durable direct-turn payload hash mismatch")
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
            # Match admission/cancellation lock order: conversation, then request.
            await session.execute(
                select(Conversation.conversation_id)
                .where(
                    Conversation.conversation_id
                    == select(DirectTurnRequestRow.conversation_id)
                    .where(DirectTurnRequestRow.request_id == request_id)
                    .scalar_subquery()
                )
                .with_for_update()
            )
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
            if row is not None and row.attempt_count == 1:
                # The pin survives turns, but its recovery budget belongs to
                # active work. Takeover/retry must retain the original clock.
                await session.execute(
                    update(Conversation)
                    .where(
                        Conversation.conversation_id == row.conversation_id,
                        Conversation.lineage_task_id.is_(None),
                    )
                    .values(active_executor_unavailable_since=None)
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
                        fencing_token=DirectTurnRequestRow.fencing_token,
                        outcome=outcome,
                        updated_at=now,
                    )
                    .returning(DirectTurnRequestRow)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row

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
                        fencing_token=DirectTurnRequestRow.fencing_token,
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
            return row

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
                        fencing_token=DirectTurnRequestRow.fencing_token,
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
                        fencing_token=DirectTurnRequestRow.fencing_token,
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
                statuses={
                    DirectTurnStatus.QUEUED,
                    DirectTurnStatus.RECOVERABLE,
                },
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
                    "fencing_token": DirectTurnRequestRow.fencing_token,
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
                    "fencing_token": DirectTurnRequestRow.fencing_token,
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

    async def has_fence(
        self, request_id: str, *, lease: Lease, session: AsyncSession | None = None
    ) -> bool:
        """Check the exact ownership tuple in one indexed EXISTS (no payload decode).

        The owner and resource comparisons run in SQL: a NULL owner column makes
        the concatenation NULL, so the comparison is false exactly where the
        previous Python ``is None`` guards returned ``False``.

        A supplied session belongs to the caller: this check neither commits nor
        closes it. Document writes use their existing transaction and locks;
        ownership predicates, cancellation, and recovery policy are unchanged.
        """

        if session is None:
            async with self._session_factory() as owned_session:
                return await self.has_fence(request_id, lease=lease, session=owned_session)
        statement = select(
            exists(
                select(1).where(
                    DirectTurnRequestRow.request_id == request_id,
                    (
                        DirectTurnRequestRow.owner_controller_id
                        + ":"
                        + DirectTurnRequestRow.owner_incarnation_id
                    )
                    == lease.owner_id,
                    ("direct-turn:conversation:" + DirectTurnRequestRow.conversation_id)
                    == lease.resource_key,
                    DirectTurnRequestRow.fencing_token == lease.fencing_token,
                    DirectTurnRequestRow.status.in_([status.value for status in ACTIVE_STATUSES]),
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
        return (
            await session.execute(
                update(DirectTurnRequestRow)
                .where(*predicates)
                .values(**values)
                .returning(DirectTurnRequestRow)
            )
        ).scalar_one_or_none()

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
        return (
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
        ).scalar_one_or_none()

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

    async def effective_channel_delivery(
        self, request_id: str
    ) -> tuple[ChannelDeliveryDescriptor | None, str | None]:
        """Resolve the latest committed absorbed route without changing admission payloads."""
        async with self._session_factory() as session:
            request = await session.scalar(
                select(DirectTurnRequestRow).where(DirectTurnRequestRow.request_id == request_id)
            )
            if request is None:
                return None, None
            absorbed = (
                await session.scalars(
                    select(DirectTurnRequestRow)
                    .where(
                        DirectTurnRequestRow.conversation_id == request.conversation_id,
                        DirectTurnRequestRow.absorbed_by_turn_id == request.turn_id,
                        DirectTurnRequestRow.status == DirectTurnStatus.ABSORBED.value,
                    )
                    .order_by(DirectTurnRequestRow.admission_order.desc())
                )
            ).all()
            for row in [*absorbed, request]:
                payload = row.payload or {}
                route = payload.get("channel_delivery")
                if isinstance(route, dict):
                    return (
                        ChannelDeliveryDescriptor.model_validate(route),
                        payload.get("metadata", {}).get("channel_assistant_delivery_mode"),
                    )
        return None, None

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
