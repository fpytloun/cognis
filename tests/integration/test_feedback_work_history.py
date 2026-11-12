from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionHistoryAvailability,
)
from cognis.api.chat_v2.normalizer import normalize_session_events
from cognis.api.chat_v2.projector import project_timeline
from cognis.api.chat_v2.work_file_projector import rebuild_session_current_files
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import Session, WorkRecordFileRow, WorkRecordRow, WorkSessionProjectionRow
from tests.integration.test_work_live_api import _auth_headers, _seed_conversation, _seed_identity
from tests.integration.test_work_live_api import work_api as work_api

OWNER = "owner@example.com"
SESSION = "canonical-history-session"
CONVERSATION = "canonical-history"


def seed_history(
    client: TestClient,
    change: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Retain actual tool-result event shapes; let production projection bind sources."""
    _seed_identity(client)
    _seed_conversation(client, conversation_id=CONVERSATION, session_id=SESSION)
    selected = {
        "path": "/srv/repo/native.md",
        "diff": "@@ -1 +1 @@\n-old\n+new",
        **(change or {}),
    }
    events = {
        seq: RawSessionEvent(
            store_id="intaris",
            session_id=SESSION,
            seq=seq,
            type="tool_result",
            event_id=f"retained-result-{seq}",
            data={
                "call_id": f"retained-call-{seq}",
                "name": "apply_patch",
                "status": "complete",
                "file_diffs": [
                    {"path": "/srv/repo/other.md", "diff": "+another ordinal"},
                    {**selected, "diff": selected["diff"] + f"\n+edit-{seq}"},
                ],
            },
        )
        for seq in (7, 8)
    }

    async def seed() -> str:
        async with client.app.state.session_factory() as db:
            session = await db.get(Session, SESSION)
            assert session is not None and session.activity_scope_id
            # The retained source is this real metadata session, not a fabricated alias.
            session.intaris_session_id = SESSION
            projection = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == SESSION,
                )
            )
            assert projection is not None
            projection.covered_through_seq = projection.target_seq = 8
            for seq, event in events.items():
                item = project_timeline(normalize_session_events([event]).events).timeline.items[0]
                db.add(
                    WorkRecordRow(
                        work_record_id=f"retained-record-{seq}",
                        owner_email=OWNER,
                        session_id=SESSION,
                        source_store="intaris",
                        source_session_id=SESSION,
                        source_seq=seq,
                        source_event_id=event.event_id,
                        source_item_id=item.id,
                        item_ordinal=0,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        occurred_at=datetime.now(UTC),
                        record_type=item.kind,
                        timeline_item=item.model_dump(mode="json"),
                        is_evidence=True,
                    )
                )
                await db.flush()
                db.add(
                    WorkRecordFileRow(
                        work_record_file_id=f"retained-file-{seq}",
                        owner_email=OWNER,
                        session_id=SESSION,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        work_record_id=f"retained-record-{seq}",
                        file_ordinal=1,
                        path=selected["path"],
                        path_id="repo:native.md",
                        path_generation_id="",
                        source_seq=seq,
                        item_ordinal=0,
                        additions=2,
                        deletions=1,
                        status="modified",
                        binary=bool(selected.get("binary")),
                        truncated=bool(selected.get("truncated")),
                        preview_omitted=bool(selected.get("preview_omitted")),
                    )
                )
            await db.flush()
            await rebuild_session_current_files(
                db,
                owner_email=OWNER,
                session_id=SESSION,
                materializer_version=WORK_MATERIALIZER_VERSION,
            )
            generation = await db.scalar(
                select(WorkRecordFileRow.path_generation_id).where(
                    WorkRecordFileRow.work_record_file_id == "retained-file-8",
                )
            )
            await db.commit()
            assert generation
            return generation

    assert client.portal is not None
    generation = client.portal.call(seed)

    async def read_events(*, session_id: str, before_seq: int, **_kwargs: Any) -> SessionEventPage:
        assert session_id == SESSION
        event = events.get(before_seq - 1)
        return SessionEventPage(
            store_id="intaris",
            session_id=SESSION,
            events=[event] if event else [],
            availability=SessionHistoryAvailability(durable_last_seq=8, first_available_seq=1),
        )

    authorized = client.app.state.cached_event_store.bind(
        EventStoreAuthority(user_email=OWNER, agent_id=f"agent-{OWNER}", agent_owner_email=OWNER)
    )
    reader = SimpleNamespace(
        authority_token=authorized.authority_token,
        read_session_events=AsyncMock(side_effect=read_events),
    )
    client.app.state.cached_event_store.bind = Mock(return_value=reader)
    return {
        "scope": {
            "key": f"conversation:{CONVERSATION}",
            "kind": "conversation",
            "conversation_id": CONVERSATION,
        },
        "path_generation_id": generation,
        "limit": 20,
    }, reader


def test_retained_history_saves_a_multiline_anchor_on_one_exact_side(work_api: TestClient) -> None:
    prefix = "@@ -1,2 +1,2 @@\n keep\n-old"
    payload, _reader = seed_history(work_api, {"diff": prefix})
    history = work_api.post(
        "/api/v1/work/file-history", headers=_auth_headers(work_api), json=payload
    )
    assert history.status_code == 200, history.text
    source = history.json()["items"][0]["feedback_source"]
    context = {"kind": "standalone"}
    response = work_api.post(
        "/api/v1/feedback/sources/resolve",
        headers=_auth_headers(work_api),
        json={"source": source, "context": context},
    )
    assert response.status_code == 200, response.text
    resolved = response.json()
    seq = source["source_event"]["seq"]
    locator = {
        "kind": "diff",
        "scope": "lines",
        "side": "new",
        "start_line": 1,
        "end_line": 2,
        "hunk_digest": hashlib.sha256(f"{prefix}\n+edit-{seq}".encode()).hexdigest(),
        "quote": f"keep\nedit-{seq}",
    }
    request = {
        "creation_key": str(uuid.uuid4()),
        "context": context,
        "anchor": {
            "source_token": resolved["source_token"],
            "content_digest": resolved["content_digest"],
            "locator": locator,
        },
        "body": "Review the complete changed range.",
    }
    created = work_api.post(
        "/api/v1/feedback/threads", headers=_auth_headers(work_api), json=request
    )
    assert created.status_code in (200, 201), created.text
    assert created.json()["anchor"]["locator"] == locator
    request["creation_key"] = str(uuid.uuid4())
    locator["side"] = "old"
    rejected = work_api.post(
        "/api/v1/feedback/threads", headers=_auth_headers(work_api), json=request
    )
    assert rejected.status_code == 422, rejected.text


def test_retained_history_http_survives_projection_and_resolves_exact_sources(
    work_api: TestClient,
) -> None:
    payload, reader = seed_history(work_api)
    aggregate = work_api.get(
        f"/api/v1/chat/v2/conversations/{CONVERSATION}/work?category=files",
        headers=_auth_headers(work_api),
    )
    assert aggregate.status_code == 200, aggregate.text
    aggregate_diff = aggregate.json()["mutations"][0]["file_diffs"][0]
    assert aggregate_diff["feedback_source"] is None
    assert aggregate_diff["preview_omitted"] and aggregate_diff["content_truncated"]
    reader.read_session_events.assert_not_awaited()

    response = work_api.post(
        "/api/v1/work/file-history",
        headers=_auth_headers(work_api),
        json=payload,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2
    sources = [item["feedback_source"] for item in items]
    assert {source["source_event"]["seq"] for source in sources} == {7, 8}
    for item, source in zip(items, sources, strict=True):
        seq = source["source_event"]["seq"]
        assert source["file_ordinal"] == 1
        assert source["path"] == item["path"] == "/srv/repo/native.md"
        assert source["root_id"] is None  # Display-root rewriting is not canonical identity.
        assert source["source_item_id"] == f"tool:retained-call-{seq}"
        assert source["source_event"] == {
            "store": "intaris",
            "session_id": SESSION,
            "seq": seq,
            "event_id": f"retained-result-{seq}",
            "event_type": "tool_result",
        }
        resolved = work_api.post(
            "/api/v1/feedback/sources/resolve",
            headers=_auth_headers(work_api),
            json={"source": source, "context": {"kind": "standalone"}},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["availability"] == "available"
        assert resolved.json()["source"] == source

    source = sources[0]
    invalid = [
        ({**source, "activity_scope_id": "another-activity"}, 404),
        ({**source, "source_event": {**source["source_event"], "event_id": "another-event"}}, 422),
        ({**source, "source_event": {**source["source_event"], "event_type": "tool_call"}}, 422),
        ({**source, "source_item_id": "tool:another-call"}, 410),
        ({**source, "file_ordinal": 0}, 422),
        ({**source, "file_ordinal": 2}, 410),
        ({**source, "path": "/srv/repo/another.md"}, 422),
        ({**source, "old_path": "/srv/repo/another.md"}, 422),
        ({**source, "root_id": "another-root"}, 422),
    ]
    for changed, status in invalid:
        denied = work_api.post(
            "/api/v1/feedback/sources/resolve",
            headers=_auth_headers(work_api),
            json={"source": changed, "context": {"kind": "standalone"}},
        )
        assert denied.status_code == status, denied.text
    _seed_identity(work_api, "other@example.com")
    for role in ("user", "admin"):
        token = work_api.app.state.auth_provider.sign_access_token(
            "other@example.com", "Other", role
        )
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            work_api.post("/api/v1/work/file-history", headers=headers, json=payload).status_code
            == 404
        )
        assert (
            work_api.post(
                "/api/v1/feedback/sources/resolve",
                headers=headers,
                json={"source": source, "context": {"kind": "standalone"}},
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "change",
    [
        {"binary": True},
        {"truncated": True},
        {"preview_omitted": True},
        {"path": "/srv/repo/.env"},
        {"diff": "+line\n" * 6_000},
    ],
)
def test_history_http_never_returns_authority_for_ineligible_previews(
    work_api: TestClient,
    change: dict[str, Any],
) -> None:
    payload, _reader = seed_history(work_api, change)
    response = work_api.post(
        "/api/v1/work/file-history",
        headers=_auth_headers(work_api),
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2
    assert all(item["feedback_source"] is None for item in response.json()["items"])
