"""Authenticated Library metadata browsing; existing routes serve content."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from cognis.api.common import api_exception, require_current_user
from cognis.api.models import DeliverableResponse
from cognis.api.serializers import deliverable_to_response
from cognis.core import library
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.document_favorites import set_favorite
from cognis.store.queries import get_deliverable, get_deliverable_lineage_summary

router = APIRouter(prefix="/api/v1/library", tags=["library"])


class FavoriteRequest(BaseModel):
    """Desired personal bookmark state."""

    favorite: bool


class CommentCountsRequest(BaseModel):
    """Exact document references visible in a human-facing listing."""

    refs: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=100)


class CommentCountsResponse(BaseModel):
    """Open, non-hidden comments across revisions, disclosed only to the owner."""

    counts: dict[str, int]


@router.post("/comment-counts", response_model=CommentCountsResponse)
async def library_comment_counts(
    request: Request, body: CommentCountsRequest
) -> CommentCountsResponse:
    """Read counts without loading document payloads or creating feedback subjects."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        counts = await library.document_comment_counts(session, user.email, body.refs)
    return CommentCountsResponse(counts=counts)


@router.put("/items/{kind}/{ref}/favorite", response_model=library.LibraryItem)
async def library_favorite(
    request: Request, kind: Literal["deliverable", "artifact"], ref: str, body: FavoriteRequest
) -> library.LibraryItem:
    """Bookmark readable content without changing its grants or revision metadata."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        item = await library.item(session, user.email, kind, ref)
        if item is None:
            raise api_exception(404, "not_found", "Item unavailable or no longer accessible")
        await set_favorite(session, user.email, kind, item.lineage_id or item.id, body.favorite)
        await session.commit()
        return item.model_copy(update={"is_favorite": body.favorite})


@router.get("/items", response_model=library.LibraryPage)
async def library_browse(
    request: Request,
    filters: Annotated[library.LibraryFilters, Depends()],
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=40, ge=1, le=100),
) -> library.LibraryPage:
    """Browse current deliverable or file revisions across accessible work."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        try:
            return await library.browse(session, user.email, filters, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise api_exception(400, "invalid_cursor", str(exc)) from exc


@router.get("/groups", response_model=library.LibraryGroupsPage)
async def library_groups(
    request: Request,
    filters: Annotated[library.LibraryFilters, Depends()],
    group_by: Literal["project", "conversation", "agent", "format"] = "project",
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=40, ge=1, le=100),
) -> library.LibraryGroupsPage:
    """Lazy project/conversation folders with authorized matching-item counts."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        try:
            return await library.groups(
                session, user.email, filters, group_by=group_by, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise api_exception(400, "invalid_cursor", str(exc)) from exc


@router.get("/items/{kind}/{ref}", response_model=library.LibraryItem)
async def library_item(
    request: Request, kind: Literal["deliverable", "artifact"], ref: str
) -> library.LibraryItem:
    """Resolve metadata by exact ID without depending on a recent list."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        item = await library.item(session, user.email, kind, ref)
    if item is None:
        raise api_exception(404, "not_found", "Item unavailable or no longer accessible")
    return item


@router.get("/deliverables/{ref}/content", response_model=DeliverableResponse)
async def library_deliverable_content(request: Request, ref: str) -> DeliverableResponse:
    """Read an exact deliverable as a person, including task and detached outputs."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        metadata = await library.item(session, user.email, "deliverable", ref)
        row = await get_deliverable(session, ref) if metadata else None
        if row is None or metadata is None:
            raise api_exception(404, "not_found", "Deliverable not found")
        lineage = await get_deliverable_lineage_summary(session, row)
    try:
        await hydrate_deliverable_payload(row, request.app.state.artifact_store)
    except FileNotFoundError as exc:
        raise api_exception(
            409, "deliverable_payload_unavailable", "Deliverable payload is unavailable"
        ) from exc
    response = deliverable_to_response(row, lineage=lineage)
    if metadata.is_readonly:
        response.conversation_id = metadata.conversation_id
        response.session_id = None
        response.turn_id = None
        if metadata.task_id is None:
            response.step_run_id = None
    return response
