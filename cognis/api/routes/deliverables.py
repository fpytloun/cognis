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

from cognis.api.common import api_exception, require_current_user
from cognis.api.models import DeliverableResponse
from cognis.api.serializers import deliverable_to_response
from cognis.core.content_refs import get_accessible_deliverable_ref
from cognis.core.deliverable_links import (
    DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS,
    DeliverableShareUnavailable,
    signed_deliverable_view_link,
    verify_deliverable_share_token,
)
from cognis.core.deliverable_media import resolve_deliverable_media
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
from cognis.rendering.rich_visuals import MediaReference, ResolvedMedia
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import DeliverableRow
from cognis.store.queries import (
    get_accessible_conversation_deliverable,
    get_conversation,
    get_deliverable,
    get_managed_conversation_link_for_target,
)
from cognis.ui_assets import resolve_standalone_asset, resolve_standalone_manifest

router = APIRouter(prefix="/api/v1/deliverables", tags=["deliverables"])
DEFAULT_SHARE_TTL_SECONDS = DEFAULT_DELIVERABLE_SHARE_TTL_SECONDS
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
    return deliverable_to_response(row)


@router.get("/{deliverable_id}/view")
async def deliverable_view(
    request: Request,
    deliverable_id: str,
    accessor_conversation_id: str | None = None,
) -> Response:
    """Return a standalone HTML render for a deliverable available to the current user."""

    row = await _authorized_deliverable(
        request,
        deliverable_id,
        accessor_conversation_id=accessor_conversation_id,
    )
    html_bytes = (
        _try_render_standalone_response(
            row,
            media_base=f"/api/v1/deliverables/{quote(row.deliverable_id)}/media",
            standalone_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/view",
            pdf_url=f"/api/v1/deliverables/{quote(row.deliverable_id)}/download.pdf",
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


@router.post("/{deliverable_id}/share-link")
async def deliverable_share_link(request: Request, deliverable_id: str) -> dict[str, str]:
    """Create a stateless signed public share link for a deliverable."""

    await _authorized_deliverable(request, deliverable_id)
    ttl_seconds = _share_ttl_seconds(request)
    try:
        link = signed_deliverable_view_link(
            request.app.state.artifact_store,
            deliverable_id,
            base_url=_public_base_url(request),
            ttl_seconds=ttl_seconds,
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
        raise api_exception(404, "not_found", str(exc)) from exc
    row = await _public_deliverable(request, deliverable_id)
    html_bytes = (
        _try_render_standalone_response(
            row,
            media_base=f"/api/v1/deliverables/share/{quote(token)}/media",
            standalone_url=f"/api/v1/deliverables/share/{quote(token)}/view",
            pdf_url=f"/api/v1/deliverables/share/{quote(token)}/download.pdf",
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


async def _authorized_deliverable(
    request: Request,
    deliverable_id: str,
    *,
    accessor_conversation_id: str | None = None,
    allow_unscoped_ref: bool = True,
) -> DeliverableRow:
    user = require_current_user(request)
    request.state.deliverable_user_scope = user.email
    artifact_store = request.app.state.artifact_store
    async with request.app.state.session_factory() as session:
        row = await get_accessible_conversation_deliverable(session, deliverable_id, user.email)
        if row is not None and not await _is_managed_deliverable(session, row):
            await hydrate_deliverable_payload(row, artifact_store)
            return row
        accessor_agent_id: str | None = None
        if accessor_conversation_id:
            accessor = await get_conversation(session, accessor_conversation_id)
            if accessor is not None and accessor.user_email == user.email:
                accessor_agent_id = accessor.agent_id
        ref = None
        if accessor_conversation_id or allow_unscoped_ref:
            ref = await get_accessible_deliverable_ref(
                session,
                artifact_store,
                deliverable_id,
                user.email,
                accessor_conversation_id=accessor_conversation_id,
                accessor_agent_id=accessor_agent_id,
            )
        if ref is not None:
            return ref.deliverable
    raise api_exception(404, "not_found", "Deliverable not found")


async def _public_deliverable(request: Request, deliverable_id: str) -> DeliverableRow:
    async with request.app.state.session_factory() as session:
        row = await get_deliverable(session, deliverable_id)
        if row is None or await _is_managed_deliverable(session, row):
            raise api_exception(404, "not_found", "Deliverable not found")
        await hydrate_deliverable_payload(row, request.app.state.artifact_store)
        return row


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
            rich_payload_override=rich_payload_override,
        )
    except DeliverableRenderError:
        return None
    return document.encode("utf-8")


async def _embedded_media_resolver(request: Request, row: DeliverableRow) -> Any:
    resolved: dict[str, ResolvedMedia] = {}
    async with request.app.state.session_factory() as session:
        for media_key in _media_manifest(row):
            media = await resolve_deliverable_media(
                session,
                request.app.state.artifact_store,
                row,
                media_key,
            )
            if media is None:
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
        return resolved.get(reference.ref_id) if target == "pdf" else None

    return resolve


async def _is_managed_deliverable(session: Any, row: DeliverableRow) -> bool:
    conversation_id = row.conversation_id
    if not conversation_id:
        return False
    return (await get_managed_conversation_link_for_target(session, conversation_id)) is not None


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
        task.add_done_callback(
            lambda completed, key=flight_key: (
                _pdf_render_flights.pop(key, None)
                if _pdf_render_flights.get(key) is completed
                else None
            )
        )
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
        html_document = render_standalone_html(row)
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
