"""Authorization, normalization, and byte resolution for rich deliverable media."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.models.deliverable import RICH_DELIVERABLE_MAX_BYTES, RichPayloadValidationError
from cognis.store.queries import get_artifact_record, mark_artifacts_attached

RICH_MEDIA_MAX_BYTES = 10 * 1024 * 1024
RICH_MEDIA_MAX_DIMENSION = 16_384
RICH_MEDIA_MAX_PIXELS = 64_000_000
SAFE_RICH_MEDIA_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_MEDIA_FIELDS = ("alt", "credit", "source_url", "role", "aspect_ratio", "focal_point")


def _media_error(
    path: str, expected: str, received: Any, reason: str
) -> RichPayloadValidationError:
    return RichPayloadValidationError(
        reason=reason,
        path=path,
        expected=expected,
        received=received,
    )


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(content)) as image:
            detected_mime = Image.MIME.get(image.format or "")
            dimensions = image.size
            if detected_mime != mime_type:
                return None
            image.verify()
            return dimensions
    except (
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None


def _iter_blocks(blocks: list[Any], path: str = "$.blocks"):
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_path = f"{path}[{index}]"
        yield block, block_path
        for key in ("blocks", "children"):
            children = block.get(key)
            if isinstance(children, list):
                yield from _iter_blocks(children, f"{block_path}.{key}")
        if block.get("type") in {"accordion", "gallery", "modal", "tabs"}:
            items = block.get("items")
            if isinstance(items, list):
                yield from _iter_blocks(items, f"{block_path}.items")


def _media_ref(media: dict[str, Any]) -> str | None:
    for key in ("ref", "artifact_id", "content_ref"):
        value = media.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def rich_payload_has_media(payload: dict[str, Any]) -> bool:
    """Return whether any authored block declares media."""

    return any(
        block.get("media") is not None for block, _path in _iter_blocks(payload.get("blocks", []))
    )


async def authorize_rich_media(
    session: AsyncSession,
    artifact_store: Any,
    payload: dict[str, Any],
    *,
    owner_email: str,
    accessor_conversation_id: str,
    accessor_agent_id: str | None,
    retain: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and normalize media, optionally retaining referenced artifacts."""

    if "media_manifest" in payload:
        raise _media_error(
            "$.media_manifest",
            "omitted; this field is controller-owned",
            payload["media_manifest"],
            "reserved_rich_media_manifest",
        )
    manifest: dict[str, dict[str, Any]] = {}
    retained: set[str] = set()
    for block, block_path in _iter_blocks(payload.get("blocks", [])):
        media = block.get("media")
        if media is None:
            continue
        media_path = f"{block_path}.media"
        if not isinstance(media, dict):
            raise _media_error(
                media_path,
                "object containing ref and optional media metadata",
                media,
                "invalid_rich_media",
            )
        ref = _media_ref(media)
        if ref is None:
            raise _media_error(
                f"{media_path}.ref",
                "authorized Cognis artifact-compatible ref",
                media,
                "missing_rich_media_ref",
            )
        row = await get_artifact_record(session, ref)
        if not await artifact_authorized_for_conversation(
            session,
            artifact=row,
            owner_email=owner_email,
            conversation_id=accessor_conversation_id,
            agent_id=accessor_agent_id,
        ):
            raise _media_error(
                f"{media_path}.ref",
                "existing artifact accessible from this conversation",
                ref,
                "rich_media_not_accessible",
            )
        assert row is not None
        if row.status == "deleted" or _expired(row):
            raise _media_error(
                f"{media_path}.ref",
                "active artifact",
                ref,
                "rich_media_not_found",
            )
        mime_type = str(row.mime_type).split(";", 1)[0].strip().lower()
        if mime_type not in SAFE_RICH_MEDIA_MIME_TYPES:
            raise _media_error(
                f"{media_path}.ref",
                f"raster image MIME in {sorted(SAFE_RICH_MEDIA_MIME_TYPES)}; SVG/HTML are not inline media",
                mime_type,
                "unsupported_rich_media_type",
            )
        if int(row.size_bytes or 0) <= 0 or int(row.size_bytes) > RICH_MEDIA_MAX_BYTES:
            raise _media_error(
                f"{media_path}.ref",
                f"image size 1..{RICH_MEDIA_MAX_BYTES} bytes",
                row.size_bytes,
                "invalid_rich_media_size",
            )
        try:
            content, stored_mime = await artifact_store.async_load(
                row.namespace, row.object_id, row.filename
            )
        except FileNotFoundError as exc:
            raise _media_error(
                f"{media_path}.ref",
                "artifact bytes still present",
                ref,
                "rich_media_not_found",
            ) from exc
        actual_mime = str(stored_mime).split(";", 1)[0].strip().lower()
        if actual_mime != mime_type or len(content) != int(row.size_bytes):
            raise _media_error(
                f"{media_path}.ref",
                "stored bytes matching artifact MIME and size metadata",
                ref,
                "rich_media_metadata_mismatch",
            )
        dimensions = _image_dimensions(content, mime_type)
        if dimensions is None:
            raise _media_error(
                f"{media_path}.ref",
                "decodable image with intrinsic dimensions",
                ref,
                "invalid_rich_media_dimensions",
            )
        width, height = dimensions
        if (
            width <= 0
            or height <= 0
            or width > RICH_MEDIA_MAX_DIMENSION
            or height > RICH_MEDIA_MAX_DIMENSION
            or width * height > RICH_MEDIA_MAX_PIXELS
        ):
            raise _media_error(
                f"{media_path}.ref",
                (
                    f"dimensions <= {RICH_MEDIA_MAX_DIMENSION}px per side and "
                    f"<= {RICH_MEDIA_MAX_PIXELS} pixels"
                ),
                dimensions,
                "invalid_rich_media_dimensions",
            )
        digest = hashlib.sha256(content).hexdigest()
        if row.content_hash and row.content_hash != digest:
            raise _media_error(
                f"{media_path}.ref",
                "stored bytes matching artifact hash metadata",
                ref,
                "rich_media_hash_mismatch",
            )
        authored = {key: media[key] for key in _MEDIA_FIELDS if key in media}
        identity = json.dumps(
            {"ref": ref, **authored}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        media_key = f"media_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        block["media"] = {"key": media_key, **authored}
        manifest[media_key] = {
            "artifact_ref": ref,
            "mime_type": mime_type,
            "filename": row.filename,
            "size_bytes": len(content),
            "width": width,
            "height": height,
            "sha256": digest,
            "provenance": {
                key: authored[key] for key in ("credit", "source_url") if key in authored
            },
        }
        retained.add(ref)
    if manifest:
        payload["media_manifest"] = manifest
        persisted_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if persisted_size > RICH_DELIVERABLE_MAX_BYTES:
            raise _media_error(
                "$.media_manifest",
                f"final rich payload <= {RICH_DELIVERABLE_MAX_BYTES} bytes",
                persisted_size,
                "rich_payload_too_large",
            )
        # Re-home retained media to the deliverable's conversation. Managed
        # ancestor access permits a controller to reference a child artifact,
        # but leaving the artifact scoped to that child makes it orphaned when
        # the child conversation is purged while the deliverable survives.
        # Explicit deletion remains authoritative and the media resolver then
        # degrades to 404.
        if retain:
            await mark_artifacts_attached(
                session,
                sorted(retained),
                conversation_id=accessor_conversation_id,
                message_role="assistant",
            )
    return payload, sorted(retained)


async def resolve_deliverable_media(
    session: AsyncSession,
    artifact_store: Any,
    deliverable: Any,
    media_key: str,
) -> tuple[bytes, dict[str, Any], Any] | None:
    """Resolve one manifest member after deliverable authorization."""

    payload = getattr(deliverable, "rich_payload", None)
    manifest = payload.get("media_manifest") if isinstance(payload, dict) else None
    item = manifest.get(media_key) if isinstance(manifest, dict) else None
    if not isinstance(item, dict):
        return None
    artifact_ref = item.get("artifact_ref")
    if not isinstance(artifact_ref, str):
        return None
    row = await get_artifact_record(session, artifact_ref)
    if row is None or row.status == "deleted" or _expired(row):
        return None
    if (
        row.mime_type.split(";", 1)[0].strip().lower() != item.get("mime_type")
        or row.filename != item.get("filename")
        or int(row.size_bytes) != item.get("size_bytes")
        or row.content_hash not in {None, item.get("sha256")}
    ):
        return None
    try:
        content, stored_mime = await artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
    except FileNotFoundError:
        return None
    if (
        len(content) != item.get("size_bytes")
        or hashlib.sha256(content).hexdigest() != item.get("sha256")
        or stored_mime.split(";", 1)[0].strip().lower() != item.get("mime_type")
    ):
        return None
    return content, item, row


def _expired(row: Any) -> bool:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)
