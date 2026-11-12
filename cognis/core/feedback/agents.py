"""Execution-scoped packet reads, result provenance, and explicit child grants."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.feedback.authoring import permits_source_revision, result_relation
from cognis.core.feedback.lifecycle import child_work
from cognis.core.feedback.primitives import (
    Actor,
    FeedbackError,
    Payloads,
    digest,
    encode,
)
from cognis.core.feedback.sources import Sources, utf16_length, utf16_slice
from cognis.core.feedback.threads import Threads, insert_absent
from cognis.models import feedback as dto
from cognis.models.feedback_tools import (
    CommentReplyArguments,
    FeedbackReadArguments,
    FeedbackRecordArguments,
)
from cognis.models.tool import ToolCall, ToolResult
from cognis.store import feedback_models as rows
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    DeliverableRow,
    DirectTurnRequestRow,
    Session,
    StepRun,
    Task,
)


@dataclass(frozen=True)
class ExecutionScope:
    session_id: str
    conversation_id: str | None
    turn_id: str | None
    agent_id: str
    user_email: str
    task_id: str | None = None
    attempt_number: int | None = None


@dataclass(frozen=True)
class FeedbackBinary:
    data: bytes
    mime_type: str
    content_id: str
    filename: str
    url: str | None = None


ToolProvenance = Callable[[str, str | None, str], Awaitable[bool]]


class FeedbackAgents:
    """Grants are exact execution references; session/conversation membership is insufficient."""

    def __init__(
        self, sources: Sources, payloads: Payloads, tool_provenance: ToolProvenance
    ) -> None:
        self.sources, self.payloads, self.prove_tool = sources, payloads, tool_provenance
        self.factory = sources.factory
        self._threads: Threads | None = None

    def bind_threads(self, threads: Threads) -> None:
        """Complete composition: agents answer comments through the canonical thread owner."""
        self._threads = threads

    async def scope(self, session_id: str, turn_id: str | None) -> ExecutionScope:
        async with self.factory() as db:
            session = await db.get(Session, session_id)
            if session is None or not session.user_email:
                raise FeedbackError(404, "not_found", "Execution session is unavailable")
            step = await db.scalar(select(StepRun).where(StepRun.session_id == session_id))
        return ExecutionScope(
            session_id=session_id,
            conversation_id=session.conversation_id,
            turn_id=turn_id,
            agent_id=session.agent_id,
            user_email=session.user_email,
            task_id=step.task_id if step else None,
            attempt_number=step.attempt_number if step else None,
        )

    async def handle(
        self,
        tool_call: ToolCall,
        session_id: str,
        turn_id: str | None,
        *,
        llm: Any = None,
        current_model: str | None = None,
        current_provider_id: str | None = None,
    ) -> ToolResult:
        from pydantic import ValidationError

        try:
            scope = await self.scope(session_id, turn_id)
            if tool_call.name == "comment_reply":
                answer = await self.comment_reply(
                    scope,
                    CommentReplyArguments.model_validate(tool_call.arguments),
                    creation_key=tool_call.call_id or "",
                )
                return ToolResult(call_id=tool_call.call_id, output=encode(answer).decode("utf-8"))
            if tool_call.name == "feedback_submission_read":
                request = FeedbackReadArguments.model_validate(tool_call.arguments)
                result = await self.read(scope, request)
                if isinstance(result, FeedbackBinary):
                    from cognis.models.artifact import ArtifactKind, AttachmentRef
                    from cognis.tools.builtin.artifact_tools import analyze_attachment_ref

                    attachment = AttachmentRef(
                        artifact_id=result.content_id,
                        filename=result.filename,
                        mime_type=result.mime_type,
                        size_bytes=len(result.data),
                        kind=ArtifactKind.IMAGE
                        if result.mime_type.startswith("image/")
                        else ArtifactKind.PDF
                        if result.mime_type == "application/pdf"
                        else ArtifactKind.FILE,
                        url=result.url,
                    )
                    analyzed = await analyze_attachment_ref(
                        attachment=attachment,
                        content=result.data,
                        owner_email=scope.user_email,
                        llm=llm,
                        artifact_store=self.sources.store,
                        session_factory=self.factory,
                        current_model=current_model,
                        current_provider_id=current_provider_id,
                        prompt=request.prompt
                        or "Read this exact selected feedback source or marked capture.",
                    )
                    return analyzed.model_copy(update={"call_id": tool_call.call_id})
            else:
                result = await self.record(
                    scope, FeedbackRecordArguments.model_validate(tool_call.arguments)
                )
            return ToolResult(call_id=tool_call.call_id, output=encode(result).decode("utf-8"))
        except FeedbackError as exc:
            return ToolResult(
                call_id=tool_call.call_id,
                is_error=True,
                output=encode({"code": exc.code, "message": str(exc)}).decode(),
            )
        except ValidationError:
            return ToolResult(
                call_id=tool_call.call_id,
                is_error=True,
                output="Invalid feedback tool arguments. Use the canonical descriptor.",
            )

    async def authority_ref(
        self,
        db: AsyncSession,
        scope: ExecutionScope,
        submission: rows.FeedbackSubmissionRow,
    ) -> str | None:
        session = await db.get(Session, scope.session_id)
        if (
            session is None
            or session.agent_id != scope.agent_id
            or session.user_email != scope.user_email
        ):
            return None
        delegated = bool(session.parent_session_id and session.delegation_mode)
        if scope.task_id is not None:
            task = await db.get(Task, scope.task_id)
            step = await db.scalar(select(StepRun).where(StepRun.session_id == scope.session_id))
            if not (
                task
                and step
                and task.attempt_number == scope.attempt_number
                and step.attempt_number == scope.attempt_number
                and step.task_id == scope.task_id
            ):
                return None
            if (
                not delegated
                and submission.destination_task_id == scope.task_id
                and submission.attempt_number == scope.attempt_number
            ):
                return submission.submission_id
        elif (
            not delegated
            and scope.conversation_id == submission.destination_conversation_id
            and session.conversation_id == scope.conversation_id
            and scope.turn_id == submission.turn_id
        ):
            request = await db.scalar(
                select(DirectTurnRequestRow).where(
                    DirectTurnRequestRow.request_id == submission.request_id
                )
            )
            if (
                request
                and request.turn_id == scope.turn_id
                and request.conversation_id == scope.conversation_id
            ):
                return submission.submission_id
        child = await db.scalar(
            select(rows.FeedbackWorkLinkRow).where(
                rows.FeedbackWorkLinkRow.submission_id == submission.submission_id,
                or_(
                    (rows.FeedbackWorkLinkRow.kind == "child_conversation")
                    & (rows.FeedbackWorkLinkRow.reference_id == scope.conversation_id)
                    & (rows.FeedbackWorkLinkRow.turn_id == scope.turn_id),
                    (rows.FeedbackWorkLinkRow.kind == "child_task")
                    & (rows.FeedbackWorkLinkRow.reference_id == scope.task_id)
                    & (rows.FeedbackWorkLinkRow.attempt_number == scope.attempt_number),
                    (rows.FeedbackWorkLinkRow.kind == "child_session")
                    & (rows.FeedbackWorkLinkRow.reference_id == scope.session_id)
                    & (rows.FeedbackWorkLinkRow.turn_id == scope.turn_id),
                ),
            )
        )
        return child.link_id if child else None

    async def allowed(
        self, db: AsyncSession, scope: ExecutionScope, submission: rows.FeedbackSubmissionRow
    ) -> bool:
        """Check the exact admission or persisted explicit child authority."""
        return await self.authority_ref(db, scope, submission) is not None

    async def packet(
        self,
        scope: ExecutionScope,
        submission_id: str,
        manifest_hash: str,
    ) -> tuple[rows.FeedbackSubmissionRow, dto.SubmissionManifest]:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackSubmissionRow, submission_id)
            if (
                row is None
                or row.delivery != "accepted"
                or row.manifest_hash != manifest_hash
                or not await self.allowed(db, scope, row)
            ):
                raise FeedbackError(
                    404, "not_found", "Feedback packet is not granted to this execution"
                )
            sources = (
                await db.scalars(
                    select(rows.FeedbackSubmissionItemRow.subject_id)
                    .where(rows.FeedbackSubmissionItemRow.submission_id == submission_id)
                    .distinct()
                )
            ).all()
            for subject_id in sources:
                _, caps = await self.sources.authorize(
                    db, Actor.user(row.sender_email), subject_id, "dispatch"
                )
                if not caps.disclose:
                    raise FeedbackError(
                        403, "disclosure_revoked", "Feedback source disclosure was revoked"
                    )
        manifest = dto.SubmissionManifest.model_validate(await self.payloads.get(row.payload_id))
        return row, manifest

    async def read(
        self,
        scope: ExecutionScope,
        request: FeedbackReadArguments,
    ) -> dict[str, Any] | FeedbackBinary:
        row, manifest = await self.packet(scope, request.submission_id, request.manifest_hash)
        if request.view == "manifest":
            return manifest.model_dump(mode="json", by_alias=True)
        if request.view == "capture":
            captures = {
                entry.anchor.locator.capture_id
                for entry in manifest.entries
                if isinstance(entry.anchor.locator, dto.HtmlLocator)
                and entry.anchor.locator.capture_id
            }
            if request.capture_id not in captures:
                raise FeedbackError(404, "not_found", "Capture is not part of this packet")
            async with self.factory() as db:
                capture = await db.get(rows.FeedbackCaptureRow, request.capture_id)
            if capture is None or not capture.payload_id:
                raise FeedbackError(410, "source_unavailable", "Selected capture is unavailable")
            data, mime = await self.payloads.get_bytes(capture.payload_id)
            return FeedbackBinary(data, mime, capture.capture_id, "feedback-capture.png")
        source = next(
            (item for item in manifest.sources if item.subject_id == request.source_id), None
        )
        if source is None:
            raise FeedbackError(404, "not_found", "Source is not part of this packet")
        evidence = await self.sources.load(row.sender_email, source.source)
        if evidence.content_digest != source.content_digest:
            raise FeedbackError(410, "source_unavailable", "Exact selected source is unavailable")
        text = evidence.text
        if evidence.format == "html":
            try:
                text = evidence.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FeedbackError(
                    422, "source_unavailable", "HTML source is not UTF-8 encoded"
                ) from exc
        if text is not None:
            end = min(utf16_length(text), request.offset + request.limit)
            return {
                "source": source.model_dump(mode="json"),
                "offset": request.offset,
                "text": utf16_slice(text, request.offset, end),
                "next_offset": end if end < utf16_length(text) else None,
            }
        mime = evidence.mime_type or "application/octet-stream"
        content_ref = (
            source.source.content_ref if source.source.kind == "document" else source.subject_id
        )
        url = None
        if evidence.format == "pdf":
            async with self.factory() as db:
                artifact = await db.get(ArtifactRecordRow, content_ref)
                if artifact is None or not await self.sources.document_access(
                    db, row.sender_email, content_ref
                ):
                    raise FeedbackError(404, "not_found", "Selected PDF is unavailable")
            # The normal attachment analyzer can fall back to PDF text when
            # the configured storage backend cannot provide a URL.
            with suppress(Exception):
                url = await self.sources.store.async_get_public_url(
                    artifact.namespace, artifact.object_id, artifact.filename
                )
        return FeedbackBinary(evidence.data, mime, content_ref, "feedback-source", url)

    async def scope_packets(
        self, scope: ExecutionScope
    ) -> list[tuple[rows.FeedbackSubmissionRow, dto.SubmissionManifest]]:
        async with self.factory() as db:
            candidates = (
                await db.scalars(
                    select(rows.FeedbackSubmissionRow).where(
                        rows.FeedbackSubmissionRow.delivery == "accepted",
                        or_(
                            (
                                rows.FeedbackSubmissionRow.destination_conversation_id
                                == scope.conversation_id
                            )
                            if scope.conversation_id
                            else false(),
                            (rows.FeedbackSubmissionRow.destination_task_id == scope.task_id)
                            if scope.task_id
                            else false(),
                            rows.FeedbackSubmissionRow.submission_id.in_(
                                select(rows.FeedbackWorkLinkRow.submission_id).where(
                                    rows.FeedbackWorkLinkRow.reference_id.in_(
                                        [
                                            value
                                            for value in [
                                                scope.conversation_id,
                                                scope.task_id,
                                                scope.session_id,
                                            ]
                                            if value
                                        ]
                                    )
                                )
                            ),
                        ),
                    )
                )
            ).all()
        result = []
        for row in candidates:
            async with self.factory() as db:
                if not await self.allowed(db, scope, row):
                    continue
            result.append(await self.packet(scope, row.submission_id, row.manifest_hash))
        return result

    async def sent_comments(self, scope: ExecutionScope) -> list[dto.ManifestEntry]:
        """Instruction comments delivered to this exact execution, newest packet last."""
        entries: dict[str, dto.ManifestEntry] = {}
        for _, manifest in await self.scope_packets(scope):
            for item in manifest.entries:
                if item.role == "instruction":
                    entries[item.entry.thread_id] = item
        return list(entries.values())

    async def open_comments(self, scope: ExecutionScope) -> list[dto.ManifestEntry]:
        """Sent comments this agent has neither answered nor resolved yet."""
        sent = await self.sent_comments(scope)
        if not sent:
            return []
        identity = f"agent:{scope.agent_id}"
        thread_ids = [item.entry.thread_id for item in sent]
        async with self.factory() as db:
            closed = set(
                (
                    await db.scalars(
                        select(rows.FeedbackThreadRow.thread_id).where(
                            rows.FeedbackThreadRow.thread_id.in_(thread_ids),
                            or_(
                                rows.FeedbackThreadRow.state == "resolved",
                                rows.FeedbackThreadRow.hidden.is_(True),
                            ),
                        )
                    )
                ).all()
            )
            answered = set(
                (
                    await db.scalars(
                        select(rows.FeedbackEntryRow.thread_id).where(
                            rows.FeedbackEntryRow.thread_id.in_(thread_ids),
                            rows.FeedbackEntryRow.author_id == identity,
                        )
                    )
                ).all()
            )
        return [
            item
            for item in sent
            if item.entry.thread_id not in closed and item.entry.thread_id not in answered
        ]

    async def comment_reply(
        self, scope: ExecutionScope, request: CommentReplyArguments, *, creation_key: str
    ) -> dict[str, Any]:
        """Answer a comment that was sent to this exact execution."""
        if self._threads is None:
            raise FeedbackError(503, "feedback_unavailable", "Comment replies are unavailable")
        sent = {item.entry.thread_id for item in await self.sent_comments(scope)}
        if request.comment_id not in sent:
            raise FeedbackError(
                404,
                "not_found",
                "Only comments sent with this request can be answered; use their fbt_ id",
            )
        async with self.factory() as db:
            agent = await db.get(Agent, scope.agent_id)
        label = (agent.display_name or agent.name if agent else None) or scope.agent_id
        state, replied = await self._threads.agent_answer(
            thread_id=request.comment_id,
            agent_id=scope.agent_id,
            agent_label=label,
            reply=request.reply,
            resolve=request.resolve,
            creation_key=creation_key or digest(encode(request.model_dump(mode="json"))),
        )
        remaining = len(await self.open_comments(scope))
        return {
            "comment_id": request.comment_id,
            "state": state,
            "replied": replied,
            "remaining_comments": remaining,
        }

    async def before_write(self, scope: ExecutionScope, supersedes: str | None) -> None:
        if not supersedes:
            return
        for _, manifest in await self.scope_packets(scope):
            selected = {
                source.source.content_ref
                for source in manifest.sources
                if isinstance(source.source, dto.DocumentSourceRef)
            }
            evidence = await self.sources.load(
                scope.user_email, dto.DocumentSourceRef(kind="document", content_ref=supersedes)
            )
            selected_lineage = any(
                source.source.kind == "document"
                and source.lineage_id == evidence.lineage_id
                and source.source.content_ref.startswith("dlv_") == supersedes.startswith("dlv_")
                for source in manifest.sources
            )
            if not selected_lineage:
                continue
            if not permits_source_revision(manifest):
                raise FeedbackError(
                    403,
                    "update_mode_forbidden",
                    "This packet does not authorize revising its source",
                )
            if evidence.producer_agent_id != scope.agent_id:
                raise FeedbackError(
                    403,
                    "revision_owner_mismatch",
                    "Only the producing agent can revise this source",
                )
            if supersedes not in selected:
                try:
                    relation = await self.result_proof(scope, manifest, supersedes)
                except FeedbackError:
                    relation = None
                if relation != "revises_source":
                    raise FeedbackError(
                        409,
                        "source_head_changed",
                        "The source changed outside this admitted execution",
                    )
            if evidence.latest_content_ref != supersedes:
                raise FeedbackError(
                    409, "source_head_changed", "The reviewed source is no longer the lineage head"
                )

    async def result_proof(
        self,
        scope: ExecutionScope,
        manifest: dto.SubmissionManifest,
        content_ref: str,
    ) -> Literal["revises_source", "separate_result"]:
        async with self.factory() as db:
            causal = await db.scalar(
                select(rows.FeedbackWorkLinkRow)
                .join(rows.FeedbackSubmissionRow)
                .where(
                    rows.FeedbackSubmissionRow.sender_email == scope.user_email,
                    rows.FeedbackWorkLinkRow.kind.in_(
                        ["result_revises_source", "result_separate_result"]
                    ),
                    rows.FeedbackWorkLinkRow.reference_id == content_ref,
                    rows.FeedbackWorkLinkRow.turn_id == scope.turn_id,
                    rows.FeedbackWorkLinkRow.attempt_number == scope.attempt_number,
                    *(
                        []
                        if scope.turn_id
                        else [rows.FeedbackWorkLinkRow.parent_reference_id == scope.session_id]
                    ),
                )
            )
            if causal is not None:
                canonical: DeliverableRow | ArtifactRecordRow | None
                if content_ref.startswith("dlv_"):
                    canonical = await db.get(DeliverableRow, content_ref)
                else:
                    canonical = await db.get(ArtifactRecordRow, content_ref)
                if canonical is None:
                    raise FeedbackError(404, "not_found", "Canonical result is unavailable")
                return result_relation(
                    manifest,
                    content_ref,
                    canonical.lineage_id,
                    canonical.revision,
                    canonical.supersedes_deliverable_id
                    if isinstance(canonical, DeliverableRow)
                    else canonical.supersedes_artifact_id,
                )
            if content_ref.startswith("dlv_"):
                document = await db.get(DeliverableRow, content_ref)
                if document is None or document.owner_agent_id != scope.agent_id:
                    raise FeedbackError(
                        404, "not_found", "Result was not produced by this execution"
                    )
                session_id, turn_id = document.session_id, document.turn_id
                supersedes = document.supersedes_deliverable_id
                lineage_id, revision = document.lineage_id, document.revision
                step = await db.get(StepRun, document.step_run_id) if document.step_run_id else None
                if scope.task_id:
                    valid = bool(
                        step
                        and step.task_id == scope.task_id
                        and document.attempt_number == scope.attempt_number
                    )
                else:
                    valid = (
                        document.conversation_id == scope.conversation_id
                        and turn_id == scope.turn_id
                    )
                tool_call = None
            else:
                artifact = await db.get(ArtifactRecordRow, content_ref)
                if (
                    artifact is None
                    or artifact.owner_agent_id != scope.agent_id
                    or artifact.deleted_at is not None
                ):
                    raise FeedbackError(
                        404, "not_found", "Result was not produced by this execution"
                    )
                session_id, tool_call = artifact.session_id, artifact.source_tool_call_id
                supersedes = artifact.supersedes_artifact_id
                lineage_id, revision = artifact.lineage_id, artifact.revision
                producer = await db.get(Session, session_id) if session_id else None
                producer_step = (
                    await db.scalar(select(StepRun).where(StepRun.session_id == session_id))
                    if session_id and scope.task_id
                    else None
                )
                valid = bool(
                    producer
                    and tool_call
                    and producer.user_email == scope.user_email
                    and (
                        producer_step
                        and producer_step.task_id == scope.task_id
                        and producer_step.attempt_number == scope.attempt_number
                        if scope.task_id
                        else producer.conversation_id == scope.conversation_id
                    )
                )
                turn_id = scope.turn_id
        if session_id is None or not valid:
            raise FeedbackError(
                403, "result_provenance_mismatch", "Result does not belong to this admitted work"
            )
        if not content_ref.startswith("dlv_") and (
            (turn_id is None and scope.task_id is None)
            or tool_call is None
            or not await self.prove_tool(session_id, turn_id, tool_call)
        ):
            raise FeedbackError(
                403, "result_provenance_mismatch", "Artifact tool-call provenance is unavailable"
            )
        return result_relation(manifest, content_ref, lineage_id, revision, supersedes)

    async def register_result(
        self,
        scope: ExecutionScope,
        row: rows.FeedbackSubmissionRow,
        manifest: dto.SubmissionManifest,
        content_ref: str,
        claimed_relation: str | None = None,
    ) -> dto.SubmissionResult:
        relation = await self.result_proof(scope, manifest, content_ref)
        if claimed_relation is not None and claimed_relation != relation:
            raise FeedbackError(
                409,
                "result_relation_mismatch",
                "Claimed result relation is not canonical provenance",
            )
        if relation == "revises_source" and not permits_source_revision(manifest):
            raise FeedbackError(
                409, "update_mode_mismatch", "Result does not match the confirmed update mode"
            )
        async with self.factory() as db:
            await insert_absent(
                db,
                rows.FeedbackWorkLinkRow,
                {
                    "link_id": "fbrl_" + digest(encode([row.submission_id, content_ref])),
                    "submission_id": row.submission_id,
                    "kind": "result_" + relation,
                    "reference_id": content_ref,
                    "turn_id": scope.turn_id,
                    "attempt_number": scope.attempt_number,
                },
                ["link_id"],
            )
            await db.commit()
        return dto.SubmissionResult(content_ref=content_ref, relation=relation)

    async def record(
        self, scope: ExecutionScope, request: FeedbackRecordArguments
    ) -> dict[str, Any]:
        row, manifest = await self.packet(scope, request.submission_id, request.manifest_hash)
        if request.action == "result":
            assert request.result_ref is not None
            return (
                await self.register_result(
                    scope, row, manifest, request.result_ref, request.relation
                )
            ).model_dump(mode="json")
        if request.action == "child":
            return (
                await self.child(
                    Actor.user(row.sender_email),
                    row.submission_id,
                    dto.ChildWorkRequest(
                        manifest_hash=row.manifest_hash,
                        child_conversation_id=request.child_conversation_id,
                        child_task_id=request.child_task_id,
                        child_session_id=request.child_session_id,
                        child_turn_id=request.child_turn_id,
                        child_attempt_number=request.child_attempt_number,
                    ),
                    scope=scope,
                )
            ).model_dump(mode="json")
        selected = {
            entry.entry.entry_version_id
            for entry in manifest.entries
            if entry.role == "instruction"
        }
        if request.entry_version_id not in selected:
            raise FeedbackError(
                404, "not_found", "Disposition is limited to selected instruction entries"
            )
        relation = (
            await self.result_proof(scope, manifest, request.result_ref)
            if request.result_ref
            else None
        )
        if (
            request.disposition == "addressed"
            and manifest.update_mode == "revise_source"
            and relation != "revises_source"
        ):
            raise FeedbackError(
                409,
                "result_required",
                "Addressing a revision requires an actual source revision result",
            )
        if request.result_ref:
            await self.register_result(scope, row, manifest, request.result_ref)
        payload = await self.payloads.put(row.sender_email, {"reason": request.reason})
        disposition_id = "fbdi_" + digest(encode(request.model_dump(mode="json")))
        async with self.factory() as db:
            await insert_absent(
                db,
                rows.FeedbackDispositionRow,
                {
                    "disposition_id": disposition_id,
                    "submission_id": row.submission_id,
                    "entry_version_id": request.entry_version_id,
                    "disposition": request.disposition,
                    "result_ref": request.result_ref,
                    "actor_session_id": scope.session_id,
                    "payload_id": payload,
                },
                ["disposition_id"],
            )
            await self.payloads.adopt(db, payload)
            await db.commit()
        return {
            "disposition_id": disposition_id,
            "entry_version_id": request.entry_version_id,
            "disposition": request.disposition,
            "resolved": False,
        }

    async def child(
        self,
        actor: Actor,
        submission_id: str,
        request: dto.ChildWorkRequest,
        *,
        scope: ExecutionScope | None = None,
    ) -> dto.ChildWorkResponse:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackSubmissionRow, submission_id)
            if (
                row is None
                or row.sender_email != actor.user_email
                or row.manifest_hash != request.manifest_hash
            ):
                raise FeedbackError(404, "not_found", "Submission is unavailable")
            if scope is not None and not await self.allowed(db, scope, row):
                raise FeedbackError(
                    403, "feedback_forbidden", "Child grant requires an admitted parent execution"
                )
            kind = (
                "child_conversation"
                if request.child_conversation_id
                else "child_task"
                if request.child_task_id
                else "child_session"
            )
            reference = (
                request.child_conversation_id or request.child_task_id or request.child_session_id
            )
            query = select(rows.FeedbackWorkLinkRow).where(
                rows.FeedbackWorkLinkRow.submission_id == submission_id,
                rows.FeedbackWorkLinkRow.kind == kind,
                rows.FeedbackWorkLinkRow.reference_id == reference,
            )
            if request.child_attempt_number is not None:
                query = query.where(
                    rows.FeedbackWorkLinkRow.attempt_number == request.child_attempt_number
                )
            if request.child_turn_id is not None:
                query = query.where(rows.FeedbackWorkLinkRow.turn_id == request.child_turn_id)
            links = (await db.scalars(query.limit(2))).all()
            if not links:
                raise FeedbackError(
                    403,
                    "child_provenance_mismatch",
                    "Select feedback_submissions at canonical child admission; ancestry is not a grant",
                )
            if len(links) != 1:
                raise FeedbackError(
                    409, "child_execution_required", "Select the child's exact admitted turn"
                )
            return await child_work(db, links[0])

    async def results(self, actor: Actor, submission_id: str) -> dto.SubmissionResultsResponse:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackSubmissionRow, submission_id)
            if row is None or row.sender_email != actor.user_email:
                raise FeedbackError(404, "not_found", "Submission is unavailable")
            sources = (
                await db.scalars(
                    select(rows.FeedbackSubmissionItemRow.subject_id)
                    .where(
                        rows.FeedbackSubmissionItemRow.submission_id == submission_id,
                    )
                    .distinct()
                )
            ).all()
            for source in sources:
                await self.sources.authorize(db, actor, source)
            links = (
                await db.scalars(
                    select(rows.FeedbackWorkLinkRow).where(
                        rows.FeedbackWorkLinkRow.submission_id == submission_id,
                        rows.FeedbackWorkLinkRow.kind.in_(
                            ["result_revises_source", "result_separate_result"]
                        ),
                    )
                )
            ).all()
        items = []
        for link in links:
            try:
                await self.sources.document(
                    actor.identity,
                    dto.DocumentSourceRef(kind="document", content_ref=link.reference_id),
                )
            except FeedbackError:
                continue
            items.append(
                dto.SubmissionResult(
                    content_ref=link.reference_id,
                    relation=cast(
                        Literal["revises_source", "separate_result"],
                        link.kind.removeprefix("result_"),
                    ),
                )
            )
        return dto.SubmissionResultsResponse(items=items)
