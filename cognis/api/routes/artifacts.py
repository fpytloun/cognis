"""Generic artifact upload and signed serving routes."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.models.artifact import ArtifactKind
from cognis.store.queries import create_artifact_record, get_artifact_record

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


def _kind_for_content_type(content_type: str) -> ArtifactKind:
    if content_type.startswith("image/"):
        return ArtifactKind.IMAGE
    if content_type.startswith("audio/"):
        return ArtifactKind.AUDIO
    if content_type.startswith("video/"):
        return ArtifactKind.VIDEO
    if content_type == "application/pdf":
        return ArtifactKind.PDF
    return ArtifactKind.FILE


@router.post("/upload")
async def upload_artifact(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    purpose: Annotated[str, Form()] = "chat_input",
) -> dict[str, object]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    artifact_store = request.app.state.artifact_store

    content = await file.read()
    max_size = artifact_store._config.max_size_bytes  # noqa: SLF001
    if len(content) > max_size:
        raise api_exception(400, "validation_error", "Artifact too large")

    artifact_id = artifact_store.generate_id("att")
    filename = file.filename or "attachment"
    content_type = (
        file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    kind = _kind_for_content_type(content_type)

    await artifact_store.async_save(
        "attachments",
        artifact_id,
        filename,
        content,
        content_type,
        owner_email=user.email,
    )

    async with request.app.state.session_factory() as session:
        await create_artifact_record(
            session,
            artifact_id=artifact_id,
            namespace="attachments",
            object_id=artifact_id,
            filename=filename,
            owner_email=user.email,
            purpose=purpose,
            kind=kind.value,
            mime_type=content_type,
            size_bytes=len(content),
            status="temporary",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        await session.commit()

    signed_url = await artifact_store.async_get_public_url("attachments", artifact_id, filename)
    return {
        "artifact_id": artifact_id,
        "kind": kind.value,
        "mime_type": content_type,
        "filename": filename,
        "size_bytes": len(content),
        "url": signed_url,
    }


@router.get("/{artifact_id}/signed-url")
async def get_signed_url(
    request: Request,
    artifact_id: str,
    ttl_seconds: int = Query(default=3600, ge=60, le=7 * 24 * 3600),
) -> dict[str, object]:
    user = require_current_user(request)
    artifact_store = request.app.state.artifact_store
    async with request.app.state.session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted":
        raise api_exception(404, "not_found", "Artifact not found")
    if row.owner_email and row.owner_email != user.email and getattr(user, "role", "") != "admin":
        raise api_exception(404, "not_found", "Artifact not found")
    url = await artifact_store.async_get_public_url(
        row.namespace,
        row.object_id,
        row.filename,
        ttl_seconds=ttl_seconds,
    )
    return {
        "artifact_id": artifact_id,
        "url": url,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
    }


@router.get("/content/{namespace}/{object_id}/{filename:path}")
async def serve_signed_artifact(
    request: Request,
    namespace: str,
    object_id: str,
    filename: str,
    exp: int,
    sig: str,
) -> Response:
    artifact_store = request.app.state.artifact_store
    if not artifact_store.verify_signed_request(namespace, object_id, filename, exp=exp, sig=sig):
        raise api_exception(403, "forbidden", "Invalid or expired artifact signature")
    content, content_type = await artifact_store.async_load(namespace, object_id, filename)
    headers = {
        "Cache-Control": "private, max-age=60",
        "Content-Length": str(len(content)),
        "X-Content-Type-Options": "nosniff",
    }
    if not content_type.startswith("image/"):
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=content,
        media_type=content_type,
        headers=headers,
    )
