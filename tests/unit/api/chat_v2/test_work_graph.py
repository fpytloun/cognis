from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.sync import ChatV2SyncError
from cognis.api.chat_v2.work_graph import (
    WORK_GRAPH_MAX_NODES,
    AuthorizedWorkRootNotReadyError,
    _resolve_authorized_work_graph_sequential,
    resolve_authorized_work_graph,
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


class _Scalars:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _Db:
    def __init__(self, values: dict[type[Any], list[Any]]) -> None:
        self.values = values
        self.queries = 0

    async def scalars(self, statement: Any) -> _Scalars:
        self.queries += 1
        entity = statement.column_descriptions[0]["entity"]
        values = [
            value
            for value in self.values.get(entity, [])
            if all(_matches(value, criterion) for criterion in statement._where_criteria)
        ]
        values.sort(key=lambda value: getattr(value, statement._order_by_clauses[0].name))
        limit_clause = getattr(statement, "_limit_clause", None)
        limit = getattr(limit_clause, "value", None)
        return _Scalars(values[:limit] if isinstance(limit, int) else values)


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


def _matches(value: Any, expression: Any) -> bool:
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
        activity_scope_id="scope-current",
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
        ),  # type: ignore[arg-type]
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
    assert managed_child.parent_key == f"session:{root_current.session_id}"


@pytest.mark.asyncio
async def test_managed_child_task_uses_its_own_activity_scope_in_batched_and_sequential() -> None:
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

    expected_ids = {"s-root", "s-managed", "s-step-current"}
    assert {node.session_id for node in batched.nodes} == expected_ids
    assert {node.session_id for node in sequential.nodes} == expected_ids
    for graph in (batched, sequential):
        managed_node = next(node for node in graph.nodes if node.session_id == "s-managed")
        step_node = next(node for node in graph.nodes if node.session_id == "s-step-current")
        assert step_node.parent_key == managed_node.key
        assert step_node.edge_kind == "task_step"
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

    assert {node.session_id for node in graph.nodes} == {
        root.session_id,
        compact.session_id,
    }
    assert graph.truncated is False


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

    assert {node.session_id for node in graph.nodes} == {
        first.session_id,
        second.session_id,
        latest.session_id,
        descendant.session_id,
    }
    assert graph.truncated is False


@pytest.mark.asyncio
async def test_resolves_delegate_managed_task_retry_graph_and_deduplicates_cycles() -> None:
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
        "s-step",
        "s-retry",
    }
    assert len({node.event_store_session_id for node in graph.nodes}) == len(graph.nodes)
    assert "s-managed-duplicate" not in {node.session_id for node in graph.nodes}
    assert any(node.edge_kind == "delegate" for node in graph.nodes)
    assert any(node.edge_kind == "managed" for node in graph.nodes)
    assert any(node.step_run_id == "step-1" and node.superseded for node in graph.nodes)


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
    assert db.queries == 33
    assert sum(node.edge_kind == "managed" for node in graph.nodes) == 44
    assert sum(node.edge_kind == "rotation" for node in graph.nodes) == 44


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

    assert len(graph.nodes) == 179
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

    assert {node.session_id for node in conversation_graph.nodes} == {
        "s-oldest",
        "s-middle",
        "s-current",
    }
    current = next(node for node in conversation_graph.nodes if node.session_id == "s-current")
    middle = next(node for node in conversation_graph.nodes if node.session_id == "s-middle")
    assert current.root_key == "session:s-current"
    assert current.parent_key == "session:s-middle"
    assert middle.parent_key == "session:s-oldest"
    assert {node.session_id for node in session_graph.nodes} == {
        "s-oldest",
        "s-middle",
        "s-current",
    }
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
