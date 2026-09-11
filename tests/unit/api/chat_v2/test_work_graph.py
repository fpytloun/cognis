from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList
from sqlalchemy.sql.selectable import Exists

from cognis.api.chat_v2 import work_graph as work_graph_module
from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.sync import ChatV2SyncError
from cognis.api.chat_v2.work_graph import (
    WORK_GRAPH_MAX_NODES,
    AuthorizedWorkGraph,
    AuthorizedWorkGraphResolver,
    AuthorizedWorkRootNotReadyError,
    _latest_session_per_conversation_statement,
    _resolve_authorized_work_graph_sequential,
    canonical_workstream_refs,
    derive_work_execution_state,
    resolve_authorized_work_graph,
    same_work_activity_scope,
    strongest_work_execution_status,
)
from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Conversation,
    ManagedConversationLink,
    Session,
    StepRun,
    Task,
    User,
)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"session_status": "pending"}, "queued"),
        ({"session_status": "active"}, "idle"),
        ({"session_status": "active", "delegated_session": True}, "running"),
        ({"session_status": "idle"}, "idle"),
        ({"session_status": "active", "direct_turn_status": "running"}, "running"),
        ({"direct_turn_status": "running"}, "running"),
        ({"managed_turn_state": "waiting"}, "waiting"),
        (
            {
                "managed_turn_state": "waiting_controller",
                "managed_conversation_state": "open",
            },
            "waiting",
        ),
        (
            {
                "managed_turn_state": "interrupted",
                "managed_conversation_state": "open",
            },
            "failed",
        ),
        ({"recovering": True}, "recovering"),
        ({"step_status": "completed"}, "completed"),
        ({"task_status": "failed"}, "failed"),
        (
            {"direct_turn_status": "running", "session_status": "cancelled"},
            "cancelled",
        ),
    ],
)
def test_execution_state_precedence(values: dict[str, Any], expected: str) -> None:
    assert derive_work_execution_state(**values) == expected


def test_current_turn_state_precedes_previous_lifetime_settlement() -> None:
    assert (
        derive_work_execution_state(
            direct_turn_status="running",
            managed_turn_state="completed",
            task_status="completed",
            session_status="active",
        )
        == "running"
    )
    assert strongest_work_execution_status(["completed", "running", "queued"]) == "running"


def test_queued_successor_does_not_hide_running_turn() -> None:
    assert strongest_work_execution_status(["running", "queued"]) == "running"


def test_managed_waiting_precedes_previous_direct_completion() -> None:
    assert (
        derive_work_execution_state(
            direct_turn_status="completed",
            managed_turn_state="waiting_controller",
            managed_conversation_state="open",
            session_status="active",
        )
        == "waiting"
    )


class _Scalars:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _Db:
    def __init__(self, values: dict[type[Any], list[Any]]) -> None:
        self.values = values
        self.queries = 0
        self.statements: list[Any] = []

    async def scalars(self, statement: Any) -> _Scalars:
        self.queries += 1
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        values = [
            value
            for value in self.values.get(entity, [])
            if all(_matches(value, criterion) for criterion in statement._where_criteria)
        ]
        if statement._order_by_clauses:
            for clause in reversed(statement._order_by_clauses):
                expression = getattr(clause, "element", clause)
                values.sort(
                    key=lambda value: getattr(value, expression.name),
                    reverse=getattr(clause, "modifier", None) is operators.desc_op,
                )
        limit_clause = getattr(statement, "_limit_clause", None)
        limit = getattr(limit_clause, "value", None)
        return _Scalars(values[:limit] if isinstance(limit, int) else values)


class _SessionFactory:
    def __init__(self) -> None:
        self.created = 0
        self.closed = 0

    @asynccontextmanager
    async def __call__(self) -> Any:
        self.created += 1
        try:
            yield object()
        finally:
            self.closed += 1


def _empty_graph(fingerprint: str = "graph") -> AuthorizedWorkGraph:
    return AuthorizedWorkGraph(
        nodes=(),
        session_rows=(),
        fingerprint=fingerprint,
        truncated=False,
    )


@pytest.mark.asyncio
async def test_work_graph_resolver_shares_concurrent_identical_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    expected = _empty_graph()

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return expected

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )

    tasks = [
        asyncio.create_task(resolver.resolve(user_email="owner@example.com", scope=scope))
        for _ in range(20)
    ]
    await started.wait()
    await asyncio.sleep(0)

    assert calls == 1
    assert resolver.flight_count == 1

    release.set()
    results = await asyncio.gather(*tasks)

    assert all(result is expected for result in results)
    assert factory.created == 1
    assert factory.closed == 1
    assert resolver.flight_count == 0
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_isolates_users_scopes_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    all_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        nonlocal calls
        calls += 1
        if calls == 4:
            all_started.set()
        await release.wait()
        return _empty_graph(str(calls))

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    conversation_scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    session_scope = TimelineScope(
        key="session:session-1",
        kind="session",
        session_id="session-1",
    )

    tasks = [
        asyncio.create_task(
            resolver.resolve(
                user_email="owner@example.com",
                scope=conversation_scope,
            )
        ),
        asyncio.create_task(
            resolver.resolve(
                user_email="other@example.com",
                scope=conversation_scope,
            )
        ),
        asyncio.create_task(
            resolver.resolve(
                user_email="owner@example.com",
                scope=session_scope,
                max_nodes=10,
            )
        ),
        asyncio.create_task(
            resolver.resolve(
                user_email="owner@example.com",
                scope=conversation_scope,
                max_nodes=10,
            )
        ),
    ]
    await all_started.wait()

    assert calls == 4
    assert resolver.flight_count == 4

    release.set()
    await asyncio.gather(*tasks)

    assert factory.created == 4
    assert factory.closed == 4
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_waiter_cancellation_keeps_shared_read_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    started = asyncio.Event()
    release = asyncio.Event()
    expected = _empty_graph()

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        started.set()
        await release.wait()
        return expected

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    cancelled_waiter = asyncio.create_task(
        resolver.resolve(user_email="owner@example.com", scope=scope)
    )
    surviving_waiter = asyncio.create_task(
        resolver.resolve(user_email="owner@example.com", scope=scope)
    )
    await started.wait()
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    assert resolver.flight_count == 1
    assert not surviving_waiter.done()

    release.set()
    assert await surviving_waiter is expected
    assert factory.created == 1
    assert factory.closed == 1
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_last_waiter_cancels_and_closes_shared_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    started = asyncio.Event()

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    waiter = asyncio.create_task(resolver.resolve(user_email="owner@example.com", scope=scope))
    await started.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    async with asyncio.timeout(1):
        while factory.closed < 1:
            await asyncio.sleep(0)

    assert factory.created == 1
    assert factory.closed == 1
    assert resolver.flight_count == 0
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_failure_is_shared_and_next_call_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    expected = _empty_graph("retry")

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            raise ValueError("graph failed")
        return expected

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    tasks = [
        asyncio.create_task(resolver.resolve(user_email="owner@example.com", scope=scope))
        for _ in range(2)
    ]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert calls == 1
    assert all(isinstance(result, ValueError) for result in results)
    assert resolver.flight_count == 0

    assert await resolver.resolve(user_email="owner@example.com", scope=scope) is expected
    assert calls == 2
    assert factory.created == 2
    assert factory.closed == 2
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_does_not_cache_completed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    calls = 0

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        nonlocal calls
        calls += 1
        return _empty_graph(str(calls))

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory)  # type: ignore[arg-type]
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )

    first = await resolver.resolve(user_email="owner@example.com", scope=scope)
    second = await resolver.resolve(user_email="owner@example.com", scope=scope)

    assert first.fingerprint == "1"
    assert second.fingerprint == "2"
    assert calls == 2
    assert factory.created == 2
    assert factory.closed == 2
    await resolver.stop()


