from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

from fastapi.testclient import TestClient

import cognis.api.routes.conversations as conversations_routes
from cognis.api.app import create_app
from cognis.api.routes.conversations import (
    _CHAT_LAST_OPENED_GLOBAL_STATE_KEY,
    _remember_chat_last_opened,
)
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    create_project,
    create_session,
    create_user,
    get_conversation,
    get_user_ui_state_value,
    update_conversation_active_session,
    update_managed_conversation_link,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user_and_agent(app: object, *, email: str = "user@example.com") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name=email.split("@")[0].title(),
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-chat",
            owner_email=email,
            name="Agent",
            status="active",
        )
        await session.commit()


def _assert_sidebar_upsert_call(
    send_sidebar_update: AsyncMock,
    conversation_id: str,
) -> None:
    send_sidebar_update.assert_awaited()
    args = send_sidebar_update.await_args.args
    kwargs = send_sidebar_update.await_args.kwargs
    assert args == (
        conversation_id,
        {"type": "sidebar_conversation_upsert", "conversation_id": conversation_id},
    )
    assert kwargs == {"include_subscribers": True}


def test_mark_read_emits_user_wide_unread_clear_once(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_to_user = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_to_user=send_to_user)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Unread conversation",
                )
                last_message_at = datetime.now(UTC)
                conversation.last_message_at = last_message_at
                conversation.last_read_at = last_message_at - timedelta(minutes=1)
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        response = client.post(f"/api/v1/conversations/{conversation_id}/read", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        send_to_user.assert_awaited_once()
        user_email, payload = send_to_user.await_args.args  # type: ignore[union-attr]
        assert user_email == "user@example.com"
        assert payload["type"] == "conversation_updated"
        assert payload["conversation_id"] == conversation_id
        assert payload["has_unread"] is False
        assert isinstance(payload["last_read_at"], str)
        assert isinstance(payload["last_message_at"], str)

        async def _last_read_at() -> datetime | None:
            async with app.state.session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                return conversation.last_read_at if conversation else None

        stored_last_read_at = asyncio.run(_last_read_at())
        assert stored_last_read_at is not None
        assert (
            datetime.fromisoformat(payload["last_read_at"]).replace(tzinfo=None)
            == stored_last_read_at
        )

        response = client.post(f"/api/v1/conversations/{conversation_id}/read", headers=headers)

        assert response.status_code == 200
        send_to_user.assert_awaited_once()

        assert send_to_user.await_args_list == [call("user@example.com", payload)]


def test_create_conversation_emits_sidebar_upsert(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_sidebar_update = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_sidebar_update_to_owner=send_sidebar_update)
        asyncio.run(_seed_user_and_agent(app))
        headers = _auth_headers(app, email="user@example.com")

        response = client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"agent_id": "agent-chat", "title": "Created from REST"},
        )

        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]
        _assert_sidebar_upsert_call(send_sidebar_update, conversation_id)


