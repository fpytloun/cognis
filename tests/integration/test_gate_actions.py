from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cognis.core.agent_loop import PendingPause
from cognis.store.models import NotificationRow
from cognis.store.queries import create_agent, create_task, create_user
from tests.unit.test_api_stage7 import _auth_headers, _create_test_client


@pytest.mark.parametrize("live_pause", [False, True])
@pytest.mark.parametrize(
    ("stored_options", "decision", "expected"),
    [
        ([], "continue", [("continue", "Approve & continue"), ("cancel", "Cancel task")]),
        ([], "cancel", [("continue", "Approve & continue"), ("cancel", "Cancel task")]),
        (None, "continue", []),
        (
            [{"label": "Revise the plan", "action": "revise(plan)"}],
            "revise(plan)",
            [("revise(plan)", "Revise the plan")],
        ),
    ],
)
def test_existing_gate_exposes_and_accepts_only_effective_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_pause: bool,
    stored_options: list[dict[str, str]] | None,
    decision: str,
    expected: list[tuple[str, str]],
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="gate-owner@example.com",
                    name="Gate Owner",
                    password_hash=app.state.password_hasher.hash("gate-test-password"),
                    role="user",
                )
                await create_user(
                    session,
                    email="other-owner@example.com",
                    name="Other Owner",
                    password_hash=app.state.password_hasher.hash("other-test-password"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="gate-agent",
                    owner_email="gate-owner@example.com",
                    name="Gate Agent",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="gate-owner@example.com",
                    agent_id="gate-agent",
                    title="Review existing gate",
                    status="paused",
                    workflow_state={
                        "status": "paused",
                        "pending_pause_type": "gate",
                        "pending_pause_payload": {
                            "pause_id": "gate-empty",
                            "step_name": "review",
                            "options": stored_options,
                        },
                    },
                )
                session.add(
                    NotificationRow(
                        notification_id="gate-empty",
                        notification_type="gate",
                        user_email="gate-owner@example.com",
                        conversation_id="gate-review",
                        task_id=task.task_id,
                        step_name="review",
                        status="pending",
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                        payload={
                            "message": "Review?",
                            "options": stored_options,
                            "task_attempt": 1,
                        },
                    )
                )
                await session.commit()
                return task.task_id

        task_id = client.portal.call(seed)
        if live_pause:
            app.state.pause_waiter.register(
                PendingPause(
                    pause_id="gate-empty",
                    pause_type="gate",
                    task_id=task_id,
                    step_name="review",
                    question="Review?",
                    options=stored_options,
                )
            )
        headers = _auth_headers(app, email="gate-owner@example.com")
        task = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert task.status_code == 200
        options = task.json()["pending_pause"]["options"]
        if expected:
            assert [(option["action"], option["label"]) for option in options] == expected
        else:
            assert options is None
        attention = client.get("/api/v1/attention-actions/gate-empty", headers=headers)
        assert attention.status_code == 200
        assert attention.json()["availability"] == (
            "actionable" if expected else "recovery_required"
        )
        assert [action["action"] for action in attention.json()["allowed_actions"]] == [
            action for action, _ in expected
        ]
        foreign_headers = _auth_headers(app, email="other-owner@example.com")
        foreign = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=foreign_headers,
            json={"pause_id": "gate-empty", "step_name": "review", "action": decision},
        )
        assert foreign.status_code == 404
        stale = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=headers,
            json={"pause_id": "gate-old", "step_name": "review", "action": decision},
        )
        assert stale.status_code == 409
        rejected = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=headers,
            json={"pause_id": "gate-empty", "step_name": "review", "action": "reject"},
        )
        assert rejected.status_code == 409
        if stored_options is None:
            unavailable = client.post(
                f"/api/v1/tasks/{task_id}/gate-response",
                headers=headers,
                json={"pause_id": "gate-empty", "step_name": "review", "action": decision},
            )
            assert unavailable.status_code == 409
            assert (
                client.get("/api/v1/attention-actions/gate-empty", headers=headers).json()["status"]
                == "pending"
            )
            return

        resolved_decision = decision
        if not live_pause and not stored_options and decision == "continue":

            async def race() -> list[httpx.Response]:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as concurrent_client:
                    return await asyncio.gather(
                        *[
                            concurrent_client.post(
                                f"/api/v1/tasks/{task_id}/gate-response",
                                headers=headers,
                                json={
                                    "pause_id": "gate-empty",
                                    "step_name": "review",
                                    "action": action,
                                },
                            )
                            for action in ("continue", "cancel")
                        ]
                    )

            results = client.portal.call(race)
            assert sorted(result.status_code for result in results) == [200, 409]
            resolved_decision = ("continue", "cancel")[
                results.index(next(result for result in results if result.status_code == 200))
            ]
        else:
            approved = client.post(
                f"/api/v1/tasks/{task_id}/gate-response",
                headers=headers,
                json={"pause_id": "gate-empty", "step_name": "review", "action": decision},
            )
            assert approved.status_code == 200

        async def notification_state() -> tuple[str, str, object, int]:
            async with app.state.session_factory() as session:
                notification = await session.get(NotificationRow, "gate-empty")
                assert notification is not None
                return (
                    notification.status,
                    notification.resolution["decision"],
                    notification.payload["options"],
                    notification.revision,
                )

        before_retry = client.portal.call(notification_state)
        assert before_retry[:3] == ("resolved", resolved_decision, stored_options)
        repeated = client.post(
            f"/api/v1/tasks/{task_id}/gate-response",
            headers=headers,
            json={"pause_id": "gate-empty", "step_name": "review", "action": resolved_decision},
        )
        assert repeated.status_code == 200
        assert client.portal.call(notification_state) == before_retry
