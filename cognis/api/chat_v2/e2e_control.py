"""Test-only Chat v2 control-plane routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from cognis.api.chat_v2.sync import advance_projection_generation
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.common import require_current_user
from cognis.store.models import Conversation, Session, WorkSessionProjectionRow

router = APIRouter(prefix="/api/v1/chat/v2/e2e", tags=["chat-v2-e2e"])


class WorkCatchingUpFixtureRequest(BaseModel):
    conversation_id: str


@router.post("/projection-generation")
async def advance_chat_v2_projection_generation(request: Request) -> dict[str, str]:
    """Advance server projection state; this router is mounted only in E2E mode."""
    require_current_user(request)
    return {"projection_version": advance_projection_generation()}


@router.post("/work-background-repair/resume")
async def resume_work_background_repair(
    request: Request,
    payload: WorkCatchingUpFixtureRequest,
) -> dict[str, str]:
    """Resume one owned Work projection; mounted only in E2E mode."""

    user = require_current_user(request)
    projection_id = await _owned_projection_id(request, payload.conversation_id, user.email)
    request.app.state.work_materializer.resume_background_repair(projection_id)
    return {"state": "running"}


@router.post("/work-catching-up")
async def prepare_work_catching_up_fixture(
    request: Request,
    payload: WorkCatchingUpFixtureRequest,
) -> dict[str, int | str]:
    """Pause repair and rewind one seeded cursor; mounted only in E2E mode."""

    user = require_current_user(request)
    materializer = request.app.state.work_materializer
    projection_id = await _owned_projection_id(request, payload.conversation_id, user.email)
    await materializer.pause_background_repair(projection_id)
    async with request.app.state.session_factory() as db:
        try:
            row = await db.get(WorkSessionProjectionRow, projection_id)
            if row is None or row.target_seq < 1:
                raise ValueError("Seeded Work projection has no source events")
            # Replay the complete seeded source. Rewinding only one event is not
            # repeatable after an earlier test has replaced projection records.
            row.covered_through_seq = 0
            row.state = "repair"
            row.priority = 0
            row.next_retry_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            await db.commit()
            return {
                "state": "paused",
                "covered_through_seq": row.covered_through_seq,
                "target_seq": row.target_seq,
            }
        except BaseException:
            materializer.resume_background_repair(projection_id)
            raise


async def _owned_projection_id(request: Request, conversation_id: str, email: str) -> str:
    async with request.app.state.session_factory() as db:
        projection_id = await db.scalar(
            select(WorkSessionProjectionRow.projection_id)
            .join(Session, Session.session_id == WorkSessionProjectionRow.session_id)
            .join(Conversation, Conversation.conversation_id == Session.conversation_id)
            .where(
                Conversation.conversation_id == conversation_id,
                Conversation.user_email == email,
                WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
            )
        )
    if projection_id is None:
        raise ValueError("Seeded Work projection does not exist")
    return str(projection_id)
