"""Read-only evidence projection derived from canonical Chat v2 timeline items.

The projection uses only authorized timeline data. It computes exact metadata
before it bounds previews, redacts structured payloads before serialization,
and does not inspect executor or repository state.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    FileDiffRef,
    TimelineItem,
    TimelineScope,
    ToolCallTimelineItem,
    WorkArtifact,
    WorkCategory,
    WorkCommandEvent,
    WorkDeliverable,
    WorkFileStat,
    WorkMaterialization,
    WorkMutationEvent,
    WorkProjectionResponse,
    WorkstreamRef,
    WorkSummary,
)
from cognis.models.tool import ToolDefinition, ToolMutationKind

_MAX_ARGUMENT_TEXT = 500
_MAX_COMMAND = 4_000
_MAX_PREVIEW = 8_000
_MAX_DIFFS = 20
_MAX_DIFF = 30_000
_MAX_DELIVERABLE_PREVIEW = 4_000
_MAX_PAGE_DIFF_PREVIEW_BYTES = 96_000
_MAX_PAGE_TEXT_PREVIEW_BYTES = 192_000
_MAX_PAGE_FILE_STATS = 200
_CONTROL_TOOL_NAMES = {
    "attach_artifact",
    "artifact_publish",
    "artifact_save",
    "cancel_subsession",
    "delegate",
    "follow_up_subsession",
    "fork_subsession",
    "request_auth_challenge",
    "request_credential",
    "request_user_input",
    "step_complete",
    "todo_write",
    "write_deliverable",
}
_CONTROL_TOOL_PREFIXES = (
    "agent_conversation_",
    "artifact_",
    "create_task",
    "manage_task",
    "memory_",
    "respond_task",
    "skill_",
    "workflow_",
)
_COGNIS_MANAGEMENT_CATEGORIES = {
    "agent_management",
    "configuration",
    "mcp_management",
    "project_management",
    "settings",
}
_COGNIS_MANAGEMENT_TOOLS = {
    "manage_agents",
    "manage_mcp_servers",
    "manage_projects",
    "manage_settings",
}
_FILE_MUTATION_CATEGORIES = {"file", "filesystem"}
_FILE_MUTATION_TOOLS = {"apply_patch", "edit", "multiedit", "write"}
_EXTERNAL_WRITE_WORDS = {
    "add",
    "archive",
    "complete",
    "create",
    "delete",
    "draft",
    "invite",
    "move",
    "post",
    "publish",
    "remove",
    "restore",
    "send",
    "update",
    "upload",
}
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "env",
    "password",
    "recovery_codes",
    "secret",
    "token",
    "totp_seed",
}
_SAFE_ARGUMENT_KEYS = {
    "action",
    "command",
    "description",
    "filename",
    "label",
    "operation",
    "path",
    "paths",
    "project_id",
    "reason",
    "source_path",
    "target",
    "title",
    "workdir",
}
_PATH_KEYS = {"file_path", "filename", "path", "paths", "source_path", "workdir"}
_SENSITIVE_TEXT_KEY = re.compile(
    r"(?i)(authorization|access[_-]?token|api[_-]?key|apikey|password|secret|token)"
)


@dataclass(frozen=True)
class _WorkRoot:
    normalized: str
    name: str
    root_id: str
    windows: bool


@dataclass(frozen=True)
class _SafePath:
    display: str
    path_id: str
    relative_path: str | None = None
    root_label: str | None = None
    root_name: str | None = None
    root_id: str | None = None


@dataclass
class _ProjectionBudget:
    text_bytes: int = _MAX_PAGE_TEXT_PREVIEW_BYTES
    diff_bytes: int = _MAX_PAGE_DIFF_PREVIEW_BYTES
    file_stats: int = _MAX_PAGE_FILE_STATS
    max_diffs: int = _MAX_DIFFS

    def take_text(self, value: str | None, limit: int) -> tuple[str | None, bool]:
        safe = _safe_text(value)
        if safe is None:
            return None, False
        bounded, truncated = _bounded_utf8(safe, min(limit, self.text_bytes))
        self.text_bytes -= len((bounded or "").encode("utf-8"))
        return bounded, truncated

    def take_diff(self, value: str) -> tuple[str, bool]:
        if self.diff_bytes <= 0:
            return "", True
        bounded, truncated = _bounded_utf8(value, min(_MAX_DIFF, self.diff_bytes))
        rendered = bounded or ""
        self.diff_bytes -= len(rendered.encode("utf-8"))
        return rendered, truncated

    def take_file_stats(self, count: int) -> int:
        accepted = min(max(count, 0), self.file_stats)
        self.file_stats -= accepted
        return accepted

    def take_structured(
        self,
        value: Any,
        *,
        max_bytes: int = 4_000,
    ) -> Any:
        redacted = _redact_structured(value)
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        allowed = min(max_bytes, self.text_bytes)
        if len(encoded) > allowed:
            marker = {"_truncated": True}
            self.text_bytes -= min(
                self.text_bytes,
                len(json.dumps(marker).encode("utf-8")),
            )
            return marker
        self.text_bytes -= len(encoded)
        return redacted


def is_work_evidence_item(
    item: TimelineItem,
    tool_definitions: Mapping[str, ToolDefinition],
) -> bool:
    """Return whether one canonical timeline item contributes to Work."""

    if isinstance(item, (AssistantDeliverableTimelineItem, ArtifactTimelineItem)):
        return True
    if not isinstance(item, ToolCallTimelineItem):
        return False
    if not item.tool_name.strip():
        return False
    definition = tool_definitions.get(item.tool_name)
    if item.tool_name == "bash":
        return _is_visible_command(item, definition)
    if not _is_completed_tool_execution(item):
        return False
    if item.file_diffs:
        return _is_completed_file_mutation(item, definition)
    if _is_file_manipulation(item, definition):
        return False
    if definition is not None:
        return _is_mutation(item, definition) and _is_meaningful_mutation(item, definition)
    return _is_historical_meaningful_mutation(item)


def work_item_category(
    item: TimelineItem,
    tool_definitions: Mapping[str, ToolDefinition],
) -> WorkCategory | None:
    """Classify one materialized Work item into its independent API category."""

    if isinstance(item, AssistantDeliverableTimelineItem):
        return "deliverables"
    if isinstance(item, ArtifactTimelineItem):
        return "artifacts"
    if not isinstance(item, ToolCallTimelineItem) or not is_work_evidence_item(
        item, tool_definitions
    ):
        return None
    if item.tool_name == "bash":
        return "commands"
    if item.file_diffs:
        return "files"
    return "mutations"


def build_work_projection(
    *,
    scope: TimelineScope,
    projection_version: str,
    items: Iterable[TimelineItem],
    tool_definitions: Mapping[str, ToolDefinition],
    has_more_before: bool,
    before_cursor: str | None,
    server_time: str,
    workstreams: Iterable[WorkstreamRef] = (),
    graph_fingerprint: str | None = None,
    graph_truncated: bool = False,
    work_revision: int = 0,
    graph_revision: int = 0,
    materialization: WorkMaterialization | None = None,
    removed_call_ids: Iterable[str] = (),
    summary: WorkSummary | None = None,
    newest_first: bool = False,
    complete_files: bool = False,
) -> WorkProjectionResponse:
    ordered = sorted(items, key=lambda item: item.sort_key, reverse=newest_first)
    if complete_files:
        ordered = [_resolve_item_file_paths(item) for item in ordered]
    workstream_list = list(workstreams)
    workstream_by_stream = {
        f"intaris:{node.event_store_session_id}": node for node in workstream_list
    }
    roots = _trusted_work_roots(ordered, derive_from_files=complete_files)
    final_deliverable: WorkDeliverable | None = None
    primary_deliverable: WorkDeliverable | None = None
    deliverables: list[WorkDeliverable] = []
    mutations: list[WorkMutationEvent] = []
    commands: list[WorkCommandEvent] = []
    artifacts: list[WorkArtifact] = []
    changed_path_ids: set[str] = set()
    budget = (
        _ProjectionBudget(
            text_bytes=2**31,
            diff_bytes=2**31,
            file_stats=2**31,
            max_diffs=2**31,
        )
        if complete_files
        else _ProjectionBudget()
    )
    seen_artifact_ids: set[str] = set()
    seen_deliverable_ids: set[str] = set()

    for item in ordered:
        if isinstance(item, AssistantDeliverableTimelineItem):
            if newest_first and item.deliverable_id in seen_deliverable_ids:
                continue
            seen_deliverable_ids.add(item.deliverable_id)
            source_workstream = _source_workstream(item, workstream_by_stream)
            content, content_truncated = budget.take_text(
                item.content,
                _MAX_DELIVERABLE_PREVIEW,
            )
            deliverable = WorkDeliverable(
                deliverable_id=item.deliverable_id,
                sort_key=item.sort_key,
                format=item.format,
                title=item.title,
                content=content,
                content_preview_truncated=content_truncated,
                recoverable=True,
                render_metadata=budget.take_structured(item.render_metadata),
                export_metadata=budget.take_structured(item.export_metadata),
                source_workstream=source_workstream,
            )
            deliverables = [
                existing
                for existing in deliverables
                if existing.deliverable_id != deliverable.deliverable_id
            ]
            deliverables.append(deliverable)
            if final_deliverable is None or not newest_first:
                final_deliverable = deliverable
            if (
                not workstream_list
                or (
                    source_workstream is not None
                    and source_workstream.key == source_workstream.root_key
                )
            ) and (primary_deliverable is None or not newest_first):
                primary_deliverable = deliverable
            continue
        if isinstance(item, ArtifactTimelineItem):
            if newest_first and item.artifact_id in seen_artifact_ids:
                continue
            seen_artifact_ids.add(item.artifact_id)
            source_workstream = _source_workstream(item, workstream_by_stream)
            title, _ = budget.take_text(item.title, _MAX_ARGUMENT_TEXT)
            artifact = WorkArtifact(
                artifact_id=item.artifact_id,
                sort_key=item.sort_key,
                filename=PurePath(_safe_path(item.filename, roots).display).name,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                title=title,
                source_workstream=source_workstream,
            )
            artifacts = [
                existing for existing in artifacts if existing.artifact_id != artifact.artifact_id
            ]
            artifacts.append(artifact)
            continue
        if not isinstance(item, ToolCallTimelineItem):
            continue

        definition = tool_definitions.get(item.tool_name)
        if not is_work_evidence_item(item, tool_definitions):
            continue
        if item.tool_name == "bash":
            commands.append(
                _command_event(
                    item,
                    roots,
                    budget,
                    _source_workstream(item, workstream_by_stream),
                )
            )
            continue

        if item.file_diffs:
            if not _is_completed_file_mutation(item, definition):
                continue
            event = _mutation_event(
                item,
                definition,
                roots,
                budget,
                _source_workstream(item, workstream_by_stream),
            )
            changed_path_ids.update(stat.path_id for stat in event.file_stats)
            changed_path_ids.update(
                _safe_path(diff.path, roots).path_id for diff in item.file_diffs
            )
            mutations.append(event)
            continue
        if definition is not None:
            if _is_mutation(item, definition) and _is_meaningful_mutation(item, definition):
                mutations.append(
                    _mutation_event(
                        item,
                        definition,
                        roots,
                        budget,
                        _source_workstream(item, workstream_by_stream),
                    )
                )
            continue
        if _is_historical_meaningful_mutation(item):
            mutations.append(
                _mutation_event(
                    item,
                    None,
                    roots,
                    budget,
                    _source_workstream(item, workstream_by_stream),
                )
            )

    additions = sum(event.additions for event in mutations)
    deletions = sum(event.deletions for event in mutations)
    generic_mutations = sum(1 for event in mutations if not event.file_diffs)
    return WorkProjectionResponse(
        projection_version=projection_version,
        scope=scope,
        final_deliverable=primary_deliverable or final_deliverable,
        deliverables=deliverables,
        workstreams=workstream_list,
        graph_fingerprint=graph_fingerprint,
        graph_truncated=graph_truncated,
        work_revision=work_revision,
        graph_revision=graph_revision,
        mutations=mutations,
        commands=commands,
        removed_call_ids=list(removed_call_ids),
        artifacts=artifacts,
        summary=summary
        or WorkSummary(
            mutations=generic_mutations,
            commands=len(commands),
            changed_files=len(changed_path_ids),
            artifacts=len(artifacts),
            deliverables=len(deliverables),
            additions=additions,
            deletions=deletions,
            omitted_files=sum(event.omitted_file_count for event in mutations),
        ),
        materialization=materialization or WorkMaterialization(),
        has_more_before=has_more_before,
        before_cursor=before_cursor,
        server_time=server_time,
    )


def _status(item: ToolCallTimelineItem) -> str:
    if item.is_error:
        return "failed"
    return item.status or ("complete" if item.result_preview is not None else "running")


def _is_completed_tool_execution(item: ToolCallTimelineItem) -> bool:
    """Return true only when the tool reached a successful terminal result."""

    return not item.is_error and _status(item) == "complete"


def _is_visible_command(
    item: ToolCallTimelineItem,
    definition: ToolDefinition | None,
) -> bool:
    if definition is None or definition.read_only or item.is_error or _is_bash_control(item):
        return False
    command = _string((item.arguments or {}).get("command"))
    if command is None or not command.strip():
        return False
    return _status(item) in {"running", "complete"}


def _is_file_manipulation(
    item: ToolCallTimelineItem,
    definition: ToolDefinition | None,
) -> bool:
    if definition is not None and definition.category in _FILE_MUTATION_CATEGORIES:
        return True
    normalized_name = re.split(r"[:./]", item.tool_name.lower())[-1]
    return normalized_name in _FILE_MUTATION_TOOLS


def _mutation_event(
    item: ToolCallTimelineItem,
    definition: ToolDefinition | None,
    roots: tuple[_WorkRoot, ...],
    budget: _ProjectionBudget,
    source_workstream: WorkstreamRef | None,
) -> WorkMutationEvent:
    arguments = _safe_arguments(item.arguments or {}, roots, budget)
    total_file_count = len(item.file_diffs)
    additions, deletions = _diff_totals(item.file_diffs)
    diffs, content_truncated = _bounded_diffs(item.file_diffs, roots, budget)
    diffs = [diff.model_copy(update={"source_workstream": source_workstream}) for diff in diffs]
    preview_ids = {diff.path_id for diff in diffs}
    file_stat_count = budget.take_file_stats(len(item.file_diffs))
    file_stats = [
        _file_stat(diff, roots, preview_ids=preview_ids)
        for diff in item.file_diffs[:file_stat_count]
    ]
    file_stats = [
        stat.model_copy(update={"source_workstream": source_workstream}) for stat in file_stats
    ]
    omitted_file_count = max(0, total_file_count - len(diffs))
    omitted_file_stat_count = max(0, total_file_count - len(file_stats))
    paths = _paths(arguments, diffs)
    operation = arguments.get("action") or arguments.get("operation")
    category = definition.category if definition is not None else "external"
    operation_kind = (
        str(operation)[:80] if operation else ("file_write" if item.file_diffs else category)
    )
    return WorkMutationEvent(
        id=item.id,
        call_id=item.call_id,
        sort_key=item.sort_key,
        created_at=item.created_at,
        updated_at=item.updated_at,
        tool_name=item.tool_name,
        display_name=item.display_name,
        category=category,
        operation_kind=operation_kind,
        status=_status(item),
        duration_ms=item.duration_ms,
        error=budget.take_text(item.result_preview, _MAX_ARGUMENT_TEXT)[0]
        if item.is_error
        else None,
        arguments=arguments,
        result_preview=budget.take_text(item.result_preview, _MAX_PREVIEW)[0],
        streamed_output=budget.take_text(item.streamed_output, _MAX_PREVIEW)[0],
        evaluation=budget.take_structured(_safe_evaluation(item.evaluation, roots)),
        output_size=item.output_size,
        truncated=item.truncated,
        has_full_output=item.has_full_output,
        recovery_call_id=item.recovery_call_id,
        tool_output_artifact_id=item.tool_output_artifact_id,
        paths=paths,
        file_stats=file_stats,
        file_diffs=diffs,
        diffs_truncated=content_truncated,
        total_file_count=total_file_count,
        omitted_file_count=omitted_file_count,
        omitted_file_stat_count=omitted_file_stat_count,
        file_stats_recoverable=omitted_file_stat_count > 0,
        additions=additions,
        deletions=deletions,
        source_workstream=source_workstream,
    )


def _command_event(
    item: ToolCallTimelineItem,
    roots: tuple[_WorkRoot, ...],
    budget: _ProjectionBudget,
    source_workstream: WorkstreamRef | None,
) -> WorkCommandEvent:
    arguments = item.arguments or {}
    result = item.result_preview or item.streamed_output
    command, command_truncated = budget.take_text(_string(arguments.get("command")), _MAX_COMMAND)
    description, _ = budget.take_text(
        _string(arguments.get("description")),
        _MAX_ARGUMENT_TEXT,
    )
    preview, preview_budget_truncated = budget.take_text(result, _MAX_PREVIEW)
    return WorkCommandEvent(
        id=item.id,
        call_id=item.call_id,
        sort_key=item.sort_key,
        created_at=item.created_at,
        updated_at=item.updated_at,
        tool_name=item.tool_name,
        display_name=item.display_name,
        command=command,
        description=description,
        workdir=_safe_path(_string(arguments.get("workdir")), roots).display,
        status=_status(item),
        duration_ms=item.duration_ms,
        exit_code=_exit_code(item),
        error=budget.take_text(result, _MAX_ARGUMENT_TEXT)[0] if item.is_error else None,
        arguments=_safe_arguments(arguments, roots, budget),
        evaluation=budget.take_structured(_safe_evaluation(item.evaluation, roots)),
        preview=preview,
        preview_truncated=preview_budget_truncated or command_truncated or item.truncated,
        has_full_output=item.has_full_output,
        recovery_call_id=item.recovery_call_id,
        tool_output_artifact_id=item.tool_output_artifact_id,
        output_size=item.output_size,
        source_workstream=source_workstream,
    )


def _source_workstream(
    item: TimelineItem,
    workstreams: Mapping[str, WorkstreamRef],
) -> WorkstreamRef | None:
    for source_ref in item.source_refs:
        node = workstreams.get(f"{source_ref.store}:{source_ref.session_id}")
        if node is not None:
            return node
    return None


def _safe_arguments(
    arguments: Mapping[str, Any],
    roots: tuple[_WorkRoot, ...],
    budget: _ProjectionBudget,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        normalized_key = _normalized_key(key)
        if _is_sensitive_key(normalized_key):
            safe[key] = "[redacted]"
        elif key not in _SAFE_ARGUMENT_KEYS and key not in _PATH_KEYS:
            continue
        elif key in _PATH_KEYS:
            if isinstance(value, list):
                safe[key] = [_safe_path(_string(part), roots).display for part in value[:20]]
            else:
                safe[key] = _safe_path(_string(value), roots).display
        else:
            safe[key] = _redact_structured(value)
    bounded = budget.take_structured(safe)
    return bounded if isinstance(bounded, dict) else {"_truncated": True}


def _safe_path(value: str | None, roots: tuple[_WorkRoot, ...]) -> _SafePath:
    if not value:
        return _SafePath(display="", path_id=_path_id("empty", ""))
    normalized = (
        _normalized_absolute_path(value)
        if _is_absolute_path(value)
        else value.replace("\\", "/").rstrip("/")
    )
    absolute = normalized.startswith("/") or bool(re.match(r"^[A-Za-z]:/", normalized))
    windows = bool(re.match(r"^[A-Za-z]:/", normalized))
    canonical = normalized.lower() if windows else normalized
    stable_path_id = _path_id("absolute" if absolute else "relative", canonical.lstrip("./"))
    matching = [root for root in roots if _path_is_within(normalized, root)]
    if matching:
        root = max(matching, key=lambda item: len(item.normalized))
        relative = _normalized_relative_path(_relative_to_root(normalized, root)) or ""
        if root.windows:
            relative = relative.lower()
        display = root.name if not relative else f"{root.name}/{relative}"
        return _SafePath(
            display=_bounded(display, _MAX_ARGUMENT_TEXT) or root.name,
            path_id=f"{root.root_id}:{relative.lower() if root.windows else relative}",
            relative_path=relative,
            root_label=root.name,
            root_name=root.name,
            root_id=root.root_id,
        )
    if not absolute:
        relative = _normalized_relative_path(normalized)
        if relative is None:
            basename = normalized.rsplit("/", 1)[-1]
            return _SafePath(
                display=_bounded(basename, _MAX_ARGUMENT_TEXT) or "",
                path_id=_path_id("unsafe-relative", normalized),
            )
        return _SafePath(
            display=_bounded(f"Unscoped/{relative}", _MAX_ARGUMENT_TEXT) or "Unscoped",
            path_id=f"unbound:{_path_id('relative', relative)}",
            relative_path=relative,
            root_label="Unscoped",
        )
    basename = normalized.rsplit("/", 1)[-1]
    return _SafePath(
        display=_bounded(basename, _MAX_ARGUMENT_TEXT) or "",
        path_id=stable_path_id,
    )


def _bounded_diffs(
    diffs: list[FileDiffRef],
    roots: tuple[_WorkRoot, ...],
    budget: _ProjectionBudget,
) -> tuple[list[FileDiffRef], bool]:
    bounded: list[FileDiffRef] = []
    for diff in diffs[: budget.max_diffs]:
        safe_path = _safe_path(diff.path, roots)
        filename = PurePath(safe_path.display).name
        additions, deletions = _diff_totals([diff])
        if filename.startswith(".env") or filename in {".npmrc", ".pypirc"}:
            placeholder, _ = budget.take_diff("… sensitive diff content omitted …")
            if not placeholder:
                break
            bounded.append(
                FileDiffRef(
                    path=safe_path.display,
                    path_id=safe_path.path_id,
                    relative_path=safe_path.relative_path,
                    root_label=safe_path.root_label,
                    root_name=safe_path.root_name,
                    root_id=safe_path.root_id,
                    additions=additions,
                    deletions=deletions,
                    diff=placeholder,
                    content_truncated=True,
                )
            )
            continue
        text = _safe_text(diff.diff) or ""
        text, content_truncated = budget.take_diff(text)
        if not text:
            break
        if content_truncated:
            text += "\n… diff content truncated …"
        bounded.append(
            FileDiffRef(
                path=safe_path.display,
                path_id=safe_path.path_id,
                relative_path=safe_path.relative_path,
                root_label=safe_path.root_label,
                root_name=safe_path.root_name,
                root_id=safe_path.root_id,
                additions=additions,
                deletions=deletions,
                diff=text,
                content_truncated=content_truncated,
            )
        )
    return bounded, any(diff.content_truncated for diff in bounded)


def _file_stat(
    diff: FileDiffRef,
    roots: tuple[_WorkRoot, ...],
    *,
    preview_ids: set[str | None],
) -> WorkFileStat:
    safe_path = _safe_path(diff.path, roots)
    additions, deletions = _diff_totals([diff])
    return WorkFileStat(
        path=safe_path.display,
        path_id=safe_path.path_id,
        relative_path=safe_path.relative_path,
        root_label=safe_path.root_label,
        root_name=safe_path.root_name,
        root_id=safe_path.root_id,
        additions=additions,
        deletions=deletions,
        preview_available=safe_path.path_id in preview_ids,
    )


def _paths(arguments: Mapping[str, Any], diffs: list[FileDiffRef]) -> list[str]:
    result = [diff.path for diff in diffs if diff.path]
    for key in _PATH_KEYS:
        value = arguments.get(key)
        values = value if isinstance(value, list) else [value]
        for part in values:
            if isinstance(part, str) and part and part not in result:
                result.append(part)
    return result[:20]


def _exit_code(item: ToolCallTimelineItem) -> int | None:
    evaluation = item.evaluation or {}
    value = evaluation.get("exit_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit] + "…"


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    if limit <= 0:
        return "", True
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(
            _redact_structured(parsed),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return _redact_text_fallback(value)


def _redact_text_fallback(value: str) -> str:
    result: list[str] = []
    offset = 0
    while match := _SENSITIVE_TEXT_KEY.search(value, offset):
        separator = match.end()
        while separator < len(value) and value[separator] in "\\\"' \t":
            separator += 1
        if separator >= len(value) or value[separator] not in ":=":
            result.append(value[offset : match.end()])
            offset = match.end()
            continue
        start = separator + 1
        while start < len(value) and value[start].isspace():
            start += 1
        if match.group(1).lower() == "authorization":
            scheme = re.match(r"(?i)(bearer|basic)\s+", value[start:])
            if scheme:
                start += scheme.end()
        end, opening, closing = _secret_value_bounds(value, start)
        result.append(value[offset:start])
        result.append(f"{opening}[redacted]{closing}")
        offset = end
    result.append(value[offset:])
    return "".join(result)


def _secret_value_bounds(value: str, start: int) -> tuple[int, str, str]:
    if start >= len(value):
        return start, "", ""
    escaped_quote = start + 1 < len(value) and value[start] == "\\" and value[start + 1] in "\"'"
    if escaped_quote:
        token = value[start : start + 2]
        end = value.find(token, start + 2)
        return (
            (len(value) if end < 0 else end + 2),
            token,
            token,
        )
    if value[start] in "\"'":
        quote = value[start]
        index = start + 1
        while index < len(value):
            if value[index] == "\\":
                index += 2
                continue
            if value[index] == quote:
                return index + 1, quote, quote
            index += 1
        return len(value), quote, quote
    if value[start] in "[{":
        opening = value[start]
        closing = "]" if opening == "[" else "}"
        depth = 0
        quote: str | None = None
        index = start
        while index < len(value):
            char = value[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index + 1, "", ""
            index += 1
        return len(value), "", ""
    index = start
    while index < len(value) and value[index] not in "\t\r\n ,&}\"'":
        index += 1
    trailing = value[index] if index < len(value) and value[index] in "\"'" else ""
    return index + len(trailing), "", trailing


def _bound_safe_text(value: str | None, limit: int) -> str | None:
    return _bounded(_safe_text(value), limit)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_sensitive_key(normalized: str) -> bool:
    return any(_normalized_key(secret) in normalized for secret in _SENSITIVE_KEYS)


def _redact_structured(value: Any, *, depth: int = 0) -> Any:
    if value is None:
        return None
    if depth >= 8:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in list(value.items())[:100]:
            key = str(raw_key)
            if _is_sensitive_key(_normalized_key(key)):
                result[key] = "[redacted]"
            else:
                result[key] = _redact_structured(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_structured(child, depth=depth + 1) for child in value[:100]]
    if isinstance(value, str):
        return _bound_safe_text(value, _MAX_ARGUMENT_TEXT)
    if isinstance(value, (int, float, bool)):
        return value
    return _bound_safe_text(str(value), _MAX_ARGUMENT_TEXT)


def _safe_evaluation(
    value: Mapping[str, Any] | None,
    roots: tuple[_WorkRoot, ...],
) -> dict[str, Any] | None:
    if not value:
        return None
    safe: dict[str, Any] = {}
    for key in ("decision", "reasoning", "risk", "path", "latency_ms", "exit_code"):
        item = value.get(key)
        if key == "path" and isinstance(item, str):
            safe[key] = _safe_path(item, roots).display
        elif isinstance(item, str):
            safe[key] = _bound_safe_text(item, _MAX_ARGUMENT_TEXT)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            safe[key] = item
    return safe or None


def _diff_totals(diffs: Iterable[FileDiffRef]) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for diff in diffs:
        for line in diff.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
    return additions, deletions


def _is_bash_control(item: ToolCallTimelineItem) -> bool:
    action = str((item.arguments or {}).get("action") or "").lower()
    return action in {"kill", "terminate"}


def _is_completed_file_mutation(
    item: ToolCallTimelineItem,
    definition: ToolDefinition | None,
) -> bool:
    if item.is_error or _status(item) != "complete":
        return False
    name = item.tool_name.lower()
    if name in _CONTROL_TOOL_NAMES or name.startswith(_CONTROL_TOOL_PREFIXES):
        return False
    if definition is None:
        return name in {
            "apply_patch",
            "edit",
            "multiedit",
            "write",
        }
    if definition.category in {"artifact", "memory", "orchestration", "workflow"}:
        return False
    return _is_mutation(item, definition)


def _selected_mutation_kind(
    item: ToolCallTimelineItem,
    definition: ToolDefinition,
) -> ToolMutationKind | None:
    operations = definition.native_operations or []
    if not operations:
        return None
    if len(operations) == 1:
        return operations[0].mutation_kind
    requested = (item.arguments or {}).get("action") or (item.arguments or {}).get("operation")
    for operation in operations:
        if operation.operation == requested:
            return operation.mutation_kind
    return None


def _is_meaningful_mutation(
    item: ToolCallTimelineItem,
    definition: ToolDefinition,
) -> bool:
    """Return successful product/external writes and reject controller plumbing."""
    name = item.tool_name.lower()
    if item.is_error or _status(item) != "complete":
        return False
    if name in _CONTROL_TOOL_NAMES or name.startswith(_CONTROL_TOOL_PREFIXES):
        return False
    if definition.category in {"artifact", "memory", "orchestration", "workflow"}:
        return False

    mutation_kind = _selected_mutation_kind(item, definition)
    if mutation_kind is ToolMutationKind.READ:
        return False
    if name in _COGNIS_MANAGEMENT_TOOLS or definition.category in _COGNIS_MANAGEMENT_CATEGORIES:
        return mutation_kind in {
            ToolMutationKind.CREATE,
            ToolMutationKind.UPDATE,
            ToolMutationKind.DELETE,
        }

    if definition.source.type not in {"mcp", "local_mcp", "intaris_mcp", "skill"}:
        return False
    operation = str(
        (item.arguments or {}).get("action") or (item.arguments or {}).get("operation") or ""
    ).lower()
    if operation:
        return operation in _EXTERNAL_WRITE_WORDS
    raw_name = definition.source.raw_tool_name or name
    return bool(_operation_words(raw_name) & _EXTERNAL_WRITE_WORDS)


def _is_historical_meaningful_mutation(item: ToolCallTimelineItem) -> bool:
    """Classify only unambiguous successful writes when registry metadata is gone."""
    if item.is_error or _status(item) != "complete":
        return False
    name = item.tool_name.lower()
    if name in _CONTROL_TOOL_NAMES or name.startswith(_CONTROL_TOOL_PREFIXES):
        return False
    operation = str(
        (item.arguments or {}).get("action") or (item.arguments or {}).get("operation") or ""
    ).lower()
    if operation:
        return operation in _EXTERNAL_WRITE_WORDS
    return bool(_operation_words(item.tool_name) & _EXTERNAL_WRITE_WORDS)


def _is_mutation(item: ToolCallTimelineItem, definition: ToolDefinition) -> bool:
    if definition.read_only:
        return False
    operations = definition.native_operations or []
    if not operations:
        return True
    if len(operations) == 1:
        return operations[0].mutation_kind is not ToolMutationKind.READ
    requested = (item.arguments or {}).get("action") or (item.arguments or {}).get("operation")
    for operation in operations:
        if operation.operation == requested:
            return operation.mutation_kind is not ToolMutationKind.READ
    return False


def _operation_words(value: str) -> set[str]:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return {part for part in re.split(r"[^a-z0-9]+", camel_split.lower()) if part}


def _trusted_work_roots(
    items: Iterable[TimelineItem],
    *,
    derive_from_files: bool = False,
) -> tuple[_WorkRoot, ...]:
    roots: dict[str, _WorkRoot] = {}
    derived_roots: dict[str, _WorkRoot] = {}
    for item in items:
        if not isinstance(item, ToolCallTimelineItem):
            continue
        if derive_from_files:
            for diff in item.file_diffs:
                derived = _derived_path_root(diff.path)
                if derived is not None:
                    key = derived.normalized.lower() if derived.windows else derived.normalized
                    derived_roots[key] = derived
        workdir = _string((item.arguments or {}).get("workdir"))
        if not workdir:
            continue
        normalized = _normalized_absolute_path(workdir)
        windows = bool(re.match(r"^[A-Za-z]:/", normalized))
        if not normalized.startswith("/") and not windows:
            continue
        key = normalized.lower() if windows else normalized
        path = PureWindowsPath(workdir) if windows else PurePosixPath(workdir)
        roots[key] = _WorkRoot(
            normalized=normalized,
            name=path.name or "work",
            root_id=_path_id("root", key),
            windows=windows,
        )
    roots.update(derived_roots)
    for key, root in list(roots.items()):
        if key in derived_roots:
            continue
        if any(
            root.windows == derived.windows and _path_is_within(root.normalized, derived)
            for derived in derived_roots.values()
        ):
            roots.pop(key)
    return tuple(sorted(roots.values(), key=lambda item: item.normalized))


def _resolve_item_file_paths(item: TimelineItem) -> TimelineItem:
    if not isinstance(item, ToolCallTimelineItem) or not item.file_diffs:
        return item
    workdir = _string((item.arguments or {}).get("workdir"))
    if not workdir:
        return item
    normalized_workdir = _normalized_absolute_path(workdir)
    if not _is_absolute_path(normalized_workdir):
        return item
    resolved: list[FileDiffRef] = []
    for diff in item.file_diffs:
        if _is_absolute_path(diff.path):
            resolved.append(diff.model_copy(update={"path": _normalized_absolute_path(diff.path)}))
            continue
        relative = _normalized_resolvable_relative_path(diff.path)
        if relative is None:
            resolved.append(diff)
            continue
        resolved.append(diff.model_copy(update={"path": f"{normalized_workdir}/{relative}"}))
    return item.model_copy(update={"file_diffs": resolved})


def _derived_path_root(value: str) -> _WorkRoot | None:
    normalized = _normalized_absolute_path(value)
    if not _is_absolute_path(normalized):
        return None
    windows = bool(re.match(r"^[A-Za-z]:/", normalized))
    parts = normalized.split("/")
    if windows:
        if len(parts) >= 4 and parts[1].lower() == "users":
            if len(parts) >= 5:
                root_parts = parts[:4]
                name = root_parts[-1]
            else:
                root_parts = parts[:3]
                name = "~"
        elif len(parts) >= 3:
            root_parts = parts[:2]
            name = root_parts[-1]
        else:
            return None
    else:
        components = [part for part in parts if part]
        if len(components) >= 3 and components[0] in {"home", "Users"}:
            if len(components) >= 4:
                root_parts = components[:3]
                name = root_parts[-1]
            else:
                root_parts = components[:2]
                name = "~"
        elif len(components) >= 3:
            root_parts = components[:2]
            name = root_parts[-1]
        elif len(components) >= 2:
            root_parts = components[:1]
            name = root_parts[-1]
        else:
            return None
    normalized_root = "/".join(root_parts) if windows else f"/{'/'.join(root_parts)}"
    key = normalized_root.lower() if windows else normalized_root
    return _WorkRoot(
        normalized=normalized_root,
        name=name,
        root_id=_path_id("root", key),
        windows=windows,
    )


def _normalized_absolute_path(value: str) -> str:
    slash_normalized = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", slash_normalized):
        normalized = ntpath.normpath(slash_normalized).replace("\\", "/")
        return f"{normalized[0].upper()}{normalized[1:]}"
    normalized = posixpath.normpath(slash_normalized)
    return normalized


def _is_absolute_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized.startswith("/") or bool(re.match(r"^[A-Za-z]:/", normalized))


def _path_id(kind: str, value: str) -> str:
    return sha256(f"{kind}:{value}".encode()).hexdigest()[:20]


def _path_is_within(value: str, root: _WorkRoot) -> bool:
    candidate = _normalized_absolute_path(value)
    left = candidate.lower() if root.windows else candidate
    right = root.normalized.lower() if root.windows else root.normalized
    return left == right or left.startswith(f"{right}/")


def _relative_to_root(value: str, root: _WorkRoot) -> str:
    candidate = _normalized_absolute_path(value)
    return candidate[len(root.normalized) :].lstrip("/")


def _normalized_relative_path(value: str) -> str | None:
    parts = [part for part in value.replace("\\", "/").split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _normalized_resolvable_relative_path(value: str) -> str | None:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized
