from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cognis.core.session import SessionManager, _map_cognis_to_intaris_status
from cognis.models.session import ConversationContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User
from cognis.store.queries import get_session_row, list_conversation_sessions


class _Guardrails:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []
        self.status_calls: list[tuple[str, str, str | None]] = []

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        del policy, details
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.calls.append((session_id, agent_id, parent_session_id))

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None:
        del user_email
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.status_calls.append((session_id, status, status_reason))


class _SlowGuardrails(_Guardrails):
    def __init__(self) -> None:
        super().__init__(fail=False)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        del intention, user_id, policy, details
        self.calls.append((session_id, agent_id, parent_session_id))
        self.entered.set()
        await self.release.wait()


class _Providers:
    def __init__(self, fail: bool = False) -> None:
        self.guardrails = _Guardrails(fail=fail)
        self.memory = object()


class _Cache:
    def __init__(self) -> None:
        self.evicted: list[str] = []

    async def evict(self, session_id: str) -> bool:
        self.evicted.append(session_id)
        return True


async def _session_factory(tmp_path) -> object:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    session_factory = create_session_factory(engine)
    from cognis.bootstrap import run_schema_bootstrap

    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", password_hash="x", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent One",
                description="Helpful assistant",
            )
        )
        await session.commit()
    return engine, session_factory


@pytest.mark.asyncio
async def test_session_manager_creates_conversation_and_root_session_atomically(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Test conversation",
    )

    assert conversation.active_session_id == root_session.session_id
    assert root_session.intaris_session_id == root_session.session_id

    async with session_factory() as session:
        stored_conversation = await session.get(Conversation, conversation.conversation_id)
        stored_session = await session.get(Session, root_session.session_id)
        assert stored_conversation is not None
        assert stored_conversation.active_session_id == root_session.session_id
        assert stored_session is not None
        assert stored_session.intaris_session_id == root_session.session_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_rolls_back_when_intaris_creation_fails(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(fail=True), _Cache())

    with pytest.raises(RuntimeError, match="intaris unavailable"):
        await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Broken conversation",
        )

    async with session_factory() as session:
        assert (await session.execute(Session.__table__.select())).all() == []
        assert (await session.execute(Conversation.__table__.select())).all() == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_recovery_uses_updated_at_not_started_at(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
            )
        )
        session.add_all(
            [
                Session(
                    session_id="fresh-session",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now,
                ),
                Session(
                    session_id="stale-parent",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-child",
                    conversation_id="conv-1",
                    parent_session_id="stale-parent",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-grandchild",
                    conversation_id="conv-1",
                    parent_session_id="stale-child",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
            ]
        )
        await session.commit()

    recovered_ids = await manager.recover_stale_sessions(stale_after_seconds=300)

    assert set(recovered_ids) == {"stale-parent", "stale-child", "stale-grandchild"}
    async with session_factory() as session:
        fresh = await session.get(Session, "fresh-session")
        stale_parent = await session.get(Session, "stale-parent")
        stale_child = await session.get(Session, "stale-child")
        stale_grandchild = await session.get(Session, "stale-grandchild")
        assert fresh is not None and fresh.status == "active"
        assert stale_parent is not None and stale_parent.status == "idle"
        assert stale_parent.idle_since is not None
        assert stale_child is not None and stale_child.status == "failed"
        assert stale_grandchild is not None and stale_grandchild.status == "failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_creates_new_root_and_marks_old_completed(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)

    # Create an initial conversation + root session
    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Rotation test",
    )

    # Rotate
    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Continued after compaction",
        completion_reason="compacted",
        compaction_summary="Summary of older turns.",
    )

    # Verify new session
    assert new_session.session_id != root_session.session_id
    assert new_session.conversation_id == conversation.conversation_id

    # Verify old session is completed
    async with session_factory() as db:
        old_row = await db.get(Session, root_session.session_id)
        assert old_row is not None
        assert old_row.status == "completed"
        assert old_row.completion_reason == "compacted"
        assert old_row.completed_at is not None

    # Verify new session is linked via previous_session_id
    async with session_factory() as db:
        new_row = await db.get(Session, new_session.session_id)
        assert new_row is not None
        assert new_row.previous_session_id == root_session.session_id

    # Verify conversation root updated
    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        assert conv is not None
        assert conv.active_session_id == new_session.session_id

    # Verify old session cache was evicted
    assert root_session.session_id in cache.evicted

    # Verify Intaris session was created for new root
    assert len(providers.guardrails.calls) == 2  # original + rotation
    assert providers.guardrails.calls[1][0] == new_session.session_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_resets_mnemory_session_id(tmp_path) -> None:
    """Rotated sessions start with mnemory_session_id=None.

    Session rotation creates a new context window.  The first recall in
    the new session creates a fresh Mnemory session and reconstructs the
    full immutable prefix (core memories + instructions) from scratch.
    """
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Mnemory reset test",
    )

    # Set a mnemory_session_id on the root session
    async with session_factory() as db:
        row = await db.get(Session, root_session.session_id)
        assert row is not None
        row.mnemory_session_id = "mnemory-abc-123"
        await db.commit()

    root_session.mnemory_session_id = "mnemory-abc-123"

    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Test",
    )

    # The new session should NOT carry forward mnemory_session_id —
    # it starts fresh so the first recall triggers is_first_call=True
    # in Mnemory, which returns core memories and instructions.
    async with session_factory() as db:
        new_row = await db.get(Session, new_session.session_id)
        assert new_row is not None
        assert new_row.mnemory_session_id is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_root_session_is_single_winner(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    providers.guardrails = _SlowGuardrails()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Race conversation",
    )

    first = asyncio.create_task(
        manager.ensure_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="first bootstrap",
        )
    )
    await providers.guardrails.entered.wait()
    second = asyncio.create_task(
        manager.ensure_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="second bootstrap",
        )
    )
    providers.guardrails.release.set()

    first_session, second_session = await asyncio.gather(first, second)

    assert first_session.session_id == second_session.session_id

    async with session_factory() as db:
        stored_conversation = await db.get(Conversation, conversation.conversation_id)
        stored_sessions = await list_conversation_sessions(db, conversation.conversation_id)
        assert stored_conversation is not None
        assert stored_conversation.active_session_id == first_session.session_id
        assert [row.session_id for row in stored_sessions] == [first_session.session_id]

    await engine.dispose()


