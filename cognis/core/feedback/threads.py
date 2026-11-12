"""Versioned discussions, private drafts, and exact conversation staging."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select, union, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.feedback.primitives import (
    Actor,
    FeedbackError,
    Payloads,
    Tokens,
    affected,
    digest,
    encode,
    identifier,
    source_actor,
)
from cognis.core.feedback.sources import CONTEXT_ADAPTER, Sources
from cognis.core.feedback.staging import delivered_versions, stage_entry
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.models import ArtifactRecordRow, DeliverableRow

logger = logging.getLogger(__name__)


async def insert_absent(
    db: AsyncSession, model: Any, values: dict[str, Any], keys: list[str]
) -> bool:
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    result = await db.execute(
        insert(model).values(**values).on_conflict_do_nothing(index_elements=keys)
    )
    return affected(result) == 1


class Threads:
    """Own discussion state, without implicitly submitting any entry."""

    def __init__(self, sources: Sources, payloads: Payloads, tokens: Tokens) -> None:
        self.sources, self.payloads, self.tokens = sources, payloads, tokens
        self.factory = sources.factory

    async def prepare_authored(
        self,
        db: AsyncSession,
        actor: Actor,
        context: dto.FeedbackContext | None,
        entry_id: str,
        version_id: str,
        subject_id: str,
        anchor_id: str,
    ) -> None:
        """Prepare only the author's explicitly owned chat, never public work."""
        if actor.user_email is None or actor.share_id or context is None or context.kind != "chat":
            return
        await self.sources.context(db, actor, context)
        await stage_entry(
            db,
            owner_email=actor.user_email,
            conversation_id=context.conversation_id,
            entry_id=entry_id,
            entry_version_id=version_id,
            subject_id=subject_id,
            anchor_id=anchor_id,
            expected_version=None,
        )

    async def visible(self, db: AsyncSession, actor: Actor, thread: rows.FeedbackThreadRow) -> None:
        _, caps = await self.sources.authorize(db, actor, thread.subject_id)
        if actor.share_id:
            share = await db.get(rows.FeedbackShareRow, actor.share_id)
            if (
                thread.audience != f"public:{actor.share_id}"
                or thread.hidden
                or (
                    share is not None
                    and share.discussion == "own_only"
                    and thread.author_id != actor.identity
                )
            ):
                raise FeedbackError(404, "not_found", "Feedback thread is unavailable")
        elif thread.hidden and not caps.moderate:
            raise FeedbackError(404, "not_found", "Feedback thread is unavailable")

    async def entry(
        self, actor: Actor, entry_version_id: str, *, include_delivery: bool = True
    ) -> dto.EntryVersion:
        async with self.factory() as db:
            version = await db.get(rows.FeedbackEntryVersionRow, entry_version_id)
            entry = await db.get(rows.FeedbackEntryRow, version.entry_id) if version else None
            thread = await db.get(rows.FeedbackThreadRow, entry.thread_id) if entry else None
            if version is None or entry is None or thread is None:
                raise FeedbackError(404, "not_found", "Feedback entry is unavailable")
            await self.visible(db, actor, thread)
            deliveries: list[dto.EntryDelivery] = []
            if include_delivery and not actor.share_id:
                results = (
                    (
                        await db.execute(
                            select(rows.FeedbackSubmissionRow)
                            .join(rows.FeedbackSubmissionItemRow)
                            .where(
                                rows.FeedbackSubmissionItemRow.entry_version_id == entry_version_id
                            )
                            .order_by(rows.FeedbackSubmissionRow.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                # Full packets are never disclosed here; these are local delivery facts.
                deliveries = [
                    dto.EntryDelivery(
                        submission_id=result.submission_id,
                        manifest_hash=result.manifest_hash,
                        delivery=result.delivery,
                    )
                    for result in results
                    if result.sender_email == actor.user_email
                ]
        payload = await self.payloads.get(version.payload_id)
        return dto.EntryVersion(
            entry_id=entry.entry_id,
            entry_version_id=version.entry_version_id,
            version=version.version,
            thread_id=entry.thread_id,
            body=payload["body"],
            author=dto.Attribution(
                kind=entry.author_kind,
                label=entry.author_label,
                is_self=entry.author_id == actor.identity,
            ),
            audience="public" if actor.share_id else entry.audience,
            reply_to_entry_id=entry.reply_to_entry_id,
            created_at=version.created_at,
            delivery=deliveries,
        )

    async def thread(self, actor: Actor, thread_id: str) -> dto.FeedbackThread:
        async with self.factory() as db:
            thread = await db.get(rows.FeedbackThreadRow, thread_id)
            if thread is None:
                raise FeedbackError(404, "not_found", "Feedback thread is unavailable")
            await self.visible(db, actor, thread)
            anchor = await db.get(rows.FeedbackAnchorRow, thread.anchor_id)
            heads = list(
                await db.scalars(
                    select(rows.FeedbackEntryRow.head_version_id)
                    .where(rows.FeedbackEntryRow.thread_id == thread_id)
                    .order_by(rows.FeedbackEntryRow.created_at, rows.FeedbackEntryRow.entry_id)
                )
            )
        assert anchor is not None
        saved_anchor = dto.FeedbackAnchor.model_validate(await self.payloads.get(anchor.payload_id))
        return dto.FeedbackThread(
            thread_id=thread.thread_id,
            subject_id=thread.subject_id,
            version=thread.version,
            state=thread.state,
            hidden=thread.hidden,
            audience="public" if actor.share_id else thread.audience,
            anchor=saved_anchor,
            entries=[await self.entry(actor, head) for head in heads],
        )

    async def list(
        self, actor: Actor, subject_id: str, *, cursor: str | None = None, limit: int = 30
    ) -> dto.ThreadPage:
        after = None
        if cursor:
            token = self.tokens.open(cursor, kind="threads", actor=actor.identity)
            if token.get("subject_id") != subject_id or token.get("share_id") != actor.share_id:
                raise FeedbackError(
                    409, "cursor_invalid", "Feedback cursor belongs to another view"
                )
            after = token["after"]
        async with self.factory() as db:
            _, caps = await self.sources.authorize(db, actor, subject_id)
            query = select(rows.FeedbackThreadRow.thread_id).where(
                rows.FeedbackThreadRow.subject_id == subject_id
            )
            if actor.share_id:
                share = await db.get(rows.FeedbackShareRow, actor.share_id)
                query = query.where(
                    rows.FeedbackThreadRow.audience == f"public:{actor.share_id}",
                    rows.FeedbackThreadRow.hidden.is_(False),
                )
                if share and share.discussion == "own_only":
                    query = query.where(rows.FeedbackThreadRow.author_id == actor.identity)
            else:
                # Removed comments leave every normal listing; audit history keeps the record.
                query = query.where(rows.FeedbackThreadRow.hidden.is_(False))
            if after:
                query = query.where(rows.FeedbackThreadRow.thread_id > after)
            ids = list(
                await db.scalars(query.order_by(rows.FeedbackThreadRow.thread_id).limit(limit + 1))
            )
        next_cursor = None
        if len(ids) > limit:
            next_cursor = self.tokens.seal(
                {
                    "kind": "threads",
                    "actor": actor.identity,
                    "subject_id": subject_id,
                    "share_id": actor.share_id,
                    "after": ids[limit - 1],
                    "exp": (datetime.now(UTC) + timedelta(hours=1)).timestamp(),
                }
            )
        return dto.ThreadPage(
            items=[await self.thread(actor, item) for item in ids[:limit]],
            next_cursor=next_cursor,
        )

    async def create(self, actor: Actor, request: dto.ThreadCreateRequest) -> dto.FeedbackThread:
        request_hash = digest(encode(request.model_dump(mode="json")))
        async with self.factory() as db:
            old = await db.scalar(
                select(rows.FeedbackThreadRow).where(
                    rows.FeedbackThreadRow.author_id == actor.identity,
                    rows.FeedbackThreadRow.creation_key == request.creation_key,
                )
            )
        if old is not None:
            if old.request_hash != request_hash:
                raise FeedbackError(
                    409, "idempotency_conflict", "Creation key has different feedback"
                )
            return await self.thread(actor, old.thread_id)
        subject, evidence = await self.sources.validate_anchor(
            actor, request.anchor, request.context
        )
        if actor.share_id:
            audience = f"public:{actor.share_id}"
        else:
            if request.audience != "internal":
                raise FeedbackError(
                    422, "invalid_audience", "Use a comment share for public comments"
                )
            audience = "internal"
        anchor_id, thread_id, entry_id, version_id = (
            identifier("fba"),
            identifier("fbt"),
            identifier("fbe"),
            identifier("fbv"),
        )
        anchor = dto.FeedbackAnchor(
            anchor_id=anchor_id,
            source=evidence.source,
            content_digest=evidence.content_digest,
            locator=request.anchor.locator,
        )
        anchor_payload = await self.payloads.put(
            actor.identity, anchor.model_dump(mode="json", by_alias=True)
        )
        body_payload = await self.payloads.put(actor.identity, {"body": request.body})
        async with self.factory() as db:
            await self.sources.authorize(db, actor, subject.subject_id, "comment")
            db.add(
                rows.FeedbackAnchorRow(
                    anchor_id=anchor_id,
                    subject_id=subject.subject_id,
                    payload_id=anchor_payload,
                    content_digest=evidence.content_digest,
                    created_by=actor.identity,
                    capture_id=getattr(request.anchor.locator, "capture_id", None),
                )
            )
            await db.flush()
            db.add(
                rows.FeedbackThreadRow(
                    thread_id=thread_id,
                    subject_id=subject.subject_id,
                    anchor_id=anchor_id,
                    author_id=actor.identity,
                    creation_key=request.creation_key,
                    request_hash=request_hash,
                    audience=audience,
                )
            )
            try:
                await db.flush()
            except IntegrityError:
                # A concurrent creation-key winner is visible after this flush.
                # Roll back our anchor and never prepare its entry a second time.
                await db.rollback()
                old = await db.scalar(
                    select(rows.FeedbackThreadRow).where(
                        rows.FeedbackThreadRow.author_id == actor.identity,
                        rows.FeedbackThreadRow.creation_key == request.creation_key,
                    )
                )
                if old is None or old.request_hash != request_hash:
                    raise FeedbackError(
                        409, "idempotency_conflict", "Concurrent feedback creation"
                    ) from None
                await db.close()
                return await self.thread(actor, old.thread_id)
            db.add(
                rows.FeedbackEntryRow(
                    entry_id=entry_id,
                    thread_id=thread_id,
                    author_id=actor.identity,
                    author_kind="verified_user" if actor.user_email else "guest_unverified",
                    author_label=actor.label,
                    audience=audience,
                    version=1,
                    head_version_id=version_id,
                    creation_key=request.creation_key,
                    request_hash=request_hash,
                )
            )
            await db.flush()
            db.add(
                rows.FeedbackEntryVersionRow(
                    entry_version_id=version_id,
                    entry_id=entry_id,
                    version=1,
                    payload_id=body_payload,
                )
            )
            await self.payloads.adopt(db, anchor_payload, body_payload)
            await self.prepare_authored(
                db, actor, request.context, entry_id, version_id, subject.subject_id, anchor_id
            )
            await db.commit()
        return await self.thread(actor, thread_id)

    async def reply(
        self, actor: Actor, thread_id: str, request: dto.EntryCreateRequest
    ) -> dto.EntryVersion:
        request_hash = digest(encode({"thread_id": thread_id, **request.model_dump(mode="json")}))
        async with self.factory() as db:
            old = await db.scalar(
                select(rows.FeedbackEntryRow).where(
                    rows.FeedbackEntryRow.author_id == actor.identity,
                    rows.FeedbackEntryRow.creation_key == request.creation_key,
                )
            )
        if old:
            if old.request_hash != request_hash:
                raise FeedbackError(409, "idempotency_conflict", "Reply key has different feedback")
            return await self.entry(actor, old.head_version_id)
        body_payload = await self.payloads.put(actor.identity, {"body": request.body})
        entry_id, version_id = identifier("fbe"), identifier("fbv")
        async with self.factory() as db:
            await self.sources.context(db, actor, request.context)
            thread = await db.get(rows.FeedbackThreadRow, thread_id)
            if thread is None:
                raise FeedbackError(404, "not_found", "Feedback thread is unavailable")
            await self.visible(db, actor, thread)
            await self.sources.authorize(db, actor, thread.subject_id, "comment")
            if thread.hidden or thread.state == "resolved":
                raise FeedbackError(409, "thread_closed", "Reopen the thread before replying")
            if request.reply_to_entry_id:
                parent = await db.get(rows.FeedbackEntryRow, request.reply_to_entry_id)
                if (
                    parent is None
                    or parent.thread_id != thread_id
                    or parent.audience != thread.audience
                ):
                    raise FeedbackError(404, "not_found", "Reply context is unavailable")
            changed = await db.execute(
                update(rows.FeedbackThreadRow)
                .where(
                    rows.FeedbackThreadRow.thread_id == thread_id,
                    rows.FeedbackThreadRow.version == request.expected_thread_version,
                )
                .values(version=request.expected_thread_version + 1)
            )
            if affected(changed) != 1:
                raise FeedbackError(409, "version_conflict", "Feedback thread changed")
            db.add(
                rows.FeedbackEntryRow(
                    entry_id=entry_id,
                    thread_id=thread_id,
                    author_id=actor.identity,
                    author_kind="verified_user" if actor.user_email else "guest_unverified",
                    author_label=actor.label,
                    audience=thread.audience,
                    reply_to_entry_id=request.reply_to_entry_id,
                    version=1,
                    head_version_id=version_id,
                    creation_key=request.creation_key,
                    request_hash=request_hash,
                )
            )
            await db.flush()
            db.add(
                rows.FeedbackEntryVersionRow(
                    entry_version_id=version_id,
                    entry_id=entry_id,
                    version=1,
                    payload_id=body_payload,
                )
            )
            await self.payloads.adopt(db, body_payload)
            await self.prepare_authored(
                db,
                actor,
                request.context,
                entry_id,
                version_id,
                thread.subject_id,
                thread.anchor_id,
            )
            await db.commit()
        return await self.entry(actor, version_id)

    async def edit(
        self, actor: Actor, entry_id: str, request: dto.EntryEditRequest
    ) -> dto.EntryVersion:
        payload = await self.payloads.put(actor.identity, {"body": request.body})
        version_id = identifier("fbv")
        async with self.factory() as db:
            if request.context is not None:
                await self.sources.context(db, actor, request.context)
            entry = await db.get(rows.FeedbackEntryRow, entry_id)
            thread = await db.get(rows.FeedbackThreadRow, entry.thread_id) if entry else None
            if entry is None or thread is None:
                raise FeedbackError(404, "not_found", "Feedback entry is unavailable")
            await self.visible(db, actor, thread)
            await self.sources.authorize(db, actor, thread.subject_id, "comment")
            if entry.author_id != actor.identity:
                raise FeedbackError(403, "feedback_forbidden", "Only the author may edit an entry")
            # Old selected versions remain immutable. Editing makes a new unsent version.
            version = request.expected_version + 1
            changed = await db.execute(
                update(rows.FeedbackEntryRow)
                .where(
                    rows.FeedbackEntryRow.entry_id == entry_id,
                    rows.FeedbackEntryRow.version == request.expected_version,
                )
                .values(version=version, head_version_id=version_id)
            )
            if affected(changed) != 1:
                raise FeedbackError(409, "version_conflict", "Feedback entry changed")
            db.add(
                rows.FeedbackEntryVersionRow(
                    entry_version_id=version_id,
                    entry_id=entry_id,
                    version=version,
                    payload_id=payload,
                )
            )
            await self.payloads.adopt(db, payload)
            await self.prepare_authored(
                db,
                actor,
                request.context,
                entry_id,
                version_id,
                thread.subject_id,
                thread.anchor_id,
            )
            await db.commit()
        return await self.entry(actor, version_id)

    async def agent_answer(
        self,
        *,
        thread_id: str,
        agent_id: str,
        agent_label: str,
        reply: str | None,
        resolve: bool,
        creation_key: str,
    ) -> tuple[str, bool]:
        """Record an agent's answer to a sent comment: a reply, a resolution, or both.

        The caller has already proved the agent's execution received this comment; this only
        writes the entry and state under the agent's own identity, in one transaction.
        Returns the resulting thread state and whether a reply was written.
        """
        identity = f"agent:{agent_id}"
        body_payload = await self.payloads.put(identity, {"body": reply}) if reply else None
        audit_payload = (
            await self.payloads.put(identity, {"state": "resolved", "reason": "Agent answered"})
            if resolve
            else None
        )
        entry_id, version_id = identifier("fbe"), identifier("fbv")
        async with self.factory() as db:
            thread = await db.get(rows.FeedbackThreadRow, thread_id)
            if thread is None or thread.hidden:
                raise FeedbackError(404, "not_found", "Comment is unavailable")
            replied = False
            if body_payload is not None:
                # One answer per call key, so retried tool calls never duplicate replies.
                existing = await db.scalar(
                    select(rows.FeedbackEntryRow).where(
                        rows.FeedbackEntryRow.author_id == identity,
                        rows.FeedbackEntryRow.creation_key == creation_key,
                    )
                )
                if existing is None:
                    first = await db.scalar(
                        select(rows.FeedbackEntryRow.entry_id)
                        .where(rows.FeedbackEntryRow.thread_id == thread_id)
                        .order_by(rows.FeedbackEntryRow.created_at)
                        .limit(1)
                    )
                    db.add(
                        rows.FeedbackEntryRow(
                            entry_id=entry_id,
                            thread_id=thread_id,
                            author_id=identity,
                            author_kind="agent",
                            author_label=agent_label,
                            audience=thread.audience,
                            reply_to_entry_id=first,
                            version=1,
                            head_version_id=version_id,
                            creation_key=creation_key,
                            request_hash=digest(encode({"thread_id": thread_id, "body": reply})),
                        )
                    )
                    await db.flush()
                    db.add(
                        rows.FeedbackEntryVersionRow(
                            entry_version_id=version_id,
                            entry_id=entry_id,
                            version=1,
                            payload_id=body_payload,
                        )
                    )
                    await self.payloads.adopt(db, body_payload)
                    replied = True
            values: dict[str, Any] = {"version": rows.FeedbackThreadRow.version + 1}
            if resolve and thread.state != "resolved":
                values["state"] = "resolved"
                assert audit_payload is not None
                db.add(
                    rows.FeedbackAuditRow(
                        audit_id=identifier("fbh"),
                        subject_id=thread.subject_id,
                        thread_id=thread_id,
                        actor_id=identity,
                        kind="resolution",
                        payload_id=audit_payload,
                    )
                )
                await self.payloads.adopt(db, audit_payload)
            if replied or "state" in values:
                await db.execute(
                    update(rows.FeedbackThreadRow)
                    .where(rows.FeedbackThreadRow.thread_id == thread_id)
                    .values(**values)
                )
            await db.commit()
            state = values.get("state", thread.state)
        return str(state), replied

    async def change_thread(
        self,
        actor: Actor,
        thread_id: str,
        request: dto.ResolutionRequest | dto.ModerationRequest | dto.ReattachRequest,
    ) -> dto.FeedbackThread:
        audit_payload = await self.payloads.put(
            actor.identity, request.model_dump(mode="json", by_alias=True)
        )
        replacement = None
        if isinstance(request, dto.ReattachRequest):
            token = self.tokens.open(
                request.anchor.source_token, kind="source", actor=source_actor(actor)
            )
            subject, evidence = await self.sources.validate_anchor(
                actor, request.anchor, CONTEXT_ADAPTER.validate_python(token["context"])
            )
            anchor_id = identifier("fba")
            replacement = dto.FeedbackAnchor(
                anchor_id=anchor_id,
                source=evidence.source,
                content_digest=evidence.content_digest,
                locator=request.anchor.locator,
            )
            anchor_payload = await self.payloads.put(
                actor.identity, replacement.model_dump(mode="json", by_alias=True)
            )
        async with self.factory() as db:
            thread = await db.get(rows.FeedbackThreadRow, thread_id)
            if thread is None:
                raise FeedbackError(404, "not_found", "Feedback thread is unavailable")
            await self.visible(db, actor, thread)
            _, caps = await self.sources.authorize(db, actor, thread.subject_id)
            if actor.share_id or not (caps.moderate or thread.author_id == actor.identity):
                raise FeedbackError(403, "feedback_forbidden", "Thread moderation is not permitted")
            values: dict[str, Any] = {"version": request.expected_version + 1}
            if isinstance(request, dto.ResolutionRequest):
                values["state"], operation = request.state, "resolution"
            elif isinstance(request, dto.ModerationRequest):
                # Authors may remove their own comment; restoring and hiding others' needs moderation.
                own_removal = request.action == "hide" and thread.author_id == actor.identity
                if not (caps.moderate or own_removal):
                    raise FeedbackError(403, "feedback_forbidden", "Moderation is not permitted")
                values["hidden"], operation = request.action == "hide", "moderation"
            else:
                assert replacement is not None
                old_subject = await db.get(rows.FeedbackSubjectRow, thread.subject_id)
                if (
                    old_subject is None
                    or subject.lineage_id is None
                    or subject.lineage_id != old_subject.lineage_id
                ):
                    raise FeedbackError(
                        422, "invalid_anchor", "Reattachment requires the same document lineage"
                    )
                db.add(
                    rows.FeedbackAnchorRow(
                        anchor_id=replacement.anchor_id,
                        subject_id=subject.subject_id,
                        payload_id=anchor_payload,
                        content_digest=replacement.content_digest,
                        created_by=actor.identity,
                        capture_id=getattr(request.anchor.locator, "capture_id", None),
                    )
                )
                await db.flush()
                # Preserve the original subject/anchor; publish the explicit mapping separately.
                await insert_absent(
                    db,
                    rows.FeedbackAnchorMappingRow,
                    {
                        "original_anchor_id": thread.anchor_id,
                        "target_subject_id": subject.subject_id,
                        "mapped_anchor_id": replacement.anchor_id,
                        "category": "remapped",
                        "payload_id": audit_payload,
                    },
                    ["original_anchor_id", "target_subject_id"],
                )
                operation = "reattach"
                await self.payloads.adopt(db, anchor_payload)
            changed = await db.execute(
                update(rows.FeedbackThreadRow)
                .where(
                    rows.FeedbackThreadRow.thread_id == thread_id,
                    rows.FeedbackThreadRow.version == request.expected_version,
                )
                .values(**values)
            )
            if affected(changed) != 1:
                raise FeedbackError(409, "version_conflict", "Feedback thread changed")
            db.add(
                rows.FeedbackAuditRow(
                    audit_id=identifier("fbh"),
                    subject_id=thread.subject_id,
                    thread_id=thread_id,
                    actor_id=actor.identity,
                    kind=operation,
                    payload_id=audit_payload,
                )
            )
            await self.payloads.adopt(db, audit_payload)
            await db.commit()
        return await self.thread(actor, thread_id)

    async def staging(self, actor: Actor, conversation_id: str) -> dto.StagingSnapshot:
        """Recover prepared/included versions without silently discarding stale items."""
        async with self.factory() as db:
            await self.sources.context(
                db, actor, dto.ChatFeedbackContext(kind="chat", conversation_id=conversation_id)
            )
            scope = await db.get(rows.FeedbackStagingScopeRow, (actor.identity, conversation_id))
            found = (
                await db.execute(
                    select(
                        rows.FeedbackStagingItemRow,
                        rows.FeedbackThreadRow.state,
                        rows.FeedbackThreadRow.hidden,
                    )
                    .join(
                        rows.FeedbackEntryRow,
                        rows.FeedbackEntryRow.entry_id == rows.FeedbackStagingItemRow.entry_id,
                    )
                    .join(
                        rows.FeedbackThreadRow,
                        rows.FeedbackThreadRow.thread_id == rows.FeedbackEntryRow.thread_id,
                    )
                    .where(
                        rows.FeedbackStagingItemRow.owner_email == actor.identity,
                        rows.FeedbackStagingItemRow.conversation_id == conversation_id,
                    )
                    .order_by(rows.FeedbackStagingItemRow.entry_id)
                )
            ).all()
            # Staging is composer state, not durable discussion membership: admission consumes
            # the selected rows. Recover sent subjects from the existing immutable packet items.
            # A version already accepted from any surface (Library, another chat) is not ready
            # again here; editing the comment creates a new version that is.
            delivered = await delivered_versions(
                db, [item.entry_version_id for item, _, _ in found]
            )
            ready = [
                item
                for item, state, hidden in found
                if item.inclusion == "included"
                or (state == "open" and not hidden and item.entry_version_id not in delivered)
            ]
            # Documents made in this chat keep their comments here even when the comments
            # were written elsewhere (Library, direct view); only the owner's subjects count.
            produced = union(
                select(
                    func.coalesce(DeliverableRow.lineage_id, DeliverableRow.deliverable_id)
                ).where(DeliverableRow.conversation_id == conversation_id),
                select(
                    func.coalesce(ArtifactRecordRow.lineage_id, ArtifactRecordRow.artifact_id)
                ).where(ArtifactRecordRow.conversation_id == conversation_id),
            ).subquery()
            commented = set(
                (
                    await db.scalars(
                        select(rows.FeedbackSubjectRow.subject_id)
                        .join(
                            rows.FeedbackThreadRow,
                            rows.FeedbackThreadRow.subject_id == rows.FeedbackSubjectRow.subject_id,
                        )
                        .where(
                            rows.FeedbackSubjectRow.owner_email == actor.identity,
                            rows.FeedbackSubjectRow.lineage_id.in_(select(*produced.c)),
                        )
                        .distinct()
                    )
                ).all()
            )
            sent = set(
                (
                    await db.scalars(
                        select(rows.FeedbackSubmissionItemRow.subject_id)
                        .join(
                            rows.FeedbackSubmissionRow,
                            rows.FeedbackSubmissionRow.submission_id
                            == rows.FeedbackSubmissionItemRow.submission_id,
                        )
                        .where(
                            rows.FeedbackSubmissionRow.sender_email == actor.identity,
                            rows.FeedbackSubmissionRow.delivery == "accepted",
                            (rows.FeedbackSubmissionRow.origin_conversation_id == conversation_id)
                            | (
                                rows.FeedbackSubmissionRow.destination_conversation_id
                                == conversation_id
                            ),
                        )
                        .distinct()
                    )
                ).all()
            )
            return dto.StagingSnapshot(
                conversation_id=conversation_id,
                version=scope.version if scope else 0,
                items=[self.staged_dto(item) for item in ready],
                receipts=[],
                subject_ids=sorted({item.subject_id for item, _, _ in found} | commented | sent),
            )

    async def stage(
        self, actor: Actor, conversation_id: str, entry_id: str, request: dto.StagingUpdateRequest
    ) -> dto.StagedItem:
        async with self.factory() as db:
            await self.sources.context(
                db, actor, dto.ChatFeedbackContext(kind="chat", conversation_id=conversation_id)
            )
            version = await db.get(rows.FeedbackEntryVersionRow, request.entry_version_id)
            entry = await db.get(rows.FeedbackEntryRow, entry_id)
            thread = await db.get(rows.FeedbackThreadRow, entry.thread_id) if entry else None
            if not version or not entry or not thread or version.entry_id != entry_id:
                raise FeedbackError(404, "not_found", "Selected feedback is unavailable")
            await self.visible(db, actor, thread)
            await self.sources.authorize(db, actor, thread.subject_id, "dispatch")
            if entry.head_version_id != version.entry_version_id:
                raise FeedbackError(409, "version_conflict", "Select the current feedback version")
            email = cast(str, actor.user_email)
            expected = request.expected_version
            if expected == 0:
                # The snapshot hides a prepared row whose version was already sent elsewhere;
                # explicitly reselecting it continues from that row instead of conflicting.
                existing = await db.get(
                    rows.FeedbackStagingItemRow, (email, conversation_id, entry_id)
                )
                if (
                    existing is not None
                    and existing.inclusion == "prepared"
                    and await delivered_versions(db, [existing.entry_version_id])
                ):
                    expected = existing.version
            item = await stage_entry(
                db,
                owner_email=email,
                conversation_id=conversation_id,
                entry_id=entry_id,
                entry_version_id=request.entry_version_id,
                subject_id=thread.subject_id,
                anchor_id=thread.anchor_id,
                expected_version=expected,
                inclusion=request.inclusion,
            )
            await db.commit()
            await db.refresh(item)
        return self.staged_dto(item)

    @staticmethod
    def staged_dto(item: rows.FeedbackStagingItemRow) -> dto.StagedItem:
        return dto.StagedItem(
            entry_id=item.entry_id,
            entry_version_id=item.entry_version_id,
            staging_version=item.version,
            inclusion=item.inclusion,
            subject_id=item.subject_id,
            anchor_id=item.anchor_id,
        )

    async def unstage(
        self, actor: Actor, conversation_id: str, entry_id: str, expected: int
    ) -> None:
        async with self.factory() as db:
            await self.sources.context(
                db, actor, dto.ChatFeedbackContext(kind="chat", conversation_id=conversation_id)
            )
            result = await db.execute(
                delete(rows.FeedbackStagingItemRow).where(
                    rows.FeedbackStagingItemRow.owner_email == actor.user_email,
                    rows.FeedbackStagingItemRow.conversation_id == conversation_id,
                    rows.FeedbackStagingItemRow.entry_id == entry_id,
                    rows.FeedbackStagingItemRow.version == expected,
                )
            )
            if affected(result) != 1:
                raise FeedbackError(409, "version_conflict", "Staged feedback changed")
            await db.execute(
                update(rows.FeedbackStagingScopeRow)
                .where(
                    rows.FeedbackStagingScopeRow.owner_email == actor.user_email,
                    rows.FeedbackStagingScopeRow.conversation_id == conversation_id,
                )
                .values(version=rows.FeedbackStagingScopeRow.version + 1)
            )
            await db.commit()

    async def save_draft(
        self, actor: Actor, key: str, request: dto.DraftRequest
    ) -> dto.DraftResponse:
        if actor.user_email is None:
            raise FeedbackError(403, "feedback_forbidden", "Drafts require account authentication")
        payload = await self.payloads.put(
            actor.identity, request.model_dump(mode="json", by_alias=True)
        )
        async with self.factory() as db:
            await self.sources.context(db, actor, request.context)
            old = await db.get(rows.FeedbackDraftRow, (actor.user_email, key))
            if old is None:
                if request.expected_version != 0:
                    raise FeedbackError(409, "version_conflict", "Draft changed")
                db.add(
                    rows.FeedbackDraftRow(
                        owner_email=actor.user_email,
                        draft_key=key,
                        version=1,
                        payload_id=payload,
                    )
                )
                version = 1
            else:
                version = request.expected_version + 1
                changed = await db.execute(
                    update(rows.FeedbackDraftRow)
                    .where(
                        rows.FeedbackDraftRow.owner_email == actor.user_email,
                        rows.FeedbackDraftRow.draft_key == key,
                        rows.FeedbackDraftRow.version == request.expected_version,
                    )
                    .values(version=version, payload_id=payload, updated_at=datetime.now(UTC))
                )
                if affected(changed) != 1:
                    raise FeedbackError(409, "version_conflict", "Draft changed")
            await self.payloads.adopt(db, payload)
            try:
                await db.commit()
            except IntegrityError as exc:
                raise FeedbackError(409, "version_conflict", "Draft changed") from exc
        return dto.DraftResponse(draft_key=key, version=version, draft=request)

    async def draft(self, actor: Actor, key: str) -> dto.DraftResponse:
        async with self.factory() as db:
            row = await db.get(rows.FeedbackDraftRow, (actor.user_email, key))
        if row is None:
            raise FeedbackError(404, "not_found", "Draft is unavailable")
        return dto.DraftResponse(
            draft_key=key,
            version=row.version,
            draft=dto.DraftRequest.model_validate(await self.payloads.get(row.payload_id)),
        )

    async def delete_draft(self, actor: Actor, key: str, expected: int) -> None:
        async with self.factory() as db:
            result = await db.execute(
                delete(rows.FeedbackDraftRow).where(
                    rows.FeedbackDraftRow.owner_email == actor.user_email,
                    rows.FeedbackDraftRow.draft_key == key,
                    rows.FeedbackDraftRow.version == expected,
                )
            )
            if affected(result) != 1:
                raise FeedbackError(409, "version_conflict", "Draft changed")
            await db.commit()
