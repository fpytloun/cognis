from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.attention_actions import project_attention_detail, project_attention_summary
from cognis.core.agent_loop import PendingPause
from cognis.core.management import resolve_task_pause_action, respond_task_input
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import WorkflowState
from cognis.store.models import NotificationRow
from cognis.store.queries import create_user


def _client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _headers(app: object, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email, role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def _row(
    notification_type: str,
    payload: dict[str, object],
    *,
    notification_id: str = "attention-1",
) -> NotificationRow:
    return NotificationRow(
        notification_id=notification_id,
        notification_type=notification_type,
        user_email="owner@example.com",
        conversation_id="conversation-1",
        task_id="task-1",
        payload=payload,
        status="pending",
        revision=4,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC),
    )


def test_summary_is_payload_free_and_detail_is_allowlisted() -> None:
    row = _row(
        "credential_request",
        {
            "credential_id": "production-api",
            "kind": "token",
            "label": "Production API",
            "required_fields": ["token"],
            "token": "must-not-leak",
            "metadata": {"secret": "must-not-leak"},
        },
    )

    summary = project_attention_summary(row, can_mutate=True)
    detail = project_attention_detail(row, can_mutate=True)

    assert "payload" not in summary.model_dump()
    assert summary.title == "Credential required"
    assert summary.has_action_form is True
    assert summary.can_resolve is True
    assert detail.display.required_fields == ["token"]
    assert detail.display.credential_id == "production-api"
    serialized = detail.model_dump_json()
    assert "must-not-leak" not in serialized


def test_oauth_url_and_code_are_detail_only_and_scheme_validated() -> None:
    row = _row(
        "auth_challenge",
        {
            "kind": "oauth_authorization",
            "metadata": {
                "authorization_url": "https://identity.example/authorize?state=private",
                "user_code": "ABCD-EFGH",
                "transaction_id": "private-transaction",
            },
        },
    )

    summary = project_attention_summary(row, can_mutate=True)
    detail = project_attention_detail(row, can_mutate=True)

    assert "identity.example" not in summary.model_dump_json()
    assert "ABCD-EFGH" not in summary.model_dump_json()
    assert detail.display.authorization_url == ("https://identity.example/authorize?state=private")
    assert detail.display.user_code == "ABCD-EFGH"
    assert "private-transaction" not in detail.model_dump_json()

    row.payload = {
        "kind": "oauth_authorization",
        "metadata": {"authorization_url": "javascript:alert(1)"},
    }
    unsafe = project_attention_detail(row, can_mutate=True)
    assert unsafe.display.authorization_url is None
    assert unsafe.can_resolve is False


def test_actions_are_explicit_and_viewers_are_read_only() -> None:
    gate = _row(
        "gate",
        {
            "message": "Deploy?",
            "options": [
                {"action": "accept_release", "label": "Accept"},
                {"action": "reject_release", "label": "Reject"},
            ],
        },
    )

    owner = project_attention_detail(gate, can_mutate=True)
    viewer = project_attention_detail(gate, can_mutate=False)

    assert [choice.action for choice in owner.allowed_actions] == [
        "accept_release",
        "reject_release",
    ]
    assert owner.has_action_form is True
    assert owner.can_resolve is True
    assert viewer.availability == "read_only"
    assert viewer.allowed_actions == []


def test_resolving_summary_is_not_actionable() -> None:
    row = _row("escalation", {"tool_name": "deploy"})
    row.status = "resolving"

    summary = project_attention_summary(row, can_mutate=True)

    assert summary.availability == "resolving"
    assert summary.has_action_form is True
    assert summary.can_resolve is False


@pytest.mark.parametrize(
    ("notification_type", "payload"),
    [
        ("unsupported_kind", {}),
        ("escalation", {}),
        ("gate", {"options": [{"label": "Approve"}]}),
        ("step_question", {"questions": "not-a-list"}),
        (
            "step_question",
            {
                "questions": [
                    {"id": "q1", "question": "Valid?", "options": []},
                    "malformed",
                ]
            },
        ),
        (
            "step_question",
            {
                "questions": [
                    {"id": "q1", "question": "First?", "options": []},
                    {"id": "q1", "question": "Duplicate?", "options": []},
                ]
            },
        ),
        (
            "step_question",
            {
                "questions": [
                    {
                        "id": "q1",
                        "question": "Invalid option?",
                        "options": [{"id": "", "label": "Broken"}],
                    }
                ]
            },
        ),
        (
            "step_question",
            {
                "questions": [
                    {
                        "id": "q1",
                        "question": "Impossible?",
                        "options": [],
                        "required": True,
                        "allow_custom": False,
                    }
                ]
            },
        ),
        ("credential_request", {"credential_id": "token", "kind": "token"}),
        ("auth_challenge", {"required_fields": ["code"]}),
        ("auth_challenge", {"kind": "otp_code", "required_fields": [123]}),
        ("auth_challenge", {"kind": "otp_code", "required_fields": ["code", 123]}),
        (
            "auth_challenge",
            {
                "kind": "oauth_authorization",
                "metadata": {"authorization_url": "javascript:alert(1)"},
            },
        ),
    ],
)
def test_malformed_and_unsupported_summaries_are_not_actionable(
    notification_type: str,
    payload: dict[str, object],
) -> None:
    summary = project_attention_summary(_row(notification_type, payload), can_mutate=True)

    assert summary.availability in {"recovery_required", "unsupported"}
    assert summary.can_resolve is False
    assert summary.has_action_form is False


