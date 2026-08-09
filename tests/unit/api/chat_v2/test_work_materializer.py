from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from cognis.api.chat_v2.append_listener import EventAppendListenerFastPath
from cognis.api.chat_v2.cached_event_store import AppendInvalidation
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventPage, SessionWatermark
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    FileDiffRef,
    SourceRef,
    TimelineScope,
    ToolCallTimelineItem,
    WorkMaterialization,
    WorkstreamRef,
    WorkSummary,
)
from cognis.api.chat_v2.work_materializer import (
    WORK_APPEND_PENDING,
    WORK_APPEND_PENDING_BYTES,
    WORK_MATERIALIZER_VERSION,
    WORK_RECORD_MAX_BYTES,
    WorkMaterializer,
    _bounded_item,
    _file_path_id,
    _merged_tool_status,
    _record_metadata,
    lock_work_projection_state,
)
from cognis.api.chat_v2.work_repository import (
    WorkCursorError,
    _aggregate_logical_summaries,
    _collapse_activity_workstreams,
    _empty_summary,
    _logical_summary_statements,
    _LogicalProjection,
    _overview_revision,
    read_activity_overview,
    read_work_page,
)
from cognis.bootstrap import run_schema_bootstrap
from cognis.models.session import SessionEvent
from cognis.models.tool import (
    NativeToolOperation,
    ToolDefinition,
    ToolMutationKind,
    ToolSource,
    declared_default_semantics,
)
from cognis.providers.guardrails.events import (
    EventAppendNotification,
    EventStoreAuthority,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Conversation,
    DirectTurnRequestRow,
    Session,
    User,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)


def test_materializer_version_rebuilds_filtered_work_evidence() -> None:
    assert WORK_MATERIALIZER_VERSION == "work-v4"


def _overview_node(
    session_id: str,
    *,
    parent_key: str | None = None,
    current: bool = False,
    activity_state: str = "active",
    updated_at: str = "2026-01-01T00:00:00+00:00",
    kind: str = "rotation",
) -> WorkstreamRef:
    return WorkstreamRef(
        key=f"session:{session_id}",
        kind=kind,
        parent_key=parent_key,
        root_key="session:s3",
        edge_kind=kind,
        ordinal=0,
        conversation_id="conversation-root",
        session_id=session_id,
        event_store_session_id=session_id,
        title=session_id,
        agent_id="agent-alice",
        status="active",
        current=current,
        activity_state=activity_state,
        updated_at=updated_at,
    )


def test_logical_projection_collapses_rotation_and_canonicalizes_old_parent() -> None:
    nodes = [
        _overview_node("s1", activity_state="closed"),
        _overview_node("s2", parent_key="session:s1", activity_state="ongoing"),
        _overview_node("s3", parent_key="session:s2", current=True),
        _overview_node(
            "child-old",
            parent_key="session:s1",
            kind="managed",
        ).model_copy(update={"conversation_id": "conversation-child"}),
        _overview_node(
            "child-new",
            parent_key="session:child-old",
            kind="rotation",
            current=True,
        ).model_copy(update={"conversation_id": "conversation-child"}),
    ]
    rows = [
        SimpleNamespace(
            session_id="s1",
            previous_session_id=None,
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="s2",
            previous_session_id="s1",
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="s3",
            previous_session_id="s2",
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="child-old",
            previous_session_id=None,
            conversation_id="conversation-child",
            activity_scope_id="scope-child",
        ),
        SimpleNamespace(
            session_id="child-new",
            previous_session_id="child-old",
            conversation_id="conversation-child",
            activity_scope_id="scope-child",
        ),
    ]

    projected = _collapse_activity_workstreams(
        nodes,
        session_rows=rows,
        physical_root_key="session:s3",
    )

    assert [node.session_id for node in projected.workstreams] == ["s3", "child-new"]
    root, child = projected.workstreams
    assert root.kind == "root"
    assert root.backing_session_ids == ["s1", "s2", "s3"]
    assert root.backing_session_count == 3
    assert root.activity_state == "ongoing"
    assert child.parent_key == root.key
    assert child.backing_session_ids == ["child-new", "child-old"]
    assert projected.ambiguous is False


def test_logical_projection_does_not_cross_scope_and_cycle_is_deterministic() -> None:
    nodes = [
        _overview_node("a", parent_key="session:b"),
        _overview_node("b", parent_key="session:a"),
        _overview_node("reset", parent_key="session:a"),
    ]
    rows = [
        SimpleNamespace(
            session_id="a",
            previous_session_id="b",
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="b",
            previous_session_id="a",
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="reset",
            previous_session_id="a",
            conversation_id="conversation-root",
            activity_scope_id="scope-reset",
        ),
    ]

    first = _collapse_activity_workstreams(
        nodes,
        session_rows=rows,
        physical_root_key="session:a",
    )
    second = _collapse_activity_workstreams(
        list(reversed(nodes)),
        session_rows=list(reversed(rows)),
        physical_root_key="session:a",
    )

    assert [node.model_dump() for node in first.workstreams] == [
        node.model_dump() for node in second.workstreams
    ]
    assert len(first.workstreams) == 2
    assert first.physical_to_logical["session:reset"] == "session:reset"
    reset = next(node for node in first.workstreams if node.session_id == "reset")
    assert reset.parent_key == first.physical_to_logical["session:a"]
    assert reset.kind != "root"


def test_logical_projection_missing_predecessor_is_disconnected_and_ambiguous() -> None:
    node = _overview_node("orphan", parent_key="session:missing")
    row = SimpleNamespace(
        session_id="orphan",
        previous_session_id="missing",
        conversation_id="conversation-root",
        activity_scope_id="scope-a",
    )

    projected = _collapse_activity_workstreams(
        [node],
        session_rows=[row],
        physical_root_key="session:other-root",
    )

    assert projected.workstreams[0].parent_key is None
    assert projected.workstreams[0].kind != "root"
    assert projected.ambiguous is True


def test_logical_projection_marks_conflicting_external_parents_ambiguous() -> None:
    nodes = [
        _overview_node("parent-a", kind="managed"),
        _overview_node("parent-b", kind="managed"),
        _overview_node("old", parent_key="session:parent-a", kind="managed"),
        _overview_node(
            "new",
            parent_key="session:parent-b",
            kind="managed",
            current=True,
        ),
    ]
    rows = [
        SimpleNamespace(
            session_id="parent-a",
            previous_session_id=None,
            conversation_id="parent-a",
            activity_scope_id="parent-a",
        ),
        SimpleNamespace(
            session_id="parent-b",
            previous_session_id=None,
            conversation_id="parent-b",
            activity_scope_id="parent-b",
        ),
        SimpleNamespace(
            session_id="old",
            previous_session_id=None,
            conversation_id="child",
            activity_scope_id="child-scope",
        ),
        SimpleNamespace(
            session_id="new",
            previous_session_id="old",
            conversation_id="child",
            activity_scope_id="child-scope",
        ),
    ]

    projected = _collapse_activity_workstreams(
        nodes,
        session_rows=rows,
        physical_root_key="session:parent-a",
    )

    assert projected.ambiguous is True
    child = next(node for node in projected.workstreams if node.session_id == "new")
    assert child.parent_key == "session:parent-b"


def test_logical_summary_distincts_entities_and_adds_event_totals() -> None:
    logical = _LogicalProjection(
        workstreams=[],
        physical_to_logical={
            "session:s1": "session:s2",
            "session:s2": "session:s2",
        },
        members_by_logical={"session:s2": ("s1", "s2")},
        ambiguous=False,
    )
    summaries = _aggregate_logical_summaries(
        physical_summaries={
            "s1": WorkSummary(
                mutations=2,
                commands=3,
                changed_files=2,
                artifacts=1,
                deliverables=1,
                additions=10,
                deletions=4,
            ),
            "s2": WorkSummary(
                mutations=5,
                commands=7,
                changed_files=2,
                artifacts=2,
                deliverables=2,
                additions=20,
                deletions=6,
            ),
        },
        logical=logical,
        file_counts=[("session:s2", 3)],
        entity_counts=[
            ("session:s2", "artifacts", 2),
            ("session:s2", "deliverables", 2),
        ],
    )

    assert summaries["session:s2"] == WorkSummary(
        mutations=7,
        commands=10,
        changed_files=3,
        artifacts=2,
        deliverables=2,
        additions=30,
        deletions=10,
    )


