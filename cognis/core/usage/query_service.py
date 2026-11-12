"""Usage query application service shared by REST and controller tools."""

from __future__ import annotations

import secrets
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import and_, literal, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from cognis.core.usage.access import (
    AuthorizedUsageScope,
    UsageAccessError,
    UsageInvocation,
    authorize_usage,
)
from cognis.core.usage.availability import check_query_availability
from cognis.core.usage.forecast import forecast_usage
from cognis.core.usage.query_adapters import attempt_detail
from cognis.core.usage.query_breakdowns import cost_breakdowns
from cognis.core.usage.query_cursors import UsageCursors
from cognis.core.usage.query_labels import group_labels
from cognis.core.usage.query_metadata import (
    UsageIndexCapabilities,
    active_projection,
    source_meta,
    utc,
)
from cognis.core.usage.query_models import (
    ProviderQuotaHistory,
    ProviderQuotaHistoryQuery,
    UsageAttemptPage,
    UsageAttemptQuery,
    UsageBucket,
    UsageFacetRequest,
    UsageFacetResponse,
    UsageGroup,
    UsageQuery,
    UsageQueryResponse,
    UsageRequestPage,
    UsageSessionPage,
    UsageSessionQuery,
    UsageSummaryRequest,
    UsageSummaryResponse,
    UsageSummarySelector,
)
from cognis.core.usage.query_populations import (
    UsagePopulationContract,
    aggregate_columns,
    combined_metrics,
)
from cognis.core.usage.query_selections import (
    request_selection,
    selection_boundary,
    selection_predicates,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageHourlyTotalRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
    UsageValuationRow,
)
from cognis.store.usage_queries import (
    authorized_selector,
    filter_predicates,
    public_group,
    scope_predicate,
)


