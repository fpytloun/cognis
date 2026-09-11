from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import select

from cognis.api.app import create_app
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.middleware import AuthenticatedUser
from cognis.api.models import TaskCommentCreateRequest, TaskCreateRequest
from cognis.api.routes.sessions import _token_usage_for_session
from cognis.api.routes.tasks import (
    _continuation_context_event,
    _task_board_progress_summaries,
    _task_final_deliverable_content,
    task_create,
)
from cognis.api.websocket import (
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    _handle_resolve_escalation,
    _handle_step_response,
)
from cognis.core.agent_direct import AGENT_DIRECT_KIND, agent_direct_context_ref
from cognis.core.agent_loop import PendingPause
from cognis.core.task_queue import TaskRerunResult
from cognis.models.search import SearchMatch, SearchSessionMatch, SearchSessionsResponse
from cognis.models.session import (
    EventReadResult,
    IntarisAgentSummaryRecord,
    IntarisSession,
    IntarisSessionSummaries,
    IntarisSessionSummaryRecord,
)
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState
from cognis.store.models import (
    Agent,
    NotificationRow,
    StepRun,
    WorkCurrentFileRow,
    WorkSessionProjectionRow,
)
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_managed_conversation_link,
    create_session,
    create_skill,
    create_skill_asset,
    create_skill_version,
    create_step_run,
    create_task,
    create_user,
    delete_system_agent_override,
    get_conversation,
    get_managed_conversation_link_for_target,
    get_task,
    get_user_ui_state_value,
    list_step_runs_for_task,
    list_task_comments,
    revoke_agent_grant,
    set_session_intaris_session_id,
    set_session_status,
    touch_conversation,
    update_agent_grant,
    update_conversation_active_session,
    update_managed_conversation_link,
    upsert_system_agent_override,
)


def _strip_order_key(item: dict[str, object]) -> dict[str, object]:
    """Return a copy of a projected timeline item without the orderKey field.

    Tests that assert exact item shapes should use this helper so they remain
    stable when the orderKey encoding changes.
    """
    return {k: v for k, v in item.items() if k != "orderKey"}


def test_session_detail_normalizes_cached_provider_token_usage() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_cache=SimpleNamespace(
                    get_context_usage=lambda session_id: {
                        "last_llm_usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 30,
                            "total_tokens": 150,
                        }
                    }
                )
            )
        )
    )

    usage = _token_usage_for_session(request, "session-1")

    assert usage is not None
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 30
    assert usage.total_tokens == 150


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_viewer_cannot_create_task(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="viewer@example.com",
                    name="Viewer",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="viewer",
                )
                await session.commit()

        client.portal.call(_seed)
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(client.app, email="viewer@example.com", role="viewer"),
            json={"agent_id": "agent-1", "title": "Do work"},
        )
        assert response.status_code == 403


def test_managed_conversation_queue_mutations_are_read_only(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id="target-session",
                    title="Target",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())
        turn_scheduler = SimpleNamespace(
            get_queued_messages=AsyncMock(return_value=[]),
            submit_turn=AsyncMock(),
            update_queued_message=AsyncMock(),
            cancel_queued_message=AsyncMock(),
        )
        client.app.state.turn_scheduler = turn_scheduler
        headers = _auth_headers(client.app, email="owner@example.com")

        get_response = client.get(f"/api/v1/conversations/{conversation_id}/queue", headers=headers)
        patch_response = client.patch(
            f"/api/v1/conversations/{conversation_id}/queue/q-1",
            headers=headers,
            json={"content": "edited"},
        )
        delete_response = client.delete(
            f"/api/v1/conversations/{conversation_id}/queue/q-1",
            headers=headers,
        )
        send_response = client.put(
            f"/api/v1/chat/v2/conversations/{conversation_id}/messages/txn-managed-send",
            headers=headers,
            json={"client_message_id": "cmsg-managed-send", "content": "direct target send"},
        )

        assert get_response.status_code == 200
        assert patch_response.status_code == 409
        assert patch_response.json()["error"]["code"] == "managed_conversation_read_only"
        assert delete_response.status_code == 409
        assert delete_response.json()["error"]["code"] == "managed_conversation_read_only"
        assert send_response.status_code == 409
        assert send_response.json()["error"]["code"] == "managed_conversation_read_only"
        turn_scheduler.submit_turn.assert_not_awaited()
        turn_scheduler.update_queued_message.assert_not_awaited()
        turn_scheduler.cancel_queued_message.assert_not_awaited()


def test_task_control_conversation_cannot_be_archived_deleted_or_purged(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> tuple[str, str]:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    context_type="web",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Persistent control task",
                    status="running",
                )
                task.control_conversation_id = conversation.conversation_id
                await session.commit()
                return task.task_id, conversation.conversation_id

        task_id, conversation_id = asyncio.run(_seed())
        owner_headers = _auth_headers(client.app, email="owner@example.com")
        admin_headers = _auth_headers(client.app, email="admin@example.com")

        archive_response = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
            json={"archived": True},
        )
        delete_response = client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        purge_response = client.delete(
            f"/api/v1/conversations/{conversation_id}/purge",
            headers=owner_headers,
        )
        admin_delete_response = client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=admin_headers,
        )

        for response in (archive_response, delete_response, purge_response):
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "task_control_conversation_persistent"
        assert admin_delete_response.status_code == 403

        async def _verify() -> None:
            async with client.app.state.session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                task = await get_task(session, task_id)
                assert conversation is not None
                assert conversation.status == "active"
                assert task is not None
                assert task.control_conversation_id == conversation_id

        asyncio.run(_verify())


async def _seed_api_managed_conversation(
    client: TestClient,
    *,
    turn_state: str,
    conversation_state: str = "open",
    active_turn_id: str | None = None,
) -> str:
    async with client.app.state.session_factory() as session:
        await create_user(
            session,
            email="owner@example.com",
            name="Owner",
            password_hash=client.app.state.password_hasher.hash("password123"),
            role="user",
        )
        await create_agent(
            session,
            agent_id="controller-agent",
            owner_email="owner@example.com",
            name="Controller",
            status="active",
        )
        await create_agent(
            session,
            agent_id="target-agent",
            owner_email="owner@example.com",
            name="Target",
            status="active",
        )
        controller = await create_conversation(
            session,
            user_email="owner@example.com",
            agent_id="controller-agent",
            context_type="web",
        )
        target = await create_conversation(
            session,
            user_email="owner@example.com",
            agent_id="target-agent",
            context_type="agent_work",
        )
        link = await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="controller-agent",
            controller_conversation_id=controller.conversation_id,
            controller_session_id="controller-session",
            target_agent_id="target-agent",
            target_conversation_id=target.conversation_id,
            target_session_id="target-session",
            title="Target",
            turn_state=turn_state,
        )
        if conversation_state != "open" or active_turn_id is not None:
            await update_managed_conversation_link(
                session,
                link.link_id,
                conversation_state=conversation_state,
                active_turn_id=active_turn_id,
            )
        await session.commit()
        return target.conversation_id