def test_sidebar_projects_open_managed_work_and_active_delegations(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> tuple[str, str, str]:
            await _seed_user_and_agent(app)
            async with app.state.session_factory() as session:
                controller = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Controller",
                )
                parent = await create_session(
                    session,
                    controller.conversation_id,
                    "user@example.com",
                    "agent-chat",
                    session_id="sess_parent",
                )
                controller.active_session_id = parent.session_id
                await session.flush()
                await create_session(
                    session,
                    controller.conversation_id,
                    "user@example.com",
                    "agent-chat",
                    session_id="sess_delegate",
                    parent_session_id=parent.session_id,
                    activity_scope_id=parent.activity_scope_id,
                    delegation_mode="delegate",
                    delegation_task="Inspect active work",
                )
                target = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="agent_work",
                    title="Managed target",
                )
                link = await create_managed_conversation_link(
                    session,
                    user_email="user@example.com",
                    controller_agent_id="agent-chat",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id=parent.session_id,
                    target_agent_id="agent-chat",
                    target_conversation_id=target.conversation_id,
                    target_session_id=None,
                    title="Managed target",
                )
                await session.commit()
                return controller.conversation_id, target.conversation_id, link.link_id

        controller_id, target_id, link_id = asyncio.run(_seed())
        scheduler_durable_running = app.state.turn_scheduler.durable_running_turn_state
        scheduler_durable_running_many = app.state.turn_scheduler.durable_running_turn_states

        async def _durable_running_turn_state(conversation_id: str):
            if conversation_id == target_id:
                return {"turn_id": "turn_live"}
            return await scheduler_durable_running(conversation_id)

        app.state.turn_scheduler.durable_running_turn_state = _durable_running_turn_state

        async def _durable_running_turn_states(conversation_ids: list[str]):
            states = await scheduler_durable_running_many(conversation_ids)
            if target_id in conversation_ids:
                states[target_id] = {"turn_id": "turn_live"}
            return states

        app.state.turn_scheduler.durable_running_turn_states = _durable_running_turn_states
        response = client.get(
            "/api/v1/conversations/sidebar",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        background_work = response.json()["background_work"]
        assert background_work["active_count"] == 2
        assert background_work["truncated"] is False
        assert {
            (item["kind"], item["controller_conversation_id"], item["status"])
            for item in background_work["items"]
        } == {
            ("delegated_session", controller_id, "active"),
            ("managed_conversation", controller_id, "running"),
        }
        managed = next(
            item for item in background_work["items"] if item["kind"] == "managed_conversation"
        )
        assert managed["target_conversation_id"] == target_id

        async def _leave_stale_running_state() -> None:
            async with app.state.session_factory() as session:
                await update_managed_conversation_link(
                    session,
                    link_id,
                    turn_state="running",
                    active_turn_id="turn_stale",
                )
                await session.commit()

        asyncio.run(_leave_stale_running_state())
        app.state.turn_scheduler.durable_running_turn_state = scheduler_durable_running
        app.state.turn_scheduler.durable_running_turn_states = scheduler_durable_running_many
        idle_response = client.get(
            "/api/v1/conversations/sidebar",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert idle_response.status_code == 200
        idle_background_work = idle_response.json()["background_work"]
        assert idle_background_work["active_count"] == 1
        idle_managed = next(
            item for item in idle_background_work["items"] if item["kind"] == "managed_conversation"
        )
        assert idle_managed["status"] == "active"

        async def _settle_cancelled_turn() -> None:
            async with app.state.session_factory() as session:
                await update_managed_conversation_link(
                    session,
                    link_id,
                    turn_state="interrupted",
                    clear_active_turn_id=True,
                    last_error="The turn was cancelled",
                )
                await session.commit()

        asyncio.run(_settle_cancelled_turn())
        cancelled_response = client.get(
            "/api/v1/conversations/sidebar",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert cancelled_response.status_code == 200
        cancelled_managed = next(
            item
            for item in cancelled_response.json()["background_work"]["items"]
            if item["kind"] == "managed_conversation"
        )
        assert cancelled_managed["status"] == "cancelled"


def test_update_conversation_title_star_archive_and_project_emit_sidebar_upserts(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_sidebar_update = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_sidebar_update_to_owner=send_sidebar_update)

        async def _seed() -> tuple[str, str]:
            await _seed_user_and_agent(app)
            async with app.state.session_factory() as session:
                project = await create_project(
                    session,
                    owner_email="user@example.com",
                    name="Project",
                    project_id="proj-rest",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Before",
                )
                await session.commit()
                return conversation.conversation_id, project.project_id

        conversation_id, project_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        mutations = [
            {"title": "After"},
            {"starred_at": "2026-01-01T00:00:00+00:00"},
            {"archived": True},
            {"archived": False},
            {"project_id": project_id},
        ]
        for payload in mutations:
            response = client.patch(
                f"/api/v1/conversations/{conversation_id}",
                headers=headers,
                json=payload,
            )
            assert response.status_code == 200

        assert send_sidebar_update.await_count == len(mutations)
        for awaited in send_sidebar_update.await_args_list:
            assert awaited.args == (
                conversation_id,
                {"type": "sidebar_conversation_upsert", "conversation_id": conversation_id},
            )
            assert awaited.kwargs == {"include_subscribers": True}


def test_delete_and_purge_conversation_emit_sidebar_removal(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_to_user = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_to_user=send_to_user)
        app.state.providers.guardrails.delete_session = AsyncMock()

        async def _seed() -> tuple[str, str]:
            await _seed_user_and_agent(app)
            async with app.state.session_factory() as session:
                soft_deleted = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Soft delete",
                )
                purged = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Purge",
                )
                await session.commit()
                return soft_deleted.conversation_id, purged.conversation_id

        soft_deleted_id, purged_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        delete_response = client.delete(f"/api/v1/conversations/{soft_deleted_id}", headers=headers)
        purge_response = client.delete(f"/api/v1/conversations/{purged_id}/purge", headers=headers)

        assert delete_response.status_code == 200
        assert purge_response.status_code == 200
        assert send_to_user.await_args_list == [
            call(
                "user@example.com",
                {"type": "sidebar_conversation_removed", "conversation_id": soft_deleted_id},
            ),
            call(
                "user@example.com",
                {"type": "sidebar_conversation_removed", "conversation_id": purged_id},
            ),
        ]


def test_managed_fork_emits_sidebar_upsert_for_new_conversation(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_sidebar_update = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_sidebar_update_to_owner=send_sidebar_update)
        app.state.providers.guardrails.record_events = AsyncMock(
            return_value=SimpleNamespace(ok=True, count=1, first_seq=1, last_seq=1)
        )
        app.state.turn_scheduler = SimpleNamespace(
            active_turn_id=lambda _conversation_id: None,
            running_turn_state=lambda _conversation_id: None,
        )

        async def _seed() -> str:
            await _seed_user_and_agent(app, email="owner@example.com")
            async with app.state.session_factory() as session:
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Controller",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-chat",
                    context_type="agent_work",
                    context_data={"kind": "agent_work", "target_agent_id": "agent-chat"},
                    title="Managed target",
                )
                target_session = await create_session(
                    session,
                    target.conversation_id,
                    "owner@example.com",
                    "agent-chat",
                    session_id="target-session",
                    intaris_session_id="target-session",
                )
                await update_conversation_active_session(
                    session, target.conversation_id, target_session.session_id
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="agent-chat",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="agent-chat",
                    target_conversation_id=target.conversation_id,
                    target_session_id=target_session.session_id,
                    title="Managed target",
                )
                await session.commit()
                return target.conversation_id

        target_conversation_id = asyncio.run(_seed())

        async def _fork_into_new_conversation(**_kwargs: object) -> tuple[object, object, bool]:
            from cognis.core.session import _to_conversation_model, _to_session_model

            async with app.state.session_factory() as session:
                fork = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-chat",
                    context_type="agent_work",
                    title="Managed target (fork)",
                )
                fork_session = await create_session(
                    session,
                    fork.conversation_id,
                    "owner@example.com",
                    "agent-chat",
                    session_id="fork-session",
                    intaris_session_id="fork-session",
                )
                await update_conversation_active_session(
                    session, fork.conversation_id, "fork-session"
                )
                await session.commit()
                return _to_conversation_model(fork), _to_session_model(fork_session), True

        app.state.session_manager.fork_into_new_conversation = AsyncMock(
            side_effect=_fork_into_new_conversation
        )
        headers = _auth_headers(app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{target_conversation_id}/managed/fork",
            headers=headers,
            json={},
        )

        assert response.status_code == 200
        forked_conversation_id = response.json()["conversation_id"]
        _assert_sidebar_upsert_call(send_sidebar_update, forked_conversation_id)


