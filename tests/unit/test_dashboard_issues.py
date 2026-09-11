from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

from cognis.api.app import create_app
from cognis.api.dashboard_issues import (
    EXECUTOR_OFFLINE_GRACE,
    ISSUE_LIMIT,
    schedule_incident_token,
)
from cognis.store.models import MCPOAuthTokenRow, Schedule
from cognis.store.queries import (
    create_agent,
    create_executor,
    create_mcp_server,
    create_schedule,
    create_user,
)


def _client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _headers(app: object, email: str, *, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, "User", role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_taskless_schedule_incidents_use_the_durable_fire_time() -> None:
    first = schedule_incident_token(
        "sched-taskless",
        None,
        datetime(2026, 8, 25, tzinfo=UTC),
    )
    second = schedule_incident_token(
        "sched-taskless",
        None,
        datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert first != second


def test_dashboard_issues_are_user_scoped_typed_and_bounded(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=client.app.state.password_hasher.hash("password123"),
                    )
                owned_mcp = await create_mcp_server(
                    session,
                    server_id="mcp-owned",
                    name="Owned MCP",
                    transport="streamable_http",
                    url="https://mcp.invalid/mcp",
                    auth_config={"type": "oauth2", "issuer": "https://issuer.invalid"},
                    owner_email="owner@example.com",
                )
                await create_mcp_server(
                    session,
                    server_id="mcp-other",
                    name="Secret Other MCP",
                    transport="stdio",
                    command="secret-command",
                    headers={"Authorization": "Bearer secret-token"},
                    owner_email="other@example.com",
                )
                unavailable = await create_executor(
                    session,
                    executor_id="exec-unavailable",
                    name="Unavailable",
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                unavailable.status = "active"
                unavailable.runtime_state = "blocked"
                unavailable.desired_config_version = 3
                unavailable.applied_config_version = 2
                unavailable.runtime_metadata = {
                    "warnings": ["secret-token"],
                    "mcp_servers": [
                        {
                            "server_id": owned_mcp.server_id,
                            "status": "failed",
                            "authorization_required": True,
                            "error": "secret-token",
                        },
                        {"server_id": "mcp-other", "status": "failed"},
                    ],
                }
                unavailable.last_observed_at = now
                degraded = await create_executor(
                    session,
                    executor_id="exec-degraded",
                    name="Degraded",
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                degraded.status = "active"
                degraded.runtime_state = "degraded"
                degraded.desired_config_version = 1
                degraded.applied_config_version = 1
                degraded.observed_tools = []
                degraded.last_observed_at = now - timedelta(minutes=20)
                foreign = await create_executor(
                    session,
                    executor_id="exec-other",
                    name="Secret Other Executor",
                    executor_type="websocket",
                    owner_email="other@example.com",
                )
                foreign.status = "active"
                foreign.runtime_state = "blocked"
                session.add(
                    MCPOAuthTokenRow(
                        token_id="oauth-owned",
                        user_email="owner@example.com",
                        mcp_server_id=owned_mcp.server_id,
                        issuer="https://issuer.invalid",
                        resource="https://mcp.invalid/mcp",
                        resource_key="https://mcp.invalid/mcp",
                        status="reauthorization_required",
                        encrypted_payload=b"secret-encrypted-token",
                    )
                )
                await session.commit()

        asyncio.run(_seed())
        statements = 0

        def _count(*_: object) -> None:
            nonlocal statements
            statements += 1

        engine = client.app.state.session_factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            response = client.get(
                "/api/v1/dashboard/issues",
                headers=_headers(client.app, "owner@example.com"),
            )
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert response.status_code == 200, response.text
        payload = response.json()
        # Authentication adds one user lookup. The issue projection itself uses
        # four fixed queries, independent of executor, MCP, and schedule counts.
        assert statements == 5
        assert payload["generated_at"]
        assert payload["summary"]["total"] == len(payload["issues"])
        assert {
            "executor_unavailable",
            "executor_config_not_converged",
            "executor_degraded",
            "tool_observation_stale",
            "mcp_auth_fault",
        }.issubset({issue["kind"] for issue in payload["issues"]})
        assert [issue["severity"] for issue in payload["issues"]] == sorted(
            [issue["severity"] for issue in payload["issues"]],
            key={"critical": 0, "warning": 1, "info": 2}.get,
        )
        serialized = response.text
        assert "Secret Other" not in serialized
        assert "secret-token" not in serialized
        assert "secret-command" not in serialized
        assert "secret-encrypted-token" not in serialized
        assert {issue["action_url"] for issue in payload["issues"]} <= {
            "/settings?tab=executors",
            "/settings?tab=tools",
        }
        assert len({issue["id"] for issue in payload["issues"]}) == len(payload["issues"])

        repeated = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        ).json()
        assert [issue["id"] for issue in repeated["issues"]] == [
            issue["id"] for issue in payload["issues"]
        ]


def test_dashboard_issues_ignore_obsolete_oauth_scopes_and_active_current_token(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                server = await create_mcp_server(
                    session,
                    server_id="mcp-owned",
                    name="Owned MCP",
                    transport="streamable_http",
                    url="https://mcp.invalid/current",
                    auth_config={"type": "oauth2", "issuer": "https://issuer.invalid"},
                    owner_email="owner@example.com",
                )
                session.add_all(
                    [
                        MCPOAuthTokenRow(
                            token_id="oauth-obsolete",
                            user_email="owner@example.com",
                            mcp_server_id=server.server_id,
                            issuer="https://old-issuer.invalid",
                            resource_key="https://mcp.invalid/old",
                            status="reauthorization_required",
                            encrypted_payload=b"obsolete",
                        ),
                        MCPOAuthTokenRow(
                            token_id="oauth-current-active",
                            user_email="owner@example.com",
                            mcp_server_id=server.server_id,
                            issuer="https://issuer.invalid",
                            resource_key="https://mcp.invalid/current",
                            status="active",
                            encrypted_payload=b"active",
                        ),
                    ]
                )
                await session.commit()

        asyncio.run(_seed())
        payload = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        ).json()
        assert [issue for issue in payload["issues"] if issue["kind"] == "mcp_auth_fault"] == []


def test_dashboard_issues_healthy_state_is_empty(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                executor = await create_executor(
                    session,
                    executor_id="exec-healthy",
                    name="Healthy",
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                executor.status = "active"
                executor.runtime_state = "active"
                executor.desired_config_version = 1
                executor.applied_config_version = 1
                executor.observed_tools = []
                executor.last_observed_at = datetime.now(UTC)
                await session.commit()

        asyncio.run(_seed())
        response = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        )
        assert response.status_code == 200
        assert response.json()["issues"] == []
        assert response.json()["summary"] == {
            "total": 0,
            "critical": 0,
            "warning": 0,
            "info": 0,
            "truncated": False,
        }


def test_dashboard_projects_only_owner_actionable_schedule_failures(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        session,
                        email=email,
                        name=email,
                        password_hash=client.app.state.password_hasher.hash("password123"),
                    )
                    await create_agent(
                        session,
                        agent_id=f"agent-{email}",
                        owner_email=email,
                        name=f"Agent {email}",
                    )
                failed = await create_schedule(
                    session,
                    schedule_id="sched-failed",
                    name="Failed schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner@example.com",
                    task_template={},
                    created_by="owner@example.com",
                )
                failed.last_run_status = "failed"
                failed.consecutive_errors = 2
                auto_disabled = await create_schedule(
                    session,
                    schedule_id="sched-disabled",
                    name="Disabled schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner@example.com",
                    task_template={},
                    enabled=False,
                    created_by="owner@example.com",
                )
                auto_disabled.last_run_status = "success"
                auto_disabled.consecutive_errors = 0
                auto_disabled.disabled_reason = "auto_consecutive_failures:5"
                manual = await create_schedule(
                    session,
                    schedule_id="sched-manual",
                    name="Manual schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner@example.com",
                    task_template={},
                    enabled=False,
                    created_by="owner@example.com",
                )
                manual.last_run_status = "failed"
                manual.disabled_reason = "disabled_by_user"
                foreign = await create_schedule(
                    session,
                    schedule_id="sched-foreign",
                    name="Foreign secret schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-other@example.com",
                    task_template={},
                    created_by="other@example.com",
                )
                foreign.last_run_status = "failed"
                await session.commit()

        asyncio.run(_seed())
        response = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        )
        assert response.status_code == 200, response.text
        schedule_issues = [
            issue for issue in response.json()["issues"] if issue["resource"]["type"] == "schedule"
        ]
        assert {(issue["resource"]["id"], issue["severity"]) for issue in schedule_issues} == {
            ("sched-failed", "warning"),
            ("sched-disabled", "critical"),
        }
        assert {issue["action_url"] for issue in schedule_issues} == {
            "/schedules/sched-failed",
            "/schedules/sched-disabled",
        }
        assert all(issue["action_label"] == "View schedule" for issue in schedule_issues)
        assert all(issue["dismiss_token"] for issue in schedule_issues)
        assert "Foreign secret schedule" not in response.text
        assert "Manual schedule" not in response.text


def test_dashboard_issues_have_a_fixed_result_cap(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                for index in range(ISSUE_LIMIT + 5):
                    executor = await create_executor(
                        session,
                        executor_id=f"exec-{index:03d}",
                        name=f"Executor {index:03d}",
                        executor_type="websocket",
                        owner_email="owner@example.com",
                    )
                    executor.status = "active"
                    executor.runtime_state = "offline"
                await session.commit()

        asyncio.run(_seed())
        payload = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        ).json()
        assert len(payload["issues"]) == ISSUE_LIMIT
        assert payload["summary"]["total"] == ISSUE_LIMIT + 5
        assert payload["summary"]["truncated"] is True


def test_dashboard_issues_suppress_recent_transient_executor_disconnect(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                recent = await create_executor(
                    session,
                    executor_id="exec-recent",
                    name="Recent disconnect",
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                recent.status = "active"
                recent.runtime_state = "offline"
                recent.last_observed_at = now - EXECUTOR_OFFLINE_GRACE / 2
                expired = await create_executor(
                    session,
                    executor_id="exec-expired",
                    name="Expired disconnect",
                    executor_type="websocket",
                    owner_email="owner@example.com",
                )
                expired.status = "active"
                expired.runtime_state = "offline"
                expired.last_observed_at = now - EXECUTOR_OFFLINE_GRACE
                await session.commit()

        asyncio.run(_seed())
        payload = client.get(
            "/api/v1/dashboard/issues",
            headers=_headers(client.app, "owner@example.com"),
        ).json()
        unavailable_ids = {
            issue["resource"]["id"]
            for issue in payload["issues"]
            if issue["kind"] == "executor_unavailable"
        }
        assert unavailable_ids == {"exec-expired"}


def test_schedule_issue_dismissal_is_incident_scoped_and_survives_reconciliation(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                await create_user(
                    session,
                    email="other@example.com",
                    name="Other",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                await create_user(
                    session,
                    email="viewer@example.com",
                    name="Viewer",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="viewer",
                )
                await create_agent(
                    session,
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner agent",
                )
                schedule = await create_schedule(
                    session,
                    schedule_id="sched-dismiss",
                    name="Dismiss schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner",
                    task_template={},
                    created_by="owner@example.com",
                )
                schedule.last_run_status = "failed"
                schedule.last_terminal_task_id = None
                schedule.last_fired_at = datetime(2026, 8, 25, tzinfo=UTC)
                schedule.consecutive_errors = 1
                await session.commit()

        asyncio.run(_seed())
        headers = _headers(client.app, "owner@example.com")
        issue = client.get("/api/v1/dashboard/issues", headers=headers).json()["issues"][0]
        dismiss_url = "/api/v1/dashboard/issues/schedules/sched-dismiss/dismiss"
        payload = {"incident_token": issue["dismiss_token"]}

        assert (
            client.post(
                dismiss_url,
                headers=_headers(client.app, "other@example.com"),
                json=payload,
            ).status_code
            == 404
        )
        assert (
            client.post(
                dismiss_url,
                headers=_headers(client.app, "viewer@example.com", role="viewer"),
                json=payload,
            ).status_code
            == 403
        )
        assert client.post(dismiss_url, headers=headers, json=payload).status_code == 200
        assert client.post(dismiss_url, headers=headers, json=payload).status_code == 200
        asyncio.run(client.app.state.notification_service.reconcile_schedule_actions())
        assert client.get("/api/v1/dashboard/issues", headers=headers).json()["issues"] == []

        async def _advance_incident() -> None:
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-dismiss")
                assert schedule is not None
                schedule.last_terminal_task_id = None
                schedule.last_fired_at = datetime(2026, 8, 26, tzinfo=UTC)
                schedule.consecutive_errors = 2
                await session.commit()

        asyncio.run(_advance_incident())
        assert client.post(dismiss_url, headers=headers, json=payload).status_code == 409
        asyncio.run(client.app.state.notification_service.reconcile_schedule_actions())
        reopened = client.get("/api/v1/dashboard/issues", headers=headers).json()["issues"]
        assert [item["resource"]["id"] for item in reopened] == ["sched-dismiss"]
