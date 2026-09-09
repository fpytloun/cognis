"""Materialize authorized Cognis images for direct Codex requests."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import warnings
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from PIL import Image, UnidentifiedImageError

from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.models.artifact import ArtifactKind, ArtifactStatus
from cognis.store.queries import get_artifact_records

MAX_CODEX_IMAGE_DIMENSION = 2048
MAX_CODEX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CODEX_ENCODED_IMAGE_BYTES = 28 * 1024 * 1024
DEFAULT_CODEX_IMAGE_CACHE_BYTES = 64 * 1024 * 1024
_SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class CodexArtifactError(ValueError):
    """An image artifact cannot be safely sent to direct Codex."""

    def __init__(self, message: str, *, artifact_ids: list[str] | None = None) -> None:
        self.artifact_ids = list(artifact_ids or [])
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        """Return a safe attachment failure for agent-loop recovery."""

        return {
            "category": "attachment_input",
            "code": type(self).__name__,
            "message": str(self),
            "artifact_ids": self.artifact_ids,
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

        data_url = await asyncio.to_thread(_normalize_data_url, content, artifact_id=artifact_id)
        if self._max_bytes <= 0 or len(data_url) > self._max_bytes:
            return data_url

        async with self._lock:
            cached = self._entries.get(cache_key)
            if cached is not None:
                self._entries.move_to_end(cache_key)
                return cached
            self._entries[cache_key] = data_url
            self._size_bytes += len(data_url)
            while self._size_bytes > self._max_bytes:
                _evicted_key, evicted_data_url = self._entries.popitem(last=False)
                self._size_bytes -= len(evicted_data_url)
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
    async with session_factory() as session:
        for record in await get_artifact_records(session, sorted(artifact_ids)):
            if await artifact_authorized_for_conversation(
                session,
                artifact=record,
                owner_email=owner_email,
                conversation_id=conversation_id,
                agent_id=agent_id,
            ):
                records[record.artifact_id] = record

    data_urls: dict[str, str] = {}
    encoded_total = 0
    now = datetime.now(UTC)
    for artifact_id in sorted(artifact_ids):
        selected_record: Any | None = records.get(artifact_id)
        if not _record_is_available(selected_record, now=now):
            raise CodexArtifactError(
                f"Image artifact is unavailable: {artifact_id}", artifact_ids=[artifact_id]
            )
        assert selected_record is not None
        if selected_record.kind != ArtifactKind.IMAGE:
            raise CodexArtifactError(
                f"Artifact is not an image: {artifact_id}", artifact_ids=[artifact_id]
            )
        if selected_record.size_bytes > MAX_CODEX_IMAGE_BYTES:
            raise CodexArtifactError(
                f"Image artifact is too large: {artifact_id}", artifact_ids=[artifact_id]
            )
        try:
            content, _stored_type = await artifact_store.async_load(
                selected_record.namespace,
                selected_record.object_id,
                selected_record.filename,
            )
        except Exception as exc:
            raise CodexArtifactError(
                f"Image artifact is unavailable: {artifact_id}", artifact_ids=[artifact_id]
            ) from exc
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
            if exc.artifact_ids:
                raise
            raise CodexArtifactError(str(exc), artifact_ids=[artifact_id]) from exc
        encoded_total += len(data_url) * reference_counts[artifact_id]
        if encoded_total > MAX_CODEX_ENCODED_IMAGE_BYTES:
            raise CodexArtifactError("Encoded image payload exceeds the direct Codex limit")
        data_urls[artifact_id] = data_url

    materialized = copy.deepcopy(messages)
    for message in materialized:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, dict):
                continue
            metadata = image_url.pop("cognis_artifact", None)
            if not isinstance(metadata, dict):
                continue
            metadata_artifact_id = metadata.get("artifact_id")
            if isinstance(metadata_artifact_id, str) and metadata_artifact_id in data_urls:
                image_url["url"] = data_urls[metadata_artifact_id]
    return materialized


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
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in _SUPPORTED_FORMATS:
                    raise CodexArtifactError(f"Unsupported image format: {artifact_id}")
                if (
                    bool(getattr(image, "is_animated", False))
                    or int(getattr(image, "n_frames", 1)) != 1
                ):
                    raise CodexArtifactError(f"Animated image is not supported: {artifact_id}")
                image.load()
                target_format = image.format
                if max(image.size) <= MAX_CODEX_IMAGE_DIMENSION:
                    return content, _FORMAT_MIME_TYPES[target_format]
                image.thumbnail(
                    (MAX_CODEX_IMAGE_DIMENSION, MAX_CODEX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                normalized_image: Image.Image = image
                if target_format == "JPEG" and image.mode not in {"RGB", "L"}:
                    normalized_image = image.convert("RGB")
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
    encoded, mime_type = _normalize_image(content, artifact_id=artifact_id)
    return f"data:{mime_type};base64,{base64.b64encode(encoded).decode('ascii')}"