def test_conversation_deliverable_detail_is_owner_scoped(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email.split("@")[0].title(),
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="owner@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Deliverable conversation",
                )
                await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="owner@example.com",
                    agent_id="agent-chat",
                    session_id="sess_deliverable_route",
                )
                deliverable = await create_deliverable(
                    session,
                    conversation_id=conversation.conversation_id,
                    session_id="sess_deliverable_route",
                    turn_id="turn-deliverable",
                    content="Fallback content",
                    format="rich",
                    title="Rich report",
                    target=None,
                    outputs={},
                    rich={"blocks": [{"type": "markdown", "content": "# Report"}]},
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()
                return deliverable.deliverable_id

        deliverable_id = asyncio.run(_seed())

        owner_response = client.get(
            f"/api/v1/deliverables/{deliverable_id}",
            headers=_auth_headers(app, email="owner@example.com"),
        )
        other_response = client.get(
            f"/api/v1/deliverables/{deliverable_id}",
            headers=_auth_headers(app, email="other@example.com"),
        )

        assert owner_response.status_code == 200
        assert owner_response.json()["deliverable_id"] == deliverable_id
        assert owner_response.json()["rich_payload"]["blocks"][0]["content"] == "# Report"
        assert other_response.status_code == 404


def test_conversation_deliverable_detail_allows_a_managed_descendant_from_its_controller(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-controller",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-child",
                    owner_email="owner@example.com",
                    name="Child",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-controller",
                    context_type="web",
                )
                child = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-child",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="agent-controller",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="sess-controller",
                    target_agent_id="agent-child",
                    target_conversation_id=child.conversation_id,
                    target_session_id="sess-child",
                    title="Child",
                )
                deliverable = await create_deliverable(
                    session,
                    conversation_id=child.conversation_id,
                    turn_id="turn-child",
                    content="Child deliverable",
                    title="Child result",
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()
                return controller.conversation_id, deliverable.deliverable_id

        controller_conversation_id, deliverable_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="owner@example.com")

        unscoped_response = client.get(
            f"/api/v1/deliverables/{deliverable_id}",
            headers=headers,
        )
        controller_response = client.get(
            f"/api/v1/deliverables/{deliverable_id}",
            params={"accessor_conversation_id": controller_conversation_id},
            headers=headers,
        )
        view_response = client.get(
            f"/api/v1/deliverables/{deliverable_id}/view",
            params={"accessor_conversation_id": controller_conversation_id},
            headers=headers,
        )

        assert unscoped_response.status_code == 404
        assert controller_response.status_code == 200
        assert controller_response.json()["deliverable_id"] == deliverable_id
        assert controller_response.json()["content"] == "Child deliverable"
        assert view_response.status_code == 200


