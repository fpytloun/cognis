"""Write and resolve revisioned artifact documents.

Single owner of artifact document writes (``artifact_write`` and
``artifact_publish``) and of exact or latest content resolution. It reuses the
deliverable revision model: every revision is its own immutable artifact, the
first revision's ID is the document's stable ``lineage_id``, and a write that
supersedes anything but the live head is rejected with the head's ID.

Transaction and side-effect contract for a write:

1. Validate content, authorize the target, and resolve the project in a
   short transaction that is rolled back (no mutation).
2. Store the bytes under a fresh ``art_`` ID (external I/O, no DB session).
3. In one transaction, lock the target, re-check that it is still the head,
   insert the revision row, and commit. The insert *is* the head advance.
4. On any failure after step 2, the stored object is deleted, so a rejected
   write leaves no row and no payload.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.artifacts.json_validation import JSONStreamValidator
from cognis.artifacts.json_validation import is_json_mime_type as is_json_mime_type
from cognis.core.content_projects import (
    human_may_access_project_content,
    lock_document_scope,
    resolve_new_content_project_id,
)
from cognis.models.deliverable import ArtifactRevisionError
from cognis.store.models import ArtifactRecordRow, BinaryTransferRow
from cognis.store.queries import (
    complete_artifact_revision_conflict,
    create_artifact_record,
    get_artifact_lineage_head,
    get_artifact_lineage_summary,
    get_artifact_record,
    lock_artifact_revision_target,
)

logger = logging.getLogger(__name__)

ARTIFACT_DOCUMENT_NAMESPACE = "artifacts"
ArtifactWriteParticipant = Callable[[AsyncSession, str, bool], Awaitable[None]]
DocumentWriteFence = Callable[[AsyncSession], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ContentActor:
    """Server-owned identity of the agent session reading or writing content."""

    user_email: str
    agent_id: str | None
    conversation_id: str | None
    session_id: str | None
    human: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactRevisionView:
    """Revision facts of one artifact, as returned to agents and the API."""

    artifact_id: str
    lineage_id: str
    revision: int
    supersedes_artifact_id: str | None
    latest_artifact_id: str
    latest_revision: int
    revision_count: int
    is_latest: bool
    project_id: str | None
    author_email: str | None
    author_agent_id: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON shape of the revision facts."""

        return {
            "artifact_id": self.artifact_id,
            "lineage_id": self.lineage_id,
            "revision": self.revision,
            "supersedes_artifact_id": self.supersedes_artifact_id,
            "latest_artifact_id": self.latest_artifact_id,
            "latest_revision": self.latest_revision,
            "revision_count": self.revision_count,
            "is_latest": self.is_latest,
            "project_id": self.project_id,
            "author_email": self.author_email,
            "author_agent_id": self.author_agent_id,
        }


def validate_document_content(content: bytes, mime_type: str) -> None:
    """Reject JSON documents that do not parse; other content is stored as given."""

    if not is_json_mime_type(mime_type):
        return
    try:
        validator = JSONStreamValidator()
        for offset in range(0, len(content), 1024 * 1024):
            validator.feed(content[offset : offset + 1024 * 1024])
        validator.finish()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactRevisionError(
            "invalid_json",
            f"content is not valid JSON: {str(exc)[:200]}",
            path="$.content",
        ) from exc


async def revision_view(session: AsyncSession, row: ArtifactRecordRow) -> ArtifactRevisionView:
    """Return lineage facts for one artifact row."""

    summary = await get_artifact_lineage_summary(session, row)
    return ArtifactRevisionView(
        artifact_id=row.artifact_id,
        lineage_id=summary.lineage_id,
        revision=int(row.revision or 1),
        supersedes_artifact_id=row.supersedes_artifact_id,
        latest_artifact_id=summary.latest_artifact_id,
        latest_revision=summary.latest_revision,
        revision_count=summary.revision_count,
        is_latest=summary.latest_artifact_id == row.artifact_id,
        project_id=row.project_id,
        author_email=row.author_email,
        author_agent_id=row.author_agent_id,
    )