@pytest.mark.asyncio
async def test_work_graph_resolver_stop_cancels_active_read_and_rejects_new_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory()
    all_started = asyncio.Event()
    calls = 0

    async def resolve(*_args: Any, **_kwargs: Any) -> AuthorizedWorkGraph:
        nonlocal calls
        calls += 1
        if calls == 2:
            all_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(work_graph_module, "resolve_authorized_work_graph", resolve)
    resolver = AuthorizedWorkGraphResolver(factory, max_flights=1)  # type: ignore[arg-type]
    conversation_scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    session_scope = TimelineScope(
        key="session:session-1",
        kind="session",
        session_id="session-1",
    )
    shared_waiter = asyncio.create_task(
        resolver.resolve(user_email="owner@example.com", scope=conversation_scope)
    )
    bypass_waiter = asyncio.create_task(
        resolver.resolve(user_email="owner@example.com", scope=session_scope)
    )
    await all_started.wait()

    assert resolver.flight_count == 1
    assert resolver.bypass_count == 1

    await resolver.stop()

    results = await asyncio.gather(
        shared_waiter,
        bypass_waiter,
        return_exceptions=True,
    )
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert factory.created == 2
    assert factory.closed == 2
    assert resolver.flight_count == 0
    assert resolver.bypass_count == 0
    with pytest.raises(RuntimeError, match="stopped"):
        await resolver.resolve(
            user_email="owner@example.com",
            scope=conversation_scope,
        )


@pytest.mark.asyncio
async def test_missing_conversation_work_root_has_typed_preflight_error() -> None:
    with pytest.raises(
        AuthorizedWorkRootNotReadyError,
        match="Authorized Work conversation root was not found",
    ):
        await resolve_authorized_work_graph(
            _Db({}),  # type: ignore[arg-type]
            user_email="owner@example.com",
            scope=TimelineScope(
                key="conversation:new",
                kind="conversation",
                conversation_id="new",
            ),
        )


