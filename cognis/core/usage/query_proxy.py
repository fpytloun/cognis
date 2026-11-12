"""Bounded retained proxy summaries and indexed physical-detail selection."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import and_, func, select, tuple_

from cognis.core.usage.query_metadata import active_projection, source_meta, utc
from cognis.core.usage.query_models import UsageSessionItem, UsageSessionPage
from cognis.core.usage.query_populations import aggregate_columns, metrics
from cognis.core.usage.query_selections import (
    UsageSelection,
    mint_selection,
    proxy_predicates,
    selection_boundary,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.core.usage.sessions import UsageSessionCorrelation
from cognis.store.usage.models import UsageAttemptRow, UsageProjectionStateRow, UsageSessionTotalRow
from cognis.store.usage_queries import filter_predicates, scope_predicate


async def proxy_sessions(
    session: Any,
    scope: Any,
    query: Any,
    *,
    population: Any,
    indexes: Any,
    cursors: Any,
) -> UsageSessionPage:
    """List retained namespaces from session summaries; never group raw payloads."""
    indexes.require_admin(scope, "usage_session_totals")
    if not indexes.supports(
        "usage_attempts",
        (
            "usage_subject_key",
            "client_id",
            "client_session_id",
            "started_at_observed",
            "row_id",
        ),
    ):
        raise UsageQueryError("usage_source_incompatible", "Proxy detail index unavailable", 503)
    if query.selection_ref:
        raise UsageQueryError("unsupported_usage_filter", "Selections are for detail drill")
    if scope.mode == "project" and query.search:
        raise UsageQueryError("unsupported_usage_filter", "Client-label search is private")
    if query.pricing_basis != "historical":
        raise UsageQueryError(
            "usage_current_valuation_unavailable", "Proxy summaries are historical"
        )
    if any(
        value.hour or value.minute or value.second or value.microsecond
        for value in (utc(query.start), utc(query.end))
    ):
        raise UsageQueryError("unsupported_usage_resolution", "Proxy totals require UTC days")
    row, state, fact = UsageSessionTotalRow, UsageProjectionStateRow, UsageAttemptRow
    fingerprint = cursors.fingerprint(query, scope, "proxy-session")
    cursor = cursors.decode(query.cursor, fingerprint)
    fence = await selection_boundary(session, query, scope, cursors)
    position = None
    if cursor:
        if len(cursor) != 3:
            raise UsageQueryError("invalid_usage_cursor", "Invalid proxy position")
        position = cursor
    client = func.coalesce(row.client_id, "")
    predicates = [
        scope_predicate(row, scope),
        row.session_kind == "proxy",
        *filter_predicates(row, query.filters, scope),
        row.bucket_day >= utc(query.start).date(),
        row.bucket_day < utc(query.end).date(),
        population.record_kind(row) == "attempt",
        state.ready.is_(True),
        state.capture_only.is_(False),
    ]
    if query.search:
        # Literal equality is indexed and cannot turn a label into SQL wildcard authority.
        predicates.append(row.client_session_id == query.search)
    if position:
        predicates.append(
            tuple_(row.usage_subject_key, client, row.client_session_id) > tuple_(*position)
        )
    keys = (
        select(row.usage_subject_key, client.label("client"), row.client_session_id)
        .join(state, active_projection(row))
        .where(*predicates)
        .distinct()
        .order_by(row.usage_subject_key, client, row.client_session_id)
        .limit(min(query.limit, 100) + 1)
        .cte("proxy_page")
    )
    values = list(
        (
            await session.execute(
                select(
                    row.usage_subject_key,
                    row.client_id,
                    row.client_session_id,
                    row.currency,
                    row.basis,
                    *aggregate_columns(row),
                )
                .join(
                    keys,
                    and_(
                        keys.c.usage_subject_key == row.usage_subject_key,
                        keys.c.client == client,
                        keys.c.client_session_id == row.client_session_id,
                    ),
                )
                .join(state, active_projection(row))
                .where(*predicates)
                .group_by(
                    row.usage_subject_key,
                    row.client_id,
                    row.client_session_id,
                    row.currency,
                    row.basis,
                )
                .order_by(row.usage_subject_key, client, row.client_session_id)
                .limit(1001)
            )
        ).mappings()
    )
    if len(values) > 1000:
        raise UsageQueryError("query_budget_exceeded", "Proxy billing strata exceeded")
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for value in values:
        grouped[
            (value["usage_subject_key"], value["client_id"] or "", value["client_session_id"])
        ].append(value)
    ordered = sorted(grouped)
    selected = ordered[: min(query.limit, 100)]
    # At most 100 indexed namespaces in one SQL statement, not a raw JSON scan.
    # Correlated ordered seeks preserve held/late facts regardless of policy cutoff.
    probes = []
    for index, key in enumerate(selected):
        correlation = UsageSessionCorrelation(
            session_kind="proxy",
            client_id=key[1] or None,
            client_session_id=key[2],
        )
        terms = [
            fact.usage_subject_key == key[0],
            *proxy_predicates(fact, correlation),
            scope_predicate(fact, scope),
            *filter_predicates(fact, query.filters, scope),
            fact.started_at_observed >= utc(query.start),
            fact.started_at_observed < utc(query.end),
            fact.row_id <= fence,
        ]
        for edge, descending in (("first", False), ("last", True)):
            column = fact.started_at_observed
            probes.append(
                select(column)
                .where(*terms)
                .order_by(
                    column.desc() if descending else column.asc(),
                    fact.row_id.desc() if descending else fact.row_id.asc(),
                )
                .limit(1)
                .scalar_subquery()
                .label(f"{edge}_{index}")
            )
    retained = (await session.execute(select(*probes))).mappings().one() if probes else {}
    meta = await source_meta(
        session, scope, start=query.start, end=query.end, precision="day", source=row
    )
    meta.limitations.append(
        "Proxy totals are retained-detail only, never lifetime totals; labels are unverified."
    )
    items = []
    for index, key in enumerate(selected):
        first, last = retained[f"first_{index}"], retained[f"last_{index}"]
        if first is None:
            continue
        correlation = UsageSessionCorrelation(
            session_kind="proxy", client_id=key[1] or None, client_session_id=key[2]
        )
        ref = mint_selection(
            cursors,
            scope,
            UsageSelection(
                kind="session",
                subject_key=key[0],
                correlation=correlation,
                start=utc(query.start),
                end=utc(query.end),
                max_row_id=fence,
            ),
        )
        items.append(
            UsageSessionItem(
                session_type="proxy",
                totals_scope="retained_detail",
                selection_ref=ref,
                client_id=correlation.client_id if scope.sensitive_provenance else None,
                client_session_id=key[2] if scope.sensitive_provenance else None,
                retained_start=utc(first),
                retained_end=utc(last),
                metrics=metrics(grouped[key], basis="historical", contract=population),
                drill_filters=query.filters,
            )
        )
    return UsageSessionPage(
        meta=meta,
        items=items,
        next_cursor=(
            cursors.encode(fingerprint, [*selected[-1]]) if len(ordered) > len(selected) else None
        ),
    )
