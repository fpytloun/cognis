from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from cognis.core.feedback import threads as threads_module
from cognis.core.feedback.primitives import FeedbackError
from cognis.models import feedback as dto
from cognis.store import feedback_models as rows
from cognis.store.models import DirectTurnRequestRow
from cognis.store.queries import create_conversation
from tests.unit.test_feedback_runtime import OTHER, OWNER, Harness
from tests.unit.test_feedback_runtime import harness as harness

ROOT = "/api/v1/feedback"


def client(h: Harness) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app()),
        base_url="http://feedback.test",
        headers={"x-test-user": OWNER},
    )


def comment(source: dto.ResolvedFeedbackSource) -> dict:
    return {
        "creation_key": uuid4().hex,
        "context": source.context.model_dump(),
        "anchor": {
            "source_token": source.source_token,
            "content_digest": source.content_digest,
            "locator": {"kind": "whole"},
        },
        "body": "Authored in this surface",
    }


async def snapshot(c: httpx.AsyncClient, conversation: str) -> dict:
    response = await c.get(f"{ROOT}/conversations/{conversation}/staging")
    assert response.status_code == 200, response.text
    return response.json()


async def include(c: httpx.AsyncClient, conversation: str, item: dict) -> dict:
    response = await c.put(
        f"{ROOT}/conversations/{conversation}/staging/{item['entry_id']}",
        json={
            "expected_version": item["staging_version"],
            "entry_version_id": item["entry_version_id"],
            "inclusion": "included",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def review(conversation: str, entry: dict, item: dict) -> dict:
    return {
        "context": {"kind": "chat", "conversation_id": conversation},
        "destination": {"kind": "origin_chat", "conversation_id": conversation},
        "selection": [
            {
                "entry_version_id": entry["entry_version_id"],
                "expected_entry_version": entry["version"],
                "staging_version": item["staging_version"],
            }
        ],
        "update_mode": "answer_only",
    }


@pytest.mark.asyncio
async def test_http_comments_prepare_composer_then_one_include_put(harness: Harness) -> None:
    h = harness
    context = dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    source = await h.source(context=context)
    requests = [comment(source) for _ in range(3)]
    async with client(h) as c:
        entries = []
        for payload in requests:
            response = await c.post(f"{ROOT}/threads", json=payload)
            assert response.status_code == 201, response.text
            entries.append(response.json()["entries"][0])
        state = await snapshot(c, h.conversation_id)
        assert state["version"] == 3 and len(state["items"]) == 3
        assert {item["inclusion"] for item in state["items"]} == {"prepared"}
        assert {item["staging_version"] for item in state["items"]} == {1}
        item = next(item for item in state["items"] if item["entry_id"] == entries[0]["entry_id"])
        assert (
            await c.post(f"{ROOT}/preflights", json=review(h.conversation_id, entries[0], item))
        ).status_code == 409
        included = await include(c, h.conversation_id, item)
        assert included["inclusion"] == "included" and included["staging_version"] == 2
        preflight = await c.post(
            f"{ROOT}/preflights", json=review(h.conversation_id, entries[0], included)
        )
        assert preflight.status_code == 200, preflight.text
        before = await snapshot(c, h.conversation_id)
        replay = await c.post(f"{ROOT}/threads", json=requests[0])
        assert replay.status_code == 201
        assert replay.json()["entries"][0] == entries[0]
        assert await snapshot(c, h.conversation_id) == before
    assert not h.scheduler.calls
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(DirectTurnRequestRow)) == 0
        assert await db.scalar(select(func.count()).select_from(rows.FeedbackSubmissionRow)) == 0
        assert await db.scalar(select(func.count()).select_from(rows.FeedbackGrantRow)) == 0


@pytest.mark.asyncio
async def test_explicit_reply_and_edit_context_prepare_only_that_chat(harness: Harness) -> None:
    h = harness
    source = await h.source()
    async with h.factory() as db:
        other_chat = await create_conversation(
            db, agent_id=h.agent_id, user_email=OWNER, context_type="web"
        )
        await db.commit()
    async with client(h) as c:
        created = await c.post(f"{ROOT}/threads", json=comment(source))
        thread = created.json()
        assert (await snapshot(c, h.conversation_id))["items"] == []
        payload = {
            "creation_key": uuid4().hex,
            "expected_thread_version": thread["version"],
            "body": "Reply from chat",
            "context": {"kind": "chat", "conversation_id": h.conversation_id},
        }
        reply = await c.post(f"{ROOT}/threads/{thread['thread_id']}/entries", json=payload)
        assert reply.status_code == 201, reply.text
        entry = reply.json()
        item = (await snapshot(c, h.conversation_id))["items"][0]
        included = await include(c, h.conversation_id, item)
        frozen = await c.post(f"{ROOT}/preflights", json=review(h.conversation_id, entry, included))
        assert frozen.status_code == 200, frozen.text
        before = await snapshot(c, h.conversation_id)
        assert (
            await c.post(f"{ROOT}/threads/{thread['thread_id']}/entries", json=payload)
        ).status_code == 201
        assert await snapshot(c, h.conversation_id) == before

        # An edit explicitly made in another owned chat does not mutate the old scope.
        edit = {
            "expected_version": 1,
            "body": "Edited in the other chat",
            "context": {"kind": "chat", "conversation_id": other_chat.conversation_id},
        }
        edited = await c.put(f"{ROOT}/entries/{entry['entry_id']}", json=edit)
        assert edited.status_code == 200, edited.text
        assert await snapshot(c, h.conversation_id) == before
        other = (await snapshot(c, other_chat.conversation_id))["items"][0]
        assert other["entry_version_id"] == edited.json()["entry_version_id"]
        assert other["inclusion"] == "prepared"
        other = await include(c, other_chat.conversation_id, other)
        after = await snapshot(c, other_chat.conversation_id)
        assert (await c.put(f"{ROOT}/entries/{entry['entry_id']}", json=edit)).status_code == 409
        assert await snapshot(c, other_chat.conversation_id) == after
        # Contextless edits remain valid, non-routing, and preserve frozen inclusions.
        assert (
            await c.put(
                f"{ROOT}/entries/{entry['entry_id']}",
                json={"expected_version": 2, "body": "Contextless"},
            )
        ).status_code == 200
        assert await snapshot(c, other_chat.conversation_id) == after
        assert await snapshot(c, h.conversation_id) == before
        assert (
            await c.post(f"{ROOT}/preflights", json=review(h.conversation_id, entry, included))
        ).status_code == 409
        assert (
            await h.service.threads.entry(h.actor, entry["entry_version_id"])
        ).body == "Reply from chat"
        # A fresh explicit chat edit returns to prepared, never silently included.
        edited = await c.put(
            f"{ROOT}/entries/{entry['entry_id']}",
            json={
                "expected_version": 3,
                "body": "Fresh explicit review",
                "context": payload["context"],
            },
        )
        assert edited.status_code == 200
        item = (await snapshot(c, h.conversation_id))["items"][0]
        assert item["inclusion"] == "prepared" and item["staging_version"] == 3
        assert item["entry_version_id"] == edited.json()["entry_version_id"]
        assert await snapshot(c, other_chat.conversation_id) == after


@pytest.mark.asyncio
async def test_standalone_and_denied_contexts_never_infer_a_chat(harness: Harness) -> None:
    h = harness
    source = await h.source()
    async with h.factory() as db:
        foreign = await create_conversation(
            db, agent_id=h.agent_id, user_email=OTHER, context_type="web"
        )
        await db.commit()
    async with client(h) as c:
        payload = comment(source)
        denied = await c.post(
            f"{ROOT}/threads",
            json={
                **payload,
                "context": {"kind": "chat", "conversation_id": foreign.conversation_id},
            },
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "stale_context"
        denied = await c.post(
            f"{ROOT}/sources/resolve",
            json={
                "source": source.source.model_dump(),
                "context": {"kind": "chat", "conversation_id": foreign.conversation_id},
            },
        )
        assert denied.status_code == 404, denied.text
        denied = await c.post(f"{ROOT}/threads", json=payload, headers={"x-test-user": OTHER})
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "stale_context"
        denied = await c.post(
            f"{ROOT}/sources/resolve",
            headers={"x-test-user": OTHER},
            json={"source": source.source.model_dump(), "context": {"kind": "standalone"}},
        )
        assert denied.status_code == 404
        created = await c.post(f"{ROOT}/threads", json=payload)
        assert created.status_code == 201, created.text
        entry = created.json()["entries"][0]
        assert (await snapshot(c, h.conversation_id))["items"] == []
        assert (
            await c.put(
                f"{ROOT}/entries/{entry['entry_id']}",
                json={
                    "expected_version": 1,
                    "body": "Standalone edit",
                    "context": {"kind": "standalone"},
                },
            )
        ).status_code == 200
        assert (await snapshot(c, h.conversation_id))["items"] == []
        assert (
            await c.put(
                f"{ROOT}/entries/{entry['entry_id']}",
                json={
                    "expected_version": 2,
                    "body": "Not your chat",
                    "context": {"kind": "chat", "conversation_id": foreign.conversation_id},
                },
            )
        ).status_code == 404
        # Original explicit first-stage semantics are not weakened by auto-preparation.
        current = (await h.service.threads.thread(h.actor, created.json()["thread_id"])).entries[0]
        response = await c.put(
            f"{ROOT}/conversations/{h.conversation_id}/staging/{entry['entry_id']}",
            json={
                "expected_version": 0,
                "entry_version_id": current.entry_version_id,
                "inclusion": "included",
            },
        )
        assert response.status_code == 200 and response.json()["inclusion"] == "prepared"
        assert (
            await c.get(
                f"{ROOT}/conversations/{h.conversation_id}/staging",
                headers={"x-test-user": OTHER, "x-test-role": "admin"},
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_guest_edit_contract_cannot_supply_chat_context(harness: Harness) -> None:
    h = harness
    source = await h.source()
    share = await h.service.public.create_share(
        h.actor,
        source.subject_id,
        dto.ShareRequest(
            source=source.source,
            expected_content_digest=source.content_digest,
            allow_comments=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    async with client(h) as c:
        response = await c.post(
            "/api/v1/public/feedback/exchange",
            json={"share_token": share.share_token, "create_guest": True, "display_name": "Guest"},
        )
        exchanged = response.json()
        headers = {
            "X-Cognis-Feedback-Capability": exchanged["capability"],
            "X-Cognis-Feedback-Guest": exchanged["guest_session"]["guest_capability"],
        }
        public = "/api/v1/public/feedback"
        response = await c.post(
            f"{public}/threads",
            headers=headers,
            json={
                "creation_key": uuid4().hex,
                "body": "Guest comment",
                "anchor": {
                    "source_token": exchanged["source"]["source_token"],
                    "content_digest": source.content_digest,
                    "locator": {"kind": "whole"},
                },
            },
        )
        assert response.status_code == 201, response.text
        entry = response.json()["entries"][0]
        response = await c.put(
            f"{public}/entries/{entry['entry_id']}",
            headers=headers,
            json={
                "expected_version": 1,
                "body": "Guest edit",
                "context": {"kind": "chat", "conversation_id": h.conversation_id},
            },
        )
        assert response.status_code == 422
        response = await c.put(
            f"{public}/entries/{entry['entry_id']}",
            headers=headers,
            json={"expected_version": 1, "body": "Guest edit"},
        )
        assert response.status_code == 200, response.text
        assert (await snapshot(c, h.conversation_id))["items"] == []
    async with h.factory() as db:
        assert await db.scalar(select(func.count()).select_from(rows.FeedbackStagingItemRow)) == 0
        assert await db.scalar(select(func.count()).select_from(rows.FeedbackSubmissionRow)) == 0
    assert not h.scheduler.calls


@pytest.mark.asyncio
async def test_preparation_failure_rolls_back_canonical_creation(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    original = threads_module.stage_entry

    async def fail_after_staging(*args, **kwargs):
        await original(*args, **kwargs)
        raise FeedbackError(409, "version_conflict", "Injected transactional conflict")

    monkeypatch.setattr(threads_module, "stage_entry", fail_after_staging)
    async with client(h) as c:
        assert (await c.post(f"{ROOT}/threads", json=comment(source))).status_code == 409
        assert (await snapshot(c, h.conversation_id))["items"] == []
    async with h.factory() as db:
        for model in (
            rows.FeedbackThreadRow,
            rows.FeedbackEntryRow,
            rows.FeedbackEntryVersionRow,
            rows.FeedbackStagingScopeRow,
        ):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_concurrent_creation_key_replay_prepares_once(harness: Harness) -> None:
    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    payload = comment(source)
    async with client(h) as c:
        responses = await asyncio.gather(
            *[c.post(f"{ROOT}/threads", json=payload) for _ in range(2)]
        )
        assert [r.status_code for r in responses] == [201, 201]
        assert responses[0].json()["thread_id"] == responses[1].json()["thread_id"]
        state = await snapshot(c, h.conversation_id)
        assert state["version"] == 1 and len(state["items"]) == 1


@pytest.mark.asyncio
async def test_concurrent_comments_and_include_edit_cas(harness: Harness) -> None:
    h = harness
    source = await h.source(
        context=dto.ChatFeedbackContext(kind="chat", conversation_id=h.conversation_id)
    )
    async with client(h) as c:
        responses = await asyncio.gather(
            *[c.post(f"{ROOT}/threads", json=comment(source)) for _ in range(3)]
        )
        assert [r.status_code for r in responses] == [201, 201, 201], [r.text for r in responses]
        state = await snapshot(c, h.conversation_id)
        assert len(state["items"]) == 3 and state["version"] == 3
        entry = responses[0].json()["entries"][0]
        item = next(i for i in state["items"] if i["entry_id"] == entry["entry_id"])
        response = await asyncio.gather(
            c.put(
                f"{ROOT}/entries/{entry['entry_id']}",
                json={
                    "expected_version": 1,
                    "body": "Concurrent edit",
                    "context": source.context.model_dump(),
                },
            ),
            c.put(
                f"{ROOT}/conversations/{h.conversation_id}/staging/{entry['entry_id']}",
                json={
                    "expected_version": 1,
                    "entry_version_id": entry["entry_version_id"],
                    "inclusion": "included",
                },
            ),
        )
        assert response[0].status_code == 200, response[0].text
        assert response[1].status_code in (200, 409), response[1].text
        state = await snapshot(c, h.conversation_id)
        item = next(i for i in state["items"] if i["entry_id"] == entry["entry_id"])
        assert item["inclusion"] == "prepared"
        assert item["entry_version_id"] == response[0].json()["entry_version_id"]
        # Two clients reviewing the same prepared version cannot both win its CAS.
        payload = {
            "expected_version": item["staging_version"],
            "entry_version_id": item["entry_version_id"],
            "inclusion": "included",
        }
        responses = await asyncio.gather(
            *[
                c.put(
                    f"{ROOT}/conversations/{h.conversation_id}/staging/{entry['entry_id']}",
                    json=payload,
                )
                for _ in range(2)
            ]
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
