"""Thin authenticated usage adapters; composition owns the service instance."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from cognis.api.common import require_current_user
from cognis.api.usage_errors import usage_http_error
from cognis.core.usage.access import UsageAccessError
from cognis.core.usage.availability import UsageResolutionError
from cognis.core.usage.query_models import (
    ProviderQuotaHistory,
    ProviderQuotaHistoryQuery,
    UsageAttemptPage,
    UsageAttemptQuery,
    UsageCatalog,
    UsageCatalogRequest,
    UsageFacetRequest,
    UsageFacetResponse,
    UsageQuery,
    UsageQueryResponse,
    UsageRequestDetail,
    UsageRequestDetailQuery,
    UsageRequestPage,
    UsageRequestQuery,
    UsageSessionPage,
    UsageSessionQuery,
    UsageSummaryRequest,
    UsageSummaryResponse,
)
from cognis.core.usage.query_service import UsageQueryService
from cognis.core.usage.query_service_errors import UsageQueryError


async def _response(operation: Awaitable[Any], request: Request) -> Any:
    """Stable, content-free failures shared with direct tool error categories."""
    try:
        result = await operation
    except UsageAccessError as exc:
        raise usage_http_error(request, exc) from exc
    except UsageQueryError as exc:
        raise usage_http_error(request, exc) from exc
    except UsageResolutionError as exc:
        raise usage_http_error(request, exc) from exc
    except Exception as exc:
        raise usage_http_error(request, exc) from exc
    if hasattr(result, "meta") and result.meta.availability.resolution == "detail_expired":
        return JSONResponse(status_code=410, content=result.model_dump(mode="json"))
    return result


def _quota_history_query(
    request: Request,
    start: str,
    end: str,
    scope: str | None = None,
    window: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
) -> ProviderQuotaHistoryQuery:
    """Adapt textual HTTP parameters before strict domain validation."""
    pairs = request.query_params.multi_items()
    if len(pairs) != len({key for key, _ in pairs}):
        raise HTTPException(status_code=422, detail="Duplicate query parameter")
    values: dict[str, Any] = dict(pairs)
    values.update(start=start, end=end)
    if scope is not None:
        try:
            values["scope"] = json.loads(scope)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(status_code=422, detail="Invalid scope JSON") from exc
    if limit is not None:
        if not re.fullmatch(r"[+-]?[0-9]{1,10}", limit):
            raise HTTPException(status_code=422, detail="Invalid limit")
        values["limit"] = int(limit)
    if window is not None:
        values["window"] = window
    if cursor is not None:
        values["cursor"] = cursor
    try:
        return ProviderQuotaHistoryQuery.model_validate(values)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def create_usage_router(service: UsageQueryService) -> APIRouter:
    """Mount this returned router at /api/v1; it owns no lifecycle or storage."""
    router = APIRouter(prefix="/usage", tags=["usage"])

    @router.post("/catalog", response_model=UsageCatalog)
    async def catalog(
        body: UsageCatalogRequest, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.catalog(user.email, body, actor_role=user.role), request)

    @router.post("/query", response_model=UsageQueryResponse)
    async def query(
        body: UsageQuery, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.query(user.email, body, actor_role=user.role), request)

    @router.get("/attempts", response_model=UsageAttemptPage)
    async def attempts(
        request: Request,
        query: Annotated[UsageAttemptQuery, Query()],
        user: Any = Depends(require_current_user),
    ) -> Any:
        return await _response(service.attempts(user.email, query, actor_role=user.role), request)

    # POST forms support nested authorized filters without bespoke query-string JSON.
    @router.post("/attempts/search", response_model=UsageAttemptPage)
    async def attempt_search(
        body: UsageAttemptQuery, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.attempts(user.email, body, actor_role=user.role), request)

    @router.post("/requests", response_model=UsageRequestPage)
    async def requests(
        body: UsageRequestQuery, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.requests(user.email, body, actor_role=user.role), request)

    @router.post("/requests/detail", response_model=UsageRequestDetail)
    async def request_detail(
        body: UsageRequestDetailQuery, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(
            service.request_detail(user.email, body, actor_role=user.role), request
        )

    @router.post("/sessions", response_model=UsageSessionPage)
    async def sessions(
        body: UsageSessionQuery, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.sessions(user.email, body, actor_role=user.role), request)

    @router.post("/session-summaries", response_model=UsageSummaryResponse)
    async def summaries(
        body: UsageSummaryRequest, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(
            service.session_summaries(user.email, body, actor_role=user.role), request
        )

    @router.post("/facets", response_model=UsageFacetResponse)
    async def facets(
        body: UsageFacetRequest, request: Request, user: Any = Depends(require_current_user)
    ) -> Any:
        return await _response(service.facets(user.email, body, actor_role=user.role), request)

    @router.get("/providers/{provider_id}/history", response_model=ProviderQuotaHistory)
    async def history(
        provider_id: str,
        request: Request,
        query: ProviderQuotaHistoryQuery = Depends(_quota_history_query),
        user: Any = Depends(require_current_user),
    ) -> Any:
        return await _response(
            service.provider_history(user.email, provider_id, query, actor_role=user.role), request
        )

    return router
