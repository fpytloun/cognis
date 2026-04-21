from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.websocket import (
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    _handle_step_response,
)
from cognis.core.agent_loop import PendingPause
from cognis.core.decision import DecisionResult
from cognis.models.session import EventReadResult
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import WorkflowState
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_session,
    create_task,
    create_user,
    set_session_intaris_session_id,
    touch_conversation,
    update_conversation_active_session,
)


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
        response = client.post(
            "/api/v1/tasks",
            headers=_auth_headers(client.app, email="viewer@example.com", role="viewer"),
            json={"agent_id": "agent-1", "title": "Do work"},
        )
        assert response.status_code == 403


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
        response = client.post(
            "/api/v1/tasks/batch-submit",
            headers=_auth_headers(app, email="user@example.com"),
            json={"task_ids": [task_one, task_two, "missing-task"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        assert any(
            item["task_id"] == "missing-task" and item["status"] == "error"
            for item in body["results"]
        )


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
        assert response.status_code == 403


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
                            "question": "Need input",
                            "step_name": "plan",
                            "options": ["A", "B"],
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
            json={"step_name": "plan", "response": "A"},
        )
        assert response.status_code == 200
        assert called["resume"] is True


def test_websocket_step_response_surfaces_resume_conflict(
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
                            "pause_id": "input_ws_conflict",
                            "question": "Need input",
                            "step_name": "plan",
                            "options": ["A", "B"],
                        },
                    },
                )
                await session.commit()
                return task.task_id

        task_id = asyncio.run(_seed())
        asyncio.run(app.state.task_queue.recover_paused_tasks())

        async def _resume_conflict(task_id: str) -> TaskModel:
            raise ValueError("No execution capacity available to resume the task")

        app.state.task_queue.resume_task = _resume_conflict

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "token": _auth_headers(app, email="user@example.com")[
                        "Authorization"
                    ].removeprefix("Bearer "),
                }
            )
            assert ws.receive_json()["type"] == "authenticated"
            ws.send_json(
                {
                    "type": "step_response",
                    "task_id": task_id,
                    "step_name": "plan",
                    "response": "A",
                }
            )
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["code"] == "conflict"


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
                    "response": "A",
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
                    "response": "B",
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"

        resolved = asyncio.run(app.state.notification_service.get(notification.notification_id))
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution == {"decision": "continue", "response": "A", "state": "resolved"}


def test_websocket_direct_chat_step_response_conflicts_without_live_pause(
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
                payload={"question": "Need input"},
            )
        )
        app.state.pause_waiter.clear(notification.notification_id)

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
                    "response": "A",
                },
            )
        )
        assert manager.errors[-1]["code"] == "conflict"


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


def test_session_events_are_proxied(monkeypatch: object, tmp_path: Path) -> None:
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
                    title="Conversation",
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
                return session_row.session_id

        session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str, after_seq: int = 0, limit: int = 0, **_: object
        ) -> EventReadResult:
            assert after_seq == 0
            assert limit == 50
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "assistant_message",
                        "data": {"content": "hello"},
                        "ts": "2026-03-28T00:00:00Z",
                    }
                ],
                last_seq=1,
                has_more=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/sessions/{session_id}/events",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["items"][0]["type"] == "assistant_message"


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


def test_conversation_list_defaults_to_active_and_supports_archived_filter(
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
                deleted = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Deleted",
                )
                deleted.status = "deleted"
                await session.commit()
                return (
                    active.conversation_id,
                    archived.conversation_id,
                    deleted.conversation_id,
                )

        active_id, archived_id, deleted_id = asyncio.run(_seed())

        active_response = client.get(
            "/api/v1/conversations",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert active_response.status_code == 200
        assert [item["conversation_id"] for item in active_response.json()["items"]] == [active_id]

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


def test_deleted_conversation_is_hidden_from_detail_and_history(
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
                    title="Hidden",
                )
                conversation.status = "deleted"
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        detail_response = client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
        history_response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
        )

        assert detail_response.status_code == 404
        assert history_response.status_code == 404


def test_archived_conversation_history_loads_without_active_session(
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
                    title="Archived",
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
                conversation.status = "archived"
                await update_conversation_active_session(
                    session, conversation.conversation_id, None
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, expected_session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id == expected_session_id
            assert after_seq == 0
            assert limit == 0
            assert allow_missing_stream is True
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "assistant_message",
                        "data": {"content": "archived history"},
                        "ts": "2026-03-28T00:00:00Z",
                    }
                ],
                last_seq=1,
                has_more=False,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["active_session_id"] == expected_session_id
        assert body["items"][0]["type"] == "assistant_message"


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

        assert socket.sent == []
        assert conversation_id in connection.subscriptions


