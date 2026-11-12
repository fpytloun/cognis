"""Content-free diagnostics for Usage HTTP reads.

Usage errors can wrap SQL parameters or arbitrary upstream payloads. Only
protocol metadata and fixed reason templates cross this logging boundary.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from cognis.core.usage.access import UsageAccessError
from cognis.core.usage.availability import UsageResolutionError
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.providers.llm.usage_normalization import safe_error

logger = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_OPERATIONS = {
    "catalog": "Load usage filters",
    "query": "Load usage",
    "attempts": "Load attempts",
    "attempt_search": "Load attempts",
    "requests": "Load request log",
    "request_detail": "Load request details",
    "sessions": "Load sessions",
    "summaries": "Load usage summaries",
    "facets": "Load filter choices",
    "history": "Load limit history",
}
_REASONS = {
    "resource_detail_expired": "Older work details expired. Choose a recent range.",
    "query_budget_exceeded": "Query limit reached. Narrow the date range or filters.",
    "usage_source_incompatible": "Stored usage needs an update.",
    "usage_pending_index_unavailable": "In-progress counts are unavailable.",
    "usage_index_unavailable": "Usage search is unavailable.",
    "usage_provider_unavailable": "Provider history is unavailable.",
    "mixed_cost_ranking": "Choose one currency and pricing basis.",
    "incomplete_cost_ranking": "Choose fully priced requests for cost ranking.",
}


def _id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def usage_http_error(request: Request, error: Exception) -> HTTPException:
    """Log one bounded diagnostic and return its request-linked safe reason."""
    route = request.scope.get("route")
    operation = _OPERATIONS.get(getattr(route, "name", ""), "Load usage")
    correlation_id = uuid4().hex
    request_id = _id(getattr(request.state, "request_id", None))
    upstream: BaseException = error
    seen: set[int] = set()
    while upstream.__cause__ is not None and id(upstream) not in seen:
        seen.add(id(upstream))
        upstream = upstream.__cause__
    normalized = safe_error(upstream)
    target = "usage-store"
    upstream_id = None
    if isinstance(upstream, (httpx.HTTPStatusError, httpx.RequestError)):
        target = upstream.request.url.host
        if isinstance(upstream, httpx.HTTPStatusError):
            upstream_id = _id(upstream.response.headers.get("x-request-id"))
    sqlstate = _id(getattr(getattr(upstream, "orig", None), "sqlstate", None))
    code, status = "usage_query_failed", 500
    if isinstance(error, UsageAccessError):
        code, status, reason = "usage_access_denied", 403, "Choose a scope you can access."
    elif isinstance(error, (UsageQueryError, UsageResolutionError)):
        code = error.code
        status = error.status if isinstance(error, UsageQueryError) else 422
        reason = _REASONS.get(
            code, "Selection unavailable. Reset filters or choose a recent range."
        )
    elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
        code, status, reason = (
            "usage_timeout",
            504,
            "The request timed out. Try a shorter date range.",
        )
    elif isinstance(error, SQLAlchemyError):
        reason = "The usage database is unavailable. Try again."
    elif isinstance(error, httpx.HTTPError):
        status = 502
        reason = "The usage service is unavailable. Try again."
    else:
        reason = "An unexpected error interrupted the query. Try again."
    # Fixed status phrase is the bounded, fully redacted upstream excerpt.
    # Free-form response bodies and exception strings are intentionally excluded.
    upstream_status = normalized.http_status
    excerpt = (
        HTTPStatus(upstream_status).phrase
        if upstream_status is not None and upstream_status in HTTPStatus._value2member_map_
        else normalized.summary_code
    )
    logger.warning(
        "Usage operation failed",
        extra={
            "extra_data": {
                "operation": operation,
                "target": target,
                "status": status,
                "upstream_status": upstream_status,
                "upstream_error": excerpt,
                "error_code": code,
                "error_class": type(error).__name__,
                "cause_class": type(upstream).__name__,
                "sqlstate": sqlstate,
                "correlation_id": correlation_id,
                "request_id": request_id,
                "upstream_request_id": upstream_id,
                "attempt": 1,
            }
        },
    )
    return HTTPException(
        status_code=status,
        detail={
            "code": code,
            "message": reason,
            "details": {"operation": operation, "correlation_id": correlation_id},
        },
    )
