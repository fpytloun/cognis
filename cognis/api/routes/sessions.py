"""Session routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_resource_owner
from cognis.api.models import (
    IntarisSessionDetailResponse,
    SessionCancelResponse,
    SessionResponse,
)
from cognis.api.serializers import session_to_response
from cognis.models.config import GenerationPerformanceSnapshot
from cognis.store.queries import get_session_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _context_usage_for_session(request: Request, session_id: str) -> dict[str, Any] | None:
    """Return latest cached context/projection diagnostics for a session."""

    session_cache = getattr(request.app.state, "session_cache", None)
    if session_cache is None:
        return None
    try:
        return session_cache.get_context_usage(session_id)
    except Exception:
        logger.debug(
            "Failed to fetch cached context usage for session %s", session_id, exc_info=True
        )
        return None


def _last_generation_for_session(
    request: Request, session_id: str
) -> GenerationPerformanceSnapshot | None:
    session_cache = getattr(request.app.state, "session_cache", None)
    if session_cache is None or not hasattr(session_cache, "get_last_generation_performance"):
        return None
    try:
        raw = session_cache.get_last_generation_performance(session_id)
        return GenerationPerformanceSnapshot.model_validate(raw) if raw is not None else None
    except Exception:
        logger.debug(
            "Failed to fetch latest generation performance for session %s",
            session_id,
            exc_info=True,
        )
        return None


def _latest_intaris_summary_text(summaries: Any) -> str | None:
    """Pick the best display summary from Intaris' combined summary payload."""

    for items in (summaries.intaris_summaries, summaries.agent_summaries):
        for item in items:
            text = (item.summary or "").strip()
            if text:
                return text
    return None


@router.get("/{session_id}", response_model=SessionResponse)
async def session_detail(request: Request, session_id: str) -> SessionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_resource_owner(request, row.user_email)
    return session_to_response(row)


@router.get("/{session_id}/intaris", response_model=IntarisSessionDetailResponse)
async def session_intaris_detail(request: Request, session_id: str) -> IntarisSessionDetailResponse:
    """Fetch Intaris session details (intention, call stats) for a session."""
    async with request.app.state.session_factory() as session:
        row = await get_session_row(session, session_id)
    if row is None:
        raise api_exception(404, "not_found", "Session not found")
    require_resource_owner(request, row.user_email)
    intaris_sid = row.intaris_session_id or row.session_id
    try:
        guardrails = request.app.state.providers.guardrails
        intaris_session = await guardrails.get_session(intaris_sid)
        summary: str | None = None
        try:
            summaries = await guardrails.get_session_summaries(intaris_sid)
            summary = _latest_intaris_summary_text(summaries)
        except Exception:
            logger.debug(
                "Failed to fetch Intaris summaries for session %s",
                intaris_sid,
                exc_info=True,
            )
        return IntarisSessionDetailResponse(
            session_id=session_id,
            intaris_session_id=intaris_sid,
            title=intaris_session.title,
            intention=intaris_session.intention,
            summary=summary,
            status=intaris_session.status,
            total_calls=intaris_session.total_calls,
            approved_count=intaris_session.approved_count,
            denied_count=intaris_session.denied_count,
            escalated_count=intaris_session.escalated_count,
            context_usage=_context_usage_for_session(request, row.session_id),
            last_generation=_last_generation_for_session(request, row.session_id),
        )
    except Exception as exc:
        logger.warning("Failed to fetch Intaris session %s", intaris_sid, exc_info=True)
        raise api_exception(
            502, "intaris_unavailable", "Unable to fetch session details from Intaris"
        ) from exc


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
