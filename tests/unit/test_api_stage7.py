from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.middleware import AuthenticatedUser
from cognis.api.models import MessageEventResponse, TaskCreateRequest
from cognis.api.routes.conversations import _project_timeline_events
from cognis.api.routes.tasks import task_create
from cognis.api.websocket import (
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    _handle_step_response,
)
from cognis.core.agent_direct import AGENT_DIRECT_KIND, agent_direct_context_ref
from cognis.core.agent_loop import PendingPause
from cognis.core.decision import DecisionResult
from cognis.core.task_queue import TaskRerunResult
from cognis.models.session import (
    EventReadResult,
    IntarisAgentSummaryRecord,
    IntarisSession,
    IntarisSessionSummaries,
    IntarisSessionSummaryRecord,
)
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import WorkflowState
from cognis.store.models import NotificationRow
from cognis.store.queries import (
    create_agent,
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
    get_conversation,
    get_user_ui_state_value,
    set_session_intaris_session_id,
    set_session_status,
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


def test_timeline_projection_keeps_tool_delimited_assistant_phases() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={"session_id": "sess_a", "turn_id": "turn_1", "content": "First"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "name": "read",
                    "visible_name": "cat_file",
                    "canonical_name": "read",
                    "arguments": '{"file_path":"README.md"}',
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="tool_result",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "result": "ok",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
            MessageEventResponse(
                seq=4,
                type="assistant_message",
                data={"session_id": "sess_a", "turn_id": "turn_1", "content": "Second"},
                timestamp="2026-01-01T00:00:04Z",
            ),
        ]
    )

    assistant_messages = [
        item for item in items if item.get("kind") == "message" and item.get("role") == "assistant"
    ]

    assert [item["content"] for item in assistant_messages] == ["First", "Second"]
    assert [item["assistantPhaseIndex"] for item in assistant_messages] == [0, 1]
    assert items[1]["arguments"] == {"file_path": "README.md"}
    assert items[1]["sessionId"] == "sess_a"
    assert items[1]["toolName"] == "read"
    assert items[1]["displayToolName"] == "cat_file"
    assert items[1]["canonicalToolName"] == "read"


def test_timeline_projection_replaces_duplicate_system_notices() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="system_message",
                data={
                    "session_id": "sess_a",
                    "notice_id": "turn-start",
                    "kind": "turn_initiated",
                    "content": "Starting",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="system_message",
                data={
                    "session_id": "sess_a",
                    "notice_id": "turn-start",
                    "kind": "turn_initiated",
                    "content": "Started",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert items == [
        {
            "id": "system:turn-start",
            "kind": "system_message",
            "text": "Started",
            "noticeId": "turn-start",
            "noticeKind": "turn_initiated",
            "timestamp": "2026-01-01T00:00:02Z",
        }
    ]


def test_timeline_projection_groups_thinking_before_assistant_answer() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Answer",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "content": "first",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_2",
                    "content": "second",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["thinking", "message"]
    assert items[0]["messageId"] == "msg_1"
    assert items[0]["sessionId"] == "sess_a"
    assert [block["content"] for block in items[0]["blocks"]] == ["first", "second"]
    assert items[1]["content"] == "Answer"


def test_timeline_projection_hides_user_message_reasoning_echo() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "title": "Reasoning",
                    "content": "User message: check current repo state",
                    "reasoning_source": "reasoning",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Answer",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message"]
    assert items[0]["content"] == "Answer"


def test_timeline_projection_uses_unique_thinking_ids_across_tool_boundaries() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_1",
                    "content": "Before tool",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="tool_call",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "call_id": "call_1",
                    "tool_name": "read",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_thinking",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "block_2",
                    "content": "After tool",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    thinking_items = [item for item in items if item["kind"] == "thinking"]
    assert len(thinking_items) == 2
    assert thinking_items[0]["id"] != thinking_items[1]["id"]


def test_timeline_projection_hides_generic_reasoning_diagnostics() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="reasoning",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "block_id": "reason_1",
                    "summary": "The model considered alternatives.",
                    "source": "summary",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                session_id="sess_a",
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Final answer",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message"]
    assert items[0]["content"] == "Final answer"