async def authorize_artifact_revision_target(
    session: AsyncSession, actor: ContentActor, artifact_id: str
) -> ArtifactRecordRow:
    """Return a target the actor may revise, or raise ``supersedes_not_found``.

    Agent access uses the current document write permissions. Human authors
    must own the document and retain project write access. Deliverable records
    are revised through ``write_deliverable``.
    """

    target = await get_artifact_record(session, artifact_id)
    not_found = ArtifactRevisionError(
        "supersedes_not_found",
        "The artifact to supersede was not found or is not accessible.",
    )
    if target is None or target.purpose == "conversation_deliverable":
        raise not_found
    if actor.human:
        if target.owner_email != actor.user_email:
            raise not_found
        if target.project_id and not await human_may_access_project_content(
            session, project_id=target.project_id, user_email=actor.user_email, access="write"
        ):
            raise not_found
        return target
    import sqlalchemy as sa

    from cognis.core.artifact_access import content_read_predicate

    predicate = await content_read_predicate(
        session,
        owner_email=actor.user_email,
        agent_id=actor.agent_id,
        conversation_id=actor.conversation_id,
        session_id=actor.session_id,
        access="write",
    )
    if not await session.scalar(
        sa.select(sa.literal(True)).where(ArtifactRecordRow.artifact_id == artifact_id, predicate)
    ):
        raise not_found
    return target


async def _write_project(
    session: AsyncSession,
    actor: ContentActor,
    project_id: str | None,
    target: ArtifactRecordRow | None,
) -> str | None:
    if target is not None:
        if project_id is not None and project_id != target.project_id:
            raise ArtifactRevisionError(
                "project_not_writable", "A revision keeps its document's project."
            )
        # Target authorization already admitted project access or an explicit
        # lineage grant. A grant must not require project assignment a second time.
        return target.project_id
    if not actor.human:
        return await resolve_new_content_project_id(
            session,
            user_email=actor.user_email,
            agent_id=actor.agent_id,
            conversation_id=actor.conversation_id,
            session_id=actor.session_id,
            requested_project_id=project_id,
            inherited_project_id=target.project_id if target else None,
            inherit=target is not None,
        )
    resolved = target.project_id if target else project_id
    if target and project_id and project_id != resolved:
        raise ArtifactRevisionError(
            "project_not_writable", "A revision keeps its document's project."
        )
    if resolved and not await human_may_access_project_content(
        session, project_id=resolved, user_email=actor.user_email, access="write"
    ):
        raise ArtifactRevisionError("project_not_writable", "Project is not writable.")
    return resolved