@pytest.mark.asyncio
async def test_conversation_without_active_id_uses_latest_authorized_session_and_rotations() -> (
    None
):
    older = _session(
        "s-older",
        "root",
        activity_scope_id="scope",
        status="completed",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    latest = _session(
        "s-latest",
        "root",
        activity_scope_id="scope",
        previous_session_id=older.session_id,
        status="completed",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        started_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root")],
                Session: [older, latest],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="conversation:root", kind="conversation", conversation_id="root"),
    )

    assert [node.session_id for node in graph.nodes] == [latest.session_id]
    assert graph.nodes[0].backing_session_ids == sorted([older.session_id, latest.session_id])
    assert graph.truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver",
    [resolve_authorized_work_graph, _resolve_authorized_work_graph_sequential],
)
async def test_conversation_fallback_orders_before_a_one_row_root_budget(
    resolver: Any,
) -> None:
    sessions = [
        _session(
            f"s-{index:04d}",
            "root",
            activity_scope_id=f"scope-{index:04d}",
            status="completed",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index in range(2048)
    ]
    newest = _session(
        "s-newest",
        "root",
        activity_scope_id="newest-scope",
        status="completed",
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    database = _Db(
        {
            Conversation: [_conversation("root")],
            Session: [*sessions, newest],
            ManagedConversationLink: [],
            Task: [],
            StepRun: [],
        }
    )

    graph = await resolver(
        database,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="conversation:root", kind="conversation", conversation_id="root"),
        max_nodes=1,
    )

    assert graph.nodes[0].session_id == newest.session_id
    root_queries = [
        statement
        for statement in database.statements
        if getattr(statement, "_limit_clause", None) is not None
        and statement._limit_clause.value == 1
        and statement._order_by_clauses
        and getattr(
            getattr(statement._order_by_clauses[0], "element", statement._order_by_clauses[0]),
            "name",
            None,
        )
        == "updated_at"
    ]
    assert len(root_queries) == 1


def test_grouped_conversation_fallback_is_database_side_and_deterministic() -> None:
    statement = _latest_session_per_conversation_statement(
        user_email="owner@example.com",
        conversation_ids=["conversation-a", "conversation-b"],
    )
    compiled = str(statement.compile(dialect=sqlite.dialect())).upper()

    assert "EXISTS" in compiled
    assert "UPDATED_AT DESC" in compiled
    assert "STARTED_AT DESC" in compiled
    assert "SESSION_ID DESC" in compiled


@pytest.mark.asyncio
async def test_valid_active_session_precedes_newer_terminal_session() -> None:
    active = _session(
        "s-active",
        "root",
        activity_scope_id="active-scope",
        status="active",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer_terminal = _session(
        "s-newer-terminal",
        "root",
        activity_scope_id="newer-scope",
        status="completed",
        updated_at=datetime(2026, 1, 3, tzinfo=UTC),
        started_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root", active_session_id=active.session_id)],
                Session: [active, newer_terminal],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="conversation:root", kind="conversation", conversation_id="root"),
    )

    assert [node.session_id for node in graph.nodes] == [active.session_id]


@pytest.mark.asyncio
async def test_sessionless_conversation_returns_stable_empty_graph() -> None:
    values = {
        Conversation: [_conversation("root")],
        Session: [],
        ManagedConversationLink: [],
        Task: [],
        StepRun: [],
    }
    scope = TimelineScope(key="conversation:root", kind="conversation", conversation_id="root")
    first = await resolve_authorized_work_graph(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )
    second = await resolve_authorized_work_graph(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )

    assert first.nodes == ()
    assert first.session_rows == ()
    assert first.truncated is False
    assert first.fingerprint == second.fingerprint


def _matches(value: Any, expression: Any) -> bool:
    unwrapped = expression
    while hasattr(unwrapped, "element"):
        if isinstance(unwrapped, Exists):
            return True
        unwrapped = unwrapped.element
    if isinstance(unwrapped, Exists):
        return True
    if isinstance(expression, BooleanClauseList):
        return all(_matches(value, clause) for clause in expression.clauses)
    left = getattr(value, expression.left.name)
    right = expression.right
    expected = right.value if isinstance(right, BindParameter) else right
    if expression.operator is operators.eq:
        return left == expected
    if expression.operator is operators.ne:
        return left != expected
    if expression.operator is operators.in_op:
        if hasattr(expected, "selected_columns"):
            return left is not None
        return left in expected
    if expression.operator is operators.is_:
        return left is expected
    if expression.operator is operators.is_not:
        return left is not expected
    raise AssertionError(f"Unsupported test predicate: {expression}")


def _conversation(identifier: str, **updates: Any) -> Any:
    values = dict(
        conversation_id=identifier,
        user_email="owner@example.com",
        agent_id="root-agent",
        agent_profile_id=None,
        title=identifier,
        status="active",
        active_session_id=None,
        context_data=None,
        lineage_kind=None,
        fork_source_conversation_id=None,
        fork_source_session_id=None,
        lineage_task_id=None,
        lineage_step_run_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    values.update(updates)
    return SimpleNamespace(**values)


def _session(identifier: str, conversation_id: str, **updates: Any) -> Any:
    values = dict(
        session_id=identifier,
        conversation_id=conversation_id,
        parent_session_id=None,
        previous_session_id=None,
        source_session_id=None,
        activity_scope_id=None,
        user_email="owner@example.com",
        agent_id="worker",
        agent_profile_id=None,
        delegation_task=None,
        status="active",
        completion_reason=None,
        intaris_session_id=f"store-{identifier}",
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    values.update(updates)
    return SimpleNamespace(**values)


def test_delegate_sharing_activity_scope_remains_a_separate_workstream() -> None:
    root = _session("s-root", "conversation", activity_scope_id="scope-current")
    root_successor = _session(
        "s-root-successor",
        "conversation",
        previous_session_id=root.session_id,
        activity_scope_id="scope-current",
    )
    delegate = _session(
        "s-delegate",
        "conversation",
        parent_session_id=root_successor.session_id,
        activity_scope_id="scope-current",
        delegation_mode="delegate",
        delegation_task="Inspect the regression",
    )
    delegate_successor = _session(
        "s-delegate-successor",
        "conversation",
        previous_session_id=delegate.session_id,
        activity_scope_id="scope-current",
        delegation_mode="delegate",
        delegation_task="Inspect the regression",
    )
    sibling_delegate = _session(
        "s-sibling-delegate",
        "conversation",
        parent_session_id=root_successor.session_id,
        activity_scope_id="scope-current",
        delegation_mode="delegate",
        delegation_task="Review the regression",
    )
    nodes = canonical_workstream_refs(
        [root, root_successor, delegate, delegate_successor, sibling_delegate],
        session_parent={
            root.session_id: (None, "root"),
            root_successor.session_id: (root.session_id, "rotation"),
            delegate.session_id: (root_successor.session_id, "delegate"),
            delegate_successor.session_id: (delegate.session_id, "rotation"),
            sibling_delegate.session_id: (root_successor.session_id, "delegate"),
        },
        root_session_id=root_successor.session_id,
        steps=[],
        tasks={},
        links=[],
        conversations={
            "conversation": _conversation(
                "conversation",
                active_session_id=root_successor.session_id,
            )
        },
    )

    assert len(nodes) == 3
    root_node = next(node for node in nodes if node.kind == "root")
    delegate_node = next(node for node in nodes if node.session_id == delegate_successor.session_id)
    sibling_node = next(node for node in nodes if node.session_id == sibling_delegate.session_id)
    assert root_node.key == root.activity_scope_id
    assert root_node.backing_session_ids == [root.session_id, root_successor.session_id]
    assert delegate_node.backing_session_ids == [delegate.session_id, delegate_successor.session_id]
    assert delegate_node.parent_key == root_node.key
    assert sibling_node.backing_session_ids == [sibling_delegate.session_id]
    assert sibling_node.parent_key == root_node.key


def test_delegate_inside_managed_conversation_keeps_parent_and_title() -> None:
    root = _session("s-root", "root", activity_scope_id="scope-root")
    managed = _session("s-managed", "managed", activity_scope_id="scope-managed")
    managed_successor = _session(
        "s-managed-successor",
        "managed",
        previous_session_id=managed.session_id,
        activity_scope_id="scope-managed",
    )
    review = _session(
        "s-review",
        "managed",
        parent_session_id=managed_successor.session_id,
        activity_scope_id="scope-managed",
        delegation_mode="delegate",
        delegation_task="Review the uncommitted diff",
        agent_id="system:code-review",
    )
    review_successor = _session(
        "s-review-successor",
        "managed",
        previous_session_id=review.session_id,
        activity_scope_id="scope-managed",
        delegation_mode="delegate",
        delegation_task="Review the uncommitted diff",
        agent_id="system:code-review",
    )
    link = SimpleNamespace(
        link_id="link-managed",
        controller_session_id=root.session_id,
        target_conversation_id="managed",
        title="Root-cause deterministic canonical repair failure",
        turn_state="running",
        conversation_state="open",
    )

    nodes = canonical_workstream_refs(
        [root, managed, managed_successor, review, review_successor],
        session_parent={
            root.session_id: (None, "root"),
            managed.session_id: (root.session_id, "managed"),
            managed_successor.session_id: (managed.session_id, "rotation"),
            review.session_id: (managed_successor.session_id, "delegate"),
            review_successor.session_id: (review.session_id, "rotation"),
        },
        root_session_id=root.session_id,
        steps=[],
        tasks={},
        links=[link],
        conversations={
            "root": _conversation("root", active_session_id=root.session_id),
            "managed": _conversation(
                "managed",
                active_session_id=managed_successor.session_id,
            ),
        },
    )

    assert len(nodes) == 3
    root_node = next(node for node in nodes if node.kind == "root")
    managed_node = next(node for node in nodes if node.kind == "managed")
    review_node = next(node for node in nodes if node.agent_id == "system:code-review")
    assert managed_node.parent_key == root_node.key
    assert managed_node.link_id == link.link_id
    assert managed_node.title == link.title
    assert managed_node.backing_session_ids == [
        managed.session_id,
        managed_successor.session_id,
    ]
    assert review_node.kind == "delegate"
    assert review_node.parent_key == managed_node.key
    assert review_node.link_id is None
    assert review_node.title == review.delegation_task
    assert review_node.backing_session_ids == [
        review.session_id,
        review_successor.session_id,
    ]


@pytest.mark.asyncio
async def test_leaf_conversation_seen_only_frontier_is_not_truncated() -> None:
    root = _session("s-root", "root")
    values = {
        Conversation: [_conversation("root", active_session_id=root.session_id)],
        Session: [root],
        ManagedConversationLink: [],
        Task: [],
        StepRun: [],
    }
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )

    batched = await resolve_authorized_work_graph(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )
    sequential = await _resolve_authorized_work_graph_sequential(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )

    assert len(batched.nodes) == len(sequential.nodes) == 1
    assert batched.truncated is sequential.truncated is False


@pytest.mark.asyncio
async def test_unseen_frontier_at_node_bound_remains_truncated() -> None:
    root = _session("s-root", "root")
    child = _session("s-child", "root", parent_session_id=root.session_id)
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root", active_session_id=root.session_id)],
                Session: [root, child],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
        max_nodes=1,
    )

    assert len(graph.nodes) == 1
    assert graph.truncated is True


@pytest.mark.asyncio
async def test_managed_conversations_contribute_only_their_current_activity_scope() -> None:
    root_current = _session("s-root-current", "root", activity_scope_id="root-current")
    root_old = _session("s-root-old", "root", activity_scope_id="root-old")
    source = _session("s-source", "source", activity_scope_id="source")
    child_current = _session(
        "s-child-current",
        "child",
        source_session_id=source.session_id,
        activity_scope_id="child-current",
    )
    child_old = _session("s-child-old", "child", activity_scope_id="child-old")
    links = [
        SimpleNamespace(
            link_id="link-current",
            user_email="owner@example.com",
            controller_conversation_id="root",
            controller_session_id=root_current.session_id,
            parent_link_id=None,
            root_link_id=None,
            target_conversation_id="child",
            target_agent_id="worker",
            target_agent_profile_id=None,
            title="Current child",
            conversation_state="open",
        ),
        SimpleNamespace(
            link_id="link-reset",
            user_email="owner@example.com",
            controller_conversation_id="root",
            controller_session_id=root_old.session_id,
            parent_link_id=None,
            root_link_id=None,
            target_conversation_id="reset-child",
            target_agent_id="worker",
            target_agent_profile_id=None,
            title="Old child",
            conversation_state="open",
        ),
    ]
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [
                    _conversation("root", active_session_id=root_current.session_id),
                    _conversation("child", active_session_id=child_current.session_id),
                    _conversation("reset-child", active_session_id="s-reset-child"),
                ],
                Session: [
                    root_current,
                    root_old,
                    source,
                    child_current,
                    child_old,
                    _session("s-reset-child", "reset-child", activity_scope_id="reset"),
                ],
                ManagedConversationLink: links,
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
    )

    assert {node.session_id for node in graph.nodes} == {
        root_current.session_id,
        child_current.session_id,
    }
    assert graph.truncated is False
    parentless = [node for node in graph.nodes if node.parent_key is None]
    assert len(parentless) == 1
    assert parentless[0].key == parentless[0].root_key
    managed_child = next(
        node for node in graph.nodes if node.session_id == child_current.session_id
    )
    assert managed_child.kind == "managed"
    assert managed_child.parent_key == root_current.activity_scope_id


