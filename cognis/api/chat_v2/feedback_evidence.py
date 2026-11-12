"""Bind only retained canonical event/file evidence to feedback locators."""

from __future__ import annotations

from cognis.api.chat_v2.schemas import FileDiffRef, SourceRef
from cognis.models.feedback import EventRef, WorkDiffSourceRef


def _eligible_feedback_diff(diff: FileDiffRef) -> bool:
    filenames = [
        path.replace("\\", "/").rsplit("/", 1)[-1] for path in (diff.path, diff.old_path) if path
    ]
    return not (
        diff.binary
        or diff.truncated
        or diff.content_truncated
        or diff.preview_omitted
        or diff.preview_omission_reason is not None
        or any(
            filename.startswith(".env") or filename in {".npmrc", ".pypirc"}
            for filename in filenames
        )
    )


def preserve_feedback_diff(original: FileDiffRef, projected: FileDiffRef) -> FileDiffRef:
    """Carry existing canonical authority only across an eligible, unchanged preview."""
    source = original.feedback_source
    if (
        not _eligible_feedback_diff(original)
        or not _eligible_feedback_diff(projected)
        or original.diff != projected.diff
    ):
        source = None
    # Display paths/roots can change; the original event/file locator must not.
    return projected.model_copy(update={"feedback_source": source})


def bind_feedback_diff(
    diff: FileDiffRef,
    *,
    activity_scope_id: str | None,
    source: SourceRef | None,
    source_item_id: str,
    file_ordinal: int,
) -> FileDiffRef:
    """Synthetic, missing, or truncated histories never acquire invented authority."""
    if (
        not activity_scope_id
        or source is None
        or source.store != "intaris"
        or not _eligible_feedback_diff(diff)
    ):
        return diff.model_copy(update={"feedback_source": None})
    return diff.model_copy(
        update={
            "feedback_source": WorkDiffSourceRef(
                kind="work_diff",
                activity_scope_id=activity_scope_id,
                source_event=EventRef.model_validate(source.model_dump()),
                source_item_id=source_item_id,
                file_ordinal=file_ordinal,
                root_id=diff.root_id,
                path=diff.path,
                old_path=diff.old_path,
            )
        }
    )
