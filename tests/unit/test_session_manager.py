from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cognis.core.session import SessionManager, _map_cognis_to_intaris_status
from cognis.models.session import ConversationContext, EventAppendResult, SessionEvent, SessionModel
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User
from cognis.store.queries import get_conversation, get_session_row, list_conversation_sessions


class _Guardrails:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []
        self.status_calls: list[tuple[str, str, str | None]] = []
        self.last_details: dict | None = None
        self.last_policy: dict | None = None
        self.policy_updates: list[tuple[str, dict | None, dict | None]] = []
        self.recorded_events: list[tuple[str, list[SessionEvent], str | None]] = []
        self.record_event_contexts: list[tuple[str | None, str | None, str | None]] = []

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
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.last_details = dict(details) if details is not None else None
        self.last_policy = dict(policy) if policy is not None else None
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

    async def update_session_policy(
        self,
        session_id: str,
        *,
        agent_id: str,
        user_id: str | None = None,
        details: dict | None = None,
        policy: dict | None = None,
    ) -> None:
        del agent_id, user_id
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.last_details = dict(details) if details is not None else None
        self.last_policy = dict(policy) if policy is not None else None
        self.policy_updates.append((session_id, self.last_details, self.last_policy))

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **_: object,
    ) -> object:
        del source
        self.recorded_events.append((session_id, events, idempotency_key))
        self.record_event_contexts.append(
            (current_user_email.get(), current_agent_id.get(), current_agent_owner_email.get())
        )
        return type(
            "_AppendResult",
            (),
            {"ok": True, "count": len(events), "first_seq": 1, "last_seq": len(events)},
        )()


class _NonAppendingGuardrails(_Guardrails):
    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        **_: object,
    ) -> object:
        del source
        self.recorded_events.append((session_id, events, idempotency_key))
        self.record_event_contexts.append(
            (current_user_email.get(), current_agent_id.get(), current_agent_owner_email.get())
        )
        return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)


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
        self.appended_events: list[tuple[SessionModel, list[SessionEvent], object]] = []

    async def evict(self, session_id: str) -> bool:
        self.evicted.append(session_id)
        return True

    async def append_recorded_events(
        self,
        session: SessionModel,
        events: list[SessionEvent],
        append_result: object,
    ) -> None:
        self.appended_events.append((session, events, append_result))


@pytest.mark.asyncio
async def test_seed_rotated_tail_events_skips_non_appendable_event_types(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)
    new_session = SessionModel(
        session_id="new-session",
        conversation_id="conv-1",
        user_email="user@example.com",
        agent_id="agent-1",
        intaris_session_id="intaris-new-session",
    )
    tail_events = [
        SimpleNamespace(seq=10, type="user_message", data={"content": "keep"}),
        SimpleNamespace(seq=11, type="tool_result_chunk", data={"content": "skip"}),
        SimpleNamespace(seq=12, type="assistant_message", data={"content": "keep too"}),
    ]

    await manager._seed_rotated_tail_events(
        new_session,
        tail_events=tail_events,
        previous_session_id="old-session",
    )

    assert len(providers.guardrails.recorded_events) == 1
    session_id, recorded_events, idempotency_key = providers.guardrails.recorded_events[0]
    assert session_id == "intaris-new-session"
    assert idempotency_key == "new-session:compaction_tail:old-session"
    assert [event.type for event in recorded_events] == ["user_message", "assistant_message"]
    assert [event.data["source_seq"] for event in recorded_events] == [10, 12]
    assert all(event.data["compaction_tail"] is True for event in recorded_events)
    assert all(event.data["source_session_id"] == "old-session" for event in recorded_events)
    assert len(cache.appended_events) == 1
    assert cache.appended_events[0][1] == recorded_events

    await engine.dispose()


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
    assert any(
        key == f"{new_session.session_id}:compaction_summary:rotation"
        and events
        and getattr(events[0], "type", None) == "compaction_summary"
        for session_id, events, key in providers.guardrails.recorded_events
        if session_id == new_session.session_id
    )
    assert (
        "user@example.com",
        "agent-1",
        "user@example.com",
    ) in providers.guardrails.record_event_contexts

    await engine.dispose()


@pytest.mark.asyncio
async def test_rotate_session_keeps_new_root_when_compaction_summary_append_fails(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    providers.guardrails = _NonAppendingGuardrails()
    cache = _Cache()
    manager = SessionManager(session_factory, providers, cache)

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Rotation append failure test",
    )

    new_session = await manager.rotate_session(
        conversation_id=conversation.conversation_id,
        current_session=root_session,
        intention="Continued after compaction",
        completion_reason="compacted",
        compaction_summary="Summary of older turns.",
    )

    async with session_factory() as db:
        conv = await db.get(Conversation, conversation.conversation_id)
        old_row = await db.get(Session, root_session.session_id)
        new_row = await db.get(Session, new_session.session_id)

    assert conv is not None
    assert conv.active_session_id == new_session.session_id
    assert old_row is not None
    assert old_row.status == "completed"
    assert old_row.completion_reason == "compacted"
    assert new_row is not None
    assert new_row.status == "active"
    assert new_row.previous_session_id == root_session.session_id
    assert providers.guardrails.recorded_events
    assert cache.appended_events == []

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

    await manager.mark_completed(
        root.session_id,
        result_content="Full durable delegate result",
        completion_reason="compacted",
    )

    assert len(providers.guardrails.status_calls) == 1
    sid, status, reason = providers.guardrails.status_calls[0]
    assert sid == root.session_id
    assert status == "completed"
    assert reason == "completion_reason=compacted"
    async with session_factory() as db_session:
        row = await get_session_row(db_session, root.session_id)
    assert row is not None
    assert row.result_content == "Full durable delegate result"

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