def test_conversation_messages_returns_empty_when_stream_missing(
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
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, _session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 7
            assert limit == 25
            assert allow_missing_stream is True
            return EventReadResult(
                events=[],
                last_seq=0,
                has_more=False,
                missing_stream_fallback_used=True,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?after_seq=7&limit=25",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "last_seq": 0,
            "has_more": False,
            "active_session_id": _session_id,
            "active_session_last_seq": 0,
            "history_truncated": False,
            "truncation_reason": None,
        }


def test_conversation_session_events_skip_malformed_rows(
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
        ) -> object:
            assert session_id
            assert allow_missing_stream is True
            return type(
                "EventRead",
                (),
                {
                    "events": [
                        {
                            "seq": 1,
                            "type": "assistant_message",
                            "data": {"content": "hello"},
                            "ts": "2026-03-28T00:00:00Z",
                        },
                        ["broken"],
                    ],
                    "last_seq": 1,
                    "has_more": False,
                    "missing_stream_fallback_used": False,
                },
            )()

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/events",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert len(body["items"]) == 1
        assert body["items"][0]["type"] == "assistant_message"


def test_conversation_session_events_return_empty_when_stream_missing(
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
            assert limit == 17
            assert allow_missing_stream is True
            return EventReadResult(
                events=[],
                last_seq=0,
                has_more=False,
                missing_stream_fallback_used=True,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/sessions/{session_id}/events?after_seq=3&limit=17",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json() == {
            "session_id": session_id,
            "items": [],
            "last_seq": 0,
            "has_more": False,
        }


def test_session_events_route_returns_empty_when_stream_missing(
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
                    title="Conversation",
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
                return session_row.session_id

        session_id = asyncio.run(_seed())

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            assert session_id
            assert after_seq == 9
            assert limit == 11
            assert allow_missing_stream is True
            return EventReadResult(
                events=[],
                last_seq=0,
                has_more=False,
                missing_stream_fallback_used=True,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/sessions/{session_id}/events?after_seq=9&limit=11",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json() == {
            "session_id": session_id,
            "items": [],
            "last_seq": 0,
            "has_more": False,
        }


def test_conversation_messages_hydrates_assistant_attachments_and_preserves_legacy_fallback(
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
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await create_artifact_record(
                    session,
                    artifact_id="img_1",
                    namespace="images",
                    object_id="img_1",
                    filename="image",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="image",
                    mime_type="image/jpeg",
                    size_bytes=123,
                    status="attached",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, _session_id = asyncio.run(_seed())

        async def _fake_read_events(**_: object) -> EventReadResult:
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "assistant_message",
                        "data": {
                            "content": "done",
                            "attachments": [
                                {
                                    "artifact_id": "img_1",
                                },
                                {
                                    "artifact_id": "img_legacy",
                                    "kind": "image",
                                    "mime_type": "image/png",
                                    "filename": "legacy.png",
                                    "size_bytes": 456,
                                    "url": "https://example.com/legacy.png",
                                },
                            ],
                        },
                    }
                ],
                last_seq=1,
                has_more=False,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        attachments = response.json()["items"][0]["data"]["attachments"]
        assert attachments[0]["artifact_id"] == "img_1"
        assert attachments[0]["filename"] == "img_1"
        assert attachments[0]["url"]
        assert attachments[1] == {
            "artifact_id": "img_legacy",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "legacy.png",
            "size_bytes": 456,
            "url": "https://example.com/legacy.png",
        }


def test_conversation_messages_preserves_existing_attachment_url_when_signing_fails(
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
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await create_artifact_record(
                    session,
                    artifact_id="img_2",
                    namespace="images",
                    object_id="img_2",
                    filename="image",
                    owner_email="user@example.com",
                    purpose="tool_output",
                    kind="image",
                    mime_type="image/jpeg",
                    size_bytes=123,
                    status="attached",
                )
                await session.commit()
                return conversation.conversation_id, session_row.session_id

        conversation_id, _session_id = asyncio.run(_seed())

        async def _fake_read_events(**_: object) -> EventReadResult:
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "assistant_message",
                        "data": {
                            "content": "done",
                            "attachments": [
                                {
                                    "artifact_id": "img_2",
                                    "kind": "image",
                                    "mime_type": "image/jpeg",
                                    "filename": "generated.jpg",
                                    "size_bytes": 123,
                                    "url": "/api/v1/images/img_2",
                                }
                            ],
                        },
                    }
                ],
                last_seq=1,
                has_more=False,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events
        app.state.artifact_store.async_get_public_url = AsyncMock(
            side_effect=RuntimeError("sign fail")
        )

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        attachments = response.json()["items"][0]["data"]["attachments"]
        assert attachments[0]["url"] == "/api/v1/images/img_2"


def test_websocket_queues_second_message_while_turn_active(
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
                agent = await create_agent(
                    session,
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id=agent.agent_id,
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id=agent.agent_id,
                )
                await set_session_intaris_session_id(
                    session, session_row.session_id, session_row.session_id
                )
                await update_conversation_active_session(
                    session, conversation.conversation_id, session_row.session_id
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        captured_direct_turn_kwargs: dict[str, object] = {}

        async def _fake_direct_turn(**kwargs: object) -> None:
            captured_direct_turn_kwargs.update(kwargs)
            await asyncio.sleep(0.2)
            return None

        async def _fake_decide(**_: object) -> DecisionResult:
            return DecisionResult(
                decision="inline",
                reason="test",
                confidence=1.0,
                predicted_tool_intensity="low",
            )

        app.state.workflow_engine.run_direct_turn = _fake_direct_turn
        app.state.decision_engine.decide = _fake_decide

        class _Entry:
            last_event_seq = 1

        async def _fake_refresh(session: object) -> object:
            return _Entry()

        app.state.session_cache.refresh = _fake_refresh

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "token": _auth_headers(app, email="user@example.com")[
                        "Authorization"
                    ].removeprefix("Bearer "),
                }
            )
            assert ws.receive_json()["type"] == "authenticated"
            ws.send_json(
                {"type": "message", "conversation_id": conversation_id, "content": "First"}
            )
            ws.send_json(
                {"type": "message", "conversation_id": conversation_id, "content": "Second"}
            )

            seen_types: set[str] = set()
            for _ in range(5):
                payload = ws.receive_json()
                seen_types.add(payload["type"])
                if payload["type"] == "queued":
                    break

            assert "queued" in seen_types
            for _ in range(5):
                payload = ws.receive_json()
                if payload["type"] == "message_complete":
                    assert captured_direct_turn_kwargs["bootstrap_wait_for_intention"] is False
                    break
