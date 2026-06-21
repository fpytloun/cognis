from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cognis.core.managed_conversation_maintenance import ManagedConversationMaintenanceService
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, User
from cognis.store.queries import (
    close_managed_conversation_link_for_retention,
    create_conversation,
    create_managed_conversation_link,
    get_managed_conversation_link,
    upsert_setting,
)


class _FakeTurnScheduler:
    def __init__(
        self,
        *,
        active: set[str] | None = None,
        stubborn: set[str] | None = None,
        turn_ids: dict[str, str | None] | None = None,
    ) -> None:
        self.active = set(active or ())
        self.stubborn = set(stubborn or ())
        self.turn_ids = dict(turn_ids or {})
        self.cancelled: list[str] = []

    def has_active_turn(self, conversation_id: str) -> bool:
        return conversation_id in self.active

    def active_turn_checkpoint(self, conversation_id: str) -> dict[str, str | None] | None:
        if conversation_id not in self.active:
            return None
        return {
            "session_id": f"sess-{conversation_id}",
            "turn_id": self.turn_ids.get(conversation_id),
        }

    async def cancel_turn(self, conversation_id: str, *, clear_queue: bool = True) -> bool:
        self.cancelled.append(conversation_id)
        if conversation_id not in self.active:
            return False
        if conversation_id not in self.stubborn:
            self.active.remove(conversation_id)
        return True


@pytest.fixture
async def managed_conversation_db(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/managed-conversations.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="controller",
                owner_email="owner@example.com",
                name="Controller",
            )
        )
        session.add(
            Agent(
                agent_id="target",
                owner_email="owner@example.com",
                name="Target",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_link(
    session_factory: object,
    *,
    link_id: str,
    target_conversation_id: str,
    updated_at: datetime,
    conversation_state: str = "open",
    turn_state: str = "completed",
    active_turn_id: str | None = None,
    notify_on_completion: bool = True,
):
    async with session_factory() as session:  # type: ignore[operator]
        controller = await create_conversation(
            session,
            "owner@example.com",
            "controller",
            "web",
            conversation_id=f"controller-{link_id}",
            title=f"Controller {link_id}",
        )
        target = await create_conversation(
            session,
            "owner@example.com",
            "target",
            "web",
            conversation_id=target_conversation_id,
            title=f"Target {link_id}",
        )
        link = await create_managed_conversation_link(
            session,
            user_email="owner@example.com",
            controller_agent_id="controller",
            controller_conversation_id=controller.conversation_id,
            controller_session_id=f"sess-controller-{link_id}",
            target_agent_id="target",
            target_conversation_id=target.conversation_id,
            target_session_id=f"sess-target-{link_id}",
            title=f"Managed {link_id}",
            turn_state=turn_state,
            notify_on_completion=notify_on_completion,
        )
        link.link_id = link_id
        link.conversation_state = conversation_state
        link.turn_state = turn_state
        link.active_turn_id = active_turn_id
        link.notify_on_completion = notify_on_completion
        link.updated_at = updated_at
        await session.commit()
        return link_id


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_closes_stale_non_active_links(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_stale_completed",
        target_conversation_id="conv-stale-completed",
        updated_at=old,
        turn_state="completed",
        active_turn_id=None,
    )

    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=_FakeTurnScheduler(),
    )
    result = await service.run_once()

    assert result.closed == 1
    assert result.cancelled_turns == 0
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_stale_completed")
        assert link is not None
        assert link.conversation_state == "closed"
        assert link.turn_state == "idle"
        assert link.active_turn_id is None
        assert link.notify_on_completion is False
        assert link.closed_at is not None
        assert link.last_error == "Closed automatically after 7 days without activity"


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_closes_stale_non_closed_states(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_stale_failed",
        target_conversation_id="conv-stale-failed",
        updated_at=old,
        conversation_state="open",
        turn_state="failed",
        active_turn_id=None,
    )
    await _create_link(
        managed_conversation_db,
        link_id="mconv_stale_state_completed",
        target_conversation_id="conv-stale-state-completed",
        updated_at=old,
        conversation_state="completed",
        turn_state="completed",
        active_turn_id=None,
    )
    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=_FakeTurnScheduler(),
    )

    result = await service.run_once()

    assert result.closed == 2
    async with managed_conversation_db() as session:
        failed = await get_managed_conversation_link(session, "mconv_stale_failed")
        completed = await get_managed_conversation_link(
            session,
            "mconv_stale_state_completed",
        )
        assert failed is not None
        assert failed.conversation_state == "closed"
        assert completed is not None
        assert completed.conversation_state == "closed"


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_cancels_stale_active_turn_before_close(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_stale_running",
        target_conversation_id="conv-stale-running",
        updated_at=old,
        turn_state="running",
        active_turn_id="turn-stale",
    )
    scheduler = _FakeTurnScheduler(
        active={"conv-stale-running"},
        turn_ids={"conv-stale-running": "turn-stale"},
    )
    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=scheduler,
    )

    result = await service.run_once()

    assert scheduler.cancelled == ["conv-stale-running"]
    assert result.cancelled_turns == 1
    assert result.closed == 1
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_stale_running")
        assert link is not None
        assert link.conversation_state == "closed"
        assert link.turn_state == "idle"
        assert link.active_turn_id is None


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_skips_when_stale_active_turn_cannot_be_cancelled(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_stubborn_running",
        target_conversation_id="conv-stubborn-running",
        updated_at=old,
        turn_state="running",
        active_turn_id="turn-stubborn",
    )
    scheduler = _FakeTurnScheduler(
        active={"conv-stubborn-running"},
        stubborn={"conv-stubborn-running"},
        turn_ids={"conv-stubborn-running": "turn-stubborn"},
    )
    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=scheduler,
    )

    result = await service.run_once()

    assert scheduler.cancelled == ["conv-stubborn-running"]
    assert result.skipped_active == 1
    assert result.closed == 0
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_stubborn_running")
        assert link is not None
        assert link.conversation_state == "open"
        assert link.active_turn_id == "turn-stubborn"


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_skips_when_active_turn_id_changed(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_changed_running",
        target_conversation_id="conv-changed-running",
        updated_at=old,
        turn_state="running",
        active_turn_id="turn-old",
    )
    scheduler = _FakeTurnScheduler(
        active={"conv-changed-running"},
        turn_ids={"conv-changed-running": "turn-new"},
    )
    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=scheduler,
    )

    result = await service.run_once()

    assert scheduler.cancelled == []
    assert result.skipped_active == 1
    assert result.closed == 0
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_changed_running")
        assert link is not None
        assert link.conversation_state == "open"
        assert link.active_turn_id == "turn-old"


