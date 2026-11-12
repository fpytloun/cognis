"""Explicit bounded maintenance, sharing the projector's short DB control fence."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Table, delete, func, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement, TextClause

from cognis.core.usage.contributions import (
    MEASURES,
    digest_json,
)
from cognis.core.usage.pricing import (
    MILLION,
    SCALE,
    UsagePriceResult,
    applicable_price_rates,
    disjoint_price_contracts,
    price_rate,
    valuation_domain,
)
from cognis.core.usage.projector import (
    PENDING_TRANSACTION_WORK,
    UsageProjectionMismatch,
    UsageProjector,
    as_utc,
    lock_scope,
    projection_transaction,
)
from cognis.core.usage.request_headers import project_request_headers
from cognis.core.usage.retention import UsageDetailRetention, refresh_detail_anchor
from cognis.models.usage import UsageCorrection, UsageModelSelection, UsagePriceDefinition
from cognis.store.models import LLMProvider
from cognis.store.usage.codec import terminal_values
from cognis.store.usage.models import (
    RETIRABLE_JOB_HISTORY,
    ProviderUsageSampleRow,
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageCorrectionRow,
    UsageDailyTotalRow,
    UsageHistoricalPriceBindingRow,
    UsageHourlyTotalRow,
    UsageMaintenanceJobRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageProjectionOutboxRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from cognis.store.usage.transactions import insert_for, notify_usage

# Pre-traffic scopes inspected per page so one failed scope cannot stall the backfill.
TRAFFIC_BACKFILL_CANDIDATES = 16


def _retirable_history(
    model: type[ProviderUsageSampleRow]
    | type[UsageProjectionOutboxRow]
    | type[UsageMaintenanceJobRow],
) -> ColumnElement[bool] | TextClause:
    return RETIRABLE_JOB_HISTORY if model is UsageMaintenanceJobRow else true()


class UsageMaintenanceBusy(ValueError):
    """The explicit caller may retry; inference never enters this boundary."""


class UsageDetailExpired(LookupError):
    """Expired/not-found details cannot be recreated by a correction."""


class UsageConflict(ValueError):
    """A stable identifier was redelivered with different canonical content."""


class UsagePriceSourceChanged(UsageConflict):
    """Metadata resolved against a provider configuration that is no longer current."""


class UsageArchiveRepriceUnsupported(ValueError):
    """Retained units cannot reconstruct the proposed per-call pricing rule."""


@dataclass(frozen=True)
class UsageMaintenanceScope:
    """Authorization is supplied by the API owner, never inferred from this value."""

    owner_key: str | None = None
    bucket_day: date | None = None
    provider_id: str | None = None
    model: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "owner_key": self.owner_key,
            "bucket_day": self.bucket_day.isoformat() if self.bucket_day else None,
            "provider_id": self.provider_id,
            "model": self.model,
        }


@dataclass(frozen=True)
class UsageRetentionPolicy:
    """Deletion stays disabled by default; tests explicitly opt in on isolated DBs."""

    enabled: bool = False
    detail_days: int = 30
    quota_days: int = 365
    bookkeeping_days: int = 30
    page_rows: int = 4
    page_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if not (
            self.detail_days >= 1
            and self.quota_days >= 1
            and self.bookkeeping_days >= 1
            and 1 <= self.page_rows <= 4
            and 4096 <= self.page_bytes <= 256 * 1024
        ):
            raise ValueError("Usage maintenance limits exceed the delivery budgets")


@dataclass(frozen=True)
class UsageJobStatus:
    """Durable progress, not evidence that skipped database qualification passed."""

    job_id: str
    kind: str
    status: str
    processed_count: int
    error_code: str | None


@dataclass(frozen=True)
class UsageRetentionPreview:
    """Read-only bounded candidates, not a deletion promise or authorization."""

    enabled: bool
    cutoff: datetime
    attempt_candidates: int
    request_candidates: int
    page_limited: bool
    pending_or_unprojected_preserved: bool
    repair_required: bool


class UsageMaintenanceService:
    """No worker/poller: the existing UsageProjector calls one page at a time."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        projector: UsageProjector,
        controller_owner_id: str,
        retention: UsageRetentionPolicy | None = None,
        replay_window_days: int = 30,
        retention_loader: Callable[[AsyncSession], Awaitable[UsageRetentionPolicy]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.projector = projector
        self.controller_owner_id = controller_owner_id
        self.retention = retention or UsageRetentionPolicy()
        self.replay_window_days = replay_window_days
        self.retention_loader = retention_loader
        self._validate_retention(self.retention)
        self._expiry_due = False
        self._expiry_scope: UsageMaintenanceScope | None = None
        self._expiry_kind = "archive"
        self._due_scope: UsageMaintenanceScope | None = None
        self._due_kind = "archive"

    def _validate_retention(self, policy: UsageRetentionPolicy) -> None:
        if policy.enabled and policy.detail_days < self.replay_window_days:
            raise ValueError(
                "Detail retention cannot be shorter than missing-event replay admission"
            )

    @staticmethod
    def _whole_day(scope: UsageMaintenanceScope) -> None:
        if scope.owner_key is None or scope.bucket_day is None:
            raise ValueError("Operation requires one explicitly authorized owner/UTC-day")
        if scope.provider_id is not None or scope.model is not None:
            raise ValueError(
                "Generation operations cover a whole owner/UTC-day, not a model subset"
            )

    async def preview_retention(self, scope: UsageMaintenanceScope) -> UsageRetentionPreview:
        """Inspect only an authorized day using capped indexed queries; never delete."""
        self._whole_day(scope)
        async with self.sessions() as session:
            policy = (
                await self.retention_loader(session)
                if self.retention_loader is not None
                else self.retention
            )
            cutoff = datetime.now(UTC) - timedelta(days=policy.detail_days)
            counts = []
            blocked = False
            for model, identity, at, eligible in (
                (
                    UsageAttemptRow,
                    UsageAttemptRow.row_id,
                    UsageAttemptRow.finalized_at,
                    UsageAttemptRow.fact_version > 0,
                ),
                (
                    UsageRequestStatsRow,
                    UsageRequestStatsRow.request_id,
                    UsageRequestStatsRow.updated_at,
                    UsageRequestStatsRow.logical_outcome.is_not(None),
                ),
            ):
                base = select(identity).where(
                    model.owner_key == scope.owner_key, model.bucket_day == scope.bucket_day
                )
                candidates = (
                    await session.scalars(
                        base.where(
                            eligible, at <= cutoff, model.projected_version == model.fact_version
                        )
                        .order_by(at, identity)
                        .limit(policy.page_rows + 1)
                    )
                ).all()
                counts.append(len(candidates))
                blocked |= (
                    await session.scalar(
                        base.where(
                            (~eligible) | (model.projected_version != model.fact_version)
                        ).limit(1)
                    )
                    is not None
                )
            state = await session.get(UsageProjectionStateRow, (scope.owner_key, scope.bucket_day))
            return UsageRetentionPreview(
                policy.enabled,
                cutoff,
                min(counts[0], policy.page_rows),
                min(counts[1], policy.page_rows),
                any(count > policy.page_rows for count in counts),
                blocked,
                state is not None and (state.retention_paused or not state.ready),
            )

    async def configure_retention(self, policy: UsageRetentionPolicy) -> None:
        """Apply persisted settings; seed bounded control-row work only after activation."""
        self.projector.schedule_expiry(None)
        self._expiry_due = False
        async with projection_transaction(self.sessions) as session:
            if self.retention_loader is not None:
                policy = await self.retention_loader(session)
            self._validate_retention(policy)
            self.retention = policy
            if policy.enabled:
                await self._seed_anchors(session, policy, datetime.now(UTC))
        self.projector.wake()

    async def _seed_anchors(
        self, session: AsyncSession, policy: UsageRetentionPolicy, now: datetime
    ) -> None:
        await self._enqueue(
            session,
            "cleanup",
            {"phase": "anchors"},
            now,
            job_id=f"uanchors_{digest_json(asdict(policy))}",
        )

    async def _refresh_policy(self, session: AsyncSession, now: datetime) -> None:
        if self.retention_loader is None:
            return
        policy = await self.retention_loader(session)
        self._validate_retention(policy)
        was_enabled = self.retention.enabled
        self.retention = policy
        if policy.enabled and not was_enabled:
            await self._seed_anchors(session, policy, now)

    async def _enqueue(
        self,
        session: AsyncSession,
        kind: str,
        scope: dict[str, Any],
        now: datetime,
        *,
        job_id: str | None = None,
        pricing_generation: int | None = None,
    ) -> UsageMaintenanceJobRow:
        job_id = job_id or f"ujob_{uuid.uuid4().hex}"
        statement = insert_for(session, UsageMaintenanceJobRow.__table__).values(
            job_id=job_id,
            kind=kind,
            status="queued",
            scope=scope,
            target_pricing_generation=pricing_generation,
            cursor=None,
            processed_count=0,
            owner_epoch=0,
            created_at=now,
            updated_at=now,
        )
        await session.execute(statement.on_conflict_do_nothing(index_elements=["job_id"]))
        job = await session.get(UsageMaintenanceJobRow, job_id)
        assert job is not None
        if job.kind != kind or job.scope != scope:
            raise UsageConflict("Usage job identity has different scope/content")
        await notify_usage(session, self.controller_owner_id, job_id)
        return job

    @staticmethod
    def _status(job: UsageMaintenanceJobRow) -> UsageJobStatus:
        return UsageJobStatus(job.job_id, job.kind, job.status, job.processed_count, job.error_code)

    async def publish_price(
        self,
        definition: UsagePriceDefinition,
        *,
        expected_provider_config: dict[str, Any] | None = None,
        expected_provider_revision: datetime | None = None,
    ) -> UsageJobStatus:
        """Immutable revision, current generation and targeted intent commit together."""
        now = datetime.now(UTC)
        async with projection_transaction(self.sessions) as session:
            if expected_provider_config is not None:
                provider = await session.get(
                    LLMProvider, definition.provider_id, with_for_update=True
                )
                if (
                    provider is None
                    or provider.status != "active"
                    or provider.config != expected_provider_config
                    or provider.updated_at != expected_provider_revision
                ):
                    raise UsagePriceSourceChanged(
                        "Provider configuration changed during resolution"
                    )
                existing = await session.get(UsagePriceRevisionRow, definition.revision_id)
                if existing is not None:
                    committed = UsagePriceDefinition.model_validate(existing.definition)
                    if definition.model_dump(exclude={"effective_at", "recorded_at"}) != (
                        committed.model_dump(exclude={"effective_at", "recorded_at"})
                    ):
                        raise UsageConflict("Immutable metadata price content differs")
                    # Concurrent warmers reuse the first committed publication timestamps.
                    definition = committed
            table = UsagePriceRevisionRow.__table__
            payload = definition.model_dump(mode="json", exclude_none=True)
            if not definition.media_operations:
                payload.pop("media_operations", None)
            statement = insert_for(session, table).values(
                **definition.model_dump(
                    exclude={"rates", "untiered_tariff", "media_operations"},
                    exclude_none=False,
                ),
                content_digest=definition.observation_digest(),
                definition=payload,
            )
            await session.execute(statement.on_conflict_do_nothing(index_elements=["revision_id"]))
            stored = await session.get(UsagePriceRevisionRow, definition.revision_id)
            assert stored is not None
            if (
                stored.content_digest != definition.observation_digest()
                or stored.definition != payload
            ):
                raise UsageConflict("Immutable price revision content differs")
            statement = insert_for(session, UsagePricingStateRow.__table__).values(
                provider_id=definition.provider_id,
                model=definition.model,
                generation=0,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=["provider_id", "model"])
            )
            state = (
                await session.scalars(
                    select(UsagePricingStateRow)
                    .where(
                        UsagePricingStateRow.provider_id == definition.provider_id,
                        UsagePricingStateRow.model == definition.model,
                    )
                    .with_for_update()
                )
            ).one()
            # The state lock serializes publications, including an initial
            # state created above. Inspect incompatible retained contracts, not
            # merely the predecessor: a disjoint intermediate is not a basis
            # reset for older overlapping facts. Stream metadata only.
            contracts = await session.stream(
                select(
                    UsagePriceRevisionRow.currency,
                    UsagePriceRevisionRow.basis,
                    UsagePriceRevisionRow.definition,
                )
                .where(
                    UsagePriceRevisionRow.provider_id == definition.provider_id,
                    UsagePriceRevisionRow.model == definition.model,
                    or_(
                        UsagePriceRevisionRow.currency != definition.currency,
                        UsagePriceRevisionRow.basis != definition.basis,
                    ),
                )
                .execution_options(yield_per=64)
            )
            try:
                async for currency, _basis, retained_definition in contracts:
                    if currency != definition.currency or not disjoint_price_contracts(
                        UsagePriceDefinition.model_validate(retained_definition), definition
                    ):
                        raise UsageConflict("A pricing domain cannot mix currency or cost basis")
            finally:
                await contracts.close()
            if state.current_revision_id != definition.revision_id:
                state.generation += 1
                state.current_revision_id = definition.revision_id
                state.updated_at = now
            job = await self._enqueue(
                session,
                "reprice",
                UsageMaintenanceScope(
                    provider_id=definition.provider_id, model=definition.model
                ).payload(),
                now,
                job_id=f"uprice_{definition.observation_digest()[:40]}_{state.generation}",
                pricing_generation=state.generation,
            )
            result = self._status(job)
        self.projector.wake()
        return result

    async def _request(
        self,
        kind: str,
        scope: UsageMaintenanceScope,
        *,
        job_id: str | None = None,
    ) -> UsageJobStatus:
        if kind in {"rebuild", "archive"}:
            self._whole_day(scope)
        async with projection_transaction(self.sessions) as session:
            await self._refresh_policy(session, datetime.now(UTC))
            if kind == "archive" and not self.retention.enabled:
                raise PermissionError("Usage detail deletion is disabled")
            if scope.owner_key is not None and scope.bucket_day is not None:
                state = await lock_scope(
                    session, scope.owner_key, scope.bucket_day, datetime.now(UTC)
                )
                if state.exclusive_job_id is not None:
                    raise UsageMaintenanceBusy("Usage scope has active maintenance")
            job = await self._enqueue(
                session, kind, scope.payload(), datetime.now(UTC), job_id=job_id
            )
            result = self._status(job)
        self.projector.wake()
        return result

    async def request_reprice(
        self, scope: UsageMaintenanceScope, *, job_id: str | None = None
    ) -> UsageJobStatus:
        """Explicitly recompute current estimates within the authorized bounded scope."""
        return await self._request("reprice", scope, job_id=job_id)

    async def request_backfill(
        self, scope: UsageMaintenanceScope, *, job_id: str | None = None
    ) -> UsageJobStatus:
        """Fill previously missing valuations from retained canonical observations."""
        return await self._request("backfill", scope, job_id=job_id)

    async def request_rebuild(
        self, scope: UsageMaintenanceScope, *, job_id: str | None = None
    ) -> UsageJobStatus:
        """Rebuild archive plus all retained facts; expose rebuilding, not partial totals."""
        return await self._request("rebuild", scope, job_id=job_id)

    async def request_archive(
        self, scope: UsageMaintenanceScope, *, job_id: str | None = None
    ) -> UsageJobStatus:
        """Explicit isolated/approved retention only; no normal policy activation here."""
        return await self._request("archive", scope, job_id=job_id)

    async def status(self, job_id: str) -> UsageJobStatus:
        """Read one durable job without resuming it implicitly."""
        async with self.sessions() as session:
            job = await session.get(UsageMaintenanceJobRow, job_id)
            if job is None:
                raise LookupError("Usage job not found")
            return self._status(job)

    async def resume(self, job_id: str) -> UsageJobStatus:
        """Manual non-polling recovery after an explicit operator decision."""
        async with projection_transaction(self.sessions) as session:
            job = await session.get(UsageMaintenanceJobRow, job_id, with_for_update=True)
            if job is None:
                raise LookupError("Usage job not found")
            if job.status in {"paused", "failed", "running"}:
                job.status = "queued"
                job.error_code = None
                job.completed_at = None
                if job.kind == "archive":
                    job.cursor = None
                    state = await lock_scope(
                        session,
                        job.scope["owner_key"],
                        date.fromisoformat(job.scope["bucket_day"]),
                        datetime.now(UTC),
                    )
                    state.retention_paused = False
            result = self._status(job)
            await notify_usage(session, self.controller_owner_id, job_id)
        self.projector.wake()
        return result

    async def correct(self, command: UsageCorrection) -> int:
        """Explicit CAS correction; canonical revision, audit and projections are atomic."""
        now = datetime.now(UTC)
        async with projection_transaction(self.sessions) as session:
            previous = await session.get(UsageCorrectionRow, command.correction_id)
            if previous is not None:
                if previous.content_digest != command.observation_digest():
                    raise UsageConflict("Correction identity differs")
                return previous.new_version
            row = await session.scalar(
                select(UsageAttemptRow)
                .where(UsageAttemptRow.attempt_id == command.replacement.context.trace.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise UsageDetailExpired("Usage detail expired or not found")
            state = await lock_scope(session, row.owner_key, row.bucket_day, now)
            if state.exclusive_job_id is not None:
                raise UsageMaintenanceBusy("Usage scope has active maintenance")
            if row.fact_version != command.expected_fact_version or row.terminal_payload is None:
                raise UsageConflict("Correction expected fact version differs")
            if row.context_digest != command.replacement.context.observation_digest():
                raise UsageConflict("Correction cannot replace attempt identity/attribution")
            prior = row.terminal_payload
            original_retention_anchor = row.finalized_at
            for name, value in terminal_values(command.replacement, now).items():
                setattr(row, name, value)
            row.finalized_at = original_retention_anchor
            row.fact_version += 1
            session.add(
                UsageCorrectionRow(
                    correction_id=command.correction_id,
                    attempt_id=row.attempt_id,
                    prior_version=command.expected_fact_version,
                    new_version=row.fact_version,
                    reason=command.reason,
                    content_digest=command.observation_digest(),
                    prior_terminal_payload=prior,
                    replacement_payload=row.terminal_payload,
                    recorded_at=now,
                )
            )
            await session.flush()
            await self.projector.project_attempts(
                session, [row], {(row.owner_key, row.bucket_day): state}, now
            )
            return row.fact_version

    @staticmethod
    def _filter(query: Any, model: Any, scope: dict[str, Any]) -> Any:
        for field, key in (
            ("owner_key", "owner_key"),
            ("bucket_day", "bucket_day"),
            ("provider_id", "provider_id"),
            ("model", "effective_model"),
        ):
            value = scope.get(field)
            if value is not None and hasattr(model, key):
                if field == "bucket_day":
                    value = date.fromisoformat(value)
                query = query.where(getattr(model, key) == value)
        return query

    async def _enqueue_traffic_backfill(self, session: AsyncSession, now: datetime) -> None:
        """Queue the next pre-traffic scope's rebuild under its deterministic job ID.

        A scope can stay below traffic version 2 after its job completed, for
        example when a controller without traffic support published that rebuild
        during a rolling upgrade. The deterministic job is then re-armed rather
        than left complete, which would otherwise block every later scope. A
        failed job needs diagnosis (`resume`), so its scope is skipped instead.
        """
        candidates = (
            await session.scalars(
                select(UsageProjectionStateRow)
                .where(
                    UsageProjectionStateRow.traffic_version < 2,
                    UsageProjectionStateRow.exclusive_job_id.is_(None),
                )
                .with_for_update(skip_locked=True)
                .order_by(UsageProjectionStateRow.owner_key, UsageProjectionStateRow.bucket_day)
                .limit(TRAFFIC_BACKFILL_CANDIDATES)
            )
        ).all()
        for state in candidates:
            payload = {"owner_key": state.owner_key, "bucket_day": state.bucket_day.isoformat()}
            job_id = f"utraffic_{digest_json(payload)}"
            existing = await session.get(UsageMaintenanceJobRow, job_id)
            if existing is not None and existing.status == "failed":
                continue
            job = await self._enqueue(session, "rebuild", payload, now, job_id=job_id)
            if job.status in ("complete", "superseded"):
                job.status = "queued"
                job.cursor = None
                job.processed_count = 0
                job.completed_at = None
                job.error_code = None
                job.updated_at = now
            return

    async def process_page(self) -> bool:
        """One bounded transaction, then yield to normal controller/inference work."""
        now = datetime.now(UTC)
        async with projection_transaction(self.sessions) as session:
            await self._refresh_policy(session, now)
            if self._expiry_due and self.retention.enabled:
                self._expiry_due = False
                pending = await session.scalar(
                    select(UsageMaintenanceJobRow.job_id)
                    .where(
                        UsageMaintenanceJobRow.kind == self._due_kind,
                        UsageMaintenanceJobRow.status.in_(["queued", "running"]),
                    )
                    .order_by(UsageMaintenanceJobRow.created_at, UsageMaintenanceJobRow.job_id)
                    .limit(1)
                )
                if pending is None:
                    await self._enqueue(
                        session,
                        self._due_kind,
                        self._due_scope.payload() if self._due_scope is not None else {},
                        now,
                    )
            await self._enqueue_traffic_backfill(session, now)
            job = await session.scalar(
                select(UsageMaintenanceJobRow)
                .where(UsageMaintenanceJobRow.status.in_(["queued", "running"]))
                .order_by(UsageMaintenanceJobRow.created_at, UsageMaintenanceJobRow.job_id)
                .limit(1)
                .with_for_update()
            )
            if job is None:
                return False
            session.info[PENDING_TRANSACTION_WORK] = True
            job.status = "running"
            job.owner_id = self.controller_owner_id
            job.owner_epoch += 1
            job.updated_at = now
            job_id, kind, scope = job.job_id, job.kind, dict(job.scope)
            try:
                async with session.begin_nested():
                    if job.kind == "rebuild":
                        await self._rebuild_page(session, job, now)
                    elif job.kind == "archive":
                        await self._archive_page(session, job, now)
                    elif job.kind == "cleanup":
                        await self._cleanup_page(session, job, now)
                    else:
                        await self._reprice_page(session, job, now)
            except (
                UsageArchiveRepriceUnsupported,
                UsageProjectionMismatch,
                UsageMaintenanceBusy,
            ) as exc:
                job = await session.get(UsageMaintenanceJobRow, job_id, populate_existing=True)
                assert job is not None
                job.status = "paused" if isinstance(exc, UsageMaintenanceBusy) else "failed"
                job.error_code = type(exc).__name__
                job.completed_at = now
                if kind == "archive":
                    state = await lock_scope(
                        session,
                        scope["owner_key"],
                        date.fromisoformat(scope["bucket_day"]),
                        now,
                    )
                    UsageDetailRetention.pause(job, state, now, type(exc).__name__)
            return True

    @property
    def page_rows(self) -> int:
        """Share the runner's adaptive bound without changing retention policy."""
        return min(self.retention.page_rows, self.projector.page_rows)

    async def _reprice_page(
        self, session: AsyncSession, job: UsageMaintenanceJobRow, now: datetime
    ) -> None:
        cursor = job.cursor or {"phase": "attempts", "row": 0}
        if cursor["phase"] == "attempts":
            query = (
                self._filter(
                    select(UsageAttemptRow).where(
                        UsageAttemptRow.row_id > cursor["row"],
                        UsageAttemptRow.fact_version > 0,
                    ),
                    UsageAttemptRow,
                    job.scope,
                )
                .order_by(UsageAttemptRow.row_id)
                .limit(self.page_rows)
            )
            rows = list((await session.scalars(query)).all())
            if rows:
                states = {
                    key: await lock_scope(session, *key, now)
                    for key in sorted({(row.owner_key, row.bucket_day) for row in rows})
                }
                if any(
                    state.exclusive_job_id not in (None, job.job_id) for state in states.values()
                ):
                    raise UsageMaintenanceBusy("Usage reprice conflicts with scoped maintenance")
                if job.kind == "backfill":
                    await self._bind_backfill(session, rows, job.job_id, now)
                await self.projector.project_attempts(session, rows, states, now)
                job.cursor = {"phase": "attempts", "row": rows[-1].row_id}
                job.processed_count += len(rows)
                return
            job.cursor = {"phase": "archives", "row": ""}
            return
        rows = list(
            (
                await session.scalars(
                    self._filter(
                        select(UsageArchivedTotalRow).where(
                            UsageArchivedTotalRow.archive_id > cursor["row"]
                        ),
                        UsageArchivedTotalRow,
                        job.scope,
                    )
                    .order_by(UsageArchivedTotalRow.archive_id)
                    .limit(self.page_rows)
                )
            ).all()
        )
        if rows:
            for row in rows:
                await self._project_archive(session, row, now)
            job.cursor = {"phase": "archives", "row": rows[-1].archive_id}
            job.processed_count += len(rows)
        else:
            job.status = "complete"
            job.completed_at = now

    async def _bind_backfill(
        self,
        session: AsyncSession,
        rows: list[UsageAttemptRow],
        job_id: str,
        now: datetime,
    ) -> None:
        """Persist the explicit effective-at decision, not a disposable receipt override."""
        candidate = (
            select(UsagePriceRevisionRow.revision_id)
            .where(
                UsagePriceRevisionRow.provider_id == UsageAttemptRow.provider_id,
                UsagePriceRevisionRow.model == UsageAttemptRow.effective_model,
                UsagePriceRevisionRow.effective_at <= UsageAttemptRow.started_at_observed,
            )
            .order_by(
                UsagePriceRevisionRow.effective_at.desc(),
                UsagePriceRevisionRow.recorded_at.desc(),
                UsagePriceRevisionRow.revision_id,
            )
            .limit(1)
            .correlate(UsageAttemptRow)
            .scalar_subquery()
        )
        selected = (
            await session.execute(
                select(UsageAttemptRow.attempt_id, candidate.label("revision_id"))
                .where(
                    UsageAttemptRow.attempt_id.in_([row.attempt_id for row in rows]),
                    UsageAttemptRow.captured_price_revision_id.is_(None),
                )
                .order_by(UsageAttemptRow.attempt_id)
            )
        ).all()
        values = [
            {
                "attempt_id": row.attempt_id,
                "revision_id": row.revision_id,
                "job_id": job_id,
                "recorded_at": now,
            }
            for row in selected
            if row.revision_id is not None
        ]
        if values:
            await session.execute(
                insert_for(session, UsageHistoricalPriceBindingRow.__table__)
                .values(values)
                .on_conflict_do_nothing(index_elements=["attempt_id"])
            )

    async def _rebuild_page(
        self, session: AsyncSession, job: UsageMaintenanceJobRow, now: datetime
    ) -> None:
        owner = job.scope["owner_key"]
        day = date.fromisoformat(job.scope["bucket_day"])
        state = await lock_scope(session, owner, day, now)
        if state.exclusive_job_id not in (None, job.job_id):
            raise UsageMaintenanceBusy("Usage scope has active maintenance")
        if (job.cursor or {}).get("phase") == "cleanup":
            for model in (
                UsageValuationRow,
                UsageSessionTotalRow,
                UsageHourlyTotalRow,
                UsageDailyTotalRow,
            ):
                table = model.__table__
                assert isinstance(table, Table)
                primary = list(table.primary_key)
                keys = (
                    (
                        await session.execute(
                            select(*primary)
                            .where(
                                table.c.owner_key == owner,
                                table.c.bucket_day == day,
                                table.c.generation < state.active_generation,
                            )
                            .order_by(*primary)
                            .limit(self.page_rows)
                        )
                    )
                    .tuples()
                    .all()
                )
                if keys:
                    await session.execute(delete(table).where(tuple_(*primary).in_(keys)))
                    return
            state.exclusive_job_id = None
            state.exclusive_job_kind = None
            state.retention_paused = False
            await refresh_detail_anchor(session, state)
            job.status = "complete"
            job.completed_at = now
            return
        if job.cursor is None:
            cut_row_id = (
                await session.scalar(
                    select(func.max(UsageAttemptRow.row_id)).where(
                        UsageAttemptRow.owner_key == owner,
                        UsageAttemptRow.bucket_day == day,
                    )
                )
                or 0
            )
            cut_outbox_id = (
                await session.scalar(select(func.max(UsageProjectionOutboxRow.row_id))) or 0
            )
            # Publish a complete state transition before the next ORM autoflush.
            state.exclusive_job_kind = "rebuild"
            state.exclusive_job_id = job.job_id
            state.ready = False
            state.building_generation = state.active_generation + 1
            state.source_cut_row_id = cut_row_id
            state.source_cut_outbox_id = cut_outbox_id
            state.build_job_id = job.job_id
            job.cursor = {"phase": "attempts", "row": 0}
            return
        generation = state.building_generation
        if generation is None:
            raise UsageConflict("Rebuild lost its fenced generation")
        cursor = job.cursor
        if cursor["phase"] == "attempts":
            rows = list(
                (
                    await session.scalars(
                        select(UsageAttemptRow)
                        .where(
                            UsageAttemptRow.owner_key == owner,
                            UsageAttemptRow.bucket_day == day,
                            UsageAttemptRow.row_id > cursor["row"],
                            UsageAttemptRow.row_id <= state.source_cut_row_id,
                        )
                        .order_by(UsageAttemptRow.row_id)
                        .limit(self.page_rows)
                    )
                ).all()
            )
            if rows:
                await project_request_headers(session, rows, now)
                await self.projector.project_attempts(
                    session, rows, {(owner, day): state}, now, generation=generation
                )
                job.cursor = {"phase": "attempts", "row": rows[-1].row_id}
                job.processed_count += len(rows)
            else:
                job.cursor = {"phase": "requests", "row": ""}
            return
        if cursor["phase"] == "requests":
            requests = list(
                (
                    await session.scalars(
                        select(UsageRequestStatsRow)
                        .where(
                            UsageRequestStatsRow.owner_key == owner,
                            UsageRequestStatsRow.bucket_day == day,
                            UsageRequestStatsRow.request_id > cursor["row"],
                        )
                        .order_by(UsageRequestStatsRow.request_id)
                        .limit(self.page_rows)
                    )
                ).all()
            )
            if requests:
                await project_request_headers(session, requests, now)
                await self.projector.project_requests(
                    session, requests, {(owner, day): state}, now, generation=generation
                )
                job.cursor = {"phase": "requests", "row": requests[-1].request_id}
                job.processed_count += len(requests)
            else:
                job.cursor = {"phase": "archives", "row": ""}
            return
        archives = list(
            (
                await session.scalars(
                    select(UsageArchivedTotalRow)
                    .where(
                        UsageArchivedTotalRow.owner_key == owner,
                        UsageArchivedTotalRow.bucket_day == day,
                        UsageArchivedTotalRow.archive_id > cursor["row"],
                    )
                    .order_by(UsageArchivedTotalRow.archive_id)
                    .limit(self.page_rows)
                )
            ).all()
        )
        if archives:
            for archive in archives:
                await self._project_archive(session, archive, now, generation=generation)
            job.cursor = {"phase": "archives", "row": archives[-1].archive_id}
            job.processed_count += len(archives)
            return
        # Publication is short. Later raw completions (including pre-cut prepared
        # rows) use the new active generation when their queued outbox is drained.
        state.active_generation = generation
        state.traffic_version = 2
        state.building_generation = None
        state.source_cut_row_id = None
        state.source_cut_outbox_id = None
        state.build_job_id = None
        state.ready = True
        state.capture_only = False
        state.updated_at = now
        job.cursor = {"phase": "cleanup"}

    async def _archive_page(
        self, session: AsyncSession, job: UsageMaintenanceJobRow, now: datetime
    ) -> None:
        owner = job.scope["owner_key"]
        day = date.fromisoformat(job.scope["bucket_day"])
        state = await lock_scope(session, owner, day, now)
        if state.exclusive_job_id not in (None, job.job_id):
            raise UsageMaintenanceBusy("Usage scope has active maintenance")
        if not self.retention.enabled:
            state.exclusive_job_id = None
            state.exclusive_job_kind = None
            job.status = "superseded"
            job.error_code = "retention_disabled"
            job.completed_at = now
            return
        state.exclusive_job_id = job.job_id
        state.exclusive_job_kind = "archive"
        await UsageDetailRetention(self.projector).process_page(
            session,
            job,
            state,
            now=now,
            detail_days=self.retention.detail_days,
            page_rows=self.page_rows,
            page_bytes=self.retention.page_bytes,
        )

    async def _project_archive(
        self,
        session: AsyncSession,
        row: UsageArchivedTotalRow,
        now: datetime,
        *,
        generation: int | None = None,
    ) -> None:
        state = await lock_scope(session, row.owner_key, row.bucket_day, now)
        key = (
            row.owner_key,
            row.bucket_day,
            generation or state.active_generation,
            "archive",
            row.archive_id,
        )
        pricing = (
            await session.get(UsagePricingStateRow, (row.provider_id, row.model))
            if row.provider_id and row.model
            else None
        )
        pricing_generation = pricing.generation if pricing else 0
        old = (await self.projector.receipts(session, [key])).get(key)
        if old is not None and old.pricing_generation == pricing_generation:
            return
        current_revision = pricing.current_revision_id if pricing else None
        definition_row = (
            await session.get(UsagePriceRevisionRow, current_revision) if current_revision else None
        )
        definition = (
            UsagePriceDefinition.model_validate(definition_row.definition)
            if definition_row
            else None
        )
        current_cost = row.current_cost
        current_status = (
            "complete"
            if row.current_priced_count == row.source_detail_count
            else "partial"
            if row.current_partial_count
            else "unpriced"
        )
        if row.basis == "provider_reported":
            # A reported monetary observation is immutable under tariff changes.
            definition = None
            current_revision = row.current_revision_id
        elif current_revision != row.current_revision_id:
            if definition is None:
                current_cost = Decimal(0)
                current_status = "unpriced"
            else:
                if row.currency is not None and row.currency != definition.currency:
                    raise UsageArchiveRepriceUnsupported("Archived pricing domain differs")
                if not row.exact_reprice_supported or not isinstance(
                    row.billing_units.get("unit_completeness"), bool
                ):
                    raise UsageArchiveRepriceUnsupported("Archived unit completeness is unknown")
                rule_version = row.billing_units.get("billing_rule_version", 1)
                if rule_version not in (1, 2, 3) or (
                    rule_version == 2 and not row.billing_units.get("service_tier_applicability")
                ):
                    raise UsageArchiveRepriceUnsupported("Archived applicability rule is unknown")
                if any(
                    rate.input_threshold != 0
                    or rate.per_million != rate.per_million.quantize(Decimal("0.000001"))
                    for rate in definition.rates
                ):
                    raise UsageArchiveRepriceUnsupported(
                        "Archived units do not support novel tier/rounding rules"
                    )
                archive_selection = UsageModelSelection(
                    provider_id=row.provider_id,
                    effective_model=row.model,
                    streaming=False,
                    source="adapter",
                    operation=row.billing_units.get("operation", "native"),
                    service_tier=row.billing_units.get("service_tier"),
                    service_tier_applicability=row.billing_units.get("service_tier_applicability"),
                    effective_parameters={
                        "service_tier": row.billing_units.get("effective_service_tier"),
                        **row.billing_units.get("media_parameters", {}),
                    },
                )
                eligible_rates = applicable_price_rates(
                    definition, row.billing_units.get("service_tier"), selection=archive_selection
                )
                if row.basis is not None and row.basis != definition.basis and eligible_rates:
                    raise UsageConflict("Archived overlapping tariffs cannot mix cost basis")
                current_cost = Decimal(0)
                complete = row.billing_units["unit_completeness"] and bool(eligible_rates)
                if definition.media_operations:
                    complete &= {r.dimension for r in eligible_rates} <= row.billing_units[
                        "units"
                    ].keys()
                priced = False
                for name, value in row.billing_units["units"].items():
                    if value:
                        rate = price_rate(
                            definition,
                            name,
                            service_tier=row.billing_units.get("service_tier"),
                            cache_ttl=row.billing_units.get("cache_ttl"),
                            input_tokens=None,
                            selection=archive_selection,
                        )
                        if rate is None:
                            complete = False
                        else:
                            priced = True
                            current_cost += Decimal(value) * rate.per_million / MILLION
                current_cost = current_cost.quantize(SCALE)
                current_status = "complete" if complete else "partial" if priced else "unpriced"
        measures = {name: getattr(row, name) for name in MEASURES}
        measures["current_cost"] = current_cost
        measures["current_priced_count"] = (
            row.source_detail_count if current_status == "complete" else 0
        )
        measures["current_partial_count"] = (
            row.source_detail_count if current_status == "partial" else 0
        )
        dimensions = dict(row.dimensions)
        if dimensions.get("schema_version", 1) == 1:
            service = dimensions.get("service_id")
            origin = (
                "api"
                if service in {"mnemory", "intaris"}
                else "cognis"
                if service == "cognis" or dimensions.get("agent_id")
                else "unknown"
            )
            dimensions.update(
                schema_version=2,
                traffic_origin=origin,
                traffic_auth_type="service" if origin == "api" else None,
                traffic_client_id=None,
                traffic_key_id=None,
            )
        payload = {
            "owner_key": row.owner_key,
            "day": row.bucket_day.isoformat(),
            "hour": 0,
            "grain": "daily",
            "dimensions": dimensions,
            "units": row.billing_units["units"],
            "measures": {
                name: str(value) if isinstance(value, Decimal) else value
                for name, value in measures.items()
            },
        }
        historical = UsagePriceResult(
            row.historical_cost
            if row.historical_priced_count or row.historical_partial_count
            else None,
            "complete"
            if row.historical_priced_count == row.source_detail_count
            else "partial"
            if row.historical_partial_count
            else "unpriced",
            row.currency,
            row.basis,
            (),
        )
        current = UsagePriceResult(
            current_cost if current_status != "unpriced" else None,
            current_status,
            definition.currency if definition else row.currency,
            definition.basis if definition else row.basis,
            (),
        )
        currency, basis = valuation_domain(historical, current)
        payload["dimensions"] = {**dimensions, "currency": currency, "basis": basis}
        receipt = self.projector.receipt_values(
            key,
            1,
            pricing_generation,
            row.historical_revision_id,
            current_revision,
            historical,
            current,
            payload,
            now,
        )
        deltas = [(old.contribution, -1)] if old else []
        deltas.append((payload, 1))
        await self.projector.apply_changes(session, {key[2]: deltas}, [receipt], now)

    async def on_idle(self) -> None:
        """Indexed control/history minima after work; no SQL is scheduled while idle."""
        if not self.retention.enabled:
            self.projector.schedule_expiry(None)
            return
        choices: list[tuple[datetime, str, UsageMaintenanceScope | None]] = []
        async with self.sessions() as session:
            row = await session.scalar(
                select(UsageProjectionStateRow)
                .where(
                    UsageProjectionStateRow.oldest_detail_at.is_not(None),
                    UsageProjectionStateRow.retention_paused.is_(False),
                    UsageProjectionStateRow.ready.is_(True),
                    UsageProjectionStateRow.exclusive_job_id.is_(None),
                )
                .order_by(
                    UsageProjectionStateRow.oldest_detail_at,
                    UsageProjectionStateRow.owner_key,
                    UsageProjectionStateRow.bucket_day,
                )
                .limit(1)
            )
            if row is not None and row.oldest_detail_at is not None:
                choices.append(
                    (
                        as_utc(row.oldest_detail_at) + timedelta(days=self.retention.detail_days),
                        "archive",
                        UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day),
                    )
                )
            for model, field, days in (
                (
                    ProviderUsageSampleRow,
                    ProviderUsageSampleRow.last_observed_at,
                    self.retention.quota_days,
                ),
                (
                    UsageProjectionOutboxRow,
                    UsageProjectionOutboxRow.completed_at,
                    self.retention.bookkeeping_days,
                ),
                (
                    UsageMaintenanceJobRow,
                    UsageMaintenanceJobRow.completed_at,
                    self.retention.bookkeeping_days,
                ),
            ):
                at = await session.scalar(
                    select(field)
                    .where(field.is_not(None), _retirable_history(model))
                    .order_by(field)
                    .limit(1)
                )
                if at is not None:
                    choices.append((as_utc(at) + timedelta(days=days), "cleanup", None))
        if not choices:
            self.projector.schedule_expiry(None)
            self._expiry_scope = None
            return
        expiry, self._expiry_kind, self._expiry_scope = min(choices, key=lambda item: item[0])
        self.projector.schedule_expiry(expiry)

    def expiry_due(self) -> None:
        """Called by the projector's single next-known-expiry timer."""
        self._expiry_due = True
        self._due_scope, self._due_kind = self._expiry_scope, self._expiry_kind

    async def _cleanup_page(
        self,
        session: AsyncSession,
        job: UsageMaintenanceJobRow,
        now: datetime,
    ) -> None:
        """Bound each control/history deletion and never remove pending outbox work."""
        if not self.retention.enabled:
            job.status, job.completed_at = "superseded", now
            job.error_code = "retention_disabled"
            return
        cursor = job.cursor or {"phase": job.scope.get("phase", "history")}
        if cursor["phase"] == "anchors":
            query = (
                select(UsageProjectionStateRow)
                .order_by(UsageProjectionStateRow.owner_key, UsageProjectionStateRow.bucket_day)
                .limit(self.page_rows)
            )
            if cursor.get("owner"):
                query = query.where(
                    tuple_(UsageProjectionStateRow.owner_key, UsageProjectionStateRow.bucket_day)
                    > (cursor["owner"], date.fromisoformat(cursor["day"]))
                )
            states = (await session.scalars(query)).all()
            if states:
                for state in states:
                    await refresh_detail_anchor(session, state)
                job.cursor = {
                    "phase": "anchors",
                    "owner": states[-1].owner_key,
                    "day": states[-1].bucket_day.isoformat(),
                }
                return
            job.cursor = {"phase": "history"}
            return
        for model, field, identifier, days in (
            (
                ProviderUsageSampleRow,
                ProviderUsageSampleRow.last_observed_at,
                ProviderUsageSampleRow.sample_id,
                self.retention.quota_days,
            ),
            (
                UsageProjectionOutboxRow,
                UsageProjectionOutboxRow.completed_at,
                UsageProjectionOutboxRow.row_id,
                self.retention.bookkeeping_days,
            ),
            (
                UsageMaintenanceJobRow,
                UsageMaintenanceJobRow.completed_at,
                UsageMaintenanceJobRow.job_id,
                self.retention.bookkeeping_days,
            ),
        ):
            ids = (
                await session.scalars(
                    select(identifier)
                    .where(field <= now - timedelta(days=days), _retirable_history(model))
                    .order_by(field, identifier)
                    .limit(self.page_rows)
                )
            ).all()
            if ids:
                await session.execute(
                    delete(model).where(
                        identifier.in_(ids),
                        field <= now - timedelta(days=days),
                        _retirable_history(model),
                    )
                )
                job.processed_count += len(ids)
                return
        job.status, job.completed_at = "complete", now