async def write_artifact_document(
    *,
    session_factory: Any,
    artifact_store: Any,
    actor: ContentActor,
    content: bytes | None = None,
    filename: str,
    mime_type: str,
    kind: str,
    purpose: str,
    supersedes: str | None = None,
    project_id: str | None = None,
    agent_ids: list[str] | None = None,
    namespace: str = ARTIFACT_DOCUMENT_NAMESPACE,
    staged: BinaryTransferRow | None = None,
    source_tool_call_id: str | None = None,
    transaction_participant: ArtifactWriteParticipant | None = None,
) -> tuple[ArtifactRecordRow, ArtifactRevisionView]:
    """Persist a new artifact document or the next revision of one."""

    if staged is None:
        if content is None:
            raise ValueError("Artifact content is required")
        validate_document_content(content, mime_type)
    else:
        filename, mime_type, namespace = staged.filename, staged.mime_type, staged.namespace
    if staged is None:
        # Inline storage needs an early authorization check before writing bytes.
        # Native bytes are already staged: their transaction must reconcile the
        # receipt before applying head checks that a previous acceptance changed.
        async with session_factory() as session:
            target = None
            if supersedes is not None:
                target = await authorize_artifact_revision_target(session, actor, supersedes)
                await lock_artifact_revision_target(session, target.artifact_id)
                if (
                    agent_ids is not None
                    and not actor.human
                    and target.owner_agent_id != actor.agent_id
                ):
                    raise ArtifactRevisionError(
                        "sharing_not_allowed", "Only the owner may change sharing."
                    )
            await _write_project(session, actor, project_id, target)
            if agent_ids is not None:
                from cognis.core.document_sharing import validate_recipients

                await validate_recipients(session, actor.user_email, agent_ids)
            await session.rollback()

    artifact_id = staged.artifact_id if staged is not None else artifact_store.generate_id("art")
    size_bytes = staged.size_bytes if staged is not None else len(content or b"")
    content_hash = (
        staged.sha256 if staged is not None else hashlib.sha256(content or b"").hexdigest()
    )
    if staged is None:
        assert content is not None
        await artifact_store.async_save(
            namespace,
            artifact_id,
            filename,
            content,
            mime_type,
            owner_email=actor.user_email,
        )
    try:
        async with session_factory() as session:
            try:
                # Commit-time decision: lock the acting chat, then the target,
                # and re-resolve access and project under those locks so a
                # concurrent sharing change cannot leave this revision behind.
                await lock_document_scope(session, actor.conversation_id)
                if staged is not None:
                    from cognis.core.binary_transfers import lock_transfer_publication

                    upload = await lock_transfer_publication(
                        session, staged, owner_email=actor.user_email, session_id=actor.session_id
                    )
                    if upload.state == "consumed":
                        previous = await get_artifact_record(session, staged.artifact_id)
                        if previous is None:
                            raise ArtifactRevisionError(
                                "invalid_transfer", "Published transfer is unavailable."
                            )
                        if transaction_participant is not None:
                            await transaction_participant(session, previous.artifact_id, False)
                        return previous, await revision_view(session, previous)
                    upload.state = "consumed"
                # Revalidate access before returning head-conflict details.
                target = None
                if supersedes:
                    from cognis.store.document_locks import lock_document_lineages

                    preliminary = await get_artifact_record(session, supersedes)
                    if preliminary is not None:
                        await lock_document_lineages(
                            session, ArtifactRecordRow, [preliminary.lineage_id or supersedes]
                        )
                    target = await authorize_artifact_revision_target(session, actor, supersedes)
                lineage = (
                    await lock_artifact_revision_target(session, supersedes)
                    if supersedes is not None
                    else None
                )
                document_project_id = await _write_project(session, actor, project_id, target)
                row = await create_artifact_record(
                    session,
                    artifact_id=artifact_id,
                    namespace=namespace,
                    object_id=artifact_id,
                    filename=filename,
                    owner_email=target.owner_email if target else actor.user_email,
                    owner_agent_id=target.owner_agent_id if target else actor.agent_id,
                    author_email=actor.user_email,
                    author_agent_id=None if actor.human else actor.agent_id,
                    purpose=purpose,
                    kind=kind,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    status="attached",
                    expires_at=None,
                    content_hash=content_hash,
                    conversation_id=target.conversation_id
                    if actor.human and target
                    else actor.conversation_id,
                    session_id=target.session_id if actor.human and target else actor.session_id,
                    message_role="user" if actor.human else "assistant",
                    lineage_id=lineage.lineage_id if lineage is not None else None,
                    revision=lineage.revision if lineage is not None else 1,
                    supersedes_artifact_id=(
                        lineage.supersedes_artifact_id if lineage is not None else None
                    ),
                    project_id=document_project_id,
                    source_tool_call_id=source_tool_call_id,
                )
                if agent_ids is not None:
                    from cognis.core.document_sharing import replace_explicit_shares

                    if target and not actor.human and target.owner_agent_id != actor.agent_id:
                        raise ArtifactRevisionError(
                            "sharing_not_allowed", "Only the owner may change sharing."
                        )
                    await replace_explicit_shares(session, row, actor.user_email, agent_ids)
                view = await revision_view(session, row)
                if transaction_participant is not None:
                    await transaction_participant(session, row.artifact_id, True)
                await session.commit()
            except ArtifactRevisionError as exc:
                raise await complete_artifact_revision_conflict(session, exc) from None
    except BaseException:
        if staged is not None:
            # The transfer lifecycle owns unconsumed cleanup. Deleting here can
            # race a recovered publication or a lost commit acknowledgement.
            raise
        try:
            await artifact_store.async_delete_object(namespace, artifact_id)
        except Exception:
            logger.warning(
                "artifact revision cleanup failed",
                extra={"extra_data": {"artifact_id": artifact_id}},
                exc_info=True,
            )
        raise
    return row, view


async def resolve_artifact_reference(
    session: AsyncSession,
    artifact_id: str,
    *,
    latest: bool,
) -> ArtifactRecordRow | None:
    """Return the exact artifact, or with ``latest`` the live head of its lineage.

    Authorization is the caller's: it must authorize the returned row, never
    only the reference it started from.
    """

    row = await get_artifact_record(session, artifact_id)
    if row is None or not latest:
        return row
    return await get_artifact_lineage_head(session, row.lineage_id or row.artifact_id)