def test_timeline_projection_hides_internal_audit_events() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                session_id="sess_a",
                type="developer_message",
                data={"content": "Internal context only"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "intention_updated", "intention": "debug issue"},
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "session_status_changed", "status": "completed"},
                timestamp="2026-01-01T00:00:03Z",
            ),
            MessageEventResponse(
                seq=4,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "session_created", "session_id": "sess_child"},
                timestamp="2026-01-01T00:00:04Z",
            ),
            MessageEventResponse(
                seq=5,
                session_id="sess_a",
                type="context_snapshot",
                data={"event": "context_snapshot", "summary": "internal context"},
                timestamp="2026-01-01T00:00:05Z",
            ),
            MessageEventResponse(
                seq=6,
                session_id="sess_a",
                type="evaluation",
                data={},
                timestamp="2026-01-01T00:00:06Z",
            ),
            MessageEventResponse(
                seq=7,
                session_id="sess_a",
                type="lifecycle",
                data={"event": "tool_discovery", "session_id": "sess_a"},
                timestamp="2026-01-01T00:00:07Z",
            ),
        ]
    )

    assert items == []


def test_timeline_projection_closes_assistant_phase_before_lifecycle_notice() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Before",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "system_notice",
                    "message": "Notice",
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "After",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "system_message", "message"]
    assert items[0]["content"] == "Before"
    assert items[2]["content"] == "After"