def test_logical_summary_queries_compile_for_sqlite_and_postgresql() -> None:
    base = select(WorkRecordRow).where(WorkRecordRow.owner_email == "owner@example.com")
    statements = _logical_summary_statements(
        statement=base,
        logical_by_session={"s1": "session:s2", "s2": "session:s2"},
        owner_email="owner@example.com",
    )

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        compiled = [str(statement.compile(dialect=dialect)) for statement in statements]
        assert all("COUNT(DISTINCT" in sql.upper() for sql in compiled)
        assert all("CASE" in sql.upper() for sql in compiled)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_profile_id", "profile-deep"),
        ("model", "model-next"),
        ("reasoning_effort", "high"),
        ("agent_display_name", "Display Next"),
        ("agent_avatar_url", "https://example.test/next.png"),
        ("current", True),
        ("status", "completed"),
        ("activity_state", "closed"),
        ("completion_reason", "complete"),
        ("completed_at", "2026-01-02T00:00:00+00:00"),
        ("backing_session_ids", ["s1", "s2"]),
        ("task_id", "task-next"),
        ("link_id", "link-next"),
    ],
)
def test_overview_revision_changes_for_visible_workstream_fields(
    field: str,
    value: Any,
) -> None:
    base = _overview_node("revision")
    materialization = WorkMaterialization(
        state="caught_up",
        completed_streams=1,
        total_streams=1,
        covered_events=1,
        target_events=1,
    )

    before = _overview_revision(
        graph_fingerprint="graph",
        graph_truncated=False,
        materialization=materialization,
        summary=_empty_summary(),
        workstreams=[base],
        logical_membership={base.key: (base.session_id,)},
        recent_records=[],
    )
    after = _overview_revision(
        graph_fingerprint="graph",
        graph_truncated=False,
        materialization=materialization,
        summary=_empty_summary(),
        workstreams=[base.model_copy(update={field: value})],
        logical_membership={base.key: (base.session_id,)},
        recent_records=[],
    )

    assert before != after


@pytest.mark.asyncio
async def test_projection_creation_uses_postgres_transaction_lock() -> None:
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=AsyncMock(),
    )

    await lock_work_projection_state(db, "session-1")

    statement, params = db.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert params == {"key": "cognis-work-projection:session-1:work-v4"}


class AuthorityStore:
    def __init__(self, pages: dict[str, list[RawSessionEvent]]) -> None:
        self.pages = pages
        self.authorities: list[EventStoreAuthority] = []

    def bind(self, authority: EventStoreAuthority) -> Any:
        self.authorities.append(authority)
        store = self

        class Reader:
            async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
                events = store.pages.get(session_id, [])
                return SessionWatermark(
                    store_id="intaris",
                    session_id=session_id,
                    last_seq=events[-1].seq if events else 0,
                )

            async def read_session_events(
                self, *, session_id: str, after_seq: int, limit: int, direction: str
            ) -> SessionEventPage:
                assert direction == "forward"
                events = [
                    event for event in store.pages.get(session_id, []) if event.seq > after_seq
                ][:limit]
                return SessionEventPage(
                    store_id="intaris",
                    session_id=session_id,
                    events=events,
                    first_seq=events[0].seq if events else None,
                    last_seq=events[-1].seq if events else after_seq,
                    has_more_after=False,
                    verified_empty=not events,
                )

        return Reader()


async def _database(tmp_path: Path, *, owners: tuple[str, ...] = ("alice",)):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-materializer.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        for owner in owners:
            email = f"{owner}@example.com"
            db.add(User(email=email, name=owner, password_hash="x", role="user"))
        await db.flush()
        for owner in owners:
            email = f"{owner}@example.com"
            agent_id = f"agent-{owner}"
            db.add(
                Agent(
                    agent_id=agent_id,
                    owner_email=email,
                    name=owner,
                    description=owner,
                )
            )
        await db.flush()
        for owner in owners:
            email = f"{owner}@example.com"
            agent_id = f"agent-{owner}"
            conversation_id = f"conversation-{owner}"
            db.add(
                Conversation(
                    conversation_id=conversation_id,
                    user_email=email,
                    agent_id=agent_id,
                    context_type="web",
                )
            )
        await db.flush()
        for owner in owners:
            email = f"{owner}@example.com"
            agent_id = f"agent-{owner}"
            conversation_id = f"conversation-{owner}"
            session_id = f"session-{owner}"
            db.add(
                Session(
                    session_id=session_id,
                    conversation_id=conversation_id,
                    user_email=email,
                    agent_id=agent_id,
                    intaris_session_id=f"intaris-{owner}",
                    delegation_metadata={},
                )
            )
        await db.commit()
    return engine, factory


