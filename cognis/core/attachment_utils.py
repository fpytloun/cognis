"""Helpers for assistant attachment normalization and hydration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

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


def normalize_attachment_refs(attachments: Iterable[dict[str, Any] | Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        item = normalize_attachment_ref(attachment)
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
        if not isinstance(attachment, dict):
            continue
        normalized = normalize_attachment_ref(
            {
                **{k: v for k, v in attachment.items() if k in _CANONICAL_ATTACHMENT_KEYS},
            }
        )
        if normalized is not None and {
            "kind",
            "mime_type",
            "filename",
            "size_bytes",
        }.issubset(normalized):
            safe.append(normalized)
    return safe


async def hydrate_attachment_refs(
    session: AsyncSession,
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
    result = await session.execute(
        sa.select(ArtifactRecordRow).where(ArtifactRecordRow.artifact_id.in_(artifact_ids))
    )
    rows = {row.artifact_id: row for row in result.scalars().all()}

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
            "size_bytes": int(
                attachment.get("size_bytes")
                if isinstance(attachment.get("size_bytes"), int)
                else row.size_bytes or 0
            ),
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


def _legacy_attachment_fallback(attachment: dict[str, Any]) -> dict[str, Any] | None:
    url = attachment.get("url")
    if not isinstance(url, str) or not url:
        return None
    return attachment


def _deleted_attachment_fallback(
    attachment: dict[str, Any], row: ArtifactRecordRow
) -> dict[str, Any] | None:
    fallback = {
        "artifact_id": attachment["artifact_id"],
        "kind": str(attachment.get("kind") or row.kind or ArtifactKind.FILE.value),
        "mime_type": str(attachment.get("mime_type") or row.mime_type),
        "filename": str(attachment.get("filename") or row.object_id or row.filename),
        "size_bytes": int(
            attachment.get("size_bytes")
            if isinstance(attachment.get("size_bytes"), int)
            else row.size_bytes or 0
        ),
    }
    if isinstance(attachment.get("url"), str) and attachment["url"]:
        fallback["url"] = attachment["url"]
    return fallback


def attachment_note(attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
        kind = str(attachment.get("kind") or "file")
        parts.append(f"{filename} ({kind})")
    return "Attachments: " + ", ".join(parts)


def merge_content_and_attachment_note(content: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return content
    note = attachment_note([a for a in attachments if isinstance(a, dict)])
    if not content.strip():
        return note
    return f"{content}\n\n{note}"
