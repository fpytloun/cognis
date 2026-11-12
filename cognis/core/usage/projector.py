"""Separate event-driven, resource-bounded usage projection owner.

Raw capture never takes these fences. Projection, repair and compaction serialize
their short pages under one DB-authoritative maintenance boundary. Receipts and
summary deltas commit together. Failure leaves the durable outbox pending.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import delete, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.usage.contributions import (
    MEASURES,
    RESOURCE_FIELDS,
    attempt_contribution,
    digest_json,
    empty_measures,
    summary_rows,
)
from cognis.core.usage.dimensions import UsageDimensionGrain, UsageDimensions, request_metric_values
from cognis.core.usage.pricing import UsagePriceResult
from cognis.core.usage.request_headers import project_request_headers
from cognis.core.usage.sessions import session_correlation
from cognis.models.usage import UsageAttribution, UsagePriceDefinition, UsageRequestStats
from cognis.models.usage_traffic import traffic_dimensions
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageHistoricalPriceBindingRow,
    UsageHourlyTotalRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageProjectionOutboxRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from cognis.store.usage.transactions import insert_for, sqlite_write_boundary

logger = logging.getLogger(__name__)
PENDING_TRANSACTION_WORK = "usage_pending_transaction_work"
TIMEOUT_BACKOFF_SECONDS = (0.05, 0.2)
SUMMARY_MODELS: tuple[
    tuple[
        UsageDimensionGrain,
        type[UsageSessionTotalRow] | type[UsageHourlyTotalRow] | type[UsageDailyTotalRow],
    ],
    ...,
] = (
    ("session", UsageSessionTotalRow),
    ("hourly", UsageHourlyTotalRow),
    ("daily", UsageDailyTotalRow),
)


class UsageMaintenanceParticipant(Protocol):
    """The projector owns scheduling; maintenance owns bounded transactional pages."""

    async def process_page(self) -> bool: ...

    async def on_idle(self) -> None: ...

    def expiry_due(self) -> None: ...


async def maintenance_boundary(session: AsyncSession) -> bool:
    """Transaction lease; a waiting controller must not consume its only wakeup."""
    await sqlite_write_boundary(session)
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text(
                "SELECT set_config('statement_timeout', '8s', true), "
                "set_config('idle_in_transaction_session_timeout', '2s', true)"
            )
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('cognis.usage.projection.v1'))")
        )
        await session.execute(text("SELECT set_config('statement_timeout', '2s', true)"))
    return True


@asynccontextmanager
async def projection_transaction(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Bound work after acquiring the DB lease; all side effects finish in this transaction."""
    async with sessions.begin() as session:
        await maintenance_boundary(session)
        deadline = asyncio.timeout(0.5)
        try:
            async with deadline:
                yield session
        except TimeoutError:
            if deadline.expired() and session.info.get(PENDING_TRANSACTION_WORK):
                raise UsagePendingTransactionTimeout from None
            raise


class UsagePendingTransactionTimeout(TimeoutError):
    """Our page deadline expired after selecting known durable pending work."""


async def lock_scope(
    session: AsyncSession,
    owner_key: str,
    day: date,
    now: datetime,
) -> UsageProjectionStateRow:
    """A durable owner/day control row; never touched by the raw writer."""
    statement = insert_for(session, UsageProjectionStateRow.__table__).values(
        owner_key=owner_key,
        bucket_day=day,
        active_generation=1,
        traffic_version=2,
        ready=True,
        capture_only=False,
        archive_revision=0,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=["owner_key", "bucket_day"])
    )
    return (
        await session.scalars(
            select(UsageProjectionStateRow)
            .where(
                UsageProjectionStateRow.owner_key == owner_key,
                UsageProjectionStateRow.bucket_day == day,
            )
            .with_for_update()
        )
    ).one()


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive values for timestamps which this store writes in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def record_detail_anchor(state: UsageProjectionStateRow, at: datetime | None) -> None:
    """Projection, not raw capture, owns the scope's indexed next-expiry hint."""
    if at is not None and (
        state.oldest_detail_at is None or as_utc(at) < as_utc(state.oldest_detail_at)
    ):
        state.oldest_detail_at = at