def test_timeline_projection_closes_assistant_phase_before_session_recovery() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "Before",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="session_recovered",
                data={"session_id": "sess_a"},
                timestamp="2026-01-01T00:00:02Z",
            ),
            MessageEventResponse(
                seq=3,
                type="assistant_message",
                data={
                    "session_id": "sess_a",
                    "turn_id": "turn_1",
                    "message_id": "msg_1",
                    "content": "After",
                },
                timestamp="2026-01-01T00:00:03Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "system_message", "message"]
    assert items[0]["content"] == "Before"
    assert items[2]["content"] == "After"


def test_timeline_projection_projects_delegation_without_fallback() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="user_message",
                data={"session_id": "sess_a", "content": "Hi"},
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="delegation",
                data={"session_id": "sess_a", "child_session_id": "sess_child"},
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert [item["kind"] for item in items] == ["message", "delegation"]
    assert items[1] == {
        "id": "delegation:sess_child",
        "kind": "delegation",
        "taskId": "sess_child",
        "taskLabel": "Sub-session",
        "status": "started",
        "timestamp": "2026-01-01T00:00:02Z",
    }


def test_timeline_projection_uses_persisted_delegation_title() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "title": "Explore project",
                    "status": "completed",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items[0]["taskLabel"] == "Explore project"


def test_timeline_projection_preserves_terminal_delegation_state() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "title": "Explore project",
                    "status": "completed",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
            MessageEventResponse(
                seq=2,
                type="delegation",
                data={
                    "session_id": "sess_a",
                    "child_session_id": "sess_child",
                    "status": "running",
                    "todos": [{"content": "stale", "status": "pending"}],
                },
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert len(items) == 1
    assert items[0]["taskLabel"] == "Explore project"
    assert items[0]["status"] == "completed"
    assert items[0]["result"] == "done"
    assert items[0]["timestamp"] == "2026-01-01T00:00:01Z"
    assert "todos" not in items[0]


def test_timeline_projection_projects_lifecycle_workflow_composed() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "workflow_composed",
                    "workflow_id": "wf_1",
                    "workflow_name": "Workflow",
                    "steps": ["plan", "build"],
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items == [
        {
            "id": "workflow-composed:sess_a:1",
            "kind": "workflow_composed",
            "workflowId": "wf_1",
            "workflowName": "Workflow",
            "lifecycle": "ephemeral",
            "steps": ["plan", "build"],
            "timestamp": "2026-01-01T00:00:01Z",
        }
    ]


def test_timeline_projection_uses_lifecycle_task_title() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="lifecycle",
                data={
                    "session_id": "sess_a",
                    "event": "task_result",
                    "task_id": "task_1",
                    "task_title": "Build projection",
                    "result_summary": "done",
                },
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items[0]["kind"] == "delegation"
    assert items[0]["taskLabel"] == "Build projection"
    assert items[0]["status"] == "completed"


def test_timeline_projection_projects_unknown_events_as_notices() -> None:
    items = _project_timeline_events(
        [
            MessageEventResponse(
                seq=1,
                type="future_event",
                data={"session_id": "sess_a", "value": 1},
                timestamp="2026-01-01T00:00:01Z",
            ),
        ]
    )

    assert items == [
        {
            "id": "event-notice:sess_a:1",
            "kind": "notice",
            "title": "Conversation event",
            "description": "Unsupported persisted event: future_event",
            "tone": "info",
            "timestamp": "2026-01-01T00:00:01Z",
        }
    ]


def test_viewer_cannot_create_task(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
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
            queued_messages=lambda _conversation_id: [],
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
        send_response = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "direct target send"},
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
        response = client.get(
            f"/api/v1/sessions/{session_id}/intaris",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["intaris_session_id"] == "intaris-session-1"
        assert body["intention"] == "Intaris intention"
        assert body["summary"] == "Latest Intaris summary"


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
                    "mode": "structured",
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": ["A"],
                            "custom_answer": None,
                        }
                    ],
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

        async def _seed() -> tuple[str, str, str, str]:
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
                await session.commit()
                return (
                    active.conversation_id,
                    archived.conversation_id,
                    starred.conversation_id,
                    deleted.conversation_id,
                )

        active_id, archived_id, starred_id, deleted_id = asyncio.run(_seed())

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


def test_first_message_slash_command_bootstraps_root_session(
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
                    title="New conversation",
                )
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())

        async def _create_session(**_: object) -> None:
            return None

        original_create_session = app.state.providers.guardrails.create_session
        app.state.providers.guardrails.create_session = _create_session
        try:
            response = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers=_auth_headers(app, email="user@example.com"),
                json={"content": "/plan"},
            )
        finally:
            app.state.providers.guardrails.create_session = original_create_session

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "command_executed"
        assert body["result"]["type"] == "system_message"
        assert body["result"]["data"] == {
            "chat_mode": "plan",
            "chat_mode_source": "conversation_override",
        }

        async def _load_active_session_id() -> str | None:
            async with app.state.session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                return conversation.active_session_id if conversation is not None else None

        assert asyncio.run(_load_active_session_id()) is not None


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

        user_messages = [
            payload for payload in socket.sent if payload.get("type") == "user_message"
        ]
        assert user_messages == [
            {
                "type": "user_message",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "message_id": "client:cmsg_1",
                "event_id": "client:cmsg_1",
                "timestamp": "2026-03-28T00:00:00Z",
                "seq": 4,
                "turn_id": "turn_1",
                "content": "hello",
                "attachments": [],
                "queue_id": None,
                "client_message_id": "cmsg_1",
                "chat_mode": None,
                "chat_mode_source": None,
            }
        ]
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
        payload = response.json()
        state_snapshot = payload.pop("state_snapshot")
        assert payload == {
            "items": [],
            "last_seq": 0,
            "has_more": False,
            "older_cursor": None,
            "has_active_turn": False,
            "active_streams": [],
            "active_tool_outputs": [],
            "active_session_id": _session_id,
            "active_session_last_seq": 0,
            "history_truncated": False,
            "truncation_reason": None,
        }
        assert state_snapshot["conversation_id"] == conversation_id
        assert state_snapshot["conversation_kind"] == "normal"
        assert state_snapshot["task"] is None
        assert state_snapshot["active_session"]["session_id"] == _session_id