# ------------------------------------------------------------------
# Status mapping tests
# ------------------------------------------------------------------


def test_map_cognis_to_intaris_status_direct_mappings() -> None:
    for status in ("active", "idle", "completed", "suspended", "terminated"):
        intaris_status, _ = _map_cognis_to_intaris_status(status)
        assert intaris_status == status


def test_map_cognis_to_intaris_status_failed_maps_to_terminated() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status("failed")
    assert intaris_status == "terminated"
    assert reason == "source_status=failed"


def test_map_cognis_to_intaris_status_cancelled_maps_to_terminated() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status("cancelled")
    assert intaris_status == "terminated"
    assert reason == "source_status=cancelled"


def test_map_cognis_to_intaris_status_completed_with_reason() -> None:
    intaris_status, reason = _map_cognis_to_intaris_status(
        "completed", completion_reason="compacted"
    )
    assert intaris_status == "completed"
    assert reason == "completion_reason=compacted"


def test_map_cognis_to_intaris_status_reason_truncated() -> None:
    _, reason = _map_cognis_to_intaris_status("suspended", reason="x" * 600)
    assert reason is not None
    assert len(reason) <= 500


# ------------------------------------------------------------------
# SessionManager Intaris sync tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_completed_syncs_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Sync test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_completed(root.session_id, completion_reason="compacted")

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "completed"
    assert reason == "completion_reason=compacted"

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_active_syncs_to_intaris_and_clears_idle(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Active test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_idle(root.session_id)
    await manager.mark_active(root.session_id)

    async with session_factory() as db_session:
        row = await get_session_row(db_session, root.session_id)
        assert row is not None
        assert row.status == "active"
        assert row.idle_since is None

    assert len(providers.guardrails.status_calls) == 2
    sid, status, reason = providers.guardrails.status_calls[-1]
    assert sid == root.session_id
    assert status == "active"
    assert reason is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_failed_syncs_terminated_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Fail test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_failed(root.session_id, result_summary="boom")

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "terminated"
    assert reason == "source_status=failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_cancelled_syncs_terminated_to_intaris(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Cancel test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    await manager.mark_cancelled(root.session_id, result_summary="user cancelled")

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "terminated"
    assert reason == "source_status=cancelled"

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_sync_failure_does_not_block_mark(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers_ok = _Providers(fail=False)
    manager_ok = SessionManager(session_factory, providers_ok, _Cache())

    conversation = await manager_ok.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Degraded test",
    )
    root = await manager_ok.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    # Now use a failing provider for mark_idle
    providers_fail = _Providers(fail=True)
    manager_fail = SessionManager(session_factory, providers_fail, _Cache())
    updated = await manager_fail.mark_idle(root.session_id)
    assert updated  # DB update succeeded despite Intaris failure

    await engine.dispose()
