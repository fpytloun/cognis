"""Authenticated, revision-pinned deliverable audio jobs."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.core.audio_context import document_audio_context
from cognis.core.audio_jobs import AudioJobService
from cognis.core.speech_models import (
    AudioJobCreate,
    AudioJobView,
    DocumentAudioContext,
    PodcastLengthEstimate,
)

router = APIRouter(prefix="/api/v1/audio/jobs", tags=["audio"])


def _service(request: Request) -> AudioJobService:
    return cast(AudioJobService, request.app.state.audio_jobs)


@router.post("", response_model=AudioJobView, status_code=202)
async def create_audio_job(request: Request, payload: AudioJobCreate) -> AudioJobView:
    """Queue narration or a narrative podcast without holding the request open."""
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        return await _service(request).create(user.email, payload)
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Deliverable not found") from exc
    except ValueError as exc:
        raise api_exception(409, "conflict", str(exc)) from exc


@router.get("", response_model=list[AudioJobView])
async def list_audio_jobs(
    request: Request, deliverable_id: str = Query(pattern=r"^dlv_[0-9a-f]{32}$")
) -> list[AudioJobView]:
    """Restore work and playback for the selected revision."""
    user = require_current_user(request)
    try:
        return await _service(request).latest(user.email, deliverable_id)
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Deliverable not found") from exc


@router.get("/source", response_model=DocumentAudioContext)
async def source_audio_context(request: Request, content_ref: str) -> DocumentAudioContext:
    """Inspect audio capabilities without inference or voice discovery."""
    user = require_current_user(request)
    can_mutate = True
    try:
        forbid_mutation_for_viewer(request)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        can_mutate = False
    try:
        return await document_audio_context(
            _service(request), user.email, content_ref, can_mutate=can_mutate
        )
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Document not found") from exc


@router.get("/estimates", response_model=list[PodcastLengthEstimate])
async def audio_length_estimates(request: Request, content_ref: str) -> list[PodcastLengthEstimate]:
    """Read-only source estimates, independent of cancellation and provider availability."""
    user = require_current_user(request)
    try:
        return await _service(request).estimates(user.email, content_ref)
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Document not found") from exc
    except ValueError as exc:
        raise api_exception(422, "invalid_source", str(exc)) from exc


@router.get("/{job_id}", response_model=AudioJobView)
async def get_audio_job(request: Request, job_id: str) -> AudioJobView:
    """Read progress without granting access through knowledge of a job ID."""
    user = require_current_user(request)
    try:
        return await _service(request).view(user.email, job_id)
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Audio job not found") from exc


@router.post("/{job_id}/cancel", response_model=AudioJobView)
async def cancel_audio_job(request: Request, job_id: str) -> AudioJobView:
    """Fence subsequent synthesis and progress writes."""
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        return await _service(request).cancel(user.email, job_id)
    except PermissionError as exc:
        raise api_exception(404, "not_found", "Audio job not found") from exc