@pytest.mark.asyncio
async def test_managed_rotation_preserves_active_old_generation_descendants() -> None:
    root = _session("s-root", "root", activity_scope_id="scope-root")
    managed_old = _session(
        "s-managed-old",
        "managed",
        activity_scope_id="scope-old",
        status="completed",
    )
    managed_current = _session(
        "s-managed-current",
        "managed",
        previous_session_id=managed_old.session_id,
        activity_scope_id="scope-current",
    )
    active_delegate = _session(
        "s-active-delegate",
        "managed",
        parent_session_id=managed_old.session_id,
        activity_scope_id="scope-old",
        delegation_mode="delegate",
        delegation_task="Review active",
    )
    active_grandchild = _session(
        "s-active-grandchild",
        "managed",
        parent_session_id=active_delegate.session_id,
        activity_scope_id="scope-old",
        delegation_mode="delegate",
        delegation_task="Review nested",
    )
    active_depth_four = _session(
        "s-active-depth-four",
        "managed",
        parent_session_id=active_grandchild.session_id,
        activity_scope_id="scope-old",
        delegation_mode="delegate",
        delegation_task="Review deeply nested",
    )
    terminal_delegate = _session(
        "s-terminal-delegate",
        "managed",
        parent_session_id=managed_old.session_id,
        activity_scope_id="scope-old",
        delegation_mode="delegate",
        delegation_task="Completed review",
        status="completed",
    )
    unrelated_active = [
        _session(
            f"a-unrelated-active-{index:02d}",
            "managed",
            parent_session_id=f"a-unrelated-parent-{index:02d}",
            activity_scope_id="scope-other",
            delegation_mode="delegate",
            delegation_task="Unrelated",
        )
        for index in range(12)
    ]
    link = SimpleNamespace(
        link_id="link-managed",
        user_email="owner@example.com",
        controller_conversation_id="root",
        controller_session_id=root.session_id,
        parent_link_id=None,
        root_link_id=None,
        target_conversation_id="managed",
        target_agent_id="worker",
        target_agent_profile_id=None,
        title="Managed",
        conversation_state="open",
    )
    values = {
        Conversation: [
            _conversation("root", active_session_id=root.session_id),
            _conversation("managed", active_session_id=managed_current.session_id),
        ],
        Session: [
            root,
            managed_old,
            managed_current,
            active_delegate,
            active_grandchild,
            active_depth_four,
            terminal_delegate,
            *unrelated_active,
        ],
        ManagedConversationLink: [link],
        Task: [],
        StepRun: [],
    }
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )

    for resolver in (resolve_authorized_work_graph, _resolve_authorized_work_graph_sequential):
        graph = await resolver(
            _Db(values),  # type: ignore[arg-type]
            user_email="owner@example.com",
            scope=scope,
            max_nodes=8,
        )
        by_session = {node.session_id: node for node in graph.nodes}
        assert set(by_session) == {
            root.session_id,
            managed_current.session_id,
            active_delegate.session_id,
            active_grandchild.session_id,
            active_depth_four.session_id,
        }
        managed_node = by_session[managed_current.session_id]
        delegate_node = by_session[active_delegate.session_id]
        grandchild_node = by_session[active_grandchild.session_id]
        depth_four_node = by_session[active_depth_four.session_id]
        assert delegate_node.parent_key == managed_node.key
        assert grandchild_node.parent_key == delegate_node.key
        assert depth_four_node.parent_key == grandchild_node.key
        assert terminal_delegate.session_id not in by_session
        assert not {row.session_id for row in unrelated_active} & set(by_session)
        assert graph.truncated is False


@pytest.mark.asyncio
async def test_managed_conversation_active_pointer_must_match_its_own_session() -> None:
    root = _session("s-root", "root", activity_scope_id="scope-root")
    managed_a = _session("s-managed-a", "managed-a", activity_scope_id="scope-a")
    managed_b = _session("s-managed-b", "managed-b", activity_scope_id="scope-b")
    links = [
        SimpleNamespace(
            link_id="link-a",
            user_email="owner@example.com",
            controller_conversation_id="root",
            controller_session_id=root.session_id,
            parent_link_id=None,
            root_link_id=None,
            target_conversation_id="managed-a",
            target_agent_id="worker",
            target_agent_profile_id=None,
            title="Managed A",
            conversation_state="open",
            turn_state=None,
        ),
        SimpleNamespace(
            link_id="link-b",
            user_email="owner@example.com",
            controller_conversation_id="root",
            controller_session_id=root.session_id,
            parent_link_id=None,
            root_link_id=None,
            target_conversation_id="managed-b",
            target_agent_id="worker",
            target_agent_profile_id=None,
            title="Managed B",
            conversation_state="open",
            turn_state=None,
        ),
    ]
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [
                    _conversation("root", active_session_id=root.session_id),
                    # A points at B's session and must use its own fallback.
                    _conversation("managed-a", active_session_id=managed_b.session_id),
                    _conversation("managed-b", active_session_id=managed_b.session_id),
                ],
                Session: [root, managed_a, managed_b],
                ManagedConversationLink: links,
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="conversation:root", kind="conversation", conversation_id="root"),
    )

    assert {node.session_id for node in graph.nodes} == {
        root.session_id,
        managed_a.session_id,
        managed_b.session_id,
    }
    assert {
        node.conversation_id: node.session_id
        for node in graph.nodes
        if node.conversation_id in {"managed-a", "managed-b"}
    } == {
        "managed-a": managed_a.session_id,
        "managed-b": managed_b.session_id,
    }


