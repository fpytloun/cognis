from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event

from cognis.api.chat_v2.cached_event_store import CachedSessionEventStore
from cognis.api.chat_v2.event_store import SessionWatermark
from cognis.api.chat_v2.item_snapshot import read_assistant_item_as_of
from cognis.api.chat_v2.schemas import SourceRef
from cognis.api.feedback_sources import FeedbackEventSources
from cognis.core.feedback.primitives import digest
from cognis.core.feedback.submissions import DispatchCommand
from cognis.core.redis_service import RedisService
from cognis.models import feedback as dto
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import Conversation, ProjectGrantRow, ProjectRow, Session
from tests.unit.api.chat_v2.test_cached_event_store import FakeClock, FakeRedis
from tests.unit.api.chat_v2.test_item_snapshot import Reader, identity, message
from tests.unit.test_feedback_runtime import OTHER, OWNER, Harness, _running_packet
from tests.unit.test_feedback_runtime import harness as harness

ROOT = "/api/v1/feedback"
REPRESENTATION = "chat-item-as-of-event-v1"


class CachedHistory(Reader):
    def __init__(self, h: Harness) -> None:
        super().__init__(
            [
                message(1, "First segment."),
                message(2, "Second segment.", delta=True),
                message(3, "Final segment.", delta=True),
            ]
        )
        self.session_id = uuid4().hex
        self.events = [e.model_copy(update={"session_id": self.session_id}) for e in self.events]
        self.h = h
        self.checked_out = 0

        def checkout(*_: Any) -> None:
            self.checked_out += 1

        def checkin(*_: Any) -> None:
            self.checked_out -= 1

        event.listen(h.engine.sync_engine, "checkout", checkout)
        event.listen(h.engine.sync_engine, "checkin", checkin)

    def bind(self, authority: Any) -> CachedHistory:
        assert authority.user_email == OWNER and authority.agent_id == self.h.agent_id
        return self

    async def read_session_events(self, **kwargs: Any) -> Any:
        # Metadata connections must be released before event-store I/O.
        assert self.checked_out == 0
        page = await super().read_session_events(**kwargs)
        events = sorted(page.events, key=lambda entry: entry.seq)
        return page.model_copy(
            update={
                "events": events,
                "session_id": kwargs["session_id"],
                "first_seq": events[0].seq if events else None,
                "last_seq": events[-1].seq if events else None,
                "has_more_after": False,
                "limit": kwargs["limit"],
                "direction": kwargs["direction"],
            }
        )

    async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
        assert session_id == self.session_id
        return SessionWatermark(
            store_id="intaris",
            session_id=session_id,
            last_seq=self.events[-1].seq,
        )


async def setup_history(h: Harness) -> CachedHistory:
    history = CachedHistory(h)
    async with h.factory() as db:
        db.add(
            Session(
                session_id=history.session_id,
                agent_id=h.agent_id,
                user_email=OWNER,
                conversation_id=h.conversation_id,
            )
        )
        await db.commit()
    clock = FakeClock()
    cache = CachedSessionEventStore(
        history,
        cast(RedisService, FakeRedis(clock)),
        "transcript-tests",
        clock=clock,
    )
    h.service.sources.event_loader = FeedbackEventSources(h.factory, cache)
    return history


@pytest.mark.asyncio
async def test_sent_transcript_comments_survive_staging_consumption_and_fresh_reads(
    harness: Harness,
) -> None:
    from cognis.models.tool import ToolCall

    h = harness
    history = await setup_history(h)
    selected = await h.service.sources.resolve(
        h.actor,
        dto.ResolveSourceRequest(
            source=dto.TranscriptSourceRef.model_validate(source(h, history, 3)),
            context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id),
        ),
    )
    receipt, scope, _ = await _running_packet(h, selected)
    sent = await h.service.agents.sent_comments(scope)
    snapshot = await h.service.threads.staging(h.actor, h.conversation_id)
    assert snapshot.items == []  # Sending consumed the composer rows.
    assert selected.subject_id in snapshot.subject_ids

    result = await h.service.agents.handle(
        ToolCall(
            call_id=uuid4().hex,
            name="comment_reply",
            arguments={
                "comment_id": sent[0].entry.thread_id,
                "reply": "Kept the implementation simple.",
                "resolve": True,
            },
        ),
        scope.session_id,
        scope.turn_id,
    )
    assert not result.is_error
    # A fresh HTTP client has no local FeedbackController or cached comment state.
    async with client(h) as c:
        response = await c.get(f"{ROOT}/conversations/{h.conversation_id}/staging")
        assert response.status_code == 200, response.text
        assert selected.subject_id in response.json()["subject_ids"]
        page = await c.get(f"{ROOT}/subjects/{selected.subject_id}/threads")
        assert page.status_code == 200, page.text
        thread = page.json()["items"][0]
        assert thread["state"] == "resolved"
        assert thread["entries"][-1]["body"] == "Kept the implementation simple."
    assert receipt.delivery == "accepted"


def source(h: Harness, history: CachedHistory, seq: int, *, explicit: bool = True) -> dict:
    ref, item_id = identity(history.events[seq - 1])
    value = {
        "kind": "transcript",
        "conversation_id": h.conversation_id,
        "source_event": ref.model_dump(mode="json"),
        "source_item_id": item_id,
        "field_path": ["content"],
    }
    if explicit:
        value["representation"] = REPRESENTATION
    return value


def client(h: Harness, email: str = OWNER) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()),
        base_url="http://feedback.test",
        headers={"x-test-user": email},
    )


async def resolve(c: httpx.AsyncClient, value: dict, context: dict | None = None) -> httpx.Response:
    return await c.post(
        f"{ROOT}/sources/resolve",
        json={
            "source": value,
            "context": context or {"kind": "standalone"},
        },
    )


@pytest.mark.asyncio
async def test_legacy_first_and_delta_bytes_and_explicit_snapshots_are_separate(
    harness: Harness,
) -> None:
    h = harness
    history = await setup_history(h)
    async with client(h) as c:
        for seq in (1, 2, 3):
            old_ref = source(h, history, seq, explicit=False)
            old = await resolve(c, old_ref)
            null = await resolve(c, {**old_ref, "representation": None})
            expected = history.events[seq - 1].data["content"]
            assert old.status_code == null.status_code == 200
            assert old.json()["content_digest"] == digest(expected.encode())
            assert old.json()["source"] == null.json()["source"] == old_ref
            assert old.json()["subject_id"] == null.json()["subject_id"]
            old_text = await c.post(
                f"{ROOT}/sources/text",
                json={
                    "source": old_ref,
                    "context": {"kind": "standalone"},
                    "scope": {"kind": "document"},
                },
            )
            assert old_text.status_code == 200, old_text.text
            assert old_text.json()["text_page"]["text"] == expected
        selected = source(h, history, 3)
        explicit = await resolve(c, selected)
        assert explicit.status_code == 200, explicit.text
        snapshot = explicit.json()
        assert snapshot["content_digest"] == digest(b"First segment.Second segment.Final segment.")
        old_calls = len(history.calls)
        assert (await resolve(c, selected)).json()["content_digest"] == snapshot["content_digest"]
        assert len(history.calls) == old_calls  # Production range cache is warm.
        history.events.append(
            message(4, "Never part of snapshot.", delta=True).model_copy(
                update={"session_id": history.session_id}
            )
        )
        assert (await resolve(c, selected)).json()["content_digest"] == snapshot["content_digest"]
        assert (await resolve(c, source(h, history, 1, explicit=False))).json()[
            "content_digest"
        ] == digest(b"First segment.")


@pytest.mark.asyncio
async def test_http_anchor_and_frozen_manifest_stay_at_selected_event(harness: Harness) -> None:
    h = harness
    history = await setup_history(h)
    selected = source(h, history, 3)
    context = {"kind": "chat", "conversation_id": h.conversation_id}
    text = "First segment.Second segment.Final segment."
    async with client(h) as c:
        loaded = await resolve(c, selected, context)
        assert loaded.status_code == 200, loaded.text
        loaded = loaded.json()
        created = await c.post(
            f"{ROOT}/threads",
            json={
                "creation_key": uuid4().hex,
                "body": "Review the complete assistant message.",
                "context": context,
                "anchor": {
                    "source_token": loaded["source_token"],
                    "content_digest": loaded["content_digest"],
                    "locator": {
                        "kind": "text",
                        "range": {
                            "representation": "feedback-text-v1",
                            "text_digest": loaded["canonical_text"]["text_digest"],
                            "start": 0,
                            "end": len(text),
                            "exact": text,
                        },
                    },
                },
            },
        )
        assert created.status_code == 201, created.text
        entry = created.json()["entries"][0]
        included = await c.put(
            f"{ROOT}/conversations/{h.conversation_id}/staging/{entry['entry_id']}",
            json={
                "expected_version": 1,
                "entry_version_id": entry["entry_version_id"],
                "inclusion": "included",
            },
        )
        assert included.status_code == 200, included.text
        checked = await c.post(
            f"{ROOT}/preflights",
            json={
                "context": context,
                "destination": {"kind": "origin_chat", "conversation_id": h.conversation_id},
                "update_mode": "answer_only",
                "selection": [
                    {
                        "entry_version_id": entry["entry_version_id"],
                        "expected_entry_version": 1,
                        "staging_version": included.json()["staging_version"],
                    }
                ],
            },
        )
        assert checked.status_code == 200, checked.text
        preflight = checked.json()
        submission = await h.service.submissions.prepare(
            h.actor,
            dto.IncludedFeedback(
                creation_key=uuid4().hex,
                preflight_id=preflight["preflight_id"],
                manifest_hash=preflight["manifest_hash"],
            ),
            DispatchCommand(
                content="Use this review.",
                origin_conversation_id=h.conversation_id,
                client_message_id=uuid4().hex,
                client_txn_id=uuid4().hex,
            ),
            standalone=False,
        )
        frozen = (await h.service.submissions.detail(h.actor, submission.submission_id)).manifest
        frozen_bytes = frozen.model_dump_json()
        history.events.append(
            message(4, "Different later replacement.").model_copy(
                update={"session_id": history.session_id}
            )
        )
        assert (await resolve(c, selected, context)).json()["content_digest"] == loaded[
            "content_digest"
        ]
        reread = await h.service.submissions.detail(h.actor, submission.submission_id)
        assert reread.manifest.model_dump_json() == frozen_bytes
        assert reread.manifest.sources[0].source == dto.TranscriptSourceRef.model_validate(selected)


