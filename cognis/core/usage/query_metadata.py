"""Usage source capabilities and scope-safe projection/retention metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.usage.access import AuthorizedUsageScope
from cognis.core.usage.query_models import (
    UsageAvailability,
    UsageCaptureCoverage,
    UsageMeta,
    UsageProjectionCoverage,
    UsageScope,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.models.usage import UsageResourceBinding
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageCoverageEpochRow,
    UsageHourlyTotalRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
)
from cognis.store.usage_queries import scope_predicate


def utc(value: datetime) -> datetime:
    """SQLite returns naive persisted UTC timestamps; never use host local time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class UsageIndexCapabilities:
    """Observed schema indexes, not caller-supplied permission/capability flags."""

    prefixes: dict[str, tuple[tuple[str, ...], ...]]

    def supports(self, table: str, prefix: tuple[str, ...]) -> bool:
        return any(columns[: len(prefix)] == prefix for columns in self.prefixes.get(table, ()))

    @classmethod
    async def discover(cls, session: AsyncSession) -> UsageIndexCapabilities:
        """Inspect once at composition time; no result/authorization caching."""
        connection = await session.connection()

        def read(sync: Any) -> dict[str, tuple[tuple[str, ...], ...]]:
            inspector = inspect(sync)
            return {
                table: tuple(tuple(item["column_names"]) for item in inspector.get_indexes(table))
                for table in (
                    "usage_attempts",
                    "usage_daily_totals",
                    "usage_hourly_totals",
                    "usage_session_totals",
                    "usage_projection_state",
                    "usage_request_stats",
                )
            }

        return cls(await connection.run_sync(read))

    def require_admin(self, scope: AuthorizedUsageScope, table: str) -> None:
        """Missing time-leading global plans are unavailable, never raw fallbacks."""
        if scope.mode != "admin":
            return
        prefix = (
            ("started_at_observed", "row_id")
            if table == "usage_attempts"
            else ("request_started_at", "request_id")
            if table == "usage_request_stats"
            else ("bucket_day",)
        )
        if not self.supports(table, prefix):
            raise UsageQueryError(
                "usage_index_unavailable",
                "Global usage requires the qualified time-leading index",
                503,
            )


def active_projection(source: Any) -> Any:
    """One active generation per opaque owner/day, shared with all read shapes."""
    state = UsageProjectionStateRow
    return and_(
        state.owner_key == source.owner_key,
        state.bucket_day == source.bucket_day,
        state.active_generation == source.generation,
    )


async def source_meta(
    session: AsyncSession,
    scope: AuthorizedUsageScope,
    *,
    start: datetime | None,
    end: datetime | None,
    precision: str,
    source: Any = UsageHourlyTotalRow,
    include_archives: bool = False,
) -> UsageMeta:
    """Read bounded authorized metadata; never infer readiness from missing rows."""
    state = UsageProjectionStateRow
    predicates = [scope_predicate(source, scope), active_projection(source)]
    dates = []
    if start is not None:
        dates.append(state.bucket_day >= utc(start).date())
    if end is not None:
        dates.append(state.bucket_day <= (utc(end) - timedelta(microseconds=1)).date())
    published = select(1).select_from(source).where(*predicates).correlate(state).exists()
    witnesses = [published]
    if include_archives:
        archive = UsageArchivedTotalRow
        # This witness reveals only an authorized retention cutoff, not archived
        # resource totals. Owner AND day correlation prevents cross-day readiness.
        witnesses.append(
            select(1)
            .where(
                archive.owner_key == state.owner_key,
                archive.bucket_day == state.bucket_day,
                scope_predicate(archive, scope),
            )
            .correlate(state)
            .exists()
        )
    fact = UsageAttemptRow
    request = UsageRequestStatsRow
    # Lift only the public typed resource contract, not arbitrary payload keys.
    resources = request.attribution["resources"]
    request_scope = select(
        request.owner_key,
        request.bucket_day,
        request.usage_subject_key,
        request.request_started_at.label("started_at_observed"),
        *(
            resources[name]
            .as_string()
            .label("binding_provenance" if name == "provenance" else name)
            for name in UsageResourceBinding.model_fields
        ),
    ).subquery()

    def captured(candidate: Any) -> Any:
        terms = [
            candidate.owner_key == state.owner_key,
            candidate.bucket_day == state.bucket_day,
            scope_predicate(candidate, scope),
        ]
        if start is not None:
            terms.append(candidate.started_at_observed >= utc(start))
        if end is not None:
            terms.append(candidate.started_at_observed < utc(end))
        return select(1).where(*terms).correlate(state).exists()

    rows = (
        await session.scalars(
            select(state)
            .where(*dates, or_(*witnesses, captured(fact), captured(request_scope.c)))
            .limit(1001)
        )
    ).all()
    if len(rows) > 1000:
        raise UsageQueryError("query_budget_exceeded", "Projection scope exceeds 1000 owner-days")
    ready = bool(rows) and all(
        row.ready and not row.capture_only and row.building_generation is None for row in rows
    )
    cutoffs = [utc(row.detail_expired_before) for row in rows if row.detail_expired_before]
    cutoff = max(cutoffs) if cutoffs else None
    availability = UsageAvailability(
        resolution=("mixed" if cutoff and (start is None or utc(start) < cutoff) else "retained")
        if rows
        else "unavailable",
        detail_retention_cutoff=cutoff,
        archive_grain="day" if cutoff else None,
        historical_repricing="unknown",
    )
    # Epoch loss diagnostics are global, not attributable to a personal/project scope.
    capture = UsageCaptureCoverage()
    if scope.mode == "admin" and start is not None and end is not None:
        epochs = (
            await session.scalars(
                select(UsageCoverageEpochRow)
                .where(
                    UsageCoverageEpochRow.started_at < end,
                    (
                        UsageCoverageEpochRow.ended_at.is_(None)
                        | (UsageCoverageEpochRow.ended_at >= start)
                    ),
                )
                .limit(1001)
            )
        ).all()
        if 0 < len(epochs) <= 1000:
            losses = sum(
                row.dropped_started + row.dropped_finalized + row.dropped_stats for row in epochs
            )
            capture = UsageCaptureCoverage(
                state="gaps" if losses else "unknown",
                dropped_observations=losses,
                gap_incidents=None,
            )
    return UsageMeta(
        scope=UsageScope(mode=scope.mode, project_id=scope.project_id),
        start=start,
        end=end,
        precision=precision,
        complete=False,
        capture=capture,
        projection=UsageProjectionCoverage(
            state="ready" if ready else ("building" if rows else "unavailable"),
            projected_through=min(utc(row.updated_at) for row in rows) if rows else None,
        ),
        availability=availability,
        accounting_degraded=not ready,
        private_resources_excluded=scope.mode == "project",
        excluded_fields=["key_id", "client_id", "client_request_id", "provider_request_id"]
        if scope.mode == "project"
        else [],
        limitations=[
            "Best-effort capture is not a complete ledger.",
            "Published generation readiness does not establish outbox or scoped capture completeness.",
        ],
    )
