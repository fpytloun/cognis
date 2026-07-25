"""Test-only Chat v2 control-plane routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.chat_v2.sync import advance_projection_generation
from cognis.api.common import require_current_user

router = APIRouter(prefix="/api/v1/chat/v2/e2e", tags=["chat-v2-e2e"])


@router.post("/projection-generation")
async def advance_chat_v2_projection_generation(request: Request) -> dict[str, str]:
    """Advance server projection state; this router is mounted only in E2E mode."""
    require_current_user(request)
    return {"projection_version": advance_projection_generation()}