def test_attention_detail_enforces_owner_and_resolution_is_idempotent(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.providers.guardrails.submit_decision = AsyncMock()

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=client.app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                await session.commit()
            await client.app.state.notification_service.create(
                notification_type="escalation",
                user_email="owner@example.com",
                conversation_id="conversation-1",
                notification_id="attention-owned",
                payload={
                    "tool_name": "deploy",
                    "arguments_display": {"token": "redacted-at-source", "target": "staging"},
                    "timeout_seconds": 300,
                },
            )

        asyncio.run(seed())
        owner_headers = _headers(client.app, "owner@example.com")
        other_headers = _headers(client.app, "other@example.com")

        assert (
            client.get(
                "/api/v1/attention-actions/attention-owned",
                headers=other_headers,
            ).status_code
            == 404
        )
        detail_response = client.get(
            "/api/v1/attention-actions/attention-owned",
            headers=owner_headers,
        )
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["revision"] == 1
        assert detail["display"]["arguments_display"]["token"] == "[redacted]"

        request = {
            "expected_revision": detail["revision"],
            "submission_id": "submission-owned-1",
            "action": "approve",
        }
        first = client.post(
            "/api/v1/attention-actions/attention-owned/resolve",
            headers=owner_headers,
            json=request,
        )
        second = client.post(
            "/api/v1/attention-actions/attention-owned/resolve",
            headers=owner_headers,
            json=request,
        )

        assert first.status_code == 200
        assert first.json()["status"] == "resolved"
        assert first.json()["revision"] > detail["revision"]
        assert second.status_code == 200
        assert second.json() == first.json()
        client.app.state.providers.guardrails.submit_decision.assert_awaited_once()


def test_stale_revision_and_expiry_return_conflict(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            await client.app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="owner@example.com",
                conversation_id="conversation-1",
                notification_id="expired-action",
                payload={
                    "kind": "otp_code",
                    "required_fields": ["code"],
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
            )
            await client.app.state.notification_service.create(
                notification_type="auth_challenge",
                user_email="owner@example.com",
                conversation_id="conversation-1",
                notification_id="fresh-action",
                payload={"kind": "otp_code", "required_fields": ["code"]},
            )

        asyncio.run(seed())
        headers = _headers(client.app, "owner@example.com")

        expired = client.post(
            "/api/v1/attention-actions/expired-action/resolve",
            headers=headers,
            json={
                "expected_revision": 1,
                "submission_id": "submission-expired",
                "action": "continue",
                "response_fields": {"code": "123456"},
            },
        )
        stale = client.post(
            "/api/v1/attention-actions/fresh-action/resolve",
            headers=headers,
            json={
                "expected_revision": 99,
                "submission_id": "submission-stale-1",
                "action": "continue",
                "response_fields": {"code": "123456"},
            },
        )

        assert expired.status_code == 409
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_task_helpers_reject_notification_for_an_old_pause() -> None:
    current_pause = PendingPause(
        pause_id="current-pause",
        pause_type="gate",
        task_id="task-1",
        step_name="current",
        options=[{"action": "continue", "label": "Continue"}],
    )
    pause_waiter = Mock()
    pause_waiter.find_pending.return_value = current_pause
    notification_service = AsyncMock()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="owner@example.com",
        agent_id="agent-1",
        status=TaskStatus.PAUSED,
        workflow_state=WorkflowState(),
    )

    with pytest.raises(ValueError, match="does not match the current task pause"):
        await resolve_task_pause_action(
            task=task,
            requested_action="continue",
            note="",
            pause_waiter=pause_waiter,
            notification_service=notification_service,
            task_queue=AsyncMock(),
            session_factory=AsyncMock(),
            user_email=task.created_by,
            notification_id="old-pause",
            expected_revision=1,
            submission_id="submission-old-gate",
        )

    notification_service.get.assert_not_awaited()
    notification_service.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_question_helper_rejects_notification_for_an_old_pause() -> None:
    current_pause = PendingPause(
        pause_id="current-question",
        pause_type="step_question",
        task_id="task-1",
        step_name="current",
        questions=[
            {
                "id": "answer",
                "question": "Continue?",
                "options": [],
                "multiple": False,
                "allow_custom": True,
                "required": True,
            }
        ],
    )
    pause_waiter = Mock()
    pause_waiter.find_pending.return_value = current_pause
    notification_service = AsyncMock()
    task = TaskModel(
        task_id="task-1",
        title="Task",
        created_by="owner@example.com",
        agent_id="agent-1",
        status=TaskStatus.PAUSED,
        workflow_state=WorkflowState(),
    )

    with pytest.raises(ValueError, match="does not match the current task pause"):
        await respond_task_input(
            task=task,
            reply={
                "mode": "structured",
                "answers": [
                    {
                        "question_id": "answer",
                        "selected_option_ids": [],
                        "custom_answer": "Yes",
                    }
                ],
            },
            pause_waiter=pause_waiter,
            notification_service=notification_service,
            task_queue=AsyncMock(),
            session_factory=AsyncMock(),
            user_email=task.created_by,
            notification_id="old-question",
            expected_revision=1,
            submission_id="submission-old-question",
        )

    notification_service.get.assert_not_awaited()
    notification_service.resolve.assert_not_awaited()