def test_conversation_messages_latest_page_reads_only_tail_sessions(
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
                    context_type="signal",
                    title="Long Signal conversation",
                )
                previous = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="sess-prev",
                )
                active = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    previous_session_id=previous.session_id,
                    session_id="sess-active",
                )
                await set_session_intaris_session_id(
                    session, previous.session_id, previous.session_id
                )
                await set_session_intaris_session_id(session, active.session_id, active.session_id)
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    active.session_id,
                )
                await session.commit()
                return conversation.conversation_id, previous.session_id, active.session_id

        conversation_id, previous_session_id, active_session_id = client.portal.call(_seed)
        calls: list[dict[str, object]] = []

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            last_n: int | None = None,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            calls.append(
                {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "limit": limit,
                    "last_n": last_n,
                    "allow_missing_stream": allow_missing_stream,
                }
            )
            if session_id == active_session_id:
                return EventReadResult(
                    events=[
                        {
                            "seq": 9,
                            "type": "assistant_message",
                            "data": {"content": "active tail"},
                            "ts": "2026-03-28T00:00:00Z",
                        }
                    ],
                    last_seq=9,
                    has_more=True,
                    missing_stream_fallback_used=False,
                )
            if session_id == previous_session_id:
                return EventReadResult(
                    events=[
                        {
                            "seq": 4,
                            "type": "assistant_message",
                            "data": {"content": "previous tail"},
                            "ts": "2026-03-27T00:00:00Z",
                        }
                    ],
                    last_seq=4,
                    has_more=False,
                    missing_stream_fallback_used=False,
                )
            raise AssertionError(f"unexpected session read: {session_id}")

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=1",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["data"]["content"] for item in body["items"]] == ["active tail"]
        assert body["has_more"] is True
        assert body["older_cursor"]
        assert body["active_session_id"] == active_session_id
        assert body["active_session_last_seq"] == 9
        assert calls == [
            {
                "session_id": active_session_id,
                "after_seq": 0,
                "limit": 0,
                "last_n": 1,
                "allow_missing_stream": True,
            }
        ]


def test_conversation_messages_latest_page_includes_compaction_marker(
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
                    title="Compacted chat",
                )
                previous = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="sess-marker-prev",
                )
                active = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    previous_session_id=previous.session_id,
                    session_id="sess-marker-active",
                )
                await set_session_intaris_session_id(
                    session, previous.session_id, previous.session_id
                )
                await set_session_intaris_session_id(session, active.session_id, active.session_id)
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    active.session_id,
                )
                await session.commit()
                return conversation.conversation_id, previous.session_id, active.session_id

        conversation_id, _previous_session_id, active_session_id = client.portal.call(_seed)
        calls: list[dict[str, object]] = []

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            last_n: int | None = None,
            allow_missing_stream: bool = False,
            **_: object,
        ) -> EventReadResult:
            calls.append(
                {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "limit": limit,
                    "last_n": last_n,
                    "allow_missing_stream": allow_missing_stream,
                }
            )
            if session_id != active_session_id:
                raise AssertionError(f"unexpected session read: {session_id}")
            if last_n == 1:
                return EventReadResult(
                    events=[
                        {
                            "seq": 2,
                            "type": "system_message",
                            "data": {
                                "content": "Automatic compaction is starting before this turn continues.",
                                "notice_id": "notice-compaction-start",
                                "kind": "compaction_start",
                            },
                            "ts": "2026-03-28T00:00:01Z",
                        }
                    ],
                    last_seq=2,
                    has_more=True,
                    missing_stream_fallback_used=False,
                )
            return EventReadResult(
                events=[
                    {
                        "seq": 1,
                        "type": "compaction_summary",
                        "data": {
                            "summary": "Durable compaction summary",
                            "method": "rotation",
                            "source_session_id": "sess-marker-prev",
                        },
                        "ts": "2026-03-28T00:00:00Z",
                    },
                    {
                        "seq": 2,
                        "type": "system_message",
                        "data": {
                            "content": "Automatic compaction is starting before this turn continues.",
                            "notice_id": "notice-compaction-start",
                            "kind": "compaction_start",
                        },
                        "ts": "2026-03-28T00:00:01Z",
                    },
                ],
                last_seq=2,
                has_more=False,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=1",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["type"] for item in body["items"]] == [
            "compaction_summary",
            "system_message",
        ]
        assert body["items"][0]["data"]["session_id"] == active_session_id
        assert body["items"][0]["data"]["source_session_id"] == "sess-marker-prev"
        assert body["items"][0]["data"]["summary"] == "Durable compaction summary"
        assert calls == [
            {
                "session_id": active_session_id,
                "after_seq": 0,
                "limit": 0,
                "last_n": 1,
                "allow_missing_stream": True,
            },
            {
                "session_id": active_session_id,
                "after_seq": 0,
                "limit": 25,
                "last_n": None,
                "allow_missing_stream": True,
            },
        ]