def test_managed_conversation_send_rejects_active_turn(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(_seed_api_managed_conversation(client, turn_state="running"))
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: True,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/send",
            headers=headers,
            json={"message": "manual instruction"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "active_turn_running"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_rejects_closed_conversation(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(
            _seed_api_managed_conversation(
                client,
                turn_state="interrupted",
                conversation_state="closed",
            )
        )
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "closed"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_rejects_active_turn(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(
            _seed_api_managed_conversation(
                client,
                turn_state="failed",
                active_turn_id="turn-active",
            )
        )
        submit_turn = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: True,
            submit_turn=submit_turn,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "active_turn_running"
        submit_turn.assert_not_awaited()


def test_managed_conversation_retry_preserves_one_shot_mode(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        conversation_id = asyncio.run(_seed_api_managed_conversation(client, turn_state="failed"))
        submit_turn = AsyncMock(return_value=None)
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
            active_turn_id=lambda _conversation_id: "turn-retry",
            submit_turn=submit_turn,
        )
        client.app.state.session_cache = SimpleNamespace(
            get_events_since_compaction=lambda *_args, **_kwargs: []
        )
        client.app.state.providers.guardrails.read_events = AsyncMock(
            return_value=EventReadResult(
                events=[
                    {
                        "type": "user_message",
                        "data": {
                            "content": "retry this in build mode",
                            "chat_mode": "build",
                            "chat_mode_source": "one_shot",
                        },
                    }
                ],
                last_seq=1,
                has_more=False,
            )
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/retry",
            headers=headers,
            json={},
        )

        assert response.status_code == 200
        submit_turn.assert_awaited_once()
        assert submit_turn.await_args.args == (
            conversation_id,
            "retry this in build mode",
        )
        assert submit_turn.await_args.kwargs["user_email"] == "owner@example.com"
        assert submit_turn.await_args.kwargs["one_shot_chat_mode"] == "build"
        assert submit_turn.await_args.kwargs["turn_id"].startswith("turn_")


def test_managed_conversation_stop_uses_stop_dispatcher_and_marks_manual_cancel(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id="target-session",
                    title="Target",
                    turn_state="running",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())
        stop_conversation = AsyncMock(return_value=True)
        client.app.state.command_dispatcher = SimpleNamespace(stop_conversation=stop_conversation)
        client.app.state.turn_scheduler = SimpleNamespace(
            running_turn_state=lambda _conversation_id: None,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/stop",
            headers=headers,
            json={"reason": "manual stop"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        stop_conversation.assert_awaited_once_with(
            conversation_id,
            user_email="owner@example.com",
        )

        async def _load_metadata() -> dict[str, object]:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                assert link is not None
                return link.control_metadata or {}

        control_metadata = asyncio.run(_load_metadata())
        assert control_metadata["cancelled_by_user"] is True
        assert control_metadata["cancel_source"] == "managed_ui"

        async def _mark_idle() -> None:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                assert link is not None
                await update_managed_conversation_link(
                    session,
                    link.link_id,
                    turn_state="idle",
                )
                await session.commit()

        asyncio.run(_mark_idle())

        async def _submit_managed_turn(
            _conversation_id: str,
            _message: str,
            **kwargs: object,
        ) -> None:
            await kwargs["admission_observer"](str(kwargs["turn_id"]), False)

        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
            active_turn_id=lambda _conversation_id: None,
            submit_turn=AsyncMock(side_effect=_submit_managed_turn),
        )

        send_response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/send",
            headers=headers,
            json={"message": "continue"},
        )

        assert send_response.status_code == 200
        control_metadata = asyncio.run(_load_metadata())
        assert "cancelled_by_user" not in control_metadata
        assert "cancel_source" not in control_metadata
        assert "cancelled_at" not in control_metadata


def test_managed_conversation_take_control_creates_normal_fork_and_closes_link(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                    context_data={"kind": "agent_work", "target_agent_id": "target-agent"},
                    title="Managed target",
                )
                target_session = await create_session(
                    session,
                    target.conversation_id,
                    "owner@example.com",
                    "target-agent",
                    session_id="target-session",
                    intaris_session_id="target-session",
                )
                await update_conversation_active_session(
                    session,
                    target.conversation_id,
                    target_session.session_id,
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id=target_session.session_id,
                    title="Managed target",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = asyncio.run(_seed_managed_conversation())

        async def _fork_into_normal_conversation(**_kwargs: object) -> tuple[object, object, bool]:
            from cognis.core.session import _to_conversation_model, _to_session_model

            async with client.app.state.session_factory() as session:
                follow_up = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="web",
                    title="Follow-up: Managed target",
                )
                follow_up_session = await create_session(
                    session,
                    follow_up.conversation_id,
                    "owner@example.com",
                    "target-agent",
                    session_id="follow-up-session",
                    intaris_session_id="follow-up-session",
                )
                await update_conversation_active_session(
                    session,
                    follow_up.conversation_id,
                    follow_up_session.session_id,
                )
                await session.commit()
                return (
                    _to_conversation_model(follow_up),
                    _to_session_model(follow_up_session),
                    True,
                )

        client.app.state.session_manager.fork_into_new_conversation = AsyncMock(
            side_effect=_fork_into_normal_conversation
        )
        client.app.state.providers.guardrails.record_events = AsyncMock(
            return_value=SimpleNamespace()
        )
        client.app.state.session_cache.append_recorded_events = AsyncMock()
        client.app.state.turn_scheduler = SimpleNamespace(
            has_active_turn=lambda _conversation_id: False,
            running_turn_state=lambda _conversation_id: None,
        )
        headers = _auth_headers(client.app, email="owner@example.com")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/managed/take-control",
            headers=headers,
            json={},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "taken_over"
        follow_up_conversation_id = body["result"]["conversation_id"]
        assert follow_up_conversation_id

        async def _load_state() -> tuple[str, dict[str, object], str]:
            async with client.app.state.session_factory() as session:
                link = await get_managed_conversation_link_for_target(
                    session,
                    conversation_id,
                    user_email="owner@example.com",
                )
                follow_up = await get_conversation(session, follow_up_conversation_id)
                assert link is not None
                assert follow_up is not None
                return link.conversation_state, link.control_metadata or {}, follow_up.context_type

        state, control_metadata, follow_up_context_type = asyncio.run(_load_state())
        assert state == "closed"
        assert control_metadata["follow_up_conversation_id"] == follow_up_conversation_id
        assert control_metadata["closed_reason"] == "taken_over_by_user"
        assert follow_up_context_type == "web"


def test_session_intaris_detail_prefers_intaris_summary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session,
                    session_row.session_id,
                    "intaris-session-1",
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        original_guardrails = app.state.providers.guardrails
        original_client = original_guardrails.client

        class _Guardrails:
            client = original_client

            async def get_session(self, session_id: str) -> IntarisSession:
                assert session_id == "intaris-session-1"
                return IntarisSession(
                    session_id=session_id,
                    user_id="user@example.com",
                    agent_id="agent-1",
                    title="Intaris title",
                    intention="Intaris intention",
                    status="active",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:01:00Z",
                )

            async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
                assert session_id == "intaris-session-1"
                return IntarisSessionSummaries(
                    intaris_summaries=[
                        IntarisSessionSummaryRecord(
                            id="summary-1",
                            session_id=session_id,
                            window_start="2026-01-01T00:00:00Z",
                            window_end="2026-01-01T00:01:00Z",
                            trigger="manual",
                            summary="Latest Intaris summary",
                            intent_alignment="aligned",
                            call_count=3,
                            created_at="2026-01-01T00:01:00Z",
                        )
                    ],
                    agent_summaries=[
                        IntarisAgentSummaryRecord(
                            id="agent-summary-1",
                            session_id=session_id,
                            summary="Agent summary",
                            created_at="2026-01-01T00:00:30Z",
                        )
                    ],
                )

        app.state.providers.guardrails = _Guardrails()
        app.state.session_cache.get_last_generation_performance = lambda requested_id: {
            "is_local": True,
            "model": "qwen3:8b",
            "runtime": "Ollama",
            "generation_tokens_per_second": 25,
            "measured_at": "2026-07-13T12:00:00Z",
        }
        response = client.get(
            f"/api/v1/sessions/{session_id}/intaris",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["intaris_session_id"] == "intaris-session-1"
        assert body["intention"] == "Intaris intention"
        assert body["summary"] == "Latest Intaris summary"
        assert body["last_generation"]["model"] == "qwen3:8b"
        assert body["last_generation"]["generation_tokens_per_second"] == 25


def test_session_intaris_detail_falls_back_without_summary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return session_row.session_id

        session_id = asyncio.run(_seed())

        original_guardrails = app.state.providers.guardrails
        original_client = original_guardrails.client

        class _Guardrails:
            client = original_client

            async def get_session(self, session_id: str) -> IntarisSession:
                return IntarisSession(
                    session_id=session_id,
                    user_id="user@example.com",
                    agent_id="agent-1",
                    title=None,
                    intention="Fallback intention",
                    status="active",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:01:00Z",
                )

            async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
                raise RuntimeError("summary endpoint unavailable")

        app.state.providers.guardrails = _Guardrails()
        response = client.get(
            f"/api/v1/sessions/{session_id}/intaris",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["intention"] == "Fallback intention"
        assert body["summary"] is None


def test_session_local_detail_is_owner_scoped_and_does_not_call_intaris(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                for email in ("owner@example.com", "viewer@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await create_agent(
                    session,
                    agent_id="owner-agent",
                    owner_email="owner@example.com",
                    name="Owner agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="owner-agent",
                    context_type="web",
                )
                row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="owner@example.com",
                    agent_id="owner-agent",
                    delegation_mode="delegate",
                    delegation_task="Nested review",
                )
                await session.commit()
                return row.session_id

        session_id = asyncio.run(_seed())
        get_session = AsyncMock(side_effect=AssertionError("local detail called Intaris"))
        app.state.providers.guardrails.get_session = get_session

        owned = client.get(
            f"/api/v1/sessions/{session_id}",
            headers=_auth_headers(app, email="owner@example.com"),
        )
        denied = client.get(
            f"/api/v1/sessions/{session_id}",
            headers=_auth_headers(app, email="viewer@example.com"),
        )

        assert owned.status_code == 200
        assert owned.json()["delegation_task"] == "Nested review"
        assert denied.status_code == 403
        get_session.assert_not_awaited()


def test_batch_submit_returns_per_item_results(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task_one = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Draft one",
                    status="draft",
                )
                task_two = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Draft two",
                    status="draft",
                )
                await session.commit()
                return task_one.task_id, task_two.task_id

        task_one, task_two = asyncio.run(_seed())
        submitted_task_ids: list[str] = []

        async def _batch_submit(task_ids: list[str]) -> dict[str, object]:
            submitted_task_ids.extend(task_ids)
            results = [
                {
                    "task_id": task_id,
                    "status": "submitted" if task_id != "missing-task" else "error",
                    "error": None if task_id != "missing-task" else "Task not found",
                }
                for task_id in task_ids
            ]
            return {
                "results": results,
                "succeeded": sum(item["status"] == "submitted" for item in results),
                "failed": sum(item["status"] == "error" for item in results),
            }

        monkeypatch.setattr(app.state.task_queue, "batch_submit", _batch_submit)  # type: ignore[attr-defined]
        response = client.post(
            "/api/v1/tasks/batch-submit",
            headers=_auth_headers(app, email="user@example.com"),
            json={"task_ids": [task_one, task_two, "missing-task"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        assert submitted_task_ids == [task_one, task_two, "missing-task"]
        assert [item["task_id"] for item in body["results"]] == submitted_task_ids
        assert any(
            item["task_id"] == "missing-task" and item["status"] == "error"
            for item in body["results"]
        )


def test_task_list_uses_keyset_cursor(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                now = datetime.now(UTC)
                for index in range(3):
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id="agent-1",
                        title=f"Task {index}",
                        status="queued",
                    )
                    task.task_id = f"cursor-task-{index}"
                    task.updated_at = now - timedelta(minutes=index)
                await session.commit()

        asyncio.run(_seed())

        first = client.get(
            "/api/v1/tasks?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["task_id"] for item in first_body["items"]] == [
            "cursor-task-0",
            "cursor-task-1",
        ]
        assert first_body["has_more"] is True
        assert first_body["cursor"]

        second = client.get(
            f"/api/v1/tasks?limit=2&cursor={first_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [item["task_id"] for item in second_body["items"]] == ["cursor-task-2"]
        assert second_body["has_more"] is False


def test_task_board_limits_columns_and_pages_independently(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                now = datetime.now(UTC)
                for index in range(3):
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id="agent-1",
                        title=f"Queued {index}",
                        status="queued",
                    )
                    task.task_id = f"board-queued-{index}"
                    task.updated_at = now - timedelta(minutes=index)
                done = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Done",
                    status="completed",
                )
                done.task_id = "board-done-0"
                done.updated_at = now - timedelta(hours=1)
                done_older_same_group = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Done",
                    status="completed",
                )
                done_older_same_group.task_id = "board-done-1"
                done_older_same_group.updated_at = now - timedelta(hours=2)
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/tasks/board?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()

        queued = body["columns"]["queued"]
        assert [item["task_id"] for item in queued["items"]] == [
            "board-queued-0",
            "board-queued-1",
        ]
        assert queued["has_more"] is True
        assert queued["total_count"] == 3
        assert queued["cursor"]

        done = body["columns"]["done"]
        assert [item["task_id"] for item in done["items"]] == ["board-done-0"]
        assert [group["latest"]["task_id"] for group in done["groups"]] == ["board-done-0"]
        assert done["groups"][0]["task_count"] == 2
        assert done["total_count"] == 1
        group_key = quote(done["groups"][0]["key"], safe="")
        group_history = client.get(
            f"/api/v1/tasks/board/done/groups/{group_key}/tasks?limit=1",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert group_history.status_code == 200
        group_history_body = group_history.json()
        assert [item["task_id"] for item in group_history_body["items"]] == ["board-done-0"]
        assert group_history_body["has_more"] is True
        assert group_history_body["cursor"]

        next_group_history = client.get(
            f"/api/v1/tasks/board/done/groups/{group_key}/tasks?limit=1&cursor={group_history_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert next_group_history.status_code == 200
        assert [item["task_id"] for item in next_group_history.json()["items"]] == ["board-done-1"]

        next_page = client.get(
            f"/api/v1/tasks/board/queued?limit=2&cursor={queued['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert next_page.status_code == 200
        assert [item["task_id"] for item in next_page.json()["items"]] == ["board-queued-2"]

        filtered = client.get(
            "/api/v1/tasks/board",
            params={"limit": 1, "q": "board-done-0"},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert filtered.status_code == 200
        assert filtered.json()["columns"]["queued"]["items"] == []
        assert [item["task_id"] for item in filtered.json()["columns"]["done"]["items"]] == [
            "board-done-0"
        ]

        oversized_query = client.get(
            "/api/v1/tasks/board",
            params={"q": "x" * 201},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert oversized_query.status_code == 422


def test_task_board_progress_summary_is_optional_owned_and_bounded(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                for email in ("user@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                    await create_agent(
                        session,
                        agent_id=f"agent-{email}",
                        owner_email=email,
                        name=email,
                        status="active",
                    )
                now = datetime.now(UTC)
                for index in range(6):
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id="agent-user@example.com",
                        title=f"Running {index}",
                        status="running",
                        task_id=f"progress-task-{index}",
                    )
                    task.updated_at = now - timedelta(minutes=index)
                    run = await create_step_run(
                        session,
                        task_id=task.task_id,
                        step_name=f"step-{index}",
                        step_type="run",
                        agent_id=task.agent_id,
                        status="running",
                    )
                    run.todos = [
                        {"content": "Done", "status": "completed"},
                        {"content": "Now", "status": "in_progress"},
                        {"content": "Later", "status": "pending"},
                    ]
                    if index == 0:
                        conversation = await create_conversation(
                            session,
                            user_email="user@example.com",
                            agent_id=task.agent_id,
                            context_type="web",
                            conversation_id="progress-conversation",
                        )
                        session_row = await create_session(
                            session,
                            conversation_id=conversation.conversation_id,
                            user_email="user@example.com",
                            agent_id=task.agent_id,
                            session_id="progress-session",
                        )
                        run.session_id = session_row.session_id
                        session.add(
                            WorkSessionProjectionRow(
                                projection_id="progress-projection",
                                owner_email="user@example.com",
                                session_id=session_row.session_id,
                                source_session_id="source-progress",
                                materializer_version=WORK_MATERIALIZER_VERSION,
                                state="caught_up",
                                additions=31,
                                deletions=9,
                            )
                        )
                        for file_index in range(4):
                            session.add(
                                WorkCurrentFileRow(
                                    current_file_id=f"progress-file-{file_index}",
                                    owner_email="user@example.com",
                                    session_id=session_row.session_id,
                                    materializer_version=WORK_MATERIALIZER_VERSION,
                                    file_projector_version="file-v1",
                                    path_generation_id=f"generation-{file_index}",
                                    source_store="intaris",
                                    source_session_id="source-progress",
                                    source_seq=file_index + 1,
                                    source_item_id=f"item-{file_index}",
                                    path=f"/repo/file-{file_index}.py",
                                    path_id=f"path-{file_index}",
                                    state="modified",
                                )
                            )
                foreign = await create_task(
                    session,
                    created_by="other@example.com",
                    agent_id="agent-other@example.com",
                    title="Foreign running",
                    status="running",
                    task_id="foreign-progress-task",
                )
                await create_step_run(
                    session,
                    task_id=foreign.task_id,
                    step_name="foreign",
                    step_type="run",
                    agent_id=foreign.agent_id,
                    status="running",
                )
                await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-user@example.com",
                    title="Waiting for gate",
                    status="paused",
                    task_id="waiting-gate-task",
                    workflow_state={
                        "status": "paused",
                        "pending_pause_type": "gate",
                        "pending_pause_payload": {"pause_id": "gate-1"},
                    },
                )
                await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-user@example.com",
                    title="Paused without action",
                    status="paused",
                    task_id="ordinary-paused-task",
                )
                await session.commit()

        asyncio.run(_seed())

        default_response = client.get(
            "/api/v1/tasks/board?limit=10",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert default_response.status_code == 200
        default_running = default_response.json()["columns"]["running"]["items"]
        assert len(default_running) == 6
        assert all(item["progress_summary"] is None for item in default_running)
        paused_items = default_response.json()["columns"]["paused"]["items"]
        assert {item["task_id"]: item["attention_type"] for item in paused_items} == {
            "ordinary-paused-task": None,
            "waiting-gate-task": "gate",
        }
        attention_response = client.get(
            "/api/v1/tasks/board?limit=5&attention_only=true",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert attention_response.status_code == 200
        assert [
            item["task_id"] for item in attention_response.json()["columns"]["paused"]["items"]
        ] == ["waiting-gate-task"]

        enriched_response = client.get(
            "/api/v1/tasks/board?limit=10&include_progress_summary=true&progress_limit=5",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert enriched_response.status_code == 200
        enriched_running = enriched_response.json()["columns"]["running"]["items"]
        assert [item["task_id"] for item in enriched_running] == [
            f"progress-task-{index}" for index in range(6)
        ]
        assert all(item["task_id"] != "foreign-progress-task" for item in enriched_running)
        for index, item in enumerate(enriched_running):
            summary = item["progress_summary"]
            if index == 5:
                assert summary is None
                continue
            assert summary == {
                "todo_total": 3,
                "todo_completed": 1,
                "todo_in_progress": 1,
                "current_step_name": f"step-{index}",
                "current_step_status": "running",
                "changed_files": 4 if index == 0 else 0,
                "additions": 31 if index == 0 else 0,
                "deletions": 9 if index == 0 else 0,
            }

        invalid_limit = client.get(
            "/api/v1/tasks/board?include_progress_summary=true&progress_limit=6",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert invalid_limit.status_code == 422


def test_task_board_progress_summary_batches_step_and_diff_queries() -> None:
    step_run = SimpleNamespace(
        task_id="task-1",
        step_run_id="step-run-1",
        step_name="implement",
        status="running",
        superseded_by_step_run_id=None,
        todos=[
            {"content": "Done", "status": "completed"},
            {"content": "Now", "status": "in_progress"},
        ],
    )
    step_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [step_run]),
    )
    diff_result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                task_id="task-1",
                changed_files=4,
                additions=31,
                deletions=9,
            )
        ],
    )
    session = SimpleNamespace(execute=AsyncMock(side_effect=[step_result, diff_result]))

    summaries = asyncio.run(
        _task_board_progress_summaries(
            session,
            owner_email="user@example.com",
            task_ids=["task-1"],
        )
    )

    assert session.execute.await_count == 2
    step_statement = str(session.execute.await_args_list[0].args[0])
    assert "step_runs.todos" in step_statement
    assert "step_runs.output" not in step_statement
    assert "step_runs.evaluation" not in step_statement
    assert "step_runs.runtime_info" not in step_statement
    assert summaries["task-1"].model_dump() == {
        "todo_total": 2,
        "todo_completed": 1,
        "todo_in_progress": 1,
        "current_step_name": "implement",
        "current_step_status": "running",
        "changed_files": 4,
        "additions": 31,
        "deletions": 9,
    }


def test_task_detail_projection_endpoints_omit_heavy_step_payloads(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Projected task",
                    status="running",
                    workflow_id="system:general-task",
                    workflow_state={"current_step_index": 0, "status": "running"},
                )
                first = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="projection-step-1",
                    step_name="plan",
                    step_type="run",
                    agent_id="agent-1",
                    attempt=1,
                    attempt_number=1,
                    status="approved",
                    runtime_info={"executor_id": "executor-1", "large": "x" * 1000},
                    completed_at=datetime.now(UTC) - timedelta(minutes=10),
                )
                first.output = {"summary": "first", "content": "heavy first content"}
                second = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="projection-step-2",
                    step_name="plan",
                    step_type="run",
                    agent_id="agent-1",
                    attempt=2,
                    attempt_number=2,
                    status="approved",
                    runtime_info={"executor_id": "executor-1", "large": "y" * 1000},
                    completed_at=datetime.now(UTC),
                    deliverable_id="projection-deliverable",
                )
                second.output = {"summary": "second", "content": "heavy second content"}
                await create_deliverable(
                    session,
                    step_run_id=second.step_run_id,
                    deliverable_id="projection-deliverable",
                    title="Projection deliverable",
                    content="heavy deliverable content",
                    format="rich",
                    rich={
                        "blocks": [
                            {
                                "type": "section_header",
                                "eyebrow": "Operations",
                                "title": "Current state",
                                "subtitle": "Verified signals",
                            },
                            {"type": "markdown", "content": "Persisted rich body"},
                        ],
                        "metadata": {},
                    },
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        summary = client.get(f"/api/v1/tasks/{task_id}/summary", headers=headers)
        assert summary.status_code == 200
        summary_body = summary.json()
        assert summary_body["step_runs"] == []
        projection = summary_body["workflow_projection"]
        assert projection["workflow_id"] == "system:general-task"
        assert projection["current_step_name"] == "execute"
        assert [step["name"] for step in projection["phases"][0]["steps"]] == ["execute"]
        assert "heavy first content" not in summary.text
        assert "heavy second content" not in summary.text
        assert "effective_workflow_definition" not in summary.text

        steps = client.get(f"/api/v1/tasks/{task_id}/steps/summary", headers=headers)
        assert steps.status_code == 200
        body = steps.json()
        assert [item["step_run_id"] for item in body["items"]] == ["projection-step-2"]
        projected = body["items"][0]
        assert projected["output"] is None
        assert projected["runtime_info"] is None
        assert projected["deliverables"] == []
        assert projected["deliverable_id"] == "projection-deliverable"
        assert projected["is_projection"] is True

        history = client.get(
            f"/api/v1/tasks/{task_id}/steps/plan/summary?limit=1",
            headers=headers,
        )
        assert history.status_code == 200
        history_body = history.json()
        assert [item["step_run_id"] for item in history_body["items"]] == ["projection-step-1"]
        assert history_body["has_more"] is True

        full_step = client.get("/api/v1/step-runs/projection-step-2", headers=headers)
        assert full_step.status_code == 200
        full_body = full_step.json()
        assert full_body["output"]["content"] == "heavy second content"
        assert full_body["runtime_info"]["large"].startswith("y")
        assert full_body["deliverables"][0]["content"] == ""
        assert full_body["is_projection"] is False

        hydrated = client.get(
            "/api/v1/step-runs/projection-step-2/deliverables/projection-deliverable",
            headers=headers,
        )
        assert hydrated.status_code == 200
        hydrated_body = hydrated.json()
        assert hydrated_body["content"] == "heavy deliverable content"
        assert hydrated_body["rich_payload"]["blocks"] == [
            {
                "type": "section_header",
                "eyebrow": "Operations",
                "title": "Current state",
                "subtitle": "Verified signals",
            },
            {"type": "markdown", "content": "Persisted rich body"},
        ]


def test_failed_task_preserves_approved_rich_step_deliverable(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        rich_blocks = [
            {
                "type": "section_header",
                "eyebrow": "Operations",
                "title": "Current state",
                "subtitle": "Verified signals",
            },
            {"type": "markdown", "content": "Persisted rich body"},
        ]

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    task_id="failed-rich-task",
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Failed rich task",
                    status="running",
                    workflow_id="system:general-task",
                    workflow_state={"current_step_index": 1, "status": "failed"},
                )
                operate = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="approved-operate",
                    step_name="operate",
                    step_type="run",
                    agent_id="agent-1",
                    attempt_number=1,
                    status="approved",
                    completed_at=datetime.now(UTC) - timedelta(minutes=1),
                    deliverable_id="approved-rich-deliverable",
                )
                await create_deliverable(
                    session,
                    step_run_id=operate.step_run_id,
                    deliverable_id="approved-rich-deliverable",
                    title="Operations dashboard",
                    content="Fallback content",
                    format="rich",
                    rich={"blocks": rich_blocks, "metadata": {}},
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()
                await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_run_id="failed-coverage",
                    step_name="coverage",
                    step_type="condition",
                    agent_id="agent-1",
                    attempt_number=1,
                    status="failed",
                    completed_at=datetime.now(UTC),
                )
                task.status = "failed"
                task.result_data = None
                await session.commit()

        asyncio.run(_seed())
        response = client.get(
            "/api/v1/step-runs/approved-operate/deliverables/approved-rich-deliverable",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json()["rich_payload"]["blocks"] == rich_blocks

        asyncio.run(
            app.state.artifact_store.async_delete(
                "deliverables",
                "approved-rich-deliverable",
                "rich.chart-v1.json",
            )
        )
        unavailable = client.get(
            "/api/v1/step-runs/approved-operate/deliverables/approved-rich-deliverable",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["error"]["code"] == "deliverable_payload_unavailable"

        asyncio.run(
            app.state.artifact_store.async_save(
                "deliverables",
                "approved-rich-deliverable",
                "rich.chart-v1.json",
                b'{"blocks":[{}]}',
                "application/json",
            )
        )
        corrupt = client.get(
            "/api/v1/step-runs/approved-operate/deliverables/approved-rich-deliverable",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert corrupt.status_code == 409
        assert corrupt.json()["error"]["code"] == "deliverable_payload_unavailable"


def test_gate_response_conflict_when_already_resolved(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Paused task",
                    status="paused",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        app.state.pause_waiter.register(
            PendingPause(
                pause_id="gate_1",
                pause_type="gate",
                task_id=task_id,
                step_name="review",
                question="Approve?",
                options=[{"label": "Continue", "action": "continue"}],
            )
        )

        first = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "continue"},
        )
        second = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "continue"},
        )
        assert first.status_code == 200
        assert second.status_code == 409


def test_task_mutation_rejects_non_owner(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_user(
                    session,
                    email="attacker@example.com",
                    name="Attacker",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Private task",
                    status="draft",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        response = client.post(
            f"/api/v1/tasks/{task_id}/cancel",
            headers=_auth_headers(app, email="attacker@example.com"),
        )
        assert response.status_code == 404


def test_task_rerun_returns_new_task_target(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Completed task",
                    status="completed",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())

        async def _fake_rerun(task_id: str) -> TaskRerunResult:
            return TaskRerunResult(
                source_task_id=task_id,
                task=TaskModel(
                    task_id="task_clone",
                    title="Completed task",
                    description="",
                    status=TaskStatus.QUEUED,
                    priority=0,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    source_type="api",
                    source_ref=None,
                    delivery=TaskDelivery(),
                    workflow_id=None,
                    workflow_state=WorkflowState(),
                ),
                created_new=True,
            )

        app.state.task_queue.rerun_task = _fake_rerun

        response = client.post(
            f"/api/v1/tasks/{task_id}/rerun",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "source_task_id": task_id,
            "task_id": "task_clone",
            "status": "queued",
            "created_new": True,
        }


def test_gate_response_returns_conflict_for_unsupported_action(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:  # type: ignore[attr-defined]
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                agent = await create_agent(
                    session,
                    owner_email="user@example.com",
                    agent_id="agent-unsupported-gate",
                    name="Unsupported Gate Agent",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id=agent.agent_id,
                    title="Paused task",
                    status="paused",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        app.state.pause_waiter.register(
            PendingPause(
                pause_id="gate_conflict",
                pause_type="gate",
                task_id=task_id,
                step_name="review",
                options=[{"label": "Continue", "action": "continue"}],
            )
        )

        response = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={"step_name": "review", "action": "cancel"},
        )

        assert response.status_code == 409


def test_task_create_allows_non_chat_source_refs(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Scheduled task",
                "source_type": "scheduler",
                "source_ref": "sched_daily_review",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source_type"] == "scheduler"
        assert body["source_ref"] == "sched_daily_review"
        assert body["delivery"]["mode"] == "preferred_channel"


def test_task_create_rejects_explicit_creator_agent_without_side_effects() -> None:
    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("task_create should reject before opening a task transaction")

    request = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_fail,
            user=AuthenticatedUser(email="user@example.com", role="user"),
        )
    )
    payload = TaskCreateRequest(
        agent_id="agent-1",
        created_by_agent_id="agent-1",
        title="Invalid explicit creator",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(task_create(request, payload))

    assert exc_info.value.status_code == 400


def test_task_create_rejects_same_conversation_outside_chat(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Board task",
                "delivery_mode": "same_conversation",
            },
        )
        assert response.status_code == 400


def test_task_create_rejects_unknown_delivery_mode(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Board task",
                "delivery_mode": "typo",
            },
        )
        assert response.status_code == 400


def test_task_create_rejects_chat_without_source_ref(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "title": "Delegated task",
                "source_type": "chat",
            },
        )
        assert response.status_code == 400


def test_task_update_validates_specific_conversation_target(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_user(
                    session,
                    email="other@example.com",
                    name="Other",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                foreign_conversation = await create_conversation(
                    session,
                    user_email="other@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Foreign",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Specific delivery",
                    status="draft",
                    delivery_mode="specific_conversation",
                )
                await session.commit()
                return task.task_id, foreign_conversation.conversation_id

        task_id, foreign_conversation_id = asyncio.run(_seed())
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth_headers(app, email="owner@example.com"),
            json={"delivery_target": foreign_conversation_id},
        )
        assert response.status_code == 403


def test_task_update_rejects_specific_conversation_without_target(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
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
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Missing target",
                    status="draft",
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth_headers(app, email="owner@example.com"),
            json={"delivery_mode": "specific_conversation"},
        )
        assert response.status_code == 400


def test_step_response_resumes_recovered_step_input(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Paused question",
                    status="paused",
                    workflow_state={
                        "current_step_index": 0,
                        "status": "paused",
                        "pending_pause_type": "step_input",
                        "pending_pause_payload": {
                            "pause_id": "input_recovered",
                            "step_name": "plan",
                            "questions": [
                                {
                                    "id": "q1",
                                    "question": "Need input",
                                    "options": [
                                        {"id": "A", "label": "A"},
                                        {"id": "B", "label": "B"},
                                    ],
                                    "multiple": False,
                                    "allow_custom": True,
                                    "required": True,
                                }
                            ],
                        },
                    },
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        asyncio.run(app.state.task_queue.recover_paused_tasks())

        called: dict[str, bool] = {"resume": False}

        async def _fake_resume(task_id: str) -> TaskModel:
            called["resume"] = True
            return TaskModel(
                task_id=task_id,
                title="Paused question",
                description="",
                status=TaskStatus.RUNNING,
                priority=0,
                created_by="user@example.com",
                agent_id="agent-1",
                source_type="api",
                source_ref=None,
                delivery=TaskDelivery(),
                workflow_id=None,
                workflow_state=WorkflowState(),
            )

        app.state.task_queue.resume_task = _fake_resume

        response = client.post(
            f"/api/v1/tasks/{task_id}/step-response",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "step_name": "plan",
                "mode": "structured",
                "answers": [
                    {
                        "question_id": "q1",
                        "selected_option_ids": ["A"],
                        "custom_answer": None,
                    }
                ],
            },
        )
        assert response.status_code == 200
        assert called["resume"] is True


def test_websocket_direct_chat_step_response_resolves_notification(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="notif_direct_ok",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Need input",
                            "options": [
                                {"id": "A", "label": "A"},
                                {"id": "B", "label": "B"},
                            ],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ]
                },
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["B"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"

        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution == {
            "decision": "continue",
            "answers": [
                {
                    "question_id": "q1",
                    "selected_option_ids": ["A"],
                    "custom_answer": None,
                }
            ],
            "mode": "structured",
            "state": "resolved",
        }


def test_websocket_direct_chat_step_response_resolves_auth_challenge(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="auth_direct_ok",
                payload={"kind": "otp_code", "required_fields": ["code"], "message": "OTP"},
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "response": "123456",
                },
            )
        )

        assert manager.errors == []
        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution is not None
        assert resolved.resolution["decision"] == "continue"
        assert resolved.resolution["challenge_completed"] is True
        assert str(resolved.resolution["response_ref"]).startswith(
            "$credential:challenge_auth_direct_ok"
        )


def test_websocket_escalation_resolve_tolerates_same_decision_duplicate() -> None:
    class _NotificationService:
        async def resolve(
            self,
            notification_id: str,
            decision: str,
            data: dict[str, object],
            *,
            user_email: str | None = None,
        ) -> bool:
            assert notification_id == "call-1"
            assert decision == "approve"
            assert data == {"note": ""}
            assert user_email == "user@example.com"
            return False

        async def get(self, notification_id: str) -> object:
            assert notification_id == "call-1"
            return SimpleNamespace(
                status="resolved",
                resolution={"decision": "approve", "state": "resolved_remote"},
            )

    class _Manager:
        def __init__(self) -> None:
            self.errors: list[dict[str, object]] = []
            self.messages: list[tuple[str, dict[str, object]]] = []

        async def send_error(self, _: object, **kwargs: object) -> None:
            self.errors.append(kwargs)

        async def send_to_conversation(
            self, conversation_id: str, payload: dict[str, object]
        ) -> None:
            self.messages.append((conversation_id, payload))

    app = SimpleNamespace(
        state=SimpleNamespace(
            pause_waiter=SimpleNamespace(find_pending=lambda **_: None),
            notification_service=_NotificationService(),
        )
    )
    manager = _Manager()
    connection = AuthenticatedWebSocket(
        connection_id="conn-1",
        websocket=object(),
        user_email="user@example.com",
        role="user",
    )

    asyncio.run(
        _handle_resolve_escalation(
            app,
            manager,  # type: ignore[arg-type]
            connection,
            {"type": "resolve_escalation", "call_id": "call-1", "decision": "approve"},
        )
    )

    assert manager.errors == []
    assert manager.messages == []


def test_websocket_direct_chat_step_response_uses_persisted_questions_without_local_pause(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                session_id=session_id,
                notification_id="notif_direct_orphan",
                payload={
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Need input",
                            "options": [{"id": "A", "label": "A"}],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ]
                },
            )
        )
        app.state.pause_waiter.clear(notification.notification_id)
        app.state.notification_service._record_user_interaction = AsyncMock()

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
                },
            )
        )
        assert manager.errors == []
        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"


def test_websocket_step_response_rejects_mismatched_task_and_notification(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Task question",
                    status="paused",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id, task.task_id

        conversation_id, session_id, task_id = asyncio.run(_seed())
        notification = asyncio.run(
            app.state.notification_service.create(
                notification_type="step_question",
                user_email="user@example.com",
                conversation_id=conversation_id,
                task_id=task_id,
                session_id=session_id,
                notification_id="notif_task_match",
                payload={"question": "Need input"},
            )
        )

        class _Manager:
            def __init__(self) -> None:
                self.errors: list[dict[str, object]] = []

            async def send_error(self, _: object, **kwargs: object) -> None:
                self.errors.append(kwargs)

        manager = _Manager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=object(),
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(
            _handle_step_response(
                app,
                manager,
                connection,
                {
                    "type": "step_response",
                    "notification_id": notification.notification_id,
                    "task_id": "task-other",
                    "response": "A",
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"


def test_conversation_list_filters_by_agent(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                first = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Agent one",
                )
                second = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-2",
                    context_type="web",
                    title="Agent two",
                )
                await session.commit()
                return first.conversation_id, second.conversation_id

        first_id, second_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations?agent_id=agent-2",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["conversation_id"] for item in body["items"]] == [second_id]
        assert body["items"][0]["agent_id"] == "agent-2"
        assert first_id != second_id


def test_conversation_list_filters_by_multiple_agents_and_channels(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> dict[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                for agent_id in ("agent-1", "agent-2", "agent-3"):
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=agent_id,
                        status="active",
                    )
                rows = {
                    "web_agent_1": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-1",
                        context_type="web",
                        title="Web agent 1",
                    ),
                    "agent_work_agent_2": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-2",
                        context_type="agent_work",
                        title="Agent work agent 2",
                    ),
                    "slack_agent_3": await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-3",
                        context_type="slack",
                        title="Slack agent 3",
                    ),
                }
                await session.commit()
                return {key: row.conversation_id for key, row in rows.items()}

        ids = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations?agent_ids=agent-1&agent_ids=agent-2"
            "&context_types=web&context_types=agent_work",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert {item["conversation_id"] for item in body["items"]} == {
            ids["web_agent_1"],
            ids["agent_work_agent_2"],
        }
        assert ids["slack_agent_3"] not in {item["conversation_id"] for item in body["items"]}


def test_conversation_search_fans_out_for_multiple_agent_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> dict[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                session_ids: dict[str, str] = {}
                for agent_id in ("agent-1", "agent-2"):
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=agent_id,
                        status="active",
                    )
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id=agent_id,
                        context_type="web",
                        title=agent_id,
                    )
                    session_row = await create_session(
                        session,
                        conversation_id=conversation.conversation_id,
                        user_email="user@example.com",
                        agent_id=agent_id,
                    )
                    intaris_session_id = f"intaris-{agent_id}"
                    await set_session_intaris_session_id(
                        session,
                        session_row.session_id,
                        intaris_session_id,
                    )
                    session_ids[agent_id] = intaris_session_id
                await session.commit()
                return session_ids

        session_ids = asyncio.run(_seed())
        requested_agent_ids: list[str | None] = []
        original_guardrails = app.state.providers.guardrails

        async def _search_sessions(payload: object, *, user_email: str) -> SearchSessionsResponse:
            assert user_email == "user@example.com"
            filters = payload.filters  # type: ignore[attr-defined]
            agent_id = filters.agent_id
            requested_agent_ids.append(agent_id)
            assert agent_id is not None
            intaris_session_id = session_ids[agent_id]
            return SearchSessionsResponse(
                sessions=[
                    SearchSessionMatch(
                        session_id=intaris_session_id,
                        match_count=1,
                        top_match=SearchMatch(
                            session_id=intaris_session_id,
                            kind="summary",
                            snippet=f"match for {agent_id}",
                            score=0.9,
                        ),
                    )
                ],
                total_estimated=1,
            )

        app.state.providers.guardrails = SimpleNamespace(
            client=original_guardrails.client,
            search_sessions=_search_sessions,
        )

        response = client.post(
            "/api/v1/search/conversations",
            json={
                "q": "match",
                "filters": {
                    "agent_id": "agent-1",
                    "agent_ids": ["agent-2"],
                    "context_types": ["web"],
                },
                "kinds": ["summary"],
                "limit": 10,
            },
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert requested_agent_ids == ["agent-1", "agent-2"]
        assert {item["agent_id"] for item in body["matches"]} == {"agent-1", "agent-2"}


def test_conversation_search_rejects_too_many_agent_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/search/conversations",
            json={
                "q": "match",
                "filters": {"agent_ids": [f"agent-{index}" for index in range(26)]},
                "kinds": ["summary"],
                "limit": 10,
            },
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "too_many_agent_filters"


def test_conversation_list_paginates_before_attention_hydration(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> list[str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                conversations = []
                for index in range(3):
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-1",
                        context_type="web",
                        title=f"Conversation {index}",
                    )
                    conversation.last_message_at = base + timedelta(minutes=index)
                    conversations.append(conversation)
                await session.commit()
                return [conversation.conversation_id for conversation in conversations]

        conversation_ids = asyncio.run(_seed())
        hydration_batch_sizes: list[int] = []

        async def _fake_attention_context(
            _session: object,
            conversations: list[object],
            _user_email: str,
        ) -> tuple[dict[str, object], dict[str, list[str]]]:
            hydration_batch_sizes.append(len(conversations))
            return {}, {}

        from cognis.api.routes import conversations as conversation_routes

        monkeypatch.setattr(
            conversation_routes,
            "_conversation_attention_context",
            _fake_attention_context,
        )

        first_response = client.get(
            "/api/v1/conversations?limit=2",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first_response.status_code == 200
        first_body = first_response.json()
        assert [item["conversation_id"] for item in first_body["items"]] == [
            conversation_ids[2],
            conversation_ids[1],
        ]
        assert first_body["has_more"] is True
        assert first_body["cursor"]

        second_response = client.get(
            f"/api/v1/conversations?limit=2&cursor={first_body['cursor']}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert second_response.status_code == 200
        second_body = second_response.json()
        assert [item["conversation_id"] for item in second_body["items"]] == [
            conversation_ids[0],
        ]
        assert second_body["has_more"] is False
        assert hydration_batch_sizes == [2, 1]


def test_conversation_list_filters_titles_before_pagination(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> list[str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                matching_ids: list[str] = []
                for index, title in enumerate(
                    [
                        "Recent unrelated",
                        "Needle first",
                        "Older unrelated",
                        "needle second",
                        None,
                        "   ",
                    ]
                ):
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id="agent-1",
                        context_type="web",
                        title=title,
                    )
                    conversation.last_message_at = base - timedelta(minutes=index)
                    if title and "needle" in title.lower():
                        matching_ids.append(conversation.conversation_id)
                await session.commit()
                return matching_ids

        matching_ids = asyncio.run(_seed())
        first_response = client.get(
            "/api/v1/conversations",
            params={"limit": 1, "q": " NEEDLE "},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first_response.status_code == 200
        first_body = first_response.json()
        assert [item["conversation_id"] for item in first_body["items"]] == [matching_ids[0]]
        assert first_body["has_more"] is True

        second_response = client.get(
            "/api/v1/conversations",
            params={"limit": 1, "q": "needle", "cursor": first_body["cursor"]},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert second_response.status_code == 200
        second_body = second_response.json()
        assert [item["conversation_id"] for item in second_body["items"]] == [matching_ids[1]]
        assert second_body["has_more"] is False

        stale_cursor_response = client.get(
            "/api/v1/conversations",
            params={"limit": 1, "q": "unrelated", "cursor": first_body["cursor"]},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert stale_cursor_response.status_code == 400
        assert stale_cursor_response.json()["error"]["code"] == "invalid_cursor"

        untitled_response = client.get(
            "/api/v1/conversations",
            params={"q": "untitled conversation"},
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert untitled_response.status_code == 200
        assert [item["title"] for item in untitled_response.json()["items"]] == [None, "   "]


def test_conversation_list_ignores_metadata_updated_at_for_activity_ordering(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                updated_activity = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Updated activity",
                )
                updated_activity.last_message_at = base + timedelta(minutes=1)
                updated_activity.updated_at = base + timedelta(minutes=10)

                newer_message = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Newer message",
                )
                newer_message.last_message_at = base + timedelta(minutes=5)
                newer_message.updated_at = base + timedelta(minutes=5)
                await session.commit()
                return updated_activity.conversation_id, newer_message.conversation_id

        metadata_updated_id, message_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"][:2]] == [
            message_id,
            metadata_updated_id,
        ]


def test_conversation_context_types_projection(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Web",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="signal",
                    title="Signal",
                )
                archived = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="slack",
                    title="Archived Slack",
                )
                archived.status = "archived"
                await session.commit()

        asyncio.run(_seed())

        active_response = client.get(
            "/api/v1/conversations/context-types",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert active_response.status_code == 200
        assert active_response.json() == ["signal", "web"]

        archived_response = client.get(
            "/api/v1/conversations/context-types?status=archived",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert archived_response.status_code == 200
        assert archived_response.json() == ["slack"]


def test_conversation_open_prefers_valid_selected_agent_candidate(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                selected = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Selected Agent",
                )
                other_agent = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-2",
                    context_type="web",
                    title="Other Agent",
                )
                await session.commit()
                return selected.conversation_id, other_agent.conversation_id

        selected_id, other_agent_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [other_agent_id, selected_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == selected_id


def test_conversation_open_allows_viewer_to_open_existing_candidate(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="viewer@example.com",
                    name="Viewer",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="viewer",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="viewer@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="viewer@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Viewer Conversation",
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="viewer@example.com", role="viewer"),
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [conversation_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == conversation_id

        async def _state() -> dict[str, object] | None:
            async with app.state.session_factory() as session:
                return await get_user_ui_state_value(
                    session,
                    "viewer@example.com",
                    "chat.last_opened:viewer-agent\x1f\x1fweb",
                )

        assert asyncio.run(_state()) is None


def test_conversation_open_honors_requested_agent_profile(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                    agent_profiles={
                        "fast": {"profile_id": "fast", "description": "Fast"},
                        "quality": {"profile_id": "quality", "description": "Quality"},
                    },
                    default_agent_profile_id="fast",
                )
                fast = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Fast",
                )
                fast.agent_profile_id = "fast"
                quality = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Quality",
                )
                quality.agent_profile_id = "quality"
                await session.commit()
                return fast.conversation_id, quality.conversation_id

        fast_id, quality_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "agent_profile_id": "quality",
                "context_type": "web",
                "candidate_conversation_ids": [fast_id, quality_id],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == quality_id


def test_conversation_open_fallback_finds_latest_matching_profile(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                    agent_profiles={
                        "fast": {"profile_id": "fast", "description": "Fast"},
                        "quality": {"profile_id": "quality", "description": "Quality"},
                    },
                    default_agent_profile_id="fast",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                quality = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Quality",
                )
                quality.agent_profile_id = "quality"
                quality.last_message_at = base
                fast = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Fast",
                )
                fast.agent_profile_id = "fast"
                fast.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return quality.conversation_id, fast.conversation_id

        quality_id, fast_id = asyncio.run(_seed())

        response = client.post(
            "/api/v1/conversations/open",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "agent_id": "agent-1",
                "agent_profile_id": "quality",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == quality_id
        assert response.json()["conversation_id"] != fast_id


def test_conversation_open_uses_server_persisted_last_opened(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Older",
                )
                older.last_message_at = base
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Newer",
                )
                newer.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        first = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [older_id],
            },
        )
        assert first.status_code == 200
        assert first.json()["conversation_id"] == older_id

        second = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == older_id
        assert second.json()["conversation_id"] != newer_id


def test_conversation_open_prefers_fresher_client_candidate_over_server_state(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Server persisted",
                )
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Client fresher",
                )
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        first = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [older_id],
            },
        )
        assert first.status_code == 200
        assert first.json()["conversation_id"] == older_id

        second = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversations": [
                    {
                        "conversation_id": newer_id,
                        "opened_at": "2999-01-01T00:00:00",
                    }
                ],
                "candidate_conversation_ids": [newer_id],
            },
        )

        assert second.status_code == 200
        assert second.json()["conversation_id"] == newer_id
        assert second.json()["conversation_id"] != older_id

        invalid_timestamp = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversations": [
                    {
                        "conversation_id": newer_id,
                        "opened_at": "not-a-date",
                    }
                ],
                "candidate_conversation_ids": [newer_id],
            },
        )

        assert invalid_timestamp.status_code == 200
        assert invalid_timestamp.json()["conversation_id"] == newer_id


