from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy import event as sa_event
from sqlalchemy.dialects import postgresql, sqlite

from cognis.api.app import _create_work_projection_callback
from cognis.api.chat_v2.append_listener import EventAppendListenerFastPath
from cognis.api.chat_v2.cached_event_store import AppendInvalidation
from cognis.api.chat_v2.event_store import (
    RawSessionEvent,
    SessionEventPage,
    SessionHistoryAvailability,
    SessionWatermark,
)
from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    FileDiffRef,
    SourceRef,
    TimelineScope,
    TodoProgress,
    ToolCallTimelineItem,
    WorkMaterialization,
    WorkstreamRef,
    WorkSummary,
)
from cognis.api.chat_v2.work_file_projector import rebuild_session_current_files
from cognis.api.chat_v2.work_materializer import (
    WORK_APPEND_PENDING,
    WORK_APPEND_PENDING_BYTES,
    WORK_LIVE_APPEND_PRIORITY,
    WORK_MATERIALIZER_VERSION,
    WORK_MAX_REPAIR_CONCURRENCY,
    WORK_RECORD_MAX_BYTES,
    WORK_VISIBLE_REFRESH_PRIORITY,
    WorkMaterializer,
    _advance_projection_target,
    _bounded_item,
    _decode_persisted_work_item,
    _file_path_id,
    _merged_tool_status,
    _old_file_path_id,
    _PendingWorkAppend,
    _published_artifact_item,
    _record_metadata,
    lock_work_projection_state,
)
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.api.chat_v2.work_repository import (
    WORK_FILES_SOURCE_RECORD_BATCH_SIZE,
    WorkCursorError,
    _activity_state,
    _aggregate_cached_logical_summaries,
    _aggregate_logical_summaries,
    _category_summary,
    _collapse_activity_workstreams,
    _empty_summary,
    _is_direct_delegate_ongoing,
    _latest_file_projection,
    _load_file_source_records,
    _logical_summary_statements,
    _logical_todo_progress,
    _LogicalProjection,
    _materialization,
    _overview_revision,
    _projection_summaries,
    _removed_call_ids,
    _session_runtime_metadata,
    _unsign,
    read_activity_overview,
    read_work_page,
)
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.models.agent import AgentLLMConfig
from cognis.models.artifact import ArtifactKind, AttachmentRef
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
    ArtifactRecordRow,
    Conversation,
    ConversationTodo,
    DirectTurnRequestRow,
    Session,
    SessionTodo,
    Task,
    User,
    WorkCurrentFileRow,
    WorkRecordFileRow,
    WorkRecordRow,
    WorkSessionProjectionRow,
)
from cognis.store.work_live_invalidation import register_live_work_waker


def test_materializer_version_rebuilds_filtered_work_evidence() -> None:
    assert WORK_MATERIALIZER_VERSION == "work-v8"


def test_successful_artifact_publish_becomes_first_class_work_artifact() -> None:
    source_ref = SourceRef(
        store="intaris",
        session_id="source",
        seq=7,
        event_type="tool_result",
    )
    item = ToolCallTimelineItem(
        id="tool:publish",
        call_id="publish",
        tool_name="artifact_publish",
        display_name="Publish backup",
        sort_key="7",
        source_refs=[source_ref],
        created_at="2026-08-22T13:47:20Z",
        updated_at="2026-08-22T13:47:22Z",
        status="complete",
        attachments=[
            AttachmentRef(
                artifact_id="att_backup",
                kind=ArtifactKind.FILE,
                mime_type="application/json",
                filename="backup.json",
                size_bytes=11776,
            )
        ],
    )

    projected = _published_artifact_item(item)

    assert projected == ArtifactTimelineItem(
        id="artifact:att_backup",
        artifact_id="att_backup",
        filename="backup.json",
        mime_type="application/json",
        size_bytes=11776,
        title="Publish backup",
        sort_key="7",
        source_refs=[source_ref],
        created_at="2026-08-22T13:47:20Z",
        updated_at="2026-08-22T13:47:22Z",
    )
    assert _record_metadata(projected, {}) == {
        "category": "artifacts",
        "entity_id": "att_backup",
        "file_path_ids": [],
        "additions": 0,
        "deletions": 0,
    }


@pytest.mark.parametrize(
    "update",
    [
        {"status": "running"},
        {"status": "failed", "is_error": True},
        {"attachments": []},
        {
            "attachments": [
                AttachmentRef(
                    artifact_id="att_one",
                    kind=ArtifactKind.FILE,
                    mime_type="application/json",
                    filename="one.json",
                    size_bytes=1,
                ),
                AttachmentRef(
                    artifact_id="att_two",
                    kind=ArtifactKind.FILE,
                    mime_type="application/json",
                    filename="two.json",
                    size_bytes=2,
                ),
            ]
        },
    ],
)
def test_artifact_publish_requires_one_successful_attachment(update: dict[str, Any]) -> None:
    item = ToolCallTimelineItem(
        id="tool:publish",
        call_id="publish",
        tool_name="artifact_publish",
        sort_key="7",
        status="complete",
        attachments=[
            AttachmentRef(
                artifact_id="att_backup",
                kind=ArtifactKind.FILE,
                mime_type="application/json",
                filename="backup.json",
                size_bytes=11776,
            )
        ],
    ).model_copy(update=update)

    assert _published_artifact_item(item) is item


@pytest.mark.parametrize("activity_scope_id", [None, "legacy-scope"])
def test_persisted_work_decoder_accepts_only_retired_scope_field(
    activity_scope_id: str | None,
) -> None:
    item = ToolCallTimelineItem(
        id="tool:legacy",
        call_id="legacy",
        tool_name="bash",
        sort_key="1",
    )
    payload = item.model_dump(mode="json") | {"activity_scope_id": activity_scope_id}

    assert _decode_persisted_work_item(payload) == item
    assert payload["activity_scope_id"] == activity_scope_id
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _decode_persisted_work_item(payload | {"unexpected": True})


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


@pytest.mark.parametrize(
    "execution_state",
    ["idle", "queued", "running", "waiting", "recovering", "completed", "failed", "cancelled"],
)
def test_logical_projection_preserves_singleton_execution_state(execution_state: str) -> None:
    node = _overview_node("single", current=True).model_copy(
        update={"execution_state": execution_state}
    )
    row = SimpleNamespace(
        session_id="single",
        previous_session_id=None,
        conversation_id="conversation-root",
        activity_scope_id="scope-a",
    )

    projected = _collapse_activity_workstreams(
        [node],
        session_rows=[row],
        physical_root_key=node.key,
    )

    assert projected.workstreams[0].execution_state == execution_state


@pytest.mark.parametrize("execution_state", ["idle", "recovering"])
def test_logical_projection_preserves_current_owner_over_historical_outcome(
    execution_state: str,
) -> None:
    previous = _overview_node("previous", activity_state="closed").model_copy(
        update={"execution_state": "completed"}
    )
    current = _overview_node(
        "current",
        parent_key=previous.key,
        current=True,
    ).model_copy(update={"execution_state": execution_state})
    rows = [
        SimpleNamespace(
            session_id="previous",
            previous_session_id=None,
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
        SimpleNamespace(
            session_id="current",
            previous_session_id="previous",
            conversation_id="conversation-root",
            activity_scope_id="scope-a",
        ),
    ]

    projected = _collapse_activity_workstreams(
        [previous, current],
        session_rows=rows,
        physical_root_key=current.key,
    )

    assert projected.workstreams[0].session_id == "current"
    assert projected.workstreams[0].execution_state == execution_state


def test_logical_projection_preserves_each_component_structural_identity() -> None:
    root = _overview_node("root", current=True, kind="root")
    delegate_old = _overview_node(
        "delegate-old",
        parent_key=root.key,
        activity_state="closed",
        kind="delegate",
    )
    delegate_successor = _overview_node(
        "delegate-successor",
        parent_key=delegate_old.key,
        current=True,
        activity_state="ongoing",
        kind="rotation",
    )
    managed_old = _overview_node(
        "managed-old",
        parent_key=root.key,
        activity_state="closed",
        kind="managed",
    )
    managed_successor = _overview_node(
        "managed-successor",
        parent_key=managed_old.key,
        current=True,
        kind="rotation",
    )
    rows = [
        SimpleNamespace(
            session_id="root",
            previous_session_id=None,
            conversation_id="conversation-root",
            activity_scope_id="scope-root",
        ),
        SimpleNamespace(
            session_id="delegate-old",
            previous_session_id=None,
            conversation_id="conversation-delegate",
            activity_scope_id="scope-delegate",
        ),
        SimpleNamespace(
            session_id="delegate-successor",
            previous_session_id="delegate-old",
            conversation_id="conversation-delegate",
            activity_scope_id="scope-delegate",
        ),
        SimpleNamespace(
            session_id="managed-old",
            previous_session_id=None,
            conversation_id="conversation-managed",
            activity_scope_id="scope-managed",
        ),
        SimpleNamespace(
            session_id="managed-successor",
            previous_session_id="managed-old",
            conversation_id="conversation-managed",
            activity_scope_id="scope-managed",
        ),
    ]

    projected = _collapse_activity_workstreams(
        [root, delegate_old, delegate_successor, managed_old, managed_successor],
        session_rows=rows,
        physical_root_key=root.key,
    )

    assert len(projected.workstreams) == 3
    projected_by_session = {node.session_id: node for node in projected.workstreams}
    logical_root = projected_by_session["root"]
    delegate = projected_by_session["delegate-successor"]
    managed = projected_by_session["managed-successor"]
    assert (logical_root.kind, logical_root.edge_kind) == ("root", "root")
    assert logical_root.parent_key is None
    assert logical_root.root_key == logical_root.key
    assert (delegate.kind, delegate.edge_kind) == ("delegate", "delegate")
    assert delegate.parent_key == logical_root.key
    assert delegate.root_key == logical_root.key
    assert delegate.activity_state == "ongoing"
    assert delegate.backing_session_ids == ["delegate-old", "delegate-successor"]
    assert (managed.kind, managed.edge_kind) == ("managed", "managed")
    assert managed.parent_key == logical_root.key
    assert managed.root_key == logical_root.key
    assert managed.backing_session_ids == ["managed-old", "managed-successor"]


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
        assert "DELETED_AT IS NULL" in compiled[1].upper()


def test_lightweight_summaries_use_projection_counters_and_dedupe_current_paths() -> None:
    states = [
        SimpleNamespace(
            session_id="s-old",
            mutation_count=2,
            command_count=3,
            artifact_count=4,
            deliverable_count=5,
            additions=6,
            deletions=7,
            omitted_file_count=8,
        ),
        SimpleNamespace(
            session_id="s-new",
            mutation_count=11,
            command_count=12,
            artifact_count=13,
            deliverable_count=14,
            additions=15,
            deletions=16,
            omitted_file_count=17,
        ),
    ]
    path_ids_by_session = {
        "s-old": {"root:shared.py", "root:old.py"},
        "s-new": {"root:shared.py", "root:new.py"},
    }
    logical = _LogicalProjection(
        workstreams=[],
        physical_to_logical={"session:s-old": "logical", "session:s-new": "logical"},
        members_by_logical={"logical": ("s-old", "s-new")},
        ambiguous=False,
    )
    summaries = _projection_summaries(
        states,  # type: ignore[arg-type]
        session_ids=["s-old", "s-new"],
        path_ids_by_session=path_ids_by_session,
    )
    aggregated = _aggregate_cached_logical_summaries(
        summaries,
        logical,
        path_ids_by_session=path_ids_by_session,
    )

    assert summaries["s-new"].commands == 12
    assert aggregated["logical"].changed_files == 3
    assert aggregated["logical"].mutations == 13
    assert aggregated["logical"].additions == 21


def test_lightweight_logical_entity_counts_match_detailed_rotation_semantics() -> None:
    logical = _LogicalProjection(
        workstreams=[],
        physical_to_logical={
            "session:s-old": "logical",
            "session:s-new": "logical",
        },
        members_by_logical={"logical": ("s-old", "s-new")},
        ambiguous=False,
    )
    physical_summaries = {
        "s-old": WorkSummary(
            mutations=1,
            commands=1,
            changed_files=0,
            artifacts=2,
            deliverables=2,
        ),
        "s-new": WorkSummary(
            mutations=1,
            commands=1,
            changed_files=0,
            artifacts=3,
            deliverables=1,
        ),
    }
    entity_counts = [
        ("logical", "artifacts", 2),
        ("logical", "deliverables", 1),
    ]

    lightweight = _aggregate_cached_logical_summaries(
        physical_summaries,
        logical,
        path_ids_by_session={"s-old": {"shared.py"}, "s-new": {"shared.py"}},
        entity_counts=entity_counts,
    )
    detailed = _aggregate_logical_summaries(
        physical_summaries=physical_summaries,
        logical=logical,
        file_counts=[("logical", 1)],
        entity_counts=[("logical", "artifacts", 2), ("logical", "deliverables", 1)],
    )

    assert lightweight["logical"] == detailed["logical"]
    assert lightweight["logical"].artifacts == 2
    assert lightweight["logical"].deliverables == 1


def test_latest_file_projection_compiles_for_sqlite_and_postgresql() -> None:
    statement = select(WorkRecordRow).where(
        WorkRecordRow.owner_email == "owner@example.com",
        WorkRecordRow.session_id.in_(["root", "child"]),
        WorkRecordRow.category == "files",
    )

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        compiled = str(select(_latest_file_projection(statement)).compile(dialect=dialect)).upper()
        assert "ROW_NUMBER() OVER" in compiled
        assert "FIRST_VALUE" in compiled
        assert "SUM(" in compiled
        assert "PATH_RANK =" in compiled


@pytest.mark.asyncio
async def test_file_source_record_hydration_batches_large_id_sets() -> None:
    class BatchDatabase:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        async def scalars(self, statement: Any) -> Any:
            batch = next(
                value for value in statement.compile().params.values() if isinstance(value, list)
            )
            self.batch_sizes.append(len(batch))
            return SimpleNamespace(
                all=lambda: [SimpleNamespace(work_record_id=record_id) for record_id in batch]
            )

    database = BatchDatabase()
    record_ids = [
        f"record-{index}" for index in range(WORK_FILES_SOURCE_RECORD_BATCH_SIZE * 2 + 37)
    ]

    records = await _load_file_source_records(  # type: ignore[arg-type]
        database,
        record_ids=record_ids,
    )

    assert list(records) == record_ids
    assert database.batch_sizes == [
        WORK_FILES_SOURCE_RECORD_BATCH_SIZE,
        WORK_FILES_SOURCE_RECORD_BATCH_SIZE,
        37,
    ]


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "terminated"])
def test_activity_state_terminal_beats_stale_delegate_runtime(terminal_status: str) -> None:
    assert (
        _activity_state(
            session_status="active",
            managed_conversation_state=None,
            ongoing=True,
        )
        == "ongoing"
    )
    assert (
        _activity_state(
            session_status=terminal_status,
            managed_conversation_state=None,
            ongoing=True,
        )
        == "closed"
    )
    assert (
        _activity_state(
            session_status="active",
            managed_conversation_state="failed",
            ongoing=True,
        )
        == "closed"
    )