def test_conversation_messages_before_cursor_loads_previous_page(
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
                    context_type="signal",
                    title="Long Signal conversation",
                )
                previous = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="sess-prev",
                )
                active = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    previous_session_id=previous.session_id,
                    session_id="sess-active",
                )
                await set_session_intaris_session_id(
                    session, previous.session_id, previous.session_id
                )
                await set_session_intaris_session_id(session, active.session_id, active.session_id)
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    active.session_id,
                )
                await session.commit()
                return conversation.conversation_id, previous.session_id, active.session_id

        conversation_id, previous_session_id, active_session_id = client.portal.call(_seed)

        async def _fake_read_events(
            session_id: str,
            after_seq: int = 0,
            limit: int = 0,
            last_n: int | None = None,
            **_: object,
        ) -> EventReadResult:
            if session_id == active_session_id and last_n == 1:
                return EventReadResult(
                    events=[
                        {
                            "seq": 9,
                            "type": "assistant_message",
                            "data": {"content": "active tail"},
                            "ts": "2026-03-28T00:00:00Z",
                        }
                    ],
                    last_seq=9,
                    has_more=True,
                    missing_stream_fallback_used=False,
                )
            if session_id == active_session_id:
                assert after_seq == 7
                assert limit == 2
                return EventReadResult(
                    events=[],
                    last_seq=9,
                    has_more=False,
                    missing_stream_fallback_used=False,
                )
            if session_id == previous_session_id:
                assert last_n == 1
                return EventReadResult(
                    events=[
                        {
                            "seq": 4,
                            "type": "assistant_message",
                            "data": {"content": "previous tail"},
                            "ts": "2026-03-27T00:00:00Z",
                        }
                    ],
                    last_seq=4,
                    has_more=False,
                    missing_stream_fallback_used=False,
                )
            raise AssertionError(f"unexpected session read: {session_id}")

        async def _fake_get_last_seq(session_id: str) -> int:
            assert session_id == active_session_id
            return 9

        app.state.providers.guardrails.read_events = _fake_read_events
        app.state.providers.guardrails.get_last_seq = _fake_get_last_seq

        first = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=1",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert first.status_code == 200
        cursor = first.json()["older_cursor"]
        assert cursor

        second = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=1&before={cursor}",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert second.status_code == 200
        body = second.json()
        assert [item["data"]["content"] for item in body["items"]] == ["previous tail"]
        assert body["active_session_id"] == active_session_id
        assert body["active_session_last_seq"] == 9
        assert body["has_more"] is False
        assert body["older_cursor"] is None


