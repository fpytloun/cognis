"""Bounded logical-request/session/sidebar reads after application authorization."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select, union_all

from cognis.core.usage.query_adapters import attempt_state, logical_outcome, logical_timing
from cognis.core.usage.query_labels import group_labels
from cognis.core.usage.query_metadata import active_projection, source_meta, utc
from cognis.core.usage.query_models import (
    UsageAvailability,
    UsageMeta,
    UsageProjectionCoverage,
    UsageRequestItem,
    UsageRequestPage,
    UsageScope,
    UsageSessionItem,
    UsageSessionPage,
    UsageSummaryItem,
    UsageSummaryResponse,
)
from cognis.core.usage.query_populations import aggregate_columns, metrics
from cognis.core.usage.query_proxy import proxy_sessions
from cognis.core.usage.query_selections import (
    UsageSelection,
    allocator_tail,
    mint_selection,
    request_selection,
    selection_boundary,
    selection_predicates,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.core.usage.sessions import UsageSessionCorrelation, session_correlation
from cognis.models.usage import UsageAttemptContext
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageProjectionStateRow,
    UsageRequestStatsRow,
    UsageSessionTotalRow,
)
from cognis.store.usage_membership import (
    direct_conversation_membership,
    invalid_memberships,
    materialized_membership,
)
from cognis.store.usage_queries import (
    authorized_selector,
    filter_predicates,
    scope_predicate,
    session_membership,
)


async def list_requests(
    session: Any, scope: Any, query: Any, *, indexes: Any, cursors: Any
) -> UsageRequestPage:
    """Physical request observations; optional logical stats cannot hide attempts."""
    indexes.require_admin(scope, "usage_attempts")
    indexes.require_admin(scope, "usage_hourly_totals")
    fact, request = UsageAttemptRow, UsageRequestStatsRow
    fingerprint = cursors.fingerprint(query, scope, "request")
    cursor = cursors.decode(query.cursor, fingerprint)
    predicates = [
        fact.request_id.is_not(None),
        scope_predicate(fact, scope),
        *filter_predicates(fact, query.filters, scope),
        fact.started_at_observed >= utc(query.start),
        fact.started_at_observed < utc(query.end),
        *selection_predicates(fact, query, scope, cursors),
    ]
    if cursor:
        try:
            at, row_id = utc(datetime.fromisoformat(cursor[0])), int(cursor[1])
        except (ValueError, TypeError, IndexError) as exc:
            raise UsageQueryError("invalid_usage_cursor", "Invalid request position") from exc
        predicates.append(
            or_(
                fact.started_at_observed < at,
                and_(fact.started_at_observed == at, fact.row_id < row_id),
            )
        )
    boundary = (
        literal(await selection_boundary(session, query, scope, cursors))
        if query.selection_ref
        else allocator_tail()
    )
    values = list(
        (
            await session.execute(
                select(fact, request, boundary.label("selection_boundary"))
                .outerjoin(
                    request,
                    and_(
                        fact.request_id == request.request_id,
                        fact.owner_key == request.owner_key,
                        fact.usage_subject_key == request.usage_subject_key,
                    ),
                )
                .where(*predicates)
                .order_by(fact.started_at_observed.desc(), fact.row_id.desc())
                .limit(query.limit + 1)
            )
        ).all()
    )
    meta = await source_meta(session, scope, start=query.start, end=query.end, precision="exact")
    page = values[: query.limit]
    if page:
        meta.availability = meta.availability.model_copy(update={"resolution": "retained"})
    if (
        not page
        and meta.availability.detail_retention_cutoff
        and utc(query.end) <= meta.availability.detail_retention_cutoff
    ):
        meta.availability.resolution = "detail_expired"
    fence = int(page[0][2]) if page else 0
    labels = await group_labels(session, scope, "session", [fact.session_id for fact, _, _ in page])
    items = [
        UsageRequestItem(
            request_id=fact_row.request_id if scope.sensitive_provenance else None,
            attempt_id=fact_row.attempt_id if scope.sensitive_provenance else None,
            cycle_id=fact_row.cycle_id if scope.sensitive_provenance else None,
            selection_ref=request_selection(fact_row, query, scope, cursors, fence),
            session_label=labels.get(fact_row.session_id),
            session_type=(
                correlation.session_kind
                if (
                    correlation := session_correlation(
                        UsageAttemptContext.model_validate(fact_row.context_payload).attribution
                    )
                )
                is not None
                and correlation.session_kind != "direct"
                else None
            ),
            state=attempt_state(fact_row),
            started_at=utc(fact_row.started_at_observed),
            logical_request_started_at=(
                utc(stats_row.request_started_at) if stats_row is not None else None
            ),
            logical_outcome=logical_outcome(stats_row),
            timing=logical_timing(stats_row),
            drill_filters=query.filters.model_copy(
                update={"logical_request_ids": [fact_row.request_id]}
                if scope.sensitive_provenance
                else {}
            ),
        )
        for fact_row, stats_row, _ in page
    ]
    next_cursor = (
        cursors.encode(
            fingerprint,
            [utc(page[-1][0].started_at_observed).isoformat(), page[-1][0].row_id],
        )
        if len(values) > query.limit
        else None
    )
    meta.limitations.append(
        "Pages contain physical attempts, not unique logical requests; "
        "logical outcomes and timings require separate captured observations."
    )
    return UsageRequestPage(meta=meta, items=items, next_cursor=next_cursor)


async def list_sessions(
    session: Any,
    scope: Any,
    query: Any,
    *,
    population: Any,
    indexes: Any,
    cursors: Any,
) -> UsageSessionPage:
    """Page current resource-preserving session totals, not raw-fact aggregation."""
    if query.session_type == "proxy":
        return await proxy_sessions(
            session, scope, query, population=population, indexes=indexes, cursors=cursors
        )
    row = UsageSessionTotalRow
    indexes.require_admin(scope, row.__tablename__)
    fingerprint = cursors.fingerprint(query, scope, "session")
    cursor = cursors.decode(query.cursor, fingerprint)
    predicates = [
        scope_predicate(row, scope),
        authorized_selector(literal("session"), row.session_id, scope),
        *filter_predicates(row, query.filters, scope),
        row.bucket_day >= utc(query.start).date(),
        row.bucket_day < utc(query.end).date(),
        row.session_kind == "cognis",
    ]
    if query.search:
        predicates.append(row.session_id == query.search)
    if any(
        value.hour or value.minute or value.second or value.microsecond
        for value in (utc(query.start), utc(query.end))
    ):
        raise UsageQueryError(
            "unsupported_usage_resolution", "Session totals require aligned UTC days"
        )
    if query.pricing_basis != "historical":
        raise UsageQueryError(
            "usage_current_valuation_unavailable", "Current pricing is not qualified", 503
        )
    if cursor:
        if len(cursor) != 1 or not isinstance(cursor[0], str):
            raise UsageQueryError("invalid_usage_cursor", "Invalid session position")
        predicates.append(row.session_id > cursor[0])
    keys = (
        select(row.session_id)
        .join(UsageProjectionStateRow, active_projection(row))
        .where(
            *predicates,
            UsageProjectionStateRow.ready.is_(True),
            UsageProjectionStateRow.capture_only.is_(False),
            population.record_kind(row) == "attempt",
        )
        .distinct()
        .order_by(row.session_id)
        .limit(query.limit + 1)
        .cte("usage_session_page")
    )
    values = list(
        (
            await session.execute(
                select(
                    row.session_id,
                    row.conversation_id,
                    row.currency,
                    row.basis,
                    *aggregate_columns(row),
                )
                .join(keys, keys.c.session_id == row.session_id)
                .join(UsageProjectionStateRow, active_projection(row))
                .where(
                    *predicates,
                    UsageProjectionStateRow.ready.is_(True),
                    UsageProjectionStateRow.capture_only.is_(False),
                    population.record_kind(row) == "attempt",
                )
                .group_by(row.session_id, row.conversation_id, row.currency, row.basis)
                .order_by(row.session_id)
                .limit(10001)
            )
        ).mappings()
    )
    if len(values) > 10000:
        raise UsageQueryError("query_budget_exceeded", "Session billing-stratum budget exceeded")
    meta = await source_meta(
        session, scope, start=query.start, end=query.end, precision="day", source=row
    )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for value in values:
        grouped[value["session_id"]].append(value)
    ordered = sorted(grouped)
    selected = ordered[: query.limit]
    labels = await group_labels(session, scope, "session", [identifier for identifier in selected])
    fence = await selection_boundary(session, query, scope, cursors)
    items = [
        UsageSessionItem(
            session_id=identifier,
            session_label=labels.get(identifier),
            selection_ref=mint_selection(
                cursors,
                scope,
                UsageSelection(
                    kind="session",
                    correlation=UsageSessionCorrelation(
                        session_kind="cognis", session_id=identifier
                    ),
                    start=utc(query.start),
                    end=utc(query.end),
                    max_row_id=fence,
                ),
            ),
            conversation_id=grouped[identifier][0]["conversation_id"],
            metrics=metrics(grouped[identifier], basis=query.pricing_basis, contract=population),
            drill_filters=query.filters.model_copy(update={"session_ids": [identifier]}),
        )
        for identifier in selected
    ]
    return UsageSessionPage(
        meta=meta,
        items=items,
        next_cursor=cursors.encode(fingerprint, [selected[-1]])
        if len(ordered) > query.limit
        else None,
    )


async def sidebar_summaries(
    session: Any,
    scope: Any,
    query: Any,
    *,
    population: Any,
    indexes: Any,
) -> UsageSummaryResponse:
    """Exactly one bounded page fetch, with current resource ACLs inside the SQL."""
    row, state = UsageSessionTotalRow, UsageProjectionStateRow
    indexes.require_admin(scope, row.__tablename__)
    if query.pricing_basis != "historical":
        raise UsageQueryError(
            "usage_current_valuation_unavailable", "Current pricing is not qualified", 503
        )
    if query.include_pending:
        raise UsageQueryError(
            "usage_pending_index_unavailable", "Pending sidebar coverage is not qualified", 503
        )
    unique_items = list({(item.kind, item.id): item for item in query.items}.values())
    members = (
        session_membership(unique_items, "self")
        if query.inclusion == "self"
        else await materialized_membership(session, unique_items, query.inclusion, scope)
    )
    invalid = (
        set()
        if query.inclusion == "self"
        else await invalid_memberships(
            session, members, scope, inclusive=query.inclusion == "inclusive"
        )
    )
    bounded = (
        select(members)
        .where(authorized_selector(literal("session"), members.c.session_id, scope))
        .limit(10001)
        .cte("usage_bounded_members")
    )
    selectors = union_all(
        *[
            select(literal(item.kind).label("kind"), literal(item.id).label("id"))
            for item in unique_items
        ]
    ).subquery("usage_selectors")
    direct = (
        select(direct_conversation_membership(unique_items, query.inclusion, scope))
        .limit(10001)
        .cte("usage_bounded_direct_members")
    )
    direct_count = select(func.count()).select_from(direct).correlate(None).scalar_subquery()
    direct_status = (
        select(
            direct.c.selector_kind,
            direct.c.selector_id,
            func.max(
                case(
                    (
                        # Invocation restrictions exclude usage, not valid descendants.
                        ~authorized_selector(
                            literal("conversation"),
                            direct.c.conversation_id,
                            replace(scope, invocation=None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("invalid"),
        )
        .group_by(direct.c.selector_kind, direct.c.selector_id)
        .cte("usage_direct_status")
    )
    active = (
        select(
            *row.__table__.c,
            state.ready.label("projection_ready"),
            state.capture_only.label("capture_only"),
            state.building_generation.label("building_generation"),
            state.detail_expired_before.label("cutoff"),
        )
        .join(state, active_projection(row))
        .where(
            scope_predicate(row, scope),
            authorized_selector(literal("session"), row.session_id, scope),
            population.record_kind(row) == "attempt",
        )
        .subquery("usage_active_sessions")
    )
    count = select(func.count()).select_from(bounded).scalar_subquery()
    physical_count = (
        select(func.count(func.distinct(active.c.session_id)))
        .select_from(bounded)
        .join(active, active.c.session_id == bounded.c.session_id)
        .where(
            bounded.c.selector_kind == selectors.c.kind,
            bounded.c.selector_id == selectors.c.id,
        )
        .correlate(selectors)
        .scalar_subquery()
    )
    statement = (
        select(
            selectors.c.kind,
            selectors.c.id,
            active.c.currency,
            active.c.basis,
            *aggregate_columns(active.c),
            func.min(
                case(
                    # A member without captured usage has no projection to wait for;
                    # its NULL outer-join row must not mark the selector unready.
                    (active.c.session_id.is_(None), 1),
                    (
                        and_(
                            active.c.projection_ready.is_(True),
                            active.c.capture_only.is_(False),
                            active.c.building_generation.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("ready"),
            func.max(active.c.cutoff).label("cutoff"),
            count.label("member_count"),
            physical_count.label("physical_sessions"),
            direct_count.label("direct_member_count"),
            func.max(direct_status.c.invalid).label("direct_invalid"),
        )
        .select_from(selectors)
        .outerjoin(
            bounded,
            and_(
                bounded.c.selector_kind == selectors.c.kind, bounded.c.selector_id == selectors.c.id
            ),
        )
        .outerjoin(active, active.c.session_id == bounded.c.session_id)
        .outerjoin(
            direct_status,
            and_(
                direct_status.c.selector_kind == selectors.c.kind,
                direct_status.c.selector_id == selectors.c.id,
            ),
        )
        .where(authorized_selector(selectors.c.kind, selectors.c.id, scope))
        .group_by(selectors.c.kind, selectors.c.id, active.c.currency, active.c.basis)
    )
    # Separate aggregate arms avoid multiplying direct and physical observations.
    direct_statement = (
        select(
            direct.c.selector_kind.label("kind"),
            direct.c.selector_id.label("id"),
            row.currency,
            row.basis,
            *aggregate_columns(row),
            func.min(
                case(
                    (
                        and_(
                            state.ready.is_(True),
                            state.capture_only.is_(False),
                            state.building_generation.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("ready"),
            func.max(state.detail_expired_before).label("cutoff"),
            direct_count.label("member_count"),
            literal(0).label("physical_sessions"),
            direct_count.label("direct_member_count"),
            func.max(direct_status.c.invalid).label("direct_invalid"),
        )
        .select_from(direct)
        .join(
            row, and_(row.conversation_id == direct.c.conversation_id, row.session_kind == "direct")
        )
        .join(state, active_projection(row))
        .outerjoin(
            direct_status,
            and_(
                direct_status.c.selector_kind == direct.c.selector_kind,
                direct_status.c.selector_id == direct.c.selector_id,
            ),
        )
        .where(
            scope_predicate(row, scope),
            authorized_selector(literal("conversation"), row.conversation_id, scope),
            population.record_kind(row) == "attempt",
        )
        .group_by(direct.c.selector_kind, direct.c.selector_id, row.currency, row.basis)
    )
    values = list(
        (await session.execute(statement.union_all(direct_statement).limit(1001))).mappings()
    )
    if len(values) > 1000 or any(
        value["member_count"] > 10000 or value["direct_member_count"] > 10000 for value in values
    ):
        raise UsageQueryError("query_budget_exceeded", "Sidebar membership/stratum budget exceeded")
    by_selector: dict[tuple[str, str], list[Any]] = defaultdict(list)
    unsafe = invalid | {
        (value["kind"], value["id"])
        for value in values
        if not value["ready"] or value["direct_invalid"]
    }
    for value in values:
        key = (value["kind"], value["id"])
        if key not in unsafe and value["finalized_count"] is not None:
            by_selector[key].append(value)
    items = []
    for selector in query.items:
        matches = by_selector.get((selector.kind, selector.id))
        cutoff = (
            max((utc(value["cutoff"]) for value in matches if value["cutoff"]), default=None)
            if matches
            else None
        )
        availability = (
            UsageAvailability(
                resolution="mixed" if cutoff else "retained",
                detail_retention_cutoff=cutoff,
                archive_grain="day" if cutoff else None,
            )
            if matches
            else UsageAvailability()
        )
        items.append(
            UsageSummaryItem(
                selector=selector,
                inclusion=query.inclusion,
                available=bool(matches),
                physical_sessions=max(value["physical_sessions"] for value in matches)
                if matches
                else None,
                metrics=metrics(matches, basis=query.pricing_basis, contract=population)
                if matches
                else None,
                availability=availability,
            )
        )
    meta = UsageMeta(
        scope=UsageScope(mode=scope.mode, project_id=scope.project_id),
        precision="lifetime",
        complete=False,
        projection=UsageProjectionCoverage(
            state="ready" if by_selector and not unsafe else "unavailable"
        ),
        private_resources_excluded=scope.mode == "project",
        availability=UsageAvailability(
            resolution="retained" if any(item.available for item in items) else "unavailable"
        ),
        limitations=["Captured projected totals only; pending/capture coverage is unknown."],
    )
    return UsageSummaryResponse(meta=meta, items=items)
