"""Session routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_owner_or_admin
from cognis.api.models import SessionCancelResponse, SessionEventsResponse, SessionResponse
from cognis.api.serializers import event_to_response, session_to_response
from cognis.store.queries import get_session_row, set_session_status

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("/{session_id}", response_model=SessionResponse)
async def session_detail(request: Request, session_id: str) -> SessionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_owner_or_admin(request, row.user_email)
    return session_to_response(row)


@router.get("/{session_id}/events", response_model=SessionEventsResponse)
async def session_events(
    request: Request,
    session_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> SessionEventsResponse:
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_owner_or_admin(request, row.user_email)
    result = await request.app.state.providers.guardrails.read_events(
        session_id=row.intaris_session_id or row.session_id,
        after_seq=after_seq,
        limit=limit,
    )
    return SessionEventsResponse(
        session_id=session_id,
        items=[event_to_response(item) for item in result.events],
        last_seq=result.last_seq,
        has_more=result.has_more,
    )


@router.post("/{session_id}/cancel", response_model=SessionCancelResponse)
async def cancel_session(request: Request, session_id: str) -> SessionCancelResponse:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
        if row is None:
            raise api_exception(404, "not_found", "Session not found")
        require_owner_or_admin(request, row.user_email)
        ok = await set_session_status(
            session,
            session_id,
            "cancelled",
            completed_at=datetime.now(UTC),
            result_summary="cancelled via API",
        )
        await session.commit()
    await request.app.state.session_cache.evict(session_id)
    return SessionCancelResponse(ok=ok, session_id=session_id)
