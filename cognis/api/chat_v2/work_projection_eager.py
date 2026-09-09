"""Transactional eager initialization for per-session Work projections."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.store.models import Session, StepRun, WorkSessionProjectionRow


def _projection_id(session_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{WORK_MATERIALIZER_VERSION}".encode()).hexdigest()[:40]
    return f"wsp_{digest}"


async def initialize_session_work_projection(
    db: AsyncSession,
    session_row: Session,
) -> bool:
    """Create one zero projection without replacing newer session state."""

    if not session_row.intaris_session_id:
        return False
    await db.scalar(
        select(Session).where(Session.session_id == session_row.session_id).with_for_update()
    )
    existing = await db.scalar(
        select(WorkSessionProjectionRow).where(
            WorkSessionProjectionRow.session_id == session_row.session_id,
            WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
        )
    )
    if existing is not None:
        return False
    db.add(
        WorkSessionProjectionRow(
            projection_id=_projection_id(session_row.session_id),
            owner_email=session_row.user_email,
            session_id=session_row.session_id,
            source_session_id=str(session_row.intaris_session_id),
            materializer_version=WORK_MATERIALIZER_VERSION,
            state="caught_up",
            target_seq=0,
            covered_through_seq=0,
            next_head_check_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    )
    await db.flush()
    return True


async def initialize_step_work_projection(db: AsyncSession, step: StepRun) -> bool:
    """Initialize an assigned task-step session projection, if one exists."""

    if not step.session_id:
        return False
    session_row = await db.get(Session, step.session_id)
    if session_row is None:
        return False
    return await initialize_session_work_projection(db, session_row)
