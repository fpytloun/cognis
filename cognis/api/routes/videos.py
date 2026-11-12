"""Authenticated video job and owner spending-policy endpoints."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from cognis.api.common import forbid_mutation_for_viewer, require_current_user, require_session_user
from cognis.models.video import (
    VideoAnalysis,
    VideoJobCreate,
    VideoMutation,
    VideoRecovery,
    VideoSpendPolicy,
)
from cognis.store.models import Setting

router = APIRouter(prefix="/api/v1/videos", tags=["videos"])


@router.put("/transfer")
async def receive_transfer(request: Request) -> dict[str, str]:
    from cognis.core.video_transfer import VideoTransfers

    return await invoke(
        VideoTransfers(request.app.state.video_service).receive(
            request.headers.get("X-Cognis-Video-Transfer"),
            request.stream(),
        )
    )


def actor(request: Request, *, mutation: bool = False) -> str:
    if mutation:
        forbid_mutation_for_viewer(request)
    return require_current_user(request).email


async def invoke[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except PermissionError as exc:
        raise HTTPException(404, "Video resource not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/models")
async def models(request: Request, model: str | None = None, provider_id: str | None = None) -> Any:
    return await invoke(request.app.state.video_service.models(actor(request), model, provider_id))


@router.post("/analyze")
async def analyze(request: Request, body: VideoAnalysis, conversation_id: str) -> Any:
    return await invoke(
        request.app.state.video_service.analyze(
            actor(request, mutation=True),
            conversation_id,
            body,
        )
    )


@router.get("/policy")
async def policy(request: Request) -> VideoSpendPolicy:
    return VideoSpendPolicy.model_validate(
        await request.app.state.video_service.policy(actor(request))
    )


@router.put("/policy")
async def set_policy(request: Request, policy: VideoSpendPolicy) -> VideoSpendPolicy:
    owner = actor(request, mutation=True)
    require_session_user(request)
    key = f"video.policy.{owner}"
    async with request.app.state.video_service.sessions() as session:
        row = await session.scalar(select(Setting).where(Setting.key == key))
        if row is None:
            row = Setting(
                key=key, value=policy.model_dump(mode="json"), category="video", updated_by=owner
            )
            session.add(row)
        else:
            row.value = policy.model_dump(mode="json")
        await session.commit()
    return policy


@router.post("/jobs")
async def create(request: Request, body: VideoJobCreate) -> Any:
    return await invoke(request.app.state.video_service.create(actor(request, mutation=True), body))


@router.get("/jobs")
async def jobs(request: Request, conversation_id: str, before: str | None = None) -> Any:
    return await invoke(
        request.app.state.video_service.list(actor(request), conversation_id, before)
    )


@router.get("/jobs/{job_id}")
async def get(
    request: Request,
    job_id: str,
    wait_seconds: int = 0,
    conversation_id: str | None = None,
    inspect_provider_error: bool = False,
) -> dict[str, Any]:
    return await invoke(
        request.app.state.video_service.get(
            actor(request),
            job_id,
            wait_seconds=wait_seconds,
            conversation_id=conversation_id,
            inspect_provider_error=inspect_provider_error,
        )
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str, body: VideoMutation) -> Any:
    return await invoke(
        request.app.state.video_service.cancel(actor(request, mutation=True), job_id, body)
    )


@router.post("/jobs/{job_id}/recover")
async def recover(request: Request, job_id: str, body: VideoRecovery) -> Any:
    return await invoke(
        request.app.state.video_service.recover(actor(request, mutation=True), job_id, body)
    )