def test_conversation_opened_endpoint_persists_direct_open(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                base = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                opened = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Opened",
                )
                opened.last_message_at = base
                latest = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Latest",
                )
                latest.last_message_at = base + timedelta(minutes=1)
                await session.commit()
                return opened.conversation_id, latest.conversation_id

        opened_id, latest_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        opened = client.post(f"/api/v1/conversations/{opened_id}/opened", headers=headers)
        assert opened.status_code == 200
        assert opened.json()["conversation_id"] == opened_id

        resolved = client.post(
            "/api/v1/conversations/open",
            headers=headers,
            json={
                "agent_id": "agent-1",
                "context_type": "web",
                "candidate_conversation_ids": [],
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["conversation_id"] == opened_id
        assert resolved.json()["conversation_id"] != latest_id


def test_conversation_sidebar_projection_returns_shaped_sidebar_payload(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-2",
                    owner_email="user@example.com",
                    name="Agent 2",
                    status="active",
                )
                web = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Web",
                )
                await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="signal",
                    title="Signal",
                )
                direct = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_ref=agent_direct_context_ref("user@example.com", "agent-1"),
                    context_data={"kind": AGENT_DIRECT_KIND},
                    title="Agent direct",
                    title_source="agent_direct",
                )
                await session.commit()
                return web.conversation_id, direct.conversation_id

        web_id, direct_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations/sidebar?context_type=web&agent_id=agent-1&limit=10",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert {agent["agent_id"] for agent in body["agents"]} >= {"agent-1", "agent-2"}
        assert body["context_types"] == ["signal", "web"]
        assert [item["conversation_id"] for item in body["conversations"]["items"]] == [web_id]
        assert body["conversations"]["has_more"] is False
        assert [item["conversation"]["conversation_id"] for item in body["agent_direct_chats"]] == [
            direct_id
        ]

        signal_response = client.get(
            "/api/v1/conversations/sidebar?context_type=signal&agent_id=agent-1&limit=10",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert signal_response.status_code == 200
        assert signal_response.json()["agent_direct_chats"] == []


def test_conversation_sidebar_task_filter_isolated_and_paginated(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                normal = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Normal",
                )
                first_task = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_data={"kind": "task_control", "task_id": "task-1"},
                    title="Task one",
                )
                second_task = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_data={"kind": "task_control", "task_id": "task-2"},
                    title="Task two",
                )
                await session.commit()
                return (
                    normal.conversation_id,
                    first_task.conversation_id,
                    second_task.conversation_id,
                )

        normal_id, first_task_id, second_task_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        first_page = client.get(
            "/api/v1/conversations/sidebar?status=task&limit=1",
            headers=headers,
        )
        assert first_page.status_code == 200
        first_body = first_page.json()["conversations"]
        assert first_body["has_more"] is True
        assert first_body["cursor"]
        first_ids = [item["conversation_id"] for item in first_body["items"]]
        assert len(first_ids) == 1

        second_page = client.get(
            "/api/v1/conversations/sidebar",
            params={"status": "task", "limit": 1, "cursor": first_body["cursor"]},
            headers=headers,
        )
        assert second_page.status_code == 200
        second_body = second_page.json()["conversations"]
        second_ids = [item["conversation_id"] for item in second_body["items"]]
        assert len(second_ids) == 1
        assert second_body["has_more"] is False
        assert set(first_ids + second_ids) == {first_task_id, second_task_id}

        active = client.get("/api/v1/conversations/sidebar", headers=headers)
        all_conversations = client.get(
            "/api/v1/conversations/sidebar?status=all",
            headers=headers,
        )
        assert active.status_code == 200
        assert all_conversations.status_code == 200
        assert [item["conversation_id"] for item in active.json()["conversations"]["items"]] == [
            normal_id
        ]
        assert [
            item["conversation_id"] for item in all_conversations.json()["conversations"]["items"]
        ] == [normal_id]


