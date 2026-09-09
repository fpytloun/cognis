from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.events import Event, EventType
from cognis.store.models import NotificationRow, Schedule
from cognis.store.queries import create_agent, create_schedule, create_user


def test_schedule_notification_deduplicates_escalates_resolves_and_is_owner_scoped(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        events: list[Event] = []

        async def _capture(event: Event) -> None:
            events.append(event)

        client.app.state.event_bus.subscribe(EventType.SCHEDULE_ACTION_CHANGED, _capture)
        client.app.state.event_bus.subscribe(EventType.NOTIFICATION_CREATED, _capture)
        client.app.state.event_bus.subscribe(EventType.NOTIFICATION_RESOLVED, _capture)

        async def _exercise() -> None:
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
                        name=email,
                    )
                schedule = await create_schedule(
                    session,
                    schedule_id="sched-1",
                    name="Daily report",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner@example.com",
                    task_template={},
                    created_by="owner@example.com",
                )
                schedule.last_run_status = "failed"
                schedule.consecutive_errors = 1
                await session.commit()

            service = client.app.state.notification_service
            await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=1,
                error_summary="first failure",
                auto_disabled=False,
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.consecutive_errors = 2
                await session.commit()
            await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=2,
                error_summary="token=must-not-appear",
                auto_disabled=False,
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.enabled = False
                schedule.consecutive_errors = 5
                schedule.disabled_reason = "auto_consecutive_failures:5"
                await session.commit()
            await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=5,
                error_summary="bounded failure",
                auto_disabled=True,
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.last_run_status = "success"
                schedule.last_terminal_task_id = "task-late-success"
                schedule.consecutive_errors = 0
                await session.commit()
            late_success = await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=0,
                error_summary=(
                    "Latest run succeeded, but the schedule remains automatically disabled."
                ),
                auto_disabled=True,
                task_id="task-late-success",
            )
            assert late_success is not None
            assert late_success.status == "pending"
            assert late_success.payload["severity"] == "critical"
            assert "remains automatically disabled" in late_success.payload["error_summary"]
            async with client.app.state.session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            NotificationRow.__table__.select().where(
                                NotificationRow.user_email == "owner@example.com"
                            )
                        )
                    ).mappings()
                )
                assert len(rows) == 1
                assert rows[0]["notification_id"] == "notif_schedule_sched-1"
                assert rows[0]["payload"]["severity"] == "critical"
                assert rows[0]["payload"]["consecutive_errors"] == 5
                assert "must-not-appear" not in str(rows[0]["payload"])
            assert (
                await service.resolve_schedule_action(
                    "sched-1",
                    user_email="other@example.com",
                    reason="not_owner",
                )
                is False
            )
            assert (
                await service.resolve_schedule_action(
                    "sched-1",
                    user_email="owner@example.com",
                    reason="schedule_recovered",
                )
                is True
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.enabled = True
                schedule.last_run_status = "success"
                schedule.consecutive_errors = 0
                schedule.disabled_reason = None
                await session.commit()
            assert await service.reconcile_schedule_actions() == 0
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.enabled = False
                schedule.last_run_status = "failed"
                schedule.last_terminal_task_id = None
                schedule.consecutive_errors = 5
                schedule.disabled_reason = "auto_consecutive_failures:5"
                await session.commit()
            reconciled = await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=5,
                error_summary="reconciled state",
                auto_disabled=True,
                reopen_resolved=False,
            )
            assert reconciled is not None
            assert reconciled.status == "resolved"
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-1")
                assert schedule is not None
                schedule.enabled = True
                schedule.last_run_status = "failed"
                schedule.consecutive_errors = 1
                schedule.disabled_reason = None
                await session.commit()
            await service.upsert_schedule_action(
                schedule_id="sched-1",
                user_email="owner@example.com",
                agent_id="agent-owner@example.com",
                schedule_name="Daily report",
                consecutive_errors=1,
                error_summary="new incident",
                auto_disabled=False,
            )
            async with client.app.state.session_factory() as session:
                reopened = await session.get(NotificationRow, "notif_schedule_sched-1")
                assert reopened is not None
                assert reopened.status == "pending"
                assert reopened.payload["severity"] == "warning"
                assert reopened.payload["auto_disabled"] is False

        asyncio.run(_exercise())
        assert [event.type for event in events].count(EventType.SCHEDULE_ACTION_CHANGED) == 6
        assert [event.type for event in events].count(EventType.NOTIFICATION_CREATED) == 0
        assert [event.type for event in events].count(EventType.NOTIFICATION_RESOLVED) == 0