@pytest.mark.asyncio
async def test_conversation_graph_excludes_tasks_in_batched_and_sequential() -> None:
    root = _session("s-root", "root", activity_scope_id="scope-root")
    managed = _session("s-managed", "managed", activity_scope_id="scope-managed")
    managed_old = _session("s-managed-old", "managed", activity_scope_id="scope-old")
    current_step_session = _session(
        "s-step-current",
        "managed",
        parent_session_id=managed.session_id,
        activity_scope_id="scope-managed",
    )
    stale_step_session = _session(
        "s-step-stale",
        "managed",
        parent_session_id=managed_old.session_id,
        activity_scope_id="scope-old",
    )
    link = SimpleNamespace(
        link_id="link-managed",
        user_email="owner@example.com",
        controller_conversation_id="root",
        controller_session_id=root.session_id,
        parent_link_id=None,
        root_link_id=None,
        target_conversation_id="managed",
        target_agent_id="worker",
        target_agent_profile_id=None,
        title="Managed",
        conversation_state="open",
    )

    def task(identifier: str, source_session_id: str) -> Any:
        return SimpleNamespace(
            task_id=identifier,
            created_by="owner@example.com",
            title=identifier,
            status="running",
            agent_id="worker",
            agent_profile_id=None,
            source_ref="managed",
            source_session_id=source_session_id,
            control_conversation_id="managed",
            attempt_number=1,
        )

    current_task = task("task-current", managed.session_id)
    stale_task = task("task-stale", managed_old.session_id)

    def step(identifier: str, task_id: str, session_id: str) -> Any:
        return SimpleNamespace(
            step_run_id=identifier,
            task_id=task_id,
            step_name=identifier,
            status="running",
            attempt=1,
            attempt_number=1,
            superseded_by_step_run_id=None,
            agent_id="worker",
            agent_profile_id=None,
            conversation_id="managed",
            session_id=session_id,
        )

    current_step = step(
        "step-current",
        current_task.task_id,
        current_step_session.session_id,
    )
    stale_step = step(
        "step-stale",
        stale_task.task_id,
        stale_step_session.session_id,
    )
    values = {
        Conversation: [
            _conversation("root", active_session_id=root.session_id),
            _conversation("managed", active_session_id=managed.session_id),
        ],
        Session: [
            root,
            managed,
            managed_old,
            current_step_session,
            stale_step_session,
        ],
        ManagedConversationLink: [link],
        Task: [current_task, stale_task],
        StepRun: [current_step, stale_step],
    }
    scope = TimelineScope(
        key="conversation:root",
        kind="conversation",
        conversation_id="root",
    )
    batched_db = _Db(values)
    sequential_db = _Db(values)

    batched = await resolve_authorized_work_graph(
        batched_db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )
    sequential = await _resolve_authorized_work_graph_sequential(
        sequential_db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )

    expected_ids = {"s-root", "s-managed"}
    assert {node.session_id for node in batched.nodes} == expected_ids
    assert {node.session_id for node in sequential.nodes} == expected_ids
    for graph in (batched, sequential):
        managed_node = next(node for node in graph.nodes if node.session_id == "s-managed")
        assert managed_node.backing_session_ids == ["s-managed", "s-step-current"]
        assert managed_node.edge_kind == "managed"
        assert "s-step-stale" not in {node.session_id for node in graph.nodes}
    assert batched_db.queries <= 40
    assert batched.truncated is sequential.truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["session", "task_step"])
async def test_nonconversation_roots_enforce_current_activity_scope(
    scope_kind: str,
) -> None:
    root = _session("s-root", "root", activity_scope_id="scope-current")
    compact = _session(
        "s-compact",
        "root",
        previous_session_id=root.session_id,
        activity_scope_id="scope-current",
    )
    old_child = _session(
        "s-old-child",
        "root",
        parent_session_id=root.session_id,
        activity_scope_id="scope-old",
    )
    task = SimpleNamespace(
        task_id="task-root",
        created_by="owner@example.com",
        title="Task",
        status="running",
        agent_id="worker",
        agent_profile_id=None,
        source_ref="root",
        source_session_id=root.session_id,
        control_conversation_id="root",
        attempt_number=1,
    )
    step = SimpleNamespace(
        step_run_id="step-root",
        task_id=task.task_id,
        step_name="inspect",
        status="running",
        attempt=1,
        attempt_number=1,
        superseded_by_step_run_id=None,
        agent_id="worker",
        agent_profile_id=None,
        conversation_id="root",
        session_id=root.session_id,
    )
    scope = (
        TimelineScope(key="session:s-root", kind="session", session_id=root.session_id)
        if scope_kind == "session"
        else TimelineScope(
            key="task_step:step-root",
            kind="task_step",
            task_id=task.task_id,
            step_run_id=step.step_run_id,
        )
    )

    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root", active_session_id=compact.session_id)],
                Session: [root, compact, old_child],
                ManagedConversationLink: [],
                Task: [task],
                StepRun: [step],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=scope,
    )

    assert {node.session_id for node in graph.nodes} == {compact.session_id}
    assert graph.nodes[0].backing_session_ids == ["s-compact", "s-root"]
    assert graph.truncated is False


@pytest.mark.asyncio
async def test_null_activity_scope_does_not_absorb_unrelated_conversation_sessions() -> None:
    root = _session("s-root", conversation_id="c-main", activity_scope_id=None)
    unrelated = _session(
        "s-unrelated",
        conversation_id="c-main",
        activity_scope_id=None,
    )
    conversation = _conversation("c-main", active_session_id="s-root")
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Session: [root, unrelated],
                Conversation: [conversation],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:c-main",
            kind="conversation",
            conversation_id="c-main",
        ),
    )
    assert [node.session_id for node in graph.nodes] == ["s-root"]


@pytest.mark.asyncio
async def test_null_scope_root_rejects_later_nonnull_rotation_like_recovery() -> None:
    root = _session("s-root", conversation_id="c-main", activity_scope_id=None)
    later = _session(
        "s-later",
        conversation_id="c-main",
        previous_session_id=root.session_id,
        activity_scope_id="later-scope",
    )
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Session: [root, later],
                Conversation: [_conversation("c-main", active_session_id=root.session_id)],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),
        user_email="owner@example.com",
        scope=TimelineScope(
            key=f"session:{root.session_id}",
            kind="session",
            session_id=root.session_id,
        ),
    )
    assert same_work_activity_scope(root.activity_scope_id, later.activity_scope_id) is False
    assert [node.session_id for node in graph.nodes] == [root.session_id]