def test_conversation_sidebar_legacy_delta_request_returns_filtered_full_reconciliation(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str, str, datetime]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                agent = await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                since = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
                agent.created_at = since - timedelta(seconds=1)
                agent.updated_at = since - timedelta(seconds=1)
                unchanged = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Before",
                )
                changed = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Changed",
                )
                archived = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Archived",
                )
                deleted = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Deleted",
                )
                unchanged.updated_at = since - timedelta(seconds=1)
                changed.updated_at = since + timedelta(seconds=1)
                archived.updated_at = since + timedelta(seconds=2)
                archived.status = "archived"
                deleted.updated_at = since + timedelta(seconds=3)
                deleted.status = "deleted"
                await session.commit()
                return (
                    unchanged.conversation_id,
                    changed.conversation_id,
                    archived.conversation_id,
                    deleted.conversation_id,
                    since,
                )

        unchanged_id, changed_id, archived_id, deleted_id, since = asyncio.run(_seed())
        response = client.get(
            f"/api/v1/conversations/sidebar?context_type=web&changed_since={quote(since.isoformat())}",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert {item["conversation_id"] for item in body["conversations"]["items"]} == {
            unchanged_id,
            changed_id,
        }
        assert archived_id not in {
            item["conversation_id"] for item in body["conversations"]["items"]
        }
        assert deleted_id not in {
            item["conversation_id"] for item in body["conversations"]["items"]
        }
        assert body["removed_conversation_ids"] == []
        assert body["agents"]
        assert body["context_types"] == ["web"]
        assert body["is_delta"] is False
        assert body["full_resync_required"] is False
        assert isinstance(body["sync_timestamp"], str)
        assert isinstance(body["sidebar_revision"], str)
        assert body["background_work_changed"] is True
        assert body["background_work"] is not None
        assert body["background_work"]["active_count"] == 0

        all_response = client.get(
            f"/api/v1/conversations/sidebar?context_type=web&status=all&changed_since={quote(since.isoformat())}",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert all_response.status_code == 200
        all_body = all_response.json()
        assert {item["conversation_id"] for item in all_body["conversations"]["items"]} == {
            unchanged_id,
            changed_id,
            archived_id,
        }
        assert deleted_id not in {
            item["conversation_id"] for item in all_body["conversations"]["items"]
        }
        assert all_body["removed_conversation_ids"] == []
        assert all_body["is_delta"] is False


def test_conversation_sidebar_delta_requires_resync_for_metadata_sources(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                own_agent = await create_agent(
                    session,
                    agent_id="agent-own",
                    owner_email="user@example.com",
                    name="Own agent",
                    status="active",
                )
                shared_agent = await create_agent(
                    session,
                    agent_id="agent-shared",
                    owner_email="owner@example.com",
                    name="Shared agent",
                    status="active",
                )
                grant = await create_agent_grant(
                    session,
                    agent_id=shared_agent.agent_id,
                    grantee_user_email="user@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await upsert_system_agent_override(
                    session,
                    owner_email="user@example.com",
                    agent_id="system:explore",
                    disabled=True,
                )
                await session.commit()
                return own_agent.agent_id, grant.grant_id

        own_agent_id, grant_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        baseline = client.get("/api/v1/conversations/sidebar", headers=headers).json()
        baseline_timestamp = datetime.fromisoformat(baseline["sync_timestamp"])

        async def _update_agent() -> None:
            async with app.state.session_factory() as session:
                agent = await session.get(Agent, own_agent_id)
                assert agent is not None
                agent.name = "Updated agent"
                agent.updated_at = baseline_timestamp + timedelta(microseconds=1)
                await session.commit()

        asyncio.run(_update_agent())
        agent_delta = client.get(
            "/api/v1/conversations/sidebar",
            params={
                "changed_since": baseline["sync_timestamp"],
                "sidebar_revision": baseline["sidebar_revision"],
            },
            headers=headers,
        )
        assert agent_delta.status_code == 200
        agent_body = agent_delta.json()
        assert agent_body["is_delta"] is False
        assert agent_body["full_resync_required"] is False
        assert (
            next(item for item in agent_body["agents"] if item["agent_id"] == own_agent_id)["name"]
            == "Updated agent"
        )

        after_agent = client.get("/api/v1/conversations/sidebar", headers=headers).json()

        async def _patch_grant() -> None:
            async with app.state.session_factory() as session:
                grant = await update_agent_grant(
                    session,
                    grant_id,
                    executor_scope="grantee_executor",
                    grantee_overrides={"execution": {"executor_id": "executor-1"}},
                )
                assert grant is not None
                await session.commit()

        asyncio.run(_patch_grant())
        patch_delta = client.get(
            "/api/v1/conversations/sidebar",
            params={
                "changed_since": after_agent["sync_timestamp"],
                "sidebar_revision": after_agent["sidebar_revision"],
            },
            headers=headers,
        )
        assert patch_delta.status_code == 200
        patch_body = patch_delta.json()
        assert patch_body["is_delta"] is False
        assert patch_body["full_resync_required"] is False
        assert any(item["agent_id"] == "agent-shared" for item in patch_body["agents"])

        after_patch = client.get("/api/v1/conversations/sidebar", headers=headers).json()
        after_patch_timestamp = datetime.fromisoformat(after_patch["sync_timestamp"])

        async def _revoke_grant() -> None:
            async with app.state.session_factory() as session:
                grant = await revoke_agent_grant(session, grant_id)
                assert grant is not None
                grant.revoked_at = after_patch_timestamp + timedelta(microseconds=1)
                await session.commit()

        asyncio.run(_revoke_grant())
        grant_delta = client.get(
            "/api/v1/conversations/sidebar",
            params={
                "changed_since": after_patch["sync_timestamp"],
                "sidebar_revision": after_patch["sidebar_revision"],
            },
            headers=headers,
        )
        assert grant_delta.status_code == 200
        grant_body = grant_delta.json()
        assert grant_body["is_delta"] is False
        assert grant_body["full_resync_required"] is False
        assert not any(item["agent_id"] == "agent-shared" for item in grant_body["agents"])

        after_grant = client.get("/api/v1/conversations/sidebar", headers=headers).json()
        after_grant_timestamp = datetime.fromisoformat(after_grant["sync_timestamp"])

        async def _create_new_context_type() -> None:
            async with app.state.session_factory() as session:
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id=own_agent_id,
                    context_type="matrix",
                    title="Matrix conversation",
                )
                conversation.created_at = after_grant_timestamp + timedelta(microseconds=1)
                conversation.updated_at = conversation.created_at
                await session.commit()

        asyncio.run(_create_new_context_type())
        context_delta = client.get(
            "/api/v1/conversations/sidebar",
            params={
                "changed_since": after_grant["sync_timestamp"],
                "sidebar_revision": after_grant["sidebar_revision"],
            },
            headers=headers,
        )
        assert context_delta.status_code == 200
        context_body = context_delta.json()
        assert context_body["is_delta"] is False
        assert context_body["context_types"] == ["matrix"]
        assert context_body["full_resync_required"] is False

        after_context = client.get("/api/v1/conversations/sidebar", headers=headers).json()

        async def _reset_override() -> None:
            async with app.state.session_factory() as session:
                deleted = await delete_system_agent_override(
                    session,
                    owner_email="user@example.com",
                    agent_id="system:explore",
                )
                assert deleted is True
                await session.commit()

        asyncio.run(_reset_override())
        override_delta = client.get(
            "/api/v1/conversations/sidebar",
            params={
                "changed_since": after_context["sync_timestamp"],
                "sidebar_revision": after_context["sidebar_revision"],
            },
            headers=headers,
        )
        assert override_delta.status_code == 200
        override_body = override_delta.json()
        assert override_body["is_delta"] is False
        assert override_body["full_resync_required"] is False
        assert any(item["agent_id"] == "system:explore" for item in override_body["agents"])


def test_conversation_sidebar_projection_query_count_is_bounded(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> list[str]:
            active_conversation_ids: list[str] = []
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                for index in range(12):
                    agent_id = f"agent-{index}"
                    await create_agent(
                        session,
                        agent_id=agent_id,
                        owner_email="user@example.com",
                        name=f"Agent {index}",
                        status="active",
                    )
                    await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id=agent_id,
                        context_type="web",
                        context_ref=agent_direct_context_ref("user@example.com", agent_id),
                        context_data={"kind": AGENT_DIRECT_KIND},
                        title=f"Direct {index}",
                        title_source="agent_direct",
                    )
                for index in range(24):
                    conversation = await create_conversation(
                        session,
                        user_email="user@example.com",
                        agent_id=f"agent-{index % 12}",
                        context_type="web" if index % 2 == 0 else "signal",
                        title=f"Conversation {index}",
                    )
                    session_row = await create_session(
                        session,
                        conversation.conversation_id,
                        "user@example.com",
                        f"agent-{index % 12}",
                    )
                    await update_conversation_active_session(
                        session,
                        conversation.conversation_id,
                        session_row.session_id,
                    )
                    task = await create_task(
                        session,
                        created_by="user@example.com",
                        agent_id=f"agent-{index % 12}",
                        title=f"Task {index}",
                        status="running",
                    )
                    await create_step_run(
                        session,
                        task_id=task.task_id,
                        step_name="work",
                        step_type="agent",
                        agent_id=f"agent-{index % 12}",
                        conversation_id=conversation.conversation_id,
                        status="running",
                        started_at=datetime.now(UTC),
                    )
                    active_conversation_ids.append(conversation.conversation_id)
                await session.commit()
            return active_conversation_ids

        active_conversation_ids = asyncio.run(_seed())

        async def _running_states(conversation_ids: list[str], *, session=None):
            assert session is not None
            return {
                conversation_id: {"turn_id": f"turn-{conversation_id}"}
                for conversation_id in conversation_ids
                if conversation_id in active_conversation_ids
            }

        app.state.turn_scheduler.durable_running_turn_states = AsyncMock(
            side_effect=_running_states
        )
        statements: list[str] = []

        def _count_statement(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            statements.append(statement)

        sa_event.listen(app.state.engine.sync_engine, "before_cursor_execute", _count_statement)
        try:
            response = client.get(
                "/api/v1/conversations/sidebar?context_type=web&limit=20",
                headers=_auth_headers(app, email="user@example.com"),
            )
        finally:
            sa_event.remove(
                app.state.engine.sync_engine,
                "before_cursor_execute",
                _count_statement,
            )

        assert response.status_code == 200
        body = response.json()
        assert len(body["conversations"]["items"]) == 12
        assert len(body["agent_direct_chats"]) == 12
        assert len(statements) <= 30


def test_conversation_list_includes_attention_status(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Blocked conversation",
                )
                session_row = await create_session(
                    session,
                    conversation.conversation_id,
                    "user@example.com",
                    "agent-1",
                    status="suspended",
                )
                session_row.completion_reason = "safety_escalation"
                conversation.active_session_id = session_row.session_id
                session.add(
                    NotificationRow(
                        notification_id="notif_attention",
                        notification_type="gate",
                        user_email="user@example.com",
                        conversation_id=conversation.conversation_id,
                        session_id=session_row.session_id,
                        status="pending",
                        payload={},
                    )
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        item = next(item for item in body["items"] if item["conversation_id"] == conversation_id)
        assert item["active_session_status"] == "suspended"
        assert item["active_session_completion_reason"] == "safety_escalation"
        assert item["active_turn_chat_mode"] is None
        assert item["active_turn_chat_mode_source"] is None
        assert item["pending_notification_types"] == ["gate"]


def test_conversation_list_defaults_to_active_and_supports_starred_and_archived_filters(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str, str, str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                active = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Active",
                )
                archived = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Archived",
                )
                archived.status = "archived"
                starred = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Starred",
                )
                starred.starred_at = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
                deleted = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Deleted",
                )
                deleted.status = "deleted"
                task_control = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_data={"kind": "task_control", "task_id": "task-1"},
                    title="Task control",
                )
                await session.commit()
                return (
                    active.conversation_id,
                    archived.conversation_id,
                    starred.conversation_id,
                    deleted.conversation_id,
                    task_control.conversation_id,
                )

        active_id, archived_id, starred_id, deleted_id, task_control_id = asyncio.run(_seed())

        active_response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert active_response.status_code == 200
        assert [item["conversation_id"] for item in active_response.json()["items"]] == [
            starred_id,
            active_id,
        ]

        starred_response = client.get(
            "/api/v1/conversations?status=starred",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert starred_response.status_code == 200
        starred_items = starred_response.json()["items"]
        assert [item["conversation_id"] for item in starred_items] == [starred_id]
        assert starred_items[0]["starred_at"] is not None

        archived_response = client.get(
            "/api/v1/conversations?status=archived",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert archived_response.status_code == 200
        assert [item["conversation_id"] for item in archived_response.json()["items"]] == [
            archived_id
        ]
        assert deleted_id not in [
            item["conversation_id"] for item in archived_response.json()["items"]
        ]

        all_response = client.get(
            "/api/v1/conversations?status=all",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert all_response.status_code == 200
        all_ids = {item["conversation_id"] for item in all_response.json()["items"]}
        assert {active_id, archived_id, starred_id} <= all_ids
        assert deleted_id not in all_ids
        assert task_control_id not in all_ids

        task_response = client.get(
            "/api/v1/conversations?status=task",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert task_response.status_code == 200
        assert [item["conversation_id"] for item in task_response.json()["items"]] == [
            task_control_id
        ]


def test_conversation_update_sets_and_clears_starred_at(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Important",
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        star_response = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
            json={"starred_at": "2026-05-07T12:00:00Z"},
        )
        assert star_response.status_code == 200
        assert star_response.json()["starred_at"] is not None

        detail_response = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["starred_at"] is not None

        unstar_response = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
            json={"starred_at": None},
        )
        assert unstar_response.status_code == 200
        assert unstar_response.json()["starred_at"] is None


def test_conversation_detail_uses_scheduler_active_turn_state(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Active",
                )
                session_row = await create_session(
                    session,
                    conversation.conversation_id,
                    "user@example.com",
                    "agent-1",
                )
                await set_session_status(
                    session,
                    session_row.session_id,
                    "completed",
                    completion_reason="finished",
                )
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    session_row.session_id,
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())
        app.state.turn_scheduler.running_turn_state = lambda _conversation_id: None

        response = client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["active_session_id"] == session_id
        assert body["active_session_status"] == "completed"
        assert body["has_active_turn"] is False
        assert body["active_turn_chat_mode"] is None
        assert body["active_turn_chat_mode_source"] is None


def test_conversation_list_orders_by_latest_activity_even_without_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                older = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Older active conversation",
                )
                older.starred_at = datetime.now(UTC)
                await touch_conversation(
                    session,
                    older.conversation_id,
                    datetime.now(UTC) - timedelta(days=1),
                )
                newer = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Brand new conversation",
                )
                await session.commit()
                return older.conversation_id, newer.conversation_id

        older_id, newer_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"]] == [
            newer_id,
            older_id,
        ]