def test_direct_delegate_without_step_run_is_ongoing_until_terminal() -> None:
    node = _overview_node("delegate", kind="delegate")
    active = SimpleNamespace(
        status="active",
        parent_session_id="controller",
        previous_session_id=None,
        delegation_mode="delegate",
        delegation_task="Read-only code trace",
    )
    completed = SimpleNamespace(
        status="completed",
        parent_session_id="controller",
        previous_session_id=None,
        delegation_mode="delegate",
        delegation_task="Read-only code trace",
    )

    assert _is_direct_delegate_ongoing(row=active, node=node) is True
    assert (
        _activity_state(
            session_status=active.status,
            managed_conversation_state=None,
            ongoing=_is_direct_delegate_ongoing(row=active, node=node),
        )
        == "ongoing"
    )
    assert _is_direct_delegate_ongoing(row=completed, node=node) is False
    assert (
        _activity_state(
            session_status=completed.status,
            managed_conversation_state=None,
            ongoing=True,
        )
        == "closed"
    )


def test_active_main_rotation_and_managed_nodes_are_not_direct_delegate_ongoing() -> None:
    row = SimpleNamespace(
        status="active",
        parent_session_id="controller",
        previous_session_id=None,
        delegation_mode="delegate",
        delegation_task="Read-only code trace",
    )
    for kind in ("root", "rotation", "managed"):
        assert (
            _is_direct_delegate_ongoing(
                row=row,
                node=_overview_node(kind, kind=kind),
            )
            is False
        )
    assert (
        _is_direct_delegate_ongoing(
            row=SimpleNamespace(
                status="active",
                parent_session_id="controller",
                previous_session_id="previous",
                delegation_mode=None,
                delegation_task=None,
            ),
            node=_overview_node("ordinary-rotation", kind="rotation"),
        )
        is False
    )


@pytest.mark.parametrize("successor_status", ["active", "terminated"])
def test_compacted_direct_delegate_collapses_with_successor_lifecycle(
    successor_status: str,
) -> None:
    old_row = SimpleNamespace(
        session_id="delegate-old",
        status="completed",
        parent_session_id="controller",
        previous_session_id=None,
        conversation_id="conversation-root",
        activity_scope_id="delegate-scope",
        delegation_mode="delegate",
        delegation_task="Read-only code trace",
    )
    successor_row = SimpleNamespace(
        session_id="delegate-successor",
        status=successor_status,
        parent_session_id="controller",
        previous_session_id=old_row.session_id,
        conversation_id="conversation-root",
        activity_scope_id="delegate-scope",
        delegation_mode="delegate",
        delegation_task="Read-only code trace",
    )
    old_node = _overview_node(
        old_row.session_id,
        parent_key="session:controller",
        kind="delegate",
        activity_state="closed",
    )
    successor_node = _overview_node(
        successor_row.session_id,
        parent_key=old_node.key,
        kind="rotation",
        current=True,
        activity_state=_activity_state(
            session_status=successor_row.status,
            managed_conversation_state=None,
            ongoing=_is_direct_delegate_ongoing(
                row=successor_row,
                node=_overview_node(successor_row.session_id, kind="rotation"),
            ),
        ),
    )

    projection = _collapse_activity_workstreams(
        [old_node, successor_node],
        session_rows=[old_row, successor_row],
        physical_root_key="session:controller",
    )

    assert len(projection.workstreams) == 1
    logical = projection.workstreams[0]
    assert logical.kind == "delegate"
    assert logical.backing_session_ids == ["delegate-old", "delegate-successor"]
    assert logical.activity_state == ("ongoing" if successor_status == "active" else "closed")


