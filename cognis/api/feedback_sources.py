"""Canonical event-projection adapter for the transport-neutral feedback source service."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.chat_v2.event_store import (
    SessionEventStore,
    SessionHistoryUnavailableError,
    require_session_history,
)
from cognis.api.chat_v2.item_snapshot import ItemSnapshotUnavailable, read_assistant_item_as_of
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.schemas import SourceRef, TimelineItem, ToolCallTimelineItem
from cognis.core.feedback.primitives import FeedbackError, encode
from cognis.core.feedback.sources import SourceEvidence, canonical_text
from cognis.core.project_access import can_read_conversation
from cognis.models import feedback as dto
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import Agent, Conversation, Session


class FeedbackEventSources:
    """Resolve only the exact authorized event/item, never a current filesystem file."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], event_store: Any) -> None:
        self.factory, self.event_store = factory, event_store

    async def tool_provenance(self, session_id: str, turn_id: str | None, call_id: str) -> bool:
        async with self.factory() as db:
            session = await db.get(Session, session_id)
            agent = await db.get(Agent, session.agent_id) if session else None
            if session is None or agent is None:
                return False
        reader = self.event_store.bind(
            EventStoreAuthority(
                user_email=session.user_email,
                agent_id=session.agent_id,
                agent_owner_email=agent.owner_email,
            )
        )
        before = None
        for _ in range(20):
            page = await reader.read_session_events(
                session_id=session_id, before_seq=before, limit=500, direction="backward"
            )
            require_session_history(page.availability, from_seq=1)
            if not page.events:
                return False
            items = project_timeline(normalize_session_events(page.events).events).timeline.items
            if any(
                isinstance(item, ToolCallTimelineItem)
                and item.call_id == call_id
                and item.turn_id == turn_id
                for item in items
            ):
                return True
            before = min(event.seq for event in page.events)
            if before <= 1:
                return False
        return False

    async def __call__(self, email: str, source: dto.FeedbackSourceRef) -> SourceEvidence:
        if source.kind == "document":
            raise FeedbackError(422, "invalid_source", "An event source is required")
        ref = source.source_event
        if ref.store != "intaris":
            raise FeedbackError(422, "invalid_source", "Unsupported canonical event store")
        async with self.factory() as db:
            session = await db.get(Session, ref.session_id)
            conversation = await db.get(Conversation, session.conversation_id) if session else None
            agent = await db.get(Agent, session.agent_id) if session else None
            if (
                session is None
                or agent is None
                or conversation is None
                or conversation.status == "deleted"
                or not await can_read_conversation(db, conversation, email)
                or (
                    source.kind == "transcript"
                    and source.conversation_id != conversation.conversation_id
                )
                or (
                    source.kind == "work_diff"
                    and source.activity_scope_id != session.activity_scope_id
                )
            ):
                raise FeedbackError(404, "not_found", "Canonical feedback source is unavailable")
        reader = self.event_store.bind(
            EventStoreAuthority(
                user_email=session.user_email,
                agent_id=session.agent_id,
                agent_owner_email=agent.owner_email,
            )
        )
        item: TimelineItem
        if source.kind == "transcript" and source.representation is not None:
            if source.field_path != ["content"]:
                raise FeedbackError(422, "invalid_source", "Select canonical assistant content")
            try:
                item = await read_assistant_item_as_of(
                    reader,
                    source=SourceRef(**ref.model_dump()),
                    item_id=source.source_item_id,
                )
            except (ItemSnapshotUnavailable, SessionHistoryUnavailableError) as exc:
                raise FeedbackError(
                    410, "source_unavailable", "Canonical item snapshot is unavailable"
                ) from exc
        else:
            item = await self._single_event(reader, source)
        return self._evidence(source, item, conversation, session)

    async def _single_event(
        self, reader: SessionEventStore, source: dto.TranscriptSourceRef | dto.WorkDiffSourceRef
    ) -> TimelineItem:
        ref = source.source_event
        page = await reader.read_session_events(
            session_id=ref.session_id,
            before_seq=ref.seq + 1,
            limit=1,
            direction="backward",
        )
        require_session_history(page.availability, from_seq=ref.seq, to_seq=ref.seq)
        event = next((item for item in page.events if item.seq == ref.seq), None)
        if event is None:
            raise FeedbackError(410, "source_unavailable", "Exact event is no longer available")
        event_id = getattr(event, "event_id", None)
        if ref.event_id and event_id != ref.event_id:
            raise FeedbackError(422, "invalid_source", "Canonical event identity does not match")
        timeline = project_timeline(normalize_session_events([event]).events).timeline
        item = next((item for item in timeline.items if item.id == source.source_item_id), None)
        if item is None:
            raise FeedbackError(410, "source_unavailable", "Exact source item is unavailable")
        matching = [
            r for r in item.source_refs if r.session_id == ref.session_id and r.seq == ref.seq
        ]
        if not matching or not any(r.event_type == ref.event_type for r in matching):
            raise FeedbackError(422, "invalid_source", "Canonical source reference does not match")
        return item

    def _evidence(
        self,
        source: dto.TranscriptSourceRef | dto.WorkDiffSourceRef,
        item: TimelineItem,
        conversation: Conversation,
        session: Session,
    ) -> SourceEvidence:
        common: dict[str, Any] = dict(
            source=source,
            owner_email=conversation.user_email,
            project_id=conversation.project_id,
            producer_conversation_id=conversation.conversation_id,
            producer_agent_id=session.agent_id,
        )
        if source.kind == "transcript":
            # Field traversal is JSON-only, bounded, and never accesses object attributes.
            value: Any = item.model_dump(mode="json")
            if not source.field_path or source.field_path[0] not in {
                "content",
                "summary",
                "text",
                "output",
                "result",
                "arguments",
                "result_preview",
                "arguments_preview",
            }:
                raise FeedbackError(422, "invalid_source", "Select an explicit visible text field")
            try:
                for key in source.field_path:
                    value = value[int(key)] if isinstance(value, list) else value[key]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise FeedbackError(422, "invalid_source", "Source field is unavailable") from exc
            if not isinstance(value, str):
                raise FeedbackError(422, "invalid_source", "Select a visible text field")
            text = canonical_text(value)
            return SourceEvidence(
                **common,
                data=value.encode(),
                text=text,
                format="transcript",
                title="Transcript selection",
                locator_kinds=["whole", "text"],
            )
        if not isinstance(item, ToolCallTimelineItem) or source.file_ordinal >= len(
            item.file_diffs
        ):
            raise FeedbackError(410, "source_unavailable", "Exact file evidence is unavailable")
        diff = item.file_diffs[source.file_ordinal]
        if (
            diff.path != source.path
            or diff.old_path != source.old_path
            or diff.root_id != source.root_id
        ):
            raise FeedbackError(422, "invalid_source", "Exact file identity does not match")
        partial = diff.binary or diff.truncated or diff.content_truncated or diff.preview_omitted
        return SourceEvidence(
            **common,
            data=encode(
                {
                    "diff": diff.diff,
                    "path": diff.path,
                    "old_path": diff.old_path,
                    "binary": diff.binary,
                    "truncated": partial,
                }
            ),
            text=canonical_text(diff.diff) if not partial else None,
            format="diff",
            title=source.path,
            availability="partial" if partial else "available",
            locator_kinds=["whole", "diff"],
        )
