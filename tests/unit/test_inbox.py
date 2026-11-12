from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.websocket import WebSocketConnectionManager
from cognis.core.events import Event, EventBus, EventType
from cognis.core.inbox import InboxService, stage_notification
from cognis.core.web_push import WebPushRuntimeConfig, WebPushService
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, User
from cognis.store.queries import (
    create_agent,
    create_executor,
    create_knowledgebase,
    create_project,
    create_user,
)


@pytest.fixture
def inbox_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    with TestClient(create_app()) as client:

        async def seed():
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "recipient@example.com", "other@example.com"):
                    await create_user(session, email=email, name=email, password_hash="test")
                agent = await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared agent",
                    status="active",
                    execution={"executor_id": "owner-executor"},
                )
                await create_executor(
                    session,
                    executor_id="owner-executor",
                    name="Owner executor",
                    owner_email="owner@example.com",
                    executor_type="websocket",
                )
                project = await create_project(
                    session, owner_email="owner@example.com", name="Shared project"
                )
                kb = await create_knowledgebase(
                    session, owner_email="owner@example.com", name="Shared knowledge"
                )
                await session.commit()
                return agent.agent_id, project.project_id, kb.knowledgebase_id

        ids = client.portal.call(seed)
        client.app.state.knowledgebase_service.enabled = True
        yield client, ids


def headers(client, email: str):
    token = client.app.state.auth_provider.sign_access_token(email, email, "user")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("resource", ["agent", "project", "knowledgebase"])
def test_sharing_lifecycle_is_durable_and_recipient_scoped(inbox_client, resource):
    client, ids = inbox_client
    owner = headers(client, "owner@example.com")
    recipient = headers(client, "recipient@example.com")
    other = headers(client, "other@example.com")
    if resource == "agent":
        url = f"/api/v1/agents/{ids[0]}/shares"
        payload = {"grantee_email": "recipient@example.com", "executor_scope": "owner_executor"}
        share = client.post(url, headers=owner, json=payload)
    elif resource == "project":
        url = f"/api/v1/projects/{ids[1]}/grants"
        payload = {
            "grantee_type": "user",
            "grantee_user_email": "recipient@example.com",
            "permission": "member",
        }
        share = client.post(url, headers=owner, json=payload)
    else:
        url = f"/api/v1/knowledgebases/{ids[2]}/shares"
        payload = {"user_email": "recipient@example.com"}
        share = client.put(url, headers=owner, json=payload)
    assert share.status_code in (200, 201), share.text
    first = client.get("/api/v1/inbox", headers=recipient).json()
    assert first["unread_count"] == 1
    assert first["items"][0]["created_at"].endswith("Z")
    assert first["items"][0]["kind"] == f"sharing.{resource}.shared"
    assert client.get("/api/v1/inbox", headers=owner).json()["items"] == []
    assert client.get("/api/v1/inbox", headers=other).json()["items"] == []
    notification_id = first["items"][0]["notification_id"]
    # Neither acknowledgement nor cursor lookup can cross the recipient boundary.
    assert (
        client.post(
            "/api/v1/inbox/read", headers=other, json={"notification_ids": [notification_id]}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/inbox", headers=recipient).json()["unread_count"] == 1
    assert client.get(f"/api/v1/inbox?cursor={notification_id}", headers=other).status_code == 400
    assert (
        client.post(
            "/api/v1/inbox/read", headers=recipient, json={"notification_ids": [notification_id]}
        ).status_code
        == 200
    )
    read_at = client.get("/api/v1/inbox", headers=recipient).json()["items"][0]["read_at"]
    client.post(
        "/api/v1/inbox/read", headers=recipient, json={"notification_ids": [notification_id]}
    )
    assert client.get("/api/v1/inbox", headers=recipient).json()["items"][0]["read_at"] == read_at
    if resource == "knowledgebase":
        assert client.put(url, headers=owner, json=payload).status_code == 200
        revoke_url = f"{url}/recipient@example.com"
    else:
        grant_id = share.json()["grant_id"]
        revoke_url = f"{url}/{grant_id}"
        if resource == "agent":
            assert client.post(url, headers=owner, json=payload).status_code == 201
            update = {"executor_scope": "grantee_executor"}
        else:
            assert client.post(url, headers=owner, json=payload).status_code == 409
            update = {"permission": "manager"}
        # Concurrent replay of the same state must emit one durable transition.
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda _: client.patch(revoke_url, headers=owner, json=update), range(2))
            )
        assert [response.status_code for response in responses] == [200, 200]
        assert client.get("/api/v1/inbox", headers=recipient).json()["unread_count"] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.delete(revoke_url, headers=owner), range(2)))
    assert any(response.status_code == 200 for response in responses)
    after = client.get("/api/v1/inbox", headers=recipient).json()
    assert after["items"][0]["kind"] == f"sharing.{resource}.revoked"
    assert after["items"][0]["href"] == "/notifications"
    assert len(after["items"]) == (2 if resource == "knowledgebase" else 3)
    # Removed access remains denied even though the inbox preserves the history.
    path = {
        "agent": f"/api/v1/agents/{ids[0]}",
        "project": f"/api/v1/projects/{ids[1]}",
        "knowledgebase": f"/api/v1/knowledgebases/{ids[2]}",
    }[resource]
    assert client.get(path, headers=recipient).status_code in (403, 404)
    assert client.get("/api/v1/inbox").status_code == 401


