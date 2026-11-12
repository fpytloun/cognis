"""Materialize authorized Cognis images for direct Codex requests."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import logging
import threading
import warnings
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageOps, JpegImagePlugin, UnidentifiedImageError

from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.models.artifact import ArtifactKind, ArtifactStatus
from cognis.providers.llm.errors import MidStreamErrorCategory
from cognis.store.queries import get_artifact_records

logger = logging.getLogger(__name__)

MAX_CODEX_IMAGE_DIMENSION = 2048
MAX_CODEX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CODEX_ENCODED_IMAGE_BYTES = 28 * 1024 * 1024
DEFAULT_CODEX_IMAGE_CACHE_BYTES = 64 * 1024 * 1024
MAX_CODEX_IMAGE_PIXELS = 16 * 1024 * 1024
# JPEG can reduce its decode dimensions in the codec before allocating pixels.
# Keep a separate source ceiling; other formats still use the full-decode budget.
MAX_CODEX_JPEG_SOURCE_PIXELS = 64 * 1024 * 1024
# The native worker owns this slot, including after its asyncio waiter is cancelled.
_decode_slot = threading.Lock()
# Still-image formats direct Codex accepts on the wire. Anything else that
# Pillow can decode is converted to _CONVERSION_FORMAT instead of rejected.
_SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_CONVERSION_FORMAT = "PNG"
_PNG_SAFE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})
_FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class CodexArtifactError(ValueError):
    """An image artifact cannot be safely sent to direct Codex.

    Per-artifact problems are handled by degrading that attachment, so this
    error only escapes materialization for a deterministic request-level
    precondition. Replaying the identical request cannot fix it.
    """

    def to_payload(self) -> dict[str, Any]:
        """Return a safe, non-retryable failure payload for agent-loop recovery."""

        return {
            "category": MidStreamErrorCategory.INVALID_REQUEST.value,
            "code": type(self).__name__,
            "message": str(self),
        }


class CodexImageNormalizationCache:
    """Bound normalized image bytes without caching artifact authorization."""

    def __init__(self, *, max_bytes: int = DEFAULT_CODEX_IMAGE_CACHE_BYTES) -> None:
        self._max_bytes = max(0, max_bytes)
        self._size_bytes = 0
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = asyncio.Lock()

    async def data_url(self, content: bytes, *, artifact_id: str) -> str:
        """Return cached normalization after the caller reauthorizes and reloads the artifact."""

        cache_key = await asyncio.to_thread(_image_content_hash, content)
        async with self._lock:
            cached = self._entries.get(cache_key)
            if cached is not None:
                self._entries.move_to_end(cache_key)
                return cached

        # Serialize misses so concurrent requests recheck the cache after the
        # first normalization. Authorization remains the caller's responsibility.
        async with self._lock:
            cached = self._entries.get(cache_key)
            if cached is not None:
                self._entries.move_to_end(cache_key)
                return cached
            data_url = await asyncio.to_thread(
                _normalize_data_url, content, artifact_id=artifact_id
            )
            if self._max_bytes > 0 and len(data_url) <= self._max_bytes:
                self._entries[cache_key] = data_url
                self._size_bytes += len(data_url)
                while self._size_bytes > self._max_bytes:
                    _, removed = self._entries.popitem(last=False)
                    self._size_bytes -= len(removed)
            return data_url

    def clear(self) -> None:
        """Remove retained normalized image content."""

        self._entries.clear()
        self._size_bytes = 0


async def materialize_codex_artifact_images(
    messages: list[dict[str, Any]],
    *,
    session_factory: Any,
    artifact_store: Any,
    owner_email: str | None,
    conversation_id: str | None,
    agent_id: str | None,
    session_id: str | None = None,
    llm_request_id: str | None = None,
    normalization_cache: CodexImageNormalizationCache | None = None,
) -> list[dict[str, Any]]:
    """Return a deep copy with authorized artifact images replaced by data URLs."""

    reference_counts = _artifact_reference_counts(messages)
    artifact_ids = set(reference_counts)
    if not artifact_ids:
        return copy.deepcopy(messages)
    if not owner_email or not conversation_id:
        raise CodexArtifactError("Cognis image materialization requires conversation identity")

    records: dict[str, Any] = {}
    rejection_categories: dict[str, str] = {}
    exception_types: dict[str, str] = {}
    now = datetime.now(UTC)
    async with session_factory() as session:
        for record in await get_artifact_records(session, sorted(artifact_ids)):
            if not _record_is_available(record, now=now):
                rejection_categories[record.artifact_id] = "record_unavailable"
            elif await artifact_authorized_for_conversation(
                session,
                artifact=record,
                owner_email=owner_email,
                conversation_id=conversation_id,
                agent_id=agent_id,
                session_id=session_id,
            ):
                records[record.artifact_id] = record
            else:
                rejection_categories[record.artifact_id] = "unauthorized"

    data_urls: dict[str, str] = {}
    # Artifacts that cannot be sent, mapped to a short model-facing reason.
    # Materialization degrades per artifact: one unreadable image must not
    # fail a whole request, because the identical retry would fail again.
    undeliverable: dict[str, str] = {}
    encoded_total = 0
    for artifact_id in sorted(artifact_ids):
        selected_record: Any | None = records.get(artifact_id)
        if not _record_is_available(selected_record, now=now):
            undeliverable[artifact_id] = "it is unavailable"
            rejection_categories.setdefault(artifact_id, "record_missing")
            continue
        assert selected_record is not None
        if selected_record.kind != ArtifactKind.IMAGE:
            undeliverable[artifact_id] = "it is not an image"
            rejection_categories[artifact_id] = "not_image"
            continue
        if selected_record.size_bytes > MAX_CODEX_IMAGE_BYTES:
            undeliverable[artifact_id] = "it is too large"
            rejection_categories[artifact_id] = "source_byte_limit"
            continue
        try:
            content, _stored_type = await artifact_store.async_load(
                selected_record.namespace,
                selected_record.object_id,
                selected_record.filename,
            )
        except Exception as exc:
            undeliverable[artifact_id] = "it is unavailable"
            rejection_categories[artifact_id] = "storage_load_failed"
            exception_types[artifact_id] = type(exc).__name__
            continue
        try:
            if normalization_cache is None:
                data_url = await asyncio.to_thread(
                    _normalize_data_url,
                    content,
                    artifact_id=artifact_id,
                )
            else:
                data_url = await normalization_cache.data_url(
                    content,
                    artifact_id=artifact_id,
                )
        except CodexArtifactError as exc:
            undeliverable[artifact_id] = f"safe image normalization failed: {exc}"
            rejection_categories[artifact_id] = "normalization_failed"
            exception_types[artifact_id] = type(exc).__name__
            continue
        projected_total = encoded_total + len(data_url) * reference_counts[artifact_id]
        if projected_total > MAX_CODEX_ENCODED_IMAGE_BYTES:
            undeliverable[artifact_id] = "the request image payload limit was reached"
            rejection_categories[artifact_id] = "request_byte_limit"
            continue
        encoded_total = projected_total
        data_urls[artifact_id] = data_url

    for artifact_id in sorted(undeliverable):
        logger.warning(
            "Native image materialization rejected",
            extra={
                "extra_data": {
                    "artifact_id": artifact_id,
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "llm_request_id": llm_request_id,
                    "category": rejection_categories[artifact_id],
                    "exception_type": exception_types.get(artifact_id),
                }
            },
        )

    materialized = copy.deepcopy(messages)
    for message in materialized:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        message["content"] = _rewrite_image_parts(
            content,
            data_urls=data_urls,
            undeliverable=undeliverable,
        )
    return materialized


def _rewrite_image_parts(
    content: list[Any],
    *,
    data_urls: dict[str, str],
    undeliverable: dict[str, str],
) -> list[Any]:
    """Replace artifact image parts with data URLs or a model-facing notice."""

    rewritten: list[Any] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            rewritten.append(part)
            continue
        image_url = part.get("image_url")
        if not isinstance(image_url, dict):
            rewritten.append(part)
            continue
        metadata = image_url.pop("cognis_artifact", None)
        artifact_id = metadata.get("artifact_id") if isinstance(metadata, dict) else None
        if not isinstance(artifact_id, str):
            rewritten.append(part)
            continue
        if artifact_id in data_urls:
            image_url["url"] = data_urls[artifact_id]
            rewritten.append(part)
            continue
        reason = undeliverable.get(artifact_id)
        if reason is None:
            rewritten.append(part)
            continue
        filename = metadata.get("filename") if isinstance(metadata, dict) else None
        label = filename if isinstance(filename, str) and filename else artifact_id
        rewritten.append(
            {
                "type": "text",
                "text": (
                    f'[Image "{label}" (artifact_id="{artifact_id}") was not sent to the '
                    f"model because {reason}. Use artifact_read with that artifact_id if "
                    "its content matters.]"
                ),
            }
        )
    return rewritten


def strip_cognis_artifact_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return provider-safe messages without Cognis-private attachment metadata."""

    projected = copy.deepcopy(messages)
    for message in projected:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url.pop("cognis_artifact", None)
    return projected