class UsageQueryService:
    """No polling, inference, upstream quota refresh or foreground rebuilds."""

    def __init__(
        self,
        session_factory: Any,
        *,
        indexes: UsageIndexCapabilities,
        cursor_secret: bytes,
    ) -> None:
        self.session_factory = session_factory
        self.indexes = indexes
        self.cursors = UsageCursors(cursor_secret)
        self.population = UsagePopulationContract()

    @asynccontextmanager
    async def _request(
        self, actor: str, query: Any, invocation: UsageInvocation | None, role: str | None
    ) -> AsyncIterator[tuple[AsyncSession, AuthorizedUsageScope]]:
        async with self.session_factory() as session:
            try:
                snapshot = (
                    isinstance(query, UsageSummaryRequest) and query.inclusion != "self"
                ) or (
                    isinstance(query, UsageQuery)
                    and (query.view == "cost_breakdowns" or query.filters.conversation_tree)
                )
                # Metadata proofs, active generations and accounting share one
                # request-owned read snapshot. Closing/cancelling rolls it back;
                # there are no writes, external waits or retry side effects here.
                if snapshot:
                    if session.bind.dialect.name == "postgresql":
                        await session.execute(
                            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        )
                    elif session.bind.dialect.name == "sqlite":
                        await session.execute(text("BEGIN"))
                if session.bind.dialect.name == "postgresql":
                    # JIT compilation can exhaust the budget before these bounded reads execute.
                    await session.execute(
                        text(
                            "SELECT set_config('statement_timeout', '1000ms', true), "
                            "set_config('jit', 'off', true)"
                        )
                    )
                scope = await authorize_usage(
                    session,
                    actor=actor,
                    requested=query.scope,
                    invocation=invocation,
                    actor_role=role,
                )
                if hasattr(query, "filters"):
                    scope.check_filters(query.filters)
                if scope.mode == "project" and (
                    getattr(query, "group_by", None) in {"client", "key"}
                    or getattr(query, "dimension", None) in {"client", "key"}
                ):
                    raise UsageAccessError("Client/key grouping is unavailable in project usage")
                yield session, scope
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "57014":
                    raise UsageQueryError(
                        "query_budget_exceeded", "Usage SQL statement budget exceeded", 503
                    ) from exc
                raise
            except ValidationError as exc:
                raise UsageQueryError(
                    "usage_source_incompatible",
                    "Stored usage is incompatible with the supported public contract",
                    503,
                ) from exc

    def _population(self) -> UsagePopulationContract:
        return self.population

    async def query(
        self,
        actor: str,
        query: UsageQuery,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> UsageQueryResponse:
        """One bounded grouped data set supplies related charts and breakdowns."""
        ranking_measure: Literal["known_priced_subtotal", "attempts", "input", "output"] | None = (
            ("known_priced_subtotal" if query.metric == "cost" else query.metric)
            if query.view == "ranking"
            else None
        )
        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            population = self._population()
            resource_filters = query.group_by in (
                "project",
                "conversation",
                "session",
                "task",
                "schedule",
            ) or any(
                getattr(query.filters, name)
                for name in (
                    "project_ids",
                    "session_ids",
                    "conversation_ids",
                    "conversation_tree",
                    "task_ids",
                    "step_run_ids",
                    "schedule_ids",
                    "task_origin",
                )
            )
            source = (
                UsageHourlyTotalRow
                if (
                    scope.mode == "project"
                    or scope.project_id is not None
                    or query.bucket == "hour"
                    or resource_filters
                    or invocation is not None
                )
                else UsageDailyTotalRow
            )
            self.indexes.require_admin(scope, source.__tablename__)
            unit = timedelta(hours=1) if source is UsageHourlyTotalRow else timedelta(days=1)
            if any(
                value.minute or value.second or value.microsecond or (unit.days and value.hour)
                for value in (query.start, query.end)
            ):
                raise UsageQueryError(
                    "unsupported_usage_resolution", "Use aligned UTC summary bucket boundaries"
                )
            if query.include_pending:
                raise UsageQueryError(
                    "usage_pending_index_unavailable",
                    "Pending coverage needs the qualified subject/time partial index",
                    503,
                )
            meta = await source_meta(
                session,
                scope,
                start=query.start,
                end=query.end,
                precision="hour" if unit.days == 0 else "day",
                source=source,
                include_archives=source is UsageHourlyTotalRow,
            )
            cutoff = meta.availability.detail_retention_cutoff
            if source is UsageHourlyTotalRow and cutoff is not None and query.start < cutoff:
                raise UsageQueryError(
                    "resource_detail_expired",
                    "Task, schedule and conversation breakdowns are not available before "
                    f"{cutoff.isoformat()}",
                )
            if meta.projection.state != "ready":
                return UsageQueryResponse(
                    meta=meta, total=None, groups=[], buckets=[], ranking_measure=ranking_measure
                )
            meta.availability.supported_dimensions = list(population.archived_dimensions)
            check_query_availability(query, meta.availability)
            if query.pricing_basis == "current":
                raise UsageQueryError(
                    "usage_current_valuation_unavailable",
                    "Current summary pricing requires the qualified pricing-generation encoder",
                    503,
                )
            dimension = query.group_by or ("service" if query.view == "companions" else None)
            group = public_group(source, dimension, scope=scope) if dimension else literal(None)
            day = source.bucket_day if query.view == "trend" else literal(None)
            hour = (
                UsageHourlyTotalRow.bucket_hour
                if source is UsageHourlyTotalRow
                and query.view == "trend"
                and query.bucket == "hour"
                else literal(None)
            )
            kind = population.record_kind(source)
            filters = query.filters
            members = None
            direct_members = None
            if tree := filters.conversation_tree:
                from cognis.store.usage_membership import (
                    MAX_MEMBERS,
                    direct_conversation_membership,
                    invalid_memberships,
                    materialized_membership,
                )

                selectors = [UsageSummarySelector(kind="conversation", id=tree.id)]
                members = await materialized_membership(
                    session,
                    selectors,
                    tree.inclusion,
                    scope,
                )
                direct_members = direct_conversation_membership(selectors, tree.inclusion, scope)
                if (
                    len(
                        (await session.execute(select(direct_members).limit(MAX_MEMBERS + 1))).all()
                    )
                    > MAX_MEMBERS
                ):
                    raise UsageQueryError(
                        "query_budget_exceeded", "Conversation membership exceeds 10000"
                    )
                invalid = await invalid_memberships(
                    session, members, scope, inclusive=tree.inclusion == "inclusive"
                )
                direct_invalid = await session.scalar(
                    select(direct_members.c.selector_id)
                    .where(
                        ~authorized_selector(
                            literal("conversation"), direct_members.c.conversation_id, scope
                        )
                    )
                    .limit(1)
                )
                if invalid or direct_invalid is not None:
                    meta.projection.state = "unavailable"
                    meta.limitations.append("Conversation execution membership is ambiguous.")
                    return UsageQueryResponse(
                        meta=meta,
                        total=None,
                        groups=[],
                        buckets=[],
                        ranking_measure=ranking_measure,
                    )
                filters = filters.model_copy(update={"conversation_tree": None})
            predicates = [
                scope_predicate(source, scope),
                *filter_predicates(source, filters, scope),
            ]
            if members is not None:
                assert direct_members is not None
                predicates.append(
                    or_(
                        UsageHourlyTotalRow.session_id.in_(
                            select(members.c.session_id).where(
                                authorized_selector(literal("session"), members.c.session_id, scope)
                            )
                        ),
                        and_(
                            UsageHourlyTotalRow.session_id.is_(None),
                            UsageHourlyTotalRow.conversation_id.in_(
                                select(direct_members.c.conversation_id)
                            ),
                        ),
                    )
                )
            if dimension == "schedule":
                predicates.append(group.is_not(None))
            # Dates/hours are compiled SQL values, never interpolated expressions.
            predicates.extend(
                [
                    source.bucket_day >= query.start.date(),
                    source.bucket_day <= query.end.date(),
                ]
            )
            if source is UsageHourlyTotalRow:
                predicates.extend(
                    [
                        or_(
                            source.bucket_day > query.start.date(),
                            UsageHourlyTotalRow.bucket_hour >= query.start.hour,
                        ),
                        or_(
                            source.bucket_day < query.end.date(),
                            UsageHourlyTotalRow.bucket_hour < query.end.hour,
                        ),
                    ]
                )
            else:
                predicates.append(source.bucket_day < query.end.date())
            if query.view == "cost_breakdowns":
                return await cost_breakdowns(
                    session,
                    scope=scope,
                    query=query,
                    population=population,
                    meta=meta,
                    base=select(*source.__table__.c)
                    .join(UsageProjectionStateRow, active_projection(source))
                    .where(
                        *predicates,
                        UsageProjectionStateRow.ready.is_(True),
                        UsageProjectionStateRow.capture_only.is_(False),
                        UsageProjectionStateRow.building_generation.is_(None),
                    ),
                )
            statement = (
                select(
                    group.label("group_key"),
                    day.label("day"),
                    hour.label("hour"),
                    source.currency,
                    source.basis,
                    kind.label("record_kind"),
                    *aggregate_columns(source),
                )
                .join(UsageProjectionStateRow, active_projection(source))
                .where(
                    *predicates,
                    UsageProjectionStateRow.ready.is_(True),
                    UsageProjectionStateRow.capture_only.is_(False),
                )
                .group_by(group, day, hour, source.currency, source.basis, kind)
                .limit(1001)
            )
            rows = list((await session.execute(statement)).mappings())
            if len(rows) > 1000:
                raise UsageQueryError("query_budget_exceeded", "Grouped series exceeds 1000 strata")
            if any(row["record_kind"] not in {"attempt", "request"} for row in rows):
                raise UsageQueryError(
                    "usage_population_mismatch", "Unknown summary record population", 503
                )
            attempts = [row for row in rows if row["record_kind"] == "attempt"]
            requests = [row for row in rows if row["record_kind"] == "request"]
            groups: dict[str | None, list[Any]] = defaultdict(list)
            buckets: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
            request_groups: dict[str | None, list[Any]] = defaultdict(list)
            request_buckets: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
            for row in attempts:
                groups[row["group_key"]].append(row)
                if query.view == "trend":
                    buckets[(row["day"], row["hour"])].append(row)
            for row in requests:
                request_groups[row["group_key"]].append(row)
                if query.view != "ranking":
                    groups.setdefault(row["group_key"], [])
                if query.view == "trend":
                    position = (row["day"], row["hour"])
                    request_buckets[position].append(row)
                    buckets.setdefault(position, [])
            total = combined_metrics(
                attempts,
                requests,
                basis=query.pricing_basis,
                contract=population,
            )
            labels = (
                await group_labels(session, scope, dimension, list(groups)) if dimension else {}
            )
            public_groups = (
                [
                    UsageGroup(
                        key=key,
                        label=labels.get(key),
                        metrics=combined_metrics(
                            values,
                            request_groups.get(key, []),
                            basis=query.pricing_basis,
                            contract=population,
                        ),
                    )
                    for key, values in groups.items()
                ]
                if dimension
                else []
            )
            if query.view == "ranking":
                if query.metric == "cost":
                    # Logical-only strata carry timing, not SDK spend allocation.
                    public_groups = [
                        item for item in public_groups if item.metrics.finalized_attempts > 0
                    ]
                    currencies = {
                        (cost.currency, cost.basis)
                        for item in public_groups
                        for cost in item.metrics.costs
                    }
                    if len(currencies) > 1:
                        raise UsageQueryError(
                            "mixed_cost_ranking", "Cost ranking requires one currency and basis"
                        )
                    if any(item.metrics.unpriced_attempts for item in public_groups):
                        meta.limitations.append(
                            "Cost ranking orders known priced subtotals, not complete spend; "
                            "unknown-cost groups sort last."
                        )
                    public_groups.sort(
                        key=lambda item: (
                            not any(cost.known_amount is not None for cost in item.metrics.costs),
                            -sum(
                                (Decimal(cost.known_amount or "0") for cost in item.metrics.costs),
                                Decimal(0),
                            ),
                            item.key or "",
                        ),
                    )
                else:
                    public_groups.sort(
                        key=lambda item: (
                            item.metrics.finalized_attempts
                            if query.metric == "attempts"
                            else getattr(item.metrics.tokens, query.metric) or -1
                        ),
                        reverse=True,
                    )
                public_groups = public_groups[: query.limit]
            series = []
            for (bucket_day, bucket_hour), values in sorted(buckets.items()):
                start = datetime.combine(bucket_day, time(hour=bucket_hour or 0), UTC)
                group_rows: dict[str | None, list[Any]] = defaultdict(list)
                logical_group_rows: dict[str | None, list[Any]] = defaultdict(list)
                for row in values:
                    group_rows[row["group_key"]].append(row)
                for row in request_buckets.get((bucket_day, bucket_hour), []):
                    logical_group_rows[row["group_key"]].append(row)
                    group_rows.setdefault(row["group_key"], [])
                series.append(
                    UsageBucket(
                        start=start,
                        end=start
                        + (timedelta(hours=1) if query.bucket == "hour" else timedelta(days=1)),
                        complete=False,
                        metrics=combined_metrics(
                            values,
                            request_buckets.get((bucket_day, bucket_hour), []),
                            basis=query.pricing_basis,
                            contract=population,
                        ),
                        groups=[
                            UsageGroup(
                                key=key,
                                label=labels.get(key),
                                metrics=combined_metrics(
                                    entries,
                                    logical_group_rows.get(key, []),
                                    basis=query.pricing_basis,
                                    contract=population,
                                ),
                            )
                            for key, entries in group_rows.items()
                        ]
                        if query.group_by
                        else [],
                        capture=meta.capture,
                        projection=meta.projection,
                        source_grain="hour" if source is UsageHourlyTotalRow else "day",
                        source="mixed" if meta.availability.resolution == "mixed" else "retained",
                    )
                )
            meta.limitations.extend(
                [
                    "Error-dispatch and reasoning-known populations are unavailable, not zero.",
                ]
            )
            prediction = (
                forecast_usage(
                    series, bucket=query.bucket, options=query.forecast, now=datetime.now(UTC)
                )
                if query.forecast
                else None
            )
            return UsageQueryResponse(
                meta=meta,
                total=total,
                groups=public_groups,
                buckets=series,
                forecast=prediction,
                ranking_measure=ranking_measure,
            )

    async def attempts(
        self,
        actor: str,
        query: UsageAttemptQuery,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> UsageAttemptPage:
        """Indexed physical-attempt page with current authority and safe provenance."""
        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            self.indexes.require_admin(scope, "usage_attempts")
            row, state, valuation, stats = (
                UsageAttemptRow,
                UsageProjectionStateRow,
                UsageValuationRow,
                UsageRequestStatsRow,
            )
            fingerprint = self.cursors.fingerprint(query, scope, "attempt")
            cursor = self.cursors.decode(query.cursor, fingerprint)
            predicates = [
                scope_predicate(row, scope),
                *filter_predicates(row, query.filters, scope),
                row.started_at_observed >= utc(query.start),
                row.started_at_observed < utc(query.end),
                *selection_predicates(row, query, scope, self.cursors),
            ]
            if cursor:
                try:
                    at, row_id = utc(datetime.fromisoformat(cursor[0])), int(cursor[1])
                except (ValueError, TypeError, IndexError) as exc:
                    raise UsageQueryError(
                        "invalid_usage_cursor", "Invalid attempt page position"
                    ) from exc
                predicates.append(
                    or_(
                        row.started_at_observed < at,
                        and_(row.started_at_observed == at, row.row_id < row_id),
                    )
                )
            join_state = and_(state.owner_key == row.owner_key, state.bucket_day == row.bucket_day)
            join_value = and_(
                valuation.owner_key == row.owner_key,
                valuation.bucket_day == row.bucket_day,
                valuation.source_kind == "attempt",
                valuation.source_id == row.attempt_id,
                valuation.fact_version == row.fact_version,
                valuation.generation == state.active_generation,
                state.ready.is_(True),
                state.capture_only.is_(False),
            )
            price = UsagePriceRevisionRow
            if query.pricing_basis == "current":
                pricing = UsagePricingStateRow
                join_value = and_(
                    join_value,
                    select(1)
                    .where(
                        pricing.provider_id == row.provider_id,
                        pricing.model == row.effective_model,
                        pricing.generation == valuation.pricing_generation,
                        pricing.current_revision_id == valuation.current_revision_id,
                    )
                    .correlate(row, valuation)
                    .exists(),
                )
            statement = (
                select(row, valuation, stats, price)
                .options(load_only(price.currency, price.basis))
                .outerjoin(state, join_state)
                .outerjoin(valuation, join_value)
                .outerjoin(
                    price,
                    and_(
                        price.revision_id
                        == getattr(valuation, f"{query.pricing_basis}_revision_id"),
                        price.provider_id == row.provider_id,
                        price.model == row.effective_model,
                    ),
                )
                .outerjoin(
                    stats,
                    and_(
                        stats.request_id == row.request_id,
                        stats.attempt_id == row.attempt_id,
                        stats.usage_subject_key == row.usage_subject_key,
                        stats.owner_key == row.owner_key,
                    ),
                )
                .where(*predicates)
                .order_by(row.started_at_observed.desc(), row.row_id.desc())
                .limit(query.limit + 1)
            )
            values = list((await session.execute(statement)).all())
            self.indexes.require_admin(scope, "usage_hourly_totals")
            meta = await source_meta(
                session, scope, start=query.start, end=query.end, precision="exact"
            )
            page = values[: query.limit]
            if page:
                meta.availability = meta.availability.model_copy(update={"resolution": "retained"})
            if (
                not page
                and meta.availability.detail_retention_cutoff
                and utc(query.end) <= meta.availability.detail_retention_cutoff
            ):
                meta.availability.resolution = "detail_expired"
            next_cursor = None
            if len(values) > query.limit:
                tail = page[-1][0]
                next_cursor = self.cursors.encode(
                    fingerprint, [utc(tail.started_at_observed).isoformat(), tail.row_id]
                )
            fence = await selection_boundary(session, query, scope, self.cursors)
            return UsageAttemptPage(
                meta=meta,
                items=[
                    attempt_detail(
                        fact,
                        value,
                        request,
                        private=scope.sensitive_provenance,
                        basis=query.pricing_basis,
                        price_revision=price_revision,
                        selection_ref=(
                            request_selection(fact, query, scope, self.cursors, fence)
                            if fact.request_id is not None
                            else None
                        ),
                    )
                    for fact, value, request, price_revision in page
                ],
                next_cursor=next_cursor,
            )

    async def catalog(
        self,
        actor: str,
        query: Any,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> Any:
        """Fresh scope-specific catalog; unsupported shapes never trigger scans."""
        from typing import get_args

        from cognis.core.usage.query_models import UsageCatalog, UsageDimension, UsageScope
        from cognis.store.usage_queries import FILTER_COLUMNS

        async with self._request(actor, query, invocation, actor_role) as (_session, scope):
            return UsageCatalog(
                scope=UsageScope(mode=scope.mode, project_id=scope.project_id),
                grouping_dimensions=list(get_args(UsageDimension)),
                summary_filters=[
                    "conversation_tree",
                    "subject_emails",
                    "task_origin",
                    "agent_scope",
                    "schedule_ids",
                    *[
                        name
                        for name, column in FILTER_COLUMNS.items()
                        if hasattr(UsageHourlyTotalRow, column)
                        and name not in {"key_ids", "client_ids"}
                    ],
                ],
                detail_filters=[
                    "subject_emails",
                    "task_origin",
                    "agent_scope",
                    "schedule_ids",
                    "states",
                    "error_categories",
                    *[name for name in FILTER_COLUMNS if name not in {"key_ids", "client_ids"}],
                ],
                summary_population_available=True,
                unavailable_metrics={
                    "reasoning_tokens": "No reasoning-known population in the committed summary schema",
                    "attempt_failure_rate": "No matched failed-dispatched population in the committed summary schema",
                    "authenticated_client": "Summary client populations mix authenticated and unverified reports; retained detail requires authenticated correlation",
                    "current_cost": "Pricing-generation-qualified current summary plan pending",
                    "pending_counts": "Qualified subject/time partial pending index pending",
                },
                session_types={
                    "cognis": "Current resource-authorized session totals",
                    "proxy": "Retained-detail totals and namespace-bound encrypted refs; never lifetime totals",
                },
            )

    async def requests(
        self,
        actor: str,
        query: UsageAttemptQuery,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> UsageRequestPage:
        """Page physical request observations with optional logical evidence."""
        from cognis.core.usage.query_explorer import list_requests

        if getattr(query, "pagination_unit", "attempt") == "request":
            from cognis.core.usage.request_logs import request_logs

            return await request_logs(
                self, actor, query, invocation=invocation, actor_role=actor_role
            )

        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            return await list_requests(
                session, scope, query, indexes=self.indexes, cursors=self.cursors
            )

    async def request_detail(self, actor: str, query: Any, **kwargs: Any) -> Any:
        """Resolve a logical request selection with fresh usage authorization."""
        from cognis.core.usage.request_logs import request_detail

        return await request_detail(self, actor, query, **kwargs)

    async def sessions(
        self,
        actor: str,
        query: UsageSessionQuery,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> UsageSessionPage:
        """Page content-free Cognis sessions, never invent companion sessions."""
        from cognis.core.usage.query_explorer import list_sessions

        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            return await list_sessions(
                session,
                scope,
                query,
                population=self._population(),
                indexes=self.indexes,
                cursors=self.cursors,
            )

    async def session_summaries(
        self,
        actor: str,
        query: UsageSummaryRequest,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> UsageSummaryResponse:
        """Bulk sidebar operation: one bounded summary fetch, no per-session queries."""
        from cognis.core.usage.query_explorer import sidebar_summaries

        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            return await sidebar_summaries(
                session,
                scope,
                query,
                population=self._population(),
                indexes=self.indexes,
            )

    async def provider_history(
        self,
        actor: str,
        provider_id: str,
        query: ProviderQuotaHistoryQuery,
        *,
        invocation: UsageInvocation | None = None,
        actor_role: str | None = None,
    ) -> ProviderQuotaHistory:
        """Stored owner/admin observations, with no quota refresh side effect."""
        from cognis.core.usage.query_quota import provider_history

        async with self._request(actor, query, invocation, actor_role) as (session, scope):
            return await provider_history(session, scope, provider_id, query, cursors=self.cursors)

    async def facets(
        self, actor: str, query: UsageFacetRequest, **kwargs: Any
    ) -> UsageFacetResponse:
        """A facet uses the same authorized grouped query, not a global inventory."""
        result = await self.query(
            actor,
            UsageQuery(
                scope=query.scope,
                start=query.start,
                end=query.end,
                filters=query.filters,
                view="ranking",
                group_by=query.dimension,
                metric="attempts",
                limit=query.limit,
            ),
            **kwargs,
        )
        return UsageFacetResponse(meta=result.meta, dimension=query.dimension, items=result.groups)


async def create_usage_query_service(
    session_factory: Any,
    *,
    cursor_secret: bytes | None = None,
) -> UsageQueryService:
    """Explicit app/runtime composition interface; inspect schema before serving."""
    async with session_factory() as session:
        indexes = await UsageIndexCapabilities.discover(session)
    return UsageQueryService(
        session_factory,
        indexes=indexes,
        cursor_secret=cursor_secret or secrets.token_bytes(32),
    )