def test_conversation_messages_latest_page_preserves_seq_order_despite_timestamps(
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
                    context_type="signal",
                    title="Long Signal conversation",
                )
                active = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="sess-active",
                )
                await set_session_intaris_session_id(session, active.session_id, active.session_id)
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    active.session_id,
                )
                await session.commit()
                return conversation.conversation_id, active.session_id

        conversation_id, active_session_id = client.portal.call(_seed)

        async def _fake_read_events(
            session_id: str,
            last_n: int | None = None,
            **_: object,
        ) -> EventReadResult:
            assert session_id == active_session_id
            assert last_n == 4
            return EventReadResult(
                events=[
                    {
                        "seq": 4,
                        "type": "assistant_message",
                        "data": {"content": "done"},
                        "ts": "2026-03-28T00:00:01Z",
                    },
                    {
                        "seq": 2,
                        "type": "tool_call",
                        "data": {"call_id": "call-1", "name": "bash"},
                        "ts": "2026-03-28T00:00:04Z",
                    },
                    {
                        "seq": 3,
                        "type": "tool_result",
                        "data": {"call_id": "call-1", "content": "output"},
                        "ts": "2026-03-28T00:00:05Z",
                    },
                    {
                        "seq": 1,
                        "type": "user_message",
                        "data": {"content": "run"},
                        "ts": "2026-03-28T00:00:03Z",
                    },
                ],
                last_seq=4,
                has_more=False,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=4",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert [item["type"] for item in response.json()["items"]] == [
            "user_message",
            "tool_call",
            "tool_result",
            "assistant_message",
        ]


def test_conversation_messages_latest_page_drops_orphan_tool_results(
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
                    context_type="signal",
                    title="Long Signal conversation",
                )
                active = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    session_id="sess-active",
                )
                await set_session_intaris_session_id(session, active.session_id, active.session_id)
                await update_conversation_active_session(
                    session,
                    conversation.conversation_id,
                    active.session_id,
                )
                await session.commit()
                return conversation.conversation_id, active.session_id

        conversation_id, active_session_id = client.portal.call(_seed)

        async def _fake_read_events(
            session_id: str,
            last_n: int | None = None,
            **_: object,
        ) -> EventReadResult:
            assert session_id == active_session_id
            assert last_n == 4
            return EventReadResult(
                events=[
                    {
                        "seq": 20,
                        "type": "tool_result",
                        "data": {"call_id": "older-call", "result": "stale output"},
                        "ts": "2026-03-28T00:00:20Z",
                    },
                    {
                        "seq": 21,
                        "type": "user_message",
                        "data": {"content": "current"},
                        "ts": "2026-03-28T00:00:21Z",
                    },
                    {
                        "seq": 22,
                        "type": "tool_call",
                        "data": {"call_id": "current-call", "name": "read"},
                        "ts": "2026-03-28T00:00:22Z",
                    },
                    {
                        "seq": 23,
                        "type": "tool_result",
                        "data": {"call_id": "current-call", "result": "current output"},
                        "ts": "2026-03-28T00:00:23Z",
                    },
                    {
                        "seq": 24,
                        "type": "assistant_message",
                        "data": {"content": "done"},
                        "ts": "2026-03-28T00:00:24Z",
                    },
                ],
                last_seq=24,
                has_more=True,
                missing_stream_fallback_used=False,
            )

        app.state.providers.guardrails.read_events = _fake_read_events

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?anchor=latest&limit=4",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["type"] for item in items] == [
            "user_message",
            "tool_call",
            "tool_result",
            "assistant_message",
        ]
        assert all(item["data"].get("call_id") != "older-call" for item in items)


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
            "active_thinking": [],
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
            "active_thinking": [],
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
                )
                await session.commit()

                from cognis.core.content_refs import (
                    build_deliverable_public_url,
                    get_accessible_deliverable_ref,
                )

                async with app.state.session_factory() as session:
                    ref = await get_accessible_deliverable_ref(
                        session, "dlv_virtual_url", "user@example.com"
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
                if payload["type"] in {"queued", "queued_messages_updated"}:
                    break

            assert seen_types & {"queued", "queued_messages_updated"}
            for _ in range(5):
                payload = ws.receive_json()
                if payload["type"] == "message_complete":
                    assert captured_direct_turn_kwargs["bootstrap_wait_for_intention"] is False
                    break