def _artifact_reference_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    artifact_counts: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            image_url = part.get("image_url") if isinstance(part, dict) else None
            metadata = image_url.get("cognis_artifact") if isinstance(image_url, dict) else None
            artifact_id = metadata.get("artifact_id") if isinstance(metadata, dict) else None
            if isinstance(artifact_id, str) and artifact_id:
                artifact_counts[artifact_id] = artifact_counts.get(artifact_id, 0) + 1
    return artifact_counts


def _record_is_available(record: Any | None, *, now: datetime) -> bool:
    if (
        record is None
        or record.status == ArtifactStatus.DELETED
        or getattr(record, "deleted_at", None) is not None
    ):
        return False
    raw_expires_at = getattr(record, "expires_at", None)
    if raw_expires_at is None:
        return True
    if not isinstance(raw_expires_at, datetime):
        return False
    expires_at = raw_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC) > now


def _normalize_image(content: bytes, *, artifact_id: str) -> tuple[bytes, str]:
    if len(content) > MAX_CODEX_IMAGE_BYTES:
        raise CodexArtifactError(f"Image artifact is too large: {artifact_id}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                source_pixel_limit = (
                    MAX_CODEX_JPEG_SOURCE_PIXELS
                    if isinstance(image, JpegImagePlugin.JpegImageFile)
                    else MAX_CODEX_IMAGE_PIXELS
                )
                if image.width * image.height > source_pixel_limit:
                    raise CodexArtifactError(f"Image exceeds source pixel budget: {artifact_id}")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                source_format = str(image.format or "")
                animated = (
                    bool(getattr(image, "is_animated", False))
                    or int(getattr(image, "n_frames", 1)) != 1
                )
                original_size = image.size
                jpeg_backed = isinstance(image, JpegImagePlugin.JpegImageFile)
                if jpeg_backed:
                    image.draft("RGB", (MAX_CODEX_IMAGE_DIMENSION, MAX_CODEX_IMAGE_DIMENSION))
                # draft() is advisory. Check the actual decoder size before load(),
                # including for aspect ratios the JPEG codec cannot shrink enough.
                if image.width * image.height > MAX_CODEX_IMAGE_PIXELS:
                    raise CodexArtifactError(f"Image exceeds decode pixel budget: {artifact_id}")
                image.load()
                # Direct Codex accepts a narrow set of still formats. Convert
                # anything else, including the first frame of an animation,
                # rather than refusing an image the model could otherwise read.
                needs_conversion = animated or source_format not in _SUPPORTED_FORMATS
                # MPO phone photos use the JPEG decoder but contain auxiliary
                # frames. Flatten the primary frame to JPEG, never replay MPO.
                target_format = (
                    "JPEG"
                    if jpeg_backed
                    else _CONVERSION_FORMAT
                    if needs_conversion
                    else source_format
                )
                oversized = max(image.size) > MAX_CODEX_IMAGE_DIMENSION
                orientation = image.getexif().get(274, 1)
                if (
                    not needs_conversion
                    and not oversized
                    and image.size == original_size
                    and orientation == 1
                ):
                    return content, _FORMAT_MIME_TYPES[target_format]
                # Resaving strips EXIF. Apply orientation first so phone photos
                # do not become sideways when their normalized bytes are cached.
                normalized_image = ImageOps.exif_transpose(image)
                if oversized:
                    normalized_image.thumbnail(
                        (MAX_CODEX_IMAGE_DIMENSION, MAX_CODEX_IMAGE_DIMENSION),
                        Image.Resampling.LANCZOS,
                    )
                if target_format == "JPEG" and normalized_image.mode not in {"RGB", "L"}:
                    normalized_image = normalized_image.convert("RGB")
                elif target_format == "PNG" and normalized_image.mode not in _PNG_SAFE_MODES:
                    normalized_image = normalized_image.convert("RGBA")
                output = io.BytesIO()
                normalized_image.save(output, format=target_format)
                return output.getvalue(), _FORMAT_MIME_TYPES[target_format]
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise CodexArtifactError(f"Image exceeds safe dimensions: {artifact_id}") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, CodexArtifactError):
            raise
        raise CodexArtifactError(f"Invalid image artifact: {artifact_id}") from exc


def _image_content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalize_data_url(content: bytes, *, artifact_id: str) -> str:
    with _decode_slot:
        encoded, mime_type = _normalize_image(content, artifact_id=artifact_id)
        return f"data:{mime_type};base64,{base64.b64encode(encoded).decode('ascii')}"
