"""Shared authorization and hydration for explicit artifact inputs."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.models.artifact import ArtifactStatus, AttachmentRef
from cognis.store.queries import (
    get_artifact_record,
    get_conversation,
    get_managed_conversation_ancestry,
    get_managed_conversation_link_for_target,
)

MAX_EXPLICIT_ARTIFACTS = 10
MAX_OUTBOUND_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES = 50 * 1024 * 1024
OUTBOUND_ARTIFACT_GRANT_KEY = "_delivery_authorization"
_OUTBOUND_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/zip",
        "audio/aac",
        "audio/flac",
        "audio/m4a",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
        "video/mp4",
        "video/webm",
    }
)


async def resolve_owned_artifact_refs(
    session_factory: Any,
    artifact_ids: object,
    *,
    user_email: str,
    conversation_id: str,
    agent_id: str | None = None,
) -> list[AttachmentRef]:
    """Resolve ordered persisted artifacts owned by the current user."""

    if artifact_ids is None:
        return []
    if (
        not isinstance(artifact_ids, Sequence)
        or isinstance(artifact_ids, (str, bytes))
        or len(artifact_ids) > MAX_EXPLICIT_ARTIFACTS
    ):
        raise ValueError(f"artifact_ids must contain at most {MAX_EXPLICIT_ARTIFACTS} IDs")
    refs: list[AttachmentRef] = []
    async with session_factory() as session:
        for raw_id in artifact_ids:
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError("artifact_ids must contain persisted Cognis artifact IDs")
            artifact_id = raw_id.strip()
            record = await get_artifact_record(session, artifact_id)
            expires_at = getattr(record, "expires_at", None)
            authorized = await artifact_authorized_for_conversation(
                session,
                artifact=record,
                owner_email=user_email,
                conversation_id=conversation_id,
                agent_id=agent_id,
            )
            if (
                record is None
                or record.artifact_id != artifact_id
                or not authorized
                or record.status == ArtifactStatus.DELETED
                or getattr(record, "deleted_at", None) is not None
                or (
                    expires_at is not None
                    and (
                        expires_at.replace(tzinfo=UTC)
                        if expires_at.tzinfo is None
                        else expires_at.astimezone(UTC)
                    )
                    <= datetime.now(UTC)
                )
                or not record.mime_type
                or not record.filename
                or record.size_bytes < 0
            ):
                raise ValueError(f"Artifact is unavailable: {artifact_id}")
            refs.append(
                AttachmentRef(
                    artifact_id=artifact_id,
                    kind=record.kind,
                    mime_type=record.mime_type,
                    filename=record.filename,
                    size_bytes=record.size_bytes,
                )
            )
    return refs


async def authorize_outbound_artifact_refs(
    session_factory: Any,
    refs: Sequence[AttachmentRef | dict[str, Any]],
    *,
    user_email: str,
    conversation_id: str,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Persist the exact artifact grant used to authorize an outbound send."""

    async with session_factory() as session:
        return await authorize_outbound_artifact_refs_in_session(
            session,
            refs,
            user_email=user_email,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )


