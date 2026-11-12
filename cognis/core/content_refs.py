"""Shared helpers for artifact-compatible virtual content references."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.artifacts.store import sanitize_artifact_filename
from cognis.core.content_projects import ContentAccess, content_project_ids
from cognis.models.deliverable import DeliverableRevisionError
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import AuditLog, DeliverableRow, StepRun, Task
from cognis.store.queries import (
    get_deliverable,
    get_managed_conversation_ancestry,
    get_managed_conversation_link_for_target,
    get_task,
)

DELIVERABLE_REF_PREFIX = "dlv_"


@dataclass(frozen=True)
class DeliverableContentRef:
    """Authorized virtual artifact-compatible view of a deliverable."""

    deliverable: DeliverableRow
    owner_email: str
    creator_agent_id: str | None
    task: Task | None
    filename: str
    mime_type: str
    content_bytes: bytes
    access_audit_details: dict[str, Any] | None = None

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
    if (
        platform_data.get("forked_from") not in {"task", "task_step"}
        and platform_data.get("kind") != "task_control"
    ):
        return None
    task_id = platform_data.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


def deliverable_mime_type(format_name: str | None) -> str:
    """Map a deliverable format to the bytes served by virtual artifact refs.

    Rich deliverable virtual artifacts intentionally serve the required fallback
    ``content`` bytes for channel/export compatibility. The structured rich
    payload remains available in artifact metadata and task/API retrieval
    surfaces, so the virtual artifact MIME/extension must describe the fallback
    text bytes rather than the rich JSON payload.
    """

    return {
        "markdown": "text/markdown",
        "plain": "text/plain",
        "html": "text/html",
        "rich": "text/markdown",
    }.get(str(format_name or "").lower(), "text/plain")


def deliverable_extension(format_name: str | None) -> str:
    """Map a deliverable format to a safe extension for served bytes."""

    return {
        "markdown": ".md",
        "plain": ".txt",
        "html": ".html",
        "rich": ".md",
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


@dataclass(frozen=True)
class DeliverableAccess:
    """Authorization facts for one deliverable and accessor."""

    owner_email: str
    creator_agent_id: str | None
    task: Task | None
    access_audit_details: dict[str, Any] | None = None


async def authorize_deliverable_access(
    session: AsyncSession,
    deliverable: DeliverableRow,
    user_email: str | None,
    *,
    scope_task_id: str | None = None,
    accessor_conversation_id: str | None = None,
    accessor_agent_id: str | None = None,
    accessor_session_id: str | None = None,
) -> DeliverableAccess | None:
    """Authorize deliverable access without loading its payload.

    This is the single access rule for reading a ``dlv_`` reference and for
    choosing it as a ``supersedes`` revision target.
    """

    from cognis.core.artifact_access import readable_deliverable_ids

    if not user_email:
        return None
    if deliverable.owner_email != user_email:
        # Explicit document sharing is authoritative. Task/chat publication can
        # supply read-only provenance for outputs without a document project.
        task = (
            await task_for_step_run(session, deliverable.step_run_id)
            if deliverable.step_run_id is not None
            else None
        )
        project_id = deliverable.project_id
        if project_id is None:
            from cognis.core.project_access import can_read_conversation, can_read_task
            from cognis.store.queries import get_conversation

            if deliverable.step_run_id is not None:
                if task is None or not await can_read_task(session, task, user_email):
                    return None
                project_id = task.project_id
            elif deliverable.conversation_id is not None:
                conversation = await get_conversation(session, deliverable.conversation_id)
                if conversation is None or not await can_read_conversation(
                    session, conversation, user_email
                ):
                    return None
                project_id = conversation.project_id
        if not await _project_document_accessible(
            session,
            project_id,
            user_email=user_email,
            agent_id=accessor_agent_id,
            conversation_id=accessor_conversation_id,
            session_id=accessor_session_id,
            access="read",
        ):
            return None
        if scope_task_id is not None and (task is None or task.task_id != scope_task_id):
            return None
        return DeliverableAccess(
            owner_email=str(deliverable.owner_email or ""),
            creator_agent_id=deliverable.owner_agent_id,
            task=task,
        )
    if accessor_agent_id or accessor_conversation_id:
        allowed = await readable_deliverable_ids(
            session,
            [deliverable.deliverable_id],
            owner_email=user_email,
            agent_id=accessor_agent_id,
            conversation_id=accessor_conversation_id,
            session_id=accessor_session_id,
        )
        if deliverable.deliverable_id not in allowed:
            return None
    task = (
        await task_for_step_run(session, deliverable.step_run_id)
        if deliverable.step_run_id is not None
        else None
    )
    if scope_task_id is not None and (task is None or task.task_id != scope_task_id):
        return None
    audit = None
    if deliverable.conversation_id and accessor_conversation_id and accessor_agent_id:
        link = await get_managed_conversation_link_for_target(
            session, deliverable.conversation_id, user_email=user_email
        )
        if link is not None:
            try:
                ancestry = await get_managed_conversation_ancestry(
                    session, link, user_email=user_email
                )
            except ValueError:
                ancestry = []
            controlling = next(
                (
                    item
                    for item in ancestry
                    if item.controller_conversation_id == accessor_conversation_id
                    and item.controller_agent_id == accessor_agent_id
                ),
                None,
            )
            if controlling is not None:
                audit = {
                    "deliverable_id": deliverable.deliverable_id,
                    "creator_agent_id": deliverable.owner_agent_id,
                    "creator_conversation_id": deliverable.conversation_id,
                    "creator_control_link_id": link.link_id,
                    "owner_email": user_email,
                    "accessor_agent_id": accessor_agent_id,
                    "accessor_conversation_id": accessor_conversation_id,
                    "control_link_id": controlling.link_id,
                    "managed_descendant_depth": link.depth - controlling.depth + 1,
                }
    return DeliverableAccess(
        owner_email=user_email,
        creator_agent_id=deliverable.owner_agent_id,
        task=task,
        access_audit_details=audit,
    )


async def _project_document_accessible(
    session: AsyncSession,
    project_id: str | None,
    *,
    user_email: str,
    agent_id: str | None,
    conversation_id: str | None,
    session_id: str | None,
    access: ContentAccess,
) -> bool:
    if not project_id or not agent_id:
        return False
    return project_id in await content_project_ids(
        session,
        user_email=user_email,
        agent_id=agent_id,
        conversation_id=conversation_id,
        session_id=session_id,
        access=access,
    )


async def authorize_supersedes_target(
    session: AsyncSession,
    deliverable_id: str,
    *,
    user_email: str | None,
    accessor_conversation_id: str | None,
    accessor_agent_id: str | None,
    accessor_session_id: str | None = None,
    lock: bool = False,
) -> DeliverableRow:
    """Return a revision target the accessor may revise, or raise a correctable error.

    A private document is revised only by the agent that owns it. A project
    document is revised by any agent session with write access to its project.
    With ``lock`` the target row is locked first, so the decision holds until
    the caller's transaction commits.
    """

    if lock:
        from cognis.store.document_locks import lock_document_lineages

        root = await session.scalar(
            select(DeliverableRow.lineage_id).where(DeliverableRow.deliverable_id == deliverable_id)
        )
        await lock_document_lineages(session, DeliverableRow, [root or deliverable_id])
        deliverable = await session.scalar(
            select(DeliverableRow)
            .where(DeliverableRow.deliverable_id == deliverable_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        deliverable = await get_deliverable(session, deliverable_id)
    from cognis.core.artifact_access import content_read_predicate

    predicate = await content_read_predicate(
        session,
        owner_email=user_email,
        agent_id=accessor_agent_id,
        conversation_id=accessor_conversation_id,
        session_id=accessor_session_id,
        model=DeliverableRow,
        access="write",
    )
    if deliverable is None or not await session.scalar(
        select(DeliverableRow.deliverable_id).where(
            DeliverableRow.deliverable_id == deliverable_id, predicate
        )
    ):
        raise DeliverableRevisionError(
            "supersedes_not_found",
            "The deliverable to supersede was not found or is not accessible.",
        )
    return deliverable


async def get_accessible_deliverable_ref(
    session: AsyncSession,
    artifact_store: Any,
    deliverable_id: str,
    user_email: str | None,
    *,
    scope_task_id: str | None = None,
    accessor_conversation_id: str | None = None,
    accessor_agent_id: str | None = None,
    accessor_session_id: str | None = None,
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
    access = await authorize_deliverable_access(
        session,
        deliverable,
        user_email,
        scope_task_id=scope_task_id,
        accessor_conversation_id=accessor_conversation_id,
        accessor_agent_id=accessor_agent_id,
        accessor_session_id=accessor_session_id,
    )
    if access is None:
        return None
    try:
        await hydrate_deliverable_payload(deliverable, artifact_store)
    except FileNotFoundError:
        return None
    content_bytes = deliverable.content.encode("utf-8")
    return DeliverableContentRef(
        deliverable=deliverable,
        owner_email=access.owner_email,
        creator_agent_id=access.creator_agent_id,
        task=access.task,
        filename=deliverable_filename(deliverable),
        mime_type=deliverable_mime_type(deliverable.format),
        content_bytes=content_bytes,
        access_audit_details=access.access_audit_details,
    )


async def get_deliverable_ref_unscoped(
    session: AsyncSession,
    artifact_store: Any,
    deliverable_id: str,
) -> DeliverableContentRef | None:
    """Resolve a deliverable ref for HMAC-protected virtual downloads."""

    deliverable = await get_deliverable(session, deliverable_id)
    if deliverable is None:
        return None
    task: Task | None = None
    owner_email = deliverable.owner_email
    creator_agent_id = deliverable.owner_agent_id
    if not owner_email:
        return None
    if deliverable.step_run_id is not None:
        task = await task_for_step_run(session, deliverable.step_run_id)
    try:
        await hydrate_deliverable_payload(deliverable, artifact_store)
    except FileNotFoundError:
        return None
    content_bytes = deliverable.content.encode("utf-8")
    return DeliverableContentRef(
        deliverable=deliverable,
        owner_email=owner_email,
        creator_agent_id=creator_agent_id,
        task=task,
        filename=deliverable_filename(deliverable),
        mime_type=deliverable_mime_type(deliverable.format),
        content_bytes=content_bytes,
    )


async def record_deliverable_access(session_factory: Any, ref: DeliverableContentRef) -> None:
    """Persist managed deliverable access attribution in an isolated transaction."""

    details = ref.access_audit_details
    if details is None:
        return
    async with session_factory() as audit_session:
        audit_session.add(
            AuditLog(
                log_id=f"audit_{uuid.uuid4().hex[:12]}",
                event_type="managed_deliverable_access",
                user_email=ref.owner_email,
                agent_id=str(details["accessor_agent_id"]),
                details=details,
            )
        )
        await audit_session.commit()


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
    return {
        "artifact_id": deliverable.deliverable_id,
        "content_ref": deliverable.deliverable_id,
        "source": "deliverable",
        "virtual": True,
        "namespace": "deliverables",
        "object_id": deliverable.deliverable_id,
        "filename": ref.filename,
        "owner_email": ref.owner_email,
        "conversation_id": deliverable.conversation_id,
        "session_id": deliverable.session_id,
        "message_role": None,
        "purpose": "workflow_deliverable" if ref.task else "conversation_deliverable",
        "kind": "file",
        "mime_type": ref.mime_type,
        "size_bytes": ref.size_bytes,
        "status": deliverable.status,
        "created_at": _serialize_datetime(getattr(deliverable, "created_at", None)),
        "expires_at": None,
        "deleted_at": None,
        "task_id": ref.task.task_id if ref.task else None,
        "step_run_id": deliverable.step_run_id,
        "deliverable_id": deliverable.deliverable_id,
        "deliverable_format": deliverable.format,
        "deliverable_version": deliverable.version,
        "creator_agent_id": ref.creator_agent_id,
        "rich_payload": getattr(deliverable, "rich_payload", None),
        "render_metadata": getattr(deliverable, "render_metadata", None),
        "export_metadata": getattr(deliverable, "export_metadata", None),
    }


def build_deliverable_public_url(
    artifact_store: Any,
    ref: DeliverableContentRef,
    *,
    ttl_seconds: int,
    mode: str = "download",
) -> str:
    """Build a signed virtual deliverable download URL without storing a blob."""

    if mode not in {"download", "view"}:
        raise ValueError(f"Unsupported artifact URL mode: {mode}")
    config = getattr(artifact_store, "_config", None)
    base_url = getattr(config, "base_url", "")
    signing_secret = getattr(config, "signing_secret", "")
    if not base_url or not signing_secret:
        raise ValueError("Artifact public URLs require base_url and signing_secret")
    signer = getattr(artifact_store, "_filesystem_signature", None)
    if not callable(signer):
        raise ValueError("Artifact store does not support signed virtual URLs")
    exp = int(time.time()) + ttl_seconds
    sig = signer("deliverables", ref.artifact_id, ref.filename, exp, mode=mode)
    route = "virtual/deliverables" if mode == "download" else "virtual/deliverables/view"
    path = f"/api/v1/artifacts/{route}/{quote(ref.artifact_id)}/{quote(ref.filename)}"
    return f"{base_url}{path}?exp={exp}&sig={sig}"


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
