"""Thin human authoring and sharing transport for Library content."""

from __future__ import annotations

import json
import mimetypes
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import ValidationError

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.api.routes.deliverables import DeliverableShareOptions, _public_base_url
from cognis.core import artifact_authoring as authoring
from cognis.core import document_people
from cognis.core import document_sharing as sharing
from cognis.core.deliverable_links import DeliverableShareUnavailable
from cognis.core.feedback.primitives import FeedbackError
from cognis.core.library import LibraryItem, item
from cognis.models.deliverable import ArtifactRevisionError


class PublicLinkRequest(DeliverableShareOptions):
    mode: Literal["view", "download"] | None = None


router = APIRouter(prefix="/api/v1/library", tags=["library"])
Kind = Literal["artifact", "deliverable"]


@router.get("/items/{kind}/{ref}/people", response_model=document_people.PeoplePage)
async def document_people_list(
    request: Request, kind: Kind, ref: str
) -> document_people.PeoplePage:
    """List direct human grants, independently of agent and public-link access."""
    owner = _author(request)
    try:
        async with request.app.state.session_factory() as session:
            return await document_people.people(session, owner, kind, ref)
    except LookupError as exc:
        raise _error(exc) from exc


@router.put("/items/{kind}/{ref}/people", response_model=document_people.PeoplePage)
async def document_people_set(
    request: Request, kind: Kind, ref: str, body: document_people.PersonGrantRequest
) -> document_people.PeoplePage:
    """Change explicit lineage access without publishing or sharing a producing conversation."""
    owner = _author(request)
    try:
        async with request.app.state.session_factory() as session:
            return await document_people.set_person(session, owner, kind, ref, body)
    except FeedbackError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc
    except LookupError as exc:
        raise _error(exc) from exc


@router.get("/agents", response_model=list[sharing.AgentAccess])
async def agent_choices(request: Request) -> list[sharing.AgentAccess]:
    """Only expose the authenticated person's selectable primary agents."""
    import sqlalchemy as sa

    from cognis.store.models import Agent

    owner = _author(request)
    async with request.app.state.session_factory() as session:
        agents = await session.scalars(
            sa.select(Agent)
            .where(
                Agent.owner_email == owner,
                Agent.status == "active",
                ~Agent.agent_id.startswith("system:"),
            )
            .order_by(Agent.name, Agent.agent_id)
        )
        return [
            sharing.AgentAccess(
                agent_id=a.agent_id,
                name=a.display_name or a.name,
                explicit=False,
                inherited=False,
                avatar_url=f"/api/v1/images/{a.avatar_image_id}"
                if a.avatar_image_id
                else a.avatar_url,
            )
            for a in agents
        ]


def _author(request: Request) -> str:
    forbid_mutation_for_viewer(request)
    return str(require_current_user(request).email)


def _error(exc: Exception) -> Any:
    if isinstance(exc, ArtifactRevisionError):
        status = 409 if "latest" in exc.code else 400
        return api_exception(status, exc.code, str(exc))
    if isinstance(exc, LookupError):
        return api_exception(404, "not_found", "Document not found")
    return api_exception(400, "validation_error", str(exc))


async def _save(request: Request, **kwargs: Any) -> LibraryItem:
    owner = _author(request)
    try:
        ref = await authoring.save_human_artifact(
            request.app.state.session_factory, request.app.state.artifact_store, owner, **kwargs
        )
    except (ValueError, LookupError, ArtifactRevisionError) as exc:
        raise _error(exc) from exc
    async with request.app.state.session_factory() as session:
        result = await item(session, owner, "artifact", ref)
    assert result is not None
    return result


@router.post("/artifacts", response_model=LibraryItem)
async def create_text(request: Request, body: authoring.TextArtifactRequest) -> LibraryItem:
    """Create a durable UTF-8 artifact, without the chat upload expiry."""
    return await _save(
        request,
        filename=body.filename,
        mime_type=body.mime_type,
        content=body.content.encode("utf-8"),
        project_id=body.project_id,
        agent_ids=body.agent_ids,
    )