@dataclass(frozen=True)
class UsageProjectionDiagnostics:
    """Content-free local health; persisted pending work survives a paused runner."""

    running: bool
    failed: bool
    paused_error_code: str | None
    timeout_retries: int = 0
    retrying: bool = False
    retry_exhausted: bool = False


class UsageProjectionMismatch(ValueError):
    """Derived bookkeeping cannot authorize deletion of different canonical facts."""


class UsageProjector:
    """One local low-priority runner, woken by commit/start/reconnect/manual recovery."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        controller_owner_id: str,
    ) -> None:
        self.sessions = session_factory
        self.controller_owner_id = controller_owner_id
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._maintenance: UsageMaintenanceParticipant | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._closing = False
        self._failed = False
        self._paused_error_code: str | None = None
        self._page_rows = 4
        self._timeout_retries = 0
        self._retrying = False
        self._retry_exhausted = False

    @property
    def page_rows(self) -> int:
        """Adapt down after a deadline; never enlarge the transaction budget."""
        return self._page_rows

    @property
    def diagnostics(self) -> UsageProjectionDiagnostics:
        """Expose a dead/paused projector rather than successful catch-up."""
        return UsageProjectionDiagnostics(
            self._task is not None and not self._task.done(),
            self._failed,
            self._paused_error_code,
            self._timeout_retries,
            self._retrying,
            self._retry_exhausted,
        )

    def schedule_expiry(self, at: datetime | None) -> None:
        """One actual known expiry, never a periodic scanner."""
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        if at is not None and not self._closing:
            self._expiry_timer = asyncio.get_running_loop().call_later(
                max(0, (at - datetime.now(UTC)).total_seconds()), self._expiry_ready
            )

    def _expiry_ready(self) -> None:
        self._expiry_timer = None
        if self._maintenance is not None:
            self._maintenance.expiry_due()
        self.wake()

    def attach_maintenance(self, maintenance: UsageMaintenanceParticipant) -> None:
        """Public composition hook; one runner also owns maintenance/expiry wakes."""
        if self._task is not None or self._maintenance is not None:
            raise RuntimeError("Usage maintenance must be attached once before start")
        self._maintenance = maintenance

    def wake(self) -> None:
        """No SQL, polling task or recurring timer is scheduled by this signal."""
        if self._closing:
            return
        self._idle.clear()
        self._wake.set()

    async def start(self) -> None:
        """Startup performs one indexed catch-up through the normal runner."""
        if self._closing:
            raise RuntimeError("Usage projector is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-projector")
            self._task.add_done_callback(self._runner_done)
            self.wake()

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        error = None if task.cancelled() else task.exception()
        if error is not None or not self._closing:
            self._failed = True
            self._paused_error_code = type(error).__name__ if error else "UnexpectedStop"
            logger.warning("Usage projector stopped: %s", self._paused_error_code)
        self._idle.set()

    async def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        """Explicit management/test barrier, outside the inference path."""
        self.wake()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout_seconds)
        return self._idle.is_set() and not self._failed and self._paused_error_code is None

    async def aclose(self) -> None:
        """Leave pending outbox/jobs durable; cancel no other application task."""
        self._closing = True
        self.schedule_expiry(None)
        if self._task is not None:
            self._task.cancel()
            done, _ = await asyncio.wait({self._task}, timeout=5)
            if done:
                self._task = None
            else:
                self._failed = True
                self._paused_error_code = "ShutdownTimeout"
                logger.warning("Usage projector shutdown paused: ShutdownTimeout")

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            self._paused_error_code = None
            self._retry_exhausted = False
            retries = 0
            try:
                while True:
                    try:
                        maintained = False
                        if self._maintenance is not None:
                            maintained = await self._maintenance.process_page()
                        projected = await self.process_page()
                        if not maintained and not projected:
                            if self._maintenance is not None:
                                await self._maintenance.on_idle()
                            break
                    except UsagePendingTransactionTimeout:
                        if retries == len(TIMEOUT_BACKOFF_SECONDS):
                            self._retry_exhausted = True
                            raise
                        self._page_rows = max(1, self._page_rows // 2)
                        self._timeout_retries += 1
                        self._retrying = True
                        try:
                            # Transaction/context rollback has finished before this wait.
                            await asyncio.sleep(TIMEOUT_BACKOFF_SECONDS[retries])
                        finally:
                            self._retrying = False
                        retries += 1
                    await asyncio.sleep(0)
            except (SQLAlchemyError, ValueError, TimeoutError) as exc:
                self._paused_error_code = type(exc).__name__
                logger.warning("Usage projection paused: %s", type(exc).__name__)
                # Exhaustion/permanent failures require a new external recovery signal.
            self._idle.set()

    async def process_page(self) -> bool:
        """Apply at most four refs; raw outboxes remain bounded to 64 refs."""
        now = datetime.now(UTC)
        async with projection_transaction(self.sessions) as session:
            outbox = await session.scalar(
                select(UsageProjectionOutboxRow)
                .where(UsageProjectionOutboxRow.completed_at.is_(None))
                .order_by(UsageProjectionOutboxRow.row_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if outbox is None:
                return False
            session.info[PENDING_TRANSACTION_WORK] = True
            if len(outbox.item_refs) > 64:
                raise ValueError("Usage outbox exceeds the bounded capture contract")
            refs = outbox.item_refs[outbox.cursor : outbox.cursor + self.page_rows]
            outbox.claimed_by = self.controller_owner_id
            outbox.claim_epoch += 1
            attempt_ids = sorted({ref["id"] for ref in refs if ref["kind"] == "attempt"})
            request_ids = sorted({ref["id"] for ref in refs if ref["kind"] == "request"})
            attempts = (
                list(
                    (
                        await session.scalars(
                            select(UsageAttemptRow)
                            .where(UsageAttemptRow.attempt_id.in_(attempt_ids))
                            .order_by(UsageAttemptRow.attempt_id)
                        )
                    ).all()
                )
                if attempt_ids
                else []
            )
            requests = (
                list(
                    (
                        await session.scalars(
                            select(UsageRequestStatsRow)
                            .where(UsageRequestStatsRow.request_id.in_(request_ids))
                            .order_by(UsageRequestStatsRow.request_id)
                        )
                    ).all()
                )
                if request_ids
                else []
            )
            scopes = sorted(
                {(row.owner_key, row.bucket_day) for row in attempts}
                | {(row.owner_key, row.bucket_day) for row in requests}
            )
            states = {key: await lock_scope(session, *key, now) for key in scopes}
            if any(
                state.exclusive_job_id is not None or not state.ready for state in states.values()
            ):
                return False
            await self.project_attempts(session, attempts, states, now)
            await self.project_requests(session, requests, states, now)
            await project_request_headers(session, [*attempts, *requests], now)
            outbox.cursor += len(refs)
            if outbox.cursor == len(outbox.item_refs):
                outbox.completed_at = now
            return True

    async def project_attempts(
        self,
        session: AsyncSession,
        rows: list[UsageAttemptRow],
        states: dict[tuple[str, date], UsageProjectionStateRow],
        now: datetime,
        *,
        generation: int | None = None,
        verify_receipts: bool = False,
    ) -> None:
        """Each receipt version fences replay, correction and rebuilt baselines."""
        if not rows:
            return
        rows = list(
            (
                await session.scalars(
                    select(UsageAttemptRow)
                    .where(
                        UsageAttemptRow.attempt_id.in_([row.attempt_id for row in rows]),
                        UsageAttemptRow.fact_version > 0,
                    )
                    .order_by(UsageAttemptRow.attempt_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not rows:
            return
        # Global maintenance boundary serializes all pricing/projector mutations.
        keys = sorted({(row.provider_id, row.effective_model) for row in rows})
        pricing = {
            (row.provider_id, row.model): row
            for row in (
                await session.scalars(
                    select(UsagePricingStateRow)
                    .where(
                        tuple_(UsagePricingStateRow.provider_id, UsagePricingStateRow.model).in_(
                            keys
                        )
                    )
                    .order_by(UsagePricingStateRow.provider_id, UsagePricingStateRow.model)
                    .with_for_update(read=True)
                )
            ).all()
        }
        bindings = {
            row.attempt_id: row.revision_id
            for row in (
                await session.scalars(
                    select(UsageHistoricalPriceBindingRow).where(
                        UsageHistoricalPriceBindingRow.attempt_id.in_(
                            [item.attempt_id for item in rows]
                        )
                    )
                )
            ).all()
        }
        revision_ids = {
            row.captured_price_revision_id or bindings.get(row.attempt_id) for row in rows
        }
        revision_ids.update(state.current_revision_id for state in pricing.values())
        definitions = {
            row.revision_id: UsagePriceDefinition.model_validate(row.definition)
            for row in (
                await session.scalars(
                    select(UsagePriceRevisionRow).where(
                        UsagePriceRevisionRow.revision_id.in_([key for key in revision_ids if key])
                    )
                )
            ).all()
        }
        receipt_keys = [
            (
                row.owner_key,
                row.bucket_day,
                generation or states[row.owner_key, row.bucket_day].active_generation,
                "attempt",
                row.attempt_id,
            )
            for row in rows
        ]
        existing = await self.receipts(session, receipt_keys)
        if verify_receipts:
            definitions.update(
                {
                    definition.revision_id: UsagePriceDefinition.model_validate(
                        definition.definition
                    )
                    for definition in (
                        await session.scalars(
                            select(UsagePriceRevisionRow).where(
                                UsagePriceRevisionRow.revision_id.in_(
                                    [
                                        receipt.current_revision_id
                                        for receipt in existing.values()
                                        if receipt.current_revision_id
                                    ]
                                )
                            )
                        )
                    ).all()
                }
            )
        changes: dict[int, list[tuple[dict[str, Any], int]]] = {}
        receipts = []
        for row, key in zip(rows, receipt_keys, strict=True):
            state = pricing.get((row.provider_id, row.effective_model))
            current_generation = state.generation if state else 0
            old = existing.get(key)
            record_detail_anchor(states[row.owner_key, row.bucket_day], row.finalized_at)
            if (
                not verify_receipts
                and old is not None
                and (
                    old.fact_version == row.fact_version
                    and old.pricing_generation == current_generation
                    and old.historical_revision_id
                    == (row.captured_price_revision_id or bindings.get(row.attempt_id))
                )
            ):
                row.projected_version = row.fact_version
                continue
            historical = definitions.get(
                row.captured_price_revision_id or bindings.get(row.attempt_id) or ""
            )
            current_id = (
                old.current_revision_id
                if verify_receipts and old is not None
                else state.current_revision_id
                if state
                else None
            )
            current = definitions.get(current_id or "")
            contribution = attempt_contribution(row, historical, current)
            payload = contribution.payload()
            if verify_receipts:
                if (
                    old is None
                    or old.fact_version != row.fact_version
                    or old.historical_revision_id
                    != (historical.revision_id if historical else None)
                    or old.current_revision_id != (current.revision_id if current else None)
                    or old.contribution != payload
                ):
                    raise UsageProjectionMismatch(
                        "Archive requires a canonical-equivalent active receipt"
                    )
                continue
            deltas = changes.setdefault(key[2], [])
            if old is not None:
                deltas.append((old.contribution, -1))
            deltas.append((payload, 1))
            receipts.append(
                self.receipt_values(
                    key,
                    row.fact_version,
                    current_generation,
                    historical.revision_id if historical else None,
                    current.revision_id if current else None,
                    contribution.historical,
                    contribution.current,
                    payload,
                    now,
                )
            )
            row.projected_version = row.fact_version
        await self.apply_changes(session, changes, receipts, now)

    async def project_requests(
        self,
        session: AsyncSession,
        rows: list[UsageRequestStatsRow],
        states: dict[tuple[str, date], UsageProjectionStateRow],
        now: datetime,
        *,
        generation: int | None = None,
        verify_receipts: bool = False,
    ) -> None:
        """Logical outcomes have their own receipt and denominator, never attempts."""
        if not rows:
            return
        rows = list(
            (
                await session.scalars(
                    select(UsageRequestStatsRow)
                    .where(UsageRequestStatsRow.request_id.in_([row.request_id for row in rows]))
                    .order_by(UsageRequestStatsRow.request_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        keys = [
            (
                row.owner_key,
                row.bucket_day,
                generation or states[row.owner_key, row.bucket_day].active_generation,
                "request",
                row.request_id,
            )
            for row in rows
        ]
        existing = await self.receipts(session, keys)
        changes: dict[int, list[tuple[dict[str, Any], int]]] = {}
        receipts = []
        for row, key in zip(rows, keys, strict=True):
            old = existing.get(key)
            if row.logical_outcome is not None:
                record_detail_anchor(states[row.owner_key, row.bucket_day], row.updated_at)
            if not verify_receipts and old is not None and old.fact_version == row.fact_version:
                row.projected_version = row.fact_version
                continue
            attribution = row.attribution
            identity = attribution["identity"]
            resources = attribution.get("resources") or {}
            hook = attribution.get("hook") or {}
            dimensions = {
                "schema_version": 2,
                **traffic_dimensions(UsageAttribution.model_validate(attribution)),
                "usage_subject_key": row.usage_subject_key,
                "resource_owner": identity.get("resource_owner"),
                "project_id": resources.get("project_id"),
                "agent_id": resources.get("agent_id"),
                "agent_profile_id": resources.get("agent_profile_id"),
                "provider_id": None,
                "effective_model": None,
                "effective_effort": None,
                "purpose": attribution["purpose"],
                "operation": "logical_request",
                "service_id": identity.get("service_id"),
                "service_operation": identity.get("service_operation"),
                "hook_id": hook.get("hook_id"),
                "hook_type": hook.get("hook_type"),
                "hook_revision": hook.get("hook_revision"),
                "hook_name": hook.get("hook_name"),
                "streaming": None,
                "source": None,
                "currency": None,
                "basis": None,
                "record_kind": "request",
                **{key: resources.get(key) for key in RESOURCE_FIELDS},
            }
            measures = empty_measures()
            dimensions = UsageDimensions.model_validate(dimensions).model_dump(mode="json")
            metrics = request_metric_values(
                server=UsageRequestStats.model_validate(
                    {**row.server_payload, "attribution": row.attribution}
                )
                if row.server_payload
                else None,
                client=UsageRequestStats.model_validate(
                    {**row.client_payload, "attribution": row.attribution}
                )
                if row.client_payload
                else None,
            )
            for name, value in metrics.model_dump().items():
                measures[name] = Decimal(str(value)) if name.endswith("_sum") else value
            payload = {
                "owner_key": row.owner_key,
                "day": row.bucket_day.isoformat(),
                "hour": row.request_started_at.hour,
                "dimensions": dimensions,
                "measures": {
                    name: str(value) if isinstance(value, Decimal) else value
                    for name, value in measures.items()
                },
                "units": {},
            }
            correlation = session_correlation(UsageAttribution.model_validate(row.attribution))
            if correlation is not None:
                payload["session"] = correlation.model_dump(mode="json")
            if verify_receipts:
                if (
                    old is None
                    or old.fact_version != row.fact_version
                    or old.contribution != payload
                ):
                    raise UsageProjectionMismatch(
                        "Archive requires a canonical-equivalent active receipt"
                    )
                continue
            deltas = changes.setdefault(key[2], [])
            if old is not None:
                deltas.append((old.contribution, -1))
            deltas.append((payload, 1))
            unknown = UsagePriceResult(None, "not_applicable", None, None, ())
            receipts.append(
                self.receipt_values(
                    key, row.fact_version, 0, None, None, unknown, unknown, payload, now
                )
            )
            row.projected_version = row.fact_version
        await self.apply_changes(session, changes, receipts, now)

    @staticmethod
    async def receipts(
        session: AsyncSession,
        keys: list[tuple[str, date, int, str, str]],
    ) -> dict[tuple[str, date, int, str, str], UsageValuationRow]:
        if not keys:
            return {}
        rows = (
            await session.scalars(
                select(UsageValuationRow)
                .where(
                    tuple_(
                        UsageValuationRow.owner_key,
                        UsageValuationRow.bucket_day,
                        UsageValuationRow.generation,
                        UsageValuationRow.source_kind,
                        UsageValuationRow.source_id,
                    ).in_(keys)
                )
                .with_for_update()
            )
        ).all()
        return {
            (row.owner_key, row.bucket_day, row.generation, row.source_kind, row.source_id): row
            for row in rows
        }

    @staticmethod
    def receipt_values(
        key: tuple[str, date, int, str, str],
        fact_version: int,
        pricing_generation: int,
        historical_revision: str | None,
        current_revision: str | None,
        historical: UsagePriceResult,
        current: UsagePriceResult,
        payload: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "owner_key": key[0],
            "bucket_day": key[1],
            "generation": key[2],
            "source_kind": key[3],
            "source_id": key[4],
            "fact_version": fact_version,
            "calculator_version": 1,
            "pricing_generation": pricing_generation,
            "historical_revision_id": historical_revision,
            "current_revision_id": current_revision,
            "historical_cost": historical.cost,
            "current_cost": current.cost,
            "historical_status": historical.status,
            "current_status": current.status,
            "currency": payload["dimensions"].get("currency"),
            "basis": payload["dimensions"].get("basis"),
            "contribution_digest": digest_json(payload),
            "contribution": payload,
            "updated_at": now,
        }

    async def apply_changes(
        self,
        session: AsyncSession,
        changes: dict[int, list[tuple[dict[str, Any], int]]],
        receipts: list[dict[str, Any]],
        now: datetime,
        *,
        grains: tuple[UsageDimensionGrain, ...] = ("session", "hourly", "daily"),
    ) -> None:
        for generation, deltas in changes.items():
            # Disposable receipts can retain pre-compaction hour parts; canonical
            # archived evidence itself is strictly UTC-day grain.
            deltas = [
                (part, weight)
                for payload, weight in deltas
                for part in payload.get("parts", [payload])
            ]
            for grain, model in SUMMARY_MODELS:
                if grain not in grains:
                    continue
                rows = summary_rows(deltas, generation=generation, grain=grain, now=now)
                if not rows:
                    continue
                table = model.__table__
                rows = [
                    {key: value for key, value in row.items() if key in table.columns}
                    for row in rows
                ]
                # Verify hash-key content set-wise before applying any additive delta.
                primary = [column.name for column in table.primary_key]
                keys = [tuple(row[name] for name in primary) for row in rows]
                checked = [*primary, "dimensions"]
                if grain == "session":
                    checked.extend(("session_kind", "client_id", "client_session_id"))
                stored = (
                    (
                        await session.execute(
                            select(*(getattr(model, name) for name in checked)).where(
                                tuple_(*(getattr(model, name) for name in primary)).in_(keys)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                expected = {tuple(row[name] for name in primary): row for row in rows}
                if any(
                    row["dimensions"]
                    != expected[tuple(row[name] for name in primary)]["dimensions"]
                    or (
                        grain == "session"
                        and any(
                            row[name] != expected[tuple(row[field] for field in primary)][name]
                            for name in ("session_kind", "client_id", "client_session_id")
                        )
                    )
                    for row in stored
                ):
                    raise ValueError("Usage summary dimension hash conflict")
                statement = insert_for(session, table).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[column.name for column in table.primary_key],
                        set_={
                            **{
                                name: table.c[name] + getattr(statement.excluded, name)
                                for name in MEASURES
                            },
                            "updated_at": now,
                        },
                    )
                )
                # Population counts, not rounded duration/money residues, prove
                # emptiness. Failed/cancelled and client-only requests count too.
                await session.execute(
                    delete(model).where(
                        tuple_(*(getattr(model, name) for name in primary)).in_(keys),
                        *(
                            getattr(model, name) == 0
                            for name in MEASURES
                            if name.endswith("_count")
                        ),
                    )
                )
        if receipts:
            table = UsageValuationRow.__table__
            statement = insert_for(session, table).values(receipts)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[column.name for column in table.primary_key],
                    set_={
                        column.name: getattr(statement.excluded, column.name)
                        for column in table.columns
                        if not column.primary_key
                    },
                )
            )