async def authorize_outbound_artifact_refs_in_session(
    session: Any,
    refs: Sequence[AttachmentRef | dict[str, Any]],
    *,
    user_email: str,
    conversation_id: str,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    if len(refs) > MAX_EXPLICIT_ARTIFACTS:
        raise ValueError(f"At most {MAX_EXPLICIT_ARTIFACTS} artifacts can be sent")
    if not refs:
        return []
    total_size = 0
    authorized: list[dict[str, Any]] = []
    accessor = await get_conversation(session, conversation_id)
    if accessor is None or accessor.user_email != user_email:
        raise ValueError("Artifact authorization conversation is unavailable")
    resolved_agent_id = agent_id or accessor.agent_id
    for value in refs:
        if isinstance(value, dict) and not value.get("artifact_id"):
            mime_type = str(value.get("mime_type") or "").casefold()
            content_b64 = value.get("content_b64")
            if mime_type not in _OUTBOUND_MIME_TYPES or not isinstance(content_b64, str):
                raise ValueError("Unsupported inline outbound attachment")
            try:
                content = base64.b64decode(content_b64, validate=True)
            except Exception as exc:
                raise ValueError("Inline outbound attachment is not valid base64") from exc
            if len(content) > MAX_OUTBOUND_ARTIFACT_BYTES:
                raise ValueError(f"Outbound artifact exceeds {MAX_OUTBOUND_ARTIFACT_BYTES} bytes")
            total_size += len(content)
            if total_size > MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES:
                raise ValueError(
                    f"Outbound artifacts exceed {MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES} bytes"
                )
            authorized.append({**value, "size_bytes": len(content), "mime_type": mime_type})
            continue
        ref = value if isinstance(value, AttachmentRef) else AttachmentRef.model_validate(value)
        record = await get_artifact_record(session, ref.artifact_id)
        if record is None:
            raise ValueError(f"Artifact is unavailable: {ref.artifact_id}")
        if not await artifact_authorized_for_conversation(
            session,
            artifact=record,
            owner_email=user_email,
            conversation_id=conversation_id,
            agent_id=resolved_agent_id,
        ):
            raise ValueError(f"Artifact is unavailable: {ref.artifact_id}")
        _validate_outbound_record(record, ref)
        total_size += record.size_bytes
        if total_size > MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES:
            raise ValueError(f"Outbound artifacts exceed {MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES} bytes")
        grant: dict[str, Any] = {
            "version": 1,
            "owner_email": user_email,
            "accessor_conversation_id": conversation_id,
            "accessor_agent_id": resolved_agent_id,
            "source_conversation_id": record.conversation_id,
        }
        if record.conversation_id is None:
            grant["scope"] = "owner_global"
        elif record.conversation_id == conversation_id:
            grant["scope"] = "conversation"
        else:
            link = await get_managed_conversation_link_for_target(
                session,
                target_conversation_id=record.conversation_id,
            )
            scope = None
            if link is not None and link.depth <= 2:
                try:
                    ancestry = await get_managed_conversation_ancestry(
                        session,
                        link,
                        user_email=user_email,
                    )
                except ValueError:
                    ancestry = []
                if any(
                    item.controller_conversation_id == conversation_id
                    and item.controller_agent_id == resolved_agent_id
                    for item in ancestry
                ):
                    scope = "descendant"
            if scope is None:
                link = await get_managed_conversation_link_for_target(
                    session,
                    target_conversation_id=conversation_id,
                )
                scope = "ancestor"
            if link is None:
                raise ValueError(f"Artifact is unavailable: {ref.artifact_id}")
            grant.update(
                {
                    "scope": scope,
                    "descendant_link_id": link.link_id,
                    "descendant_owner_epoch": link.owner_epoch,
                }
            )
        payload = safe_attachment_metadata([ref])[0]
        payload[OUTBOUND_ARTIFACT_GRANT_KEY] = grant
        authorized.append(payload)
    return authorized


async def outbound_artifact_grant_is_valid(
    session: Any,
    *,
    attachment: dict[str, Any],
    artifact: Any | None,
    owner_email: str,
) -> bool:
    """Revalidate an attachment against the exact persisted authorization grant."""

    grant = attachment.get(OUTBOUND_ARTIFACT_GRANT_KEY)
    if not isinstance(grant, dict) or grant.get("version") != 1 or artifact is None:
        return False
    if grant.get("owner_email") != owner_email or artifact.owner_email != owner_email:
        return False
    if artifact.status == ArtifactStatus.DELETED or artifact.deleted_at is not None:
        return False
    expires_at = getattr(artifact, "expires_at", None)
    if expires_at is not None:
        normalized_expiry = (
            expires_at.replace(tzinfo=UTC)
            if expires_at.tzinfo is None
            else expires_at.astimezone(UTC)
        )
        if normalized_expiry <= datetime.now(UTC):
            return False
    if artifact.conversation_id != grant.get("source_conversation_id"):
        return False
    try:
        _validate_outbound_record(artifact, AttachmentRef.model_validate(attachment))
    except ValueError:
        return False
    scope = grant.get("scope")
    if scope == "owner_global":
        return artifact.conversation_id is None
    accessor_id = grant.get("accessor_conversation_id")
    if not isinstance(accessor_id, str):
        return False
    accessor = await get_conversation(session, accessor_id)
    if (
        accessor is None
        or accessor.user_email != owner_email
        or accessor.agent_id != grant.get("accessor_agent_id")
    ):
        return False
    if scope == "conversation":
        return artifact.conversation_id == accessor_id
    if scope not in {"ancestor", "descendant"}:
        return False
    target_conversation_id = accessor_id if scope == "ancestor" else artifact.conversation_id
    link = await get_managed_conversation_link_for_target(
        session,
        target_conversation_id=target_conversation_id,
    )
    return bool(
        link is not None
        and link.link_id == grant.get("descendant_link_id")
        and link.owner_epoch == grant.get("descendant_owner_epoch")
        and link.conversation_state == "open"
        and await artifact_authorized_for_conversation(
            session,
            artifact=artifact,
            owner_email=owner_email,
            conversation_id=accessor_id,
            agent_id=grant.get("accessor_agent_id"),
        )
    )


def validate_outbound_attachment_batch(
    attachments: Sequence[dict[str, Any]],
) -> None:
    if len(attachments) > MAX_EXPLICIT_ARTIFACTS:
        raise ValueError(f"At most {MAX_EXPLICIT_ARTIFACTS} artifacts can be sent")
    total = 0
    for item in attachments:
        mime_type = str(item.get("mime_type") or "").casefold()
        size_bytes = item.get("size_bytes")
        if mime_type not in _OUTBOUND_MIME_TYPES:
            raise ValueError(f"Unsupported outbound artifact MIME type: {mime_type or 'missing'}")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("Outbound artifact size metadata is invalid")
        if size_bytes > MAX_OUTBOUND_ARTIFACT_BYTES:
            raise ValueError(f"Outbound artifact exceeds {MAX_OUTBOUND_ARTIFACT_BYTES} bytes")
        total += size_bytes
        if total > MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES:
            raise ValueError(f"Outbound artifacts exceed {MAX_OUTBOUND_ARTIFACT_TOTAL_BYTES} bytes")


def _validate_outbound_record(record: Any, ref: AttachmentRef) -> None:
    if record.mime_type.casefold() not in _OUTBOUND_MIME_TYPES:
        raise ValueError(f"Unsupported outbound artifact MIME type: {record.mime_type}")
    if record.size_bytes > MAX_OUTBOUND_ARTIFACT_BYTES:
        raise ValueError(f"Outbound artifact exceeds {MAX_OUTBOUND_ARTIFACT_BYTES} bytes")
    if (
        ref.mime_type.casefold() != record.mime_type.casefold()
        or ref.size_bytes != record.size_bytes
        or ref.filename != record.filename
    ):
        raise ValueError(f"Artifact metadata changed: {ref.artifact_id}")


def safe_attachment_metadata(value: object) -> list[dict[str, object]]:
    """Return metadata that cannot expose bytes, local paths, or provider URLs."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        try:
            ref = item if isinstance(item, AttachmentRef) else AttachmentRef.model_validate(item)
        except Exception:
            continue
        result.append(
            {
                "artifact_id": ref.artifact_id,
                "kind": ref.kind.value,
                "mime_type": ref.mime_type,
                "filename": ref.filename,
                "size_bytes": ref.size_bytes,
            }
        )
    return result
