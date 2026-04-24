"""Session routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_resource_owner
from cognis.api.models import SessionCancelResponse, SessionEventsResponse, SessionResponse
from cognis.api.serializers import serialize_event_rows, session_to_response
from cognis.store.queries import get_session_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("/{session_id}", response_model=SessionResponse)
async def session_detail(request: Request, session_id: str) -> SessionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_resource_owner(request, row.user_email)
    return session_to_response(row)


@router.get("/{session_id}/intaris")
async def session_intaris_detail(request: Request, session_id: str) -> dict[str, Any]:
    """Fetch Intaris session details (intention, call stats) for a session."""
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_resource_owner(request, row.user_email)
    intaris_sid = row.intaris_session_id or row.session_id
    try:
        intaris_session = await request.app.state.providers.guardrails.get_session(intaris_sid)
        return {
            "session_id": session_id,
            "intaris_session_id": intaris_sid,
            "intention": intaris_session.intention,
            "status": intaris_session.status,
            "total_calls": intaris_session.total_calls,
            "approved_count": intaris_session.approved_count,
            "denied_count": intaris_session.denied_count,
            "escalated_count": intaris_session.escalated_count,
        }
    except Exception as exc:
        logger.warning("Failed to fetch Intaris session %s", intaris_sid, exc_info=True)
        raise api_exception(
            502, "intaris_unavailable", "Unable to fetch session details from Intaris"
        ) from exc


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
    require_resource_owner(request, row.user_email)
    result = await request.app.state.providers.guardrails.read_events(
        session_id=row.intaris_session_id or row.session_id,
        after_seq=after_seq,
        limit=limit,
        allow_missing_stream=True,
    )
    if result.missing_stream_fallback_used:
        logger.warning(
            "Session history missing in Intaris; returning empty history",
            extra={
                "extra_data": {
                    "session_id": row.session_id,
                    "intaris_session_id": row.intaris_session_id or row.session_id,
                }
            },
        )
    return SessionEventsResponse(
        session_id=session_id,
        items=serialize_event_rows(
            result.events,
            log_label="session_events",
            log_context={"session_id": row.session_id},
        ),
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
        require_resource_owner(request, row.user_email)
    ok = await request.app.state.session_manager.mark_cancelled(
        session_id,
        result_summary="cancelled via API",
    )
    return SessionCancelResponse(ok=ok, session_id=session_id)