def _event(session_id: str, seq: int, event_type: str, data: dict[str, Any]) -> RawSessionEvent:
    return RawSessionEvent(
        store_id="intaris",
        session_id=session_id,
        seq=seq,
        type=event_type,
        data=data,
        timestamp=datetime(2026, 1, 1, 0, 0, seq, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_live_append_materializes_in_background(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    caught_up: list[str] = []
    commit_checks: list[asyncio.Task[None]] = []

    def after_projection_commit(conversation_id: str) -> None:
        caught_up.append(conversation_id)

        async def assert_committed() -> None:
            async with factory() as db:
                state = await db.scalar(select(WorkSessionProjectionRow))
                assert state is not None
                assert (state.covered_through_seq, state.state) == (1, "caught_up")

        commit_checks.append(asyncio.create_task(assert_committed()))

    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        on_projection_caught_up=after_projection_commit,
    )
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await materializer.handle_append(
        EventAppendNotification(
            authority=EventStoreAuthority(
                user_email="alice@example.com",
                agent_id="agent-alice",
                agent_owner_email="alice@example.com",
            ),
            session_id="intaris-alice",
            first_seq=1,
            last_seq=1,
            event_count=1,
            events=(
                SessionEvent(
                    type="tool_call",
                    data={"call_id": "live-call", "name": "write", "arguments": {"path": "a.py"}},
                ),
            ),
        )
    )
    for _ in range(100):
        async with factory() as db:
            state = await db.scalar(select(WorkSessionProjectionRow))
            record = await db.scalar(
                select(WorkRecordRow).where(WorkRecordRow.call_id == "live-call")
            )
        if state is not None and state.state == "caught_up" and record is not None:
            break
        await asyncio.sleep(0.01)
    assert state is not None
    assert (state.covered_through_seq, state.state) == (1, "caught_up")
    assert record is not None
    assert caught_up == ["conversation-alice"]
    await asyncio.gather(*commit_checks)
    await materializer.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_append_queue_coalesces_active_batches_to_max_target_repair(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_worker_count=2,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    processed: list[Any] = []

    async def blocked(item: Any) -> None:
        processed.append(item)
        started.set()
        await release.wait()

    materializer._process_append = blocked  # type: ignore[method-assign]
    materializer.start()
    authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )

    def notification(seq: int) -> EventAppendNotification:
        return EventAppendNotification(
            authority=authority,
            session_id="intaris-alice",
            first_seq=seq,
            last_seq=seq,
            event_count=1,
            events=(SessionEvent(type="user_message", data={"content": str(seq)}),),
        )

    assert materializer.enqueue_append(notification(1))
    await started.wait()
    assert materializer._append_pending_events == 1
    assert materializer._append_pending_bytes > 0
    assert materializer.enqueue_append(notification(1))
    assert materializer._append_repair_pending == {}
    assert materializer.enqueue_append(notification(2))
    assert materializer.enqueue_append(notification(3))
    pending = next(iter(materializer._append_repair_pending.values()))
    assert (pending.first_seq, pending.last_seq, pending.target_seq) == (2, 3, 3)
    assert pending.retained_events == 0
    assert pending.payload_bytes == 0
    assert materializer._append_pending == {}
    assert materializer._append_pending_events == 1
    assert materializer._append_pending_bytes <= materializer._append_max_pending_bytes

    release.set()
    for _ in range(100):
        if not materializer._append_pending and not materializer._append_active:
            break
        await asyncio.sleep(0.01)
    assert [(item.first_seq, item.last_seq) for item in processed] == [(1, 1), (2, 3)]
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0
    await materializer.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_append_queue_converts_missing_and_oversized_payloads_to_repair(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_max_session_bytes=1,
    )
    materializer._append_accepting = True
    authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )
    oversized = EventAppendNotification(
        authority=authority,
        session_id="intaris-alice",
        first_seq=1,
        last_seq=1,
        event_count=1,
        events=(SessionEvent(type="user_message", data={"content": "large"}),),
    )
    missing = EventAppendNotification(
        authority=authority,
        session_id="intaris-alice",
        first_seq=2,
        last_seq=2,
        event_count=1,
    )

    assert materializer.enqueue_append(oversized)
    assert materializer.enqueue_append(missing)
    pending = next(iter(materializer._append_repair_pending.values()))
    assert pending.repair_required is True
    assert pending.events == ()
    assert pending.target_seq == 2
    assert materializer._append_pending == {}
    assert materializer._append_pending_bytes == 0
    assert WORK_APPEND_PENDING._value.get() == 1
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await materializer.stop()
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_append_queue_gap_and_global_overflow_become_authoritative_repair(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_max_pending_events=2,
        append_max_pending_bytes=1024,
    )
    materializer._append_accepting = True
    authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )

    def notification(seq: int, content: str) -> EventAppendNotification:
        return EventAppendNotification(
            authority=authority,
            session_id="intaris-alice",
            first_seq=seq,
            last_seq=seq,
            event_count=1,
            events=(SessionEvent(type="user_message", data={"content": content}),),
        )

    assert materializer.enqueue_append(notification(1, "first"))
    assert materializer.enqueue_append(notification(3, "gap"))

    pending = next(iter(materializer._append_repair_pending.values()))
    assert pending.repair_required is True
    assert pending.target_seq == 3
    assert pending.events == ()
    assert materializer._append_pending == {}
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0

    await materializer.stop()

    overflow = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_max_pending_events=1,
    )
    overflow._append_accepting = True
    assert overflow.enqueue_append(notification(1, "first"))
    assert overflow.enqueue_append(notification(2, "second"))
    pending = next(iter(overflow._append_repair_pending.values()))
    assert pending.repair_required is True
    assert pending.target_seq == 2
    assert overflow._append_pending == {}
    assert overflow._append_pending_events == 0
    assert overflow._append_pending_bytes == 0
    await overflow.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_listener_saturation_retains_and_drains_all_sessions_without_polling(
    tmp_path: Path,
) -> None:
    owners = ("alice", "bob", "carol", "dave")
    engine, factory = await _database(tmp_path, owners=owners)
    pages = {
        f"intaris-{owner}": [
            _event(f"intaris-{owner}", 1, "user_message", {"content": owner}),
            *(
                [_event("intaris-alice", 2, "user_message", {"content": "alice-2"})]
                if owner == "alice"
                else []
            ),
        ]
        for owner in owners
    }
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore(pages),
        tool_definitions=lambda: {},
        append_max_pending_sessions=2,
    )
    materializer._append_accepting = True
    async with factory() as db:
        rows = (await db.scalars(select(Session))).all()
        for row in rows:
            state = await materializer._ensure_state(db, row, 0)
            state.state = "caught_up"
        await db.commit()

    class EventStore:
        def invalidate_append_local(
            self, notification: EventAppendNotification
        ) -> AppendInvalidation:
            return AppendInvalidation(
                session_token=notification.session_id,
                authority_token=notification.authority.user_email,
                last_seq=notification.last_seq,
                has_events=bool(notification.events),
                local_revision=notification.last_seq,
            )

    class PendingWarms:
        def __init__(self) -> None:
            self.values: dict[str, tuple[str, int, str]] = {}

        def put(self, token: str, value: tuple[str, int, str]) -> bool:
            self.values[token] = value
            return False

        def __len__(self) -> int:
            return len(self.values)

    class Dispatcher:
        def enqueue(self, _work: AppendInvalidation) -> bool:
            return True

    listener = EventAppendListenerFastPath(
        event_store=EventStore(),
        pending_warms=PendingWarms(),
        invalidation_dispatcher=Dispatcher(),
        work_materializer=materializer,
        on_mapping_size=lambda _size: None,
        on_mapping_overflow=lambda: None,
    )

    for owner in owners:
        authority = EventStoreAuthority(
            user_email=f"{owner}@example.com",
            agent_id=f"agent-{owner}",
            agent_owner_email=f"{owner}@example.com",
        )
        await listener(
            EventAppendNotification(
                authority=authority,
                session_id=f"intaris-{owner}",
                first_seq=1,
                last_seq=1,
                event_count=1,
                events=(SessionEvent(type="user_message", data={"content": owner}),),
            )
        )

    assert set(item.session_id for item in materializer._append_pending.values()) == {
        "intaris-carol",
        "intaris-dave",
    }
    assert {
        item.session_id: item.target_seq for item in materializer._append_repair_pending.values()
    } == {
        "intaris-alice": 1,
        "intaris-bob": 1,
    }

    alice_authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )
    for last_seq in (1, 2):
        await listener(
            EventAppendNotification(
                authority=alice_authority,
                session_id="intaris-alice",
                first_seq=last_seq,
                last_seq=last_seq,
                event_count=1,
                events=(
                    SessionEvent(
                        type="user_message",
                        data={"content": f"alice-{last_seq}"},
                    ),
                ),
            )
        )
    alice_repair = next(
        item
        for item in materializer._append_repair_pending.values()
        if item.session_id == "intaris-alice"
    )
    assert alice_repair.target_seq == 2
    assert alice_repair.events == ()
    assert alice_repair.payload_bytes == 0
    assert materializer._append_pending_events == 2

    materializer.start()
    expected_targets = {"session-alice": 2} | {
        f"session-{owner}": 1 for owner in owners if owner != "alice"
    }
    async with asyncio.timeout(5):
        while True:
            async with factory() as db:
                states = (
                    await db.scalars(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.materializer_version
                            == WORK_MATERIALIZER_VERSION
                        )
                    )
                ).all()
            covered = {state.session_id: state.covered_through_seq for state in states}
            if all(
                covered.get(session_id) == target for session_id, target in expected_targets.items()
            ):
                break
            await asyncio.sleep(0.01)

    await materializer.stop()
    assert materializer._append_pending == {}
    assert materializer._append_repair_pending == {}
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_rejects_without_queue_accounting(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    notification = EventAppendNotification(
        authority=EventStoreAuthority(
            user_email="alice@example.com",
            agent_id="agent-alice",
            agent_owner_email="alice@example.com",
        ),
        session_id="intaris-alice",
        first_seq=1,
        last_seq=1,
        event_count=1,
        events=(SessionEvent(type="user_message", data={"content": "rejected"}),),
    )

    assert materializer.enqueue_append(notification) is False
    assert materializer._append_pending == {}
    assert materializer._append_repair_pending == {}
    assert materializer._append_active == {}
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0

    await materializer.stop()
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_drains_payload_and_repair_intent(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    store = AuthorityStore(
        {
            "intaris-alice": [_event("intaris-alice", 1, "user_message", {"content": "alice"})],
            "intaris-bob": [_event("intaris-bob", 1, "user_message", {"content": "bob"})],
        }
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        append_max_pending_sessions=1,
    )
    materializer._append_accepting = True

    for owner in ("alice", "bob"):
        assert materializer.enqueue_append(
            EventAppendNotification(
                authority=EventStoreAuthority(
                    user_email=f"{owner}@example.com",
                    agent_id=f"agent-{owner}",
                    agent_owner_email=f"{owner}@example.com",
                ),
                session_id=f"intaris-{owner}",
                first_seq=1,
                last_seq=1,
                event_count=1,
                events=(SessionEvent(type="user_message", data={"content": owner}),),
            )
        )

    assert len(materializer._append_pending) == 1
    assert len(materializer._append_repair_pending) == 1
    materializer.start()
    await materializer.stop()

    async with factory() as db:
        states = (await db.scalars(select(WorkSessionProjectionRow))).all()
    by_session = {state.session_id: state for state in states}
    assert by_session["session-alice"].target_seq == 1
    assert by_session["session-alice"].state == "repair"
    assert by_session["session-bob"].covered_through_seq == 1
    assert by_session["session-bob"].state == "caught_up"
    assert materializer._append_pending == {}
    assert materializer._append_repair_pending == {}
    assert materializer._append_pending_events == 0
    assert materializer._append_pending_bytes == 0
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_timeout_retains_repair_intent_for_retry(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    original_process_append = materializer._process_append
    original_mark_source_pending = materializer.mark_source_pending
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def blocked_mark_source_pending(
        *, owner_email: str, source_session_id: str, target_seq: int
    ) -> bool:
        del owner_email, source_session_id, target_seq
        entered.set()
        await blocked.wait()
        return True

    async def fail_process(_item: Any) -> None:
        raise RuntimeError("materialization failed")

    materializer._process_append = fail_process  # type: ignore[method-assign]
    materializer.mark_source_pending = blocked_mark_source_pending  # type: ignore[method-assign]
    materializer._append_accepting = True
    assert materializer.enqueue_append(
        EventAppendNotification(
            authority=EventStoreAuthority(
                user_email="alice@example.com",
                agent_id="agent-alice",
                agent_owner_email="alice@example.com",
            ),
            session_id="intaris-alice",
            first_seq=1,
            last_seq=2,
            event_count=2,
            events=(
                SessionEvent(type="user_message", data={"content": "one"}),
                SessionEvent(type="user_message", data={"content": "two"}),
            ),
        )
    )
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await entered.wait()

    with pytest.raises(TimeoutError, match="retained repair intents"):
        await materializer.stop(timeout_seconds=0.01)

    repair = next(iter(materializer._append_repair_pending.values()))
    assert repair.session_id == "intaris-alice"
    assert repair.target_seq == 2
    assert repair.events == ()
    assert repair.payload_bytes == 0
    assert WORK_APPEND_PENDING._value.get() == 1
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0

    materializer._process_append = original_process_append  # type: ignore[method-assign]
    materializer.mark_source_pending = original_mark_source_pending  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await materializer.stop()
    async with factory() as db:
        state = await db.scalar(select(WorkSessionProjectionRow))
    assert state is not None
    assert (state.state, state.target_seq) == ("repair", 2)
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_append_and_repair_persistence_retain_monotonic_intent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, factory = await _database(tmp_path)
    store = AuthorityStore(
        {
            "intaris-alice": [
                _event("intaris-alice", 1, "user_message", {"content": "one"}),
                _event("intaris-alice", 2, "user_message", {"content": "two"}),
            ]
        }
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        append_retry_initial_seconds=10,
        append_retry_max_seconds=10,
        append_retry_jitter_ratio=0,
    )
    original_process_append = materializer._process_append
    original_mark_source_pending = materializer.mark_source_pending
    process_calls = 0
    persist_calls = 0

    async def fail_process(_item: Any) -> None:
        nonlocal process_calls
        process_calls += 1
        raise RuntimeError("materialization failed")

    async def fail_persist(*, owner_email: str, source_session_id: str, target_seq: int) -> bool:
        nonlocal persist_calls
        del owner_email, source_session_id, target_seq
        persist_calls += 1
        raise RuntimeError("database unavailable")

    materializer._process_append = fail_process  # type: ignore[method-assign]
    materializer.mark_source_pending = fail_persist  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )

    with caplog.at_level("INFO"):
        assert materializer.enqueue_append(
            EventAppendNotification(
                authority=authority,
                session_id="intaris-alice",
                first_seq=1,
                last_seq=1,
                event_count=1,
                events=(SessionEvent(type="user_message", data={"content": "one"}),),
            )
        )
        async with asyncio.timeout(1):
            while not materializer._append_repair_pending:
                await asyncio.sleep(0.01)

        repair = next(iter(materializer._append_repair_pending.values()))
        assert repair.target_seq == 1
        assert repair.events == ()
        assert repair.payload_bytes == 0
        assert repair.retry_count == 1
        assert materializer._append_pending_events == 0
        assert materializer._append_pending_bytes == 0
        assert WORK_APPEND_PENDING._value.get() == 1
        assert WORK_APPEND_PENDING_BYTES._value.get() == 0
        assert process_calls == 1
        assert persist_calls == 1
        assert "repair retained for retry" in caplog.text

        retry_not_before = repair.retry_not_before
        assert materializer.enqueue_append(
            EventAppendNotification(
                authority=authority,
                session_id="intaris-alice",
                first_seq=2,
                last_seq=2,
                event_count=1,
                events=(SessionEvent(type="user_message", data={"content": "two"}),),
            )
        )
        repair = next(iter(materializer._append_repair_pending.values()))
        assert repair.target_seq == 2
        assert repair.retry_not_before == retry_not_before
        assert repair.events == ()
        assert repair.payload_bytes == 0
        assert len(materializer._append_repair_pending) == 1
        assert materializer._append_pending == {}
        assert materializer._append_pending_events == 0
        assert materializer._append_pending_bytes == 0

        materializer._process_append = original_process_append  # type: ignore[method-assign]
        materializer.mark_source_pending = original_mark_source_pending  # type: ignore[method-assign]
        repair.retry_not_before = 0.0
        materializer._append_available.set()
        async with asyncio.timeout(1):
            while materializer._append_repair_pending or materializer._append_active:
                await asyncio.sleep(0.01)

    async with factory() as db:
        state = await db.scalar(select(WorkSessionProjectionRow))
    assert state is not None
    assert (state.state, state.target_seq) == ("repair", 2)
    assert "repair persisted" in caplog.text
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await materializer.stop()
    await engine.dispose()


@pytest.mark.asyncio
async def test_sustained_repair_persistence_failure_uses_bounded_backoff(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_retry_initial_seconds=0.05,
        append_retry_max_seconds=0.2,
        append_retry_jitter_ratio=0,
    )
    original_mark_source_pending = materializer.mark_source_pending
    call_times: list[float] = []

    async def fail_persist(*, owner_email: str, source_session_id: str, target_seq: int) -> bool:
        del owner_email, source_session_id, target_seq
        call_times.append(asyncio.get_running_loop().time())
        raise RuntimeError("database unavailable")

    materializer.mark_source_pending = fail_persist  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    assert materializer.enqueue_append(
        EventAppendNotification(
            authority=EventStoreAuthority(
                user_email="alice@example.com",
                agent_id="agent-alice",
                agent_owner_email="alice@example.com",
            ),
            session_id="intaris-alice",
            first_seq=1,
            last_seq=1,
            event_count=1,
        )
    )

    await asyncio.sleep(0.14)

    assert 2 <= len(call_times) <= 3
    assert all(
        later - earlier >= 0.04 for earlier, later in zip(call_times, call_times[1:], strict=False)
    )
    repair = next(iter(materializer._append_repair_pending.values()))
    assert repair.retry_count >= 2
    assert repair.events == ()
    assert repair.payload_bytes == 0
    assert WORK_APPEND_PENDING._value.get() == 1
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0

    with pytest.raises(TimeoutError, match="retained repair intents"):
        await materializer.stop(timeout_seconds=0.01)
    assert len(materializer._append_repair_pending) == 1
    materializer.mark_source_pending = original_mark_source_pending  # type: ignore[method-assign]
    repair = next(iter(materializer._append_repair_pending.values()))
    repair.retry_not_before = 0.0
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await materializer.stop()
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_payload_append_with_missing_session_retains_repair_intent(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        append_retry_initial_seconds=10,
        append_retry_max_seconds=10,
        append_retry_jitter_ratio=0,
    )
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    authority = EventStoreAuthority(
        user_email="alice@example.com",
        agent_id="agent-alice",
        agent_owner_email="alice@example.com",
    )
    assert materializer.enqueue_append(
        EventAppendNotification(
            authority=authority,
            session_id="intaris-late",
            first_seq=1,
            last_seq=1,
            event_count=1,
            events=(SessionEvent(type="user_message", data={"content": "late"}),),
        )
    )

    async with asyncio.timeout(1):
        while not materializer._append_repair_pending:
            await asyncio.sleep(0.01)
    repair = next(iter(materializer._append_repair_pending.values()))
    assert repair.session_id == "intaris-late"
    assert repair.target_seq == 1
    assert repair.events == ()
    assert repair.payload_bytes == 0

    async with factory() as db:
        original = await db.get(Session, "session-alice")
        assert original is not None
        db.add(
            Session(
                session_id="session-late",
                conversation_id=original.conversation_id,
                user_email=original.user_email,
                agent_id=original.agent_id,
                intaris_session_id="intaris-late",
                delegation_metadata={},
            )
        )
        await db.commit()
    repair.retry_not_before = 0.0
    materializer._append_available.set()
    async with asyncio.timeout(1):
        while materializer._append_repair_pending or materializer._append_active:
            await asyncio.sleep(0.01)

    async with factory() as db:
        state = await db.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == "session-late"
            )
        )
    assert state is not None
    assert (state.state, state.target_seq) == ("repair", 1)
    await materializer.stop()
    assert WORK_APPEND_PENDING._value.get() == 0
    assert WORK_APPEND_PENDING_BYTES._value.get() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_zero_evidence_advances_coverage_and_replay_is_idempotent(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    store = AuthorityStore({})
    materializer = WorkMaterializer(
        session_factory=factory, event_store=store, tool_definitions=lambda: {}
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        event = _event("intaris-alice", 1, "user_message", {"content": "hello"})
        await materializer._materialize_batch(
            db, row=row, state=state, raw_events=[event], target_seq=1
        )
        await db.commit()
        assert state.covered_through_seq == 1
        assert state.state == "caught_up"
        assert await db.scalar(select(func.count()).select_from(WorkRecordRow)) == 0

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, state.projection_id)
        assert state is not None
        state.covered_through_seq = 0
        row = await db.get(Session, "session-alice")
        assert row is not None
        await materializer._materialize_batch(
            db, row=row, state=state, raw_events=[event], target_seq=1
        )
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(WorkRecordRow)) == 0
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_first", [False, True], ids=["call-result", "result-call"])
async def test_cross_batch_write_pairing_recomputes_queryable_evidence_in_both_orders(
    tmp_path: Path,
    result_first: bool,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {
            "manage": ToolDefinition(
                name="manage",
                description="Manage resources",
                source=ToolSource(type="executor"),
                read_only=False,
                category="agent_management",
                native_operations=[
                    NativeToolOperation(
                        operation="list",
                        summary="List resources",
                        mutation_kind=ToolMutationKind.READ,
                        input_schema={
                            "type": "object",
                            "properties": {"action": {"const": "list"}},
                            "required": ["action"],
                        },
                        semantics=declared_default_semantics(ToolMutationKind.READ),
                        examples=[{"action": "list"}],
                    ),
                    NativeToolOperation(
                        operation="create",
                        summary="Create resource",
                        mutation_kind=ToolMutationKind.CREATE,
                        input_schema={
                            "type": "object",
                            "properties": {"action": {"const": "create"}},
                            "required": ["action"],
                        },
                        semantics=declared_default_semantics(ToolMutationKind.CREATE),
                        examples=[{"action": "create"}],
                    ),
                ],
            )
        },
    )
    result = _event(
        "intaris-alice",
        1 if result_first else 2,
        "tool_result",
        {"call_id": "call-1", "name": "tool", "result": "saved"},
    )
    call = _event(
        "intaris-alice",
        2 if result_first else 1,
        "tool_call",
        {
            "call_id": "call-1",
            "name": "manage",
            "arguments": {
                "action": "create",
                "path": "a.py",
                "content": "x",
                "mode": "replace",
            },
        },
    )
    first, second = (result, call) if result_first else (call, result)
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 2)
        await materializer._materialize_batch(
            db, row=row, state=state, raw_events=[first], target_seq=2
        )
        await db.commit()
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        state = await db.scalar(select(WorkSessionProjectionRow))
        assert row is not None and state is not None
        await materializer._materialize_batch(
            db, row=row, state=state, raw_events=[second], target_seq=2
        )
        await db.commit()
        records = (
            await db.scalars(select(WorkRecordRow).where(WorkRecordRow.call_id == "call-1"))
        ).all()
        assert len(records) == 2
        for record in records:
            merged = ToolCallTimelineItem.model_validate(record.timeline_item)
            assert merged.arguments == {
                "action": "create",
                "path": "a.py",
                "content": "x",
                "mode": "replace",
            }
            assert merged.result_preview == "saved"
            assert merged.status == "complete"
        assert sum(record.is_evidence for record in records) == 1
        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=TimelineScope(
                key="session:session-alice",
                kind="session",
                session_id="session-alice",
            ),
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=10,
        )
        assert len(page.items) == 1
        projected = page.items[0]
        assert isinstance(projected, ToolCallTimelineItem)
        assert projected.arguments == {
            "action": "create",
            "path": "a.py",
            "content": "x",
            "mode": "replace",
        }
        assert projected.result_preview == "saved"
    await engine.dispose()