@pytest.mark.asyncio
async def test_latest_session_root_includes_same_scope_predecessors_and_descendants_only() -> None:
    cross_scope = _session("s0", "root", activity_scope_id="scope-old")
    first = _session(
        "s1",
        "root",
        previous_session_id=cross_scope.session_id,
        activity_scope_id="scope-current",
    )
    second = _session(
        "s2",
        "root",
        previous_session_id=first.session_id,
        activity_scope_id="scope-current",
    )
    latest = _session(
        "s3",
        "root",
        previous_session_id=second.session_id,
        parent_session_id="controller",
        activity_scope_id="scope-current",
    )
    descendant = _session(
        "child",
        "root",
        parent_session_id=first.session_id,
        activity_scope_id="scope-current",
    )
    controller = _session("controller", "controller", activity_scope_id="controller")
    sibling = _session(
        "sibling",
        "controller",
        parent_session_id=controller.session_id,
        activity_scope_id="controller",
    )

    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [
                    _conversation("root", active_session_id=latest.session_id),
                    _conversation("controller", active_session_id=controller.session_id),
                ],
                Session: [
                    cross_scope,
                    first,
                    second,
                    latest,
                    descendant,
                    controller,
                    sibling,
                ],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key=f"session:{latest.session_id}",
            kind="session",
            session_id=latest.session_id,
        ),
    )

    assert {node.session_id for node in graph.nodes} == {latest.session_id}
    assert graph.nodes[0].backing_session_ids == ["child", "s1", "s2", "s3"]
    assert graph.truncated is False


@pytest.mark.asyncio
async def test_conversation_graph_resolves_delegate_and_managed_work_but_excludes_tasks() -> None:
    conversations = [
        _conversation("root", active_session_id="s-root"),
        _conversation("managed", active_session_id="s-managed"),
        _conversation("task-control", active_session_id="s-step"),
    ]
    sessions = [
        _session("s-root", "root"),
        _session("s-rotated", "root", previous_session_id="s-root"),
        _session("s-child", "root", parent_session_id="s-rotated"),
        _session("s-cycle", "root", parent_session_id="s-child", previous_session_id="s-cycle"),
        _session("s-managed", "managed"),
        _session("s-managed-duplicate", "managed", intaris_session_id="store-s-managed"),
        _session("s-step", "task-control"),
        _session("s-retry", "task-control", previous_session_id="s-step"),
    ]
    link = SimpleNamespace(
        link_id="link-1",
        user_email="owner@example.com",
        controller_conversation_id="root",
        controller_session_id="s-root",
        parent_link_id=None,
        root_link_id=None,
        target_conversation_id="managed",
        target_agent_id="managed-agent",
        target_agent_profile_id=None,
        title="Managed implementation",
        conversation_state="open",
    )
    task = SimpleNamespace(
        task_id="task-1",
        created_by="owner@example.com",
        title="Task",
        status="running",
        agent_id="worker",
        agent_profile_id=None,
        source_ref="root",
        source_session_id="s-root",
        control_conversation_id="task-control",
        attempt_number=1,
    )
    steps = [
        SimpleNamespace(
            step_run_id="step-1",
            task_id="task-1",
            step_name="implement",
            status="failed",
            attempt=1,
            attempt_number=1,
            superseded_by_step_run_id="step-2",
            agent_id="worker",
            agent_profile_id=None,
            conversation_id="task-control",
            session_id="s-step",
        ),
        SimpleNamespace(
            step_run_id="step-2",
            task_id="task-1",
            step_name="implement",
            status="running",
            attempt=2,
            attempt_number=2,
            superseded_by_step_run_id=None,
            agent_id="worker",
            agent_profile_id=None,
            conversation_id="task-control",
            session_id="s-retry",
        ),
    ]
    db = _Db(
        {
            Conversation: conversations,
            Session: sessions,
            ManagedConversationLink: [link],
            Task: [task],
            StepRun: steps,
        }
    )
    graph = await resolve_authorized_work_graph(
        db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
    )

    assert {node.session_id for node in graph.nodes} == {
        "s-root",
        "s-rotated",
        "s-child",
        "s-cycle",
        "s-managed",
    }
    assert len({node.event_store_session_id for node in graph.nodes}) == len(graph.nodes)
    assert "s-managed-duplicate" not in {node.session_id for node in graph.nodes}
    assert any(node.edge_kind == "delegate" for node in graph.nodes)
    assert any(node.edge_kind == "managed" for node in graph.nodes)
    assert not any(node.step_run_id for node in graph.nodes)


@pytest.mark.asyncio
async def test_200_stream_graph_resolves_with_bounded_query_count() -> None:
    child_count = 199
    root = _session("s-root", "root")
    children = [
        _session(f"s-child-{index:03d}", "root", parent_session_id=root.session_id)
        for index in range(child_count)
    ]
    db = _Db(
        {
            Conversation: [_conversation("root", active_session_id=root.session_id)],
            Session: [root, *children],
            ManagedConversationLink: [],
            Task: [],
            StepRun: [],
        }
    )

    graph = await resolve_authorized_work_graph(
        db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
    )

    assert graph.truncated is False
    assert len(graph.nodes) == child_count + 1
    assert {node.session_id for node in graph.nodes} == {
        root.session_id,
        *(child.session_id for child in children),
    }
    assert db.queries == 18


@pytest.mark.asyncio
async def test_graph_stage_timeout_is_explicit_and_bounded() -> None:
    class _SlowDb(_Db):
        async def scalars(self, statement: Any) -> _Scalars:
            await asyncio.Event().wait()
            return await super().scalars(statement)

    started = asyncio.get_running_loop().time()
    with pytest.raises(ChatV2SyncError) as raised:
        await resolve_authorized_work_graph(
            _SlowDb({}),  # type: ignore[arg-type]
            user_email="owner@example.com",
            scope=TimelineScope(
                key="conversation:root",
                kind="conversation",
                conversation_id="root",
            ),
            deadline=asyncio.get_running_loop().time() + 0.02,
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert raised.value.code == "work_graph_timeout"
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_179_stream_mixed_topology_resolves_with_bounded_queries() -> None:
    root = _session("s-root", "root")
    conversations = [_conversation("root", active_session_id=root.session_id)]
    sessions = [root]
    links = []
    for index in range(89):
        sessions.append(
            _session(f"s-delegate-{index:03d}", "root", parent_session_id=root.session_id)
        )
    for index in range(44):
        conversation_id = f"managed-{index:03d}"
        rotation_session_id = f"s-managed-{index:03d}-rotation"
        active_session_id = f"s-managed-{index:03d}"
        conversations.append(_conversation(conversation_id, active_session_id=active_session_id))
        sessions.extend(
            [
                _session(
                    rotation_session_id,
                    conversation_id,
                    previous_session_id=active_session_id,
                ),
                _session(active_session_id, conversation_id),
            ]
        )
        links.append(
            SimpleNamespace(
                link_id=f"link-{index:03d}",
                user_email="owner@example.com",
                controller_conversation_id="root",
                controller_session_id="s-root",
                parent_link_id=None,
                root_link_id=None,
                target_conversation_id=conversation_id,
                target_agent_id="worker",
                target_agent_profile_id=None,
                title=f"Managed {index}",
                conversation_state="open",
            )
        )
    sessions.append(_session("s-final", "root", source_session_id="s-delegate-000"))
    db = _Db(
        {
            Conversation: conversations,
            Session: sessions,
            ManagedConversationLink: links,
            Task: [],
            StepRun: [],
        }
    )

    graph = await resolve_authorized_work_graph(
        db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
    )

    assert len(graph.nodes) == 178
    assert graph.truncated is False
    assert db.queries == 28
    assert sum(node.edge_kind == "managed" for node in graph.nodes) == 88
    assert sum(node.edge_kind == "rotation" for node in graph.nodes) == 0


@pytest.mark.asyncio
async def test_rejects_forged_or_missing_descendants_and_bounds_cycles() -> None:
    root = _conversation(
        "root",
        active_session_id="s-root",
        context_data={"unrelated_session_id": "foreign-session"},
    )
    sessions = [
        _session("s-root", "root"),
        _session("foreign-session", "foreign-conversation"),
    ]
    sessions.extend(
        _session(f"s-{index}", "root", parent_session_id="s-root") for index in range(20)
    )
    forged = SimpleNamespace(
        link_id="forged",
        user_email="owner@example.com",
        controller_conversation_id="root",
        controller_session_id="s-root",
        parent_link_id=None,
        root_link_id=None,
        target_conversation_id="other-user-conversation",
        target_agent_id="other",
        target_agent_profile_id=None,
        title="forged",
        conversation_state="open",
    )
    db = _Db(
        {
            Conversation: [root],
            Session: sessions,
            ManagedConversationLink: [forged],
            Task: [],
            StepRun: [],
        }
    )
    graph = await resolve_authorized_work_graph(
        db,  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
        max_nodes=8,
    )
    assert graph.truncated is True
    assert all(node.conversation_id == "root" for node in graph.nodes)
    assert "foreign-session" not in {node.session_id for node in graph.nodes}
    assert len(graph.nodes) <= 8


@pytest.mark.asyncio
async def test_exact_node_cap_reports_pending_descendants_as_truncated() -> None:
    root = _session("s-root", "root")
    children = [
        _session(f"s-child-{index}", "root", parent_session_id="s-root") for index in range(3)
    ]
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root", active_session_id="s-root")],
                Session: [root, *children],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="session:s-root", kind="session", session_id="s-root"),
        max_nodes=3,
    )

    assert len(graph.nodes) == 3
    assert graph.truncated is True


