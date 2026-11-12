"""Transaction-local causality for successful canonical document writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.artifact_revisions import ArtifactWriteParticipant
from cognis.core.feedback.primitives import Actor, FeedbackError, digest, encode
from cognis.core.feedback.threads import insert_absent
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.models import (
    ArtifactRecordRow,
    DeliverableRow,
    DirectTurnRequestRow,
    Session,
    StepRun,
    Task,
)

if TYPE_CHECKING:
    from cognis.core.feedback.agents import ExecutionScope, FeedbackAgents


def permits_source_revision(manifest: dto.SubmissionManifest) -> bool:
    """Keep legacy restrictions and bound request-driven revisions to one document."""
    return manifest.update_mode == "revise_source" or (
        manifest.update_mode == "follow_request"
        and len(manifest.sources) == 1
        and manifest.sources[0].source.kind == "document"
    )


def result_relation(
    manifest: dto.SubmissionManifest,
    content_ref: str,
    lineage_id: str | None,
    revision: int,
    supersedes: str | None,
) -> Literal["revises_source", "separate_result"]:
    """Classify the flushed canonical revision, never an agent's claimed relation."""
    revises = any(
        source.source.kind == "document"
        and source.source.content_ref.startswith("dlv_") == content_ref.startswith("dlv_")
        and lineage_id is not None
        and source.lineage_id == lineage_id
        and source.revision is not None
        and revision > source.revision
        for source in manifest.sources
    )
    return "revises_source" if supersedes and revises else "separate_result"


async def assert_writing_execution(db: AsyncSession, scope: ExecutionScope) -> None:
    """Serialize result acceptance with the canonical execution cancellation owner."""
    session = await db.get(Session, scope.session_id)
    if session is None or session.status != "active":
        raise FeedbackError(409, "execution_changed", "The producing execution is no longer active")
    if scope.task_id:
        task = await db.scalar(select(Task).where(Task.task_id == scope.task_id).with_for_update())
        valid = bool(
            task and task.status == "running" and task.attempt_number == scope.attempt_number
        )
        step = await db.scalar(
            select(StepRun).where(
                StepRun.session_id == scope.session_id,
                StepRun.task_id == scope.task_id,
                StepRun.attempt_number == scope.attempt_number,
                StepRun.status == "running",
            )
        )
        valid = valid and step is not None
    elif session.parent_session_id and session.delegation_mode:
        valid = True  # Secondary sessions are immutable, single-execution work.
    else:
        request = await db.scalar(
            select(DirectTurnRequestRow)
            .where(
                DirectTurnRequestRow.conversation_id == scope.conversation_id,
                DirectTurnRequestRow.turn_id == scope.turn_id,
            )
            .with_for_update()
        )
        valid = bool(
            request and request.status == "running" and request.cancel_requested_at is None
        )
    if not valid:
        raise FeedbackError(409, "execution_changed", "The producing execution is no longer active")


async def prepare_result_write(
    agents: FeedbackAgents, scope: ExecutionScope
) -> ArtifactWriteParticipant | None:
    """Load immutable payloads before SQL, then bind only the canonical writer's new row."""
    packets = await agents.scope_packets(scope)
    if not packets and scope.task_id is None:
        return None

    async def bind(db: AsyncSession, content_ref: str, created: bool) -> None:
        await assert_writing_execution(db, scope)
        if not packets:
            return
        document: DeliverableRow | ArtifactRecordRow | None
        if content_ref.startswith("dlv_"):
            document = await db.get(DeliverableRow, content_ref)
        else:
            document = await db.get(ArtifactRecordRow, content_ref)
        if document is None:
            raise FeedbackError(404, "not_found", "The canonical result is unavailable")
        for original, manifest in packets:
            current = await db.get(rows.FeedbackSubmissionRow, original.submission_id)
            if (
                current is None
                or current.manifest_hash != original.manifest_hash
                or not await agents.allowed(db, scope, current)
            ):
                raise FeedbackError(403, "forbidden", "Feedback execution authority changed")
            for source in manifest.sources:
                _, caps = await agents.sources.authorize(
                    db, Actor.user(current.sender_email), source.subject_id
                )
                if not caps.disclose:
                    raise FeedbackError(403, "forbidden", "Feedback disclosure authority changed")
            relation = result_relation(
                manifest,
                content_ref,
                document.lineage_id,
                document.revision,
                document.supersedes_deliverable_id
                if isinstance(document, DeliverableRow)
                else document.supersedes_artifact_id,
            )
            if relation == "revises_source" and not permits_source_revision(manifest):
                raise FeedbackError(
                    403,
                    "update_mode_forbidden",
                    "This packet does not authorize revising its source",
                )
            kind = "result_" + relation
            if not created:
                existing = await db.scalar(
                    select(rows.FeedbackWorkLinkRow).where(
                        rows.FeedbackWorkLinkRow.submission_id == current.submission_id,
                        rows.FeedbackWorkLinkRow.kind == kind,
                        rows.FeedbackWorkLinkRow.reference_id == content_ref,
                        rows.FeedbackWorkLinkRow.parent_reference_id == scope.session_id,
                        rows.FeedbackWorkLinkRow.turn_id == scope.turn_id,
                        rows.FeedbackWorkLinkRow.attempt_number == scope.attempt_number,
                    )
                )
                if existing is None:
                    raise FeedbackError(
                        409,
                        "result_replay_mismatch",
                        "A replay cannot claim another write's result",
                    )
                continue
            await insert_absent(
                db,
                rows.FeedbackWorkLinkRow,
                {
                    "link_id": "fbwl_" + digest(encode([current.submission_id, content_ref])),
                    "submission_id": current.submission_id,
                    "kind": kind,
                    "reference_id": content_ref,
                    "parent_reference_id": scope.session_id,
                    "turn_id": scope.turn_id,
                    "attempt_number": scope.attempt_number,
                },
                ["link_id"],
            )

    return bind
