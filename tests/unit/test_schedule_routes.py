from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    attach_project_workflow,
    create_agent,
    create_project,
    create_schedule,
    create_task,
    create_user,
    create_workflow,
    get_task,
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
                    agent_profiles={
                        "fast": {
                            "profile_id": "fast",
                            "description": "Fast responses",
                            "enabled": True,
                        }
                    },
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


def test_schedule_create_rejects_other_user_workflow(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
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
                    name="Agent",
                    status="active",
                )
                await create_workflow(
                    session,
                    workflow_id="wf_other_schedule",
                    name="Other Schedule Workflow",
                    definition={
                        "workflow_id": "wf_other_schedule",
                        "name": "Other Schedule Workflow",
                        "steps": [{"name": "run", "type": "run"}],
                        "owner_email": "other@example.com",
                    },
                    is_system=False,
                    owner_email="other@example.com",
                )
                await session.commit()

        asyncio.run(_seed())

        response = client.post(
            "/api/v1/schedules",
            headers=_auth_headers(app, email="owner@example.com"),
            json={
                "name": "Schedule",
                "schedule_type": "cron",
                "cron_expr": "0 9 * * *",
                "agent_id": "agent-1",
                "workflow_id": "wf_other_schedule",
            },
        )

        assert response.status_code == 404


def test_schedule_agent_profile_round_trip(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
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
                    name="Agent",
                    status="active",
                    agent_profiles={
                        "fast": {
                            "profile_id": "fast",
                            "description": "Fast responses",
                            "enabled": True,
                        },
                        "quality": {
                            "profile_id": "quality",
                            "description": "Quality responses",
                            "enabled": True,
                        },
                    },
                    default_agent_profile_id="fast",
                )
                await session.commit()

        asyncio.run(_seed())
        headers = _auth_headers(app, email="owner@example.com")

        create_response = client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "Profiled schedule",
                "schedule_type": "cron",
                "cron_expr": "0 9 * * *",
                "agent_id": "agent-1",
                "agent_profile_id": "fast",
                "task_template": {"title": "Profiled run"},
            },
        )
        assert create_response.status_code == 201
        schedule_id = create_response.json()["schedule_id"]
        assert create_response.json()["agent_profile_id"] == "fast"

        detail_response = client.get(f"/api/v1/schedules/{schedule_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["agent_profile_id"] == "fast"

        list_response = client.get("/api/v1/schedules", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["agent_profile_id"] == "fast"

        update_response = client.put(
            f"/api/v1/schedules/{schedule_id}",
            headers=headers,
            json={"agent_profile_id": "quality"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["agent_profile_id"] == "quality"

        clear_response = client.put(
            f"/api/v1/schedules/{schedule_id}",
            headers=headers,
            json={"agent_profile_id": None},
        )
        assert clear_response.status_code == 200
        assert clear_response.json()["agent_profile_id"] is None


def test_schedule_update_rejects_clearing_project_for_bound_workflow(
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
                    name="Agent",
                    status="active",
                )
                project = await create_project(
                    session,
                    project_id="project-bound",
                    owner_email="owner@example.com",
                    name="Project",
                )
                await attach_project_workflow(session, project.project_id, "system:research")
                schedule = await create_schedule(
                    session,
                    created_by="owner@example.com",
                    agent_id="agent-1",
                    name="Schedule",
                    schedule_type="cron",
                    cron_expr="0 9 * * *",
                    task_template={"title": "Scheduled task"},
                    workflow_id="system:research",
                    project_id=project.project_id,
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())

        response = client.put(
            f"/api/v1/schedules/{schedule_id}",
            headers=_auth_headers(app, email="owner@example.com"),
            json={"project_id": None},
        )

        assert response.status_code == 404


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


def test_schedule_list_filters_by_project(monkeypatch: object, tmp_path: Path) -> None:
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
                project_one = await create_project(
                    session,
                    project_id="project-one",
                    owner_email="user@example.com",
                    name="Project One",
                )
                project_two = await create_project(
                    session,
                    project_id="project-two",
                    owner_email="user@example.com",
                    name="Project Two",
                )
                first = await create_schedule(
                    session,
                    name="First schedule",
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    project_id=project_one.project_id,
                    task_template={},
                    next_fire_at=datetime.now(UTC) + timedelta(hours=1),
                    created_by="user@example.com",
                )
                await create_schedule(
                    session,
                    name="Second schedule",
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    project_id=project_two.project_id,
                    task_template={},
                    next_fire_at=datetime.now(UTC) + timedelta(hours=1),
                    created_by="user@example.com",
                )
                await session.commit()
                return project_one.project_id, first.schedule_id

        project_id, schedule_id = asyncio.run(_seed())

        response = client.get(
            "/api/v1/schedules",
            headers=_auth_headers(app, email="user@example.com"),
            params={"project_id": project_id},
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["schedule_id"] for item in body] == [schedule_id]


def test_schedule_trigger_returns_created_task_id(monkeypatch: object, tmp_path: Path) -> None:
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
                    name="Manual trigger",
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    agent_profile_id="fast",
                    task_template={"title": "Triggered task"},
                    created_by="user@example.com",
                )
                await session.commit()
                return schedule.schedule_id

        schedule_id = asyncio.run(_seed())

        response = client.post(
            f"/api/v1/schedules/{schedule_id}/trigger",
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["task_id"].startswith("task_")
        assert body["schedule_id"] == schedule_id
        assert body["last_run_status"] in {"queued", "ready", "running"}

        async def _load_task_profile(task_id: str) -> str | None:
            async with app.state.session_factory() as session:
                task = await get_task(session, task_id)
                return task.agent_profile_id if task is not None else None

        assert asyncio.run(_load_task_profile(body["task_id"])) == "fast"


def test_project_response_counts_active_schedules(monkeypatch: object, tmp_path: Path) -> None:
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
                project = await create_project(
                    session,
                    project_id="project-counts",
                    owner_email="user@example.com",
                    name="Project Counts",
                )
                await create_schedule(
                    session,
                    name="Active schedule",
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    project_id=project.project_id,
                    task_template={},
                    next_fire_at=datetime.now(UTC) + timedelta(hours=1),
                    created_by="user@example.com",
                )
                await create_schedule(
                    session,
                    name="Expired one-shot",
                    schedule_type="one_shot",
                    one_shot_at=datetime.now(UTC) - timedelta(hours=1),
                    timezone="UTC",
                    agent_id="agent-1",
                    project_id=project.project_id,
                    task_template={},
                    next_fire_at=None,
                    created_by="user@example.com",
                )
                await create_schedule(
                    session,
                    name="Disabled schedule",
                    schedule_type="interval",
                    interval_seconds=600,
                    timezone="UTC",
                    agent_id="agent-1",
                    project_id=project.project_id,
                    task_template={},
                    enabled=False,
                    next_fire_at=datetime.now(UTC) + timedelta(hours=1),
                    created_by="user@example.com",
                )
                await session.commit()
                return project.project_id

        project_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        list_response = client.get("/api/v1/projects", headers=headers)
        assert list_response.status_code == 200
        listed_project = next(
            item for item in list_response.json() if item["project_id"] == project_id
        )
        assert listed_project["active_schedule_count"] == 1

        detail_response = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["active_schedule_count"] == 1
