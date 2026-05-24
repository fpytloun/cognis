"""Shared helpers for artifact-compatible virtual content references."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.artifacts.store import sanitize_artifact_filename
from cognis.store.models import DeliverableRow, StepRun, Task
from cognis.store.queries import get_deliverable, get_task

DELIVERABLE_REF_PREFIX = "dlv_"


@dataclass(frozen=True)
class DeliverableContentRef:
    """Authorized virtual artifact-compatible view of a task deliverable."""

    deliverable: DeliverableRow
    task: Task
    filename: str
    mime_type: str
    content_bytes: bytes

    @property
    def artifact_id(self) -> str:
        return self.deliverable.deliverable_id

    @property
    def size_bytes(self) -> int:
        return len(self.content_bytes)


def is_deliverable_ref(ref_id: str) -> bool:
    """Return whether a content ref names a task deliverable."""

    return ref_id.startswith(DELIVERABLE_REF_PREFIX)


def continuation_scope_task_id(runtime_metadata: dict[str, Any] | None) -> str | None:
    """Extract task/fork continuation scope from tool runtime metadata."""

    if not isinstance(runtime_metadata, dict):
        return None
    conversation_context = runtime_metadata.get("conversation_context")
    if not isinstance(conversation_context, dict):
        return None
    platform_data = conversation_context.get("platform_data")
    if not isinstance(platform_data, dict):
        return None
    if platform_data.get("forked_from") not in {"task", "task_step"}:
        return None
    task_id = platform_data.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


def deliverable_mime_type(format_name: str | None) -> str:
    """Map a deliverable format to a text MIME type."""

    return {
        "markdown": "text/markdown",
        "plain": "text/plain",
        "html": "text/html",
    }.get(str(format_name or "").lower(), "text/plain")


def deliverable_extension(format_name: str | None) -> str:
    """Map a deliverable format to a safe file extension."""

    return {
        "markdown": ".md",
        "plain": ".txt",
        "html": ".html",
    }.get(str(format_name or "").lower(), ".txt")


def deliverable_filename(deliverable: DeliverableRow) -> str:
    """Return a deterministic, safe filename for a virtual deliverable."""

    extension = deliverable_extension(deliverable.format)
    raw_title = str(deliverable.title or "").strip()
    base = raw_title or f"deliverable-{deliverable.deliverable_id}"
    base = re.sub(r"\s+", "-", base)
    filename = sanitize_artifact_filename(base, default=f"deliverable-{deliverable.deliverable_id}")
    if not filename.lower().endswith(extension):
        filename = f"{filename}{extension}"
    return filename


async def get_accessible_deliverable_ref(
    session: AsyncSession,
    deliverable_id: str,
    user_email: str | None,
    *,
    scope_task_id: str | None = None,
) -> DeliverableContentRef | None:
    """Resolve and authorize a deliverable virtual content ref.

    Returns ``None`` for missing or unauthorized deliverables so callers can preserve
    not-found semantics without leaking cross-user existence.
    """

    if not user_email or not deliverable_id:
        return None
    deliverable = await get_deliverable(session, deliverable_id)
    if deliverable is None:
        return None
    task = await task_for_step_run(session, deliverable.step_run_id)
    if (
        task is None
        or task.created_by != user_email
        or (scope_task_id is not None and task.task_id != scope_task_id)
    ):
        return None
    content_bytes = deliverable.content.encode("utf-8")
    return DeliverableContentRef(
        deliverable=deliverable,
        task=task,
        filename=deliverable_filename(deliverable),
        mime_type=deliverable_mime_type(deliverable.format),
        content_bytes=content_bytes,
    )


async def get_deliverable_ref_unscoped(
    session: AsyncSession,
    deliverable_id: str,
) -> DeliverableContentRef | None:
    """Resolve a deliverable ref for HMAC-protected virtual downloads."""

    deliverable = await get_deliverable(session, deliverable_id)
    if deliverable is None:
        return None
    task = await task_for_step_run(session, deliverable.step_run_id)
    if task is None:
        return None
    content_bytes = deliverable.content.encode("utf-8")
    return DeliverableContentRef(
        deliverable=deliverable,
        task=task,
        filename=deliverable_filename(deliverable),
        mime_type=deliverable_mime_type(deliverable.format),
        content_bytes=content_bytes,
    )


async def task_for_step_run(session: AsyncSession, step_run_id: str) -> Task | None:
    """Return the task owning a step run."""

    result = await session.execute(
        select(StepRun.task_id).where(StepRun.step_run_id == step_run_id)
    )
    task_id = result.scalar_one_or_none()
    if not isinstance(task_id, str):
        return None
    return await get_task(session, task_id)


def deliverable_metadata_item(ref: DeliverableContentRef) -> dict[str, Any]:
    """Return artifact-shaped metadata for a virtual deliverable."""

    deliverable = ref.deliverable
    task = ref.task
    return {
        "artifact_id": deliverable.deliverable_id,
        "content_ref": deliverable.deliverable_id,
        "source": "deliverable",
        "virtual": True,
        "namespace": "deliverables",
        "object_id": deliverable.deliverable_id,
        "filename": ref.filename,
        "owner_email": task.created_by,
        "conversation_id": None,
        "session_id": None,
        "message_role": None,
        "purpose": "workflow_deliverable",
        "kind": "file",
        "mime_type": ref.mime_type,
        "size_bytes": ref.size_bytes,
        "status": deliverable.status,
        "created_at": _serialize_datetime(getattr(deliverable, "created_at", None)),
        "expires_at": None,
        "deleted_at": None,
        "task_id": task.task_id,
        "step_run_id": deliverable.step_run_id,
        "deliverable_id": deliverable.deliverable_id,
        "deliverable_format": deliverable.format,
        "deliverable_version": deliverable.version,
    }


def build_deliverable_public_url(
    artifact_store: Any,
    ref: DeliverableContentRef,
    *,
    ttl_seconds: int,
) -> str:
    """Build a signed virtual deliverable download URL without storing a blob."""

    config = getattr(artifact_store, "_config", None)
    base_url = getattr(config, "base_url", "")
    signing_secret = getattr(config, "signing_secret", "")
    if not base_url or not signing_secret:
        raise ValueError("Artifact public URLs require base_url and signing_secret")
    signer = getattr(artifact_store, "_filesystem_signature", None)
    if not callable(signer):
        raise ValueError("Artifact store does not support signed virtual URLs")
    exp = int(time.time()) + ttl_seconds
    sig = signer("deliverables", ref.artifact_id, ref.filename, exp)
    path = f"/api/v1/artifacts/virtual/deliverables/{quote(ref.artifact_id)}/{quote(ref.filename)}"
    return f"{base_url}{path}?exp={exp}&sig={sig}"


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