def test_projected_payload_is_bounded() -> None:
    item = ToolCallTimelineItem(
        id="tool:large",
        call_id="large",
        tool_name="write",
        sort_key="1",
        source_refs=[
            SourceRef(
                store="intaris",
                session_id="session",
                seq=1,
                event_type="tool_result",
            )
        ],
        result_preview="x" * (WORK_RECORD_MAX_BYTES * 2),
        status="complete",
    )
    payload = _bounded_item(item)
    assert len(json.dumps(payload, separators=(",", ":")).encode()) <= WORK_RECORD_MAX_BYTES
    assert payload["truncated"] is True


def test_large_file_evidence_keeps_parent_record_bounded() -> None:
    item = ToolCallTimelineItem(
        id="tool:large-files",
        call_id="large-files",
        tool_name="write",
        sort_key="1",
        file_diffs=[
            FileDiffRef(
                path=f"file-{index}.py",
                path_id=f"root:file-{index}.py",
                diff=("+" + "x" * 2_000_000) if index == 0 else ("+" + "x" * 10_000),
            )
            for index in range(500)
        ],
        status="complete",
    )

    payload = _bounded_item(item)

    assert len(json.dumps(payload, separators=(",", ":")).encode()) <= WORK_RECORD_MAX_BYTES
    assert payload["file_diffs"] == []


