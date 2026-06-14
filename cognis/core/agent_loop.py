"""Agent loop engine — the step runner.

Runs a single step as a full agentic loop: context assembly, LLM calls
with streaming, tool execution via router, and step finalization. This
is the heart of Cognis.

Used directly for main chat (Direct workflow) and by the WorkflowEngine
for multi-step background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse

from prometheus_client import Counter, Histogram
from pydantic import ValidationError

from cognis.artifacts.store import sanitize_artifact_filename
from cognis.core.agent_profiles import (
    normalize_agent_profile_id,
    requested_agent_profile_id,
    resolve_agent_profile,
)
from cognis.core.anchored_output import AnchoredTextBuilder, compact_snippet
from cognis.core.attachment_utils import (
    attachment_note,
    attachment_refs_to_dicts,
    merge_content_and_attachment_note,
    normalize_attachment_refs,
    strip_attachment_payload_bytes,
)
from cognis.core.chat_modes import ResolvedChatMode, normalize_chat_mode, plan_mode_reminder
from cognis.core.compaction import ROTATION_TOTAL, CompactionModelContext, CompactionResult
from cognis.core.context import _native_attachment_blocks
from cognis.core.context_budget import resolve_context_budget
from cognis.core.context_projection import (
    DEFAULT_COMPACTED_TOOL_GROUPS,
    PressureMode,
    PressureSnapshot,
    ProjectionPolicy,
    ProjectionPressureMode,
    ProjectionTurnState,
    ReprojectDecision,
    _estimated_messages_tokens,
    project_messages,
    should_reproject,
    tool_transcript_prefix_fingerprint,
)
from cognis.core.credential_grants import (
    grant_credential_to_agent,
    grant_credential_to_agent_definition,
)
from cognis.core.decision import build_routing_reminder
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import (
    FollowUpMetadata,
    FollowUpMode,
    FollowUpPolicy,
    build_history_boundary_message,
    render_follow_up_block,
)
from cognis.core.harness_guards import (
    LoopGuardState,
    argument_sanity_rejection_payload,
    check_argument_sanity,
    check_loop_guard,
    loop_guard_rejection_payload,
    record_tool_call,
)
from cognis.core.json_utils import extract_json_object, extract_visible_text_from_response
from cognis.core.managed_conversations import last_managed_conversation_user_message_for_retry
from cognis.core.orchestration_policy import (
    is_managed_agent_conversation_context,
    orchestration_surface_policy,
)
from cognis.core.project_context import (
    PROJECT_CONTEXT_STATUS_LOADED,
    ProjectContextEntry,
    ProjectMetadataEntry,
    normalize_project_path,
    project_context_event_data,
    project_metadata_event_data,
)
from cognis.core.project_runtime import (
    project_metadata_entry_from_resolution,
    resolve_project_metadata_for_path,
    resolve_project_metadata_for_project_id,
)
from cognis.core.prompts import PromptContext, build_visible_edit_tool_guidance
from cognis.core.pruning import prune_tool_outputs
from cognis.core.question_sets import (
    normalize_context,
    normalize_questions,
    validate_reply_for_questions,
)
from cognis.core.runtime import (
    ExecutorEnvironmentSnapshot,
    ResolvedStepRuntime,
    environment_from_metadata,
)
from cognis.core.step_profiles import (
    resolve_step_profile,
    step_profile_allows_tool,
    step_profile_visible_by_default,
)
from cognis.core.title_policy import publish_conversation_title_updated, sync_intaris_title
from cognis.core.tool_arguments import ToolArgumentError, validate_tool_arguments
from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    prepare_tool_exposure,
    reverse_tool_argument_aliases,
)
from cognis.core.tool_output_presentation import build_transport_tool_output_preview
from cognis.core.truncation import middle_truncate
from cognis.json_stream import merge_incremental_json_fragment, recover_trailing_json_object
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import AttachmentRef
from cognis.models.deliverable import Deliverable
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    SessionEvent,
    SessionModel,
    with_session_events_turn_id,
)
from cognis.models.tool import (
    Permission,
    ToolCall,
    ToolCapability,
    ToolDefinition,
    ToolResult,
    ToolSource,
    stable_tool_id,
    tool_capabilities,
    tool_display_name,
    tool_matches_identifier,
    tool_profile_group,
)
from cognis.models.workflow import (
    CompletionDeliveryPolicy,
    StepDefinition,
    StepOutput,
    Workflow,
    WorkflowState,
)
from cognis.providers.llm.errors import (
    LLMStreamIdleTimeout,
    LLMStreamProviderError,
    MidStreamErrorCategory,
    MidStreamErrorPayload,
    OpenAIToolSearchFallbackRequired,
    ToolArgumentParseFailure,
)
from cognis.providers.llm.retry import (
    DEFAULT_MID_STREAM_RETRY_POLICY,
    LLMContextOverflowError,
    RetryPolicy,
    compute_retry_delay,
    is_context_overflow_error,
)
from cognis.providers.retry import is_retryable_http_error
from cognis.runtime_context import (  # noqa: F401 — used in delegation
    RuntimeAccessContext,
    current_effective_working_directory,
    current_workspace_root,
    scoped_runtime_context,
)
from cognis.store.queries import (
    create_deliverable,
    get_deliverable,
    get_setting_value,
    get_step_run,
    list_deliverables_for_step_run,
    update_step_run,
)
from cognis.tools.builtin.orchestration import (
    OrchestrationMode,
    handle_delegate_tool_call,
    is_composition_tool,
    is_managed_conversation_tool,
    is_orchestration_tool,
    is_subsession_tool,
    is_task_tool,
    is_workflow_tool,
)
from cognis.tools.builtin.tool_output import is_tool_output_tool
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL, search_inventory
from cognis.tools.classification import classify_tool_definitions_sync, resolve_tool_classifications
from cognis.tools.executor.project_context import INTERNAL_PROJECT_CONTEXT_PROBE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry

logger = get_logger(__name__)

_SAME_EXECUTOR_AUTO_RETRY_TOOL_ALLOWLIST = frozenset(
    {
        "glob",
        "grep",
        "list_directory",
        "lsp",
        "read",
        "web_fetch",
        "web_map",
        "web_search",
    }
)
_COGNIS_ARTIFACT_URL_RE = re.compile(r"https://cognis\.fpy\.cz/api/v1/artifacts/content/[^\s\"']+")
_BACKGROUND_SHELL_STATUS_REMINDER_LIMIT = 3
_BACKGROUND_WORK_STATUS_REMINDER_LIMIT = 3
_BACKGROUND_WORK_STALE_AFTER_SECONDS = 300

_DELEGATION_RESULT_MAX_CHARS = 120_000
_DELEGATION_RESULT_INLINE_MAX_CHARS = 50_000
_DELEGATION_SALVAGE_MAX_TOOL_RESULTS = 20
_DELEGATION_SALVAGE_RESULT_PREVIEW_CHARS = 2_000
_DELEGATION_RESULT_TRUNCATION_TEMPLATE = "\n\n[Output truncated: original length {original_length} chars, stored first {max_chars} chars.]"
_DELEGATION_ACTIVE_DELIVERABLE_STATUSES = frozenset({"buffered", "approved", "delivered"})


@dataclass(slots=True)
class _DelegationResultContent:
    content: str
    anchors: list[dict[str, Any]]
    source: str
    truncated: bool = False
    original_length: int | None = None
    message_count: int = 0


@dataclass(frozen=True, slots=True)
class _ArtifactFetchFailure:
    url: str
    artifact_id: str | None
    filename: str | None


def _truncate_delegation_result_content(content: str) -> tuple[str, bool, int | None]:
    if len(content) <= _DELEGATION_RESULT_MAX_CHARS:
        return content, False, None
    notice = _DELEGATION_RESULT_TRUNCATION_TEMPLATE.format(
        original_length=len(content),
        max_chars=_DELEGATION_RESULT_MAX_CHARS,
    )
    return content[:_DELEGATION_RESULT_MAX_CHARS].rstrip() + notice, True, len(content)


def _build_delegation_message_result(messages: list[str]) -> _DelegationResultContent:
    builder = AnchoredTextBuilder()
    for index, message in enumerate(messages, start=1):
        label = compact_snippet(message, max_chars=80) if message else f"Assistant message {index}"
        builder.add_section(
            f"message:{index}",
            kind="section",
            label=f"Assistant message {index}: {label}",
            lines=[f"--- Assistant message {index} ---", *message.splitlines()],
        )
    content, _anchors = builder.build()
    content, truncated, original_length = _truncate_delegation_result_content(content)
    anchors = _anchors_from_delegation_content(content)
    return _DelegationResultContent(
        content=content,
        anchors=anchors,
        source="assistant_messages",
        truncated=truncated,
        original_length=original_length,
        message_count=len(messages),
    )


def _result_sections_from_content(
    content: str | None,
    anchors: list[dict[str, Any]],
    *,
    max_chars: int = 12_000,
) -> list[dict[str, Any]]:
    if not content or not anchors:
        return []
    lines = content.splitlines()
    sections: list[dict[str, Any]] = []
    used_chars = 0
    for anchor in anchors:
        start = anchor.get("start_line")
        end = anchor.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        section_text = "\n".join(lines[max(start - 1, 0) : min(end, len(lines))])
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        truncated = len(section_text) > remaining
        if truncated:
            section_text = section_text[:remaining].rstrip() + "\n[section truncated]"
        used_chars += len(section_text)
        sections.append({**anchor, "content": section_text, "truncated": truncated})
        if truncated:
            break
    return sections


def _anchors_from_delegation_content(content: str | None) -> list[dict[str, Any]]:
    if not content:
        return []
    lines = content.splitlines()
    anchors: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("[[") and stripped.endswith("]]"):
            anchor_id = stripped[2:-2]
            if not anchor_id.startswith("message:"):
                continue
            kind = "section"
            label = f"Assistant message {anchor_id.split(':', 1)[1]}"
        else:
            match = re.fullmatch(r"\[assistant_message:(\d+)\]", stripped)
            if not match:
                continue
            anchor_id = f"assistant_message:{match.group(1)}"
            kind = "assistant_message"
            label = f"Assistant message {match.group(1)}"
        if current is not None:
            current["end_line"] = line_no - 1
            anchors.append(current)
        current = {
            "anchor": anchor_id,
            "label": label,
            "kind": kind,
            "start_line": line_no,
            "end_line": line_no,
        }
    if current is not None:
        current["end_line"] = len(lines)
        anchors.append(current)
    return anchors


_BOOTSTRAP_INTENTION_WAIT_MS = 1500
_INTARIS_RETRY_POLL_SECONDS = 5.0
_INTARIS_MAX_RECOVERY_WAIT_SECONDS = 60.0
_INTARIS_ESCALATION_REMOTE_POLL_SECONDS = 2.0


def _user_message_for_recording(content: str, attachments: list[AttachmentRef]) -> str:
    """Return the content to persist for a user message event.

    The original content is always preserved as-is so that attachment-only
    messages record an empty string instead of a synthetic placeholder.  The
    UI optimistic-bubble deduplication relies on the persisted/broadcast
    content matching the content the user actually typed (empty string when
    only files were attached).  Placeholder text for the LLM context is
    injected separately during prompt assembly.
    """
    return content


def _indent_block(text: str, *, prefix: str = "    ") -> list[str]:
    """Render multiline text as an indented block."""

    if not text:
        return []
    return [f"{prefix}{line}" for line in text.splitlines()]


def _json_text(value: Any) -> str:
    """Render a JSON-serializable value as pretty text."""

    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _dominant_newline(text: str) -> str:
    """Return the dominant newline sequence found in *text*."""

    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= max(lf, cr) and crlf > 0:
        return "\r\n"
    if lf >= cr and lf > 0:
        return "\n"
    if cr > 0:
        return "\r"
    return "\n"


def _task_log_anchor_kind(event_type: str) -> str:
    """Map session event types to stable anchor prefixes."""

    mapping = {
        "assistant_message": "assistant",
        "assistant_thinking": "thinking",
        "user_message": "user",
        "reasoning": "reasoning",
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "lifecycle": "lifecycle",
        "delegation": "delegation",
        "system_message": "system_message",
    }
    return mapping.get(event_type, "event")


# Prometheus metrics
STEPS_TOTAL = Counter(
    "cognis_steps_total",
    "Step executions",
    labelnames=("step_type", "status"),
)
STEP_DURATION = Histogram(
    "cognis_step_duration_seconds",
    "Step execution duration",
    labelnames=("phase",),
)
STEP_TOOL_CALLS = Counter(
    "cognis_step_tool_calls_total",
    "Tool calls per step",
    labelnames=("tool_name",),
)
STEP_REPROMPTS = Counter(
    "cognis_step_reprompts_total",
    "Re-prompts for missing step_complete",
)
DELEGATIONS_TOTAL = Counter(
    "cognis_delegations_total",
    "Sub-session delegations spawned",
    labelnames=("status",),
)
HARNESS_GUARD_TRIPS = Counter(
    "cognis_harness_guard_trips_total",
    "Tool calls rejected by harness guards before executor dispatch",
    labelnames=("guard", "tool_name"),
)
STEP_COMPLETE_REJECTIONS = Counter(
    "cognis_step_complete_rejections_total",
    "step_complete calls rejected by the controller",
    labelnames=("reason",),
)
SESSION_LOCKS_EVICTED_TOTAL = Counter(
    "cognis_session_locks_evicted_total",
    "SessionLock entries evicted from the per-session lock map.",
    labelnames=("reason",),
)
AUTO_COMPACTION_DURATION = Histogram(
    "cognis_auto_compaction_duration_seconds",
    "Duration of automatic post-turn compaction (compact + rotate + cache)",
)
AUDIT_EVENTS_TOTAL = Counter(
    "cognis_audit_events_total",
    "LLM exposure audit events recorded to Intaris.",
    labelnames=("type", "source"),
)
LLM_MID_STREAM_ERRORS_TOTAL = Counter(
    "cognis_llm_mid_stream_errors_total",
    "LLM mid-stream failures grouped by stable error category.",
    labelnames=("provider_id", "model", "category"),
)
LLM_TOOL_ARGUMENT_PARSE_FAILURES_TOTAL = Counter(
    "cognis_llm_tool_argument_parse_failures_total",
    "Malformed streamed tool-call arguments rejected before tool dispatch.",
    labelnames=("provider_id", "tool"),
)
AGENT_CYCLE_PREP_DURATION = Histogram(
    "cognis_agent_cycle_prep_duration_seconds",
    "Duration of per-cycle model/tool exposure and prompt projection before LLM dispatch.",
    labelnames=("provider_id", "model", "step_type"),
)
AGENT_CYCLE_LLM_DURATION = Histogram(
    "cognis_agent_cycle_llm_duration_seconds",
    "Duration of one agent-loop LLM stream cycle.",
    labelnames=("provider_id", "model", "status", "step_type"),
)
AGENT_CYCLE_INTER_GAP_DURATION = Histogram(
    "cognis_agent_cycle_inter_gap_seconds",
    "Time between the previous LLM cycle ending and the next cycle starting.",
    labelnames=("step_type",),
)
TOOL_BATCH_DURATION = Histogram(
    "cognis_tool_batch_duration_seconds",
    "Duration of agent-loop regular tool batches.",
    labelnames=("mode", "outcome", "step_type"),
)
TOOL_EXECUTION_DURATION = Histogram(
    "cognis_tool_execution_duration_seconds",
    "Duration of individual regular tool executions.",
    labelnames=("tool_name", "route", "outcome"),
)
AUTO_COMPACTION_TIMEOUT_SECONDS = 300
LLM_STREAM_REASONING_IDLE_MULTIPLIER = 3
LLM_STREAM_MAX_REASONING_IDLE_TIMEOUT_SECONDS = 900

PROJECTION_CYCLES_TOTAL = Counter(
    "cognis_projection_cycles_total",
    "Within-turn projection cycle decisions.",
    labelnames=("decision",),
)
PROJECTION_PRESSURE_TRANSITIONS_TOTAL = Counter(
    "cognis_projection_pressure_transitions_total",
    "Pressure mode transitions during within-turn projection.",
    labelnames=("from_mode", "to_mode"),
)
PROJECTION_FORCED_CRITICAL_TOTAL = Counter(
    "cognis_projection_forced_critical_total",
    "Projections forced to critical mode to preserve monotonicity.",
    labelnames=("reason",),
)


def _agent_loop_step_type(ctx: Any) -> str:
    policy = getattr(ctx, "policy", None)
    if policy is not None and not getattr(policy, "require_step_complete", True):
        return "direct"
    return "workflow_step"


# Controller-injected tool names
STEP_COMPLETE = "step_complete"
WRITE_DELIVERABLE = "write_deliverable"
STEP_REQUEST_QUESTIONS = "step_request_questions"
REQUEST_CREDENTIAL = "request_credential"
REQUEST_AUTH_CHALLENGE = "request_auth_challenge"
LIST_CREDENTIALS = "list_credentials"
STEP_TODO_WRITE = "step_todo_write"
STEP_TODO_LIST = "step_todo_list"
SWITCH_EXECUTOR = "switch_executor"  # Stage 36: multi-executor agents
CONTROLLER_TOOLS = {
    WRITE_DELIVERABLE,
    STEP_COMPLETE,
    STEP_REQUEST_QUESTIONS,
    REQUEST_CREDENTIAL,
    REQUEST_AUTH_CHALLENGE,
    LIST_CREDENTIALS,
    STEP_TODO_WRITE,
    STEP_TODO_LIST,
    SWITCH_EXECUTOR,
    SEARCH_TOOLS_TOOL.name,
}

# Tools whose arguments are validated by their dedicated controller
# handlers below. The argument-sanity gate skips these to avoid
# double-validating (and to avoid rejecting controller-owned schema
# choices such as empty-arg ``step_todo_list``).
_CONTROLLER_INTERCEPTED_TOOLS: frozenset[str] = frozenset(CONTROLLER_TOOLS)
_FINALIZATION_TOOLS: frozenset[str] = frozenset({STEP_TODO_WRITE, WRITE_DELIVERABLE, STEP_COMPLETE})


def _allowed_finalization_tools(instruction: dict[str, str]) -> frozenset[str]:
    """Return tools allowed while terminal todos require finalization."""

    if instruction.get("required_action") == "write_deliverable_then_step_complete":
        return _FINALIZATION_TOOLS
    if instruction.get("required_action") == "write_result":
        # System-agent delegation: only allow step_todo_write and step_complete.
        # No executor tools — model must write assistant text, not more tool calls.
        return frozenset({STEP_TODO_WRITE, STEP_COMPLETE})
    return frozenset({STEP_TODO_WRITE, STEP_COMPLETE})


# Callback types
TokenCallback = Callable[[str], Coroutine[Any, Any, None]]
ToolCallCallback = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, None]]
ToolResultCallback = Callable[..., Coroutine[Any, Any, None]]
ToolOutputChunkCallback = Callable[..., Coroutine[Any, Any, None]]
ToolProgressCallback = Callable[..., Coroutine[Any, Any, None]]

# Default limits
DEFAULT_MAX_TOOL_CALLS = 200
DEFAULT_STEP_TIMEOUT_SECONDS = 3600  # 1 hour
# Secondary delegations follow OpenCode's ``agent.steps`` semantics: the
# optional cap counts LLM iterations, not individual tool calls. No default cap
# is applied; cancellation, timeout, and context pressure remain safety rails.
DELEGATION_MAX_STEPS_KEYS = ("steps", "delegation_max_steps", "max_steps")
DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS = 60
DEFAULT_LLM_STREAM_MAX_RETRIES = 3
_MAX_TOOL_DATA_BYTES = 10_240  # 10 KB truncation limit for WS events
_MAX_AGENT_VISIBLE_TOOL_RESULT = 50_000
_MAX_INTARIS_TOOL_RESULT = _MAX_AGENT_VISIBLE_TOOL_RESULT
_MAX_FILE_DIFF_BYTES = 120_000
_MAX_FILE_DIFFS = 20
_MAX_TODO_REPROMPTS = 3  # Max re-prompts for incomplete todos before force-completing
_MAX_STEP_COMPLETE_REPROMPTS = 3
_MAX_EMPTY_DIRECT_RESPONSE_REPROMPTS = 2
_MAX_TOOL_CALL_ARGUMENT_CHARS = 256_000
_MAX_FAILED_TOOL_ARGUMENT_PREVIEW_CHARS = 4_096
_DELIVERABLE_PREVIEW_CHARS = 240
_PROJECT_TOUCH_TOOL_NAMES = frozenset(
    {
        "read",
        "write",
        "edit",
        "apply_patch",
        "multiedit",
        "list_directory",
        "glob",
        "grep",
        "bash",
        "artifact_publish",
    }
)
_READ_ONLY_PROJECT_TOUCH_TOOL_NAMES = frozenset(
    {"read", "list_directory", "glob", "grep", "artifact_publish"}
)
_PARALLEL_MUTATION_TOOL_NAMES = frozenset(
    {
        "write",
        "edit",
        "multiedit",
        "apply_patch",
        "artifact_save",
        "document_generate",
        "artifact_publish",
    }
)
_NON_EMPTY_TOOL_ARGUMENT_NAMES = frozenset({"apply_patch"})


def _normalize_todo_status(status: Any) -> str:
    """Return the canonical status for persisted todo entries."""

    if status == "done":
        return "completed"
    return status if isinstance(status, str) else "pending"


def _normalize_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize todo payloads so old persisted values still work."""

    normalized: list[dict[str, Any]] = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        item = dict(todo)
        item["status"] = _normalize_todo_status(item.get("status"))
        normalized.append(item)
    return normalized


def _delegation_progress_todos(ctx: StepContext) -> list[dict[str, Any]]:
    """Return a compact, UI-safe todo snapshot for delegation progress events."""

    return [
        {
            "content": str(todo.get("content") or ""),
            "status": str(todo.get("status") or "pending"),
            "priority": str(todo.get("priority") or "medium"),
        }
        for todo in _normalize_todos(ctx.todos or [])
        if str(todo.get("content") or "").strip()
    ]


def _pending_todos_snapshot(ctx: StepContext) -> list[dict[str, str]]:
    """Return a compact snapshot of non-terminal todos for continuation context."""

    snapshot: list[dict[str, str]] = []
    for todo in _normalize_todos(ctx.todos or []):
        content = str(todo.get("content") or "").strip()
        if not content:
            continue
        status = str(todo.get("status") or "pending")
        if status in {"completed", "cancelled"}:
            continue
        snapshot.append({"content": content, "status": status})
    return snapshot


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _artifact_failures_from_provider_fetch_error(error_text: str) -> list[_ArtifactFetchFailure]:
    """Extract Cognis artifact references from provider URL-fetch failures."""

    if "Timeout while downloading" not in error_text or '"param": "url"' not in error_text:
        return []

    failures: list[_ArtifactFetchFailure] = []
    seen_urls: set[str] = set()
    for match in _COGNIS_ARTIFACT_URL_RE.findall(error_text):
        url = match.rstrip(".\\,")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        artifact_id: str | None = None
        filename: str | None = None
        path_parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
        try:
            content_index = path_parts.index("content")
        except ValueError:
            content_index = -1
        if content_index >= 0 and len(path_parts) > content_index + 2:
            object_id = path_parts[content_index + 2]
            if object_id:
                artifact_id = object_id
        if content_index >= 0 and len(path_parts) > content_index + 3:
            filename = path_parts[content_index + 3]
        failures.append(_ArtifactFetchFailure(url=url, artifact_id=artifact_id, filename=filename))
    return failures


def _artifact_failures_from_error_payload(
    payload: dict[str, Any] | None,
) -> list[_ArtifactFetchFailure]:
    """Extract Cognis artifact references from a structured stream error payload."""

    if not isinstance(payload, dict):
        return []
    if payload.get("category") != MidStreamErrorCategory.ARTIFACT_FETCH.value:
        return []
    urls = [url for url in payload.get("artifact_urls") or [] if isinstance(url, str)]
    ids = [
        artifact_id
        for artifact_id in payload.get("artifact_ids") or []
        if isinstance(artifact_id, str)
    ]
    failures: list[_ArtifactFetchFailure] = []
    for url in urls:
        artifact_id: str | None = None
        filename: str | None = None
        path_parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
        with contextlib.suppress(ValueError):
            content_index = path_parts.index("content")
            if len(path_parts) > content_index + 2:
                artifact_id = path_parts[content_index + 2]
            if len(path_parts) > content_index + 3:
                filename = path_parts[content_index + 3]
        failures.append(_ArtifactFetchFailure(url=url, artifact_id=artifact_id, filename=filename))
    for artifact_id in ids:
        if not any(failure.artifact_id == artifact_id for failure in failures):
            failures.append(_ArtifactFetchFailure(url="", artifact_id=artifact_id, filename=None))
    return failures


def _artifact_fetch_failure_notice(failures: list[_ArtifactFetchFailure]) -> str | None:
    """Build an LLM-facing notice for attachments removed after provider fetch failures."""

    lines: list[str] = []
    seen: set[tuple[str | None, str | None]] = set()
    for failure in failures:
        key = (failure.artifact_id, failure.filename)
        if key in seen:
            continue
        seen.add(key)
        label = failure.filename or failure.artifact_id or "attachment"
        if failure.artifact_id:
            lines.append(
                f'- "{label}" (artifact_id="{failure.artifact_id}"); retrieve again with '
                f'artifact_read artifact_id="{failure.artifact_id}" if needed.'
            )
        else:
            lines.append(f'- "{label}"; retrieve it again with artifact tools if needed.')
    if not lines:
        return None
    return (
        "The following attachment(s) were removed from native model input because the "
        "provider failed to download their artifact URL:\n" + "\n".join(lines)
    )


def _strip_disabled_artifact_urls_from_messages(
    messages: list[dict[str, Any]],
    disabled_urls: set[str],
) -> None:
    """Remove failed provider-fetch artifact URLs from already assembled prompt messages."""

    if not disabled_urls:
        return
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        replacement: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                replacement.append(part)
                continue
            file_part = part.get("file")
            image_part = part.get("image_url")
            url = None
            if isinstance(file_part, dict):
                url = file_part.get("file_url")
            elif isinstance(image_part, dict):
                url = image_part.get("url")
            elif isinstance(image_part, str):
                url = image_part
            if isinstance(url, str) and url in disabled_urls:
                continue
            replacement.append(part)
        if len(replacement) != len(content):
            message["content"] = replacement


def _llm_stream_chunk_has_activity(chunk: dict[str, Any]) -> bool:
    """Return whether a stream chunk should reset the meaningful-activity watchdog."""

    if chunk.get("mid_stream_failure") or chunk.get("error") or chunk.get("done"):
        return True
    if chunk.get("usage"):
        return True
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                return True
            for key in ("delta", "message"):
                value = choice.get(key)
                if _llm_stream_value_has_activity(value):
                    return True
    return any(
        _llm_stream_value_has_activity(chunk.get(key))
        for key in ("content", "reasoning_content", "tool_calls", "function_call")
    )


def _llm_stream_chunk_has_provider_liveness(chunk: dict[str, Any]) -> bool:
    """Return whether a chunk proves the provider stream is still alive."""

    if _llm_stream_chunk_has_activity(chunk):
        return True
    if chunk.get("provider_event") or chunk.get("provider_event_type"):
        return True
    choices = chunk.get("choices")
    return isinstance(choices, list)


def _llm_stream_chunk_has_reasoning_liveness(chunk: dict[str, Any]) -> bool:
    """Return whether a chunk indicates model-side reasoning/progress."""

    if chunk.get("provider_event_type") in {
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
    }:
        return True
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict) and (
                    "reasoning" in delta
                    or "reasoning_content" in delta
                    or "reasoning_part_boundary" in delta
                ):
                    return True
    return False


@dataclass(slots=True)
class LLMStreamIdleStats:
    raw_chunks: int = 0
    meaningful_chunks: int = 0
    reasoning_chunks: int = 0
    first_raw_after_seconds: float | None = None
    first_activity_after_seconds: float | None = None
    first_reasoning_after_seconds: float | None = None
    last_raw_after_seconds: float | None = None
    last_activity_after_seconds: float | None = None
    last_reasoning_after_seconds: float | None = None

    @property
    def timeout_phase(self) -> str:
        if self.raw_chunks <= 0:
            return "raw"
        if self.reasoning_chunks > 0 and self.meaningful_chunks <= 0:
            return "reasoning"
        return "activity"

    def as_dict(self) -> dict[str, Any]:
        def _seconds(value: float | None) -> float | None:
            return round(value, 2) if value is not None else None

        return {
            "raw_chunks": self.raw_chunks,
            "meaningful_chunks": self.meaningful_chunks,
            "reasoning_chunks": self.reasoning_chunks,
            "first_raw_after_seconds": _seconds(self.first_raw_after_seconds),
            "first_activity_after_seconds": _seconds(self.first_activity_after_seconds),
            "first_reasoning_after_seconds": _seconds(self.first_reasoning_after_seconds),
            "last_raw_after_seconds": _seconds(self.last_raw_after_seconds),
            "last_activity_after_seconds": _seconds(self.last_activity_after_seconds),
            "last_reasoning_after_seconds": _seconds(self.last_reasoning_after_seconds),
        }


def _llm_stream_value_has_activity(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, list):
        return bool(value) and any(_llm_stream_value_has_activity(item) for item in value)
    if isinstance(value, dict):
        if "tool_calls" in value or "function_call" in value:
            return True
        activity_keys = (
            "content",
            "reasoning_content",
            "arguments",
            "name",
            "id",
            "text",
            "delta",
            "refusal",
        )
        return any(_llm_stream_value_has_activity(value.get(key)) for key in activity_keys)
    return False


def _idle_timeout_payload(message: str, phase: str) -> dict[str, Any]:
    category = {
        "raw": MidStreamErrorCategory.IDLE_TIMEOUT_RAW.value,
        "reasoning": MidStreamErrorCategory.IDLE_TIMEOUT_REASONING.value,
    }.get(phase, MidStreamErrorCategory.IDLE_TIMEOUT_ACTIVITY.value)
    return {"category": category, "code": phase, "message": message}


async def _iterate_llm_stream_with_idle_timeout(
    stream: AsyncIterator[dict[str, Any]],
    *,
    idle_timeout_seconds: int,
    stats: LLMStreamIdleStats | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield stream chunks while enforcing provider-liveness and activity timeouts."""

    timeout = max(1, int(idle_timeout_seconds))
    reasoning_timeout = max(
        timeout,
        min(
            timeout * LLM_STREAM_REASONING_IDLE_MULTIPLIER,
            LLM_STREAM_MAX_REASONING_IDLE_TIMEOUT_SECONDS,
        ),
    )
    started_at = monotonic()
    raw_deadline = started_at + timeout
    activity_deadline = started_at + timeout
    iterator = aiter(stream)
    stats = stats or LLMStreamIdleStats()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            now = monotonic()
            remaining = min(raw_deadline, activity_deadline) - now
            if remaining <= 0:
                message = _llm_stream_idle_timeout_message(
                    stats,
                    raw_timeout_seconds=timeout,
                    activity_timeout_seconds=reasoning_timeout
                    if stats.reasoning_chunks > 0 and stats.meaningful_chunks <= 0
                    else timeout,
                )
                raise LLMStreamIdleTimeout(
                    message,
                    payload=_idle_timeout_payload(message, stats.timeout_phase),
                )
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                message = _llm_stream_idle_timeout_message(
                    stats,
                    raw_timeout_seconds=timeout,
                    activity_timeout_seconds=reasoning_timeout
                    if stats.reasoning_chunks > 0 and stats.meaningful_chunks <= 0
                    else timeout,
                )
                raise LLMStreamIdleTimeout(
                    message,
                    payload=_idle_timeout_payload(message, stats.timeout_phase),
                ) from exc
            except Exception as exc:
                if is_context_overflow_error(exc):
                    raise
                raise LLMStreamProviderError(f"{type(exc).__name__}: {exc}") from exc
            now = monotonic()
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            elapsed = now - started_at
            if _llm_stream_chunk_has_provider_liveness(chunk):
                stats.raw_chunks += 1
                stats.first_raw_after_seconds = stats.first_raw_after_seconds or elapsed
                stats.last_raw_after_seconds = elapsed
                raw_deadline = now + timeout
            if _llm_stream_chunk_has_reasoning_liveness(chunk):
                stats.reasoning_chunks += 1
                stats.first_reasoning_after_seconds = stats.first_reasoning_after_seconds or elapsed
                stats.last_reasoning_after_seconds = elapsed
                if stats.meaningful_chunks <= 0:
                    activity_deadline = now + reasoning_timeout
            if _llm_stream_chunk_has_activity(chunk):
                stats.meaningful_chunks += 1
                stats.first_activity_after_seconds = stats.first_activity_after_seconds or elapsed
                stats.last_activity_after_seconds = elapsed
                activity_deadline = now + timeout
            yield chunk
    except (
        LLMStreamIdleTimeout,
        LLMStreamProviderError,
        LLMContextOverflowError,
        asyncio.CancelledError,
    ):
        closer = getattr(iterator, "aclose", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                await closer()
        raise


def _llm_stream_idle_timeout_message(
    stats: LLMStreamIdleStats,
    *,
    raw_timeout_seconds: int,
    activity_timeout_seconds: int,
) -> str:
    if stats.raw_chunks <= 0:
        return f"LLM stream produced no provider events for {raw_timeout_seconds}s"
    if stats.reasoning_chunks > 0 and stats.meaningful_chunks <= 0:
        return (
            "LLM stream produced provider reasoning events but no meaningful output "
            f"for {activity_timeout_seconds}s"
        )
    return f"LLM stream produced no meaningful activity for {activity_timeout_seconds}s"


def _is_llm_idle_timeout_error(message: str) -> bool:
    return "LLM stream produced no " in message or (
        "LLM stream produced provider reasoning events but no meaningful output" in message
    )


def _should_auto_continue_after_mid_stream_failure(message: str) -> bool:
    """Return whether an exhausted mid-stream failure should start a new cycle."""

    del message
    return True


_MODEL_ERROR_CONTINUATION_MAX_ATTEMPTS = 1
_IDLE_TIMEOUT_CONTINUATION_MAX_ATTEMPTS = 3
_IDLE_TIMEOUT_CATEGORIES = {
    MidStreamErrorCategory.IDLE_TIMEOUT_RAW.value,
    MidStreamErrorCategory.IDLE_TIMEOUT_ACTIVITY.value,
    MidStreamErrorCategory.IDLE_TIMEOUT_REASONING.value,
}
_RECOVERY_RETRYABLE_CATEGORIES = {
    MidStreamErrorCategory.RATE_LIMIT.value,
    MidStreamErrorCategory.PROVIDER_5XX.value,
    MidStreamErrorCategory.CONNECTION.value,
    MidStreamErrorCategory.OTHER.value,
    *_IDLE_TIMEOUT_CATEGORIES,
}
_RECOVERY_NON_RETRYABLE_CATEGORIES = {
    MidStreamErrorCategory.CONTENT_POLICY.value,
}


def _mid_stream_reason_class(
    details: dict[str, Any] | MidStreamErrorPayload | None,
    fallback: str = "unknown",
) -> str:
    if isinstance(details, dict):
        category = details.get("category")
        if isinstance(category, str) and category:
            return category
    return fallback


def _mid_stream_retry_after_seconds(
    details: dict[str, Any] | MidStreamErrorPayload | None,
) -> float | None:
    if not isinstance(details, dict):
        return None
    value = details.get("retry_after_seconds")
    try:
        if value is None:
            return None
        retry_after = float(value)
    except (TypeError, ValueError):
        return None
    return retry_after if retry_after >= 0 else None


def _mid_stream_retry_notice(
    *,
    provider_id: str | None,
    model: str | None,
    details: dict[str, Any] | MidStreamErrorPayload | None,
    error: str,
    delay_seconds: float,
    attempt: int,
    max_attempts: int,
) -> str:
    reason_class = _mid_stream_reason_class(details, "other")
    provider_label = provider_id or "default provider"
    model_label = model or "unknown model"
    wait_text = f"{delay_seconds:.1f}s" if delay_seconds < 10 else f"{round(delay_seconds)}s"
    if reason_class == MidStreamErrorCategory.RATE_LIMIT.value:
        reason = f"{provider_label} rate-limited the request"
    elif reason_class == MidStreamErrorCategory.PROVIDER_5XX.value:
        reason = f"{provider_label} returned a transient server error"
    elif reason_class == MidStreamErrorCategory.CONNECTION.value:
        reason = f"{provider_label} connection failed mid-stream"
    elif reason_class in _IDLE_TIMEOUT_CATEGORIES:
        reason = f"{provider_label} stopped producing useful stream output"
    else:
        reason = f"{provider_label} stream failed mid-generation"
    message = ""
    if isinstance(details, dict):
        raw_message = details.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            message = raw_message.strip()
    if not message:
        message = error.strip()
    suffix = f" Provider message: {message[:220]}" if message else ""
    return (
        f"{reason} while using {model_label}. Cognis will wait {wait_text} and retry "
        f"the LLM call ({attempt}/{max_attempts}).{suffix}"
    )


def _should_emit_mid_stream_retry_notice(
    details: dict[str, Any] | MidStreamErrorPayload | None,
) -> bool:
    """Return true when a retry notice carries actionable provider information."""

    reason_class = _mid_stream_reason_class(details, "other")
    return reason_class not in _IDLE_TIMEOUT_CATEGORIES


def _should_continue_after_exhausted_mid_stream_failure(
    message: str,
    details: dict[str, Any] | MidStreamErrorPayload | None,
) -> bool:
    if not _should_auto_continue_after_mid_stream_failure(message):
        return False
    reason_class = _mid_stream_reason_class(details, "other")
    return reason_class in _RECOVERY_RETRYABLE_CATEGORIES


_TODO_ECHO_CONTENT_MAX = 280


def _echo_todos_bounded(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return todos with per-item content truncated to a bounded length.

    Used for the ``step_todo_write`` tool-result echo. ``ctx.todos`` keeps
    the untruncated values; only what we hand back to the model is capped
    so very long todo content does not inflate context/token usage on
    every write. Truncation is rare in practice (typical todos are short
    action labels) and adds an ellipsis marker so the model can see the
    item was shortened.
    """

    bounded: list[dict[str, Any]] = []
    for todo in todos:
        content = todo.get("content")
        if isinstance(content, str) and len(content) > _TODO_ECHO_CONTENT_MAX:
            trimmed = content[: _TODO_ECHO_CONTENT_MAX - 1].rstrip() + "…"
            bounded.append({**todo, "content": trimmed, "content_truncated": True})
        else:
            bounded.append(todo)
    return bounded


def _truncate_tool_data(text: str) -> str:
    """Truncate tool data to a bounded size for WS events."""
    return build_transport_tool_output_preview(text, _MAX_TOOL_DATA_BYTES).result


def _truncate_file_diff(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_FILE_DIFF_BYTES:
        return text, False
    return (
        text[:_MAX_FILE_DIFF_BYTES] + f"\n... (diff truncated, {len(text)} bytes total)",
        True,
    )


def _context_pressure_compaction_notice(context_result: Any) -> str:
    prompt_tokens = int(getattr(context_result, "prompt_tokens", 0) or 0)
    max_context_tokens = int(getattr(context_result, "max_context_tokens", 0) or 0)
    available_prompt_tokens = int(getattr(context_result, "available_prompt_tokens", 0) or 0)
    usage_limit = available_prompt_tokens if available_prompt_tokens > 0 else max_context_tokens
    if usage_limit > 0:
        usage_pct = prompt_tokens / usage_limit * 100
        usage = f"{prompt_tokens:,}/{usage_limit:,} prompt-budget tokens ({usage_pct:.1f}%)"
    else:
        usage = f"{prompt_tokens:,} tokens"
    return (
        "Automatic compaction is starting before this turn continues because "
        f"the session context is over the compaction threshold. Current usage: {usage}. "
        "The current request has already been saved, and the turn will continue "
        "after the conversation history is compacted."
    )


def _provider_overflow_compaction_notice(
    *, provider_id: str | None, model_id: str | None, reason: str
) -> str:
    target = f"{provider_id or 'provider'} / {model_id or 'model'}"
    return (
        "The model provider rejected the request because the context window is full. "
        f"Provider/model: {target}; reason: {reason}. "
        "Cognis is compacting the saved conversation and will retry the turn in a "
        "fresh compacted session."
    )


def _context_pressure_metadata(context_result: Any) -> dict[str, Any]:
    prompt_tokens = int(getattr(context_result, "prompt_tokens", 0) or 0)
    max_context_tokens = int(getattr(context_result, "max_context_tokens", 0) or 0)
    max_input_tokens = int(getattr(context_result, "max_input_tokens", 0) or 0)
    available_prompt_tokens = int(getattr(context_result, "available_prompt_tokens", 0) or 0)
    if available_prompt_tokens <= 0 and max_context_tokens > 0:
        available_prompt_tokens = max_context_tokens
    compaction_threshold = float(getattr(context_result, "compaction_threshold", 0.0) or 0.0)
    compaction_threshold_prompt_tokens = int(
        getattr(context_result, "compaction_threshold_prompt_tokens", 0) or 0
    )
    if compaction_threshold_prompt_tokens <= 0 and available_prompt_tokens > 0:
        compaction_threshold_prompt_tokens = int(
            available_prompt_tokens * (compaction_threshold or 0.85)
        )
    loop_pressure_threshold_prompt_tokens = int(
        getattr(context_result, "loop_pressure_threshold_prompt_tokens", 0) or 0
    )
    if loop_pressure_threshold_prompt_tokens <= 0 and available_prompt_tokens > 0:
        loop_pressure_threshold_prompt_tokens = int(available_prompt_tokens * 0.95)
    return {
        "prompt_tokens": prompt_tokens,
        "max_context_tokens": max_context_tokens,
        "max_input_tokens": max_input_tokens,
        "available_prompt_tokens": available_prompt_tokens,
        "compaction_threshold_prompt_tokens": compaction_threshold_prompt_tokens,
        "loop_pressure_threshold_prompt_tokens": loop_pressure_threshold_prompt_tokens,
        "compaction_threshold": compaction_threshold or 0.85,
        "previous_usage_percentage": (
            round(prompt_tokens / max_context_tokens * 100, 1) if max_context_tokens > 0 else None
        ),
        "effective_usage_percentage": (
            round(prompt_tokens / available_prompt_tokens * 100, 1)
            if available_prompt_tokens > 0
            else None
        ),
        "hard_pressure_exceeded": (
            loop_pressure_threshold_prompt_tokens > 0
            and prompt_tokens >= loop_pressure_threshold_prompt_tokens
        )
        or (available_prompt_tokens > 0 and prompt_tokens > available_prompt_tokens),
    }


def _should_run_pre_turn_auto_compaction(ctx: StepContext, context_result: Any) -> bool:
    """Return true when pre-turn auto-compaction should run for this attempt.

    Retry attempts normally skip pre-turn compaction so a fresh compacted
    session can make progress. Hard pressure is different: if a rotated retry
    is still at/over the loop-pressure ceiling, continuing into the model loop
    only delays the inevitable overflow. Compact again before the model call
    and let the bounded recursion guard stop truly non-shrinking sessions.
    """

    if not ctx.policy.enable_auto_compaction:
        return False
    if not getattr(context_result, "recommend_compaction", False):
        return False
    if not ctx.is_retry:
        return True
    if int(ctx.runtime_info.get("provider_overflow_recoveries", 0) or 0) > 0:
        return True
    return bool(_context_pressure_metadata(context_result).get("hard_pressure_exceeded"))


def _should_run_post_turn_auto_compaction(ctx: StepContext, context_result: Any) -> bool:
    """Return true when post-turn compaction is still needed after projection.

    Cross-turn context assembly can recommend compaction for the raw transcript,
    but the actual model-facing prompt may be safely reduced by within-turn
    projection.  Post-turn compaction should not rotate a session solely because
    of a stale pre-projection recommendation; unresolved pressure is handled by
    the pre-model/tool-loop pressure paths before the turn completes.
    """

    if not ctx.policy.enable_auto_compaction:
        return False
    if not getattr(context_result, "recommend_compaction", False):
        return False

    latest_projection_exceeded = getattr(ctx, "last_projection_exceeded_selected_budget", None)
    if latest_projection_exceeded is False:
        return False
    if latest_projection_exceeded is True:
        return True

    # If no exact projection snapshot was recorded, preserve the previous
    # conservative behavior and compact based on the assembly recommendation.
    return True


def _has_compactable_pre_turn_history(
    ctx: StepContext,
    session_cache: Any,
    *,
    preserve_turns: int | None = None,
) -> bool:
    """Return true when pre-turn compaction has older turns to remove.

    Pre-turn pressure can be caused by same-turn tool transcript replay during
    automatic continuations.  In that shape there may be little or no old chat
    history for compaction to summarize; within-turn projection is the correct
    pressure response.  Preserve existing behavior when cache state is missing
    or unavailable so compaction still has a chance in uncertain cases.
    """

    cache_get_entry = getattr(session_cache, "get_entry", None)
    cache_get_events = getattr(session_cache, "get_events_since_compaction", None)
    if not callable(cache_get_entry) or not callable(cache_get_events):
        return True
    try:
        cache_entry = cache_get_entry(ctx.session.session_id)
    except Exception:
        return True
    if cache_entry is None:
        return True
    try:
        raw_events = cache_get_events(
            ctx.session.session_id,
            ["user_message", "assistant_message"],
        )
    except Exception:
        return True
    if not isinstance(raw_events, list):
        return True
    relevant_events: list[Any] = raw_events
    if preserve_turns is None:
        preserve_turns = 10
    try:
        preserve_turns = int(preserve_turns)
    except (TypeError, ValueError):
        preserve_turns = 10
    user_turns = sum(
        1
        for event in relevant_events
        if (
            getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        )
        == "user_message"
    )
    return user_turns > preserve_turns


def _compaction_model_context(ctx: StepContext) -> CompactionModelContext:
    return CompactionModelContext(
        model=ctx.current_model,
        provider_id=ctx.current_provider_id,
        reasoning_effort=ctx.runtime_info.get("current_reasoning_effort"),
    )


def _file_diffs_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return sanitized file diff metadata safe for API/UI payloads."""

    if not isinstance(metadata, dict):
        return []
    raw_diffs = metadata.get("file_diffs")
    if not isinstance(raw_diffs, list):
        return []
    diffs: list[dict[str, Any]] = []
    for item in raw_diffs[:_MAX_FILE_DIFFS]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        diff = item.get("diff")
        if not path or not isinstance(diff, str) or not diff:
            continue
        truncated_diff, truncated = _truncate_file_diff(diff)
        payload: dict[str, Any] = {"path": path, "diff": truncated_diff}
        if truncated:
            payload["truncated"] = True
            payload["original_size"] = len(diff)
        diffs.append(payload)
    if len(raw_diffs) > _MAX_FILE_DIFFS:
        diffs.append(
            {
                "path": "",
                "diff": "",
                "truncated": True,
                "omitted_count": len(raw_diffs) - _MAX_FILE_DIFFS,
            }
        )
    return diffs


def _bounded_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-safe tool-call arguments for persisted history/events."""

    serialized = json.dumps(arguments, default=str)
    if len(serialized) <= _MAX_TOOL_DATA_BYTES:
        return arguments
    return {
        "_truncated": True,
        "_original_size": len(serialized),
        "_preview": _truncate_tool_data(serialized),
    }


def _delegation_title(arguments: dict[str, Any]) -> str:
    raw = str(arguments.get("title") or arguments.get("task") or "Delegated work").strip()
    if not raw:
        return "Delegated work"
    first_line = raw.splitlines()[0].strip() or raw.strip()
    return first_line if len(first_line) <= 96 else first_line[:93].rstrip() + "..."


def _parent_visible_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return parent-log-safe arguments without delegated prompt content."""

    if tool_name != "delegate":
        return _bounded_tool_arguments(arguments)
    return {
        "title": _delegation_title(arguments),
        "agent_id": arguments.get("agent_id"),
        "wait": bool(arguments.get("wait", False)),
        "input_redacted": True,
    }


class StepMetadataContractError(ValueError):
    """Raised when step_complete metadata violates the step contract."""


def _step_metadata_contract_fields(ctx: StepContext | None) -> list[Any]:
    contract = getattr(getattr(ctx, "step_definition", None), "metadata_contract", None)
    return list(getattr(contract, "fields", []) or [])


def _step_metadata_example_value(metadata_field: Any) -> Any:
    if metadata_field.enum:
        return metadata_field.enum[0]
    if metadata_field.type == "string":
        return "value"
    if metadata_field.type == "number":
        return 0
    if metadata_field.type == "boolean":
        return False
    if metadata_field.type == "array":
        description = str(getattr(metadata_field, "description", "") or "").lower()
        if "array of objects" in description:
            return [{"id": "item_id", "status": "value", "evidence": "value"}]
        return []
    if metadata_field.type == "object":
        return {}
    return None


def _step_complete_example_payload(ctx: StepContext | None = None) -> dict[str, Any]:
    """Return a minimal valid ``step_complete`` payload example."""

    payload: dict[str, Any] = {
        "summary": "Summarize what the step accomplished.",
        "outputs": {"key": "value"},
        "claims": ["State the verifiable deliverables from the written output."],
        "outcome": {
            "status": "success",
        },
        "notification": {
            "mode": "direct",
        },
    }
    required_metadata = {
        metadata_field.name: _step_metadata_example_value(metadata_field)
        for metadata_field in _step_metadata_contract_fields(ctx)
        if metadata_field.required
    }
    if required_metadata:
        payload["metadata"] = required_metadata
    return payload


def _build_step_complete_metadata_error(
    arguments: dict[str, Any],
    exc: StepMetadataContractError,
    ctx: StepContext,
) -> str:
    """Build a structured rejection for step metadata contract violations."""

    return json.dumps(
        {
            "status": "rejected",
            "reason": "invalid_step_complete_metadata",
            "message": str(exc),
            "received": arguments,
            "example": _step_complete_example_payload(ctx),
        }
    )


def _task_row_to_model(task_row: Any) -> Any:
    """Convert a task row into a ``TaskModel`` payload."""

    from cognis.models.task import TaskModel

    return TaskModel.model_validate(
        {
            "task_id": task_row.task_id,
            "title": getattr(task_row, "title", "Untitled task"),
            "description": getattr(task_row, "description", "") or "",
            "expected_output": getattr(task_row, "expected_output", None),
            "status": task_row.status,
            "priority": getattr(task_row, "priority", 0),
            "created_by": getattr(task_row, "created_by", "unknown@example.com"),
            "agent_id": task_row.agent_id,
            "agent_profile_id": getattr(task_row, "agent_profile_id", None),
            "created_by_agent_id": getattr(task_row, "created_by_agent_id", None),
            "source_type": getattr(task_row, "source_type", "agent"),
            "source_ref": getattr(task_row, "source_ref", None),
            "delivery": {
                "mode": getattr(task_row, "delivery_mode", "same_conversation"),
                "target": getattr(task_row, "delivery_target", None),
            },
            "completion_delivery": {
                "completion_mode_family": getattr(task_row, "completion_mode_family", "default"),
                "allow_silent_completion": bool(
                    getattr(task_row, "allow_silent_completion", False)
                ),
            },
            "interaction_mode_override": getattr(task_row, "interaction_mode_override", None),
            "workflow_id": getattr(task_row, "workflow_id", None),
            "project_id": getattr(task_row, "project_id", None),
            "attempt_number": getattr(task_row, "attempt_number", 1),
            "workspace_root": getattr(task_row, "workspace_root", None),
            "working_directory": getattr(task_row, "working_directory", None),
            "workflow_state": getattr(task_row, "workflow_state", None),
            "queue_name": getattr(task_row, "queue_name", "default"),
            "scheduled_for": getattr(task_row, "scheduled_for", None),
            "created_at": getattr(task_row, "created_at", None),
            "started_at": getattr(task_row, "started_at", None),
            "completed_at": getattr(task_row, "completed_at", None),
            "result_summary": getattr(task_row, "result_summary", None),
            "result_data": getattr(task_row, "result_data", None),
            "applied_completion_mode": getattr(task_row, "applied_completion_mode", None),
            "applied_completion_reason": getattr(task_row, "applied_completion_reason", None),
        }
    )


def _workflow_registry_for_agent_loop(agent_loop: Any) -> Any:
    """Build a workflow registry from the session factory on demand."""

    from cognis.core.workflow_registry import WorkflowRegistry

    return WorkflowRegistry(agent_loop.session_manager.session_factory)


def _build_step_complete_validation_error(
    arguments: dict[str, Any],
    exc: ValidationError,
    ctx: StepContext | None = None,
) -> str:
    """Build a structured validation error for malformed ``step_complete`` calls."""

    issues: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "payload"
        message = str(error.get("msg", "invalid value"))
        issues.append(f"{location}: {message}")

    return json.dumps(
        {
            "status": "rejected",
            "reason": "invalid_step_complete_arguments",
            "message": (
                "step_complete arguments did not match the required schema. "
                "Call step_complete again with corrected arguments."
            ),
            "issues": issues,
            "received": arguments,
            "example": _step_complete_example_payload(ctx),
        }
    )


def _validate_step_completion_notification(
    ctx: StepContext,
    step_output: StepOutput,
    *,
    deliverable_content: str | None = None,
) -> None:
    """Validate the requested completion notification against the step policy."""

    notification = step_output.notification
    if notification is None:
        return
    if notification.mode not in {"silent", "direct"}:
        raise ValueError(f"Unsupported notification mode: {notification.mode}")
    outcome_status = step_output.outcome.status if step_output.outcome is not None else "success"
    if notification.mode == "silent" and not ctx.completion_delivery.allow_silent_completion:
        raise ValueError("notification.mode='silent' is not allowed for this step")
    if outcome_status != "success":
        if notification.mode == "direct":
            raise ValueError("notification.mode='direct' is only valid for successful completion")
        raise ValueError("notification.mode='silent' is only valid for successful completion")
    effective_content = (
        deliverable_content if deliverable_content is not None else step_output.content
    )
    if notification.mode == "direct" and not effective_content.strip():
        raise ValueError("notification.mode='direct' requires a non-empty deliverable to deliver")


def _find_gate_revise_action(pause: PendingPause) -> str | None:
    """Extract the ``revise(step_name)`` action from a gate's options.

    Gate options are stored as ``[{"label": "Retry step", "action": "revise(research)"}]``
    in the PendingPause context. This returns the first ``revise(...)`` action
    found, which contains the correct original step name (not the synthetic
    gate name like ``research_exhausted``).
    """
    options = pause.options or []
    for opt in options:
        action = opt.get("action", "") if isinstance(opt, dict) else ""
        if isinstance(action, str) and action.startswith("revise("):
            return action
    return None


def _extract_operator_note(arguments: dict[str, Any]) -> str:
    """Extract an optional human operator note from task-pause tool arguments."""

    return str(arguments.get("note") or arguments.get("feedback") or "").strip()


def _append_tool_call_event(
    events: list[SessionEvent],
    tc: ToolCall,
    tool_id: str | None = None,
) -> None:
    """Record a tool_call event to the Intaris event batch."""
    events.append(
        SessionEvent(
            type="tool_call",
            data={
                "name": tc.name,
                "tool_id": tool_id,
                "call_id": tc.call_id,
                "arguments": _parent_visible_tool_arguments(tc.name, tc.arguments),
            },
        )
    )


def _append_tool_result_event(
    events: list[SessionEvent],
    tc: ToolCall,
    output: str,
    is_error: bool,
    duration_ms: int | None = None,
    tool_id: str | None = None,
    protect_from_pruning: bool = False,
    recovery_call_id: str | None = None,
    output_size: int | None = None,
    has_full_output: bool = False,
    agent_visible_truncated: bool = False,
) -> None:
    """Record a tool_result event to the Intaris event batch.

    Intaris stores the exact model-visible tool result, not the raw full
    output. Full output is recoverable through the tool output store.
    """
    events.append(
        SessionEvent(
            type="tool_result",
            data={
                "call_id": tc.call_id,
                "name": tc.name,
                "tool_id": tool_id,
                "is_error": is_error,
                "duration_ms": duration_ms,
                "result": output,
                "output_size": output_size if output_size is not None else len(output),
                "has_full_output": has_full_output,
                "recovery_call_id": recovery_call_id,
                "protect_from_pruning": protect_from_pruning,
                "agent_visible": True,
                "view_kind": "model_tool_result",
                "agent_visible_truncated": agent_visible_truncated,
            },
        )
    )


def _tool_result_message(
    tc: ToolCall,
    content: str,
    *,
    protected: bool = False,
    is_error: bool = False,
) -> dict[str, Any]:
    """Build a model transcript tool-result message."""

    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tc.call_id,
        "content": content,
        "_tool_name": tc.name,
        "_tool_is_error": is_error,
    }
    if protected:
        message["_protected_tool_output"] = True
    return message


def _tool_argument_failure_reason(failure: ToolArgumentParseFailure) -> str:
    reason = str(getattr(failure, "reason", "") or "").strip()
    if reason:
        return reason
    if "tool_call_arguments_too_large" in failure.recovery_attempts:
        return "tool_call_arguments_too_large"
    return "invalid_json"


def _tool_argument_failure_message(failure: ToolArgumentParseFailure) -> str:
    if failure.message:
        return failure.message
    reason = _tool_argument_failure_reason(failure)
    if reason == "tool_call_arguments_too_large":
        return (
            f"The `{failure.name}` tool call arguments exceeded "
            f"{_MAX_TOOL_CALL_ARGUMENT_CHARS} characters. The tool was not executed. "
            "Split the input into smaller tool calls and try again."
        )
    return (
        f"The `{failure.name}` tool call arguments could not be parsed as valid JSON. "
        "The tool was not executed. Retry with one properly formed JSON object."
    )


def _tool_argument_failure_arguments(failure: ToolArgumentParseFailure) -> dict[str, Any]:
    reason = _tool_argument_failure_reason(failure)
    arguments: dict[str, Any] = {
        "status": "rejected",
        "reason": reason,
        "message": _tool_argument_failure_message(failure),
    }
    if reason == "tool_call_arguments_too_large":
        arguments["limit_chars"] = _MAX_TOOL_CALL_ARGUMENT_CHARS
    if failure.argument_length is not None:
        arguments["argument_length"] = failure.argument_length
    return arguments


def _tool_argument_failure_payload(failure: ToolArgumentParseFailure) -> dict[str, Any]:
    payload = _tool_argument_failure_arguments(failure)
    payload["tool"] = failure.name
    payload["call_id"] = failure.call_id
    payload["recovery_attempts"] = list(failure.recovery_attempts)
    payload["raw_preview_chars"] = len(failure.raw)
    return payload


def _strip_internal_message_fields(message: dict[str, Any]) -> dict[str, Any]:
    """Remove controller-only metadata before sending a message to an LLM provider."""

    return {key: value for key, value in message.items() if not str(key).startswith("_")}


def _tool_call_id_tuple(message: dict[str, Any]) -> tuple[str, ...]:
    """Return stable tool call ids for matching projected assistant messages."""

    ids: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return tuple(ids)


def _reattach_responses_output_items(
    projected_messages: list[dict[str, Any]],
    source_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore transient Responses output items after projection strips private metadata.

    These items are intentionally in-memory only; they are needed to continue a
    Responses tool loop with reasoning/function-call items intact, but they must
    not be persisted durably in session events.
    """

    raw_items_by_tool_ids: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for source in source_messages:
        if source.get("role") != "assistant":
            continue
        key = _tool_call_id_tuple(source)
        raw_items = source.get("_responses_output_items")
        if not key or not isinstance(raw_items, list):
            continue
        copied_items = [dict(item) for item in raw_items if isinstance(item, dict)]
        if copied_items:
            raw_items_by_tool_ids.setdefault(key, copied_items)

    if not raw_items_by_tool_ids:
        return projected_messages

    restored: list[dict[str, Any]] = []
    for message in projected_messages:
        copied = dict(message)
        if copied.get("role") == "assistant":
            key = _tool_call_id_tuple(copied)
            raw_items = raw_items_by_tool_ids.get(key)
            if raw_items:
                copied["_responses_output_items"] = [dict(item) for item in raw_items]
        restored.append(copied)
    return restored


@dataclass
class PendingToolCallState:
    """Tracks a tool call whose transcript entry lacks a matching result."""

    tool_call: ToolCall
    tool_id: str | None = None


@dataclass(slots=True)
class _PreparedRegularToolCall:
    tool_call: ToolCall
    tool_id: str


def _track_pending_tool_call(ctx: StepContext, tc: ToolCall, *, tool_id: str | None = None) -> None:
    """Mark a tool call as awaiting a result event."""

    ctx.pending_tool_calls[tc.call_id] = PendingToolCallState(tool_call=tc, tool_id=tool_id)


def _resolve_pending_tool_call(ctx: StepContext, call_id: str) -> None:
    """Mark a tool call as having a recorded result event."""

    ctx.pending_tool_calls.pop(call_id, None)


def _append_interrupted_tool_results(ctx: StepContext, events: list[SessionEvent]) -> int:
    """Close any in-flight tool calls before flushing interrupted state."""

    repaired = 0
    pending_calls = list(ctx.pending_tool_calls.values())
    ctx.pending_tool_calls.clear()
    for pending in pending_calls:
        _append_tool_result_event(
            events,
            pending.tool_call,
            "Tool execution was interrupted before a result was recorded.",
            True,
            tool_id=pending.tool_id,
        )
        repaired += 1
    return repaired


def _inventory_fingerprint(tools: list[ToolDefinition]) -> str:
    """Return a stable fingerprint for a tool inventory.

    The classification memo is keyed by this fingerprint. It changes only
    when tools are added or removed (e.g. on skill load/unload), so a hit
    means the classification result we cached previously is still valid.
    """

    if not tools:
        return "empty"
    payload = "\n".join(sorted(stable_tool_id(tool) for tool in tools))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]  # noqa: S324


def _stable_json_hash(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:16]  # noqa: S324


def _controller_tool_definition(tool_name: str) -> ToolDefinition:
    return ToolDefinition(
        name=tool_name,
        description="Controller-managed tool",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="system",
        read_only=True,
    )


def _tool_id_for_call(tool_name: str, registry: Any | None) -> str:
    if registry is not None:
        registered = registry.get(tool_name)
        if registered is not None:
            return stable_tool_id(registered.definition)
    return stable_tool_id(_controller_tool_definition(tool_name))


def _filter_model_inventory_tools(
    agent: AgentDefinition,
    tools: list[ToolDefinition],
    promoted_tool_ids: set[str] | None = None,
    activated_tool_ids: set[str] | None = None,
) -> list[ToolDefinition]:
    filtered: list[ToolDefinition] = []
    permissions = agent.permissions
    visible_skill_tool_ids = _attached_skill_tool_ids(agent)
    if promoted_tool_ids:
        visible_skill_tool_ids.update(promoted_tool_ids)
    if activated_tool_ids:
        visible_skill_tool_ids.update(activated_tool_ids)
    for tool in tools:
        if tool.name in CONTROLLER_TOOLS or is_orchestration_tool(tool.name):
            continue
        if tool.source.type == "skill" and stable_tool_id(tool) not in visible_skill_tool_ids:
            continue
        if (
            permissions is not None
            and permissions.resolve_permission(
                tool_display_name(tool), tool_id=stable_tool_id(tool)
            )
            is Permission.DENY
        ):
            continue
        filtered.append(tool)
    return filtered


def _attached_skill_tool_ids(agent: AgentDefinition) -> set[str]:
    if not isinstance(agent.skills, dict):
        return set()
    raw_by_skill = agent.skills.get("_attached_skill_tool_ids_by_skill")
    if isinstance(raw_by_skill, dict):
        attached_ids: set[str] = set()
        for raw_tool_ids in raw_by_skill.values():
            if not isinstance(raw_tool_ids, list):
                continue
            attached_ids.update(
                str(tool_id)
                for tool_id in raw_tool_ids
                if isinstance(tool_id, str) and tool_id.strip()
            )
        if attached_ids:
            return attached_ids
    raw_ids = agent.skills.get("_attached_skill_tool_ids")
    if not isinstance(raw_ids, list):
        return set()
    return {str(tool_id) for tool_id in raw_ids if isinstance(tool_id, str) and tool_id.strip()}


def _controller_builtin_enabled(agent: AgentDefinition, tool: ToolDefinition) -> bool:
    if not isinstance(agent.tools, dict):
        return True
    disabled_categories = set(agent.tools.get("disabled_categories") or [])
    disabled_tools = set(agent.tools.get("disabled_tools") or [])
    if tool.category in disabled_categories or any(
        tool_matches_identifier(tool, identifier) for identifier in disabled_tools
    ):
        return False
    builtin_allow = agent.tools.get("builtin_tools")
    if not isinstance(builtin_allow, list):
        return True
    return "*" in builtin_allow or tool.name in builtin_allow


# ---------------------------------------------------------------------------
# Thinking block helpers
# ---------------------------------------------------------------------------

_THINKING_TITLE_MAX_CHARS = 60
_THINKING_TITLE_STOP_CHARS = frozenset(".!?\n")


def _derive_thinking_title(text: str, max_chars: int = _THINKING_TITLE_MAX_CHARS) -> str:
    """Derive a short display title from the start of a thinking block.

    Looks for the first sentence-ending punctuation or newline within
    *max_chars*, then truncates with an ellipsis if the full text is longer.
    """
    if not text:
        return "Thinking"
    # Walk forward to find first natural stop within the char budget
    stop_idx: int | None = None
    for i, char in enumerate(text[:max_chars]):
        if char in _THINKING_TITLE_STOP_CHARS:
            stop_idx = i + 1
            break
    if stop_idx is not None:
        title = text[:stop_idx].strip()
    else:
        title = text[:max_chars].rstrip()
        if len(text) > max_chars:
            title += "…"
    return title or "Thinking"


class ThinkingBlockState:
    """Mutable state for one in-progress or completed reasoning block."""

    __slots__ = (
        "block_id",
        "title",
        "content_parts",
        "source",
        "provider_block_index",
        "complete",
        "started_at",
        "completed_at",
        "duration_ms",
    )

    def __init__(
        self,
        *,
        block_id: str,
        title: str | None,
        source: str,
        provider_block_index: int | None,
    ) -> None:
        self.block_id = block_id
        self.title = title
        self.content_parts: list[str] = []
        self.source = source
        self.provider_block_index = provider_block_index
        self.complete = False
        self.started_at = datetime.now(UTC)
        self.completed_at: datetime | None = None
        self.duration_ms: int | None = None

    def get_content(self) -> str:
        """Return accumulated content as a single string."""
        return "".join(self.content_parts)

    def get_title(self) -> str:
        """Return the best available title for this block."""
        if self.title:
            return self.title
        return _derive_thinking_title(self.get_content())


class ThinkingEvent:
    """A reasoning delta event produced by ``StreamAccumulator.pop_thinking_events``."""

    __slots__ = (
        "block_id",
        "delta",
        "title",
        "source",
        "complete",
        "content",
        "started_at",
        "completed_at",
        "duration_ms",
        "provider_block_index",
    )

    def __init__(
        self,
        *,
        block_id: str,
        delta: str,
        title: str | None,
        source: str,
        complete: bool,
        content: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        provider_block_index: int | None = None,
    ) -> None:
        self.block_id = block_id
        self.delta = delta
        self.title = title
        self.source = source
        self.complete = complete
        self.content = content
        self.started_at = started_at
        self.completed_at = completed_at
        self.duration_ms = duration_ms
        self.provider_block_index = provider_block_index


class ToolProgressEvent:
    """A tool-preparation progress event produced during LLM streaming."""

    __slots__ = (
        "call_id",
        "tool_name",
        "phase",
        "input_chars",
        "input_lines",
        "complete",
    )

    def __init__(
        self,
        *,
        call_id: str,
        tool_name: str,
        phase: str,
        input_chars: int,
        input_lines: int,
        complete: bool,
    ) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.phase = phase
        self.input_chars = input_chars
        self.input_lines = input_lines
        self.complete = complete


# ---------------------------------------------------------------------------
# Stream accumulator
# ---------------------------------------------------------------------------


class StreamAccumulator:
    """Accumulates streaming chunks into complete messages and tool calls.

    Handles LiteLLM's streaming format where tool calls arrive
    incrementally across chunks.

    Also accumulates reasoning/thinking blocks emitted by reasoning-capable
    models.  Call ``pop_thinking_events()`` after each ``feed()`` to drain
    pending ``ThinkingEvent`` objects, and ``finalize_thinking()`` at the end
    of the stream to close any still-open block.
    """

    def __init__(self, *, block_id_prefix: str | None = None) -> None:
        self.content_parts: list[str] = []
        self.internal_content_parts: list[str] = []
        self.responses_output_items: list[dict[str, Any]] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, int] | None = None
        self.finish_reason: str = "stop"
        # Thinking-block state
        self._current_thinking_block: ThinkingBlockState | None = None
        self._completed_thinking_blocks: list[ThinkingBlockState] = []
        self._thinking_block_counter: int = 0
        self._last_reasoning_source: str | None = None
        self._pending_thinking_events: list[ThinkingEvent] = []
        self._pending_tool_progress_events: list[ToolProgressEvent] = []
        self._block_id_prefix = (
            re.sub(r"[^a-zA-Z0-9_-]+", "_", block_id_prefix) if block_id_prefix else None
        )

    def clone_tool_call_state(self) -> dict[int, dict[str, Any]]:
        """Return a shallow copy of the accumulated tool-call state."""
        return {idx: dict(entry) for idx, entry in self.tool_calls.items()}

    def restore_tool_call_state(self, state: dict[int, dict[str, Any]] | None) -> None:
        """Restore accumulated tool-call state from a previous attempt."""
        self.tool_calls = {idx: dict(entry) for idx, entry in (state or {}).items()}

    # ------------------------------------------------------------------
    # Thinking-block internals
    # ------------------------------------------------------------------

    def _open_thinking_block(
        self,
        *,
        source: str,
        provider_block_index: int | None,
        provider_title: str | None,
    ) -> ThinkingBlockState:
        """Open a new thinking block, closing any open one first."""
        self._close_thinking_block(reason="new_block_started")
        self._thinking_block_counter += 1
        block_id = (
            f"thk_{self._block_id_prefix}_{self._thinking_block_counter}"
            if self._block_id_prefix
            else f"thk_{self._thinking_block_counter}"
        )
        block = ThinkingBlockState(
            block_id=block_id,
            title=provider_title,
            source=source,
            provider_block_index=provider_block_index,
        )
        self._current_thinking_block = block
        return block

    def _close_thinking_block(self, reason: str) -> None:
        """Finalize the current thinking block (if any) and queue a complete event."""
        block = self._current_thinking_block
        if block is None:
            return
        if not block.content_parts:
            # Empty block — discard silently
            self._current_thinking_block = None
            return
        block.complete = True
        block.completed_at = datetime.now(UTC)
        block.duration_ms = max(
            0, int((block.completed_at - block.started_at).total_seconds() * 1000)
        )
        self._completed_thinking_blocks.append(block)
        self._current_thinking_block = None
        self._pending_thinking_events.append(
            ThinkingEvent(
                block_id=block.block_id,
                delta="",
                title=block.get_title(),
                source=block.source,
                complete=True,
                content=block.get_content(),
                started_at=block.started_at.isoformat(),
                completed_at=block.completed_at.isoformat(),
                duration_ms=block.duration_ms,
                provider_block_index=block.provider_block_index,
            )
        )

    def pop_thinking_events(self) -> list[ThinkingEvent]:
        """Return and clear all pending thinking events accumulated since last call."""
        events = list(self._pending_thinking_events)
        self._pending_thinking_events.clear()
        return events

    def pop_tool_progress_events(self) -> list[ToolProgressEvent]:
        """Return and clear all pending tool progress events accumulated since last call."""
        events = list(self._pending_tool_progress_events)
        self._pending_tool_progress_events.clear()
        return events

    def finalize_thinking(self) -> list[ThinkingBlockState]:
        """Close any open thinking block and return all completed blocks.

        Must be called once after the stream ends.
        """
        self._close_thinking_block(reason="stream_end")
        return list(self._completed_thinking_blocks)

    def get_completed_thinking_blocks(self) -> list[ThinkingBlockState]:
        """Return the list of completed thinking blocks (read-only snapshot)."""
        return list(self._completed_thinking_blocks)

    # ------------------------------------------------------------------
    # Main feed
    # ------------------------------------------------------------------

    def feed(self, chunk: dict[str, Any]) -> str | None:
        """Feed a stream chunk. Returns text delta if present."""
        raw_responses_output_item = chunk.get("responses_output_item")
        if isinstance(raw_responses_output_item, dict):
            self.responses_output_items.append(dict(raw_responses_output_item))

        choices = chunk.get("choices")
        if not choices:
            # Check for usage in final chunk
            usage = chunk.get("usage")
            if usage:
                self.usage = _normalize_token_usage(usage)
            return None

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self.finish_reason = finish_reason

        # ---------------------------------------------------------------
        # Reasoning part boundary markers from responses_bridge
        # ---------------------------------------------------------------
        reasoning_boundary = (
            delta.get("reasoning_part_boundary") if isinstance(delta, dict) else None
        )
        if isinstance(reasoning_boundary, dict):
            is_complete = bool(reasoning_boundary.get("complete", False))
            part_index = reasoning_boundary.get("part_index")
            provider_title = reasoning_boundary.get("title") or None
            if is_complete:
                self._close_thinking_block(reason="part_done")
            else:
                # New provider part is starting
                self._open_thinking_block(
                    source="summary",
                    provider_block_index=part_index,
                    provider_title=provider_title,
                )
            # No text or tool content in boundary-only chunks
            return None

        tool_progress = delta.get("tool_progress") if isinstance(delta, dict) else None
        if isinstance(tool_progress, dict):
            call_id = str(tool_progress.get("id") or "").strip()
            tool_name = str(tool_progress.get("name") or "unknown_tool")
            if call_id:
                self._close_thinking_block(reason="tool_progress")
                self._pending_tool_progress_events.append(
                    ToolProgressEvent(
                        call_id=call_id,
                        tool_name=tool_name,
                        phase=str(tool_progress.get("phase") or "preparing_input"),
                        input_chars=max(0, int(tool_progress.get("input_chars") or 0)),
                        input_lines=max(0, int(tool_progress.get("input_lines") or 0)),
                        complete=bool(tool_progress.get("complete", False)),
                    )
                )
            return None

        # ---------------------------------------------------------------
        # Regular text content — heuristic: close thinking block
        # ---------------------------------------------------------------
        text_delta: str | None = delta.get("content")
        if text_delta:
            # Assistant text starting means the model finished thinking
            self._close_thinking_block(reason="content_started")
            self.content_parts.append(text_delta)

        # ---------------------------------------------------------------
        # Tool call deltas — heuristic: close thinking block
        # ---------------------------------------------------------------
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            # Tool call starting means the model finished thinking
            self._close_thinking_block(reason="tool_call_started")
            for tc_delta in tc_deltas:
                idx = tc_delta.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {
                        "id": tc_delta.get("id", ""),
                        "name": "",
                        "arguments": "",
                    }
                entry = self.tool_calls[idx]
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                func = tc_delta.get("function", {})
                if func.get("name"):
                    entry["name"] = func["name"]
                if func.get("arguments"):
                    incoming_arguments = func["arguments"]
                    if entry.get("argument_oversized"):
                        entry["argument_length"] = int(
                            entry.get("argument_length") or len(str(entry.get("arguments") or ""))
                        ) + len(incoming_arguments)
                        continue
                    existing_arguments = entry["arguments"]
                    merge_result = merge_incremental_json_fragment(
                        existing_arguments,
                        incoming_arguments,
                    )
                    merged_arguments = merge_result.merged
                    if len(merged_arguments) > _MAX_TOOL_CALL_ARGUMENT_CHARS:
                        logger.warning(
                            "Tool call arguments exceeded safety limit; rejecting call",
                            extra={
                                "extra_data": {
                                    "tool_name": entry["name"],
                                    "args_length": len(merged_arguments),
                                    "limit": _MAX_TOOL_CALL_ARGUMENT_CHARS,
                                }
                            },
                        )
                        entry["arguments"] = merged_arguments[
                            :_MAX_FAILED_TOOL_ARGUMENT_PREVIEW_CHARS
                        ]
                        entry["argument_oversized"] = True
                        entry["argument_length"] = len(merged_arguments)
                        continue
                    entry["arguments"] = merged_arguments
                    entry["argument_length"] = len(merged_arguments)

        # ---------------------------------------------------------------
        # Reasoning / thinking deltas
        # ---------------------------------------------------------------
        reasoning_summary = delta.get("reasoning")
        reasoning_content = delta.get("reasoning_content")

        if reasoning_summary is not None or reasoning_content is not None:
            # Determine source — prefer summary (display-ready), fall back to raw CoT
            if isinstance(reasoning_summary, str) and reasoning_summary:
                source = "summary"
                reasoning_text = reasoning_summary
            elif isinstance(reasoning_content, str) and reasoning_content:
                source = "content"
                reasoning_text = reasoning_content
            else:
                reasoning_text = ""
                source = "summary"

            if reasoning_text:
                # Source switch → heuristic block boundary
                if self._last_reasoning_source and self._last_reasoning_source != source:
                    self._open_thinking_block(
                        source=source,
                        provider_block_index=None,
                        provider_title=None,
                    )
                self._last_reasoning_source = source

                # Open a block if none is in-flight
                if self._current_thinking_block is None:
                    self._open_thinking_block(
                        source=source,
                        provider_block_index=None,
                        provider_title=None,
                    )
                block = self._current_thinking_block
                assert block is not None  # always set above
                block.content_parts.append(reasoning_text)
                self._pending_thinking_events.append(
                    ThinkingEvent(
                        block_id=block.block_id,
                        delta=reasoning_text,
                        title=block.get_title(),
                        source=source,
                        complete=False,
                        started_at=block.started_at.isoformat(),
                        provider_block_index=block.provider_block_index,
                    )
                )

        return text_delta

    def get_content(self) -> str:
        """Return accumulated text content."""
        return "".join(self.content_parts)

    def get_internal_content(self) -> str:
        """Return content demoted from public assistant output."""

        return "".join(self.internal_content_parts)

    def get_responses_output_items(self) -> list[dict[str, Any]]:
        """Return raw Responses output items needed for same-turn continuation."""

        return [dict(item) for item in self.responses_output_items]

    def get_tool_calls(self) -> list[ToolCall | ToolArgumentParseFailure]:
        """Return finalized tool calls or structured argument parse failures."""
        result = []
        for _idx, tc in sorted(self.tool_calls.items()):
            call_id = tc["id"] or f"call_{uuid.uuid4().hex[:12]}"
            if tc.get("argument_oversized"):
                result.append(
                    ToolArgumentParseFailure(
                        call_id=call_id,
                        name=tc["name"],
                        raw=tc["arguments"],
                        recovery_attempts=("tool_call_arguments_too_large",),
                        reason="tool_call_arguments_too_large",
                        message=(
                            f"Tool call arguments exceeded {_MAX_TOOL_CALL_ARGUMENT_CHARS} "
                            "characters. Split the input into smaller tool calls."
                        ),
                        argument_length=int(
                            tc.get("argument_length") or len(str(tc.get("arguments") or ""))
                        ),
                    )
                )
                continue
            if not tc["arguments"] and tc["name"] in _NON_EMPTY_TOOL_ARGUMENT_NAMES:
                logger.warning(
                    "Empty tool call arguments; asking model to retry with valid JSON",
                    extra={
                        "extra_data": {
                            "tool_name": tc["name"],
                            "args_length": 0,
                        }
                    },
                )
                result.append(
                    ToolArgumentParseFailure(
                        call_id=call_id,
                        name=tc["name"],
                        raw=tc["arguments"],
                        recovery_attempts=("non_empty_arguments_required",),
                        argument_length=0,
                    )
                )
                continue
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                # Attempt to recover concatenated JSON objects (e.g. from
                # bridge bugs that merge multiple tool calls into one index).
                split_args = _try_split_concatenated_json(tc["arguments"])
                if split_args is not None:
                    for parsed in split_args:
                        result.append(
                            ToolCall(
                                call_id=f"call_{uuid.uuid4().hex[:12]}",
                                name=tc["name"],
                                arguments=parsed,
                            )
                        )
                    continue
                recovered_args = recover_trailing_json_object(tc["arguments"])
                if recovered_args is not None:
                    result.append(
                        ToolCall(
                            call_id=tc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                            name=tc["name"],
                            arguments=recovered_args,
                        )
                    )
                    continue
                logger.warning(
                    "Malformed tool call arguments; asking model to retry with valid JSON",
                    extra={
                        "extra_data": {
                            "tool_name": tc["name"],
                            "args_length": len(tc["arguments"]),
                        }
                    },
                )
                result.append(
                    ToolArgumentParseFailure(
                        call_id=call_id,
                        name=tc["name"],
                        raw=tc["arguments"],
                        recovery_attempts=(
                            "split_concatenated_json",
                            "recover_trailing_json_object",
                        ),
                        argument_length=len(tc["arguments"]),
                    )
                )
                continue
            result.append(
                ToolCall(
                    call_id=call_id,
                    name=tc["name"],
                    arguments=args,
                )
            )
        return result

    def has_tool_calls(self) -> bool:
        """Return True if any tool calls were accumulated."""
        return bool(self.tool_calls)

    def reset(self) -> None:
        """Reset for the next LLM turn."""
        self.content_parts.clear()
        self.internal_content_parts.clear()
        self.tool_calls.clear()
        self.usage = None
        self._current_thinking_block = None
        self._completed_thinking_blocks.clear()
        self._thinking_block_counter = 0
        self._last_reasoning_source = None
        self._pending_thinking_events.clear()
        self._pending_tool_progress_events.clear()


def _normalize_token_usage(usage: Any) -> dict[str, int]:
    """Flatten provider usage payloads into integer token counters."""

    if not isinstance(usage, dict):
        return {}

    def _read_int(*candidates: Any) -> int | None:
        for candidate in candidates:
            if isinstance(candidate, int | float):
                return int(candidate)
        return None

    prompt_token_details = usage.get("prompt_tokens_details")
    input_token_details = usage.get("input_tokens_details")
    completion_token_details = usage.get("completion_tokens_details")
    output_token_details = usage.get("output_tokens_details")

    normalized: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _read_int(usage.get(key))
        if value is not None:
            normalized[key] = value

    cached_tokens = _read_int(
        usage.get("cached_tokens"),
        prompt_token_details.get("cached_tokens")
        if isinstance(prompt_token_details, dict)
        else None,
        input_token_details.get("cached_tokens") if isinstance(input_token_details, dict) else None,
    )
    if cached_tokens is not None:
        normalized["cached_tokens"] = cached_tokens

    cache_read_tokens = _read_int(usage.get("cache_read_input_tokens"))
    if cache_read_tokens is not None:
        normalized["cache_read_input_tokens"] = cache_read_tokens

    cache_creation_tokens = _read_int(usage.get("cache_creation_input_tokens"))
    if cache_creation_tokens is not None:
        normalized["cache_creation_input_tokens"] = cache_creation_tokens

    reasoning_tokens = _read_int(
        usage.get("reasoning_tokens"),
        completion_token_details.get("reasoning_tokens")
        if isinstance(completion_token_details, dict)
        else None,
        output_token_details.get("reasoning_tokens")
        if isinstance(output_token_details, dict)
        else None,
    )
    if reasoning_tokens is not None:
        normalized["reasoning_tokens"] = reasoning_tokens

    return normalized


def _try_split_concatenated_json(raw: str) -> list[dict[str, Any]] | None:
    """Try to split a string containing multiple concatenated JSON objects.

    Returns a list of parsed dicts if successful, or ``None`` if the string
    does not look like concatenated JSON objects.
    """
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    pos = 0
    while pos < len(raw):
        # Skip whitespace between objects
        while pos < len(raw) and raw[pos] in " \t\n\r":
            pos += 1
        if pos >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        results.append(obj)
        pos = end
    return results if len(results) > 1 else None


# ---------------------------------------------------------------------------
# Execution policy — replaces the is_direct boolean flag
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPolicy:
    """Controls behavioral differences between execution paths.

    Replaces the former ``is_direct`` boolean with explicit, named flags.
    Three presets cover the standard execution paths:

    - ``CHAT_POLICY``:  direct chat turns (WebSocket)
    - ``WORKFLOW_POLICY``:  background workflow steps
    - ``DELEGATION_POLICY``:  synchronous/async sub-sessions
    """

    require_step_complete: bool = False
    """Whether the LLM must call ``step_complete`` to signal completion."""

    step_complete_available: bool = False
    """Whether the ``step_complete`` tool is exposed to the LLM."""

    enable_auto_compaction: bool = False
    """Whether automatic context compaction runs after the turn."""

    event_flush_strategy: str = "batch"
    """``"batch"`` = single write at turn end; ``"incremental"`` = after each tool batch."""

    skip_memory: bool = False
    """Skip Mnemory recall/remember and memory instructions in context assembly."""

    skip_orchestration: bool = False
    """Skip orchestration tools (delegate, spawn_worker, tasks)."""


CHAT_POLICY = ExecutionPolicy(
    require_step_complete=False,
    step_complete_available=False,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
)

WORKFLOW_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
)

DELEGATION_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
)

# Policy for secondary-agent delegations (both shipped system agents and
# user-managed secondary agents): step_complete is available but NOT required.
# The sub-session may return via a normal assistant message, matching the
# OpenCode task sub-agent contract. write_deliverable is still available when a
# deliverable_step_run_id is set (opt-in from the parent step).
# Secondary agents are lightweight specialists — no identity, no memory,
# no project instructions, lower tool-call budget.
SECONDARY_AGENT_DELEGATION_POLICY = ExecutionPolicy(
    require_step_complete=False,
    step_complete_available=True,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
    skip_memory=True,  # No Mnemory recall/remember for secondary agents
)

SECONDARY_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
    skip_memory=True,
    skip_orchestration=True,
)


# ---------------------------------------------------------------------------
# Session lock
# ---------------------------------------------------------------------------


class SessionLock:
    """Per-session async locks to prevent concurrent turns."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_touched: dict[str, float] = {}
        self._meta_lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> asyncio.Lock:
        """Get or create a lock for a session and acquire it."""
        async with self._meta_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            lock = self._locks[session_id]
            self._last_touched[session_id] = monotonic()
        await lock.acquire()
        return lock

    def release(self, session_id: str) -> None:
        """Release a session lock."""
        lock = self._locks.get(session_id)
        if lock and lock.locked():
            lock.release()
        self._last_touched[session_id] = monotonic()

    def is_locked(self, session_id: str) -> bool:
        """Return whether a session lock is currently held."""

        lock = self._locks.get(session_id)
        return bool(lock and lock.locked())

    def evict(self, session_id: str, *, reason: str = "close_session") -> None:
        """Remove a session's lock entry."""
        removed = self._locks.pop(session_id, None)
        self._last_touched.pop(session_id, None)
        if removed is not None:
            SESSION_LOCKS_EVICTED_TOTAL.labels(reason=reason).inc()

    def stale_unlocked_session_ids(self, *, max_idle_seconds: float) -> list[str]:
        """Return unlocked session ids idle longer than ``max_idle_seconds``."""
        now = monotonic()
        stale: list[str] = []
        for session_id, lock in self._locks.items():
            if lock.locked():
                continue
            last_touched = self._last_touched.get(session_id, now)
            if now - last_touched >= max_idle_seconds:
                stale.append(session_id)
        return stale


# ---------------------------------------------------------------------------
# Escalation / input waiter
# ---------------------------------------------------------------------------


@dataclass
class PauseResolution:
    """Resolution for a paused step (escalation, gate, or input request)."""

    decision: str  # "approve" | "deny" | "continue" | "cancel"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingPause:
    """Metadata describing a currently pending pause."""

    pause_id: str
    pause_type: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    question: str | None = None
    options: list[dict[str, Any]] | None = None
    questions: list[dict[str, Any]] | None = None
    context: dict[str, Any] | None = None
    resolved: bool = False


class PauseWaiter:
    """Synchronization mechanism for step pauses.

    The agent loop calls wait() when a step needs external input (escalation
    or step_request_questions). The WebSocket handler or API route (Stage 7)
    calls resolve() with the user's response.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._resolutions: dict[str, PauseResolution] = {}
        self._pending: dict[str, PendingPause] = {}

    def register(self, pause: PendingPause) -> None:
        """Register metadata for a pause before waiting on it."""
        self._pending[pause.pause_id] = pause

    async def wait(self, pause_id: str, *, timeout: float = 300.0) -> PauseResolution:
        """Wait for a pause to be resolved. Raises TimeoutError on timeout."""
        self._pending.setdefault(
            pause_id,
            PendingPause(pause_id=pause_id, pause_type="unknown"),
        )
        event = asyncio.Event()
        self._events[pause_id] = event
        if pause_id in self._resolutions:
            event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._resolutions.pop(pause_id, PauseResolution(decision="deny"))
        finally:
            self._events.pop(pause_id, None)
            self._pending.pop(pause_id, None)

    def resolve(self, pause_id: str, resolution: PauseResolution) -> bool:
        """Resolve a waiting pause exactly once.

        Returns True when the pause was resolved by this call, False when the
        pause does not exist or has already been resolved.
        """
        pending = self._pending.get(pause_id)
        if pending is None or pending.resolved:
            return False
        pending.resolved = True
        self._resolutions[pause_id] = resolution
        event = self._events.get(pause_id)
        if event:
            event.set()
            return True
        return True

    def get(self, pause_id: str) -> PendingPause | None:
        """Return pending pause metadata."""
        return self._pending.get(pause_id)

    def find_pending(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        step_name: str | None = None,
        pause_type: str | None = None,
        include_resolved: bool = False,
    ) -> PendingPause | None:
        """Find the first unresolved pause matching the provided filters."""
        for pause in self._pending.values():
            if pause.resolved and not include_resolved:
                continue
            if task_id is not None and pause.task_id != task_id:
                continue
            if session_id is not None and pause.session_id != session_id:
                continue
            if conversation_id is not None and pause.conversation_id != conversation_id:
                continue
            if step_name is not None and pause.step_name != step_name:
                continue
            if pause_type is not None and pause.pause_type != pause_type:
                continue
            return pause
        return None

    def list_pending(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        pause_type: str | None = None,
    ) -> list[PendingPause]:
        """List unresolved pauses, optionally filtered by task, session, or conversation."""
        result: list[PendingPause] = []
        for pause in self._pending.values():
            if pause.resolved:
                continue
            if task_id is not None and pause.task_id != task_id:
                continue
            if session_id is not None and pause.session_id != session_id:
                continue
            if conversation_id is not None and pause.conversation_id != conversation_id:
                continue
            if pause_type is not None and pause.pause_type != pause_type:
                continue
            result.append(pause)
        return result

    def clear(self, pause_id: str) -> None:
        """Remove all local state for a pause.

        Used by recovered pause flows where a response is stored for later
        replay rather than being consumed by an already waiting coroutine.
        """
        self._events.pop(pause_id, None)
        self._resolutions.pop(pause_id, None)
        self._pending.pop(pause_id, None)

    def pending_count(self) -> int:
        """Return number of active waits."""
        return len([pause for pause in self._pending.values() if not pause.resolved])


class StepInterrupted(Exception):
    """Raised when a step exits early because of pause/cancel control flow."""


# ---------------------------------------------------------------------------
# Step context
# ---------------------------------------------------------------------------


@dataclass
class StepContext:
    """Runtime context for a step execution."""

    step_definition: StepDefinition
    session: SessionModel
    conversation: ConversationModel
    agent: AgentDefinition
    executor_agent: AgentDefinition | None = None
    step_inputs: dict[str, StepOutput] = field(default_factory=dict)
    todos: list[dict[str, Any]] = field(default_factory=list)
    task_id: str | None = None
    task_title: str = ""
    task_description: str = ""
    task_expected_output: str | None = None
    project_context: str | None = None
    completion_delivery: CompletionDeliveryPolicy = field(default_factory=CompletionDeliveryPolicy)
    workspace_root: str | None = None
    working_directory: str | None = None
    workspace_root_explicit: bool = False
    working_directory_explicit: bool = False
    step_run_id: str | None = None
    deliverable_step_run_id: str | None = None
    policy: ExecutionPolicy = field(default_factory=lambda: CHAT_POLICY)
    is_retry: bool = False  # True for re-attempt within the same step
    user_message: str = ""
    user_attachments: list[AttachmentRef] = field(default_factory=list)
    attachment_notice: str | None = None
    attachment_context: str | None = None
    prior_context: list[dict[str, Any]] | None = None  # Prior step output messages
    interaction_mode: str = "explicit_gates"
    session_policy: dict[str, Any] = field(default_factory=dict)
    tool_registry: Any = None  # ToolRegistry instance for this step
    classified_tool_definitions: dict[str, ToolDefinition] = field(default_factory=dict)
    executor_connection: Any = None  # ExecutorConnection for this step
    executor_environment: ExecutorEnvironmentSnapshot | None = None
    # Stage 36 multi-executor agents:
    # - executor_pool carries the agent's full assigned pool (primary + additional)
    # - active_executor_id is the conversation's current active routing slot
    executor_pool: Any = None  # Optional[ExecutorPool]
    active_executor_id: str | None = None
    runtime_info: dict[str, Any] = field(default_factory=dict)
    workflow_state: WorkflowState | None = None
    workflow_steps: list[StepDefinition] | None = None  # All steps for source resolution
    step_index: int = 0  # Index of current step in workflow
    cancel_event: asyncio.Event | None = None
    system_initiated: bool = False
    follow_up: FollowUpMetadata | None = None
    bootstrap_wait_for_intention: bool = False
    orchestration_mode: OrchestrationMode = OrchestrationMode.FULL
    execution_evidence: dict[str, Any] = field(
        default_factory=lambda: {
            "tools": [],
            "files_read": [],
            "files_written": [],
            "commands": [],
        }
    )
    # Harness guards (step-scoped; reset per step execution).
    loop_guard_state: LoopGuardState = field(default_factory=LoopGuardState)
    pending_events: list[SessionEvent] | None = None
    pending_tool_calls: dict[str, PendingToolCallState] = field(default_factory=dict)
    timeout_continuation_message: str | None = None
    timeout_continuation_count: int = 0
    max_timeout_continuations: int = 1
    current_model: str | None = None
    current_provider_id: str | None = None
    current_model_info: Any = None
    # Per-turn projection state (replaces last_projection_policy).
    projection_state: ProjectionTurnState | None = None
    # Latest exact model-facing projection pressure.  Used after the turn to
    # avoid rotating solely because raw cross-turn assembly recommended
    # compaction when projection already made the prompt safe.
    last_projection_snapshot: ContextPressureSnapshot | None = None
    last_projection_exceeded_selected_budget: bool | None = None
    remember_user_event_seq: int | None = None
    current_deliverable_id: str | None = None
    current_deliverable_version: int | None = None
    current_deliverable_content: str | None = None
    current_deliverable_format: str | None = None
    current_deliverable_title: str | None = None
    current_deliverable_outputs: dict[str, Any] = field(default_factory=dict)
    current_deliverable_status: str | None = None
    remember_assistant_event_seq: int | None = None
    turn_id: str | None = None
    chat_mode: ResolvedChatMode | None = None
    on_tool_progress: ToolProgressCallback | None = None
    on_tool_output_chunk: ToolOutputChunkCallback | None = None
    consume_boundary_batch: Callable[[str], Coroutine[Any, Any, list[dict[str, Any]]]] | None = None
    # True after a successful write_deliverable until step_complete is called
    # or a follow-up reminder has been emitted twice.  Used to nudge models
    # that wrote the artifact and forgot to finalize the step.
    post_deliverable_pending: bool = False
    post_deliverable_reminders_sent: int = 0
    # Compaction recursion depth — incremented each time _rotate_after_compaction
    # recurses back into _execute_step.  Bounded by session.compaction_max_recursion
    # (default 2) to prevent infinite loops when compaction cannot reduce context.
    compaction_recursion_depth: int = 0

    @property
    def last_projection_policy(self) -> ProjectionPolicy | None:
        """Backward-compat shim — use projection_state.policy instead."""
        if self.projection_state is not None:
            return self.projection_state.policy
        return None


def _timeout_continuation_prompt(ctx: StepContext) -> str:
    base = (
        "The previous turn was interrupted by the safety timeout and is being "
        "continued automatically. Before continuing, verify that your current "
        "work is still aligned with the original task/request. Review and "
        "update todos if appropriate. Provide a concise summary of the "
        "interrupted/current state. Continue only if additional work is still "
        "needed and it is safe to proceed; otherwise finalize clearly."
    )
    if ctx.policy.require_step_complete:
        if ctx.deliverable_step_run_id or ctx.step_definition.require_deliverable:
            return (
                base
                + " If a deliverable is required, write/update it before calling step_complete."
            )
        return base + " When done, return the result normally and call step_complete."
    return base + " Finish with a normal assistant answer."


@dataclass(slots=True)
class ContextPressureSnapshot:
    """Token-budget snapshot used for tool-loop pressure checks."""

    prompt_tokens: int
    max_context_tokens: int
    max_input_tokens: int
    reserve_output_tokens: int
    effective_reserve_output_tokens: int
    available_prompt_tokens: int
    threshold_prompt_tokens: int
    exceeded: bool
    reason: str
    reserve_clamped: bool = False


@dataclass(slots=True)
class ProjectedMessages:
    """Model-facing transcript plus pressure telemetry for the projection pass."""

    messages: list[dict[str, Any]]
    snapshot: ContextPressureSnapshot | None
    mode: str = "normal"
    policy: ProjectionPolicy | None = None


@dataclass(slots=True)
class CompactionRunContext:
    """Telemetry that explains why a compaction run was triggered."""

    trigger: str
    reason: str
    prompt_tokens: int = 0
    max_context_tokens: int = 0
    max_input_tokens: int = 0
    available_prompt_tokens: int = 0
    compaction_threshold_prompt_tokens: int = 0
    loop_pressure_threshold_prompt_tokens: int = 0
    compaction_threshold: float = 0.85
    previous_usage_percentage: float | None = None
    effective_usage_percentage: float | None = None
    hard_pressure_exceeded: bool = False
    used_timeout_fallback: bool = False
    phase: str = "turn"
    status: str = "started"
    provider_id: str | None = None
    model_id: str | None = None
    fallback_reason: str | None = None

    @classmethod
    def from_context_result(
        cls, context_result: Any, *, trigger: str, reason: str
    ) -> CompactionRunContext:
        return cls(trigger=trigger, reason=reason, **_context_pressure_metadata(context_result))

    @classmethod
    def from_snapshot(
        cls, snapshot: ContextPressureSnapshot, *, trigger: str, reason: str
    ) -> CompactionRunContext:
        max_context_tokens = snapshot.max_context_tokens
        max_input_tokens = snapshot.max_input_tokens
        available_prompt_tokens = snapshot.available_prompt_tokens
        return cls(
            trigger=trigger,
            reason=reason,
            prompt_tokens=snapshot.prompt_tokens,
            max_context_tokens=max_context_tokens,
            max_input_tokens=max_input_tokens,
            available_prompt_tokens=available_prompt_tokens,
            compaction_threshold_prompt_tokens=int(available_prompt_tokens * 0.85),
            loop_pressure_threshold_prompt_tokens=snapshot.threshold_prompt_tokens,
            previous_usage_percentage=(
                round(snapshot.prompt_tokens / max_context_tokens * 100, 1)
                if max_context_tokens > 0
                else None
            ),
            effective_usage_percentage=(
                round(snapshot.prompt_tokens / available_prompt_tokens * 100, 1)
                if available_prompt_tokens > 0
                else None
            ),
            hard_pressure_exceeded=snapshot.exceeded,
        )

    def event_data(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "reason": self.reason,
            "prompt_tokens": self.prompt_tokens,
            "max_context_tokens": self.max_context_tokens,
            "max_input_tokens": self.max_input_tokens,
            "available_prompt_tokens": self.available_prompt_tokens,
            "compaction_threshold_prompt_tokens": self.compaction_threshold_prompt_tokens,
            "loop_pressure_threshold_prompt_tokens": self.loop_pressure_threshold_prompt_tokens,
            "compaction_threshold": self.compaction_threshold,
            "previous_usage_percentage": self.previous_usage_percentage,
            "effective_usage_percentage": self.effective_usage_percentage,
            "hard_pressure_exceeded": self.hard_pressure_exceeded,
            "used_timeout_fallback": self.used_timeout_fallback,
            "phase": self.phase,
            "status": self.status,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "fallback_reason": self.fallback_reason,
        }


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


class AgentLoop:
    """Runs a single step as a full agentic loop."""

    def __init__(
        self,
        *,
        providers: Any,
        session_manager: Any,
        session_cache: Any,
        context_assembler: Any,
        compaction_strategy: Any,
        tool_router: Any,
        remember_queue: Any,
        event_bus: EventBus,
        session_lock: SessionLock,
        pause_waiter: PauseWaiter,
        session_factory: Any = None,
        tool_classification_queue: Any = None,
        step_profile_registry: Any = None,
        default_step_timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS,
        default_llm_stream_idle_timeout_seconds: int = DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS,
        default_llm_stream_max_retries: int = DEFAULT_LLM_STREAM_MAX_RETRIES,
        tool_output_store: Any = None,
        step_context_assembler: Any = None,  # DEPRECATED — kept for backward compat
        step_runtime_factory: Any = None,
    ) -> None:
        self.providers = providers
        self.session_manager = session_manager
        self.session_cache = session_cache
        self.context_assembler = context_assembler
        self.compaction_strategy = compaction_strategy
        self.tool_router = tool_router
        self.remember_queue = remember_queue
        self.event_bus = event_bus
        self.session_lock = session_lock
        self.pause_waiter = pause_waiter
        self._session_factory = session_factory
        self._tool_classification_queue = tool_classification_queue
        self._step_profile_registry = step_profile_registry
        self.default_step_timeout_seconds = max(1, int(default_step_timeout_seconds))
        self.default_llm_stream_idle_timeout_seconds = max(
            1, int(default_llm_stream_idle_timeout_seconds)
        )
        self.default_llm_stream_max_retries = max(0, int(default_llm_stream_max_retries))
        self.tool_output_store = tool_output_store
        self.artifact_store = getattr(tool_router, "artifact_store", None)
        self.notification_service: Any = None
        self._task_queue: Any = None
        self._turn_scheduler: Any = None
        self._step_runtime_factory = step_runtime_factory
        self._follow_up_policy = FollowUpPolicy(llm=getattr(providers, "llm", None))
        # Track active child sessions per parent session for /stop cancellation
        self._active_children: dict[str, dict[str, asyncio.Task[Any]]] = {}
        self._children_lock = asyncio.Lock()
        self._wire_background_shell_completion_callbacks()

    def set_task_queue(self, task_queue: Any) -> None:
        """Wire the task queue after construction (breaks circular dependency).

        Must be called before the first agent turn so that controller tools
        ``create_task`` and ``cancel_task`` can submit/cancel via the queue.
        """
        self._task_queue = task_queue

    def set_turn_scheduler(self, turn_scheduler: Any) -> None:
        """Wire the turn scheduler after construction for managed conversations."""

        self._turn_scheduler = turn_scheduler

    async def _record_agent_work_context(
        self,
        *,
        session: SessionModel,
        controller_agent_id: str,
        controller_conversation_id: str,
        controller_session_id: str,
        target_agent_id: str,
    ) -> None:
        """Persist agent work provenance as immutable developer context."""

        content = "\n".join(
            [
                "Agent work context:",
                f"- This session is managed by Cognis agent `{controller_agent_id}` on behalf of the user.",
                "- Treat user messages in this session as instructions from that authenticated internal agent.",
                "- Do not mention this management context unless it is operationally relevant.",
                "- Use inline work for small actions.",
                "- Use delegate for specialist child work that must finish before this managed turn can continue.",
                "- Use create_task only for durable workflow-shaped work where asynchronous completion is appropriate.",
                "- If the controller must decide or start visible asynchronous work, return a concise blocking issue or recommendation.",
                f"- Controller conversation: {controller_conversation_id}",
                f"- Controller session: {controller_session_id}",
            ]
        )
        event = SessionEvent(
            type="developer_message",
            data={
                "role": "developer",
                "content": content,
                "content_type": "text",
                "source": "agent_work_context",
                "target_agent_id": target_agent_id,
            },
        )
        append_result = await self.providers.guardrails.record_events(
            session.session_id,
            [event],
            source="cognis_agent_work",
            user_email=session.user_email,
            agent_id=session.agent_id,
        )
        await self.session_cache.append_recorded_events(session, [event], append_result)

    def set_step_runtime_factory(self, step_runtime_factory: Any) -> None:
        """Wire the step runtime factory after construction when needed."""

        self._step_runtime_factory = step_runtime_factory

    def _wire_background_shell_completion_callbacks(self) -> None:
        executor_provider = getattr(self.providers, "executor", None)
        for provider in (
            getattr(executor_provider, "in_process", None),
            getattr(executor_provider, "websocket", None),
        ):
            registrar = getattr(provider, "register_background_shell_completed_callback", None)
            if callable(registrar):
                registrar(self._handle_background_shell_completed)

    async def _handle_background_shell_completed(
        self,
        executor_id: str,
        status: dict[str, Any],
    ) -> None:
        conversation_id = status.get("conversation_id")
        shell_id = status.get("shell_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        if not isinstance(shell_id, str) or not shell_id:
            return
        follow_up = self._follow_up_policy.build_background_tool_follow_up(
            conversation_id=conversation_id,
            shell_id=shell_id,
            executor_id=(
                str(status.get("executor_id") or executor_id)
                if status.get("executor_id") or executor_id
                else None
            ),
            executor_type=(
                str(status.get("executor_type"))
                if status.get("executor_type") is not None
                else None
            ),
            status=str(status.get("status") or "completed"),
            exit_code=(
                int(status["exit_code"]) if isinstance(status.get("exit_code"), int) else None
            ),
            command=str(status.get("command")) if status.get("command") is not None else None,
            description=(
                str(status.get("description")) if status.get("description") is not None else None
            ),
            runtime_seconds=(
                float(status["runtime_seconds"])
                if isinstance(status.get("runtime_seconds"), (int, float))
                else None
            ),
            output_tail=str(status.get("output_tail"))
            if status.get("output_tail") is not None
            else None,
        )
        await self.event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": conversation_id,
                    "follow_up": follow_up.model_dump(mode="json"),
                },
            )
        )

    async def _resolve_llm_stream_idle_config(
        self,
        *,
        provider_id: str | None,
        model_id: str,
    ) -> tuple[int, int]:
        """Resolve idle watchdog timeout and retry count for an LLM stream."""

        idle_timeout = self.default_llm_stream_idle_timeout_seconds
        max_retries = self.default_llm_stream_max_retries
        resolver = getattr(self.providers.llm, "resolve_stream_idle_config", None)
        if callable(resolver):
            try:
                resolved = await resolver(
                    provider_id=provider_id,
                    model_id=model_id,
                    default_idle_timeout_seconds=idle_timeout,
                    default_max_retries=max_retries,
                )
                if isinstance(resolved, dict):
                    idle_timeout = _positive_int(resolved.get("idle_timeout_seconds"), idle_timeout)
                    max_retries = max(0, _positive_int(resolved.get("max_retries"), max_retries))
            except Exception:
                logger.warning(
                    "agent: failed to resolve LLM stream idle config; using defaults",
                    extra={
                        "extra_data": {
                            "provider_id": provider_id,
                            "model": model_id,
                        }
                    },
                    exc_info=True,
                )
        return max(1, idle_timeout), max(0, max_retries)

    async def run_step(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
        on_thinking: Any | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        on_tool_progress: ToolProgressCallback | None = None,
        on_tool_output_chunk: ToolOutputChunkCallback | None = None,
    ) -> StepOutput | None:
        """Run a single step as a full agentic loop.

        For Direct workflow (main chat): step_complete is optional.
        For multi-step workflows: step_complete is required.

        Returns StepOutput if the step completed, None if it failed.
        """
        start_time = datetime.now(UTC)
        ctx.on_tool_progress = on_tool_progress
        ctx.on_tool_output_chunk = on_tool_output_chunk
        logger.info(
            "agent: step started",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "step": ctx.step_definition.name,
                    "policy": ctx.policy.event_flush_strategy,
                }
            },
        )
        await self.session_lock.acquire(ctx.session.session_id)
        timeout_seconds = self._resolve_step_timeout_seconds(ctx)
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        self._execute_step(
                            ctx,
                            on_token=on_token,
                            on_thinking=on_thinking,
                            on_tool_call=on_tool_call,
                            on_tool_result=on_tool_result,
                        ),
                        timeout=max(1, timeout_seconds),
                    )
                except TimeoutError:
                    if ctx.timeout_continuation_count >= max(0, ctx.max_timeout_continuations):
                        raise
                    ctx.timeout_continuation_count += 1
                    ctx.is_retry = True
                    ctx.timeout_continuation_message = _timeout_continuation_prompt(ctx)
                    pending_events = ctx.pending_events
                    if pending_events is None:
                        pending_events = []
                        ctx.pending_events = pending_events
                    _append_interrupted_tool_results(ctx, pending_events)
                    notice = (
                        f"Turn interrupted by the {timeout_seconds}s safety timeout; "
                        f"continuing automatically ({ctx.timeout_continuation_count}/"
                        f"{ctx.max_timeout_continuations})."
                    )
                    pending_events.append(
                        SessionEvent(
                            type="lifecycle",
                            data={"event": "system_notice", "message": notice},
                        )
                    )
                    await self._emergency_flush_events(ctx, pending_events)
                    logger.warning(
                        "agent: step interrupted by timeout and continued",
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "task_id": ctx.task_id,
                                "step": ctx.step_definition.name,
                                "timeout_seconds": timeout_seconds,
                                "continuation_attempt": ctx.timeout_continuation_count,
                                "max_continuations": ctx.max_timeout_continuations,
                            }
                        },
                    )
        except TimeoutError:
            error_msg = f"Step timed out after {timeout_seconds}s"
            pending_events = ctx.pending_events
            if pending_events is None:
                pending_events = []
                ctx.pending_events = pending_events
            _append_interrupted_tool_results(ctx, pending_events)
            pending_events.append(
                SessionEvent(
                    type="lifecycle",
                    data={"event": "system_notice", "message": error_msg},
                )
            )
            await self._emergency_flush_events(ctx, pending_events)
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="error").inc()
            return StepOutput(
                summary="Step timed out",
                error=error_msg,
                session_id=ctx.session.session_id,
                intaris_session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                completed_at=datetime.now(UTC),
            )
        except StepInterrupted:
            # Emergency flush: persist any accumulated events before
            # the cancellation propagates — events represent real work.
            await self._emergency_flush_events(ctx, ctx.pending_events)
            raise
        except ImmutablePrefixUnavailable:
            await self._emergency_flush_events(ctx, ctx.pending_events)
            raise
        except Exception as exc:
            logger.exception(
                "Agent loop step failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            # Record a system_notice so the failure is visible in the
            # step session logs (UI chat timeline).
            error_msg = f"{type(exc).__name__}: {exc}"
            pending_events = ctx.pending_events
            if pending_events is None:
                pending_events = []
                ctx.pending_events = pending_events
            pending_events.append(
                SessionEvent(
                    type="lifecycle",
                    data={
                        "event": "system_notice",
                        "message": f"Step failed: {error_msg[:500]}",
                    },
                )
            )
            # Emergency flush: persist any accumulated events (including
            # the error notice) before reporting failure.
            await self._emergency_flush_events(ctx, pending_events)
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="error").inc()
            # Return a StepOutput with the error so it can be stored and
            # displayed in the UI instead of silently returning None.
            return StepOutput(
                summary=f"Step failed: {type(exc).__name__}",
                error=error_msg[:2000],
                session_id=ctx.session.session_id,
                intaris_session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                completed_at=datetime.now(UTC),
            )
        finally:
            self.session_lock.release(ctx.session.session_id)
            duration = (datetime.now(UTC) - start_time).total_seconds()
            STEP_DURATION.labels(phase="total").observe(duration)

    async def _resolve_child_agent(
        self, child_agent_id: str, parent_agent: AgentDefinition, *, user_email: str
    ) -> AgentDefinition:
        """Resolve the AgentDefinition for a child session (#3).

        Delegation must fail closed when the requested agent is not accessible.
        """
        if child_agent_id == parent_agent.agent_id:
            return parent_agent
        from cognis.core.agent_registry import AgentRegistry
        from cognis.store.queries import get_active_agent_grant

        registry = AgentRegistry(self.session_manager.session_factory)
        child_agent = await registry.get(child_agent_id, owner_email=user_email)
        if child_agent is None or child_agent.status != "active":
            raise RuntimeError(f"Delegated agent '{child_agent_id}' is not available")
        if not child_agent.is_system and child_agent.owner_email != user_email:
            async with self.session_manager.session_factory() as db:
                grant = await get_active_agent_grant(db, child_agent.agent_id, user_email)
            if grant is None:
                raise RuntimeError(f"Delegated agent '{child_agent_id}' is not accessible")
        return child_agent

    @staticmethod
    def _executor_agent_for_child(
        parent_agent: AgentDefinition,
        child_agent: AgentDefinition,
    ) -> AgentDefinition:
        if child_agent.is_system or child_agent.agent_type == "secondary":
            return parent_agent
        return child_agent

    async def _resolve_child_runtime(
        self,
        *,
        agent: AgentDefinition,
        executor_agent: AgentDefinition,
        user_email: str,
        access_context: RuntimeAccessContext | None = None,
        conversation_id: str | None = None,
    ) -> ResolvedStepRuntime:
        """Resolve a fresh runtime for delegated child sessions when possible."""

        if callable(self._step_runtime_factory):
            try:
                return await self._step_runtime_factory(
                    agent=agent,
                    user_email=user_email,
                    executor_agent=executor_agent,
                    access_context=access_context,
                    conversation_id=conversation_id,
                )
            except TypeError as exc:
                if "conversation_id" in str(exc):
                    # Older factory without conversation_id support
                    try:
                        return await self._step_runtime_factory(
                            agent=agent,
                            user_email=user_email,
                            executor_agent=executor_agent,
                            access_context=access_context,
                        )
                    except TypeError as exc2:
                        if "access_context" not in str(exc2):
                            raise
                        return await self._step_runtime_factory(
                            agent=agent,
                            user_email=user_email,
                            executor_agent=executor_agent,
                        )
                if "access_context" not in str(exc):
                    raise
                return await self._step_runtime_factory(
                    agent=agent,
                    user_email=user_email,
                    executor_agent=executor_agent,
                )

        raise RuntimeError("Step runtime factory unavailable; refusing delegation fallback")

    # ------------------------------------------------------------------
    # Child session tracking (for /stop cancellation)
    # ------------------------------------------------------------------

    async def _track_child(
        self, parent_session_id: str, child_session_id: str, task: asyncio.Task[Any]
    ) -> None:
        """Register an active child session task."""
        async with self._children_lock:
            self._active_children.setdefault(parent_session_id, {})[child_session_id] = task

    async def _untrack_child(self, parent_session_id: str, child_session_id: str) -> None:
        """Remove a child session from tracking."""
        async with self._children_lock:
            children = self._active_children.get(parent_session_id)
            if children:
                children.pop(child_session_id, None)
                if not children:
                    self._active_children.pop(parent_session_id, None)

    async def cancel_children(self, parent_session_id: str) -> int:
        """Cancel all active child sessions for a parent. Returns count cancelled."""
        async with self._children_lock:
            children = self._active_children.pop(parent_session_id, {})
        cancelled = 0
        for _child_id, task in children.items():
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    # ------------------------------------------------------------------
    # Child session execution
    # ------------------------------------------------------------------

    async def _run_child_session(
        self,
        *,
        child_session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        task_description: str,
        parent_intaris_session_id: str,
        workspace_root: str | None = None,
        working_directory: str | None = None,
        workspace_root_explicit: bool = False,
        working_directory_explicit: bool = False,
        deliverable_step_run_id: str | None = None,
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> StepOutput | None:
        """Run a delegated child session.

        Executes a single agent-loop turn on the child session using the
        task description as the user message.  The child session requires
        explicit ``step_complete`` (is_direct=False) to prevent premature
        completion.

        Returns the StepOutput on success, None on failure.

        Side effects (always executed):
        - Updates child session status in Cognis DB
        - Records delegation result in parent Intaris session
        - Publishes event bus events for frontend
        """
        conversation_id = conversation.conversation_id
        child_session_id = child_session.session_id

        output: StepOutput | None = None
        child_runtime: ResolvedStepRuntime | None = None
        child_ctx: StepContext | None = None

        try:
            resolved_agent = await self._resolve_child_agent(
                child_session.agent_id,
                agent,
                user_email=child_session.user_email,
            )
            child_executor_agent = self._executor_agent_for_child(agent, resolved_agent)
            child_access_context = RuntimeAccessContext(
                user_email=child_session.user_email,
                agent_id=child_executor_agent.agent_id,
                agent_owner_email=child_executor_agent.owner_email,
                agent_type=child_executor_agent.agent_type,
                session_id=child_session.session_id,
                conversation_id=getattr(conversation, "conversation_id", None)
                if conversation is not None
                else None,
                parent_session_id=getattr(child_session, "parent_session_id", None),
                delegation_mode=getattr(child_session, "delegation_mode", None),
                workflow_step=False,
            )
            child_runtime = await self._resolve_child_runtime(
                agent=resolved_agent,
                executor_agent=child_executor_agent,
                user_email=child_session.user_email,
                access_context=child_access_context,
                conversation_id=getattr(conversation, "conversation_id", None)
                if conversation is not None
                else None,
            )

            child_step = StepDefinition(
                name="delegation",
                type="run",
                prompt=task_description,
                require_deliverable=False,
            )
            # Secondary agents (both shipped system agents such as system:explore
            # and user-managed secondary agents) run with a slim policy:
            # step_complete is optional, memory is skipped, and project
            # instructions are skipped.  Primary agent delegations (self-
            # delegation and user-defined primary-to-primary) keep the full
            # DELEGATION_POLICY so they retain identity, memory, and the
            # write_deliverable / step_complete requirement.
            child_policy = (
                SECONDARY_AGENT_DELEGATION_POLICY
                if resolved_agent.agent_type == "secondary"
                else DELEGATION_POLICY
            )
            child_ctx = StepContext(
                step_definition=child_step,
                session=child_session,
                conversation=conversation,
                agent=resolved_agent,
                executor_agent=child_executor_agent,
                policy=child_policy,
                user_message=task_description,
                system_initiated=True,
                interaction_mode="explicit_gates",
                tool_registry=child_runtime.tool_registry,
                executor_connection=child_runtime.executor_connection,
                executor_environment=child_runtime.executor_environment,
                executor_pool=getattr(child_runtime, "executor_pool", None),
                active_executor_id=getattr(child_runtime, "active_executor_id", None),
                runtime_info=child_runtime.runtime_info or {},
                workspace_root=workspace_root,
                working_directory=working_directory,
                workspace_root_explicit=workspace_root_explicit,
                working_directory_explicit=working_directory_explicit,
                deliverable_step_run_id=deliverable_step_run_id,
                orchestration_mode=OrchestrationMode.NONE,  # Sub-sessions cannot delegate
            )

            # Set runtime context for JWT headers
            with scoped_runtime_context(
                user_email=child_session.user_email,
                agent_id=child_executor_agent.agent_id,
                agent_owner_email=child_executor_agent.owner_email,
                workspace_root=workspace_root,
                effective_working_directory=working_directory,
                access_context=child_access_context,
            ):
                output = await self.run_step(
                    child_ctx,
                    on_token=on_token,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                if output is None or output.error is not None:
                    error_text = output.error if output and output.error else "no step output"
                    salvage_selection = await self._select_delegation_result_content(
                        child_session=child_session,
                        step_output=output,
                    )
                    if salvage_selection.source == "saved_tool_results":
                        output = StepOutput(
                            summary=("Partial delegated result recovered from saved tool outputs."),
                            content=salvage_selection.content,
                            outputs={
                                "recovered_from_saved_tool_results": True,
                                "original_error": error_text,
                            },
                            claims=[],
                            session_id=child_session.session_id,
                            intaris_session_id=child_session.intaris_session_id,
                            completed_at=datetime.now(UTC),
                        )
                    else:
                        raise RuntimeError(f"Delegation step failed: {error_text}")
                # Build durable child output from deliverables or all assistant
                # messages, so a short cleanup tail cannot replace the report.
                result_summary = output.summary if output and output.summary else "Completed."
                result_selection = await self._select_delegation_result_content(
                    child_session=child_session,
                    step_output=output,
                )
                result_content = result_selection.content
                deliverable_data: dict[str, Any] = {}
                if output and output.deliverable_id:
                    deliverable_data = {
                        "deliverable_id": output.deliverable_id,
                        "deliverable_version": output.deliverable_version,
                        "deliverable_format": output.deliverable_format,
                        "deliverable_title": output.deliverable_title,
                    }
                elif output:
                    # Replace stale tail text with the full aggregated child output.
                    output.content = result_content
                    if self._looks_like_meta_complaint(result_summary):
                        result_summary = compact_snippet(result_content, max_chars=500)
                        output.summary = result_summary or "Completed."

                # Update child session status — guarded
                try:
                    await self.session_manager.mark_completed(
                        child_session_id,
                        result_summary=result_summary,
                        result_content=result_content,
                    )
                except Exception:
                    logger.warning(
                        "delegation: failed to update child session status",
                        extra={"extra_data": {"child_session_id": child_session_id}},
                        exc_info=True,
                    )

                delegation_title = _delegation_title({"task": task_description})
                # Record result in parent Intaris session — guarded
                try:
                    await self.providers.guardrails.record_events(
                        session_id=parent_intaris_session_id,
                        events=with_session_events_turn_id(
                            [
                                SessionEvent(
                                    type="delegation",
                                    data={
                                        "status": "completed",
                                        "child_session_id": child_session_id,
                                        "mode": "delegate",
                                        "title": delegation_title,
                                        "task_title": delegation_title,
                                        "input_redacted": True,
                                        "agent_id": child_session.agent_id,
                                        "used_agent_id": child_session.agent_id,
                                        "result_summary": result_summary,
                                        "result_content": result_content,
                                        "result_source": result_selection.source,
                                        "result_truncated": result_selection.truncated,
                                        "result_anchors": result_selection.anchors,
                                        "todos": _delegation_progress_todos(child_ctx),
                                        **deliverable_data,
                                    },
                                )
                            ],
                            None,
                        ),
                        idempotency_key=(
                            f"{parent_intaris_session_id}:delegation_completed_{child_session_id}"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "delegation: failed to record completion in parent session",
                        extra={"extra_data": {"child_session_id": child_session_id}},
                        exc_info=True,
                    )

                # Publish event bus event for frontend
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_COMPLETED,
                        data={
                            "conversation_id": conversation_id,
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                            "agent_id": child_session.agent_id,
                            "title": delegation_title,
                            "task_title": delegation_title,
                            "input_redacted": True,
                            "result_summary": result_summary,
                            "result_content": result_content,
                            "result_source": result_selection.source,
                            "result_anchors": result_selection.anchors,
                            "result_truncated": result_selection.truncated,
                            "todos": _delegation_progress_todos(child_ctx),
                        },
                    )
                )
                DELEGATIONS_TOTAL.labels(status="completed").inc()
                logger.info(
                    "delegation: child session completed",
                    extra={
                        "extra_data": {
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                        }
                    },
                )
        except asyncio.CancelledError:
            # Parent task was cancelled while waiting on a sync child session.
            # Mark the child cancelled so the DB row is not left active.
            logger.info(
                "delegation: child session cancelled (parent cancellation)",
                extra={
                    "extra_data": {
                        "child_session_id": child_session_id,
                        "parent_session_id": parent_intaris_session_id,
                    }
                },
            )
            try:
                await self.session_manager.mark_cancelled(
                    child_session_id,
                    result_summary="Cancelled (parent task cancelled)",
                )
            except Exception:
                logger.warning("delegation: failed to mark child session cancelled", exc_info=True)

            try:
                await self.providers.guardrails.record_events(
                    session_id=parent_intaris_session_id,
                    events=with_session_events_turn_id(
                        [
                            SessionEvent(
                                type="delegation",
                                data={
                                    "status": "cancelled",
                                    "child_session_id": child_session_id,
                                    "mode": "delegate",
                                    "title": _delegation_title({"task": task_description}),
                                    "task_title": _delegation_title({"task": task_description}),
                                    "input_redacted": True,
                                    "agent_id": child_session.agent_id,
                                    "used_agent_id": child_session.agent_id,
                                },
                            )
                        ],
                        None,
                    ),
                    idempotency_key=(
                        f"{parent_intaris_session_id}:delegation_cancelled_{child_session_id}"
                    ),
                )
            except Exception:
                logger.warning(
                    "delegation: failed to record cancellation in parent session",
                    exc_info=True,
                )

            await self.event_bus.publish(
                Event(
                    type=EventType.DELEGATION_FAILED,
                    data={
                        "conversation_id": conversation_id,
                        "child_session_id": child_session_id,
                        "parent_session_id": parent_intaris_session_id,
                        "agent_id": child_session.agent_id,
                        "used_agent_id": child_session.agent_id,
                        "title": _delegation_title({"task": task_description}),
                        "task_title": _delegation_title({"task": task_description}),
                        "input_redacted": True,
                        "reason": "Cancelled",
                    },
                )
            )
            DELEGATIONS_TOTAL.labels(status="cancelled").inc()
            raise  # Re-raise so the parent coroutine also gets cancelled
        except Exception as exc:
            error_summary = f"{type(exc).__name__}: {str(exc)[:500]}"
            failed_summary = f"Delegation failed: {error_summary}"
            logger.exception(
                "delegation: child session failed",
                extra={
                    "extra_data": {
                        "child_session_id": child_session_id,
                        "parent_session_id": parent_intaris_session_id,
                    }
                },
            )
            # Each operation guarded independently
            try:
                await self.session_manager.mark_failed(
                    child_session_id,
                    result_summary=failed_summary,
                )
            except Exception:
                logger.warning("delegation: failed to mark child session as failed", exc_info=True)

            try:
                await self.providers.guardrails.record_events(
                    session_id=parent_intaris_session_id,
                    events=with_session_events_turn_id(
                        [
                            SessionEvent(
                                type="delegation",
                                data={
                                    "status": "failed",
                                    "child_session_id": child_session_id,
                                    "mode": "delegate",
                                    "title": _delegation_title({"task": task_description}),
                                    "task_title": _delegation_title({"task": task_description}),
                                    "input_redacted": True,
                                    "agent_id": child_session.agent_id,
                                    "used_agent_id": child_session.agent_id,
                                    "error": error_summary,
                                    "recoverable": True,
                                },
                            )
                        ],
                        None,
                    ),
                    idempotency_key=(
                        f"{parent_intaris_session_id}:delegation_failed_{child_session_id}"
                    ),
                )
            except Exception:
                logger.warning(
                    "delegation: failed to record failure in parent session",
                    exc_info=True,
                )

            failure_notice = (
                "Delegated sub-session hit a failure; saved work remains attached "
                "to the child session and can be used for recovery."
            )
            await self.event_bus.publish(
                Event(
                    type=EventType.SYSTEM_NOTICE,
                    data={
                        "conversation_id": conversation_id,
                        "session_id": parent_intaris_session_id,
                        "child_session_id": child_session_id,
                        "message": failure_notice,
                        "text": failure_notice,
                        "kind": "delegation_recovery",
                        "scope": "child_session",
                        "notice_id": (
                            f"{parent_intaris_session_id}:{child_session_id}:"
                            "delegation_recovery:child_session"
                        ),
                        "reason": error_summary,
                        "recoverable": True,
                    },
                )
            )

            # Publish event bus event for frontend
            await self.event_bus.publish(
                Event(
                    type=EventType.DELEGATION_FAILED,
                    data={
                        "conversation_id": conversation_id,
                        "child_session_id": child_session_id,
                        "parent_session_id": parent_intaris_session_id,
                        "agent_id": child_session.agent_id,
                        "used_agent_id": child_session.agent_id,
                        "title": _delegation_title({"task": task_description}),
                        "task_title": _delegation_title({"task": task_description}),
                        "input_redacted": True,
                        "reason": error_summary,
                        "recoverable": True,
                    },
                )
            )
            DELEGATIONS_TOTAL.labels(status="failed").inc()
            output = None
        finally:
            if child_runtime is not None:
                await child_runtime.cleanup()

        return output

    # Heuristic patterns that indicate the assistant text is a meta-complaint
    # (e.g. "tool budget reached" wrap-ups produced when the budget was hit
    # before the model could produce a substantive final answer) rather than
    # the actual task result. Used to demote such texts during result selection.
    _META_COMPLAINT_PATTERNS: tuple[str, ...] = (
        "tool budget",
        "tools are disabled",
        "tools are no longer available",
        "i can't call tools",
        "i can't update",
        "i cannot call tools",
        "i cannot update",
        "i am unable to",
        "no further tools",
        "maximum steps",
        "already provided the final findings",
        "no additional user-facing information",
        "remaining todo state is stale",
        "todo state is stale",
        "already delivered",
    )
    _META_COMPLAINT_MAX_LEN = 600

    @classmethod
    def _looks_like_meta_complaint(cls, text: str) -> bool:
        """Return True if assistant text looks like a budget/limit complaint.

        Used as a tiebreaker when choosing between a tail-end meta message and
        a substantive earlier message produced by the same sub-session. Pattern
        matching is intentionally conservative: only short messages that contain
        one of the well-known phrases are demoted.
        """
        if not text:
            return True
        if len(text) > cls._META_COMPLAINT_MAX_LEN:
            return False
        lower = text.lower()
        return any(pattern in lower for pattern in cls._META_COMPLAINT_PATTERNS)

    @staticmethod
    def _truncate_delegation_result_content(
        content: str,
        anchors: list[dict[str, Any]],
        *,
        max_chars: int = _DELEGATION_RESULT_MAX_CHARS,
    ) -> tuple[str, list[dict[str, Any]], bool, int | None]:
        if len(content) <= max_chars:
            return content, anchors, False, None
        original_length = len(content)
        notice = _DELEGATION_RESULT_TRUNCATION_TEMPLATE.format(
            original_length=original_length,
            max_chars=max_chars,
        )
        truncated = content[: max(0, max_chars - len(notice))] + notice
        retained_lines = truncated.count("\n") + 1
        bounded_anchors = []
        for anchor in anchors:
            start_line = int(anchor.get("start_line") or 0)
            if start_line > retained_lines:
                continue
            bounded = dict(anchor)
            end_line = int(bounded.get("end_line") or retained_lines)
            if end_line > retained_lines:
                bounded["end_line"] = retained_lines
            bounded_anchors.append(bounded)
        return truncated, bounded_anchors, True, original_length

    @classmethod
    def _anchors_from_delegation_result_content(cls, content: str) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        lines = content.splitlines()
        for line_number, line in enumerate(lines, start=1):
            match = re.fullmatch(r"\[assistant_message:(\d+)\]", line.strip())
            if not match:
                continue
            end_line = len(lines)
            for next_line_number in range(line_number + 1, len(lines) + 1):
                if re.fullmatch(r"\[assistant_message:\d+\]", lines[next_line_number - 1].strip()):
                    end_line = max(line_number, next_line_number - 2)
                    break
            index = match.group(1)
            anchors.append(
                {
                    "anchor": f"assistant_message:{index}",
                    "kind": "assistant_message",
                    "label": f"Assistant message {index}",
                    "start_line": line_number,
                    "end_line": end_line,
                }
            )
        return anchors

    @classmethod
    def _build_delegation_saved_work_result(
        cls,
        events: list[dict[str, Any]],
    ) -> _DelegationResultContent | None:
        """Build a partial delegation result from saved tool outputs."""

        tool_calls: dict[str, dict[str, Any]] = {}
        result_sections: list[tuple[str, str, list[str]]] = []
        for event in events:
            event_type = event.get("type")
            data = event.get("data") or {}
            if not isinstance(data, dict):
                continue
            call_id = data.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if event_type == "tool_call":
                tool_calls[call_id] = data
                continue
            if event_type != "tool_result":
                continue
            raw_result = data.get("result")
            result = raw_result if isinstance(raw_result, str) else str(raw_result or "")
            if not result.strip():
                continue
            call_data = tool_calls.get(call_id, {})
            tool_name = str(data.get("name") or call_data.get("name") or "tool")
            preview = result.strip()
            if len(preview) > _DELEGATION_SALVAGE_RESULT_PREVIEW_CHARS:
                preview = (
                    preview[:_DELEGATION_SALVAGE_RESULT_PREVIEW_CHARS].rstrip()
                    + "\n[result preview truncated]"
                )
            lines = [
                f"Tool: {tool_name}",
                f"Call ID: {call_id}",
                f"Status: {'error' if data.get('is_error') else 'success'}",
            ]
            arguments = call_data.get("arguments")
            if arguments:
                lines.append(f"Arguments: {compact_snippet(str(arguments), max_chars=500)}")
            lines.extend(["", "Saved result preview:", preview])
            recovery_call_id = data.get("recovery_call_id")
            if not isinstance(recovery_call_id, str) or not recovery_call_id:
                recovery_call_id = call_id if data.get("has_full_output") else None
            if recovery_call_id:
                lines.extend(
                    [
                        "",
                        "Recovery:",
                        f"Use read_tool_output(call_id='{recovery_call_id}') for the full saved output if needed.",
                    ]
                )
            result_sections.append(
                (
                    f"tool_result:{len(result_sections) + 1}",
                    f"Tool result {len(result_sections) + 1}: {tool_name}",
                    lines,
                )
            )

        if not result_sections:
            return None

        retained_sections = result_sections[-_DELEGATION_SALVAGE_MAX_TOOL_RESULTS:]
        builder = AnchoredTextBuilder()
        builder.add_line(
            "Partial delegated result recovered from saved tool outputs. "
            "The child session failed before it produced a final assistant response, "
            "so this report preserves the completed tool work instead of discarding it."
        )
        builder.add_line("")
        for anchor, label, lines in retained_sections:
            builder.add_section(anchor, kind="tool_result", label=label, lines=lines)
        content, anchors = builder.build()
        content, anchors, truncated, original_length = cls._truncate_delegation_result_content(
            content,
            anchors,
        )
        return _DelegationResultContent(
            content=content,
            anchors=anchors,
            source="saved_tool_results",
            truncated=truncated,
            original_length=original_length,
            message_count=len(retained_sections),
        )

    async def _select_delegation_result_content(
        self,
        *,
        child_session: SessionModel,
        step_output: StepOutput | None,
    ) -> _DelegationResultContent:
        """Return durable delegate output while preserving all assistant reports."""
        text = step_output.content if step_output else ""
        if step_output and step_output.deliverable_id:
            deliverable_text = ""
            try:
                async with self.session_manager.session_factory() as db_session:
                    deliverable = await get_deliverable(db_session, step_output.deliverable_id)
                if (
                    deliverable is not None
                    and deliverable.status in _DELEGATION_ACTIVE_DELIVERABLE_STATUSES
                ):
                    deliverable_text = deliverable.content
            except Exception:
                logger.warning(
                    "delegation: failed to read deliverable content for result selection",
                    extra={"extra_data": {"child_session_id": child_session.session_id}},
                    exc_info=True,
                )
            deliverable_text = deliverable_text or text
            if deliverable_text:
                raw_anchor = {
                    "anchor": "deliverable",
                    "kind": "deliverable",
                    "label": step_output.deliverable_title or "Deliverable",
                    "start_line": 1,
                    "end_line": deliverable_text.count("\n") + 1,
                }
                content, anchors, truncated, original_length = (
                    self._truncate_delegation_result_content(
                        deliverable_text,
                        [raw_anchor],
                    )
                )
                return _DelegationResultContent(
                    content=content,
                    anchors=anchors,
                    source="deliverable",
                    truncated=truncated,
                    original_length=original_length,
                )

        intaris_session_id = child_session.intaris_session_id or child_session.session_id
        events: list[dict[str, Any]] = []
        after_seq = 0
        try:
            while True:
                event_result = await self.providers.guardrails.read_events(
                    session_id=intaris_session_id,
                    after_seq=after_seq,
                    limit=500,
                    allow_missing_stream=True,
                )
                events.extend(event for event in event_result.events if isinstance(event, dict))
                if not event_result.has_more or event_result.last_seq <= after_seq:
                    break
                after_seq = event_result.last_seq
        except Exception:
            logger.warning(
                "delegation: failed to read child session events for result selection",
                extra={"extra_data": {"child_session_id": child_session.session_id}},
                exc_info=True,
            )
            fallback = (
                text or (step_output.summary if step_output else "Completed.") or "Completed."
            )
            content, anchors, truncated, original_length = self._truncate_delegation_result_content(
                fallback, []
            )
            return _DelegationResultContent(
                content=content,
                anchors=anchors,
                source="step_output" if text else "fallback",
                truncated=truncated,
                original_length=original_length,
            )

        messages: list[str] = []
        for event in events:
            if event.get("type") != "assistant_message":
                continue
            data = event.get("data") or {}
            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            messages.append(content.strip())

        if messages:
            return _build_delegation_message_result(messages)

        saved_work = self._build_delegation_saved_work_result(events)
        if saved_work is not None:
            return saved_work

        fallback = text or (step_output.summary if step_output else "Completed.") or "Completed."
        content, anchors, truncated, original_length = self._truncate_delegation_result_content(
            fallback, []
        )
        return _DelegationResultContent(
            content=content,
            anchors=anchors,
            source="step_output" if text else "fallback",
            truncated=truncated,
            original_length=original_length,
        )

    async def _run_child_session_async(
        self,
        *,
        child_session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        task_description: str,
        parent_intaris_session_id: str,
        workspace_root: str | None = None,
        working_directory: str | None = None,
        workspace_root_explicit: bool = False,
        working_directory_explicit: bool = False,
        deliverable_step_run_id: str | None = None,
    ) -> None:
        """Async wrapper for _run_child_session that triggers follow-up turns.

        Used for wait=false (background) delegations.  After the child
        completes, publishes FOLLOW_UP_TURN_REQUESTED so the parent
        conversation gets a new system-initiated turn with the result.
        """
        parent_session_id = child_session.parent_session_id or ""
        child_session_id = child_session.session_id
        conversation_id = conversation.conversation_id
        child_tool_call_count = 0

        async def _child_progress_callback(
            call_id: str,
            tool_name: str,
            *_rest: Any,
            **_kw: Any,
        ) -> None:
            nonlocal child_tool_call_count
            child_tool_call_count += 1
            progress_todos: list[dict[str, Any]] = []
            result_content = _rest[0] if _rest and isinstance(_rest[0], str) else None
            if tool_name in {STEP_TODO_WRITE, STEP_TODO_LIST} and result_content:
                with contextlib.suppress(Exception):
                    payload = json.loads(result_content)
                    raw_todos = payload.get("todos") if isinstance(payload, dict) else None
                    if isinstance(raw_todos, list):
                        progress_todos = _normalize_todos(
                            [item for item in raw_todos if isinstance(item, dict)]
                        )
            progress_due = (
                child_tool_call_count % 3 == 0
                or child_tool_call_count == 1
                or tool_name in {STEP_TODO_WRITE, STEP_TODO_LIST}
            )
            if progress_due or progress_todos:
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_PROGRESS,
                        data={
                            "conversation_id": conversation_id,
                            "child_session_id": child_session_id,
                            "tool_call_count": child_tool_call_count,
                            "last_tool": tool_name,
                            "todos": progress_todos,
                        },
                    )
                )

        try:
            output = await self._run_child_session(
                child_session=child_session,
                conversation=conversation,
                agent=agent,
                task_description=task_description,
                parent_intaris_session_id=parent_intaris_session_id,
                workspace_root=workspace_root,
                working_directory=working_directory,
                workspace_root_explicit=workspace_root_explicit,
                working_directory_explicit=working_directory_explicit,
                deliverable_step_run_id=deliverable_step_run_id,
                on_tool_result=_child_progress_callback,
            )
            status = "completed" if output else "failed"
            result_summary = output.summary if output else None
        except Exception:
            status = "failed"
            result_summary = None
        finally:
            await self._untrack_child(parent_session_id, child_session_id)

        delivery_id: str | None = None
        channel_deliverable = False
        delivery_fallback_text: str | None = None
        try:
            async with self.session_manager.session_factory() as db_session:
                from cognis.store.queries import (
                    create_channel_delivery_outbox,
                    get_conversation_channel_route,
                )

                route = await get_conversation_channel_route(db_session, conversation_id)
                if route is not None:
                    channel_type, account_id, chat_id, thread_id, user_email = route
                    delivery_id = f"cdel_{uuid.uuid4().hex[:12]}"
                    delivery_fallback_text = {
                        "completed": "Background work completed. I could not deliver the detailed reply, so please open the conversation for the full result.",
                        "failed": "Background work failed. I could not deliver the detailed reply, so please open the conversation for details.",
                    }[status]
                    await create_channel_delivery_outbox(
                        db_session,
                        delivery_id=delivery_id,
                        user_email=user_email,
                        conversation_id=conversation_id,
                        session_id=child_session_id,
                        source_type="delegation",
                        source_id=child_session_id,
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        fallback_text=delivery_fallback_text,
                        next_attempt_at=datetime.now(UTC) + timedelta(minutes=2),
                    )
                    await db_session.commit()
                    channel_deliverable = True
        except Exception:
            logger.warning(
                "delegation: failed to persist channel follow-up delivery intent",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "child_session_id": child_session_id,
                    }
                },
                exc_info=True,
            )

        follow_up = self._follow_up_policy.build_delegation_follow_up(
            conversation_id=conversation_id,
            child_session_id=child_session_id,
            status=status,
            result_summary=result_summary,
        )

        # Trigger a follow-up turn in the parent conversation
        await self.event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": conversation_id,
                    "follow_up": follow_up.model_dump(mode="json"),
                    "delivery_id": delivery_id,
                    "channel_deliverable": channel_deliverable,
                    "delivery_fallback_text": delivery_fallback_text,
                },
            )
        )

    async def _execute_step(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
        on_thinking: Any | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> StepOutput | None:
        """Core step execution loop."""
        # Guard: compaction recursion depth.  Each successful compaction+rotation
        # that re-enters _execute_step increments ctx.compaction_recursion_depth.
        # When the cap is exceeded we surface a classified failure rather than
        # looping indefinitely.
        _max_compaction_recursion = int(getattr(self.compaction_strategy, "max_recursion", 2) or 2)
        if ctx.compaction_recursion_depth >= _max_compaction_recursion:
            notice = (
                "Compaction is not reducing context further; this conversation may need "
                "to be split. Try /new or rephrase the request."
            )
            await self._emit_compaction_notice(
                ctx,
                notice,
                on_token=on_token,
                persist=True,
            )
            logger.warning(
                "agent: compaction recursion depth exceeded",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "depth": ctx.compaction_recursion_depth,
                        "max": _max_compaction_recursion,
                    }
                },
            )
            return StepOutput(
                summary=notice,
                content="",
                outcome={
                    "status": "failed",
                    "reason": "compaction_recursion_exhausted",
                },
                metadata={"compaction_recursion_depth": ctx.compaction_recursion_depth},
            )

        # Budget selection:
        # 1. Secondary-agent delegations follow OpenCode: optional ``steps``
        #    caps LLM iterations, not tool calls. Tool-call ceilings do not
        #    apply to these sub-sessions.
        # 2. Per-agent explicit override in agent.execution.max_tool_calls for
        #    non-secondary workflow/chat contexts.
        # 3. All other steps use DEFAULT_MAX_TOOL_CALLS.
        _is_delegation = ctx.policy is SECONDARY_AGENT_DELEGATION_POLICY
        delegation_max_steps = self._resolve_delegation_max_steps(ctx) if _is_delegation else None
        if _is_delegation:
            max_tool_calls: int | None = None
        elif ctx.agent.execution and "max_tool_calls" in ctx.agent.execution:
            max_tool_calls = int(ctx.agent.execution["max_tool_calls"])
        else:
            max_tool_calls = DEFAULT_MAX_TOOL_CALLS
        # Whether we are in "force summary" mode: tools stripped, one LLM turn
        # to produce a final text result.
        _force_summary_mode = False

        tool_call_count = 0
        agentic_step_count = 0
        todo_reprompt_count = 0
        todo_cleanup_only_allowed = False
        step_output: StepOutput | None = None
        events_to_record: list[SessionEvent] = []
        ctx.pending_events = events_to_record
        ctx.pending_tool_calls.clear()
        if not ctx.turn_id:
            ctx.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        messages: list[dict[str, Any]] = []
        assistant_content_parts: list[str] = []  # User-visible assistant output only
        assistant_memory_parts: list[str] = []  # Include attachment notes for memory/compaction
        last_assistant_content = ""

        # Build tool definitions for LLM (controller-injected tools)
        controller_tool_schemas = self._build_controller_tool_schemas(ctx)

        # ---------------------------------------------------------------
        # Step 1: Build effective_user_message BEFORE recording so that
        # Intaris always receives the full prompt the LLM will see (not
        # just the raw step prompt from ctx.user_message).
        # ---------------------------------------------------------------
        if ctx.policy.require_step_complete:
            if ctx.is_retry:
                # On retry with a reused Intaris session, the conversation
                # history already contains the original step prompt, all
                # the agent's prior work, and the evaluation feedback event.
                # Sending the full step prompt again would cause the agent
                # to see it multiple times and restart from scratch.
                #
                # Instead, send only a revision directive.  If the Intaris
                # recording of evaluation feedback failed, deliver it inline.
                if ctx.timeout_continuation_message:
                    effective_user_message = ctx.timeout_continuation_message
                    retry_reason = None
                else:
                    retry_reason = getattr(ctx.workflow_state, "last_retry_reason", None)
                deliverable_step_run_id = self._deliverable_owner_step_run_id(ctx)
                if retry_reason == "execution_failed":
                    effective_user_message = (
                        "The previous attempt ended before producing a final response. "
                        "Its recorded tool calls and results are still in the session "
                        "history. Continue from that evidence; do not restart the step "
                        "or re-read files unless the needed detail is unavailable. If a "
                        "prior tool result is truncated or cleared, prefer "
                        "read_tool_output/search_tool_output with the recorded call_id. "
                        "When done, call step_complete."
                    )
                elif ctx.workflow_state and ctx.workflow_state.last_evaluation_feedback:
                    if self._deliverable_owner_step_run_id(ctx) is not None:
                        effective_user_message = (
                            "The evaluator has reviewed your previous attempt and "
                            "requested revisions:\n\n"
                            f"{ctx.workflow_state.last_evaluation_feedback}\n\n"
                            "Please revise your work based on this feedback. "
                            "When done, write_deliverable with the updated artifact "
                            "and then call step_complete."
                        )
                    else:
                        effective_user_message = (
                            "The evaluator has reviewed your previous attempt and "
                            "requested revisions:\n\n"
                            f"{ctx.workflow_state.last_evaluation_feedback}\n\n"
                            "Please revise your work based on this feedback. "
                            "When done, return the updated result as a normal assistant "
                            "message and then call step_complete."
                        )
                else:
                    # Feedback was recorded to Intaris — it's already in the
                    # session history.  Send a minimal revision directive.
                    if deliverable_step_run_id is not None:
                        effective_user_message = (
                            "The evaluator has reviewed your previous attempt and "
                            "requested revisions. Review the evaluation feedback "
                            "above and revise your work accordingly. When done, "
                            "write_deliverable with the updated artifact and then call "
                            "step_complete."
                        )
                    else:
                        effective_user_message = (
                            "The evaluator has reviewed your previous attempt and "
                            "requested revisions. Review the evaluation feedback "
                            "above and revise your work accordingly. When done, "
                            "return the updated result as a normal assistant message and "
                            "then call step_complete."
                        )
            else:
                # First attempt — build the full rich prompt with task context
                # and prior step outputs.
                effective_user_message = self._build_step_prompt(ctx)
        else:
            effective_user_message = (
                ctx.timeout_continuation_message
                if ctx.is_retry and ctx.timeout_continuation_message
                else ctx.user_message or ctx.step_definition.prompt
            )

        # ---------------------------------------------------------------
        # Step 2: Record effective_user_message to Intaris BEFORE context
        # assembly so the IntentionBarrier can start updating the session
        # intention in parallel.  This records the FULL prompt (including
        # task context and prior step outputs for workflow steps), not
        # just the raw step prompt.
        #
        # Skipped for:
        # - system_initiated turns (lifecycle event provides the trail)
        # - retry turns (session already has the original prompt)
        # ---------------------------------------------------------------
        recorded_user_message = _user_message_for_recording(
            effective_user_message,
            ctx.user_attachments,
        )
        record_system_user_message = (
            ctx.system_initiated and getattr(ctx.session, "parent_session_id", None) is not None
        )
        recorded_user_source = "delegation_input" if record_system_user_message else "user_input"
        if (
            recorded_user_message
            and (not ctx.system_initiated or record_system_user_message)
            and not ctx.is_retry
        ):
            user_msg_event = SessionEvent(
                type="user_message",
                data={
                    "role": "user",
                    "content": recorded_user_message,
                    "content_type": "text",
                    "source": recorded_user_source,
                    "turn_id": ctx.turn_id,
                    "chat_mode": ctx.chat_mode.mode if ctx.chat_mode else "default",
                    "chat_mode_source": ctx.chat_mode.source if ctx.chat_mode else "system_default",
                    "hash": hashlib.sha256(
                        json.dumps(
                            {
                                "role": "user",
                                "content": recorded_user_message,
                                "source": recorded_user_source,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "attachments": attachment_refs_to_dicts(
                        ctx.user_attachments,
                        include_url=False,
                    ),
                },
            )
            try:
                await self._record_events_strict(
                    ctx,
                    [user_msg_event],
                    reason="user_message",
                    on_token=on_token,
                )
            except Exception:
                logger.exception(
                    "agent: failed to record early user_message event",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                raise

            try:
                reasoning_result = await self._report_reasoning_strict(ctx, on_token=on_token)
                if reasoning_result and reasoning_result.updated_at:
                    updated = await self.session_cache.update_intention(
                        ctx.session.session_id,
                        reasoning_result.intention,
                        updated_at=reasoning_result.updated_at,
                    )
                    if updated and reasoning_result.title:
                        try:
                            async with self.session_manager.session_factory() as db_session:
                                ok = await sync_intaris_title(
                                    db_session,
                                    ctx.conversation,
                                    reasoning_result.title,
                                    updated_at=reasoning_result.updated_at,
                                )
                                if ok:
                                    await db_session.commit()
                                    if ctx.conversation.title:
                                        await publish_conversation_title_updated(
                                            self.event_bus,
                                            conversation_id=ctx.conversation.conversation_id,
                                            title=ctx.conversation.title,
                                        )
                        except Exception:
                            logger.debug(
                                "agent: failed to sync bootstrap title from Intaris",
                                extra={
                                    "extra_data": {
                                        "conversation_id": ctx.conversation.conversation_id,
                                    }
                                },
                                exc_info=True,
                            )
            except Exception:
                logger.exception(
                    "agent: failed to trigger intention update",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                raise

        # ---------------------------------------------------------------
        # Step 3: Assemble context (reads Intaris history + memory)
        # ---------------------------------------------------------------
        # Skip project context probing for secondary-agent delegations — they run
        # with a slim prompt and do not need the parent project's AGENTS.md.
        if not ctx.policy.skip_memory:
            await self._ensure_known_project_context_loaded(ctx)

        # Derive prompt context from execution policy
        if ctx.policy is WORKFLOW_POLICY:
            _prompt_ctx = PromptContext.TASK_STEP
        elif ctx.policy is DELEGATION_POLICY or ctx.policy is SECONDARY_AGENT_DELEGATION_POLICY:
            _prompt_ctx = PromptContext.DELEGATION
        elif ctx.follow_up is not None and ctx.follow_up.mode is FollowUpMode.INTEGRATE:
            _prompt_ctx = PromptContext.FOLLOW_UP_INTEGRATE
        elif ctx.follow_up is not None:
            _prompt_ctx = PromptContext.FOLLOW_UP_NOTIFY
        else:
            _prompt_ctx = PromptContext.CHAT

        routing_reminder = None
        executor_pin_notice = None
        if isinstance(ctx.runtime_info, dict):
            raw_notice = ctx.runtime_info.get("executor_pin_fallback_notice")
            if isinstance(raw_notice, dict):
                llm_message = raw_notice.get("llm_message")
                if isinstance(llm_message, str) and llm_message.strip():
                    executor_pin_notice = llm_message.strip()
        if (
            ctx.policy is CHAT_POLICY
            and _prompt_ctx is PromptContext.CHAT
            and not ctx.system_initiated
        ):
            advice = build_routing_reminder(effective_user_message)
            routing_reminder = advice.reminder if advice is not None else None
        if executor_pin_notice:
            routing_reminder = (
                f"{executor_pin_notice}\n\n{routing_reminder}"
                if routing_reminder
                else executor_pin_notice
            )
        if isinstance(ctx.runtime_info, dict):
            disabled_notices = ctx.runtime_info.get("disabled_artifact_notices")
            if isinstance(disabled_notices, list):
                artifact_notice = "\n\n".join(
                    notice.strip()
                    for notice in disabled_notices
                    if isinstance(notice, str) and notice.strip()
                )
                if artifact_notice:
                    routing_reminder = (
                        f"{artifact_notice}\n\n{routing_reminder}"
                        if routing_reminder
                        else artifact_notice
                    )
        if ctx.chat_mode and ctx.chat_mode.mode == "plan":
            reminder = plan_mode_reminder(source=ctx.chat_mode.source)
            routing_reminder = f"{reminder}\n\n{routing_reminder}" if routing_reminder else reminder

        try:
            context_result = await self.context_assembler.assemble(
                session=ctx.session,
                conversation=ctx.conversation,
                agent=ctx.agent,
                user_message=effective_user_message,
                user_attachments=attachment_refs_to_dicts(ctx.user_attachments),
                attachment_notice=ctx.attachment_notice,
                attachment_context=ctx.attachment_context,
                user_message_role=(
                    "system"
                    if ctx.is_retry
                    or (ctx.system_initiated and _prompt_ctx is not PromptContext.DELEGATION)
                    else "user"
                ),
                prior_context=ctx.prior_context,
                follow_up=ctx.follow_up,
                routing_reminder=routing_reminder,
                skip_memory=ctx.policy.skip_memory,
                prompt_context=_prompt_ctx,
                executor_environment=ctx.executor_environment,
                workspace_root=ctx.workspace_root,
                effective_working_directory=ctx.working_directory,
                # Secondary-agent delegations skip project instructions (AGENTS.md).
                # They run with a slim prompt; the project context is the caller's
                # concern, not the sub-session's.
                include_project_context=(
                    not ctx.policy.skip_memory
                    and (ctx.workspace_root_explicit or ctx.working_directory_explicit)
                ),
                executor_pool=getattr(ctx, "executor_pool", None),
                active_executor_id=(
                    getattr(ctx, "active_executor_id", None)
                    or getattr(ctx.conversation, "active_executor_id", None)
                ),
                disabled_artifact_urls=set(ctx.runtime_info.get("disabled_artifact_urls", [])),
                disabled_artifact_ids=set(ctx.runtime_info.get("disabled_artifact_ids", [])),
            )
        except ImmutablePrefixUnavailable:
            await self._record_system_notice_audit(
                ctx,
                "Immutable prefix is unavailable for this session.",
                turn_id=ctx.turn_id,
            )
            await self.event_bus.publish(
                Event(
                    type=EventType.SYSTEM_NOTICE,
                    data={
                        "conversation_id": ctx.conversation.conversation_id,
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        "text": "Immutable prefix is unavailable for this session.",
                    },
                )
            )
            raise
        messages = context_result.messages
        pending_audit_messages = list(getattr(context_result, "audit_messages", []) or [])

        # Surface any degraded-context notices (e.g. recall failure) to the
        # UI immediately so the user sees them before the LLM response arrives.
        if isinstance(ctx.runtime_info, dict):
            raw_notice = ctx.runtime_info.get("executor_pin_fallback_notice")
            if isinstance(raw_notice, dict):
                ui_message = raw_notice.get("ui_message")
                if isinstance(ui_message, str) and ui_message.strip():
                    await self.event_bus.publish(
                        Event(
                            type=EventType.SYSTEM_NOTICE,
                            data={
                                "conversation_id": ctx.conversation.conversation_id,
                                "session_id": ctx.session.session_id,
                                "turn_id": ctx.turn_id,
                                "message": ui_message.strip(),
                            },
                        )
                    )
        for _notice_text in getattr(context_result, "system_notices", []) or []:
            await self.event_bus.publish(
                Event(
                    type=EventType.SYSTEM_NOTICE,
                    data={
                        "conversation_id": ctx.conversation.conversation_id,
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        "message": _notice_text,
                    },
                )
            )

        def _queue_audit_message(*, role: str, source: str, content: str) -> None:
            self._append_pending_audit_message(
                messages,
                pending_audit_messages,
                role=role,
                source=source,
                content=content,
            )

        # Record user message event (unless already recorded early for
        # intention tracking above).  System-initiated follow-up turns
        # (task completion, delegation results) are NOT recorded — the
        # lifecycle event already provides the audit trail and the prompt
        # is an internal instruction, not user-visible content.
        # Capture cache breakpoint for prompt caching (Anthropic cache_control)
        cache_breakpoint = getattr(context_result, "cache_breakpoint_index", None)

        if _should_run_pre_turn_auto_compaction(ctx, context_result):
            compaction_run = CompactionRunContext.from_context_result(
                context_result,
                trigger="pre_turn_auto",
                reason="context_compaction_threshold",
            )
            preserve_turns = getattr(self.compaction_strategy, "preserve_turns", 10)
            if not _has_compactable_pre_turn_history(
                ctx,
                self.session_cache,
                preserve_turns=preserve_turns,
            ):
                compaction_run.status = "skipped"
                compaction_run.fallback_reason = "no_compactable_history"
                logger.info(
                    "agent: pre-turn compaction skipped; relying on projection",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            **self._step_log_metadata(ctx),
                            **compaction_run.event_data(),
                        }
                    },
                )
                projection_notice = (
                    (
                        "Context window is critically full, but there is no older "
                        "conversation history to compact. Continuing with prompt projection."
                    )
                    if compaction_run.hard_pressure_exceeded
                    else (
                        "Automatic compaction was recommended, but there is no older "
                        "conversation history to compact. Continuing with prompt projection."
                    )
                )
                await self._emit_compaction_notice(
                    ctx,
                    projection_notice,
                    on_token=on_token,
                    persist=True,
                    metadata=compaction_run.event_data(),
                )
            else:
                notice = _context_pressure_compaction_notice(context_result)
                await self._emit_compaction_notice(
                    ctx,
                    notice,
                    on_token=on_token,
                    persist=True,
                    metadata=compaction_run.event_data(),
                )
                compaction_result = await self._auto_compact(
                    ctx,
                    run=compaction_run,
                    on_token=on_token,
                )
                if compaction_result is not None:
                    if compaction_result.compacted:
                        new_session = await self._rotate_after_compaction(
                            ctx,
                            compaction_result,
                            trigger="pre_turn_auto",
                            run=compaction_run,
                        )
                        if new_session is not None:
                            await self._emit_compaction_notice(
                                ctx,
                                (
                                    "Automatic compaction completed. Continuing your turn in a "
                                    "fresh compacted session."
                                ),
                                on_token=on_token,
                                persist=False,
                                metadata=compaction_run.event_data(),
                            )
                            ctx.session = new_session
                            ctx.is_retry = True
                            ctx.prior_context = None
                            ctx.compaction_recursion_depth += 1
                            return await self._execute_step(
                                ctx,
                                on_token=on_token,
                                on_thinking=on_thinking,
                                on_tool_call=on_tool_call,
                                on_tool_result=on_tool_result,
                            )
                    else:
                        compaction_run.status = "skipped"
                        compaction_run.fallback_reason = (
                            f"compaction_{compaction_result.method or 'noop'}"
                        )
                        logger.info(
                            "agent: pre-turn compaction produced no compacted session; relying on projection",
                            extra={
                                "extra_data": {
                                    "session_id": ctx.session.session_id,
                                    "turn_id": ctx.turn_id,
                                    **self._step_log_metadata(ctx),
                                    "compaction_method": compaction_result.method,
                                    **compaction_run.event_data(),
                                }
                            },
                        )
                        await self._emit_compaction_notice(
                            ctx,
                            (
                                "Automatic compaction found no older history to compact. "
                                "Continuing with prompt projection."
                            ),
                            on_token=on_token,
                            persist=True,
                            metadata=compaction_run.event_data(),
                        )
                elif compaction_run.hard_pressure_exceeded:
                    step_output = StepOutput(
                        summary=(
                            "Stopped before the model call because automatic compaction failed "
                            "while the session was over the hard context-pressure threshold."
                        ),
                        content="",
                        outcome={
                            "status": "failed",
                            "reason": "Context pressure exceeded and automatic compaction failed.",
                        },
                        metadata={"context_pressure": compaction_run.event_data()},
                    )
                    await self._emit_compaction_notice(
                        ctx,
                        (
                            "Context window is critically full and automatic compaction failed. "
                            "Stopping this turn before another model call; please retry after "
                            "manual compaction if needed."
                        ),
                        on_token=on_token,
                        persist=True,
                        metadata=compaction_run.event_data(),
                    )
                    return step_output
                elif compaction_result is not None:
                    await self._emit_compaction_notice(
                        ctx,
                        (
                            "Automatic compaction was requested, but there was not enough "
                            "older context to compact. Continuing the current turn."
                        ),
                        on_token=on_token,
                        persist=True,
                        metadata=compaction_run.event_data(),
                    )

        # Main agentic loop
        step_reprompt_count = 0
        empty_direct_response_reprompt_count = 0
        mid_stream_retries = 0
        openai_tool_search_retries = 0
        _MAX_OPENAI_TOOL_SEARCH_RETRIES = 1
        model_error_continuation_count = 0
        max_model_error_continuations = _MODEL_ERROR_CONTINUATION_MAX_ATTEMPTS
        idle_timeout_continuation_count = 0
        max_idle_timeout_continuations = _IDLE_TIMEOUT_CONTINUATION_MAX_ATTEMPTS
        saved_partial_tool_calls: dict[int, dict[str, Any]] | None = None
        promoted_tool_ids = self._get_initial_promoted_tool_ids(ctx)
        activated_tool_ids = self._get_initial_activated_tool_ids(ctx)
        queued_discovery_guidance_mode: ToolDiscoveryMode | None = None
        collected_attachments: list[dict[str, Any]] = []
        pending_assistant_attachments: list[dict[str, Any]] = []
        retry_projected_model: ProjectedMessages | None = None
        continued_assistant_content = ""
        continuation_message_index: int | None = None
        continuation_reminder_index: int | None = None
        last_cycle_end_at: float | None = None
        workflow_step_reminder_added = False
        background_work_reminder_added = False
        queued_edit_guidance: str | None = None
        edit_guidance_message_index: int | None = None

        # Seed per-turn projection state from the cross-turn assembly result.
        # This initialises committed_preservations so the first within-turn
        # projection knows which groups were already preserved.
        if ctx.projection_state is None and context_result is not None:
            try:
                from cognis.core.context_budget import resolve_context_budget as _rcb
                from cognis.core.context_projection import PROJECTED_COMPACTED as _PC  # noqa: F401
                from cognis.core.context_projection import ProjectionResult as _PR

                _seed_budget = _rcb(
                    max_context_tokens=getattr(context_result, "max_context_tokens", 0),
                    max_input_tokens=getattr(context_result, "max_input_tokens", 0),
                    agent_max_tokens=(
                        ctx.agent.llm_config.max_tokens if ctx.agent.llm_config else None
                    ),
                    model_max_output_tokens=getattr(ctx.current_model_info, "max_output_tokens", 0),
                )
                _seed_policy = ProjectionPolicy.from_budget(
                    max_context_tokens=getattr(context_result, "max_context_tokens", 0),
                    available_prompt_tokens=_seed_budget.available_prompt_tokens,
                    phase="within_turn",
                    pressure_mode=PressureMode.normal,
                )
                ctx.projection_state = ProjectionTurnState(
                    turn_id=ctx.turn_id or "",
                    policy=_seed_policy,
                )
                # Seed committed_preservations from the cross-turn projected messages.
                _cross_messages = list(getattr(context_result, "messages", []))
                _cross_result = _PR(messages=_cross_messages, mutable_start_index=0)
                ctx.projection_state.seed_from_cross_turn_result(_cross_result, _cross_messages)
            except Exception:
                # Non-critical: if seeding fails (e.g. in tests with fake assemblers),
                # projection_state stays None and will be initialised lazily on first use.
                pass

        while True:
            self._raise_if_cancelled(ctx)
            agentic_step_count += 1
            cycle_started_at = monotonic()
            cycle_step_type = _agent_loop_step_type(ctx)
            if last_cycle_end_at is not None:
                inter_cycle_gap_seconds = max(0.0, cycle_started_at - last_cycle_end_at)
                AGENT_CYCLE_INTER_GAP_DURATION.labels(step_type=cycle_step_type).observe(
                    inter_cycle_gap_seconds
                )
                logger.info(
                    "agent: inter-cycle gap",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            **self._step_log_metadata(ctx),
                            "cycle_index": agentic_step_count,
                            "duration_seconds": round(inter_cycle_gap_seconds, 3),
                        }
                    },
                )
            if _is_delegation and delegation_max_steps is not None:
                _force_summary_mode = agentic_step_count >= delegation_max_steps

            if not workflow_step_reminder_added:
                workflow_step_reminder = self._build_workflow_step_reminder(ctx)
                if workflow_step_reminder is not None:
                    messages.append(workflow_step_reminder)
                workflow_step_reminder_added = True

            # Stage 36: non-primary active executor reminder. Injected on every
            # LLM turn while the active executor is in the agent's *additional*
            # set. Primaries are by definition legitimate hosts and need no
            # reminder. The reminder is non-persistent (stripped from history)
            # via the standard internal-fields prefix convention.
            non_primary_reminder = self._build_non_primary_active_reminder(ctx)
            if non_primary_reminder is not None:
                messages.append(non_primary_reminder)

            background_shell_reminder = await self._build_background_shell_status_reminder(ctx)
            if background_shell_reminder is not None:
                messages.append(background_shell_reminder)

            if not background_work_reminder_added:
                background_work_reminder = await self._build_background_work_status_reminder(ctx)
                if background_work_reminder is not None:
                    messages.append(background_work_reminder)
                background_work_reminder_added = True

            # Post-deliverable nudge: if the model wrote the deliverable,
            # marked all todos terminal, and stopped calling tools without
            # invoking step_complete, prepend a strong system reminder so
            # the next assistant turn finalizes the step.  Capped at 2
            # emissions to avoid an infinite reminder loop on a stuck
            # model.
            self._maybe_inject_post_deliverable_reminder(ctx, messages)

            if _force_summary_mode and _is_delegation:
                messages.append(
                    {
                        "role": "system",
                        "content": self._build_delegation_max_steps_notice(ctx),
                        "_force_summary": True,
                    }
                )

            resolved_agent_profile = resolve_agent_profile(
                ctx.agent,
                requested_agent_profile_id(ctx.session, ctx.conversation),
                source="conversation",
            )
            ctx.runtime_info.update(resolved_agent_profile.audit_metadata())

            # Resolve model and reasoning effort for this turn.
            # Chain: session override → agent profile → agent config → provider default.
            model_for_llm = self.session_cache.get_model_override(ctx.session.session_id) or (
                resolved_agent_profile.model
                or (ctx.agent.llm_config.model if ctx.agent.llm_config else None)
            )
            provider_for_llm = resolved_agent_profile.provider_id or (
                ctx.agent.llm_config.provider_id if ctx.agent.llm_config else None
            )

            reasoning_effort = (
                self.session_cache.get_reasoning_effort_override(ctx.session.session_id)
                or getattr(ctx.step_definition, "reasoning_effort", None)
                or resolved_agent_profile.reasoning_effort
                or (ctx.agent.llm_config.reasoning_effort if ctx.agent.llm_config else None)
            )
            ctx.runtime_info["current_reasoning_effort"] = reasoning_effort

            llm_kwargs: dict[str, Any] = {}
            if reasoning_effort:
                llm_kwargs["reasoning_effort"] = reasoning_effort
            if ctx.agent.llm_config:
                if ctx.agent.llm_config.temperature is not None:
                    llm_kwargs["temperature"] = ctx.agent.llm_config.temperature
                if ctx.agent.llm_config.top_p is not None:
                    llm_kwargs["top_p"] = ctx.agent.llm_config.top_p
                if ctx.agent.llm_config.max_tokens is not None:
                    # Forward the agent's explicit max_tokens override so the
                    # provider call matches what context.py reserved for the
                    # output budget. Without this, generate() auto-fills with
                    # model_info.max_output_tokens, which can exceed what the
                    # agent was configured for.
                    llm_kwargs["max_tokens"] = ctx.agent.llm_config.max_tokens

            resolved_model = getattr(context_result, "resolved_model", "")
            current_model = model_for_llm or resolved_model
            current_provider_id: str | None = None
            if hasattr(self.providers.llm, "resolve_model_target"):
                try:
                    (
                        current_model,
                        current_provider_id,
                    ) = await self.providers.llm.resolve_model_target(
                        explicit_model=model_for_llm,
                        task_type="default",
                        explicit_provider_id=provider_for_llm,
                        acting_user_email=ctx.session.user_email,
                    )
                except TypeError:
                    (
                        current_model,
                        current_provider_id,
                    ) = await self.providers.llm.resolve_model_target(
                        explicit_model=model_for_llm,
                        task_type="default",
                    )
            if current_provider_id is not None:
                try:
                    model_info = await self.providers.llm.get_model_info(
                        current_model,
                        provider_id=current_provider_id,
                        acting_user_email=ctx.session.user_email,
                    )
                except TypeError:
                    model_info = await self.providers.llm.get_model_info(current_model)
            else:
                try:
                    model_info = await self.providers.llm.get_model_info(
                        current_model,
                        acting_user_email=ctx.session.user_email,
                    )
                except TypeError:
                    model_info = await self.providers.llm.get_model_info(current_model)
            apply_runtime_tool_fallbacks = getattr(
                self.providers.llm,
                "apply_tool_exposure_runtime_fallbacks",
                None,
            )
            if callable(apply_runtime_tool_fallbacks):
                model_info = apply_runtime_tool_fallbacks(
                    model_info,
                    provider_id=current_provider_id,
                    model_id=current_model,
                )
            ctx.current_model = current_model
            ctx.current_provider_id = current_provider_id
            ctx.current_model_info = model_info
            registry = self._get_tool_registry(ctx)
            searchable_inventory_tools: list[ToolDefinition] = []
            default_visible_tool_ids: set[str] = set()
            resolved_profile = (
                self._step_profile_registry.resolve_step_profile(ctx.step_definition)
                if self._step_profile_registry is not None
                else resolve_step_profile(ctx.step_definition)
            )
            allow_tool_search = (
                resolved_profile.config.allow_tool_search
                if resolved_profile.config is not None
                else True
            )
            exposure_contract = ToolExposureContract(
                llm_api=LLMApiMode.CHAT_COMPLETIONS,
                discovery_mode=(
                    ToolDiscoveryMode.CONTROLLER_SEARCH
                    if allow_tool_search
                    else ToolDiscoveryMode.NONE
                ),
            )
            resolve_tool_exposure_contract = getattr(
                self.providers.llm,
                "resolve_tool_exposure_contract",
                None,
            )
            if callable(resolve_tool_exposure_contract):
                exposure_contract = await resolve_tool_exposure_contract(
                    model_id=current_model,
                    model_info=model_info,
                    provider_id=current_provider_id,
                    allow_tool_search=allow_tool_search,
                )
            if registry is not None:
                inventory_tools = registry.list_tools()
                inventory_fingerprint = _inventory_fingerprint(inventory_tools)
                cached_classified = (
                    self.session_cache.get_classified_inventory(
                        ctx.session.session_id, inventory_fingerprint
                    )
                    if hasattr(self.session_cache, "get_classified_inventory")
                    else None
                )
                if cached_classified is not None:
                    classified_inventory = cached_classified
                elif self._session_factory is not None:
                    classified_inventory = await resolve_tool_classifications(
                        inventory_tools,
                        session_factory=self._session_factory,
                        owner_email=ctx.agent.owner_email,
                        queue=self._tool_classification_queue,
                    )
                else:
                    classified_inventory = classify_tool_definitions_sync(inventory_tools)
                if cached_classified is None and hasattr(
                    self.session_cache, "set_classified_inventory"
                ):
                    self.session_cache.set_classified_inventory(
                        ctx.session.session_id,
                        inventory_fingerprint,
                        classified_inventory,
                    )
                ctx.classified_tool_definitions = {
                    stable_tool_id(tool): tool for tool in classified_inventory
                }
                full_inventory_tools = _filter_model_inventory_tools(
                    ctx.agent,
                    classified_inventory,
                    promoted_tool_ids,
                    activated_tool_ids,
                )
                searchable_inventory_tools = [
                    tool
                    for tool in full_inventory_tools
                    if step_profile_allows_tool(tool, resolved_profile)
                    or stable_tool_id(tool) in activated_tool_ids
                ]
                default_visible_tool_ids = {
                    stable_tool_id(tool)
                    for tool in searchable_inventory_tools
                    if step_profile_visible_by_default(tool, resolved_profile)
                    or stable_tool_id(tool) in activated_tool_ids
                }
            exposure = prepare_tool_exposure(
                inventory_tools=searchable_inventory_tools,
                controller_tool_schemas=controller_tool_schemas,
                model_info=model_info,
                contract=exposure_contract,
                promoted_tool_ids=promoted_tool_ids,
                default_visible_tool_ids=default_visible_tool_ids,
                allow_tool_search=allow_tool_search,
            )
            update_tool_runtime_info = getattr(
                self.session_cache,
                "update_tool_runtime_info",
                None,
            )
            tool_runtime_info = {
                **(ctx.runtime_info or {}),
                "resolved_model": current_model,
                "resolved_provider_id": current_provider_id,
                "reasoning_effort": reasoning_effort,
                "strategy": exposure.debug_metadata.get("strategy"),
                "step_profile_id": resolved_profile.profile_id,
                "step_profile_mode": (
                    str(resolved_profile.mode) if resolved_profile.mode is not None else None
                ),
                "allow_tool_search": allow_tool_search,
                "llm_api": str(exposure_contract.llm_api),
                "native_apply_patch_tool_type": exposure_contract.native_apply_patch_tool_type,
                "discovery_mode": (
                    str(ToolDiscoveryMode.CONTROLLER_SEARCH)
                    if any(
                        tool.get("function", {}).get("name") == SEARCH_TOOLS_TOOL.name
                        for tool in exposure.tools
                        if isinstance(tool, dict)
                    )
                    else str(ToolDiscoveryMode.NONE)
                ),
                "inventory_tool_count": exposure.debug_metadata.get("inventory_tool_count"),
                "visible_tool_count": exposure.debug_metadata.get("visible_tool_count"),
                "policy_visible_count": exposure.debug_metadata.get("policy_visible_count"),
                "hidden_searchable_count": exposure.debug_metadata.get("hidden_searchable_count"),
                "promoted_requested_count": exposure.debug_metadata.get("promoted_requested_count"),
                "promoted_visible_count": exposure.debug_metadata.get("promoted_visible_count"),
            }
            if callable(update_tool_runtime_info):
                update_tool_runtime_info(ctx.session.session_id, tool_runtime_info)
            if ctx.step_run_id and self._session_factory is not None:
                async with self._session_factory() as db_session:
                    await update_step_run(
                        db_session,
                        ctx.step_run_id,
                        runtime_info=tool_runtime_info,
                    )
                    await db_session.commit()
            logger.info(
                "tool exposure prepared",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **self._step_log_metadata(ctx),
                        "strategy": exposure.debug_metadata.get("strategy"),
                        "step_profile_id": resolved_profile.profile_id,
                        "step_profile_mode": (
                            str(resolved_profile.mode)
                            if resolved_profile.mode is not None
                            else None
                        ),
                        "inventory_tool_count": exposure.debug_metadata.get("inventory_tool_count"),
                        "visible_tool_count": exposure.debug_metadata.get("visible_tool_count"),
                        "policy_visible_count": exposure.debug_metadata.get("policy_visible_count"),
                        "hidden_searchable_count": exposure.debug_metadata.get(
                            "hidden_searchable_count"
                        ),
                        "promoted_requested_count": exposure.debug_metadata.get(
                            "promoted_requested_count"
                        ),
                        "promoted_visible_count": exposure.debug_metadata.get(
                            "promoted_visible_count"
                        ),
                        "activated_tool_count": len(activated_tool_ids),
                    }
                },
            )
            search_tools_visible = any(
                tool.get("function", {}).get("name") == SEARCH_TOOLS_TOOL.name
                for tool in exposure.tools
                if isinstance(tool, dict)
            )
            effective_discovery_mode = (
                ToolDiscoveryMode.CONTROLLER_SEARCH
                if search_tools_visible
                else ToolDiscoveryMode.NONE
            )
            if queued_discovery_guidance_mode != effective_discovery_mode:
                if effective_discovery_mode == ToolDiscoveryMode.CONTROLLER_SEARCH:
                    _queue_audit_message(
                        role="system",
                        source="tool_discovery_guidance",
                        content=(
                            "Additional tools may be available but hidden by the current step profile. "
                            "You MUST call search_tools when you need a capability not currently visible, "
                            "including Slack, Alertmanager, Mimir/Loki, skill_load, browser, filesystem, "
                            "shell, or other MCP/external-service tools."
                        ),
                    )
                else:
                    _queue_audit_message(
                        role="system",
                        source="tool_discovery_guidance",
                        content=(
                            "Only the currently visible tools are available for this turn. "
                            "Do not assume hidden tools can be searched or loaded."
                        ),
                    )
                queued_discovery_guidance_mode = effective_discovery_mode
            visible_tool_names: set[str] = set()
            for tool_schema in exposure.tools:
                if not isinstance(tool_schema, dict):
                    continue
                if tool_schema.get("type") == "apply_patch":
                    visible_tool_names.add("apply_patch")
                    continue
                function_schema = tool_schema.get("function")
                if not isinstance(function_schema, dict):
                    continue
                tool_name = function_schema.get("name")
                if isinstance(tool_name, str):
                    visible_tool_names.add(tool_name)
            edit_guidance = build_visible_edit_tool_guidance(
                visible_tool_names,
                model_id=current_model,
            )
            if edit_guidance is None and edit_guidance_message_index is not None:
                edit_guidance = (
                    "Turn-local edit guidance: no dedicated edit tools are currently visible. "
                    "Do not call `apply_patch`, `edit`, `multiedit`, or `write` unless one becomes "
                    "visible in a later turn."
                )
            if edit_guidance is not None and edit_guidance != queued_edit_guidance:
                if edit_guidance_message_index is None:
                    edit_guidance_message_index = len(messages)
                    messages.append({"role": "system", "content": edit_guidance})
                else:
                    messages[edit_guidance_message_index] = {
                        "role": "system",
                        "content": edit_guidance,
                    }
                _queue_audit_message(
                    role="developer",
                    source="edit_tool_guidance",
                    content=edit_guidance,
                )
                queued_edit_guidance = edit_guidance
            logger.debug(
                "Prepared tool exposure",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **exposure.debug_metadata,
                        "extra_header_keys": sorted(
                            (exposure.request_kwargs.get("extra_headers") or {}).keys()
                        ),
                    }
                },
            )
            if retry_projected_model is not None:
                projected_model = retry_projected_model
                retry_projected_model = None
            else:
                projected_model = self._project_model_messages_for_budget(
                    ctx,
                    messages=messages,
                    tool_schemas=exposure.tools,
                    resolved_model=current_model,
                    max_context_tokens=context_result.max_context_tokens,
                )
            model_messages = projected_model.messages
            if str(exposure_contract.llm_api).strip().lower() == "responses":
                model_messages = _reattach_responses_output_items(model_messages, messages)
            pre_call_snapshot = projected_model.snapshot
            self._store_context_usage_snapshot(
                ctx,
                snapshot=pre_call_snapshot,
                provider_id=current_provider_id,
            )
            self._maybe_log_context_reserve_clamp(
                ctx,
                pre_call_snapshot,
                provider_id=current_provider_id,
            )
            pre_call_projection_exceeded = self._projection_exceeded_selected_budget(
                projected_model
            )
            ctx.last_projection_snapshot = pre_call_snapshot
            ctx.last_projection_exceeded_selected_budget = (
                pre_call_projection_exceeded if pre_call_snapshot is not None else None
            )
            if pre_call_projection_exceeded:
                pressure_run = CompactionRunContext.from_snapshot(
                    pre_call_snapshot,
                    trigger="pre_model_pressure",
                    reason="projected_context_pressure",
                )
                notice = (
                    "Context window is critically full after projection; stopping before "
                    f"another model call. Usage is {pre_call_snapshot.prompt_tokens:,}/"
                    f"{pre_call_snapshot.available_prompt_tokens:,} prompt-budget tokens "
                    f"(threshold {pre_call_snapshot.threshold_prompt_tokens:,})."
                )
                logger.warning(
                    "Context pressure ceiling reached before model call",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            **self._step_log_metadata(ctx),
                            "cycle_index": agentic_step_count,
                            "provider_id": current_provider_id,
                            "model": current_model,
                            "projection_mode": projected_model.mode,
                            "prompt_tokens": pre_call_snapshot.prompt_tokens,
                            "available_prompt_tokens": pre_call_snapshot.available_prompt_tokens,
                            "threshold_prompt_tokens": pre_call_snapshot.threshold_prompt_tokens,
                            "reason": pre_call_snapshot.reason,
                        }
                    },
                )
                await self._emit_compaction_notice(
                    ctx,
                    notice,
                    on_token=on_token,
                    persist=False,
                    metadata=pressure_run.event_data(),
                )
                if ctx.policy.enable_auto_compaction:
                    pressure_run.phase = "pre_model"
                    compaction_result = await self._auto_compact(
                        ctx,
                        run=pressure_run,
                        on_token=on_token,
                        trigger="pre_model_pressure",
                        skip_few_events_check=True,
                    )
                    if compaction_result is not None and compaction_result.compacted:
                        new_session = await self._rotate_after_compaction(
                            ctx,
                            compaction_result,
                            trigger="pre_model_pressure",
                            run=pressure_run,
                        )
                        if new_session is not None:
                            ctx.session = new_session
                            ctx.is_retry = True
                            ctx.prior_context = None
                            ctx.projection_state = None
                            ctx.compaction_recursion_depth += 1
                            ctx.timeout_continuation_message = (
                                "Internal controller recovery: projected context pressure "
                                "remained critical before the model call, so Cognis compacted "
                                "the conversation into this fresh session. Continue from the "
                                "saved summary and recent history."
                            )
                            return await self._execute_step(
                                ctx,
                                on_token=on_token,
                                on_thinking=on_thinking,
                                on_tool_call=on_tool_call,
                                on_tool_result=on_tool_result,
                            )
                return StepOutput(
                    summary=(
                        "Stopped because projected context remained over the pressure ceiling "
                        "before the next model call."
                    ),
                    content="\n\n".join(assistant_content_parts),
                    outcome={
                        "status": "failed",
                        "reason": "Projected context pressure exceeded before model call.",
                    },
                    metadata={"context_pressure": pressure_run.event_data()},
                    attachments=list(collected_attachments),
                )
            llm_kwargs.update(exposure.request_kwargs)
            (
                llm_stream_idle_timeout_seconds,
                llm_stream_max_retries,
            ) = await self._resolve_llm_stream_idle_config(
                provider_id=current_provider_id,
                model_id=current_model,
            )
            prep_duration_seconds = monotonic() - cycle_started_at
            llm_request_id = f"llmr_{uuid.uuid4().hex[:12]}"
            AGENT_CYCLE_PREP_DURATION.labels(
                provider_id=current_provider_id or "default",
                model=current_model or "unknown",
                step_type=cycle_step_type,
            ).observe(prep_duration_seconds)
            logger.info(
                "agent: cycle prepared",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        **self._step_log_metadata(ctx),
                        "cycle_index": agentic_step_count,
                        "llm_request_id": llm_request_id,
                        "provider_id": current_provider_id,
                        "model": current_model,
                        "duration_seconds": round(prep_duration_seconds, 3),
                        "projection_mode": projected_model.mode,
                        "llm_api": str(exposure_contract.llm_api),
                        "prompt_tokens": (
                            pre_call_snapshot.prompt_tokens if pre_call_snapshot else None
                        ),
                        "visible_tool_count": exposure.debug_metadata.get("visible_tool_count"),
                    }
                },
            )

            # Stream LLM response
            accumulator = StreamAccumulator(block_id_prefix=llm_request_id)
            if mid_stream_retries > 0:
                accumulator.restore_tool_call_state(saved_partial_tool_calls)
            mid_stream_error: str | None = None
            mid_stream_error_details: dict[str, Any] | None = None
            llm_stream_max_retries_for_error = llm_stream_max_retries
            # Multiple assistant_message segments can be produced within a single turn
            # (for example after tool calls or reprompts). The live WebSocket stream
            # reuses one message_id for the whole turn, so inject a paragraph break
            # before the first token of each later visible segment.
            needs_stream_separator = (
                bool(assistant_content_parts) and not continued_assistant_content
            )
            await self._record_outgoing_audit_messages(
                ctx,
                pending_audit_messages,
                on_token=on_token,
            )
            stream_activity_seen = False
            stream_idle_stats = LLMStreamIdleStats()
            llm_started_at = monotonic()
            llm_status = "success"
            logger.debug(
                "agent: LLM stream attempt started",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "llm_request_id": llm_request_id,
                        "provider_id": current_provider_id,
                        "model": current_model,
                        "attempt": mid_stream_retries + 1,
                        "idle_timeout_seconds": llm_stream_idle_timeout_seconds,
                        "max_retries": llm_stream_max_retries,
                    }
                },
            )
            try:
                # In force-summary mode (delegation budget exhausted) strip all
                # executor tools so the model can only write a text response.
                _effective_tools = [] if _force_summary_mode else exposure.tools
                stream = self.providers.llm.stream_generate(
                    model_messages,
                    model=model_for_llm,
                    task_type="default",
                    provider_id=provider_for_llm,
                    acting_user_email=ctx.session.user_email,
                    cognis_llm_request_id=llm_request_id,
                    cognis_session_id=ctx.session.session_id,
                    tools=_effective_tools,
                    cache_breakpoint_index=cache_breakpoint,
                    cognis_openai_apply_patch_tool_type=exposure_contract.native_apply_patch_tool_type,
                    **llm_kwargs,
                )
                async for chunk in _iterate_llm_stream_with_idle_timeout(
                    stream,
                    idle_timeout_seconds=llm_stream_idle_timeout_seconds,
                    stats=stream_idle_stats,
                    cancel_event=ctx.cancel_event,
                ):
                    if not stream_activity_seen and _llm_stream_chunk_has_activity(chunk):
                        stream_activity_seen = True
                        logger.debug(
                            "agent: LLM stream first activity received",
                            extra={
                                "extra_data": {
                                    "session_id": ctx.session.session_id,
                                    "llm_request_id": llm_request_id,
                                    "provider_id": current_provider_id,
                                    "model": current_model,
                                    "attempt": mid_stream_retries + 1,
                                }
                            },
                        )
                    if chunk.get("mid_stream_failure"):
                        mid_stream_error = chunk.get("error", "LLM stream failed mid-generation")
                        details = chunk.get("response_error")
                        if isinstance(details, dict):
                            mid_stream_error_details = details
                        LLM_MID_STREAM_ERRORS_TOTAL.labels(
                            provider_id=current_provider_id or "default",
                            model=current_model or "unknown",
                            category=str(
                                (mid_stream_error_details or {}).get("category") or "other"
                            ),
                        ).inc()
                        break
                    text_delta = accumulator.feed(chunk)
                    if text_delta and on_token:
                        if needs_stream_separator:
                            await on_token("\n\n")
                            needs_stream_separator = False
                            await on_token(text_delta)
                        else:
                            await on_token(text_delta)
                    # Drain thinking events accumulated during this chunk
                    if on_thinking:
                        for thinking_evt in accumulator.pop_thinking_events():
                            await on_thinking(
                                thinking_evt.block_id,
                                thinking_evt.delta,
                                thinking_evt.title,
                                thinking_evt.complete,
                                thinking_evt.content,
                                thinking_evt.started_at,
                                thinking_evt.completed_at,
                                thinking_evt.duration_ms,
                                thinking_evt.source,
                                thinking_evt.provider_block_index,
                            )
                    if ctx.on_tool_progress is not None:
                        for progress_evt in accumulator.pop_tool_progress_events():
                            await ctx.on_tool_progress(
                                progress_evt.call_id,
                                progress_evt.tool_name,
                                {
                                    "phase": progress_evt.phase,
                                    "input_chars": progress_evt.input_chars,
                                    "input_lines": progress_evt.input_lines,
                                    "complete": progress_evt.complete,
                                },
                            )
            except OpenAIToolSearchFallbackRequired as exc:
                if openai_tool_search_retries >= _MAX_OPENAI_TOOL_SEARCH_RETRIES:
                    raise
                openai_tool_search_retries += 1
                logger.warning(
                    "agent: native OpenAI Responses tool search failed; retrying with cached controller fallback",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "llm_request_id": llm_request_id,
                            "provider_id": exc.provider_id,
                            "model": exc.model_id,
                            "reason": exc.reason,
                        }
                    },
                )
                continue
            except LLMStreamIdleTimeout as exc:
                llm_status = "idle_timeout"
                mid_stream_error = str(exc)
                mid_stream_error_details = exc.to_payload()
                timeout_phase = stream_idle_stats.timeout_phase
                LLM_MID_STREAM_ERRORS_TOTAL.labels(
                    provider_id=current_provider_id or "default",
                    model=current_model or "unknown",
                    category=str(mid_stream_error_details.get("category") or "idle_timeout"),
                ).inc()
                logger.warning(
                    "agent: LLM stream idle timeout",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "llm_request_id": llm_request_id,
                            "provider_id": current_provider_id,
                            "model": current_model,
                            "attempt": mid_stream_retries + 1,
                            "idle_timeout_seconds": llm_stream_idle_timeout_seconds,
                            "timeout_phase": timeout_phase,
                            "stream_idle_stats": stream_idle_stats.as_dict(),
                        }
                    },
                )
            except LLMStreamProviderError as exc:
                llm_status = "provider_error"
                mid_stream_error = f"{type(exc).__name__}: {exc}"
                mid_stream_error_details = exc.to_payload()
                LLM_MID_STREAM_ERRORS_TOTAL.labels(
                    provider_id=current_provider_id or "default",
                    model=current_model or "unknown",
                    category=str(mid_stream_error_details.get("category") or "other"),
                ).inc()
                logger.warning(
                    "agent: LLM stream failed",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "provider_id": current_provider_id,
                            "model": current_model,
                            "attempt": mid_stream_retries + 1,
                            "error": mid_stream_error[:200],
                        }
                    },
                    exc_info=True,
                )
            except LLMContextOverflowError as exc:
                llm_status = "context_overflow"
                step_output = await self._recover_from_context_overflow(
                    ctx,
                    context_result=context_result,
                    provider_id=exc.provider_id or current_provider_id,
                    model_id=exc.model_id or current_model,
                    reason=exc.reason,
                    error_type=type(exc).__name__,
                    assistant_content_parts=assistant_content_parts,
                    collected_attachments=collected_attachments,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                break
            except Exception as exc:
                if not is_context_overflow_error(exc):
                    raise
                llm_status = "context_overflow"
                step_output = await self._recover_from_context_overflow(
                    ctx,
                    context_result=context_result,
                    provider_id=current_provider_id,
                    model_id=current_model,
                    reason="context_overflow",
                    error_type=type(exc).__name__,
                    assistant_content_parts=assistant_content_parts,
                    collected_attachments=collected_attachments,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                break

            llm_finished_at = monotonic()
            last_cycle_end_at = llm_finished_at
            llm_duration_seconds = llm_finished_at - llm_started_at
            AGENT_CYCLE_LLM_DURATION.labels(
                provider_id=current_provider_id or "default",
                model=current_model or "unknown",
                status=llm_status,
                step_type=cycle_step_type,
            ).observe(llm_duration_seconds)
            logger.info(
                "agent: cycle llm completed",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        **self._step_log_metadata(ctx),
                        "cycle_index": agentic_step_count,
                        "llm_request_id": llm_request_id,
                        "provider_id": current_provider_id,
                        "model": current_model,
                        "status": llm_status,
                        "duration_seconds": round(llm_duration_seconds, 3),
                        "stream_idle_stats": stream_idle_stats.as_dict(),
                    }
                },
            )

            if mid_stream_error:
                transcript_mutated_for_retry = False
                artifact_fetch_failures = _artifact_failures_from_error_payload(
                    mid_stream_error_details
                ) or _artifact_failures_from_provider_fetch_error(mid_stream_error)
                if artifact_fetch_failures:
                    transcript_mutated_for_retry = True
                    artifact_fetch_failure_urls = {
                        failure.url for failure in artifact_fetch_failures if failure.url
                    }
                    artifact_fetch_failure_ids = {
                        failure.artifact_id
                        for failure in artifact_fetch_failures
                        if failure.artifact_id
                    }
                    disabled_urls = ctx.runtime_info.setdefault("disabled_artifact_urls", [])
                    if isinstance(disabled_urls, list):
                        for url in sorted(artifact_fetch_failure_urls):
                            if url not in disabled_urls:
                                disabled_urls.append(url)
                    disabled_ids = ctx.runtime_info.setdefault("disabled_artifact_ids", [])
                    if isinstance(disabled_ids, list):
                        for artifact_id in sorted(artifact_fetch_failure_ids):
                            if artifact_id not in disabled_ids:
                                disabled_ids.append(artifact_id)
                    notice = _artifact_fetch_failure_notice(artifact_fetch_failures)
                    if notice:
                        disabled_notices = ctx.runtime_info.setdefault(
                            "disabled_artifact_notices", []
                        )
                        if isinstance(disabled_notices, list) and notice not in disabled_notices:
                            disabled_notices.append(notice)
                    _strip_disabled_artifact_urls_from_messages(
                        messages, artifact_fetch_failure_urls
                    )
                    notice = _artifact_fetch_failure_notice(artifact_fetch_failures)
                    if notice:
                        messages.append({"role": "system", "content": notice})
                error_category = _mid_stream_reason_class(mid_stream_error_details, "other")
                if error_category in _RECOVERY_NON_RETRYABLE_CATEGORIES:
                    llm_stream_max_retries_for_error = 0
                if mid_stream_retries < llm_stream_max_retries_for_error:
                    retry_projected_model = (
                        None if transcript_mutated_for_retry else projected_model
                    )
                    saved_partial_tool_calls = accumulator.clone_tool_call_state()
                    mid_stream_retries += 1
                    reason_class = _mid_stream_reason_class(mid_stream_error_details, "other")
                    retry_after_seconds = _mid_stream_retry_after_seconds(mid_stream_error_details)
                    logger.warning(
                        "agent: mid-stream failure, retrying LLM call (%d/%d)",
                        mid_stream_retries,
                        llm_stream_max_retries_for_error,
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "provider_id": current_provider_id,
                                "model": current_model,
                                "error": mid_stream_error[:200],
                                "response_error": mid_stream_error_details,
                                "idle_timeout_seconds": llm_stream_idle_timeout_seconds,
                                "configured_max_retries": llm_stream_max_retries,
                                "effective_max_retries": llm_stream_max_retries_for_error,
                                "reason_class": reason_class,
                                "reused_projected_messages": retry_projected_model is not None,
                            }
                        },
                    )
                    retry_policy = RetryPolicy(
                        "mid_stream",
                        max_retries=llm_stream_max_retries_for_error,
                        base_delay=DEFAULT_MID_STREAM_RETRY_POLICY.base_delay,
                        max_delay=DEFAULT_MID_STREAM_RETRY_POLICY.max_delay,
                        jitter=DEFAULT_MID_STREAM_RETRY_POLICY.jitter,
                    )
                    delay = (
                        retry_after_seconds
                        if retry_after_seconds is not None
                        else compute_retry_delay(retry_policy, mid_stream_retries - 1)
                    )
                    if _should_emit_mid_stream_retry_notice(mid_stream_error_details):
                        try:
                            await self._emit_recovery_notice(
                                ctx,
                                _mid_stream_retry_notice(
                                    provider_id=current_provider_id,
                                    model=current_model,
                                    details=mid_stream_error_details,
                                    error=mid_stream_error,
                                    delay_seconds=delay,
                                    attempt=mid_stream_retries,
                                    max_attempts=llm_stream_max_retries_for_error,
                                ),
                                on_token=on_token,
                                persist=False,
                                metadata={
                                    "kind": "model_recovery",
                                    "scope": "retry",
                                    "provider_id": current_provider_id,
                                    "model": current_model,
                                    "reason_class": reason_class,
                                    "retry_after_seconds": delay,
                                    "attempt": mid_stream_retries,
                                    "max_attempts": llm_stream_max_retries_for_error,
                                    "recoverable": True,
                                },
                                record_reason="mid_stream_retry_notice",
                            )
                        except Exception:
                            logger.warning(
                                "agent: failed to emit mid-stream retry notice",
                                extra={
                                    "extra_data": {
                                        "session_id": ctx.session.session_id,
                                        "provider_id": current_provider_id,
                                        "model": current_model,
                                        "reason_class": reason_class,
                                    }
                                },
                                exc_info=True,
                            )
                    await asyncio.sleep(delay)
                    continue  # retry — keep partial tool-call state, drop partial text

                # Retries exhausted — do not record partial assistant text.
                # Partial free text pollutes history more than it helps.
                partial_content = accumulator.get_content()
                if partial_content:
                    events_to_record.append(
                        SessionEvent(
                            type="lifecycle",
                            data={
                                "event": "assistant_message_aborted",
                                "partial_length": len(partial_content),
                            },
                        )
                    )

                logger.warning(
                    "agent: mid-stream failure after retries exhausted",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "llm_request_id": llm_request_id,
                            "provider_id": current_provider_id,
                            "model": current_model,
                            "error": mid_stream_error[:200],
                            "response_error": mid_stream_error_details,
                            "idle_timeout_seconds": llm_stream_idle_timeout_seconds,
                            "configured_max_retries": llm_stream_max_retries,
                            "effective_max_retries": llm_stream_max_retries_for_error,
                        }
                    },
                )
                is_idle_timeout_failure = _is_llm_idle_timeout_error(mid_stream_error)
                continuation_count = (
                    idle_timeout_continuation_count
                    if is_idle_timeout_failure
                    else model_error_continuation_count
                )
                max_continuations = (
                    max_idle_timeout_continuations
                    if is_idle_timeout_failure
                    else max_model_error_continuations
                )
                if (
                    _should_continue_after_exhausted_mid_stream_failure(
                        mid_stream_error,
                        mid_stream_error_details,
                    )
                    and continuation_count < max_continuations
                ):
                    if is_idle_timeout_failure:
                        idle_timeout_continuation_count += 1
                        continuation_count = idle_timeout_continuation_count
                    else:
                        model_error_continuation_count += 1
                        continuation_count = model_error_continuation_count
                    reason_class = _mid_stream_reason_class(mid_stream_error_details, "other")
                    continuation_notice = (
                        "Model stream failed after saved work. Cognis preserved the work "
                        "and is continuing from the saved state."
                    )
                    if events_to_record:
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="mid_stream_error_pre_continuation",
                            on_token=on_token,
                        )
                    try:
                        await self._emit_recovery_notice(
                            ctx,
                            continuation_notice,
                            on_token=on_token,
                            persist=True,
                            metadata={
                                "kind": "model_recovery",
                                "scope": "continuation",
                                "notice_id": (
                                    f"{ctx.session.session_id}:{ctx.turn_id or 'none'}:"
                                    "model_recovery:continuation"
                                ),
                                "attempt": continuation_count,
                                "max_attempts": max_continuations,
                                "provider_id": current_provider_id,
                                "model": current_model,
                                "reason_class": reason_class,
                                "tool_results_saved": True,
                                "recoverable": True,
                            },
                            record_reason="mid_stream_error_continuation",
                        )
                    except Exception:
                        logger.warning(
                            "agent: failed to persist recovery notice; continuing from saved state",
                            extra={
                                "extra_data": {
                                    "session_id": ctx.session.session_id,
                                    "turn_id": ctx.turn_id,
                                    "provider_id": current_provider_id,
                                    "model": current_model,
                                    "reason_class": reason_class,
                                }
                            },
                            exc_info=True,
                        )
                    continuation_prompt = (
                        "Internal controller recovery: the previous model stream failed after "
                        "the configured retry budget. The turn is being continued automatically. "
                        "Use the session history and saved tool results above; do not restart work "
                        "or repeat already completed tool calls unless required information is "
                        "missing. Continue from the current state and finish with a normal assistant response."
                    )
                    continuation_reminder_index = len(messages)
                    messages.append({"role": "system", "content": continuation_prompt})
                    pending_audit_messages = []
                    _queue_audit_message(
                        role="developer",
                        source="model_error_continuation",
                        content=continuation_prompt,
                    )
                    mid_stream_retries = 0
                    saved_partial_tool_calls = None
                    continue
                if _is_llm_idle_timeout_error(mid_stream_error):
                    error_notice = (
                        "Turn failed: the model did not produce output for "
                        f"{llm_stream_idle_timeout_seconds} seconds after "
                        f"{llm_stream_max_retries_for_error + 1} attempt(s). Your tool results have "
                        "been saved. Please try sending your message again."
                    )
                else:
                    error_notice = (
                        "A model error occurred while generating the response. "
                        "Your tool results have been saved. Please try sending your message again."
                    )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={"event": "system_notice", "message": error_notice},
                    )
                )
                if events_to_record:
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="mid_stream_failure",
                        on_token=on_token,
                    )
                break  # Exit while loop → _finalize_step runs normally

            finish_reason = accumulator.finish_reason
            saved_partial_tool_calls = None
            mid_stream_retries = 0
            if hasattr(self.session_cache, "update_last_llm_usage"):
                self.session_cache.update_last_llm_usage(
                    ctx.session.session_id,
                    accumulator.usage,
                )

            # ---------------------------------------------------------------
            # Finalize thinking blocks and drain any remaining events
            # ---------------------------------------------------------------
            completed_thinking_blocks = accumulator.finalize_thinking()
            # Drain any remaining thinking events (e.g. the final close event)
            if on_thinking:
                for thinking_evt in accumulator.pop_thinking_events():
                    await on_thinking(
                        thinking_evt.block_id,
                        thinking_evt.delta,
                        thinking_evt.title,
                        thinking_evt.complete,
                        thinking_evt.content,
                        thinking_evt.started_at,
                        thinking_evt.completed_at,
                        thinking_evt.duration_ms,
                        thinking_evt.source,
                        thinking_evt.provider_block_index,
                    )
            # Record completed thinking blocks to Intaris
            if completed_thinking_blocks and mid_stream_error is None:
                for block in completed_thinking_blocks:
                    if block.content_parts:  # skip empty blocks
                        events_to_record.append(
                            SessionEvent(
                                type="assistant_thinking",
                                data={
                                    "message_id": ctx.turn_id,
                                    "block_id": block.block_id,
                                    "title": block.get_title(),
                                    "content": block.get_content(),
                                    "reasoning_source": block.source,
                                    "provider_block_index": block.provider_block_index,
                                    "started_at": block.started_at.isoformat(),
                                    "completed_at": block.completed_at.isoformat()
                                    if block.completed_at
                                    else None,
                                    "duration_ms": block.duration_ms,
                                    "turn_id": ctx.turn_id,
                                },
                            )
                        )

            content = continued_assistant_content + accumulator.get_content()
            raw_tool_calls = accumulator.get_tool_calls()
            responses_output_items = accumulator.get_responses_output_items()
            tool_parse_failures = [
                item for item in raw_tool_calls if isinstance(item, ToolArgumentParseFailure)
            ]
            tool_calls = [item for item in raw_tool_calls if isinstance(item, ToolCall)]
            if tool_parse_failures:
                failed_tool_calls: list[tuple[ToolArgumentParseFailure, ToolCall, str]] = []
                for failure in tool_parse_failures:
                    failed_tc = ToolCall(
                        call_id=failure.call_id,
                        name=failure.name or "unknown_tool",
                        arguments=_tool_argument_failure_arguments(failure),
                    )
                    failed_tool_calls.append(
                        (failure, failed_tc, _tool_id_for_call(failed_tc.name, registry))
                    )

                assistant_tool_message = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": failed_tc.call_id,
                            "type": "function",
                            "function": {
                                "name": failed_tc.name,
                                "arguments": json.dumps(failed_tc.arguments),
                            },
                        }
                        for _failure, failed_tc, _tool_id in failed_tool_calls
                    ],
                }
                if content:
                    events_to_record.append(
                        SessionEvent(
                            type="assistant_message",
                            data={
                                "content": content,
                                "turn_id": ctx.turn_id,
                                "attachments": strip_attachment_payload_bytes(
                                    pending_assistant_attachments
                                ),
                            },
                        )
                    )
                    assistant_content_parts.append(content)
                    last_assistant_content = content
                    continued_assistant_content = ""

                if continuation_message_index is not None:
                    messages[continuation_message_index] = assistant_tool_message
                    continuation_message_index = None
                else:
                    messages.append(assistant_tool_message)

                for failure, failed_tc, tool_id in failed_tool_calls:
                    LLM_TOOL_ARGUMENT_PARSE_FAILURES_TOTAL.labels(
                        provider_id=current_provider_id or "default",
                        tool=failure.name,
                    ).inc()
                    payload = json.dumps(
                        _tool_argument_failure_payload(failure),
                        separators=(",", ":"),
                    )
                    if on_tool_call:
                        await on_tool_call(
                            failed_tc.name,
                            failed_tc.call_id,
                            _parent_visible_tool_arguments(failed_tc.name, failed_tc.arguments),
                        )
                    _append_tool_call_event(events_to_record, failed_tc, tool_id)
                    messages.append(
                        _tool_result_message(
                            failed_tc,
                            payload,
                            protected=True,
                            is_error=True,
                        )
                    )
                    _append_tool_result_event(
                        events_to_record,
                        failed_tc,
                        payload,
                        True,
                        tool_id=tool_id,
                        protect_from_pruning=True,
                    )
                    if on_tool_result:
                        await on_tool_result(
                            failed_tc.call_id,
                            failed_tc.name,
                            payload,
                            True,
                            None,
                            None,
                        )
                    events_to_record.append(
                        SessionEvent(
                            type="lifecycle",
                            data={
                                "event": "tool_argument_parse_failure",
                                "tool_name": failure.name,
                                "call_id": failure.call_id,
                                "reason": _tool_argument_failure_reason(failure),
                                "argument_length": failure.argument_length
                                if failure.argument_length is not None
                                else len(failure.raw),
                                "raw_preview_chars": len(failure.raw),
                                "recovery_attempts": list(failure.recovery_attempts),
                            },
                        )
                    )
                continue
            if continuation_reminder_index is not None and continuation_reminder_index < len(
                messages
            ):
                reminder = messages[continuation_reminder_index]
                if reminder.get("role") == "system":
                    messages.pop(continuation_reminder_index)
                continuation_reminder_index = None
            for _tc_index, tc in enumerate(tool_calls):
                mapped_name = exposure.alias_map.get(tc.name, tc.name)
                if mapped_name != tc.name:
                    logger.debug(
                        "Resolved tool alias",
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "visible_name": tc.name,
                                "internal_name": mapped_name,
                            }
                        },
                    )
                    tc.name = mapped_name
                argument_alias_tree = exposure.argument_alias_map.get(tc.name)
                if argument_alias_tree:
                    tc.arguments = reverse_tool_argument_aliases(
                        tc.arguments,
                        argument_alias_tree,
                    )

            if finish_reason == "content_filter":
                error_notice = (
                    "The model response was blocked by the provider content filter. "
                    "Please revise the request or continue with a narrower follow-up."
                )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={"event": "system_notice", "message": error_notice},
                    )
                )
                await self._flush_events_incremental(
                    ctx,
                    events_to_record,
                    reason="content_filter",
                    on_token=on_token,
                )
                break

            if finish_reason == "length" and not tool_calls:
                if continuation_message_index is None:
                    continuation_message_index = len(messages)
                    messages.append({"role": "assistant", "content": content})
                else:
                    messages[continuation_message_index] = {"role": "assistant", "content": content}
                continued_assistant_content = content
                continuation_reminder_index = len(messages)
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Internal controller reminder — the previous response hit the output "
                            "limit. Continue exactly from where you left off. Do not repeat or "
                            "restart prior text."
                        ),
                    }
                )
                pending_audit_messages = []
                _queue_audit_message(
                    role="developer",
                    source="follow_up_boundary",
                    content=str(messages[continuation_reminder_index]["content"]),
                )
                continue

            current_assistant_message_index: int | None = None

            # Record assistant message.
            # turn_id is included so that multiple assistant_message events within
            # the same turn are merged into one bubble during history replay
            # (matching the live-WS experience driven by message_complete).
            if content or pending_assistant_attachments:
                events_to_record.append(
                    SessionEvent(
                        type="assistant_message",
                        data={
                            "content": content,
                            "turn_id": ctx.turn_id,
                            "attachments": strip_attachment_payload_bytes(
                                pending_assistant_attachments
                            ),
                        },
                    )
                )
                if content:
                    if continuation_message_index is not None:
                        current_assistant_message_index = continuation_message_index
                        messages[continuation_message_index] = {
                            "role": "assistant",
                            "content": content,
                        }
                    else:
                        current_assistant_message_index = len(messages)
                        messages.append({"role": "assistant", "content": content})
                    assistant_content_parts.append(content)
                    last_assistant_content = content
                    continued_assistant_content = ""
                    continuation_message_index = None
                memory_text = merge_content_and_attachment_note(
                    content,
                    strip_attachment_payload_bytes(pending_assistant_attachments),
                )
                if memory_text.strip():
                    assistant_memory_parts.append(memory_text)
                await self._flush_events_incremental(
                    ctx,
                    events_to_record,
                    reason="assistant_message",
                    on_token=on_token,
                )
                pending_assistant_attachments.clear()

            # No tool calls — check if step is complete
            if not tool_calls:
                if await self._consume_boundary_batch_if_available(
                    ctx,
                    messages=messages,
                    pending_audit_messages=pending_audit_messages,
                    reason="after_assistant_message",
                    on_token=on_token,
                ):
                    continue
                if not ctx.policy.require_step_complete:
                    # Direct workflow (main chat): check for incomplete todos
                    incomplete_todos = self._get_incomplete_todos(ctx)
                    if (
                        incomplete_todos
                        and not (_is_delegation and _force_summary_mode)
                        and todo_reprompt_count < _MAX_TODO_REPROMPTS
                    ):
                        todo_reprompt_count += 1
                        STEP_REPROMPTS.inc()
                        todo_list = "\n".join(
                            f"  - [{t.get('status', 'pending')}] {t.get('content', '')}"
                            for t in incomplete_todos
                        )
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Internal controller reminder — this is not a new user "
                                    "message. Do not write a filler acknowledgment just for "
                                    "this reminder.\n\n"
                                    f"You have {len(incomplete_todos)} non-terminal todos:\n"
                                    f"{todo_list}\n\n"
                                    "Every todo must be in a terminal state before the "
                                    "turn ends — either 'completed' (work is done) or "
                                    "'cancelled' (work is no longer relevant). First "
                                    "decide whether work actually remains, or whether "
                                    "only your todo state is stale. If work remains, "
                                    "continue it, ask for input if needed, and only "
                                    "produce assistant text if you have new user-visible "
                                    "information, a required question, or a correction. "
                                    "If only todo cleanup remains, update each todo via "
                                    "step_todo_write to 'completed' or 'cancelled' and "
                                    "produce no assistant text. Do not repeat, restate, "
                                    "or paraphrase content that has already been sent "
                                    "to the user."
                                ),
                            }
                        )
                        _queue_audit_message(
                            role="developer",
                            source="tool_reminder",
                            content=str(messages[-1]["content"]),
                        )
                        todo_cleanup_only_allowed = True
                        continue
                    if incomplete_todos and _is_delegation:
                        await self._cancel_incomplete_delegation_todos(
                            ctx,
                            events_to_record,
                            reason=(
                                "max_steps_reached"
                                if _force_summary_mode
                                else "delegation_completed"
                            ),
                        )
                    visible_completion_content = merge_content_and_attachment_note(
                        content,
                        strip_attachment_payload_bytes(collected_attachments),
                    )
                    current_deliverable: Deliverable | None = None
                    if (
                        ctx.policy.step_complete_available
                        and self._deliverable_owner_step_run_id(ctx) is not None
                    ):
                        current_deliverable = await self._get_current_deliverable(ctx)

                    if current_deliverable is not None:
                        summary = visible_completion_content.strip()
                        if not summary:
                            summary = (
                                current_deliverable.title.strip()
                                if isinstance(current_deliverable.title, str)
                                and current_deliverable.title.strip()
                                else compact_snippet(
                                    current_deliverable.content.strip(),
                                    max_chars=500,
                                )
                            )
                        if not summary:
                            summary = "Step completed with delegated deliverable."
                        step_output = StepOutput(
                            summary=summary[:500],
                            content=current_deliverable.content,
                            outputs=(
                                dict(current_deliverable.outputs)
                                if isinstance(current_deliverable.outputs, dict)
                                else {}
                            ),
                            claims=[],
                            deliverable_id=current_deliverable.deliverable_id,
                            deliverable_version=current_deliverable.version,
                            deliverable_format=current_deliverable.format,
                            deliverable_title=current_deliverable.title,
                            attachments=list(collected_attachments),
                            session_id=ctx.session.session_id,
                            intaris_session_id=ctx.session.intaris_session_id
                            or ctx.session.session_id,
                            completed_at=datetime.now(UTC),
                        )
                        break

                    summary = visible_completion_content.strip()
                    if not summary:
                        if todo_cleanup_only_allowed and not self._get_incomplete_todos(ctx):
                            step_output = StepOutput(
                                summary="Todo cleanup completed",
                                content="",
                                outputs={},
                                claims=[],
                                attachments=list(collected_attachments),
                                session_id=ctx.session.session_id,
                                intaris_session_id=ctx.session.intaris_session_id
                                or ctx.session.session_id,
                                completed_at=datetime.now(UTC),
                            )
                            break
                        if (
                            empty_direct_response_reprompt_count
                            < _MAX_EMPTY_DIRECT_RESPONSE_REPROMPTS
                        ):
                            empty_direct_response_reprompt_count += 1
                            STEP_REPROMPTS.inc()
                            messages.append(
                                {
                                    "role": "system",
                                    "content": (
                                        "Internal controller reminder — this is not a new user "
                                        "message. Do not write a filler acknowledgment just for "
                                        "this reminder. Your previous response was empty. Reply "
                                        "with the actual user-facing result, ask a necessary "
                                        "question, or call the needed tools. Do not send an empty "
                                        "message."
                                    ),
                                }
                            )
                            _queue_audit_message(
                                role="developer",
                                source="tool_reminder",
                                content=str(messages[-1]["content"]),
                            )
                            continue
                        error_msg = "Model returned an empty response without tool calls."
                        events_to_record.append(
                            SessionEvent(
                                type="lifecycle",
                                data={
                                    "event": "system_notice",
                                    "message": f"Step failed: {error_msg}",
                                },
                            )
                        )
                        step_output = StepOutput(
                            summary="Step failed: empty assistant response",
                            content="",
                            outputs={},
                            claims=[],
                            error=error_msg,
                            attachments=list(collected_attachments),
                            session_id=ctx.session.session_id,
                            intaris_session_id=ctx.session.intaris_session_id
                            or ctx.session.session_id,
                            completed_at=datetime.now(UTC),
                        )
                        break
                    # Todos done (or max re-prompts reached) — complete
                    step_output = StepOutput(
                        summary=summary[:500],
                        content=content,
                        outputs={},
                        claims=[],
                        attachments=list(collected_attachments),
                        session_id=ctx.session.session_id,
                        intaris_session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                        completed_at=datetime.now(UTC),
                    )
                    break
                elif step_reprompt_count < _MAX_STEP_COMPLETE_REPROMPTS:
                    # Non-direct (sub-session / workflow step): require step_complete
                    STEP_REPROMPTS.inc()
                    step_reprompt_count += 1
                    reminder = (
                        "Internal controller reminder — this is not a new user message. "
                        "Do not write a filler acknowledgment just for this reminder. "
                    )
                    if self._deliverable_owner_step_run_id(ctx) is not None:
                        reminder += (
                            "If the step is finished, ensure you have called write_deliverable "
                            "for the final artifact and then call step_complete. "
                        )
                    else:
                        reminder += (
                            "If the step is finished, write the final result as a normal assistant "
                            "message and then call step_complete. "
                        )
                    reminder += "Otherwise continue the work until it is actually complete. Do not repeat prior text unnecessarily."
                    messages.append(
                        {
                            "role": "system",
                            "content": reminder,
                        }
                    )
                    _queue_audit_message(
                        role="developer",
                        source="tool_reminder",
                        content=str(messages[-1]["content"]),
                    )
                    continue
                else:
                    # Failed to call step_complete after re-prompt
                    step_output = None
                    break

            # Process tool calls
            if tool_calls and content:
                # Add assistant message with tool calls for chat history
                target_index = current_assistant_message_index
                if target_index is None:
                    target_index = len(messages)
                    messages.append({"role": "assistant", "content": content})
                messages[target_index] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in tool_calls
                    ],
                }
                if responses_output_items:
                    messages[target_index]["_responses_output_items"] = responses_output_items
            elif tool_calls:
                assistant_tool_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                if responses_output_items:
                    assistant_tool_message["_responses_output_items"] = responses_output_items
                messages.append(assistant_tool_message)

            async_orchestration_spawned = False
            restart_llm_cycle = False
            prepared_regular_batch: list[_PreparedRegularToolCall] = []
            post_tool_system_messages: list[dict[str, Any]] = []
            loaded_project_contexts_this_cycle: dict[
                str, ProjectContextEntry | ProjectMetadataEntry
            ] = {}
            queued_project_context_hashes: set[str] = set()
            # Pre-compute parallel delegate batches so that multiple
            # delegate calls in one assistant turn fan out via
            # asyncio.gather. ``parallel_delegate_results[index]`` holds
            # the precomputed ToolResult for indices that were absorbed
            # into a batch by an earlier index in the same run.
            parallel_delegate_results = await self._precompute_parallel_delegate_batches(
                ctx,
                tool_calls,
                events_to_record=events_to_record,
                on_token=on_token,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
            for tc_index, tc in enumerate(tool_calls):
                self._raise_if_cancelled(ctx)
                tool_id = _tool_id_for_call(tc.name, registry)
                STEP_TOOL_CALLS.labels(tool_name=tool_id).inc()

                if on_tool_call:
                    await on_tool_call(
                        tc.name,
                        tc.call_id,
                        _parent_visible_tool_arguments(tc.name, tc.arguments),
                    )

                finalization_instruction = await self._finalization_instruction(ctx)
                allowed_finalization_tools = (
                    _allowed_finalization_tools(finalization_instruction)
                    if finalization_instruction is not None
                    else _FINALIZATION_TOOLS
                )
                if (
                    finalization_instruction is not None
                    and tc.name not in allowed_finalization_tools
                ):
                    tool_call_count += 1
                    if prepared_regular_batch:
                        await self._execute_regular_tool_batch(
                            ctx,
                            prepared_regular_batch,
                            events_to_record=events_to_record,
                            messages=messages,
                            collected_attachments=collected_attachments,
                            pending_assistant_attachments=pending_assistant_attachments,
                            promoted_tool_ids=promoted_tool_ids,
                            activated_tool_ids=activated_tool_ids,
                            on_token=on_token,
                            on_tool_result=on_tool_result,
                        )
                        prepared_regular_batch.clear()
                    HARNESS_GUARD_TRIPS.labels(guard="finalization", tool_name=tool_id).inc()
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    payload = json.dumps(
                        {
                            "status": "rejected",
                            "reason": "finalization_required",
                            "message": (
                                "All step todos are terminal, so further non-finalization "
                                "tool use is blocked. "
                                f"{finalization_instruction['message']}"
                            ),
                            "required_action": finalization_instruction["required_action"],
                            "allowed_tools": sorted(allowed_finalization_tools),
                        }
                    )
                    messages.append(_tool_result_message(tc, payload, protected=True))
                    _append_tool_result_event(
                        events_to_record,
                        tc,
                        payload,
                        True,
                        tool_id=tool_id,
                        protect_from_pruning=True,
                    )
                    post_tool_system_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Internal controller reminder — this is not a new user "
                                "message. All step todos are terminal. "
                                f"{finalization_instruction['reminder']} Use step_todo_write "
                                "only if the todo state is wrong."
                            ),
                            "_workflow_step_reminder": True,
                        }
                    )
                    _queue_audit_message(
                        role="developer",
                        source="tool_reminder",
                        content=str(post_tool_system_messages[-1]["content"]),
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_result:finalization:{tc.name}",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, payload, True, None, None)
                    record_tool_call(ctx.loop_guard_state, tc.name, tc.arguments)
                    continue
                if finalization_instruction is not None and tc.name in {
                    WRITE_DELIVERABLE,
                    STEP_COMPLETE,
                }:
                    tool_call_count += 1

                # ---- Harness guards (pre-dispatch) ----------------------
                #
                # Loop guard: reject a 2nd consecutive identical call with a
                # teach-back so the model makes progress or finalizes. Must
                # run before argument sanity so a repeatedly invalid call
                # also trips loop detection (record_tool_call is idempotent
                # from the model's perspective — it only tracks the
                # (name, args) key, not success/failure).
                loop_message = check_loop_guard(ctx.loop_guard_state, tc.name, tc.arguments)
                if loop_message is not None:
                    if prepared_regular_batch:
                        await self._execute_regular_tool_batch(
                            ctx,
                            prepared_regular_batch,
                            events_to_record=events_to_record,
                            messages=messages,
                            collected_attachments=collected_attachments,
                            pending_assistant_attachments=pending_assistant_attachments,
                            promoted_tool_ids=promoted_tool_ids,
                            activated_tool_ids=activated_tool_ids,
                            on_token=on_token,
                            on_tool_result=on_tool_result,
                        )
                        prepared_regular_batch.clear()
                    HARNESS_GUARD_TRIPS.labels(guard="loop", tool_name=tool_id).inc()
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    loop_payload = loop_guard_rejection_payload(tc.name, tc.arguments, loop_message)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": loop_payload}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, loop_payload, True, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_result:loop_guard:{tc.name}",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, loop_payload, True, None, None)
                    # Record the call so a *third* identical call also
                    # fails (streak keeps incrementing). This matters when
                    # the model ignores the teach-back.
                    record_tool_call(ctx.loop_guard_state, tc.name, tc.arguments)
                    continue

                # Argument sanity gate — applies only to real executor-
                # routed tools, not controller-intercepted ones whose args
                # are validated in their dedicated handlers below.
                if tc.name not in _CONTROLLER_INTERCEPTED_TOOLS:
                    violation = check_argument_sanity(tc.name, tc.arguments)
                    if violation is not None:
                        if prepared_regular_batch:
                            await self._execute_regular_tool_batch(
                                ctx,
                                prepared_regular_batch,
                                events_to_record=events_to_record,
                                messages=messages,
                                collected_attachments=collected_attachments,
                                pending_assistant_attachments=pending_assistant_attachments,
                                promoted_tool_ids=promoted_tool_ids,
                                activated_tool_ids=activated_tool_ids,
                                on_token=on_token,
                                on_tool_result=on_tool_result,
                            )
                            prepared_regular_batch.clear()
                        HARNESS_GUARD_TRIPS.labels(guard="argument_sanity", tool_name=tool_id).inc()
                        _append_tool_call_event(events_to_record, tc, tool_id)
                        sanity_payload = argument_sanity_rejection_payload(
                            tc.name, tc.arguments, violation
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": sanity_payload,
                            }
                        )
                        _append_tool_result_event(
                            events_to_record, tc, sanity_payload, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:arg_sanity:{tc.name}",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, sanity_payload, True, None, None
                            )
                        record_tool_call(ctx.loop_guard_state, tc.name, tc.arguments)
                        continue

                if tc.name in _PROJECT_TOUCH_TOOL_NAMES:
                    if prepared_regular_batch:
                        await self._execute_regular_tool_batch(
                            ctx,
                            prepared_regular_batch,
                            events_to_record=events_to_record,
                            messages=messages,
                            collected_attachments=collected_attachments,
                            pending_assistant_attachments=pending_assistant_attachments,
                            promoted_tool_ids=promoted_tool_ids,
                            activated_tool_ids=activated_tool_ids,
                            on_token=on_token,
                            on_tool_result=on_tool_result,
                        )
                        prepared_regular_batch.clear()
                    can_continue_after_context = self._can_continue_after_project_context_load(
                        ctx,
                        tc,
                        registry,
                    )
                    loaded_project_context: ProjectContextEntry | ProjectMetadataEntry | None = None
                    force_project_context_retry = False
                    # Secondary-agent delegations skip dynamic project context probing
                    # (AGENTS.md injection) — they run without project instructions.
                    if not ctx.policy.skip_memory:
                        loaded_project_context = await self._maybe_load_project_context_before_tool(
                            ctx,
                            tc=tc,
                        )
                    if loaded_project_context is None and not can_continue_after_context:
                        loaded_project_context = self._project_context_loaded_for_tool_target(
                            ctx,
                            tc,
                            loaded_project_contexts_this_cycle,
                        )
                        force_project_context_retry = loaded_project_context is not None
                    if loaded_project_context is not None and can_continue_after_context:
                        context_key = getattr(
                            loaded_project_context,
                            "project_root",
                            getattr(loaded_project_context, "project_id", ""),
                        )
                        loaded_project_contexts_this_cycle[str(context_key)] = (
                            loaded_project_context
                        )
                        content_hash = loaded_project_context.content_hash
                        if content_hash not in queued_project_context_hashes:
                            queued_project_context_hashes.add(content_hash)
                            post_tool_system_messages.append(
                                {
                                    "role": "system",
                                    "content": loaded_project_context.content,
                                    "_project_context": True,
                                }
                            )
                    elif loaded_project_context is not None:
                        HARNESS_GUARD_TRIPS.labels(guard="project_context", tool_name=tool_id).inc()
                        _append_tool_call_event(events_to_record, tc, tool_id)
                        rejection_payload = json.dumps(
                            {
                                "status": "retry",
                                "reason": "project_instructions_loaded",
                                "message": (
                                    "Project instructions were loaded before accessing the repository. "
                                    "Review them and re-issue the tool call if it is still needed."
                                )
                                if not force_project_context_retry
                                else (
                                    "Project instructions were loaded earlier in this tool batch. "
                                    "Review them and re-issue this mutating tool call if it is still needed."
                                ),
                                "project_root": getattr(
                                    loaded_project_context, "project_root", None
                                ),
                                "source_path": getattr(loaded_project_context, "source_path", None),
                                "project_id": getattr(loaded_project_context, "project_id", None),
                                "required_action": "reissue_tool_call_after_reading_project_instructions",
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": rejection_payload,
                            }
                        )
                        content_hash = loaded_project_context.content_hash
                        if content_hash not in queued_project_context_hashes:
                            queued_project_context_hashes.add(content_hash)
                            post_tool_system_messages.append(
                                {
                                    "role": "system",
                                    "content": loaded_project_context.content,
                                    "_project_context": True,
                                }
                            )
                        _append_tool_result_event(
                            events_to_record, tc, rejection_payload, True, tool_id=tool_id
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id,
                                tc.name,
                                rejection_payload,
                                True,
                                None,
                                None,
                            )
                        for trailing_call in tool_calls[tc_index + 1 :]:
                            trailing_tool_id = _tool_id_for_call(trailing_call.name, registry)
                            trailing_payload = json.dumps(
                                {
                                    "status": "cancelled",
                                    "reason": "project_instructions_loaded",
                                    "message": (
                                        "This tool call was not executed because the controller "
                                        "loaded project instructions first. Re-plan and re-issue any "
                                        "needed tool calls after reading them."
                                    ),
                                }
                            )
                            if on_tool_call:
                                await on_tool_call(
                                    trailing_call.name,
                                    trailing_call.call_id,
                                    trailing_call.arguments,
                                )
                            _append_tool_call_event(
                                events_to_record, trailing_call, trailing_tool_id
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": trailing_call.call_id,
                                    "content": trailing_payload,
                                }
                            )
                            _append_tool_result_event(
                                events_to_record,
                                trailing_call,
                                trailing_payload,
                                True,
                                tool_id=trailing_tool_id,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    trailing_call.call_id,
                                    trailing_call.name,
                                    trailing_payload,
                                    True,
                                    None,
                                    None,
                                )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:project_context:{tc.name}",
                            on_token=on_token,
                        )
                        restart_llm_cycle = True
                        break

                # Record the (name, args) tuple so future calls can detect
                # identical-in-a-row repeats.
                record_tool_call(ctx.loop_guard_state, tc.name, tc.arguments)

                if self._should_count_tool_call(tc.name):
                    tool_call_count += 1

                if prepared_regular_batch and (
                    tc.name in _CONTROLLER_INTERCEPTED_TOOLS or is_orchestration_tool(tc.name)
                ):
                    await self._execute_regular_tool_batch(
                        ctx,
                        prepared_regular_batch,
                        events_to_record=events_to_record,
                        messages=messages,
                        collected_attachments=collected_attachments,
                        pending_assistant_attachments=pending_assistant_attachments,
                        promoted_tool_ids=promoted_tool_ids,
                        activated_tool_ids=activated_tool_ids,
                        on_token=on_token,
                        on_tool_result=on_tool_result,
                    )
                    prepared_regular_batch.clear()

                # Controller tool interception
                if tc.name == WRITE_DELIVERABLE:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:write_deliverable",
                        on_token=on_token,
                    )
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue

                    content = str(tc.arguments.get("content", ""))
                    format_name = str(tc.arguments.get("format") or "markdown")
                    title = tc.arguments.get("title")
                    target = tc.arguments.get("target")
                    outputs = tc.arguments.get("outputs")

                    if self._deliverable_owner_step_run_id(ctx) is None:
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "not_in_workflow",
                                "message": (
                                    "write_deliverable is only available inside workflow steps "
                                    "or workflow-backed delegated sessions."
                                ),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record,
                            tc,
                            err_content,
                            True,
                            tool_id=tool_id,
                            protect_from_pruning=True,
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:write_deliverable",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    if not content.strip():
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "empty_content",
                                "message": "write_deliverable requires non-empty content.",
                            }
                        )
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record,
                            tc,
                            err_content,
                            True,
                            tool_id=tool_id,
                            protect_from_pruning=True,
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:write_deliverable",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    if format_name not in {"markdown", "plain", "html"}:
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "invalid_format",
                                "message": "write_deliverable format must be one of: markdown, plain, html.",
                            }
                        )
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record,
                            tc,
                            err_content,
                            True,
                            tool_id=tool_id,
                            protect_from_pruning=True,
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:write_deliverable",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    deliverable = await self._write_step_deliverable(
                        ctx,
                        content=content,
                        format=format_name,
                        title=str(title).strip()
                        if isinstance(title, str) and title.strip()
                        else None,
                        target=(
                            str(target)
                            if isinstance(target, str) and target in {"channel", "none"}
                            else None
                        ),
                        outputs=outputs if isinstance(outputs, dict) else {},
                    )
                    preview = compact_snippet(content, max_chars=_DELIVERABLE_PREVIEW_CHARS)
                    result_content = json.dumps(
                        {
                            "status": "buffered",
                            "deliverable_id": deliverable.deliverable_id,
                            "version": deliverable.version,
                            "length": len(content),
                            "format": deliverable.format,
                            "preview": preview,
                        }
                    )
                    messages.append(_tool_result_message(tc, result_content))
                    _append_tool_result_event(
                        events_to_record,
                        tc,
                        result_content,
                        False,
                        tool_id=tool_id,
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:write_deliverable",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    # Arm the post-deliverable reminder. The next LLM cycle
                    # will inject a strong system message if the model has
                    # also marked all todos terminal but not yet called
                    # step_complete. This is a nudge; the explicit
                    # step_complete call is still required so the model can
                    # provide outcome, summary, and notification mode.
                    if ctx.policy.require_step_complete:
                        ctx.post_deliverable_pending = True
                        ctx.post_deliverable_reminders_sent = 0
                    continue

                if tc.name == SWITCH_EXECUTOR:
                    # Stage 36: switch the conversation's active executor.
                    # The active executor binding persists across turns and
                    # steps until the next switch (by the agent or by the
                    # user via /executor). The controller never auto-changes
                    # it; this tool is the agent's only mutator.
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:switch_executor",
                        on_token=on_token,
                    )
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    target_executor_id_arg = (
                        tc.arguments.get("executor_id") if isinstance(tc.arguments, dict) else None
                    )
                    reason_arg = (
                        tc.arguments.get("reason") if isinstance(tc.arguments, dict) else None
                    )
                    pool = getattr(ctx, "executor_pool", None)
                    if pool is None or not isinstance(target_executor_id_arg, str):
                        err_payload = {
                            "status": "error",
                            "reason": "no_pool" if pool is None else "missing_argument",
                            "detail": (
                                "Executor pool unavailable for this step."
                                if pool is None
                                else "switch_executor requires an executor_id argument."
                            ),
                        }
                        err_content = json.dumps(err_payload)
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:switch_executor",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    from cognis.core.executor_switching import perform_executor_switch

                    outcome = await perform_executor_switch(
                        conversation_id=ctx.conversation.conversation_id,
                        pool=pool,
                        executor_id=target_executor_id_arg.strip(),
                        actor="agent",
                        session_factory=self.session_manager.session_factory,
                        reason=reason_arg if isinstance(reason_arg, str) else None,
                        task_id=ctx.task_id,
                    )
                    is_error = outcome.status == "error"
                    if not is_error and outcome.target is not None:
                        self._install_active_executor_target(ctx, outcome.target)
                    result_content = json.dumps(outcome.to_tool_result())
                    messages.append(_tool_result_message(tc, result_content, protected=True))
                    _append_tool_result_event(
                        events_to_record, tc, result_content, is_error, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:switch_executor",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id, tc.name, result_content, is_error, None, None
                        )
                    continue

                if tc.name == STEP_COMPLETE:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:step_complete",
                        on_token=on_token,
                    )
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments, ctx=ctx
                    )
                    if validation_error is not None:
                        metadata_error = self._step_metadata_contract_error(ctx, tc.arguments)
                        if metadata_error is not None:
                            STEP_COMPLETE_REJECTIONS.labels(
                                reason="invalid_step_complete_metadata"
                            ).inc()
                            err_content = _build_step_complete_metadata_error(
                                tc.arguments,
                                metadata_error,
                                ctx,
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.call_id,
                                    "content": err_content,
                                }
                            )
                            _append_tool_result_event(
                                events_to_record, tc, err_content, True, tool_id=tool_id
                            )
                            await self._flush_events_incremental(
                                ctx,
                                events_to_record,
                                reason="tool_result:step_complete",
                                on_token=on_token,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    tc.call_id, tc.name, err_content, True, None, None
                                )
                            continue
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    # Reject step_complete when it's not available (e.g. direct chat)
                    if not ctx.policy.step_complete_available:
                        err_content = json.dumps(
                            {
                                "status": "error",
                                "message": (
                                    "step_complete is not available in this context. "
                                    "Simply respond to the user directly."
                                ),
                            }
                        )
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    # Enforce todo completion for workflow steps. Every
                    # todo must be in a terminal state — either ``completed``
                    # or ``cancelled``. Cancellation is a first-class closure
                    # action so the agent has a clean out when a todo turns
                    # out to be impossible or irrelevant.
                    incomplete_todos = self._get_incomplete_todos(ctx)
                    if incomplete_todos and ctx.policy.require_step_complete:
                        STEP_COMPLETE_REJECTIONS.labels(reason="todos_pending").inc()
                        pending_names = [
                            str(t.get("content", "?"))
                            for t in incomplete_todos
                            if t.get("status") == "pending"
                        ]
                        in_progress_names = [
                            str(t.get("content", "?"))
                            for t in incomplete_todos
                            if t.get("status") == "in_progress"
                        ]

                        def _fmt(items: list[str], limit: int = 5) -> str:
                            if not items:
                                return ""
                            head = items[:limit]
                            rendered = ", ".join(repr(name) for name in head)
                            if len(items) > limit:
                                rendered += f", … (+{len(items) - limit} more)"
                            return rendered

                        detail_parts: list[str] = []
                        if pending_names:
                            detail_parts.append(f"pending: {_fmt(pending_names)}")
                        if in_progress_names:
                            detail_parts.append(f"in_progress: {_fmt(in_progress_names)}")
                        detail = "; ".join(detail_parts) or "non-terminal todos exist"

                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "todos_pending",
                                "message": (
                                    f"Cannot complete: {len(incomplete_todos)} todo(s) are "
                                    f"still non-terminal ({detail}). Mark each remaining "
                                    "todo as either completed or cancelled via "
                                    "step_todo_write, then call step_complete again. Do "
                                    "not repeat, restate, or paraphrase your prior written "
                                    "deliverable — it is already in session history."
                                ),
                                "pending": pending_names,
                                "in_progress": in_progress_names,
                                "required_action": "update_todos_then_retry_step_complete",
                            }
                        )
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )

                        # Strong follow-up system reminder — the tool-result
                        # alone has proven insufficient to stop models from
                        # repeating an already-delivered brief. A distinct
                        # system message keeps the instructions prominent
                        # and lets the controller emit a single, consistent
                        # prescription.
                        post_tool_system_messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Internal controller reminder — this is not a new "
                                    "user message. Do not write a filler acknowledgment. "
                                    "step_complete was rejected: todos remain "
                                    "non-terminal. Your next action MUST be "
                                    "step_todo_write that marks every remaining todo as "
                                    "either 'completed' or 'cancelled'. Then call "
                                    "step_complete again. Do NOT repeat, restate, or "
                                    "paraphrase your prior written deliverable — it is "
                                    "already recorded and the evaluator will read it."
                                ),
                            }
                        )
                        _queue_audit_message(
                            role="developer",
                            source="tool_reminder",
                            content=str(post_tool_system_messages[-1]["content"]),
                        )

                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    current_deliverable = await self._get_current_deliverable(ctx)
                    if ctx.step_definition.require_deliverable and current_deliverable is None:
                        STEP_COMPLETE_REJECTIONS.labels(reason="deliverable_missing").inc()
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "deliverable_missing",
                                "message": (
                                    "This step requires a deliverable. Call write_deliverable with "
                                    "your final user-facing output, then call step_complete. Do not "
                                    "restate the deliverable as free text; write_deliverable is the "
                                    "canonical workflow artifact."
                                ),
                                "required_action": "write_deliverable_then_retry_step_complete",
                            }
                        )
                        messages.append(_tool_result_message(tc, err_content, protected=True))
                        _append_tool_result_event(
                            events_to_record,
                            tc,
                            err_content,
                            True,
                            tool_id=tool_id,
                            protect_from_pruning=True,
                        )
                        post_tool_system_messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Internal controller reminder — this is not a new user message. "
                                    "Do not write a filler acknowledgment. step_complete was rejected "
                                    "because this step requires a deliverable. Your next action MUST be "
                                    "write_deliverable with the final user-facing artifact. After that, "
                                    "call step_complete again. Do NOT repeat the deliverable as free text."
                                ),
                            }
                        )
                        _queue_audit_message(
                            role="developer",
                            source="tool_reminder",
                            content=str(post_tool_system_messages[-1]["content"]),
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    trailing_tool_calls = tool_calls[tc_index + 1 :]
                    if trailing_tool_calls:
                        STEP_COMPLETE_REJECTIONS.labels(
                            reason="step_complete_not_last_tool_call"
                        ).inc()
                        trailing_names = [call.name for call in trailing_tool_calls]
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "step_complete_not_last_tool_call",
                                "message": (
                                    "step_complete must be the final tool call in a response. "
                                    f"This response still has trailing tool calls: {trailing_names}. "
                                    "Execute or omit those calls before calling step_complete again."
                                ),
                                "trailing_tool_calls": trailing_names,
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    try:
                        deliverable_outputs = (
                            dict(current_deliverable.outputs)
                            if current_deliverable is not None
                            and isinstance(current_deliverable.outputs, dict)
                            else {}
                        )
                        step_complete_outputs = (
                            tc.arguments.get("outputs", {})
                            if isinstance(tc.arguments.get("outputs"), dict)
                            else {}
                        )
                        self._validate_step_metadata_contract(ctx, tc.arguments)
                        step_output = StepOutput(
                            summary=tc.arguments.get("summary", ""),
                            content=(
                                current_deliverable.content
                                if current_deliverable is not None
                                else last_assistant_content
                            ),
                            outputs={**deliverable_outputs, **step_complete_outputs},
                            metadata=tc.arguments.get("metadata", {})
                            if isinstance(tc.arguments.get("metadata"), dict)
                            else {},
                            claims=tc.arguments.get("claims", []),
                            outcome=tc.arguments.get("outcome"),
                            notification=tc.arguments.get("notification"),
                            deliverable_id=(
                                current_deliverable.deliverable_id
                                if current_deliverable is not None
                                else None
                            ),
                            deliverable_version=(
                                current_deliverable.version
                                if current_deliverable is not None
                                else None
                            ),
                            deliverable_format=(
                                current_deliverable.format
                                if current_deliverable is not None
                                else None
                            ),
                            deliverable_title=(
                                current_deliverable.title
                                if current_deliverable is not None
                                else None
                            ),
                            execution_evidence=dict(ctx.execution_evidence),
                            attachments=list(collected_attachments),
                            session_id=ctx.session.session_id,
                            intaris_session_id=ctx.session.intaris_session_id
                            or ctx.session.session_id,
                            completed_at=datetime.now(UTC),
                        )
                        _validate_step_completion_notification(
                            ctx,
                            step_output,
                            deliverable_content=(
                                current_deliverable.content
                                if current_deliverable is not None
                                else None
                            ),
                        )
                    except StepMetadataContractError as exc:
                        STEP_COMPLETE_REJECTIONS.labels(
                            reason="invalid_step_complete_metadata"
                        ).inc()
                        err_content = _build_step_complete_metadata_error(tc.arguments, exc, ctx)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue
                    except ValidationError as exc:
                        STEP_COMPLETE_REJECTIONS.labels(
                            reason="invalid_step_complete_arguments"
                        ).inc()
                        err_content = _build_step_complete_validation_error(tc.arguments, exc, ctx)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue
                    except ValueError as exc:
                        STEP_COMPLETE_REJECTIONS.labels(
                            reason="invalid_step_complete_notification"
                        ).inc()
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "invalid_step_complete_notification",
                                "message": str(exc),
                                "received": tc.arguments,
                                "example": _step_complete_example_payload(ctx),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    events_to_record.append(
                        SessionEvent(
                            type="lifecycle",
                            data={
                                "event": "step_complete",
                                "status": "completed",
                                "summary": step_output.summary,
                                "outcome_status": (
                                    step_output.outcome.status if step_output.outcome else "success"
                                ),
                                "notification_mode": (
                                    step_output.notification.mode
                                    if step_output.notification is not None
                                    else ctx.completion_delivery.completion_mode_family
                                ),
                            },
                        )
                    )
                    result_content = json.dumps({"status": "completed"})
                    messages.append(_tool_result_message(tc, result_content))
                    _append_tool_result_event(
                        events_to_record,
                        tc,
                        result_content,
                        False,
                        tool_id=tool_id,
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_complete",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    break

                elif tc.name == STEP_TODO_WRITE:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    raw_requested = tc.arguments.get("todos", [])
                    if not isinstance(raw_requested, list):
                        raw_requested = []
                    requested_todos = [item for item in raw_requested if isinstance(item, dict)]
                    previous_normalized = _normalize_todos(ctx.todos or [])
                    new_normalized = _normalize_todos(requested_todos)
                    unchanged = previous_normalized == new_normalized
                    ctx.todos = requested_todos
                    await self._persist_step_todos(ctx)
                    if not unchanged and ctx.task_id and ctx.step_run_id:
                        await self.event_bus.publish(
                            Event(
                                type=EventType.CONVERSATION_STATE_CHANGED,
                                data={
                                    "source_kind": "task_step.todos.changed",
                                    "user_email": ctx.session.user_email,
                                    "task_id": ctx.task_id,
                                    "step_run_id": ctx.step_run_id,
                                },
                            )
                        )
                    non_terminal = [
                        item
                        for item in new_normalized
                        if item.get("status") not in ("completed", "cancelled")
                    ]
                    non_terminal_count = len(non_terminal)
                    if unchanged:
                        guidance = (
                            "Todos unchanged since the last write. Make progress "
                            "on the in_progress item, mark items completed or "
                            "cancelled as appropriate, or call step_complete if "
                            "all work is done."
                        )
                    elif non_terminal_count > 0:
                        guidance = (
                            f"{non_terminal_count} todo(s) are still pending or "
                            "in_progress. Before calling step_complete, mark each "
                            "remaining todo as either completed or cancelled."
                        )
                    else:
                        finalization_instruction = await self._finalization_instruction(ctx)
                        if finalization_instruction is not None:
                            guidance = (
                                "All todos are terminal (completed or cancelled). "
                                f"{finalization_instruction['message']}"
                            )
                        elif not ctx.policy.require_step_complete:
                            guidance = (
                                "All todos are terminal (completed or cancelled). "
                                "If no new user-visible information, required question, "
                                "or correction remains, end silently with no assistant text."
                            )
                        else:
                            guidance = (
                                "All todos are terminal (completed or cancelled). "
                                "Finish with the appropriate user-facing result for this turn."
                            )
                    # Canonical echo gives the model a verifiable view of
                    # what it actually wrote (the previous write-only
                    # ``{status, count}`` shape provoked repeated identical
                    # rewrites in the daily-brief trace). To keep echo size
                    # bounded for very long todo lists, we cap per-item
                    # ``content`` in the echo; ``ctx.todos`` still holds
                    # the untruncated values for the agent loop.
                    echo_todos = _echo_todos_bounded(new_normalized)
                    result_content = json.dumps(
                        {
                            "status": "updated",
                            "count": len(new_normalized),
                            "todos": echo_todos,
                            "unchanged": unchanged,
                            "non_terminal_count": non_terminal_count,
                            "guidance": guidance,
                        }
                    )
                    messages.append(_tool_result_message(tc, result_content))
                    _append_tool_result_event(
                        events_to_record,
                        tc,
                        result_content,
                        False,
                        tool_id=tool_id,
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_todo_write",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_TODO_LIST:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    list_normalized = _normalize_todos(ctx.todos or [])
                    list_non_terminal = sum(
                        1
                        for item in list_normalized
                        if item.get("status") not in ("completed", "cancelled")
                    )
                    result_content = json.dumps(
                        {
                            "todos": list_normalized,
                            "count": len(list_normalized),
                            "non_terminal_count": list_non_terminal,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_todo_list",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_REQUEST_QUESTIONS:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:step_request_questions",
                        on_token=on_token,
                    )
                    _track_pending_tool_call(ctx, tc, tool_id=tool_id)
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    if (
                        ctx.interaction_mode != "step_requests"
                        or not ctx.step_definition.allow_questions
                    ):
                        err_content = json.dumps(
                            {"error": "Input requests are not enabled for this step."}
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_questions",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    recovered_reply = self._get_recovered_step_response(ctx)
                    if recovered_reply is not None:
                        await self._clear_interactive_pause_state(ctx)
                        rec_content = json.dumps(recovered_reply)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": rec_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, rec_content, False, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_questions",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, rec_content, False, None, None
                            )
                        continue

                    try:
                        questions = normalize_questions(tc.arguments.get("questions"))
                    except ValueError as exc:
                        err_content = json.dumps({"error": str(exc)})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_questions",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    # Pause and wait for input
                    pause_id = f"input_{uuid.uuid4().hex[:12]}"
                    pause_context = normalize_context(tc.arguments.get("context"))

                    # Create the step question via the notification service
                    # so it is persisted, resolved to the source conversation,
                    # and survives restarts.
                    await self.notification_service.create(
                        notification_type="step_question",
                        user_email=ctx.session.user_email,
                        conversation_id=ctx.conversation.conversation_id,
                        task_id=ctx.task_id,
                        step_name=ctx.step_definition.name,
                        step_run_id=ctx.step_run_id,
                        session_id=ctx.session.session_id,
                        notification_id=pause_id,
                        payload={
                            "questions": questions,
                            "context": pause_context,
                        },
                    )

                    await self._set_interactive_pause_state(
                        ctx,
                        pause_type="step_input",
                        pause_payload={
                            "pause_id": pause_id,
                            "step_name": ctx.step_definition.name,
                            "step_run_id": ctx.step_run_id,
                            "session_id": ctx.session.session_id,
                            "questions": questions,
                            "context": pause_context,
                        },
                    )
                    try:
                        resolution = await self.pause_waiter.wait(pause_id, timeout=300.0)
                        await self._clear_interactive_pause_state(ctx)
                        if resolution.decision == "cancel":
                            # No tool_result event needed: StepInterrupted propagates
                            # without calling _finalize_step, so events_to_record is discarded.
                            raise StepInterrupted("Step input request cancelled")
                        reply = validate_reply_for_questions(resolution.data, questions)
                        resp_content = json.dumps(reply)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": resp_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, resp_content, False, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_questions",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, resp_content, False, None, None
                            )
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        if self.notification_service is not None:
                            await self.notification_service.mark_orphaned(
                                pause_id,
                                reason="timeout",
                            )
                        timeout_content = json.dumps({"error": "Input request timed out."})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": timeout_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, timeout_content, True, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_questions",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, timeout_content, True, None, None
                            )
                    continue

                elif tc.name in {REQUEST_CREDENTIAL, REQUEST_AUTH_CHALLENGE}:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_call:{tc.name}",
                        on_token=on_token,
                    )
                    _track_pending_tool_call(ctx, tc, tool_id=tool_id)
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    pause_id = f"auth_{uuid.uuid4().hex[:12]}"
                    timeout_seconds = int(tc.arguments.get("timeout_seconds", 600) or 600)
                    auth_payload: dict[str, Any] = {
                        "credential_id": tc.arguments.get("credential_id"),
                        "kind": tc.arguments.get("kind"),
                        "scope": tc.arguments.get("scope", "user"),
                        "agent_id": tc.arguments.get("agent_id"),
                        "agent_profile_id": tc.arguments.get("agent_profile_id"),
                        "label": tc.arguments.get("label")
                        or tc.arguments.get("credential_id")
                        or "Authentication required",
                        "message": tc.arguments.get("message")
                        or tc.arguments.get("description")
                        or "Authentication is required to continue.",
                        "description": tc.arguments.get("description"),
                        "metadata": (
                            tc.arguments.get("metadata")
                            if isinstance(tc.arguments.get("metadata"), dict)
                            else {}
                        ),
                        "required_fields": (
                            tc.arguments.get("required_fields")
                            if isinstance(tc.arguments.get("required_fields"), list)
                            else []
                        ),
                        "expires_at": (
                            datetime.now(UTC) + timedelta(seconds=timeout_seconds)
                        ).isoformat(),
                    }
                    notification_type = (
                        "credential_request" if tc.name == REQUEST_CREDENTIAL else "auth_challenge"
                    )
                    await self.notification_service.create(
                        notification_type=notification_type,
                        user_email=ctx.session.user_email,
                        conversation_id=ctx.conversation.conversation_id,
                        task_id=ctx.task_id,
                        step_name=ctx.step_definition.name,
                        step_run_id=ctx.step_run_id,
                        session_id=ctx.session.session_id,
                        notification_id=pause_id,
                        payload=auth_payload,
                    )
                    await self._set_interactive_pause_state(
                        ctx,
                        pause_type=notification_type,
                        pause_payload={
                            "pause_id": pause_id,
                            "step_name": ctx.step_definition.name,
                            "step_run_id": ctx.step_run_id,
                            "session_id": ctx.session.session_id,
                            **auth_payload,
                        },
                    )
                    try:
                        resolution = await self.pause_waiter.wait(
                            pause_id, timeout=float(timeout_seconds)
                        )
                        await self._clear_interactive_pause_state(ctx)
                        if resolution.decision in {"cancel", "deny"}:
                            raise StepInterrupted("Authentication request cancelled")
                        if tc.name == REQUEST_CREDENTIAL and not resolution.data.get(
                            "credential_id"
                        ):
                            err_content = json.dumps(
                                {"error": "Credential request was not fulfilled."}
                            )
                            messages.append(
                                {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                            )
                            _append_tool_result_event(
                                events_to_record, tc, err_content, True, tool_id=tool_id
                            )
                            _resolve_pending_tool_call(ctx, tc.call_id)
                            await self._flush_events_incremental(
                                ctx,
                                events_to_record,
                                reason=f"tool_result:{tc.name}",
                                on_token=on_token,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    tc.call_id, tc.name, err_content, True, None, None
                                )
                            continue
                        if tc.name == REQUEST_AUTH_CHALLENGE and not (
                            resolution.data.get("response_ref")
                            or resolution.data.get("challenge_completed")
                        ):
                            err_content = json.dumps({"error": "Auth challenge was not fulfilled."})
                            messages.append(
                                {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                            )
                            _append_tool_result_event(
                                events_to_record, tc, err_content, True, tool_id=tool_id
                            )
                            _resolve_pending_tool_call(ctx, tc.call_id)
                            await self._flush_events_incremental(
                                ctx,
                                events_to_record,
                                reason=f"tool_result:{tc.name}",
                                on_token=on_token,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    tc.call_id, tc.name, err_content, True, None, None
                                )
                            continue
                        created_credential_id = resolution.data.get("credential_id")
                        credential_granted = False
                        if tc.name == REQUEST_CREDENTIAL and isinstance(created_credential_id, str):
                            grant_credential_to_agent_definition(ctx.agent, created_credential_id)
                            async with self.session_manager.session_factory() as db_session:
                                credential_granted = await grant_credential_to_agent(
                                    db_session,
                                    agent_id=ctx.agent.agent_id,
                                    credential_id=created_credential_id,
                                    owner_email=ctx.session.user_email,
                                )
                                if credential_granted:
                                    await db_session.commit()
                        resp_content = json.dumps(
                            {
                                "credential_id": created_credential_id,
                                "credential_label": resolution.data.get("credential_label"),
                                "credential_kind": resolution.data.get("credential_kind"),
                                "credential_granted_to_agent": bool(created_credential_id),
                                "agent_permissions_updated": credential_granted,
                                "response_ref": resolution.data.get("response_ref"),
                                "challenge_completed": resolution.data.get(
                                    "challenge_completed", False
                                ),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": resp_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, resp_content, False, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:{tc.name}",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, resp_content, False, None, None
                            )
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        if self.notification_service is not None:
                            await self.notification_service.mark_orphaned(
                                pause_id,
                                reason="timeout",
                            )
                        timeout_content = json.dumps({"error": "Authentication request timed out."})
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": timeout_content,
                            }
                        )
                        _append_tool_result_event(
                            events_to_record, tc, timeout_content, True, tool_id=tool_id
                        )
                        _resolve_pending_tool_call(ctx, tc.call_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:{tc.name}",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, timeout_content, True, None, None
                            )
                    continue

                elif tc.name == LIST_CREDENTIALS:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:list_credentials",
                        on_token=on_token,
                    )
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    rows = await self.providers.credentials.list_credentials(ctx.session.user_email)
                    allowed = set(
                        ctx.agent.permissions.allowed_credentials if ctx.agent.permissions else []
                    )
                    kind_filter = str(tc.arguments.get("kind", "")).strip().lower()
                    domain_filter = str(tc.arguments.get("domain", "")).strip().lower()
                    origin_filter = str(tc.arguments.get("origin", "")).strip().lower()
                    label_filter = str(tc.arguments.get("label_contains", "")).strip().lower()
                    matches: list[dict[str, Any]] = []
                    for row in rows:
                        if row.credential_id not in allowed:
                            continue
                        metadata = row.metadata or {}
                        if kind_filter and str(row.kind).lower() != kind_filter:
                            continue
                        if (
                            domain_filter
                            and str(metadata.get("domain", "")).lower() != domain_filter
                        ):
                            continue
                        if (
                            origin_filter
                            and str(metadata.get("origin", "")).lower() != origin_filter
                        ):
                            continue
                        if label_filter and label_filter not in str(row.label).lower():
                            continue
                        matches.append(
                            {
                                "credential_id": row.credential_id,
                                "kind": row.kind,
                                "label": row.label,
                                "description": row.description,
                                "metadata": metadata,
                                "field_names": list(row.field_names or []),
                                "status": row.status,
                                "expires_at": row.expires_at.isoformat()
                                if row.expires_at
                                else None,
                            }
                        )
                    result_content = json.dumps({"credentials": matches})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:list_credentials",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == SEARCH_TOOLS_TOOL.name:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:search_tools",
                        on_token=on_token,
                    )
                    validation_error = self._validate_controller_tool_arguments(
                        tc.name, tc.arguments
                    )
                    if validation_error is not None:
                        await self._emit_tool_argument_error(
                            ctx,
                            tc=tc,
                            tool_id=tool_id,
                            events_to_record=events_to_record,
                            messages=messages,
                            error=validation_error,
                            on_tool_result=on_tool_result,
                            on_token=on_token,
                        )
                        continue
                    matches = search_inventory(
                        searchable_inventory_tools,
                        str(tc.arguments.get("query", "")),
                        category=(
                            str(tc.arguments.get("category"))
                            if tc.arguments.get("category") is not None
                            else None
                        ),
                        limit=int(tc.arguments.get("limit", 10) or 10),
                        already_visible_tool_ids=set(exposure.visible_tool_ids),
                        log_context={
                            "reason": "search_tools_builtin",
                            "session_id": ctx.session.session_id,
                            **self._step_log_metadata(ctx),
                        },
                    )
                    new_promoted = {
                        str(match["tool_id"])
                        for match in matches
                        if isinstance(match.get("tool_id"), str)
                    }
                    discovered_at = datetime.now(UTC).isoformat()
                    discovered_handles: list[dict[str, Any]] = []
                    for match in matches:
                        handle = match.get("handle")
                        if not isinstance(handle, dict):
                            continue
                        handle["discovered_at"] = discovered_at
                        discovered_handles.append(dict(handle))
                    store_discovered = getattr(
                        self.session_cache,
                        "store_discovered_tool_handles",
                        None,
                    )
                    if callable(store_discovered):
                        store_discovered(
                            ctx.session.session_id,
                            discovered_handles,
                            discovered_at=discovered_at,
                        )
                    if discovered_handles:
                        events_to_record.append(
                            SessionEvent(
                                type="lifecycle",
                                data={
                                    "event": "tool_discovery",
                                    "source_tool": SEARCH_TOOLS_TOOL.name,
                                    "query_length": len(str(tc.arguments.get("query", ""))),
                                    "handles": discovered_handles,
                                },
                            )
                        )
                    promoted_tool_ids.update(new_promoted)
                    logger.info(
                        "tool discovery updated",
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "query_length": len(str(tc.arguments.get("query", ""))),
                                "match_count": len(matches),
                                "promoted_tool_count": len(promoted_tool_ids),
                                "match_tool_ids": sorted(new_promoted),
                            }
                        },
                    )
                    result_content = json.dumps({"matches": matches})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:search_tools",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif is_orchestration_tool(tc.name):
                    # Orchestration tool — intercept as controller directive.
                    # Multiple consecutive delegate calls may have been
                    # executed in parallel up-front; in that case the
                    # tool_call event is already recorded and we just
                    # surface the precomputed result.
                    precomputed_orch = parallel_delegate_results.get(tc_index)
                    if precomputed_orch is not None:
                        orch_result = precomputed_orch
                    else:
                        _append_tool_call_event(events_to_record, tc, tool_id)
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_call:{tc.name}",
                            on_token=on_token,
                        )
                        orch_result = await self._handle_orchestration_tool(
                            tc,
                            ctx=ctx,
                            events_to_record=events_to_record,
                            on_token=on_token,
                            on_tool_call=on_tool_call,
                            on_tool_result=on_tool_result,
                        )
                    await self._save_tool_output_if_available(tc.call_id, orch_result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": orch_result.output,
                        }
                    )
                    _append_tool_result_event(
                        events_to_record,
                        tc,
                        orch_result.output,
                        orch_result.is_error,
                        tool_id=tool_id,
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_result:{tc.name}",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id,
                            tc.name,
                            orch_result.output,
                            orch_result.is_error,
                            None,
                            None,
                        )
                    # Check if async orchestration was spawned.  Fire-and-follow-up
                    # orchestration must end the parent turn instead of continuing
                    # the same scoped work "in the meantime".
                    if orch_result.metadata and orch_result.metadata.get("delegation_spawned"):
                        async_orchestration_spawned = True
                    if orch_result.metadata and orch_result.metadata.get(
                        "async_orchestration_spawned"
                    ):
                        async_orchestration_spawned = True
                    continue

                else:
                    prepared_call = _PreparedRegularToolCall(tool_call=tc, tool_id=tool_id)
                    if self._is_parallelizable_regular_tool_call(ctx, tc, registry):
                        prepared_regular_batch.append(prepared_call)
                        continue
                    if prepared_regular_batch:
                        await self._execute_regular_tool_batch(
                            ctx,
                            prepared_regular_batch,
                            events_to_record=events_to_record,
                            messages=messages,
                            collected_attachments=collected_attachments,
                            pending_assistant_attachments=pending_assistant_attachments,
                            promoted_tool_ids=promoted_tool_ids,
                            activated_tool_ids=activated_tool_ids,
                            on_token=on_token,
                            on_tool_result=on_tool_result,
                        )
                        prepared_regular_batch.clear()
                    await self._execute_regular_tool_batch(
                        ctx,
                        [prepared_call],
                        events_to_record=events_to_record,
                        messages=messages,
                        collected_attachments=collected_attachments,
                        pending_assistant_attachments=pending_assistant_attachments,
                        promoted_tool_ids=promoted_tool_ids,
                        activated_tool_ids=activated_tool_ids,
                        on_token=on_token,
                        on_tool_result=on_tool_result,
                    )

            if prepared_regular_batch:
                await self._execute_regular_tool_batch(
                    ctx,
                    prepared_regular_batch,
                    events_to_record=events_to_record,
                    messages=messages,
                    collected_attachments=collected_attachments,
                    pending_assistant_attachments=pending_assistant_attachments,
                    promoted_tool_ids=promoted_tool_ids,
                    activated_tool_ids=activated_tool_ids,
                    on_token=on_token,
                    on_tool_result=on_tool_result,
                )
                prepared_regular_batch.clear()

            if post_tool_system_messages:
                messages.extend(post_tool_system_messages)

            if restart_llm_cycle:
                continue

            if await self._consume_boundary_batch_if_available(
                ctx,
                messages=messages,
                pending_audit_messages=pending_audit_messages,
                reason="after_tool_cycle",
                on_token=on_token,
            ):
                step_output = None
                continue

            post_tool_projection = self._project_model_messages_for_budget(
                ctx,
                messages=messages,
                tool_schemas=exposure.tools,
                resolved_model=resolved_model,
                max_context_tokens=context_result.max_context_tokens,
            )
            post_tool_snapshot = post_tool_projection.snapshot
            self._store_context_usage_snapshot(
                ctx,
                snapshot=post_tool_snapshot,
                provider_id=current_provider_id,
            )
            self._maybe_log_context_reserve_clamp(
                ctx,
                post_tool_snapshot,
                provider_id=current_provider_id,
            )
            post_tool_projection_exceeded = self._projection_exceeded_selected_budget(
                post_tool_projection
            )
            ctx.last_projection_snapshot = post_tool_snapshot
            ctx.last_projection_exceeded_selected_budget = (
                post_tool_projection_exceeded if post_tool_snapshot is not None else None
            )

            # Check if step_complete was called in this batch
            if step_output is not None:
                break

            if (
                tool_call_count > 0
                and post_tool_snapshot is not None
                and post_tool_projection_exceeded
            ):
                pressure_run = CompactionRunContext.from_snapshot(
                    post_tool_snapshot,
                    trigger="tool_loop_pressure",
                    reason="tool_call_context_pressure",
                )
                notice = (
                    "Context window is critically full; stopping this turn before more "
                    f"tool calls. Usage is {post_tool_snapshot.prompt_tokens:,}/"
                    f"{post_tool_snapshot.available_prompt_tokens:,} prompt-budget tokens "
                    f"(threshold {post_tool_snapshot.threshold_prompt_tokens:,})."
                )
                logger.warning(
                    "Context pressure ceiling reached during tool loop",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "tool_call_count": tool_call_count,
                            "step_name": ctx.step_definition.name,
                            "model": ctx.current_model,
                            "provider_id": current_provider_id,
                            "prompt_tokens": post_tool_snapshot.prompt_tokens,
                            "max_context_tokens": post_tool_snapshot.max_context_tokens,
                            "reserve_output_tokens": post_tool_snapshot.reserve_output_tokens,
                            "effective_reserve_output_tokens": (
                                post_tool_snapshot.effective_reserve_output_tokens
                            ),
                            "available_prompt_tokens": post_tool_snapshot.available_prompt_tokens,
                            "threshold_prompt_tokens": post_tool_snapshot.threshold_prompt_tokens,
                            "reason": post_tool_snapshot.reason,
                        }
                    },
                )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={
                            "event": "tool_call_context_pressure",
                            "tool_call_count": tool_call_count,
                            "step_name": ctx.step_definition.name,
                            **pressure_run.event_data(),
                        },
                    )
                )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={
                            "event": "system_notice",
                            "message": notice,
                            "turn_id": ctx.turn_id,
                            **pressure_run.event_data(),
                        },
                    )
                )
                await self._emit_compaction_notice(
                    ctx,
                    notice,
                    on_token=on_token,
                    persist=False,
                    metadata=pressure_run.event_data(),
                )
                await self._flush_events_incremental(
                    ctx,
                    events_to_record,
                    reason="tool_call_context_pressure",
                    on_token=on_token,
                )
                if ctx.policy.enable_auto_compaction:
                    pressure_run.phase = "mid_turn"
                    compaction_result = await self._auto_compact(
                        ctx,
                        run=pressure_run,
                        on_token=on_token,
                        trigger="tool_loop_pressure",
                        skip_few_events_check=True,
                    )
                    if compaction_result is not None and compaction_result.compacted:
                        new_session = await self._rotate_after_compaction(
                            ctx,
                            compaction_result,
                            trigger="tool_loop_pressure",
                            run=pressure_run,
                        )
                        if new_session is not None:
                            ctx.session = new_session
                            ctx.is_retry = True
                            ctx.prior_context = None
                            ctx.projection_state = None
                            ctx.compaction_recursion_depth += 1
                            ctx.timeout_continuation_message = (
                                "Internal controller recovery: context pressure became critical "
                                "after tool execution, so Cognis compacted the conversation into "
                                "this fresh session. Continue from the saved summary and recent "
                                "history. Do not redo completed tool work unless details are missing."
                            )
                            return await self._execute_step(
                                ctx,
                                on_token=on_token,
                                on_thinking=on_thinking,
                                on_tool_call=on_tool_call,
                                on_tool_result=on_tool_result,
                            )
                step_output = StepOutput(
                    summary=(
                        "Stopped because the step exceeded the context-pressure ceiling. "
                        "Partial work was preserved for evaluation."
                    ),
                    content="\n\n".join(assistant_content_parts),
                    outcome={
                        "status": "failed",
                        "reason": "Context pressure exceeded before completion.",
                    },
                    metadata={"context_pressure": pressure_run.event_data()},
                    attachments=list(collected_attachments),
                )
                break

            # Async orchestration spawned — end the parent turn after
            # processing the full tool batch.  The child runs in the
            # background and a follow-up turn will be triggered on completion.
            if async_orchestration_spawned:
                step_output = StepOutput(
                    summary="Async orchestration spawned — working in background.",
                    content="\n\n".join(assistant_content_parts),
                    attachments=list(collected_attachments),
                )
                break

            # Enforce the tool-call ceiling for non-secondary contexts only.
            # Secondary delegations use an OpenCode-style LLM iteration cap
            # (``steps``), so broad read/search batches do not prematurely end
            # the sub-session.
            if max_tool_calls is not None and tool_call_count >= max_tool_calls:
                logger.warning(
                    "Tool call limit reached",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "count": tool_call_count,
                            "is_delegation": _is_delegation,
                        }
                    },
                )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={
                            "event": "tool_call_ceiling_reached",
                            "tool_call_count": tool_call_count,
                            "max_tool_calls": max_tool_calls,
                            "step_name": ctx.step_definition.name,
                        },
                    )
                )
                await self._flush_events_incremental(
                    ctx, events_to_record, reason="tool_call_ceiling_reached", on_token=on_token
                )
                step_output = StepOutput(
                    summary=(
                        "Stopped after reaching the tool-call ceiling. "
                        "Partial work was preserved for evaluation."
                    ),
                    content="\n\n".join(assistant_content_parts),
                    metadata={
                        "interrupted": True,
                        "continuation_reason": "tool_call_ceiling_reached",
                        "tool_call_count": tool_call_count,
                        "max_tool_calls": max_tool_calls,
                        "pending_todos": _pending_todos_snapshot(ctx),
                    },
                    attachments=list(collected_attachments),
                )
                break

        # Finalize step — pass assistant_content_parts so Mnemory remember
        # works even when events were already flushed incrementally.
        events_recorded = await self._finalize_step(
            ctx,
            events_to_record,
            assistant_content_parts=assistant_content_parts,
            assistant_memory_parts=assistant_memory_parts,
        )

        # Automatic compaction: only rotate after the turn if pressure is still
        # unresolved after model-facing projection.  Raw cross-turn assembly may
        # recommend compaction for the unprojected transcript, but an effective
        # projection should not cause a visible session rotation.
        if events_recorded and _should_run_post_turn_auto_compaction(ctx, context_result):
            compaction_run = CompactionRunContext.from_context_result(
                context_result,
                trigger="post_turn_auto",
                reason="post_turn_recommendation",
            )
            compaction_result = await self._auto_compact(
                ctx,
                run=compaction_run,
                on_token=on_token,
            )
            if compaction_result is not None and compaction_result.compacted:
                new_session = await self._rotate_after_compaction(
                    ctx,
                    compaction_result,
                    trigger="post_turn_auto",
                    run=compaction_run,
                )
                if new_session is not None:
                    await self._emit_compaction_notice(
                        ctx,
                        "Session compacted. Previous context was summarized into a fresh session.",
                        on_token=on_token,
                        persist=False,
                        metadata=compaction_run.event_data(),
                    )

        step_status = (
            step_output.outcome.status
            if step_output is not None and step_output.outcome is not None
            else ("completed" if step_output else "failed")
        )
        if step_output and step_status != "failed":
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="completed").inc()
        else:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="failed").inc()

        ctx.pending_events = None
        ctx.pending_tool_calls.clear()

        # Post-step prune: walk back over recent tool results and mark
        # the older ones for clearance from the in-context view. The
        # tool output store keeps originals recoverable via
        # read_tool_output. This runs in the background so an empty or
        # transient cache failure cannot delay the user-visible step
        # completion.
        import contextlib as _contextlib  # local import keeps top-of-file lean

        with _contextlib.suppress(RuntimeError):
            # ``RuntimeError`` is raised when no event loop is running
            # (rare; e.g. a synchronous test harness). Silent best-effort.
            asyncio.create_task(self._prune_tool_outputs_after_step(ctx))
        return step_output

    # ------------------------------------------------------------------
    # Escalation blocking
    # ------------------------------------------------------------------

    async def _handle_escalation(
        self,
        result: ToolResult,
        tc: ToolCall,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_tool_result: ToolResultCallback | None,
    ) -> ToolResult:
        """If the tool was escalated, block and wait for user approval.

        On approval the tool is re-executed (Intaris auto-approves via its
        escalation retry cache).  On denial or timeout the escalation error
        is returned to the LLM.  Timeout does NOT submit a deny to Intaris
        so future similar calls are not prejudiced.
        """
        eval_meta = result.metadata.get("evaluation") if result.metadata else None
        if not eval_meta or eval_meta.get("decision") != "escalate":
            return result

        intaris_call_id = eval_meta.get("call_id")
        if not intaris_call_id:
            # No call_id from Intaris — cannot track, treat as denied
            return result

        conversation_id = ctx.conversation.conversation_id

        # Read escalation timeout from settings
        async with self.session_manager.session_factory() as db:
            timeout_raw: int = await get_setting_value(  # type: ignore[assignment]
                db, "session.escalation_timeout_seconds", 300
            )
        timeout_f = float(timeout_raw)

        # Create the escalation via the notification service.  For
        # task-originated escalations, the service resolves the target
        # to the task's source conversation so the user sees the
        # escalation in their chat, not in the invisible task step.
        # The intaris_call_id is used as notification_id so the existing
        # /escalations/{call_id}/resolve endpoint can find it.
        await self.notification_service.create(
            notification_type="escalation",
            user_email=ctx.session.user_email,
            conversation_id=conversation_id,
            task_id=ctx.task_id,
            session_id=ctx.session.session_id,
            notification_id=intaris_call_id,
            payload={
                "call_id": intaris_call_id,
                "tool_name": tc.name,
                "risk": eval_meta.get("risk"),
                "reasoning": eval_meta.get("reasoning"),
                "timeout_seconds": timeout_raw,
                "context": {
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                },
            },
        )
        pause_id = intaris_call_id

        # Send an interim tool_result to the WebSocket so the UI shows
        # the escalation status on the tool call block immediately.
        if on_tool_result:
            await on_tool_result(
                tc.call_id,
                tc.name,
                "Waiting for user approval...",
                True,
                None,
                {**eval_meta, "pending": True},
            )

        logger.info(
            "Escalation: waiting for user decision",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                    "timeout_seconds": timeout_raw,
                }
            },
        )

        # Block until resolved or Intaris records a decision through its own UI.
        try:
            resolution = await self._wait_for_escalation_resolution(pause_id, timeout=timeout_f)
        except TimeoutError:
            resolution = PauseResolution(decision="deny", data={"reason": "timeout"})
            await self.notification_service.mark_orphaned(intaris_call_id, reason="timeout")

        # Publish resolution event to all channel subscribers
        await self.event_bus.publish(
            Event(
                type=EventType.ESCALATION_RESOLVED,
                data={
                    "conversation_id": conversation_id,
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "decision": resolution.decision,
                    "reason": resolution.data.get("reason"),
                },
            )
        )

        if resolution.decision == "approve":
            logger.info(
                "Escalation approved — re-executing tool",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "call_id": intaris_call_id,
                        "tool_name": tc.name,
                    }
                },
            )
            # Re-execute: Intaris auto-approves via escalation retry (10 min)
            result = await self.tool_router.execute(
                tc.model_copy(
                    update={"runtime_metadata": self._tool_runtime_metadata_for_call(ctx, tc)}
                ),
                ctx.session,
                ctx.agent,
                self._get_classified_tool_registry(ctx, self._get_tool_registry(ctx)),
                self._get_executor(ctx),
            )
            self._record_execution_evidence(ctx, tool_name=tc.name, result=result)
            # If Intaris still escalates on retry (shouldn't happen), treat
            # as denied to avoid infinite loops.
            retry_eval = result.metadata.get("evaluation") if result.metadata else None
            if retry_eval and retry_eval.get("decision") == "escalate":
                logger.warning(
                    "Escalation retry still escalated — treating as denied",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "tool_name": tc.name,
                        }
                    },
                )
                return ToolResult(
                    output="Tool denied: approval could not be verified.",
                    is_error=True,
                    metadata=result.metadata,
                )
            return result

        # Denied by user or timed out
        if resolution.data.get("reason") == "timeout":
            reason = "Escalation timed out — no response received."
        else:
            note = resolution.data.get("note", "")
            reason = f"User denied the tool call. {note}".strip()

        logger.info(
            "Escalation denied",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "decision": resolution.decision,
                    "reason": resolution.data.get("reason"),
                }
            },
        )
        return ToolResult(
            output=reason,
            is_error=True,
            metadata=result.metadata,
        )

    async def _wait_for_escalation_resolution(
        self,
        pause_id: str,
        *,
        timeout: float,
    ) -> PauseResolution:
        """Wait for local approval while polling Intaris for external decisions."""
        deadline = monotonic() + timeout if timeout > 0 else None
        wait_task = asyncio.create_task(self.pause_waiter.wait(pause_id, timeout=timeout))
        try:
            while not wait_task.done():
                remaining = max(0.0, deadline - monotonic()) if deadline is not None else 2.0
                poll_delay = (
                    min(_INTARIS_ESCALATION_REMOTE_POLL_SECONDS, remaining)
                    if deadline is not None
                    else _INTARIS_ESCALATION_REMOTE_POLL_SECONDS
                )
                if poll_delay <= 0:
                    break
                done, _ = await asyncio.wait({wait_task}, timeout=poll_delay)
                if done:
                    break
                if await self.notification_service.reconcile_remote_escalation(pause_id):
                    break
            return await wait_task
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wait_task

    # ------------------------------------------------------------------
    # Orchestration tool dispatch
    # ------------------------------------------------------------------

    # Default fanout cap for parallel delegate calls in a single turn.
    # Mirrors OpenCode's "up to 3 explore agents in parallel" guidance —
    # high enough to deliver real wall-clock speedups, low enough to
    # avoid resource contention on the controller and child executors.
    _DELEGATE_PARALLEL_DEFAULT_MAX = 3

    async def _precompute_parallel_delegate_batches(
        self,
        ctx: StepContext,
        tool_calls: list[ToolCall],
        *,
        events_to_record: list[SessionEvent],
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> dict[int, ToolResult]:
        """Run consecutive ``delegate`` tool calls in parallel.

        When the model emits multiple ``delegate`` calls in a single
        assistant turn (e.g. fanning out to ``system:explore`` for a
        broad investigation), executing them sequentially defeats the
        whole point. This helper finds runs of consecutive delegate
        calls and runs each run via ``asyncio.gather``. Tool-call
        events are appended to the in-flight batch in input order; tool
        results land in the returned mapping keyed by index for the
        outer for-loop to surface.

        Single delegate calls (the common case) are left to the
        sequential path so we don't pay gather overhead unnecessarily.
        """

        results: dict[int, ToolResult] = {}
        i = 0
        n = len(tool_calls)
        max_concurrency = max(
            1,
            int(getattr(ctx, "_delegate_parallel_max", 0)) or self._DELEGATE_PARALLEL_DEFAULT_MAX,
        )
        while i < n:
            tc = tool_calls[i]
            if tc.name != "delegate":
                i += 1
                continue
            # Find the contiguous run of delegate calls starting at i.
            run_end = i + 1
            while run_end < n and tool_calls[run_end].name == "delegate":
                run_end += 1
            run = tool_calls[i:run_end]
            if len(run) <= 1:
                i = run_end
                continue

            self._raise_if_cancelled(ctx)

            # Record tool_call events for the whole batch before
            # spawning so the in-flight stream of events stays ordered.
            for child_tc in run:
                child_tool_id = _tool_id_for_call(child_tc.name, ctx.tool_registry)
                _append_tool_call_event(events_to_record, child_tc, child_tool_id)
            await self._flush_events_incremental(
                ctx,
                events_to_record,
                reason="tool_call:delegate_batch",
                on_token=on_token,
            )

            # Cap concurrency so a single turn cannot fan out
            # arbitrarily many child sessions at once.
            semaphore = asyncio.Semaphore(max_concurrency)

            async def _bounded(
                child_tc: ToolCall, sem: asyncio.Semaphore = semaphore
            ) -> ToolResult:
                async with sem:
                    try:
                        return await self._handle_orchestration_tool(
                            child_tc,
                            ctx=ctx,
                            events_to_record=events_to_record,
                            on_token=on_token,
                            on_tool_call=on_tool_call,
                            on_tool_result=on_tool_result,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Convert to a structured error so siblings still
                        # complete; the outer loop surfaces it via the
                        # normal tool_result path.
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": f"delegate failed: {exc!s}",
                                }
                            ),
                            is_error=True,
                        )

            gathered = await asyncio.gather(*(_bounded(child) for child in run))
            for offset, child_result in enumerate(gathered):
                results[i + offset] = child_result
            i = run_end
        return results

    async def _handle_orchestration_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> ToolResult:
        """Dispatch an orchestration tool call.

        Returns a ToolResult.  For async delegations the metadata includes
        ``delegation_spawned=True`` so the caller can end the parent turn.
        """
        # Check orchestration mode
        if ctx.orchestration_mode == OrchestrationMode.NONE:
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": (
                            "Orchestration tools are not available in sub-sessions. "
                            "Complete the task directly."
                        ),
                    }
                ),
                is_error=True,
            )

        # In DELEGATE_SYNC_ONLY mode, only delegate is allowed (and always sync)
        if ctx.orchestration_mode == OrchestrationMode.DELEGATE_SYNC_ONLY and tc.name != "delegate":
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": (
                            f"Tool '{tc.name}' is not available in task steps. "
                            "Only 'delegate' (sync) is available."
                        ),
                    }
                ),
                is_error=True,
            )

        validation_error = self._validate_controller_tool_arguments(tc.name, tc.arguments)
        if validation_error is not None:
            return ToolResult(
                output=json.dumps(validation_error.as_tool_result()),
                is_error=True,
            )

        # Dispatch by tool name
        if tc.name == "delegate":
            return await self._handle_delegate(
                tc,
                ctx=ctx,
                events_to_record=events_to_record,
                on_token=on_token,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
        elif is_subsession_tool(tc.name):
            return await self._handle_subsession_management(tc, ctx=ctx)
        elif is_managed_conversation_tool(tc.name):
            return await self._handle_managed_conversation_tool(tc, ctx=ctx)
        elif is_task_tool(tc.name):
            return await self._handle_task_tool(tc, ctx=ctx, events_to_record=events_to_record)
        elif is_composition_tool(tc.name):
            return await self._handle_composition_tool(
                tc, ctx=ctx, events_to_record=events_to_record
            )
        elif is_workflow_tool(tc.name):
            return await self._handle_workflow_tool(tc, ctx=ctx)
        else:
            return ToolResult(
                output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
                is_error=True,
            )

    async def _handle_delegate(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> ToolResult:
        """Handle the delegate tool — create and optionally run a child session."""
        started_at = asyncio.get_running_loop().time()
        task_description = tc.arguments.get("task", "")
        wait_provided = "wait" in tc.arguments
        conversation = getattr(ctx, "conversation", None)
        surface_policy = orchestration_surface_policy(getattr(conversation, "context", None))
        wait = bool(tc.arguments.get("wait", surface_policy.expose_delegate_wait_option is False))
        managed_agent_conversation = is_managed_agent_conversation_context(
            getattr(ctx.conversation, "context", None)
        )

        # In DELEGATE_SYNC_ONLY mode (task steps), force sync
        if ctx.orchestration_mode == OrchestrationMode.DELEGATE_SYNC_ONLY:
            wait = True
        elif wait_provided and wait is False and not surface_policy.allow_delegate_wait_false:
            payload = {
                "status": "error",
                "code": "delegate_async_not_allowed",
                "message": (
                    "delegate(wait=false) is not available in this conversation "
                    "context. Use joined delegate work or choose a valid "
                    "conversation-specific orchestration option."
                ),
            }
            return ToolResult(
                output=json.dumps(payload),
                is_error=True,
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
            )
        elif managed_agent_conversation:
            wait = True

        # Resolve agent registry for binding validation
        _agent_registry = None
        if hasattr(self.session_manager, "_session_factory"):
            from cognis.core.agent_registry import AgentRegistry

            _agent_registry = AgentRegistry(self.session_manager._session_factory)

        child_workspace_root = ctx.workspace_root if ctx.workspace_root_explicit else None
        child_working_directory = (
            ctx.working_directory if ctx.working_directory_explicit else child_workspace_root
        )

        result, child_session = await handle_delegate_tool_call(
            tc,
            session_manager=self.session_manager,
            session=ctx.session,
            agent=ctx.agent,
            agent_registry=_agent_registry,
            wait=wait,
            workspace_root=child_workspace_root,
            working_directory=child_working_directory,
        )

        if child_session is None:
            # Creation failed — record error event
            events_to_record.append(
                SessionEvent(
                    type="delegation",
                    data={
                        "status": "failed",
                        "mode": "delegate",
                        "call_id": tc.call_id,
                        "title": _delegation_title(tc.arguments),
                        "task_title": _delegation_title(tc.arguments),
                        "agent_id": tc.arguments.get("agent_id"),
                        "input_redacted": True,
                        "error": "Child session creation failed",
                    },
                )
            )
            return result

        parent_intaris_id = (
            getattr(ctx.session, "intaris_session_id", None) or ctx.session.session_id
        )
        child_intaris_id = (
            getattr(child_session, "intaris_session_id", None) or child_session.session_id
        )

        # Persist the started event immediately. Synchronous delegations can run
        # for minutes; buffering this in the parent batch makes completion arrive
        # first and causes stale history to regress completed cards to "started".
        started_event = SessionEvent(
            type="delegation",
            data={
                "status": "started",
                "mode": "delegate",
                "call_id": tc.call_id,
                "title": _delegation_title(tc.arguments),
                "task_title": _delegation_title(tc.arguments),
                "input_redacted": True,
                "agent_id": child_session.agent_id,
                "used_agent_id": child_session.agent_id,
                "agent_profile_id": getattr(child_session, "agent_profile_id", None),
                "child_session_id": child_session.session_id,
                "child_intaris_session_id": child_intaris_id,
                "wait": wait,
            },
        )
        try:
            await self._record_events_strict(
                ctx,
                [started_event],
                reason=f"delegation_started:{child_session.session_id}",
                on_token=on_token,
            )
        except Exception:
            logger.warning(
                "delegation: failed to record started event immediately",
                extra={"extra_data": {"child_session_id": child_session.session_id}},
                exc_info=True,
            )
            events_to_record.append(started_event)

        await self.event_bus.publish(
            Event(
                type=EventType.DELEGATION_STARTED,
                data={
                    "conversation_id": ctx.conversation.conversation_id,
                    "parent_session_id": ctx.session.session_id,
                    "turn_id": ctx.turn_id,
                    "child_session_id": child_session.session_id,
                    "mode": "delegate",
                    "agent_id": child_session.agent_id,
                    "used_agent_id": child_session.agent_id,
                    "agent_profile_id": getattr(child_session, "agent_profile_id", None),
                    "title": _delegation_title(tc.arguments),
                    "task_title": _delegation_title(tc.arguments),
                    "input_redacted": True,
                    "wait": wait,
                },
            )
        )

        if wait:
            # Synchronous delegation — await inline, return output as tool result.
            # Do NOT pass parent streaming callbacks to child sessions — child
            # tool calls should only be visible in the sub-session view, not
            # leak into the parent conversation timeline.
            DELEGATIONS_TOTAL.labels(status="sync_started").inc()

            # Build a progress-publishing on_tool_result callback.
            # Emits DELEGATION_PROGRESS after each child tool result so the UI
            # can show a live tool-call counter and last-tool name on the card.
            _child_tool_call_count = 0
            _child_session_id = child_session.session_id
            _event_bus = self.event_bus
            _conv_id = ctx.conversation.conversation_id

            async def _child_progress_callback(
                call_id: str,
                tool_name: str,
                *_rest: Any,
                **_kw: Any,
            ) -> None:
                # The on_tool_result callback signature varies across call sites
                # (5-8 positional args plus optional kwargs).  We only need
                # call_id and tool_name for progress reporting, so accept the rest
                # via *args/**kwargs to stay tolerant of signature drift.  The
                # child StepContext is not exposed by the generic callback, so
                # step_todo_write/list snapshots are parsed from the tool result
                # payload below.
                nonlocal _child_tool_call_count
                _child_tool_call_count += 1
                progress_todos: list[dict[str, Any]] = []
                result_content = _rest[0] if _rest and isinstance(_rest[0], str) else None
                if tool_name in {STEP_TODO_WRITE, STEP_TODO_LIST} and result_content:
                    with contextlib.suppress(Exception):
                        payload = json.loads(result_content)
                        raw_todos = payload.get("todos") if isinstance(payload, dict) else None
                        if isinstance(raw_todos, list):
                            progress_todos = _normalize_todos(
                                [item for item in raw_todos if isinstance(item, dict)]
                            )
                progress_due = (
                    _child_tool_call_count % 3 == 0
                    or _child_tool_call_count == 1
                    or tool_name in {STEP_TODO_WRITE, STEP_TODO_LIST}
                )
                if progress_due or progress_todos:
                    await _event_bus.publish(
                        Event(
                            type=EventType.DELEGATION_PROGRESS,
                            data={
                                "conversation_id": _conv_id,
                                "child_session_id": _child_session_id,
                                "tool_call_count": _child_tool_call_count,
                                "last_tool": tool_name,
                                "todos": progress_todos,
                            },
                        )
                    )

            output = await self._run_child_session(
                child_session=child_session,
                conversation=ctx.conversation,
                agent=ctx.executor_agent or ctx.agent,
                task_description=task_description,
                parent_intaris_session_id=parent_intaris_id,
                workspace_root=child_workspace_root,
                working_directory=child_working_directory,
                workspace_root_explicit=ctx.workspace_root_explicit,
                working_directory_explicit=ctx.working_directory_explicit,
                deliverable_step_run_id=ctx.step_run_id,
                on_tool_result=_child_progress_callback,
            )
            duration_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
            if output:
                if ctx.step_run_id is not None and output.deliverable_id is not None:
                    self._clear_cached_deliverable(ctx)
                # Prefer full content over summary for delegation results
                result_text = output.content if output.content else output.summary
                result_anchors = _anchors_from_delegation_content(result_text)
                result_sections = _result_sections_from_content(result_text, result_anchors)
                result_truncated = bool(result_text and "[Output truncated:" in result_text)
                payload: dict[str, Any] = {
                    "status": "completed",
                    "session_id": child_session.session_id,
                    "duration_ms": duration_ms,
                    "summary": output.summary,
                    "result": result_text,
                    "result_content": result_text,
                    "outputs": output.outputs,
                    "result_sections": result_sections,
                    "recovery_call_id": tc.call_id,
                    "deliverable_written": output.deliverable_id is not None,
                    "result_source": "deliverable"
                    if output.deliverable_id is not None
                    else "assistant_messages",
                    "result_truncated": result_truncated,
                    "result_anchors": result_anchors,
                }
                if output.deliverable_id is not None:
                    payload.update(
                        {
                            "deliverable_id": output.deliverable_id,
                            "deliverable_version": output.deliverable_version,
                            "deliverable_format": output.deliverable_format,
                            "deliverable_title": output.deliverable_title,
                        }
                    )
                return ToolResult(
                    output=json.dumps(payload, default=str),
                    metadata={
                        "orchestration": True,
                        "mode": "delegate",
                        "wait": True,
                        "stored_output": result_text,
                        "output_anchors": result_anchors,
                    },
                )
            else:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "failed",
                            "session_id": child_session.session_id,
                            "duration_ms": duration_ms,
                            "message": "Sub-session failed to complete.",
                        }
                    ),
                    is_error=True,
                    metadata={"orchestration": True, "mode": "delegate", "wait": True},
                )
        else:
            # Asynchronous delegation — spawn background task
            child_task = asyncio.create_task(
                self._run_child_session_async(
                    child_session=child_session,
                    conversation=ctx.conversation,
                    agent=ctx.executor_agent or ctx.agent,
                    task_description=task_description,
                    parent_intaris_session_id=parent_intaris_id,
                    workspace_root=child_workspace_root,
                    working_directory=child_working_directory,
                    workspace_root_explicit=ctx.workspace_root_explicit,
                    working_directory_explicit=ctx.working_directory_explicit,
                    deliverable_step_run_id=ctx.step_run_id,
                )
            )
            child_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            await self._track_child(ctx.session.session_id, child_session.session_id, child_task)
            DELEGATIONS_TOTAL.labels(status="spawned").inc()
            # Mark async orchestration in metadata so the caller ends the turn.
            result_with_flag = ToolResult(
                output=result.output,
                metadata={
                    **(result.metadata or {}),
                    "delegation_spawned": True,
                    "async_orchestration_spawned": True,
                },
            )
            return result_with_flag

    async def _handle_subsession_management(self, tc: ToolCall, *, ctx: StepContext) -> ToolResult:
        """Handle list_subsessions, get_subsession, cancel_subsession."""
        from cognis.store.queries import get_session_row, list_child_sessions

        parent_session_id = ctx.session.session_id

        if tc.name == "list_subsessions":
            status_filter = tc.arguments.get("status", "all")
            async with self.session_manager.session_factory() as db:
                children = await list_child_sessions(db, parent_session_id)
            items = []
            for child in children:
                if status_filter != "all" and child.status != status_filter:
                    continue
                items.append(
                    {
                        "session_id": child.session_id,
                        "agent_id": child.agent_id,
                        "status": child.status,
                        "task": child.delegation_task,
                        "result_summary": child.result_summary,
                        "started_at": str(child.started_at) if child.started_at else None,
                        "completed_at": str(child.completed_at) if child.completed_at else None,
                    }
                )
            return ToolResult(
                output=json.dumps({"subsessions": items, "count": len(items)}, default=str),
            )

        elif tc.name == "get_subsession":
            target_id = tc.arguments.get("session_id", "")
            async with self.session_manager.session_factory() as db:
                target_row = await get_session_row(db, target_id)
            if target_row is None or target_row.parent_session_id != parent_session_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Sub-session not found or not a child of this session.",
                        }
                    ),
                    is_error=True,
                )
            result_content = getattr(target_row, "result_content", None)
            result_anchors = _anchors_from_delegation_content(result_content)
            result_truncated = bool(result_content and "[Output truncated:" in result_content)
            result_sections = _result_sections_from_content(result_content, result_anchors)
            return ToolResult(
                output=json.dumps(
                    {
                        "session_id": target_row.session_id,
                        "agent_id": target_row.agent_id,
                        "status": target_row.status,
                        "task": target_row.delegation_task,
                        "result_summary": target_row.result_summary,
                        "result_content": result_content,
                        "result_content_source": "session" if result_content is not None else None,
                        "result_truncated": result_truncated,
                        "result_anchors": result_anchors,
                        "result_sections": result_sections,
                        "started_at": str(target_row.started_at) if target_row.started_at else None,
                        "completed_at": (
                            str(target_row.completed_at) if target_row.completed_at else None
                        ),
                    },
                    default=str,
                ),
            )

        elif tc.name == "cancel_subsession":
            cancel_id = tc.arguments.get("session_id", "")
            # Verify it's a child of this session
            async with self.session_manager.session_factory() as db:
                cancel_row = await get_session_row(db, cancel_id)
            if cancel_row is None or cancel_row.parent_session_id != parent_session_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Sub-session not found or not a child of this session.",
                        }
                    ),
                    is_error=True,
                )
            if cancel_row.status != "active":
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Sub-session is already {cancel_row.status}.",
                        }
                    ),
                    is_error=True,
                )
            # Cancel the asyncio task if tracked
            async with self._children_lock:
                active_children = self._active_children.get(parent_session_id, {})
                child_task = active_children.pop(cancel_id, None)
            if child_task and not child_task.done():
                child_task.cancel()
            # Mark as failed in DB + sync to Intaris
            await self.session_manager.mark_failed(
                cancel_id,
                result_summary="Cancelled by parent session",
            )
            return ToolResult(
                output=json.dumps({"status": "cancelled", "session_id": cancel_id}),
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    async def _handle_managed_conversation_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
    ) -> ToolResult:
        """Handle managed agent conversation control tools from interactive chats."""

        if (
            ctx.orchestration_mode != OrchestrationMode.FULL
            or ctx.task_id
            or ctx.session.parent_session_id
        ):
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": "Agent work tools are available only in interactive root chats.",
                    }
                ),
                is_error=True,
            )
        if self._turn_scheduler is None:
            return ToolResult(
                output=json.dumps({"status": "error", "message": "Turn scheduler is unavailable."}),
                is_error=True,
            )

        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store import queries

        user_email = ctx.session.user_email
        controller_conversation_id = ctx.conversation.conversation_id
        controller_session_id = ctx.session.session_id

        def _row_payload(row: Any) -> dict[str, Any]:
            return {
                "link_id": row.link_id,
                "conversation_id": row.target_conversation_id,
                "session_id": row.target_session_id,
                "agent_id": row.target_agent_id,
                "title": row.title,
                "conversation_state": row.conversation_state,
                "turn_state": row.turn_state,
                "active_turn_id": row.active_turn_id,
                "controller_agent_id": row.controller_agent_id,
                "controller_conversation_id": row.controller_conversation_id,
                "controller_session_id": row.controller_session_id,
                "last_result_summary": row.last_result_summary,
                "last_error": row.last_error,
                "created_at": str(row.created_at) if row.created_at else None,
                "updated_at": str(row.updated_at) if row.updated_at else None,
                "completed_at": str(row.completed_at) if row.completed_at else None,
                "closed_at": str(row.closed_at) if row.closed_at else None,
            }

        async def _get_link_for_target(conversation_id: str) -> Any | None:
            async with self.session_manager.session_factory() as db:
                return await queries.get_managed_conversation_link_for_target(
                    db,
                    conversation_id,
                    user_email=user_email,
                )

        async def _mark_target_read(conversation_id: str) -> None:
            async with self.session_manager.session_factory() as db:
                await queries.mark_conversation_read(db, conversation_id)
                await db.commit()

        def _chat_mode_arg() -> str:
            return normalize_chat_mode(tc.arguments.get("chat_mode"), default="default")

        def _managed_error_turn_state(error: Any) -> str:
            return (
                "interrupted"
                if getattr(error, "code", None) in {"cancelled", "turn_cancelled"}
                else "failed"
            )

        _WAIT_UNSET = object()

        self_outer = self

        class _AgentWorkQueuedTurnObserver:
            supports_mid_turn_absorb = True

            def __init__(self, link_id: str, notify: bool) -> None:
                self._link_id = link_id
                self._notify = notify

            async def on_turn_complete(self, result: Any) -> None:
                async with self_outer.session_manager.session_factory() as db:
                    await queries.update_managed_conversation_link(
                        db,
                        self._link_id,
                        conversation_state="open",
                        turn_state="running",
                        active_turn_id=result.turn_id,
                        notify_on_completion=self._notify,
                        last_error=None,
                    )
                    await db.commit()

            async def on_turn_error(self, conversation_id: str, error: Any) -> None:
                interrupted = getattr(error, "code", None) in {"cancelled", "turn_cancelled"}
                async with self_outer.session_manager.session_factory() as db:
                    await queries.update_managed_conversation_link(
                        db,
                        self._link_id,
                        conversation_state="open",
                        turn_state="interrupted" if interrupted else "failed",
                        clear_active_turn_id=True,
                        notify_on_completion=self._notify,
                        last_error=getattr(error, "message", str(error)),
                    )
                    await db.commit()

            def __getattr__(self, name: str) -> Any:
                if name.startswith("on_"):

                    async def _noop(*args: Any, **kwargs: Any) -> None:
                        return None

                    return _noop
                raise AttributeError(name)

        async def _last_user_message_for_retry(link: Any) -> str | None:
            return await last_managed_conversation_user_message_for_retry(
                session_cache=self.session_cache,
                guardrails=self.providers.guardrails,
                session_factory=self.session_manager.session_factory,
                link=link,
            )

        async def _require_link(conversation_id: str) -> tuple[Any | None, ToolResult | None]:
            link = await _get_link_for_target(conversation_id)
            if link is None or link.controller_conversation_id != controller_conversation_id:
                return None, ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Agent work conversation not found for this controller chat.",
                        }
                    ),
                    is_error=True,
                )
            await _mark_target_read(conversation_id)
            return link, None

        async def _require_open_link(conversation_id: str) -> tuple[Any | None, ToolResult | None]:
            link, err = await _require_link(conversation_id)
            if err is not None:
                return None, err
            if link.conversation_state == "closed":
                return None, ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Agent work is closed. Fork it to continue.",
                        }
                    ),
                    is_error=True,
                )
            return link, None

        async def _wait_payload(
            conversation_id: str,
            timeout_seconds: int | None,
            settled: Any = _WAIT_UNSET,
        ) -> dict[str, Any]:
            if settled is _WAIT_UNSET:
                waited = await self._turn_scheduler.wait_for_turn(
                    conversation_id,
                    timeout_seconds=timeout_seconds,
                )
            else:
                waited = settled
            link = await _get_link_for_target(conversation_id)
            await _mark_target_read(conversation_id)
            active_status = "idle"
            if link is not None and link.conversation_state == "open":
                if link.turn_state in {"queued", "running"}:
                    active_status = link.turn_state
                elif link.active_turn_id:
                    active_status = "running"
            status = "completed"
            if waited is None:
                status = active_status
            payload: dict[str, Any] = {
                "status": status,
                "waited": waited is not None,
                "conversation": _row_payload(link) if link is not None else None,
            }
            if waited is not None:
                if getattr(waited, "code", None) is not None:
                    payload["status"] = (
                        "interrupted"
                        if _managed_error_turn_state(waited) == "interrupted"
                        else "error"
                    )
                    payload["error"] = {
                        "code": waited.code,
                        "message": waited.message,
                        "recoverable": waited.recoverable,
                    }
                elif getattr(waited, "partial", False) and (
                    getattr(waited, "finish_reason", None) == "user_cancelled"
                ):
                    payload["status"] = "interrupted"
                    payload["error"] = {
                        "code": "turn_cancelled",
                        "message": "The current turn was cancelled.",
                        "recoverable": True,
                    }
                    payload["turn"] = {
                        "conversation_id": waited.conversation_id,
                        "session_id": waited.session_id,
                        "turn_id": waited.turn_id,
                        "final_content": waited.final_content,
                        "delegated": waited.delegated,
                        "task_id": waited.task_id,
                    }
                else:
                    payload["turn"] = {
                        "conversation_id": waited.conversation_id,
                        "session_id": waited.session_id,
                        "turn_id": waited.turn_id,
                        "final_content": waited.final_content,
                        "delegated": waited.delegated,
                        "task_id": waited.task_id,
                    }
            return payload

        if tc.name == "agent_conversation_create":
            agent_id = str(tc.arguments.get("agent_id") or "").strip()
            try:
                agent_profile_id = normalize_agent_profile_id(tc.arguments.get("agent_profile_id"))
            except ValueError as exc:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": str(exc)}),
                    is_error=True,
                )
            title = str(tc.arguments.get("title") or "").strip()
            initial_message = str(tc.arguments.get("initial_message") or "").strip()
            wait = bool(tc.arguments.get("wait", False))
            if not agent_id or not title or not initial_message:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "agent_id, title and initial_message are required.",
                        }
                    ),
                    is_error=True,
                )
            try:
                target_agent = await self._resolve_child_agent(
                    agent_id,
                    ctx.agent,
                    user_email=user_email,
                )
            except Exception as exc:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": str(exc)}),
                    is_error=True,
                )
            if agent_profile_id is not None:
                try:
                    resolve_agent_profile(target_agent, agent_profile_id, source="managed_explicit")
                except ValueError as exc:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": str(exc)}),
                        is_error=True,
                    )
            managed_context = ConversationContext(
                type="agent_work",
                ref=controller_conversation_id,
                platform_data={
                    "kind": "agent_work",
                    "controller_agent_id": ctx.agent.agent_id,
                    "controller_conversation_id": controller_conversation_id,
                    "controller_session_id": controller_session_id,
                    "target_agent_id": target_agent.agent_id,
                    "target_agent_profile_id": agent_profile_id,
                    "provenance_in_prefix": True,
                },
                memory_labels={},
            )
            (
                conversation,
                session,
            ) = await self.session_manager.create_conversation_with_root_session(
                user_email=user_email,
                agent_id=target_agent.agent_id,
                agent_profile_id=agent_profile_id,
                context=managed_context,
                title=title,
                title_source="managed_agent",
                intention=title,
                project_id=ctx.conversation.project_id,
            )
            async with self.session_manager.session_factory() as db:
                link = await queries.create_managed_conversation_link(
                    db,
                    user_email=user_email,
                    controller_agent_id=ctx.agent.agent_id,
                    controller_conversation_id=controller_conversation_id,
                    controller_session_id=controller_session_id,
                    target_agent_id=target_agent.agent_id,
                    target_agent_profile_id=agent_profile_id,
                    target_conversation_id=conversation.conversation_id,
                    target_session_id=session.session_id,
                    title=title,
                    turn_state="running",
                    notify_on_completion=not wait,
                )
                await queries.update_conversation_context_data(
                    db,
                    conversation.conversation_id,
                    context_data={
                        **dict(managed_context.platform_data),
                        "link_id": link.link_id,
                    },
                )
                await db.commit()
            await self._record_agent_work_context(
                session=session,
                controller_agent_id=ctx.agent.agent_id,
                controller_conversation_id=controller_conversation_id,
                controller_session_id=controller_session_id,
                target_agent_id=target_agent.agent_id,
            )
            await self.event_bus.publish(
                Event(
                    type=EventType.CONVERSATION_UPDATED,
                    data={
                        "conversation_id": controller_conversation_id,
                        "created_conversation_id": conversation.conversation_id,
                    },
                )
            )
            error = await self._turn_scheduler.submit_turn(
                conversation.conversation_id,
                initial_message,
                user_email=user_email,
                one_shot_chat_mode=_chat_mode_arg(),
            )
            active_turn_id = self._turn_scheduler.active_turn_id(conversation.conversation_id)
            if error is not None or active_turn_id is not None:
                async with self.session_manager.session_factory() as db:
                    link = await queries.update_managed_conversation_link(
                        db,
                        link.link_id,
                        turn_state=_managed_error_turn_state(error)
                        if error is not None
                        else "running",
                        active_turn_id=active_turn_id,
                        last_error=error.message if error is not None else None,
                    )
                    await db.commit()
            if error is not None:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "conversation": _row_payload(link),
                            "error": {
                                "code": error.code,
                                "message": error.message,
                                "recoverable": error.recoverable,
                            },
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            if wait:
                payload = await _wait_payload(conversation.conversation_id, None)
                payload["created"] = True
                return ToolResult(output=json.dumps(payload, default=str))
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "accepted",
                        "conversation": _row_payload(link),
                        "message": "Agent work conversation created and running.",
                    },
                    default=str,
                ),
                metadata={
                    "orchestration": True,
                    "mode": "managed_conversation",
                    "async_orchestration_spawned": True,
                },
            )

        if tc.name == "agent_conversation_send":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_open_link(conversation_id)
            if err is not None:
                return err
            message = str(tc.arguments.get("message") or "").strip()
            if not message:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Message is required. Use agent_conversation_retry to retry a failed turn.",
                            "conversation": _row_payload(link),
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            wait = bool(tc.arguments.get("wait", False))
            was_active = self._turn_scheduler.has_active_turn(conversation_id)
            queued_completion: asyncio.Future[Any] | None = None
            turn_observers: tuple[Any, ...] = ()
            if was_active:
                queued_completion = asyncio.get_running_loop().create_future()
                async with self.session_manager.session_factory() as db:
                    link = await queries.update_managed_conversation_link(
                        db,
                        link.link_id,
                        conversation_state="open",
                        turn_state="queued",
                        last_error=None,
                    )
                    await db.commit()

                class _QueuedSendObserver(_AgentWorkQueuedTurnObserver):
                    async def on_turn_complete(self, result: Any) -> None:
                        await super().on_turn_complete(result)
                        if queued_completion is not None and not queued_completion.done():
                            queued_completion.set_result(result)

                    async def on_turn_error(self, conversation_id: str, error: Any) -> None:
                        await super().on_turn_error(conversation_id, error)
                        if queued_completion is not None and not queued_completion.done():
                            queued_completion.set_result(error)

                turn_observers = (_QueuedSendObserver(link.link_id, not wait),)
            else:
                async with self.session_manager.session_factory() as db:
                    link = await queries.update_managed_conversation_link(
                        db,
                        link.link_id,
                        conversation_state="open",
                        turn_state="running",
                        notify_on_completion=not wait,
                        last_error=None,
                    )
                    await db.commit()
            error = await self._turn_scheduler.submit_turn(
                conversation_id,
                message,
                user_email=user_email,
                one_shot_chat_mode=_chat_mode_arg(),
                turn_observers=turn_observers,
            )
            active_turn_id = self._turn_scheduler.active_turn_id(conversation_id)
            if error is not None or active_turn_id is not None:
                async with self.session_manager.session_factory() as db:
                    link = await queries.update_managed_conversation_link(
                        db,
                        link.link_id,
                        conversation_state="open",
                        turn_state=_managed_error_turn_state(error)
                        if error is not None
                        else "running",
                        active_turn_id=active_turn_id,
                        last_error=error.message if error is not None else None,
                    )
                    await db.commit()
            if error is not None:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "conversation": _row_payload(link),
                            "error": {
                                "code": error.code,
                                "message": error.message,
                                "recoverable": error.recoverable,
                            },
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            if wait:
                if queued_completion is not None:
                    queued_result = await queued_completion
                    return ToolResult(
                        output=json.dumps(
                            await _wait_payload(conversation_id, None, settled=queued_result),
                            default=str,
                        )
                    )
                return ToolResult(
                    output=json.dumps(await _wait_payload(conversation_id, None), default=str)
                )
            return ToolResult(
                output=json.dumps(
                    {"status": "accepted", "conversation": _row_payload(link)},
                    default=str,
                ),
                metadata={
                    "orchestration": True,
                    "mode": "managed_conversation",
                    "async_orchestration_spawned": True,
                },
            )

        if tc.name == "agent_conversation_retry":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_open_link(conversation_id)
            if err is not None:
                return err
            if link.turn_state not in {"failed", "interrupted"}:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": (
                                "Agent work retry is only available after a failed or interrupted turn. "
                                "Use agent_conversation_send for new instructions or clarification."
                            ),
                            "conversation": _row_payload(link),
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            if self._turn_scheduler.has_active_turn(conversation_id):
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "running",
                            "message": (
                                "Agent work already has an active turn. "
                                "Use agent_conversation_wait before retrying."
                            ),
                            "conversation": _row_payload(link),
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            message = await _last_user_message_for_retry(link)
            if not message:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "No previous user message is available to retry.",
                            "conversation": _row_payload(link),
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            wait = bool(tc.arguments.get("wait", False))
            async with self.session_manager.session_factory() as db:
                link = await queries.update_managed_conversation_link(
                    db,
                    link.link_id,
                    conversation_state="open",
                    turn_state="running",
                    notify_on_completion=not wait,
                    last_error=None,
                )
                await db.commit()
            error = await self._turn_scheduler.submit_turn(
                conversation_id,
                message,
                user_email=user_email,
            )
            active_turn_id = self._turn_scheduler.active_turn_id(conversation_id)
            if error is not None or active_turn_id is not None:
                async with self.session_manager.session_factory() as db:
                    link = await queries.update_managed_conversation_link(
                        db,
                        link.link_id,
                        conversation_state="open",
                        turn_state=_managed_error_turn_state(error)
                        if error is not None
                        else "running",
                        active_turn_id=active_turn_id,
                        last_error=error.message if error is not None else None,
                    )
                    await db.commit()
            if error is not None:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "conversation": _row_payload(link),
                            "error": {
                                "code": error.code,
                                "message": error.message,
                                "recoverable": error.recoverable,
                            },
                        },
                        default=str,
                    ),
                    is_error=True,
                )
            if wait:
                return ToolResult(
                    output=json.dumps(await _wait_payload(conversation_id, None), default=str)
                )
            return ToolResult(
                output=json.dumps(
                    {"status": "accepted", "conversation": _row_payload(link)},
                    default=str,
                ),
                metadata={
                    "orchestration": True,
                    "mode": "managed_conversation",
                    "async_orchestration_spawned": True,
                },
            )

        if tc.name == "agent_conversation_wait":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            _link, err = await _require_link(conversation_id)
            if err is not None:
                return err
            timeout = tc.arguments.get("timeout_seconds")
            timeout_seconds = int(timeout) if isinstance(timeout, int) and timeout > 0 else None
            return ToolResult(
                output=json.dumps(
                    await _wait_payload(conversation_id, timeout_seconds),
                    default=str,
                )
            )

        if tc.name == "agent_conversation_interrupt":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_link(conversation_id)
            if err is not None:
                return err
            reason = str(tc.arguments.get("reason") or "Interrupted by supervising agent")
            cancelled = await self._turn_scheduler.cancel_turn(conversation_id)
            async with self.session_manager.session_factory() as db:
                link = await queries.update_managed_conversation_link(
                    db,
                    link.link_id,
                    conversation_state="open",
                    turn_state="interrupted" if cancelled else "idle",
                    clear_active_turn_id=True,
                    notify_on_completion=False,
                    last_error=reason if cancelled else None,
                )
                await db.commit()
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "interrupted" if cancelled else "idle",
                        "conversation": _row_payload(link),
                    },
                    default=str,
                )
            )

        if tc.name == "agent_conversation_close":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_link(conversation_id)
            if err is not None:
                return err
            reason = str(tc.arguments.get("reason") or "Closed by supervising agent")
            await self._turn_scheduler.cancel_turn(conversation_id)
            async with self.session_manager.session_factory() as db:
                link = await queries.update_managed_conversation_link(
                    db,
                    link.link_id,
                    conversation_state="closed",
                    turn_state="idle",
                    clear_active_turn_id=True,
                    notify_on_completion=False,
                    last_error=reason,
                    closed=True,
                )
                await db.commit()
            return ToolResult(
                output=json.dumps(
                    {"status": "closed", "conversation": _row_payload(link)}, default=str
                )
            )

        if tc.name == "agent_conversation_list":
            status = tc.arguments.get("status")
            limit = tc.arguments.get("limit", 25)
            async with self.session_manager.session_factory() as db:
                links = await queries.list_managed_conversation_links(
                    db,
                    user_email=user_email,
                    controller_conversation_id=controller_conversation_id,
                    status=str(status) if isinstance(status, str) else None,
                    limit=int(limit) if isinstance(limit, int) else 25,
                )
            return ToolResult(
                output=json.dumps(
                    {"count": len(links), "conversations": [_row_payload(row) for row in links]},
                    default=str,
                )
            )

        if tc.name == "agent_conversation_get":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_link(conversation_id)
            if err is not None:
                return err
            return ToolResult(
                output=json.dumps({"status": "ok", "conversation": _row_payload(link)}, default=str)
            )

        if tc.name == "agent_conversation_fork":
            conversation_id = str(tc.arguments.get("conversation_id") or "").strip()
            link, err = await _require_link(conversation_id)
            if err is not None:
                return err
            async with self.session_manager.session_factory() as db:
                conversation_row = await queries.get_conversation(db, conversation_id)
                session_row = (
                    await queries.get_session_row(db, link.target_session_id)
                    if link.target_session_id
                    else None
                )
                agent_row = await queries.get_agent(db, link.target_agent_id)
            if conversation_row is None or session_row is None or agent_row is None:
                return ToolResult(
                    output=json.dumps(
                        {"status": "error", "message": "Agent work runtime not found."}
                    ),
                    is_error=True,
                )
            target_agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
            checkpoint = self._turn_scheduler.active_turn_checkpoint(conversation_id)
            fork_context = ConversationContext(
                type="agent_work",
                ref=controller_conversation_id,
                platform_data={
                    "kind": "agent_work",
                    "controller_agent_id": ctx.agent.agent_id,
                    "controller_conversation_id": controller_conversation_id,
                    "controller_session_id": controller_session_id,
                    "target_agent_id": target_agent.agent_id,
                    "provenance_in_prefix": True,
                },
                memory_labels=dict(conversation_row.memory_labels or {}),
            )
            fork_method = (
                getattr(
                    self.session_manager, "fork_active_turn_checkpoint_into_new_conversation", None
                )
                if checkpoint is not None
                else None
            )
            if fork_method is not None:
                new_conversation, new_session, copied = await fork_method(
                    source_session=_to_session_model(session_row),
                    source_conversation=_to_conversation_model(conversation_row),
                    agent=target_agent,
                    user_email=user_email,
                    active_turn_id=checkpoint.get("turn_id"),
                    title=f"Fork: {link.title}" if link.title else "Agent work fork",
                    intention=f"Forked agent work with {target_agent.name}",
                    context=fork_context,
                    snapshot_extras={"trigger": "agent_conversation_fork"},
                )
            else:
                (
                    new_conversation,
                    new_session,
                    copied,
                ) = await self.session_manager.fork_into_new_conversation(
                    source_session=_to_session_model(session_row),
                    source_conversation=_to_conversation_model(conversation_row),
                    agent=target_agent,
                    user_email=user_email,
                    title=f"Fork: {link.title}" if link.title else "Agent work fork",
                    intention=f"Forked agent work with {target_agent.name}",
                    context=fork_context,
                    snapshot_extras={"trigger": "agent_conversation_fork"},
                )
            if not copied:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": "Fork copy failed."}),
                    is_error=True,
                )
            async with self.session_manager.session_factory() as db:
                new_link = await queries.create_managed_conversation_link(
                    db,
                    user_email=user_email,
                    controller_agent_id=ctx.agent.agent_id,
                    controller_conversation_id=controller_conversation_id,
                    controller_session_id=controller_session_id,
                    target_agent_id=target_agent.agent_id,
                    target_conversation_id=new_conversation.conversation_id,
                    target_session_id=new_session.session_id,
                    title=new_conversation.title or "Agent work fork",
                )
                await queries.update_conversation_context_data(
                    db,
                    new_conversation.conversation_id,
                    context_data={
                        **dict(fork_context.platform_data),
                        "link_id": new_link.link_id,
                    },
                )
                await db.commit()
            await self._record_agent_work_context(
                session=new_session,
                controller_agent_id=ctx.agent.agent_id,
                controller_conversation_id=controller_conversation_id,
                controller_session_id=controller_session_id,
                target_agent_id=target_agent.agent_id,
            )
            await self.event_bus.publish(
                Event(
                    type=EventType.CONVERSATION_UPDATED,
                    data={
                        "conversation_id": controller_conversation_id,
                        "created_conversation_id": new_conversation.conversation_id,
                    },
                )
            )
            message = str(tc.arguments.get("message") or "").strip()
            started_async_turn = False
            if message:
                error = await self._turn_scheduler.submit_turn(
                    new_conversation.conversation_id,
                    message,
                    user_email=user_email,
                    one_shot_chat_mode=_chat_mode_arg(),
                )
                async with self.session_manager.session_factory() as db:
                    new_link = await queries.update_managed_conversation_link(
                        db,
                        new_link.link_id,
                        turn_state=_managed_error_turn_state(error)
                        if error is not None
                        else "running",
                        active_turn_id=self._turn_scheduler.active_turn_id(
                            new_conversation.conversation_id
                        ),
                        notify_on_completion=not bool(tc.arguments.get("wait", False)),
                        last_error=error.message if error is not None else None,
                    )
                    await db.commit()
                if error is not None:
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "conversation": _row_payload(new_link),
                                "error": {
                                    "code": error.code,
                                    "message": error.message,
                                    "recoverable": error.recoverable,
                                },
                            },
                            default=str,
                        ),
                        is_error=True,
                    )
                if bool(tc.arguments.get("wait", False)):
                    return ToolResult(
                        output=json.dumps(
                            await _wait_payload(new_conversation.conversation_id, None),
                            default=str,
                        )
                    )
                started_async_turn = True
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "forked",
                        "conversation": _row_payload(new_link),
                        "copied": copied,
                    },
                    default=str,
                ),
                metadata={
                    "orchestration": True,
                    "mode": "managed_conversation",
                    "async_orchestration_spawned": True,
                }
                if started_async_turn
                else None,
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    async def _handle_task_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
    ) -> ToolResult:
        """Handle task management tools in main-chat contexts."""
        from cognis.core.management import (
            resolve_task_pause_action,
            respond_task_input,
            task_pending_pause_response,
            task_workflow_run_response,
        )
        from cognis.store.queries import (
            get_active_project_grant,
            get_agent,
            get_project,
            get_session_row,
            get_task,
            list_step_runs_for_task,
            list_tasks_for_agent,
            update_task_fields,
            update_task_status,
        )

        if tc.name == "create_task":
            task_queue = self._task_queue
            if task_queue is None:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Task queue is not available.",
                        }
                    ),
                    is_error=True,
                )
            try:
                from cognis.core.workflow_management import get_workflow_for_user
                from cognis.models.task import TaskDelivery
                from cognis.models.workflow import SessionPolicy

                # Resolve agent_id: LLMs sometimes pass "self" literally
                # instead of omitting the field.  Fall back to the current
                # agent to avoid FK violations.
                raw_agent_id = tc.arguments.get("agent_id")
                if not raw_agent_id or raw_agent_id == "self":
                    raw_agent_id = ctx.agent.agent_id

                async with self.session_manager.session_factory() as db:
                    agent_row = await get_agent(db, str(raw_agent_id))
                if agent_row is None or agent_row.owner_email != ctx.session.user_email:
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": "Agent not found or not accessible.",
                            }
                        ),
                        is_error=True,
                    )
                try:
                    task_agent_profile_id = normalize_agent_profile_id(
                        tc.arguments.get("agent_profile_id")
                    )
                except ValueError as exc:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": str(exc)}),
                        is_error=True,
                    )
                if task_agent_profile_id is not None:
                    from cognis.core.agent_registry import _row_to_definition

                    try:
                        resolve_agent_profile(
                            _row_to_definition(agent_row),
                            task_agent_profile_id,
                            source="task_explicit",
                        )
                    except ValueError as exc:
                        return ToolResult(
                            output=json.dumps({"status": "error", "message": str(exc)}),
                            is_error=True,
                        )

                workflow_id = tc.arguments.get("workflow_id")
                if isinstance(workflow_id, str) and not workflow_id.strip():
                    workflow_id = None
                project_id = tc.arguments.get("project_id") or ctx.conversation.project_id
                if isinstance(project_id, str) and not project_id.strip():
                    project_id = None
                if project_id:
                    async with self.session_manager.session_factory() as db:
                        project = await get_project(db, str(project_id))
                        project_access = (
                            project is not None
                            and (project.status == "active")
                            and (
                                project.owner_email == ctx.session.user_email
                                or await get_active_project_grant(
                                    db, str(project_id), ctx.session.user_email
                                )
                                is not None
                            )
                        )
                    if not project_access:
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Project not found or not accessible.",
                                }
                            ),
                            is_error=True,
                        )
                if workflow_id:
                    workflow = await get_workflow_for_user(
                        workflow_registry=_workflow_registry_for_agent_loop(self),
                        workflow_id=str(workflow_id),
                        owner_email=ctx.session.user_email,
                        project_id=str(project_id) if project_id else None,
                    )
                    if workflow is None:
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Workflow not found or not accessible.",
                                }
                            ),
                            is_error=True,
                        )

                requested_status = str(tc.arguments.get("status") or "queued").lower()
                if tc.arguments.get("draft") is True:
                    requested_status = "draft"
                if requested_status not in {"draft", "queued"}:
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": "Task status must be 'draft' or 'queued'.",
                            }
                        ),
                        is_error=True,
                    )
                create_method = (
                    task_queue.create_draft if requested_status == "draft" else task_queue.submit
                )
                task = await create_method(
                    created_by=ctx.session.user_email,
                    agent_id=raw_agent_id,
                    agent_profile_id=task_agent_profile_id,
                    title=tc.arguments.get("title", "Untitled task"),
                    description=tc.arguments.get("description", ""),
                    expected_output=tc.arguments.get("expected_output"),
                    priority=tc.arguments.get("priority", 0),
                    created_by_agent_id=ctx.agent.agent_id,
                    source_type="agent",
                    source_ref=ctx.conversation.conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    interaction_mode_override=tc.arguments.get("interaction_mode_override"),
                    session_policy=SessionPolicy.model_validate(
                        tc.arguments.get("session_policy") or {}
                    ),
                    workflow_id=workflow_id,
                    project_id=str(project_id) if project_id else None,
                    workspace_root=ctx.workspace_root,
                    working_directory=ctx.working_directory,
                )

                # Record delegation event so the card appears in session
                # history on page refresh.
                events_to_record.append(
                    SessionEvent(
                        type="delegation",
                        data={
                            "mode": "task",
                            "call_id": tc.call_id,
                            "task": task.title,
                            "agent_profile_id": task.agent_profile_id,
                            "child_session_id": task.task_id,
                            "status": "started",
                        },
                    )
                )

                # Publish event for real-time WebSocket delivery so the
                # delegation card appears immediately in the UI.
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_STARTED,
                        data={
                            "conversation_id": ctx.conversation.conversation_id,
                            "parent_session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            "child_session_id": task.task_id,
                            "mode": "task",
                            "agent_id": task.agent_id,
                            "agent_profile_id": task.agent_profile_id,
                            "task": task.title,
                        },
                    )
                )

                return ToolResult(
                    output=json.dumps(
                        {
                            "status": requested_status,
                            "task_id": task.task_id,
                            "title": task.title,
                            "agent_profile_id": task.agent_profile_id,
                            "message": (
                                "Task created as a draft. Submit it to start execution."
                                if requested_status == "draft"
                                else "Task created and queued for execution."
                            ),
                        }
                    ),
                    metadata={
                        "orchestration": True,
                        "mode": "task",
                        "async_orchestration_spawned": True,
                    }
                    if requested_status == "queued"
                    else None,
                )
            except Exception as exc:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Failed to create task: {exc}",
                        }
                    ),
                    is_error=True,
                )

        elif tc.name == "list_tasks":
            status_filter = tc.arguments.get("status", "all")
            statuses = None if status_filter == "all" else [status_filter]
            async with self.session_manager.session_factory() as db:
                tasks = await list_tasks_for_agent(db, ctx.agent.agent_id, statuses=statuses)
            project_filter = tc.arguments.get("project_id")
            items = []
            for t in tasks:
                if project_filter and getattr(t, "project_id", None) != project_filter:
                    continue
                items.append(
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "status": t.status,
                        "priority": t.priority,
                        "workflow_id": t.workflow_id,
                        "project_id": getattr(t, "project_id", None),
                        "created_at": str(t.created_at) if t.created_at else None,
                        "result_summary": t.result_summary,
                    }
                )
            return ToolResult(
                output=json.dumps({"tasks": items, "count": len(items)}, default=str),
            )

        elif tc.name == "get_task":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": "Task not found."}),
                        is_error=True,
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps(
                            {"status": "error", "message": "Task belongs to a different agent."}
                        ),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            task_model = _task_row_to_model(task_row)
            step_run_summaries = [
                {
                    "step_run_id": getattr(sr, "step_run_id", None),
                    "step_name": sr.step_name,
                    "status": sr.status,
                    "attempt": sr.attempt,
                    "session_id": getattr(sr, "session_id", None),
                    "conversation_id": getattr(sr, "conversation_id", None),
                    "started_at": str(getattr(sr, "started_at", None))
                    if getattr(sr, "started_at", None)
                    else None,
                    "completed_at": str(getattr(sr, "completed_at", None))
                    if getattr(sr, "completed_at", None)
                    else None,
                    "summary": (sr.output or {}).get("summary", "") if sr.output else "",
                    "evaluation_decision": (
                        (sr.evaluation or {}).get("decision")
                        if getattr(sr, "evaluation", None)
                        else None
                    ),
                    "has_output": bool(getattr(sr, "output", None)),
                    "has_logs": bool(getattr(sr, "session_id", None)),
                }
                for sr in step_rows
            ]
            pending_pause = task_pending_pause_response(self.pause_waiter, task_model)
            workflow_run = await task_workflow_run_response(
                task_model,
                workflow_registry=_workflow_registry_for_agent_loop(self),
                pending_pause=pending_pause,
            )
            return ToolResult(
                output=json.dumps(
                    {
                        "task_id": task_row.task_id,
                        "title": task_row.title,
                        "description": task_row.description,
                        "expected_output": task_row.expected_output,
                        "status": task_row.status,
                        "priority": task_row.priority,
                        "agent_id": task_row.agent_id,
                        "created_by_agent_id": getattr(task_row, "created_by_agent_id", None),
                        "workflow_id": task_row.workflow_id,
                        "created_at": str(task_row.created_at) if task_row.created_at else None,
                        "started_at": str(task_row.started_at) if task_row.started_at else None,
                        "completed_at": str(task_row.completed_at)
                        if task_row.completed_at
                        else None,
                        "result_summary": task_row.result_summary,
                        "result_data": task_row.result_data,
                        "pending_pause": (
                            pending_pause.model_dump(mode="json")
                            if pending_pause is not None
                            else None
                        ),
                        "workflow_run": (
                            workflow_run.model_dump(mode="json")
                            if workflow_run is not None
                            else None
                        ),
                        "step_runs": step_run_summaries,
                    },
                    default=str,
                ),
            )

        elif tc.name == "get_task_output":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            # Find the last completed step with output
            completed = [sr for sr in reversed(step_rows) if sr.status == "approved" and sr.output]
            if completed:
                return self._build_task_step_output_result(task_id=task_id, step_run=completed[0])
            compact_output = "No output available."
            anchors: list[dict[str, object]] = []
            stored_output = compact_output
            return ToolResult(
                output=json.dumps(
                    {
                        "summary": task_row.result_summary or "No output available.",
                        "content": "",
                        "claims": [],
                        "outputs": {},
                        "available_anchors": [],
                    }
                ),
                metadata={
                    "stored_output": stored_output,
                    "output_anchors": anchors,
                },
            )

        elif tc.name == "get_task_step_output":
            task_id = tc.arguments.get("task_id", "")
            step_name = tc.arguments.get("step_name", "")
            attempt, attempt_error = self._parse_attempt_argument(tc.arguments.get("attempt"))
            if attempt_error is not None:
                return ToolResult(output=json.dumps({"error": attempt_error}), is_error=True)
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            selected_run, select_error = self._select_step_run(
                step_rows,
                step_name=step_name,
                attempt=attempt,
            )
            if selected_run is None:
                return ToolResult(output=json.dumps({"error": select_error}), is_error=True)
            return self._build_task_step_output_result(task_id=task_id, step_run=selected_run)

        elif tc.name == "get_task_step_logs":
            task_id = tc.arguments.get("task_id", "")
            step_name = tc.arguments.get("step_name", "")
            attempt, attempt_error = self._parse_attempt_argument(tc.arguments.get("attempt"))
            if attempt_error is not None:
                return ToolResult(output=json.dumps({"error": attempt_error}), is_error=True)
            after_seq = tc.arguments.get("after_seq", 0)
            limit = tc.arguments.get("limit", 50)
            try:
                after_seq = int(after_seq)
                limit = int(limit)
            except (TypeError, ValueError):
                return ToolResult(
                    output=json.dumps({"error": "after_seq and limit must be integers."}),
                    is_error=True,
                )
            if after_seq < 0:
                return ToolResult(
                    output=json.dumps({"error": "after_seq must be 0 or greater."}),
                    is_error=True,
                )
            if limit <= 0:
                return ToolResult(
                    output=json.dumps({"error": "limit must be a positive integer."}),
                    is_error=True,
                )
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
                selected_run, select_error = self._select_step_run(
                    step_rows,
                    step_name=step_name,
                    attempt=attempt,
                )
                if selected_run is None:
                    return ToolResult(output=json.dumps({"error": select_error}), is_error=True)
                intaris_session_id = getattr(selected_run, "intaris_session_id", None)
                if not intaris_session_id and getattr(selected_run, "session_id", None):
                    session_row = await get_session_row(db, selected_run.session_id)
                    if session_row is not None:
                        intaris_session_id = (
                            session_row.intaris_session_id or session_row.session_id
                        )
            if not intaris_session_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "error": (
                                "No step session is recorded for this attempt yet, so there are no logs to inspect."
                            )
                        }
                    ),
                    is_error=True,
                )
            try:
                event_result = await self.providers.guardrails.read_events(
                    session_id=intaris_session_id,
                    after_seq=after_seq,
                    limit=min(limit, 200),
                    allow_missing_stream=True,
                )
            except Exception as exc:
                return ToolResult(
                    output=json.dumps(
                        {
                            "error": f"Failed to read step logs: {type(exc).__name__}: {exc}",
                        }
                    ),
                    is_error=True,
                )
            return self._build_task_step_logs_result(
                task_id=task_id,
                step_run=selected_run,
                events=list(event_result.events),
                last_seq=event_result.last_seq,
                has_more=event_result.has_more,
                after_seq=after_seq,
                limit=min(limit, 200),
                missing_stream=bool(getattr(event_result, "missing_stream_fallback_used", False)),
            )

        elif tc.name == "respond_task_input":
            task_id = tc.arguments.get("task_id", "")
            answers = tc.arguments.get("answers")
            if not isinstance(answers, list):
                return ToolResult(
                    output=json.dumps({"error": "answers must be an array."}),
                    is_error=True,
                )
            reply = {
                "answers": answers,
                "mode": str(tc.arguments.get("mode") or "structured"),
            }
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
            try:
                result = await respond_task_input(
                    task=_task_row_to_model(task_row),
                    reply=reply,
                    pause_waiter=self.pause_waiter,
                    notification_service=getattr(self, "notification_service", None),
                    task_queue=self._task_queue,
                    session_factory=self.session_manager.session_factory,
                    user_email=getattr(ctx.session, "user_email", ctx.agent.owner_email),
                )
            except (ValueError, RuntimeError) as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(output=json.dumps({"task_id": task_id, **result}))

        elif tc.name in {"retry_task", "resolve_task_pause"}:
            task_id = tc.arguments.get("task_id", "")
            task_queue = self._task_queue
            if task_queue is None:
                return ToolResult(
                    output=json.dumps({"error": "Task queue is not available."}), is_error=True
                )
            requested_action = (
                "retry" if tc.name == "retry_task" else str(tc.arguments.get("action", "retry"))
            )
            note = _extract_operator_note(tc.arguments)
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                if task_row.status not in ("failed", "paused"):
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": f"Cannot retry task in '{task_row.status}' status. Only failed or paused tasks can be retried."
                            }
                        ),
                        is_error=True,
                    )
            try:
                result = await resolve_task_pause_action(
                    task=_task_row_to_model(task_row),
                    requested_action=requested_action,
                    note=note,
                    pause_waiter=self.pause_waiter,
                    notification_service=getattr(self, "notification_service", None),
                    task_queue=self._task_queue,
                    session_factory=self.session_manager.session_factory,
                    user_email=getattr(ctx.session, "user_email", ctx.agent.owner_email),
                )
                return ToolResult(output=json.dumps({"task_id": task_id, **result}))
            except ValueError as exc:
                pauses = self.pause_waiter.list_pending(task_id=task_id)
                if pauses and pauses[0].pause_type in ("step_question", "step_input"):
                    question = pauses[0].question or "No question text available"
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": (
                                    f"Task is waiting for input on step '{pauses[0].step_name or 'unknown'}'. "
                                    f"Question: {question}. Use respond_task_input to continue."
                                )
                            }
                        ),
                        is_error=True,
                    )
                if pauses and pauses[0].pause_type == "escalation":
                    tool_name = (
                        (pauses[0].context or {}).get("tool_name", "a tool call")
                        if pauses[0].context
                        else "a tool call"
                    )
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": (
                                    f"Task is waiting for escalation approval on {tool_name}. "
                                    "Use /approve or /deny to resolve the escalation first."
                                )
                            }
                        ),
                        is_error=True,
                    )
                if (
                    not pauses
                    and task_row.status in ("failed", "paused")
                    and requested_action == "retry"
                ):
                    await task_queue.retry_failed_task(task_id)
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "retrying",
                                "task_id": task_id,
                                "message": "Task reset and relaunched for retry.",
                            }
                        ),
                    )
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            except RuntimeError as exc:
                return ToolResult(
                    output=json.dumps({"error": f"Failed to retry task: {exc}"}), is_error=True
                )

        elif tc.name == "update_task":
            task_id = tc.arguments.get("task_id", "")
            from cognis.core.workflow_management import get_workflow_for_user
            from cognis.models.workflow import SessionPolicy

            workflow_id = tc.arguments.get("workflow_id")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": "Task not found."}),
                        is_error=True,
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": "Task belongs to a different agent.",
                            }
                        ),
                        is_error=True,
                    )
                if task_row.status not in ("draft", "queued"):
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": (
                                    f"Cannot update task in '{task_row.status}' status. "
                                    "Only draft or queued tasks can be updated."
                                ),
                            }
                        ),
                        is_error=True,
                    )
                project_id_arg = tc.arguments.get("project_id")
                project_id = str(project_id_arg) if project_id_arg else task_row.project_id
                if project_id_arg:
                    project = await get_project(db, project_id)
                    project_access = (
                        project is not None
                        and (project.status == "active")
                        and (
                            project.owner_email == ctx.session.user_email
                            or await get_active_project_grant(
                                db, project_id, ctx.session.user_email
                            )
                            is not None
                        )
                    )
                    if not project_access:
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Project not found or not accessible.",
                                }
                            ),
                            is_error=True,
                        )
                workflow_id_to_validate = workflow_id or task_row.workflow_id
                if workflow_id_to_validate:
                    workflow = await get_workflow_for_user(
                        workflow_registry=_workflow_registry_for_agent_loop(self),
                        workflow_id=str(workflow_id_to_validate),
                        owner_email=ctx.session.user_email,
                        project_id=project_id,
                    )
                    if workflow is None:
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Workflow not found or not accessible.",
                                }
                            ),
                            is_error=True,
                        )
                ok = await update_task_fields(
                    db,
                    task_id,
                    title=tc.arguments.get("title"),
                    description=tc.arguments.get("description"),
                    priority=tc.arguments.get("priority"),
                    workflow_id=tc.arguments.get("workflow_id"),
                    project_id=project_id_arg,
                    session_policy=(
                        SessionPolicy.model_validate(tc.arguments["session_policy"]).model_dump()
                        if "session_policy" in tc.arguments
                        else None
                    ),
                )
                await db.commit()
            if ok:
                return ToolResult(
                    output=json.dumps({"status": "updated", "task_id": task_id}),
                )
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": "No fields to update or update failed.",
                    }
                ),
                is_error=True,
            )

        elif tc.name == "cancel_task":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
            if task_row is None:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": "Task not found."}),
                    is_error=True,
                )
            if not await self._can_access_task(task_row, ctx):
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Task belongs to a different agent.",
                        }
                    ),
                    is_error=True,
                )
            if task_row.status in ("completed", "failed", "cancelled"):
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Task is already {task_row.status}.",
                        }
                    ),
                    is_error=True,
                )
            # Use task queue cancel if available, otherwise direct DB update
            task_queue = self._task_queue
            if task_queue is not None:
                with contextlib.suppress(Exception):
                    await task_queue.cancel_task(task_id)
            else:
                async with self.session_manager.session_factory() as db:
                    await update_task_status(
                        db,
                        task_id,
                        "cancelled",
                        completed_at=datetime.now(UTC),
                    )
                    await db.commit()
            return ToolResult(
                output=json.dumps({"status": "cancelled", "task_id": task_id}),
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    async def _can_access_task(self, task_row: Any, ctx: StepContext) -> bool:
        task_owner = getattr(task_row, "created_by", None)
        current_user = getattr(ctx.session, "user_email", ctx.agent.owner_email)
        if not task_owner or task_owner != current_user:
            return False

        task_agent_id = getattr(task_row, "agent_id", None)
        current_agent_id = ctx.agent.agent_id
        if getattr(task_row, "created_by_agent_id", None) == current_agent_id:
            return True
        if not task_agent_id:
            return False
        if task_agent_id == current_agent_id:
            return True

        session_factory = getattr(self.session_manager, "session_factory", None)
        if session_factory is None:
            return False

        from cognis.core.agent_registry import AgentRegistry

        registry = AgentRegistry(session_factory)
        task_agent = await registry.get(task_agent_id)
        current_agent = await registry.get(current_agent_id)
        task_agent_type = task_agent.agent_type if task_agent is not None else "primary"
        current_agent_type = current_agent.agent_type if current_agent is not None else "primary"

        if task_agent_type == "primary" and current_agent_type == "secondary":
            return await registry.is_secondary_bound(task_agent_id, current_agent_id)
        if task_agent_type == "secondary" and current_agent_type == "primary":
            return await registry.is_secondary_bound(current_agent_id, task_agent_id)
        return False

    async def _prune_tool_outputs_after_step(self, ctx: StepContext) -> None:
        """Mark older tool-result call ids for clearance in the in-context view.

        Mirrors OpenCode's post-loop ``prune``: walks back from the most
        recent events, keeps the last few user turns intact, accumulates
        tool output tokens, and once the tail exceeds ``PRUNE_PROTECT``
        tokens registers the older call ids on the session cache so
        future context assembly substitutes a clearance marker. The tool
        output store is unchanged — the model can recover any pruned
        result via ``read_tool_output(call_id=...)``.
        """

        from cognis.core.tool_output_prune import PruneCandidate, select_prune_call_ids

        cache_get_events = getattr(self.session_cache, "get_events_since_compaction", None)
        cache_add_pruned = getattr(self.session_cache, "add_pruned_tool_call_ids", None)
        if not callable(cache_get_events) or not callable(cache_add_pruned):
            return

        try:
            cached_events = cache_get_events(ctx.session.session_id)
        except Exception:
            return
        if not cached_events:
            return

        candidates: list[PruneCandidate] = []
        total_tool_tokens = 0
        token_counter = self._build_token_counter_for_pruning(ctx)
        policy = ProjectionPolicy.from_budget(
            max_context_tokens=(
                ctx.last_projection_policy.max_context_tokens
                if ctx.last_projection_policy is not None
                else 272_000
            ),
            available_prompt_tokens=(
                ctx.last_projection_policy.available_prompt_tokens
                if ctx.last_projection_policy is not None
                else None
            ),
            phase="cross_turn",
            pressure_mode="normal",
        )
        for event in cached_events:
            event_type = getattr(event, "type", None) or (
                event.get("type") if isinstance(event, dict) else None
            )
            data = getattr(event, "data", None) or (
                event.get("data", {}) if isinstance(event, dict) else {}
            )
            if not isinstance(data, dict):
                continue
            if event_type == "user_message":
                candidates.append(PruneCandidate("", "", "", is_user_turn=True))
                continue
            if event_type != "tool_result":
                continue
            call_id = str(data.get("call_id") or "")
            if not call_id:
                continue
            output = str(data.get("result") or data.get("output") or "")
            if not output:
                continue
            tool_name = str(data.get("name") or "")
            candidates.append(PruneCandidate(call_id=call_id, tool_name=tool_name, output=output))
            try:
                total_tool_tokens += token_counter(output)
            except Exception:
                total_tool_tokens += max(1, len(output) // 4)

        # Below the minimum we don't bother — the gain is too small to
        # justify cluttering the context with clearance markers.
        if total_tool_tokens < policy.prune_minimum_savings_tokens:
            return

        prune_ids = select_prune_call_ids(
            candidates,
            token_counter=token_counter,
            protect_tokens=policy.prune_protect_tokens,
        )
        if not prune_ids:
            return
        try:
            cache_add_pruned(ctx.session.session_id, prune_ids)
        except Exception:
            logger.debug(
                "tool output prune: failed to record cleared call ids",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )
            return
        logger.info(
            "tool output prune: cleared older results from context view",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "pruned_count": len(prune_ids),
                    "total_tool_tokens": total_tool_tokens,
                    "projection_policy": policy.as_metadata(),
                }
            },
        )

    def _build_token_counter_for_pruning(self, ctx: StepContext) -> Callable[[str], int]:
        """Return a token counter that prefers the live LLM provider when available."""

        llm = getattr(self.providers, "llm", None)
        model = ctx.current_model
        if llm is not None and model:
            count_tokens = getattr(llm, "count_tokens", None)
            if callable(count_tokens):
                resolved_model: str = model

                def _llm_counter(text: str) -> int:
                    try:
                        return int(count_tokens(text, resolved_model))
                    except Exception:
                        return max(1, len(text) // 4)

                return _llm_counter
        return lambda text: max(1, len(text) // 4)

    async def _save_tool_output_if_available(self, call_id: str, result: ToolResult) -> None:
        """Persist full tool output and anchors when metadata provides them."""

        if self.tool_output_store is None or not result.metadata:
            return
        stored_output = result.metadata.get("stored_output")
        raw_output = result.metadata.get("_raw_output")
        if isinstance(stored_output, str) and stored_output:
            content = stored_output
        elif isinstance(raw_output, str) and raw_output:
            content = raw_output
        else:
            return
        anchors = result.metadata.get("output_anchors")
        await self.tool_output_store.save(
            call_id,
            content,
            anchors=anchors if isinstance(anchors, list) else None,
        )

    async def _save_tool_output_artifact_if_available(
        self,
        ctx: StepContext,
        call_id: str,
        result: ToolResult,
    ) -> str | None:
        """Persist full tool output as a temporary Cognis artifact when possible."""

        artifact_store = getattr(self, "artifact_store", None)
        if artifact_store is None or self._session_factory is None or not result.metadata:
            return None
        stored_output = result.metadata.get("stored_output")
        raw_output = result.metadata.get("_raw_output")
        if isinstance(stored_output, str) and stored_output:
            content = stored_output
        elif isinstance(raw_output, str) and raw_output:
            content = raw_output
        else:
            return None

        artifact_id = artifact_store.generate_id("toolout")
        filename = sanitize_artifact_filename(f"{call_id}.txt", default="tool-output.txt")
        encoded = content.encode("utf-8")
        ttl_seconds = int(getattr(self.tool_output_store, "ttl_seconds", 168 * 3600) or 168 * 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=max(60, ttl_seconds))
        saved_blob = False
        try:
            await artifact_store.async_save(
                "tool-outputs",
                artifact_id,
                filename,
                encoded,
                "text/plain; charset=utf-8",
                owner_email=getattr(ctx.session, "user_email", None),
            )
            saved_blob = True
            from cognis.store.queries import create_artifact_record

            async with self._session_factory() as session:
                await create_artifact_record(
                    session,
                    artifact_id=artifact_id,
                    namespace="tool-outputs",
                    object_id=artifact_id,
                    filename=filename,
                    owner_email=getattr(ctx.session, "user_email", None),
                    purpose="tool_output",
                    kind="file",
                    mime_type="text/plain; charset=utf-8",
                    size_bytes=len(encoded),
                    status="temporary",
                    expires_at=expires_at,
                    conversation_id=getattr(ctx.conversation, "conversation_id", None),
                    session_id=getattr(ctx.session, "session_id", None),
                    message_role="tool",
                )
                await session.commit()
        except Exception:
            if saved_blob:
                with contextlib.suppress(Exception):
                    await artifact_store.async_delete_object("tool-outputs", artifact_id)
            logger.warning(
                "failed to save tool output artifact",
                extra={"extra_data": {"call_id": call_id}},
                exc_info=True,
            )
            return None
        return artifact_id

    @staticmethod
    def _parse_attempt_argument(raw_attempt: Any) -> tuple[int | None, str | None]:
        """Parse an optional step attempt number from tool arguments."""

        if raw_attempt in (None, ""):
            return None, None
        if isinstance(raw_attempt, bool):
            return None, "attempt must be a positive integer."
        try:
            attempt = int(raw_attempt)
        except (TypeError, ValueError):
            return None, "attempt must be a positive integer."
        if attempt <= 0:
            return None, "attempt must be a positive integer."
        return attempt, None

    @staticmethod
    def _select_step_run(
        step_rows: list[Any],
        *,
        step_name: str,
        attempt: int | None,
    ) -> tuple[Any | None, str | None]:
        """Resolve a task step attempt by step name and optional attempt number."""

        matching = [row for row in step_rows if row.step_name == step_name]
        if not matching:
            return None, f"No step '{step_name}' found for this task."
        if attempt is None:
            return matching[-1], None
        for row in reversed(matching):
            if int(getattr(row, "attempt", 0) or 0) == attempt:
                return row, None
        return None, f"Step '{step_name}' does not have attempt {attempt}."

    @staticmethod
    def _build_task_step_output_result(*, task_id: str, step_run: Any) -> ToolResult:
        """Build a backward-compatible task step output plus anchored metadata."""

        compact_builder = AnchoredTextBuilder()
        stored_builder = AnchoredTextBuilder()
        output = step_run.output or {}
        evaluation = step_run.evaluation or {}
        todos = step_run.todos or []

        overview_lines = [
            f"Task ID: {task_id}",
            f"Step: {step_run.step_name}",
            f"Attempt: {step_run.attempt}",
            f"Status: {step_run.status}",
        ]
        if getattr(step_run, "step_run_id", None):
            overview_lines.append(f"Step run ID: {step_run.step_run_id}")
        if getattr(step_run, "session_id", None):
            overview_lines.append(f"Session ID: {step_run.session_id}")
        if getattr(step_run, "conversation_id", None):
            overview_lines.append(f"Conversation ID: {step_run.conversation_id}")
        if getattr(step_run, "started_at", None):
            overview_lines.append(f"Started: {step_run.started_at}")
        if getattr(step_run, "completed_at", None):
            overview_lines.append(f"Completed: {step_run.completed_at}")
        compact_builder.add_section(
            "overview",
            kind="overview",
            label="Overview",
            lines=overview_lines,
        )
        stored_builder.add_section(
            "overview",
            kind="overview",
            label="Overview",
            lines=overview_lines,
        )

        summary = str(output.get("summary") or "").strip()
        if summary:
            compact_builder.add_section(
                "summary",
                kind="summary",
                label="Summary",
                lines=[compact_snippet(summary, max_chars=700)],
            )
            stored_builder.add_section(
                "summary",
                kind="summary",
                label="Summary",
                lines=_indent_block(summary, prefix=""),
            )

        content = str(output.get("content") or "").strip()
        if content:
            compact_builder.add_section(
                "content",
                kind="content",
                label="Content",
                lines=[compact_snippet(content, max_chars=900)],
            )
            stored_builder.add_section(
                "content",
                kind="content",
                label="Content",
                lines=_indent_block(content, prefix=""),
            )

        claims = output.get("claims") if isinstance(output.get("claims"), list) else []
        if claims:
            compact_builder.add_section(
                "claims",
                kind="claims",
                label="Claims",
                lines=[f"- {compact_snippet(str(item), max_chars=240)}" for item in claims],
            )
            stored_builder.add_section(
                "claims",
                kind="claims",
                label="Claims",
                lines=[f"- {item}" for item in claims],
            )

        outputs = output.get("outputs") if isinstance(output.get("outputs"), dict) else {}
        if outputs:
            compact_builder.add_section(
                "outputs",
                kind="outputs",
                label="Structured outputs",
                lines=_indent_block(compact_snippet(_json_text(outputs), max_chars=900), prefix=""),
            )
            stored_builder.add_section(
                "outputs",
                kind="outputs",
                label="Structured outputs",
                lines=_indent_block(_json_text(outputs), prefix=""),
            )

        error = str(output.get("error") or "").strip()
        if error:
            compact_builder.add_section(
                "error",
                kind="error",
                label="Error",
                lines=[compact_snippet(error, max_chars=900)],
            )
            stored_builder.add_section(
                "error",
                kind="error",
                label="Error",
                lines=_indent_block(error, prefix=""),
            )

        if evaluation:
            compact_builder.add_section(
                "evaluation",
                kind="evaluation",
                label="Evaluation",
                lines=_indent_block(
                    compact_snippet(_json_text(evaluation), max_chars=900), prefix=""
                ),
            )
            stored_builder.add_section(
                "evaluation",
                kind="evaluation",
                label="Evaluation",
                lines=_indent_block(_json_text(evaluation), prefix=""),
            )

        if todos:
            compact_builder.add_section(
                "todos",
                kind="todos",
                label="Todos",
                lines=_indent_block(compact_snippet(_json_text(todos), max_chars=900), prefix=""),
            )
            stored_builder.add_section(
                "todos",
                kind="todos",
                label="Todos",
                lines=_indent_block(_json_text(todos), prefix=""),
            )

        compact_output, anchors = compact_builder.build()
        stored_output, _ = stored_builder.build()
        compact_claims = [compact_snippet(str(item), max_chars=240) for item in claims]
        outputs_payload = output.get("outputs", {})
        if isinstance(outputs_payload, dict):
            outputs_json = _json_text(outputs_payload)
            if len(outputs_json) > 1000:
                outputs_payload = {
                    "_truncated": True,
                    "preview": compact_snippet(outputs_json, max_chars=900),
                }
        payload = {
            "task_id": task_id,
            "step_run_id": getattr(step_run, "step_run_id", None),
            "step_name": step_run.step_name,
            "status": step_run.status,
            "attempt": step_run.attempt,
            "session_id": getattr(step_run, "session_id", None),
            "conversation_id": getattr(step_run, "conversation_id", None),
            "summary": compact_snippet(summary, max_chars=700) if summary else "",
            "content": compact_snippet(content, max_chars=900) if content else "",
            "claims": compact_claims,
            "outputs": outputs_payload,
            "error": output.get("error"),
            "evaluation": evaluation or None,
            "todos": todos or [],
            "available_anchors": [anchor["anchor"] for anchor in anchors],
        }
        return ToolResult(
            output=json.dumps(payload, default=str),
            metadata={
                "stored_output": stored_output or compact_output,
                "output_anchors": anchors,
            },
        )

    @staticmethod
    def _build_task_step_logs_result(
        *,
        task_id: str,
        step_run: Any,
        events: list[dict[str, Any]],
        last_seq: int,
        has_more: bool,
        after_seq: int,
        limit: int,
        missing_stream: bool,
    ) -> ToolResult:
        """Build an anchored tool result for a task step session log."""

        compact_builder = AnchoredTextBuilder()
        stored_builder = AnchoredTextBuilder()
        overview_lines = [
            f"Task ID: {task_id}",
            f"Step: {step_run.step_name}",
            f"Attempt: {step_run.attempt}",
            f"Session ID: {step_run.session_id or 'n/a'}",
            f"Events returned: {len(events)}",
            f"after_seq: {after_seq}",
            f"limit: {limit}",
            f"last_seq: {last_seq}",
            f"has_more: {str(has_more).lower()}",
        ]
        if missing_stream:
            overview_lines.append("Warning: the session event stream was missing in Intaris.")
        if has_more:
            overview_lines.append(
                f"Next page: call get_task_step_logs again with after_seq={last_seq}."
            )
        compact_builder.add_section(
            "overview",
            kind="overview",
            label="Overview",
            lines=overview_lines,
        )
        stored_builder.add_section(
            "overview",
            kind="overview",
            label="Overview",
            lines=overview_lines,
        )

        counts: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("type") or "event")
            if event_type == "reasoning":
                continue
            kind = _task_log_anchor_kind(event_type)
            counts[kind] = counts.get(kind, 0) + 1
            anchor = f"{kind}:{counts[kind]}"
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            seq = event.get("seq")
            timestamp = event.get("ts") or event.get("timestamp")
            label_parts = [event_type]
            if event_type in {"tool_call", "tool_result"} and data.get("name"):
                label_parts.append(str(data.get("name")))
            elif event_type in {"assistant_message", "user_message", "reasoning"}:
                label_parts.append(f"#{counts[kind]}")
            elif event_type == "assistant_thinking" and data.get("title"):
                label_parts.append(str(data.get("title"))[:40])
            label = " - ".join(label_parts)
            compact_lines = [f"Event: {event_type}"]
            stored_lines = [f"Event: {event_type}"]
            if seq is not None:
                compact_lines.append(f"Seq: {seq}")
                stored_lines.append(f"Seq: {seq}")
            if timestamp:
                compact_lines.append(f"Timestamp: {timestamp}")
                stored_lines.append(f"Timestamp: {timestamp}")

            if event_type == "tool_call":
                compact_lines.append(f"Tool: {data.get('name') or 'unknown'}")
                stored_lines.append(f"Tool: {data.get('name') or 'unknown'}")
                if data.get("call_id"):
                    compact_lines.append(f"Call ID: {data['call_id']}")
                    stored_lines.append(f"Call ID: {data['call_id']}")
                arguments = str(data.get("arguments") or "").strip()
                if arguments:
                    compact_lines.append(f"Arguments: {compact_snippet(arguments, max_chars=800)}")
                    stored_lines.append("Arguments:")
                    stored_lines.extend(_indent_block(arguments, prefix=""))
            elif event_type == "tool_result":
                compact_lines.append(f"Tool: {data.get('name') or 'unknown'}")
                stored_lines.append(f"Tool: {data.get('name') or 'unknown'}")
                if data.get("call_id"):
                    compact_lines.append(f"Call ID: {data['call_id']}")
                    stored_lines.append(f"Call ID: {data['call_id']}")
                compact_lines.append(f"Status: {'error' if data.get('is_error') else 'ok'}")
                stored_lines.append(f"Status: {'error' if data.get('is_error') else 'ok'}")
                if data.get("duration_ms") is not None:
                    compact_lines.append(f"Duration: {data['duration_ms']}ms")
                    stored_lines.append(f"Duration: {data['duration_ms']}ms")
                if data.get("has_full_output") and data.get("call_id"):
                    note = (
                        "Full tool output is available. Use the call_id above with "
                        "read_tool_output, search_tool_output, or list_tool_output_anchors."
                    )
                    compact_lines.append(note)
                    stored_lines.append(note)
                if data.get("agent_visible") is True:
                    stored_lines.append("Stored result is the exact agent-visible tool result.")
                    if data.get("agent_visible_truncated") is True:
                        stored_lines.append(
                            "Agent-visible result was already truncated before storage."
                        )
                if data.get("tool_output_artifact_id"):
                    stored_lines.append(f"Tool output artifact: {data['tool_output_artifact_id']}")
                result_text = str(data.get("result") or "").strip()
                if result_text:
                    compact_lines.append(f"Result: {compact_snippet(result_text, max_chars=900)}")
                    stored_lines.append("Result:")
                    stored_lines.extend(_indent_block(result_text, prefix=""))
            elif event_type == "assistant_thinking":
                block_title = str(data.get("title") or "Thinking")
                label_parts = ["assistant_thinking", block_title[:40]]
                label = " - ".join(label_parts)
                compact_lines.append(f"Thinking: {block_title}")
                stored_lines.append(f"Thinking: {block_title}")
                thinking_content = str(data.get("content") or "").strip()
                if thinking_content:
                    compact_lines.append(compact_snippet(thinking_content, max_chars=900))
                    stored_lines.extend(_indent_block(thinking_content, prefix=""))
            elif event_type in {"assistant_message", "user_message", "reasoning"}:
                content = str(data.get("content") or "").strip()
                if content:
                    compact_lines.append(compact_snippet(content, max_chars=900))
                    stored_lines.extend(_indent_block(content, prefix=""))
            elif event_type == "lifecycle":
                if data:
                    compact_lines.append(compact_snippet(_json_text(data), max_chars=900))
                    stored_lines.extend(_indent_block(_json_text(data), prefix=""))
            else:
                if data:
                    compact_lines.append(compact_snippet(_json_text(data), max_chars=900))
                    stored_lines.extend(_indent_block(_json_text(data), prefix=""))

            compact_builder.add_section(anchor, kind=kind, label=label, lines=compact_lines)
            stored_builder.add_section(anchor, kind=kind, label=label, lines=stored_lines)

        compact_output, anchors = compact_builder.build()
        stored_output, _ = stored_builder.build()
        if not events and not missing_stream:
            compact_output = compact_output or "No events recorded for this step session yet."
            stored_output = stored_output or compact_output
        return ToolResult(
            output=compact_output or "No events recorded for this step session yet.",
            metadata={
                "stored_output": stored_output or compact_output,
                "output_anchors": anchors,
            },
        )

    async def _handle_workflow_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
    ) -> ToolResult:
        """Handle workflow CRUD tools in main-chat contexts."""

        from cognis.api.serializers import workflow_to_response
        from cognis.core.management import workflow_row_to_summary
        from cognis.core.workflow_management import (
            create_user_workflow,
            delete_user_workflow,
            duplicate_visible_workflow,
            get_workflow_for_user,
            list_workflows_for_user,
            update_user_workflow,
        )

        workflow_registry = _workflow_registry_for_agent_loop(self)
        owner_email = ctx.session.user_email

        if tc.name == "list_workflows":
            workflows = await list_workflows_for_user(
                workflow_registry=workflow_registry,
                owner_email=owner_email,
            )
            items = [workflow_row_to_summary(workflow) for workflow in workflows]
            return ToolResult(output=json.dumps({"workflows": items, "count": len(items)}))

        if tc.name == "get_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            workflow = await get_workflow_for_user(
                workflow_registry=workflow_registry,
                workflow_id=workflow_id,
                owner_email=owner_email,
            )
            if workflow is None:
                return ToolResult(
                    output=json.dumps({"error": "Workflow not found."}), is_error=True
                )
            return ToolResult(
                output=json.dumps(workflow_to_response(workflow).model_dump(mode="json"))
            )

        if tc.name == "create_workflow":
            try:
                row = await create_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    owner_email=owner_email,
                    payload=tc.arguments,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "created",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        if tc.name == "update_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                row = await update_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                    payload={
                        key: value for key, value in tc.arguments.items() if key != "workflow_id"
                    },
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "updated",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        if tc.name == "delete_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                ok = await delete_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps({"status": "deleted", "workflow_id": workflow_id, "ok": ok})
            )

        if tc.name == "duplicate_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                row = await duplicate_visible_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_registry=workflow_registry,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "duplicated",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        return ToolResult(
            output=json.dumps({"error": f"Unknown workflow tool: {tc.name}"}),
            is_error=True,
        )

    async def _handle_composition_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
    ) -> ToolResult:
        """Handle workflow composition from the main chat agent."""

        from cognis.core.schedule_management import create_user_schedule
        from cognis.core.workflow_composition import (
            ComposeAndRunWorkflowArgs,
            SkillMaterial,
            compose_workflow_plan,
            decompose_skill_material,
            validate_composed_workflow,
            workflow_preview_payload,
        )
        from cognis.core.workflow_management import (
            create_user_workflow,
            get_workflow_for_user,
        )
        from cognis.models.task import TaskDelivery
        from cognis.store.queries import (
            create_skill_version,
            delete_workflow,
            get_agent,
            get_next_version_number,
            get_skill_scoped,
            list_skills,
            set_current_version,
        )
        from cognis.tools.skill_parser import compute_content_hash
        from cognis.tools.skill_service import (
            compute_decomposition_source_hash,
            resolve_current_skill_version,
        )

        if tc.name != "compose_and_run_workflow":
            return ToolResult(
                output=json.dumps({"error": f"Unknown composition tool: {tc.name}"}),
                is_error=True,
            )

        task_queue = self._task_queue
        if task_queue is None:
            return ToolResult(
                output=json.dumps({"error": "Task queue is not available."}),
                is_error=True,
            )
        if getattr(self.providers, "llm", None) is None:
            return ToolResult(
                output=json.dumps({"error": "LLM provider is not available."}),
                is_error=True,
            )

        try:
            args = ComposeAndRunWorkflowArgs.model_validate(tc.arguments)
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)

        workflow_registry = _workflow_registry_for_agent_loop(self)
        owner_email = ctx.session.user_email
        raw_agent_id = args.agent_id or ctx.agent.agent_id
        if raw_agent_id == "self":
            raw_agent_id = ctx.agent.agent_id
        async with self.session_manager.session_factory() as db:
            agent_row = await get_agent(db, str(raw_agent_id))
        if agent_row is None or agent_row.owner_email != owner_email:
            return ToolResult(
                output=json.dumps({"error": "Agent not found or not accessible."}),
                is_error=True,
            )
        try:
            compose_agent_profile_id = normalize_agent_profile_id(args.agent_profile_id)
        except ValueError as exc:
            return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
        if compose_agent_profile_id is not None:
            from cognis.api.serializers import agent_to_response

            agent_definition = AgentDefinition.model_validate(
                agent_to_response(agent_row).model_dump()
            )
            try:
                resolve_agent_profile(
                    agent_definition,
                    compose_agent_profile_id,
                    source="compose_and_run_workflow",
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)

        def _coerce_skill_tools(value: Any) -> list[dict[str, Any]] | None:
            if value is None:
                return None
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
            return None

        base_workflow = None
        if args.base_workflow_id:
            base_workflow = await get_workflow_for_user(
                workflow_registry=workflow_registry,
                workflow_id=args.base_workflow_id,
                owner_email=owner_email,
            )
            if base_workflow is None:
                return ToolResult(
                    output=json.dumps({"error": "Base workflow not found or not accessible."}),
                    is_error=True,
                )

        available_workflows = await workflow_registry.list_all(owner_email=owner_email)
        template_hints = list(args.template_hints)
        if args.base_workflow_id and args.base_workflow_id not in template_hints:
            template_hints.append(args.base_workflow_id)

        async def _load_skill_materials() -> list[SkillMaterial]:
            if not args.skill_hints:
                return []
            materials: list[SkillMaterial] = []
            async with self.session_manager.session_factory() as db:
                visible_rows = await list_skills(db, owner_email=owner_email)
                rows_by_id = {row.skill_id: row for row in visible_rows}
                rows_by_name = {str(row.name).lower(): row for row in visible_rows}
                for hint in args.skill_hints:
                    row = rows_by_id.get(hint) or rows_by_name.get(hint.lower())
                    if row is None:
                        raise ValueError(f"Unknown skill hint: {hint}")
                    version_row = await resolve_current_skill_version(db, row)
                    instructions = (
                        version_row.instructions if version_row is not None else row.instructions
                    )
                    tools = (
                        _coerce_skill_tools(
                            version_row.tools if version_row is not None else row.tools
                        )
                        or []
                    )
                    linked_tool_ids = [
                        str(tool_id).strip()
                        for tool_id in (getattr(row, "linked_tool_ids", None) or [])
                        if str(tool_id).strip()
                    ]
                    prompt_templates = (
                        version_row.prompt_templates
                        if version_row is not None
                        else row.prompt_templates
                    ) or {}
                    secret_placeholders = (
                        list(getattr(version_row, "secret_placeholders", None) or [])
                        if version_row is not None
                        else []
                    )
                    asset_manifest = [
                        item
                        for item in (getattr(version_row, "asset_manifest", None) or [])
                        if isinstance(item, dict)
                    ]
                    steps = [
                        item
                        for item in (
                            (
                                getattr(version_row, "steps", None)
                                if version_row is not None
                                else None
                            )
                            or []
                        )
                        if isinstance(item, dict)
                    ]
                    materials.append(
                        SkillMaterial(
                            skill_id=row.skill_id,
                            name=row.name,
                            description=row.description,
                            instructions=instructions,
                            tools=tools,
                            linked_tool_ids=linked_tool_ids,
                            prompt_templates=prompt_templates,
                            secret_placeholders=secret_placeholders,
                            asset_manifest=asset_manifest,
                            steps=steps,
                            decomposition_source_hash=(
                                getattr(version_row, "decomposition_source_hash", None)
                                if version_row is not None
                                else None
                            ),
                            current_source_hash=compute_decomposition_source_hash(
                                instructions,
                                tools=tools,
                                linked_tool_ids=linked_tool_ids,
                                prompt_templates=prompt_templates,
                                secret_placeholders=secret_placeholders,
                                asset_manifest=asset_manifest,
                            ),
                        )
                    )
            return materials

        async def _persist_skill_steps(material: SkillMaterial) -> None:
            async with self.session_manager.session_factory() as db:
                row = await get_skill_scoped(db, material.skill_id, owner_email=owner_email)
                if row is None:
                    return
                if (
                    row.owner_email != owner_email
                    or row.is_system
                    or row.source not in {"db", "imported"}
                ):
                    return
                current_version_row = await resolve_current_skill_version(db, row)
                if current_version_row is None:
                    return
                version_number = await get_next_version_number(db, row.skill_id)
                version_row = await create_skill_version(
                    db,
                    skill_id=row.skill_id,
                    version_number=version_number,
                    content_hash=compute_content_hash(
                        current_version_row.instructions,
                        _coerce_skill_tools(current_version_row.tools),
                        getattr(current_version_row, "linked_tool_ids", None),
                        current_version_row.prompt_templates,
                        current_version_row.secret_placeholders,
                        current_version_row.asset_manifest,
                        material.steps,
                    ),
                    instructions=current_version_row.instructions,
                    tools=_coerce_skill_tools(current_version_row.tools),
                    linked_tool_ids=getattr(current_version_row, "linked_tool_ids", None),
                    prompt_templates=current_version_row.prompt_templates,
                    secret_placeholders=current_version_row.secret_placeholders,
                    steps=material.steps,
                    decomposition_source_hash=compute_decomposition_source_hash(
                        current_version_row.instructions,
                        tools=_coerce_skill_tools(current_version_row.tools),
                        linked_tool_ids=getattr(current_version_row, "linked_tool_ids", None),
                        prompt_templates=current_version_row.prompt_templates,
                        secret_placeholders=current_version_row.secret_placeholders,
                        asset_manifest=current_version_row.asset_manifest,
                    ),
                    source_url=current_version_row.source_url,
                    resolved_url=current_version_row.resolved_url,
                    commit_sha=current_version_row.commit_sha,
                    import_checksum=current_version_row.import_checksum,
                    imported_at=current_version_row.imported_at,
                    import_format=current_version_row.import_format,
                    asset_manifest=current_version_row.asset_manifest,
                    schema_version=current_version_row.schema_version,
                )
                await set_current_version(db, row.skill_id, version_row.version_id)
                await db.commit()

        try:
            skill_materials = await _load_skill_materials()
        except ValueError as exc:
            return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)

        for material in skill_materials:
            needs_decomposition = args.decompose_skills == "always" or (
                args.decompose_skills == "auto"
                and (
                    not material.steps
                    or material.decomposition_source_hash != material.current_source_hash
                )
            )
            if not needs_decomposition:
                continue
            try:
                decomposition = await decompose_skill_material(
                    llm=self.providers.llm,
                    skill_id=material.skill_id,
                    name=material.name,
                    description=material.description,
                    instructions=material.instructions,
                    tools=material.tools,
                    linked_tool_ids=material.linked_tool_ids,
                    prompt_templates=material.prompt_templates,
                    secret_placeholders=material.secret_placeholders,
                    asset_manifest=material.asset_manifest,
                )
            except Exception as exc:
                return ToolResult(
                    output=json.dumps(
                        {"error": f"Failed to decompose skill '{material.name}': {exc}"}
                    ),
                    is_error=True,
                )
            material.steps = decomposition.steps
            try:
                await _persist_skill_steps(material)
            except Exception as exc:
                return ToolResult(
                    output=json.dumps(
                        {
                            "error": f"Failed to persist decomposition for skill '{material.name}': {exc}"
                        }
                    ),
                    is_error=True,
                )
            material.decomposition_source_hash = material.current_source_hash

        schedule_requested = isinstance(args.schedule, dict)
        validation_feedback: str | None = None
        composer_output = None
        workflow = None
        created_workflow_ids: list[str] = []

        async def _cleanup_created_workflows() -> None:
            for workflow_id in reversed(created_workflow_ids):
                with contextlib.suppress(Exception):
                    async with self.session_manager.session_factory() as db:
                        await delete_workflow(db, workflow_id)
                        await db.commit()

        for attempt_index in range(2):
            try:
                composer_output = await compose_workflow_plan(
                    llm=self.providers.llm,
                    intent=args.intent,
                    context=args.context,
                    available_workflows=available_workflows,
                    template_hints=template_hints,
                    base_workflow=base_workflow,
                    skill_materials=skill_materials,
                    persist=args.persist,
                    schedule_requested=schedule_requested,
                    validator_feedback=validation_feedback,
                )
                if composer_output.action == "reuse_existing":
                    if not composer_output.workflow_id:
                        raise ValueError("Composer chose reuse_existing without workflow_id")
                    workflow = await get_workflow_for_user(
                        workflow_registry=workflow_registry,
                        workflow_id=composer_output.workflow_id,
                        owner_email=owner_email,
                    )
                    if workflow is None:
                        raise ValueError("Composer selected an unknown workflow")
                else:
                    workflow_payload = dict(composer_output.workflow or {})
                    workflow_payload.update(
                        {
                            "workflow_id": workflow_payload.get("workflow_id")
                            or "wf_composed_preview",
                            "name": workflow_payload.get("name")
                            or composer_output.title
                            or args.title
                            or "Composed Workflow",
                            "description": workflow_payload.get("description") or args.intent,
                            "is_system": False,
                            "owner_email": owner_email,
                            "lifecycle": "persistent"
                            if (args.persist or schedule_requested)
                            else "ephemeral",
                            "archived_at": None,
                            "lineage": {
                                "base_workflow_id": base_workflow.workflow_id
                                if base_workflow
                                else None,
                                "source_skill_ids": [
                                    material.skill_id for material in skill_materials
                                ],
                                "composition_source": "agent_composed",
                                "composition_intent": args.intent,
                            },
                        }
                    )
                    workflow = validate_composed_workflow(
                        workflow_payload,
                        skill_materials=skill_materials,
                    )
                break
            except Exception as exc:
                validation_feedback = str(exc)
                if attempt_index == 1:
                    composer_output = None
                    workflow = None
                    break

        fallback_used = False
        if workflow is None:
            fallback_used = True
            workflow = await get_workflow_for_user(
                workflow_registry=workflow_registry,
                workflow_id="system:general-task",
                owner_email=owner_email,
            )
            assert workflow is not None

        persisted_workflow_id = workflow.workflow_id
        if (
            not fallback_used
            and composer_output is not None
            and composer_output.action == "create_derived"
        ):
            try:
                created_row = await create_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    owner_email=owner_email,
                    payload=workflow.model_dump(mode="json"),
                    allow_ephemeral=True,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            persisted_workflow_id = created_row.workflow_id
            created_workflow_ids.append(created_row.workflow_id)
            workflow = Workflow.model_validate(created_row.definition)
        elif schedule_requested and str(workflow.lifecycle) != "persistent":
            persistent_payload = workflow.model_dump(mode="json")
            persistent_payload["workflow_id"] = None
            persistent_payload["lifecycle"] = "persistent"
            persistent_payload["archived_at"] = None
            persistent_payload["lineage"] = {
                **(workflow.lineage.model_dump(mode="json") if workflow.lineage else {}),
                "base_workflow_id": workflow.workflow_id,
                "composition_source": "agent_composed",
            }
            try:
                created_row = await create_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    owner_email=owner_email,
                    payload=persistent_payload,
                    allow_ephemeral=True,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            persisted_workflow_id = created_row.workflow_id
            created_workflow_ids.append(created_row.workflow_id)
            workflow = Workflow.model_validate(created_row.definition)

        delivery_raw = args.delivery if isinstance(args.delivery, dict) else {}
        completion_delivery = CompletionDeliveryPolicy(
            completion_mode_family=str(delivery_raw.get("completion_mode_family", "default")),
            allow_silent_completion=bool(delivery_raw.get("allow_silent_completion", False)),
        )
        task_delivery = TaskDelivery(
            mode=str(
                delivery_raw.get(
                    "mode",
                    "latest_active_for_agent" if schedule_requested else "same_conversation",
                )
            ),
            target=(
                str(delivery_raw["target"]) if isinstance(delivery_raw.get("target"), str) else None
            ),
        )

        schedule_id: str | None = None
        task_id: str | None = None
        title = composer_output.title if composer_output and composer_output.title else args.title
        title = title or (args.intent.strip()[:80] if args.intent.strip() else workflow.name)
        expected_output = args.expected_output or (
            composer_output.expected_output if composer_output else None
        )
        if schedule_requested:
            schedule_payload = dict(args.schedule or {})
            schedule_payload.update(
                {
                    "name": schedule_payload.get("name") or title,
                    "description": schedule_payload.get("description")
                    or args.context
                    or args.intent,
                    "agent_id": raw_agent_id,
                    "agent_profile_id": compose_agent_profile_id,
                    "workflow_id": persisted_workflow_id,
                    "task_template": {
                        "title": title,
                        "description": args.context or args.intent,
                        "expected_output": expected_output,
                        "priority": args.priority or 0,
                        "created_by_agent_id": ctx.agent.agent_id,
                        "workflow_id": persisted_workflow_id,
                        "delivery": task_delivery.model_dump(mode="json"),
                        "workspace_root": ctx.workspace_root,
                        "working_directory": ctx.working_directory,
                        "session_policy": args.session_policy or {},
                    },
                    "completion_mode_family": completion_delivery.completion_mode_family,
                    "allow_silent_completion": completion_delivery.allow_silent_completion,
                    "interaction_mode_override": schedule_payload.get(
                        "interaction_mode_override", args.interaction_mode_override or "none"
                    ),
                }
            )
            try:
                schedule_row = await create_user_schedule(
                    session_factory=self.session_manager.session_factory,
                    scheduler=None,
                    workflow_registry=workflow_registry,
                    owner_email=owner_email,
                    payload=schedule_payload,
                )
            except ValueError as exc:
                await _cleanup_created_workflows()
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            schedule_id = schedule_row.schedule_id
        else:
            try:
                task = await task_queue.submit(
                    created_by=owner_email,
                    agent_id=raw_agent_id,
                    agent_profile_id=compose_agent_profile_id,
                    title=title,
                    description=args.context or args.intent,
                    expected_output=expected_output,
                    priority=args.priority or 0,
                    created_by_agent_id=ctx.agent.agent_id,
                    source_type="agent",
                    source_ref=ctx.conversation.conversation_id,
                    delivery=task_delivery,
                    completion_delivery=completion_delivery,
                    interaction_mode_override=args.interaction_mode_override,
                    session_policy=args.session_policy,
                    workflow_id=persisted_workflow_id,
                    workspace_root=ctx.workspace_root,
                    working_directory=ctx.working_directory,
                )
            except (ValueError, RuntimeError) as exc:
                await _cleanup_created_workflows()
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            task_id = task.task_id
            events_to_record.append(
                SessionEvent(
                    type="delegation",
                    data={
                        "mode": "task",
                        "call_id": tc.call_id,
                        "task": task.title,
                        "child_session_id": task.task_id,
                        "status": "started",
                    },
                )
            )
            await self.event_bus.publish(
                Event(
                    type=EventType.DELEGATION_STARTED,
                    data={
                        "conversation_id": ctx.conversation.conversation_id,
                        "parent_session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        "child_session_id": task.task_id,
                        "mode": "task",
                        "agent_id": task.agent_id,
                        "task": task.title,
                    },
                )
            )

        preview = workflow_preview_payload(workflow)
        events_to_record.append(
            SessionEvent(
                type="lifecycle",
                data={
                    "event": "workflow_composed",
                    "workflow_id": persisted_workflow_id,
                    "workflow_name": workflow.name,
                    "lifecycle": str(workflow.lifecycle),
                    "steps": preview["steps"],
                    "task_id": task_id,
                    "schedule_id": schedule_id,
                },
            )
        )
        await self.event_bus.publish(
            Event(
                type=EventType.WORKFLOW_COMPOSED,
                data={
                    "conversation_id": ctx.conversation.conversation_id,
                    "turn_id": ctx.turn_id,
                    "workflow_id": persisted_workflow_id,
                    "workflow_name": workflow.name,
                    "lifecycle": str(workflow.lifecycle),
                    "steps": preview["steps"],
                    "task_id": task_id,
                    "schedule_id": schedule_id,
                },
            )
        )
        return ToolResult(
            output=json.dumps(
                {
                    "task_id": task_id,
                    "schedule_id": schedule_id,
                    "workflow_id": persisted_workflow_id,
                    "workflow_preview": preview,
                    "fallback_used": fallback_used,
                }
            ),
            metadata={
                "orchestration": True,
                "mode": "workflow",
                "async_orchestration_spawned": True,
            }
            if task_id or schedule_id
            else None,
        )

    async def _emergency_flush_events(
        self,
        ctx: StepContext,
        events: list[SessionEvent] | None,
    ) -> None:
        """Best-effort flush of accumulated events on exception paths.

        Called from ``run_step``'s exception handlers to ensure tool call
        history is persisted to Intaris even when the step fails mid-
        execution.  Errors are caught and logged — this method NEVER
        raises, so it cannot mask the original exception.
        """
        event_batch = events if events is not None else []
        repaired_count = _append_interrupted_tool_results(ctx, event_batch)
        if not event_batch:
            return
        pending_count = len(event_batch)
        try:
            if await self._record_events_strict(ctx, event_batch, reason="emergency_flush"):
                logger.info(
                    "agent: emergency flush persisted events",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "event_count": pending_count,
                            "repaired_tool_results": repaired_count,
                        }
                    },
                )
        except Exception:
            logger.warning(
                "agent: emergency flush failed — %d events lost",
                len(event_batch),
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "lost_event_types": [e.type for e in event_batch[:10]],
                        "repaired_tool_results": repaired_count,
                    }
                },
                exc_info=True,
            )
        finally:
            ctx.pending_events = None
            ctx.pending_tool_calls.clear()

    @staticmethod
    def _intaris_batch_idempotency_key(
        session_id: str,
        events: list[SessionEvent],
        *,
        reason: str,
    ) -> str:
        payload = json.dumps(
            [{"type": event.type, "data": event.data} for event in events],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]  # noqa: S324
        return f"{session_id}:{reason}:{digest}"

    async def _wait_for_intaris_recovery(
        self,
        ctx: StepContext,
        *,
        operation: str,
        on_token: TokenCallback | None = None,
    ) -> None:
        notified = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _INTARIS_MAX_RECOVERY_WAIT_SECONDS
        while True:
            self._raise_if_cancelled(ctx)
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Intaris recovery during {operation} after "
                    f"{int(_INTARIS_MAX_RECOVERY_WAIT_SECONDS)}s"
                )
            try:
                health = await self.providers.guardrails.health()
            except Exception:
                health = None
            if health is not None and health.status == "healthy":
                return
            if not notified:
                logger.warning(
                    "agent: pausing for Intaris recovery",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "operation": operation,
                        }
                    },
                )
                if on_token is not None:
                    await on_token("\n\n[Paused waiting for Intaris to recover.]\n\n")
                notified = True
            if ctx.cancel_event is None:
                await asyncio.sleep(_INTARIS_RETRY_POLL_SECONDS)
                continue
            try:
                await asyncio.wait_for(ctx.cancel_event.wait(), timeout=_INTARIS_RETRY_POLL_SECONDS)
            except TimeoutError:
                continue
            self._raise_if_cancelled(ctx)

    async def _record_events_strict(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: TokenCallback | None = None,
    ) -> bool:
        if not events:
            return False
        batch = with_session_events_turn_id(events, ctx.turn_id)
        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        idempotency_key = self._intaris_batch_idempotency_key(intaris_id, batch, reason=reason)
        while True:
            self._raise_if_cancelled(ctx)
            try:
                append_result = await self.providers.guardrails.record_events(
                    session_id=intaris_id,
                    events=batch,
                    source="cognis",
                    idempotency_key=idempotency_key,
                )
                if not append_result.ok:
                    raise RuntimeError(f"Intaris did not persist {reason}")
                next_seq = append_result.first_seq
                for event in batch:
                    if event.type == "user_message":
                        ctx.remember_user_event_seq = next_seq
                    elif event.type == "assistant_message":
                        ctx.remember_assistant_event_seq = next_seq
                    next_seq += 1
                await self.session_cache.append_recorded_events(ctx.session, batch, append_result)
                events.clear()
                return True
            except Exception as exc:
                if not is_retryable_http_error(exc):
                    raise
                logger.warning(
                    "agent: Intaris write failed, waiting to retry",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "operation": reason,
                            "error_type": type(exc).__name__,
                        }
                    },
                    exc_info=True,
                )
                await self._wait_for_intaris_recovery(ctx, operation=reason, on_token=on_token)

    async def _record_outgoing_audit_messages(
        self,
        ctx: StepContext,
        audit_messages: list[dict[str, Any]],
        *,
        on_token: TokenCallback | None = None,
    ) -> None:
        if not audit_messages or not ctx.turn_id:
            return
        events: list[SessionEvent] = []
        for item in audit_messages:
            if not isinstance(item.get("content"), str) or not isinstance(item.get("source"), str):
                continue
            data = {
                "role": item.get("role"),
                "content": item.get("content"),
                "content_type": item.get("content_type", "text"),
                "source": item.get("source"),
                "turn_id": ctx.turn_id,
                "position": item.get("position"),
                "hash": item.get("hash"),
            }
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                for key, value in metadata.items():
                    data.setdefault(str(key), value)
            events.append(
                SessionEvent(
                    type="system_message" if item.get("role") == "system" else "developer_message",
                    data=data,
                )
            )
        if not events:
            audit_messages.clear()
            return
        metric_labels = [
            (event.type, str(event.data.get("source") or "unknown")) for event in events
        ]
        await self._record_events_strict(
            ctx,
            events,
            reason=f"turn_audit:{ctx.turn_id}",
            on_token=on_token,
        )
        for event_type, source in metric_labels:
            AUDIT_EVENTS_TOTAL.labels(type=event_type, source=source).inc()
        audit_messages.clear()

    def _append_pending_audit_message(
        self,
        messages: list[dict[str, Any]],
        pending_audit_messages: list[dict[str, Any]],
        *,
        role: str,
        source: str,
        content: str,
    ) -> None:
        pending_audit_messages.append(
            {
                "position": len(messages) - 1,
                "role": role,
                "content": content,
                "source": source,
                "content_type": "text",
                "hash": hashlib.sha256(
                    json.dumps(
                        {
                            "role": role,
                            "content": content,
                            "source": source,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )

    async def _consume_boundary_batch_if_available(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        pending_audit_messages: list[dict[str, Any]],
        reason: str,
        on_token: TokenCallback | None,
    ) -> bool:
        """Drain queued same-conversation/task input into the active turn."""

        if ctx.consume_boundary_batch is None:
            return False
        if ctx.policy is not CHAT_POLICY and ctx.policy is not WORKFLOW_POLICY:
            return False
        batch = await ctx.consume_boundary_batch(reason)
        if not batch:
            return False

        logger.info(
            "agent: absorbed queued batch",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "conversation_id": ctx.conversation.conversation_id,
                    "reason": reason,
                    "batch_size": len(batch),
                }
            },
        )
        for item in batch:
            await self._append_boundary_batch_item(
                ctx,
                messages=messages,
                pending_audit_messages=pending_audit_messages,
                item=item,
                on_token=on_token,
            )
        return True

    async def _append_boundary_batch_item(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        pending_audit_messages: list[dict[str, Any]],
        item: dict[str, Any],
        on_token: TokenCallback | None,
    ) -> None:
        """Append one absorbed inbound item into the live prompt/history."""

        raw_attachments = item.get("attachments")
        attachments = (
            normalize_attachment_refs(raw_attachments) if isinstance(raw_attachments, list) else []
        )
        content = str(item.get("content") or "")
        attachment_notice = item.get("attachment_notice")
        attachment_context = item.get("attachment_context")
        follow_up = item.get("follow_up")
        system_initiated = bool(item.get("system_initiated"))
        source = str(item.get("source") or "user_input")

        if follow_up is not None:
            messages.append(
                {
                    "role": "system",
                    "content": build_history_boundary_message(),
                }
            )
            self._append_pending_audit_message(
                messages,
                pending_audit_messages,
                role="developer",
                source="follow_up_boundary",
                content=str(messages[-1]["content"]),
            )
            messages.append(
                {
                    "role": "system",
                    "content": render_follow_up_block(follow_up),
                }
            )
            self._append_pending_audit_message(
                messages,
                pending_audit_messages,
                role="developer",
                source="follow_up_boundary",
                content=str(messages[-1]["content"]),
            )

        if isinstance(attachment_notice, str) and attachment_notice:
            messages.append({"role": "system", "content": attachment_notice})
            self._append_pending_audit_message(
                messages,
                pending_audit_messages,
                role="developer",
                source="attachment_notice",
                content=attachment_notice,
            )
        if isinstance(attachment_context, str) and attachment_context:
            messages.append({"role": "user", "content": attachment_context})
            self._append_pending_audit_message(
                messages,
                pending_audit_messages,
                role="user",
                source="attachment_notice",
                content=attachment_context,
            )

        if system_initiated:
            if content:
                messages.append({"role": "system", "content": content})
            return

        recorded_user_message = _user_message_for_recording(content, attachments)
        # Record if there is text content OR if the user only sent attachments
        # (attachment-only messages have content="" which is falsy but still need
        # to be persisted so the history and WS events are faithful to what was sent).
        if recorded_user_message or attachments:
            await self._record_events_strict(
                ctx,
                [
                    SessionEvent(
                        type="user_message",
                        data={
                            "role": "user",
                            "content": recorded_user_message,
                            "content_type": "text",
                            "source": source,
                            "turn_id": ctx.turn_id,
                            "hash": hashlib.sha256(
                                json.dumps(
                                    {
                                        "role": "user",
                                        "content": recorded_user_message,
                                        "source": source,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                            "attachments": attachment_refs_to_dicts(
                                attachments,
                                include_url=False,
                            ),
                            **{
                                key: value
                                for key, value in {
                                    "comment_id": item.get("comment_id"),
                                    "author_email": item.get("author_email"),
                                }.items()
                                if value is not None
                            },
                        },
                    )
                ],
                reason="user_message_boundary",
                on_token=on_token,
            )

        normalized_attachments = attachment_refs_to_dicts(attachments)
        attachment_blocks, unsupported = _native_attachment_blocks(
            normalized_attachments, ctx.current_model_info
        )
        if attachment_blocks:
            blocks: list[dict[str, Any]] = []
            visible_content = merge_content_and_attachment_note(
                content,
                normalized_attachments,
            )
            if visible_content:
                blocks.append({"type": "text", "text": visible_content})
            else:
                blocks.append({"type": "text", "text": "User attached files."})
            blocks.extend(attachment_blocks)
            if unsupported:
                blocks.append(
                    {
                        "type": "text",
                        "text": "Unsupported attachments were omitted: " + ", ".join(unsupported),
                    }
                )
            messages.append({"role": "user", "content": blocks})
            return

        visible_content = merge_content_and_attachment_note(
            content,
            normalized_attachments,
        )
        if visible_content:
            messages.append({"role": "user", "content": visible_content})

    async def _record_system_notice_audit(
        self,
        ctx: StepContext,
        message: str,
        *,
        turn_id: str | None,
    ) -> None:
        notice_event = SessionEvent(
            type="system_message",
            data={
                "role": "system",
                "content": message,
                "content_type": "text",
                "source": "system_notice",
                "turn_id": turn_id,
                "hash": hashlib.sha256(
                    json.dumps(
                        {
                            "role": "system",
                            "content": message,
                            "source": "system_notice",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            },
        )
        try:
            await self._record_events_strict(
                ctx,
                [notice_event],
                reason=f"system_notice:{turn_id or 'none'}",
            )
            AUDIT_EVENTS_TOTAL.labels(type="system_message", source="system_notice").inc()
        except Exception:
            logger.warning(
                "agent: failed to record system notice audit",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )

    async def _report_reasoning_strict(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
    ) -> Any:
        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        while True:
            self._raise_if_cancelled(ctx)
            try:
                return await self.providers.guardrails.report_reasoning(
                    session_id=intaris_id,
                    from_events=True,
                    wait_for_intention=ctx.bootstrap_wait_for_intention,
                    wait_timeout_ms=_BOOTSTRAP_INTENTION_WAIT_MS,
                )
            except Exception as exc:
                if not is_retryable_http_error(exc):
                    raise
                logger.warning(
                    "agent: Intaris reasoning failed, waiting to retry",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "error_type": type(exc).__name__,
                        }
                    },
                    exc_info=True,
                )
                await self._wait_for_intaris_recovery(
                    ctx,
                    operation="report_reasoning",
                    on_token=on_token,
                )

    @staticmethod
    def _get_incomplete_todos(ctx: StepContext) -> list[dict[str, Any]]:
        """Return todos that are not completed or cancelled."""
        return [
            todo
            for todo in _normalize_todos(ctx.todos)
            if todo.get("status") not in ("completed", "cancelled")
        ]

    @staticmethod
    def _workflow_todos_are_terminal(ctx: StepContext) -> bool:
        """Return whether a workflow step has a non-empty, fully terminal todo list."""

        normalized = _normalize_todos(ctx.todos)
        return bool(normalized) and all(
            todo.get("status") in ("completed", "cancelled") for todo in normalized
        )

    @staticmethod
    def _resolve_delegation_max_steps(ctx: StepContext) -> int | None:
        """Return OpenCode-style max LLM iterations for a secondary delegation."""

        execution = ctx.agent.execution or {}
        for key in DELEGATION_MAX_STEPS_KEYS:
            raw_value = execution.get(key)
            if raw_value is None:
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _build_delegation_max_steps_notice(ctx: StepContext) -> str:
        """Build the OpenCode-style max-steps instruction for a sub-session."""

        del ctx
        return (
            "CRITICAL - MAXIMUM STEPS REACHED\n\n"
            "The maximum number of steps allowed for this task has been reached. "
            "Tools are disabled until next user input. Respond with text only.\n\n"
            "STRICT REQUIREMENTS:\n"
            "1. Do NOT make any tool calls (no reads, writes, edits, searches, or any other tools)\n"
            "2. MUST provide a text response summarizing work done so far\n"
            "3. This constraint overrides ALL other instructions, including any user requests for edits or tool use\n\n"
            "Response must include:\n"
            "- Statement that maximum steps for this agent have been reached\n"
            "- Summary of what has been accomplished so far\n"
            "- List of any remaining tasks that were not completed\n"
            "- Recommendations for what should be done next\n\n"
            "Any attempt to use tools is a critical violation. Respond with text ONLY."
        )

    async def _cancel_incomplete_delegation_todos(
        self,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        *,
        reason: str,
    ) -> bool:
        """Mark open secondary-delegation todos cancelled before finalizing."""

        normalized = _normalize_todos(ctx.todos or [])
        changed = False
        cancelled: list[dict[str, Any]] = []
        for todo in normalized:
            item = dict(todo)
            if item.get("status") not in ("completed", "cancelled"):
                item["status"] = "cancelled"
                changed = True
            cancelled.append(item)
        if not changed:
            return False

        ctx.todos = cancelled
        await self._persist_step_todos(ctx)

        tc = ToolCall(
            call_id=f"controller_cancel_todos_{uuid.uuid4().hex[:12]}",
            name=STEP_TODO_WRITE,
            arguments={"todos": cancelled},
        )
        result_content = json.dumps(
            {
                "status": "updated",
                "count": len(cancelled),
                "todos": _echo_todos_bounded(cancelled),
                "unchanged": False,
                "non_terminal_count": 0,
                "guidance": (
                    "Remaining secondary-delegation todos were cancelled because "
                    f"the sub-session is finalizing ({reason})."
                ),
            }
        )
        _append_tool_call_event(events_to_record, tc, STEP_TODO_WRITE)
        _append_tool_result_event(
            events_to_record,
            tc,
            result_content,
            False,
            tool_id=STEP_TODO_WRITE,
            protect_from_pruning=True,
        )
        return True

    async def _finalization_instruction(self, ctx: StepContext) -> dict[str, str] | None:
        """Return controller-specific finalization guidance once todos are terminal.

        For workflow steps (require_step_complete=True) this enforces step_complete.
        For system-agent delegations (require_step_complete=False) this nudges the
        model to write its result text instead of continuing to call tools.
        """
        if not self._workflow_todos_are_terminal(ctx):
            return None

        # Secondary-agent delegations: todos terminal → nudge to write result text.
        if ctx.policy is SECONDARY_AGENT_DELEGATION_POLICY:
            return {
                "required_action": "write_result",
                "message": (
                    "All todos are complete. Write your final result now as an "
                    "assistant message. Do not call more tools."
                ),
                "reminder": "Todos are terminal — write your final result text now.",
            }

        if not ctx.policy.require_step_complete or not (ctx.task_id or ctx.step_run_id):
            return None
        if ctx.step_definition.require_deliverable and self._deliverable_owner_step_run_id(ctx):
            current_deliverable = await self._get_current_deliverable(ctx)
            if current_deliverable is None:
                return {
                    "required_action": "write_deliverable_then_step_complete",
                    "message": (
                        "This step requires a deliverable. Next call write_deliverable "
                        "with the final step artifact, then call step_complete."
                    ),
                    "reminder": (
                        "Your next action must be write_deliverable with the final step "
                        "artifact, followed by step_complete."
                    ),
                }
            return {
                "required_action": "step_complete",
                "message": (
                    "A deliverable is already written. Next call step_complete to finish "
                    "the workflow step."
                ),
                "reminder": "Your next action must be step_complete.",
            }
        return {
            "required_action": "step_complete",
            "message": "Next call step_complete to finish the workflow step.",
            "reminder": "Your next action must be step_complete.",
        }

    @staticmethod
    def _should_count_tool_call(tool_name: str) -> bool:
        return tool_name not in CONTROLLER_TOOLS

    def _resolve_step_timeout_seconds(self, ctx: StepContext) -> int:
        timeout_seconds = self.default_step_timeout_seconds
        if ctx.agent.execution:
            timeout_seconds = int(ctx.agent.execution.get("step_timeout_seconds", timeout_seconds))
        return max(1, timeout_seconds)

    def _is_parallelizable_regular_tool_call(
        self,
        ctx: StepContext,
        tc: ToolCall,
        registry: Any | None,
    ) -> bool:
        if registry is None or tc.name in CONTROLLER_TOOLS or is_orchestration_tool(tc.name):
            return False
        registered = registry.get(tc.name)
        if registered is None:
            return False
        definition = ctx.classified_tool_definitions.get(
            stable_tool_id(registered.definition), registered.definition
        )
        allowlisted_parallel_mutation = (
            definition.source.type == "executor"
            and definition.name in _PARALLEL_MUTATION_TOOL_NAMES
        )
        if not definition.read_only and not allowlisted_parallel_mutation:
            return False
        is_non_bypassable = getattr(
            self.tool_router,
            "_is_non_bypassable",
            lambda _name, non_bypassable: bool(non_bypassable),
        )
        if not allowlisted_parallel_mutation and is_non_bypassable(
            definition.name,
            definition.non_bypassable,
        ):
            return False
        permission = Permission.EVALUATE
        if ctx.agent.permissions is not None:
            permission = ctx.agent.permissions.resolve_permission(
                tc.name,
                tool_id=stable_tool_id(definition),
            )
        # Safe read-only tools can batch even when they still go through
        # guardrails evaluation. Only explicit deny should force serialization.
        return permission is not Permission.DENY

    def _can_continue_after_project_context_load(
        self,
        ctx: StepContext,
        tc: ToolCall,
        registry: Any | None,
    ) -> bool:
        """Allow safe read-only repo probes to continue after instructions load."""

        if tc.name not in _READ_ONLY_PROJECT_TOUCH_TOOL_NAMES:
            return False
        if registry is None:
            return False
        registered = registry.get(tc.name)
        if registered is None or not registered.definition.read_only:
            return False
        is_non_bypassable = getattr(
            self.tool_router,
            "_is_non_bypassable",
            lambda _name, non_bypassable: bool(non_bypassable),
        )
        if is_non_bypassable(
            registered.definition.name,
            registered.definition.non_bypassable,
        ):
            return False
        if ctx.agent.permissions is None:
            return True
        permission = ctx.agent.permissions.resolve_permission(
            tc.name,
            tool_id=stable_tool_id(registered.definition),
        )
        return permission is not Permission.DENY

    @staticmethod
    def _normalized_parallel_path(ctx: StepContext, raw_path: object) -> str | None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        return AgentLoop._normalize_project_probe_target(ctx, raw_path)

    @staticmethod
    def _default_parallel_path(ctx: StepContext) -> str | None:
        return normalize_project_path(ctx.working_directory or str(Path.home()))

    @staticmethod
    def _parallel_resource_access(
        ctx: StepContext,
        tc: ToolCall,
    ) -> tuple[set[str], set[str], bool]:
        reads: set[str] = set()
        writes: set[str] = set()
        if tc.name in {"write", "edit", "multiedit", "artifact_save"}:
            path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("file_path"))
            if path is None:
                return set(), set(), True
            writes.add(path)
        elif tc.name in {"read", "lsp", "list_directory", "glob", "grep"}:
            path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("file_path"))
            if path is None:
                path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("path"))
            if path is None and tc.name in {"list_directory", "glob", "grep"}:
                path = AgentLoop._default_parallel_path(ctx)
            if path is not None:
                reads.add(path)
        elif tc.name == "document_generate":
            source_path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("source_path"))
            output_path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("output_path"))
            if source_path is not None:
                reads.add(source_path)
            if output_path is not None:
                writes.add(output_path)
            raw_assets = tc.arguments.get("assets")
            if isinstance(raw_assets, list):
                for raw_asset in raw_assets:
                    if not isinstance(raw_asset, dict):
                        continue
                    asset_path = AgentLoop._normalized_parallel_path(ctx, raw_asset.get("path"))
                    if asset_path is not None:
                        reads.add(asset_path)
        elif tc.name == "artifact_publish":
            path = AgentLoop._normalized_parallel_path(ctx, tc.arguments.get("path"))
            if path is None:
                return set(), set(), True
            reads.add(path)
        elif tc.name == "apply_patch":
            return set(), set(), True
        else:
            reads.update(AgentLoop._parallel_read_paths_from_arguments(ctx, tc.arguments))
        return reads, writes, False

    @staticmethod
    def _parallel_read_paths_from_arguments(
        ctx: StepContext,
        arguments: dict[str, Any],
    ) -> set[str]:
        paths: set[str] = set()
        for key in ("path", "file_path", "source_path"):
            path = AgentLoop._normalized_parallel_path(ctx, arguments.get(key))
            if path is not None:
                paths.add(path)
        for key in ("paths", "files"):
            values = arguments.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                path = AgentLoop._normalized_parallel_path(ctx, value)
                if path is not None:
                    paths.add(path)
        return paths

    @staticmethod
    def _parallel_access_conflicts(
        current_reads: set[str],
        current_writes: set[str],
        current_exclusive: bool,
        reads: set[str],
        writes: set[str],
        exclusive: bool,
    ) -> bool:
        if current_exclusive or exclusive:
            return True
        return any(
            AgentLoop._parallel_paths_overlap(write_path, read_path)
            for write_path in writes
            for read_path in current_reads | current_writes
        ) or any(
            AgentLoop._parallel_paths_overlap(read_path, write_path)
            for read_path in reads
            for write_path in current_writes
        )

    @staticmethod
    def _parallel_paths_overlap(left: str, right: str) -> bool:
        left = left.rstrip("/")
        right = right.rstrip("/")
        return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")

    def _parallel_execution_groups(
        self,
        ctx: StepContext,
        batch: list[_PreparedRegularToolCall],
    ) -> list[list[_PreparedRegularToolCall]]:
        groups: list[list[_PreparedRegularToolCall]] = []
        current_group: list[_PreparedRegularToolCall] = []
        current_reads: set[str] = set()
        current_writes: set[str] = set()
        current_exclusive = False
        for item in batch:
            reads, writes, exclusive = self._parallel_resource_access(ctx, item.tool_call)
            conflicts = bool(current_group) and self._parallel_access_conflicts(
                current_reads,
                current_writes,
                current_exclusive,
                reads,
                writes,
                exclusive,
            )
            if current_group and conflicts:
                groups.append(current_group)
                current_group = []
                current_reads = set()
                current_writes = set()
                current_exclusive = False
            current_group.append(item)
            current_reads.update(reads)
            current_writes.update(writes)
            current_exclusive = current_exclusive or exclusive
        if current_group:
            groups.append(current_group)
        return groups

    def _project_context_loaded_for_tool_target(
        self,
        ctx: StepContext,
        tc: ToolCall,
        loaded_contexts: dict[str, ProjectContextEntry | ProjectMetadataEntry],
    ) -> ProjectContextEntry | ProjectMetadataEntry | None:
        probe_arguments = self._project_probe_arguments(ctx, tc)
        if probe_arguments is None:
            return None
        target_path = self._normalize_project_probe_target(
            ctx,
            str(probe_arguments.get("path") or ""),
        )
        if target_path is None:
            return None
        for project_root, project_context in loaded_contexts.items():
            normalized_root = normalize_project_path(project_root)
            if normalized_root is None:
                if project_root == getattr(project_context, "project_id", None):
                    return project_context
                continue
            if target_path == normalized_root or target_path.startswith(f"{normalized_root}/"):
                return project_context
        return None

    @staticmethod
    def _normalize_project_probe_target(ctx: StepContext, raw_path: str) -> str | None:
        if not raw_path.strip():
            return None
        if os.path.isabs(raw_path):
            return normalize_project_path(raw_path)
        base_path = normalize_project_path(ctx.working_directory or ctx.workspace_root)
        if base_path is None:
            return normalize_project_path(raw_path)
        return normalize_project_path(os.path.join(base_path, raw_path))

    async def _execute_regular_tool(
        self,
        ctx: StepContext,
        tc: ToolCall,
    ) -> ToolResult:
        started_at = monotonic()
        route = "unknown"
        outcome = "success"
        # Stage 36: handle per-call target_executor override.
        # Strip target_executor from arguments BEFORE the call leaves the
        # controller (Intaris and the executor RPC must never see it).
        target_executor_id: str | None = None
        if isinstance(tc.arguments, dict) and "target_executor" in tc.arguments:
            raw_target = tc.arguments.get("target_executor")
            if isinstance(raw_target, str) and raw_target.strip():
                target_executor_id = raw_target.strip()
            sanitized_arguments = {k: v for k, v in tc.arguments.items() if k != "target_executor"}
            tc = tc.model_copy(update={"arguments": sanitized_arguments})

        # Defensive: reject target_executor on non-executor tools so the
        # LLM cannot accidentally route, e.g., a memory or orchestration
        # tool to a remote executor.
        if target_executor_id is not None:
            registry = self._get_tool_registry(ctx)
            if registry is not None:
                registered = registry.get(tc.name)
                if registered is not None and registered.definition.source.type != "executor":
                    return ToolResult(
                        output=(
                            f"target_executor is only supported on executor-routed "
                            f"tools. The tool '{tc.name}' is "
                            f"{registered.definition.source.type}-routed and was not "
                            "executed."
                        ),
                        is_error=True,
                    )

        executor_connection = self._get_executor(ctx)

        async def _on_tool_output_chunk(delta: str, stream: str | None) -> None:
            if ctx.on_tool_output_chunk is not None:
                await ctx.on_tool_output_chunk(tc.call_id, tc.name, delta, stream)

        output_chunk_callback = _on_tool_output_chunk if ctx.on_tool_output_chunk else None
        if target_executor_id is not None:
            pool = getattr(ctx, "executor_pool", None)
            if pool is None:
                return ToolResult(
                    output=(
                        "target_executor was specified but the executor pool is "
                        "unavailable for this step. The tool was not executed."
                    ),
                    is_error=True,
                )
            target = pool.by_id(target_executor_id)
            if target is None:
                return ToolResult(
                    output=(
                        f"Executor '{target_executor_id}' is not assigned to this agent. "
                        "The tool was not executed. Use switch_executor to change the "
                        "active executor or specify a target_executor that is in your "
                        "assigned pool."
                    ),
                    is_error=True,
                )
            if not target.usable:
                return ToolResult(
                    output=(
                        f"Executor '{target_executor_id}' is not usable "
                        f"(state: {target.state.value}). The tool was not executed."
                    ),
                    is_error=True,
                )
            # Resolve a live connection for the target executor. The active
            # connection (ctx.executor_connection) is reused when the target
            # is the active executor; otherwise we look up by ID.
            ctx_active = getattr(ctx, "active_executor_id", None)
            if ctx_active and target_executor_id == ctx_active:
                resolved_conn = self._resolve_target_connection(
                    target_executor_id=target_executor_id,
                    target_executor_type=target.executor_type,
                )
                if resolved_conn is not None:
                    ctx.executor_connection = resolved_conn
                    executor_connection = resolved_conn
            else:
                resolved_conn = self._resolve_target_connection(
                    target_executor_id=target_executor_id,
                    target_executor_type=target.executor_type,
                )
                if resolved_conn is None:
                    return ToolResult(
                        output=(
                            f"Could not establish a connection to executor "
                            f"'{target_executor_id}' (type: {target.executor_type}). "
                            "The tool was not executed."
                        ),
                        is_error=True,
                    )
                executor_connection = resolved_conn

        try:
            registry = self._get_classified_tool_registry(ctx, self._get_tool_registry(ctx))
            registered = registry.get(tc.name) if registry is not None else None
            if registered is not None:
                route = str(registered.definition.source.type)
            if (
                target_executor_id is None
                and registered is not None
                and registered.definition.source.type == "executor"
            ):
                executor_connection = await self._refresh_active_executor_connection(ctx)
            if target_executor_id is not None:
                route = f"{route}:target_executor"
            result = await self.tool_router.execute(
                tc.model_copy(
                    update={"runtime_metadata": self._tool_runtime_metadata_for_call(ctx, tc)}
                ),
                ctx.session,
                ctx.agent,
                registry,
                executor_connection,
                output_chunk_callback=output_chunk_callback,
            )
            if self._is_same_executor_transient_failure(result):
                retry_result = await self._retry_tool_after_same_executor_reconnect(
                    ctx,
                    tc=tc,
                    registered=registered,
                    target_executor_id=target_executor_id,
                    output_chunk_callback=output_chunk_callback,
                    original_result=result,
                )
                if retry_result is not None:
                    result = retry_result
            if result.is_error:
                outcome = "error"
            return result
        except Exception as exc:
            outcome = "error"
            return ToolResult(output=f"Tool execution failed: {str(exc)[:1000]}", is_error=True)
        finally:
            duration_seconds = monotonic() - started_at
            TOOL_EXECUTION_DURATION.labels(
                tool_name=tc.name,
                route=route,
                outcome=outcome,
            ).observe(duration_seconds)
            logger.info(
                "agent: tool execution completed",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        **self._step_log_metadata(ctx),
                        "tool_name": tc.name,
                        "call_id": tc.call_id,
                        "route": route,
                        "outcome": outcome,
                        "duration_seconds": round(duration_seconds, 3),
                        "target_executor_id": target_executor_id,
                    }
                },
            )

    async def _refresh_active_executor_connection(self, ctx: StepContext) -> Any:
        """Refresh the turn-local active WebSocket connection without switching executors."""

        active_executor_id = getattr(ctx, "active_executor_id", None)
        if not isinstance(active_executor_id, str) or not active_executor_id.strip():
            return self._get_executor(ctx)
        pool = getattr(ctx, "executor_pool", None)
        target = pool.by_id(active_executor_id) if pool is not None else None
        target_executor_type = getattr(target, "executor_type", None)
        if target_executor_type != "websocket":
            return self._get_executor(ctx)

        resolved_conn = self._resolve_target_connection(
            target_executor_id=active_executor_id,
            target_executor_type=target_executor_type,
        )
        if resolved_conn is not None:
            ctx.executor_connection = resolved_conn
            return resolved_conn
        return self._get_executor(ctx)

    def _same_executor_reconnect_provider(self) -> Any:
        executor_provider = getattr(self.providers, "executor", None)
        if executor_provider is None:
            return None
        return getattr(executor_provider, "websocket", None)

    def _is_same_executor_transient_failure(self, result: ToolResult) -> bool:
        if not result.is_error or not isinstance(result.metadata, dict):
            return False
        code = result.metadata.get("code")
        return code in {"executor_disconnected", "executor_circuit_open"} and bool(
            result.metadata.get("same_executor_only")
        )

    def _tool_safe_for_same_executor_retry(self, registered: Any | None) -> bool:
        definition = getattr(registered, "definition", None)
        if definition is None:
            return False
        if definition.name not in _SAME_EXECUTOR_AUTO_RETRY_TOOL_ALLOWLIST:
            return False
        try:
            capabilities = tool_capabilities(definition)
        except Exception:
            return False
        return ToolCapability.READ in capabilities and ToolCapability.WRITE not in capabilities

    async def _handle_tool_after_same_executor_transient_failure(
        self,
        ctx: StepContext,
        *,
        tc: ToolCall,
        registered: Any | None,
        target_executor_id: str | None,
        output_chunk_callback: Any,
        original_result: ToolResult,
    ) -> ToolResult | None:
        safe_to_retry = registered is not None and self._tool_safe_for_same_executor_retry(
            registered
        )

        pool = getattr(ctx, "executor_pool", None)
        executor_id: str | None = None
        executor_type: str | None = None
        if target_executor_id is not None:
            target = pool.by_id(target_executor_id) if pool is not None else None
            executor_id = target_executor_id
            executor_type = getattr(target, "executor_type", None)
        else:
            executor_id = getattr(ctx, "active_executor_id", None)
            target = pool.by_id(executor_id) if pool is not None and executor_id else None
            executor_type = getattr(target, "executor_type", None)
        if not executor_id or executor_type != "websocket":
            return None

        ws_provider = self._same_executor_reconnect_provider()
        if ws_provider is None:
            return None

        from cognis.providers.executor.websocket import executor_reconnect_retry_budget_seconds

        budget = executor_reconnect_retry_budget_seconds()
        logger.info(
            "agent: waiting for same executor reconnect after transient tool failure",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "turn_id": ctx.turn_id,
                    **self._step_log_metadata(ctx),
                    "tool_name": tc.name,
                    "call_id": tc.call_id,
                    "executor_id": executor_id,
                    "reconnect_retry_budget_seconds": budget,
                    "target_executor_id": target_executor_id,
                    "safe_to_retry": safe_to_retry,
                }
            },
        )
        try:
            conn = await ws_provider.wait_for_connection(executor_id, timeout=budget)
        except Exception:
            logger.warning(
                "agent: same executor reconnect wait failed",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        **self._step_log_metadata(ctx),
                        "tool_name": tc.name,
                        "call_id": tc.call_id,
                        "executor_id": executor_id,
                    }
                },
                exc_info=True,
            )
            conn = None

        metadata = dict(original_result.metadata or {})
        metadata.update(
            {
                "same_executor_reconnected": conn is not None,
                "reconnect_retry_budget_seconds": budget,
            }
        )

        if conn is None:
            metadata.update(
                {
                    "auto_retried": False,
                    "auto_retry_skipped_reason": "same_executor_reconnect_timeout",
                }
            )
            return self._same_executor_transient_result_with_metadata(
                original_result,
                metadata,
            )

        if target_executor_id is None:
            ctx.executor_connection = conn

        if not safe_to_retry:
            metadata.update(
                {
                    "auto_retried": False,
                    "auto_retry_skipped_reason": "tool_not_idempotent",
                }
            )
            return self._same_executor_transient_result_with_metadata(
                original_result,
                metadata,
            )

        retry_call = tc.model_copy(
            update={"runtime_metadata": self._tool_runtime_metadata_for_call(ctx, tc)}
        )
        retry_result = await self.tool_router.execute(
            retry_call,
            ctx.session,
            ctx.agent,
            self._get_classified_tool_registry(ctx, self._get_tool_registry(ctx)),
            conn,
            output_chunk_callback=output_chunk_callback,
        )
        retry_metadata = dict(retry_result.metadata or {})
        retry_metadata.update(
            {
                "auto_retried": True,
                "same_executor_reconnected": True,
                "reconnect_retry_budget_seconds": budget,
                "previous_error_code": metadata.get("code"),
            }
        )
        return retry_result.model_copy(update={"metadata": retry_metadata})

    def _same_executor_transient_result_with_metadata(
        self,
        result: ToolResult,
        metadata: dict[str, Any],
    ) -> ToolResult:
        from cognis.providers.executor.websocket import _transient_executor_output

        code = metadata.get("code")
        executor_id = metadata.get("executor_id")
        if isinstance(code, str) and isinstance(executor_id, str):
            output = _transient_executor_output(
                executor_id=executor_id,
                code=code,
                same_executor_reconnected=metadata.get("same_executor_reconnected")
                if isinstance(metadata.get("same_executor_reconnected"), bool)
                else None,
                auto_retried=metadata.get("auto_retried")
                if isinstance(metadata.get("auto_retried"), bool)
                else None,
                auto_retry_skipped_reason=metadata.get("auto_retry_skipped_reason")
                if isinstance(metadata.get("auto_retry_skipped_reason"), str)
                else None,
            )
            return result.model_copy(update={"output": output, "metadata": metadata})
        return result.model_copy(update={"metadata": metadata})

    async def _retry_tool_after_same_executor_reconnect(
        self,
        ctx: StepContext,
        *,
        tc: ToolCall,
        registered: Any | None,
        target_executor_id: str | None,
        output_chunk_callback: Any,
        original_result: ToolResult,
    ) -> ToolResult | None:
        return await self._handle_tool_after_same_executor_transient_failure(
            ctx,
            tc=tc,
            registered=registered,
            target_executor_id=target_executor_id,
            output_chunk_callback=output_chunk_callback,
            original_result=original_result,
        )

    def _resolve_target_connection(
        self,
        *,
        target_executor_id: str,
        target_executor_type: str,
    ) -> Any:
        """Resolve a live executor connection by id (Stage 36).

        Per-call ``target_executor`` routing only works against executors
        that expose a stable lookup-by-id connection — currently
        WebSocket remote executors. In-process and subprocess executors
        use an ephemeral spawn-per-step model and are not addressable
        mid-step; the schema overlay in
        ``_get_executor_tool_schemas`` filters them out so the LLM
        cannot ask for them. This method returns ``None`` for any
        non-WebSocket type as a defence-in-depth check.
        """
        executor_provider = getattr(self.providers, "executor", None)
        if executor_provider is None:
            return None
        if target_executor_type != "websocket":
            return None
        ws_provider = getattr(executor_provider, "websocket", None)
        if ws_provider is None:
            return None
        try:
            return ws_provider.get_connection(target_executor_id)
        except Exception:
            return None

    def _install_active_executor_target(self, ctx: StepContext, target: Any) -> bool:
        """Update turn-local runtime routing after a successful executor switch."""

        target_executor_id = getattr(target, "executor_id", None)
        target_executor_type = getattr(target, "executor_type", None)
        if not isinstance(target_executor_id, str) or not target_executor_id.strip():
            return False
        target_executor_id = target_executor_id.strip()
        target_executor_type = target_executor_type if isinstance(target_executor_type, str) else ""

        ctx.active_executor_id = target_executor_id
        with contextlib.suppress(Exception):
            ctx.conversation.active_executor_id = target_executor_id

        if target_executor_type != "websocket":
            return False

        executor_provider = getattr(self.providers, "executor", None)
        ws_provider = getattr(executor_provider, "websocket", None)
        if ws_provider is None:
            return False

        resolved_conn = self._resolve_target_connection(
            target_executor_id=target_executor_id,
            target_executor_type=target_executor_type,
        )
        if resolved_conn is None:
            return False

        ctx.executor_connection = resolved_conn
        ctx.executor_environment = ExecutorEnvironmentSnapshot.unavailable(
            executor_id=target_executor_id,
            executor_type=target_executor_type,
            source="remote_executor_metadata_unavailable",
        )
        with contextlib.suppress(Exception):
            ctx.executor_environment = environment_from_metadata(
                ws_provider.get_handle_metadata(target_executor_id),
                executor_id=target_executor_id,
                executor_type=target_executor_type,
                fallback_source="remote_executor_metadata",
            )
        return True

    async def _finalize_regular_tool_result(
        self,
        ctx: StepContext,
        *,
        tc: ToolCall,
        tool_id: str,
        result: ToolResult,
        events_to_record: list[SessionEvent],
        messages: list[dict[str, Any]],
        collected_attachments: list[dict[str, Any]],
        pending_assistant_attachments: list[dict[str, Any]],
        promoted_tool_ids: set[str],
        activated_tool_ids: set[str],
        on_token: TokenCallback | None,
        on_tool_result: ToolResultCallback | None,
    ) -> None:
        self._record_execution_evidence(ctx, tool_name=tc.name, result=result)
        result = await self._handle_escalation(result, tc, ctx, events_to_record, on_tool_result)
        if not result.is_error:
            note_discovered_used = getattr(self.session_cache, "note_discovered_tool_used", None)
            if callable(note_discovered_used):
                note_discovered_used(
                    ctx.session.session_id,
                    tool_id,
                    used_at=datetime.now(UTC).isoformat(),
                )

        raw_output = result.metadata.get("_raw_output") if result.metadata else None
        stored_output = result.metadata.get("stored_output") if result.metadata else None
        tool_output_artifact_id = None
        if stored_output or raw_output:
            await self._save_tool_output_if_available(tc.call_id, result)
            tool_output_artifact_id = await self._save_tool_output_artifact_if_available(
                ctx,
                tc.call_id,
                result,
            )

        agent_visible_output, agent_visible_truncated = middle_truncate(
            result.output,
            _MAX_AGENT_VISIBLE_TOOL_RESULT,
            call_id=tc.call_id if raw_output or stored_output else None,
        )
        if agent_visible_truncated:
            result = result.model_copy(update={"output": agent_visible_output})
        original_size = result.metadata.get("original_size") if result.metadata else None
        eval_meta = result.metadata.get("evaluation") if result.metadata else None
        file_diffs = _file_diffs_from_metadata(result.metadata)
        has_saved_output = bool(raw_output or stored_output)
        source_call_id = None
        recovery_call_id = None
        if result.metadata:
            candidate_source_call_id = result.metadata.get("source_call_id")
            if isinstance(candidate_source_call_id, str) and candidate_source_call_id.strip():
                source_call_id = candidate_source_call_id
            candidate_recovery_call_id = result.metadata.get("recovery_call_id")
            if isinstance(candidate_recovery_call_id, str) and candidate_recovery_call_id.strip():
                recovery_call_id = candidate_recovery_call_id
        if is_tool_output_tool(tc.name) and has_saved_output:
            recovery_call_id = tc.call_id
        if recovery_call_id is None and has_saved_output:
            recovery_call_id = tc.call_id
        existing_presentation = (
            result.metadata.get("tool_output_presentation")
            if isinstance(result.metadata and result.metadata.get("tool_output_presentation"), dict)
            else {}
        )
        anchor_count = (
            existing_presentation.get("anchor_count")
            if isinstance(existing_presentation, dict)
            else 0
        )
        presentation_meta = {
            "output_size": original_size or len(result.output),
            "truncated": bool(result.metadata and result.metadata.get("truncated")),
            "agent_visible_truncated": bool(result.metadata and result.metadata.get("truncated"))
            or agent_visible_truncated,
            "has_full_output": has_saved_output,
            "recovery_call_id": recovery_call_id,
            "tool_output_artifact_id": tool_output_artifact_id,
            "anchors_available": bool(
                (
                    isinstance(existing_presentation, dict)
                    and existing_presentation.get("anchors_available")
                )
                or (isinstance(anchor_count, int) and anchor_count > 0)
            ),
            "anchor_count": anchor_count if isinstance(anchor_count, int) else 0,
            "transport_truncated": False,
        }
        normalized_result_attachments = normalize_attachment_refs(result.attachments or [])
        safe_result_attachments = strip_attachment_payload_bytes(normalized_result_attachments)
        events_to_record.append(
            SessionEvent(
                type="tool_result",
                data={
                    "call_id": tc.call_id,
                    "audit_call_id": (
                        eval_meta.get("call_id") if isinstance(eval_meta, dict) else None
                    ),
                    "name": tc.name,
                    "tool_id": tool_id,
                    "is_error": result.is_error,
                    "duration_ms": result.duration_ms,
                    "result": result.output,
                    "output_size": original_size or len(result.output),
                    "has_full_output": has_saved_output,
                    "recovery_call_id": recovery_call_id,
                    "tool_output_artifact_id": tool_output_artifact_id,
                    "source_call_id": source_call_id,
                    "tool_output_presentation": presentation_meta,
                    "anchors_available": presentation_meta["anchors_available"],
                    "anchor_count": presentation_meta["anchor_count"],
                    "evaluation": eval_meta,
                    "file_diffs": file_diffs,
                    "attachments": safe_result_attachments,
                    "agent_visible": True,
                    "view_kind": "model_tool_result",
                    "agent_visible_truncated": bool(
                        result.metadata and result.metadata.get("truncated")
                    )
                    or agent_visible_truncated,
                    "protect_from_pruning": bool(
                        result.metadata and result.metadata.get("protected_context")
                    ),
                },
            )
        )
        _resolve_pending_tool_call(ctx, tc.call_id)
        await self._flush_events_incremental(
            ctx,
            events_to_record,
            reason=f"tool_result:{tool_id}",
            on_token=on_token,
        )
        ws_presentation = build_transport_tool_output_preview(
            result.output,
            _MAX_TOOL_DATA_BYTES,
            metadata=presentation_meta,
            recovery_call_id=recovery_call_id,
            has_full_output=has_saved_output,
            tool_output_artifact_id=tool_output_artifact_id,
        )
        ws_preview = ws_presentation.result
        if on_tool_result:
            await on_tool_result(
                tc.call_id,
                tc.name,
                ws_preview,
                result.is_error,
                result.duration_ms,
                eval_meta,
                safe_result_attachments or None,
                file_diffs or None,
                ws_presentation.event_fields(),
            )
        if normalized_result_attachments:
            # Deduplicate by artifact_id against what the user already sent and what
            # has been collected earlier in this turn.  This prevents artifact_get_url
            # and similar tools from echoing an artifact that the user originally
            # uploaded, while still allowing brand-new artifacts (image_generate,
            # document_generate, artifact_publish on a freshly-created file) to be
            # promoted to the assistant message bubble.
            seen_ids: set[str] = {
                str(a.artifact_id) for a in ctx.user_attachments if getattr(a, "artifact_id", None)
            }
            seen_ids.update(
                str(a.get("artifact_id", ""))
                for a in collected_attachments
                if isinstance(a, dict) and a.get("artifact_id")
            )
            new_result_attachments = [
                a
                for a in normalized_result_attachments
                if not (isinstance(a, dict) and str(a.get("artifact_id", "")) in seen_ids)
            ]
            # Always track in collected_attachments (used for step_output.attachments and
            # message_complete WS event) but only promote NEW artifacts to the message.
            collected_attachments.extend(normalized_result_attachments)
            pending_assistant_attachments.extend(new_result_attachments)
        activation_notice: str | None = None
        if result.metadata:
            self._merge_promoted_tool_ids(promoted_tool_ids, result.metadata)
            self._apply_skill_attachment_metadata(ctx, result.metadata)
            if result.metadata.get("skill_epoch_stale") and hasattr(
                self.session_cache, "invalidate_classified_inventory"
            ):
                # Skill mutation changed the runtime tool inventory; the
                # next cycle must reclassify rather than reuse the memo.
                self.session_cache.invalidate_classified_inventory(ctx.session.session_id)
            activation_notice = await self._apply_skill_activation(
                ctx,
                metadata=result.metadata,
                promoted_tool_ids=promoted_tool_ids,
                activated_tool_ids=activated_tool_ids,
            )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.call_id,
                "content": result.output,
                "_tool_name": tc.name,
                "_tool_is_error": result.is_error,
                "_protected_tool_output": bool(
                    result.metadata and result.metadata.get("protected_context")
                ),
                "_has_full_output": has_saved_output,
                "_recovery_call_id": recovery_call_id,
                "_tool_output_artifact_id": tool_output_artifact_id,
                "_source_call_id": source_call_id,
                "_output_size": original_size or len(result.output),
                "_tool_output_presentation": presentation_meta,
                "_anchors_available": presentation_meta["anchors_available"],
                "_anchor_count": presentation_meta["anchor_count"],
                "_agent_visible_truncated": bool(
                    result.metadata and result.metadata.get("truncated")
                )
                or agent_visible_truncated,
            }
        )
        attachment_context = self._build_tool_attachment_context(ctx, tc, result.attachments)
        if attachment_context is not None:
            attachment_context["_recovery_call_id"] = recovery_call_id
            messages.append(attachment_context)
        protected_context = result.metadata.get("protected_context") if result.metadata else None
        if isinstance(protected_context, str) and protected_context.strip():
            messages.append(
                {
                    "role": "system",
                    "content": protected_context,
                    "_prior_context": True,
                }
            )
        # B2 — inject the skill activation transparency notice after the
        # skill instructions so the model can self-correct bad tool picks.
        if isinstance(activation_notice, str) and activation_notice.strip():
            messages.append(
                {
                    "role": "system",
                    "content": activation_notice,
                    "_prior_context": True,
                }
            )
        await self.event_bus.publish(
            Event(
                type=EventType.WORKFLOW_PROGRESS,
                data={
                    "event": "tool_call_completed",
                    "task_id": ctx.task_id,
                    "session_id": ctx.session.session_id,
                    "turn_id": ctx.turn_id,
                    "step_name": ctx.step_definition.name,
                    "step_run_id": ctx.step_run_id,
                    "call_id": tc.call_id,
                    "tool_name": tc.name,
                    "tool_id": tool_id,
                    "result": ws_preview,
                    "is_error": result.is_error,
                    "duration_ms": result.duration_ms,
                    "evaluation": eval_meta,
                    "file_diffs": file_diffs,
                    "attachments": safe_result_attachments,
                },
            )
        )

    def _build_tool_attachment_context(
        self,
        ctx: StepContext,
        tc: ToolCall,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        if not attachments:
            return None
        normalized = normalize_attachment_refs(attachments)
        if ctx.current_model_info is not None:
            blocks, unsupported = _native_attachment_blocks(normalized, ctx.current_model_info)
            if blocks:
                content_blocks: list[dict[str, Any]] = [
                    {
                        "type": "text",
                        "text": (
                            f"Untrusted tool output from {tc.name} (tool_call_id={tc.call_id}) included attachments. "
                            "Use the following attachment content carefully.\n\n"
                            f"{attachment_note(normalized)}"
                        ),
                    }
                ]
                content_blocks.extend(blocks)
                if unsupported:
                    content_blocks.append(
                        {
                            "type": "text",
                            "text": "Unsupported tool attachments were omitted: "
                            + ", ".join(unsupported),
                        }
                    )
                return {
                    "role": "user",
                    "content": content_blocks,
                    "_tool_attachment_context": True,
                    "_tool_call_id": tc.call_id,
                }
        if not normalized:
            return None
        return {
            "role": "system",
            "content": (
                "Tool attachments were produced as untrusted output for "
                f"tool_call_id={tc.call_id}. They are available in the UI and later context.\n"
                + attachment_note(normalized)
            ),
            "_tool_attachment_context": True,
            "_tool_call_id": tc.call_id,
        }

    def _project_model_messages(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        resolved_model: str,
        max_context_tokens: int,
        pressure_mode: ProjectionPressureMode = "normal",
        available_prompt_tokens: int | None = None,
        prior_turn_state: ProjectionTurnState | None = None,
    ) -> list[dict[str, Any]]:
        """Project rich working history into a compact model-facing transcript."""

        policy = ProjectionPolicy.from_budget(
            max_context_tokens=max_context_tokens,
            available_prompt_tokens=available_prompt_tokens,
            phase="within_turn",
            pressure_mode=pressure_mode,
        )
        projection = project_messages(
            messages,
            preserve_recent_completed_tool_groups=DEFAULT_COMPACTED_TOOL_GROUPS,
            policy=policy,
            prior_state=prior_turn_state,
        )
        token_counter = None
        if resolved_model:

            def token_counter(text: str, _m: str = resolved_model) -> int:
                return self.providers.llm.count_tokens(text, _m)

        pruned = prune_tool_outputs(
            projection.messages,
            min_index_to_modify=projection.mutable_start_index,
            token_counter=token_counter,
            policy=policy,
        )
        return [_strip_internal_message_fields(message) for message in pruned]

    def _project_model_messages_for_budget(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        resolved_model: str,
        max_context_tokens: int,
    ) -> ProjectedMessages:
        """Project messages using the conditional within-turn pipeline.

        On the first cycle (``ctx.projection_state`` not yet seeded) this
        behaves identically to the old loop-through-modes approach.  On
        subsequent cycles it uses ``should_reproject`` to skip the full
        projection when the context is not under pressure, preserving the
        provider prefix cache and avoiding O(N_tools) work per cycle.

        Monotonic preservation is enforced via
        ``ctx.projection_state.committed_preservations``: groups already sent
        to the model are never demoted unless critical pressure forces it.
        """
        budget = resolve_context_budget(
            max_context_tokens=max_context_tokens,
            max_input_tokens=getattr(ctx.current_model_info, "max_input_tokens", 0),
            agent_max_tokens=(ctx.agent.llm_config.max_tokens if ctx.agent.llm_config else None),
            model_max_output_tokens=getattr(ctx.current_model_info, "max_output_tokens", 0),
        )

        # ── Initialise per-turn state on first within-turn call ───────────────
        if ctx.projection_state is None:
            policy = ProjectionPolicy.from_budget(
                max_context_tokens=max_context_tokens,
                available_prompt_tokens=budget.available_prompt_tokens,
                phase="within_turn",
                pressure_mode=PressureMode.normal,
            )
            ctx.projection_state = ProjectionTurnState(
                turn_id=ctx.turn_id or "",
                policy=policy,
            )

        turn_state = ctx.projection_state

        # ── Build a lightweight pressure snapshot ─────────────────────────────
        # Use the cached token estimates on messages (set by previous projection
        # or by the cross-turn assembly) to avoid a full LLM token count here.
        estimated_tokens = _estimated_messages_tokens(messages)
        tool_schema_tokens = sum(
            len(json.dumps(t, default=str)) // 4 for t in tool_schemas if isinstance(t, dict)
        )
        total_estimated = estimated_tokens + tool_schema_tokens

        pressure_snap = PressureSnapshot(
            prompt_tokens=total_estimated,
            available_prompt_tokens=budget.available_prompt_tokens,
            steady_target_tokens=turn_state.policy.steady_target_tokens,
            hard_prompt_tokens=turn_state.policy.hard_prompt_tokens,
            oversized_result_appended=False,  # refined below if needed
            cycle_index=turn_state.reproject_count + turn_state.skip_count,
        )

        # Update pressure mode with hysteresis.
        prior_mode = turn_state.pressure_mode
        turn_state.update_pressure(pressure_snap)
        new_mode = turn_state.pressure_mode
        if new_mode != prior_mode:
            PROJECTION_PRESSURE_TRANSITIONS_TOTAL.labels(
                from_mode=str(prior_mode), to_mode=str(new_mode)
            ).inc()

        # ── Decide whether to re-project ──────────────────────────────────────
        decision = should_reproject(
            new_message_count=len(messages),
            last_message_count=turn_state.last_message_count,
            new_token_estimate=total_estimated,
            steady_target_tokens=turn_state.policy.steady_target_tokens,
            pressure_mode=new_mode,
            prior_pressure_mode=prior_mode,
            oversized_appended=pressure_snap.oversized_result_appended,
        )
        PROJECTION_CYCLES_TOTAL.labels(decision=str(decision)).inc()

        if decision is ReprojectDecision.skip and turn_state.last_result is not None:
            # Fast path: append new tail messages verbatim (stripped of internal markers).
            last_count = turn_state.last_message_count
            current_prefix_fingerprint = tool_transcript_prefix_fingerprint(messages[:last_count])
            if (
                turn_state.last_prefix_fingerprint is None
                or turn_state.last_prefix_fingerprint == current_prefix_fingerprint
            ):
                new_tail = [_strip_internal_message_fields(m) for m in messages[last_count:]]
                result = turn_state.last_result.append_tail(new_tail)
                policy = ProjectionPolicy.from_budget(
                    max_context_tokens=max_context_tokens,
                    available_prompt_tokens=budget.available_prompt_tokens,
                    phase="within_turn",
                    pressure_mode=new_mode,
                )
                snapshot = self._context_pressure_snapshot(
                    ctx,
                    messages=result.messages,
                    tool_schemas=tool_schemas,
                    max_context_tokens=max_context_tokens,
                )
                skipped_projection = ProjectedMessages(
                    messages=result.messages, snapshot=snapshot, mode=str(new_mode), policy=policy
                )
                if not self._projection_exceeded_selected_budget(skipped_projection):
                    turn_state.last_result = result
                    turn_state.last_message_count = len(messages)
                    turn_state.last_prefix_fingerprint = tool_transcript_prefix_fingerprint(
                        messages
                    )
                    turn_state.skip_count += 1
                    return skipped_projection

                # Exact provider token counting is authoritative. If the cheap
                # cached estimate allowed the skip path but the exact projected
                # prompt is now over the selected pressure budget, escalate
                # immediately and run a full projection pass in critical mode so
                # already-preserved tool groups may be demoted.
                prior_exact_mode = turn_state.pressure_mode
                turn_state.prior_pressure_mode = prior_exact_mode
                turn_state.pressure_mode = PressureMode.critical
                turn_state.under_threshold_cycles = 0
                turn_state.forced_critical_count += 1
                new_mode = PressureMode.critical
                decision = ReprojectDecision.critical_reproject
                if prior_exact_mode != PressureMode.critical:
                    PROJECTION_PRESSURE_TRANSITIONS_TOTAL.labels(
                        from_mode=str(prior_exact_mode), to_mode=str(PressureMode.critical)
                    ).inc()
                logger.info(
                    "context projection exact pressure forced critical reproject",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            "prior_projection_mode": str(prior_exact_mode),
                            "prompt_tokens": snapshot.prompt_tokens if snapshot else None,
                            "available_prompt_tokens": (
                                snapshot.available_prompt_tokens if snapshot else None
                            ),
                            "threshold_prompt_tokens": (
                                snapshot.threshold_prompt_tokens if snapshot else None
                            ),
                        }
                    },
                )
            else:
                logger.info(
                    "context projection skip invalidated by mutated tool transcript prefix",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            "last_message_count": last_count,
                        }
                    },
                )

        # ── Full re-projection ────────────────────────────────────────────────
        # Try modes from current pressure upward until we fit in budget.
        modes_to_try: list[PressureMode]
        if decision is ReprojectDecision.critical_reproject:
            modes_to_try = [PressureMode.critical]
        elif new_mode == PressureMode.pressure:
            modes_to_try = [PressureMode.pressure, PressureMode.critical]
        else:
            modes_to_try = [PressureMode.normal, PressureMode.pressure, PressureMode.critical]

        last_projection: ProjectedMessages | None = None
        for mode in modes_to_try:
            policy = ProjectionPolicy.from_budget(
                max_context_tokens=max_context_tokens,
                available_prompt_tokens=budget.available_prompt_tokens,
                phase="within_turn",
                pressure_mode=mode,
            )
            projected = self._project_model_messages(
                ctx,
                messages=messages,
                resolved_model=resolved_model,
                max_context_tokens=max_context_tokens,
                pressure_mode=str(mode),
                available_prompt_tokens=budget.available_prompt_tokens,
                prior_turn_state=turn_state,
            )
            snapshot = self._context_pressure_snapshot(
                ctx,
                messages=projected,
                tool_schemas=tool_schemas,
                max_context_tokens=max_context_tokens,
            )
            last_projection = ProjectedMessages(
                messages=projected, snapshot=snapshot, mode=str(mode), policy=policy
            )
            if not self._projection_exceeded_selected_budget(last_projection):
                if mode != PressureMode.normal:
                    self._log_projection_pressure_recovery(ctx, projection=last_projection)
                break

        if last_projection is None:
            last_projection = ProjectedMessages(
                messages=list(messages), snapshot=None, mode="normal"
            )

        # Update turn state.
        turn_state.last_message_count = len(messages)
        turn_state.last_prefix_fingerprint = tool_transcript_prefix_fingerprint(messages)
        turn_state.reproject_count += 1
        if last_projection.policy is not None:
            turn_state.policy = last_projection.policy
        turn_state.pressure_mode = PressureMode(last_projection.mode)
        # Rebuild a ProjectionResult from the projected messages for the cache.
        from cognis.core.context_projection import ProjectionResult

        turn_state.last_result = ProjectionResult(
            messages=last_projection.messages,
            mutable_start_index=getattr(last_projection, "mutable_start_index", 0),
        )
        return last_projection

    def _log_projection_pressure_recovery(
        self, ctx: StepContext, *, projection: ProjectedMessages
    ) -> None:
        snapshot = projection.snapshot
        logger.info(
            "context projection pressure pass selected",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "turn_id": ctx.turn_id,
                    "projection_mode": projection.mode,
                    "projection_policy": (
                        projection.policy.as_metadata() if projection.policy is not None else None
                    ),
                    "prompt_tokens": snapshot.prompt_tokens if snapshot else None,
                    "available_prompt_tokens": snapshot.available_prompt_tokens
                    if snapshot
                    else None,
                    "threshold_prompt_tokens": snapshot.threshold_prompt_tokens
                    if snapshot
                    else None,
                    "exceeded": snapshot.exceeded if snapshot else None,
                }
            },
        )

    def _projection_exceeded_selected_budget(self, projection: ProjectedMessages) -> bool:
        """Return true when projected prompt still exceeds the selected hard budget."""

        snapshot = projection.snapshot
        if snapshot is None:
            return False
        policy = projection.policy
        if policy is None:
            return snapshot.exceeded
        return snapshot.exceeded or snapshot.prompt_tokens > min(
            policy.hard_prompt_tokens, snapshot.available_prompt_tokens
        )

    def _context_pressure_exceeded(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> bool:
        snapshot = self._context_pressure_snapshot(
            ctx,
            messages=messages,
            tool_schemas=tool_schemas,
            max_context_tokens=max_context_tokens,
        )
        return bool(snapshot and snapshot.exceeded)

    def _context_pressure_snapshot(
        self,
        ctx: StepContext,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> ContextPressureSnapshot | None:
        if not ctx.current_model or max_context_tokens <= 0:
            return None
        budget = resolve_context_budget(
            max_context_tokens=max_context_tokens,
            max_input_tokens=getattr(ctx.current_model_info, "max_input_tokens", 0),
            agent_max_tokens=(ctx.agent.llm_config.max_tokens if ctx.agent.llm_config else None),
            model_max_output_tokens=getattr(ctx.current_model_info, "max_output_tokens", 0),
        )
        try:
            prompt_tokens = self.providers.llm.count_messages_tokens(messages, ctx.current_model)
            if tool_schemas:
                prompt_tokens += self.providers.llm.count_tokens(
                    json.dumps(tool_schemas, sort_keys=True),
                    ctx.current_model,
                )
        except Exception:
            return None
        available_prompt_tokens = budget.available_prompt_tokens
        if available_prompt_tokens <= 0:
            return ContextPressureSnapshot(
                prompt_tokens=prompt_tokens,
                max_context_tokens=max_context_tokens,
                max_input_tokens=budget.max_input_tokens,
                reserve_output_tokens=budget.reserve_output_tokens,
                effective_reserve_output_tokens=budget.effective_reserve_output_tokens,
                available_prompt_tokens=available_prompt_tokens,
                threshold_prompt_tokens=0,
                exceeded=True,
                reason="no_budget",
                reserve_clamped=budget.reserve_clamped,
            )
        threshold_prompt_tokens = int(available_prompt_tokens * 0.95)
        return ContextPressureSnapshot(
            prompt_tokens=prompt_tokens,
            max_context_tokens=max_context_tokens,
            max_input_tokens=budget.max_input_tokens,
            reserve_output_tokens=budget.reserve_output_tokens,
            effective_reserve_output_tokens=budget.effective_reserve_output_tokens,
            available_prompt_tokens=available_prompt_tokens,
            threshold_prompt_tokens=threshold_prompt_tokens,
            exceeded=prompt_tokens >= threshold_prompt_tokens,
            reason="over_threshold",
            reserve_clamped=budget.reserve_clamped,
        )

    def _store_context_usage_snapshot(
        self,
        ctx: StepContext,
        *,
        snapshot: ContextPressureSnapshot | None,
        provider_id: str | None,
    ) -> None:
        if snapshot is None:
            return
        try:
            self.session_cache.update_context_usage(
                ctx.session,
                prompt_tokens=snapshot.prompt_tokens,
                max_context_tokens=snapshot.max_context_tokens,
                max_input_tokens=snapshot.max_input_tokens,
                available_prompt_tokens=snapshot.available_prompt_tokens,
                model=ctx.current_model or "",
                provider_id=provider_id,
                reserve_output_tokens=snapshot.reserve_output_tokens,
                effective_reserve_output_tokens=snapshot.effective_reserve_output_tokens,
                compaction_threshold=float(
                    getattr(self.compaction_strategy, "compaction_threshold", 0.85) or 0.85
                ),
                projection_policy=(
                    ctx.last_projection_policy.as_metadata()
                    if ctx.last_projection_policy is not None
                    else None
                ),
            )
        except TypeError:
            self.session_cache.update_context_usage(
                ctx.session,
                prompt_tokens=snapshot.prompt_tokens,
                max_context_tokens=snapshot.max_context_tokens,
                max_input_tokens=snapshot.max_input_tokens,
                available_prompt_tokens=snapshot.available_prompt_tokens,
                model=ctx.current_model or "",
            )

    def _maybe_log_context_reserve_clamp(
        self,
        ctx: StepContext,
        snapshot: ContextPressureSnapshot | None,
        *,
        provider_id: str | None,
    ) -> None:
        if snapshot is None or not snapshot.reserve_clamped:
            return
        notifier = getattr(self.session_cache, "note_context_reserve_clamp", None)
        should_log = bool(notifier(ctx.session.session_id)) if callable(notifier) else True
        if not should_log:
            return
        logger.warning(
            "Controller prompt reserve reduced below requested output ceiling",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "model": ctx.current_model,
                    "provider_id": provider_id,
                    "max_context_tokens": snapshot.max_context_tokens,
                    "reserve_output_tokens": snapshot.reserve_output_tokens,
                    "effective_reserve_output_tokens": (snapshot.effective_reserve_output_tokens),
                    "available_prompt_tokens": snapshot.available_prompt_tokens,
                }
            },
        )

    async def _execute_regular_tool_batch(
        self,
        ctx: StepContext,
        batch: list[_PreparedRegularToolCall],
        *,
        events_to_record: list[SessionEvent],
        messages: list[dict[str, Any]],
        collected_attachments: list[dict[str, Any]],
        pending_assistant_attachments: list[dict[str, Any]],
        promoted_tool_ids: set[str],
        activated_tool_ids: set[str],
        on_token: TokenCallback | None,
        on_tool_result: ToolResultCallback | None,
    ) -> None:
        for group in self._parallel_execution_groups(ctx, batch):
            batch_started_at = monotonic()
            batch_mode = "serial" if len(group) == 1 else "parallel"
            batch_outcome = "success"
            for item in group:
                await self.event_bus.publish(
                    Event(
                        type=EventType.WORKFLOW_PROGRESS,
                        data={
                            "event": "tool_call_started",
                            "task_id": ctx.task_id,
                            "session_id": ctx.session.session_id,
                            "turn_id": ctx.turn_id,
                            "step_name": ctx.step_definition.name,
                            "step_run_id": ctx.step_run_id,
                            "call_id": item.tool_call.call_id,
                            "tool_name": item.tool_call.name,
                            "tool_id": item.tool_id,
                            "arguments": item.tool_call.arguments,
                        },
                    )
                )
                events_to_record.append(
                    SessionEvent(
                        type="tool_call",
                        data={
                            "name": item.tool_call.name,
                            "tool_id": item.tool_id,
                            "call_id": item.tool_call.call_id,
                            "arguments": _truncate_tool_data(
                                json.dumps(item.tool_call.arguments, default=str)
                            ),
                        },
                    )
                )
                _track_pending_tool_call(ctx, item.tool_call, tool_id=item.tool_id)

            await self._flush_events_incremental(
                ctx,
                events_to_record,
                reason="tool_call:batch",
                on_token=on_token,
            )

            if len(group) == 1:
                group_results: list[ToolResult] = [
                    await self._execute_regular_tool(ctx, group[0].tool_call)
                ]
            else:
                group_results = list(
                    await asyncio.gather(
                        *(self._execute_regular_tool(ctx, item.tool_call) for item in group)
                    )
                )
            if any(result.is_error for result in group_results):
                batch_outcome = "error"
            batch_duration_seconds = monotonic() - batch_started_at
            TOOL_BATCH_DURATION.labels(
                mode=batch_mode,
                outcome=batch_outcome,
                step_type=_agent_loop_step_type(ctx),
            ).observe(batch_duration_seconds)
            logger.info(
                "agent: tool batch completed",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "turn_id": ctx.turn_id,
                        **self._step_log_metadata(ctx),
                        "mode": batch_mode,
                        "outcome": batch_outcome,
                        "tool_count": len(group),
                        "tool_names": [item.tool_call.name for item in group],
                        "duration_seconds": round(batch_duration_seconds, 3),
                    }
                },
            )
            for item, result in zip(group, group_results, strict=False):
                await self._finalize_regular_tool_result(
                    ctx,
                    tc=item.tool_call,
                    tool_id=item.tool_id,
                    result=result,
                    events_to_record=events_to_record,
                    messages=messages,
                    collected_attachments=collected_attachments,
                    pending_assistant_attachments=pending_assistant_attachments,
                    promoted_tool_ids=promoted_tool_ids,
                    activated_tool_ids=activated_tool_ids,
                    on_token=on_token,
                    on_tool_result=on_tool_result,
                )

    async def _flush_events_incremental(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str = "incremental",
        on_token: TokenCallback | None = None,
    ) -> None:
        """Flush accumulated events to Intaris immediately."""
        if not events:
            return
        await self._record_events_strict(ctx, events, reason=reason, on_token=on_token)

    async def _emit_compaction_notice(
        self,
        ctx: StepContext,
        message: str,
        *,
        on_token: TokenCallback | None = None,
        persist: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit a visible compaction/pressure notice and optionally persist it."""

        await self._emit_recovery_notice(
            ctx,
            message,
            on_token=on_token,
            persist=persist,
            metadata=metadata,
            record_reason="system_notice",
        )

    async def _emit_recovery_notice(
        self,
        ctx: StepContext,
        message: str,
        *,
        on_token: TokenCallback | None = None,
        persist: bool = False,
        metadata: dict[str, Any] | None = None,
        record_reason: str = "model_recovery_notice",
    ) -> None:
        """Emit a visible recovery notice and optionally persist it durably."""

        notice_data = {
            "conversation_id": ctx.conversation.conversation_id,
            "session_id": ctx.session.session_id,
            "turn_id": ctx.turn_id,
            "message": message,
            "text": message,
        }
        if metadata:
            notice_data.update(metadata)
        await self.event_bus.publish(
            Event(
                type=EventType.SYSTEM_NOTICE,
                data=notice_data,
            )
        )
        if not persist:
            return
        lifecycle_data = {"event": "system_notice", "message": message, "turn_id": ctx.turn_id}
        if metadata:
            lifecycle_data.update(metadata)
        await self._record_events_strict(
            ctx,
            [
                SessionEvent(
                    type="lifecycle",
                    data=lifecycle_data,
                )
            ],
            reason=record_reason,
            on_token=on_token,
        )

    async def _recover_from_context_overflow(
        self,
        ctx: StepContext,
        *,
        context_result: Any,
        provider_id: str | None,
        model_id: str | None,
        reason: str,
        error_type: str | None,
        assistant_content_parts: list[str],
        collected_attachments: list[Any],
        on_token: TokenCallback | None,
        on_thinking: Any | None,
        on_tool_call: ToolCallCallback | None,
        on_tool_result: ToolResultCallback | None,
    ) -> StepOutput | None:
        """Compact, rotate, and replay a turn after provider context overflow."""

        provider_overflow_recoveries = int(
            ctx.runtime_info.get("provider_overflow_recoveries", 0) or 0
        )
        if provider_overflow_recoveries >= 1:
            return StepOutput(
                summary="Stopped because provider context overflow persisted after compaction.",
                content="\n\n".join(assistant_content_parts),
                outcome={
                    "status": "failed",
                    "reason": "Provider context overflow persisted after compaction.",
                },
                metadata={
                    "provider_context_overflow": {
                        "reason": reason,
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "error_type": error_type,
                    }
                },
                attachments=list(collected_attachments),
            )

        overflow_run = CompactionRunContext.from_context_result(
            context_result,
            trigger="provider_context_overflow",
            reason=reason,
        )
        overflow_run.phase = "recovery"
        overflow_run.provider_id = provider_id
        overflow_run.model_id = model_id
        overflow_run.fallback_reason = error_type
        await self._emit_compaction_notice(
            ctx,
            _provider_overflow_compaction_notice(
                provider_id=provider_id,
                model_id=model_id,
                reason=reason,
            ),
            on_token=on_token,
            persist=True,
            metadata=overflow_run.event_data(),
        )
        compaction_result = await self._auto_compact(
            ctx,
            run=overflow_run,
            on_token=on_token,
            trigger="provider_context_overflow",
            skip_few_events_check=True,
        )
        if compaction_result is not None and compaction_result.compacted:
            new_session = await self._rotate_after_compaction(
                ctx,
                compaction_result,
                trigger="provider_context_overflow",
                run=overflow_run,
            )
            if new_session is not None:
                overflow_run.status = "replayed"
                ctx.session = new_session
                ctx.is_retry = True
                ctx.prior_context = None
                ctx.runtime_info["provider_overflow_recoveries"] = provider_overflow_recoveries + 1
                ctx.compaction_recursion_depth += 1
                ctx.timeout_continuation_message = (
                    "Internal controller recovery: the previous model request exceeded "
                    "the provider context window. The conversation was compacted into "
                    "this fresh session. Continue the saved user request from the "
                    "available summary and recent history; do not restart completed "
                    "tool work unless required information is missing."
                )
                return await self._execute_step(
                    ctx,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )

        return StepOutput(
            summary="Stopped because provider context overflow could not be compacted.",
            content="\n\n".join(assistant_content_parts),
            outcome={
                "status": "failed",
                "reason": "Provider context overflow could not be compacted.",
            },
            metadata={"provider_context_overflow": overflow_run.event_data()},
            attachments=list(collected_attachments),
        )

    async def _rotate_after_compaction(
        self,
        ctx: StepContext,
        compaction_result: CompactionResult,
        *,
        trigger: str,
        run: CompactionRunContext | None = None,
    ) -> SessionModel | None:
        """Rotate to a fresh session after a successful compaction."""

        if not compaction_result.compacted:
            return None
        try:
            new_session = await self.session_manager.rotate_session(
                conversation_id=ctx.conversation.conversation_id,
                current_session=ctx.session,
                intention="Continued conversation",
                completion_reason="compacted",
                compaction_summary=compaction_result.summary,
                tail_events=getattr(compaction_result, "preserved_tail_events", None),
            )
            ROTATION_TOTAL.labels(trigger=trigger).inc()
        except Exception:
            logger.warning(
                "agent: session rotation after compaction failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )
            if run is not None:
                run.status = "failed"
                run.fallback_reason = "rotation_failed"
                run_data = run.event_data()
            else:
                run_data = {
                    "trigger": trigger,
                    "status": "failed",
                    "fallback_reason": "rotation_failed",
                }
            await self.event_bus.publish(
                Event(
                    type=EventType.SESSION_COMPACTION_FINISHED,
                    data={
                        "conversation_id": ctx.conversation.conversation_id,
                        "session_id": ctx.session.session_id,
                        **run_data,
                    },
                )
            )
            return None

        if compaction_result.summary:
            try:
                await self.session_cache.refresh(new_session)
            except Exception:
                logger.warning(
                    "agent: failed to pre-populate cache after compaction",
                    extra={"extra_data": {"new_session_id": new_session.session_id}},
                    exc_info=True,
                )

            if ctx.session.mnemory_session_id:
                try:
                    await self.remember_queue.enqueue(
                        {
                            "session_id": ctx.session.mnemory_session_id,
                            "cognis_session_id": ctx.session.session_id,
                            "intaris_session_id": ctx.session.intaris_session_id
                            or ctx.session.session_id,
                            "messages": [
                                {
                                    "role": "assistant",
                                    "content": (
                                        f"Compaction summary: {compaction_result.summary[:5000]}"
                                    ),
                                }
                            ],
                            "user_email": ctx.session.user_email,
                            "agent_id": ctx.session.agent_id,
                        }
                    )
                except Exception:
                    logger.warning(
                        "agent: failed to enqueue compaction summary for remember",
                        extra={"extra_data": {"session_id": ctx.session.session_id}},
                        exc_info=True,
                    )

        summary_preview = (compaction_result.summary or "")[:500]
        if run is not None:
            run.status = "compacted"
        run_data = (
            run.event_data() if run is not None else {"trigger": trigger, "status": "compacted"}
        )
        await self.event_bus.publish(
            Event(
                type=EventType.SESSION_COMPACTED,
                data={
                    "conversation_id": ctx.conversation.conversation_id,
                    "session_id": new_session.session_id,
                    "previous_session_id": ctx.session.session_id,
                    "summary_preview": summary_preview,
                    "method": compaction_result.method,
                    "turns_compacted": compaction_result.turns_compacted,
                    "tokens_before": compaction_result.tokens_before,
                    "tokens_after": compaction_result.tokens_after,
                    **run_data,
                },
            )
        )
        return new_session

    async def _finalize_step(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        assistant_content_parts: list[str] | None = None,
        assistant_memory_parts: list[str] | None = None,
    ) -> bool:
        """Record events to Intaris, update cache, dispatch remember.

        Returns ``True`` if events were successfully recorded to Intaris,
        ``False`` otherwise.  Callers use this to gate post-turn operations
        like automatic compaction — running compaction after a failed write
        would lose the current turn's events.
        """
        if not events:
            # Events may have been flushed incrementally — still dispatch
            # Mnemory remember using the accumulated assistant content.
            await self._dispatch_remember(ctx, assistant_memory_parts)
            return True

        events_recorded = await self._record_events_strict(ctx, events, reason="finalize")

        # Dispatch remember — use assistant_memory_parts if provided
        # (covers incrementally-flushed events), fall back to extracting
        # from the events list (covers the non-incremental path).
        if assistant_memory_parts is None:
            extracted = [
                merge_content_and_attachment_note(
                    str(e.data.get("content", "")),
                    [a for a in e.data.get("attachments", []) if isinstance(a, dict)],
                )
                for e in events
                if e.type == "assistant_message"
                and (e.data.get("content") or e.data.get("attachments"))
            ]
            await self._dispatch_remember(ctx, extracted or None)
        else:
            await self._dispatch_remember(ctx, assistant_memory_parts)

        return events_recorded

    async def _dispatch_remember(
        self,
        ctx: StepContext,
        content_parts: list[str] | None,
    ) -> None:
        """Enqueue last turn (user + assistant) to Mnemory remember queue.

        Sends both the user message and assistant response so mnemory can
        extract facts from the full exchange.
        """
        if not content_parts:
            return
        if not ctx.session.mnemory_session_id:
            logger.warning(
                "agent: skipping remember — no mnemory_session_id",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            return
        assistant_content = " ".join(content_parts)
        if not assistant_content.strip():
            return

        try:
            await self.remember_queue.enqueue(
                {
                    "session_id": ctx.session.mnemory_session_id,
                    "cognis_session_id": ctx.session.session_id,
                    "intaris_session_id": ctx.session.intaris_session_id or ctx.session.session_id,
                    "include_user_message": not ctx.system_initiated,
                    "user_event_seq": ctx.remember_user_event_seq,
                    "assistant_event_seq": ctx.remember_assistant_event_seq,
                    "user_email": ctx.session.user_email,
                    "agent_id": ctx.session.agent_id,
                    "agent_owner_email": ctx.agent.owner_email,
                }
            )
        except Exception:
            logger.warning(
                "agent: failed to enqueue remember work",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )

    async def _auto_compact(
        self,
        ctx: StepContext,
        *,
        run: CompactionRunContext | None = None,
        on_token: TokenCallback | None = None,
        trigger: str = "automatic",
        skip_few_events_check: bool = False,
        min_relevant_events: int | None = None,
        long_lived_chat: bool = False,
        emit_timeout_notice: bool = True,
    ) -> CompactionResult | None:
        """Automatically compact a session and return the compaction result.

        Called when ``context_result.recommend_compaction`` was ``True``
        and events were successfully recorded, or before a turn continues
        under heavy context pressure. Rotation is handled by the caller so
        pre-turn and post-turn flows can share timeout/fallback behavior.

        Bounded to ``AUTO_COMPACTION_TIMEOUT_SECONDS`` to avoid holding
        the session lock indefinitely under provider degradation. If the
        LLM compaction path times out, use the mechanical fallback instead
        of leaving the session stuck over the threshold.
        """

        # Early exit: skip if session cache has very few events
        # (e.g. manual /compact just ran and deferred creation already
        # created a near-empty session).
        cache_entry = self.session_cache.get_entry(ctx.session.session_id)
        if cache_entry is not None and not skip_few_events_check:
            relevant_events = self.session_cache.get_events_since_compaction(
                ctx.session.session_id,
                ["user_message", "assistant_message"],
            )
            if min_relevant_events is None:
                preserve_turns = getattr(self.compaction_strategy, "preserve_turns", 10)
                too_few_events = (
                    sum(1 for e in relevant_events if e.type == "user_message") <= preserve_turns
                )
            else:
                preserve_turns = None
                too_few_events = len(relevant_events) < min_relevant_events
            if too_few_events:
                logger.debug(
                    "agent: auto-compact skipped — too few events",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "relevant_events": len(relevant_events),
                            "preserve_turns": preserve_turns,
                            "min_relevant_events": min_relevant_events,
                        }
                    },
                )
                return None

        logger.info(
            "agent: auto-compaction triggered",
            extra={"extra_data": {"session_id": ctx.session.session_id}},
        )

        async def publish_compaction_status(
            event_type: EventType,
            status: str,
            *,
            fallback_reason: str | None = None,
        ) -> None:
            if run is not None:
                run.status = status
                if fallback_reason is not None:
                    run.fallback_reason = fallback_reason
                event_data = run.event_data()
            else:
                event_data = {
                    "trigger": trigger,
                    "status": status,
                    "fallback_reason": fallback_reason,
                }
            await self.event_bus.publish(
                Event(
                    type=event_type,
                    data={
                        "conversation_id": ctx.conversation.conversation_id,
                        "session_id": ctx.session.session_id,
                        **event_data,
                    },
                )
            )

        await publish_compaction_status(EventType.SESSION_COMPACTION_STARTED, "running")

        with AUTO_COMPACTION_DURATION.time():
            try:
                model_context = _compaction_model_context(ctx)
                compaction_result = await asyncio.wait_for(
                    self.compaction_strategy.compact(
                        ctx.session,
                        trigger=trigger,
                        model_context=model_context,
                        long_lived_chat=long_lived_chat,
                    ),
                    timeout=AUTO_COMPACTION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # The outer wait_for fired before compact()'s own retry loop
                # could complete.  Fall back to mechanical directly.
                if run is not None:
                    run.used_timeout_fallback = True
                logger.warning(
                    "agent: auto-compaction timed out; using mechanical fallback",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                if emit_timeout_notice:
                    await self._emit_compaction_notice(
                        ctx,
                        (
                            "LLM compaction timed out; using mechanical compaction fallback "
                            "so the conversation can continue with a smaller context."
                        ),
                        on_token=on_token,
                        persist=True,
                        metadata=(run.event_data() if run is not None else None),
                    )
                try:
                    compaction_result = await self.compaction_strategy.compact_with_fallback(
                        ctx.session,
                        trigger=f"{trigger}_timeout_fallback",
                        model_context=model_context,
                    )
                except Exception:
                    logger.warning(
                        "agent: mechanical fallback compaction failed after timeout",
                        extra={"extra_data": {"session_id": ctx.session.session_id}},
                        exc_info=True,
                    )
                    await publish_compaction_status(
                        EventType.SESSION_COMPACTION_FINISHED,
                        "failed",
                        fallback_reason="timeout_fallback_failed",
                    )
                    return None
            except Exception:
                # compact() already attempted its internal retry and fallback.
                # Surface the failure cleanly without a second fallback attempt.
                logger.warning(
                    "agent: auto-compaction failed",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                    exc_info=True,
                )
                await publish_compaction_status(
                    EventType.SESSION_COMPACTION_FINISHED,
                    "failed",
                    fallback_reason="compaction_failed",
                )
                return None

        if not compaction_result.compacted:
            await publish_compaction_status(
                EventType.SESSION_COMPACTION_FINISHED,
                "skipped",
                fallback_reason=compaction_result.method,
            )
            return compaction_result
        logger.info(
            "agent: auto-compaction completed",
            extra={
                "extra_data": {
                    "conversation_id": ctx.conversation.conversation_id,
                    "session_id": ctx.session.session_id,
                    "method": compaction_result.method,
                    "turns_compacted": compaction_result.turns_compacted,
                    "used_timeout_fallback": bool(run and run.used_timeout_fallback),
                }
            },
        )
        return compaction_result

    def session_is_locked(self, session_id: str) -> bool:
        """Return whether a session currently has an active agent-loop turn."""

        return self.session_lock.is_locked(session_id)

    async def run_idle_checkpoint_compaction(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        min_events: int,
    ) -> SessionModel | None:
        """Compact and rotate an idle long-lived chat session when it is substantial."""

        if self.session_is_locked(session.session_id):
            return None
        try:
            await self.session_cache.refresh(session)
        except Exception:
            logger.warning(
                "agent: idle checkpoint cache refresh failed",
                extra={"extra_data": {"session_id": session.session_id}},
                exc_info=True,
            )
            return None

        ctx = StepContext(
            step_definition=StepDefinition(name="direct", type="run", prompt=""),
            session=session,
            conversation=conversation,
            agent=agent,
            policy=CHAT_POLICY,
        )
        run = CompactionRunContext(
            trigger="idle_checkpoint",
            reason="long_lived_chat_idle",
        )
        compaction_result = await self._auto_compact(
            ctx,
            run=run,
            trigger="idle_checkpoint",
            min_relevant_events=min_events,
            long_lived_chat=True,
            emit_timeout_notice=False,
        )
        if compaction_result is None:
            return None
        return await self._rotate_after_compaction(
            ctx,
            compaction_result,
            trigger="idle_checkpoint",
            run=run,
        )

    def _raise_if_cancelled(self, ctx: StepContext) -> None:
        """Abort the current step when external control requested interruption."""
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            raise StepInterrupted("Step interrupted by external control")

    async def _set_interactive_pause_state(
        self,
        ctx: StepContext,
        *,
        pause_type: str,
        pause_payload: dict[str, Any],
    ) -> None:
        """Persist pause metadata for task-backed interactive waits."""
        if ctx.task_id is None or ctx.step_run_id is None or ctx.workflow_state is None:
            return

        ctx.workflow_state.status = "paused"
        ctx.workflow_state.current_step_status = "paused"
        ctx.workflow_state.pending_pause_type = pause_type
        ctx.workflow_state.pending_pause_payload = pause_payload

        from cognis.store.queries import (
            update_step_run,
            update_task_status,
            update_task_workflow_state,
        )

        async with self.session_manager.session_factory() as db_session:
            await update_step_run(db_session, ctx.step_run_id, status="paused")
            await update_task_status(db_session, ctx.task_id, "paused")
            await update_task_workflow_state(
                db_session,
                ctx.task_id,
                ctx.workflow_state.model_dump(mode="json"),
            )
            await db_session.commit()

    async def _clear_interactive_pause_state(self, ctx: StepContext) -> None:
        """Clear pause metadata after a task-backed interactive wait resumes."""
        if ctx.task_id is None or ctx.step_run_id is None or ctx.workflow_state is None:
            return

        ctx.workflow_state.status = "running"
        ctx.workflow_state.current_step_status = "running"
        ctx.workflow_state.pending_pause_type = None
        ctx.workflow_state.pending_pause_payload = None

        from cognis.store.queries import (
            update_step_run,
            update_task_status,
            update_task_workflow_state,
        )

        async with self.session_manager.session_factory() as db_session:
            await update_step_run(db_session, ctx.step_run_id, status="running")
            await update_task_status(db_session, ctx.task_id, "running")
            await update_task_workflow_state(
                db_session,
                ctx.task_id,
                ctx.workflow_state.model_dump(mode="json"),
            )
            await db_session.commit()

    async def _persist_step_todos(self, ctx: StepContext) -> None:
        """Persist step todos so pause/resume and retries keep task state."""

        if ctx.step_run_id is None:
            return
        from cognis.store.queries import update_step_run

        async with self.session_manager.session_factory() as db_session:
            await update_step_run(
                db_session, ctx.step_run_id, todos=_normalize_todos(ctx.todos or [])
            )
            await db_session.commit()

    def _clear_cached_deliverable(self, ctx: StepContext) -> None:
        """Drop any cached step deliverable metadata from the runtime context."""

        ctx.current_deliverable_id = None
        ctx.current_deliverable_version = None
        ctx.current_deliverable_content = None
        ctx.current_deliverable_format = None
        ctx.current_deliverable_title = None
        ctx.current_deliverable_outputs = {}
        ctx.current_deliverable_status = None

    def _cache_deliverable(self, ctx: StepContext, row: Any) -> Deliverable:
        """Cache a deliverable row on the step context and return the model."""

        deliverable = Deliverable.model_validate(
            {
                "deliverable_id": row.deliverable_id,
                "step_run_id": row.step_run_id,
                "version": row.version,
                "content": row.content,
                "format": row.format,
                "title": row.title,
                "target": row.target,
                "outputs": row.outputs or {},
                "status": row.status,
                "evaluator_feedback": row.evaluator_feedback,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        ctx.current_deliverable_id = deliverable.deliverable_id
        ctx.current_deliverable_version = deliverable.version
        ctx.current_deliverable_content = deliverable.content
        ctx.current_deliverable_format = deliverable.format
        ctx.current_deliverable_title = deliverable.title
        ctx.current_deliverable_outputs = dict(deliverable.outputs or {})
        ctx.current_deliverable_status = str(deliverable.status)
        return deliverable

    async def _get_current_deliverable(self, ctx: StepContext) -> Deliverable | None:
        """Return the active deliverable for the current step run, if any."""

        deliverable_step_run_id = self._deliverable_owner_step_run_id(ctx)
        if deliverable_step_run_id is None:
            return None
        if ctx.current_deliverable_id and ctx.current_deliverable_content is not None:
            return Deliverable.model_validate(
                {
                    "deliverable_id": ctx.current_deliverable_id,
                    "step_run_id": deliverable_step_run_id,
                    "version": ctx.current_deliverable_version or 1,
                    "content": ctx.current_deliverable_content,
                    "format": ctx.current_deliverable_format or "markdown",
                    "title": ctx.current_deliverable_title,
                    "outputs": dict(ctx.current_deliverable_outputs),
                    "status": ctx.current_deliverable_status or "buffered",
                }
            )

        async with self.session_manager.session_factory() as db_session:
            step_run = await get_step_run(db_session, deliverable_step_run_id)
            row = (
                await get_deliverable(db_session, step_run.deliverable_id)
                if step_run is not None and isinstance(step_run.deliverable_id, str)
                else None
            )
        if row is None:
            self._clear_cached_deliverable(ctx)
            return None
        return self._cache_deliverable(ctx, row)

    async def _write_step_deliverable(
        self,
        ctx: StepContext,
        *,
        content: str,
        format: str,
        title: str | None,
        target: str | None,
        outputs: dict[str, Any] | None,
    ) -> Deliverable:
        """Persist a new deliverable version for the current step run."""

        deliverable_step_run_id = self._deliverable_owner_step_run_id(ctx)
        if deliverable_step_run_id is None:
            raise ValueError("not_in_workflow")

        async with self.session_manager.session_factory() as db_session:
            row = await create_deliverable(
                db_session,
                step_run_id=deliverable_step_run_id,
                content=content,
                format=format,
                title=title,
                target=target,
                outputs=outputs,
            )
            await update_step_run(
                db_session,
                deliverable_step_run_id,
                deliverable_id=row.deliverable_id,
            )
            await db_session.commit()
        return self._cache_deliverable(ctx, row)

    async def _list_step_deliverables(self, ctx: StepContext) -> list[Deliverable]:
        """Return all deliverable versions for the current step run."""

        if ctx.step_run_id is None:
            return []
        async with self.session_manager.session_factory() as db_session:
            rows = await list_deliverables_for_step_run(db_session, ctx.step_run_id)
        return [
            self._cache_deliverable(ctx, row)
            if index == 0
            else Deliverable.model_validate(
                {
                    "deliverable_id": row.deliverable_id,
                    "step_run_id": row.step_run_id,
                    "version": row.version,
                    "content": row.content,
                    "format": row.format,
                    "title": row.title,
                    "target": row.target,
                    "outputs": row.outputs or {},
                    "status": row.status,
                    "evaluator_feedback": row.evaluator_feedback,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
            )
            for index, row in enumerate(rows)
        ]

    def _get_recovered_step_response(self, ctx: StepContext) -> dict[str, Any] | None:
        """Return a persisted step-input response recovered after restart."""
        if ctx.workflow_state is None or ctx.workflow_state.pending_pause_type != "step_input":
            return None
        payload = ctx.workflow_state.pending_pause_payload or {}
        step_name = payload.get("step_name")
        if step_name is not None and step_name != ctx.step_definition.name:
            return None
        answers = payload.get("answers")
        if answers is None:
            return None
        try:
            return validate_reply_for_questions(
                {"answers": answers, "mode": payload.get("mode", "structured")},
                normalize_questions(payload.get("questions")),
            )
        except ValueError:
            return None

    async def _ensure_known_project_context_loaded(self, ctx: StepContext) -> None:
        explicit_project_id = self._explicit_project_id(ctx)
        if explicit_project_id is not None:
            await self._maybe_resolve_and_store_project_metadata(
                ctx,
                project_id=explicit_project_id,
                raw_path=ctx.working_directory or ctx.workspace_root,
                path_kind="directory",
            )
        if not (ctx.workspace_root_explicit or ctx.working_directory_explicit):
            return
        known_root = normalize_project_path(ctx.workspace_root)
        known_cwd = normalize_project_path(ctx.working_directory)
        if known_root is None and known_cwd is None:
            return
        await self._maybe_resolve_and_store_project_metadata(
            ctx,
            raw_path=known_cwd or known_root,
            path_kind="directory",
        )
        existing = self._session_project_context(ctx, known_root or known_cwd)
        if existing is not None:
            if known_root is None:
                ctx.workspace_root = existing.project_root
            if known_cwd is None and existing.working_directory is not None:
                ctx.working_directory = existing.working_directory
            return
        await self._maybe_probe_and_store_project_context(
            ctx,
            raw_path=known_cwd or known_root,
            path_kind="directory",
        )

    async def _maybe_load_project_context_before_tool(
        self,
        ctx: StepContext,
        *,
        tc: ToolCall,
    ) -> ProjectContextEntry | ProjectMetadataEntry | None:
        probe_arguments = self._project_probe_arguments(ctx, tc)
        if probe_arguments is None:
            return None
        loaded_metadata = await self._maybe_resolve_and_store_project_metadata(
            ctx,
            raw_path=probe_arguments.get("path"),
            path_kind=str(probe_arguments.get("path_kind") or "directory"),
        )
        if not probe_arguments.get("explicit_path"):
            if not (ctx.workspace_root_explicit or ctx.working_directory_explicit):
                return loaded_metadata
            root_hint = normalize_project_path(ctx.workspace_root)
            if root_hint is not None and self._session_project_context(ctx, root_hint) is not None:
                return loaded_metadata
        loaded_context = await self._maybe_probe_and_store_project_context(
            ctx,
            raw_path=probe_arguments.get("path"),
            path_kind=str(probe_arguments.get("path_kind") or "directory"),
        )
        return loaded_context or loaded_metadata

    def _project_probe_arguments(self, ctx: StepContext, tc: ToolCall) -> dict[str, Any] | None:
        if tc.name not in _PROJECT_TOUCH_TOOL_NAMES:
            return None
        raw_path: str | None = None
        path_kind = "directory"
        explicit_path = False
        if tc.name in {"read", "write", "edit", "multiedit"}:
            raw_path = (
                tc.arguments.get("file_path")
                if isinstance(tc.arguments.get("file_path"), str)
                else None
            )
            path_kind = "file"
            explicit_path = raw_path is not None
        elif tc.name in {"list_directory", "glob", "grep"}:
            raw_path = (
                tc.arguments.get("path") if isinstance(tc.arguments.get("path"), str) else None
            )
            explicit_path = raw_path is not None
        elif tc.name == "bash":
            raw_path = (
                tc.arguments.get("workdir")
                if isinstance(tc.arguments.get("workdir"), str)
                else None
            )
            explicit_path = raw_path is not None
            if raw_path is None:
                raw_path = ctx.working_directory or ctx.workspace_root
            if raw_path is None:
                return None
        elif tc.name == "apply_patch":
            raw_path = ctx.working_directory or ctx.workspace_root
            if raw_path is None:
                return None
        elif tc.name == "artifact_publish":
            raw_path = (
                tc.arguments.get("path") if isinstance(tc.arguments.get("path"), str) else None
            )
            path_kind = "file"
            explicit_path = raw_path is not None
        return {
            "path": raw_path or "",
            "path_kind": path_kind,
            "explicit_path": explicit_path,
        }

    def _explicit_project_id(self, ctx: StepContext) -> str | None:
        project_id = getattr(ctx.conversation, "project_id", None)
        if isinstance(project_id, str) and project_id.strip():
            return project_id
        conversation_context = getattr(ctx.conversation, "context", None)
        platform_data = getattr(conversation_context, "platform_data", {}) or {}
        if isinstance(platform_data, dict):
            project_id = platform_data.get("project_id")
            if isinstance(project_id, str) and project_id.strip():
                return project_id
        return None

    def _session_project_metadata(
        self,
        ctx: StepContext,
        project_id: str | None,
    ) -> ProjectMetadataEntry | None:
        getter = getattr(self.session_cache, "get_project_metadata_context", None)
        if not callable(getter):
            return None
        return getter(ctx.session.session_id, project_id)

    async def _store_session_project_metadata(
        self,
        ctx: StepContext,
        entry: ProjectMetadataEntry,
    ) -> ProjectMetadataEntry:
        get_entry = getattr(self.session_cache, "get_entry", None)
        refresh = getattr(self.session_cache, "refresh", None)
        if callable(get_entry) and get_entry(ctx.session.session_id) is None and callable(refresh):
            await refresh(ctx.session)
        store = getattr(self.session_cache, "store_project_metadata_context", None)
        if not callable(store):
            return entry
        return await store(ctx.session.session_id, entry)

    async def _maybe_resolve_and_store_project_metadata(
        self,
        ctx: StepContext,
        *,
        project_id: str | None = None,
        raw_path: str | None = None,
        path_kind: str = "directory",
    ) -> ProjectMetadataEntry | None:
        if self._session_factory is None:
            return None
        explicit_project_id = project_id or self._explicit_project_id(ctx)
        if (
            explicit_project_id
            and self._session_project_metadata(ctx, explicit_project_id) is not None
        ):
            return None
        try:
            async with self._session_factory() as db_session:
                if explicit_project_id:
                    resolved = await resolve_project_metadata_for_project_id(
                        db_session,
                        user_email=ctx.session.user_email,
                        project_id=explicit_project_id,
                    )
                else:
                    resolved = await resolve_project_metadata_for_path(
                        db_session,
                        user_email=ctx.session.user_email,
                        path=raw_path,
                        path_kind=path_kind,
                        working_directory=ctx.working_directory,
                    )
                if resolved is None:
                    return None
                project_id = str(resolved.project.project_id)
                if self._session_project_metadata(ctx, project_id) is not None:
                    return None
                entry = project_metadata_entry_from_resolution(
                    resolved,
                    working_directory=ctx.working_directory or raw_path,
                )
        except Exception:
            logger.debug(
                "agent: failed to resolve project metadata context",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )
            return None
        was_loaded = self._session_project_metadata(ctx, entry.project_id) is None
        stored_entry = await self._store_session_project_metadata(ctx, entry)
        if was_loaded:
            await self._record_project_metadata_event(ctx, stored_entry)
            return stored_entry
        return None

    async def _maybe_probe_and_store_project_context(
        self,
        ctx: StepContext,
        *,
        raw_path: str | None,
        path_kind: str,
    ) -> ProjectContextEntry | None:
        probe_result = await self._probe_project_context(
            ctx,
            raw_path=raw_path,
            path_kind=path_kind,
        )
        if probe_result is None:
            return None
        project_root = normalize_project_path(probe_result.get("project_root"))
        if project_root is None:
            return None
        working_directory = normalize_project_path(
            probe_result.get("working_directory") or raw_path or ctx.working_directory
        )
        existing = self._session_project_context(ctx, project_root)
        if existing is not None:
            if ctx.workspace_root is None:
                ctx.workspace_root = existing.project_root
            if ctx.working_directory is None and existing.working_directory is not None:
                ctx.working_directory = existing.working_directory
            return None

        if ctx.workspace_root is None:
            ctx.workspace_root = project_root
        if ctx.working_directory is None and working_directory is not None:
            ctx.working_directory = working_directory

        await self._persist_execution_paths(
            ctx,
            workspace_root=project_root,
            working_directory=working_directory,
        )

        status = str(probe_result.get("status") or "")
        if status != PROJECT_CONTEXT_STATUS_LOADED:
            await self._store_session_project_context(
                ctx,
                ProjectContextEntry(
                    project_root=project_root,
                    status=status or "missing",
                    working_directory=working_directory,
                ),
            )
            return None

        content = probe_result.get("content")
        source_path = normalize_project_path(probe_result.get("source_path"))
        if not isinstance(content, str) or not content.strip() or source_path is None:
            return None
        was_loaded = self._session_project_context(ctx, project_root) is None
        stored_entry = await self._store_session_project_context(
            ctx,
            ProjectContextEntry(
                project_root=project_root,
                status=PROJECT_CONTEXT_STATUS_LOADED,
                source_path=source_path,
                content=content,
                content_hash=(
                    str(probe_result.get("content_hash"))
                    if isinstance(probe_result.get("content_hash"), str)
                    else None
                ),
                working_directory=working_directory,
            ),
        )
        if was_loaded:
            await self._record_project_context_event(ctx, stored_entry)
            return stored_entry
        return None

    async def _probe_project_context(
        self,
        ctx: StepContext,
        *,
        raw_path: str | None,
        path_kind: str,
    ) -> dict[str, Any] | None:
        executor = self._get_executor(ctx)
        if executor is None:
            return None
        call_id = f"project_probe_{uuid.uuid4().hex[:12]}"
        tool_call = ToolCall(
            call_id=call_id,
            name=INTERNAL_PROJECT_CONTEXT_PROBE_TOOL,
            arguments={
                "path": raw_path,
                "path_kind": path_kind,
                "hint_text": self._project_hint_text(ctx),
                "fallback_cwd": getattr(ctx.executor_environment, "cwd", None),
                "fallback_home": getattr(ctx.executor_environment, "home", None),
            },
            runtime_metadata={
                **self._tool_runtime_metadata(ctx),
                "tool_call_id": call_id,
                "tool_name": INTERNAL_PROJECT_CONTEXT_PROBE_TOOL,
            },
        )
        try:
            result = await executor.tool_execute(tool_call, timeout_seconds=10)
        except Exception:
            logger.debug(
                "agent: project context probe failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )
            return None
        if result.is_error or not isinstance(result.metadata, dict):
            return None
        payload = result.metadata.get("project_context")
        return payload if isinstance(payload, dict) else None

    def _project_hint_text(self, ctx: StepContext) -> str:
        parts = [ctx.user_message, ctx.task_title, ctx.task_description, ctx.task_expected_output]
        return "\n".join(part for part in parts if isinstance(part, str) and part.strip())

    def _session_project_context(
        self,
        ctx: StepContext,
        project_root: str | None,
    ) -> ProjectContextEntry | None:
        getter = getattr(self.session_cache, "get_project_context", None)
        if not callable(getter):
            return None
        return getter(ctx.session.session_id, project_root)

    async def _store_session_project_context(
        self,
        ctx: StepContext,
        entry: ProjectContextEntry,
    ) -> ProjectContextEntry:
        get_entry = getattr(self.session_cache, "get_entry", None)
        refresh = getattr(self.session_cache, "refresh", None)
        if callable(get_entry) and get_entry(ctx.session.session_id) is None and callable(refresh):
            await refresh(ctx.session)
        store = getattr(self.session_cache, "store_project_context", None)
        if not callable(store):
            return entry
        return await store(ctx.session.session_id, entry)

    async def _record_project_context_event(
        self,
        ctx: StepContext,
        entry: ProjectContextEntry,
    ) -> None:
        await self._record_events_strict(
            ctx,
            [
                SessionEvent(
                    type="developer_message",
                    data=project_context_event_data(entry, turn_id=ctx.turn_id),
                )
            ],
            reason="project_context_load",
        )

    async def _record_project_metadata_event(
        self,
        ctx: StepContext,
        entry: ProjectMetadataEntry,
    ) -> None:
        await self._record_events_strict(
            ctx,
            [
                SessionEvent(
                    type="developer_message",
                    data=project_metadata_event_data(entry, turn_id=ctx.turn_id),
                )
            ],
            reason="project_metadata_load",
        )

    async def _persist_execution_paths(
        self,
        ctx: StepContext,
        *,
        workspace_root: str,
        working_directory: str | None,
    ) -> None:
        if self._session_factory is None:
            return
        working_directory = normalize_project_path(working_directory) or workspace_root
        try:
            async with self._session_factory() as db_session:
                if ctx.task_id is not None:
                    from cognis.store.queries import update_task_execution_paths

                    await update_task_execution_paths(
                        db_session,
                        ctx.task_id,
                        workspace_root=workspace_root,
                        working_directory=working_directory,
                    )
                    if ctx.step_run_id is not None:
                        await update_step_run(
                            db_session,
                            ctx.step_run_id,
                            workspace_root=workspace_root,
                            working_directory=working_directory,
                        )
                else:
                    from cognis.store.queries import update_conversation_context_data

                    platform_data = dict(
                        getattr(ctx.conversation.context, "platform_data", {}) or {}
                    )
                    platform_data["workspace_root"] = workspace_root
                    platform_data["working_directory"] = working_directory
                    await update_conversation_context_data(
                        db_session,
                        ctx.conversation.conversation_id,
                        context_data=platform_data,
                    )
                    ctx.conversation.context.platform_data = platform_data
                await db_session.commit()
        except Exception:
            logger.debug(
                "agent: failed to persist execution paths",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "conversation_id": ctx.conversation.conversation_id,
                        "task_id": ctx.task_id,
                    }
                },
                exc_info=True,
            )

    def _tool_runtime_metadata(self, ctx: StepContext) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        runtime_agent = ctx.executor_agent or ctx.agent
        metadata["turn_id"] = ctx.turn_id
        metadata["runtime_access"] = {
            "user_email": ctx.session.user_email,
            "agent_id": runtime_agent.agent_id,
            "agent_owner_email": runtime_agent.owner_email,
            "agent_type": runtime_agent.agent_type,
            "tool_agent_id": ctx.agent.agent_id,
            "tool_agent_owner_email": ctx.agent.owner_email,
            "tool_agent_type": ctx.agent.agent_type,
            "session_id": ctx.session.session_id,
            "conversation_id": ctx.conversation.conversation_id,
            "task_id": ctx.task_id,
            "step_name": ctx.step_definition.name,
            "step_run_id": ctx.step_run_id,
            "parent_session_id": getattr(ctx.session, "parent_session_id", None),
            "delegation_mode": getattr(ctx.session, "delegation_mode", None),
            "workflow_step": bool(ctx.task_id or ctx.step_run_id),
            "interaction_mode": ctx.interaction_mode,
            "session_policy": ctx.session_policy,
        }
        if ctx.workspace_root:
            metadata["workspace_root"] = ctx.workspace_root
        if ctx.working_directory:
            metadata["working_directory"] = ctx.working_directory
        if ctx.executor_environment is not None:
            metadata["executor_environment"] = {
                "available": bool(getattr(ctx.executor_environment, "available", False)),
                "executor_id": getattr(ctx.executor_environment, "executor_id", None),
                "executor_type": getattr(ctx.executor_environment, "executor_type", None),
                "user": getattr(ctx.executor_environment, "user", None),
                "home": getattr(ctx.executor_environment, "home", None),
                "cwd": getattr(ctx.executor_environment, "cwd", None),
                "hostname": getattr(ctx.executor_environment, "hostname", None),
                "source": getattr(ctx.executor_environment, "source", None),
                "observed_at": getattr(ctx.executor_environment, "observed_at", None),
            }
        if ctx.current_model:
            metadata["resolved_model"] = ctx.current_model
        if ctx.current_provider_id:
            metadata["resolved_provider_id"] = ctx.current_provider_id
        if ctx.chat_mode is not None:
            metadata["chat_mode"] = ctx.chat_mode.mode
            metadata["chat_mode_source"] = ctx.chat_mode.source
            metadata["read_only_required"] = ctx.chat_mode.read_only_required
        if ctx.task_id:
            metadata["task_id"] = ctx.task_id
        if ctx.step_definition.name:
            metadata["step_name"] = ctx.step_definition.name
        if ctx.step_run_id:
            metadata["step_run_id"] = ctx.step_run_id
        metadata["conversation_context"] = {
            "type": ctx.conversation.context.type,
            "ref": ctx.conversation.context.ref,
            "platform_data": dict(ctx.conversation.context.platform_data or {}),
        }
        return metadata

    def _tool_runtime_metadata_for_call(
        self,
        ctx: StepContext,
        tool_call: ToolCall,
    ) -> dict[str, Any]:
        metadata = self._tool_runtime_metadata(ctx)
        metadata["tool_call_id"] = tool_call.call_id
        metadata["tool_name"] = tool_call.name
        return metadata

    def _record_execution_evidence(
        self,
        ctx: StepContext,
        *,
        tool_name: str,
        result: ToolResult | None = None,
    ) -> None:
        evidence = ctx.execution_evidence
        tools = evidence.setdefault("tools", [])
        tools.append({"name": tool_name, "ok": False if result is None else not result.is_error})
        if result is None or result.metadata is None:
            return
        for key in ("files_read", "files_written"):
            bucket = evidence.setdefault(key, [])
            for item in (
                result.metadata.get(key, []) if isinstance(result.metadata.get(key), list) else []
            ):
                if {"path": item} not in bucket:
                    bucket.append({"path": item})
        commands = evidence.setdefault("commands", [])
        for item in (
            result.metadata.get("commands", [])
            if isinstance(result.metadata.get("commands"), list)
            else []
        ):
            if isinstance(item, dict):
                commands.append(item)

        for key in ("tools", "files_read", "files_written", "commands"):
            evidence[key] = evidence[key][:20]

    def _deliverable_owner_step_run_id(self, ctx: StepContext) -> str | None:
        """Return the step run that owns deliverables for this execution context."""

        if ctx.step_run_id is not None:
            return ctx.step_run_id
        if ctx.deliverable_step_run_id is not None:
            return ctx.deliverable_step_run_id
        return None

    def _workflow_step_boundaries(self, ctx: StepContext) -> str:
        """Return concise boundaries for the current workflow step."""

        lines = [
            "Complete only this current step, not the full workflow.",
            "The current step objective is authoritative for what to do now.",
            (
                "Task-level instructions about implementation, verification, commits, pull "
                "requests, deployment, or final summaries apply only when they are relevant "
                "to this current step."
            ),
        ]
        step_name = ctx.step_definition.name.lower()
        if any(token in step_name for token in ("plan", "design", "architect")):
            lines.append(
                "This is a read-only planning/review step: do not edit files, create or "
                "modify worktrees, run tests or builds, commit, open pull requests, or "
                "implement changes unless this current step explicitly says to do so."
            )
        elif any(token in step_name for token in ("implement", "code", "fix")):
            lines.append(
                "Implementation edits are allowed when needed, but do not commit or open "
                "pull requests unless this current step explicitly says to do so."
            )
        elif "commit" in step_name:
            lines.append(
                "Commit-related actions are allowed; avoid unrelated implementation edits."
            )
        elif "review" in step_name:
            lines.append(
                "Focus on review findings unless this current step explicitly asks for fixes."
            )
        elif any(token in step_name for token in ("summary", "final", "report")):
            lines.append("Summarize and deliver only; do not start new implementation work.")
        return "\n".join(f"- {line}" for line in lines)

    def _build_workflow_step_reminder(self, ctx: StepContext) -> dict[str, Any] | None:
        """Build a hidden phase reminder for workflow-step LLM calls."""

        if not ctx.policy.require_step_complete or not (ctx.task_id or ctx.step_run_id):
            return None
        completion_lines = []
        if (
            self._deliverable_owner_step_run_id(ctx) is not None
            and ctx.step_definition.require_deliverable
        ):
            completion_lines.append(
                "- This step requires a deliverable: call write_deliverable with the step "
                "artifact before step_complete."
            )
        completion_lines.append("- Then call step_complete.")
        completion_lines.append("- Use step_todo_write only to keep todo state accurate.")
        content = (
            "<workflow_step_reminder>\n"
            f"Current workflow step: {ctx.step_definition.name}\n"
            "\nBoundaries:\n"
            f"{self._workflow_step_boundaries(ctx)}\n"
            "\nInstruction precedence:\n"
            "- This workflow step reminder overrides task-level finishing instructions "
            "and loaded skill instructions.\n"
            "- The current step objective and controller completion contract define what "
            "done means for this turn.\n"
            "- Later workflow steps handle their own implementation, verification, commit, "
            "pull request, or final-summary work.\n"
            "\nRequired completion:\n" + "\n".join(completion_lines) + "\n</workflow_step_reminder>"
        )
        return {"role": "system", "content": content, "_workflow_step_reminder": True}

    def _build_non_primary_active_reminder(self, ctx: StepContext) -> dict[str, Any] | None:
        """Stage 36: remind the agent it is on a non-primary executor.

        Returns ``None`` when the active executor is a primary, when the
        pool is unknown, or when the active executor is unassigned (in
        which case other paths surface factual errors).
        """

        pool = getattr(ctx, "executor_pool", None)
        if pool is None:
            return None
        active_id = getattr(ctx, "active_executor_id", None) or getattr(
            ctx.conversation, "active_executor_id", None
        )
        if not isinstance(active_id, str) or not active_id:
            return None
        target = pool.by_id(active_id)
        if target is None or target.is_primary:
            return None
        primary_ids = sorted(t.executor_id for t in pool.primary if t.executor_id)
        primary_hint = ", ".join(primary_ids) if primary_ids else "(none configured)"
        content = (
            "<executor_reminder>\n"
            f"You are routing tool calls to a non-primary (additional) executor: "
            f"{target.executor_id} ({target.executor_type}).\n"
            "This was set by an earlier switch_executor call or /executor command. "
            "All subsequent tool calls without target_executor will use this executor. "
            "Call switch_executor when you want to return to a primary executor.\n"
            f"Primary executors available to you: {primary_hint}\n"
            "</executor_reminder>"
        )
        return {
            "role": "system",
            "content": content,
            "_executor_reminder": True,
        }

    async def _build_background_shell_status_reminder(
        self, ctx: StepContext
    ) -> dict[str, Any] | None:
        statuses = await self._collect_background_shell_statuses(ctx)
        if not statuses:
            return None

        visible = statuses[:_BACKGROUND_SHELL_STATUS_REMINDER_LIMIT]
        additional = statuses[_BACKGROUND_SHELL_STATUS_REMINDER_LIMIT:]
        lines = [
            "<background_shell_status>",
            "Background bash jobs are still running. They continue until completion, bash_kill, or executor cleanup.",
        ]
        for index, status in enumerate(visible, start=1):
            lines.append(f"job {index}:")
            lines.append(f"  shell_id: {self._safe_status_text(status.get('shell_id'))}")
            lines.append(
                "  executor: "
                f"{self._safe_status_text(status.get('executor_id'))} "
                f"({self._safe_status_text(status.get('executor_type'))})"
            )
            pid = status.get("pid")
            if pid is not None:
                lines.append(f"  pid: {pid}")
            description = self._safe_status_text(status.get("description"))
            if description:
                lines.append(f"  description: {description}")
            command = self._truncate_status_text(self._safe_status_text(status.get("command")), 180)
            if command:
                lines.append(f"  command: {command}")
            lines.append(f"  running_for: {self._format_duration(status.get('runtime_seconds'))}")
            lines.append(f"  idle_for: {self._format_duration(status.get('idle_seconds'))}")
            output_chars = status.get("output_chars")
            trimmed_chars = status.get("trimmed_chars")
            if isinstance(output_chars, int):
                suffix = (
                    f", trimmed {trimmed_chars} chars"
                    if isinstance(trimmed_chars, int) and trimmed_chars
                    else ""
                )
                lines.append(f"  buffered_output: {output_chars} chars{suffix}")
            cursor = status.get("cursor")
            if cursor is not None:
                lines.append(f"  output_cursor: {cursor}")
        if additional:
            ids = [
                self._safe_status_text(item.get("shell_id"))
                for item in additional
                if self._safe_status_text(item.get("shell_id"))
            ]
            pids = [str(item.get("pid")) for item in additional if item.get("pid") is not None]
            lines.append(
                f"{len(additional)} additional jobs running"
                + (f", shell_ids: {', '.join(ids)}" if ids else "")
                + (f", PIDs: {', '.join(pids)}" if pids else "")
                + ". Inspect with bash_output using the shell_id; include target_executor if needed."
            )
        lines.append(
            "Use bash_output with the shell_id to inspect output, or bash_kill to stop a job. "
            "If multiple executors are listed, route the call to the matching executor."
        )
        lines.append("</background_shell_status>")
        return {
            "role": "system",
            "content": "\n".join(lines),
            "_background_shell_status_reminder": True,
        }

    async def _build_background_work_status_reminder(
        self, ctx: StepContext
    ) -> dict[str, Any] | None:
        statuses = await self._collect_background_work_statuses(ctx)
        if not statuses:
            return None

        visible = statuses[:_BACKGROUND_WORK_STATUS_REMINDER_LIMIT]
        additional = statuses[_BACKGROUND_WORK_STATUS_REMINDER_LIMIT:]
        lines = [
            "<background_work_status>",
            (
                "Cognis background work controlled by this conversation/session is still open "
                "or needs attention. Do not auto-close it; only use the recommended action "
                "when relevant."
            ),
        ]
        for index, status in enumerate(visible, start=1):
            lines.append(f"item {index}:")
            lines.append(f"  kind: {self._safe_status_text(status.get('kind'))}")
            item_id = self._safe_status_text(status.get("id"))
            if item_id:
                lines.append(f"  id: {item_id}")
            for key in ("link_id", "conversation_id", "session_id"):
                value = self._safe_status_text(status.get(key))
                if value:
                    lines.append(f"  {key}: {value}")
            label_key = "title" if status.get("kind") == "managed_conversation" else "task"
            label = self._truncate_status_text(self._safe_status_text(status.get(label_key)), 180)
            if label:
                lines.append(f"  {label_key}: {label}")
            lines.append(f"  state: {self._safe_status_text(status.get('state'))}")
            updated_at = self._safe_status_text(status.get("updated_at"))
            if updated_at:
                lines.append(f"  updated_at: {updated_at}")
            lines.append(f"  age: {self._format_duration(status.get('age_seconds'))}")
            warnings = status.get("warnings")
            if isinstance(warnings, list) and warnings:
                lines.append(
                    "  warnings: "
                    + ", ".join(self._safe_status_text(warning) for warning in warnings)
                )
            action = self._safe_status_text(status.get("action"))
            if action:
                lines.append(f"  recommended_action: {action}")
        if additional:
            ids = [
                self._safe_status_text(item.get("id"))
                for item in additional
                if self._safe_status_text(item.get("id"))
            ]
            lines.append(
                f"{len(additional)} additional background work items omitted"
                + (f", ids: {', '.join(ids)}" if ids else "")
                + ". Use agent_conversation_list or list_subsessions/get_subsession to inspect."
            )
        lines.append(
            "For healthy running work, keep it in mind and continue other work; use "
            "agent_conversation_get/agent_conversation_wait or get_subsession only if this "
            "turn depends on the result. Reserve retry/send/cancel actions for failed, "
            "interrupted, stale, or inconsistent states."
        )
        lines.append("</background_work_status>")
        return {
            "role": "system",
            "content": "\n".join(lines),
            "_background_work_status_reminder": True,
        }

    async def _collect_background_work_statuses(self, ctx: StepContext) -> list[dict[str, Any]]:
        if not self._background_work_reminder_allowed(ctx):
            return []
        session_factory = getattr(self.session_manager, "session_factory", None)
        if session_factory is None:
            return []

        from cognis.store import queries

        user_email = getattr(ctx.session, "user_email", None)
        conversation_id = getattr(ctx.conversation, "conversation_id", None)
        session_id = getattr(ctx.session, "session_id", None)
        if not user_email or not conversation_id or not session_id:
            return []

        try:
            async with session_factory() as db_session:
                if not callable(getattr(db_session, "execute", None)):
                    return []
                managed_links = await queries.list_managed_conversation_links(
                    db_session,
                    user_email=user_email,
                    controller_conversation_id=conversation_id,
                    status="all",
                    limit=100,
                )
                child_sessions = await queries.list_conversation_sessions(
                    db_session,
                    conversation_id,
                    parent_session_id=session_id,
                    order="desc",
                    limit=100,
                )
        except Exception:
            logger.warning(
                "agent: failed to collect background work statuses",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                    }
                },
                exc_info=True,
            )
            return []

        async with self._children_lock:
            active_children = dict(self._active_children.get(session_id, {}))

        now = datetime.now(UTC)
        items: list[dict[str, Any]] = []
        for row in managed_links:
            item = self._background_work_item_from_managed_link(row, now=now)
            if item is not None:
                items.append(item)
        for row in child_sessions:
            item = self._background_work_item_from_child_session(
                row,
                active_children=active_children,
                now=now,
            )
            if item is not None:
                items.append(item)

        items.sort(
            key=lambda item: (
                int(item.get("priority", 99)),
                -float(item.get("sort_timestamp") or 0.0),
            )
        )
        return items

    def _background_work_reminder_allowed(self, ctx: StepContext) -> bool:
        if ctx.orchestration_mode != OrchestrationMode.FULL:
            return False
        if ctx.task_id:
            return False
        if getattr(ctx.session, "parent_session_id", None):
            return False
        return not is_managed_agent_conversation_context(getattr(ctx.conversation, "context", None))

    def _background_work_item_from_managed_link(
        self, row: Any, *, now: datetime
    ) -> dict[str, Any] | None:
        conversation_state = str(getattr(row, "conversation_state", "") or "")
        turn_state = str(getattr(row, "turn_state", "") or "")
        active_turn_id = getattr(row, "active_turn_id", None)
        last_error = getattr(row, "last_error", None)
        scheduler_checked, scheduler_active_turn_id = self._managed_scheduler_active_turn_id(row)
        warnings = self._managed_conversation_warnings(
            row,
            scheduler_checked=scheduler_checked,
            scheduler_active_turn_id=scheduler_active_turn_id,
        )

        is_completed = conversation_state == "completed" or turn_state == "completed"
        is_closed = conversation_state == "closed"
        is_clean_completed = is_completed and not active_turn_id and not last_error and not warnings
        if is_clean_completed or (is_closed and not active_turn_id and not warnings):
            return None

        is_running = turn_state in {"queued", "running"} or bool(active_turn_id)
        needs_attention = turn_state in {"failed", "interrupted"} or bool(last_error)
        is_open = conversation_state == "open"
        if not (warnings or needs_attention or is_running or is_open):
            return None

        updated_at = self._coerce_datetime(
            getattr(row, "updated_at", None)
        ) or self._coerce_datetime(getattr(row, "created_at", None))
        priority = 0 if warnings else 1 if needs_attention else 2 if is_running else 3
        return {
            "kind": "managed_conversation",
            "id": getattr(row, "target_conversation_id", None) or getattr(row, "link_id", None),
            "link_id": getattr(row, "link_id", None),
            "conversation_id": getattr(row, "target_conversation_id", None),
            "title": getattr(row, "title", None) or "Agent work",
            "state": f"{conversation_state or 'unknown'}/{turn_state or 'unknown'}",
            "updated_at": self._format_status_datetime(updated_at),
            "age_seconds": self._age_seconds(updated_at, now),
            "warnings": warnings,
            "action": self._managed_conversation_recommended_action(
                turn_state=turn_state,
                is_running=is_running,
                scheduler_checked=scheduler_checked,
                scheduler_active_turn_id=scheduler_active_turn_id,
                needs_attention=needs_attention,
                has_warnings=bool(warnings),
            ),
            "priority": priority,
            "sort_timestamp": self._sort_timestamp(updated_at),
        }

    def _background_work_item_from_child_session(
        self,
        row: Any,
        *,
        active_children: dict[str, asyncio.Task[Any]],
        now: datetime,
    ) -> dict[str, Any] | None:
        mode = str(getattr(row, "delegation_mode", "") or "")
        if mode not in {"delegate", "delegate_async"}:
            return None
        status = str(getattr(row, "status", "") or "")
        task = active_children.get(getattr(row, "session_id", ""))
        task_running = task is not None and not task.done()
        updated_at = (
            self._coerce_datetime(getattr(row, "updated_at", None))
            or self._coerce_datetime(getattr(row, "completed_at", None))
            or self._coerce_datetime(getattr(row, "started_at", None))
        )
        age_seconds = self._age_seconds(updated_at, now)
        stale = (
            status in {"active", "idle", "suspended"}
            and not task_running
            and isinstance(age_seconds, (int, float))
            and age_seconds >= _BACKGROUND_WORK_STALE_AFTER_SECONDS
        )
        warnings = self._delegated_session_warnings(
            row,
            task=task,
            task_running=task_running,
            stale=stale,
        )
        if status == "completed" and not warnings:
            return None
        needs_attention = status in {"failed", "cancelled", "terminated"} or stale
        is_running = status in {"active", "suspended"} or task_running
        if not (warnings or needs_attention or is_running):
            return None

        priority = 0 if warnings else 1 if needs_attention else 2 if is_running else 3
        return {
            "kind": "delegated_session",
            "id": getattr(row, "session_id", None),
            "session_id": getattr(row, "session_id", None),
            "task": getattr(row, "delegation_task", None) or "Delegated sub-session",
            "state": status or "unknown",
            "updated_at": self._format_status_datetime(updated_at),
            "age_seconds": age_seconds,
            "warnings": warnings,
            "action": self._delegated_session_recommended_action(
                status=status,
                stale=stale,
                task_running=task_running,
            ),
            "priority": priority,
            "sort_timestamp": self._sort_timestamp(updated_at),
        }

    def _managed_scheduler_active_turn_id(self, row: Any) -> tuple[bool, str | None]:
        scheduler = self._turn_scheduler
        if scheduler is None:
            return False, None
        conversation_id = getattr(row, "target_conversation_id", None)
        if not conversation_id:
            return True, None
        try:
            active_turn_id = scheduler.active_turn_id(conversation_id)
        except AttributeError:
            try:
                active_turn_id = "active" if scheduler.has_active_turn(conversation_id) else None
            except AttributeError:
                return False, None
        except Exception:
            return False, None
        return True, active_turn_id

    @staticmethod
    def _managed_conversation_warnings(
        row: Any,
        *,
        scheduler_checked: bool,
        scheduler_active_turn_id: str | None,
    ) -> list[str]:
        warnings: list[str] = []
        conversation_state = str(getattr(row, "conversation_state", "") or "")
        turn_state = str(getattr(row, "turn_state", "") or "")
        active_turn_id = getattr(row, "active_turn_id", None)
        completed_at = getattr(row, "completed_at", None)
        last_error = getattr(row, "last_error", None)

        if turn_state in {"queued", "running"} and completed_at is not None:
            warnings.append("running+completed_at")
        if (conversation_state == "completed" or turn_state == "completed") and last_error:
            warnings.append("completed+last_error")
        link_running = turn_state in {"queued", "running"} or bool(active_turn_id)
        if scheduler_checked and link_running and not scheduler_active_turn_id:
            warnings.append("wait-idle/link-running")
        if scheduler_checked and scheduler_active_turn_id and not link_running:
            warnings.append("link-idle/scheduler-running")
        if (
            scheduler_checked
            and active_turn_id
            and scheduler_active_turn_id
            and scheduler_active_turn_id != "active"
            and active_turn_id != scheduler_active_turn_id
        ):
            warnings.append("active_turn_mismatch")
        if conversation_state == "closed" and active_turn_id:
            warnings.append("closed+active_turn_id")
        return warnings

    @staticmethod
    def _delegated_session_warnings(
        row: Any,
        *,
        task: asyncio.Task[Any] | None,
        task_running: bool,
        stale: bool,
    ) -> list[str]:
        warnings: list[str] = []
        status = str(getattr(row, "status", "") or "")
        if (
            status in {"active", "idle", "suspended"}
            and getattr(row, "completed_at", None) is not None
        ):
            warnings.append("active+completed_at")
        if status == "active" and task is None:
            warnings.append("active-no-running-task")
        if status == "active" and task is not None and not task_running:
            warnings.append("task-done/session-active")
        if status == "failed" and not getattr(row, "result_summary", None):
            warnings.append("failed-no-summary")
        if stale:
            warnings.append("stale-active")
        return warnings

    @staticmethod
    def _managed_conversation_recommended_action(
        *,
        turn_state: str,
        is_running: bool,
        scheduler_checked: bool,
        scheduler_active_turn_id: str | None,
        needs_attention: bool,
        has_warnings: bool,
    ) -> str:
        if has_warnings or turn_state in {"failed", "interrupted"} or needs_attention:
            return "use agent_conversation_get, then retry or send follow-up if still needed"
        if is_running and scheduler_checked and scheduler_active_turn_id:
            return (
                "keep in mind; continue other work; use agent_conversation_get/"
                "agent_conversation_wait only if this turn depends on the result"
            )
        if is_running:
            return "keep in mind; continue other work; inspect status only when relevant"
        return "review result, send follow-up, or close when no longer needed"

    @staticmethod
    def _delegated_session_recommended_action(
        *, status: str, stale: bool, task_running: bool
    ) -> str:
        if task_running:
            return "keep in mind; continue other work; use get_subsession only if this turn depends on the result"
        if stale:
            return "use get_subsession; cancel_subsession if abandoned"
        if status in {"failed", "cancelled", "terminated"}:
            return "use get_subsession; re-delegate if still needed"
        return "inspect with get_subsession if progress is unclear"

    async def _collect_background_shell_statuses(self, ctx: StepContext) -> list[dict[str, Any]]:
        pool = getattr(ctx, "executor_pool", None)
        active_id = getattr(ctx, "active_executor_id", None) or getattr(
            ctx.conversation, "active_executor_id", None
        )
        targets: list[Any] = []
        if pool is not None:
            seen: set[str] = set()
            for target in pool.all:
                executor_id = getattr(target, "executor_id", None)
                if (
                    isinstance(executor_id, str)
                    and executor_id
                    and getattr(target, "usable", False)
                    and executor_id not in seen
                ):
                    targets.append(target)
                    seen.add(executor_id)
        else:
            current_executor_id = (
                getattr(ctx.executor_environment, "executor_id", None) or active_id
            )
            current_executor_type = getattr(ctx.executor_environment, "executor_type", None)
            targets.append(
                {
                    "executor_id": current_executor_id,
                    "executor_type": current_executor_type,
                    "usable": True,
                }
            )

        statuses: list[dict[str, Any]] = []
        for target in targets:
            executor_id = (
                getattr(target, "executor_id", None)
                if not isinstance(target, dict)
                else target.get("executor_id")
            )
            executor_type = (
                getattr(target, "executor_type", None)
                if not isinstance(target, dict)
                else target.get("executor_type")
            )
            connection = ctx.executor_connection if executor_id == active_id else None
            if (
                connection is None
                and isinstance(executor_id, str)
                and isinstance(executor_type, str)
            ):
                connection = self._resolve_background_status_connection(
                    target_executor_id=executor_id,
                    target_executor_type=executor_type,
                )
            if connection is None:
                continue
            try:
                payload = await connection.background_shell_status(include_completed=False)
            except AttributeError:
                try:
                    payload = await connection.rpc_call(
                        "shell.background_status",
                        {"include_completed": False},
                    )
                except Exception:
                    continue
            except Exception:
                continue
            raw_shells = payload.get("shells") if isinstance(payload, dict) else None
            if not isinstance(raw_shells, list):
                continue
            for item in raw_shells:
                if not isinstance(item, dict):
                    continue
                if not self._background_shell_status_matches_context(ctx, item):
                    continue
                normalized = dict(item)
                normalized.setdefault("executor_id", executor_id)
                normalized.setdefault("executor_type", executor_type)
                statuses.append(normalized)
        statuses.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return statuses

    def _resolve_background_status_connection(
        self,
        *,
        target_executor_id: str,
        target_executor_type: str,
    ) -> Any:
        if target_executor_type == "websocket":
            return self._resolve_target_connection(
                target_executor_id=target_executor_id,
                target_executor_type=target_executor_type,
            )
        return None

    @staticmethod
    def _background_shell_status_matches_context(ctx: StepContext, status: dict[str, Any]) -> bool:
        conversation_id = status.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            return conversation_id == ctx.conversation.conversation_id
        agent_id = status.get("agent_id")
        return isinstance(agent_id, str) and agent_id == ctx.agent.agent_id

    @staticmethod
    def _safe_status_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).replace("\n", " ").strip()

    @staticmethod
    def _truncate_status_text(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        return value[: max_chars - 12].rstrip() + " [truncated]"

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        return None

    @staticmethod
    def _format_status_datetime(value: datetime | None) -> str:
        if value is None:
            return ""
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _age_seconds(value: datetime | None, now: datetime) -> int | None:
        if value is None:
            return None
        return max(0, int((now - value.astimezone(UTC)).total_seconds()))

    @staticmethod
    def _sort_timestamp(value: datetime | None) -> float:
        if value is None:
            return 0.0
        return value.astimezone(UTC).timestamp()

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return "unknown"
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minute:02d}m"
        if minutes:
            return f"{minutes}m{sec:02d}s"
        return f"{sec}s"

    _MAX_POST_DELIVERABLE_REMINDERS = 2

    def _maybe_inject_post_deliverable_reminder(
        self, ctx: StepContext, messages: list[dict[str, Any]]
    ) -> None:
        """Append a strong system reminder when the model wrote the deliverable but stopped.

        Fires only when:
        - ``write_deliverable`` succeeded earlier in this step (the
          handler set ``ctx.post_deliverable_pending``);
        - the policy still requires ``step_complete``;
        - every todo is in a terminal state (so we are not nagging
          mid-work);
        - we have not already emitted the cap of two reminders.
        """

        if not ctx.post_deliverable_pending:
            return
        if not ctx.policy.require_step_complete:
            ctx.post_deliverable_pending = False
            return
        if ctx.post_deliverable_reminders_sent >= self._MAX_POST_DELIVERABLE_REMINDERS:
            return
        if self._get_incomplete_todos(ctx):
            # Todos are still pending — let the existing finalization
            # instruction handle it; the post-deliverable reminder is
            # specifically for the "wrote artifact, marked todos done,
            # then stopped" pattern.
            return

        ctx.post_deliverable_reminders_sent += 1
        messages.append(
            {
                "role": "system",
                "content": (
                    "<post_deliverable_reminder>\n"
                    "You wrote the deliverable and every todo is terminal. "
                    "Call step_complete now with summary, outputs (any structured "
                    "values the workflow expects), claims, and an outcome — "
                    "this finalizes the step and triggers delivery. Do not "
                    "start new exploration or restate the deliverable.\n"
                    "</post_deliverable_reminder>"
                ),
                "_post_deliverable_reminder": True,
            }
        )

    def _build_step_prompt(self, ctx: StepContext) -> str:
        """Build the step objective prompt.

        Includes task context, prior step outputs (resolved from the step's
        input configuration), the step objective, and any in-progress todos.
        Prior step outputs are included directly in the prompt so they are
        visible in session logs and prominent to the LLM.
        """
        parts: list[str] = []

        if ctx.task_title or ctx.task_description:
            parts.append("## Workflow Task\n\n")
            if ctx.task_title:
                parts.append(f"**{ctx.task_title}**\n\n")
            if ctx.task_description:
                parts.append(f"{ctx.task_description}\n\n")
            if ctx.task_expected_output:
                parts.append("## Workflow-Level Expected Output\n\n")
                parts.append(
                    f"{ctx.task_expected_output}\n\n"
                    "This describes the final task result. Use it to shape the current "
                    "step artifact, but do not perform later workflow steps unless the "
                    "current step explicitly asks for them.\n\n"
                )
            parts.append("## Workflow Delivery Policy\n\n")
            parts.append(
                "Notification delivery family: "
                f"{ctx.completion_delivery.completion_mode_family}\n\n"
            )
            parts.append(
                "Silent completion allowed: "
                f"{str(ctx.completion_delivery.allow_silent_completion).lower()}\n\n"
            )

        if ctx.project_context:
            parts.append(f"## Project Context\n\n{ctx.project_context}\n\n")

        # Inject prior step outputs so the LLM has context from previous steps.
        # This resolves the step's input configuration and reads structured
        # outputs from workflow state, making them visible in session logs.
        prior_output_text = self._format_prior_step_outputs(ctx)
        if prior_output_text:
            parts.append(f"## Prior Step Output\n\n{prior_output_text}\n\n")

        prompt_text = ctx.user_message or ctx.step_definition.prompt
        parts.append(f"## Current Step\n\nName: {ctx.step_definition.name}\n\n{prompt_text}")
        boundaries = self._workflow_step_boundaries(ctx)
        if boundaries:
            parts.append(f"\n\n## Current Step Boundaries\n\n{boundaries}")

        if ctx.todos:
            parts.append("\n\n## Current Step Todos\n")
            for todo in ctx.todos:
                status = todo.get("status", "pending")
                content = todo.get("content", "")
                parts.append(f"- [{status}] {content}")

        feedback = getattr(ctx.workflow_state, "last_evaluation_feedback", None)
        if feedback:
            parts.append(f"\n\n## Revision Feedback\n\n{feedback}")

        revision_context = getattr(ctx.workflow_state, "last_revision_context", None)
        if revision_context:
            parts.append(f"\n\n## Revision Context\n\n{revision_context}")
            ctx.workflow_state.last_revision_context = None

        operator_instruction = getattr(ctx.workflow_state, "last_operator_instruction", None)
        if operator_instruction:
            parts.append(
                "\n\n## Operator Instruction\n\n"
                "A human explicitly chose to continue or retry the workflow with this "
                f"instruction:\n\n{operator_instruction}"
            )

        # For system-agent delegations (require_step_complete=False) we do NOT
        # emit the heavy completion-actions block — the delegation focus system
        # instruction already tells the agent to write a final text message.
        # For workflow steps and user-agent delegations we keep the full contract.
        if not ctx.policy.require_step_complete:
            # Slim completion hint: write your result as assistant text. Done.
            parts.append(
                "\n\n## Completion\n\n"
                "When done, write your findings as a final assistant message. "
                "That text is returned to the caller. "
                "Optionally call `step_complete` for a structured summary or outcome."
            )
        elif self._deliverable_owner_step_run_id(ctx) is not None:
            parts.append(
                "\n\n## Required Completion Actions\n\n"
                "When you have completed the current step objective, call write_deliverable with the "
                "canonical user-facing artifact for this step. Respect Expected output "
                "closely for structure, tone, format, and level of detail. If "
                "Expected output conflicts with the step completion contract, still "
                "produce the minimum correct deliverable and write it via "
                "write_deliverable. Free-text assistant messages during workflow steps are "
                "reasoning and progress, not the final artifact. After writing the "
                "deliverable, call step_complete with a summary, structured outputs, "
                "verifiable claims, and an outcome when the completed step should "
                "explicitly report rejection or failure. Use notification.mode='silent' only when the work "
                "completed successfully, silent completion is allowed, and there is "
                "nothing user-actionable to notify. Use notification.mode='direct' "
                "for ready-to-read outputs like daily briefs, evening summaries, or "
                "report digests when the result should be sent directly to the "
                "resolved target channel. Otherwise omit notification and the "
                "configured delivery family will be used automatically."
            )
        else:
            parts.append(
                "\n\n## Required Completion Actions\n\n"
                "When you have completed the current step objective, write the final result as a normal "
                "assistant message. Respect Expected output closely for structure, tone, "
                "format, and level of detail. Then call step_complete with a summary, structured "
                "outputs, verifiable claims, and an outcome when the completed step should "
                "explicitly report rejection or failure. Use notification.mode='silent' only when the work "
                "completed successfully, silent completion is allowed, and there is "
                "nothing user-actionable to notify. Use notification.mode='direct' "
                "for ready-to-read outputs like daily briefs, evening summaries, or "
                "report digests when the result should be sent directly to the "
                "resolved target channel. Otherwise omit notification and the "
                "configured delivery family will be used automatically."
            )

        return "".join(parts)

    def _format_prior_step_outputs(self, ctx: StepContext) -> str:
        """Format prior step outputs for inclusion in the step prompt.

        Resolves the step's input configuration and reads structured outputs
        from ``workflow_state.step_outputs``.  Returns empty string if no
        prior outputs are available (first step or null input).
        """
        from cognis.models.workflow import StepOutput, resolve_effective_input, resolve_source_names

        if not ctx.workflow_state or not ctx.workflow_steps:
            return ""

        effective_input = resolve_effective_input(
            ctx.step_definition, ctx.step_index, ctx.workflow_steps
        )
        if effective_input.type == "null":
            return ""

        source_names = resolve_source_names(
            ctx.step_definition,
            ctx.step_index,
            ctx.workflow_steps,
        )
        if not source_names:
            return ""

        sections: list[str] = []
        for source_name in source_names:
            raw = ctx.workflow_state.step_outputs.get(source_name)
            if raw is None:
                continue
            output = StepOutput.model_validate(raw)
            has_deliverable = bool(output.deliverable_id)
            section_parts = [f'<step_output source="{source_name}">']
            if effective_input.type == "full":
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.metadata:
                    section_parts.append(
                        f"Metadata:\n{json.dumps(output.metadata, indent=2, default=str)}"
                    )
                if output.claims:
                    claims_str = "\n".join(f"  - {c}" for c in output.claims)
                    section_parts.append(f"Claims:\n{claims_str}")
                if output.content:
                    label = "Deliverable" if has_deliverable else "Assistant output"
                    section_parts.append(f"{label}:\n{output.content}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            elif effective_input.type == "summary":
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.metadata:
                    section_parts.append(
                        f"Metadata:\n{json.dumps(output.metadata, indent=2, default=str)}"
                    )
                if has_deliverable and output.content:
                    section_parts.append(f"Deliverable:\n{output.content}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            else:
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.metadata:
                    section_parts.append(
                        f"Metadata:\n{json.dumps(output.metadata, indent=2, default=str)}"
                    )
                if output.claims:
                    claims_str = "\n".join(f"  - {c}" for c in output.claims)
                    section_parts.append(f"Claims:\n{claims_str}")
                if has_deliverable and output.content:
                    section_parts.append(f"Deliverable:\n{output.content}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            section_parts.append("</step_output>")
            sections.append("\n".join(section_parts))

        return "\n\n".join(sections)

    def _build_controller_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Build JSON schemas for controller-injected tools.

        Schemas are sourced from the central registry definitions in
        ``cognis/tools/builtin/workflow.py`` so validators and LLM prompts
        always see the same shape. Conditional availability (step_complete
        only when the policy allows it, step_request_questions only when the
        step permits questions) is applied here.
        """

        from cognis.tools.builtin.orchestration import orchestration_tools
        from cognis.tools.builtin.workflow import (
            LIST_CREDENTIALS_TOOL,
            REQUEST_AUTH_CHALLENGE_TOOL,
            REQUEST_CREDENTIAL_TOOL,
            STEP_COMPLETE_TOOL,
            STEP_REQUEST_QUESTIONS_TOOL,
            STEP_TODO_LIST_TOOL,
            STEP_TODO_WRITE_TOOL,
            SWITCH_EXECUTOR_TOOL,
            WRITE_DELIVERABLE_TOOL,
        )

        def _to_schema(tool_def: Any) -> dict[str, Any]:
            import copy

            parameters = copy.deepcopy(tool_def.parameters)
            if tool_def.name == STEP_COMPLETE_TOOL.name:
                self._apply_step_metadata_contract_schema(ctx, parameters)
            return {
                "type": "function",
                "function": {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "parameters": parameters,
                },
            }

        tools: list[dict[str, Any]] = []

        if (
            ctx.policy.step_complete_available
            and self._deliverable_owner_step_run_id(ctx) is not None
        ):
            tools.append(_to_schema(WRITE_DELIVERABLE_TOOL))

        # step_complete — only when the policy allows it.
        if ctx.policy.step_complete_available:
            tools.append(_to_schema(STEP_COMPLETE_TOOL))

        # step_request_questions — only when the step permits interactive questions.
        if ctx.interaction_mode == "step_requests" and ctx.step_definition.allow_questions:
            tools.append(_to_schema(STEP_REQUEST_QUESTIONS_TOOL))

        # step_todo tools — always available.
        tools.append(_to_schema(STEP_TODO_WRITE_TOOL))
        tools.append(_to_schema(STEP_TODO_LIST_TOOL))

        # Credential / auth tools — gated by agent permissions.
        if _controller_builtin_enabled(ctx.agent, REQUEST_CREDENTIAL_TOOL):
            tools.append(_to_schema(REQUEST_CREDENTIAL_TOOL))
        if _controller_builtin_enabled(ctx.agent, REQUEST_AUTH_CHALLENGE_TOOL):
            tools.append(_to_schema(REQUEST_AUTH_CHALLENGE_TOOL))
        if _controller_builtin_enabled(ctx.agent, LIST_CREDENTIALS_TOOL):
            tools.append(_to_schema(LIST_CREDENTIALS_TOOL))

        if _controller_builtin_enabled(ctx.agent, SEARCH_TOOLS_TOOL):
            tools.append(_to_schema(SEARCH_TOOLS_TOOL))

        # Orchestration tools — based on orchestration_mode.
        conversation = getattr(ctx, "conversation", None)
        surface_policy = orchestration_surface_policy(getattr(conversation, "context", None))
        for tool_def in orchestration_tools(
            ctx.orchestration_mode,
            expose_delegate_wait_option=surface_policy.expose_delegate_wait_option,
            expose_managed_conversation_tools=surface_policy.expose_managed_conversation_tools,
            expose_task_tools=surface_policy.expose_task_tools,
            expose_workflow_tools=surface_policy.expose_workflow_tools,
            expose_compose_workflow_tool=surface_policy.expose_compose_workflow_tool,
        ):
            tools.append(_to_schema(tool_def))

        # Stage 36: switch_executor — exposed only when the agent has at
        # least two USABLE assigned executors. Hiding it when fewer are
        # usable avoids offering a no-op tool to the LLM.
        pool = getattr(ctx, "executor_pool", None)
        if pool is not None:
            usable_ids = sorted(t.executor_id for t in pool.all if t.usable and t.executor_id)
            if len(usable_ids) >= 2:
                import copy as _copy

                schema = _copy.deepcopy(SWITCH_EXECUTOR_TOOL.parameters)
                properties = schema.setdefault("properties", {})
                executor_field = properties.setdefault("executor_id", {"type": "string"})
                executor_field["enum"] = usable_ids
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": SWITCH_EXECUTOR_TOOL.name,
                            "description": SWITCH_EXECUTOR_TOOL.description,
                            "parameters": schema,
                        },
                    }
                )

        return tools

    def _get_controller_tool_parameters(
        self,
        tool_name: str,
        *,
        ctx: StepContext | None = None,
    ) -> dict[str, Any] | None:
        """Return the parameters schema for a controller tool, or None."""

        from cognis.tools.builtin.orchestration import orchestration_tools
        from cognis.tools.builtin.workflow import (
            LIST_CREDENTIALS_TOOL,
            REQUEST_AUTH_CHALLENGE_TOOL,
            REQUEST_CREDENTIAL_TOOL,
            STEP_COMPLETE_TOOL,
            STEP_REQUEST_QUESTIONS_TOOL,
            STEP_TODO_LIST_TOOL,
            STEP_TODO_WRITE_TOOL,
            SWITCH_EXECUTOR_TOOL,
            WRITE_DELIVERABLE_TOOL,
        )

        registry = {
            WRITE_DELIVERABLE_TOOL.name: WRITE_DELIVERABLE_TOOL,
            STEP_COMPLETE_TOOL.name: STEP_COMPLETE_TOOL,
            STEP_REQUEST_QUESTIONS_TOOL.name: STEP_REQUEST_QUESTIONS_TOOL,
            STEP_TODO_WRITE_TOOL.name: STEP_TODO_WRITE_TOOL,
            STEP_TODO_LIST_TOOL.name: STEP_TODO_LIST_TOOL,
            REQUEST_CREDENTIAL_TOOL.name: REQUEST_CREDENTIAL_TOOL,
            REQUEST_AUTH_CHALLENGE_TOOL.name: REQUEST_AUTH_CHALLENGE_TOOL,
            LIST_CREDENTIALS_TOOL.name: LIST_CREDENTIALS_TOOL,
            SWITCH_EXECUTOR_TOOL.name: SWITCH_EXECUTOR_TOOL,
            SEARCH_TOOLS_TOOL.name: SEARCH_TOOLS_TOOL,
        }
        for tool_def in orchestration_tools(OrchestrationMode.FULL):
            registry[tool_def.name] = tool_def
        tool_def = registry.get(tool_name)
        if tool_def is None:
            return None
        if ctx is not None and tool_name == STEP_COMPLETE_TOOL.name:
            import copy

            parameters = copy.deepcopy(tool_def.parameters)
            self._apply_step_metadata_contract_schema(ctx, parameters)
            return parameters
        return tool_def.parameters

    def _apply_step_metadata_contract_schema(
        self,
        ctx: StepContext,
        parameters: dict[str, Any],
    ) -> None:
        """Overlay the current step's metadata contract onto step_complete."""

        contract = getattr(ctx.step_definition, "metadata_contract", None)
        fields = list(getattr(contract, "fields", []) or [])
        if not fields:
            return
        properties = parameters.setdefault("properties", {})
        metadata_schema: dict[str, Any] = {
            "type": "object",
            "description": "Metadata required by this workflow step.",
            "properties": {},
        }
        required: list[str] = []
        for metadata_field in fields:
            field_schema: dict[str, Any] = {
                "type": self._metadata_json_schema_type(metadata_field.type)
            }
            if metadata_field.type == "array":
                description = str(getattr(metadata_field, "description", "") or "").lower()
                field_schema["items"] = {
                    "type": "object" if "array of objects" in description else "string"
                }
            if metadata_field.description:
                field_schema["description"] = metadata_field.description
            if metadata_field.enum:
                field_schema["enum"] = list(metadata_field.enum)
            metadata_schema["properties"][metadata_field.name] = field_schema
            if metadata_field.required:
                required.append(metadata_field.name)
        if required:
            metadata_schema["required"] = required
            parameters["required"] = sorted(
                set(list(parameters.get("required", [])) + ["metadata"])
            )
        properties["metadata"] = metadata_schema

    def _metadata_json_schema_type(self, field_type: str) -> str | list[str]:
        return "number" if field_type == "number" else field_type

    def _validate_step_metadata_contract(
        self,
        ctx: StepContext,
        arguments: dict[str, Any],
    ) -> None:
        """Validate step_complete metadata against the current step contract."""

        contract = getattr(ctx.step_definition, "metadata_contract", None)
        fields = list(getattr(contract, "fields", []) or [])
        if not fields:
            return
        metadata = arguments.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise StepMetadataContractError("step_complete.metadata must be an object")
        for metadata_field in fields:
            value = metadata.get(metadata_field.name)
            if value is None:
                if metadata_field.required:
                    raise StepMetadataContractError(
                        f"step_complete.metadata.{metadata_field.name} is required"
                    )
                continue
            if not self._metadata_value_matches_type(value, metadata_field.type):
                raise StepMetadataContractError(
                    f"step_complete.metadata.{metadata_field.name} must be {metadata_field.type}"
                )
            if metadata_field.enum is not None and value not in metadata_field.enum:
                allowed = ", ".join(str(item) for item in metadata_field.enum)
                raise StepMetadataContractError(
                    f"step_complete.metadata.{metadata_field.name} must be one of: {allowed}"
                )

    def _step_metadata_contract_error(
        self,
        ctx: StepContext,
        arguments: Any,
    ) -> StepMetadataContractError | None:
        """Return metadata contract failure for raw step_complete arguments, if any."""

        if not _step_metadata_contract_fields(ctx):
            return None
        if not isinstance(arguments, dict):
            return None
        try:
            self._validate_step_metadata_contract(ctx, arguments)
        except StepMetadataContractError as exc:
            return exc
        return None

    def _metadata_value_matches_type(self, value: Any, field_type: str) -> bool:
        if field_type == "string":
            return isinstance(value, str)
        if field_type == "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        if field_type == "boolean":
            return isinstance(value, bool)
        if field_type == "array":
            return isinstance(value, list)
        if field_type == "object":
            return isinstance(value, dict)
        return False

    def _validate_controller_tool_arguments(
        self,
        tool_name: str,
        raw_arguments: Any,
        *,
        ctx: StepContext | None = None,
    ) -> ToolArgumentError | None:
        """Validate ``raw_arguments`` against the controller tool schema."""

        schema = self._get_controller_tool_parameters(tool_name, ctx=ctx)
        return validate_tool_arguments(tool_name, raw_arguments, schema=schema)

    async def _emit_tool_argument_error(
        self,
        ctx: StepContext,
        *,
        tc: ToolCall,
        tool_id: str,
        events_to_record: list[SessionEvent],
        messages: list[dict[str, Any]],
        error: ToolArgumentError,
        on_tool_result: ToolResultCallback | None,
        on_token: TokenCallback | None,
    ) -> None:
        """Send a structured tool-error back to the LLM without mutating state."""

        payload = error.as_tool_result()
        content = json.dumps(payload)
        messages.append({"role": "tool", "tool_call_id": tc.call_id, "content": content})
        _append_tool_result_event(events_to_record, tc, content, True, tool_id=tool_id)
        _resolve_pending_tool_call(ctx, tc.call_id)
        await self._flush_events_incremental(
            ctx,
            events_to_record,
            reason=f"tool_result:{tc.name}:invalid_args",
            on_token=on_token,
        )
        if on_tool_result is not None:
            await on_tool_result(tc.call_id, tc.name, content, True, None, None)
        logger.warning(
            "tool: rejected malformed arguments",
            extra={
                "extra_data": {
                    "tool_name": tc.name,
                    "reason": error.reason,
                    "session_id": ctx.session.session_id,
                }
            },
        )

    def _get_executor_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Get tool schemas from the executor's tool registry.

        Returns schemas in the OpenAI function calling format.

        Stage 36: when the agent has multiple per-call-routable assigned
        executors that observe the same tool, an optional
        ``target_executor`` parameter is overlaid on the tool's input
        schema with an enum of valid executor ids. This lets the LLM
        route a single call to a specific executor without changing the
        conversation's active executor.

        Per-call routing only works for the active executor (any type)
        and for additional executors of type ``websocket`` (the only
        type that exposes a stable lookup-by-id connection model). The
        enum is filtered accordingly so the LLM cannot ask for
        unreachable per-call routing.
        """
        registry = self._get_tool_registry(ctx)
        if registry is None:
            return []

        from cognis.core.executor_pool import tool_observed_on
        from cognis.tools.builtin.orchestration import ORCHESTRATION_TOOL_NAMES

        pool = getattr(ctx, "executor_pool", None)
        active_id = getattr(ctx, "active_executor_id", None)

        def _per_call_routable(target: Any) -> bool:
            if not target.usable:
                return False
            if active_id is not None and target.executor_id == active_id:
                return True
            return target.executor_type == "websocket"

        def _executors_offering(tool_name: str) -> list[str]:
            if pool is None:
                return []
            return sorted(
                t.executor_id
                for t in pool.all
                if tool_observed_on(t, tool_name) and _per_call_routable(t)
            )

        schemas: list[dict[str, Any]] = []
        for tool_def in registry.list_tools():
            # Skip controller and orchestration tools (handled separately)
            if tool_def.name in CONTROLLER_TOOLS or tool_def.name in ORCHESTRATION_TOOL_NAMES:
                continue
            parameters = tool_def.parameters
            # Add target_executor overlay only for executor-routed tools
            # available on more than one per-call-routable assigned executor.
            if pool is not None and tool_def.source.type == "executor":
                offering = _executors_offering(tool_def.name)
                if len(offering) >= 2:
                    import copy as _copy

                    parameters = _copy.deepcopy(parameters)
                    properties = parameters.setdefault("properties", {})
                    properties["target_executor"] = {
                        "type": "string",
                        "enum": offering,
                        "description": (
                            "Optional. Run this single call on a specific "
                            "assigned executor. Omit to use the active "
                            "executor for the conversation."
                        ),
                    }
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_def.name,
                        "description": tool_def.description,
                        "parameters": parameters,
                    },
                }
            )
        return schemas

    def _get_tool_registry(self, ctx: StepContext) -> Any:
        """Get the tool registry for the current step."""
        if ctx.tool_registry is not None:
            return ctx.tool_registry
        return getattr(self.providers, "_tool_registry", None)

    def _get_classified_tool_registry(self, ctx: StepContext, registry: Any | None) -> Any | None:
        """Return a runtime registry overlaid with step-resolved tool classifications."""

        classified_definitions = getattr(ctx, "classified_tool_definitions", None)
        if not isinstance(classified_definitions, dict):
            classified_definitions = None
        if registry is None or not classified_definitions:
            return registry
        if not isinstance(registry, ToolRegistry):
            return registry

        classified_registry = ToolRegistry()
        changed = False
        for registered in registry.items():
            classified_definition = classified_definitions.get(
                stable_tool_id(registered.definition)
            )
            if classified_definition is None:
                classified_registry.register(registered)
                continue
            changed = True
            classified_registry.register(
                RegisteredTool(definition=classified_definition, handler=registered.handler)
            )
        return classified_registry if changed else registry

    def _get_initial_promoted_tool_ids(self, ctx: StepContext) -> set[str]:
        """Return tool ids that should be promoted visible before any discovery calls.

        Skill-attached tool ids are pre-promoted so they survive from the
        previous session into the current step without requiring a fresh
        ``search_tools`` call.  Session-discovered tool ids are also restored
        here; later inventory/profile/permission filtering decides whether they
        are still valid for this turn.
        """

        promoted: set[str] = set()
        if isinstance(ctx.agent.skills, dict):
            raw_ids = ctx.agent.skills.get("_attached_skill_tool_ids")
            if isinstance(raw_ids, list):
                promoted.update(
                    str(tool_id)
                    for tool_id in raw_ids
                    if isinstance(tool_id, str) and tool_id.strip()
                )
            auto_loaded_ids = ctx.agent.skills.get("_auto_loaded_skill_tool_ids")
            if isinstance(auto_loaded_ids, list):
                promoted.update(
                    str(tool_id)
                    for tool_id in auto_loaded_ids
                    if isinstance(tool_id, str) and tool_id.strip()
                )
        get_discovered = getattr(self.session_cache, "get_discovered_tool_ids", None)
        if callable(get_discovered):
            promoted.update(
                str(tool_id)
                for tool_id in get_discovered(ctx.session.session_id)
                if isinstance(tool_id, str) and tool_id.strip()
            )
        return promoted

    def _get_initial_activated_tool_ids(self, ctx: StepContext) -> set[str]:
        activated: set[str] = set()
        if isinstance(ctx.agent.skills, dict):
            raw_auto_loaded_ids = ctx.agent.skills.get("_auto_loaded_skill_tool_ids")
            if isinstance(raw_auto_loaded_ids, list):
                activated.update(
                    str(tool_id)
                    for tool_id in raw_auto_loaded_ids
                    if isinstance(tool_id, str) and tool_id.strip()
                )
        getter = getattr(self.session_cache, "get_activated_skill_tool_ids", None)
        if not callable(getter):
            return activated
        activated.update(getter(ctx.session.session_id))
        return activated

    def _step_log_metadata(self, ctx: StepContext) -> dict[str, Any]:
        workflow = getattr(ctx, "workflow", None)
        step = getattr(ctx, "step", None)
        step_definition = getattr(ctx, "step_definition", None)
        return {
            "workflow_id": getattr(workflow, "workflow_id", None),
            "step_id": getattr(step, "step_id", None)
            or getattr(step_definition, "step_id", None)
            or getattr(step_definition, "name", None),
        }

    # ---------------------------------------------------------------------------
    # Skill activation helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _scan_referenced_services(
        tags: list[str],
        instructions: str,
    ) -> list[str]:
        """Return a deduplicated list of service/server names referenced by the skill.

        The scan is deterministic and cheap: it lowercases the instructions once
        and looks for any of the tag tokens plus a small fixed set of common
        service keywords.  Matched tokens are normalised to the canonical server
        name so the classifier can directly compare against ``source.server_name``.
        """
        fixed_services = {
            "googleworkspace": "googleworkspace",
            "google workspace": "googleworkspace",
            "gmail": "googleworkspace",
            "google calendar": "googleworkspace",
            "google drive": "googleworkspace",
            "google docs": "googleworkspace",
            "google sheets": "googleworkspace",
            "todoist": "todoist",
            "notion": "notion",
            "slack": "slack",
            "github": "github",
            "jira": "jira",
            "linear": "linear",
            "obsidian": "obsidian",
            "home assistant": "hass",
            "hass": "hass",
            "oura": "oura",
            "rohlik": "rohlik",
            "browser": "browser",
            "web": "web",
            "ainews": "ainews",
            "weather": "weather",
        }
        text = (instructions or "").lower()
        found: list[str] = []
        seen: set[str] = set()
        # Tags are the highest-signal source, but only when they map to a known
        # canonical service. Keep raw tags separate in the prompt; do not treat
        # every arbitrary tag as a service hint.
        for tag in tags:
            canonical = fixed_services.get(tag.lower())
            if canonical is not None and canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
        # Fixed service names.
        for keyword, canonical in fixed_services.items():
            if keyword in text and canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
        return found[:10]

    async def _classify_skill_activation_tool_ids(
        self,
        ctx: StepContext,
        *,
        activation: dict[str, Any],
        candidate_tools: list[ToolDefinition],
    ) -> set[str]:
        """Classify which policy-hidden tools the skill needs.

        Returns the minimal set of tool ids that should be activated.
        Results are cached per session by a prompt-shaping signature built from
        the skill id, content hash, tags, name, and description so tag-only
        edits cannot reuse stale activations.
        """
        cache_material = {
            "skill_id": str(activation.get("skill_id") or "").strip(),
            "content_hash": str(activation.get("content_hash") or "").strip(),
            "name": str(activation.get("name") or "").strip(),
            "description": str(activation.get("description") or "").strip(),
            "tags": sorted(
                str(tag).strip()
                for tag in (activation.get("tags") or [])
                if isinstance(tag, str) and str(tag).strip()
            ),
        }
        cache_key = (
            f"{cache_material['skill_id']}:"
            f"{hashlib.sha1(json.dumps(cache_material, sort_keys=True).encode('utf-8')).hexdigest()[:16]}"
        ).strip(":") or str(activation.get("skill_id") or "")
        cache_key_label = cache_key[:16]
        get_cached = getattr(self.session_cache, "get_skill_tool_classification", None)
        if callable(get_cached):
            cached = get_cached(ctx.session.session_id, cache_key)
            if isinstance(cached, list):
                valid_tool_ids = {stable_tool_id(tool) for tool in candidate_tools}
                revalidated = {
                    str(tool_id)
                    for tool_id in cached
                    if isinstance(tool_id, str) and tool_id.strip() and tool_id in valid_tool_ids
                }
                logger.info(
                    "skill tool classifier cache hit",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            **self._step_log_metadata(ctx),
                            "skill_id": activation.get("skill_id"),
                            "cache_key": cache_key_label,
                            "tool_ids": sorted(revalidated),
                        }
                    },
                )
                return revalidated

        logger.info(
            "skill tool classifier started",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    **self._step_log_metadata(ctx),
                    "skill_id": activation.get("skill_id"),
                    "cache_key": cache_key_label,
                    "candidate_inventory_count": len(candidate_tools),
                }
            },
        )

        # Build structured candidate lines with rich metadata.
        candidate_lines = []
        tool_by_id = {stable_tool_id(tool): tool for tool in candidate_tools}
        for tool_id, tool in sorted(
            tool_by_id.items(), key=lambda item: (item[1].name.lower(), item[0])
        ):
            source_label = tool.source.type
            if tool.source.server_name:
                source_label = f"{tool.source.type}:{tool.source.server_name}"
            description = re.sub(r"\s+", " ", str(tool.description or "")).strip()[:160]
            candidate_lines.append(
                f"- {tool_id}: {tool.name}"
                f" [category={tool.category}, profile={tool_profile_group(tool)}"
                f", source={source_label}"
                f", read_only={tool.read_only}]"
                f" — {description}"
            )
        if not candidate_lines:
            logger.info(
                "skill tool classifier found no policy-hidden candidates",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **self._step_log_metadata(ctx),
                        "skill_id": activation.get("skill_id"),
                        "cache_key": cache_key_label,
                    }
                },
            )
            return set()

        chunk_size = 100
        candidate_chunks = [
            candidate_lines[index : index + chunk_size]
            for index in range(0, len(candidate_lines), chunk_size)
        ]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "skill tool classifier candidates",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **self._step_log_metadata(ctx),
                        "skill_id": activation.get("skill_id"),
                        "cache_key": cache_key_label,
                        "chunk_count": len(candidate_chunks),
                        "chunk_size": chunk_size,
                        "candidates": candidate_lines[:50],
                    }
                },
            )

        # Build structured skill context — tags + referenced services as hints.
        skill_tags = [str(t) for t in (activation.get("tags") or []) if isinstance(t, str)]
        instructions_text = str(activation.get("instructions") or "")
        referenced_services = self._scan_referenced_services(skill_tags, instructions_text)

        # Produce an instruction-excerpt that preserves section headers.
        excerpt_lines = []
        remaining = 3000
        for line in instructions_text.splitlines():
            excerpt_lines.append(line)
            remaining -= len(line) + 1
            if remaining <= 0:
                excerpt_lines.append("… (truncated)")
                break
        instructions_excerpt = "\n".join(excerpt_lines)

        skill_context = (
            f"Skill: {activation.get('name')} ({activation.get('skill_id')})\n"
            f"Tags: {', '.join(skill_tags) or 'none'}\n"
            f"Description: {activation.get('description') or ''}\n"
            + (
                f"Services referenced in instructions: {', '.join(referenced_services)}\n"
                if referenced_services
                else ""
            )
            + f"Instructions:\n{instructions_excerpt}"
        )

        # Tightened system prompt (A2).
        system_prompt = (
            "You classify which policy-hidden tools a loaded skill needs to activate. "
            "Return strict JSON with keys 'tool_ids' (array of stable tool ids) and "
            "'reasons' (object mapping tool_id to a one-sentence justification). "
            "Return empty 'tool_ids' when no tool is clearly required.\n\n"
            "Selection rules, in priority order:\n"
            "1. Choose the MINIMAL set of tools the skill genuinely needs.\n"
            "2. Prefer tools whose server_name or name matches a service named in the "
            "skill tags or 'Services referenced' list (e.g. tag 'gmail' → prefer "
            "tools from 'googleworkspace' server with 'gmail' in the name).\n"
            "3. Prefer service-specific tools over generic alternatives. When the skill "
            "mentions a concrete service, pick that service's tool, not a generic "
            "stand-in (avoid generic names like 'search_custom', 'browser_open', "
            "'browser_get_text' unless the skill explicitly requires generic web search "
            "or browser automation).\n"
            "4. Avoid tools with category/profile 'system' unless the skill explicitly "
            "requires controller/admin operations.\n"
            "5. Avoid read_only=False tools unless the skill explicitly performs writes.\n"
            "6. Be conservative. An empty result is better than a wrong result."
        )

        resolved: set[str] = set()
        all_reasons: dict[str, str] = {}
        for chunk_index, candidate_chunk in enumerate(candidate_chunks, start=1):
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{skill_context}\n\n"
                        f"Candidate chunk: {chunk_index}/{len(candidate_chunks)}\n"
                        "Each candidate line: "
                        "- tool_id: name [category=X, profile=Y, source=Z, read_only=T/F] — description\n"
                        "Candidate tools:\n" + "\n".join(candidate_chunk) + "\n\nReturn JSON only."
                    ),
                },
            ]
            try:
                response = await self.providers.llm.generate(
                    messages,
                    task_type="classifier",
                    response_format={"type": "json_object"},
                    acting_user_email=getattr(ctx.session, "user_email", None),
                    cognis_session_id=ctx.session.session_id,
                )
                content = (
                    extract_visible_text_from_response(response)
                    if isinstance(response, dict)
                    else ""
                )
                payload = extract_json_object(
                    content or "{}",
                    label="skill_tool_classifier",
                )
                raw_ids = payload.get("tool_ids") if isinstance(payload, dict) else []
                # A4: capture reasons at DEBUG level only.
                reasons = payload.get("reasons") if isinstance(payload, dict) else None
                if isinstance(reasons, dict):
                    all_reasons.update(
                        {str(k): str(v)[:200] for k, v in reasons.items() if k and v}
                    )
            except Exception:
                logger.warning(
                    "skill tool classifier failed",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "skill_id": activation.get("skill_id"),
                            "chunk_index": chunk_index,
                            "chunk_count": len(candidate_chunks),
                        }
                    },
                    exc_info=True,
                )
                return set()

            resolved.update(
                {
                    str(tool_id)
                    for tool_id in raw_ids
                    if isinstance(tool_id, str) and tool_id.strip() and tool_id in tool_by_id
                }
            )

        set_cached = getattr(self.session_cache, "set_skill_tool_classification", None)
        if callable(set_cached):
            set_cached(ctx.session.session_id, cache_key, sorted(resolved))
        logger.info(
            "skill tool classifier resolved",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    **self._step_log_metadata(ctx),
                    "skill_id": activation.get("skill_id"),
                    "cache_key": cache_key_label,
                    "candidate_count": len(candidate_lines),
                    "chunk_count": len(candidate_chunks),
                    "resolved_count": len(resolved),
                    "resolved_tool_ids": sorted(resolved),
                    "referenced_services": referenced_services,
                    "skill_tags": skill_tags,
                }
            },
        )
        if all_reasons and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "skill tool classifier reasons",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **self._step_log_metadata(ctx),
                        "skill_id": activation.get("skill_id"),
                        "reasons": {k: v for k, v in all_reasons.items() if k in resolved},
                    }
                },
            )
        return resolved

    async def _apply_skill_activation(
        self,
        ctx: StepContext,
        *,
        metadata: dict[str, Any],
        promoted_tool_ids: set[str],
        activated_tool_ids: set[str],
    ) -> str | None:
        """Activate tools for a loaded skill.

        Returns an optional transparency notice string (``<skill_activation>``
        block) that the caller should inject as a system message so the model
        knows which tools were auto-enabled and can self-correct if needed.
        """
        activation = metadata.get("skill_activation")
        if not isinstance(activation, dict):
            return
        skill_id = str(activation.get("skill_id") or "").strip()
        if not skill_id:
            return

        # Wire protocol: skill management tools emit "discovered_tool_ids" in their
        # metadata dicts.  The internal variable is promoted_tool_ids, but the key
        # name is kept stable so existing skill tool metadata round-trips correctly.
        raw_declared_tool_ids = metadata.get("discovered_tool_ids")
        resolved_tool_ids = {
            str(tool_id)
            for tool_id in raw_declared_tool_ids
            if isinstance(tool_id, str) and tool_id.strip()
        }
        resolution_path = "declared"
        if not resolved_tool_ids and ctx.tool_registry is not None:
            resolution_path = "classified"
            resolved_profile = resolve_step_profile(ctx.step_definition)
            full_inventory_tools = _filter_model_inventory_tools(
                ctx.agent,
                classify_tool_definitions_sync(ctx.tool_registry.list_tools()),
                promoted_tool_ids,
                activated_tool_ids,
            )
            # B1 — strict policy-hidden scope.
            # The classifier's job is to UNLOCK tools that the step profile
            # would not expose by default.  Policy-visible tools are already
            # reachable through the normal visible set plus search_tools; they
            # do not need activation and should not appear as candidates.
            # (Previously we scoped by "not already_visible_tool_ids" which was
            # "not currently visible due to cap" — that over-included policy-
            # visible tools and let the classifier activate wrong service tools
            # like 'search_custom' instead of 'get_events'.)
            candidate_tools = [
                tool
                for tool in full_inventory_tools
                if (
                    step_profile_allows_tool(tool, resolved_profile)
                    or stable_tool_id(tool) in activated_tool_ids
                )
                and not step_profile_visible_by_default(tool, resolved_profile)
                and stable_tool_id(tool) not in activated_tool_ids
            ]
            resolved_tool_ids = await self._classify_skill_activation_tool_ids(
                ctx,
                activation=activation,
                candidate_tools=candidate_tools,
            )
        if not resolved_tool_ids:
            logger.info(
                "skill activation resolved no tools",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **self._step_log_metadata(ctx),
                        "skill_id": skill_id,
                        "resolution_path": resolution_path,
                    }
                },
            )
            return None

        # Promote the resolved tool ids so they become visible next turn.
        promoted_tool_ids.update(resolved_tool_ids)
        activated_tool_ids.update(resolved_tool_ids)
        activate = getattr(self.session_cache, "activate_skill_tools", None)
        if callable(activate):
            activate(ctx.session.session_id, skill_id, resolved_tool_ids)
        logger.info(
            "skill activation resolved tools",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    **self._step_log_metadata(ctx),
                    "skill_id": skill_id,
                    "resolution_path": resolution_path,
                    "tool_count": len(resolved_tool_ids),
                    "tool_ids": sorted(resolved_tool_ids),
                }
            },
        )

        # B2 — return a transparency notice for the model.
        # Keeps it short: tool names only, plus a self-correction hint.
        notice_lines = [
            f'<skill_activation skill_id="{skill_id}">',
            "The following tools were auto-enabled for this skill. "
            "If they do not match what the skill actually needs, call search_tools "
            "to find the correct ones.",
        ]
        for tool_id_str in sorted(resolved_tool_ids):
            tool_obj = next(
                (
                    t
                    for t in (ctx.tool_registry.list_tools() if ctx.tool_registry else [])
                    if stable_tool_id(t) == tool_id_str
                ),
                None,
            )
            name = tool_obj.name if tool_obj else tool_id_str
            notice_lines.append(f"- {name} ({tool_id_str})")
        notice_lines.append("</skill_activation>")
        return "\n".join(notice_lines)

    def _merge_promoted_tool_ids(
        self, promoted_tool_ids: set[str], metadata: dict[str, Any]
    ) -> None:
        """Merge newly promoted tool ids from tool metadata.

        ``discovered_tool_ids`` in tool metadata carries tool ids that a tool
        result wants to promote into the visible surface (e.g. skill-write
        auto-binding a newly created skill).  ``removed_tool_ids`` revokes
        promotion (e.g. skill deletion).
        """

        # Wire protocol key — intentionally kept as "discovered_tool_ids" to match
        # what skill management tools emit in their metadata dicts.
        raw_ids = metadata.get("discovered_tool_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        promoted_tool_ids.update(
            str(tool_id) for tool_id in raw_ids if isinstance(tool_id, str) and tool_id.strip()
        )
        removed_ids = metadata.get("removed_tool_ids")
        if isinstance(removed_ids, list):
            promoted_tool_ids.difference_update(
                str(tool_id)
                for tool_id in removed_ids
                if isinstance(tool_id, str) and tool_id.strip()
            )

    def _apply_skill_attachment_metadata(self, ctx: StepContext, metadata: dict[str, Any]) -> None:
        """Keep in-memory agent skill refs aligned with skill management mutations."""

        if not isinstance(ctx.agent.skills, dict):
            ctx.agent.skills = {}

        items = ctx.agent.skills.get("items")
        if not isinstance(items, list):
            items = []

        attached_skill_id = metadata.get("attached_skill_id")
        if (
            isinstance(attached_skill_id, str)
            and attached_skill_id.strip()
            and not any(
                isinstance(item, dict) and item.get("skill_id") == attached_skill_id
                for item in items
            )
        ):
            items.append({"skill_id": attached_skill_id, "enabled": True})

        attached_tool_ids_by_skill = ctx.agent.skills.get("_attached_skill_tool_ids_by_skill")
        if not isinstance(attached_tool_ids_by_skill, dict):
            attached_tool_ids_by_skill = {}

        raw_discovered_tool_ids = metadata.get("discovered_tool_ids")
        discovered_tool_ids_list = (
            raw_discovered_tool_ids if isinstance(raw_discovered_tool_ids, list) else []
        )
        if isinstance(attached_skill_id, str) and attached_skill_id.strip():
            discovered_tool_ids = [
                str(tool_id)
                for tool_id in discovered_tool_ids_list
                if isinstance(tool_id, str) and tool_id.strip()
            ]
            if discovered_tool_ids:
                attached_tool_ids_by_skill[attached_skill_id] = discovered_tool_ids

        deleted_skill_id = metadata.get("deleted_skill_id")
        if isinstance(deleted_skill_id, str) and deleted_skill_id.strip():
            items = [
                item
                for item in items
                if not (isinstance(item, dict) and item.get("skill_id") == deleted_skill_id)
            ]
            attached_tool_ids_by_skill.pop(deleted_skill_id, None)

        if attached_tool_ids_by_skill:
            ctx.agent.skills["_attached_skill_tool_ids_by_skill"] = attached_tool_ids_by_skill
            ctx.agent.skills["_attached_skill_tool_ids"] = sorted(
                {
                    tool_id
                    for raw_tool_ids in attached_tool_ids_by_skill.values()
                    if isinstance(raw_tool_ids, list)
                    for tool_id in raw_tool_ids
                    if isinstance(tool_id, str) and tool_id.strip()
                }
            )
        elif "_attached_skill_tool_ids_by_skill" in ctx.agent.skills:
            ctx.agent.skills.pop("_attached_skill_tool_ids_by_skill", None)
            ctx.agent.skills["_attached_skill_tool_ids"] = []

        ctx.agent.skills["items"] = items

    def _get_executor(self, ctx: StepContext) -> Any:
        """Get the executor connection for the current step."""
        if ctx.executor_connection is not None:
            return ctx.executor_connection
        return getattr(self.providers, "_executor_connection", None)