@pytest.mark.asyncio
async def test_logical_todo_progress_isolated_deduped_and_cancelled_excluded(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    async with factory() as db:
        root = await db.get(Session, "session-alice")
        assert root is not None
        compact = Session(
            session_id="session-compact",
            activity_scope_id=root.activity_scope_id,
            conversation_id=root.conversation_id,
            previous_session_id=root.session_id,
            user_email=root.user_email,
            agent_id=root.agent_id,
            intaris_session_id="intaris-compact",
            delegation_metadata={},
            status="active",
        )
        managed_conversation = Conversation(
            conversation_id="conversation-managed",
            user_email=root.user_email,
            agent_id=root.agent_id,
            title="Managed",
            context_type="web",
            status="active",
        )
        managed = Session(
            session_id="session-managed",
            activity_scope_id="scope-managed",
            conversation_id=managed_conversation.conversation_id,
            user_email=root.user_email,
            agent_id=root.agent_id,
            intaris_session_id="intaris-managed",
            delegation_metadata={},
            status="active",
        )
        db.add_all([compact, managed_conversation, managed])
        db.add_all(
            [
                ConversationTodo(
                    conversation_id=root.conversation_id,
                    position=0,
                    content="done",
                    status="completed",
                ),
                ConversationTodo(
                    conversation_id=root.conversation_id,
                    position=1,
                    content="working",
                    status="in_progress",
                ),
                ConversationTodo(
                    conversation_id=root.conversation_id,
                    position=2,
                    content="cancelled",
                    status="cancelled",
                ),
                SessionTodo(
                    session_id=root.session_id,
                    position=0,
                    content="mirrored done",
                    status="completed",
                ),
                ConversationTodo(
                    conversation_id=managed.conversation_id,
                    position=0,
                    content="managed pending",
                    status="pending",
                ),
            ]
        )
        await db.commit()
        logical = _LogicalProjection(
            workstreams=[],
            physical_to_logical={
                f"session:{root.session_id}": f"session:{compact.session_id}",
                f"session:{compact.session_id}": f"session:{compact.session_id}",
                f"session:{managed.session_id}": f"session:{managed.session_id}",
            },
            members_by_logical={
                f"session:{compact.session_id}": (root.session_id, compact.session_id),
                f"session:{managed.session_id}": (managed.session_id,),
            },
            ambiguous=False,
        )

        progress = await _logical_todo_progress(
            db,
            owner_email=root.user_email,
            session_rows=[root, compact, managed],
            logical=logical,
        )

        assert progress[f"session:{compact.session_id}"].model_dump() == {
            "total": 2,
            "completed": 1,
            "in_progress": 1,
        }
        assert progress[f"session:{managed.session_id}"].model_dump() == {
            "total": 1,
            "completed": 0,
            "in_progress": 0,
        }
    await engine.dispose()


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
        ("todo_progress", TodoProgress(total=2, completed=1, in_progress=1)),
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
        state="live",
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
    assert params == {"key": "cognis-work-projection:session-1:work-v8"}


def test_target_advance_repairs_stale_caught_up_state_without_breaking_active_lease() -> None:
    now = datetime.now(UTC)
    state = WorkSessionProjectionRow(
        projection_id="projection-target",
        owner_email="owner@example.com",
        session_id="session-target",
        source_session_id="source-target",
        materializer_version=WORK_MATERIALIZER_VERSION,
        state="caught_up",
        covered_through_seq=4,
        target_seq=4,
        next_retry_at=now + timedelta(hours=1),
    )

    assert _advance_projection_target(state, 5, now=now) is True
    assert (state.target_seq, state.state, state.next_retry_at) == (5, "repair", None)

    state.state = "caught_up"
    state.lease_owner = "controller-a"
    state.lease_expires_at = now + timedelta(seconds=30)
    assert _advance_projection_target(state, 6, now=now) is True
    assert state.state == "materializing"
    assert state.lease_owner == "controller-a"
    assert state.lease_expires_at == now + timedelta(seconds=30)


class AuthorityStore:
    def __init__(self, pages: dict[str, list[RawSessionEvent]]) -> None:
        self.pages = pages
        self.authorities: list[EventStoreAuthority] = []
        self.stale_watermarks: set[str] = set()
        self.invalidations: list[tuple[str, str, str]] = []
        self.store_id = "intaris"

    async def invalidate_session(self, store_id: str, session_id: str, *, source: str) -> None:
        self.invalidations.append((store_id, session_id, source))
        self.stale_watermarks.discard(session_id)

    def bind(self, authority: EventStoreAuthority) -> Any:
        self.authorities.append(authority)
        store = self

        class Reader:
            async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
                events = store.pages.get(session_id, [])
                visible = events[:-1] if session_id in store.stale_watermarks else events
                return SessionWatermark(
                    store_id="intaris",
                    session_id=session_id,
                    last_seq=visible[-1].seq if visible else 0,
                    availability=SessionHistoryAvailability(
                        durable_last_seq=visible[-1].seq if visible else 0,
                        first_available_seq=visible[0].seq if visible else None,
                    ),
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
                    availability=SessionHistoryAvailability(
                        durable_last_seq=store.pages.get(session_id, [])[-1].seq
                        if store.pages.get(session_id)
                        else 0,
                        first_available_seq=store.pages.get(session_id, [])[0].seq
                        if store.pages.get(session_id)
                        else None,
                    ),
                )

        return Reader()


class BlockingAuthorityStore(AuthorityStore):
    def __init__(
        self,
        pages: dict[str, list[RawSessionEvent]],
        *,
        blocked_session_ids: set[str],
    ) -> None:
        super().__init__(pages)
        self.blocked_session_ids = blocked_session_ids
        self.read_session_ids: set[str] = set()
        self.started_session_ids: set[str] = set()
        self.all_blocked = asyncio.Event()
        self.release = asyncio.Event()

    def bind(self, authority: EventStoreAuthority) -> Any:
        reader = super().bind(authority)
        store = self

        class Reader:
            async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
                store.read_session_ids.add(session_id)
                if session_id in store.blocked_session_ids and not store.release.is_set():
                    store.started_session_ids.add(session_id)
                    if store.started_session_ids == store.blocked_session_ids:
                        store.all_blocked.set()
                    await store.release.wait()
                return await reader.read_session_high_watermark(session_id=session_id)

            async def read_session_events(
                self, *, session_id: str, after_seq: int, limit: int, direction: str
            ) -> SessionEventPage:
                return await reader.read_session_events(
                    session_id=session_id,
                    after_seq=after_seq,
                    limit=limit,
                    direction=direction,
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
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
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
        if state is not None and state.state == "caught_up" and record is not None and caught_up:
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
async def test_background_repair_pause_is_quiescent_and_resumable(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    async with factory() as db:
        db.add_all(
            [
                WorkSessionProjectionRow(
                    projection_id=f"projection-{owner}",
                    owner_email=f"{owner}@example.com",
                    session_id=f"session-{owner}",
                    source_session_id=f"intaris-{owner}",
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    state="repair",
                    target_seq=1,
                )
                for owner in ("alice", "bob")
            ]
        )
        await db.commit()

    await materializer.pause_background_repair("projection-alice")
    claims = await materializer._claim()
    assert [claim.projection_id for claim in claims] == ["projection-bob"]

    async with factory() as db:
        bob = await db.get(WorkSessionProjectionRow, "projection-bob")
        assert bob is not None
        bob.state = "caught_up"
        bob.covered_through_seq = bob.target_seq
        bob.lease_owner = None
        bob.lease_expires_at = None
        await db.commit()
    materializer.resume_background_repair("projection-alice")
    claims = await materializer._claim()
    assert [claim.projection_id for claim in claims] == ["projection-alice"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_background_pause_does_not_strand_projection(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    await materializer._background_cycle_lock.acquire()
    pause = asyncio.create_task(materializer.pause_background_repair("projection-alice"))
    await asyncio.sleep(0)
    pause.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pause
    materializer._background_cycle_lock.release()
    assert "projection-alice" not in materializer._paused_background_projection_ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_caught_up_lag_is_claimed_and_converges_without_refresh(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    source = "intaris-alice"
    store = AuthorityStore(
        {
            source: [
                _event(
                    source,
                    1,
                    "tool_call",
                    {"call_id": "self-heal", "name": "bash", "arguments": {"command": "true"}},
                )
            ]
        }
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        worker_id="controller-self-heal",
    )
    async with factory() as db:
        db.add(
            WorkSessionProjectionRow(
                projection_id="projection-self-heal",
                owner_email="alice@example.com",
                session_id="session-alice",
                source_session_id=source,
                materializer_version=WORK_MATERIALIZER_VERSION,
                state="caught_up",
                covered_through_seq=0,
                target_seq=1,
                priority=0,
                next_retry_at=datetime.now(UTC) + timedelta(hours=1),
                next_head_check_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db.commit()

    claims = await materializer._claim()
    assert [claim.projection_id for claim in claims] == ["projection-self-heal"]
    await materializer._repair("projection-self-heal")

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, "projection-self-heal")
        assert state is not None
        assert (state.state, state.covered_through_seq, state.target_seq) == (
            "caught_up",
            1,
            1,
        )
        assert state.lease_owner is None
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

    class Dispatcher:
        def enqueue(self, _work: AppendInvalidation) -> bool:
            return True

    listener = EventAppendListenerFastPath(
        event_store=EventStore(),
        invalidation_dispatcher=Dispatcher(),
        work_materializer=materializer,
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
    # The normal repair worker can claim the persisted intent before this
    # assertion. Both states preserve the monotonic target.
    assert state.state in {"repair", "materializing"}
    assert state.target_seq == 2
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
    # The normal repair worker can claim the persisted intent before this
    # assertion. Both states preserve the monotonic target.
    assert state.state in {"repair", "materializing"}
    assert state.target_seq == 2
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
    assert state.state in {"repair", "materializing"}
    assert state.target_seq == 1
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
async def test_command_only_batch_skips_file_rebuild_and_retention_scrubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    rebuild = AsyncMock(return_value=0)
    scrub_records = AsyncMock(return_value=0)
    scrub_files = AsyncMock(return_value=0)
    monkeypatch.setattr(
        "cognis.api.chat_v2.work_materializer.rebuild_session_current_files",
        rebuild,
    )
    monkeypatch.setattr(
        "cognis.api.chat_v2.work_materializer.scrub_persisted_source_previews",
        scrub_records,
    )
    monkeypatch.setattr(
        "cognis.api.chat_v2.work_materializer.scrub_expired_current_file_content",
        scrub_files,
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {
            "bash": ToolDefinition(
                name="bash",
                description="Run a command",
                source=ToolSource(type="skill"),
                read_only=False,
                category="shell",
            )
        },
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 2)
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[
                _event(
                    "intaris-alice",
                    1,
                    "tool_call",
                    {
                        "call_id": "command-only",
                        "name": "bash",
                        "arguments": {"command": "git status --short"},
                    },
                ),
                _event(
                    "intaris-alice",
                    2,
                    "tool_result",
                    {
                        "call_id": "command-only",
                        "name": "bash",
                        "status": "complete",
                        "result": "clean",
                    },
                ),
            ],
            target_seq=2,
        )
        await db.commit()

        assert (state.state, state.covered_through_seq, state.command_count) == (
            "caught_up",
            2,
            1,
        )
        assert state.file_count == 0
    rebuild.assert_not_awaited()
    scrub_records.assert_not_awaited()
    scrub_files.assert_not_awaited()
    await engine.dispose()


@pytest.mark.asyncio
async def test_projection_counters_use_two_owner_scoped_queries_without_file_join(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 0)
        await db.flush()
        sa_event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            await materializer._refresh_projection_counters(db, state=state)
        finally:
            sa_event.remove(engine.sync_engine, "before_cursor_execute", capture)

    selects = [
        statement.lower()
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 2
    record_query = next(statement for statement in selects if "from work_records" in statement)
    file_query = next(statement for statement in selects if "from work_record_files" in statement)
    assert "work_records.owner_email" in record_query
    assert record_query.count("from work_records") == 1
    assert " join " not in record_query
    assert "work_record_files.owner_email" in file_query
    assert " join " not in file_query
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
        records = (
            await db.scalars(select(WorkRecordRow).where(WorkRecordRow.call_id == "call-1"))
        ).all()
        assert len(records) == 1
        records[0].timeline_item = records[0].timeline_item | {"activity_scope_id": None}
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
            assert "activity_scope_id" not in record.timeline_item
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


@pytest.mark.asyncio
@pytest.mark.parametrize("result_first", [False, True], ids=["call-result", "result-call"])
async def test_cross_batch_artifact_publish_counts_one_artifact_in_both_orders(
    tmp_path: Path,
    result_first: bool,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    result = _event(
        "intaris-alice",
        1 if result_first else 2,
        "tool_result",
        {
            "call_id": "call-publish",
            "name": "artifact_publish",
            "result": '{"artifact_id":"att_backup"}',
            "attachments": [
                {
                    "artifact_id": "att_backup",
                    "kind": "file",
                    "mime_type": "application/json",
                    "filename": "backup.json",
                    "size_bytes": 11776,
                }
            ],
        },
    )
    call = _event(
        "intaris-alice",
        2 if result_first else 1,
        "tool_call",
        {
            "call_id": "call-publish",
            "name": "artifact_publish",
            "arguments": {"path": "/tmp/backup.json"},
        },
    )
    first, second = (result, call) if result_first else (call, result)
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 2)
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[first],
            target_seq=2,
        )
        await db.commit()
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        state = await db.scalar(select(WorkSessionProjectionRow))
        assert row is not None and state is not None
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[second],
            target_seq=2,
        )
        await db.commit()
        records = (
            await db.scalars(
                select(WorkRecordRow)
                .where(WorkRecordRow.call_id == "call-publish")
                .order_by(WorkRecordRow.source_seq)
            )
        ).all()
        evidence = [record for record in records if record.is_evidence]
        assert len(evidence) == 1
        assert evidence[0].category == "artifacts"
        assert evidence[0].entity_id == "att_backup"
        assert ArtifactTimelineItem.model_validate(evidence[0].timeline_item).artifact_id == (
            "att_backup"
        )
        assert state.artifact_count == 1
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


def test_default_source_preview_policy_records_not_persisted_reason() -> None:
    payload = _bounded_item(
        ToolCallTimelineItem(
            id="tool:write",
            call_id="write",
            tool_name="write",
            sort_key="1",
            file_diffs=[FileDiffRef(path="src/app.py", diff="+change")],
            status="complete",
        )
    )

    assert payload["file_diffs"][0]["diff"] == ""
    assert payload["file_diffs"][0]["preview_omitted"] is True
    assert payload["file_diffs"][0]["preview_omission_reason"] == "not_persisted"


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
                    **(
                        {
                            "status": "renamed",
                            "old_path": "src/original.py",
                            "binary": True,
                            "generated": True,
                            "truncated": True,
                            "preview_omitted": True,
                        }
                        if index == 0
                        else {}
                    ),
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
        assert record.timeline_item["file_diffs"] == []
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
        assert projected.file_diffs[0].content_truncated
        assert all(diff.content_truncated for diff in projected.file_diffs)
        first = next(diff for diff in projected.file_diffs if diff.path == "src/file-0000.py")
        assert first.status == "renamed"
        assert first.old_path == "src/original.py"
        assert first.binary is True
        assert first.generated is True
        assert first.truncated is True
        assert first.preview_omitted is True
        projection = build_work_projection(
            scope=page.scope,
            projection_version=page.projection_version,
            items=page.items,
            tool_definitions=definitions,
            has_more_before=False,
            before_cursor=None,
            server_time=page.server_time,
            complete_files=True,
        )
        projected_first = next(
            diff
            for mutation in projection.mutations
            for diff in mutation.file_diffs
            if diff.path.endswith("file-0000.py")
        )
        assert projected_first.status == "renamed"
        assert projected_first.old_path is not None
        assert projected_first.old_path.endswith("original.py")
        assert projected_first.binary is True
        assert projected_first.generated is True
        assert projected_first.truncated is True
        assert projected_first.preview_omitted is True
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
        assert len(projected.file_diffs) == 1
        diff = projected.file_diffs[0]
        assert diff.path == "src/renamed.py"
        assert diff.path_id is not None
        assert diff.diff == ""
        assert diff.preview_omitted is True
        assert diff.old_path == "src/deleted.py"
        assert diff.status == "renamed"
        assert diff.binary is True
        assert diff.generated is True
        assert diff.truncated is True
        assert diff.preview_omitted is True
        assert diff.additions == 1
        assert diff.deletions == 0
        assert diff.content_truncated is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_current_file_generations_follow_source_order_and_preserve_rename_history(
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
    events = [
        _event(
            "intaris-alice",
            1,
            "tool_result",
            {"call_id": "one", "name": "write", "file_diffs": [{"path": "A", "diff": "+a"}]},
        ),
        _event(
            "intaris-alice",
            2,
            "tool_result",
            {
                "call_id": "two",
                "name": "write",
                "file_diffs": [{"path": "B", "old_path": "A", "status": "renamed", "diff": ""}],
            },
        ),
        _event(
            "intaris-alice",
            3,
            "tool_result",
            {
                "call_id": "three",
                "name": "write",
                "file_diffs": [{"path": "B", "status": "deleted", "diff": "-a"}],
            },
        ),
        _event(
            "intaris-alice",
            4,
            "tool_result",
            {"call_id": "four", "name": "write", "file_diffs": [{"path": "B", "diff": "+new"}]},
        ),
    ]
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 4)
        await materializer._materialize_batch(
            db, row=row, state=state, raw_events=events, target_seq=4
        )
        records = list(
            (await db.scalars(select(WorkRecordRow).order_by(WorkRecordRow.source_seq))).all()
        )
        records[0].occurred_at = datetime(2030, 1, 1, tzinfo=UTC)
        records[-1].occurred_at = datetime(2020, 1, 1, tzinfo=UTC)
        await rebuild_session_current_files(
            db,
            owner_email=row.user_email,
            session_id=row.session_id,
            materializer_version=WORK_MATERIALIZER_VERSION,
        )
        facts = list(
            (
                await db.scalars(select(WorkRecordFileRow).order_by(WorkRecordFileRow.source_seq))
            ).all()
        )
        assert facts[0].path_generation_id == facts[1].path_generation_id
        assert facts[1].path_generation_id == facts[2].path_generation_id
        assert facts[3].path_generation_id != facts[2].path_generation_id
    await engine.dispose()


def test_old_rename_identity_uses_root_only_for_relative_same_root_paths() -> None:
    same_root = FileDiffRef(
        path="new.py",
        old_path="old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    cross_root = FileDiffRef(
        path="new.py",
        old_path="/other/old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    windows = FileDiffRef(
        path="new.py",
        old_path=r"D:\other\old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    unbound = FileDiffRef(path="new.py", old_path="../old.py", diff="")
    absolute_same_root = FileDiffRef(
        path="new.py",
        old_path="/repo/src/old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    absolute_windows_same_root = FileDiffRef(
        path="new.py",
        old_path=r"C:\repo\src\old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    traversed_posix_cross_root = FileDiffRef(
        path="new.py",
        old_path="/repo/../other/file.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    traversed_windows_cross_root = FileDiffRef(
        path="new.py",
        old_path=r"C:\repo\..\other\file.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    normalized_posix_same_root = FileDiffRef(
        path="new.py",
        old_path="/repo/src/../old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )
    normalized_windows_same_root = FileDiffRef(
        path="new.py",
        old_path=r"C:\repo\src\..\old.py",
        root_id="root-a",
        relative_path="new.py",
        diff="",
    )

    assert _old_file_path_id(same_root, workdir="/repo") == "root-a:old.py"
    assert (_old_file_path_id(cross_root, workdir="/repo") or "").startswith("unbound:")
    assert (_old_file_path_id(windows, workdir=r"C:\repo") or "").startswith("unbound:")
    assert (_old_file_path_id(unbound, workdir=None) or "").startswith("unbound:")
    assert _old_file_path_id(absolute_same_root, workdir="/repo") == "root-a:src/old.py"
    assert _old_file_path_id(absolute_windows_same_root, workdir=r"c:\repo") == "root-a:src/old.py"
    assert (_old_file_path_id(traversed_posix_cross_root, workdir="/repo") or "").startswith(
        "unbound:"
    )
    assert (_old_file_path_id(traversed_windows_cross_root, workdir=r"C:\repo") or "").startswith(
        "unbound:"
    )
    assert _old_file_path_id(normalized_posix_same_root, workdir="/repo") == "root-a:old.py"
    assert _old_file_path_id(normalized_windows_same_root, workdir=r"C:\repo") == "root-a:old.py"


@pytest.mark.asyncio
async def test_rename_onto_occupied_destination_preserves_both_generations(
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
    events = [
        _event(
            "intaris-alice",
            1,
            "tool_result",
            {
                "call_id": "a",
                "name": "write",
                "file_diffs": [{"path": "A", "diff": "+a"}],
            },
        ),
        _event(
            "intaris-alice",
            2,
            "tool_result",
            {
                "call_id": "b",
                "name": "write",
                "file_diffs": [{"path": "B", "diff": "+b"}],
            },
        ),
        _event(
            "intaris-alice",
            3,
            "tool_result",
            {
                "call_id": "rename",
                "name": "write",
                "file_diffs": [
                    {
                        "path": "B",
                        "old_path": "A",
                        "status": "renamed",
                        "diff": "",
                    }
                ],
            },
        ),
    ]
    async with factory() as db:
        session = await db.get(Session, "session-alice")
        assert session is not None
        state = await materializer._ensure_state(db, session, 3)
        await materializer._materialize_batch(
            db,
            row=session,
            state=state,
            raw_events=events,
            target_seq=3,
        )
        facts = list(
            (
                await db.scalars(select(WorkRecordFileRow).order_by(WorkRecordFileRow.source_seq))
            ).all()
        )
        assert facts[0].path_generation_id == facts[2].path_generation_id
        assert facts[1].path_generation_id != facts[2].path_generation_id
        rows = list(
            (
                await db.scalars(
                    select(WorkCurrentFileRow).order_by(WorkCurrentFileRow.path_generation_id)
                )
            ).all()
        )
        assert len(rows) == 2
        assert {row.state for row in rows} == {"overwritten", "renamed"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_daily_retention_scrubs_expired_source_preview_without_new_events(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    async with factory() as db:
        record = WorkRecordRow(
            work_record_id="record-expired",
            owner_email="alice@example.com",
            session_id="session-alice",
            materializer_version=WORK_MATERIALIZER_VERSION,
            source_store="intaris",
            source_session_id="intaris-alice",
            source_seq=1,
            source_event_id="event-1",
            source_item_id="item-1",
            item_ordinal=0,
            occurred_at=datetime.now(UTC),
            record_type="tool_call",
            category="mutation",
            is_evidence=True,
            timeline_item={
                "id": "item-1",
                "kind": "tool_call",
                "file_diffs": [{"path": "secret.txt", "diff": "+secret"}],
            },
            source_content_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add(record)
        await db.commit()
    restarted = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        source_preview_max_lifetime_seconds=60,
    )
    assert await restarted._scrub_source_content() == 1
    async with factory() as db:
        record = await db.get(WorkRecordRow, "record-expired")
        assert record is not None
        assert record.timeline_item["file_diffs"][0]["diff"] == ""
        assert (
            record.timeline_item["file_diffs"][0]["preview_omission_reason"] == "retention_expired"
        )
        assert record.source_content_scrubbed_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_files_projection_collapses_rename_chains_without_crossing_roots(
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

    def file_event(
        seq: int,
        *,
        path: str,
        additions: int,
        deletions: int,
        status: str,
        old_path: str | None = None,
    ) -> RawSessionEvent:
        return _event(
            "intaris-alice",
            seq,
            "tool_result",
            {
                "call_id": f"rename-{seq}",
                "name": "write",
                "file_diffs": [
                    {
                        "path": path,
                        "old_path": old_path,
                        "status": status,
                        "diff": "\n".join([*["+line"] * additions, *["-line"] * deletions]),
                        "additions": additions,
                        "deletions": deletions,
                    }
                ],
            },
        )

    events = [
        file_event(
            1,
            path="/root-a/old.py",
            additions=1,
            deletions=1,
            status="modified",
        ),
        file_event(
            2,
            path="/root-a/mid.py",
            old_path="/root-a/old.py",
            additions=2,
            deletions=0,
            status="renamed",
        ),
        file_event(
            3,
            path="/root-a/new.py",
            old_path="/root-a/mid.py",
            additions=3,
            deletions=1,
            status="renamed",
        ),
        file_event(
            4,
            path="/root-a/new.py",
            additions=4,
            deletions=2,
            status="modified",
        ),
        file_event(
            5,
            path="/root-b/old.py",
            additions=5,
            deletions=3,
            status="modified",
        ),
    ]

    async with factory() as db:
        session = await db.get(Session, "session-alice")
        assert session is not None
        state = await materializer._ensure_state(db, session, len(events))
        await materializer._materialize_batch(
            db,
            row=session,
            state=state,
            raw_events=events,
            target_seq=len(events),
        )
        await db.commit()

        page = await read_work_page(
            db,
            owner_email=session.user_email,
            scope=TimelineScope(
                key=f"session:{session.session_id}",
                kind="session",
                session_id=session.session_id,
            ),
            session_rows=[session],
            graph_fingerprint="rename-graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )
        projection = build_work_projection(
            scope=page.scope,
            projection_version=page.projection_version,
            items=page.items,
            tool_definitions=definitions,
            has_more_before=False,
            before_cursor=None,
            server_time=page.server_time,
            summary=page.summary,
            newest_first=True,
            complete_files=True,
        )
    diffs = [diff for mutation in projection.mutations for diff in mutation.file_diffs]
    root_a = [diff for diff in diffs if diff.path.endswith("new.py")]
    root_b = [diff for diff in diffs if diff.path.endswith("old.py")]
    assert len(root_a) == 1
    assert len(root_b) == 1
    assert len({diff.path_id for diff in root_a}) == 1
    assert root_a[0].status == "modified"
    assert root_a[0].old_path is None
    assert root_a[0].additions == 10
    assert root_a[0].deletions == 4
    assert root_b[0].path_id not in {diff.path_id for diff in root_a}
    assert projection.summary.changed_files == 2
    assert projection.summary.additions == 15
    assert projection.summary.deletions == 7
    await engine.dispose()


@pytest.mark.asyncio
async def test_files_projection_keeps_recreated_rename_source_separate(
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

    def event(
        seq: int,
        path: str,
        *,
        status: str,
        additions: int,
        deletions: int,
        old_path: str | None = None,
    ) -> RawSessionEvent:
        return _event(
            "intaris-alice",
            seq,
            "tool_result",
            {
                "call_id": f"reuse-{seq}",
                "name": "write",
                "file_diffs": [
                    {
                        "path": path,
                        "old_path": old_path,
                        "status": status,
                        "diff": "\n".join([*["+line"] * additions, *["-line"] * deletions]),
                        "additions": additions,
                        "deletions": deletions,
                    }
                ],
            },
        )

    events = [
        event(1, "/repo/A.py", status="modified", additions=1, deletions=1),
        event(
            2,
            "/repo/B.py",
            old_path="/repo/A.py",
            status="renamed",
            additions=2,
            deletions=1,
        ),
        event(3, "/repo/A.py", status="added", additions=3, deletions=0),
        event(4, "/repo/A.py", status="modified", additions=4, deletions=2),
    ]

    async with factory() as db:
        session = await db.get(Session, "session-alice")
        assert session is not None
        state = await materializer._ensure_state(db, session, len(events))
        await materializer._materialize_batch(
            db,
            row=session,
            state=state,
            raw_events=events,
            target_seq=len(events),
        )
        await db.commit()
        page = await read_work_page(
            db,
            owner_email=session.user_email,
            scope=TimelineScope(
                key=f"session:{session.session_id}",
                kind="session",
                session_id=session.session_id,
            ),
            session_rows=[session],
            graph_fingerprint="reuse-graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )
        projection = build_work_projection(
            scope=page.scope,
            projection_version=page.projection_version,
            items=page.items,
            tool_definitions=definitions,
            has_more_before=False,
            before_cursor=None,
            server_time=page.server_time,
            summary=page.summary,
            newest_first=True,
            complete_files=True,
        )

    diffs = [diff for mutation in projection.mutations for diff in mutation.file_diffs]
    recreated = [diff for diff in diffs if diff.path.endswith("A.py")]
    renamed = [diff for diff in diffs if diff.path.endswith("B.py")]
    assert len(recreated) == 1
    assert recreated[0].status == "modified"
    assert (recreated[0].additions, recreated[0].deletions) == (7, 2)
    assert len(renamed) == 1
    assert renamed[0].status == "renamed"
    assert renamed[0].additions == 3
    assert renamed[0].deletions == 2
    assert recreated[0].path_id not in {diff.path_id for diff in renamed}
    assert projection.summary.changed_files == 2
    assert projection.summary.additions == 10
    assert projection.summary.deletions == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_files_projection_is_complete_and_bounded_by_unique_paths(
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
    scope = TimelineScope(
        key="session:session-alice",
        kind="session",
        session_id="session-alice",
    )
    expected_counts: dict[str, list[int]] = {
        "src/a.py": [0, 0],
        "src/b.py": [0, 0],
        "src/c.py": [0, 0],
    }

    async with factory() as db:
        root = await db.get(Session, "session-alice")
        assert root is not None
        child = Session(
            session_id="session-child",
            conversation_id=root.conversation_id,
            user_email=root.user_email,
            agent_id=root.agent_id,
            intaris_session_id="intaris-child",
            parent_session_id=root.session_id,
            delegation_metadata={},
        )
        db.add(child)
        await db.flush()
        db.add_all(
            [
                WorkSessionProjectionRow(
                    projection_id="projection-root-files",
                    owner_email=root.user_email,
                    session_id=root.session_id,
                    source_session_id=root.intaris_session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    target_seq=601,
                    covered_through_seq=601,
                    state="caught_up",
                ),
                WorkSessionProjectionRow(
                    projection_id="projection-child-files",
                    owner_email=child.user_email,
                    session_id=child.session_id,
                    source_session_id=child.intaris_session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    target_seq=602,
                    covered_through_seq=602,
                    state="caught_up",
                ),
            ]
        )

        records: list[WorkRecordRow] = []
        file_rows: list[WorkRecordFileRow] = []

        def add_record(
            seq: int,
            session: Session,
            diffs: list[FileDiffRef],
        ) -> None:
            item = ToolCallTimelineItem(
                id=f"tool:file-history-{seq}",
                call_id=f"file-history-{seq}",
                tool_name="write",
                sort_key=f"{seq:08d}",
                source_refs=[
                    SourceRef(
                        store="intaris",
                        session_id=session.intaris_session_id,
                        seq=seq,
                        event_type="tool_result",
                    )
                ],
                arguments={"workdir": "/repo"},
                file_diffs=diffs,
                status="complete",
            )
            record_id = f"file-history-record-{seq}"
            records.append(
                WorkRecordRow(
                    work_record_id=record_id,
                    owner_email=session.user_email,
                    session_id=session.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id=session.intaris_session_id,
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
            for ordinal, diff in enumerate(diffs):
                path = diff.path
                additions = int(diff.additions or 0)
                deletions = int(diff.deletions or 0)
                expected_counts[path][0] += additions
                expected_counts[path][1] += deletions
                file_rows.append(
                    WorkRecordFileRow(
                        work_record_file_id=f"file-history-row-{seq}-{ordinal}",
                        owner_email=session.user_email,
                        session_id=session.session_id,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        work_record_id=record_id,
                        file_ordinal=ordinal,
                        path=path,
                        path_id=f"root:{path}",
                        path_generation_id=f"root:{path}:0",
                        source_seq=seq,
                        item_ordinal=0,
                        additions=additions,
                        deletions=deletions,
                        status=diff.status,
                        old_path=diff.old_path,
                        binary=diff.binary,
                        generated=diff.generated,
                        truncated=diff.truncated,
                    )
                )

        for seq in range(1, 601):
            path = "src/a.py" if seq % 2 else "src/b.py"
            add_record(
                seq,
                root if seq <= 300 else child,
                [
                    FileDiffRef(
                        path=path,
                        diff=f"-old-{seq}\n+new-{seq}",
                        status="modified",
                        additions=1,
                        deletions=1,
                    )
                ],
            )
        add_record(
            601,
            root,
            [
                FileDiffRef(
                    path="src/a.py",
                    diff="-removed",
                    status="deleted",
                    binary=True,
                    additions=0,
                    deletions=7,
                ),
                FileDiffRef(
                    path="src/c.py",
                    diff="+first",
                    status="added",
                    additions=3,
                    deletions=0,
                ),
                FileDiffRef(
                    path="src/b.py",
                    diff="+latest-b",
                    status="modified",
                    additions=2,
                    deletions=0,
                ),
            ],
        )
        add_record(
            602,
            child,
            [
                FileDiffRef(
                    path="src/c.py",
                    old_path="src/old-c.py",
                    diff="-first\n+final",
                    status="renamed",
                    generated=True,
                    truncated=True,
                    additions=5,
                    deletions=2,
                )
            ],
        )
        db.add_all(records)
        await db.flush()
        db.add_all(file_rows)
        await db.flush()
        for session in (root, child):
            await rebuild_session_current_files(
                db,
                owner_email=session.user_email,
                session_id=session.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
            )
        await db.commit()

        base = select(WorkRecordRow).where(
            WorkRecordRow.owner_email == root.user_email,
            WorkRecordRow.session_id.in_([root.session_id, child.session_id]),
            WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
            WorkRecordRow.is_evidence.is_(True),
        )
        latest = _latest_file_projection(base)
        projected_rows = (await db.execute(select(latest))).mappings().all()
        assert len(projected_rows) == len(expected_counts)
        assert {str(row["path"]) for row in projected_rows} == set(expected_counts)
        assert len({str(row["work_record_id"]) for row in projected_rows}) <= len(expected_counts)

        queries: list[tuple[str, Any]] = []

        def capture_query(
            _connection: Any,
            _cursor: Any,
            statement: str,
            parameters: Any,
            _context: Any,
            _executemany: Any,
        ) -> None:
            queries.append((statement, parameters))

        sa_event.listen(engine.sync_engine, "before_cursor_execute", capture_query)
        page = await read_work_page(
            db,
            owner_email=root.user_email,
            scope=scope,
            session_rows=[root, child],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=None,
            limit=1,
            category="files",
            tool_definitions=definitions,
        )
        sa_event.remove(engine.sync_engine, "before_cursor_execute", capture_query)

        workstreams = [
            WorkstreamRef(
                key=f"session:{session.session_id}",
                kind="root" if session is root else "delegate",
                parent_key=None if session is root else f"session:{root.session_id}",
                root_key=f"session:{root.session_id}",
                edge_kind="root" if session is root else "delegate",
                ordinal=index,
                conversation_id=session.conversation_id,
                session_id=session.session_id,
                event_store_session_id=session.intaris_session_id,
                title=session.session_id,
                agent_id=session.agent_id,
                status=session.status,
            )
            for index, session in enumerate((root, child))
        ]
        projection = build_work_projection(
            scope=scope,
            projection_version=page.projection_version,
            items=page.items,
            tool_definitions=definitions,
            has_more_before=page.has_more_before,
            before_cursor=page.before_cursor,
            server_time=page.server_time,
            workstreams=workstreams,
            summary=page.summary,
            newest_first=True,
            complete_files=True,
        )
        files = {
            diff.path: diff for mutation in projection.mutations for diff in mutation.file_diffs
        }

        assert len(queries) <= 10
        assert any("work_current_files" in statement for statement, _ in queries)
        assert all("ROW_NUMBER() OVER" not in statement.upper() for statement, _ in queries)
        assert len(page.items) == 3
        assert page.has_more_before is False
        assert len(files) == len(expected_counts)
        assert page.summary is not None
        assert page.summary.changed_files == len(expected_counts)
        assert page.summary.additions == sum(diff.additions or 0 for diff in files.values())
        assert page.summary.deletions == sum(diff.deletions or 0 for diff in files.values())
        for path, counts in expected_counts.items():
            projected = next(diff for key, diff in files.items() if key.endswith(path))
            assert projected.additions is not None
            assert projected.deletions is not None
            assert projected.additions <= counts[0]
            assert projected.deletions <= counts[1]
        file_a = next(diff for key, diff in files.items() if key.endswith("src/a.py"))
        file_c = next(diff for key, diff in files.items() if key.endswith("src/c.py"))
        assert file_a.status == "modified"
        assert file_c.status == "renamed"
        assert file_c.generated is True
        assert file_c.truncated is True
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
async def test_targeted_refresh_repairs_missed_first_and_subsequent_append(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    store = AuthorityStore(
        {"intaris-alice": [_event("intaris-alice", 1, "user_message", {"content": "first"})]}
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        materializer = WorkMaterializer(
            session_factory=factory,
            event_store=store,
            tool_definitions=lambda: {},
        )
        state = await materializer._ensure_state(db, row, 0)
        state.state = "caught_up"
        await db.commit()

    restarted = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    await restarted.prioritize_sessions([row])
    claimed = await restarted._claim()
    assert len(claimed) == 1
    await restarted._repair(claimed[0].projection_id)
    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claimed[0].projection_id)
        assert state is not None
        assert (state.state, state.covered_through_seq) == ("caught_up", 1)
        assert state.next_head_check_at is None
        await db.commit()

    store.pages["intaris-alice"].append(
        _event("intaris-alice", 2, "user_message", {"content": "second"})
    )
    restarted_again = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    await restarted_again.prioritize_sessions([row])
    claimed = await restarted_again._claim()
    assert len(claimed) == 1
    await restarted_again._repair(claimed[0].projection_id)
    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claimed[0].projection_id)
        assert state is not None
        assert (state.state, state.covered_through_seq) == ("caught_up", 2)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_seq", [1, 2])
async def test_repair_invalidates_stale_head_even_when_target_was_not_advanced(
    tmp_path: Path,
    target_seq: int,
) -> None:
    engine, factory = await _database(tmp_path)
    source_session_id = "intaris-alice"
    store = AuthorityStore(
        {
            source_session_id: [
                _event(source_session_id, 1, "user_message", {"content": "retained"}),
                _event(source_session_id, 2, "user_message", {"content": "new"}),
            ]
        }
    )
    store.stale_watermarks.add(source_session_id)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, target_seq)
        state.covered_through_seq = 1
        state.target_seq = target_seq
        state.state = "repair"
        await db.commit()

    claimed = await materializer._claim()
    assert len(claimed) == 1
    await materializer._repair(claimed[0].projection_id)

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claimed[0].projection_id)
        assert state is not None
        assert (state.state, state.covered_through_seq, state.target_seq) == (
            "caught_up",
            2,
            2,
        )
    assert store.invalidations == [("intaris", source_session_id, "work_repair_target_ahead")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_completed_system_agent_runtime_metadata_requires_recorded_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    system_agent = SYSTEM_AGENTS["system:implement"].model_copy(
        update={
            "llm_config": AgentLLMConfig(
                model="system-model",
                reasoning_effort="high",
            )
        }
    )
    monkeypatch.setitem(SYSTEM_AGENTS, "system:implement", system_agent)
    async with factory() as db:
        row = Session(
            session_id="session-system-implement",
            activity_scope_id="conversation:conversation-alice",
            conversation_id="conversation-alice",
            user_email="alice@example.com",
            agent_id="system:implement",
            intaris_session_id="intaris-system-implement",
            delegation_metadata={},
            status="completed",
        )
        db.add(row)
        await db.commit()

        metadata = await _session_runtime_metadata(
            db,
            owner_email="alice@example.com",
            session_rows=[row],
            turn_rows=[],
        )

        row.delegation_metadata = {
            "model": "observed-model",
            "reasoning_effort": "low",
        }
        await db.commit()
        observed = await _session_runtime_metadata(
            db,
            owner_email="alice@example.com",
            session_rows=[row],
            turn_rows=[],
        )

    # Present-day defaults do not establish which model ran a historical session.
    assert metadata[row.session_id] == (None, None, None, "Implement", None)
    # Copied delegation metadata is not proof of the runtime that completed
    # this historical session.
    assert observed[row.session_id] == (None, None, None, "Implement", None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_projection_callback_publishes_changing_dedup_revisions(
    tmp_path: Path,
) -> None:
    assert int.from_bytes(hashlib.sha256(b"b").digest()[:8], "big") < int.from_bytes(
        hashlib.sha256(b"a").digest()[:8],
        "big",
    )
    engine, factory = await _database(tmp_path)
    cluster = SimpleNamespace(publish_work_invalidation=AsyncMock(return_value=True))
    callback = _create_work_projection_callback(factory, cluster)
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        materializer = WorkMaterializer(
            session_factory=factory,
            event_store=AuthorityStore({}),
            tool_definitions=lambda: {},
        )
        state = await materializer._ensure_state(db, row, 0)
        state.state = "caught_up"
        await db.commit()

    await callback("conversation-alice")
    first_revisions = {
        call.kwargs["revision"] for call in cluster.publish_work_invalidation.await_args_list
    }
    assert cluster.publish_work_invalidation.await_count == 1
    assert (
        cluster.publish_work_invalidation.await_args.kwargs["scope_key"]
        == "conversation:conversation-alice"
    )
    cluster.publish_work_invalidation.reset_mock()
    async with factory() as db:
        state = await db.scalar(select(WorkSessionProjectionRow))
        assert state is not None
        state.target_seq = 1
        state.covered_through_seq = 1
        await db.commit()
    await callback("conversation-alice")
    second_revisions = {
        call.kwargs["revision"] for call in cluster.publish_work_invalidation.await_args_list
    }
    assert len(first_revisions) == len(second_revisions) == 1
    assert next(iter(second_revisions)) == next(iter(first_revisions)) + 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_topology_status_invalidation_is_owner_scoped_and_commit_safe(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    published: list[dict[str, int]] = []

    def capture(revisions: dict[str, int]) -> None:
        published.append(revisions)

    unregister = register_live_work_waker(capture)
    try:
        async with factory() as db:
            row = await db.get(Session, "session-alice")
            assert row is not None
            row.status = "completed"
            await db.commit()
        assert set(published[-1]) == {"alice@example.com"}
        first_revision = published[-1]["alice@example.com"]

        async with factory() as db:
            row = await db.get(Session, "session-alice")
            assert row is not None
            row.status = "failed"
            await db.commit()
        assert published[-1]["alice@example.com"] == first_revision + 1
        committed_count = len(published)

        async with factory() as db:
            row = await db.get(Session, "session-alice")
            assert row is not None
            row.status = "active"
            await db.rollback()
        assert len(published) == committed_count
    finally:
        unregister()
        await engine.dispose()


@pytest.mark.asyncio
async def test_bulk_lifecycle_updates_publish_owner_revision(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    published: list[dict[str, int]] = []

    def capture(revisions: dict[str, int]) -> None:
        published.append(revisions)

    unregister = register_live_work_waker(capture)
    try:
        async with factory() as db:
            db.add(
                Task(
                    task_id="task-alice",
                    title="Task",
                    created_by="alice@example.com",
                    agent_id="agent-alice",
                )
            )
            await db.commit()
        published.clear()

        mutations = (
            update(Session).where(Session.session_id == "session-alice").values(status="completed"),
            update(Conversation)
            .where(Conversation.conversation_id == "conversation-alice")
            .values(status="archived"),
            update(Task).where(Task.task_id == "task-alice").values(status="running"),
        )
        for statement in mutations:
            async with factory() as db:
                await db.execute(statement)
                await db.commit()

        async with factory() as db:
            await db.execute(
                sqlite.insert(DirectTurnRequestRow)
                .values(
                    request_id="dtr-bulk-owner",
                    turn_id="turn-bulk-owner",
                    conversation_id="conversation-alice",
                    session_id="session-alice",
                    agent_id="agent-alice",
                    user_id="alice@example.com",
                    idempotency_scope="conversation-alice",
                    idempotency_key="bulk-owner",
                    admission_hash="admission",
                    payload_hash="payload",
                    payload={},
                    status="queued",
                )
                .on_conflict_do_nothing()
            )
            await db.commit()

        assert len(published) == 4
        revisions = [item["alice@example.com"] for item in published]
        assert revisions == list(range(revisions[0], revisions[0] + 4))
    finally:
        unregister()
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_lagging_failure_is_not_reclaimed_without_explicit_retry(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob", "carol"))
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="claim-worker",
    )
    async with factory() as db:
        alice = await db.get(Session, "session-alice")
        bob = await db.get(Session, "session-bob")
        assert alice is not None and bob is not None
        terminal = await materializer._ensure_state(db, alice, 1)
        terminal.state = "failed"
        terminal.next_retry_at = None
        due = await materializer._ensure_state(db, bob, 1)
        due.state = "repair"
        due.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        carol = await db.get(Session, "session-carol")
        assert carol is not None
        failed_retry = await materializer._ensure_state(db, carol, 1)
        failed_retry.state = "failed"
        failed_retry.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    claimed = await materializer._claim()
    assert {row.session_id for row in claimed} == {"session-bob", "session-carol"}
    async with factory() as db:
        terminal = await db.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == "session-alice"
            )
        )
        assert terminal is not None
        assert terminal.state == "failed"
        assert terminal.lease_owner is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_urgent_claim_does_not_resurrect_terminal_or_backed_off_repair(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="urgent-worker",
    )
    now = datetime.now(UTC)
    async with factory() as db:
        alice = await db.get(Session, "session-alice")
        bob = await db.get(Session, "session-bob")
        assert alice is not None and bob is not None
        terminal = await materializer._ensure_state(db, alice, 1)
        terminal.state = "failed"
        terminal.priority = WORK_VISIBLE_REFRESH_PRIORITY
        terminal.lease_fence = 3
        backed_off = await materializer._ensure_state(db, bob, 1)
        backed_off.state = "repair"
        backed_off.priority = WORK_VISIBLE_REFRESH_PRIORITY
        backed_off.next_retry_at = now + timedelta(minutes=1)
        backed_off.lease_fence = 4
        await db.commit()
        terminal_id = terminal.projection_id
        backed_off_id = backed_off.projection_id

    assert await materializer._claim_urgent(terminal_id) is None
    assert await materializer._claim_urgent(backed_off_id) is None
    assert await materializer._still_urgent_projection_ids([terminal_id, backed_off_id]) == set()
    assert (
        await materializer._continue_urgent_claim(
            SimpleNamespace(
                projection_id=terminal_id,
                lease_fence=3,
                priority=WORK_VISIBLE_REFRESH_PRIORITY,
            )
        )
        is False
    )
    assert (
        await materializer._continue_urgent_claim(
            SimpleNamespace(
                projection_id=backed_off_id,
                lease_fence=4,
                priority=WORK_VISIBLE_REFRESH_PRIORITY,
            )
        )
        is False
    )
    async with factory() as db:
        terminal = await db.get(WorkSessionProjectionRow, terminal_id)
        backed_off = await db.get(WorkSessionProjectionRow, backed_off_id)
        assert terminal is not None and backed_off is not None
        assert (terminal.state, terminal.priority, terminal.next_retry_at) == (
            "failed",
            0,
            None,
        )
        assert backed_off.state == "repair"
        assert backed_off.priority == 0
        assert backed_off.next_retry_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_unavailable_repair_preserves_demand_received_during_lease(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="concurrent-demand-worker",
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        state.state = "repair"
        state.priority = WORK_VISIBLE_REFRESH_PRIORITY
        await db.commit()
        projection_id = state.projection_id

    claim = await materializer._claim_urgent(projection_id)
    assert claim is not None

    class UnavailableReader:
        async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
            assert await materializer.mark_source_pending(
                owner_email="alice@example.com",
                source_session_id=session_id,
                target_seq=2,
            )
            return SessionWatermark(
                store_id="intaris",
                session_id=session_id,
                last_seq=2,
                availability=SessionHistoryAvailability(
                    durable_last_seq=2,
                    first_available_seq=2,
                ),
            )

    materializer._reader_for = AsyncMock(return_value=UnavailableReader())  # type: ignore[method-assign]
    await materializer._repair(
        projection_id,
        lease_owner=materializer._urgent_worker_id,
        repair_slot=materializer._urgent_repair_slot,
    )

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, projection_id)
        assert state is not None
        assert (state.state, state.target_seq, state.priority, state.retry_count) == (
            "repair",
            2,
            WORK_LIVE_APPEND_PRIORITY,
            0,
        )
        assert state.next_retry_at is None
        assert state.lease_owner is None
    assert await materializer._continue_urgent_claim(claim) is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_noncontiguous_repair_preempts_historical_backlog(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob", "carol"))
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="claim-worker",
    )
    now = datetime.now(UTC)
    async with factory() as db:
        for owner, age_hours in (("alice", 3), ("bob", 2)):
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await materializer._ensure_state(db, row, 1)
            state.state = "repair"
            state.priority = 0
            state.last_error = None
            state.updated_at = now - timedelta(hours=age_hours)
        active = await db.get(Session, "session-carol")
        assert active is not None
        active_state = await materializer._ensure_state(db, active, 2)
        active_state.state = "repair"
        active_state.priority = 0
        active_state.last_error = "noncontiguous live append"
        active_state.updated_at = now
        await db.commit()

    claimed = await materializer._claim()

    assert len(claimed) == 2
    assert claimed[0].session_id == "session-carol"
    assert {row.session_id for row in claimed} != {"session-alice", "session-bob"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_controllers_claim_only_two_of_ten_due_repairs(
    tmp_path: Path,
) -> None:
    owners = tuple(f"owner-{index}" for index in range(10))
    engine, factory = await _database(tmp_path, owners=owners)
    first = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="controller-a",
    )
    second = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="controller-b",
    )
    async with factory() as db:
        rows = list((await db.scalars(select(Session))).all())
        assert len(rows) == 10
        for row in rows:
            state = await first._ensure_state(db, row, 1)
            state.state = "repair"
            state.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    first_claim = await first._claim()
    second_claim = await second._claim()
    assert len(first_claim) == 2
    assert second_claim == []
    async with factory() as db:
        leased = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(WorkSessionProjectionRow.lease_owner.is_not(None))
            )
            or 0
        )
        queued = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(
                    WorkSessionProjectionRow.state == "repair",
                    WorkSessionProjectionRow.lease_owner.is_(None),
                )
            )
            or 0
        )
    assert (leased, queued) == (2, 8)
    await engine.dispose()


@pytest.mark.asyncio
async def test_visible_refresh_claims_ahead_of_four_thousand_historical_repairs(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="visible-worker",
    )
    async with factory() as db:
        visible = await db.get(Session, "session-alice")
        assert visible is not None
        visible_state = await materializer._ensure_state(db, visible, 5)
        visible_state.state = "repair"
        await db.execute(
            insert(Session),
            [
                {
                    "session_id": f"backlog-{index:04d}",
                    "conversation_id": visible.conversation_id,
                    "user_email": visible.user_email,
                    "agent_id": visible.agent_id,
                    "intaris_session_id": f"source-backlog-{index:04d}",
                    "status": "completed",
                }
                for index in range(4000)
            ],
        )
        await db.execute(
            insert(WorkSessionProjectionRow),
            [
                {
                    "projection_id": f"projection-backlog-{index:04d}",
                    "owner_email": visible.user_email,
                    "session_id": f"backlog-{index:04d}",
                    "source_session_id": f"source-backlog-{index:04d}",
                    "materializer_version": WORK_MATERIALIZER_VERSION,
                    "state": "repair",
                    "target_seq": 0,
                    "covered_through_seq": 0,
                    "priority": 100,
                }
                for index in range(4000)
            ],
        )
        await db.commit()

    await materializer.prioritize_sessions([visible])
    claimed = await materializer._claim()
    assert claimed[0].session_id == visible.session_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_visible_priority_preserves_live_lease_and_reclaims_expired_materializing(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="visible-worker",
    )
    now = datetime.now(UTC)
    async with factory() as db:
        expired = await db.get(Session, "session-alice")
        active = await db.get(Session, "session-bob")
        assert expired is not None and active is not None
        expired_state = await materializer._ensure_state(db, expired, 0)
        expired_state.state = "materializing"
        expired_state.lease_owner = "stale"
        expired_state.lease_expires_at = now - timedelta(seconds=1)
        active_state = await materializer._ensure_state(db, active, 0)
        active_state.state = "materializing"
        active_state.lease_owner = "active-worker"
        active_state.lease_expires_at = now + timedelta(minutes=1)
        await db.commit()

    await materializer.prioritize_sessions([expired, active])
    async with factory() as db:
        expired_state = await db.get(WorkSessionProjectionRow, expired_state.projection_id)
        active_state = await db.get(WorkSessionProjectionRow, active_state.projection_id)
        assert expired_state is not None and active_state is not None
        assert expired_state.state == "repair"
        assert active_state.state == "materializing"
        assert active_state.lease_owner == "active-worker"
    claimed = await materializer._claim()
    assert [row.session_id for row in claimed] == [expired.session_id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_visible_refresh_uses_urgent_lane_while_two_historical_repairs_are_slow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.api.chat_v2.work_materializer.WORK_REPAIR_PAGE_SIZE",
        1,
    )
    owners = ("history-a", "history-b", "visible")
    engine, factory = await _database(tmp_path, owners=owners)
    blocked_sources = {"intaris-history-a", "intaris-history-b"}
    store = BlockingAuthorityStore(
        {
            "intaris-history-a": [
                _event("intaris-history-a", 1, "user_message", {"content": "old a"})
            ],
            "intaris-history-b": [
                _event("intaris-history-b", 1, "user_message", {"content": "old b"})
            ],
            "intaris-visible": [
                _event("intaris-visible", 1, "user_message", {"content": "new"}),
                _event(
                    "intaris-visible",
                    2,
                    "tool_call",
                    {
                        "call_id": "visible-command",
                        "name": "bash",
                        "arguments": {"command": "date"},
                    },
                ),
            ],
        },
        blocked_session_ids=blocked_sources,
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
        worker_id="urgent-controller",
    )
    async with factory() as db:
        for owner in owners[:2]:
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await materializer._ensure_state(db, row, 1)
            state.state = "repair"
            state.updated_at = datetime.now(UTC) - timedelta(hours=1)
        visible = await db.get(Session, "session-visible")
        assert visible is not None
        visible_state = await materializer._ensure_state(db, visible, 0)
        visible_state.state = "caught_up"
        visible_state.next_head_check_at = datetime.now(UTC) + timedelta(hours=1)
        await db.commit()

    materializer.start()
    try:
        async with asyncio.timeout(2):
            await store.all_blocked.wait()
        async with factory() as db:
            active_before_refresh = int(
                await db.scalar(
                    select(func.count())
                    .select_from(WorkSessionProjectionRow)
                    .where(WorkSessionProjectionRow.lease_owner.is_not(None))
                )
                or 0
            )
        assert active_before_refresh == 2

        await materializer.prioritize_sessions([visible])
        assert "intaris-visible" not in store.read_session_ids

        async with asyncio.timeout(2):
            while True:
                async with factory() as db:
                    visible_state = await db.scalar(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.session_id == "session-visible"
                        )
                    )
                    records = list(
                        (
                            await db.scalars(
                                select(WorkRecordRow).where(
                                    WorkRecordRow.session_id == "session-visible"
                                )
                            )
                        ).all()
                    )
                if (
                    visible_state is not None
                    and visible_state.state == "caught_up"
                    and any(
                        record.timeline_item.get("call_id") == "visible-command"
                        for record in records
                    )
                ):
                    break
                await asyncio.sleep(0.01)

        assert not store.release.is_set()
        async with factory() as db:
            slow_states = list(
                (
                    await db.scalars(
                        select(WorkSessionProjectionRow).where(
                            WorkSessionProjectionRow.session_id.in_(
                                ["session-history-a", "session-history-b"]
                            )
                        )
                    )
                ).all()
            )
        assert {state.state for state in slow_states} == {"materializing"}
    finally:
        store.release.set()
        await materializer.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_urgent_lane_rotates_stale_siblings_while_root_keeps_appending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cognis.api.chat_v2.work_materializer.WORK_REPAIR_PAGE_SIZE",
        1,
    )
    owners = ("history-a", "history-b", "root", "sibling-a", "sibling-b")
    engine, factory = await _database(tmp_path, owners=owners)
    blocked_sources = {"intaris-history-a", "intaris-history-b"}
    store = BlockingAuthorityStore(
        {
            "intaris-history-a": [
                _event("intaris-history-a", 1, "user_message", {"content": "old a"})
            ],
            "intaris-history-b": [
                _event("intaris-history-b", 1, "user_message", {"content": "old b"})
            ],
            "intaris-root": [],
            "intaris-sibling-a": [
                _event(
                    "intaris-sibling-a",
                    1,
                    "tool_call",
                    {
                        "call_id": "sibling-a-command",
                        "name": "bash",
                        "arguments": {"command": "date"},
                    },
                ),
                _event(
                    "intaris-sibling-a",
                    2,
                    "tool_result",
                    {
                        "call_id": "sibling-a-command",
                        "name": "bash",
                        "status": "complete",
                        "content": "ok",
                    },
                ),
            ],
            "intaris-sibling-b": [
                _event(
                    "intaris-sibling-b",
                    1,
                    "tool_call",
                    {
                        "call_id": "sibling-b-command",
                        "name": "bash",
                        "arguments": {"command": "date"},
                    },
                ),
                _event(
                    "intaris-sibling-b",
                    2,
                    "tool_result",
                    {
                        "call_id": "sibling-b-command",
                        "name": "bash",
                        "status": "complete",
                        "content": "ok",
                    },
                ),
            ],
        },
        blocked_session_ids=blocked_sources,
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {
            "bash": ToolDefinition(
                name="bash",
                description="Run a command",
                source=ToolSource(type="skill"),
                read_only=False,
                category="shell",
            )
        },
        worker_id="fair-urgent-controller",
    )
    async with factory() as db:
        for owner in owners[:2]:
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await materializer._ensure_state(db, row, 1)
            state.state = "repair"
            state.updated_at = datetime.now(UTC) - timedelta(hours=1)
        visible_rows = [await db.get(Session, f"session-{owner}") for owner in owners[2:]]
        assert all(row is not None for row in visible_rows)
        for row in visible_rows:
            assert row is not None
            state = await materializer._ensure_state(db, row, 0)
            state.state = "caught_up"
            state.next_head_check_at = datetime.now(UTC) + timedelta(hours=1)
        await db.commit()

    siblings_caught_up = asyncio.Event()
    latest_root_seq = 0
    latest_root_call_id = ""

    async def append_root_until_siblings_finish() -> None:
        nonlocal latest_root_call_id, latest_root_seq
        while not siblings_caught_up.is_set():
            latest_root_call_id = f"root-command-{(latest_root_seq // 2) + 1}"
            store.pages["intaris-root"].extend(
                [
                    _event(
                        "intaris-root",
                        latest_root_seq + 1,
                        "tool_call",
                        {
                            "call_id": latest_root_call_id,
                            "name": "bash",
                            "arguments": {"command": "date"},
                        },
                    ),
                    _event(
                        "intaris-root",
                        latest_root_seq + 2,
                        "tool_result",
                        {
                            "call_id": latest_root_call_id,
                            "name": "bash",
                            "status": "complete",
                            "content": "ok",
                        },
                    ),
                ]
            )
            latest_root_seq += 2
            assert await materializer.mark_source_pending(
                owner_email="root@example.com",
                source_session_id="intaris-root",
                target_seq=latest_root_seq,
            )
            await asyncio.sleep(0.01)

    materializer.start()
    appender: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            await store.all_blocked.wait()
        await materializer.prioritize_sessions([row for row in visible_rows if row is not None])
        appender = asyncio.create_task(append_root_until_siblings_finish())

        deadline = monotonic() + 5
        while True:
            async with factory() as db:
                sibling_states = list(
                    (
                        await db.scalars(
                            select(WorkSessionProjectionRow).where(
                                WorkSessionProjectionRow.session_id.in_(
                                    ["session-sibling-a", "session-sibling-b"]
                                )
                            )
                        )
                    ).all()
                )
                sibling_calls = {
                    str(record.timeline_item.get("call_id"))
                    for record in (
                        await db.scalars(
                            select(WorkRecordRow).where(
                                WorkRecordRow.session_id.in_(
                                    ["session-sibling-a", "session-sibling-b"]
                                ),
                                WorkRecordRow.is_evidence.is_(True),
                            )
                        )
                    ).all()
                }
            if {state.state for state in sibling_states} == {"caught_up"} and {
                "sibling-a-command",
                "sibling-b-command",
            } <= sibling_calls:
                siblings_caught_up.set()
                break
            assert monotonic() < deadline, {
                "states": [
                    (
                        state.session_id,
                        state.state,
                        state.priority,
                        state.lease_owner,
                        state.target_seq,
                        state.covered_through_seq,
                    )
                    for state in sibling_states
                ],
                "calls": sibling_calls,
            }
            await asyncio.sleep(0.02)
        assert latest_root_seq >= 3
        assert latest_root_call_id
        assert appender is not None
        await appender

        async with factory() as db:
            root_state = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == "session-root"
                )
            )
        assert root_state is not None
        assert root_state.target_seq == latest_root_seq
        assert 0 < root_state.covered_through_seq <= latest_root_seq
        assert not store.release.is_set()
    finally:
        siblings_caught_up.set()
        if appender is not None:
            await appender
        store.release.set()
        await materializer.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_urgent_repair_bypasses_saturated_background_event_read_admission(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAdmission:
        async def run(self, operation: Any) -> Any:
            started.set()
            await release.wait()
            return await operation()

    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        event_read_admission=BlockingAdmission(),  # type: ignore[arg-type]
    )
    normal = asyncio.create_task(materializer._run_event_read(lambda: asyncio.sleep(0)))
    await started.wait()
    assert (
        await asyncio.wait_for(
            materializer._run_event_read(
                lambda: asyncio.sleep(0, result="urgent"),
                urgent=True,
            ),
            timeout=0.2,
        )
        == "urgent"
    )
    release.set()
    await normal
    await engine.dispose()


@pytest.mark.asyncio
async def test_urgent_lane_is_globally_capped_and_exact_across_controllers(
    tmp_path: Path,
) -> None:
    owners = ("normal-a", "normal-b", "urgent-a", "urgent-b")
    engine, factory = await _database(tmp_path, owners=owners)
    first = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="controller-a",
    )
    second = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="controller-b",
    )
    urgent_projection_ids: dict[str, str] = {}
    async with factory() as db:
        for owner in owners[:2]:
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await first._ensure_state(db, row, 1)
            state.state = "repair"
        for owner in owners[2:]:
            row = await db.get(Session, f"session-{owner}")
            assert row is not None
            state = await first._ensure_state(db, row, 0)
            state.state = "caught_up"
            state.next_head_check_at = datetime.now(UTC) + timedelta(hours=1)
            urgent_projection_ids[owner] = state.projection_id
        await db.commit()

    assert len(await first._claim()) == 2
    async with factory() as db:
        urgent_a = await db.get(
            WorkSessionProjectionRow,
            urgent_projection_ids["urgent-a"],
        )
        urgent_b = await db.get(
            WorkSessionProjectionRow,
            urgent_projection_ids["urgent-b"],
        )
        assert urgent_a is not None and urgent_b is not None
        urgent_a.state = urgent_b.state = "repair"
        urgent_a.priority = WORK_VISIBLE_REFRESH_PRIORITY
        urgent_b.priority = WORK_VISIBLE_REFRESH_PRIORITY + 1
        urgent_a.next_head_check_at = urgent_b.next_head_check_at = datetime.now(UTC)
        await db.commit()

    urgent_claim = await first._claim_urgent(urgent_projection_ids["urgent-a"])
    duplicate_lane_claim = await second._claim_urgent(urgent_projection_ids["urgent-b"])

    assert urgent_claim is not None
    assert urgent_claim.session_id == "session-urgent-a"
    assert duplicate_lane_claim is None
    async with factory() as db:
        active = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(WorkSessionProjectionRow.lease_owner.is_not(None))
            )
            or 0
        )
        urgent_active = int(
            await db.scalar(
                select(func.count())
                .select_from(WorkSessionProjectionRow)
                .where(WorkSessionProjectionRow.lease_owner.like("work-urgent:%"))
            )
            or 0
        )
    assert active == WORK_MAX_REPAIR_CONCURRENCY == 3
    assert urgent_active == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_visible_prioritization_coalesces_one_urgent_claim(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    async with factory() as db:
        visible = await db.get(Session, "session-alice")
        assert visible is not None

    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    claim_calls = 0

    async def blocked_claim(projection_id: str) -> WorkSessionProjectionRow | None:
        nonlocal claim_calls
        assert projection_id
        claim_calls += 1
        claim_started.set()
        await release_claim.wait()
        return None

    materializer._claim_urgent = blocked_claim  # type: ignore[method-assign]
    materializer._still_urgent_projection_ids = AsyncMock(  # type: ignore[method-assign]
        return_value=set()
    )
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    try:
        await materializer.prioritize_sessions([visible])
        async with asyncio.timeout(1):
            await claim_started.wait()
        urgent_task = materializer._urgent_task

        await materializer.prioritize_sessions([visible])

        assert materializer._urgent_task is urgent_task
        assert len(materializer._urgent_pending) == 1
        assert claim_calls == 1
        release_claim.set()
        assert urgent_task is not None
        await urgent_task
        assert claim_calls == 1
    finally:
        release_claim.set()
        await materializer.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_during_urgent_completion_schedules_one_fenced_follow_up(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    store = AuthorityStore(
        {"intaris-alice": [_event("intaris-alice", 1, "user_message", {"content": "visible"})]}
    )
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=store,
        tool_definitions=lambda: {},
    )
    async with factory() as db:
        visible = await db.get(Session, "session-alice")
        assert visible is not None

    continuation_started = asyncio.Event()
    release_continuation = asyncio.Event()
    continuation_calls = 0
    original_continue = materializer._continue_urgent_claim

    async def gated_continue(claim: Any) -> bool:
        nonlocal continuation_calls
        continuation_calls += 1
        if continuation_calls == 1:
            continuation_started.set()
            await release_continuation.wait()
        return await original_continue(claim)

    materializer._continue_urgent_claim = gated_continue  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    try:
        await materializer.prioritize_sessions([visible])
        async with asyncio.timeout(2):
            await continuation_started.wait()
        urgent_task = materializer._urgent_task
        assert urgent_task is not None

        await materializer.prioritize_sessions([visible])
        assert not materializer._urgent_pending

        release_continuation.set()
        async with asyncio.timeout(2):
            await urgent_task

        assert continuation_calls == 2
        assert len(store.authorities) == 2
        async with factory() as db:
            state = await db.scalar(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id == visible.session_id
                )
            )
            assert state is not None
            assert state.state == "caught_up"
            assert state.priority == 0
    finally:
        release_continuation.set()
        await materializer.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_cancels_urgent_repair_and_releases_its_fenced_lease(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="shutdown-controller",
    )
    async with factory() as db:
        visible = await db.get(Session, "session-alice")
        assert visible is not None

    repair_started = asyncio.Event()
    repair_cancelled = asyncio.Event()

    async def blocked_repair(
        projection_id: str,
        *,
        lease_owner: str | None = None,
        repair_slot: asyncio.Semaphore | None = None,
    ) -> None:
        assert projection_id
        assert lease_owner == materializer._urgent_worker_id
        assert repair_slot is materializer._urgent_repair_slot
        repair_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            repair_cancelled.set()

    materializer._repair = blocked_repair  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await materializer.prioritize_sessions([visible])
    async with asyncio.timeout(1):
        await repair_started.wait()

    await materializer.stop()

    assert repair_cancelled.is_set()
    assert materializer._urgent_task is None
    async with factory() as db:
        state = await db.scalar(
            select(WorkSessionProjectionRow).where(
                WorkSessionProjectionRow.session_id == visible.session_id
            )
        )
        assert state is not None
        assert state.state == "repair"
        assert state.priority == WORK_VISIBLE_REFRESH_PRIORITY
        assert state.lease_owner is None
        assert state.lease_expires_at is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_does_not_release_a_reclaimed_urgent_fence(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="old-controller",
    )
    async with factory() as db:
        visible = await db.get(Session, "session-alice")
        assert visible is not None

    repair_started = asyncio.Event()

    async def blocked_repair(
        projection_id: str,
        *,
        lease_owner: str | None = None,
        repair_slot: asyncio.Semaphore | None = None,
    ) -> None:
        assert projection_id
        assert lease_owner == materializer._urgent_worker_id
        assert repair_slot is materializer._urgent_repair_slot
        repair_started.set()
        await asyncio.Event().wait()

    materializer._repair = blocked_repair  # type: ignore[method-assign]
    materializer._task = asyncio.create_task(asyncio.Event().wait())
    materializer.start()
    await materializer.prioritize_sessions([visible])
    async with asyncio.timeout(1):
        await repair_started.wait()
    claim = materializer._urgent_active_claim
    assert claim is not None

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claim.projection_id)
        assert state is not None
        state.lease_owner = "work-urgent:new-controller"
        state.lease_fence = claim.lease_fence + 1
        state.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await db.commit()

    await materializer.stop()

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, claim.projection_id)
        assert state is not None
        assert state.lease_owner == "work-urgent:new-controller"
        assert state.lease_fence == claim.lease_fence + 1
        assert state.lease_expires_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_caught_up_projection_does_not_schedule_periodic_head_checks(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 0)
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[],
            target_seq=0,
        )
        assert state.next_head_check_at is None

        row.status = "completed"
        await materializer._materialize_batch(
            db,
            row=row,
            state=state,
            raw_events=[],
            target_seq=0,
        )
        assert state.next_head_check_at is None
        state.next_head_check_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await materializer._claim() == []
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
async def test_cursor_survives_head_check_fence_and_late_materialization(
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
        assert len(page.before_cursor) < 2000
        cursor_payload = _unsign(page.before_cursor, "secret")
        assert "snapshot_at" in cursor_payload
        assert "snapshot" not in cursor_payload
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
        late_item = ToolCallTimelineItem(
            id="tool:late",
            call_id="call-late",
            tool_name="write",
            sort_key="late",
            status="complete",
        )
        db.add(
            WorkRecordRow(
                work_record_id="record-late",
                owner_email=row.user_email,
                session_id=row.session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                source_store="intaris",
                source_session_id="intaris-alice",
                source_seq=3,
                source_item_id=late_item.id,
                item_ordinal=0,
                occurred_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
                record_type=late_item.kind,
                call_id=late_item.call_id,
                timeline_item=late_item.model_dump(mode="json"),
            )
        )
        state.covered_through_seq = 3
        state.target_seq = 3
        await db.commit()
        stable = await read_work_page(
            db,
            owner_email=row.user_email,
            scope=scope,
            session_rows=[row],
            graph_fingerprint="graph",
            cursor_secret="secret",
            before=page.before_cursor,
            limit=1,
        )
        assert len(stable.items) == 1
        assert isinstance(stable.items[0], ToolCallTimelineItem)
        assert stable.items[0].call_id == "call-1"
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
async def test_sessionless_activity_overview_is_empty_and_successful(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    scope = TimelineScope(
        key="conversation:sessionless",
        kind="conversation",
        conversation_id="sessionless",
    )
    async with factory() as db:
        overview = await read_activity_overview(
            db,
            owner_email="owner@example.com",
            scope=scope,
            session_rows=[],
            workstreams=[],
            graph_fingerprint="empty-graph",
            graph_truncated=False,
        )

    assert overview.workstreams == []
    assert overview.summary == _empty_summary()
    assert overview.materialization.state == "live"
    assert overview.graph_truncated is False
    assert overview.recent == {}
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
                    timeline_item=(
                        item.model_dump(mode="json") | {"activity_scope_id": "legacy-scope"}
                        if seq == 2
                        else item.model_dump(mode="json")
                    ),
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
async def test_set_based_category_summary_matches_exact_filtered_semantics(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    snapshot_at = datetime(2026, 1, 2, tzinfo=UTC)
    from_time = datetime(2026, 1, 1, tzinfo=UTC)

    def record(
        record_id: str,
        *,
        session_id: str,
        category: str,
        entity_id: str | None = None,
        occurred_at: datetime = from_time,
        materialized_at: datetime = from_time,
        is_evidence: bool = True,
    ) -> WorkRecordRow:
        return WorkRecordRow(
            work_record_id=record_id,
            owner_email=("bob@example.com" if session_id == "session-bob" else "alice@example.com"),
            session_id=session_id,
            materializer_version=WORK_MATERIALIZER_VERSION,
            source_store="intaris",
            source_session_id=f"source-{session_id}",
            source_seq=int(hashlib.sha256(record_id.encode()).hexdigest()[:8], 16),
            source_item_id=f"tool:{record_id}",
            item_ordinal=0,
            occurred_at=occurred_at,
            record_type="tool_pair",
            category=category,
            entity_id=entity_id,
            is_evidence=is_evidence,
            call_id=record_id,
            timeline_item={
                "kind": "tool_call",
                "id": f"tool:{record_id}",
                "sort_key": record_id,
                "call_id": record_id,
                "tool_name": "bash",
                "status": "complete",
            },
            materialized_at=materialized_at,
        )

    async with factory() as db:
        base = await db.get(Session, "session-alice")
        assert base is not None
        rotation = Session(
            session_id="session-alice-rotation",
            conversation_id=base.conversation_id,
            user_email=base.user_email,
            agent_id=base.agent_id,
            intaris_session_id="intaris-alice-rotation",
            previous_session_id=base.session_id,
            activity_scope_id=base.activity_scope_id,
            delegation_metadata={},
        )
        db.add(rotation)
        db.add_all(
            [
                ArtifactRecordRow(
                    artifact_id="artifact-active",
                    namespace="test",
                    object_id="active",
                    filename="active.txt",
                    owner_email="alice@example.com",
                    mime_type="text/plain",
                    status="ready",
                ),
                ArtifactRecordRow(
                    artifact_id="artifact-foreign",
                    namespace="test",
                    object_id="foreign",
                    filename="foreign.txt",
                    owner_email="bob@example.com",
                    mime_type="text/plain",
                    status="ready",
                ),
                ArtifactRecordRow(
                    artifact_id="artifact-deleted",
                    namespace="test",
                    object_id="deleted",
                    filename="deleted.txt",
                    owner_email="alice@example.com",
                    mime_type="text/plain",
                    status="ready",
                    deleted_at=from_time,
                ),
            ]
        )
        records = [
            record("command-old", session_id=base.session_id, category="commands"),
            record("command-new", session_id=rotation.session_id, category="commands"),
            record("mutation", session_id=rotation.session_id, category="mutations"),
            record(
                "deliverable-old",
                session_id=base.session_id,
                category="deliverables",
                entity_id="deliverable-shared",
            ),
            record(
                "deliverable-new",
                session_id=rotation.session_id,
                category="deliverables",
                entity_id="deliverable-shared",
            ),
            record(
                "artifact-active-old",
                session_id=base.session_id,
                category="artifacts",
                entity_id="artifact-active",
            ),
            record(
                "artifact-active-new",
                session_id=rotation.session_id,
                category="artifacts",
                entity_id="artifact-active",
            ),
            record(
                "artifact-foreign-record",
                session_id=rotation.session_id,
                category="artifacts",
                entity_id="artifact-foreign",
            ),
            record(
                "artifact-deleted-record",
                session_id=rotation.session_id,
                category="artifacts",
                entity_id="artifact-deleted",
            ),
            record("file-old", session_id=base.session_id, category="files"),
            record("file-new", session_id=rotation.session_id, category="files"),
            record(
                "late-command",
                session_id=rotation.session_id,
                category="commands",
                materialized_at=snapshot_at + timedelta(seconds=1),
            ),
            record(
                "old-command",
                session_id=base.session_id,
                category="commands",
                occurred_at=from_time - timedelta(seconds=1),
            ),
            record(
                "removed-command",
                session_id=base.session_id,
                category="commands",
                is_evidence=False,
            ),
            record("foreign-command", session_id="session-bob", category="commands"),
        ]
        db.add_all(records)
        await db.flush()
        db.add_all(
            [
                WorkRecordFileRow(
                    work_record_file_id="file-old-shared",
                    owner_email=base.user_email,
                    session_id=base.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    work_record_id="file-old",
                    file_ordinal=0,
                    path="src/shared.py",
                    path_id="repo:src/shared.py",
                    path_generation_id="shared-old",
                    source_seq=1,
                    item_ordinal=0,
                    additions=2,
                    deletions=1,
                ),
                WorkRecordFileRow(
                    work_record_file_id="file-new-shared",
                    owner_email=base.user_email,
                    session_id=rotation.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    work_record_id="file-new",
                    file_ordinal=0,
                    path="src/shared.py",
                    path_id="repo:src/shared.py",
                    path_generation_id="shared-new",
                    source_seq=1,
                    item_ordinal=0,
                    additions=3,
                    deletions=2,
                ),
                WorkRecordFileRow(
                    work_record_file_id="file-new-other",
                    owner_email=base.user_email,
                    session_id=rotation.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    work_record_id="file-new",
                    file_ordinal=1,
                    path="src/other.py",
                    path_id="repo:src/other.py",
                    path_generation_id="other",
                    source_seq=1,
                    item_ordinal=0,
                    additions=4,
                    deletions=0,
                ),
            ]
        )
        await db.commit()

        statement = select(WorkRecordRow).where(
            WorkRecordRow.owner_email == base.user_email,
            WorkRecordRow.session_id.in_([base.session_id, rotation.session_id]),
            WorkRecordRow.materializer_version == WORK_MATERIALIZER_VERSION,
            WorkRecordRow.is_evidence.is_(True),
            WorkRecordRow.occurred_at >= from_time,
            WorkRecordRow.materialized_at <= snapshot_at,
        )
        query_count = 0

        def count_query(*_args: Any) -> None:
            nonlocal query_count
            query_count += 1

        sa_event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        try:
            summary = await _category_summary(
                db,
                statement=statement,
                owner_email=base.user_email,
            )
        finally:
            sa_event.remove(engine.sync_engine, "before_cursor_execute", count_query)

        legacy_base = statement.with_only_columns(
            WorkRecordRow.category,
            WorkRecordRow.entity_id,
        ).subquery()
        legacy_counts = dict(
            (
                await db.execute(
                    select(legacy_base.c.category, func.count())
                    .where(legacy_base.c.category.in_(["commands", "mutations"]))
                    .group_by(legacy_base.c.category)
                )
            ).all()
        )
        legacy_deliverables = int(
            await db.scalar(
                select(func.count(func.distinct(legacy_base.c.entity_id))).where(
                    legacy_base.c.category == "deliverables",
                    legacy_base.c.entity_id.is_not(None),
                )
            )
            or 0
        )
        legacy_artifacts = int(
            await db.scalar(
                select(func.count(func.distinct(legacy_base.c.entity_id)))
                .select_from(
                    legacy_base.join(
                        ArtifactRecordRow,
                        and_(
                            ArtifactRecordRow.artifact_id == legacy_base.c.entity_id,
                            ArtifactRecordRow.owner_email == base.user_email,
                            ArtifactRecordRow.deleted_at.is_(None),
                        ),
                    )
                )
                .where(legacy_base.c.category == "artifacts")
            )
            or 0
        )
        file_record_ids = statement.with_only_columns(WorkRecordRow.work_record_id).where(
            WorkRecordRow.category == "files"
        )
        legacy_changed_files = int(
            await db.scalar(
                select(func.count(func.distinct(WorkRecordFileRow.path_id))).where(
                    WorkRecordFileRow.work_record_id.in_(file_record_ids)
                )
            )
            or 0
        )
        legacy_file_totals = (
            await db.execute(
                select(
                    func.coalesce(func.sum(WorkRecordFileRow.additions), 0),
                    func.coalesce(func.sum(WorkRecordFileRow.deletions), 0),
                ).where(WorkRecordFileRow.work_record_id.in_(file_record_ids))
            )
        ).one()
        legacy = WorkSummary(
            mutations=int(legacy_counts.get("mutations", 0)),
            commands=int(legacy_counts.get("commands", 0)),
            changed_files=legacy_changed_files,
            artifacts=legacy_artifacts,
            deliverables=legacy_deliverables,
            additions=int(legacy_file_totals[0]),
            deletions=int(legacy_file_totals[1]),
            omitted_files=0,
        )

        assert query_count == 1
        assert summary == legacy
        assert summary == WorkSummary(
            mutations=1,
            commands=2,
            changed_files=2,
            artifacts=1,
            deliverables=1,
            additions=9,
            deletions=3,
            omitted_files=0,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_removed_call_ids_preserve_latest_materialized_state_across_rotations(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path, owners=("alice", "bob"))
    started = datetime(2026, 1, 1, tzinfo=UTC)

    def call_state(
        record_id: str,
        *,
        call_id: str,
        session_id: str,
        is_evidence: bool,
        occurred_offset: int,
        materialized_offset: int,
    ) -> WorkRecordRow:
        return WorkRecordRow(
            work_record_id=record_id,
            owner_email=("bob@example.com" if session_id == "session-bob" else "alice@example.com"),
            session_id=session_id,
            materializer_version=WORK_MATERIALIZER_VERSION,
            source_store="intaris",
            source_session_id=f"source-{session_id}",
            source_seq=int(hashlib.sha256(record_id.encode()).hexdigest()[:8], 16),
            source_item_id=f"tool:{record_id}",
            item_ordinal=0,
            occurred_at=started + timedelta(seconds=occurred_offset),
            record_type="tool_pair",
            category="commands",
            is_evidence=is_evidence,
            call_id=call_id,
            timeline_item={
                "kind": "tool_call",
                "id": f"tool:{record_id}",
                "sort_key": record_id,
                "call_id": call_id,
                "tool_name": "bash",
                "status": "complete",
            },
            materialized_at=started + timedelta(seconds=materialized_offset),
        )

    async with factory() as db:
        base = await db.get(Session, "session-alice")
        assert base is not None
        rotation = Session(
            session_id="session-alice-rotation",
            conversation_id=base.conversation_id,
            user_email=base.user_email,
            agent_id=base.agent_id,
            intaris_session_id="intaris-alice-rotation",
            previous_session_id=base.session_id,
            activity_scope_id=base.activity_scope_id,
            delegation_metadata={},
        )
        db.add(rotation)
        db.add_all(
            [
                call_state(
                    "removed-add",
                    call_id="removed",
                    session_id=base.session_id,
                    is_evidence=True,
                    occurred_offset=30,
                    materialized_offset=10,
                ),
                call_state(
                    "removed-tombstone",
                    call_id="removed",
                    session_id=rotation.session_id,
                    is_evidence=False,
                    occurred_offset=10,
                    materialized_offset=30,
                ),
                call_state(
                    "readded-add",
                    call_id="readded",
                    session_id=base.session_id,
                    is_evidence=True,
                    occurred_offset=30,
                    materialized_offset=10,
                ),
                call_state(
                    "readded-tombstone",
                    call_id="readded",
                    session_id=rotation.session_id,
                    is_evidence=False,
                    occurred_offset=20,
                    materialized_offset=20,
                ),
                call_state(
                    "readded-final",
                    call_id="readded",
                    session_id=rotation.session_id,
                    is_evidence=True,
                    occurred_offset=10,
                    materialized_offset=30,
                ),
                call_state(
                    "foreign-tombstone",
                    call_id="foreign",
                    session_id="session-bob",
                    is_evidence=False,
                    occurred_offset=40,
                    materialized_offset=40,
                ),
            ]
        )
        await db.commit()

        removed = await _removed_call_ids(
            db,
            owner_email=base.user_email,
            session_ids=[base.session_id, rotation.session_id],
        )

        assert removed == ["removed"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_initial_category_page_snapshot_excludes_concurrent_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database(tmp_path)
    snapshot_at = datetime(2026, 1, 2, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return snapshot_at if tz is not None else snapshot_at.replace(tzinfo=None)

    monkeypatch.setattr("cognis.api.chat_v2.work_repository.datetime", FrozenDateTime)

    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        db.add(
            WorkSessionProjectionRow(
                projection_id="projection-snapshot",
                owner_email=row.user_email,
                session_id=row.session_id,
                source_session_id=row.intaris_session_id,
                materializer_version=WORK_MATERIALIZER_VERSION,
                target_seq=2,
                covered_through_seq=2,
                state="caught_up",
            )
        )
        for record_id, materialized_at in (
            ("before-snapshot", snapshot_at - timedelta(microseconds=1)),
            ("concurrent-write", snapshot_at + timedelta(microseconds=1)),
        ):
            db.add(
                WorkRecordRow(
                    work_record_id=record_id,
                    owner_email=row.user_email,
                    session_id=row.session_id,
                    materializer_version=WORK_MATERIALIZER_VERSION,
                    source_store="intaris",
                    source_session_id=row.intaris_session_id,
                    source_seq=1 if record_id == "before-snapshot" else 2,
                    source_item_id=f"tool:{record_id}",
                    item_ordinal=0,
                    occurred_at=snapshot_at - timedelta(seconds=1),
                    record_type="tool_pair",
                    category="commands",
                    is_evidence=True,
                    call_id=record_id,
                    timeline_item={
                        "kind": "tool_call",
                        "id": f"tool:{record_id}",
                        "sort_key": record_id,
                        "call_id": record_id,
                        "tool_name": "bash",
                        "status": "complete",
                    },
                    materialized_at=materialized_at,
                )
            )
        await db.commit()

        query_count = 0

        def count_query(*_args: Any) -> None:
            nonlocal query_count
            query_count += 1

        sa_event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        try:
            page = await read_work_page(
                db,
                owner_email=row.user_email,
                scope=TimelineScope(
                    key=f"session:{row.session_id}",
                    kind="session",
                    session_id=row.session_id,
                ),
                session_rows=[row],
                graph_fingerprint="snapshot-graph",
                cursor_secret="secret",
                before=None,
                limit=10,
                category="commands",
            )
        finally:
            sa_event.remove(engine.sync_engine, "before_cursor_execute", count_query)

        assert [item.call_id for item in page.items if isinstance(item, ToolCallTimelineItem)] == [
            "before-snapshot"
        ]
        assert page.summary is not None
        assert page.summary.commands == 1
        assert query_count <= 5
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
                    timeline_item=(
                        item.model_dump(mode="json") | {"activity_scope_id": "legacy-scope"}
                        if seq == 1006
                        else item.model_dump(mode="json")
                    ),
                    **_record_metadata(item, definitions),
                )
            )
            if is_file:
                file_rows.append(
                    WorkRecordFileRow(
                        work_record_file_id=f"category-file-{seq}",
                        owner_email=row.user_email,
                        session_id=row.session_id,
                        materializer_version=WORK_MATERIALIZER_VERSION,
                        work_record_id=f"category-record-{seq}",
                        file_ordinal=0,
                        path=f"file-{seq}.py",
                        path_id=f"root:file-{seq}.py",
                        path_generation_id=f"root:file-{seq}.py:0",
                        source_seq=seq,
                        item_ordinal=0,
                        additions=1,
                        deletions=0,
                    )
                )
        await db.flush()
        db.add_all(file_rows)
        await db.flush()
        await rebuild_session_current_files(
            db,
            owner_email=row.user_email,
            session_id=row.session_id,
            materializer_version=WORK_MATERIALIZER_VERSION,
        )
        projection_state = await db.get(
            WorkSessionProjectionRow,
            "projection-categories",
        )
        assert projection_state is not None
        await WorkMaterializer(
            session_factory=factory,
            event_store=AuthorityStore({}),
            tool_definitions=lambda: definitions,
        )._refresh_projection_counters(db, state=projection_state)
        await db.commit()
        assert projection_state.command_count == 1002
        assert projection_state.mutation_count == 0

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
        assert overview.workstreams[0].model is None
        assert overview.workstreams[0].reasoning_effort is None
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
        assert overview_query_count <= 16
        db.add(
            DirectTurnRequestRow(
                request_id="dtr-overview-old-profile",
                turn_id="turn-overview-old-profile",
                conversation_id=row.conversation_id,
                session_id=row.session_id,
                agent_id=row.agent_id,
                user_id=row.user_email,
                idempotency_scope="overview",
                idempotency_key="overview-old-profile",
                admission_hash="admission",
                payload_hash="payload",
                payload={
                    "schema_version": 1,
                    "content": "Previous",
                    "attachments": [],
                    "metadata": {"channel_default_agent_profile_id": "old"},
                },
                status="completed",
            )
        )
        await db.flush()
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
            "old": {"reasoning_effort": "low", "model": "old-model"},
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
        for workstream in many_overview.workstreams[:3]:
            assert workstream.agent_profile_id is None
            assert workstream.reasoning_effort is None
            assert workstream.model is None

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
        assert query_count <= 4

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
        repair_idle_check_seconds=0.01,
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
async def test_retention_runs_after_startup_delay_and_not_on_repair_wakes(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        repair_idle_check_seconds=0.01,
        retention_initial_delay_seconds=0.05,
        retention_interval_seconds=60,
    )
    retained = asyncio.Event()
    scrub = AsyncMock(side_effect=lambda: retained.set())
    materializer._scrub_source_content = scrub  # type: ignore[method-assign]

    materializer.start()
    materializer.wake()
    await asyncio.sleep(0.02)
    scrub.assert_not_awaited()
    async with asyncio.timeout(1):
        await retained.wait()
    await materializer.stop()

    scrub.assert_awaited_once()
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
                async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
                    return SessionWatermark(
                        store_id="intaris",
                        session_id=session_id,
                        last_seq=2,
                        availability=SessionHistoryAvailability(
                            durable_last_seq=2,
                            first_available_seq=1,
                        ),
                    )

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
                        availability=SessionHistoryAvailability(
                            durable_last_seq=2,
                            first_available_seq=1,
                        ),
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


@pytest.mark.asyncio
async def test_repair_converges_when_live_target_advances_during_event_read(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    page_started = asyncio.Event()
    release_page = asyncio.Event()

    class AdvancingStore(AuthorityStore):
        def bind(self, authority: EventStoreAuthority) -> Any:
            reader = super().bind(authority)

            class Reader:
                async def read_session_high_watermark(self, *, session_id: str) -> SessionWatermark:
                    return await reader.read_session_high_watermark(session_id=session_id)

                async def read_session_events(
                    self,
                    *,
                    session_id: str,
                    after_seq: int,
                    limit: int,
                    direction: str,
                ) -> SessionEventPage:
                    page_started.set()
                    await release_page.wait()
                    return await reader.read_session_events(
                        session_id=session_id,
                        after_seq=after_seq,
                        limit=limit,
                        direction=direction,
                    )

            return Reader()

    store = AdvancingStore(
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
        worker_id="repair-worker",
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 2)
        state.state = "repair"
        state.last_error = "noncontiguous live append"
        await db.commit()
        projection_id = state.projection_id

    claimed = await materializer._claim()
    assert [state.projection_id for state in claimed] == [projection_id]
    repair = asyncio.create_task(materializer._repair(projection_id))
    await page_started.wait()

    store.pages["intaris-alice"].append(
        _event("intaris-alice", 3, "user_message", {"content": "three"})
    )
    await materializer._process_append(
        _PendingWorkAppend(
            authority=EventStoreAuthority(
                user_email="alice@example.com",
                agent_id="agent-alice",
                agent_owner_email="alice@example.com",
            ),
            session_id="intaris-alice",
            first_seq=3,
            last_seq=3,
            target_seq=3,
            events=(SessionEvent(type="user_message", data={"content": "three"}),),
            payload_bytes=16,
        )
    )
    release_page.set()
    await repair

    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, projection_id)
        assert state is not None
        assert (
            state.state,
            state.covered_through_seq,
            state.target_seq,
            state.last_error,
            state.lease_owner,
        ) == ("caught_up", 3, 3, None, None)
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


def _repair_only_append(*, target_seq: int) -> _PendingWorkAppend:
    return _PendingWorkAppend(
        authority=EventStoreAuthority(
            user_email="alice@example.com",
            agent_id="agent-alice",
            agent_owner_email="alice@example.com",
        ),
        session_id="intaris-alice",
        first_seq=target_seq,
        last_seq=target_seq,
        target_seq=target_seq,
        events=(),
        payload_bytes=0,
        repair_required=True,
    )


@pytest.mark.asyncio
async def test_noncontiguous_append_releases_lease_for_immediate_repair(
    tmp_path: Path,
) -> None:
    engine, factory = await _database(tmp_path)
    materializer = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="append-worker",
    )
    async with factory() as db:
        row = await db.get(Session, "session-alice")
        assert row is not None
        state = await materializer._ensure_state(db, row, 1)
        state.state = "caught_up"
        state.covered_through_seq = 1
        state.target_seq = 1
        state.next_retry_at = datetime.now(UTC) + timedelta(hours=1)
        await db.commit()
        projection_id = state.projection_id
    await materializer._process_append(
        _PendingWorkAppend(
            authority=EventStoreAuthority(
                user_email="alice@example.com",
                agent_id="agent-alice",
                agent_owner_email="alice@example.com",
            ),
            session_id="intaris-alice",
            first_seq=3,
            last_seq=3,
            target_seq=3,
            events=(SessionEvent(type="user_message", data={"content": "gap"}),),
            payload_bytes=16,
        )
    )
    async with factory() as db:
        state = await db.get(WorkSessionProjectionRow, projection_id)
        assert state is not None
        assert state.state == "repair"
        assert state.priority == 20_000
        assert state.next_retry_at is None
        assert state.lease_owner is None
        assert state.lease_expires_at is None
    restarted = WorkMaterializer(
        session_factory=factory,
        event_store=AuthorityStore({}),
        tool_definitions=lambda: {},
        worker_id="restart-worker",
    )
    claimed = await restarted._claim()
    assert [state.projection_id for state in claimed] == [projection_id]
    await engine.dispose()


def test_materialization_accepts_sqlite_naive_retry_timestamp() -> None:
    retry_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    state = SimpleNamespace(
        state="failed",
        covered_through_seq=1,
        target_seq=2,
        next_retry_at=retry_at,
    )

    result = _materialization([state])

    assert result.state == "failed"
    assert result.retry_after_ms is not None
    assert 0 <= result.retry_after_ms <= 1_000