def test_record_metadata_uses_canonical_file_path_identity() -> None:
    item = ToolCallTimelineItem(
        id="tool:aliases",
        call_id="aliases",
        tool_name="write",
        sort_key="1",
        arguments={"workdir": "/repo"},
        file_diffs=[
            FileDiffRef(path="/repo/src/a.py", diff="+one"),
            FileDiffRef(path="src/a.py", diff="-old\n+new"),
        ],
        status="complete",
    )
    definitions = {
        "write": ToolDefinition(
            name="write",
            description="Write",
            source=ToolSource(type="skill"),
            read_only=False,
            category="filesystem",
        )
    }

    metadata = _record_metadata(item, definitions)

    assert _file_path_id(item.file_diffs[0], workdir="/repo") == _file_path_id(
        item.file_diffs[1], workdir="/repo"
    )
    assert metadata["file_path_ids"] == []
    assert (metadata["additions"], metadata["deletions"]) == (2, 1)


def test_path_identity_is_component_aware() -> None:
    root_file = FileDiffRef(path="a.py", diff="")
    parent_file = FileDiffRef(path="../a.py", diff="")
    dotted_file = FileDiffRef(path="src/./a.py", diff="")
    normalized_file = FileDiffRef(path="src/a.py", diff="")
    absolute_file = FileDiffRef(path="/repo/src/a.py", diff="")
    windows_file = FileDiffRef(path="C:/repo/src/a.py", diff="")
    windows_alias = FileDiffRef(path="c:/repo/src/./a.py", diff="")
    other_drive_file = FileDiffRef(path="D:/repo/src/a.py", diff="")

    assert _file_path_id(root_file, workdir="/repo") != _file_path_id(parent_file, workdir="/repo")
    assert _file_path_id(dotted_file, workdir="/repo") == _file_path_id(
        normalized_file, workdir="/repo"
    )
    assert _file_path_id(absolute_file, workdir="/repo") == _file_path_id(
        normalized_file, workdir="/repo"
    )
    assert _file_path_id(windows_file, workdir="C:/repo") == _file_path_id(
        windows_alias, workdir="c:/repo"
    )
    assert _file_path_id(windows_file, workdir="C:/repo") != _file_path_id(
        other_drive_file, workdir="C:/repo"
    )


@pytest.mark.asyncio
async def test_thousands_of_file_paths_materialize_and_remain_queryable(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    definitions = {
        "write": ToolDefinition(
            name="write",
            description="Write",
            source=ToolSource(type="skill"),
            read_only=False,
            category="filesystem",
        )
    }
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: definitions,
    )
    file_count = 4_000
    event = _event(
        "intaris-alice",
        1,
        "tool_result",
        {
            "call_id": "many-files",
            "name": "write",
            "file_diffs": [
                {
                    "path": f"src/file-{index:04d}.py",
                    "diff": "+line",
                }
                for index in range(file_count)
            ],
        },
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[event],
            target_seq=1,
        )
        await db.commit()
        assert state.state == "caught_up"
        record = await db.scalar(select(WorkRecordRow).where(WorkRecordRow.call_id == "many-files"))
        assert record is not None
        assert (
            len(json.dumps(record.timeline_item, separators=(",", ":")).encode())
            <= WORK_RECORD_MAX_BYTES
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkRecordFileRow)
                .where(WorkRecordFileRow.work_record_id == record.work_record_id)
            )
            == file_count
        )
        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=TimelineScope(
                key="session:session-alice",
                kind="session",
                session_id="session-alice",
            ),
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )
        projected = next(item for item in page.items if isinstance(item, ToolCallTimelineItem))
        assert len(projected.file_diffs) == file_count
        assert {diff.path for diff in projected.file_diffs} == {
            f"src/file-{index:04d}.py" for index in range(file_count)
        }
        assert all(diff.diff == "" for diff in projected.file_diffs)
        assert all(diff.content_truncated for diff in projected.file_diffs)
        assert page.summary is not None
        assert page.summary.changed_files == file_count
    await engine.dispose()