@pytest.mark.asyncio
async def test_intaris_sync_failure_logs_safe_diagnostics(tmp_path, caplog) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers_ok = _Providers(fail=False)
    manager_ok = SessionManager(session_factory, providers_ok, _Cache())

    conversation = await manager_ok.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Diagnostics test",
    )
    root = await manager_ok.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    providers_fail = _Providers(fail=True)
    manager_fail = SessionManager(session_factory, providers_fail, _Cache())
    await manager_fail.mark_idle(root.session_id)

    matching = [
        record
        for record in caplog.records
        if record.message == "session: failed to sync status to Intaris"
    ]
    assert matching
    extra = matching[-1].__dict__.get("extra_data") or {}
    assert extra["session_id"] == root.session_id
    assert extra["target_session_id"] == root.session_id
    assert extra["uses_intaris_session_id"] is False
    assert extra["has_user_email"] is True
    assert extra["user_email_hash"]
    assert "user@example.com" not in str(extra)

    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_conversation_clears_active_session_and_marks_session_completed(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Archive test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    archived = await manager.archive_conversation(conversation.conversation_id)

    assert archived is True
    assert providers.guardrails.status_calls[-1] == (
        root.session_id,
        "completed",
        "completion_reason=conversation_archived",
    )

    async with session_factory() as session:
        stored_conversation = await get_conversation(session, conversation.conversation_id)
        stored_session = await get_session_row(session, root.session_id)
        assert stored_conversation is not None
        assert stored_conversation.status == "archived"
        assert stored_conversation.active_session_id is None
        assert stored_session is not None
        assert stored_session.status == "completed"
        assert stored_session.completion_reason == "conversation_archived"

    await engine.dispose()


@pytest.mark.asyncio
async def test_soft_delete_conversation_clears_active_session_and_marks_session_completed(
    tmp_path,
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    conversation = await manager.create_conversation(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Delete test",
    )
    root = await manager.create_root_session(
        conversation_id=conversation.conversation_id,
        user_email="user@example.com",
        agent_id="agent-1",
        intention="test",
    )

    deleted = await manager.soft_delete_conversation(conversation.conversation_id)

    assert deleted is True
    assert providers.guardrails.status_calls[-1] == (
        root.session_id,
        "completed",
        "completion_reason=conversation_deleted",
    )

    async with session_factory() as session:
        stored_conversation = await get_conversation(session, conversation.conversation_id)
        stored_session = await get_session_row(session, root.session_id)
        assert stored_conversation is not None
        assert stored_conversation.status == "deleted"
        assert stored_conversation.active_session_id is None
        assert stored_session is not None
        assert stored_session.status == "completed"
        assert stored_session.completion_reason == "conversation_deleted"

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_root_session_passes_workdir_and_allow_paths_to_intaris(tmp_path) -> None:
    """Executor-visible working directory must reach Intaris on session create."""

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    workdir = "/home/user/projects/cognis"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=workdir,
    ):
        conversation, root = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Wired",
        )
    del conversation, root

    assert providers.guardrails.last_details is not None
    assert providers.guardrails.last_details["working_directory"] == workdir
    assert providers.guardrails.last_details["source"] == "cognis"
    assert providers.guardrails.last_policy is not None
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert f"{workdir}/*" in allow_paths
    assert not any(path.endswith("/.cognis/*") for path in allow_paths)

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_root_session_falls_back_to_executor_cwd_when_context_unset(
    tmp_path,
) -> None:
    """Chat sessions must allow the executor cwd even without platform path data."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    executor_env = SimpleNamespace(home="/home/user", cwd="/home/user/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        executor_environment=executor_env,
    ):
        conversation, root = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="No workdir",
        )
    del conversation, root

    assert providers.guardrails.last_details is not None
    assert providers.guardrails.last_details["working_directory"] == "/home/user/src/cognis"
    assert providers.guardrails.last_policy is not None
    assert "/tmp/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/var/tmp/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/src/cognis/*" in providers.guardrails.last_policy["allow_paths"]
    assert "/home/user/.local/share/cognis/*" in providers.guardrails.last_policy["allow_paths"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_session_policy_includes_project_source_paths(tmp_path) -> None:
    """Project source local_path entries must be added to allow_paths."""

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-1",
                name="Test Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-1",
            name="cognis",
            local_path="/home/user/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-1",
            name="intaris",
            local_path="/home/user/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    workdir = "/home/user/projects/work"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=workdir,
    ):
        conversation = await manager.create_conversation(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Project conv",
            project_id="proj-1",
        )
        await manager.create_root_session(
            conversation_id=conversation.conversation_id,
            user_email="user@example.com",
            agent_id="agent-1",
            intention="work",
        )

    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths
    assert "/home/user/src/cognis/*" in allow_paths
    assert "/home/user/src/intaris/*" in allow_paths
    assert f"{workdir}/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_conversation_with_root_session_uses_project_source_paths(
    tmp_path,
) -> None:
    """Task step conversations must allow every configured project source."""

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-1",
                name="Test Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-1",
            name="cognis",
            local_path="/home/user/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-1",
            name="intaris",
            local_path="/home/user/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    narrowed_workdir = "/home/user/src/cognis/ui/src/lib"
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory=narrowed_workdir,
    ):
        conversation, _ = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-1"),
            title="Task step",
            project_id="proj-1",
        )

    assert conversation.project_id == "proj-1"
    assert providers.guardrails.last_details == {
        "source": "cognis",
        "working_directory": narrowed_workdir,
    }
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths
    assert "/home/user/src/cognis/*" in allow_paths
    assert "/home/user/src/intaris/*" in allow_paths
    assert f"{narrowed_workdir}/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_intaris_session_policy_expands_tilde_with_executor_home(tmp_path) -> None:
    """Project sources using ~ must be expanded with the executor home directory."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context
    from cognis.store.queries import create_project_source

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    async with session_factory() as session:
        from cognis.store.models import ProjectRow

        session.add(
            ProjectRow(
                project_id="proj-tilde",
                name="Tilde Project",
                owner_email="user@example.com",
            )
        )
        await session.flush()
        await create_project_source(
            session,
            project_id="proj-tilde",
            name="cognis",
            local_path="~/src/cognis",
            remote_url=None,
            default_branch="main",
        )
        await create_project_source(
            session,
            project_id="proj-tilde",
            name="intaris",
            local_path="~/src/intaris",
            remote_url=None,
            default_branch="main",
        )
        await session.commit()

    executor_env = SimpleNamespace(home="/home/executor", cwd="/home/executor/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        effective_working_directory="~/src/cognis/api",
        executor_environment=executor_env,
    ):
        conversation, _ = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="task", ref="task-tilde"),
            title="Tilde task step",
            project_id="proj-tilde",
        )

    assert conversation.project_id == "proj-tilde"
    assert providers.guardrails.last_details == {
        "source": "cognis",
        "working_directory": "~/src/cognis/api",
    }
    allow_paths = providers.guardrails.last_policy["allow_paths"]
    assert "~/src/cognis/*" not in allow_paths
    assert "~/src/intaris/*" not in allow_paths
    assert "/home/executor/src/cognis/*" in allow_paths
    assert "/home/executor/src/intaris/*" in allow_paths
    assert "/home/executor/src/cognis/api/*" in allow_paths
    assert "/home/executor/.local/share/cognis/*" in allow_paths
    assert "/tmp/*" in allow_paths
    assert "/var/tmp/*" in allow_paths

    await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_does_not_inherit_implicit_runtime_workdir(tmp_path) -> None:
    """Delegated child sessions stay neutral unless the caller scopes paths explicitly."""

    from types import SimpleNamespace

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    executor_env = SimpleNamespace(home="/home/user", cwd="/home/user/src/cognis")
    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        executor_environment=executor_env,
        workspace_root="/home/user/src/codex",
        effective_working_directory="/home/user/src/codex/codex-rs/protocol/src",
    ):
        conversation, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Parent",
        )
        child = await manager.create_child_session(
            parent,
            mode="sync",
            task_description="Inspect policy",
            agent_id="agent-1",
            effective_agent_id="agent-1",
        )
    del conversation, parent, child

    assert providers.guardrails.last_policy is not None
    assert (
        "/home/user/src/codex/codex-rs/protocol/src/*"
        not in providers.guardrails.last_policy["allow_paths"]
    )
    assert providers.guardrails.last_details is not None
    assert "working_directory" not in providers.guardrails.last_details

    await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_uses_explicit_delegation_workdir(tmp_path) -> None:
    """Explicit delegation paths are still reflected in Intaris details and policy."""

    from cognis.runtime_context import scoped_runtime_context

    engine, session_factory = await _session_factory(tmp_path)
    providers = _Providers()
    manager = SessionManager(session_factory, providers, _Cache())

    with scoped_runtime_context(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
    ):
        _, parent = await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Parent",
        )
        child = await manager.create_child_session(
            parent,
            mode="sync",
            task_description="Inspect policy",
            agent_id="agent-1",
            effective_agent_id="agent-1",
            workspace_root="/home/user/src/cognis",
            working_directory="/home/user/src/cognis/cognis/core",
        )
    del parent, child

    assert providers.guardrails.last_policy is not None
    assert "/home/user/src/cognis/cognis/core/*" in providers.guardrails.last_policy["allow_paths"]
    assert providers.guardrails.last_details is not None
    assert (
        providers.guardrails.last_details["working_directory"]
        == "/home/user/src/cognis/cognis/core"
    )

    await engine.dispose()