@pytest.mark.asyncio
async def test_exact_exhaustive_node_cap_is_not_truncated() -> None:
    root = _session("s-root", "root")
    children = [
        _session(f"s-child-{index}", "root", parent_session_id="s-root") for index in range(2)
    ]
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [_conversation("root", active_session_id="s-root")],
                Session: [root, *children],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="session:s-root", kind="session", session_id="s-root"),
        max_nodes=3,
    )

    assert len(graph.nodes) == 3
    assert graph.truncated is False


@pytest.mark.asyncio
async def test_root_is_reserved_before_a_maximum_managed_link_frontier() -> None:
    conversations = [_conversation("root", active_session_id="s-root")]
    sessions = [_session("s-root", "root")]
    links: list[Any] = []
    for index in range(600):
        conversation_id = f"managed-{index}"
        session_id = f"s-managed-{index}"
        conversations.append(_conversation(conversation_id, active_session_id=session_id))
        sessions.append(_session(session_id, conversation_id))
        links.append(
            SimpleNamespace(
                link_id=f"link-{index}",
                user_email="owner@example.com",
                controller_conversation_id="root",
                controller_session_id="s-root",
                parent_link_id=None,
                root_link_id=None,
                target_conversation_id=conversation_id,
                target_agent_id="worker",
                target_agent_profile_id=None,
                title=f"Managed {index}",
                conversation_state="open",
            )
        )
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: conversations,
                Session: sessions,
                ManagedConversationLink: links,
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
    )
    assert graph.truncated is True
    assert 1 <= len(graph.nodes) <= WORK_GRAPH_MAX_NODES
    assert "s-root" in {node.session_id for node in graph.nodes}


@pytest.mark.asyncio
async def test_session_root_does_not_walk_back_to_parent_or_sibling() -> None:
    graph = await resolve_authorized_work_graph(
        _Db(
            {
                Conversation: [
                    _conversation("parent", active_session_id="s-parent"),
                    _conversation("child", active_session_id="s-child"),
                ],
                Session: [
                    _session("s-child", "child", parent_session_id="s-parent"),
                    _session("s-grandchild", "child", parent_session_id="s-child"),
                    _session("s-parent", "parent"),
                    _session("s-sibling", "parent", parent_session_id="s-parent"),
                ],
                ManagedConversationLink: [],
                Task: [],
                StepRun: [],
            }
        ),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(
            key="session:s-child",
            kind="session",
            session_id="s-child",
        ),
    )
    assert {node.session_id for node in graph.nodes} == {"s-child", "s-grandchild"}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["session", "task_step"])
async def test_deep_delegate_root_excludes_same_scope_ancestors_and_siblings(
    scope_kind: str,
) -> None:
    controller = _session("s-controller", "conversation", activity_scope_id="scope")
    parent = _session(
        "s-parent",
        "conversation",
        parent_session_id=controller.session_id,
        activity_scope_id="scope",
        delegation_mode="delegate",
    )
    requested_predecessor = _session(
        "s-requested-predecessor",
        "conversation",
        parent_session_id=parent.session_id,
        activity_scope_id="scope",
        delegation_mode="delegate",
    )
    managed = _session("s-managed", "managed", activity_scope_id="managed-scope")
    link = SimpleNamespace(
        link_id="link-managed",
        user_email="owner@example.com",
        controller_conversation_id="conversation",
        controller_session_id=requested_predecessor.session_id,
        parent_link_id=None,
        root_link_id=None,
        target_conversation_id="managed",
        target_agent_id="worker",
        target_agent_profile_id=None,
        title="Managed descendant",
        conversation_state="open",
    )
    requested = _session(
        "s-requested",
        "conversation",
        previous_session_id=requested_predecessor.session_id,
        activity_scope_id="scope",
    )
    descendant = _session(
        "s-descendant",
        "conversation",
        parent_session_id=requested.session_id,
        activity_scope_id="scope",
        delegation_mode="delegate",
    )
    sibling = _session(
        "s-sibling",
        "conversation",
        parent_session_id=parent.session_id,
        activity_scope_id="scope",
        delegation_mode="delegate",
    )
    task = SimpleNamespace(
        task_id="task-deep",
        created_by="owner@example.com",
        title="Deep task",
        status="running",
        agent_id="worker",
        agent_profile_id=None,
        source_ref="conversation",
        source_session_id=requested.session_id,
        control_conversation_id=None,
        attempt_number=1,
    )
    step = SimpleNamespace(
        step_run_id="step-deep",
        task_id=task.task_id,
        step_name="inspect",
        status="running",
        attempt=1,
        attempt_number=1,
        superseded_by_step_run_id=None,
        agent_id="worker",
        agent_profile_id=None,
        conversation_id="conversation",
        session_id=requested.session_id,
    )
    values = {
        Conversation: [
            _conversation("conversation", active_session_id=controller.session_id),
            _conversation("managed", active_session_id=managed.session_id),
        ],
        Session: [
            controller,
            parent,
            requested_predecessor,
            requested,
            descendant,
            sibling,
            managed,
        ],
        ManagedConversationLink: [link],
        Task: [task],
        StepRun: [step],
    }
    scope = (
        TimelineScope(
            key=f"session:{requested.session_id}",
            kind="session",
            session_id=requested.session_id,
        )
        if scope_kind == "session"
        else TimelineScope(
            key=f"task_step:{step.step_run_id}",
            kind="task_step",
            task_id=task.task_id,
            step_run_id=step.step_run_id,
        )
    )

    for resolver in (resolve_authorized_work_graph, _resolve_authorized_work_graph_sequential):
        graph = await resolver(
            _Db(values),  # type: ignore[arg-type]
            user_email="owner@example.com",
            scope=scope,
        )

        assert {node.session_id for node in graph.nodes} == {
            requested.session_id,
            descendant.session_id,
            managed.session_id,
        }
        assert sum(node.parent_key is None for node in graph.nodes) == 1
        assert graph.nodes[0].session_id == requested.session_id
        assert graph.nodes[0].backing_session_ids == [
            requested.session_id,
            requested_predecessor.session_id,
        ]


