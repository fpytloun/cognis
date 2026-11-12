"""Source-local history, preferred follow-ups and conservative anchor mappings."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, func, or_, select, true, update

from cognis.core.feedback.primitives import (
    Actor,
    FeedbackError,
    Payloads,
    affected,
    aware,
    identifier,
)
from cognis.core.feedback.sources import SOURCE_ADAPTER, Sources, describe_text, utf16_length
from cognis.core.feedback.submissions import Submissions
from cognis.core.feedback.threads import Threads, insert_absent
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.models import Conversation


def history_before(
    created_at: SQLColumnExpression[datetime],
    key: SQLColumnExpression[str],
    cursor: tuple[str, str] | None,
) -> ColumnElement[bool]:
    """Apply pagination before bounded reads, including equal-timestamp tie breaks."""
    if cursor is None:
        return true()
    timestamp = datetime.fromisoformat(cursor[0])
    return or_(created_at < timestamp, and_(created_at == timestamp, key < cursor[1]))


class Continuity:
    """Never infer completion from current conversation activity or resolution from a result."""

    def __init__(
        self, sources: Sources, payloads: Payloads, threads: Threads, submissions: Submissions
    ) -> None:
        self.sources, self.payloads, self.threads, self.submissions = (
            sources,
            payloads,
            threads,
            submissions,
        )
        self.factory = sources.factory

    async def summary(self, actor: Actor, subject_id: str) -> dto.SubjectSummary:
        async with self.factory() as db:
            subject, caps = await self.sources.authorize(db, actor, subject_id)
            states = {
                str(row[0]): int(row[1])
                for row in (
                    await db.execute(
                        select(
                            rows.FeedbackThreadRow.state,
                            func.count(),
                        )
                        .where(
                            rows.FeedbackThreadRow.subject_id == subject_id,
                            rows.FeedbackThreadRow.hidden.is_(False),
                        )
                        .group_by(rows.FeedbackThreadRow.state)
                    )
                ).all()
            }
            preference = await db.get(
                rows.FeedbackPreferenceRow, (actor.identity, subject.lineage_id or subject_id)
            )
            conversation = (
                await db.get(Conversation, preference.conversation_id)
                if preference and preference.conversation_id
                else None
            )
            preferred = (
                conversation.conversation_id
                if (
                    conversation
                    and conversation.user_email == actor.user_email
                    and conversation.status == "active"
                )
                else None
            )
        return dto.SubjectSummary(
            subject_id=subject_id,
            capabilities=caps,
            open_threads=states.get("open", 0),
            resolved_threads=states.get("resolved", 0),
            preferred_followup=preferred,
            version=preference.version if preference else 1,
        )

    async def preference(
        self, actor: Actor, subject_id: str, request: dto.FollowupPreferenceRequest
    ) -> dto.SubjectSummary:
        async with self.factory() as db:
            subject, _ = await self.sources.authorize(db, actor, subject_id, "dispatch")
            if request.conversation_id:
                conversation = await db.get(Conversation, request.conversation_id)
                if (
                    not conversation
                    or conversation.user_email != actor.user_email
                    or conversation.status != "active"
                ):
                    raise FeedbackError(404, "not_found", "Preferred follow-up is unavailable")
            key = subject.lineage_id or subject_id
            current = await db.get(rows.FeedbackPreferenceRow, (actor.identity, key))
            if current is None:
                if request.expected_version:
                    raise FeedbackError(409, "version_conflict", "Follow-up preference changed")
                inserted = await insert_absent(
                    db,
                    rows.FeedbackPreferenceRow,
                    {
                        "owner_email": actor.identity,
                        "subject_id": key,
                        "version": 1,
                        "conversation_id": request.conversation_id,
                    },
                    ["owner_email", "subject_id"],
                )
                if not inserted:
                    raise FeedbackError(409, "version_conflict", "Follow-up preference changed")
            else:
                changed = await db.execute(
                    update(rows.FeedbackPreferenceRow)
                    .where(
                        rows.FeedbackPreferenceRow.owner_email == actor.identity,
                        rows.FeedbackPreferenceRow.subject_id == key,
                        rows.FeedbackPreferenceRow.version == request.expected_version,
                    )
                    .values(
                        version=request.expected_version + 1,
                        conversation_id=request.conversation_id,
                    )
                )
                if affected(changed) != 1:
                    raise FeedbackError(409, "version_conflict", "Follow-up preference changed")
            await db.commit()
        return await self.summary(actor, subject_id)

    async def history(
        self, actor: Actor, subject_id: str, cursor: str | None, limit: int
    ) -> dto.HistoryPage:
        before = None
        if cursor:
            token = self.sources.tokens.open(cursor, kind="history", actor=actor.identity)
            if token["subject_id"] != subject_id:
                raise FeedbackError(404, "not_found", "History cursor is unavailable")
            before = (token["time"], token["id"])
        async with self.factory() as db:
            _, caps = await self.sources.authorize(db, actor, subject_id)
            visible_threads = select(rows.FeedbackThreadRow.thread_id).where(
                rows.FeedbackThreadRow.subject_id == subject_id,
                true() if caps.moderate else rows.FeedbackThreadRow.hidden.is_(False),
            )
            visible_versions = (
                select(rows.FeedbackEntryVersionRow.entry_version_id)
                .join(
                    rows.FeedbackEntryRow,
                    rows.FeedbackEntryRow.entry_id == rows.FeedbackEntryVersionRow.entry_id,
                )
                .where(rows.FeedbackEntryRow.thread_id.in_(visible_threads))
            )
            visible_submissions = select(rows.FeedbackSubmissionItemRow.submission_id).where(
                rows.FeedbackSubmissionItemRow.subject_id == subject_id,
                rows.FeedbackSubmissionItemRow.role == "instruction",
                rows.FeedbackSubmissionItemRow.entry_version_id.in_(visible_versions),
            )
            audits = (
                await db.scalars(
                    select(rows.FeedbackAuditRow)
                    .where(
                        rows.FeedbackAuditRow.subject_id == subject_id,
                        rows.FeedbackAuditRow.kind.in_(
                            ["resolution", "reattach", "moderation"]
                            if caps.moderate
                            else ["resolution", "reattach"]
                        ),
                        or_(
                            rows.FeedbackAuditRow.thread_id.is_(None),
                            rows.FeedbackAuditRow.thread_id.in_(visible_threads),
                        ),
                        history_before(
                            rows.FeedbackAuditRow.created_at, rows.FeedbackAuditRow.audit_id, before
                        ),
                    )
                    .order_by(
                        rows.FeedbackAuditRow.created_at.desc(),
                        rows.FeedbackAuditRow.audit_id.desc(),
                    )
                    .limit(1000)
                )
            ).all()
            submissions = (
                await db.scalars(
                    select(rows.FeedbackSubmissionRow)
                    .join(rows.FeedbackSubmissionItemRow)
                    .where(
                        rows.FeedbackSubmissionItemRow.subject_id == subject_id,
                        rows.FeedbackSubmissionRow.submission_id.in_(visible_submissions),
                        history_before(
                            rows.FeedbackSubmissionRow.created_at,
                            rows.FeedbackSubmissionRow.submission_id,
                            before,
                        ),
                    )
                    .distinct()
                    .order_by(rows.FeedbackSubmissionRow.created_at.desc())
                    .limit(1000)
                )
            ).all()
            dispositions = (
                await db.scalars(
                    select(rows.FeedbackDispositionRow)
                    .join(
                        rows.FeedbackSubmissionItemRow,
                        (
                            rows.FeedbackSubmissionItemRow.submission_id
                            == rows.FeedbackDispositionRow.submission_id
                        )
                        & (
                            rows.FeedbackSubmissionItemRow.entry_version_id
                            == rows.FeedbackDispositionRow.entry_version_id
                        ),
                    )
                    .where(
                        rows.FeedbackSubmissionItemRow.subject_id == subject_id,
                        rows.FeedbackDispositionRow.entry_version_id.in_(visible_versions),
                        history_before(
                            rows.FeedbackDispositionRow.created_at,
                            rows.FeedbackDispositionRow.disposition_id,
                            before,
                        ),
                    )
                    .order_by(rows.FeedbackDispositionRow.created_at.desc())
                    .limit(1000)
                )
            ).all()
            results = (
                await db.scalars(
                    select(rows.FeedbackWorkLinkRow)
                    .where(
                        rows.FeedbackWorkLinkRow.kind.in_(
                            ["result_revises_source", "result_separate_result"]
                        ),
                        rows.FeedbackWorkLinkRow.submission_id.in_(visible_submissions),
                        history_before(
                            rows.FeedbackWorkLinkRow.created_at,
                            rows.FeedbackWorkLinkRow.link_id,
                            before,
                        ),
                    )
                    .order_by(
                        rows.FeedbackWorkLinkRow.created_at.desc(),
                        rows.FeedbackWorkLinkRow.link_id.desc(),
                    )
                    .limit(1000)
                )
            ).all()
            references = {value.result_ref for value in dispositions if value.result_ref}
            references.update(value.reference_id for value in results)
            readable = {
                ref
                for ref in references
                if await self.sources.document_access(db, actor.identity, ref)
            }
        items: list[tuple[tuple[str, str], dto.HistoryItem]] = []
        for audit in audits:
            payload = await self.payloads.get(audit.payload_id)
            item = dto.HistoryItem(
                kind=cast(Literal["resolution", "reattach", "moderation"], audit.kind),
                subject_id=subject_id,
                created_at=audit.created_at,
                reason=payload.get("reason"),
            )
            items.append(((aware(audit.created_at).isoformat(), audit.audit_id), item))
        for submission in submissions:
            receipt = None
            if submission.sender_email == actor.user_email:
                with suppress(FeedbackError):
                    receipt = (
                        await self.submissions.detail(actor, submission.submission_id)
                    ).receipt
            items.append(
                (
                    (aware(submission.created_at).isoformat(), submission.submission_id),
                    dto.HistoryItem(
                        kind="submission",
                        subject_id=subject_id,
                        created_at=submission.created_at,
                        receipt=receipt,
                        state=submission.delivery,
                    ),
                )
            )
        for disposition in dispositions:
            payload = await self.payloads.get(disposition.payload_id)
            items.append(
                (
                    (aware(disposition.created_at).isoformat(), disposition.disposition_id),
                    dto.HistoryItem(
                        kind="disposition",
                        subject_id=subject_id,
                        created_at=disposition.created_at,
                        entry_version_id=disposition.entry_version_id,
                        state=disposition.disposition,
                        reason=payload["reason"],
                        result_ref=disposition.result_ref
                        if disposition.result_ref in readable
                        else None,
                    ),
                )
            )
        for result in results:
            if result.reference_id not in readable:
                continue
            items.append(
                (
                    (aware(result.created_at).isoformat(), result.link_id),
                    dto.HistoryItem(
                        kind="result",
                        subject_id=subject_id,
                        created_at=result.created_at,
                        result_ref=result.reference_id,
                        state=result.kind.removeprefix("result_"),
                    ),
                )
            )
        items = sorted(
            (value for value in items if before is None or value[0] < before),
            reverse=True,
            key=lambda value: value[0],
        )
        selected = items[:limit]
        next_cursor = None
        if len(items) > limit and selected:
            next_cursor = self.sources.tokens.seal(
                {
                    "kind": "history",
                    "actor": actor.identity,
                    "subject_id": subject_id,
                    "time": selected[-1][0][0],
                    "id": selected[-1][0][1],
                    "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                }
            )
        return dto.HistoryPage(items=[value[1] for value in selected], next_cursor=next_cursor)

    async def mapping(
        self, actor: Actor, anchor_id: str, target_subject_id: str
    ) -> dto.AnchorMapping:
        async with self.factory() as db:
            original = await db.get(rows.FeedbackAnchorRow, anchor_id)
            if original is None:
                raise FeedbackError(404, "not_found", "Anchor is unavailable")
            source, _ = await self.sources.authorize(db, actor, original.subject_id)
            target, _ = await self.sources.authorize(db, actor, target_subject_id)
            mapping = await db.get(rows.FeedbackAnchorMappingRow, (anchor_id, target_subject_id))
            if target.source.get("kind") != "document":
                raise FeedbackError(
                    422, "invalid_mapping", "Cross-version mapping requires a document"
                )
            target_ref = target.source["content_ref"]
            if source.subject_id == target.subject_id:
                return dto.AnchorMapping(
                    original_anchor_id=anchor_id,
                    mapped_anchor_id=anchor_id,
                    category="exact",
                    target_content_ref=target_ref,
                    reason="Exact source revision",
                    version=1,
                )
            if mapping:
                payload = await self.payloads.get(mapping.payload_id)
                return dto.AnchorMapping(
                    original_anchor_id=anchor_id,
                    mapped_anchor_id=mapping.mapped_anchor_id,
                    category=cast(Literal["exact", "remapped", "not_located"], mapping.category),
                    target_content_ref=target_ref,
                    reason=payload.get("reason", "Explicit reattachment"),
                    version=1,
                )
        original_dto = dto.FeedbackAnchor.model_validate(
            await self.payloads.get(original.payload_id)
        )
        reason = "The original anchor remains available; no unambiguous mapping was found."
        if not source.lineage_id or source.lineage_id != target.lineage_id:
            raise FeedbackError(
                422, "invalid_mapping", "Target is not in the original document lineage"
            )
        locator = original_dto.locator
        if isinstance(locator, dto.TextLocator):
            range_ = locator.range
            scope: dto.FeedbackTextScope = dto.DocumentTextScope(kind="document")
        elif (
            isinstance(locator, dto.RichLocator)
            and locator.range
            and locator.identity == "semantic"
        ):
            range_ = locator.range
            scope = dto.RichFieldTextScope(
                kind="rich_field",
                block_id=locator.block_id,
                identity=locator.identity,
                item_key=locator.item_key,
                field_path=locator.field_path,
            )
        else:
            return dto.AnchorMapping(
                original_anchor_id=anchor_id,
                category="not_located",
                target_content_ref=target_ref,
                reason=reason,
                version=1,
            )
        evidence = await self.sources.load(
            actor.identity, SOURCE_ADAPTER.validate_python(target.source)
        )
        try:
            text = await self.sources.text_for_scope(evidence, scope)
        except FeedbackError:
            return dto.AnchorMapping(
                original_anchor_id=anchor_id,
                category="not_located",
                target_content_ref=target_ref,
                reason=reason,
                version=1,
            )
        start = text.find(range_.exact)
        if start < 0 or text.find(range_.exact, start + 1) >= 0:
            return dto.AnchorMapping(
                original_anchor_id=anchor_id,
                category="not_located",
                target_content_ref=target_ref,
                reason=reason,
                version=1,
            )
        before, after = text[:start], text[start + len(range_.exact) :]
        if (range_.prefix and not before.endswith(range_.prefix)) or (
            range_.suffix and not after.startswith(range_.suffix)
        ):
            return dto.AnchorMapping(
                original_anchor_id=anchor_id,
                category="not_located",
                target_content_ref=target_ref,
                reason=reason,
                version=1,
            )
        updated_range = range_.model_copy(
            update={
                "start": utf16_length(before),
                "end": utf16_length(before + range_.exact),
                "text_digest": describe_text(text).text_digest,
            }
        )
        updated_locator = locator.model_copy(update={"range": updated_range})
        mapped_anchor = dto.FeedbackAnchor(
            anchor_id=identifier("fban"),
            source=SOURCE_ADAPTER.validate_python(target.source),
            content_digest=target.content_digest,
            locator=updated_locator,
        )
        payload = await self.payloads.put(
            actor.identity, mapped_anchor.model_dump(mode="json", by_alias=True)
        )
        metadata = await self.payloads.put(
            actor.identity, {"reason": "Unique quote and matching context in the same lineage"}
        )
        async with self.factory() as db:
            await self.sources.authorize(db, actor, source.subject_id)
            await self.sources.authorize(db, actor, target.subject_id)
            db.add(
                rows.FeedbackAnchorRow(
                    anchor_id=mapped_anchor.anchor_id,
                    subject_id=target.subject_id,
                    content_digest=target.content_digest,
                    payload_id=payload,
                    created_by=actor.identity,
                )
            )
            await db.flush()
            await insert_absent(
                db,
                rows.FeedbackAnchorMappingRow,
                {
                    "original_anchor_id": anchor_id,
                    "target_subject_id": target_subject_id,
                    "mapped_anchor_id": mapped_anchor.anchor_id,
                    "category": "remapped",
                    "payload_id": metadata,
                },
                ["original_anchor_id", "target_subject_id"],
            )
            await self.payloads.adopt(db, payload, metadata)
            await db.commit()
        return await self.mapping(actor, anchor_id, target_subject_id)
