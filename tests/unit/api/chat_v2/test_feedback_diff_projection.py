from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.api.chat_v2.feedback_evidence import bind_feedback_diff, preserve_feedback_diff
from cognis.api.chat_v2.schemas import FileDiffRef, SourceRef, TimelineScope, ToolCallTimelineItem
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.api.routes.work import _project_exact_file_diff


def bound_diff(**updates: Any) -> FileDiffRef:
    return bind_feedback_diff(
        FileDiffRef(
            **{
                "path": "/srv/repo/src/exact.py",
                "old_path": "/srv/repo/src/before.py",
                "diff": "@@ -1 +1 @@\n-before\n+after",
                **updates,
            }
        ),
        activity_scope_id="activity-exact",
        source=SourceRef(
            store="intaris",
            session_id="session-exact",
            seq=7,
            event_id="result-event",
            event_type="tool_result",
        ),
        source_item_id="tool:exact-call",
        file_ordinal=1,
    )


def project(diff: FileDiffRef, *, aggregate: bool = False) -> FileDiffRef:
    projection = build_work_projection(
        scope=TimelineScope(
            key="session:session-exact", kind="session", session_id="session-exact"
        ),
        projection_version="work-v7",
        items=[
            ToolCallTimelineItem(
                id="tool:exact-call",
                sort_key="7",
                call_id="exact-call",
                tool_name="files" if aggregate else "apply_patch",
                status="complete",
                file_diffs=[diff],
            )
        ],
        tool_definitions={},
        has_more_before=False,
        before_cursor=None,
        server_time="",
        complete_files=aggregate,
    )
    return projection.mutations[0].file_diffs[0]


def test_work_projection_preserves_only_the_original_canonical_locator() -> None:
    original = bound_diff()
    result = project(original)
    assert result.feedback_source == original.feedback_source
    assert result.path != original.path  # Safe display names are not source identity.
    assert result.feedback_source.file_ordinal == 1
    assert result.feedback_source.path == original.path
    assert result.feedback_source.old_path == original.old_path
    assert result.feedback_source.root_id == original.root_id


@pytest.mark.parametrize(
    "change",
    [
        {"binary": True},
        {"truncated": True},
        {"content_truncated": True},
        {"preview_omitted": True},
        {"preview_omission_reason": "sensitive"},
        {"preview_omission_reason": "projection_budget"},
        {"path": "/srv/repo/.env.production"},
        {"path": r"C:\repo\.npmrc"},
        {"path": "/srv/repo/.pypirc"},
        {"old_path": "/srv/repo/.env"},
    ],
)
def test_original_and_final_preview_must_both_remain_eligible(change: dict[str, Any]) -> None:
    original = bound_diff()
    # Metadata hydration can change flags after binding: neither side can upgrade it.
    assert (
        preserve_feedback_diff(original.model_copy(update=change), original).feedback_source is None
    )
    assert (
        preserve_feedback_diff(original, original.model_copy(update=change)).feedback_source is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"binary": True},
        {"truncated": True},
        {"content_truncated": True},
        {"preview_omitted": True},
        {"preview_omission_reason": "not_persisted"},
    ],
)
def test_projection_never_resurrects_previously_partial_evidence(change: dict[str, Any]) -> None:
    original = bound_diff().model_copy(update=change)
    projected = project(original)
    assert projected.feedback_source is None
    if original.content_truncated:
        assert projected.content_truncated


def test_projection_budget_and_sensitive_redaction_remove_existing_authority() -> None:
    original = bound_diff().model_copy(update={"diff": "+line\n" * 6_000})
    result = project(original)
    assert result.content_truncated and result.preview_omission_reason == "projection_budget"
    assert result.feedback_source is None
    sensitive = project(bound_diff().model_copy(update={"path": "/srv/repo/.env"}))
    assert sensitive.content_truncated and sensitive.feedback_source is None
    redacted = project(bound_diff().model_copy(update={"diff": "+password=private-value"}))
    assert "private-value" not in redacted.diff
    assert redacted.feedback_source is None


def test_unbound_and_current_file_aggregates_never_acquire_authority() -> None:
    original = FileDiffRef(path="/srv/repo/src/exact.py", diff="+change")
    assert project(original).feedback_source is None
    assert project(original, aggregate=True).feedback_source is None
    assert project(bound_diff(), aggregate=True).feedback_source is None


def test_binding_rejects_sensitive_or_partial_originals() -> None:
    assert bound_diff(binary=True).feedback_source is None
    assert bound_diff(preview_omission_reason="sensitive").feedback_source is None
    assert bound_diff(path="/srv/repo/.env").feedback_source is None
    assert bound_diff(old_path=r"C:\repo\.pypirc").feedback_source is None


def test_exact_history_preserves_nonzero_ordinal_among_duplicate_paths() -> None:
    original = bound_diff()
    exact = _project_exact_file_diff(
        items=[
            ToolCallTimelineItem(
                id="tool:exact-call",
                sort_key="7",
                call_id="exact-call",
                tool_name="apply_patch",
                status="complete",
                source_refs=[
                    SourceRef(
                        store="intaris",
                        session_id="session-exact",
                        seq=7,
                        event_id="result-event",
                        event_type="tool_result",
                    )
                ],
                file_diffs=[original.model_copy(update={"diff": "+different ordinal"}), original],
            )
        ],
        source_item_id="tool:exact-call",
        file_ordinal=1,
        fact=SimpleNamespace(
            source_seq=7,
            path=original.path,
            old_path=original.old_path,
            path_id="repo:src/exact.py",
            path_generation_id="generation-exact",
            additions=1,
            deletions=1,
            status="modified",
            binary=False,
            generated=False,
            truncated=False,
        ),
        scope=TimelineScope(
            key="session:session-exact", kind="session", session_id="session-exact"
        ),
        projection_version="work-v7",
        activity_scope_id="activity-exact",
    )
    assert exact is not None
    assert exact.feedback_source == original.feedback_source
    assert exact.diff == original.diff