@pytest.mark.asyncio
async def test_managed_conversation_retention_close_skips_rows_that_became_fresh(
    managed_conversation_db,
) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=7)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_became_fresh",
        target_conversation_id="conv-became-fresh",
        updated_at=datetime.now(UTC),
        turn_state="completed",
        active_turn_id=None,
    )

    async with managed_conversation_db() as session:
        row = await close_managed_conversation_link_for_retention(
            session,
            "mconv_became_fresh",
            reason="should not close",
            older_than=cutoff,
        )
        await session.commit()

    assert row is None
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_became_fresh")
        assert link is not None
        assert link.conversation_state == "open"


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_ignores_fresh_and_closed_links(
    managed_conversation_db,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_fresh",
        target_conversation_id="conv-fresh",
        updated_at=now - timedelta(days=1),
    )
    await _create_link(
        managed_conversation_db,
        link_id="mconv_already_closed",
        target_conversation_id="conv-already-closed",
        updated_at=old,
        conversation_state="closed",
        notify_on_completion=False,
    )
    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=_FakeTurnScheduler(),
    )

    result = await service.run_once()

    assert result.closed == 0
    async with managed_conversation_db() as session:
        fresh = await get_managed_conversation_link(session, "mconv_fresh")
        closed = await get_managed_conversation_link(session, "mconv_already_closed")
        assert fresh is not None
        assert fresh.conversation_state == "open"
        assert closed is not None
        assert closed.conversation_state == "closed"


@pytest.mark.asyncio
async def test_managed_conversation_cleanup_setting_zero_disables_cleanup(
    managed_conversation_db,
) -> None:
    old = datetime.now(UTC) - timedelta(days=30)
    await _create_link(
        managed_conversation_db,
        link_id="mconv_disabled_cleanup",
        target_conversation_id="conv-disabled-cleanup",
        updated_at=old,
    )
    async with managed_conversation_db() as session:
        await upsert_setting(
            session,
            "managed_conversations.cleanup_retention_days",
            0,
            "managed_conversations",
        )
        await session.commit()

    service = ManagedConversationMaintenanceService(
        session_factory=managed_conversation_db,
        turn_scheduler=_FakeTurnScheduler(),
    )
    result = await service.run_once()

    assert result.retention_days is None
    assert result.closed == 0
    async with managed_conversation_db() as session:
        link = await get_managed_conversation_link(session, "mconv_disabled_cleanup")
        assert link is not None
        assert link.conversation_state == "open"