@pytest.mark.asyncio
async def test_as_of_http_rejects_wrong_authority_identity_and_hidden_events(
    harness: Harness,
) -> None:
    h = harness
    history = await setup_history(h)
    selected = source(h, history, 3)
    async with client(h) as c:
        for field, value in (
            ("source_item_id", "different-item"),
            ("conversation_id", "different-conversation"),
            ("field_path", ["tool_result"]),
        ):
            bad = {**selected, field: value}
            assert (await resolve(c, bad)).status_code in (404, 410, 422)
        for field, value in (
            ("session_id", "different-session"),
            ("event_id", "different-event"),
            ("event_type", "tool_result"),
        ):
            bad = copy.deepcopy(selected)
            bad["source_event"][field] = value
            assert (await resolve(c, bad)).status_code in (404, 410)
        hidden = message(4, "Hidden context.").model_copy(
            update={
                "session_id": history.session_id,
                "data": {
                    **message(4, "").data,
                    "content": "Hidden context.",
                    "source": "question_context",
                },
            }
        )
        history.events.append(hidden)
        # Canonical item ID is known from the same assistant identity, not a fabricated text source.
        hidden_ref = copy.deepcopy(selected)
        hidden_ref["source_event"].update(seq=4, event_id="event-4")
        assert (await resolve(c, hidden_ref)).status_code == 410
    async with client(h, OTHER) as c:
        assert (await resolve(c, selected)).status_code == 404


@pytest.mark.asyncio
async def test_warm_source_cache_cannot_outlive_project_grant(harness: Harness) -> None:
    h = harness
    history = await setup_history(h)
    async with h.factory() as db:
        db.add(ProjectRow(project_id="transcript-project", owner_email=OWNER, name="Transcript"))
        await db.flush()
        db.add(
            ProjectGrantRow(
                grant_id="transcript-grant",
                project_id="transcript-project",
                grantee_user_email=OTHER,
                permission="viewer",
                granted_by=OWNER,
            )
        )
        conversation = await db.get(Conversation, h.conversation_id)
        conversation.project_id, conversation.visibility = "transcript-project", "project"
        await db.commit()
    selected = source(h, history, 3)
    async with client(h, OTHER) as c:
        first = await resolve(c, selected)
        assert first.status_code == 200, first.text
        calls = len(history.calls)
        assert (await resolve(c, selected)).status_code == 200
        assert len(history.calls) == calls
        async with h.factory() as db:
            grant = await db.get(ProjectGrantRow, "transcript-grant")
            grant.revoked_at = datetime.now(UTC)
            await db.commit()
        assert (await resolve(c, selected)).status_code == 404
        assert len(history.calls) == calls


@pytest.mark.asyncio
async def test_production_cached_reader_keeps_canonical_snapshot(harness: Harness) -> None:
    h = harness
    history = await setup_history(h)
    selected = source(h, history, 3)
    reader = h.service.sources.event_loader.event_store.bind(
        EventStoreAuthority(
            user_email=OWNER,
            agent_id=h.agent_id,
            agent_owner_email=OWNER,
        )
    )
    result = await read_assistant_item_as_of(
        reader,
        source=SourceRef.model_validate(selected["source_event"]),
        item_id=selected["source_item_id"],
    )
    assert result.content == "First segment.Second segment.Final segment."
