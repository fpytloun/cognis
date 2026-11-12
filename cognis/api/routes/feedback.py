"""Thin authenticated/public adapters for the shared feedback application."""

from __future__ import annotations

import base64
import html
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

from cognis.api.common import forbid_mutation_for_viewer, require_current_user
from cognis.api.user_preferences import theme_family_for_user
from cognis.core.audio_context import document_audio_context
from cognis.core.audio_jobs import AudioJobService
from cognis.core.feedback.primitives import Actor, FeedbackError
from cognis.core.feedback.service import FeedbackService
from cognis.core.feedback.sources import SOURCE_ADAPTER, SourceEvidence
from cognis.core.feedback.submissions import DispatchCommand
from cognis.core.public_document_audio import public_audio_job, public_audio_request
from cognis.core.speech_models import (
    AudioJobCreate,
    AudioJobView,
    DocumentAudioContext,
    PodcastLengthEstimate,
)
from cognis.models import feedback as dto
from cognis.models.feedback_routes import (
    CAPABILITY_HEADER,
    GUEST_HEADER,
    PRIVATE_PREFIX,
    PUBLIC_PREFIX,
)
from cognis.rendering.deliverables import render_standalone_shell
from cognis.store import feedback_models as rows


class FeedbackRoute(APIRoute):
    """Normalize input/dependency errors without echoing private request content."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def adapted(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {"code": "invalid_request", "message": "Invalid feedback request"}
                    },
                )
            except HTTPException as exc:
                detail: object = exc.detail
                if not isinstance(detail, dict) or not {"code", "message"} <= detail.keys():
                    detail = {
                        "code": {
                            401: "unauthorized",
                            403: "forbidden",
                            404: "not_found",
                            409: "conflict",
                            422: "invalid_request",
                        }.get(exc.status_code, "http_error"),
                        "message": detail if isinstance(detail, str) else "Feedback request failed",
                    }
                return JSONResponse(
                    status_code=exc.status_code, content={"detail": detail}, headers=exc.headers
                )

        return adapted


router = APIRouter(prefix=PRIVATE_PREFIX, tags=["feedback"], route_class=FeedbackRoute)
public_router = APIRouter(prefix=PUBLIC_PREFIX, tags=["public-feedback"], route_class=FeedbackRoute)


async def feedback_error_response(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, FeedbackError)
    return JSONResponse(
        status_code=exc.status,
        content={
            "detail": {"code": exc.code, "message": str(exc)},
        },
    )


def service(request: Request) -> FeedbackService:
    feedback = getattr(request.app.state, "feedback", None)
    if feedback is None:
        raise FeedbackError(503, "feedback_unavailable", "Feedback service is unavailable")
    return cast(FeedbackService, feedback)


def signed(request: Request) -> Actor:
    user = require_current_user(request)
    if request.method not in {"GET", "HEAD"}:
        forbid_mutation_for_viewer(request)
    return Actor.user(user.email)


async def public(request: Request, *, guest: bool = False) -> Actor:
    origin = request.headers.get("origin")
    if origin == "null" or (origin and urlsplit(origin).netloc != request.url.netloc):
        raise FeedbackError(403, "origin_forbidden", "Public capability origin is not allowed")
    capability = request.headers.get(CAPABILITY_HEADER)
    if not capability:
        raise FeedbackError(401, "capability_required", "A comment-share capability is required")
    actor, _ = await service(request).public.actor(capability, request.headers.get(GUEST_HEADER))
    if guest and not actor.guest_session_id:
        raise FeedbackError(401, "guest_required", "A bounded guest session is required")
    await service(request).public.rate(actor, "public_request", 120)
    return actor


async def capture_actor(request: Request) -> Actor:
    return (
        await public(request, guest=True)
        if request.url.path.startswith(PUBLIC_PREFIX)
        else signed(request)
    )


def public_thread(thread: dto.FeedbackThread, share_id: str) -> dto.FeedbackThread:
    return thread.model_copy(
        update={
            "subject_id": share_id,
            "audience": "public",
            "entries": [entry.model_copy(update={"delivery": []}) for entry in thread.entries],
        }
    )


@router.post("/sources/resolve", response_model=dto.ResolvedFeedbackSource)
async def resolve(
    request: Request, payload: dto.ResolveSourceRequest
) -> dto.ResolvedFeedbackSource:
    return await service(request).sources.resolve(signed(request), payload)


@router.post("/sources/text", response_model=dto.ScopedCanonicalFeedbackText)
async def text(request: Request, payload: dto.ScopedTextRequest) -> dto.ScopedCanonicalFeedbackText:
    return await service(request).sources.scoped_text(signed(request), payload)


@router.get("/subjects/{subject_id}/threads", response_model=dto.ThreadPage)
async def threads(
    request: Request,
    subject_id: str,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
) -> dto.ThreadPage:
    return await service(request).threads.list(
        signed(request), subject_id, cursor=cursor, limit=limit
    )


@router.post("/threads", response_model=dto.FeedbackThread, status_code=201)
async def create_thread(request: Request, payload: dto.ThreadCreateRequest) -> dto.FeedbackThread:
    actor = signed(request)
    await service(request).public.rate(actor, "comment", 60)
    return await service(request).threads.create(actor, payload)


@router.get("/threads/{thread_id}", response_model=dto.FeedbackThread)
async def get_thread(request: Request, thread_id: str) -> dto.FeedbackThread:
    return await service(request).threads.thread(signed(request), thread_id)


@router.post("/threads/{thread_id}/entries", response_model=dto.EntryVersion, status_code=201)
async def reply(
    request: Request, thread_id: str, payload: dto.EntryCreateRequest
) -> dto.EntryVersion:
    actor = signed(request)
    await service(request).public.rate(actor, "comment", 60)
    return await service(request).threads.reply(actor, thread_id, payload)


@router.put("/entries/{entry_id}", response_model=dto.EntryVersion)
async def edit(request: Request, entry_id: str, payload: dto.EntryEditRequest) -> dto.EntryVersion:
    return await service(request).threads.edit(signed(request), entry_id, payload)


@router.post("/threads/{thread_id}/resolution", response_model=dto.FeedbackThread)
async def resolution(
    request: Request, thread_id: str, payload: dto.ResolutionRequest
) -> dto.FeedbackThread:
    return await service(request).threads.change_thread(signed(request), thread_id, payload)


@router.post("/threads/{thread_id}/moderation", response_model=dto.FeedbackThread)
async def moderation(
    request: Request, thread_id: str, payload: dto.ModerationRequest
) -> dto.FeedbackThread:
    return await service(request).threads.change_thread(signed(request), thread_id, payload)


@router.post("/threads/{thread_id}/reattach", response_model=dto.FeedbackThread)
async def reattach(
    request: Request, thread_id: str, payload: dto.ReattachRequest
) -> dto.FeedbackThread:
    return await service(request).threads.change_thread(signed(request), thread_id, payload)


@router.get("/subjects/{subject_id}/summary", response_model=dto.SubjectSummary)
async def summary(request: Request, subject_id: str) -> dto.SubjectSummary:
    return await service(request).continuity.summary(signed(request), subject_id)


@router.get("/subjects/{subject_id}/history", response_model=dto.HistoryPage)
async def history(
    request: Request,
    subject_id: str,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
) -> dto.HistoryPage:
    return await service(request).continuity.history(signed(request), subject_id, cursor, limit)


@router.get("/anchors/{anchor_id}/mapping", response_model=dto.AnchorMapping)
async def mapping(request: Request, anchor_id: str, target_subject_id: str) -> dto.AnchorMapping:
    return await service(request).continuity.mapping(signed(request), anchor_id, target_subject_id)


@router.put("/subjects/{subject_id}/preferred-followup", response_model=dto.SubjectSummary)
async def preference(
    request: Request, subject_id: str, payload: dto.FollowupPreferenceRequest
) -> dto.SubjectSummary:
    return await service(request).continuity.preference(signed(request), subject_id, payload)


@router.get("/drafts/{draft_key}", response_model=dto.DraftResponse)
async def get_draft(request: Request, draft_key: str) -> dto.DraftResponse:
    result = await service(request).threads.draft(signed(request), draft_key)
    assert result is not None
    return result


@router.put("/drafts/{draft_key}", response_model=dto.DraftResponse)
async def put_draft(
    request: Request, draft_key: str, payload: dto.DraftRequest
) -> dto.DraftResponse:
    result = await service(request).threads.save_draft(signed(request), draft_key, payload)
    assert result is not None
    return result


@router.delete("/drafts/{draft_key}", status_code=204)
async def delete_draft(
    request: Request, draft_key: str, expected_version: int = Query(ge=1)
) -> Response:
    await service(request).threads.delete_draft(signed(request), draft_key, expected_version)
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/staging", response_model=dto.StagingSnapshot)
async def staging(request: Request, conversation_id: str) -> dto.StagingSnapshot:
    return await service(request).staging(signed(request), conversation_id)


@router.put("/conversations/{conversation_id}/staging/{entry_id}", response_model=dto.StagedItem)
async def stage(
    request: Request, conversation_id: str, entry_id: str, payload: dto.StagingUpdateRequest
) -> dto.StagedItem:
    return await service(request).threads.stage(signed(request), conversation_id, entry_id, payload)


@router.delete("/conversations/{conversation_id}/staging/{entry_id}", status_code=204)
async def unstage(
    request: Request, conversation_id: str, entry_id: str, expected_version: int = Query(ge=1)
) -> Response:
    await service(request).threads.unstage(
        signed(request), conversation_id, entry_id, expected_version
    )
    return Response(status_code=204)


@router.post("/destinations", response_model=dto.DestinationResponse)
async def destinations(
    request: Request, payload: dto.DestinationRequest
) -> dto.DestinationResponse:
    return await service(request).submissions.destinations(signed(request), payload)


@router.post("/preflights", response_model=dto.FeedbackPreflight)
async def preflight(
    request: Request, payload: dto.FeedbackPreflightRequest
) -> dto.FeedbackPreflight:
    return await service(request).submissions.preflight(signed(request), payload)


@router.post("/submissions", response_model=dto.SubmissionReceipt, status_code=202)
async def submit(request: Request, payload: dto.SubmissionCreateRequest) -> dto.SubmissionReceipt:
    actor = signed(request)
    submission = await service(request).submissions.prepare(
        actor, payload, DispatchCommand(), standalone=True
    )
    return await service(request).submissions.dispatch(actor, submission.submission_id)


@router.get(
    "/submissions/by-creation-key/{creation_key}/receipt", response_model=dto.SubmissionReceipt
)
async def receipt_key(request: Request, creation_key: str) -> dto.SubmissionReceipt:
    return await service(request).submissions.by_creation_key(signed(request), creation_key)


@router.get("/submissions/{submission_id}", response_model=dto.SubmissionDetail)
async def submission_detail(request: Request, submission_id: str) -> dto.SubmissionDetail:
    return await service(request).submissions.detail(signed(request), submission_id)


@router.post("/submissions/{submission_id}/check-delivery", response_model=dto.SubmissionReceipt)
async def check_delivery(request: Request, submission_id: str) -> dto.SubmissionReceipt:
    return await service(request).submissions.receipt(signed(request), submission_id)


@router.get("/submissions/{submission_id}/results", response_model=dto.SubmissionResultsResponse)
async def results(request: Request, submission_id: str) -> dto.SubmissionResultsResponse:
    return await service(request).agents.results(signed(request), submission_id)


@router.post(
    "/submissions/{submission_id}/children", response_model=dto.ChildWorkResponse, status_code=201
)
async def children(
    request: Request, submission_id: str, payload: dto.ChildWorkRequest
) -> dto.ChildWorkResponse:
    return await service(request).agents.child(signed(request), submission_id, payload)


@router.get("/subjects/{subject_id}/grants", response_model=dto.GrantListResponse)
async def grants(request: Request, subject_id: str) -> dto.GrantListResponse:
    return await service(request).public.grants(signed(request), subject_id)


@router.put("/subjects/{subject_id}/grants", response_model=dto.GrantResponse)
async def grant(request: Request, subject_id: str, payload: dto.GrantRequest) -> dto.GrantResponse:
    return await service(request).public.grant(signed(request), subject_id, payload)


@router.get("/subjects/{subject_id}/public-shares", response_model=dto.ShareListResponse)
async def shares(request: Request, subject_id: str) -> dto.ShareListResponse:
    return await service(request).public.shares(signed(request), subject_id)


@router.post(
    "/subjects/{subject_id}/public-shares", response_model=dto.ShareResponse, status_code=201
)
async def share(request: Request, subject_id: str, payload: dto.ShareRequest) -> dto.ShareResponse:
    return await service(request).public.create_share(signed(request), subject_id, payload)


@router.put("/public-shares/{share_id}", response_model=dto.ShareResponse)
async def update_share(
    request: Request, share_id: str, payload: dto.ShareUpdateRequest
) -> dto.ShareResponse:
    return await service(request).public.update_share(signed(request), share_id, payload)


@router.delete("/public-shares/{share_id}", response_model=dto.ShareResponse)
async def revoke_share(
    request: Request, share_id: str, expected_version: int = Query(ge=1)
) -> dto.ShareResponse:
    return await service(request).public.update_share(
        signed(request),
        share_id,
        dto.ShareUpdateRequest(expected_version=expected_version, revoked=True),
    )


@router.post(
    "/public-shares/{share_id}/outcomes", response_model=dto.PublicOutcome, status_code=201
)
async def publish_outcome(
    request: Request, share_id: str, payload: dto.PublicOutcomeRequest
) -> dto.PublicOutcome:
    return await service(request).public.publish_outcome(signed(request), share_id, payload)


@router.get("/public-shares/{share_id}/link", response_model=dto.ShareResponse)
async def copy_share_link(request: Request, response: Response, share_id: str) -> dto.ShareResponse:
    """Copy is explicit and owner-only; do not put bearer tokens in cached listings."""
    response.headers["Cache-Control"] = "no-store"
    return await service(request).public.copy_share(signed(request), share_id)


@public_router.post("/exchange", response_model=dto.PublicExchangeResponse)
async def exchange(
    request: Request, payload: dto.PublicExchangeRequest
) -> dto.PublicExchangeResponse:
    if request.headers.get("origin") == "null":
        raise FeedbackError(403, "origin_forbidden", "Artifact frames cannot exchange capabilities")
    result = await service(request).public.exchange(payload.share_token)
    if payload.create_guest and result.allow_comments:
        result.guest_session = await service(request).public.guest(
            result.capability, dto.GuestSessionRequest(display_name=payload.display_name)
        )
    return result


@public_router.post("/guest-sessions", response_model=dto.GuestSessionResponse, status_code=201)
async def guest_session(
    request: Request, payload: dto.GuestSessionRequest
) -> dto.GuestSessionResponse:
    await public(request)
    return await service(request).public.guest(request.headers[CAPABILITY_HEADER], payload)


async def public_source(request: Request) -> tuple[Actor, rows.FeedbackShareRow]:
    actor = await public(request)
    async with service(request).sources.factory() as db:
        share = await db.get(rows.FeedbackShareRow, actor.share_id)
    assert share is not None
    return actor, share


async def public_document(request: Request) -> tuple[rows.FeedbackShareRow, SourceEvidence]:
    """Revalidate the capability and exact bytes before document side operations."""
    _, share = await public_source(request)
    evidence = await service(request).sources.load(
        share.owner_email, SOURCE_ADAPTER.validate_python(share.source)
    )
    if evidence.content_digest != share.content_digest:
        raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
    return share, evidence


@public_router.get("/audio/source", response_model=DocumentAudioContext)
async def public_audio_context(request: Request, response: Response) -> DocumentAudioContext:
    response.headers["Cache-Control"] = "no-store"
    share, _ = await public_document(request)
    result = await document_audio_context(
        cast(AudioJobService, request.app.state.audio_jobs),
        share.owner_email,
        share.source["content_ref"],
        can_mutate=True,
    )
    # Visitors can request a bounded plan, never cancel someone else's work.
    return result.model_copy(update={"can_cancel": False})


@public_router.get("/audio/estimates", response_model=list[PodcastLengthEstimate])
async def public_audio_estimates(
    request: Request, response: Response
) -> list[PodcastLengthEstimate]:
    response.headers["Cache-Control"] = "no-store"
    share, _ = await public_document(request)
    try:
        return await cast(AudioJobService, request.app.state.audio_jobs).estimates(
            share.owner_email, share.source["content_ref"]
        )
    except ValueError as exc:
        raise FeedbackError(422, "invalid_source", str(exc)) from exc


@public_router.post("/audio", response_model=AudioJobView, status_code=202)
async def public_audio_create(
    request: Request, response: Response, payload: AudioJobCreate
) -> AudioJobView:
    response.headers["Cache-Control"] = "no-store"
    share, _ = await public_document(request)
    actor = Actor(
        identity=f"share:{share.share_id}", label="Public reader", share_id=share.share_id
    )
    await service(request).public.rate(actor, "audio", 6)
    try:
        admitted = public_audio_request(share.source["content_ref"], share.content_digest, payload)
        return await cast(AudioJobService, request.app.state.audio_jobs).create(
            share.owner_email, admitted
        )
    except ValueError as exc:
        raise FeedbackError(409, "invalid_audio_options", str(exc)) from exc


@public_router.get("/audio/{job_id}", response_model=AudioJobView)
async def public_audio_get(request: Request, response: Response, job_id: str) -> AudioJobView:
    response.headers["Cache-Control"] = "no-store"
    share, _ = await public_document(request)
    try:
        return await public_audio_job(
            cast(AudioJobService, request.app.state.audio_jobs),
            share.owner_email,
            share.source["content_ref"],
            job_id,
        )
    except PermissionError as exc:
        raise FeedbackError(404, "not_found", "Audio job not found") from exc


@public_router.get("/source/download")
async def public_document_download(
    request: Request, format: Literal["original", "pdf", "html", "markdown"] = "original"
) -> Response:
    share, _ = await public_document(request)
    if format == "original":
        return await source_bytes(request, download=True)
    if not share.source["content_ref"].startswith("dlv_"):
        raise FeedbackError(422, "unsupported_format", "Use the original artifact download.")
    from cognis.api.routes.deliverables import _cached_pdf, _single_html_response
    from cognis.store.deliverable_storage import hydrate_deliverable_payload
    from cognis.store.models import DeliverableRow

    async with service(request).sources.factory() as db:
        row = await db.get(DeliverableRow, share.source["content_ref"])
    if row is None:
        raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
    await hydrate_deliverable_payload(row, request.app.state.artifact_store)
    if format == "html":
        return await _single_html_response(request, row, cache_control="no-store")
    data = (
        await _cached_pdf(request, row, access_scope=f"feedback:{share.share_id}")
        if format == "pdf"
        else (row.content or "").encode()
    )
    return Response(
        data,
        media_type="application/pdf" if format == "pdf" else "text/markdown",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.get("/source", response_model=dto.PublicContentResponse)
async def source_content(request: Request) -> dto.PublicContentResponse:
    _, share = await public_source(request)
    evidence = await service(request).sources.load(
        share.owner_email, SOURCE_ADAPTER.validate_python(share.source)
    )
    if evidence.content_digest != share.content_digest:
        raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
    content = evidence.text
    if evidence.format == "html":
        content = evidence.data.decode("utf-8")
    return dto.PublicContentResponse(
        format=evidence.format,
        mime_type=evidence.mime_type or source_mime(evidence.format),
        filename=evidence.title,
        content=content,
        rich_payload=evidence.rich_payload,
        theme_family=await theme_family_for_user(
            service(request).sources.factory, share.owner_email
        ),
    )


def source_mime(format_: str) -> str:
    return {
        "pdf": "application/pdf",
        "html": "text/html",
        "rich": "application/json",
        "image": "application/octet-stream",
    }.get(format_, "text/plain")


@public_router.get("/source/bytes")
async def source_bytes(request: Request, download: bool = False) -> Response:
    _, share = await public_source(request)
    evidence = await service(request).sources.load(
        share.owner_email, SOURCE_ADAPTER.validate_python(share.source)
    )
    if evidence.content_digest != share.content_digest:
        raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
    data = evidence.data
    mime = evidence.mime_type or source_mime(evidence.format)
    if download and share.source["content_ref"].startswith("dlv_"):
        if evidence.format == "rich":
            from cognis.api.routes.deliverables import _single_html_response
            from cognis.store.deliverable_storage import hydrate_deliverable_payload
            from cognis.store.models import DeliverableRow

            async with request.app.state.session_factory() as db:
                row = await db.get(DeliverableRow, share.source["content_ref"])
            if row is None:
                raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
            await hydrate_deliverable_payload(row, request.app.state.artifact_store)
            return await _single_html_response(request, row, cache_control="no-store")
        else:
            data = (evidence.text or "").encode()
            mime = "text/markdown"
    return Response(
        data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@public_router.get("/viewer")
async def viewer(request: Request) -> Response:
    _, share = await public_source(request)
    evidence = await service(request).sources.load(
        share.owner_email, SOURCE_ADAPTER.validate_python(share.source)
    )
    if evidence.content_digest != share.content_digest:
        raise FeedbackError(410, "source_unavailable", "Exact shared source is unavailable")
    payload = evidence.rich_payload or {
        "blocks": [{"type": "markdown", "content": evidence.text or ""}]
    }
    embedded = ""
    if evidence.format == "html":
        embedded = "data:text/html;base64," + base64.b64encode(evidence.data).decode("ascii")
    row = SimpleNamespace(
        deliverable_id=share.share_id,
        title=evidence.title,
        format="rich",
        rich_payload=payload,
        content=evidence.text or "",
    )
    rendered = render_standalone_shell(
        row,
        media_base="",
        standalone_url="",
        pdf_url="",
        html_url="",
        embedded_document_url=embedded,
        theme_family=await theme_family_for_user(
            service(request).sources.factory, share.owner_email
        ),
    )
    attribute = html.escape(request.headers[CAPABILITY_HEADER], quote=True)
    rendered = rendered.replace(
        'id="cognis-deliverable-root"',
        f'id="cognis-deliverable-root" data-feedback-share-token="{attribute}"',
        1,
    )
    return Response(
        rendered,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.post("/sources/resolve", response_model=dto.ResolvedFeedbackSource)
async def public_resolve(
    request: Request, payload: dto.ResolveSourceRequest
) -> dto.ResolvedFeedbackSource:
    actor = await public(request)
    result = await service(request).sources.resolve(actor, payload)
    return result.model_copy(
        update={
            "subject_id": actor.share_id,
            "lineage_id": None,
            "revision": None,
            "latest_content_ref": None,
        }
    )


@public_router.post("/sources/text", response_model=dto.ScopedCanonicalFeedbackText)
async def public_text(
    request: Request, payload: dto.ScopedTextRequest
) -> dto.ScopedCanonicalFeedbackText:
    return await service(request).sources.scoped_text(await public(request), payload)


@public_router.get("/threads", response_model=dto.ThreadPage)
async def public_threads(
    request: Request, cursor: str | None = None, limit: int = Query(30, ge=1, le=100)
) -> dto.ThreadPage:
    actor, share = await public_source(request)
    result = await service(request).threads.list(
        actor, share.subject_id, cursor=cursor, limit=limit
    )
    return result.model_copy(
        update={"items": [public_thread(thread, share.share_id) for thread in result.items]}
    )


@public_router.get("/threads/{thread_id}", response_model=dto.FeedbackThread)
async def public_get_thread(request: Request, thread_id: str) -> dto.FeedbackThread:
    actor = await public(request)
    return public_thread(
        await service(request).threads.thread(actor, thread_id), str(actor.share_id)
    )


@public_router.post("/threads", response_model=dto.FeedbackThread, status_code=201)
async def public_create(
    request: Request, payload: dto.PublicThreadCreateRequest
) -> dto.FeedbackThread:
    actor = await public(request, guest=True)
    await service(request).public.rate(actor, "comment", 30)
    result = await service(request).threads.create(
        actor,
        dto.ThreadCreateRequest(
            creation_key=payload.creation_key,
            context=dto.StandaloneFeedbackContext(kind="standalone"),
            anchor=payload.anchor,
            body=payload.body,
        ),
    )
    return public_thread(result, str(actor.share_id))


@public_router.post(
    "/threads/{thread_id}/entries", response_model=dto.EntryVersion, status_code=201
)
async def public_reply(
    request: Request, thread_id: str, payload: dto.PublicEntryCreateRequest
) -> dto.EntryVersion:
    actor = await public(request, guest=True)
    await service(request).public.rate(actor, "comment", 30)
    result = await service(request).threads.reply(
        actor,
        thread_id,
        dto.EntryCreateRequest(
            **payload.model_dump(),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )
    return result.model_copy(update={"delivery": []})


@public_router.put("/entries/{entry_id}", response_model=dto.EntryVersion)
async def public_edit(
    request: Request, entry_id: str, payload: dto.PublicEntryEditRequest
) -> dto.EntryVersion:
    result = await service(request).threads.edit(
        await public(request, guest=True),
        entry_id,
        dto.EntryEditRequest(**payload.model_dump()),
    )
    return result.model_copy(update={"delivery": []})


@public_router.get("/outcomes", response_model=dto.PublicOutcomePage)
async def public_outcomes(request: Request) -> dto.PublicOutcomePage:
    return await service(request).public.outcomes(await public(request))


@router.post("/captures", response_model=dto.CaptureResponse, status_code=201)
@public_router.post("/captures", response_model=dto.CaptureResponse, status_code=201)
async def capture(request: Request, payload: dto.CaptureRequest) -> dto.CaptureResponse:
    return await service(request).captures.create(await capture_actor(request), payload)


@router.get("/captures/{capture_id}", response_model=dto.CaptureResponse)
@public_router.get("/captures/{capture_id}", response_model=dto.CaptureResponse)
async def capture_status(request: Request, capture_id: str) -> dto.CaptureResponse:
    return service(request).captures.response(
        await service(request).captures.get(await capture_actor(request), capture_id)
    )


@router.put("/captures/{capture_id}/pixels", response_model=dto.CaptureResponse)
@public_router.put("/captures/{capture_id}/pixels", response_model=dto.CaptureResponse)
async def upload_pixels(
    request: Request,
    capture_id: str,
    expected_version: int = Query(ge=1),
    width: int = Query(ge=1, le=16384),
    height: int = Query(ge=1, le=16384),
) -> dto.CaptureResponse:
    actor = await capture_actor(request)
    if request.headers.get("content-type", "").split(";")[0] not in {"image/png", "image/webp"}:
        raise FeedbackError(
            422, "invalid_capture", "Only explicit PNG/WebP pixel adoption is allowed"
        )
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > service(request).captures.MAX_BYTES:
            raise FeedbackError(413, "capture_too_large", "Capture exceeds the byte limit")
        data.extend(chunk)
    return await service(request).captures.adopt(
        actor, capture_id, expected_version, width, height, bytes(data)
    )


@router.get("/captures/{capture_id}/pixels")
@public_router.get("/captures/{capture_id}/pixels")
async def pixels(request: Request, capture_id: str) -> Response:
    actor = await capture_actor(request)
    row = await service(request).captures.get(actor, capture_id)
    if not row.payload_id:
        raise FeedbackError(410, "capture_unavailable", "Capture pixels are not ready")
    data, mime = await service(request).payloads.get_bytes(row.payload_id)
    return Response(
        data,
        media_type=mime,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