def test_conversation_list_ignores_update_time_when_conversation_has_no_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                finished_later = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Finished later",
                )
                await touch_conversation(
                    session,
                    finished_later.conversation_id,
                    datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
                )
                no_messages = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="No messages",
                )
                no_messages.last_message_at = None
                no_messages.created_at = datetime(2026, 5, 7, 11, 0, tzinfo=UTC)
                no_messages.updated_at = datetime(2026, 5, 8, 13, 0, tzinfo=UTC)
                await session.commit()
                return finished_later.conversation_id, no_messages.conversation_id

        finished_later_id, no_messages_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["conversation_id"] for item in response.json()["items"]] == [
            finished_later_id,
            no_messages_id,
        ]


def test_deleted_conversation_is_hidden_from_detail(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Hidden",
                )
                conversation.status = "deleted"
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        detail_response = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)

        assert detail_response.status_code == 404


def test_websocket_replay_skips_missing_active_session_error(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Replay",
                )
                conversation.active_session_id = "sess-missing"
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        class _Socket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def send_json(self, payload: dict[str, object]) -> None:
                self.sent.append(payload)

        manager = WebSocketConnectionManager(app)
        socket = _Socket()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=socket,
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(manager.replay(connection, conversation_id=conversation_id, last_seq=0))

        assert [payload["type"] for payload in socket.sent] == [
            "queued_messages_updated",
            "conversation_state_snapshot",
        ]
        state_payload = socket.sent[-1]
        assert state_payload["conversation_id"] == conversation_id
        state = state_payload["state"]
        assert isinstance(state, dict)
        assert state["conversation_id"] == conversation_id
        assert state["conversation_kind"] == "normal"
        assert state["task"] is None
        assert conversation_id in connection.subscriptions