@router.post("/artifacts/upload", response_model=LibraryItem)
async def upload(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    filename: Annotated[str | None, Form(max_length=240)] = None,
    project_id: Annotated[str | None, Form()] = None,
    agent_ids: Annotated[str, Form()] = "[]",
) -> LibraryItem:
    """Bound multipart reads before handing bytes to the canonical writer."""
    _author(request)
    content = await file.read(authoring.UPLOAD_MAX_BYTES + 1)
    name = filename or file.filename or "upload"
    try:
        recipients = sharing.AgentSharesRequest.model_validate({"agent_ids": json.loads(agent_ids)})
    except (ValueError, ValidationError) as exc:
        raise api_exception(400, "validation_error", "Invalid agent selection.") from exc
    mime = mimetypes.guess_type(name)[0] or file.content_type or "application/octet-stream"
    return await _save(
        request,
        filename=name,
        mime_type=mime,
        content=content,
        project_id=project_id,
        agent_ids=recipients.agent_ids,
    )


@router.get("/artifacts/{ref}/edit", response_model=authoring.EditableArtifact)
async def edit_source(request: Request, ref: str) -> authoring.EditableArtifact:
    """Never use truncated preview content as an edit source."""
    owner = _author(request)
    try:
        return await authoring.editable_content(
            request.app.state.session_factory, request.app.state.artifact_store, owner, ref
        )
    except (ValueError, LookupError, ArtifactRevisionError) as exc:
        raise _error(exc) from exc


@router.post("/artifacts/{ref}/revisions", response_model=LibraryItem)
async def revise(request: Request, ref: str, body: authoring.TextArtifactRequest) -> LibraryItem:
    """Create an immutable successor or report a stale-head conflict."""
    return await _save(
        request,
        filename=body.filename,
        mime_type=body.mime_type,
        content=body.content.encode("utf-8"),
        supersedes=ref,
        project_id=body.project_id,
        agent_ids=body.agent_ids,
    )


@router.get("/items/{kind}/{ref}/sharing", response_model=sharing.DocumentAccess)
async def access(request: Request, kind: Kind, ref: str) -> sharing.DocumentAccess:
    owner = _author(request)
    async with request.app.state.session_factory() as session:
        try:
            row = await sharing.owned_document(session, kind, ref, owner)
            return await sharing.document_access(session, row, owner)
        except LookupError as exc:
            raise _error(exc) from exc


@router.put("/items/{kind}/{ref}/sharing", response_model=sharing.DocumentAccess)
async def set_access(
    request: Request, kind: Kind, ref: str, body: sharing.AgentSharesRequest
) -> sharing.DocumentAccess:
    owner = _author(request)
    async with request.app.state.session_factory() as session:
        try:
            row = await sharing.owned_document(session, kind, ref, owner, lock=True)
            result = await sharing.replace_explicit_shares(
                session, row, owner, body.agent_ids, body.write_agent_ids
            )
            await session.commit()
            return result
        except (ValueError, LookupError) as exc:
            raise _error(exc) from exc


@router.post("/items/{kind}/{ref}/public-link", response_model=sharing.PublicLink)
async def create_public_link(
    request: Request, kind: Kind, ref: str, body: PublicLinkRequest
) -> sharing.PublicLink:
    """Public sharing is distinct from authenticated agent/project access."""
    owner = _author(request)
    ttl = (
        int((body.expires_at - datetime.now(UTC)).total_seconds())
        if body.expires_at
        else body.ttl_seconds or 7 * 86400
    )
    try:
        return await sharing.public_link(
            request.app.state.session_factory,
            request.app.state.artifact_store,
            owner,
            kind,
            ref,
            mode=body.mode,
            ttl_seconds=ttl,
            base_url=_public_base_url(request),
        )
    except (ValueError, LookupError) as exc:
        raise _error(exc) from exc
    except DeliverableShareUnavailable as exc:
        raise api_exception(503, "share_unavailable", str(exc)) from exc
