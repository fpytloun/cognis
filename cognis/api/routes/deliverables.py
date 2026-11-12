"""Deliverable retrieval and standalone export API routes."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.api.models import (
    DeliverableResponse,
    DeliverableRevisionsResponse,
    DeliverableRevisionSummary,
)
from cognis.api.serializers import deliverable_to_response
from cognis.api.user_preferences import theme_family_for_user
from cognis.core.content_refs import (
    get_deliverable_ref_unscoped,
    task_for_step_run,
)
from cognis.core.deliverable_links import (
    DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS,
    DeliverableShareUnavailable,
    signed_deliverable_view_link,
    verify_deliverable_share_token,
)
from cognis.core.deliverable_media import resolve_deliverable_media
from cognis.core.deliverable_reads import readable_deliverables
from cognis.rendering.deliverables import (
    HTML_CACHE_FILENAME,
    PDF_CACHE_FILENAME,
    DeliverableRenderError,
    _markdown_headings,
    deliverable_cache_key,
    render_pdf_bytes,
    render_standalone_html,
    render_standalone_shell,
)
from cognis.rendering.offline_deliverables import render_offline_html
from cognis.rendering.rich_visuals import MediaReference, ResolvedMedia
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.document_people import person_may_read_document
from cognis.store.document_visibility import unmanaged_deliverable_clause
from cognis.store.models import DELIVERABLE_LIVE_STATUSES, DeliverableRow
from cognis.store.queries import (
    deliverable_lineage_key,
    get_conversation,
    get_deliverable,
    get_deliverable_lineage_head,
    get_deliverable_lineage_summary,
    list_deliverable_lineage,
)
from cognis.ui_assets import resolve_standalone_asset, resolve_standalone_manifest

router = APIRouter(prefix="/api/v1/deliverables", tags=["deliverables"])
DEFAULT_SHARE_TTL_SECONDS = DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS
MAX_SINGLE_HTML_BYTES = 32 * 1024 * 1024
_pdf_render_flights: dict[tuple[str, str, str], asyncio.Task[bytes]] = {}
STANDALONE_CSP = (
    "sandbox allow-scripts allow-same-origin allow-downloads; default-src 'none'; "
    "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self' data:; media-src 'self' data:"
)
STATIC_CSP = (
    "sandbox allow-scripts allow-same-origin allow-downloads; default-src 'none'; "
    "connect-src 'self'; img-src 'self' data:; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; font-src data:; media-src data:;"
)


@router.get("/s/{token}")
async def short_public_deliverable_view(token: str) -> RedirectResponse:
    """Redirect a bounded signed URL to the public standalone view."""

    return RedirectResponse(
        url=f"/api/v1/deliverables/share/{quote(token, safe='')}/view",
        status_code=307,
    )


@router.get("/standalone-assets/{asset_path:path}")
async def standalone_deliverable_asset(asset_path: str) -> FileResponse:
    """Serve one hashed standalone bundle asset without session authentication."""

    asset = resolve_standalone_asset(asset_path)
    if asset is None:
        raise api_exception(404, "not_found", "Standalone deliverable asset not found")
    return FileResponse(
        asset,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Cross-Origin-Resource-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{deliverable_id}", response_model=DeliverableResponse)
async def deliverable_detail(
    request: Request,
    deliverable_id: str,
    accessor_conversation_id: str | None = None,
) -> DeliverableResponse:
    """Return a conversation-scoped deliverable available to the current user."""

    row = await _authorized_deliverable(
        request,
        deliverable_id,
        accessor_conversation_id=accessor_conversation_id,
        allow_unscoped_ref=False,
    )
    async with request.app.state.session_factory() as session:
        lineage = await get_deliverable_lineage_summary(session, row)
    return deliverable_to_response(row, lineage=lineage)


@router.get("/{deliverable_id}/revisions", response_model=DeliverableRevisionsResponse)
async def deliverable_revisions(
    request: Request, deliverable_id: str
) -> DeliverableRevisionsResponse:
    """List live revisions in the lineage of a deliverable the current user may read.

    Readers are the owner of each revision or, for project documents, people
    with a role in the document's project.

    Only metadata is returned; each revision is opened through its normal detail
    endpoint, which applies its own access rules.
    """

    user = require_current_user(request)
    from cognis.core.library import item as library_item

    async with request.app.state.session_factory() as session:
        row = await get_deliverable(session, deliverable_id)
        if row is None or not await _person_may_read_revision(session, row, user.email):
            raise api_exception(404, "not_found", "Deliverable not found")
        lineage_id = deliverable_lineage_key(row)
        lineage_rows = await list_deliverable_lineage(session, lineage_id)
        if all(item.deliverable_id != row.deliverable_id for item in lineage_rows):
            # A rejected or replaced draft is outside live history; list it last
            # so its own view still has context.
            lineage_rows.append(row)
        revisions: list[DeliverableRevisionSummary] = []
        for item in lineage_rows:
            if not await _person_may_read_revision(session, item, user.email):
                continue
            _owner_email, task_id = await _deliverable_owner(session, item)
            conversation_id = item.conversation_id
            step_run_id = item.step_run_id
            if _owner_email != user.email:
                source = await library_item(session, user.email, "deliverable", item.deliverable_id)
                conversation_id = source.conversation_id if source else None
                task_id = source.task_id if source else None
                if task_id is None:
                    step_run_id = None
            revisions.append(
                DeliverableRevisionSummary(
                    deliverable_id=item.deliverable_id,
                    author_email=item.author_email,
                    author_agent_id=item.author_agent_id,
                    revision=item.revision or 1,
                    title=item.title,
                    format=item.format,
                    status=item.status,
                    step_run_id=step_run_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    created_at=item.created_at,
                )
            )
    latest_id = next(
        (item.deliverable_id for item in revisions if item.status in DELIVERABLE_LIVE_STATUSES),
        row.deliverable_id,
    )
    for summary in revisions:
        summary.is_latest = summary.deliverable_id == latest_id
    return DeliverableRevisionsResponse(
        lineage_id=lineage_id,
        latest_deliverable_id=latest_id,
        revisions=revisions,
    )


async def _person_may_read_revision(session: Any, row: DeliverableRow, user_email: str) -> bool:
    if (await _deliverable_owner(session, row))[0] == user_email:
        return True
    return not await _is_managed_deliverable(session, row) and await person_may_read_document(
        session, row, user_email
    )


async def _deliverable_owner(session: Any, row: DeliverableRow) -> tuple[str | None, str | None]:
    """Return the human owner email and task ID for a deliverable."""

    if row.step_run_id is not None:
        task = await task_for_step_run(session, row.step_run_id)
        return (
            (row.owner_email or task.created_by, task.task_id)
            if task is not None
            else (row.owner_email, None)
        )
    if row.conversation_id is not None:
        conversation = await get_conversation(session, row.conversation_id)
        return (
            row.owner_email or (conversation.user_email if conversation is not None else None)
        ), None
    return row.owner_email, None


@router.get("/{deliverable_id}/view")
async def deliverable_view(
    request: Request,
    deliverable_id: str,
    accessor_conversation_id: str | None = None,
    expired_link: bool = False,
) -> Response:
    """Return a standalone HTML render for a deliverable available to the current user."""

    row = await _authorized_deliverable(
        request,
        deliverable_id,
        accessor_conversation_id=accessor_conversation_id,
    )
    if expired_link:
        await _require_deliverable_owner(request, deliverable_id)
    html_bytes = (
        _try_render_standalone_response(
            row,
            theme_family=await _publication_theme_family(request, row),
            media_base=f"/api/v1/deliverables/{quote(row.deliverable_id)}/media",
            share_expired=expired_link,
            standalone_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/view",
            pdf_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/download.pdf",
            html_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/download.html",
            markdown_url=(
                f"/api/v1/deliverables/{quote(row.deliverable_id)}/download.md"
                if row.format in {"rich", "markdown"}
                else ""
            ),
        )
        if _standalone_assets_available(request)
        else None
    )
    standalone = html_bytes is not None
    if html_bytes is None:
        html_bytes = await _cached_html(request, row)
    return Response(
        html_bytes,
        media_type="text/html; charset=utf-8",
        headers=_html_headers(
            row,
            len(html_bytes),
            cache_control="private, max-age=60",
            standalone=standalone,
        ),
    )


@router.get("/{deliverable_id}/download.pdf")
async def deliverable_pdf(request: Request, deliverable_id: str) -> Response:
    """Return a cached standalone PDF render for a deliverable available to the current user."""

    row = await _authorized_deliverable(request, deliverable_id)
    pdf_bytes = await _cached_pdf(
        request,
        row,
        access_scope=str(request.state.deliverable_user_scope),
    )
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers=_pdf_headers(row, len(pdf_bytes), cache_control="private, max-age=60"),
    )


@router.get("/{deliverable_id}/media/{media_key}")
async def deliverable_media(
    request: Request,
    deliverable_id: str,
    media_key: str,
    accessor_conversation_id: str | None = None,
) -> Response:
    """Serve one manifest-referenced image to an authenticated deliverable reader."""

    row = await _authorized_deliverable(
        request,
        deliverable_id,
        accessor_conversation_id=accessor_conversation_id,
    )
    return await _media_response(
        request,
        row,
        media_key,
        cache_control="private, max-age=60",
    )


@router.get("/{deliverable_id}/download.html")
async def deliverable_html_download(request: Request, deliverable_id: str) -> Response:
    """Download an offline, self-contained publication."""

    row = await _authorized_deliverable(request, deliverable_id)
    return await _single_html_response(request, row, cache_control="private, no-store")


@router.get("/{deliverable_id}/download.md")
async def deliverable_markdown_download(request: Request, deliverable_id: str) -> Response:
    """Download canonical Markdown or rich fallback content."""

    row = await _authorized_deliverable(request, deliverable_id)
    return _markdown_response(row, cache_control="private, no-store")


class DeliverableShareOptions(BaseModel):
    ttl_seconds: int | None = Field(default=None, ge=60, le=90 * 86400)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_expiration(self) -> DeliverableShareOptions:
        if self.ttl_seconds is not None and self.expires_at is not None:
            raise ValueError("Select a duration or an expiration date, not both")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("Expiration requires a timezone")
            remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
            if not 60 <= remaining <= 90 * 86400:
                raise ValueError("Expiration must be 1 minute to 90 days from now")
        return self


@router.get("/{deliverable_id}/viewer-context")
async def deliverable_viewer_context(request: Request, deliverable_id: str) -> dict[str, str]:
    """Expose owner-only controls after normal session authentication."""
    forbid_mutation_for_viewer(request)
    await _require_deliverable_owner(request, deliverable_id)
    return {"share_link_url": f"/api/v1/deliverables/{quote(deliverable_id)}/share-link"}


async def _require_deliverable_owner(request: Request, deliverable_id: str) -> None:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        ref = await get_deliverable_ref_unscoped(
            session, request.app.state.artifact_store, deliverable_id
        )
    if ref is None or ref.owner_email != user.email:
        raise api_exception(404, "not_found", "Deliverable not found")


@router.post("/{deliverable_id}/share-link")
async def deliverable_share_link(
    request: Request,
    deliverable_id: str,
    options: DeliverableShareOptions | None = None,
) -> dict[str, str]:
    """Create a stateless signed public share link for a deliverable."""

    forbid_mutation_for_viewer(request)
    # Public links are an owner decision; project readers cannot mint them.
    await _authorized_deliverable(request, deliverable_id, allow_project_reader=False)
    ttl_seconds = (
        options.ttl_seconds
        if options and options.ttl_seconds is not None
        else _share_ttl_seconds(request)
    )
    try:
        link = signed_deliverable_view_link(
            request.app.state.artifact_store,
            deliverable_id,
            base_url=_public_base_url(request),
            ttl_seconds=ttl_seconds,
            expires_at=int(options.expires_at.timestamp())
            if options and options.expires_at
            else None,
        )
    except DeliverableShareUnavailable as exc:
        raise api_exception(503, "share_unavailable", str(exc)) from exc
    return {
        "url": link.url,
        "expires_at": (link.expires_at or datetime.now(UTC)).isoformat(),
    }


@router.get("/share/{token}/view")
async def public_deliverable_view(request: Request, token: str) -> Response:
    """Return a standalone HTML render for a signed public share token."""

    try:
        deliverable_id, _expires_at = verify_deliverable_share_token(
            request.app.state.artifact_store, token
        )
    except DeliverableShareUnavailable as exc:
        if request.cookies.get("cognis_session") or request.headers.get("Authorization"):
            try:
                deliverable_id, expires_at = verify_deliverable_share_token(
                    request.app.state.artifact_store, token, allow_expired=True
                )
            except DeliverableShareUnavailable:
                pass
            else:
                if expires_at < int(time.time()):
                    return RedirectResponse(
                        f"/api/v1/deliverables/{quote(deliverable_id)}/view?expired_link=true",
                        status_code=307,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                    )
        raise api_exception(404, "not_found", str(exc)) from exc
    row = await _public_deliverable(request, deliverable_id)
    html_bytes = (
        _try_render_standalone_response(
            row,
            theme_family=await _publication_theme_family(request, row),
            media_base=f"/api/v1/deliverables/share/{quote(token)}/media",
            standalone_url=f"/api/v1/deliverables/share/{quote(token)}/view",
            pdf_url=f"/api/v1/deliverables/share/{quote(token)}/download.pdf",
            html_url=f"/api/v1/deliverables/share/{quote(token)}/download.html",
            markdown_url=(
                f"/api/v1/deliverables/share/{quote(token)}/download.md"
                if row.format in {"rich", "markdown"}
                else ""
            ),
        )
        if _standalone_assets_available(request)
        else None
    )
    standalone = html_bytes is not None
    if html_bytes is None:
        html_document = _render_html_with_media(
            row,
            download_pdf_url=f"/api/v1/deliverables/share/{quote(token)}/download.pdf",
            media_resolver=_proxy_media_resolver(
                row,
                route_prefix=f"/api/v1/deliverables/share/{quote(token)}/media",
            ),
        )
        html_bytes = html_document.encode("utf-8")
    return Response(
        html_bytes,
        media_type="text/html; charset=utf-8",
        headers=_html_headers(
            row,
            len(html_bytes),
            cache_control="no-store",
            standalone=standalone,
        ),
    )


@router.get("/share/{token}/download.pdf")
async def public_deliverable_pdf(request: Request, token: str) -> Response:
    """Return a standalone PDF render for a signed public share token."""

    try:
        deliverable_id, _expires_at = verify_deliverable_share_token(
            request.app.state.artifact_store, token
        )
    except DeliverableShareUnavailable as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    row = await _public_deliverable(request, deliverable_id)
    pdf_bytes = await _cached_pdf(request, row, access_scope=f"share:{token}")
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers=_pdf_headers(row, len(pdf_bytes), cache_control="no-store"),
    )


@router.get("/share/{token}/media/{media_key}")
async def public_deliverable_media(request: Request, token: str, media_key: str) -> Response:
    """Serve one manifest-referenced image under the deliverable share token."""

    try:
        deliverable_id, expires_at = verify_deliverable_share_token(
            request.app.state.artifact_store, token
        )
    except DeliverableShareUnavailable as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    row = await _public_deliverable(request, deliverable_id)
    max_age = max(0, min(60, expires_at - int(time.time())))
    return await _media_response(
        request,
        row,
        media_key,
        cache_control=f"public, max-age={max_age}",
    )


@router.get("/share/{token}/download.html")
async def public_deliverable_html_download(request: Request, token: str) -> Response:
    """Download an offline publication using a valid share token."""

    row = await _shared_deliverable(request, token)
    return await _single_html_response(request, row, cache_control="no-store")


@router.get("/share/{token}/download.md")
async def public_deliverable_markdown_download(request: Request, token: str) -> Response:
    """Download the Markdown fallback using a valid share token."""

    row = await _shared_deliverable(request, token)
    return _markdown_response(row, cache_control="no-store")


async def _shared_deliverable(request: Request, token: str) -> DeliverableRow:
    try:
        deliverable_id, _expires_at = verify_deliverable_share_token(
            request.app.state.artifact_store, token
        )
    except DeliverableShareUnavailable as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    return await _public_deliverable(request, deliverable_id)


async def _single_html_response(
    request: Request, row: DeliverableRow, *, cache_control: str
) -> Response:
    media_resolver = await _embedded_media_resolver(request, row, require_all=True)
    theme_family = await _publication_theme_family(request, row, use_viewer=True)
    format_ = str(row.format).lower()
    payload = (
        row.rich_payload
        if format_ == "rich" and isinstance(row.rich_payload, dict)
        else _wrapped_standalone_rich_payload(row, format_=format_)
    )
    try:
        document = (
            await asyncio.to_thread(
                render_offline_html,
                row,
                payload=payload,
                media_resolver=media_resolver,
                theme_family=theme_family,
            )
        ).encode("utf-8")
    except DeliverableRenderError as exc:
        raise api_exception(
            503, "export_unavailable", "Offline publication assets are unavailable"
        ) from exc
    if len(document) > MAX_SINGLE_HTML_BYTES:
        raise api_exception(413, "export_too_large", "Single HTML export exceeds 32 MiB")
    return Response(
        document,
        media_type="text/html; charset=utf-8",
        headers={
            **_html_headers(row, len(document), cache_control=cache_control),
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(_filename(row, '.html'), safe='')}"
            ),
        },
    )


async def _publication_theme_family(
    request: Request, row: DeliverableRow, *, use_viewer: bool = False
) -> str:
    # Only the family is public. Appearance and all other preferences stay private.
    viewer_email = getattr(request.state, "deliverable_user_scope", None) if use_viewer else None
    email = viewer_email or row.owner_email
    return await theme_family_for_user(request.app.state.session_factory, email)


def _markdown_response(row: DeliverableRow, *, cache_control: str) -> Response:
    if row.format not in {"rich", "markdown"}:
        raise api_exception(404, "not_found", "Markdown export not available")
    document = str(row.content or "").encode("utf-8")
    return Response(
        document,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": cache_control,
            "Content-Length": str(len(document)),
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(_filename(row, '.md'), safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _authorized_deliverable(
    request: Request,
    deliverable_id: str,
    *,
    accessor_conversation_id: str | None = None,
    allow_unscoped_ref: bool = True,
    allow_project_reader: bool = True,
) -> DeliverableRow:
    user = require_current_user(request)
    request.state.deliverable_user_scope = user.email
    artifact_store = request.app.state.artifact_store
    async with request.app.state.session_factory() as session:
        rows = await readable_deliverables(
            session,
            [deliverable_id],
            user.email,
            accessor_conversation_id=accessor_conversation_id,
            allow_unscoped_ref=allow_unscoped_ref,
            allow_project_reader=allow_project_reader,
        )
    read = rows.get(deliverable_id)
    if read is None:
        raise api_exception(404, "not_found", "Deliverable not found")
    await _hydrate_available_payload(
        read.row, artifact_store, hide_missing=read.kind == "reference"
    )
    return read.row


async def _public_deliverable(request: Request, deliverable_id: str) -> DeliverableRow:
    """Resolve a public link to the newest live revision of the shared document.

    Links name the revision they were created from, but outside viewers always see the latest
    version; the head must belong to the same owner and also be publishable.
    """
    async with request.app.state.session_factory() as session:
        row = await get_deliverable(session, deliverable_id)
        if row is None or await _is_managed_deliverable(session, row):
            raise api_exception(404, "not_found", "Deliverable not found")
        head = await get_deliverable_lineage_head(session, deliverable_lineage_key(row))
        if (
            head is not None
            and head.deliverable_id != row.deliverable_id
            and head.owner_email == row.owner_email
            and not await _is_managed_deliverable(session, head)
        ):
            row = head
        await _hydrate_available_payload(row, request.app.state.artifact_store)
        return row


async def _hydrate_available_payload(
    row: DeliverableRow, artifact_store: Any, *, hide_missing: bool = False
) -> None:
    """Translate absent payload storage after access has been authorized."""
    try:
        await hydrate_deliverable_payload(row, artifact_store)
    except FileNotFoundError as exc:
        if hide_missing:
            raise api_exception(404, "not_found", "Deliverable not found") from exc
        raise api_exception(
            409, "deliverable_payload_unavailable", "Deliverable payload is unavailable"
        ) from exc


async def _media_response(
    request: Request,
    row: DeliverableRow,
    media_key: str,
    *,
    cache_control: str,
) -> Response:
    if re.fullmatch(r"media_[0-9a-f]{24}", media_key) is None:
        raise api_exception(404, "not_found", "Deliverable media not found")
    async with request.app.state.session_factory() as session:
        resolved = await resolve_deliverable_media(
            session,
            request.app.state.artifact_store,
            row,
            media_key,
        )
    if resolved is None:
        raise api_exception(404, "not_found", "Deliverable media not found")
    content, item, _artifact = resolved
    filename = str(item.get("filename") or media_key)
    return Response(
        content,
        media_type=str(item["mime_type"]),
        headers={
            "Cache-Control": cache_control,
            "Content-Length": str(len(content)),
            "Content-Disposition": (f"inline; filename*=UTF-8''{quote(filename, safe='')}"),
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


def _media_manifest(row: DeliverableRow) -> dict[str, dict[str, Any]]:
    payload = getattr(row, "rich_payload", None)
    manifest = payload.get("media_manifest") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        return {}
    return {
        key: value
        for key, value in manifest.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _proxy_media_resolver(
    row: DeliverableRow,
    *,
    route_prefix: str,
) -> Any:
    manifest = _media_manifest(row)

    def resolve(reference: MediaReference, target: str) -> ResolvedMedia | None:
        item = manifest.get(reference.ref_id)
        mime_type = str((item or {}).get("mime_type") or reference.mime_type or "")
        if target != "html" or item is None or not mime_type.startswith("image/"):
            return None
        return ResolvedMedia(
            src=f"{route_prefix}/{quote(reference.ref_id, safe='')}",
            mime_type=mime_type,
            filename=str(item.get("filename") or "") or None,
        )

    return resolve


def _render_html_with_media(
    row: DeliverableRow,
    *,
    download_pdf_url: str | None,
    media_resolver: Any,
) -> str:
    if _media_manifest(row):
        return render_standalone_html(
            row,
            download_pdf_url=download_pdf_url,
            media_resolver=media_resolver,
        )
    return render_standalone_html(row, download_pdf_url=download_pdf_url)


def _standalone_assets_available(request: Request) -> bool:
    return bool(getattr(request.app.state, "serve_ui", False)) and (
        resolve_standalone_manifest() is not None
    )


_MIN_HEADINGS_FOR_STANDALONE_TOC = 3


def _wrapped_standalone_rich_payload(row: DeliverableRow, *, format_: str) -> dict[str, Any]:
    """Wraps a non-rich deliverable's content as a single rich block.

    Mirrors `AssistantDeliverableBlock.svelte`'s client-side wrap so the
    standalone page renders markdown/plain/html deliverables through the
    exact same unified RichDeliverable shell (TOC, full-view, hero, etc.)
    as embedded chat and rich deliverables, instead of falling back to the
    older, more limited `render_standalone_html` renderer.
    """

    content = str(getattr(row, "content", "") or "")
    block_type = "code" if format_ == "plain" else "raw_html" if format_ == "html" else "markdown"
    metadata: dict[str, Any] = {}
    if block_type == "markdown":
        # A single wrapped markdown block only ever contributes one
        # top-level TOC entry (its own title), so the generic multi-block
        # substantiality heuristic (`isSubstantialDocument` in
        # publication.ts, gated on >= 4 top-level headings) can never
        # trigger here -- force the TOC on/off explicitly instead, using
        # the same heading-count threshold the old bespoke markdown card
        # used, with a depth deep enough for nested markdown headings to
        # actually be extracted (`buildTocItems` only descends into a
        # markdown block's own headings when `depth > level`).
        heading_count = len(_markdown_headings(content))
        metadata = {
            "toc": {"enabled": heading_count >= _MIN_HEADINGS_FOR_STANDALONE_TOC, "depth": 4}
        }
    return {"metadata": metadata, "blocks": [{"type": block_type, "content": content}]}


def _try_render_standalone_response(
    row: DeliverableRow,
    *,
    media_base: str,
    standalone_url: str,
    pdf_url: str,
    html_url: str = "",
    markdown_url: str = "",
    share_expired: bool = False,
    theme_family: str | None = None,
) -> bytes | None:
    format_ = str(getattr(row, "format", "") or "").lower()
    rich_payload_override = (
        None if format_ == "rich" else _wrapped_standalone_rich_payload(row, format_=format_)
    )
    try:
        document = render_standalone_shell(
            row,
            media_base=media_base,
            standalone_url=standalone_url,
            pdf_url=pdf_url,
            html_url=html_url,
            markdown_url=markdown_url,
            rich_payload_override=rich_payload_override,
            share_expired=share_expired,
            theme_family=theme_family,
        )
    except DeliverableRenderError:
        return None
    return document.encode("utf-8")


async def _embedded_media_resolver(
    request: Request, row: DeliverableRow, *, require_all: bool = False
) -> Any:
    resolved: dict[str, ResolvedMedia] = {}
    manifest = _media_manifest(row)
    if (
        require_all
        and sum(int(item.get("size_bytes") or 0) for item in manifest.values()) * 4 // 3
        > MAX_SINGLE_HTML_BYTES
    ):
        raise api_exception(413, "export_too_large", "Single HTML export exceeds 32 MiB")
    async with request.app.state.session_factory() as session:
        for media_key in manifest:
            media = await resolve_deliverable_media(
                session,
                request.app.state.artifact_store,
                row,
                media_key,
            )
            if media is None:
                if require_all:
                    raise api_exception(
                        409, "deliverable_media_unavailable", "Deliverable media is unavailable"
                    )
                continue
            content, item, _artifact = media
            mime_type = str(item["mime_type"])
            encoded = base64.b64encode(content).decode("ascii")
            resolved[media_key] = ResolvedMedia(
                src=f"data:{mime_type};base64,{encoded}",
                mime_type=mime_type,
                filename=str(item.get("filename") or "") or None,
            )

    def resolve(reference: MediaReference, target: str) -> ResolvedMedia | None:
        return (
            resolved.get(reference.ref_id)
            if target == "pdf" or (require_all and target == "html")
            else None
        )

    return resolve


async def _is_managed_deliverable(session: Any, row: DeliverableRow) -> bool:
    return bool(await session.scalar(select(~unmanaged_deliverable_clause(row))))


async def _cached_html(request: Request, row: DeliverableRow) -> bytes:
    artifact_store = request.app.state.artifact_store
    namespace, object_id = _storage_ref(row)
    cache_key = deliverable_cache_key(row)
    if row.html_cache_key == cache_key and await artifact_store.async_exists(
        namespace, object_id, HTML_CACHE_FILENAME
    ):
        content, _mime = await artifact_store.async_load(namespace, object_id, HTML_CACHE_FILENAME)
        return cast(bytes, content)
    html_bytes = _render_html_with_media(
        row,
        download_pdf_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/download.pdf",
        media_resolver=_proxy_media_resolver(
            row,
            route_prefix=f"/api/v1/deliverables/{quote(row.deliverable_id)}/media",
        ),
    ).encode()
    await artifact_store.async_save(
        namespace, object_id, HTML_CACHE_FILENAME, html_bytes, "text/html; charset=utf-8"
    )
    await _update_cache_key(request, row.deliverable_id, html_cache_key=cache_key)
    row.html_cache_key = cache_key
    return html_bytes


async def _cached_pdf(request: Request, row: DeliverableRow, *, access_scope: str) -> bytes:
    artifact_store = request.app.state.artifact_store
    namespace, object_id = _storage_ref(row)
    cache_key = deliverable_cache_key(row)
    if row.pdf_cache_key == cache_key and await artifact_store.async_exists(
        namespace, object_id, PDF_CACHE_FILENAME
    ):
        content, _mime = await artifact_store.async_load(namespace, object_id, PDF_CACHE_FILENAME)
        return cast(bytes, content)
    del access_scope
    flight_key = (namespace, object_id, cache_key)
    task = _pdf_render_flights.get(flight_key)
    if task is None:
        task = asyncio.create_task(_render_and_cache_pdf(request, row, cache_key=cache_key))
        _pdf_render_flights[flight_key] = task

        def clear_pdf_flight(
            completed: asyncio.Task[bytes], key: tuple[str, str, str] = flight_key
        ) -> None:
            if _pdf_render_flights.get(key) is completed:
                _pdf_render_flights.pop(key, None)

        task.add_done_callback(clear_pdf_flight)
    try:
        return await asyncio.shield(task)
    except DeliverableRenderError as exc:
        raise api_exception(503, "render_unavailable", exc.reason) from exc


async def _render_and_cache_pdf(
    request: Request,
    row: DeliverableRow,
    *,
    cache_key: str,
) -> bytes:
    artifact_store = request.app.state.artifact_store
    namespace, object_id = _storage_ref(row)
    if row.pdf_cache_key == cache_key and await artifact_store.async_exists(
        namespace, object_id, PDF_CACHE_FILENAME
    ):
        content, _mime = await artifact_store.async_load(namespace, object_id, PDF_CACHE_FILENAME)
        return cast(bytes, content)
    if _media_manifest(row):
        media_resolver = await _embedded_media_resolver(request, row)
        html_document = render_standalone_html(
            row,
            media_resolver=media_resolver,
            render_target="pdf",
        )
    else:
        html_document = render_standalone_html(row, render_target="pdf")
    rendered = await render_pdf_bytes(html_document)
    await artifact_store.async_save(
        namespace, object_id, PDF_CACHE_FILENAME, rendered.content, "application/pdf"
    )
    await _update_cache_key(request, row.deliverable_id, pdf_cache_key=cache_key)
    row.pdf_cache_key = cache_key
    return rendered.content


async def _update_cache_key(request: Request, deliverable_id: str, **values: Any) -> None:
    from sqlalchemy import update

    async with request.app.state.session_factory() as session:
        await session.execute(
            update(DeliverableRow)
            .where(DeliverableRow.deliverable_id == deliverable_id)
            .values(**values)
        )
        await session.commit()


def _storage_ref(row: DeliverableRow) -> tuple[str, str]:
    return (
        row.storage_namespace or "deliverables",
        row.storage_object_id or row.deliverable_id,
    )


def _filename(row: DeliverableRow, extension: str) -> str:
    raw_title = str(row.title or f"deliverable-{row.deliverable_id}").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw_title).strip(".-_")
    safe = safe or f"deliverable-{row.deliverable_id}"
    if not safe.lower().endswith(extension):
        safe = f"{safe}{extension}"
    return safe


def _html_headers(
    row: DeliverableRow,
    content_length: int,
    *,
    cache_control: str,
    standalone: bool = False,
) -> dict[str, str]:
    return {
        "Cache-Control": cache_control,
        "Content-Length": str(content_length),
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(_filename(row, '.html'), safe='')}",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": STANDALONE_CSP if standalone else STATIC_CSP,
    }


def _pdf_headers(row: DeliverableRow, content_length: int, *, cache_control: str) -> dict[str, str]:
    return {
        "Cache-Control": cache_control,
        "Content-Length": str(content_length),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(_filename(row, '.pdf'), safe='')}",
        "X-Content-Type-Options": "nosniff",
    }


def _public_base_url(request: Request) -> str:
    config = getattr(request.app.state.artifact_store, "_config", None)
    base_url = str(getattr(config, "base_url", "") or "").rstrip("/")
    if base_url:
        return base_url
    return str(request.base_url).rstrip("/")


def _share_ttl_seconds(request: Request) -> int:
    config = getattr(request.app.state, "config", None)
    raw = getattr(config, "deliverable_share_link_ttl_seconds", DEFAULT_SHARE_TTL_SECONDS)
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SHARE_TTL_SECONDS