@pytest.mark.asyncio
async def test_small_file_diff_preview_survives_materialization_and_hydration(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    definitions = {
        "write": ToolDefinition(
            name="write",
            description="Write",
            source=ToolSource(type="skill"),
            read_only=False,
            category="filesystem",
        )
    }
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: definitions,
    )
    event = _event(
        "intaris-alice",
        1,
        "tool_result",
        {
            "call_id": "small-file",
            "name": "write",
            "file_diffs": [
                {
                    "path": "src/renamed.py",
                    "old_path": "src/deleted.py",
                    "status": "renamed",
                    "diff": "+line",
                    "binary": True,
                    "generated": True,
                    "truncated": True,
                    "preview_omitted": True,
                },
                {
                    "path": "src/removed.py",
                    "status": "deleted",
                    "diff": "-line",
                },
            ],
        },
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[event],
            target_seq=1,
        )
        await db.commit()

        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=TimelineScope(
                key="session:session-alice",
                kind="session",
                session_id="session-alice",
            ),
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )

        projected = next(item for item in page.items if isinstance(item, ToolCallTimelineItem))
        assert len(projected.file_diffs) == 2
        diff = projected.file_diffs[0]
        assert diff.path == "src/renamed.py"
        assert diff.path_id is not None
        assert diff.diff == "+line"
        assert diff.old_path == "src/deleted.py"
        assert diff.status == "renamed"
        assert diff.binary is True
        assert diff.generated is True
        assert diff.truncated is True
        assert diff.preview_omitted is True
        assert diff.additions == 1
        assert diff.deletions == 0
        assert diff.content_truncated is False
        deleted = projected.file_diffs[1]
        assert deleted.path == "src/removed.py"
        assert deleted.status == "deleted"
        assert deleted.diff == "-line"
        assert deleted.additions == 0
        assert deleted.deletions == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_binds_each_owner_authority_for_persisted_repair_states(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    pages = {
        "intaris-alice": [_event("intaris-alice", 1, "user_message", {"content": "a"})],
        "intaris-bob": [_event("intaris-bob", 1, "user_message", {"content": "b"})],
    }
    store = AuthorityStore(pages)
    materializer = WorkMaterializer(
        session_factory=factory, event_store=store, tool_definitions=lambda: {}
    )
    async with factory() as db:
        for owner in ("alice", "bob"):
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await materializer._ensure_state(db, row, 1)
            state.state = "repair"
        await db.commit()

    claimed = await materializer._claim()
    assert len(claimed) == 2
    for state in claimed:
        await materializer._repair(state.projection_id)
    async with factory() as db:
        states = (await db.scalars(select(WorkSessionProjectionRow))).all()
        assert {state.state for state in states} == {"caught_up"}
        assert {state.covered_through_seq for state in states} == {1}
    assert {(item.user_email, item.agent_id) for item in store.authorities} == {
        ("alice@example.com", "agent-alice"),
        ("bob@example.com", "agent-bob"),
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_lease_fence_cannot_commit_after_reclaim(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    store = AuthorityStore(
        {
            "intaris-alice": [
                _event("intaris-alice", 1, "tool_call", {"call_id": "c", "name": "write"})
            ]
        }
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        worker_id="stale-worker",
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        state.state = "materializing"
        state.lease_owner = "stale-worker"
        state.lease_fence = 7
        state.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await db.commit()

    original_bind = store.bind

    def reclaiming_bind(authority: EventStoreAuthority) -> Any:
        reader = original_bind(authority)
        original_read = reader.read_session_events

        async def read_and_reclaim(**kwargs: Any) -> SessionEventPage:
            page = await original_read(**kwargs)
            async with factory() as db:
                state = await db.scalar(select(WorkSessionProjectionRow))
                assert state is not None
                state.lease_owner = "new-worker"
                state.lease_fence = 8
                await db.commit()
            return page

        reader.read_session_events = read_and_reclaim
        return reader

    store.bind = reclaiming_bind  # type: ignore[method-assign]
    await materializer._repair(state.projection_id)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(WorkRecordRow)) == 0
        state = await db.get(WorkSessionProjectionRow, state.projection_id)
        assert state is not None
        assert (state.lease_owner, state.lease_fence) == ("new-worker", 8)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_survives_head_check_fence_but_rejects_late_materialization(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    scope = TimelineScope(key="session:session-alice", kind="session", session_id="session-alice")
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = WorkSessionProjectionRow(
            projection_id="projection",
            owner_email=row.user_email,
            session_id=row.session_id,
            source_session_id="intaris-alice",
            materializer_version=WORK_MATERIALIZER_VERSION,
            target_seq=2,
            covered_through_seq=2,
            state="caught_up",
        )
        db.add(state)
        for seq in (2, 1):
            item = ToolCallTimelineItem(
                id=f"tool:{seq}",
                call_id=f"call-{seq}",
                tool_name="write",
                sort_key=str(seq),
                source_refs=[
                    SourceRef(
                        store="intaris",
                        session_id="intaris-alice",
                        seq=seq,
                        event_type="tool_call",
                    )
                ],
                status="complete",
            )
            db.add(
                WorkRecordRow(
                    work_record_id=f"record-{seq}",
                    owner_email=row.user_email,
                    session_id=row.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id="intaris-alice",
                    source_seq=seq,
                    source_item_id=item.id,
                    item_ordinal=0,
                    occurred_at=datetime(2026, 1, seq, tzinfo=UTC),
                    record_type=item.kind,
                    call_id=item.call_id,
                    timeline_item=item.model_dump(mode="json"),
                )
            )
        await db.commit()
        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
        )
        assert page.before_cursor
        state.lease_fence += 1
        state.head_checked_at = datetime.now(UTC)
        await db.commit()
        unchanged = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=page.before_cursor,
            limit=1,
        )
        assert len(unchanged.items) == 1
        assert isinstance(unchanged.items[0], ToolCallTimelineItem)
        assert unchanged.items[0].call_id == "call-1"
        state.covered_through_seq = 3
        state.target_seq = 3
        await db.commit()
        with pytest.raises(WorkCursorError):
            await read_work_page(
                db,
                owner_email=row.user_email,
                scope=scope,
                session_rows=[row],
                graph_fingerprint="graph",
                cursor_secret="secret",
                before=page.before_cursor,
                limit=1,
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_artifact_row_removes_projected_artifact(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    scope = TimelineScope(key="session:session-alice", kind="session", session_id="session-alice")
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        db.add(
            WorkSessionProjectionRow(
                projection_id="projection-artifact",
                owner_email=row.user_email,
                session_id=row.session_id,
                source_session_id="intaris-alice",
                materializer_version=WORK_MATERIALIZER_VERSION,
                target_seq=1,
                covered_through_seq=1,
                state="caught_up",
            )
        )
        item = ArtifactTimelineItem(
            id="artifact:missing",
            artifact_id="missing",
            filename="secret.txt",
            sort_key="1",
            source_refs=[
                SourceRef(
                    store="intaris",
                    session_id="intaris-alice",
                    seq=1,
                    event_type="artifact",
                )
            ],
        )
        db.add(
            WorkRecordRow(
                work_record_id="artifact-record",
                owner_email=row.user_email,
                session_id=row.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                source_store="intaris",
                source_session_id="intaris-alice",
                source_seq=1,
                source_item_id=item.id,
                item_ordinal=0,
                occurred_at=datetime.now(UTC),
                record_type=item.kind,
                is_evidence=True,
                category="artifacts",
                entity_id=item.artifact_id,
                timeline_item=item.model_dump(mode="json"),
            )
        )
        await db.commit()
        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=10,
        )
        assert page.items == []
        overview = await read_activity_overview(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            workstreams=[
                WorkstreamRef(
                    key=f"session:{row.session_id}",
                    kind="root",
                    root_key=f"session:{row.session_id}",
                    edge_kind="root",
                    ordinal=0,
                    conversation_id=row.conversation_id,
                    session_id=row.session_id,
                    event_store_session_id=row.intaris_session_id,
                    title="Alice session",
                    agent_id=row.agent_id,
                    status=row.status,
                )
            ],
            graph_fingerprint="graph",
            graph_truncated=False,
        )
        assert overview.recent_work.artifacts == []
        assert overview.summary.artifacts == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_idle_start_does_not_scan_sessions_create_states_or_read_intaris(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    statements: list[str] = []

    @sa_event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    store = AuthorityStore({})
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    materializer.start()
    await asyncio.sleep(0.05)
    await materializer.stop()
    worker_statements = tuple(statements)

    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(WorkSessionProjectionRow)) == 0
    assert store.authorities == []
    assert not any(" FROM sessions" in statement for statement in worker_statements)
    await engine.dispose()


@pytest.mark.asyncio
async def test_thousands_of_caught_up_states_never_trigger_head_reads(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    async with factory() as db:
        base = await db.get(Session, "session-alice")
        assert base is not None
        for index in range(2000):
            session_id = f"rotation-{index:02d}"
            db.add(
                Session(
                    session_id=session_id,
                    conversation_id=base.conversation_id,
                    user_email=base.user_email,
                    agent_id=base.agent_id,
                    intaris_session_id=f"intaris-{session_id}",
                    delegation_metadata={},
                    status="active",
                )
            )
            db.add(
                WorkSessionProjectionRow(
                    projection_id=f"projection-{index:02d}",
                    owner_email=base.user_email,
                    session_id=session_id,
                    source_session_id=f"intaris-{session_id}",
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    target_seq=0,
                    covered_through_seq=0,
                    state="caught_up",
                    materialized_at=datetime.now(UTC),
                )
            )
        await db.commit()
    store = AuthorityStore({})
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    for _ in range(5):
        assert await materializer._claim() == []
    assert store.authorities == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_pages_only_evidence_records(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    scope = TimelineScope(key="session:session-alice", kind="session", session_id="session-alice")
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        db.add(
            WorkSessionProjectionRow(
                projection_id="projection-evidence",
                owner_email=row.user_email,
                session_id=row.session_id,
                source_session_id="intaris-alice",
                materializer_version=WORK_MATERIALIZER_VERSION,
                target_seq=12,
                covered_through_seq=12,
                state="caught_up",
            )
        )
        stale = ToolCallTimelineItem(
            id="tool:stale-v1",
            call_id="stale-v1",
            tool_name="apply_patch",
            sort_key="9999",
            source_refs=[
                SourceRef(
                    store="intaris",
                    session_id="intaris-alice",
                    seq=99,
                    event_type="tool_call",
                )
            ],
            status="running",
        )
        db.add(
            WorkRecordRow(
                work_record_id="stale-v1-record",
                owner_email=row.user_email,
                session_id=row.session_id,
                materializer_version="work-v1",
                source_store="intaris",
                source_session_id="intaris-alice",
                source_seq=99,
                source_item_id=stale.id,
                item_ordinal=0,
                occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
                record_type=stale.kind,
                is_evidence=True,
                call_id=stale.call_id,
                timeline_item=stale.model_dump(mode="json"),
            )
        )
        obsolete = ToolCallTimelineItem(
            id="tool:obsolete-page-2",
            call_id="page-2",
            tool_name="bash",
            sort_key="0001",
            status="running",
        )
        db.add(
            WorkRecordRow(
                work_record_id="obsolete-page-2-record",
                owner_email=row.user_email,
                session_id=row.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                source_store="intaris",
                source_session_id="intaris-alice",
                source_seq=98,
                source_item_id=obsolete.id,
                item_ordinal=0,
                occurred_at=datetime(2025, 12, 31, tzinfo=UTC),
                record_type=obsolete.kind,
                is_evidence=False,
                call_id=obsolete.call_id,
                timeline_item=obsolete.model_dump(mode="json"),
                materialized_at=datetime(2025, 12, 31, tzinfo=UTC),
            )
        )
        for seq in range(12, 0, -1):
            evidence = seq <= 2
            item = ToolCallTimelineItem(
                id=f"tool:page-{seq}",
                call_id=f"page-{seq}",
                tool_name="write" if evidence else "read",
                sort_key=str(seq),
                source_refs=[
                    SourceRef(
                        store="intaris",
                        session_id="intaris-alice",
                        seq=seq,
                        event_type="tool_call",
                    )
                ],
                status="complete",
            )
            db.add(
                WorkRecordRow(
                    work_record_id=f"page-record-{seq}",
                    owner_email=row.user_email,
                    session_id=row.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id="intaris-alice",
                    source_seq=seq,
                    source_item_id=item.id,
                    item_ordinal=0,
                    occurred_at=datetime(2026, 1, seq, tzinfo=UTC),
                    record_type=item.kind,
                    is_evidence=evidence,
                    call_id=item.call_id,
                    timeline_item=item.model_dump(mode="json"),
                )
            )
        await db.commit()
        page = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
        )
        assert [item.call_id for item in page.items if isinstance(item, ToolCallTimelineItem)] == [
            "page-2"
        ]
        assert page.has_more_before is True
        assert page.before_cursor is not None
        assert "stale-v1" not in page.removed_call_ids
        assert "page-12" in page.removed_call_ids
        assert "page-2" not in page.removed_call_ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_category_pages_are_independent_and_files_are_complete(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    scope = TimelineScope(key="session:session-alice", kind="session", session_id="session-alice")
    definitions = {
        "bash": ToolDefinition(
            name="bash",
            description="Run a command",
            source=ToolSource(type="skill"),
            read_only=False,
            category="shell",
        ),
        "write": ToolDefinition(
            name="write",
            description="Write a file",
            source=ToolSource(type="skill"),
            read_only=False,
            category="filesystem",
        ),
    }
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        db.add(
            WorkSessionProjectionRow(
                projection_id="projection-categories",
                owner_email=row.user_email,
                session_id=row.session_id,
                source_session_id="intaris-alice",
                materializer_version=WORK_MATERIALIZER_VERSION,
                target_seq=1006,
                covered_through_seq=1006,
                state="caught_up",
            )
        )
        file_rows: list[WorkRecordFileRow] = []
        for seq in range(1, 1007):
            is_file = seq <= 4
            item = ToolCallTimelineItem(
                id=f"tool:category-{seq}",
                call_id=f"category-{seq}",
                tool_name="write" if is_file else "bash",
                sort_key=f"{seq:04d}",
                source_refs=[
                    SourceRef(
                        store="intaris",
                        session_id=row.intaris_session_id,
                        seq=seq,
                        event_type="tool_result",
                    )
                ],
                arguments=({"path": f"file-{seq}.py"} if is_file else {"command": f"printf {seq}"}),
                file_diffs=(
                    [FileDiffRef(path=f"file-{seq}.py", diff=f"+line-{seq}")] if is_file else []
                ),
                status="complete",
            )
            db.add(
                WorkRecordRow(
                    work_record_id=f"category-record-{seq}",
                    owner_email=row.user_email,
                    session_id=row.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id="intaris-alice",
                    source_seq=seq,
                    source_item_id=item.id,
                    item_ordinal=0,
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
                    record_type=item.kind,
                    is_evidence=True,
                    call_id=item.call_id,
                    timeline_item=item.model_dump(mode="json"),
                    **_record_metadata(item, definitions),
                )
            )
            if is_file:
                file_rows.append(
                    WorkRecordFileRow(
                        work_record_file_id=f"category-file-{seq}",
                        work_record_id=f"category-record-{seq}",
                        file_ordinal=0,
                        path=f"file-{seq}.py",
                        path_id=f"root:file-{seq}.py",
                        additions=1,
                        deletions=0,
                    )
                )
        await db.flush()
        db.add_all(file_rows)
        await db.commit()

        files = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )
        assert len(files.items) == 4
        assert files.has_more_before is False
        assert files.before_cursor is None
        assert files.summary is not None
        assert (files.summary.changed_files, files.summary.commands) == (4, 1002)

        commands = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="commands",
            tool_definitions=definitions,
        )
        assert [
            item.call_id for item in commands.items if isinstance(item, ToolCallTimelineItem)
        ] == ["category-1006"]
        assert commands.before_cursor is not None
        assert commands.summary == files.summary
        exact_commands = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="commands",
            exact_session_id=row.session_id,
            tool_definitions=definitions,
        )
        assert exact_commands.before_cursor is not None
        with pytest.raises(WorkCursorError):
            await read_work_page(
                db,
                owner_email=row.user_email,
                scope=scope,
                session_rows=[row],
                graph_fingerprint="graph",
                cursor_secret="secret",
                before=exact_commands.before_cursor,
                limit=1,
                category="commands",
                tool_definitions=definitions,
            )
        with pytest.raises(WorkCursorError, match="not found"):
            await read_work_page(
                db,
                owner_email=row.user_email,
                scope=scope,
                session_rows=[row],
                graph_fingerprint="graph",
                cursor_secret="secret",
                before=None,
                limit=1,
                category="commands",
                exact_session_id="session-unknown",
                tool_definitions=definitions,
            )

        overview_query_count = 0

        def count_overview_query(*_args: Any) -> None:
            nonlocal overview_query_count
            overview_query_count += 1

        row.delegation_metadata = {"reasoning_effort": "low", "model": "delegated-model"}
        overview_agent = await db.get(Agent, row.agent_id)
        assert overview_agent is not None
        overview_agent.display_name = "Alice Display"
        overview_agent.avatar_url = "https://example.test/alice.png"
        await db.commit()
        sa_event.listen(engine.sync_engine, "before_cursor_execute", count_overview_query)
        overview = await read_activity_overview(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            workstreams=[
                WorkstreamRef(
                    key=f"session:{row.session_id}",
                    kind="root",
                    root_key=f"session:{row.session_id}",
                    edge_kind="root",
                    ordinal=0,
                    conversation_id=row.conversation_id,
                    session_id=row.session_id,
                    event_store_session_id=row.intaris_session_id,
                    title="Alice session",
                    agent_id=row.agent_id,
                    status=row.status,
                )
            ],
            graph_fingerprint="graph",
            graph_truncated=False,
            tool_definitions=definitions,
        )
        sa_event.remove(engine.sync_engine, "before_cursor_execute", count_overview_query)
        assert overview.summary == files.summary
        assert overview.detail == "lightweight"
        assert overview.workstreams[0].summary == files.summary
        assert overview.workstreams[0].activity_state == "active"
        assert overview.workstreams[0].model == "delegated-model"
        assert overview.workstreams[0].reasoning_effort == "low"
        assert overview.workstreams[0].agent_display_name == "Alice Display"
        assert overview.workstreams[0].agent_avatar_url == "https://example.test/alice.png"
        assert len(overview.recent["commands"]) == 10
        assert overview.recent["commands"][0].id == "tool:category-1006"
        assert len(overview.recent_work.commands) == 10
        assert overview.recent_work.commands[0].command == "printf 1006"
        assert overview.recent_work.commands[0].source_workstream is not None
        assert overview.recent_work.commands[0].source_workstream.session_id == row.session_id
        assert overview.recent_work.commands[0].arguments == {}
        assert all(
            diff.diff == ""
            for mutation in overview.recent_work.files
            for diff in mutation.file_diffs
        )
        assert overview_query_count == 20
        db.add(
            DirectTurnRequestRow(
                request_id="dtr-overview-reasoning",
                turn_id="turn-overview-reasoning",
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                agent_id=row.agent_id,
                user_id=row.user_email,
                idempotency_scope="overview",
                idempotency_key="overview-reasoning",
                admission_hash="admission",
                payload_hash="payload",
                payload={
                    "schema_version": 1,
                    "content": "Continue",
                    "attachments": [],
                    "metadata": {"channel_default_agent_profile_id": "deep"},
                },
                status="running",
            )
        )
        many_rows = [row]
        many_workstreams = [overview.workstreams[0]]
        agent = overview_agent
        agent.default_agent_profile_id = "default-profile"
        agent.agent_profiles = {
            "deep": {"reasoning_effort": "medium", "model": "profile-model"},
            "default-profile": {"reasoning_effort": "low", "model": "default-model"},
        }
        for index in range(1, 200):
            extra = Session(
                session_id=f"session-overview-{index:03d}",
                activity_scope_id=row.activity_scope_id,
                conversation_id=row.conversation_id,
                user_email=row.user_email,
                agent_id=row.agent_id,
                intaris_session_id=f"intaris-overview-{index:03d}",
                delegation_metadata={},
                status="active",
                agent_profile_id="deep" if index == 1 else None,
            )
            db.add(extra)
            many_rows.append(extra)
            many_workstreams.append(
                WorkstreamRef(
                    key=f"session:{extra.session_id}",
                    kind="delegate",
                    parent_key=f"session:{row.session_id}",
                    root_key=f"session:{row.session_id}",
                    edge_kind="delegate",
                    ordinal=index,
                    conversation_id=extra.conversation_id,
                    session_id=extra.session_id,
                    event_store_session_id=extra.intaris_session_id,
                    title=f"Session {index}",
                    agent_id=extra.agent_id,
                    status=extra.status,
                )
            )
        await db.commit()
        many_query_count = 0

        def count_many_query(*_args: Any) -> None:
            nonlocal many_query_count
            many_query_count += 1

        sa_event.listen(engine.sync_engine, "before_cursor_execute", count_many_query)
        many_overview = await read_activity_overview(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=many_rows,
            workstreams=many_workstreams,
            graph_fingerprint="graph-many",
            graph_truncated=False,
            tool_definitions=definitions,
        )
        sa_event.remove(engine.sync_engine, "before_cursor_execute", count_many_query)
        assert len(many_overview.workstreams) == 200
        assert many_query_count == overview_query_count
        assert many_overview.workstreams[0].agent_profile_id == "deep"
        assert many_overview.workstreams[0].reasoning_effort == "low"
        assert many_overview.workstreams[0].model == "delegated-model"
        assert many_overview.workstreams[1].reasoning_effort == "medium"
        assert many_overview.workstreams[1].model == "profile-model"
        assert many_overview.workstreams[2].agent_profile_id == "default-profile"
        assert many_overview.workstreams[2].reasoning_effort == "low"
        assert many_overview.workstreams[2].model == "default-model"

        query_count = 0

        def count_query(*_args: Any) -> None:
            nonlocal query_count
            query_count += 1

        sa_event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        older_commands = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=commands.before_cursor,
            limit=1,
            category="commands",
            tool_definitions=definitions,
        )
        sa_event.remove(engine.sync_engine, "before_cursor_execute", count_query)
        assert [
            item.call_id for item in older_commands.items if isinstance(item, ToolCallTimelineItem)
        ] == ["category-1005"]
        assert older_commands.summary == commands.summary
        assert query_count <= 3

        midnight_slice = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=10,
            category="files",
            from_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=2),
            to_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=3),
            tool_definitions=definitions,
        )
        assert [
            item.call_id for item in midnight_slice.items if isinstance(item, ToolCallTimelineItem)
        ] == ["category-2"]

        with pytest.raises(WorkCursorError):
            await read_work_page(
                db,
                owner_email=row.user_email,
                scope=scope,
                session_rows=[row],
                graph_fingerprint="graph",
                cursor_secret="secret",
                before=commands.before_cursor,
                limit=1,
                category="mutations",
                tool_definitions=definitions,
            )
        with pytest.raises(WorkCursorError):
            await read_work_page(
                db,
                owner_email=row.user_email,
                scope=scope,
                session_rows=[row],
                graph_fingerprint="graph",
                cursor_secret="secret",
                before=commands.before_cursor,
                limit=1,
                category="commands",
                from_time=datetime(2026, 1, 2, tzinfo=UTC),
                tool_definitions=definitions,
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_loop_recovers_after_one_claim_failure(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    calls = 0
    repaired = asyncio.Event()

    async def claim() -> list[WorkSessionProjectionRow]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient claim failure")
        repaired.set()
        return []

    materializer._claim = claim  # type: ignore[method-assign]
    materializer.start()
    materializer._wake.set()
    async with asyncio.timeout(3):
        await repaired.wait()
    await materializer.stop()
    assert calls >= 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_partial_page_releases_repair_state_atomically_for_reclaim(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    events = [
        _event(
            "intaris-alice",
            seq,
            "tool_call",
            {"call_id": f"partial-{seq}", "name": "write", "arguments": {"path": "a.py"}},
        )
        for seq in (1, 2)
    ]

    class PartialStore(AuthorityStore):
        def bind(self, authority: EventStoreAuthority) -> Any:
            self.authorities.append(authority)
            store = self

            class Reader:
                async def read_session_events(
                    self, *, session_id: str, after_seq: int, limit: int, direction: str
                ) -> SessionEventPage:
                    del limit, direction
                    page = [event for event in store.pages[session_id] if event.seq > after_seq][:1]
                    return SessionEventPage(
                        store_id="intaris",
                        session_id=session_id,
                        events=page,
                        first_seq=page[0].seq,
                        last_seq=page[0].seq,
                        has_more_after=page[0].seq < 2,
                        verified_empty=False,
                    )

            return Reader()

    store = PartialStore({"intaris-alice": events})
    first = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        worker_id="page-one",
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await first._ensure_state(db, row, 2)
        state.state = "repair"
        await db.commit()
    claimed = await first._claim()
    assert len(claimed) == 1
    await first._repair(claimed[0].projection_id)
    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claimed[0].projection_id)
        assert state is not None
        assert (state.covered_through_seq, state.state, state.lease_owner) == (1, "repair", None)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.lease_owner.is_not(None),
                    WorkSessionProjectionRow.lease_expires_at >= datetime.now(UTC),
                )
            )
            == 0
        )
    second = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        worker_id="page-two",
    )
    reclaimed = await second._claim()
    assert len(reclaimed) == 1
    await second._repair(reclaimed[0].projection_id)
    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, reclaimed[0].projection_id)
        assert state is not None
        assert (state.covered_through_seq, state.state, state.lease_owner) == (
            2,
            "caught_up",
            None,
        )
    await engine.dispose()


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("running", "complete", "complete"),
        ("pending", "failed", "failed"),
        ("waiting", "cancelled", "cancelled"),
        ("running", "denied", "denied"),
        ("pending", "skipped", "skipped"),
        ("failed", "running", "failed"),
        ("complete", "pending", "complete"),
    ],
)
def test_tool_status_merge_terminal_precedence(
    previous: str,
    current: str,
    expected: str,
) -> None:
    assert _merged_tool_status(previous, current) == expected
