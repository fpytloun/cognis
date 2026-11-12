"""Indexed logical-request browsing; provenance never grants resource access."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, or_, select, tuple_

from cognis.core.usage.attempt_reads import retained_attempt_statement
from cognis.core.usage.query_adapters import attempt_detail, logical_timing, seconds, server_event
from cognis.core.usage.query_labels import group_labels
from cognis.core.usage.query_metadata import source_meta, utc
from cognis.core.usage.query_models import (
    CostSubtotal,
    TokenTotals,
    UsageFilters,
    UsagePrivateRequestLog,
    UsageRequestDetail,
    UsageRequestLog,
    UsageRequestPage,
)
from cognis.core.usage.query_selections import (
    UsageSelection,
    mint_selection,
    read_selection,
    selection_boundary,
    selection_predicates,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.models.usage import UsageAttribution, UsageNumericExtra
from cognis.store.usage.models import UsageAttemptRow, UsageRequestLogRow, UsageRequestStatsRow
from cognis.store.usage_queries import FILTER_COLUMNS, filter_predicates, scope_predicate

MAX_HYDRATED_ATTEMPTS = 10_000


def _error(status: int | None, outcome: str | None) -> str | None:
    if outcome == "cancelled":
        return "cancelled"
    if status is not None and status >= 400:
        return {
            400: "invalid_request",
            401: "authentication",
            403: "authentication",
            404: "invalid_request",
            422: "invalid_request",
            429: "rate_limit",
            504: "timeout",
        }.get(status, "provider" if status >= 500 else "invalid_request")
    return "unknown" if outcome == "error" else None


def _state(outcome: str | None) -> str:
    return {"success": "succeeded", "error": "failed", "cancelled": "cancelled"}.get(
        outcome or "", "unknown"
    )


def _predicates(query: Any, scope: Any) -> list[Any]:
    row = UsageRequestLogRow
    special = {"model_ids", "provider_ids", "states", "error_categories", "http_status_codes"}
    for field in query.filters.model_fields_set:
        value = getattr(query.filters, field)
        if (
            value is not None
            and field not in special
            and (field not in FILTER_COLUMNS or not hasattr(row, FILTER_COLUMNS[field]))
            and field not in {"subject_emails", "subject_keys"}
        ):
            raise UsageQueryError(
                "unsupported_usage_filter", f"Request logs do not support {field}"
            )
    filters = query.filters.model_copy(update={field: None for field in special})
    predicates = [
        scope_predicate(row, scope),
        *filter_predicates(row, filters, scope),
        row.started_at >= utc(query.start),
        row.started_at < utc(query.end),
    ]
    attempt = UsageAttemptRow
    terms = [
        attempt.owner_key == row.owner_key,
        attempt.usage_subject_key == row.usage_subject_key,
        attempt.request_id == row.request_id,
        scope_predicate(attempt, scope),
    ]
    if query.filters.provider_ids:
        terms.append(attempt.provider_id.in_(query.filters.provider_ids))
    if query.filters.model_ids:
        terms.append(attempt.effective_model.in_(query.filters.model_ids))
    if query.filters.provider_ids or query.filters.model_ids:
        predicates.append(select(1).where(*terms).exists())
    if query.filters.states:
        outcomes = [
            {"succeeded": "success", "failed": "error", "cancelled": "cancelled"}.get(state)
            for state in query.filters.states
        ]
        states = [row.logical_outcome.in_([value for value in outcomes if value])]
        if any(value is None for value in outcomes):
            states.append(row.logical_outcome.is_(None))
        predicates.append(or_(*states))
    status = row.http_metadata["http_status_code"].as_integer()
    if query.filters.http_status_codes:
        predicates.append(status.in_(query.filters.http_status_codes))
    if query.filters.error_categories:
        raw_error = row.http_metadata["error"]["category"].as_string()
        normalized_error = case(
            (raw_error == "permission", "authentication"),
            (raw_error == "context_limit", "invalid_request"),
            (raw_error == "provider_unavailable", "provider"),
            (raw_error == "transport", "connection"),
            else_=raw_error,
        )
        errors = []
        for category in query.filters.error_categories:
            if category == "cancelled":
                errors.append(row.logical_outcome == "cancelled")
            elif category == "authentication":
                errors.append(status.in_([401, 403]))
            elif category == "invalid_request":
                errors.append(status.in_([400, 404, 422]))
            elif category == "rate_limit":
                errors.append(status == 429)
            elif category == "timeout":
                errors.append(status == 504)
            elif category == "provider":
                errors.append(and_(status >= 500, status != 504))
            elif category == "unknown":
                errors.append(
                    and_(row.logical_outcome == "error", or_(status < 400, status.is_(None)))
                )
        predicates.append(
            or_(
                normalized_error.in_(query.filters.error_categories),
                and_(raw_error.is_(None), or_(*errors) if errors else row.row_id < 0),
            )
        )
    return predicates


def _identity(row: Any) -> tuple[str, str, str]:
    return row.owner_key, row.usage_subject_key, row.request_id


async def _hydrate(
    session: Any,
    headers: list[Any],
    scope: Any,
    basis: str,
    *,
    fence: int,
    recorded_before: datetime,
) -> tuple[dict[tuple[str, str, str], list[Any]], dict[tuple[str, str, str], Any]]:
    if not headers:
        return {}, {}
    row = UsageAttemptRow
    statement = (
        retained_attempt_statement(basis)
        .where(
            tuple_(row.owner_key, row.usage_subject_key, row.request_id).in_(
                [_identity(header) for header in headers]
            ),
            scope_predicate(row, scope),
            row.row_id <= fence,
            row.registered_at <= recorded_before,
        )
        .order_by(row.row_id)
        .limit(MAX_HYDRATED_ATTEMPTS + 1)
    )
    selected = (await session.execute(statement)).all()
    if len(selected) > MAX_HYDRATED_ATTEMPTS:
        raise UsageQueryError(
            "query_budget_exceeded", "Request page has too many retained attempts; narrow the page"
        )
    attempts: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for attempt, valuation, stats, price in selected:
        attempts[_identity(attempt)].append(
            attempt_detail(
                attempt,
                valuation,
                stats,
                private=scope.sensitive_provenance,
                basis=basis,
                price_revision=price,
            )
        )
    stats = {
        _identity(item): item
        for item in (
            await session.scalars(
                select(UsageRequestStatsRow).where(
                    tuple_(
                        UsageRequestStatsRow.owner_key,
                        UsageRequestStatsRow.usage_subject_key,
                        UsageRequestStatsRow.request_id,
                    ).in_([_identity(header) for header in headers])
                )
            )
        ).all()
    }
    return attempts, stats


def _item(
    header: Any,
    attempts: list[Any],
    stats: Any,
    scope: Any,
    selection: str,
    session_label: str | None = None,
) -> UsagePrivateRequestLog | UsageRequestLog:
    attribution = UsageAttribution.model_validate(header.attribution)
    metadata = header.http_metadata or {}
    server = server_event(stats)
    measured = server.server if server is not None else None
    category = _error(metadata.get("http_status_code"), header.logical_outcome)
    safe_error = metadata.get("error") or {}
    if safe_error.get("category"):
        category = {
            "permission": "authentication",
            "context_limit": "invalid_request",
            "provider_unavailable": "provider",
            "transport": "connection",
        }.get(safe_error["category"], safe_error["category"])
    tokens = {}
    for field in TokenTotals.model_fields:
        known = [
            getattr(attempt.tokens, field)
            for attempt in attempts
            if getattr(attempt.tokens, field) is not None
        ]
        tokens[field] = sum(known) if known else None
    costs: dict[tuple[str, str], dict[str, Any]] = {}
    for attempt in attempts:
        for cost in attempt.costs:
            item = costs.setdefault(
                (cost.currency, cost.basis),
                {
                    "known_amount": Decimal(0),
                    "currency": cost.currency,
                    "basis": cost.basis,
                    "priced_attempts": 0,
                    "unpriced_attempts": 0,
                    "partially_priced_attempts": 0,
                },
            )
            item["known_amount"] += Decimal(cost.known_amount)
            for count in ("priced_attempts", "unpriced_attempts", "partially_priced_attempts"):
                item[count] += getattr(cost, count)
    unpriced = sum(
        not attempt.costs or any(cost.quality != "complete" for cost in attempt.costs)
        for attempt in attempts
    )
    cost_items = [
        CostSubtotal(
            **{
                **item,
                "known_amount": format(item["known_amount"], "f"),
                "quality": "partial" if unpriced else "complete",
            }
        )
        for item in costs.values()
    ]
    quality = (
        "complete"
        if attempts and all(attempt.usage_quality == "complete" for attempt in attempts)
        else "partial"
        if any(value is not None for value in tokens.values())
        else "unknown"
    )
    values = dict(
        request_id=header.request_id if scope.sensitive_provenance else None,
        drill_filters=UsageFilters(
            logical_request_ids=[header.request_id] if scope.sensitive_provenance else None
        ),
        selection_ref=selection,
        session_label=session_label,
        started_at=utc(header.started_at),
        logical_request_started_at=utc(header.started_at),
        timing=logical_timing(stats),
        traffic_origin=header.traffic_origin,
        operations=sorted({a.inference_operation for a in attempts if a.inference_operation}),
        media_usage=[
            UsageNumericExtra(
                name=quantities[0].name,
                unit=quantities[0].unit,
                value=sum(value.value for value in quantities),
                source="adapter",
                status="partial"
                if len(quantities) != len(attempts)
                or any(value.status == "partial" for value in quantities)
                else "derived"
                if any(value.status == "derived" for value in quantities)
                else "reported",
            )
            for name in sorted(
                {value.name for attempt in attempts for value in attempt.media_usage}
            )
            if (
                quantities := [
                    value
                    for attempt in attempts
                    for value in attempt.media_usage
                    if value.name == name
                ]
            )
        ],
        auth_type=header.traffic_auth_type,
        state=_state(header.logical_outcome),
        completed_at=measured.completed_at if measured else None,
        requested_model=attempts[0].requested_model if attempts else None,
        provider_ids=sorted({a.provider_id for a in attempts if a.provider_id}),
        resolved_models=sorted({a.resolved_model for a in attempts if a.resolved_model}),
        request_path=metadata.get("request_path"),
        streaming=metadata.get("streaming"),
        http_status_code=metadata.get("http_status_code"),
        finish_reason=metadata.get("finish_reason"),
        service_id=attribution.identity.service_id,
        error_category=category,
        error_summary=next((a.error_summary for a in reversed(attempts) if a.error_summary), None)
        if category
        else None,
        latency_seconds=seconds(stats.server_elapsed_ms) if stats else None,
        ttft_seconds=seconds(stats.server_ttft_ms) if stats and metadata.get("streaming") else None,
        tokens=TokenTotals(**tokens),
        usage_quality=quality,
        costs=cost_items,
        unpriced_attempts=unpriced,
        attempt_count=len(attempts),
        retry_count=sum(a.retry for a in attempts),
        fallback_count=sum(a.fallback for a in attempts),
    )
    if scope.sensitive_provenance:
        return UsagePrivateRequestLog(
            **values,
            client_id=header.traffic_client_id,
            key_id=header.traffic_key_id,
            user_agent=metadata.get("user_agent"),
            app_name=metadata.get("app_name"),
            app_version=metadata.get("app_version"),
            client_labels=attribution.client_labels,
        )
    return UsageRequestLog(**values)


async def request_logs(
    service: Any, actor: str, query: Any, *, invocation: Any = None, actor_role: str | None = None
) -> UsageRequestPage:
    """Page request headers before any attempt hydration or aggregation."""
    async with service._request(actor, query, invocation, actor_role) as (session, scope):
        meta = await source_meta(
            session, scope, start=query.start, end=query.end, precision="exact"
        )
        if meta.projection.state != "ready":
            return UsageRequestPage(meta=meta, pagination_unit="request", items=[])
        row = UsageRequestLogRow
        predicates = _predicates(query, scope)
        fingerprint = service.cursors.fingerprint(query, scope, "logical-requests-v1")
        cursor = service.cursors.decode(query.cursor, fingerprint)
        now = datetime.now(UTC)
        fence = int(await session.scalar(select(func.max(row.row_id))) or 0)
        if cursor is not None:
            try:
                fence, scanned_at, last_at, last_id = cursor
                fence, last_id = int(fence), int(last_id)
                now, last_at = datetime.fromisoformat(scanned_at), datetime.fromisoformat(last_at)
            except (ValueError, TypeError) as exc:
                raise UsageQueryError("invalid_usage_cursor", "Invalid request position") from exc
            predicates.append(
                or_(row.started_at < last_at, and_(row.started_at == last_at, row.row_id < last_id))
            )
        predicates.extend([row.row_id <= fence, row.registered_at <= now])
        if query.selection_ref:
            selection = read_selection(service.cursors, scope, query.selection_ref)
            if utc(query.start) < selection.start or utc(query.end) > selection.end:
                raise UsageQueryError(
                    "usage_selection_window", "Selection cannot expand its window"
                )
            if selection.subject_key is not None:
                predicates.append(row.usage_subject_key == selection.subject_key)
            if selection.kind == "request":
                predicates.append(row.request_id == selection.request_id)
                if selection.owner_key is not None:
                    predicates.append(row.owner_key == selection.owner_key)
            else:
                attempt = UsageAttemptRow
                predicates.append(
                    select(1)
                    .where(
                        attempt.owner_key == row.owner_key,
                        attempt.usage_subject_key == row.usage_subject_key,
                        attempt.request_id == row.request_id,
                        *selection_predicates(attempt, query, scope, service.cursors),
                    )
                    .exists()
                )
        headers = list(
            (
                await session.scalars(
                    select(row)
                    .where(*predicates)
                    .order_by(row.started_at.desc(), row.row_id.desc())
                    .limit(query.limit + 1)
                )
            ).all()
        )
        has_more, headers = len(headers) > query.limit, headers[: query.limit]
        if headers:
            meta.availability.resolution = "retained"
        attempt_fence = await selection_boundary(session, query, scope, service.cursors)
        attempts, stats = await _hydrate(
            session,
            headers,
            scope,
            query.pricing_basis,
            fence=attempt_fence,
            recorded_before=now,
        )
        items = []
        labels = await group_labels(session, scope, "session", [h.session_id for h in headers])
        for header in headers:
            selection_ref = mint_selection(
                service.cursors,
                scope,
                UsageSelection(
                    kind="request",
                    subject_key=header.usage_subject_key,
                    owner_key=header.owner_key,
                    request_id=header.request_id,
                    start=utc(query.start),
                    end=utc(query.end),
                    max_row_id=attempt_fence,
                    recorded_before=now,
                ),
            )
            items.append(
                _item(
                    header,
                    attempts.get(_identity(header), []),
                    stats.get(_identity(header)),
                    scope,
                    selection_ref,
                    labels.get(header.session_id),
                )
            )
        next_cursor = None
        if has_more and headers:
            last = headers[-1]
            next_cursor = service.cursors.encode(
                fingerprint, [fence, now.isoformat(), utc(last.started_at).isoformat(), last.row_id]
            )
        return UsageRequestPage(
            meta=meta, items=items, next_cursor=next_cursor, pagination_unit="request"
        )


async def request_detail(
    service: Any, actor: str, query: Any, *, invocation: Any = None, actor_role: str | None = None
) -> UsageRequestDetail:
    """Resolve an encrypted request selection under current ownership/resource SQL."""
    async with service._request(actor, query, invocation, actor_role) as (session, scope):
        meta = await source_meta(
            session, scope, start=query.start, end=query.end, precision="exact"
        )
        selection = read_selection(service.cursors, scope, query.selection_ref)
        if meta.projection.state != "ready":
            return UsageRequestDetail(meta=meta)
        if selection.kind != "request" or selection.request_id is None:
            raise UsageQueryError("invalid_usage_cursor", "A request selection is required")
        if utc(query.start) < selection.start or utc(query.end) > selection.end:
            raise UsageQueryError("usage_selection_window", "Selection cannot expand its window")
        row = UsageRequestLogRow
        predicates = [
            *_predicates(query, scope),
            row.request_id == selection.request_id,
            row.usage_subject_key == selection.subject_key,
            row.registered_at <= selection.recorded_before,
        ]
        if selection.owner_key is not None:
            predicates.append(row.owner_key == selection.owner_key)
        header = await session.scalar(select(row).where(*predicates).limit(1))
        if header is None:
            return UsageRequestDetail(meta=meta)
        attempts, stats = await _hydrate(
            session,
            [header],
            scope,
            query.pricing_basis,
            fence=selection.max_row_id,
            recorded_before=selection.recorded_before,
        )
        labels = await group_labels(session, scope, "session", [header.session_id])
        item = _item(
            header,
            attempts.get(_identity(header), []),
            stats.get(_identity(header)),
            scope,
            query.selection_ref,
            labels.get(header.session_id),
        )
        # The browsing window selects the logical header, not its later retries.
        # Keep the request identity, current authorization and registration fence.
        attempt = UsageAttemptRow
        terms = [
            scope_predicate(attempt, scope),
            attempt.owner_key == header.owner_key,
            attempt.usage_subject_key == header.usage_subject_key,
            attempt.request_id == header.request_id,
            attempt.row_id <= selection.max_row_id,
            attempt.registered_at <= selection.recorded_before,
        ]
        fingerprint = service.cursors.fingerprint(query, scope, "logical-request-attempts-v1")
        position = service.cursors.decode(query.cursor, fingerprint)
        if position is not None:
            try:
                last_at, last_id = datetime.fromisoformat(position[0]), int(position[1])
            except (ValueError, TypeError, IndexError) as exc:
                raise UsageQueryError("invalid_usage_cursor", "Invalid attempt position") from exc
            terms.append(
                or_(
                    attempt.started_at_observed < last_at,
                    and_(attempt.started_at_observed == last_at, attempt.row_id < last_id),
                )
            )
        rows = list(
            (
                await session.execute(
                    retained_attempt_statement(query.pricing_basis)
                    .where(*terms)
                    .order_by(attempt.started_at_observed.desc(), attempt.row_id.desc())
                    .limit(query.limit + 1)
                )
            ).all()
        )
        has_more, rows = len(rows) > query.limit, rows[: query.limit]
        details = [
            attempt_detail(
                raw,
                valuation,
                stats,
                basis=query.pricing_basis,
                private=scope.sensitive_provenance,
                price_revision=price,
                selection_ref=query.selection_ref,
            )
            for raw, valuation, stats, price in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1][0]
            next_cursor = service.cursors.encode(
                fingerprint, [utc(last.started_at_observed).isoformat(), last.row_id]
            )
        if header is not None:
            meta.availability.resolution = "retained"
        return UsageRequestDetail(
            meta=meta, request=item, attempts=details, next_cursor=next_cursor
        )