def test_stale_schedule_failures_do_not_create_or_reopen_incidents(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:

        async def _exercise() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                await create_agent(
                    session,
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner agent",
                )
                schedule = await create_schedule(
                    session,
                    schedule_id="sched-stale",
                    name="Stale schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner",
                    task_template={},
                    created_by="owner@example.com",
                )
                schedule.enabled = False
                schedule.last_run_status = "failed"
                schedule.consecutive_errors = 1
                schedule.disabled_reason = "disabled_by_user"
                await session.commit()

            service = client.app.state.notification_service
            assert (
                await service.upsert_schedule_action(
                    schedule_id="sched-stale",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    schedule_name="Stale schedule",
                    consecutive_errors=1,
                    error_summary="late failure",
                    auto_disabled=False,
                )
                is None
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-stale")
                assert schedule is not None
                schedule.enabled = True
                schedule.last_run_status = "failed"
                schedule.last_terminal_task_id = "task-newer"
                schedule.consecutive_errors = 1
                schedule.disabled_reason = None
                await session.commit()
            assert (
                await service.upsert_schedule_action(
                    schedule_id="sched-stale",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    schedule_name="Stale schedule",
                    consecutive_errors=1,
                    error_summary="delayed taskless failure",
                    auto_disabled=False,
                    task_id=None,
                )
                is None
            )
            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-stale")
                assert schedule is not None
                schedule.enabled = True
                schedule.last_run_status = "success"
                schedule.last_terminal_task_id = "task-newer"
                schedule.consecutive_errors = 0
                schedule.disabled_reason = None
                await session.commit()
            assert (
                await service.upsert_schedule_action(
                    schedule_id="sched-stale",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    schedule_name="Stale schedule",
                    consecutive_errors=1,
                    error_summary="older failure",
                    auto_disabled=False,
                )
                is None
            )
            async with client.app.state.session_factory() as session:
                assert await session.get(NotificationRow, "notif_schedule_sched-stale") is None

        asyncio.run(_exercise())


def test_schedule_failure_uses_original_external_event_and_purpose_invalidation(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        external_events: list[Event] = []
        invalidations: list[Event] = []

        async def _external(event: Event) -> None:
            external_events.append(event)

        async def _invalidated(event: Event) -> None:
            invalidations.append(event)

        client.app.state.event_bus.subscribe(EventType.SCHEDULE_ERROR, _external)
        client.app.state.event_bus.subscribe(EventType.NOTIFICATION_CREATED, _external)
        client.app.state.event_bus.subscribe(EventType.SCHEDULE_ACTION_CHANGED, _invalidated)

        async def _exercise() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                await create_agent(
                    session,
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner agent",
                )
                schedule = await create_schedule(
                    session,
                    schedule_id="sched-delivery",
                    name="Delivery schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner",
                    task_template={},
                    created_by="owner@example.com",
                )
                schedule.last_run_status = "failed"
                schedule.consecutive_errors = 1
                await session.commit()

            await client.app.state.event_bus.publish(
                Event(
                    type=EventType.SCHEDULE_ERROR,
                    data={
                        "schedule_id": "sched-delivery",
                        "created_by": "owner@example.com",
                        "agent_id": "agent-owner",
                        "schedule_name": "Delivery schedule",
                        "consecutive_errors": 1,
                        "error": "delivery failed",
                    },
                )
            )
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, "notif_schedule_sched-delivery")
                assert row is not None
                assert row.status == "pending"
                assert row.payload["action_url"] == "/schedules/sched-delivery"

        asyncio.run(_exercise())
        assert [event.type for event in external_events] == [EventType.SCHEDULE_ERROR]
        assert len(invalidations) == 1
        assert invalidations[0].data["status"] == "pending"


def test_overlapping_terminal_results_follow_canonical_projection_order(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:

        async def _exercise() -> None:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                )
                await create_agent(
                    session,
                    agent_id="agent-owner",
                    owner_email="owner@example.com",
                    name="Owner agent",
                )
                schedule = await create_schedule(
                    session,
                    schedule_id="sched-overlap",
                    name="Overlap schedule",
                    schedule_type="interval",
                    interval_seconds=60,
                    agent_id="agent-owner",
                    task_template={},
                    max_concurrent_runs=2,
                    created_by="owner@example.com",
                )
                # B was created after A and completed successfully first. A then
                # failed, so terminal projection order makes A canonical.
                schedule.last_run_status = "failed"
                schedule.last_terminal_task_id = "task-a"
                schedule.consecutive_errors = 1
                await session.commit()

            service = client.app.state.notification_service
            incident = await service.upsert_schedule_action(
                schedule_id="sched-overlap",
                user_email="owner@example.com",
                agent_id="agent-owner",
                schedule_name="Overlap schedule",
                consecutive_errors=1,
                error_summary="A failed after B succeeded",
                auto_disabled=False,
                task_id="task-a",
            )
            assert incident is not None
            assert incident.task_id == "task-a"

            async with client.app.state.session_factory() as session:
                schedule = await session.get(Schedule, "sched-overlap")
                assert schedule is not None
                # In the reverse completion order, B succeeds after A failed.
                schedule.last_run_status = "success"
                schedule.last_terminal_task_id = "task-b"
                schedule.consecutive_errors = 0
                await session.commit()
            assert await service.resolve_schedule_action(
                "sched-overlap",
                user_email="owner@example.com",
                reason="schedule_recovered",
            )
            assert (
                await service.upsert_schedule_action(
                    schedule_id="sched-overlap",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    schedule_name="Overlap schedule",
                    consecutive_errors=1,
                    error_summary="delayed A failure",
                    auto_disabled=False,
                    task_id="task-a",
                )
                is None
            )
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, "notif_schedule_sched-overlap")
                assert row is not None
                assert row.status == "resolved"

                schedule = await session.get(Schedule, "sched-overlap")
                assert schedule is not None
                # A projected success, then B projected failure before A's
                # asynchronous notification resolution reached the service.
                schedule.last_run_status = "failed"
                schedule.last_terminal_task_id = "task-b"
                schedule.consecutive_errors = 1
                await session.commit()
            assert (
                await service.upsert_schedule_action(
                    schedule_id="sched-overlap",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    schedule_name="Overlap schedule",
                    consecutive_errors=1,
                    error_summary="B failed after A succeeded",
                    auto_disabled=False,
                    task_id="task-b",
                )
                is not None
            )
            assert (
                await service.resolve_schedule_action(
                    "sched-overlap",
                    user_email="owner@example.com",
                    reason="schedule_recovered",
                    expected_terminal_task_id="task-a",
                    require_canonical_success=True,
                )
                is False
            )
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, "notif_schedule_sched-overlap")
                assert row is not None
                assert row.status == "pending"
                assert row.task_id == "task-b"

        asyncio.run(_exercise())
