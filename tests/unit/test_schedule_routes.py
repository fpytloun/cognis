from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_agent,
    create_schedule,
    create_task,
    create_user,
    update_schedule_fire_state,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_schedule_routes_report_latest_task_status(monkeypatch: object, tmp_path: Path) -> None:
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
                schedule = await create_schedule(
                    session,
                    name="Heartbeat",
                    description=None,
                    schedule_type="interval",
                    interval_seconds=1800,
                    timezone="UTC",
                    agent_id="agent-1",
                    workflow_id=None,
                    task_template={},
                    created_by="user@example.com",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Heartbeat run",
                    status="running",
                    source_type="scheduler",
                    source_ref=schedule.schedule_id,
                )
                await update_schedule_fire_state(
                    session,
                    schedule.schedule_id,
                    last_fired_at=task.created_at or datetime.now(UTC),
                    next_fire_at=None,
                    last_run_status="success",
                    consecutive_errors=0,
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        list_response = client.get("/api/v1/schedules", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["last_run_status"] == "running"

        detail_response = client.get(f"/api/v1/schedules/{schedule_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["last_run_status"] == "running"


def test_schedule_routes_preserve_scheduler_failure_without_new_task(
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
                schedule = await create_schedule(
                    session,
                    name="Daily review",
                    description=None,
                    schedule_type="cron",
                    cron_expr="0 9 * * *",
                    timezone="UTC",
                    agent_id="agent-1",
                    workflow_id=None,
                    task_template={},
                    created_by="user@example.com",
                )
                await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Previous run",
                    status="completed",
                    source_type="scheduler",
                    source_ref=schedule.schedule_id,
                )
                await update_schedule_fire_state(
                    session,
                    schedule.schedule_id,
                    last_fired_at=datetime.now(UTC) + timedelta(days=1),
                    next_fire_at=None,
                    last_run_status="failed",
                    consecutive_errors=1,
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        list_response = client.get("/api/v1/schedules", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["last_run_status"] == "failed"

        detail_response = client.get(f"/api/v1/schedules/{schedule_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["last_run_status"] == "failed"


def test_schedule_disable_response_keeps_latest_task_status(
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
                schedule = await create_schedule(
                    session,
                    name="Recurring check",
                    description=None,
                    schedule_type="interval",
                    interval_seconds=900,
                    timezone="UTC",
                    agent_id="agent-1",
                    workflow_id=None,
                    task_template={},
                    created_by="user@example.com",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Active run",
                    status="running",
                    source_type="scheduler",
                    source_ref=schedule.schedule_id,
                )
                await update_schedule_fire_state(
                    session,
                    schedule.schedule_id,
                    last_fired_at=task.created_at or datetime.now(UTC),
                    next_fire_at=None,
                    last_run_status="success",
                    consecutive_errors=0,
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        disable_response = client.post(f"/api/v1/schedules/{schedule_id}/disable", headers=headers)
        assert disable_response.status_code == 200
        body = disable_response.json()
        assert body["enabled"] is False
        assert body["last_run_status"] == "running"


def test_schedule_status_ignores_other_users_tasks(monkeypatch: object, tmp_path: Path) -> None:
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
                await create_user(
                    session,
                    email="other@example.com",
                    name="Other User",
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
                    owner_email="other@example.com",
                    name="Agent 2",
                    status="active",
                )
                schedule = await create_schedule(
                    session,
                    name="Scoped status",
                    description=None,
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    workflow_id=None,
                    task_template={},
                    created_by="user@example.com",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="agent-1",
                    title="Owner task",
                    status="running",
                    source_type="scheduler",
                    source_ref=schedule.schedule_id,
                )
                await create_task(
                    session,
                    created_by="other@example.com",
                    agent_id="agent-2",
                    title="Foreign task",
                    status="failed",
                    source_type="scheduler",
                    source_ref=schedule.schedule_id,
                )
                await update_schedule_fire_state(
                    session,
                    schedule.schedule_id,
                    last_fired_at=task.created_at or datetime.now(UTC),
                    next_fire_at=None,
                    last_run_status="success",
                    consecutive_errors=0,
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        detail_response = client.get(f"/api/v1/schedules/{schedule_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["last_run_status"] == "running"