def test_agent_tool_concurrent_share_replay(inbox_client):
    from cognis.core.agent_management import (
        AgentManagementDependencies,
        handle_agent_management_action,
    )

    client, ids = inbox_client

    async def exercise():
        deps = AgentManagementDependencies(
            session_factory=client.app.state.session_factory, event_bus=client.app.state.event_bus
        )
        arguments = {
            "action": "share_create",
            "agent_id": ids[0],
            "grantee_email": "recipient@example.com",
        }

        async def share():
            return await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id=ids[0],
                arguments=arguments,
                self_mutation_approved=True,
            )

        results = await asyncio.gather(share(), share())
        assert all(result["status"] == "shared" for result in results)

    asyncio.run(exercise())
    assert (
        client.get("/api/v1/inbox", headers=headers(client, "recipient@example.com")).json()[
            "unread_count"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_transaction_rollback_pagination_and_push(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/inbox.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    bus = EventBus()
    service = InboxService(factory, bus)
    events = []

    async def record(event):
        events.append(event)

    bus.subscribe_all(record)
    try:
        async with factory() as session:
            session.add(User(email="recipient@example.com", name="Recipient", role="user"))
            await session.commit()
        async with factory() as session:
            stage_notification(
                session,
                user_email="recipient@example.com",
                kind="test",
                title="Rolled back",
                body="Not committed",
                href="/notifications",
            )
            await session.rollback()
        assert (await service.list_page("recipient@example.com")).items == []
        assert events == []
        async with factory() as session:
            rows = [
                stage_notification(
                    session,
                    user_email="recipient@example.com",
                    kind="test",
                    title=str(i),
                    body="Account activity",
                    href="/notifications",
                )
                for i in range(4)
            ]
            # Exercise deterministic tie-breaking for identical creation timestamps.
            for row in rows:
                row.created_at = datetime.now(UTC).replace(microsecond=0)
            await session.commit()
        await service.publish(rows[0])
        assert [e.type for e in events] == [EventType.INBOX_CREATED, EventType.INBOX_CHANGED] or [
            e.type for e in events
        ] == [EventType.INBOX_CHANGED, EventType.INBOX_CREATED]
        first = await service.list_page("recipient@example.com", limit=2)
        second = await service.list_page("recipient@example.com", limit=2, cursor=first.next_cursor)
        assert len({item.notification_id for item in first.items + second.items}) == 4
        assert second.next_cursor is None
        assert first.unread_count == 4
        push = WebPushService(
            session_factory=factory,
            event_bus=EventBus(),
            config=WebPushRuntimeConfig(enabled=False, public_key="", private_key="", subject=""),
        )
        event = Event(
            type=EventType.INBOX_CREATED,
            data={"user_email": rows[0].user_email, "notification_id": rows[0].notification_id},
        )
        payload = await push._event_payload(event)
        assert payload["kind"] == "inbox" and payload["url"] == "/notifications"
        assert "actions" not in payload
        await service.mark_read("recipient@example.com", [rows[0].notification_id])
        assert await push._event_payload(event) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_and_remote_inbox_fanout_only_matches_recipient():
    signals = SimpleNamespace(owner_token_matches=lambda email, token: token == f"token:{email}")
    app = SimpleNamespace(state=SimpleNamespace(cluster_signals=signals))
    manager = WebSocketConnectionManager(app)
    manager._by_user = {"recipient@example.com": set(), "other@example.com": set()}
    manager.send_to_user = AsyncMock()
    await manager._handle_event(
        Event(type=EventType.INBOX_CHANGED, data={"user_email": "recipient@example.com"})
    )
    manager.send_to_user.assert_awaited_once_with(
        "recipient@example.com", {"type": "inbox_changed"}
    )
    manager.send_to_user.reset_mock()
    await manager._handle_cluster_scope_invalidated(
        Event(
            type=EventType.CLUSTER_SCOPE_INVALIDATED,
            data={
                "kind": "inbox_changed",
                "scope": {"owner_token": "token:recipient@example.com"},
                "revision": "1",
            },
        )
    )
    manager.send_to_user.assert_awaited_once_with(
        "recipient@example.com", {"type": "inbox_changed"}
    )
