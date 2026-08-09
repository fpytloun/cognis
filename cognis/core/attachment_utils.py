"""Helpers for assistant attachment normalization and hydration."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.models.artifact import ArtifactKind
from cognis.store.models import ArtifactRecordRow

_CANONICAL_ATTACHMENT_KEYS = {
    "artifact_id",
    "kind",
    "mime_type",
    "filename",
    "size_bytes",
    "url",
}


def attachment_label(attachment: dict[str, Any]) -> str:
    filename = str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
    kind = str(attachment.get("kind") or "file")
    artifact_id = attachment.get("artifact_id")
    details = [kind]
    if isinstance(artifact_id, str) and artifact_id:
        details.append(f"artifact_id={artifact_id}")
    return f"{filename} ({', '.join(details)})"


def normalize_attachment_ref(attachment: dict[str, Any]) -> dict[str, Any] | None:
    artifact_id = attachment.get("artifact_id")

    if not isinstance(artifact_id, str) or not artifact_id:
        return None

    normalized: dict[str, Any] = {
        "artifact_id": artifact_id,
    }
    kind = attachment.get("kind")
    if isinstance(kind, str) and kind:
        normalized["kind"] = kind
    mime_type = attachment.get("mime_type")
    if isinstance(mime_type, str) and mime_type:
        normalized["mime_type"] = mime_type
    filename = attachment.get("filename")
    if isinstance(filename, str) and filename:
        normalized["filename"] = filename
    size_bytes = attachment.get("size_bytes")
    if isinstance(size_bytes, int):
        normalized["size_bytes"] = size_bytes
    url = attachment.get("url")
    if isinstance(url, str) and url:
        normalized["url"] = url
    return normalized


def attachment_ref_to_dict(
    attachment: dict[str, Any] | Any,
    *,
    include_url: bool = True,
) -> dict[str, Any] | None:
    """Return a canonical attachment dict from either a model or a raw dict."""

    if isinstance(attachment, dict):
        raw = attachment
    elif hasattr(attachment, "model_dump"):
        exclude = None if include_url else {"url"}
        raw = attachment.model_dump(mode="json", exclude=exclude)
    else:
        return None
    if not include_url:
        raw = {key: value for key, value in raw.items() if key != "url"}
    return normalize_attachment_ref(raw)


def attachment_refs_to_dicts(
    attachments: Iterable[dict[str, Any] | Any],
    *,
    include_url: bool = True,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for attachment in attachments:
        item = attachment_ref_to_dict(attachment, include_url=include_url)
        if item is not None:
            normalized.append(item)
    return normalized


def normalize_attachment_refs(attachments: Iterable[dict[str, Any] | Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for attachment in attachments:
        item = attachment_ref_to_dict(attachment)
        if item is None:
            continue
        dedupe_key = (
            str(item["artifact_id"]),
            str(item.get("filename", "")),
            str(item.get("mime_type", "")),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(item)
    return normalized


def strip_attachment_payload_bytes(
    attachments: Iterable[dict[str, Any] | Any],
) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for attachment in attachments:
        normalized = attachment_ref_to_dict(attachment)
        if normalized is not None and {
            "kind",
            "mime_type",
            "filename",
            "size_bytes",
        }.issubset(normalized):
            safe.append(normalized)
    return safe


@dataclass(frozen=True, slots=True)
class _HydrationRecord:
    artifact_id: str
    namespace: str
    object_id: str
    filename: str
    kind: str
    mime_type: str
    size_bytes: int
    owner_email: str | None
    conversation_id: str | None
    session_id: str | None
    status: str
    deleted_at: datetime | None

    @classmethod
    def from_row(cls, row: ArtifactRecordRow) -> _HydrationRecord:
        return cls(
            artifact_id=row.artifact_id,
            namespace=row.namespace,
            object_id=row.object_id,
            filename=row.filename,
            kind=row.kind,
            mime_type=row.mime_type,
            size_bytes=int(row.size_bytes or 0),
            owner_email=row.owner_email,
            conversation_id=row.conversation_id,
            session_id=row.session_id,
            status=row.status,
            deleted_at=row.deleted_at,
        )


def _attachment_size_bytes(
    attachment: dict[str, Any], row: ArtifactRecordRow | _HydrationRecord
) -> int:
    size = attachment.get("size_bytes")
    if isinstance(size, int):
        return size
    return int(row.size_bytes or 0)


async def _load_hydration_records(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_ids: list[str],
) -> dict[str, _HydrationRecord]:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(ArtifactRecordRow).where(ArtifactRecordRow.artifact_id.in_(artifact_ids))
        )
        return {row.artifact_id: _HydrationRecord.from_row(row) for row in result.scalars().all()}


async def hydrate_attachment_refs(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_store: Any,
    attachments: Iterable[dict[str, Any] | Any],
    *,
    owner_email: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_attachment_refs(attachments)
    if not normalized:
        return []

    artifact_ids = [item["artifact_id"] for item in normalized]
    rows = await _load_hydration_records(session_factory, artifact_ids)

    hydrated: list[dict[str, Any]] = []
    for attachment in normalized:
        artifact_id = str(attachment["artifact_id"])
        row = rows.get(artifact_id)
        if row is None:
            legacy = _legacy_attachment_fallback(attachment)
            if legacy is not None:
                hydrated.append(legacy)
            continue
        if (
            owner_email is not None
            and row.owner_email is not None
            and row.owner_email != owner_email
        ):
            continue
        if (
            conversation_id is not None
            and row.conversation_id is not None
            and row.conversation_id != conversation_id
        ):
            continue
        if session_id is not None and row.session_id is not None and row.session_id != session_id:
            continue
        if row.status == "deleted":
            legacy = _deleted_attachment_fallback(attachment, row)
            if legacy is not None:
                hydrated.append(legacy)
            continue

        hydrated_attachment = {
            **attachment,
            "kind": str(attachment.get("kind") or row.kind or ArtifactKind.FILE.value),
            "mime_type": str(attachment.get("mime_type") or row.mime_type),
            "filename": str(attachment.get("filename") or row.object_id or row.filename),
            "size_bytes": _attachment_size_bytes(attachment, row),
        }
        try:
            hydrated_attachment["url"] = await artifact_store.async_get_public_url(
                row.namespace,
                row.object_id,
                row.filename,
            )
        except Exception:
            if (
                not isinstance(hydrated_attachment.get("url"), str)
                or not hydrated_attachment["url"]
            ):
                hydrated_attachment.pop("url", None)
        hydrated.append(hydrated_attachment)
    return hydrated


async def hydrate_attachment_ref_groups(
    session_factory: async_sessionmaker[AsyncSession],
    artifact_store: Any,
    attachment_groups: Iterable[Iterable[dict[str, Any] | Any]],
    *,
    owner_email: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> list[list[dict[str, Any]]]:
    """Hydrate multiple attachment lists with one artifact-record lookup."""

    normalized_groups = [normalize_attachment_refs(group) for group in attachment_groups]
    artifact_ids = sorted(
        {
            str(attachment["artifact_id"])
            for group in normalized_groups
            for attachment in group
            if attachment.get("artifact_id")
        }
    )
    if not artifact_ids:
        return [[] for _group in normalized_groups]

    rows = await _load_hydration_records(session_factory, artifact_ids)

    hydrated_groups: list[list[dict[str, Any]]] = []
    public_urls: dict[str, str] = {}
    for normalized in normalized_groups:
        hydrated: list[dict[str, Any]] = []
        for attachment in normalized:
            artifact_id = str(attachment["artifact_id"])
            row = rows.get(artifact_id)
            if row is None:
                legacy = _legacy_attachment_fallback(attachment)
                if legacy is not None:
                    hydrated.append(legacy)
                continue
            if (
                owner_email is not None
                and row.owner_email is not None
                and row.owner_email != owner_email
            ):
                continue
            if (
                conversation_id is not None
                and row.conversation_id is not None
                and row.conversation_id != conversation_id
            ):
                continue
            if (
                session_id is not None
                and row.session_id is not None
                and row.session_id != session_id
            ):
                continue
            if row.status == "deleted":
                legacy = _deleted_attachment_fallback(attachment, row)
                if legacy is not None:
                    hydrated.append(legacy)
                continue

            hydrated_attachment = {
                **attachment,
                "kind": str(attachment.get("kind") or row.kind or ArtifactKind.FILE.value),
                "mime_type": str(attachment.get("mime_type") or row.mime_type),
                "filename": str(attachment.get("filename") or row.object_id or row.filename),
                "size_bytes": _attachment_size_bytes(attachment, row),
            }
            try:
                public_url = public_urls.get(artifact_id)
                if public_url is None:
                    public_url = await artifact_store.async_get_public_url(
                        row.namespace,
                        row.object_id,
                        row.filename,
                    )
                    public_urls[artifact_id] = public_url
                hydrated_attachment["url"] = public_url
            except Exception:
                if (
                    not isinstance(hydrated_attachment.get("url"), str)
                    or not hydrated_attachment["url"]
                ):
                    hydrated_attachment.pop("url", None)
            hydrated.append(hydrated_attachment)
        hydrated_groups.append(hydrated)
    return hydrated_groups


def _legacy_attachment_fallback(attachment: dict[str, Any]) -> dict[str, Any] | None:
    url = attachment.get("url")
    if not isinstance(url, str) or not url:
        return None
    return attachment


def _deleted_attachment_fallback(
    attachment: dict[str, Any], row: ArtifactRecordRow | _HydrationRecord
) -> dict[str, Any] | None:
    fallback = {
        "artifact_id": attachment["artifact_id"],
        "kind": str(attachment.get("kind") or row.kind or ArtifactKind.FILE.value),
        "mime_type": str(attachment.get("mime_type") or row.mime_type),
        "filename": str(attachment.get("filename") or row.object_id or row.filename),
        "size_bytes": _attachment_size_bytes(attachment, row),
    }
    if isinstance(attachment.get("url"), str) and attachment["url"]:
        fallback["url"] = attachment["url"]
    return fallback


def attachment_note(attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        parts.append(attachment_label(attachment))
    return "Attachments: " + ", ".join(parts)


def attachment_placeholder_text(kinds: Iterable[ArtifactKind | str]) -> str:
    normalized: list[ArtifactKind] = []
    for kind in kinds:
        if isinstance(kind, ArtifactKind):
            normalized.append(kind)
            continue
        if isinstance(kind, str) and kind:
            with contextlib.suppress(ValueError):
                normalized.append(ArtifactKind(kind))
    if not normalized:
        return "User attached files."
    if len(normalized) != 1:
        return "User attached files."
    kind = normalized[0]
    # ArtifactKind.FILE has value "file" which would produce "a file file." — handle separately.
    if kind == ArtifactKind.FILE:
        return "User attached a file."
    article = "an" if kind in {ArtifactKind.AUDIO, ArtifactKind.IMAGE} else "a"
    return f"User attached {article} {kind.value} file."


def merge_content_and_attachment_note(content: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return content
    note = attachment_note([a for a in attachments if isinstance(a, dict)])
    if not content.strip():
        return note
    return f"{content}\n\n{note}"