def test_websocket_replay_includes_user_messages(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> tuple[str, str]:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Replay",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 3
            assert limit > 0
            assert allow_missing_stream is True
            return EventReadResult(
                events=[
                    {
                        "seq": 4,
                        "type": "user_message",
                        "timestamp": "2026-03-28T00:00:00Z",
                        "data": {
                            "session_id": session_id,
                            "event_id": "client:cmsg_1",
                            "message_id": "client:cmsg_1",
                            "content": "hello",
                            "client_message_id": "cmsg_1",
                            "turn_id": "turn_1",
                            "attachments": [],
                        },
                    }
                ],
                last_seq=4,
                has_more=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        class _Socket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []

            async def send_json(self, payload: dict[str, object]) -> None:
                self.sent.append(payload)

        manager = WebSocketConnectionManager(app)
        socket = _Socket()
        connection = AuthenticatedWebSocket(
            connection_id="conn-1",
            websocket=socket,
            user_email="user@example.com",
            role="user",
        )

        asyncio.run(manager.replay(connection, conversation_id=conversation_id, last_seq=3))

        assert conversation_id in connection.subscriptions


def test_signed_artifact_route_serves_skill_assets_without_artifact_record(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> str:
            await artifact_store.async_save(
                "skills",
                "ska_script",
                "assets/tool.py",
                b"print('hi')\n",
                "text/x-python",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                skill = await create_skill(
                    session,
                    skill_id="skill_asset_test",
                    name="Asset Test",
                    instructions="Use an asset.",
                    owner_email="user@example.com",
                )
                version = await create_skill_version(
                    session,
                    skill_id=skill.skill_id,
                    version_number=1,
                    content_hash="hash",
                    instructions=skill.instructions,
                )
                await create_skill_asset(
                    session,
                    asset_id="sa_script",
                    skill_version_id=version.version_id,
                    filename="assets/tool.py",
                    artifact_namespace="skills",
                    artifact_object_id="ska_script",
                    content_hash="content-hash",
                    size_bytes=12,
                    content_type="text/x-python",
                )
                await session.commit()
            return await artifact_store.async_get_public_url(
                "skills", "ska_script", "assets/tool.py"
            )

        signed_url = asyncio.run(_seed())
        response = client.get(signed_url)

        assert response.status_code == 200
        assert response.content == b"print('hi')\n"
        assert response.headers["content-type"].startswith("text/x-python")


def test_signed_artifact_view_route_serves_html_inline_only_with_view_signature(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> tuple[str, str]:
            await artifact_store.async_save(
                "reports",
                "html_report",
                "report.html",
                b"<!doctype html><title>Report</title><script>window.ok=true</script>",
                "text/html; charset=utf-8",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="html_report",
                    namespace="reports",
                    object_id="html_report",
                    filename="report.html",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/html; charset=utf-8",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()
            download_url = await artifact_store.async_get_public_url(
                "reports", "html_report", "report.html"
            )
            view_url = await artifact_store.async_get_public_url(
                "reports", "html_report", "report.html", mode="view"
            )
            return download_url, view_url

        download_url, view_url = asyncio.run(_seed())

        download_response = client.get(download_url)
        view_response = client.get(view_url)
        forged_view_response = client.get(download_url.replace("/content/", "/view/"))

        assert download_response.status_code == 200
        assert download_response.headers["content-disposition"].startswith("attachment;")
        assert view_response.status_code == 200
        assert view_response.headers["content-type"].startswith("text/html")
        assert view_response.headers["content-disposition"].startswith("inline;")
        csp = view_response.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts")
        assert "connect-src 'none'" in csp
        assert "https:" not in csp
        assert forged_view_response.status_code == 403


def test_signed_artifact_view_route_rejects_non_html(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        artifact_store = app.state.artifact_store

        async def _seed() -> str:
            await artifact_store.async_save(
                "reports",
                "plain_report",
                "report.txt",
                b"plain report",
                "text/plain",
                owner_email="user@example.com",
            )
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="plain_report",
                    namespace="reports",
                    object_id="plain_report",
                    filename="report.txt",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=12,
                    status="attached",
                )
                await session.commit()
            return await artifact_store.async_get_public_url(
                "reports", "plain_report", "report.txt", mode="view"
            )

        response = client.get(asyncio.run(_seed()))

        assert response.status_code == 415


def test_artifact_signed_url_api_returns_view_url_for_html_artifact(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="html_api_report",
                    namespace="reports",
                    object_id="html_api_report",
                    filename="report.html",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/html",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/artifacts/html_api_report/signed-url?ttl_seconds=604800&mode=view",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "view"
        assert "/api/v1/artifacts/view/reports/html_api_report/report.html" in body["url"]


def test_artifact_signed_url_api_rejects_view_url_for_non_html_artifact(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_artifact_record(
                    session,
                    artifact_id="plain_api_report",
                    namespace="reports",
                    object_id="plain_api_report",
                    filename="report.txt",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain",
                    size_bytes=64,
                    status="attached",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.get(
            "/api/v1/artifacts/plain_api_report/signed-url?mode=view",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 415


def test_signed_virtual_deliverable_route_serves_exact_content(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent",
                )
                task = await create_task(
                    session,
                    task_id="task-virtual-url",
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Task",
                )
                step_run = await create_step_run(
                    session,
                    step_run_id="sr-virtual-url",
                    task_id=task.task_id,
                    step_name="execute",
                    step_type="direct",
                    agent_id="agent-1",
                    status="completed",
                )
                await create_deliverable(
                    session,
                    deliverable_id="dlv_virtual_url",
                    step_run_id=step_run.step_run_id,
                    title="Virtual URL",
                    content="# Virtual\n\nExact content.",
                    format="markdown",
                    artifact_store=app.state.artifact_store,
                )
                await session.commit()

                from cognis.core.content_refs import (
                    build_deliverable_public_url,
                    get_accessible_deliverable_ref,
                )

                async with app.state.session_factory() as session:
                    ref = await get_accessible_deliverable_ref(
                        session,
                        app.state.artifact_store,
                        "dlv_virtual_url",
                        "user@example.com",
                    )
                assert ref is not None
                return build_deliverable_public_url(
                    app.state.artifact_store,
                    ref,
                    ttl_seconds=3600,
                )

        signed_url = client.portal.call(_seed)
        response = client.get(signed_url)

        assert response.status_code == 200
        assert response.content == b"# Virtual\n\nExact content."
        assert response.headers["content-type"].startswith("text/markdown")
        assert "Virtual-URL.md" in response.headers["content-disposition"]


def test_task_continuation_context_is_not_intention_eligible() -> None:
    event = _continuation_context_event("Task context", source="task_chat_context")

    assert event.type == "user_message"
    assert event.data == {
        "role": "user",
        "content": "Task context",
        "content_type": "text",
        "source": "task_chat_context",
        "intention_eligible": False,
        "prompt_visibility": "model_only",
        "prompt_provenance": {
            "kind": "internal_workflow_prompt",
            "source": "task_chat_context",
        },
    }


@pytest.mark.parametrize("intent", ["record_only", "context_only", "answer_pause"])
def test_non_revision_comment_intents_do_not_require_expected_attempt(intent: str) -> None:
    payload = TaskCommentCreateRequest(body="Comment", intent=intent)

    assert payload.expected_attempt is None


def test_request_revision_comment_requires_expected_attempt() -> None:
    with pytest.raises(ValueError, match="expected_attempt is required"):
        TaskCommentCreateRequest(body="Revise", intent="request_revision")


def test_task_revision_comment_attempt_cas_has_no_stale_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = Workflow(
        workflow_id="wf-comment-revision",
        name="Comment revision",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="build", type="run"),
            StepDefinition(name="review", type="run"),
        ],
    )
    state = WorkflowState(
        current_step_index=2,
        status="completed",
        step_outputs={
            "plan": {"summary": "plan"},
            "build": {"summary": "build"},
            "review": {"summary": "review"},
        },
        effective_workflow_definition=workflow.model_dump(mode="json"),
    )

    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                    status="active",
                )
                task = await create_task(
                    session,
                    task_id="task-comment-revision",
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    title="Comment revision",
                    status="completed",
                    workflow_id=workflow.workflow_id,
                    workflow_state=state.model_dump(mode="json"),
                )
                task.attempt_number = 2
                for name in ("plan", "build", "review"):
                    await create_step_run(
                        session,
                        task_id=task.task_id,
                        step_name=name,
                        step_type="run",
                        agent_id="agent-1",
                        step_run_id=f"sr-comment-{name}",
                        status="approved",
                        attempt_number=2,
                    )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        headers = _auth_headers(client.app, email="owner@example.com")
        client.app.state.task_queue._execution_store.claim_existing = AsyncMock(  # noqa: SLF001
            return_value=None
        )

        missing = client.post(
            f"/api/v1/tasks/{task_id}/comments",
            headers=headers,
            json={
                "body": "Missing attempt",
                "intent": "request_revision",
                "target_step": "build",
            },
        )
        assert missing.status_code == 422

        stale = client.post(
            f"/api/v1/tasks/{task_id}/comments",
            headers=headers,
            json={
                "body": "Stale revision",
                "intent": "request_revision",
                "target_step": "build",
                "expected_attempt": 1,
            },
        )
        assert stale.status_code == 409

        async def _verify_stale() -> None:
            async with client.app.state.session_factory() as session:
                task = await get_task(session, task_id)
                comments = await list_task_comments(session, task_id)
                step_runs = await list_step_runs_for_task(session, task_id)
            assert task is not None and task.attempt_number == 2
            assert comments == []
            assert {row.status for row in step_runs} == {"approved"}

        asyncio.run(_verify_stale())

        current = client.post(
            f"/api/v1/tasks/{task_id}/comments",
            headers=headers,
            json={
                "body": "Current revision",
                "intent": "request_revision",
                "target_step": "build",
                "expected_attempt": 2,
            },
        )
        assert current.status_code == 201
        response = current.json()
        assert response["attempt_number"] == 2
        assert response["applied"] is True
        assert response["metadata"]["action_result"]["new_attempt"] == 3
        assert response["metadata"]["action_result"] == {
            "new_attempt": 3,
            "target_step": "build",
            "superseded_count": 2,
            "relaunched": False,
        }

        async def _verify_current() -> None:
            async with client.app.state.session_factory() as session:
                task = await get_task(session, task_id)
                comments = await list_task_comments(session, task_id)
                step_runs = list(
                    await session.scalars(select(StepRun).where(StepRun.task_id == task_id))
                )
            assert task is not None and task.attempt_number == 3
            assert len(comments) == 1
            assert comments[0].body == "Current revision"
            by_step = {row.step_name: row.status for row in step_runs}
            assert by_step == {
                "plan": "approved",
                "build": "superseded",
                "review": "superseded",
            }

        asyncio.run(_verify_current())


@pytest.mark.asyncio
async def test_task_chat_omits_superseded_deliverable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_step_run(_session: object, _step_run_id: str) -> SimpleNamespace:
        return SimpleNamespace(task_id="task-1", attempt_number=1)

    monkeypatch.setattr("cognis.api.routes.tasks.get_step_run", _get_step_run)
    task = SimpleNamespace(
        task_id="task-1",
        attempt_number=2,
        result_data={},
    )
    old_deliverable = SimpleNamespace(
        deliverable_id="dlv-old",
        step_run_id="sr-old",
        attempt_number=1,
        status="approved",
        title="Old result",
    )

    content, deliverable_id = await _task_final_deliverable_content(
        object(),
        task,
        {"sr-old": [old_deliverable]},
    )

    assert content == ""
    assert deliverable_id is None