@pytest.mark.asyncio
async def test_indexed_frontier_ignores_513_irrelevant_siblings(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-graph.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        db.add(User(email="owner@example.com", name="Owner", password_hash="x", role="user"))
        await db.flush()
        db.add(
            Agent(
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
                description="Agent",
            )
        )
        await db.flush()
        db.add_all(
            [
                Conversation(
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    active_session_id="s-root",
                ),
                Conversation(
                    conversation_id="descendant",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    active_session_id="s-descendant",
                    lineage_kind="conversation",
                    fork_source_session_id="s-root",
                ),
                Conversation(
                    conversation_id="irrelevant",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    active_session_id="s-irrelevant",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Session(
                    session_id="s-root",
                    activity_scope_id="scope-current",
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-root",
                    delegation_metadata={},
                ),
                Session(
                    session_id="s-descendant",
                    conversation_id="descendant",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-descendant",
                    source_session_id="s-root",
                    delegation_metadata={},
                ),
                Session(
                    session_id="s-irrelevant",
                    conversation_id="irrelevant",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-irrelevant",
                    delegation_metadata={},
                ),
                *[
                    Session(
                        session_id=f"s-sibling-{index:03d}",
                        conversation_id="irrelevant",
                        user_email="owner@example.com",
                        agent_id="agent-1",
                        intaris_session_id=f"stream-sibling-{index:03d}",
                        parent_session_id="s-irrelevant",
                        delegation_metadata={},
                    )
                    for index in range(513)
                ],
            ]
        )
        await db.commit()

    async with factory() as db:
        graph = await resolve_authorized_work_graph(
            db,
            user_email="owner@example.com",
            scope=TimelineScope(
                key="session:s-root",
                kind="session",
                session_id="s-root",
            ),
        )
    assert {node.session_id for node in graph.nodes} == {"s-root"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_sqlite_179_stream_graph_uses_bounded_indexed_queries(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-graph-179.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        db.add(User(email="owner@example.com", name="Owner", password_hash="x", role="user"))
        await db.flush()
        db.add(
            Agent(
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
                description="Agent",
            )
        )
        await db.flush()
        db.add(
            Conversation(
                conversation_id="root",
                user_email="owner@example.com",
                agent_id="agent-1",
                context_type="web",
                active_session_id="s-root",
            )
        )
        await db.flush()
        db.add_all(
            [
                Session(
                    session_id="s-root",
                    activity_scope_id="scope-current",
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-root",
                    delegation_metadata={},
                ),
                *[
                    Session(
                        session_id=f"s-child-{index:03d}",
                        activity_scope_id="scope-current",
                        conversation_id="root",
                        user_email="owner@example.com",
                        agent_id="agent-1",
                        intaris_session_id=f"stream-child-{index:03d}",
                        parent_session_id="s-root",
                        delegation_metadata={},
                    )
                    for index in range(178)
                ],
            ]
        )
        await db.commit()

    query_count = 0

    def count_query(*_args: Any) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_query)
    try:
        async with factory() as db:
            graph = await resolve_authorized_work_graph(
                db,
                user_email="owner@example.com",
                scope=TimelineScope(
                    key="conversation:root",
                    kind="conversation",
                    conversation_id="root",
                ),
                deadline=asyncio.get_running_loop().time() + 2.0,
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_query)

    assert len(graph.nodes) == 1
    assert graph.nodes[0].backing_session_count == 179
    assert graph.truncated is False
    assert query_count <= 24
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_root_loads_real_database_rotation_ancestry(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-rotations.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with factory() as db:
        db.add(User(email="owner@example.com", name="Owner", password_hash="x", role="user"))
        await db.flush()
        db.add(
            Agent(
                agent_id="agent-1",
                owner_email="owner@example.com",
                name="Agent",
                description="Agent",
            )
        )
        await db.flush()
        db.add(
            Conversation(
                conversation_id="root",
                user_email="owner@example.com",
                agent_id="agent-1",
                context_type="web",
                active_session_id="s-current",
            )
        )
        await db.flush()
        db.add_all(
            [
                Session(
                    session_id="s-oldest",
                    activity_scope_id="scope-current",
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-oldest",
                    delegation_metadata={},
                ),
                Session(
                    session_id="s-middle",
                    activity_scope_id="scope-current",
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-middle",
                    previous_session_id="s-oldest",
                    delegation_metadata={},
                ),
                Session(
                    session_id="s-current",
                    activity_scope_id="scope-current",
                    conversation_id="root",
                    user_email="owner@example.com",
                    agent_id="agent-1",
                    intaris_session_id="stream-current",
                    previous_session_id="s-middle",
                    delegation_metadata={},
                ),
            ]
        )
        await db.commit()

    async with factory() as db:
        conversation_graph = await resolve_authorized_work_graph(
            db,
            user_email="owner@example.com",
            scope=TimelineScope(
                key="conversation:root",
                kind="conversation",
                conversation_id="root",
            ),
        )
        session_graph = await resolve_authorized_work_graph(
            db,
            user_email="owner@example.com",
            scope=TimelineScope(
                key="session:s-current",
                kind="session",
                session_id="s-current",
            ),
        )

    assert {node.session_id for node in conversation_graph.nodes} == {"s-current"}
    assert {node.session_id for node in session_graph.nodes} == {"s-current"}
    for graph in (conversation_graph, session_graph):
        current = graph.nodes[0]
        assert current.root_key == "scope-current"
        assert current.parent_key is None
        assert current.backing_session_ids == ["s-current", "s-middle", "s-oldest"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_topology_reparenting_changes_graph_fingerprint() -> None:
    root = _session("s-root", "root")
    parent = _session("s-parent", "root", parent_session_id="s-root")
    child = _session("s-child", "root", parent_session_id="s-root")
    values = {
        Conversation: [_conversation("root", active_session_id="s-root")],
        Session: [root, parent, child],
        ManagedConversationLink: [],
        Task: [],
        StepRun: [],
    }
    first = await resolve_authorized_work_graph(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="session:s-root", kind="session", session_id="s-root"),
    )
    child.parent_session_id = "s-parent"
    second = await resolve_authorized_work_graph(
        _Db(values),  # type: ignore[arg-type]
        user_email="owner@example.com",
        scope=TimelineScope(key="session:s-root", kind="session", session_id="s-root"),
    )

    assert first.fingerprint != second.fingerprint
    assert next(node for node in second.nodes if node.session_id == "s-child").parent_key == (
        "session:s-parent"
    )