def test_slash_command_suggestions_route_returns_dispatcher_items(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Suggestions conversation",
                )
                await create_session(
                    session,
                    session_id="sess-suggestions",
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    status="active",
                )
                conversation.active_session_id = "sess-suggestions"
                await session.commit()
                return conversation.conversation_id

        class _Dispatcher:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def suggest(self, command_input: str, **kwargs: object) -> list[object]:
                self.calls.append({"command_input": command_input, **kwargs})
                return [
                    SimpleNamespace(
                        kind="parameter",
                        command="/skill",
                        value="cognis-coding",
                        label="Cognis Coding",
                        description="Coding guidance",
                        insert_text="/skill cognis-coding",
                        suffix="none",
                        badges=["loaded"],
                    )
                ]

        dispatcher = _Dispatcher()
        app.state.command_dispatcher = dispatcher
        conversation_id = asyncio.run(_seed())

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/slash-command-suggestions",
            params={"input": "/skill cog", "limit": 5},
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == {
            "items": [
                {
                    "kind": "parameter",
                    "command": "/skill",
                    "value": "cognis-coding",
                    "label": "Cognis Coding",
                    "description": "Coding guidance",
                    "insert_text": "/skill cognis-coding",
                    "suffix": "none",
                    "badges": ["loaded"],
                }
            ]
        }
        assert dispatcher.calls[0]["command_input"] == "/skill cog"
        assert dispatcher.calls[0]["limit"] == 5


def test_conversation_detail_can_skip_legacy_state_snapshot(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Lightweight detail",
                )
                await session.commit()
                return conversation.conversation_id

        snapshot_for_conversation = AsyncMock(return_value=None)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            conversations_routes,
            "snapshot_for_conversation",
            snapshot_for_conversation,
        )
        conversation_id = asyncio.run(_seed())

        response = client.get(
            f"/api/v1/conversations/{conversation_id}",
            params={"include_state": "false"},
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["conversation_id"] == conversation_id
        assert payload["conversation_state"] is None
        snapshot_for_conversation.assert_not_awaited()


def test_conversation_open_can_skip_legacy_state_snapshot(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Lightweight open",
                )
                await session.commit()

        snapshot_for_conversation = AsyncMock(return_value=None)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            conversations_routes,
            "snapshot_for_conversation",
            snapshot_for_conversation,
        )
        asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            json={
                "agent_id": "agent-chat",
                "context_type": "web",
                "include_state": False,
            },
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["agent_id"] == "agent-chat"
        assert payload["conversation_state"] is None
        snapshot_for_conversation.assert_not_awaited()


# ---------------------------------------------------------------------------
# Global last-opened key tests (Issue 3 — PWA conversation-first restore)
# ---------------------------------------------------------------------------


def test_remember_chat_last_opened_writes_global_key(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """_remember_chat_last_opened must write the agent-agnostic global key."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _run() -> dict[str, Any] | None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("pw"),
                    role="user",
                )
                await _remember_chat_last_opened(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-a",
                    context_type="web",
                    agent_profile_id=None,
                    conversation_id="conv-123",
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                await session.commit()
                return await get_user_ui_state_value(
                    session, "user@example.com", _CHAT_LAST_OPENED_GLOBAL_STATE_KEY
                )

        state = asyncio.run(_run())
        assert state is not None
        assert state["conversation_id"] == "conv-123"
        assert state["agent_id"] == "agent-a"
        assert state["context_type"] == "web"


def test_open_conversation_global_key_fallback(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """open_conversation must restore the last-opened conversation via the global key
    even when the request's agent_id differs from the conversation's agent."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("pw"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-a",
                    owner_email="user@example.com",
                    name="Agent A",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-b",
                    owner_email="user@example.com",
                    name="Agent B",
                    status="active",
                )
                # Conversation belongs to agent-b
                conv = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-b",
                    context_type="web",
                    title="Last opened",
                )
                # Write global key pointing at agent-b's conversation
                await _remember_chat_last_opened(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-b",
                    context_type="web",
                    agent_profile_id=None,
                    conversation_id=conv.conversation_id,
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                await session.commit()
                return conv.conversation_id, "agent-a"

        conv_id, requesting_agent = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        # Request with agent-a — no agent-a conversations exist, but the global
        # key points at agent-b's conversation. The endpoint should return it.
        response = client.post(
            "/api/v1/conversations/open",
            json={"agent_id": requesting_agent, "context_type": "web"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == conv_id


# ---------------------------------------------------------------------------
# Compaction projection tests (Issue 4B — rotation/context_seed markers)
# ---------------------------------------------------------------------------
