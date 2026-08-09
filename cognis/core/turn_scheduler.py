"""Transport-agnostic turn orchestration.

TurnScheduler owns the full lifecycle of a chat turn — from user message
to response — without any dependency on WebSocket or other transport layers.

It handles:
- Turn submission and serialization (one active turn per conversation)
- Decision engine dispatch for inline chat turns
- Follow-up turns (system-initiated, via EventBus subscription)
- Turn cancellation
- Error classification
- Post-turn housekeeping (last_message_at, session cache refresh, title change)
- Conversation runtime loading (including deferred session creation after compaction)

Transport layers (WebSocket, REST, channel adapters) use TurnScheduler
as their single entry point for chat turns and register TurnObserver
instances for real-time streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import inspect
import json
import re
import uuid
from collections import OrderedDict, defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from time import monotonic
from typing import Any, Protocol, cast, runtime_checkable

import httpx
from prometheus_client import Counter, Histogram
from sqlalchemy import and_, case, delete, or_, select, update

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.audio.transcription import transcribe_audio_bytes
from cognis.core.agent_direct import is_agent_direct_context
from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.core.artifact_inputs import authorize_outbound_artifact_refs_in_session
from cognis.core.attachment_compat import supports_native_image_input
from cognis.core.attachment_utils import (
    attachment_placeholder_text,
    attachment_refs_to_dicts,
    normalize_attachment_refs,
    strip_attachment_payload_bytes,
)
from cognis.core.chat_modes import (
    ChatMode,
    ResolvedChatMode,
    optional_chat_mode,
    parse_chat_mode_directive,
    resolve_chat_mode,
)
from cognis.core.chat_v2_runtime_relay import RelayGenerationContext
from cognis.core.commands import is_system_slash_command_message
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.controller_runtime import ControllerLifecycleState, ControllerRuntime
from cognis.core.direct_turn_runtime import (
    DirectTurnExecutionFence,
    DurableDirectTurnRuntime,
    PermanentDirectTurnControllerError,
    StaleDirectTurnOwner,
)
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import (
    LLM_CYCLE_CEILING_CONTINUATION_REASON,
    STEP_TIMEOUT_CONTINUATION_REASON,
    TOOL_CALL_CEILING_CONTINUATION_REASON,
    ContinuationFollowUp,
    FollowUpMetadata,
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRelevanceHint,
    FollowUpRequiredAction,
    FollowUpStatus,
    build_automatic_continuation_follow_up,
    build_follow_up_id,
    parse_follow_up_metadata,
    positive_optional_int,
    render_follow_up_turn_notice,
    truncate_follow_up_text,
)
from cognis.core.harness_guards import (
    SameTurnToolCallLedger,
    tool_call_argument_fingerprint,
)
from cognis.core.long_lived_chat import is_long_lived_chat_context
from cognis.core.managed_conversations import (
    ManagedConversationAdmissionConflict,
    ManagedConversationTurnObserver,
)
from cognis.core.message_envelope import message_metadata
from cognis.core.runtime import TransientExecutorUnavailable
from cognis.core.runtime_metadata import assistant_message_runtime_metadata
from cognis.core.title_policy import can_adopt_intaris_title, sync_intaris_title
from cognis.core.tool_output_presentation import build_transport_tool_output_preview
from cognis.core.tool_output_spool import ToolOutputSpool, ToolOutputSpoolPage
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.channel import ChannelDeliveryDescriptor
from cognis.models.retry import (
    RetryReason,
    normalize_retry_reason,
    retry_notice_text,
    retry_reason_from_interruption,
)
from cognis.models.session import (
    BLOCKED_STATES,
    ConversationModel,
    SessionEvent,
    SessionModel,
    SessionStatus,
    SessionTransition,
)
from cognis.models.task import TaskDelivery
from cognis.providers.executor.delivery import AmbiguousToolOutcome
from cognis.providers.retry import is_retryable_http_error
from cognis.runtime_context import (
    current_agent_id,
    current_agent_owner_email,
    current_effective_working_directory,
    current_user_email,
    current_workspace_root,
    scoped_runtime_context,
)
from cognis.store import queries
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.direct_turns import (
    TERMINAL_STATUSES,
    DirectTurnAdmissionGuard,
    DirectTurnAdmissionRejected,
    DirectTurnStatus,
    DirectTurnStore,
    MaterializedDirectTurnPayload,
)
from cognis.store.models import (
    DirectTurnRequestRow,
    FollowUpDedupeRow,
    FollowUpIntentRow,
    ManagedConversationLink,
)
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)
_MAX_AUDIO_TRANSCRIPT_CHARS = 16_000
_MAX_AUDIO_TRANSCRIPT_CONTEXT_CHARS = 32_000

_CANCELLED_TURN_ERROR_CODES = {"cancelled", "queued_turn_cancelled", "turn_cancelled"}

_MAX_ACTIVE_TOOL_OUTPUT_CHARS = 64_000
_ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS = 6 * 60 * 60


def _durable_turn_error_message(error: TurnError) -> str:
    """Return a stable category-only message safe for durable history."""

    if error.code == "executor_unavailable":
        return "The selected executor is temporarily unavailable. Try again shortly."
    if error.code in _CANCELLED_TURN_ERROR_CODES:
        return "The turn was cancelled."
    if error.code.startswith("provider_"):
        return "A required provider was unavailable while processing this turn."
    return "Turn execution failed."


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

TURNS_TOTAL = Counter(
    "cognis_turns_total",
    "Total chat turns executed",
    ["outcome"],  # completed, delegated, error, cancelled
)
TURN_DURATION = Histogram(
    "cognis_turn_duration_seconds",
    "Duration of chat turns",
    ["type"],  # user, system
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
FOLLOW_UP_DEDUPE_TOTAL = Counter(
    "cognis_follow_up_dedupe_total",
    "Suppressed duplicate follow-up turn requests",
    ["reason"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ACTIVE_TURNS_PER_USER = 20
DEFAULT_MAX_QUEUED_MESSAGES = 20
DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS = 21600
DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS = 20
_MAX_DEFERRED_LOCKS = 200
FOLLOW_UP_DEDUPE_TTL_SECONDS = 600.0
FOLLOW_UP_INTENT_LEASE_SECONDS = 120.0
FOLLOW_UP_INTENT_MAX_ATTEMPTS = 3
# Durable direct turns should ride out short dependency outages. The runtime
# retries at a bounded cadence, so this keeps the FIFO head recoverable for
# roughly a minute while still surfacing persistent failures eventually.
DIRECT_TURN_TRANSIENT_MAX_ATTEMPTS = 120
MAX_TURN_TOOL_CALL_LEDGERS = 4096


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _positive_int_setting(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if not isinstance(value, int | float | str | bytes | bytearray):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int_setting(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if not isinstance(value, int | float | str | bytes | bytearray):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _is_expired_timestamp(value: datetime | None, *, now: datetime | None = None) -> bool:
    normalized = _normalize_utc(value)
    if normalized is None:
        return True
    return normalized <= (now or _utcnow())


def _effective_user_content(content: str, attachments: list[AttachmentRef]) -> str:
    if content.strip():
        return content
    if not attachments:
        return content
    return attachment_placeholder_text(attachment.kind for attachment in attachments)


def _should_bootstrap_wait_for_intention(conversation: ConversationModel) -> bool:
    """Return whether the first model turn should wait for Intaris intention bootstrap."""

    if can_adopt_intaris_title(conversation):
        return True
    context = conversation.context
    if context is None:
        return False
    if not is_agent_direct_context(context.type, context.platform_data):
        return False
    return not (conversation.title or "").strip()


def _utf16_code_units(value: str) -> int:
    """Return the string length in JavaScript-compatible UTF-16 code units."""

    return len(value.encode("utf-16-le")) // 2


def _model_copy_or_self(value: Any) -> Any:
    """Return a defensive model copy when the runtime object supports it."""

    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        return model_copy(deep=True)
    return value


def _trim_callback_args(callback: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Drop newly added optional callback args for older observers."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return args
    if any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    ):
        return args
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return args[: len(positional)]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TurnError:
    """Structured error from a failed turn."""

    code: str
    message: str
    recoverable: bool
    detail: dict[str, Any] | None = None
    turn_id: str | None = None
    transient: bool = False


@dataclass(slots=True)
class TurnResult:
    """Result of a completed turn."""

    conversation_id: str
    session_id: str
    message_id: str
    turn_id: str | None = None
    last_seq: int = 0
    context_usage: dict[str, Any] | None = None
    last_generation: dict[str, Any] | None = None
    delegated: bool = False
    task_id: str | None = None
    error: TurnError | None = None
    title_changed: bool = False
    new_title: str | None = None
    final_content: str | None = None
    system_initiated: bool = False
    channel_deliverable: bool = False
    delivery_id: str | None = None
    delivery_fallback_text: str | None = None
    attachments: list[dict[str, Any]] | None = None
    final_deliverable_id: str | None = None
    completed_at: datetime | None = None
    chat_mode: ChatMode = "default"
    chat_mode_source: str = "system_default"
    partial: bool = False
    finish_reason: str | None = None
    assistant_phase_index: int = 0
    turn_cycle_index: int | None = None
    managed_continuation_pending: bool = False
    runtime: dict[str, Any] | None = None


@dataclass(slots=True)
class ActiveStreamState:
    """Volatile snapshot of the currently unpersisted assistant stream."""

    conversation_id: str
    session_id: str
    message_id: str
    turn_id: str | None
    content: str = ""
    chunk_count: int = 0
    assistant_phase_index: int = 0
    turn_cycle_index: int = 0
    updated_at: datetime = field(default_factory=_utcnow)

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "content": self.content,
            "chunk_count": self.chunk_count,
            "assistant_phase_index": self.assistant_phase_index,
            "assistant_phase_authoritative": True,
            "content_offset": _utf16_code_units(self.content),
            "updated_at": self.updated_at.isoformat(),
        }
        snapshot["turn_cycle_index"] = self.turn_cycle_index
        return snapshot


@dataclass(slots=True)
class ActiveToolOutputSnapshot:
    """Volatile bounded snapshot of streamed tool output."""

    conversation_id: str
    session_id: str
    call_id: str
    tool_name: str
    turn_id: str | None
    assistant_phase_index: int | None = None
    turn_cycle_index: int = 0
    status: str = "running"
    result: str = ""
    stream: str | None = None
    is_error: bool = False
    chunk_count: int = 0
    content_offset: int = 0
    output_size: int = 0
    truncated: bool = False
    agent_visible_truncated: bool = False
    transport_truncated: bool = False
    has_full_output: bool = False
    recovery_call_id: str | None = None
    tool_output_artifact_id: str | None = None
    anchors_available: bool = False
    anchor_count: int = 0
    progress_phase: str | None = None
    progress_input_chars: int | None = None
    progress_input_lines: int | None = None
    progress_complete: bool | None = None
    managed_conversation: dict[str, Any] | None = None
    # Parent-log-safe structured tool arguments (dict). Carried on the runtime
    # overlay so the live tool card renders its per-tool subtitle/body before
    # the canonical tool_call event lands. Never contains delegated prompt
    # content (delegate arguments are redacted upstream in on_tool_call).
    arguments: dict[str, Any] | None = None
    updated_at: datetime = field(default_factory=_utcnow)

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) - self.updated_at > timedelta(
            seconds=_ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS
        )

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "assistant_phase_index": self.assistant_phase_index,
            "assistant_phase_authoritative": self.assistant_phase_index is not None,
            "status": self.status,
            "result": self.result,
            "stream": self.stream,
            "is_error": self.is_error,
            "chunk_count": self.chunk_count,
            "content_offset": self.content_offset,
            "output_size": self.output_size,
            "truncated": self.truncated,
            "agent_visible_truncated": self.agent_visible_truncated,
            "transport_truncated": self.transport_truncated,
            "has_full_output": self.has_full_output,
            "recovery_call_id": self.recovery_call_id,
            "tool_output_artifact_id": self.tool_output_artifact_id,
            "anchors_available": self.anchors_available,
            "anchor_count": self.anchor_count,
            "progress_phase": self.progress_phase,
            "progress_input_chars": self.progress_input_chars,
            "progress_input_lines": self.progress_input_lines,
            "progress_complete": self.progress_complete,
            "managed_conversation": self.managed_conversation,
            "arguments": self.arguments,
            "updated_at": self.updated_at.isoformat(),
        }
        snapshot["turn_cycle_index"] = self.turn_cycle_index
        return snapshot

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> ActiveToolOutputSnapshot | None:
        try:
            updated_raw = data.get("updated_at")
            updated_at = (
                datetime.fromisoformat(updated_raw) if isinstance(updated_raw, str) else _utcnow()
            )
            return cls(
                conversation_id=str(data["conversation_id"]),
                session_id=str(data["session_id"]),
                call_id=str(data["call_id"]),
                tool_name=str(data.get("tool_name") or "tool"),
                turn_id=data.get("turn_id") if isinstance(data.get("turn_id"), str) else None,
                assistant_phase_index=data.get("assistant_phase_index")
                if isinstance(data.get("assistant_phase_index"), int)
                else None,
                turn_cycle_index=(
                    data["turn_cycle_index"] if isinstance(data.get("turn_cycle_index"), int) else 0
                ),
                status=str(data.get("status") or "running"),
                result=str(data.get("result") or ""),
                stream=data.get("stream") if isinstance(data.get("stream"), str) else None,
                is_error=bool(data.get("is_error")),
                chunk_count=int(data.get("chunk_count") or 0),
                content_offset=int(data.get("content_offset") or 0),
                output_size=int(data.get("output_size") or 0),
                truncated=bool(data.get("truncated")),
                agent_visible_truncated=bool(data.get("agent_visible_truncated")),
                transport_truncated=bool(data.get("transport_truncated")),
                has_full_output=bool(data.get("has_full_output")),
                recovery_call_id=data.get("recovery_call_id")
                if isinstance(data.get("recovery_call_id"), str)
                else None,
                tool_output_artifact_id=data.get("tool_output_artifact_id")
                if isinstance(data.get("tool_output_artifact_id"), str)
                else None,
                anchors_available=bool(data.get("anchors_available")),
                anchor_count=int(data.get("anchor_count") or 0),
                progress_phase=data.get("progress_phase")
                if isinstance(data.get("progress_phase"), str)
                else None,
                progress_input_chars=int(data["progress_input_chars"])
                if isinstance(data.get("progress_input_chars"), int)
                else None,
                progress_input_lines=int(data["progress_input_lines"])
                if isinstance(data.get("progress_input_lines"), int)
                else None,
                progress_complete=bool(data.get("progress_complete"))
                if isinstance(data.get("progress_complete"), bool)
                else None,
                managed_conversation=data.get("managed_conversation")
                if isinstance(data.get("managed_conversation"), dict)
                else None,
                arguments=data.get("arguments")
                if isinstance(data.get("arguments"), dict)
                else None,
                updated_at=updated_at,
            )
        except Exception:
            return None


def _automatic_continuation_queue_fields(follow_up: Any) -> dict[str, str]:
    """Return presentation metadata for an internal automatic continuation."""

    if isinstance(follow_up, ContinuationFollowUp):
        return {
            "kind": "automatic_continuation",
            "continuation_reason": follow_up.reason,
        }
    if isinstance(follow_up, dict) and follow_up.get("origin_kind") == "continuation":
        return {
            "kind": "automatic_continuation",
            "continuation_reason": str(follow_up.get("reason") or ""),
        }
    return {}


@dataclass(slots=True)
class _QueuedMessage:
    """A message queued behind an active turn."""

    content: str
    user_email: str
    intention_eligible: bool = True
    user_message_metadata: dict[str, Any] | None = None
    contextual_messages: list[dict[str, Any]] | None = None
    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:12]}")
    queue_id: str = field(default_factory=lambda: f"qmsg_{uuid.uuid4().hex}")
    session_id: str | None = None
    client_message_id: str | None = None
    attachments: list[dict[str, Any]] | None = None
    attachment_notice: str | None = None
    attachment_context: str | None = None
    system_initiated: bool = False
    channel_deliverable: bool = False
    delivery_id: str | None = None
    delivery_fallback_text: str | None = None
    follow_up: FollowUpMetadata | None = None
    outbound_attachments: list[dict[str, Any]] | None = None
    turn_observers: tuple[TurnObserver, ...] = ()
    one_shot_chat_mode: ChatMode | None = None
    channel_account_id: str | None = None
    channel_delivery: ChannelDeliveryDescriptor | None = None
    is_retry: bool = False
    retry_source_turn_id: str | None = None
    retry_reason: RetryReason | None = None
    retry_attempt: int = 1
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def snapshot(self, position: int) -> dict[str, Any]:
        snapshot = {
            "queue_id": self.queue_id,
            "turn_id": self.turn_id,
            "client_message_id": self.client_message_id,
            "content": self.content,
            "attachments": strip_attachment_payload_bytes(self.attachments or []),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "position": position,
        }
        snapshot.update(_automatic_continuation_queue_fields(self.follow_up))
        return snapshot


@dataclass(slots=True)
class _TurnControl:
    """Mutable state for one active conversation turn."""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    settled: bool = False
    turn_id: str | None = None
    chat_mode: ChatMode = "default"
    chat_mode_source: str = "system_default"
    turn_observers: list[TurnObserver] = field(default_factory=list)
    absorbed_follow_up_ids: set[str] = field(default_factory=set)
    absorbed_outbound_attachments: list[dict[str, Any]] = field(default_factory=list)
    active_delivery_id: str | None = None
    absorbed_channel_deliverable: bool = False
    absorbed_delivery_id: str | None = None
    absorbed_delivery_fallback_text: str | None = None
    suppressed_channel_delivery_ids: list[str] = field(default_factory=list)
    channel_delivery: ChannelDeliveryDescriptor | None = None


def _user_message_event_id(
    *,
    conversation_id: str,
    session_id: str | None,
    turn_id: str | None,
    client_message_id: str | None,
    queue_id: str | None,
    content: str,
) -> str:
    """Build a stable client-visible id for live user message events."""

    if client_message_id:
        return f"client:{client_message_id}"
    if queue_id:
        return f"queue:{queue_id}"
    digest = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "content": content,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"user:{session_id or conversation_id}:{turn_id or digest}:{digest}"


def _user_message_event_payload(
    *,
    conversation_id: str,
    session_id: str | None,
    content: str,
    attachments: list[dict[str, Any]],
    turn_id: str | None = None,
    chat_mode: str | None = None,
    chat_mode_source: str | None = None,
    client_message_id: str | None = None,
    queue_id: str | None = None,
) -> dict[str, Any]:
    event_id = _user_message_event_id(
        conversation_id=conversation_id,
        session_id=session_id,
        turn_id=turn_id,
        client_message_id=client_message_id,
        queue_id=queue_id,
        content=content,
    )
    return {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "content": content,
        "attachments": attachments,
        "turn_id": turn_id,
        "chat_mode": chat_mode,
        "chat_mode_source": chat_mode_source,
        "client_message_id": client_message_id,
        "queue_id": queue_id,
        "event_id": event_id,
        "message_id": event_id,
    }


_AUTOMATIC_CONTINUATION_REASONS = {
    LLM_CYCLE_CEILING_CONTINUATION_REASON,
    TOOL_CALL_CEILING_CONTINUATION_REASON,
    STEP_TIMEOUT_CONTINUATION_REASON,
}


def _automatic_continuation_metadata(step_output: Any | None) -> dict[str, Any] | None:
    metadata = getattr(step_output, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("continuation_reason") not in _AUTOMATIC_CONTINUATION_REASONS:
        return None
    return metadata


def _automatic_continuation_exhausted_subject(reason: str) -> str:
    if reason == TOOL_CALL_CEILING_CONTINUATION_REASON:
        return "tool-call ceilings"
    if reason == LLM_CYCLE_CEILING_CONTINUATION_REASON:
        return "LLM cycle ceilings"
    if reason == STEP_TIMEOUT_CONTINUATION_REASON:
        return "step timeouts"
    return "turn boundaries"


def _turn_error_from_step_output(step_output: Any | None) -> TurnError | None:
    error_text = str(getattr(step_output, "error", "") or "").strip()
    if not error_text:
        return None
    summary = str(getattr(step_output, "summary", "") or "").strip()
    message = summary or error_text
    lowered = error_text.lower()
    transient = (
        any(
            marker in lowered
            for marker in (
                "circuit breaker",
                "dns",
                "connection reset",
                "temporar",
                "timeout",
                "timed out",
            )
        )
        or re.search(
            r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:_code)?)\s*[:=/ -]\s*5\d\d\b",
            lowered,
        )
        is not None
    )
    return TurnError(
        code="step_failed",
        message=message[:500],
        recoverable=True,
        detail={"error_detail": error_text[:2000]},
        transient=transient,
    )


class SessionCreationFailedError(Exception):
    """Raised when session creation fails during runtime loading."""


# ---------------------------------------------------------------------------
# TurnObserver protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TurnObserver(Protocol):
    """Optional streaming observer for real-time turn delivery.

    Transport layers (WebSocket, SSE, etc.) implement this protocol
    to receive streaming updates during a turn. The TurnScheduler
    calls these methods as the turn progresses.

    All methods are fire-and-forget — errors are logged but never
    propagate to the turn execution.
    """

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        turn_id: str | None,
        assistant_phase_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_tool_progress(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        progress: dict[str, Any],
        turn_id: str | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None = None,
        file_diffs: list[dict[str, Any]] | None = None,
        turn_id: str | None = None,
        presentation: dict[str, Any] | None = None,
        assistant_phase_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_tool_output_chunk(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        delta: str,
        stream: str | None,
        turn_id: str | None = None,
        chunk_index: int | None = None,
        content_offset: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        source: str | None = None,
        provider_block_index: int | None = None,
        turn_cycle_index: int | None = None,
    ) -> None: ...

    async def on_context_usage(
        self,
        conversation_id: str,
        session_id: str,
        usage: dict[str, Any],
        turn_id: str | None = None,
    ) -> None: ...

    async def on_turn_complete(self, result: TurnResult) -> None: ...

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None: ...

    async def on_system_message(
        self,
        conversation_id: str,
        text: str,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
        retry_reason: str | None = None,
        retry_source_turn_id: str | None = None,
        attempt: int | None = None,
    ) -> None: ...

    async def on_queued(self, conversation_id: str, queued_count: int) -> None: ...

    async def on_queued_messages(
        self, conversation_id: str, messages: list[dict[str, Any]]
    ) -> None: ...


# ---------------------------------------------------------------------------
# TurnScheduler
# ---------------------------------------------------------------------------


class TurnScheduler:
    """Transport-agnostic turn orchestration.

    Owns the full lifecycle of a chat turn. Transport layers call
    ``submit_turn()`` and optionally register ``TurnObserver`` instances
    for real-time streaming. Lifecycle events are published to the
    EventBus for non-streaming consumers.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        workflow_engine: Any,
        decision_engine: Any,
        task_queue: Any,
        session_manager: Any,
        session_cache: Any,
        redis_service: Any | None = None,
        compaction_strategy: Any,
        agent_loop: Any,
        pause_waiter: Any,
        notification_service: Any,
        providers: Any,
        artifact_store: Any,
        workflow_registry: Any,
        event_bus: EventBus,
        tool_output_spool: ToolOutputSpool | None = None,
        controller_runtime: ControllerRuntime | None = None,
        runtime_mode: str = "simple",
    ) -> None:
        self._session_factory = session_factory
        self._workflow_engine = workflow_engine
        self._decision_engine = decision_engine
        self._task_queue = task_queue
        self._session_manager = session_manager
        self._session_cache = session_cache
        self._redis_service = redis_service
        self._compaction_strategy = compaction_strategy
        self._agent_loop = agent_loop
        self._pause_waiter = pause_waiter
        self._notification_service = notification_service
        self._providers = providers
        self._artifact_store = artifact_store
        self._workflow_registry = workflow_registry
        self._event_bus = event_bus
        self._channel_delivery: Any | None = None
        self._tool_output_spool = tool_output_spool or ToolOutputSpool()
        self._controller_runtime = controller_runtime
        self._direct_turn_store: DirectTurnStore | None = None
        self._direct_turn_runtime: DurableDirectTurnRuntime | None = None
        self._durable_turn_observers: dict[str, tuple[TurnObserver, ...]] = {}
        self._durable_queue_cache: dict[str, list[dict[str, Any]]] = {}
        self._durable_fences: dict[str, DirectTurnExecutionFence] = {}
        self._durable_request_by_conversation: dict[str, str] = {}
        self._interrupted_durable_requests: set[str] = set()
        self._relay_generation_contexts: dict[str, RelayGenerationContext] = {}
        if controller_runtime is not None and callable(session_factory):
            self._direct_turn_store = DirectTurnStore(session_factory)
            self._direct_turn_runtime = DurableDirectTurnRuntime(
                store=self._direct_turn_store,
                lease_store=DatabaseLeaseStore(session_factory),
                controller_id=controller_runtime.controller_id,
                incarnation_id=controller_runtime.incarnation_id,
                artifact_store=artifact_store,
                execute_claimed_turn=self._execute_claimed_direct_turn,
                reconcile_canonical_append=self._reconcile_direct_turn_append,
                can_claim_turn=self._can_claim_direct_turn,
                on_permanent_failure=self._handle_permanent_direct_turn_failure,
                on_fenced_permanent_failure=self._handle_fenced_permanent_direct_turn_failure,
                on_state_change=self._publish_durable_turn_change,
                simple_mode=runtime_mode == "simple",
            )

        # Per-conversation turn serialization
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._turn_controls: dict[str, _TurnControl] = {}
        self._turn_sessions: dict[str, str] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._retry_admission_locks: dict[str, asyncio.Lock] = {}
        self._queued_messages: dict[str, deque[_QueuedMessage]] = defaultdict(deque)
        self._boundary_input_events: dict[str, asyncio.Event] = {}
        self._boundary_input_waiters: dict[str, int] = defaultdict(int)
        self._boundary_action_generations: dict[str, int] = defaultdict(int)
        self._turn_scope_change_events: dict[str, asyncio.Event] = {}
        self._turn_scope_change_generations: dict[str, int] = defaultdict(int)
        self._turn_scope_change_waiters: dict[str, int] = defaultdict(int)
        self._accepting_turns = True
        self._admission_drain_lock = asyncio.Lock()
        self._queued_relaunches: dict[str, str] = {}
        self._cancelled_queued_relaunches: set[str] = set()
        self._escalation_notice_pause_ids: dict[str, str] = {}
        self._pending_follow_ups: set[tuple[str, str]] = set()
        self._handled_follow_ups: dict[tuple[str, str], float] = {}
        self._follow_up_lease_owner = f"follow-up-worker:{uuid.uuid4().hex}"
        self._follow_up_lease_seconds = FOLLOW_UP_INTENT_LEASE_SECONDS
        self._pending_follow_up_finalizations: set[tuple[str, str]] = set()
        self._pending_follow_up_transitions: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._follow_up_recovery_lock = asyncio.Lock()
        self._follow_up_recovery_task: asyncio.Task[None] | None = None
        self._follow_up_recovery_stop = asyncio.Event()
        self._assistant_phase_by_turn: dict[tuple[str, str], int] = {}
        self._assistant_phase_tool_keys: set[tuple[str, str, str]] = set()
        self._assistant_phase_by_tool: dict[tuple[str, str, str], int] = {}
        self._turn_cycle_by_turn: dict[tuple[str, str], int] = {}
        self._turn_cycle_by_tool: dict[tuple[str, str, str], int] = {}
        self._turn_tool_call_ledgers: OrderedDict[tuple[str, str], SameTurnToolCallLedger] = (
            OrderedDict()
        )
        self._active_streams: dict[str, ActiveStreamState] = {}
        self._active_streams_lock = asyncio.Lock()
        self._published_title_updates: dict[str, str] = {}
        self._active_tool_outputs: dict[tuple[str, str, str], ActiveToolOutputSnapshot] = {}
        self._active_tool_outputs_lock = asyncio.Lock()
        self._active_tool_output_l2: dict[str, dict[str, Any]] = {}
        self._turn_waiters: dict[str, list[asyncio.Future[TurnResult | TurnError]]] = defaultdict(
            list
        )

        # Per-user concurrent turn limit
        self._user_turn_counts: dict[str, int] = defaultdict(int)

        # Conversation-scoped observers (multiple allowed — e.g. multiple browser tabs)
        self._observers: dict[str, list[TurnObserver]] = defaultdict(list)
        self._global_observers: list[TurnObserver] = []
        self._observer_failures: dict[tuple[str, int], int] = defaultdict(int)
        self._disabled_observers: set[tuple[str, int]] = set()

        # Per-conversation session creation locks (bootstrap + compaction recovery)
        self._deferred_creation_locks: dict[str, asyncio.Lock] = {}
        self._idle_checkpoint_locks: dict[str, asyncio.Lock] = {}

        # Register for follow-up turn events
        event_bus.subscribe(EventType.FOLLOW_UP_TURN_REQUESTED, self._handle_follow_up_event)
        event_bus.subscribe(EventType.CONVERSATION_UPDATED, self._handle_conversation_updated)
        event_bus.subscribe(
            EventType.CLUSTER_SCOPE_INVALIDATED,
            self._handle_cluster_scope_invalidated,
        )
        logger.info("turn_scheduler: registered on EventBus")
        logger.info("turn_scheduler: follow-up dedupe backed by durable store when available")

    def _turn_lock(self, conversation_id: str) -> asyncio.Lock:
        """Return the per-conversation lock protecting active-turn ownership."""

        lock = self._turn_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[conversation_id] = lock
        return lock

    def turn_admission_lock(self, conversation_id: str) -> asyncio.Lock:
        """Expose the conversation lock for controller-side admission-safe mutations."""

        return self._turn_lock(conversation_id)

    def retry_admission_lock(self, conversation_id: str) -> asyncio.Lock:
        """Serialize retry eligibility checks with retry turn admission."""

        lock = self._retry_admission_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._retry_admission_locks[conversation_id] = lock
        return lock

    def _tool_call_ledger_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        source_turn_id: str | None,
    ) -> SameTurnToolCallLedger:
        """Create a bounded turn ledger, seeded from a retry/continuation source."""

        ledger = SameTurnToolCallLedger()
        if source_turn_id:
            source_key = (conversation_id, source_turn_id)
            source = self._turn_tool_call_ledgers.get(source_key)
            if source is not None:
                ledger.seed_from(source)
                self._turn_tool_call_ledgers.move_to_end(source_key)
        key = (conversation_id, turn_id)
        self._turn_tool_call_ledgers[key] = ledger
        self._turn_tool_call_ledgers.move_to_end(key)
        while len(self._turn_tool_call_ledgers) > MAX_TURN_TOOL_CALL_LEDGERS:
            self._turn_tool_call_ledgers.popitem(last=False)
        return ledger

    async def _prepare_tool_call_ledger_for_turn(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        turn_id: str,
        source_turn_id: str | None,
    ) -> SameTurnToolCallLedger:
        """Return a ledger seeded from memory or reconstructed from Intaris."""

        source_cached = bool(
            source_turn_id and (conversation_id, source_turn_id) in self._turn_tool_call_ledgers
        )
        ledger = self._tool_call_ledger_for_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_turn_id=source_turn_id,
        )
        if source_turn_id and not source_cached:
            await self._reconstruct_turn_tool_call_ledger(
                ledger,
                conversation_id=conversation_id,
                current_session=session,
                source_turn_id=source_turn_id,
            )
        return ledger

    async def _reconstruct_turn_tool_call_ledger(
        self,
        ledger: SameTurnToolCallLedger,
        *,
        conversation_id: str,
        current_session: SessionModel,
        source_turn_id: str,
    ) -> None:
        """Rebuild successful source-turn calls from Intaris event streams.

        This slow path runs only when a retry/continuation source is absent
        from the bounded in-memory LRU (for example after controller restart).
        Intaris remains the durable source of truth; no derived call state is
        written to the Cognis database.
        """

        session_refs: list[str] = []
        seen_refs: set[str] = set()

        def add_ref(value: str | None) -> None:
            if value and value not in seen_refs:
                seen_refs.add(value)
                session_refs.append(value)

        current_session_id = getattr(current_session, "session_id", None)
        add_ref(getattr(current_session, "intaris_session_id", None) or current_session_id)
        try:
            if current_session_id:
                async with self._session_factory() as db_session:
                    rows, _truncated = await queries.get_root_session_chain(
                        db_session,
                        conversation_id,
                        current_session_id,
                    )
                for row in reversed(rows):
                    add_ref(row.intaris_session_id or row.session_id)
        except Exception:
            logger.warning(
                "turn_scheduler: failed to list retry-lineage session streams",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "source_turn_id": source_turn_id,
                    }
                },
                exc_info=True,
            )

        calls_by_turn: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        successful_call_ids_by_turn: dict[str, set[str]] = defaultdict(set)
        parent_turn_by_turn: dict[str, str] = {}
        guardrails = getattr(self._providers, "guardrails", None)
        read_events = getattr(guardrails, "read_events", None)
        if not callable(read_events):
            return
        for intaris_session_id in session_refs:
            after_seq = 0
            try:
                while True:
                    result = await read_events(
                        session_id=intaris_session_id,
                        after_seq=after_seq,
                        limit=500,
                        types=["tool_call", "tool_result", "system_message"],
                        allow_missing_stream=True,
                    )
                    for event in list(getattr(result, "events", []) or []):
                        event_type = (
                            event.get("type")
                            if isinstance(event, dict)
                            else getattr(event, "type", None)
                        )
                        data = (
                            event.get("data", {})
                            if isinstance(event, dict)
                            else getattr(event, "data", {})
                        )
                        if not isinstance(data, dict):
                            continue
                        event_turn_id = data.get("turn_id")
                        if not isinstance(event_turn_id, str) or not event_turn_id:
                            continue
                        retry_source = data.get("retry_source_turn_id")
                        if isinstance(retry_source, str) and retry_source:
                            parent_turn_by_turn[event_turn_id] = retry_source
                        elif (
                            event_type == "system_message"
                            and data.get("event") == "turn_initiated"
                            and data.get("origin_kind") == FollowUpOriginKind.CONTINUATION.value
                            and isinstance(data.get("source_id"), str)
                        ):
                            parent_turn_by_turn[event_turn_id] = data["source_id"]
                        call_id = data.get("call_id")
                        if not isinstance(call_id, str) or not call_id:
                            continue
                        if event_type == "tool_call":
                            name = data.get("canonical_name") or data.get("name")
                            fingerprint = data.get("duplicate_guard_fingerprint")
                            if not isinstance(fingerprint, str) or not fingerprint:
                                arguments = data.get("arguments")
                                if isinstance(arguments, str):
                                    try:
                                        arguments = json.loads(arguments)
                                    except json.JSONDecodeError:
                                        arguments = None
                                if isinstance(name, str) and isinstance(arguments, dict):
                                    fingerprint = tool_call_argument_fingerprint(name, arguments)
                            if (
                                isinstance(name, str)
                                and isinstance(fingerprint, str)
                                and fingerprint
                            ):
                                calls_by_turn[event_turn_id][call_id] = (
                                    name,
                                    fingerprint,
                                )
                        elif event_type == "tool_result" and (
                            data.get("is_error") is False or data.get("ambiguity") is not None
                        ):
                            successful_call_ids_by_turn[event_turn_id].add(call_id)
                    last_seq = int(getattr(result, "last_seq", 0) or 0)
                    if not getattr(result, "has_more", False) or last_seq <= after_seq:
                        break
                    after_seq = last_seq
            except Exception:
                logger.warning(
                    "turn_scheduler: failed to reconstruct retry-lineage tool calls",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "source_turn_id": source_turn_id,
                            "intaris_session_id": intaris_session_id,
                        }
                    },
                    exc_info=True,
                )

        reconstructed = 0
        lineage_turn_ids: list[str] = []
        lineage_seen: set[str] = set()
        lineage_turn_id: str | None = source_turn_id
        while lineage_turn_id and lineage_turn_id not in lineage_seen:
            lineage_seen.add(lineage_turn_id)
            lineage_turn_ids.append(lineage_turn_id)
            lineage_turn_id = parent_turn_by_turn.get(lineage_turn_id)
        for ancestor_turn_id in lineage_turn_ids:
            calls_by_id = calls_by_turn.get(ancestor_turn_id, {})
            for call_id in successful_call_ids_by_turn.get(ancestor_turn_id, set()):
                call = calls_by_id.get(call_id)
                if call is None:
                    continue
                ledger.record_fingerprint(call[0], call[1])
                reconstructed += 1
        if reconstructed:
            logger.info(
                "turn_scheduler: reconstructed retry-lineage tool-call ledger",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "source_turn_id": source_turn_id,
                        "tool_call_count": reconstructed,
                    }
                },
            )

    # ------------------------------------------------------------------
    # Observer management
    # ------------------------------------------------------------------

    def add_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Register a conversation-scoped streaming observer."""
        if not any(item is observer for item in self._observers[conversation_id]):
            self._observers[conversation_id].append(observer)

    def add_global_observer(self, observer: TurnObserver) -> None:
        """Register one configuration-owned observer for every conversation."""
        if any(item is observer for item in self._global_observers):
            return
        if len(self._global_observers) >= 8:
            raise RuntimeError("maximum global observer count reached")
        self._global_observers.append(observer)

    def remove_global_observer(self, observer: TurnObserver) -> None:
        """Remove one global observer by identity."""
        self._global_observers = [item for item in self._global_observers if item is not observer]
        for key in [key for key in self._observer_failures if key[1] == id(observer)]:
            self._observer_failures.pop(key, None)
            self._disabled_observers.discard(key)

    def remove_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Remove a streaming observer for a conversation."""
        observers = self._observers.get(conversation_id)
        if observers:
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            self._observer_failures.pop((conversation_id, id(observer)), None)
            self._disabled_observers.discard((conversation_id, id(observer)))
            if not observers:
                del self._observers[conversation_id]

    def remove_all_observers(self, observer: TurnObserver) -> None:
        """Remove an observer from all conversations (e.g. on disconnect)."""
        empty_keys: list[str] = []
        for cid, observers in self._observers.items():
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            self._observer_failures.pop((cid, id(observer)), None)
            self._disabled_observers.discard((cid, id(observer)))
            if not observers:
                empty_keys.append(cid)
        for cid in empty_keys:
            del self._observers[cid]

    async def active_stream_snapshots(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return volatile in-flight assistant stream snapshots for a conversation."""

        async with self._active_streams_lock:
            snapshot = self._active_streams.get(conversation_id)
            if snapshot is None or not snapshot.content:
                return []
            return [snapshot.snapshot()]

    async def active_tool_output_snapshots(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return volatile bounded streamed tool-output snapshots."""

        await self._load_active_tool_output_l2(conversation_id)
        control = self._turn_controls.get(conversation_id)
        active_turn_id = (
            control.turn_id
            if control is not None
            and not control.settled
            and self.has_running_turn(conversation_id)
            else None
        )
        should_persist = False
        async with self._active_tool_outputs_lock:
            if active_turn_id is None:
                stale_output_keys = [
                    key for key in self._active_tool_outputs if key[0] == conversation_id
                ]
                for key in stale_output_keys:
                    self._active_tool_outputs.pop(key, None)
                should_persist = bool(stale_output_keys)
                snapshots: list[dict[str, Any]] = []
            else:
                self._prune_active_tool_outputs_locked()
                stale_keys: list[tuple[str, str, str]] = []
                snapshots = []
                for key, snapshot in self._active_tool_outputs.items():
                    cid, _, _ = key
                    if cid != conversation_id:
                        continue
                    if snapshot.turn_id != active_turn_id:
                        stale_keys.append(key)
                        continue
                    has_visible_progress = bool(snapshot.result or snapshot.progress_phase)
                    if (
                        snapshot.status == "running"
                        and has_visible_progress
                        and not snapshot.expired()
                    ):
                        snapshots.append(snapshot.snapshot())
                for key in stale_keys:
                    self._active_tool_outputs.pop(key, None)
                should_persist = bool(stale_keys)
        if should_persist:
            await self._persist_active_tool_output_l2(conversation_id)
        return snapshots

    def _active_tool_output_cache_key(self, conversation_id: str) -> str:
        return f"active_tool_outputs:{conversation_id}"

    async def _load_active_tool_output_l2(self, conversation_id: str) -> None:
        cache = self._redis_service
        if cache is None:
            return
        try:
            raw = await cache.get(self._active_tool_output_cache_key(conversation_id))
        except Exception:
            logger.debug("active tool output L2 read failed", exc_info=True)
            return
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            return
        if not isinstance(payload, list):
            return
        async with self._active_tool_outputs_lock:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                snapshot = ActiveToolOutputSnapshot.from_snapshot(item)
                if snapshot is None or snapshot.expired():
                    continue
                self._active_tool_outputs[
                    (snapshot.conversation_id, snapshot.session_id, snapshot.call_id)
                ] = snapshot

    async def _persist_active_tool_output_l2(self, conversation_id: str) -> None:
        cache = self._redis_service
        if cache is None:
            return
        async with self._active_tool_outputs_lock:
            self._prune_active_tool_outputs_locked()
            payload = [
                snapshot.snapshot()
                for (cid, _, _), snapshot in self._active_tool_outputs.items()
                if (
                    cid == conversation_id
                    and snapshot.status == "running"
                    and (snapshot.result or snapshot.progress_phase)
                    and not snapshot.expired()
                )
            ]
        try:
            key = self._active_tool_output_cache_key(conversation_id)
            if payload:
                await cache.setex(
                    key, _ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS, json.dumps(payload)
                )
            else:
                await cache.delete(key)
        except Exception:
            logger.debug("active tool output L2 write failed", exc_info=True)

    def _prune_active_tool_outputs_locked(self) -> None:
        expired = [key for key, snapshot in self._active_tool_outputs.items() if snapshot.expired()]
        for key in expired:
            self._active_tool_outputs.pop(key, None)

    async def _append_active_tool_output_chunk(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        delta: str,
        stream: str | None,
        turn_cycle_index: int | None = None,
    ) -> tuple[int, int]:
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id, turn_id, call_id, turn_cycle_index
            )
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                    assistant_phase_index=self._assistant_phase_for_tool(
                        conversation_id, turn_id, call_id
                    ),
                    turn_cycle_index=effective_turn_cycle_index,
                )
                self._active_tool_outputs[key] = snapshot
            snapshot.turn_cycle_index = effective_turn_cycle_index
            snapshot.assistant_phase_index = self._assistant_phase_for_tool(
                conversation_id, turn_id, call_id
            )
            index = snapshot.chunk_count
            offset = snapshot.content_offset
            next_result = snapshot.result + delta
            full_output_size = snapshot.output_size + len(delta)
            next_offset = snapshot.content_offset + _utf16_code_units(delta)
            preview = build_transport_tool_output_preview(
                next_result,
                _MAX_ACTIVE_TOOL_OUTPUT_CHARS,
                metadata={"output_size": full_output_size},
            )
            snapshot.result = preview.result
            snapshot.truncated = preview.truncated
            snapshot.transport_truncated = snapshot.truncated
            snapshot.stream = stream
            snapshot.is_error = snapshot.is_error or stream == "stderr"
            snapshot.chunk_count += 1
            snapshot.content_offset = next_offset
            snapshot.output_size = full_output_size
            snapshot.updated_at = _utcnow()
        self._tool_output_spool.append(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            turn_id=turn_id,
            text=delta,
            stream=stream,
        )
        await self._persist_active_tool_output_l2(conversation_id)
        return index, offset

    async def _record_active_tool_arguments(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        arguments: dict[str, Any] | None,
        turn_cycle_index: int | None = None,
    ) -> None:
        """Record parent-safe tool arguments on the active-tool snapshot.

        Called from ``on_tool_call`` so the runtime overlay tool item can render
        its per-tool subtitle/body immediately, even before the canonical
        ``tool_call`` event is persisted. Delegate arguments are already
        redacted upstream. This does not, by itself, make the snapshot visible
        in the overlay (visibility still requires result or progress); it seeds
        the arguments so they are present once the tool emits progress/output.
        """
        if not isinstance(arguments, dict):
            return
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id, turn_id, call_id, turn_cycle_index
            )
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                    assistant_phase_index=self._assistant_phase_for_tool(
                        conversation_id, turn_id, call_id
                    ),
                    turn_cycle_index=effective_turn_cycle_index,
                )
                self._active_tool_outputs[key] = snapshot
            snapshot.arguments = dict(arguments)
            snapshot.turn_cycle_index = effective_turn_cycle_index
            snapshot.updated_at = _utcnow()
        await self._persist_active_tool_output_l2(conversation_id)

    async def _update_active_tool_progress(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        progress: dict[str, Any],
        turn_cycle_index: int | None = None,
    ) -> None:
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id, turn_id, call_id, turn_cycle_index
            )
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                    assistant_phase_index=self._assistant_phase_for_tool(
                        conversation_id, turn_id, call_id
                    ),
                    turn_cycle_index=effective_turn_cycle_index,
                )
                self._active_tool_outputs[key] = snapshot
            snapshot.tool_name = tool_name
            snapshot.turn_id = turn_id
            snapshot.assistant_phase_index = self._assistant_phase_for_tool(
                conversation_id, turn_id, call_id
            )
            snapshot.turn_cycle_index = effective_turn_cycle_index
            snapshot.status = "running"
            phase = progress.get("phase")
            snapshot.progress_phase = phase if isinstance(phase, str) else None
            input_chars = progress.get("input_chars")
            snapshot.progress_input_chars = input_chars if isinstance(input_chars, int) else None
            input_lines = progress.get("input_lines")
            snapshot.progress_input_lines = input_lines if isinstance(input_lines, int) else None
            complete = progress.get("complete")
            snapshot.progress_complete = complete if isinstance(complete, bool) else None
            managed_conversation = progress.get("managed_conversation")
            snapshot.managed_conversation = (
                dict(managed_conversation)
                if tool_name.startswith("agent_conversation_")
                and isinstance(managed_conversation, dict)
                else None
            )
            snapshot.updated_at = _utcnow()
        await self._persist_active_tool_output_l2(conversation_id)

    async def _finalize_active_tool_output(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        result: str,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
        turn_cycle_index: int | None = None,
    ) -> None:
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id, turn_id, call_id, turn_cycle_index
            )
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                    assistant_phase_index=self._assistant_phase_for_tool(
                        conversation_id, turn_id, call_id
                    ),
                    turn_cycle_index=effective_turn_cycle_index,
                )
                self._active_tool_outputs[key] = snapshot
            meta = metadata or {}
            snapshot.assistant_phase_index = self._assistant_phase_for_tool(
                conversation_id, turn_id, call_id
            )
            snapshot.turn_cycle_index = effective_turn_cycle_index
            snapshot.status = "failed" if is_error else "completed"
            if not (meta.get("transport_truncated") and len(snapshot.result) > len(result)):
                snapshot.result = result
            snapshot.is_error = is_error
            snapshot.output_size = int(meta.get("output_size") or len(snapshot.result))
            snapshot.truncated = bool(meta.get("truncated"))
            snapshot.agent_visible_truncated = bool(meta.get("agent_visible_truncated"))
            snapshot.transport_truncated = bool(meta.get("transport_truncated"))
            snapshot.has_full_output = bool(meta.get("has_full_output"))
            snapshot.recovery_call_id = (
                meta.get("recovery_call_id")
                if isinstance(meta.get("recovery_call_id"), str)
                else None
            )
            snapshot.tool_output_artifact_id = (
                meta.get("tool_output_artifact_id")
                if isinstance(meta.get("tool_output_artifact_id"), str)
                else None
            )
            snapshot.anchors_available = bool(meta.get("anchors_available"))
            snapshot.anchor_count = int(meta.get("anchor_count") or 0)
            snapshot.updated_at = _utcnow()
            self._active_tool_outputs.pop(key, None)
        self._tool_output_spool.mark_complete(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            turn_id=turn_id,
            status="failed" if is_error else "completed",
        )
        await self._persist_active_tool_output_l2(conversation_id)

    def read_live_tool_output_page(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        offset: int = 0,
        limit: int = 200,
        latest: bool = False,
    ) -> ToolOutputSpoolPage | None:
        return self._tool_output_spool.page(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            offset=offset,
            limit=limit,
            latest=latest,
        )

    async def _append_active_stream_chunk(
        self,
        *,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        turn_cycle_index: int | None = None,
    ) -> tuple[int, int]:
        """Append a live token to the volatile stream snapshot.

        Returns the zero-based chunk index and starting content offset for the
        chunk. Transport clients use these values to make chunk application
        idempotent across reconnects and foreground reconciliation.
        """

        async with self._active_streams_lock:
            stream = self._active_streams.get(conversation_id)
            current_phase = (
                self._assistant_phase_by_turn.get((conversation_id, turn_id), 0)
                if turn_id is not None
                else 0
            )
            # Cycle fallback is the last recorded turn cycle, never the phase
            # counter (phase != cycle when a cycle issues multiple tool calls).
            effective_turn_cycle_index = self._effective_turn_cycle_for_turn(
                conversation_id,
                turn_id,
                turn_cycle_index,
            )
            if (
                stream is None
                or stream.session_id != session_id
                or stream.message_id != message_id
                or stream.turn_id != turn_id
                # The phase counter advances when a tool call fires mid-turn
                # (_bump_assistant_phase_for_tool). The snapshot's phase is
                # captured at creation and never updated, so a multi-phase turn
                # (assistant → tool → assistant) produces a phase-0 snapshot for
                # the second assistant segment — its id diverges from the
                # live.assistant_complete patch which uses the final counter value,
                # leaving an orphaned streaming spinner in the UI.
                # Reset the snapshot whenever the scheduler phase has advanced
                # past the snapshot's phase so the streaming item always carries
                # the correct phase and its id matches the completion patch.
                or current_phase > stream.assistant_phase_index
            ):
                stream = ActiveStreamState(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    assistant_phase_index=current_phase,
                    turn_cycle_index=effective_turn_cycle_index,
                )
                self._active_streams[conversation_id] = stream
            stream.turn_cycle_index = effective_turn_cycle_index
            index = stream.chunk_count
            offset = _utf16_code_units(stream.content)
            stream.content += delta
            stream.chunk_count += 1
            stream.updated_at = _utcnow()
            return index, offset

    async def _reset_active_stream(self, conversation_id: str) -> None:
        async with self._active_streams_lock:
            self._active_streams.pop(conversation_id, None)

    def _bump_assistant_phase(self, conversation_id: str, turn_id: str | None) -> None:
        if turn_id is None:
            return
        key = (conversation_id, turn_id)
        self._assistant_phase_by_turn[key] = self._assistant_phase_by_turn.get(key, 0) + 1

    def _bump_assistant_phase_for_tool(
        self,
        conversation_id: str,
        turn_id: str | None,
        call_id: str | None,
        tool_name: str | None = None,
    ) -> int | None:
        del tool_name
        if turn_id is None or call_id is None:
            return None
        tool_key = (conversation_id, turn_id, call_id)
        if tool_key in self._assistant_phase_tool_keys:
            return self._assistant_phase_by_tool.get(tool_key)
        phase = self._assistant_phase_by_turn.get((conversation_id, turn_id), 0)
        self._assistant_phase_tool_keys.add(tool_key)
        self._assistant_phase_by_tool[tool_key] = phase
        self._bump_assistant_phase(conversation_id, turn_id)
        return phase

    def _assistant_phase_for_tool(
        self,
        conversation_id: str,
        turn_id: str | None,
        call_id: str | None,
    ) -> int | None:
        if turn_id is None or call_id is None:
            return None
        return self._assistant_phase_by_tool.get((conversation_id, turn_id, call_id))

    def _record_turn_cycle_for_tool(
        self,
        conversation_id: str,
        turn_id: str | None,
        call_id: str | None,
        turn_cycle_index: int | None,
    ) -> None:
        if turn_id is None or call_id is None or turn_cycle_index is None:
            return
        self._turn_cycle_by_tool[(conversation_id, turn_id, call_id)] = turn_cycle_index

    def _record_turn_cycle_for_turn(
        self,
        conversation_id: str,
        turn_id: str | None,
        turn_cycle_index: int | None,
    ) -> None:
        if turn_id is None or turn_cycle_index is None:
            return
        self._turn_cycle_by_turn[(conversation_id, turn_id)] = turn_cycle_index

    def _turn_cycle_for_turn(
        self,
        conversation_id: str,
        turn_id: str | None,
    ) -> int | None:
        if turn_id is None:
            return None
        return self._turn_cycle_by_turn.get((conversation_id, turn_id))

    def _effective_turn_cycle_for_turn(
        self,
        conversation_id: str,
        turn_id: str | None,
        turn_cycle_index: int | None,
    ) -> int:
        if isinstance(turn_cycle_index, int):
            effective_turn_cycle_index = turn_cycle_index
        elif turn_id is not None:
            effective_turn_cycle_index = self._turn_cycle_by_turn.get((conversation_id, turn_id), 0)
        else:
            effective_turn_cycle_index = 0
        self._record_turn_cycle_for_turn(conversation_id, turn_id, effective_turn_cycle_index)
        return effective_turn_cycle_index

    def _turn_cycle_for_tool(
        self,
        conversation_id: str,
        turn_id: str | None,
        call_id: str | None,
    ) -> int | None:
        if turn_id is None or call_id is None:
            return None
        return self._turn_cycle_by_tool.get((conversation_id, turn_id, call_id))

    def _effective_turn_cycle_for_tool(
        self,
        conversation_id: str,
        turn_id: str | None,
        call_id: str | None,
        turn_cycle_index: int | None = None,
        fallback_cycle_index: int | None = None,
    ) -> int:
        if isinstance(turn_cycle_index, int):
            effective_turn_cycle_index = turn_cycle_index
        else:
            mapped = self._turn_cycle_for_tool(conversation_id, turn_id, call_id)
            if isinstance(mapped, int):
                effective_turn_cycle_index = mapped
            elif isinstance(fallback_cycle_index, int):
                effective_turn_cycle_index = fallback_cycle_index
            else:
                effective_turn_cycle_index = self._effective_turn_cycle_for_turn(
                    conversation_id, turn_id, None
                )
        self._record_turn_cycle_for_tool(
            conversation_id, turn_id, call_id, effective_turn_cycle_index
        )
        return effective_turn_cycle_index

    def _clear_assistant_phase(self, conversation_id: str, turn_id: str | None) -> None:
        if turn_id is None:
            return
        self._assistant_phase_by_turn.pop((conversation_id, turn_id), None)
        self._assistant_phase_tool_keys = {
            key for key in self._assistant_phase_tool_keys if key[:2] != (conversation_id, turn_id)
        }
        self._assistant_phase_by_tool = {
            key: phase
            for key, phase in self._assistant_phase_by_tool.items()
            if key[:2] != (conversation_id, turn_id)
        }
        self._turn_cycle_by_turn.pop((conversation_id, turn_id), None)
        self._turn_cycle_by_tool = {
            key: cycle
            for key, cycle in self._turn_cycle_by_tool.items()
            if key[:2] != (conversation_id, turn_id)
        }

    async def _pop_active_stream(
        self,
        *,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
    ) -> ActiveStreamState | None:
        async with self._active_streams_lock:
            stream = self._active_streams.get(conversation_id)
            if (
                stream is None
                or stream.session_id != session_id
                or stream.message_id != message_id
                or stream.turn_id != turn_id
            ):
                return None
            self._active_streams.pop(conversation_id, None)
            return stream

    async def _persist_cancelled_active_stream(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        message_id: str,
        turn_id: str | None,
        user_email: str,
        agent: AgentDefinition,
        chat_mode: str = "default",
        chat_mode_source: str = "system_default",
        finish_reason: str = "user_cancelled",
        clear_on_success: bool = True,
        stream_snapshot: ActiveStreamState | None = None,
    ) -> tuple[str | None, int, int | None]:
        """Persist already streamed assistant text before its local stream is discarded."""

        stream = stream_snapshot
        if stream is None:
            async with self._active_streams_lock:
                stream = self._active_streams.get(conversation_id)
                if stream is not None and (
                    stream.session_id != session.session_id
                    or stream.message_id != message_id
                    or stream.turn_id != turn_id
                ):
                    stream = None
        if stream is None or not stream.content:
            return None, 0, None

        event_data = {
            "content": stream.content,
            "turn_id": turn_id,
            "runtime": assistant_message_runtime_metadata(
                agent,
                self._tool_runtime_info(session.session_id),
            ),
            "partial": True,
            "cancelled": finish_reason == "user_cancelled",
            "finish_reason": finish_reason,
            "assistant_phase_index": stream.assistant_phase_index,
            # Persist chat_mode so the history projector can stamp the
            # plan-mode marker on cancelled assistant messages after refresh.
            "chat_mode": chat_mode,
            "chat_mode_source": chat_mode_source,
        }
        if stream.turn_cycle_index is not None:
            event_data["turn_cycle_index"] = stream.turn_cycle_index
        intaris_session_id = session.intaris_session_id or session.session_id
        digest = hashlib.sha256(stream.content.encode("utf-8")).hexdigest()[:16]
        event_data["message_id"] = (
            turn_id or message_id
            if finish_reason == "user_cancelled"
            else f"{turn_id or message_id}:{finish_reason}"
        )
        event = SessionEvent(type="assistant_message", data=event_data)
        idempotency_key = (
            f"cancelled-active-stream:{intaris_session_id}:"
            f"{turn_id or message_id}:{finish_reason}:{stream.chunk_count}:{digest}"
        )
        try:
            append_result = await self._providers.guardrails.record_events(
                session_id=intaris_session_id,
                events=[event],
                source="cognis",
                idempotency_key=idempotency_key,
                user_email=user_email,
                agent_id=agent.agent_id,
                agent_owner_email=getattr(agent, "owner_email", user_email),
            )
            if not append_result.ok:
                raise RuntimeError("Intaris did not persist cancelled assistant stream")
            await self._session_cache.append_recorded_events(session, [event], append_result)
            if clear_on_success:
                await self._pop_active_stream(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                )
            return stream.content, append_result.last_seq, stream.turn_cycle_index
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist cancelled assistant stream",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "turn_id": turn_id,
                    }
                },
                exc_info=True,
            )
            return None, 0, None

    def _tool_runtime_info(self, session_id: str) -> dict[str, Any]:
        reader = getattr(self._session_cache, "get_tool_runtime_info", None)
        if not callable(reader):
            return {}
        info = reader(session_id)
        return info if isinstance(info, dict) else {}

    async def _persist_follow_up_turn_notice(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        follow_up: FollowUpMetadata,
        turn_id: str,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
    ) -> None:
        """Persist and broadcast the visible notice for a follow-up initiated turn."""

        text = render_follow_up_turn_notice(follow_up)
        notice_id = f"turn-init:{follow_up.follow_up_id}"
        data: dict[str, Any] = {
            "content": text,
            "text": text,
            "message": text,
            "event": "turn_initiated",
            "notice_id": notice_id,
            "kind": "turn_initiated",
            "scope": "turn",
            "turn_id": turn_id,
            "follow_up_id": follow_up.follow_up_id,
            "origin_kind": follow_up.origin_kind.value,
            "status": follow_up.status.value,
        }
        topic_ref = getattr(follow_up, "topic_ref", None)
        if topic_ref:
            data["source_id"] = topic_ref
        source_title = getattr(follow_up, "task_title", None) or getattr(follow_up, "title", None)
        if source_title:
            data["source_title"] = source_title

        try:
            event = SessionEvent(type="system_message", data=data)
            session_id = session.session_id
            intaris_session_id = getattr(session, "intaris_session_id", None) or session_id
            idempotency_key = f"{intaris_session_id}:turn_initiated:{follow_up.follow_up_id}"
            append_result = await self._providers.guardrails.record_events(
                session_id=intaris_session_id,
                events=[event],
                source="cognis",
                idempotency_key=idempotency_key,
                user_email=user_email,
                agent_id=agent.agent_id,
                agent_owner_email=getattr(agent, "owner_email", user_email),
            )
            if not append_result.ok:
                raise RuntimeError("Intaris did not persist follow-up turn notice")
            if append_result.count <= 0:
                return
            await self._session_cache.append_recorded_events(session, [event], append_result)
            await self._notify_observers_system_message(
                conversation_id,
                text,
                notice_id=notice_id,
                kind="turn_initiated",
                scope="turn",
                turn_id=turn_id,
                turn_observers=turn_observers,
            )
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist follow-up turn notice",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": getattr(session, "session_id", None),
                        "turn_id": turn_id,
                        "follow_up_id": follow_up.follow_up_id,
                    }
                },
                exc_info=True,
            )

    async def _persist_retry_turn_notice(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        turn_id: str,
        retry_source_turn_id: str | None,
        retry_reason: RetryReason | str | None,
        retry_attempt: int,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
    ) -> None:
        """Persist and broadcast the visible notice for a user-initiated retry turn."""

        reason = normalize_retry_reason(retry_reason)
        text = retry_notice_text(reason)
        source_turn_id = retry_source_turn_id or turn_id
        attempt = max(1, retry_attempt)
        notice_id = f"retry:{source_turn_id}:{turn_id}:{attempt}"
        data: dict[str, Any] = {
            "content": text,
            "text": text,
            "message": text,
            "event": "system_notice",
            "notice_id": notice_id,
            "kind": "model_recovery",
            "scope": "turn",
            "turn_id": turn_id,
            "retry_source_turn_id": retry_source_turn_id,
            "retry_reason": reason.value,
            "attempt": attempt,
        }
        try:
            event = SessionEvent(type="system_message", data=data)
            session_id = session.session_id
            intaris_session_id = getattr(session, "intaris_session_id", None) or session_id
            idempotency_key = (
                f"{intaris_session_id}:retry_turn:{source_turn_id}:{turn_id}:{attempt}"
            )
            append_result = await self._providers.guardrails.record_events(
                session_id=intaris_session_id,
                events=[event],
                source="cognis",
                idempotency_key=idempotency_key,
                user_email=user_email,
                agent_id=agent.agent_id,
                agent_owner_email=getattr(agent, "owner_email", user_email),
            )
            if not append_result.ok:
                raise RuntimeError("Intaris did not persist retry turn notice")
            if append_result.count > 0:
                await self._session_cache.append_recorded_events(session, [event], append_result)
                await self._notify_observers_system_message(
                    conversation_id,
                    text,
                    notice_id=notice_id,
                    kind="model_recovery",
                    scope="turn",
                    turn_id=turn_id,
                    retry_reason=reason.value,
                    retry_source_turn_id=retry_source_turn_id,
                    attempt=attempt,
                    turn_observers=turn_observers,
                )
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist retry turn notice",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": getattr(session, "session_id", None),
                        "turn_id": turn_id,
                        "retry_source_turn_id": retry_source_turn_id,
                    }
                },
                exc_info=True,
            )

    async def _persist_admitted_user_message(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        content: str,
        intention_eligible: bool = True,
        attachments: list[AttachmentRef],
        turn_id: str,
        client_message_id: str | None,
        chat_mode: ResolvedChatMode,
        cancel_event: asyncio.Event,
        intaris_session_id_override: str | None = None,
        user_message_metadata: dict[str, Any] | None = None,
        contextual_messages: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, int | None]:
        """Persist scheduler-owned direct-turn admission before runtime resolution."""
        user_message_metadata = user_message_metadata or message_metadata()

        guardrails = getattr(self._providers, "guardrails", None)
        record_events = getattr(guardrails, "record_events", None)
        if not callable(record_events):
            return False, None
        source = "user_input"
        hash_payload = {
            "role": "user",
            "content": content,
            "source": source,
            "message_metadata": user_message_metadata,
            "intention_eligible": intention_eligible,
        }
        event_data: dict[str, Any] = {
            "role": "user",
            "content": content,
            "message_metadata": user_message_metadata,
            "content_type": "text",
            "source": source,
            "intention_eligible": intention_eligible,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "chat_mode": chat_mode.mode,
            "chat_mode_source": chat_mode.source,
            "attachments": attachment_refs_to_dicts(attachments, include_url=False),
        }
        if contextual_messages:
            event_data["context_messages"] = contextual_messages
            hash_payload["context_messages"] = contextual_messages
        event_data["hash"] = hashlib.sha256(
            json.dumps(
                hash_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event = SessionEvent(
            type="user_message",
            data=event_data,
        )
        intaris_session_id = (
            intaris_session_id_override
            or getattr(session, "intaris_session_id", None)
            or session.session_id
        )
        deadline = monotonic() + 60.0
        while True:
            try:
                append_result = await record_events(
                    session_id=intaris_session_id,
                    events=[event],
                    source="cognis",
                    idempotency_key=f"{intaris_session_id}:admitted_user_message:{turn_id}",
                    user_email=user_email,
                    agent_id=agent.agent_id,
                    agent_owner_email=getattr(agent, "owner_email", user_email),
                )
                break
            except Exception as exc:
                if not is_retryable_http_error(exc) or monotonic() >= deadline:
                    raise
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=5.0)
                except TimeoutError:
                    continue
                raise asyncio.CancelledError from exc
        if not append_result.ok:
            raise RuntimeError("Intaris did not persist admitted user message")
        current_intaris_session_id = (
            getattr(session, "intaris_session_id", None) or session.session_id
        )
        if (
            append_result.count > 0
            and intaris_session_id == current_intaris_session_id
            and hasattr(self._session_cache, "append_recorded_events")
        ):
            await self._session_cache.append_recorded_events(session, [event], append_result)
        return True, append_result.first_seq

    async def _persist_turn_error_event(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        turn_id: str,
        error: TurnError,
        chat_mode: ChatMode,
        chat_mode_source: str,
    ) -> None:
        """Persist one sanitized terminal failure marker for retry discovery."""

        guardrails = getattr(self._providers, "guardrails", None)
        record_events = getattr(guardrails, "record_events", None)
        if not callable(record_events):
            return
        event = SessionEvent(
            # Intaris stores failures as lifecycle data; "error" isn't a
            # canonical event type and is rejected with HTTP 400.
            type="lifecycle",
            data={
                "event": "turn_error",
                "status": "failed",
                "error_id": turn_id,
                "turn_id": turn_id,
                "title": "Turn failed",
                "message": _durable_turn_error_message(error),
                "error_code": error.code,
                "recoverable": error.recoverable,
                "chat_mode": chat_mode,
                "chat_mode_source": chat_mode_source,
            },
        )
        intaris_session_id = getattr(session, "intaris_session_id", None) or session.session_id
        append_result = await record_events(
            session_id=intaris_session_id,
            events=[event],
            source="cognis",
            idempotency_key=f"{intaris_session_id}:turn_error:{turn_id}",
            user_email=user_email,
            agent_id=agent.agent_id,
            agent_owner_email=getattr(agent, "owner_email", user_email),
        )
        if not append_result.ok:
            raise RuntimeError("Intaris did not persist turn failure")
        if append_result.count > 0 and hasattr(self._session_cache, "append_recorded_events"):
            await self._session_cache.append_recorded_events(session, [event], append_result)

    async def _persist_retry_source_consumed(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        retry_source_turn_id: str,
        retry_turn_id: str,
    ) -> None:
        """Mark a failed source turn consumed after its retry succeeds."""

        guardrails = getattr(self._providers, "guardrails", None)
        record_events = getattr(guardrails, "record_events", None)
        if not callable(record_events):
            return
        event = SessionEvent(
            type="lifecycle",
            data={
                "event": "retry_source_consumed",
                "status": "completed",
                "turn_id": retry_source_turn_id,
                "retry_source_turn_id": retry_source_turn_id,
                "retry_turn_id": retry_turn_id,
            },
        )
        intaris_session_id = getattr(session, "intaris_session_id", None) or session.session_id
        append_result = await record_events(
            session_id=intaris_session_id,
            events=[event],
            source="cognis",
            idempotency_key=(
                f"{intaris_session_id}:retry_source_consumed:{retry_source_turn_id}:{retry_turn_id}"
            ),
            user_email=user_email,
            agent_id=agent.agent_id,
            agent_owner_email=getattr(agent, "owner_email", user_email),
        )
        if not append_result.ok:
            raise RuntimeError("Intaris did not persist consumed retry source")
        if append_result.count > 0 and hasattr(self._session_cache, "append_recorded_events"):
            await self._session_cache.append_recorded_events(session, [event], append_result)

    async def _clear_redo_on_accepted_user_turn(
        self,
        conversation_id: str,
        *,
        content: str,
        system_initiated: bool,
    ) -> None:
        if system_initiated or is_system_slash_command_message(content):
            return
        try:
            async with self._session_factory() as db_session:
                await queries.clear_conversation_history_rebase_metadata(
                    db_session, conversation_id
                )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to clear redo metadata for accepted user turn",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )

    async def _mark_managed_conversation_turn_running(
        self,
        *,
        target_conversation_id: str,
        target_session_id: str,
        turn_id: str | None,
    ) -> None:
        """Mirror scheduler-owned launches into the managed-conversation link."""

        if not turn_id or not callable(self._session_factory):
            return
        try:
            async with self._session_factory() as db_session:
                await queries.mark_managed_conversation_turn_running(
                    db_session,
                    target_conversation_id,
                    turn_id=turn_id,
                    target_session_id=target_session_id,
                )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to mark managed conversation turn running",
                extra={
                    "extra_data": {
                        "target_conversation_id": target_conversation_id,
                        "target_session_id": target_session_id,
                        "turn_id": turn_id,
                    }
                },
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_turn(
        self,
        conversation_id: str,
        content: str,
        *,
        user_email: str,
        intention_eligible: bool | None = None,
        user_message_metadata: dict[str, Any] | None = None,
        contextual_messages: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        outbound_attachments: list[dict[str, Any]] | None = None,
        system_initiated: bool = False,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
        client_message_id: str | None = None,
        queued_message_id: str | None = None,
        prepared_attachment_notice: str | None = None,
        prepared_attachment_context: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
        channel_default_agent_profile_id: str | None = None,
        channel_account_id: str | None = None,
        channel_delivery: ChannelDeliveryDescriptor | None = None,
        is_retry: bool = False,
        retry_source_turn_id: str | None = None,
        retry_reason: RetryReason | str | None = None,
        retry_attempt: int = 1,
        turn_id: str | None = None,
        admission_observer: Callable[[str, bool], Awaitable[None]] | None = None,
        admission_transaction_participant: Callable[[Any, Any, bool], Awaitable[None]]
        | None = None,
        allow_queue: bool = True,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        _durable_request_id: str | None = None,
        durable_request_id: str | None = None,
        durable_admission_guard: DirectTurnAdmissionGuard | None = None,
        _durable_lease: Lease | None = None,
        _durable_user_append_session_id: str | None = None,
        _recovery_context: str | None = None,
        _materialized_attachments: list[AttachmentRef] | None = None,
    ) -> TurnError | None:
        """Submit a chat turn for execution.

        Returns a ``TurnError`` immediately if the turn cannot be started
        (authorization failure, session blocked, escalation pending, etc.).
        Returns ``None`` on successful submission — results are delivered
        via ``TurnObserver`` callbacks and EventBus lifecycle events.

        Turns are serialized per conversation. If a turn is already active,
        the message is queued up to ``session.max_queued_messages`` unless
        ``allow_queue`` is false.
        """
        if not self._accepting_turns and queued_message_id is None:
            return TurnError(
                code="controller_draining",
                message="This controller is draining and cannot accept new turns.",
                recoverable=True,
                transient=True,
                turn_id=turn_id,
            )
        admitted_turn_id = turn_id or self.new_turn_id()
        if intention_eligible is None:
            intention_eligible = not system_initiated
        contextual_messages = [
            {**item, "intention_eligible": False} for item in (contextual_messages or [])
        ]
        if not system_initiated and user_message_metadata is None:
            user_message_metadata = message_metadata()
        if _materialized_attachments is not None:
            if _durable_request_id is None or _durable_lease is None or attachments is not None:
                raise ValueError("materialized attachments require a fenced durable turn")
            normalized_attachments = _materialized_attachments
        else:
            normalized_attachments, attachment_error = await self._resolve_attachments_for_turn(
                user_email=user_email,
                attachments=attachments or [],
            )
            if attachment_error is not None:
                return attachment_error

        # Used ONLY for conversation bootstrapping (title generation, intention bootstrap).
        # All other paths use the raw content so that attachment-only messages record and
        # broadcast an empty string, which the UI optimistic-bubble deduplication expects.
        bootstrap_content = _effective_user_content(content, normalized_attachments)

        # Load conversation runtime only after validating attachments so
        # failed first sends do not bootstrap a session unnecessarily.
        try:
            runtime = await self._load_conversation_runtime(
                conversation_id, user_message=bootstrap_content
            )
        except SessionCreationFailedError:
            return TurnError(
                code="session_creation_failed",
                message="Could not create a session. Try again or check the diagnostics page.",
                recoverable=True,
                transient=True,
            )
        if runtime is None:
            return TurnError(
                code="not_found",
                message="Conversation not found",
                recoverable=False,
            )

        conversation, session, agent, bootstrap_wait_for_intention = runtime
        session.channel_default_agent_profile_id = channel_default_agent_profile_id

        async def _refresh_managed_runtime_for_admission() -> TurnError | None:
            nonlocal conversation, session, agent, bootstrap_wait_for_intention
            if getattr(getattr(conversation, "context", None), "type", None) != "agent_work":
                return None
            try:
                refreshed = await self._load_conversation_runtime(
                    conversation_id,
                    user_message=bootstrap_content,
                )
            except SessionCreationFailedError:
                return TurnError(
                    code="session_creation_failed",
                    message="Could not refresh the managed conversation runtime.",
                    recoverable=True,
                    transient=True,
                )
            if refreshed is None:
                return TurnError(
                    code="not_found",
                    message="Conversation not found",
                    recoverable=False,
                )
            conversation, session, agent, bootstrap_wait_for_intention = refreshed
            session.channel_default_agent_profile_id = channel_default_agent_profile_id
            return None

        if one_shot_chat_mode is None and not system_initiated:
            directive = parse_chat_mode_directive(content)
            if directive is not None and directive.one_shot and directive.remaining_content:
                one_shot_chat_mode = directive.mode
                content = directive.remaining_content
                bootstrap_content = _effective_user_content(content, normalized_attachments)

        # Authorization check
        if not system_initiated and conversation.user_email != user_email:
            return TurnError(
                code="forbidden",
                message="Conversation access denied",
                recoverable=False,
            )

        if prepared_attachment_notice is None and prepared_attachment_context is None:
            attachment_notice, attachment_context = await self._build_attachment_support_messages(
                session=session,
                agent=agent,
                attachments=normalized_attachments,
                acting_user_email=user_email,
            )
        else:
            attachment_notice = prepared_attachment_notice
            attachment_context = prepared_attachment_context

        # Conversation state check
        if conversation.status in {"archived", "deleted"}:
            return TurnError(
                code="conflict",
                message="Conversation is not active",
                recoverable=False,
            )

        # Session state check
        if session.status in BLOCKED_STATES:
            if session.status == SessionStatus.SUSPENDED:
                return TurnError(
                    code="session_suspended",
                    message="Session is suspended. Resolve the pending escalation to continue.",
                    recoverable=True,
                )
            return TurnError(
                code="session_ended",
                message="This session has ended. Use /new to start a fresh conversation.",
                recoverable=False,
            )

        # Escalation-pending check
        if not system_initiated:
            pending_esc = self._pause_waiter.find_pending(
                pause_type="escalation",
                conversation_id=conversation_id,
            )
            if pending_esc is not None and self._direct_turn_store is None:
                async with self._turn_lock(conversation_id):
                    refresh_error = await _refresh_managed_runtime_for_admission()
                    if refresh_error is not None:
                        return refresh_error
                    pending_esc = self._pause_waiter.find_pending(
                        pause_type="escalation",
                        conversation_id=conversation_id,
                    )
                    if pending_esc is not None:
                        if not allow_queue:
                            return TurnError(
                                code="queueing_not_allowed",
                                message=(
                                    "A pending escalation prevents starting this turn. "
                                    "Wait for it to resolve before sending another message."
                                ),
                                recoverable=True,
                                transient=True,
                                turn_id=admitted_turn_id,
                            )
                        queue = self._queued_messages[conversation_id]
                        if client_message_id and any(
                            queued.client_message_id == client_message_id for queued in queue
                        ):
                            await self._notify_queue_updated(
                                conversation_id, turn_observers=turn_observers
                            )
                            return None
                        queued_message = _QueuedMessage(
                            turn_id=admitted_turn_id,
                            queue_id=self._new_queue_id(),
                            session_id=getattr(session, "session_id", None),
                            content=content,
                            user_email=user_email,
                            intention_eligible=intention_eligible,
                            user_message_metadata=user_message_metadata,
                            contextual_messages=contextual_messages,
                            client_message_id=client_message_id,
                            attachments=[
                                item.model_dump(mode="json") for item in normalized_attachments
                            ],
                            attachment_notice=attachment_notice,
                            attachment_context=attachment_context,
                            outbound_attachments=outbound_attachments,
                            follow_up=follow_up,
                            channel_deliverable=channel_deliverable,
                            delivery_id=delivery_id,
                            delivery_fallback_text=delivery_fallback_text,
                            turn_observers=tuple(turn_observers or ()),
                            one_shot_chat_mode=one_shot_chat_mode,
                            channel_account_id=channel_account_id,
                            is_retry=is_retry,
                            retry_source_turn_id=retry_source_turn_id,
                            retry_reason=(
                                normalize_retry_reason(retry_reason) if is_retry else None
                            ),
                            retry_attempt=max(1, retry_attempt),
                        )
                        if admission_observer is not None:
                            try:
                                await admission_observer(admitted_turn_id, True)
                            except ManagedConversationAdmissionConflict as exc:
                                return TurnError(
                                    code="managed_admission_conflict",
                                    message=str(exc),
                                    recoverable=True,
                                    turn_id=admitted_turn_id,
                                )
                            except Exception as exc:
                                error = TurnError(
                                    code="managed_admission_failed",
                                    message=f"Managed turn admission failed: {exc}",
                                    recoverable=True,
                                    turn_id=admitted_turn_id,
                                )
                                await self._publish_turn_error(
                                    conversation_id,
                                    getattr(session, "session_id", None) or "",
                                    error,
                                    turn_id=admitted_turn_id,
                                    turn_observers=tuple(turn_observers or ()),
                                )
                                return error
                        try:
                            async with self._admission_drain_lock:
                                if not self._accepting_turns and queued_message_id is None:
                                    return TurnError(
                                        code="controller_draining",
                                        message=(
                                            "This controller is draining and cannot accept new turns."
                                        ),
                                        recoverable=True,
                                        transient=True,
                                        turn_id=admitted_turn_id,
                                    )
                                queue.append(queued_message)
                            await self._touch_conversation(conversation_id)
                            await self._clear_redo_on_accepted_user_turn(
                                conversation_id,
                                content=content,
                                system_initiated=system_initiated,
                            )
                            await self._notify_queue_updated(
                                conversation_id, turn_observers=turn_observers
                            )
                            last_notified_pause_id = self._escalation_notice_pause_ids.get(
                                conversation_id
                            )
                            if last_notified_pause_id != pending_esc.pause_id:
                                self._escalation_notice_pause_ids[conversation_id] = (
                                    pending_esc.pause_id
                                )
                                await self._notify_observers_system_message(
                                    conversation_id,
                                    "Waiting for escalation resolution. "
                                    "Use /approve or /deny, or use the buttons above.",
                                )
                            return None
                        except Exception as exc:
                            with contextlib.suppress(ValueError):
                                queue.remove(queued_message)
                            if admission_observer is None:
                                raise
                            error = TurnError(
                                code="managed_admission_failed",
                                message=f"Managed turn admission failed: {exc}",
                                recoverable=True,
                                turn_id=admitted_turn_id,
                            )
                            await self._publish_turn_error(
                                conversation_id,
                                getattr(session, "session_id", None) or "",
                                error,
                                turn_id=admitted_turn_id,
                                turn_observers=tuple(turn_observers or ()),
                            )
                            with contextlib.suppress(Exception):
                                await self._notify_queue_updated(
                                    conversation_id, turn_observers=turn_observers
                                )
                            return error
            if pending_esc is not None and self._direct_turn_store is not None:
                last_notified_pause_id = self._escalation_notice_pause_ids.get(conversation_id)
                if last_notified_pause_id != pending_esc.pause_id:
                    self._escalation_notice_pause_ids[conversation_id] = pending_esc.pause_id
                    await self._notify_observers_system_message(
                        conversation_id,
                        "Waiting for escalation resolution. "
                        "Use /approve or /deny, or use the buttons above.",
                        turn_observers=turn_observers,
                    )
            else:
                self._escalation_notice_pause_ids.pop(conversation_id, None)

            pending_questions = self._pause_waiter.list_pending(
                conversation_id=conversation_id,
                pause_type="step_question",
            )
            if any(pause.task_id is None for pause in pending_questions):
                return TurnError(
                    code="pending_question",
                    message="Answer the pending question before sending a new message.",
                    recoverable=True,
                )
            for pause_type in ("auth_challenge", "credential_request"):
                pending_inputs = self._pause_waiter.list_pending(
                    conversation_id=conversation_id,
                    pause_type=pause_type,
                )
                if any(pause.task_id is None for pause in pending_inputs):
                    return TurnError(
                        code="pending_input_request",
                        message="Answer the pending input request before sending a new message.",
                        recoverable=True,
                    )

        max_active_turns, max_queued_messages = await self._load_turn_limits()

        if self._direct_turn_store is not None and _durable_request_id is None:
            pending = await self._direct_turn_store.list_conversation_pending(conversation_id)
            if pending and not allow_queue:
                return TurnError(
                    code="queueing_not_allowed",
                    message="A turn is already active or queued for this conversation.",
                    recoverable=True,
                    transient=True,
                    turn_id=admitted_turn_id,
                )
            if len(pending) >= max_queued_messages + 1:
                return TurnError(
                    code="queue_full",
                    message="Message queue is full. Wait for the current turn to finish.",
                    recoverable=True,
                    transient=True,
                )
            stable_key = (
                idempotency_key
                or client_message_id
                or delivery_id
                or (follow_up.follow_up_id if follow_up is not None else None)
                or f"oneshot:{uuid.uuid4().hex}"
            )
            scope = idempotency_scope or (
                f"direct:{conversation_id}:{user_email}:{'system' if system_initiated else 'user'}"
            )
            metadata = {
                "outbound_attachments": outbound_attachments or [],
                "system_initiated": system_initiated,
                "intention_eligible": intention_eligible,
                "follow_up": follow_up.model_dump(mode="json")
                if follow_up is not None and hasattr(follow_up, "model_dump")
                else None,
                "channel_deliverable": channel_deliverable,
                "delivery_id": delivery_id,
                "delivery_fallback_text": delivery_fallback_text,
                "client_message_id": client_message_id,
                "attachment_notice": attachment_notice,
                "attachment_context": attachment_context,
                "one_shot_chat_mode": one_shot_chat_mode,
                "channel_default_agent_profile_id": channel_default_agent_profile_id,
                "channel_account_id": channel_account_id,
                "is_retry": is_retry,
                "retry_source_turn_id": retry_source_turn_id,
                "retry_reason": (normalize_retry_reason(retry_reason).value if is_retry else None),
                "retry_attempt": max(1, retry_attempt),
                "absorbable": all(
                    getattr(observer, "supports_mid_turn_absorb", False)
                    for observer in (turn_observers or ())
                ),
            }
            if user_message_metadata is not None:
                metadata["user_message_metadata"] = user_message_metadata
            if contextual_messages:
                metadata["contextual_messages"] = contextual_messages
            async with self._admission_drain_lock:
                if not self._accepting_turns:
                    return TurnError(
                        code="controller_draining",
                        message="This controller is draining and cannot accept new turns.",
                        recoverable=True,
                        transient=True,
                    )
                try:
                    admission = await self._direct_turn_store.admit(
                        conversation_id=conversation_id,
                        session_id=getattr(session, "session_id", None),
                        agent_id=agent.agent_id,
                        user_id=user_email,
                        idempotency_scope=scope,
                        idempotency_key=stable_key,
                        payload={
                            "schema_version": 1,
                            "content": content,
                            "attachments": [
                                item.model_dump(mode="json") for item in normalized_attachments
                            ],
                            "metadata": metadata,
                            "channel_delivery": (
                                channel_delivery.model_dump(mode="json")
                                if channel_delivery is not None
                                else None
                            ),
                            "retry_reason": (
                                normalize_retry_reason(retry_reason).value if is_retry else None
                            ),
                        },
                        request_id=durable_request_id or queued_message_id,
                        turn_id=admitted_turn_id,
                        admission_guard=durable_admission_guard,
                        transaction_participant=admission_transaction_participant,
                    )
                except DirectTurnAdmissionRejected:
                    return TurnError(
                        code="managed_admission_conflict",
                        message="Managed conversation admission fence changed.",
                        recoverable=True,
                        transient=True,
                        turn_id=admitted_turn_id,
                    )
            self._durable_turn_observers[admission.request.request_id] = tuple(turn_observers or ())
            if admission_observer is not None and admission.created:
                await admission_observer(admission.request.turn_id, True)
            if not system_initiated and admission.created:
                await self._touch_conversation(conversation_id)
            if admission.created:
                await self._clear_redo_on_accepted_user_turn(
                    conversation_id,
                    content=content,
                    system_initiated=system_initiated,
                )
                await self._publish_durable_turn_change(admission.request)
            await self._notify_queue_updated(
                conversation_id,
                turn_observers=turn_observers,
            )
            if self._direct_turn_runtime is not None:
                await self._direct_turn_runtime.wake()
            return None

        async with self._turn_lock(conversation_id):
            refresh_error = await _refresh_managed_runtime_for_admission()
            if refresh_error is not None:
                return refresh_error
            # Per-user concurrent turn limit
            if not system_initiated:
                user_active = self._user_turn_counts.get(user_email, 0)
                if user_active >= max_active_turns:
                    return TurnError(
                        code="rate_limited",
                        message="Too many concurrent turns. Wait for a turn to finish.",
                        recoverable=True,
                        transient=True,
                    )

            # Queue if a turn is already active or still cancelling.  This lock is
            # the hard per-conversation serialization boundary: a new turn must
            # not be launched until the previous task has run its final cleanup.
            active = self._active_turns.get(conversation_id)
            if active is not None:
                if active.done():
                    self._active_turns.pop(conversation_id, None)
                else:
                    if not allow_queue:
                        return TurnError(
                            code="queueing_not_allowed",
                            message="A turn is already active for this conversation.",
                            recoverable=True,
                            transient=True,
                            turn_id=admitted_turn_id,
                        )
                    if is_retry:
                        return TurnError(
                            code="active_turn_in_progress",
                            message="A turn is already active for this conversation.",
                            recoverable=True,
                            transient=True,
                        )
                    queue = self._queued_messages[conversation_id]
                    if client_message_id and any(
                        queued.client_message_id == client_message_id for queued in queue
                    ):
                        await self._notify_queue_updated(
                            conversation_id, turn_observers=turn_observers
                        )
                        return None
                    if len(queue) >= max_queued_messages:
                        return TurnError(
                            code="queue_full",
                            message="Message queue is full. Wait for the current turn to finish.",
                            recoverable=True,
                            transient=True,
                        )
                    queued_message = _QueuedMessage(
                        turn_id=admitted_turn_id,
                        queue_id=self._new_queue_id(),
                        session_id=getattr(session, "session_id", None),
                        content=content,
                        user_email=user_email,
                        intention_eligible=intention_eligible,
                        user_message_metadata=user_message_metadata,
                        contextual_messages=contextual_messages,
                        client_message_id=client_message_id,
                        attachments=[
                            item.model_dump(mode="json") for item in normalized_attachments
                        ],
                        attachment_notice=attachment_notice,
                        attachment_context=attachment_context,
                        outbound_attachments=outbound_attachments,
                        system_initiated=system_initiated,
                        follow_up=follow_up,
                        channel_deliverable=channel_deliverable,
                        delivery_id=delivery_id,
                        delivery_fallback_text=delivery_fallback_text,
                        turn_observers=tuple(turn_observers or ()),
                        one_shot_chat_mode=one_shot_chat_mode,
                        channel_account_id=channel_account_id,
                        channel_delivery=channel_delivery,
                    )
                    if admission_observer is not None:
                        try:
                            await admission_observer(admitted_turn_id, True)
                        except ManagedConversationAdmissionConflict as exc:
                            return TurnError(
                                code="managed_admission_conflict",
                                message=str(exc),
                                recoverable=True,
                                turn_id=admitted_turn_id,
                            )
                        except Exception as exc:
                            error = TurnError(
                                code="managed_admission_failed",
                                message=f"Managed turn admission failed: {exc}",
                                recoverable=True,
                                turn_id=admitted_turn_id,
                            )
                            await self._publish_turn_error(
                                conversation_id,
                                getattr(session, "session_id", None) or "",
                                error,
                                turn_id=admitted_turn_id,
                                system_initiated=system_initiated,
                                channel_deliverable=channel_deliverable,
                                delivery_id=delivery_id,
                                delivery_fallback_text=delivery_fallback_text,
                                turn_observers=tuple(turn_observers or ()),
                            )
                            return error
                    try:
                        async with self._admission_drain_lock:
                            if not self._accepting_turns and queued_message_id is None:
                                return TurnError(
                                    code="controller_draining",
                                    message=(
                                        "This controller is draining and cannot accept new turns."
                                    ),
                                    recoverable=True,
                                    transient=True,
                                    turn_id=admitted_turn_id,
                                )
                            queue.append(queued_message)
                        if not system_initiated:
                            await self._touch_conversation(conversation_id)
                        await self._clear_redo_on_accepted_user_turn(
                            conversation_id,
                            content=content,
                            system_initiated=system_initiated,
                        )
                        await self._notify_queue_updated(
                            conversation_id, turn_observers=turn_observers
                        )
                        return None
                    except Exception as exc:
                        with contextlib.suppress(ValueError):
                            queue.remove(queued_message)
                        if admission_observer is None:
                            raise
                        error = TurnError(
                            code="managed_admission_failed",
                            message=f"Managed turn admission failed: {exc}",
                            recoverable=True,
                            turn_id=admitted_turn_id,
                        )
                        await self._publish_turn_error(
                            conversation_id,
                            getattr(session, "session_id", None) or "",
                            error,
                            turn_id=admitted_turn_id,
                            system_initiated=system_initiated,
                            channel_deliverable=channel_deliverable,
                            delivery_id=delivery_id,
                            delivery_fallback_text=delivery_fallback_text,
                            turn_observers=tuple(turn_observers or ()),
                        )
                        with contextlib.suppress(Exception):
                            await self._notify_queue_updated(
                                conversation_id, turn_observers=turn_observers
                            )
                        return error

            loaded_session_id = session.session_id
            try:
                session_locked = self._agent_loop.session_is_locked(session.session_id)
            except AttributeError:
                session_locked = False
            if session_locked:
                await self._agent_loop.wait_for_session_unlock(session.session_id)
            refreshed_runtime = await self._load_conversation_runtime(
                conversation_id,
                user_message=bootstrap_content,
            )
            if refreshed_runtime is None:
                return TurnError(
                    code="not_found",
                    message="Conversation not found",
                    recoverable=False,
                )
            conversation, session, agent, bootstrap_wait_for_intention = refreshed_runtime
            if conversation.status in {"archived", "deleted"}:
                return TurnError(
                    code="conflict",
                    message="Conversation is not active",
                    recoverable=False,
                )
            if session.status in BLOCKED_STATES:
                return TurnError(
                    code="session_ended",
                    message="This session has ended. Use /new to start a fresh conversation.",
                    recoverable=False,
                )
            if (
                session.session_id != loaded_session_id
                and prepared_attachment_notice is None
                and prepared_attachment_context is None
            ):
                (
                    attachment_notice,
                    attachment_context,
                ) = await self._build_attachment_support_messages(
                    session=session,
                    agent=agent,
                    attachments=normalized_attachments,
                    acting_user_email=user_email,
                )

            checkpoint_conversation = _model_copy_or_self(conversation)
            checkpoint_session = _model_copy_or_self(session)

            if session.status == SessionStatus.IDLE:
                try:
                    updated = await self._session_manager.mark_active(session.session_id)
                    if updated:
                        session.status = SessionStatus.ACTIVE
                        session.idle_since = None
                except Exception:
                    logger.warning(
                        "turn_scheduler: failed to reactivate idle session",
                        extra={
                            "extra_data": {
                                "conversation_id": conversation_id,
                                "session_id": session.session_id,
                            }
                        },
                        exc_info=True,
                    )

            await self._clear_redo_on_accepted_user_turn(
                conversation_id,
                content=content,
                system_initiated=system_initiated,
            )

            # Launch the turn while still holding the conversation lock so no
            # other submitter can observe a gap between admission and ownership
            # registration.
            if admission_observer is not None:
                try:
                    await admission_observer(admitted_turn_id, False)
                except ManagedConversationAdmissionConflict as exc:
                    return TurnError(
                        code="managed_admission_conflict",
                        message=str(exc),
                        recoverable=True,
                        turn_id=admitted_turn_id,
                    )
                except Exception as exc:
                    error = TurnError(
                        code="managed_admission_failed",
                        message=f"Managed turn admission failed: {exc}",
                        recoverable=True,
                        turn_id=admitted_turn_id,
                    )
                    await self._publish_turn_error(
                        conversation_id,
                        getattr(session, "session_id", None) or "",
                        error,
                        turn_id=admitted_turn_id,
                        system_initiated=system_initiated,
                        channel_deliverable=channel_deliverable,
                        delivery_id=delivery_id,
                        delivery_fallback_text=delivery_fallback_text,
                        turn_observers=tuple(turn_observers or ()),
                    )
                    return error
            try:
                if not system_initiated:
                    await self._touch_conversation(conversation_id)
                async with self._admission_drain_lock:
                    if (
                        queued_message_id is not None
                        and queued_message_id in self._cancelled_queued_relaunches
                    ):
                        self._queued_relaunches.pop(queued_message_id, None)
                        self._cancelled_queued_relaunches.discard(queued_message_id)
                        return TurnError(
                            code="queued_turn_cancelled",
                            message="The queued turn was cancelled during shutdown.",
                            recoverable=True,
                            turn_id=admitted_turn_id,
                        )
                    if not self._accepting_turns and queued_message_id is None:
                        return TurnError(
                            code="controller_draining",
                            message="This controller is draining and cannot accept new turns.",
                            recoverable=True,
                            transient=True,
                            turn_id=admitted_turn_id,
                        )
                    self._launch_turn(
                        conversation=conversation,
                        session=session,
                        agent=agent,
                        content=content,
                        user_email=user_email,
                        intention_eligible=intention_eligible,
                        user_message_metadata=user_message_metadata,
                        contextual_messages=contextual_messages,
                        attachments=normalized_attachments,
                        outbound_attachments=outbound_attachments,
                        attachment_notice=attachment_notice,
                        attachment_context=attachment_context,
                        system_initiated=system_initiated,
                        follow_up=follow_up,
                        channel_deliverable=channel_deliverable,
                        delivery_id=delivery_id,
                        delivery_fallback_text=delivery_fallback_text,
                        bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                        turn_observers=tuple(turn_observers or ()),
                        client_message_id=client_message_id,
                        queue_id=queued_message_id,
                        one_shot_chat_mode=one_shot_chat_mode,
                        channel_default_agent_profile_id=channel_default_agent_profile_id,
                        is_retry=is_retry,
                        retry_source_turn_id=retry_source_turn_id,
                        retry_reason=retry_reason,
                        retry_attempt=retry_attempt,
                        turn_id=admitted_turn_id,
                        checkpoint_conversation=(
                            checkpoint_conversation if not system_initiated else None
                        ),
                        checkpoint_session=checkpoint_session if not system_initiated else None,
                        durable_request_id=_durable_request_id,
                        durable_lease=_durable_lease,
                        durable_user_append_session_id=_durable_user_append_session_id,
                        recovery_context=_recovery_context,
                        channel_delivery=channel_delivery,
                    )
                    if queued_message_id is not None:
                        self._queued_relaunches.pop(queued_message_id, None)
            except Exception as exc:
                if admission_observer is None:
                    raise
                error = TurnError(
                    code="managed_admission_failed",
                    message=f"Managed turn admission failed: {exc}",
                    recoverable=True,
                    turn_id=admitted_turn_id,
                )
                await self._publish_turn_error(
                    conversation_id,
                    getattr(session, "session_id", None) or "",
                    error,
                    turn_id=admitted_turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                    turn_observers=tuple(turn_observers or ()),
                )
                return error
        return None

    async def begin_drain(self) -> dict[str, int]:
        """Close admission while preserving already accepted queued turns."""
        async with self._admission_drain_lock:
            self._accepting_turns = False
            queued = sum(len(queue) for queue in self._queued_messages.values())
        await self.stop_direct_turn_claims()
        return {"queued_preserved": queued}

    async def start_direct_turn_runtime(self) -> None:
        runtime = getattr(self, "_direct_turn_runtime", None)
        if runtime is not None:
            await runtime.start()

    async def wake_direct_turn_runtime(self) -> None:
        """Wake the durable worker after an externally committed queue mutation."""
        runtime = getattr(self, "_direct_turn_runtime", None)
        if runtime is not None:
            await runtime.wake()

    def _can_claim_direct_turn(self, row: DirectTurnRequestRow) -> bool:
        runtime = self._controller_runtime
        if runtime is None or not runtime.schema_compatible:
            return False
        if runtime.state is not ControllerLifecycleState.READY:
            return False
        return (
            self._pause_waiter.find_pending(
                pause_type="escalation",
                conversation_id=row.conversation_id,
            )
            is None
        )

    async def _handle_permanent_direct_turn_failure(
        self,
        row: DirectTurnRequestRow,
        exc: Exception,
    ) -> None:
        _, session, agent, _ = await self._load_conversation_runtime(row.conversation_id)
        metadata = row.payload.get("metadata") or {}
        outcome = row.outcome if isinstance(row.outcome, dict) else {}
        append_phase = outcome.get("user_append_phase") or outcome.get("phase")
        user_message_already_appended = append_phase in {
            "user_appended",
            "recovered_model_boundary",
            "reconciled_canonical_append",
        }
        chat_mode = ResolvedChatMode(mode="default", source="system_default")
        if not user_message_already_appended:
            payload_attachments = row.payload.get("attachments")
            attachments = (
                [
                    AttachmentRef.model_validate(attachment)
                    for attachment in payload_attachments
                    if isinstance(attachment, dict)
                ]
                if isinstance(payload_attachments, list)
                and isinstance(exc, PermanentDirectTurnControllerError)
                else []
            )
            await self._persist_admitted_user_message(
                session=session,
                agent=agent,
                user_email=row.user_id,
                intention_eligible=bool(metadata.get("intention_eligible", True)),
                content=str(row.payload.get("content") or ""),
                attachments=attachments,
                turn_id=row.turn_id,
                client_message_id=metadata.get("client_message_id"),
                chat_mode=chat_mode,
                cancel_event=asyncio.Event(),
            )
        error = TurnError(
            code=(
                "controller_execution_failed"
                if isinstance(exc, PermanentDirectTurnControllerError)
                else "attachment_unavailable"
            ),
            message=(
                "The message could not be processed."
                if isinstance(exc, PermanentDirectTurnControllerError)
                else "An attachment is no longer available."
            ),
            recoverable=False,
            transient=False,
            turn_id=row.turn_id,
        )
        await self._persist_turn_error_event(
            session=session,
            agent=agent,
            user_email=row.user_id,
            turn_id=row.turn_id,
            error=error,
            chat_mode=chat_mode.mode,
            chat_mode_source=chat_mode.source,
        )
        await self._publish_turn_error(
            row.conversation_id,
            session.session_id,
            error,
            turn_id=row.turn_id,
            turn_observers=self._durable_turn_observers.pop(row.request_id, ()),
            durable_session=session,
            durable_agent=agent,
            durable_user_email=row.user_id,
        )
        await self._notify_queue_updated(row.conversation_id)

    async def _handle_fenced_permanent_direct_turn_failure(
        self,
        row: DirectTurnRequestRow,
        exc: Exception,
        lease: Lease,
    ) -> None:
        await self._handle_permanent_direct_turn_failure(row, exc)
        payload = row.payload if isinstance(row.payload, dict) else {}
        descriptor = payload.get("channel_delivery")
        if isinstance(descriptor, dict):
            await self._persist_direct_turn_terminal_delivery(
                request_id=row.request_id,
                lease=lease,
                descriptor=ChannelDeliveryDescriptor.model_validate(descriptor),
                content=str(exc),
                attachments=None,
                error=True,
            )

    def set_channel_delivery_service(self, service: Any) -> None:
        """Attach the channel delivery adapter without importing it here."""
        self._channel_delivery = service

    async def _persist_direct_turn_terminal_delivery(
        self,
        *,
        request_id: str,
        lease: Lease,
        descriptor: ChannelDeliveryDescriptor | None,
        content: str,
        attachments: list[dict[str, Any]] | None,
        error: bool = False,
    ) -> None:
        if descriptor is None or self._channel_delivery is None:
            return
        await self._channel_delivery.deliver_fenced_direct_turn(
            request_id=request_id,
            lease=lease,
            descriptor=descriptor,
            content=content,
            attachments=attachments,
            error=error,
        )

    async def _persist_direct_turn_step_error_delivery(
        self,
        *,
        request_id: str | None,
        lease: Lease | None,
        descriptor: ChannelDeliveryDescriptor | None,
        error: TurnError,
    ) -> bool:
        """Persist a terminal step error without consuming recoverable retries."""
        if request_id is None or lease is None or error.transient:
            return False
        await self._persist_direct_turn_terminal_delivery(
            request_id=request_id,
            lease=lease,
            descriptor=descriptor,
            content=error.message,
            attachments=None,
            error=True,
        )
        return descriptor is not None and self._channel_delivery is not None

    async def _prepare_durable_transient_retry(
        self,
        *,
        request_id: str | None,
        lease: Lease | None,
        error: TurnError,
        execution_fence: DirectTurnExecutionFence | None,
    ) -> tuple[bool, TurnError]:
        """Suppress visibility while a fenced durable retry remains available."""
        if (
            not error.transient
            or request_id is None
            or lease is None
            or execution_fence is None
            or self._direct_turn_store is None
        ):
            return False, error
        await execution_fence.assert_current()
        current = await self._direct_turn_store.get(request_id)
        if current is None:
            raise StaleDirectTurnOwner(request_id)
        if error.code == "executor_unavailable":
            retry_after_seconds = (error.detail or {}).get("retry_after_seconds")
            if isinstance(retry_after_seconds, (int, float)) and retry_after_seconds > 0:
                execution_fence.retry_after_seconds = float(retry_after_seconds)
        if current.attempt_count < DIRECT_TURN_TRANSIENT_MAX_ATTEMPTS:
            execution_fence.interruption_reason = error.code
            return True, error
        detail = dict(error.detail or {})
        detail["durable_retry_exhausted"] = True
        detail["attempts"] = current.attempt_count
        detail["max_attempts"] = DIRECT_TURN_TRANSIENT_MAX_ATTEMPTS
        return (
            False,
            replace(
                error,
                recoverable=False,
                transient=False,
                detail=detail,
            ),
        )

    async def _cleanup_durable_retry_attempt(
        self,
        *,
        conversation_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        """Discard only process-local attempt overlays before durable reclaim."""
        await self._reset_active_stream(conversation_id)
        if hasattr(self._session_cache, "clear_active_thinking"):
            self._session_cache.clear_active_thinking(session_id)
        self._clear_assistant_phase(conversation_id, turn_id)

    async def _notify_durable_retry_pending(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        error: TurnError,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None,
    ) -> None:
        """Show a non-terminal notice while a durable request waits for reclaim."""
        await self._notify_observers_system_message(
            conversation_id,
            "Turn paused because a required service is temporarily unavailable. "
            "Cognis will resume it automatically.",
            notice_id=f"turn-paused:{turn_id}",
            kind="turn_retry_pending",
            scope="transient_retry",
            turn_id=turn_id,
            retry_reason=error.code,
            turn_observers=turn_observers,
        )

    async def _settle_durable_direct_turn(
        self,
        *,
        request_id: str,
        lease: Lease,
        turn_id: str,
        succeeded: bool,
        cancelled: bool,
        transient_failure: bool,
        transient_phase: str,
        transient_session_id: str,
        interruption_reason: str | None = None,
        source_phase: str | None = None,
        retry_after_seconds: float | None = None,
        ambiguous: bool = False,
        ambiguity_detail: dict[str, Any] | None = None,
    ) -> DirectTurnStatus:
        """Settle terminal outcomes, keeping transient failures reclaimable."""
        store = self._direct_turn_store
        if store is None:
            raise StaleDirectTurnOwner(request_id)
        current = await store.get(request_id)
        if current is None:
            raise StaleDirectTurnOwner(request_id)
        if transient_failure and not cancelled:
            recovered = await store.settle_transient_failure(
                request_id,
                lease=lease,
                outcome={
                    "phase": transient_phase,
                    "turn_id": turn_id,
                    "session_id": transient_session_id,
                    "user_append_phase": transient_phase,
                    "user_append_session_id": transient_session_id,
                    **(
                        {
                            "interruption_reason": interruption_reason,
                            "source_phase": source_phase,
                        }
                        if interruption_reason
                        else {}
                    ),
                    **(
                        {"retry_after_seconds": retry_after_seconds}
                        if retry_after_seconds is not None
                        else {}
                    ),
                },
                retry_after_seconds=retry_after_seconds,
            )
            if recovered is None:
                raise StaleDirectTurnOwner(request_id)
            if recovered.cancel_requested_at is not None:
                return DirectTurnStatus.CANCELLED
            return DirectTurnStatus(recovered.status)
        status = (
            DirectTurnStatus.COMPLETED
            if succeeded
            else DirectTurnStatus.AMBIGUOUS
            if ambiguous
            else DirectTurnStatus.CANCELLED
            if cancelled or bool(current.cancel_requested_at)
            else DirectTurnStatus.FAILED
        )
        terminal = await store.mark_terminal(
            request_id,
            lease=lease,
            status=status,
            outcome={
                "phase": "ambiguous" if ambiguous else "terminal",
                "turn_id": turn_id,
                "succeeded": succeeded,
                "ambiguous": ambiguous,
                **({"ambiguity": ambiguity_detail} if ambiguity_detail else {}),
            },
        )
        if terminal is None:
            raise StaleDirectTurnOwner(request_id)
        return status

    async def stop_direct_turn_claims(self) -> None:
        runtime = getattr(self, "_direct_turn_runtime", None)
        if runtime is not None:
            await runtime.stop_claiming()

    async def stop_direct_turn_runtime(self) -> None:
        runtime = getattr(self, "_direct_turn_runtime", None)
        if runtime is not None:
            await runtime.stop()

    async def _execute_claimed_direct_turn(
        self,
        row: DirectTurnRequestRow,
        payload: MaterializedDirectTurnPayload,
        fence: DirectTurnExecutionFence,
    ) -> None:
        metadata = payload.metadata
        raw_follow_up = metadata.get("follow_up")
        follow_up = (
            parse_follow_up_metadata(raw_follow_up) if isinstance(raw_follow_up, dict) else None
        )
        raw_mode = metadata.get("one_shot_chat_mode")
        one_shot_mode = optional_chat_mode(raw_mode)
        durable_outcome = row.outcome if isinstance(row.outcome, dict) else {}
        interruption_reason = durable_outcome.get("interruption_reason")
        recovery_context = (
            "The previous attempt of this same turn was interrupted because "
            f"{interruption_reason}. Continue the user's request from the durable "
            "conversation history. Verify recent tool calls and external state before "
            "repeating any action that may have side effects; do not assume an "
            "in-flight action failed merely because its result is absent."
            if isinstance(interruption_reason, str) and interruption_reason
            else None
        )
        raw_append_phase = durable_outcome.get("user_append_phase")
        append_phase = (
            str(raw_append_phase)
            if isinstance(raw_append_phase, str)
            else str(durable_outcome.get("phase") or "user_append_pending")
        )
        durable_append_session_id = (
            str(durable_outcome.get("user_append_session_id") or durable_outcome["session_id"])
            if isinstance(
                durable_outcome.get("user_append_session_id") or durable_outcome.get("session_id"),
                str,
            )
            else None
        )
        if append_phase in {"canonical_user_append", "user_append_uncertain"}:
            try:
                append_phase = (
                    "user_appended"
                    if await self._reconcile_direct_turn_append(row)
                    else "user_append_pending"
                )
            except Exception as exc:
                direct_turn_store = self._direct_turn_store
                if direct_turn_store is not None:
                    await direct_turn_store.mark_recoverable(
                        row.request_id,
                        lease=fence.lease,
                        outcome={**durable_outcome, "phase": "user_append_uncertain"},
                    )
                raise StaleDirectTurnOwner(row.request_id) from exc
            direct_turn_store = self._direct_turn_store
            fence.set_user_append_state(
                append_phase,
                session_id=durable_append_session_id,
            )
            if direct_turn_store is None or (
                await direct_turn_store.checkpoint(
                    row.request_id,
                    lease=fence.lease,
                    phase=append_phase,
                    metadata=(
                        {
                            "session_id": durable_append_session_id,
                            "user_append_phase": append_phase,
                            "user_append_session_id": durable_append_session_id,
                        }
                        if durable_append_session_id is not None
                        else None
                    ),
                )
                is None
            ):
                raise StaleDirectTurnOwner(row.request_id)
        recovered_after_user_append = append_phase in {
            "recovered_model_boundary",
            "reconciled_canonical_append",
            "user_appended",
        }
        self._durable_request_by_conversation[row.conversation_id] = row.request_id
        self._durable_fences[row.request_id] = fence
        row_session_id = getattr(row, "session_id", None)
        row_owner_controller_id = getattr(row, "owner_controller_id", None)
        row_owner_incarnation_id = getattr(row, "owner_incarnation_id", None)
        row_fencing_token = getattr(row, "fencing_token", None)
        if (
            row_session_id
            and row.turn_id
            and row_owner_controller_id
            and row_owner_incarnation_id
            and row_fencing_token is not None
        ):
            if not hasattr(self, "_relay_generation_contexts"):
                self._relay_generation_contexts = {}
            self._relay_generation_contexts[row.conversation_id] = RelayGenerationContext(
                direct_request_id=row.request_id,
                turn_id=row.turn_id,
                session_id=row_session_id,
                conversation_id=row.conversation_id,
                owner_controller_id=row_owner_controller_id,
                owner_incarnation_id=row_owner_incarnation_id,
                fencing_token=row_fencing_token,
            )
        try:
            error = await self.submit_turn(
                row.conversation_id,
                payload.content,
                user_email=row.user_id,
                intention_eligible=bool(metadata.get("intention_eligible", True)),
                _materialized_attachments=payload.attachments,
                outbound_attachments=metadata.get("outbound_attachments") or [],
                system_initiated=bool(metadata.get("system_initiated")),
                follow_up=follow_up,
                channel_deliverable=bool(metadata.get("channel_deliverable")),
                delivery_id=metadata.get("delivery_id"),
                delivery_fallback_text=metadata.get("delivery_fallback_text"),
                turn_observers=self._durable_turn_observers.get(row.request_id, ()),
                client_message_id=metadata.get("client_message_id"),
                user_message_metadata=metadata.get("user_message_metadata"),
                contextual_messages=metadata.get("contextual_messages") or [],
                queued_message_id=row.request_id,
                prepared_attachment_notice=metadata.get("attachment_notice"),
                prepared_attachment_context=metadata.get("attachment_context"),
                one_shot_chat_mode=one_shot_mode,
                channel_default_agent_profile_id=metadata.get("channel_default_agent_profile_id"),
                channel_account_id=metadata.get("channel_account_id"),
                channel_delivery=payload.channel_delivery,
                is_retry=bool(metadata.get("is_retry")) or recovered_after_user_append,
                retry_source_turn_id=(
                    row.turn_id
                    if recovery_context is not None
                    else metadata.get("retry_source_turn_id")
                ),
                retry_reason=(
                    retry_reason_from_interruption(interruption_reason)
                    if recovery_context is not None
                    else getattr(payload, "retry_reason", None) or metadata.get("retry_reason")
                ),
                retry_attempt=max(
                    1,
                    getattr(row, "attempt_count", 1)
                    if recovery_context is not None
                    else int(metadata.get("retry_attempt") or 1),
                ),
                turn_id=row.turn_id,
                _durable_request_id=row.request_id,
                _durable_lease=fence.lease,
                _durable_user_append_session_id=durable_append_session_id,
                _recovery_context=recovery_context,
            )
            if error is not None:
                raise RuntimeError(error.message)
            task = self._active_turns.get(row.conversation_id)
            if task is not None:
                await asyncio.shield(task)
        finally:
            getattr(self, "_relay_generation_contexts", {}).pop(row.conversation_id, None)
            self._durable_request_by_conversation.pop(row.conversation_id, None)
            self._durable_fences.pop(row.request_id, None)

    async def _mark_durable_absorbed(self, request_id: str, lease: Lease) -> None:
        if self._direct_turn_store is None:
            return
        if await self._direct_turn_store.mark_absorbed(request_id, lease=lease) is None:
            raise StaleDirectTurnOwner(request_id)

    async def _publish_durable_turn_change(self, row: DirectTurnRequestRow) -> None:
        """Invalidate remote Chat v2 projections after a durable turn transition."""

        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is None:
            return
        await cluster_signals.publish_chat_change(
            row.conversation_id,
            session_id=row.session_id,
            revision=f"direct-turn:{row.request_id}:{row.status}:{row.updated_at.isoformat()}",
        )

    async def _checkpoint_durable_absorbed_append(
        self,
        request_id: str,
        lease: Lease,
        intaris_session_id: str,
    ) -> None:
        if self._direct_turn_store is None:
            return
        row = await self._direct_turn_store.checkpoint(
            request_id,
            lease=lease,
            phase="canonical_user_append",
            metadata={"session_id": intaris_session_id},
        )
        if row is None:
            raise StaleDirectTurnOwner(request_id)

    async def _assert_durable_conversation_fence(self, conversation_id: str) -> None:
        request_id = self._durable_request_by_conversation.get(conversation_id)
        if request_id is None:
            return
        fence = self._durable_fences.get(request_id)
        if fence is None:
            raise StaleDirectTurnOwner(request_id)
        await fence.assert_current()

    async def _reconcile_direct_turn_append(self, row: DirectTurnRequestRow) -> bool:
        outcome = row.outcome if isinstance(row.outcome, dict) else {}
        session_id = outcome.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return False
        expected_types = outcome.get("event_types")
        types = (
            [str(item) for item in expected_types if isinstance(item, str)]
            if isinstance(expected_types, list)
            else ["assistant_message"]
            if outcome.get("phase") == "model_response"
            else ["user_message"]
        )
        expected_call_ids = {
            str(item) for item in (outcome.get("call_ids") or []) if isinstance(item, str)
        }
        async with self._session_factory() as db_session:
            agent = await queries.get_agent(db_session, row.agent_id)
        system_agent = SYSTEM_AGENTS.get(row.agent_id)
        agent_owner_email = (
            agent.owner_email
            if agent is not None
            else system_agent.owner_email
            if system_agent is not None
            else None
        )
        if agent_owner_email is None:
            raise RuntimeError("Direct-turn agent is unavailable for event-store reconciliation")
        with scoped_runtime_context(
            user_email=row.user_id,
            agent_id=row.agent_id,
            agent_owner_email=agent_owner_email,
        ):
            after_seq = 0
            while True:
                result = await self._providers.guardrails.read_events(
                    session_id=session_id,
                    after_seq=after_seq,
                    limit=200,
                    types=types,
                    allow_missing_stream=True,
                )
                for event in result.events:
                    data = event.get("data") if isinstance(event, dict) else None
                    if not isinstance(data, dict):
                        continue
                    if (
                        data.get("queue_id") == row.request_id
                        or data.get("turn_id") == row.turn_id
                        or data.get("call_id") in expected_call_ids
                    ):
                        return True
                if not result.has_more or result.last_seq <= after_seq:
                    return False

    async def drain_active_turns(self, *, timeout_seconds: float) -> dict[str, int]:
        """Wait for local active turns without cancelling them on timeout."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        initial = len([task for task in self._active_turns.values() if not task.done()])
        while True:
            tasks = [task for task in self._active_turns.values() if not task.done()]
            queued = sum(len(queue) for queue in self._queued_messages.values())
            registering = len(self._queued_relaunches)
            if not tasks and not queued and not registering:
                return {"active": initial, "completed": initial, "timed_out": 0}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {
                    "active": initial,
                    "completed": initial - len(tasks),
                    "timed_out": len(tasks) + queued + registering,
                }
            if tasks:
                await asyncio.wait(
                    tasks,
                    timeout=min(remaining, 0.1),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(min(remaining, 0.01))

    async def cancel_active_turns_and_wait(self, *, timeout_seconds: float) -> dict[str, int]:
        """Request normal cancellation and bound settlement before teardown."""
        async with self._admission_drain_lock:
            active_conversation_ids = [
                conversation_id
                for conversation_id, task in self._active_turns.items()
                if not task.done()
            ]
            conversation_ids = set(active_conversation_ids)
            conversation_ids.update(
                conversation_id for conversation_id, queue in self._queued_messages.items() if queue
            )
            conversation_ids.update(self._queued_relaunches.values())
            self._cancelled_queued_relaunches.update(
                queued.queue_id for queue in self._queued_messages.values() for queued in queue
            )
            self._cancelled_queued_relaunches.update(self._queued_relaunches)
            tasks = [
                self._active_turns[conversation_id] for conversation_id in active_conversation_ids
            ]
        cancellation_requests = [
            asyncio.create_task(self.cancel_turn(conversation_id, clear_queue=True))
            for conversation_id in conversation_ids
        ]
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        if cancellation_requests:
            await asyncio.wait(cancellation_requests, timeout=timeout_seconds)
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if tasks and remaining:
            await asyncio.wait(tasks, timeout=remaining)
        abandoned_tasks = sum(not task.done() for task in tasks)
        abandoned_requests = sum(not task.done() for task in cancellation_requests)
        return {
            "requested": len(conversation_ids),
            "settled": len(tasks) - abandoned_tasks,
            "abandoned": abandoned_tasks + abandoned_requests,
        }

    async def interrupt_active_turns_and_wait(
        self,
        *,
        reason: str,
        timeout_seconds: float,
    ) -> dict[str, int]:
        """Make active durable turns reclaimable without publishing cancellation."""

        store = self._direct_turn_store
        if store is None:
            return await self.cancel_active_turns_and_wait(timeout_seconds=timeout_seconds)
        interrupted = 0
        tasks: list[asyncio.Task[None]] = []
        async with self._admission_drain_lock:
            active = [
                (conversation_id, task)
                for conversation_id, task in self._active_turns.items()
                if not task.done()
            ]
            for conversation_id, task in active:
                request_id = self._durable_request_by_conversation.get(conversation_id)
                fence = self._durable_fences.get(request_id or "")
                if request_id is None or fence is None:
                    tasks.append(task)
                    task.cancel()
                    continue
                async with self._active_streams_lock:
                    stream = self._active_streams.get(conversation_id)
                    if stream is not None and stream.content:
                        try:
                            conversation, session, agent, _ = await self._load_conversation_runtime(
                                conversation_id
                            )
                            persisted_content, _, _ = await self._persist_cancelled_active_stream(
                                conversation_id=conversation_id,
                                session=session,
                                message_id=stream.message_id,
                                turn_id=stream.turn_id,
                                user_email=conversation.user_email,
                                agent=agent,
                                finish_reason="controller_interrupted",
                                clear_on_success=False,
                                stream_snapshot=stream,
                            )
                        except Exception:
                            persisted_content = None
                            logger.exception(
                                "turn_scheduler: failed to checkpoint interrupted assistant stream",
                                extra={"extra_data": {"conversation_id": conversation_id}},
                            )
                        if persisted_content is None:
                            # Never release a durable request after discarding visible
                            # output. The controller remains owner and shutdown reports
                            # the turn as abandoned rather than allowing a lossy retry.
                            continue
                        append_phase = fence.user_append_phase or "user_append_pending"
                        recovered = await store.settle_transient_failure(
                            request_id,
                            lease=fence.lease,
                            outcome={
                                "phase": append_phase,
                                "user_append_phase": append_phase,
                                **(
                                    {"user_append_session_id": fence.user_append_session_id}
                                    if fence.user_append_session_id
                                    else {}
                                ),
                                "interruption_reason": reason,
                                "source_phase": fence.last_phase,
                                "source_metadata": fence.last_metadata or {},
                            },
                        )
                        if (
                            recovered is None
                            or recovered.status != DirectTurnStatus.RECOVERABLE.value
                        ):
                            continue
                        self._interrupted_durable_requests.add(request_id)
                        interrupted += 1
                        tasks.append(task)
                        task.cancel()
                        self._active_streams.pop(conversation_id, None)
                        continue
                append_phase = fence.user_append_phase or "user_append_pending"
                recovered = await store.settle_transient_failure(
                    request_id,
                    lease=fence.lease,
                    outcome={
                        "phase": append_phase,
                        "user_append_phase": append_phase,
                        **(
                            {"user_append_session_id": fence.user_append_session_id}
                            if fence.user_append_session_id
                            else {}
                        ),
                        "interruption_reason": reason,
                        "source_phase": fence.last_phase,
                        "source_metadata": fence.last_metadata or {},
                    },
                )
                if recovered is None or recovered.status != DirectTurnStatus.RECOVERABLE.value:
                    continue
                self._interrupted_durable_requests.add(request_id)
                interrupted += 1
                tasks.append(task)
                task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        else:
            pending = set()
        return {
            "requested": len(tasks),
            "interrupted": interrupted,
            "settled": len(tasks) - len(pending),
            "abandoned": len(pending),
        }

    async def cancel_turn(
        self,
        conversation_id: str,
        *,
        clear_queue: bool = True,
    ) -> bool:
        """Cancel the active turn and its delegated child sub-sessions.

        ``clear_queue`` is reserved for explicit user stop commands. UI stop
        controls should cancel only the active turn so already queued messages
        remain pending and run after the cancelled turn settles.

        Managed conversations are independent root conversations. Cancelling a
        supervising turn must not cancel healthy managed work; callers that
        explicitly interrupt a managed conversation cancel its target directly.
        """
        durable_cancelled = False
        cancelled_active_request_id: str | None = None
        if self._direct_turn_store is not None:
            active_durable_request_id = self._durable_request_by_conversation.get(conversation_id)
            for row in await self._direct_turn_store.list_conversation_pending(conversation_id):
                if (
                    not clear_queue
                    and row.status
                    not in {
                        DirectTurnStatus.CLAIMED.value,
                        DirectTurnStatus.RUNNING.value,
                        DirectTurnStatus.ABSORBING.value,
                    }
                    and row.request_id != active_durable_request_id
                ):
                    continue
                cancel_result = await self._direct_turn_store.request_cancel(row.request_id)
                durable_cancelled = cancel_result is not None or durable_cancelled
                if cancel_result is not None and row.status in {
                    DirectTurnStatus.CLAIMED.value,
                    DirectTurnStatus.RUNNING.value,
                    DirectTurnStatus.ABSORBING.value,
                }:
                    cancelled_active_request_id = row.request_id
        queued_to_cancel: list[_QueuedMessage] = []
        async with self._turn_lock(conversation_id):
            control = self._turn_controls.get(conversation_id)
            queue = self._queued_messages.get(conversation_id)
            if clear_queue and queue is not None:
                queued_to_cancel = list(queue)
                queue.clear()
            if control is not None:
                if isinstance(control, asyncio.Event):
                    control.set()
                else:
                    control.cancel_event.set()
                active_task = self._active_turns.get(conversation_id)
                if active_task is not None and not active_task.done():
                    active_task.cancel()
            session_id = self._turn_sessions.get(conversation_id)
        cleared_queue = bool(queued_to_cancel)
        queued_delivery_ids: list[str] = []
        for queued in queued_to_cancel:
            if queued.follow_up is not None:
                await self._mark_follow_up_intent(
                    conversation_id,
                    queued.follow_up.follow_up_id,
                    status="failed",
                    error="Queued follow-up was cancelled.",
                )
            delivery_id = getattr(queued, "delivery_id", None)
            if delivery_id:
                queued_delivery_ids.append(delivery_id)
            queued_turn_id = getattr(queued, "turn_id", None)
            await self._publish_turn_error(
                conversation_id,
                getattr(queued, "session_id", None) or "",
                TurnError(
                    code="queued_turn_cancelled",
                    message="The queued turn was cancelled.",
                    recoverable=True,
                    turn_id=queued_turn_id,
                ),
                turn_id=queued_turn_id,
                system_initiated=bool(getattr(queued, "system_initiated", False)),
                channel_deliverable=bool(getattr(queued, "channel_deliverable", False)),
                delivery_id=getattr(queued, "delivery_id", None),
                delivery_fallback_text=getattr(queued, "delivery_fallback_text", None),
                turn_observers=tuple(getattr(queued, "turn_observers", ()) or ()),
            )
        await self._suppress_channel_delivery_ids(
            queued_delivery_ids,
            selected_delivery_id=None,
            reason="cleared queued follow-up turn",
        )
        if cleared_queue:
            await self._notify_queue_updated(conversation_id)
        cluster_signals = getattr(self, "cluster_signals", None)
        if cancelled_active_request_id is not None and cluster_signals is not None:
            from cognis.core.cluster_signals import ClusterSignalKind, ClusterSignalScope

            await cluster_signals.publish(
                ClusterSignalKind.TURN_CANCEL_REQUESTED,
                scope=ClusterSignalScope(
                    conversation_id=conversation_id,
                    direct_request_id=cancelled_active_request_id,
                ),
                revision=datetime.now(UTC),
            )
        # Also cancel child sub-sessions via the agent loop
        if session_id:
            cancelled = await self._agent_loop.cancel_children(session_id)
            if cancelled:
                logger.info(
                    "turn_scheduler: cancelled child sub-sessions",
                    extra={"extra_data": {"count": cancelled, "session_id": session_id}},
                )
        return control is not None or cleared_queue or durable_cancelled

    def has_active_turn(self, conversation_id: str) -> bool:
        """Check if a turn is currently active for a conversation."""
        active = self._active_turns.get(conversation_id)
        return active is not None and not active.done()

    def relay_generation_context(self, conversation_id: str) -> RelayGenerationContext | None:
        """Return the immutable generation captured from the active durable claim."""
        return self._relay_generation_contexts.get(conversation_id)

    async def durable_relay_generation_context(
        self, conversation_id: str
    ) -> RelayGenerationContext | None:
        """Resolve the current PostgreSQL-owned relay generation."""
        if self._direct_turn_store is None:
            return None
        row = await self._direct_turn_store.get_conversation_active(conversation_id)
        if (
            row is None
            or not row.session_id
            or not row.turn_id
            or not row.owner_controller_id
            or not row.owner_incarnation_id
            or row.fencing_token is None
        ):
            return None
        return RelayGenerationContext(
            direct_request_id=row.request_id,
            turn_id=row.turn_id,
            session_id=row.session_id,
            conversation_id=row.conversation_id,
            owner_controller_id=row.owner_controller_id,
            owner_incarnation_id=row.owner_incarnation_id,
            fencing_token=row.fencing_token,
        )

    async def durable_terminal_relay_generation_context(
        self, request_id: str
    ) -> RelayGenerationContext | None:
        """Resolve one exact terminal PostgreSQL-owned relay generation."""
        if self._direct_turn_store is None:
            return None
        row = await self._direct_turn_store.get(request_id)
        try:
            status = DirectTurnStatus(row.status) if row is not None else None
        except (TypeError, ValueError):
            return None
        if (
            row is None
            or status not in TERMINAL_STATUSES
            or not row.session_id
            or not row.turn_id
            or not row.owner_controller_id
            or not row.owner_incarnation_id
            or row.fencing_token is None
        ):
            return None
        return RelayGenerationContext(
            direct_request_id=row.request_id,
            turn_id=row.turn_id,
            session_id=row.session_id,
            conversation_id=row.conversation_id,
            owner_controller_id=row.owner_controller_id,
            owner_incarnation_id=row.owner_incarnation_id,
            fencing_token=row.fencing_token,
        )

    async def durable_running_turn_state(self, conversation_id: str) -> dict[str, Any] | None:
        """Return cluster-authoritative active state, enriched by local runtime when owned here."""

        if self._direct_turn_store is None:
            return self.running_turn_state(conversation_id)
        row = await self._direct_turn_store.get_conversation_active(conversation_id)
        if row is None:
            return None
        return self._durable_runtime_state_from_row(row, self.running_turn_state(conversation_id))

    @staticmethod
    def _durable_runtime_state_from_row(
        row: DirectTurnRequestRow, local: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Map one durable direct-turn row to public runtime state."""

        status = (
            "cancelling"
            if row.cancel_requested_at is not None
            else "starting"
            if row.status == DirectTurnStatus.CLAIMED.value
            else "waiting"
            if row.status == DirectTurnStatus.ABSORBING.value
            else "running"
        )
        return {
            "turn_id": row.turn_id,
            "session_id": row.session_id or "",
            "status": status,
            "chat_mode": local.get("chat_mode") if local else None,
            "chat_mode_source": local.get("chat_mode_source") if local else None,
            "started_at": row.started_at.isoformat() if row.started_at is not None else None,
            "updated_at": row.updated_at.isoformat(),
        }

    async def durable_running_turn_states(
        self, conversation_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return cluster-authoritative active state for multiple conversations."""

        if self._direct_turn_store is None:
            return {
                conversation_id: state
                for conversation_id in conversation_ids
                if (state := self.running_turn_state(conversation_id)) is not None
            }
        rows = await self._direct_turn_store.list_conversations_active(conversation_ids)
        return {
            conversation_id: self._durable_runtime_state_from_row(
                row, self.running_turn_state(conversation_id)
            )
            for conversation_id, row in rows.items()
        }

    def active_turn_checkpoint(self, conversation_id: str) -> dict[str, str | None] | None:
        """Return active turn identity used to fork from the last completed checkpoint."""

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        control = self._turn_controls.get(conversation_id)
        return {
            "session_id": self._turn_sessions.get(conversation_id),
            "turn_id": control.turn_id if control is not None else None,
        }

    def running_turn_state(self, conversation_id: str) -> dict[str, Any] | None:
        """Return visible running-turn state for a conversation.

        A turn may remain internally busy for cleanup and queue draining after
        the user-visible work has settled.
        """

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        control = self._turn_controls.get(conversation_id)
        if control and control.settled:
            return None
        return {
            "turn_id": control.turn_id if control else None,
            "chat_mode": control.chat_mode if control else None,
            "chat_mode_source": control.chat_mode_source if control else None,
        }

    def has_running_turn(self, conversation_id: str) -> bool:
        """Check if a turn is still visibly running for the user."""

        return self.running_turn_state(conversation_id) is not None

    async def wait_for_turn(
        self,
        conversation_id: str,
        *,
        timeout_seconds: int | None = None,
    ) -> TurnResult | TurnError | None:
        """Wait for a currently running turn, or return immediately when idle."""

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TurnResult | TurnError] = loop.create_future()
        self._turn_waiters[conversation_id].append(future)
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=max(1, int(timeout_seconds)),
            )
        except TimeoutError:
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._turn_waiters[conversation_id].remove(future)

    def turn_scope_change_generation(self, conversation_id: str) -> int:
        """Return the local generation for durable turn-state invalidations."""

        return self._turn_scope_change_generations.get(conversation_id, 0)

    async def wait_for_turn_scope_change(
        self,
        conversation_id: str,
        *,
        after_generation: int,
        timeout_seconds: float,
    ) -> bool:
        """Wait for a local or remote durable turn-state invalidation."""

        self._turn_scope_change_waiters[conversation_id] += 1
        event = self._turn_scope_change_events.setdefault(conversation_id, asyncio.Event())
        try:
            if self.turn_scope_change_generation(conversation_id) > after_generation:
                return True
            event.clear()
            if self.turn_scope_change_generation(conversation_id) > after_generation:
                return True
            try:
                await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_seconds))
            except TimeoutError:
                return False
            return self.turn_scope_change_generation(conversation_id) > after_generation
        finally:
            remaining = self._turn_scope_change_waiters.get(conversation_id, 1) - 1
            if remaining > 0:
                self._turn_scope_change_waiters[conversation_id] = remaining
            else:
                self._turn_scope_change_waiters.pop(conversation_id, None)
                self._turn_scope_change_events.pop(conversation_id, None)
                self._turn_scope_change_generations.pop(conversation_id, None)

    def _signal_turn_scope_change(self, conversation_id: str) -> None:
        """Wake waiters that must re-read authoritative durable turn state."""

        if self._turn_scope_change_waiters.get(conversation_id, 0) <= 0:
            return
        self._turn_scope_change_generations[conversation_id] += 1
        event = self._turn_scope_change_events.get(conversation_id)
        if event is not None:
            event.set()

    def boundary_action_generation(self, conversation_id: str) -> int:
        """Return the current actionable same-turn boundary generation."""

        return self._boundary_action_generations.get(conversation_id, 0)

    def signal_actionable_boundary_event(self, conversation_id: str) -> None:
        """Wake sibling joined waits without replacing the settled tool result."""

        self._boundary_action_generations[conversation_id] += 1
        self._signal_boundary_input_change(conversation_id)

    async def wait_for_boundary_input(
        self,
        conversation_id: str,
        *,
        after_generation: int | None = None,
    ) -> str:
        """Wait for actionable queued intake or a same-turn boundary event.

        Queue mutations signal an event, while the queue/store remains the
        authority. Rechecking before and after ``clear()`` avoids a lost wakeup
        when admission races waiter setup. Multiple joined managed waits may
        share this primitive without consuming or leasing the queued input.
        """

        event = self._boundary_input_events.setdefault(conversation_id, asyncio.Event())
        self._boundary_input_waiters[conversation_id] += 1
        try:
            while True:
                queued_reason = await self._actionable_boundary_queue_reason(conversation_id)
                if queued_reason is not None:
                    return queued_reason
                if (
                    after_generation is not None
                    and self.boundary_action_generation(conversation_id) > after_generation
                ):
                    return "managed_sibling_settled"
                event.clear()
                queued_reason = await self._actionable_boundary_queue_reason(conversation_id)
                if queued_reason is not None:
                    return queued_reason
                if (
                    after_generation is not None
                    and self.boundary_action_generation(conversation_id) > after_generation
                ):
                    return "managed_sibling_settled"
                await event.wait()
        finally:
            remaining = self._boundary_input_waiters.get(conversation_id, 1) - 1
            if remaining > 0:
                self._boundary_input_waiters[conversation_id] = remaining
            else:
                self._boundary_input_waiters.pop(conversation_id, None)
                if self._boundary_input_events.get(conversation_id) is event:
                    self._boundary_input_events.pop(conversation_id, None)

    async def _actionable_boundary_queue_reason(self, conversation_id: str) -> str | None:
        """Return why the next absorbable batch should resume the active turn."""

        if self._direct_turn_store is not None:
            active_request_id = self._durable_request_by_conversation.get(conversation_id)
            rows = await self._direct_turn_store.list_conversation_pending(conversation_id)
            for row in rows:
                if row.request_id == active_request_id or row.status in {
                    DirectTurnStatus.CLAIMED.value,
                    DirectTurnStatus.RUNNING.value,
                    DirectTurnStatus.ABSORBING.value,
                }:
                    continue
                metadata = row.payload.get("metadata") or {}
                if row.status != DirectTurnStatus.QUEUED.value or not metadata.get("absorbable"):
                    break
                if not metadata.get("system_initiated"):
                    return "queued_user_input"
                if self._follow_up_is_actionable_boundary_event(metadata.get("follow_up")):
                    return "queued_completion"
            return None

        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return None
        for queued in queue:
            if not self._queued_message_is_absorbable(queued):
                break
            if not queued.system_initiated:
                return "queued_user_input"
            if self._follow_up_is_actionable_boundary_event(queued.follow_up):
                return "queued_completion"
        return None

    @staticmethod
    def _follow_up_is_actionable_boundary_event(follow_up: Any) -> bool:
        """Exclude automatic continuation noise from cooperative wait wakeups."""

        if follow_up is None:
            return False
        if isinstance(follow_up, dict):
            return follow_up.get("origin_kind") != FollowUpOriginKind.CONTINUATION.value
        return getattr(follow_up, "origin_kind", None) != FollowUpOriginKind.CONTINUATION

    def _signal_boundary_input_change(self, conversation_id: str) -> None:
        """Wake local boundary-input waiters after any authoritative queue change."""

        event = self._boundary_input_events.get(conversation_id)
        if event is not None:
            event.set()

    def attach_turn_observer(
        self,
        conversation_id: str,
        observer: TurnObserver,
        *,
        turn_id: str | None = None,
    ) -> bool:
        """Attach a live observer to one active turn.

        Returns true only when this call added the observer, so callers can
        safely detach it when their bounded wait ends. Queued managed turns
        receive their observer during admission; attaching one while a queue
        item is being promoted would otherwise leave a dequeue handoff race.
        """

        control = self._turn_controls.get(conversation_id)
        if (
            control is not None
            and not control.settled
            and (turn_id is None or control.turn_id == turn_id)
        ):
            if observer not in control.turn_observers:
                control.turn_observers.append(observer)
                return True
            return False
        return False

    def detach_turn_observer(
        self,
        conversation_id: str,
        observer: TurnObserver,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Remove a wait-scoped observer from its active turn."""

        control = self._turn_controls.get(conversation_id)
        if control is not None and (turn_id is None or control.turn_id == turn_id):
            with contextlib.suppress(ValueError):
                control.turn_observers.remove(observer)

    def active_turn_id(self, conversation_id: str) -> str | None:
        """Return active turn ID for a conversation, if any."""

        control = self._turn_controls.get(conversation_id)
        active = self._active_turns.get(conversation_id)
        if active is None or active.done() or control is None:
            return None
        return control.turn_id

    def queued_count(self, conversation_id: str) -> int:
        """Return the number of queued messages for a conversation."""
        if self._direct_turn_store is not None:
            return len(self._durable_queue_cache.get(conversation_id, ()))
        return len(self._queued_messages.get(conversation_id, []))

    def queued_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return safe metadata for pending queued messages."""
        if self._direct_turn_store is not None:
            return list(self._durable_queue_cache.get(conversation_id, ()))
        return [
            queued.snapshot(position=index + 1)
            for index, queued in enumerate(self._queued_messages.get(conversation_id, []))
        ]

    async def get_queued_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        if self._direct_turn_store is None:
            return self.queued_messages(conversation_id)
        rows = [
            row
            for row in await self._direct_turn_store.list_conversation_pending(conversation_id)
            if row.status
            in {
                DirectTurnStatus.QUEUED.value,
                DirectTurnStatus.RECOVERABLE.value,
            }
        ]
        messages = [
            {
                "queue_id": row.request_id,
                "request_id": row.request_id,
                "turn_id": row.turn_id,
                "client_message_id": (row.payload.get("metadata") or {}).get("client_message_id"),
                "content": row.payload.get("content", ""),
                "attachments": row.payload.get("attachments", []),
                "queued_at": row.created_at.isoformat(),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "status": row.status,
                "cancel_requested": row.cancel_requested_at is not None,
                "position": index + 1,
                **_automatic_continuation_queue_fields(
                    (row.payload.get("metadata") or {}).get("follow_up")
                ),
            }
            for index, row in enumerate(rows)
        ]
        self._durable_queue_cache[conversation_id] = messages
        return messages

    def _remove_durable_queue_cache_entry(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        """Remove a request once durable ownership moves it out of queued state."""

        cached = self._durable_queue_cache.get(conversation_id)
        if cached is None:
            return
        self._durable_queue_cache[conversation_id] = [
            item for item in cached if item.get("request_id") != request_id
        ]

    async def _load_turn_limits(self) -> tuple[int, int]:
        """Load user/conversation turn limits from DB-backed settings."""
        if not callable(self._session_factory):
            return DEFAULT_MAX_ACTIVE_TURNS_PER_USER, DEFAULT_MAX_QUEUED_MESSAGES
        async with self._session_factory() as db_session:
            max_active_turns_raw = await get_setting_value(
                db_session,
                "session.max_active_turns_per_user",
                DEFAULT_MAX_ACTIVE_TURNS_PER_USER,
            )
            max_queued_messages_raw = await get_setting_value(
                db_session,
                "session.max_queued_messages",
                DEFAULT_MAX_QUEUED_MESSAGES,
            )
        return (
            _positive_int_setting(max_active_turns_raw, DEFAULT_MAX_ACTIVE_TURNS_PER_USER),
            _positive_int_setting(max_queued_messages_raw, DEFAULT_MAX_QUEUED_MESSAGES),
        )

    async def _load_idle_checkpoint_settings(self) -> tuple[int, int]:
        """Load idle checkpoint compaction settings for long-lived chats."""

        if not callable(self._session_factory):
            return (
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            )
        async with self._session_factory() as db_session:
            threshold_raw = await get_setting_value(
                db_session,
                "session.long_lived_chat_idle_compaction_seconds",
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
            )
            min_events_raw = await get_setting_value(
                db_session,
                "session.long_lived_chat_idle_compaction_min_events",
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            )
        return (
            _non_negative_int_setting(
                threshold_raw,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
            ),
            _positive_int_setting(
                min_events_raw,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            ),
        )

    def _conversation_idle_seconds(
        self,
        conversation: ConversationModel,
        session: SessionModel,
        *,
        now: datetime,
    ) -> float | None:
        """Return idle age before the incoming turn touches the conversation."""

        candidates = (
            conversation.last_message_at,
            session.idle_since,
            session.updated_at,
            session.started_at,
            conversation.updated_at,
            conversation.created_at,
        )
        for candidate in candidates:
            normalized = _normalize_utc(candidate)
            if normalized is None:
                continue
            return max(0.0, (now - normalized).total_seconds())
        return None

    async def _maybe_idle_checkpoint_compact(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
    ) -> SessionModel:
        """Rotate long-lived chats that have been idle before handling the new turn."""

        if not is_long_lived_chat_context(getattr(conversation, "context", None)):
            return session
        threshold_seconds, min_events = await self._load_idle_checkpoint_settings()
        if threshold_seconds <= 0:
            return session
        idle_seconds = self._conversation_idle_seconds(
            conversation,
            session,
            now=datetime.now(UTC),
        )
        if idle_seconds is None or idle_seconds < threshold_seconds:
            return session
        lock = self._idle_checkpoint_locks.setdefault(conversation.conversation_id, asyncio.Lock())
        if lock.locked():
            return session
        async with lock:
            try:
                if self._agent_loop.session_is_locked(session.session_id):
                    return session
            except AttributeError:
                return session
            try:
                new_session = await self._agent_loop.run_idle_checkpoint_compaction(
                    conversation=conversation,
                    session=session,
                    agent=agent,
                    min_events=min_events,
                )
            except Exception:
                logger.warning(
                    "turn_scheduler: idle checkpoint compaction failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation.conversation_id,
                            "session_id": session.session_id,
                        }
                    },
                    exc_info=True,
                )
                return session
            if new_session is None:
                return session
            new_session = cast(SessionModel, new_session)
            conversation.active_session_id = new_session.session_id
            if len(self._idle_checkpoint_locks) > _MAX_DEFERRED_LOCKS:
                stale_ids = [
                    cid
                    for cid, idle_lock in self._idle_checkpoint_locks.items()
                    if not idle_lock.locked()
                ]
                for cid in stale_ids[
                    : max(0, len(self._idle_checkpoint_locks) - _MAX_DEFERRED_LOCKS)
                ]:
                    self._idle_checkpoint_locks.pop(cid, None)
            return new_session

    async def _prepare_idle_checkpoint_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        checkpoint_conversation: ConversationModel,
        checkpoint_session: SessionModel,
    ) -> tuple[ConversationModel, SessionModel]:
        """Run deferred idle compaction and bind the admitted turn to its session."""

        checkpoint_result = await self._maybe_idle_checkpoint_compact(
            conversation=checkpoint_conversation,
            session=checkpoint_session,
            agent=agent,
        )
        if checkpoint_result.session_id == checkpoint_session.session_id:
            return conversation, session

        conversation.active_session_id = checkpoint_result.session_id
        self._turn_sessions[conversation.conversation_id] = checkpoint_result.session_id
        return conversation, checkpoint_result

    async def _prepare_idle_checkpoint_turn_cancellation_safe(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        checkpoint_conversation: ConversationModel,
        checkpoint_session: SessionModel,
    ) -> tuple[ConversationModel, SessionModel, bool]:
        """Finish a started checkpoint before honoring turn cancellation."""

        checkpoint_task = asyncio.create_task(
            self._prepare_idle_checkpoint_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                checkpoint_conversation=checkpoint_conversation,
                checkpoint_session=checkpoint_session,
            )
        )
        cancellation_requested = False
        while True:
            try:
                result = await asyncio.shield(checkpoint_task)
                break
            except asyncio.CancelledError:
                if checkpoint_task.cancelled():
                    raise
                # Rotation commits the active-session pointer before all
                # Intaris continuity events and cache state are seeded. Keep
                # shielding through repeated stop requests until checkpoint
                # work reaches that consistency boundary.
                cancellation_requested = True
        conversation, session = result
        return conversation, session, cancellation_requested

    async def _load_visible_conversation_title(
        self,
        conversation_id: str,
        fallback: str | None,
    ) -> str | None:
        """Return the latest persisted title, falling back to the in-memory title."""

        try:
            async with self._session_factory() as db_session:
                row = await queries.get_conversation(db_session, conversation_id)
                return row.title if row is not None else fallback
        except Exception:
            logger.debug(
                "turn_scheduler: failed to reload conversation title",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
            return fallback

    async def _adopt_late_intaris_title(
        self,
        conversation: ConversationModel,
        session: SessionModel,
    ) -> bool:
        """Adopt a title that Intaris produced after bootstrap reasoning."""

        intaris_session_id = getattr(session, "intaris_session_id", None) or session.session_id
        if not intaris_session_id:
            return False
        if not hasattr(conversation, "title_source"):
            return False
        if not can_adopt_intaris_title(conversation):
            return False

        try:
            intaris_session = await self._providers.guardrails.get_session(intaris_session_id)
            title = intaris_session.title
            if not title:
                return False
            async with self._session_factory() as db_session:
                changed = await sync_intaris_title(
                    db_session,
                    conversation,
                    title,
                    updated_at=intaris_session.updated_at,
                )
                if changed:
                    await db_session.commit()
                return changed
        except Exception:
            logger.debug(
                "turn_scheduler: failed to adopt late Intaris title",
                extra={
                    "extra_data": {
                        "conversation_id": conversation.conversation_id,
                        "session_id": session.session_id,
                        "intaris_session_id": intaris_session_id,
                    }
                },
                exc_info=True,
            )
            return False

    async def _handle_conversation_updated(self, event: Event) -> None:
        """Remember realtime title updates already published via EventBus."""

        conversation_id = event.data.get("conversation_id")
        title = event.data.get("title")
        if isinstance(conversation_id, str) and isinstance(title, str) and title.strip():
            self._published_title_updates[conversation_id] = title

    async def _handle_cluster_scope_invalidated(self, event: Event) -> None:
        """Wake durable queue waiters after a remote controller chat mutation."""

        scope = event.data.get("scope")
        if not isinstance(scope, dict):
            return
        conversation_id = scope.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            if event.data.get("kind") == "turn_cancel_requested":
                request_id = scope.get("direct_request_id")
                if isinstance(request_id, str):
                    await self._cancel_local_active_turn(conversation_id, request_id=request_id)
            elif event.data.get("kind") == "chat_scope_changed":
                await self._cancel_local_turn_if_durably_requested(conversation_id)
            self._signal_turn_scope_change(conversation_id)
            self._signal_boundary_input_change(conversation_id)

    async def _cancel_local_turn_if_durably_requested(self, conversation_id: str) -> bool:
        """Heal a missed cancel signal from the durable direct-turn record."""

        request_id = self._durable_request_by_conversation.get(conversation_id)
        if request_id is None or self._direct_turn_store is None:
            return False
        get_request = getattr(self._direct_turn_store, "get", None)
        if not callable(get_request):
            return False
        row = await get_request(request_id)
        if row is None or row.cancel_requested_at is None:
            return False
        return await self._cancel_local_active_turn(conversation_id, request_id=request_id)

    async def _cancel_local_active_turn(self, conversation_id: str, *, request_id: str) -> bool:
        """Interrupt an active turn owned by this controller only."""

        async with self._turn_lock(conversation_id):
            if self._durable_request_by_conversation.get(conversation_id) != request_id:
                return False
            control = self._turn_controls.get(conversation_id)
            if control is None:
                return False
            if isinstance(control, asyncio.Event):
                control.set()
            else:
                control.cancel_event.set()
            active_task = self._active_turns.get(conversation_id)
            if active_task is not None and not active_task.done():
                active_task.cancel()
            return True

    async def cancel_queued_message(self, conversation_id: str, queue_id: str) -> bool:
        if self._direct_turn_store is not None:
            row = await self._direct_turn_store.get(queue_id)
            if (
                row is None
                or row.conversation_id != conversation_id
                or row.status
                not in {
                    DirectTurnStatus.QUEUED.value,
                    DirectTurnStatus.RECOVERABLE.value,
                }
            ):
                return False
            result = await self._direct_turn_store.request_cancel(queue_id)
            if result is None:
                return False
            await self._notify_queue_updated(conversation_id)
            return True
        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return False
        for index, queued in enumerate(queue):
            if queued.queue_id != queue_id:
                continue
            del queue[index]
            await self._notify_queue_updated(conversation_id)
            if queued.follow_up is not None:
                await self._mark_follow_up_intent(
                    conversation_id,
                    queued.follow_up.follow_up_id,
                    status="failed",
                    error="Queued follow-up was cancelled.",
                )
            if queued.delivery_id:
                await self._suppress_channel_delivery_ids(
                    [queued.delivery_id],
                    selected_delivery_id=None,
                    reason="cancelled queued follow-up turn",
                )
            return True
        return False

    async def update_queued_message(
        self, conversation_id: str, queue_id: str, *, content: str
    ) -> dict[str, Any] | None:
        if self._direct_turn_store is not None:
            row = await self._direct_turn_store.get(queue_id)
            if row is None or row.conversation_id != conversation_id:
                return None
            payload = dict(row.payload)
            payload["content"] = content
            edited = await self._direct_turn_store.edit(
                queue_id,
                payload=payload,
                payload_version=row.payload_version,
                expected_payload_hash=row.payload_hash,
            )
            if edited is None:
                return None
            await self._notify_queue_updated(conversation_id)
            return next(
                (
                    item
                    for item in await self.get_queued_messages(conversation_id)
                    if item["queue_id"] == queue_id
                ),
                None,
            )
        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return None
        for index, queued in enumerate(queue):
            if queued.queue_id != queue_id:
                continue
            queued.content = content
            queued.updated_at = _utcnow()
            snapshot = queued.snapshot(position=index + 1)
            await self._notify_queue_updated(conversation_id)
            return snapshot
        return None

    @staticmethod
    def _new_queue_id() -> str:
        return f"qmsg_{uuid.uuid4().hex}"

    @staticmethod
    def new_turn_id() -> str:
        """Reserve a stable turn identity before scheduler admission."""

        return f"turn_{uuid.uuid4().hex[:12]}"

    async def _notify_queue_updated(
        self,
        conversation_id: str,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        self._signal_boundary_input_change(conversation_id)
        messages = await self.get_queued_messages(conversation_id)
        observers = self._iter_observers(conversation_id, turn_observers=turn_observers)
        await asyncio.gather(
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_queued,
                    conversation_id,
                    len(messages),
                )
                for observer in observers
            ),
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_queued_messages,
                    conversation_id,
                    messages,
                )
                for observer in observers
                if callable(getattr(observer, "on_queued_messages", None))
            ),
        )

    async def _consume_queued_batch_for_active_turn(
        self,
        conversation_id: str,
        *,
        reason: str,
    ) -> list[dict[str, Any]]:
        """Drain the currently queued inbox batch for an active direct turn."""

        if self._direct_turn_store is not None:
            active_request_id = self._durable_request_by_conversation.get(conversation_id)
            fence = (
                self._durable_fences.get(active_request_id)
                if active_request_id is not None
                else None
            )
            control = self._turn_controls.get(conversation_id)
            if fence is None or control is None or self._controller_runtime is None:
                return []
            rows = await self._direct_turn_store.list_conversation_pending(conversation_id)
            durable_payloads: list[dict[str, Any]] = []
            for row in rows:
                if row.request_id == active_request_id:
                    continue
                metadata = row.payload.get("metadata") or {}
                if row.status != DirectTurnStatus.QUEUED.value or not metadata.get("absorbable"):
                    break
                absorbing = await self._direct_turn_store.begin_absorb(
                    row.request_id,
                    lease=fence.lease,
                    controller_id=self._controller_runtime.controller_id,
                    incarnation_id=self._controller_runtime.incarnation_id,
                    absorbed_by_turn_id=control.turn_id or cast(str, active_request_id),
                    session_id=self._turn_sessions.get(conversation_id),
                )
                if absorbing is None:
                    break
                materialized = await self._direct_turn_store.materialize_claimed_payload(
                    row.request_id,
                    lease=fence.lease,
                    artifact_store=self._artifact_store,
                )
                if materialized is None:
                    raise StaleDirectTurnOwner(row.request_id)
                for observer in self._durable_turn_observers.get(row.request_id, ()):
                    if observer not in control.turn_observers:
                        control.turn_observers.append(observer)
                raw_follow_up = materialized.metadata.get("follow_up")
                follow_up = (
                    parse_follow_up_metadata(raw_follow_up)
                    if isinstance(raw_follow_up, dict)
                    else None
                )
                if follow_up is not None:
                    control.absorbed_follow_up_ids.add(follow_up.follow_up_id)
                outbound_attachments = materialized.metadata.get("outbound_attachments")
                if isinstance(outbound_attachments, list):
                    control.absorbed_outbound_attachments.extend(outbound_attachments)
                if materialized.metadata.get("channel_deliverable"):
                    control.absorbed_channel_deliverable = True
                    if materialized.channel_delivery is not None:
                        control.channel_delivery = materialized.channel_delivery
                delivery_id = materialized.metadata.get("delivery_id")
                delivery_fallback_text = materialized.metadata.get("delivery_fallback_text")
                if isinstance(delivery_id, str) and delivery_id:
                    if control.active_delivery_id or control.absorbed_delivery_id:
                        control.suppressed_channel_delivery_ids.append(delivery_id)
                    else:
                        control.absorbed_delivery_id = delivery_id
                        if isinstance(delivery_fallback_text, str):
                            control.absorbed_delivery_fallback_text = delivery_fallback_text
                elif (
                    isinstance(delivery_fallback_text, str)
                    and not control.absorbed_delivery_fallback_text
                ):
                    control.absorbed_delivery_fallback_text = delivery_fallback_text
                durable_payloads.append(
                    {
                        "durable_request_id": row.request_id,
                        "queue_id": row.request_id,
                        "client_message_id": materialized.metadata.get("client_message_id"),
                        "content": materialized.content,
                        "attachments": materialized.attachments,
                        "attachment_notice": materialized.metadata.get("attachment_notice"),
                        "attachment_context": materialized.metadata.get("attachment_context"),
                        "system_initiated": bool(materialized.metadata.get("system_initiated")),
                        "intention_eligible": bool(
                            materialized.metadata.get("intention_eligible", True)
                        ),
                        "follow_up": follow_up,
                    }
                )
                attachment_notice = materialized.metadata.get("attachment_notice")
                if isinstance(attachment_notice, str) and attachment_notice:
                    await self._notify_observers_system_message(
                        conversation_id,
                        attachment_notice,
                        turn_observers=control.turn_observers,
                    )
                if not materialized.metadata.get("system_initiated"):
                    await self._event_bus.publish(
                        Event(
                            type=EventType.USER_MESSAGE,
                            data=_user_message_event_payload(
                                conversation_id=conversation_id,
                                session_id=self._turn_sessions.get(conversation_id),
                                content=materialized.content,
                                attachments=strip_attachment_payload_bytes(
                                    materialized.attachments
                                ),
                                queue_id=row.request_id,
                                client_message_id=materialized.metadata.get("client_message_id"),
                            ),
                        )
                    )
            if durable_payloads:
                await self._suppress_absorbed_channel_delivery_intents(
                    control,
                    selected_delivery_id=(
                        control.active_delivery_id or control.absorbed_delivery_id
                    ),
                )
                await self._notify_queue_updated(
                    conversation_id,
                    turn_observers=control.turn_observers,
                )
            return durable_payloads

        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return []
        control = self._turn_controls.get(conversation_id)
        if control is None:
            return []

        batch: list[_QueuedMessage] = []
        while queue and self._queued_message_is_absorbable(queue[0]):
            batch.append(queue.popleft())
        if not batch:
            return []

        self._merge_active_turn_observers(control.turn_observers, batch)

        payloads: list[dict[str, Any]] = []
        for queued in batch:
            if queued.follow_up is not None:
                control.absorbed_follow_up_ids.add(queued.follow_up.follow_up_id)
            if queued.outbound_attachments:
                control.absorbed_outbound_attachments.extend(queued.outbound_attachments)
            if queued.channel_deliverable:
                control.absorbed_channel_deliverable = True
            if queued.delivery_id:
                if control.active_delivery_id or control.absorbed_delivery_id:
                    control.suppressed_channel_delivery_ids.append(queued.delivery_id)
                else:
                    control.absorbed_delivery_id = queued.delivery_id
                    if queued.delivery_fallback_text:
                        control.absorbed_delivery_fallback_text = queued.delivery_fallback_text
            elif queued.delivery_fallback_text and not control.absorbed_delivery_fallback_text:
                control.absorbed_delivery_fallback_text = queued.delivery_fallback_text
            payloads.append(
                {
                    "queue_id": queued.queue_id,
                    "client_message_id": queued.client_message_id,
                    "content": queued.content,
                    "attachments": list(queued.attachments or []),
                    "attachment_notice": queued.attachment_notice,
                    "attachment_context": queued.attachment_context,
                    "system_initiated": queued.system_initiated,
                    "intention_eligible": queued.intention_eligible,
                    "follow_up": queued.follow_up,
                }
            )
            if queued.attachment_notice:
                await self._notify_observers_system_message(
                    conversation_id,
                    queued.attachment_notice,
                    turn_observers=control.turn_observers,
                )
            if not queued.system_initiated:
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        data=_user_message_event_payload(
                            conversation_id=conversation_id,
                            session_id=self._turn_sessions.get(conversation_id),
                            content=queued.content,
                            attachments=strip_attachment_payload_bytes(queued.attachments or []),
                            queue_id=queued.queue_id,
                            client_message_id=queued.client_message_id,
                        ),
                    )
                )

        await self._suppress_absorbed_channel_delivery_intents(
            control,
            selected_delivery_id=control.active_delivery_id or control.absorbed_delivery_id,
        )

        logger.info(
            "turn_scheduler: absorbed queued batch into active turn",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "batch_size": len(payloads),
                    "remaining_queue": self.queued_count(conversation_id),
                }
            },
        )
        await self._notify_queue_updated(conversation_id, turn_observers=control.turn_observers)
        return payloads

    def _queued_message_is_absorbable(self, queued: _QueuedMessage) -> bool:
        """Return whether a queued message can be merged mid-turn."""

        return all(
            getattr(observer, "supports_mid_turn_absorb", False)
            for observer in queued.turn_observers
        )

    def _merge_active_turn_observers(
        self,
        active_observers: list[TurnObserver],
        batch: list[_QueuedMessage],
    ) -> None:
        """Merge queued per-submit observers into the active turn."""

        for queued in batch:
            for observer in queued.turn_observers:
                absorbed = False
                for active_observer in active_observers:
                    absorb = getattr(active_observer, "absorb_queued_observer", None)
                    if callable(absorb) and absorb(observer):
                        absorbed = True
                        break
                if absorbed:
                    continue
                if not getattr(observer, "supports_mid_turn_absorb", False):
                    continue
                if observer not in active_observers:
                    active_observers.append(observer)

    async def _purge_expired_follow_ups(self) -> None:
        now = monotonic()
        expired = [
            key
            for key, handled_at in self._handled_follow_ups.items()
            if now - handled_at >= FOLLOW_UP_DEDUPE_TTL_SECONDS
        ]
        for key in expired:
            self._handled_follow_ups.pop(key, None)
        try:
            async with self._session_factory() as db_session:
                await db_session.execute(
                    delete(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.expires_at <= _utcnow(),
                        or_(
                            FollowUpDedupeRow.status.not_in(("processing", "admitted")),
                            FollowUpDedupeRow.lease_expires_at <= _utcnow(),
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                await db_session.commit()
        except Exception:
            logger.debug("turn_scheduler: durable follow-up purge unavailable", exc_info=True)

    @staticmethod
    def _follow_up_dedupe_key(conversation_id: str, follow_up_id: str) -> str:
        return f"{conversation_id}:{follow_up_id}"

    @staticmethod
    def _follow_up_turn_id(conversation_id: str, follow_up_id: str) -> str:
        """Return the stable observable turn identity for a durable follow-up."""

        digest = hashlib.sha256(f"{conversation_id}:{follow_up_id}".encode()).hexdigest()
        return f"turn_fup_{digest[:16]}"

    async def _register_follow_up(self, conversation_id: str, follow_up_id: str) -> bool:
        await self._purge_expired_follow_ups()
        key = (conversation_id, follow_up_id)
        dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
        try:
            async with self._session_factory() as db_session:
                row = await db_session.get(FollowUpDedupeRow, dedupe_key)
                if (
                    row is not None
                    and row.status == "processing"
                    and row.lease_owner == self._follow_up_lease_owner
                ):
                    self._pending_follow_ups.add(key)
                    return True
                reason = row.status if row is not None else "missing"
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason=reason).inc()
                return False
        except Exception:
            logger.debug("turn_scheduler: durable follow-up register unavailable", exc_info=True)
            if key in self._pending_follow_ups:
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason="pending").inc()
                return False
            if key in self._handled_follow_ups:
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason="handled").inc()
                return False
            self._pending_follow_ups.add(key)
            return True

    async def _mark_follow_up_handled(self, conversation_id: str, follow_up_id: str) -> None:
        key = (conversation_id, follow_up_id)
        dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
        try:
            now = _utcnow()
            async with self._session_factory() as db_session:
                dedupe = await db_session.execute(
                    update(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.dedupe_key == dedupe_key,
                        FollowUpDedupeRow.status.in_(("processing", "admitted")),
                        FollowUpDedupeRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(
                        status="handled",
                        expires_at=now + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS),
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                intent = await db_session.execute(
                    update(FollowUpIntentRow)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                        FollowUpIntentRow.status.in_(("processing", "admitted")),
                        FollowUpIntentRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(
                        status="submitted",
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=None,
                        updated_at=now,
                    )
                )
                if not dedupe.rowcount or not intent.rowcount:
                    await db_session.rollback()
                    raise RuntimeError("Follow-up finalization lost its durable admission fence.")
                await db_session.commit()
            self._pending_follow_up_finalizations.discard(key)
            self._pending_follow_ups.discard(key)
            self._handled_follow_ups[key] = monotonic()
        except Exception:
            self._pending_follow_up_finalizations.add(key)
            logger.debug(
                "turn_scheduler: durable follow-up handled mark unavailable", exc_info=True
            )

    async def _clear_follow_up_pending(self, conversation_id: str, follow_up_id: str) -> None:
        await self._mark_follow_up_intent(
            conversation_id,
            follow_up_id,
            status="pending",
        )

    def _build_automatic_continuation_follow_up(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        metadata: dict[str, Any],
        prior_follow_up: FollowUpMetadata | None,
    ) -> ContinuationFollowUp | None:
        reason = str(metadata.get("continuation_reason") or "").strip()
        if reason in _AUTOMATIC_CONTINUATION_REASONS:
            return build_automatic_continuation_follow_up(
                conversation_id=conversation_id,
                turn_id=turn_id,
                reason=reason,
                metadata=metadata,
                prior_follow_up=prior_follow_up,
            )
        return None

    async def _schedule_automatic_continuation(
        self,
        *,
        conversation_id: str,
        session_id: str,
        turn_id: str,
        user_email: str,
        metadata: dict[str, Any],
        prior_follow_up: FollowUpMetadata | None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
        one_shot_chat_mode: ChatMode | None = None,
    ) -> bool:
        reason = str(metadata.get("continuation_reason") or "").strip()
        follow_up = self._build_automatic_continuation_follow_up(
            conversation_id=conversation_id,
            turn_id=turn_id,
            metadata=metadata,
            prior_follow_up=prior_follow_up,
        )
        tool_call_count = positive_optional_int(metadata.get("tool_call_count"))
        max_tool_calls = positive_optional_int(metadata.get("max_tool_calls"))
        cycle_count = positive_optional_int(metadata.get("cycle_count"))
        max_llm_cycles = positive_optional_int(metadata.get("max_llm_cycles"))
        if follow_up is None:
            subject = _automatic_continuation_exhausted_subject(reason)
            message = f"Automatic continuation stopped after repeated {subject}. Send a new message to continue manually."
            await self._notify_observers_system_message(
                conversation_id,
                message,
                turn_observers=turn_observers,
            )
            logger.warning(
                "turn_scheduler: automatic continuation ceiling exhausted",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "reason": reason,
                        "tool_call_count": tool_call_count,
                        "max_tool_calls": max_tool_calls,
                        "cycle_count": cycle_count,
                        "max_llm_cycles": max_llm_cycles,
                    }
                },
            )
            return False

        if reason == TOOL_CALL_CEILING_CONTINUATION_REASON:
            count_text = (
                f" ({tool_call_count}/{max_tool_calls} tool calls)"
                if tool_call_count is not None and max_tool_calls is not None
                else ""
            )
            message = f"Tool-call limit reached{count_text}. Continuing automatically."
        elif reason == LLM_CYCLE_CEILING_CONTINUATION_REASON:
            count_text = (
                f" ({cycle_count}/{max_llm_cycles} LLM cycles)"
                if cycle_count is not None and max_llm_cycles is not None
                else ""
            )
            message = f"LLM cycle limit reached{count_text}. Continuing automatically."
        elif reason == STEP_TIMEOUT_CONTINUATION_REASON:
            timeout_seconds = positive_optional_int(metadata.get("timeout_seconds"))
            timeout_text = f" after {timeout_seconds}s" if timeout_seconds is not None else ""
            message = f"Step timed out{timeout_text}. Continuing automatically."
        else:
            message = "Turn boundary reached. Continuing automatically."
        await self._notify_observers_system_message(
            conversation_id,
            message,
            turn_observers=turn_observers,
        )
        if not await self._durably_admit_follow_up(
            conversation_id,
            {"conversation_id": conversation_id, "follow_up": follow_up.model_dump(mode="json")},
        ):
            return False
        self._queued_messages[conversation_id].append(
            _QueuedMessage(
                content="",
                user_email=user_email,
                system_initiated=True,
                follow_up=follow_up,
                turn_observers=tuple(turn_observers),
                one_shot_chat_mode=one_shot_chat_mode,
            )
        )
        logger.info(
            "turn_scheduler: queued automatic continuation",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "reason": reason,
                    "attempt": follow_up.attempt,
                    "tool_call_count": tool_call_count,
                    "max_tool_calls": max_tool_calls,
                    "cycle_count": cycle_count,
                    "max_llm_cycles": max_llm_cycles,
                    "pending_todo_count": len(follow_up.pending_todos),
                }
            },
        )
        await self._notify_queue_updated(conversation_id, turn_observers=turn_observers)
        return True

    async def _schedule_tool_call_ceiling_continuation(
        self,
        *,
        conversation_id: str,
        session_id: str,
        turn_id: str,
        user_email: str,
        metadata: dict[str, Any],
        prior_follow_up: FollowUpMetadata | None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
        one_shot_chat_mode: ChatMode | None = None,
    ) -> bool:
        return await self._schedule_automatic_continuation(
            conversation_id=conversation_id,
            session_id=session_id,
            turn_id=turn_id,
            user_email=user_email,
            metadata=metadata,
            prior_follow_up=prior_follow_up,
            turn_observers=turn_observers,
            one_shot_chat_mode=one_shot_chat_mode,
        )

    # ------------------------------------------------------------------
    # Follow-up turn handling (EventBus subscriber)
    # ------------------------------------------------------------------

    async def _handle_follow_up_event(self, event: Event) -> None:
        try:
            await self._handle_follow_up_event_inner(event)
        except Exception as exc:
            conversation_id = event.data.get("conversation_id")
            raw_follow_up = event.data.get("follow_up")
            follow_up_id = (
                raw_follow_up.get("follow_up_id") if isinstance(raw_follow_up, dict) else None
            )
            if isinstance(conversation_id, str) and isinstance(follow_up_id, str):
                await self._mark_follow_up_intent(
                    conversation_id,
                    follow_up_id,
                    status="pending",
                    error=str(exc),
                )
            raise

    async def _handle_follow_up_event_inner(self, event: Event) -> None:
        """Handle a FOLLOW_UP_TURN_REQUESTED event."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            logger.warning("turn_scheduler: follow-up event missing conversation_id, dropping")
            return

        raw_follow_up = event.data.get("follow_up")
        if not isinstance(raw_follow_up, dict):
            logger.warning(
                "turn_scheduler: follow-up event missing typed metadata, dropping",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            return
        try:
            follow_up = parse_follow_up_metadata(raw_follow_up)
        except Exception as exc:
            logger.warning(
                "turn_scheduler: invalid follow-up metadata, dropping",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "follow_up_id": raw_follow_up.get("follow_up_id"),
                        "origin_kind": raw_follow_up.get("origin_kind"),
                        "error": str(exc),
                    }
                },
                exc_info=True,
            )
            return
        origin_session_id = event.data.get("origin_session_id")
        if isinstance(origin_session_id, str):
            async with self._session_factory() as db_session:
                is_current = await queries.origin_session_is_in_active_scope(
                    db_session,
                    conversation_id=conversation_id,
                    origin_session_id=origin_session_id,
                )
                if not is_current:
                    intent = await self._persist_follow_up_intent(
                        db_session,
                        conversation_id=conversation_id,
                        event_payload=dict(event.data),
                    )
                    intent.status = "submitted"
                    intent.lease_owner = None
                    intent.lease_expires_at = None
                    intent.last_error = None
                    await db_session.commit()
                    delivery_id = event.data.get("delivery_id")
                    if isinstance(delivery_id, str):
                        await self._suppress_channel_delivery_ids([delivery_id])
                    return
        # Use submit_turn for unified serialization
        # Determine user_email from the conversation
        async with self._session_factory() as db_session:
            row = await queries.get_conversation(db_session, conversation_id)
        if row is None:
            await self._mark_follow_up_intent(
                conversation_id,
                follow_up.follow_up_id,
                status="failed",
                error="Follow-up conversation not found.",
            )
            logger.warning(
                "turn_scheduler: follow-up conversation not found",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            return

        delivery_id = event.data.get("delivery_id")
        if not isinstance(delivery_id, str):
            delivery_id = None
        channel_deliverable = bool(event.data.get("channel_deliverable"))
        delivery_fallback_text = event.data.get("delivery_fallback_text")
        if not isinstance(delivery_fallback_text, str):
            delivery_fallback_text = None

        if delivery_id is not None:
            channel_deliverable = True
        elif channel_deliverable or "channel_deliverable" not in event.data:
            (
                delivery_id,
                channel_deliverable,
                delivery_fallback_text,
            ) = await self._ensure_follow_up_channel_delivery_intent(
                conversation_id=conversation_id,
                conversation=row,
                follow_up=follow_up,
                fallback_text=delivery_fallback_text,
            )
        event.data.update(
            {
                "delivery_id": delivery_id,
                "channel_deliverable": channel_deliverable,
                "delivery_fallback_text": delivery_fallback_text,
            }
        )
        await self._update_follow_up_intent_payload(
            conversation_id,
            follow_up.follow_up_id,
            dict(event.data),
        )
        if not await self._durably_admit_follow_up(
            conversation_id,
            dict(event.data),
        ):
            return

        error = await self.submit_turn(
            conversation_id,
            "",
            user_email=row.user_email,
            attachments=event.data.get("attachments")
            if isinstance(event.data.get("attachments"), list)
            else None,
            outbound_attachments=event.data.get("attachments")
            if isinstance(event.data.get("attachments"), list)
            else None,
            system_initiated=True,
            follow_up=follow_up,
            channel_deliverable=channel_deliverable,
            delivery_id=delivery_id,
            delivery_fallback_text=delivery_fallback_text,
            client_message_id=f"follow-up:{follow_up.follow_up_id}",
            one_shot_chat_mode=event.data.get("one_shot_chat_mode")
            if event.data.get("one_shot_chat_mode") in {"default", "plan", "build"}
            else None,
        )
        if error is not None:
            if not error.transient:
                await self._publish_turn_error(
                    conversation_id,
                    row.active_session_id or "",
                    error,
                    system_initiated=True,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                )
            await self._mark_follow_up_intent(
                conversation_id,
                follow_up.follow_up_id,
                status="pending" if error.transient else "failed",
                error=error.message,
            )

    async def _durably_admit_follow_up(
        self,
        conversation_id: str,
        event_payload: dict[str, Any],
    ) -> bool:
        """Persist, lease, and fence a follow-up before any execution."""

        raw_follow_up = event_payload.get("follow_up")
        follow_up_id = (
            raw_follow_up.get("follow_up_id") if isinstance(raw_follow_up, dict) else None
        )
        if not isinstance(follow_up_id, str):
            return False
        try:
            async with self._session_factory() as db_session:
                await self._persist_follow_up_intent(
                    db_session,
                    conversation_id=conversation_id,
                    event_payload=event_payload,
                )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: follow-up intent persistence unavailable; execution blocked",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
            return False
        if await self._claim_follow_up_intent(conversation_id, follow_up_id) is not True:
            return False
        if not await self._register_follow_up(conversation_id, follow_up_id):
            await self._mark_follow_up_intent(
                conversation_id,
                follow_up_id,
                status="pending",
            )
            return False
        if await self._mark_follow_up_admitted(conversation_id, follow_up_id) is not True:
            await self._mark_follow_up_intent(
                conversation_id,
                follow_up_id,
                status="failed",
                error="Could not durably fence follow-up admission.",
            )
            return False
        return True

    async def _persist_follow_up_intent(
        self,
        db_session: Any,
        *,
        conversation_id: str,
        follow_up: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> FollowUpIntentRow:
        """Insert an idempotent durable intent in the caller's transaction."""

        if event_payload is None:
            if follow_up is None:
                raise ValueError("follow_up or event_payload is required")
            event_payload = {
                "conversation_id": conversation_id,
                "follow_up": follow_up,
            }
        raw_follow_up = event_payload.get("follow_up")
        if not isinstance(raw_follow_up, dict):
            raise ValueError("event_payload.follow_up is required")
        follow_up = raw_follow_up
        follow_up_id = str(follow_up["follow_up_id"])
        existing = (
            await db_session.execute(
                select(FollowUpIntentRow).where(
                    FollowUpIntentRow.conversation_id == conversation_id,
                    FollowUpIntentRow.follow_up_id == follow_up_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
            if await db_session.get(FollowUpDedupeRow, dedupe_key) is None:
                db_session.add(
                    FollowUpDedupeRow(
                        dedupe_key=dedupe_key,
                        conversation_id=conversation_id,
                        follow_up_id=follow_up_id,
                        status="pending",
                        expires_at=_utcnow() + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS),
                    )
                )
                await db_session.flush()
            return existing
        row = FollowUpIntentRow(
            intent_id=f"fui_{uuid.uuid4().hex[:24]}",
            conversation_id=conversation_id,
            follow_up_id=follow_up_id,
            event_payload=event_payload,
            status="pending",
            attempt_count=0,
        )
        db_session.add(row)
        db_session.add(
            FollowUpDedupeRow(
                dedupe_key=self._follow_up_dedupe_key(conversation_id, follow_up_id),
                conversation_id=conversation_id,
                follow_up_id=follow_up_id,
                status="pending",
                expires_at=_utcnow() + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS),
            )
        )
        await db_session.flush()
        return row

    async def _claim_follow_up_intent(
        self,
        conversation_id: str,
        follow_up_id: str,
    ) -> bool | None:
        """Lease a pending or stale intent and count the execution attempt."""

        try:
            now = _utcnow()
            stale_before = now - timedelta(seconds=self._follow_up_lease_seconds)
            async with self._session_factory() as db_session:
                result = await db_session.execute(
                    update(FollowUpIntentRow)
                    .execution_options(synchronize_session=False)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                        or_(
                            FollowUpIntentRow.status == "pending",
                            and_(
                                FollowUpIntentRow.status == "processing",
                                or_(
                                    FollowUpIntentRow.lease_expires_at <= now,
                                    and_(
                                        FollowUpIntentRow.lease_owner.is_(None),
                                        FollowUpIntentRow.updated_at <= stale_before,
                                    ),
                                ),
                            ),
                        ),
                        FollowUpIntentRow.attempt_count < FOLLOW_UP_INTENT_MAX_ATTEMPTS,
                    )
                    .values(
                        status="processing",
                        attempt_count=FollowUpIntentRow.attempt_count + 1,
                        lease_owner=self._follow_up_lease_owner,
                        lease_expires_at=now + timedelta(seconds=self._follow_up_lease_seconds),
                        updated_at=now,
                    )
                )
                if not result.rowcount:
                    await db_session.rollback()
                    return False
                dedupe = await db_session.execute(
                    update(FollowUpDedupeRow)
                    .execution_options(synchronize_session=False)
                    .where(
                        FollowUpDedupeRow.dedupe_key
                        == self._follow_up_dedupe_key(conversation_id, follow_up_id),
                        or_(
                            FollowUpDedupeRow.status == "pending",
                            and_(
                                FollowUpDedupeRow.status == "processing",
                                or_(
                                    FollowUpDedupeRow.lease_expires_at <= now,
                                    and_(
                                        FollowUpDedupeRow.lease_owner.is_(None),
                                        FollowUpDedupeRow.updated_at <= stale_before,
                                    ),
                                ),
                            ),
                        ),
                    )
                    .values(
                        status="processing",
                        expires_at=now + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS),
                        lease_owner=self._follow_up_lease_owner,
                        lease_expires_at=now + timedelta(seconds=self._follow_up_lease_seconds),
                        updated_at=now,
                    )
                )
                if not dedupe.rowcount:
                    await db_session.rollback()
                    return False
                await db_session.commit()
                return True
        except Exception:
            logger.debug("turn_scheduler: follow-up intent claim unavailable", exc_info=True)
            return None

    async def _update_follow_up_intent_payload(
        self,
        conversation_id: str,
        follow_up_id: str,
        event_payload: dict[str, Any],
    ) -> None:
        try:
            async with self._session_factory() as db_session:
                await db_session.execute(
                    update(FollowUpIntentRow)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                    )
                    .values(event_payload=event_payload, updated_at=_utcnow())
                )
                await db_session.commit()
        except Exception:
            logger.debug(
                "turn_scheduler: follow-up intent payload update unavailable", exc_info=True
            )

    async def _mark_follow_up_intent(
        self,
        conversation_id: str,
        follow_up_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> bool:
        """Atomically transition the durable intent and its dedupe fence."""

        key = (conversation_id, follow_up_id)
        try:
            async with self._session_factory() as db_session:
                intent_status = (
                    case(
                        (
                            FollowUpIntentRow.attempt_count >= FOLLOW_UP_INTENT_MAX_ATTEMPTS,
                            "failed",
                        ),
                        else_=status,
                    )
                    if error and status == "pending"
                    else status
                )
                intent = await db_session.execute(
                    update(FollowUpIntentRow)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                        or_(
                            FollowUpIntentRow.lease_owner == self._follow_up_lease_owner,
                            and_(
                                FollowUpIntentRow.lease_owner.is_(None),
                                FollowUpIntentRow.status.in_(("pending", "processing")),
                            ),
                        ),
                    )
                    .values(
                        status=intent_status,
                        last_error=error,
                        attempt_count=FollowUpIntentRow.attempt_count,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=_utcnow(),
                    )
                    .returning(FollowUpIntentRow.status)
                )
                resolved_status = intent.scalar_one_or_none()
                if resolved_status is None:
                    await db_session.rollback()
                    existing_status = (
                        await db_session.execute(
                            select(FollowUpIntentRow.status).where(
                                FollowUpIntentRow.conversation_id == conversation_id,
                                FollowUpIntentRow.follow_up_id == follow_up_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_status in {"pending", "processing"}:
                        self._pending_follow_up_transitions[key] = (status, error)
                    else:
                        self._pending_follow_up_transitions.pop(key, None)
                    return False
                dedupe_status = (
                    "handled" if resolved_status in {"failed", "submitted"} else "pending"
                )
                dedupe = await db_session.execute(
                    update(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.dedupe_key
                        == self._follow_up_dedupe_key(conversation_id, follow_up_id),
                        or_(
                            FollowUpDedupeRow.lease_owner == self._follow_up_lease_owner,
                            and_(
                                FollowUpDedupeRow.lease_owner.is_(None),
                                FollowUpDedupeRow.status.in_(("pending", "processing")),
                            ),
                        ),
                    )
                    .values(
                        status=dedupe_status,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=_utcnow(),
                    )
                )
                if not dedupe.rowcount:
                    await db_session.rollback()
                    self._pending_follow_up_transitions[key] = (status, error)
                    return False
                await db_session.commit()
                self._pending_follow_up_transitions.pop(key, None)
                self._pending_follow_ups.discard(key)
                if dedupe_status == "handled":
                    self._handled_follow_ups[key] = monotonic()
                return True
        except Exception:
            self._pending_follow_up_transitions[key] = (status, error)
            logger.debug("turn_scheduler: follow-up intent mark unavailable", exc_info=True)
            return False

    async def _mark_follow_up_admitted(
        self,
        conversation_id: str,
        follow_up_id: str,
    ) -> bool | None:
        """Fence an admitted turn from recovery replay on another replica."""

        try:
            now = _utcnow()
            dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
            async with self._session_factory() as db_session:
                intent = await db_session.execute(
                    update(FollowUpIntentRow)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                        FollowUpIntentRow.status == "processing",
                        FollowUpIntentRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(
                        status="admitted",
                        lease_expires_at=now + timedelta(seconds=self._follow_up_lease_seconds),
                        updated_at=now,
                    )
                )
                dedupe = await db_session.execute(
                    update(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.dedupe_key == dedupe_key,
                        FollowUpDedupeRow.status == "processing",
                        FollowUpDedupeRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(
                        status="admitted",
                        lease_expires_at=now + timedelta(seconds=self._follow_up_lease_seconds),
                        updated_at=now,
                    )
                )
                if not intent.rowcount or not dedupe.rowcount:
                    await db_session.rollback()
                    return False
                await db_session.commit()
                return True
        except Exception:
            logger.debug("turn_scheduler: follow-up admission mark unavailable", exc_info=True)
            return None

    async def _renew_owned_follow_up_leases(self) -> None:
        """Renew healthy admitted work and retry deferred atomic finalization."""

        for conversation_id, follow_up_id in tuple(self._pending_follow_up_finalizations):
            await self._mark_follow_up_handled(conversation_id, follow_up_id)
        for (conversation_id, follow_up_id), (
            status,
            error,
        ) in tuple(self._pending_follow_up_transitions.items()):
            await self._mark_follow_up_intent(
                conversation_id,
                follow_up_id,
                status=status,
                error=error,
            )
        now = _utcnow()
        lease_expires_at = now + timedelta(seconds=self._follow_up_lease_seconds)
        deferred = set(self._pending_follow_up_transitions) | set(
            self._pending_follow_up_finalizations
        )
        async with self._session_factory() as db_session:
            owned = list(
                (
                    await db_session.execute(
                        select(
                            FollowUpIntentRow.conversation_id,
                            FollowUpIntentRow.follow_up_id,
                        ).where(
                            FollowUpIntentRow.status == "admitted",
                            FollowUpIntentRow.lease_owner == self._follow_up_lease_owner,
                        )
                    )
                ).all()
            )
            for conversation_id, follow_up_id in owned:
                if (conversation_id, follow_up_id) in deferred:
                    continue
                intent = await db_session.execute(
                    update(FollowUpIntentRow)
                    .where(
                        FollowUpIntentRow.conversation_id == conversation_id,
                        FollowUpIntentRow.follow_up_id == follow_up_id,
                        FollowUpIntentRow.status == "admitted",
                        FollowUpIntentRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(lease_expires_at=lease_expires_at, updated_at=now)
                )
                dedupe = await db_session.execute(
                    update(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.dedupe_key
                        == self._follow_up_dedupe_key(conversation_id, follow_up_id),
                        FollowUpDedupeRow.status == "admitted",
                        FollowUpDedupeRow.lease_owner == self._follow_up_lease_owner,
                    )
                    .values(lease_expires_at=lease_expires_at, updated_at=now)
                )
                if not intent.rowcount or not dedupe.rowcount:
                    await db_session.rollback()
                    raise RuntimeError("Follow-up lease renewal lost its paired durable fence.")
            await db_session.commit()

    async def start_follow_up_recovery(self, *, interval_seconds: float) -> None:
        """Start bounded periodic recovery of retriable durable follow-up intents."""

        if self._follow_up_recovery_task is not None:
            return
        self.configure_follow_up_recovery(interval_seconds=interval_seconds)
        self._follow_up_recovery_stop.clear()
        self._follow_up_recovery_task = asyncio.create_task(
            self._follow_up_recovery_loop(max(0.1, interval_seconds)),
            name="follow-up-intent-recovery",
        )

    def configure_follow_up_recovery(self, *, interval_seconds: float) -> None:
        """Configure lease safety before any startup recovery pass."""

        self._follow_up_lease_seconds = max(
            FOLLOW_UP_INTENT_LEASE_SECONDS,
            interval_seconds * 4,
        )

    async def stop_follow_up_recovery(self) -> None:
        """Stop periodic follow-up recovery and wait for worker shutdown."""

        self._follow_up_recovery_stop.set()
        if self._follow_up_recovery_task is None:
            return
        self._follow_up_recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._follow_up_recovery_task
        self._follow_up_recovery_task = None

    async def _follow_up_recovery_loop(self, interval_seconds: float) -> None:
        while not self._follow_up_recovery_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._follow_up_recovery_stop.wait(),
                    timeout=interval_seconds,
                )
                continue
            except TimeoutError:
                pass
            try:
                await self.recover_follow_up_intents()
            except Exception:
                logger.warning("turn_scheduler: follow-up intent recovery failed", exc_info=True)

    async def recover_follow_up_intents(
        self,
        *,
        limit: int = 100,
        reclaim_processing: bool = False,
    ) -> int:
        """Claim pending or durably stale intents without stealing healthy work."""

        async with self._follow_up_recovery_lock:
            del reclaim_processing  # Leases, not process startup, define safe reclamation.
            await self._renew_owned_follow_up_leases()
            now = _utcnow()
            stale_before = now - timedelta(seconds=self._follow_up_lease_seconds)
            stale_processing = and_(
                FollowUpIntentRow.status == "processing",
                or_(
                    FollowUpIntentRow.lease_expires_at <= now,
                    and_(
                        FollowUpIntentRow.lease_owner.is_(None),
                        FollowUpIntentRow.updated_at <= stale_before,
                    ),
                ),
            )
            exhausted_intent = and_(
                or_(FollowUpIntentRow.status == "pending", stale_processing),
                FollowUpIntentRow.attempt_count >= FOLLOW_UP_INTENT_MAX_ATTEMPTS,
            )
            stale_admitted = and_(
                FollowUpIntentRow.status == "admitted",
                or_(
                    FollowUpIntentRow.lease_expires_at <= now,
                    and_(
                        FollowUpIntentRow.lease_owner.is_(None),
                        FollowUpIntentRow.updated_at <= stale_before,
                    ),
                ),
            )
            async with self._session_factory() as db_session:
                abandoned = list(
                    (
                        await db_session.execute(
                            select(
                                FollowUpIntentRow.conversation_id,
                                FollowUpIntentRow.follow_up_id,
                            ).where(stale_admitted)
                        )
                    ).all()
                )
                for conversation_id, follow_up_id in abandoned:
                    dedupe = await db_session.execute(
                        update(FollowUpDedupeRow)
                        .where(
                            FollowUpDedupeRow.dedupe_key
                            == self._follow_up_dedupe_key(conversation_id, follow_up_id),
                            FollowUpDedupeRow.status == "admitted",
                            or_(
                                FollowUpDedupeRow.lease_expires_at <= now,
                                FollowUpDedupeRow.lease_owner.is_(None),
                            ),
                        )
                        .values(
                            status="handled",
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    intent = await db_session.execute(
                        update(FollowUpIntentRow)
                        .where(
                            FollowUpIntentRow.conversation_id == conversation_id,
                            FollowUpIntentRow.follow_up_id == follow_up_id,
                            stale_admitted,
                        )
                        .values(
                            status="failed",
                            lease_owner=None,
                            lease_expires_at=None,
                            last_error=(
                                "Follow-up execution owner was lost after admission; "
                                "work was not replayed."
                            ),
                            updated_at=now,
                        )
                    )
                    if not dedupe.rowcount or not intent.rowcount:
                        await db_session.rollback()
                        raise RuntimeError(
                            "Stale admitted follow-up lost its paired durable fence."
                        )
                exhausted = list(
                    (
                        await db_session.execute(
                            select(
                                FollowUpIntentRow.conversation_id,
                                FollowUpIntentRow.follow_up_id,
                            ).where(
                                exhausted_intent,
                            )
                        )
                    ).all()
                )
                for conversation_id, follow_up_id in exhausted:
                    dedupe = await db_session.execute(
                        update(FollowUpDedupeRow)
                        .where(
                            FollowUpDedupeRow.dedupe_key
                            == self._follow_up_dedupe_key(conversation_id, follow_up_id),
                            or_(
                                FollowUpDedupeRow.status == "pending",
                                FollowUpDedupeRow.lease_expires_at <= now,
                                FollowUpDedupeRow.lease_owner.is_(None),
                            ),
                        )
                        .values(
                            status="handled",
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    intent = await db_session.execute(
                        update(FollowUpIntentRow)
                        .where(
                            FollowUpIntentRow.conversation_id == conversation_id,
                            FollowUpIntentRow.follow_up_id == follow_up_id,
                            exhausted_intent,
                        )
                        .values(
                            status="failed",
                            lease_owner=None,
                            lease_expires_at=None,
                            last_error="Follow-up retry attempts exhausted during recovery.",
                            updated_at=now,
                        )
                    )
                    if not dedupe.rowcount or not intent.rowcount:
                        await db_session.rollback()
                        raise RuntimeError("Exhausted follow-up lost its paired durable fence.")
                await db_session.commit()
                rows = list(
                    (
                        await db_session.execute(
                            select(FollowUpIntentRow)
                            .where(
                                or_(
                                    FollowUpIntentRow.status == "pending",
                                    stale_processing,
                                ),
                                FollowUpIntentRow.attempt_count < FOLLOW_UP_INTENT_MAX_ATTEMPTS,
                            )
                            .order_by(FollowUpIntentRow.updated_at, FollowUpIntentRow.intent_id)
                            .limit(max(1, limit))
                        )
                    )
                    .scalars()
                    .all()
                )
            for row in rows:
                await self._handle_follow_up_event(
                    Event(type=EventType.FOLLOW_UP_TURN_REQUESTED, data=dict(row.event_payload))
                )
            return len(rows)

    async def _managed_join_tool_result_is_durable(
        self,
        link: ManagedConversationLink,
    ) -> bool:
        """Reconcile the parent transcript before transferring fallback ownership."""

        async with self._session_factory() as db_session:
            session_row = await queries.get_session_row(
                db_session,
                str(link.handoff_controller_session_id),
            )
            agent_row = await queries.get_agent(db_session, link.controller_agent_id)
        if session_row is None:
            raise RuntimeError("Managed join controller session is unavailable")
        system_agent = SYSTEM_AGENTS.get(link.controller_agent_id)
        agent_owner_email = (
            agent_row.owner_email
            if agent_row is not None
            else system_agent.owner_email
            if system_agent is not None
            else None
        )
        if agent_owner_email is None:
            raise RuntimeError("Managed join controller agent is unavailable")
        intaris_session_id = session_row.intaris_session_id or session_row.session_id
        after_seq = 0
        with scoped_runtime_context(
            user_email=link.user_email,
            agent_id=link.controller_agent_id,
            agent_owner_email=agent_owner_email,
        ):
            while True:
                result = await self._providers.guardrails.read_events(
                    session_id=intaris_session_id,
                    after_seq=after_seq,
                    limit=200,
                    types=["tool_result"],
                    allow_missing_stream=True,
                )
                for event in result.events:
                    data = event.get("data") if isinstance(event, dict) else None
                    if (
                        isinstance(data, dict)
                        and data.get("call_id") == link.handoff_tool_call_id
                        and data.get("turn_id") == link.handoff_controller_turn_id
                    ):
                        return True
                if not result.has_more or result.last_seq <= after_seq:
                    return False
                after_seq = result.last_seq

    async def recover_managed_join_handoffs_for_parent(
        self,
        *,
        controller_session_id: str | None = None,
        controller_turn_id: str | None = None,
    ) -> int:
        """Claim unacknowledged joins and create one correlated fallback when ready."""

        async with self._session_factory() as db_session:
            links = await queries.list_pending_managed_conversation_join_handoffs(
                db_session,
                controller_session_id=controller_session_id,
                controller_turn_id=controller_turn_id,
            )

        recovered: list[ManagedConversationLink] = []
        for link in links:
            if link.handoff_state == "pending":
                try:
                    if await self._managed_join_tool_result_is_durable(link):
                        async with self._session_factory() as db_session:
                            await queries.acknowledge_managed_conversation_join_handoff(
                                db_session,
                                link.link_id,
                                target_turn_id=str(link.handoff_target_turn_id),
                                controller_session_id=str(link.handoff_controller_session_id),
                                controller_turn_id=str(link.handoff_controller_turn_id),
                                tool_call_id=str(link.handoff_tool_call_id),
                            )
                            await db_session.commit()
                        continue
                except Exception:
                    logger.warning(
                        "turn_scheduler: failed to reconcile managed join transcript",
                        extra={"extra_data": {"link_id": link.link_id}},
                        exc_info=True,
                    )
                    continue
                async with self._session_factory() as db_session:
                    claimed = await queries.claim_managed_conversation_join_handoff(
                        db_session,
                        link.link_id,
                        target_turn_id=str(link.handoff_target_turn_id),
                        controller_session_id=str(link.handoff_controller_session_id),
                        controller_turn_id=str(link.handoff_controller_turn_id),
                        tool_call_id=str(link.handoff_tool_call_id),
                    )
                    await db_session.commit()
                if claimed is None:
                    continue
                link = claimed
            recovered.append(link)

        notified = 0
        for link in recovered:
            target_turn_id = str(link.handoff_target_turn_id or "")
            if not target_turn_id:
                continue
            if link.active_turn_id == target_turn_id:
                if link.turn_state in {"queued", "running"}:
                    continue
                already_settled = False
            elif link.last_result_turn_id == target_turn_id:
                already_settled = True
            else:
                continue
            failed = link.turn_state in {"failed", "interrupted"}
            await self._notify_managed_conversation_controller(
                target_conversation_id=link.target_conversation_id,
                target_session_id=link.target_session_id or "",
                turn_state=link.turn_state,
                conversation_state=link.conversation_state,
                status=(
                    FollowUpStatus.CANCELLED
                    if link.turn_state == "interrupted"
                    else FollowUpStatus.FAILED
                    if failed
                    else FollowUpStatus.COMPLETED
                ),
                summary=link.last_result_summary or link.last_error,
                turn_id=target_turn_id,
                error_message=link.last_error,
                recoverable=True if failed else None,
                already_settled=already_settled,
            )
            notified += 1
        return notified

    async def recover_managed_conversation_notifications(self) -> int:
        """Create durable controller handoffs for startup-interrupted managed work."""

        recovered_join_handoffs = await self.recover_managed_join_handoffs_for_parent()
        fallback_notifications: list[tuple[str, str, str]] = []
        async with self._session_factory() as db_session:
            fallback_links = list(
                (
                    await db_session.execute(
                        select(ManagedConversationLink).where(
                            ManagedConversationLink.conversation_state == "open",
                            ManagedConversationLink.turn_state == "interrupted",
                            ManagedConversationLink.active_turn_id.is_(None),
                            ManagedConversationLink.notify_on_completion.is_(True),
                            ManagedConversationLink.handoff_state == "fallback_claimed",
                            ManagedConversationLink.handoff_target_turn_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for link in fallback_links:
                target_turn_id = str(link.handoff_target_turn_id)
                row = await db_session.scalar(
                    select(DirectTurnRequestRow).where(
                        DirectTurnRequestRow.turn_id == target_turn_id,
                        DirectTurnRequestRow.conversation_id == link.target_conversation_id,
                        DirectTurnRequestRow.status == "completed",
                    )
                )
                if row is None:
                    continue
                if await queries.settle_completed_fallback_managed_conversation_turn(
                    db_session,
                    link.link_id,
                    target_turn_id=target_turn_id,
                ):
                    fallback_notifications.append(
                        (
                            link.target_conversation_id,
                            link.target_session_id or row.session_id or "",
                            target_turn_id,
                        )
                    )
            await db_session.commit()
        fallback_recovered = 0
        for target_conversation_id, target_session_id, target_turn_id in fallback_notifications:
            if await self._notify_managed_conversation_controller(
                target_conversation_id=target_conversation_id,
                target_session_id=target_session_id,
                turn_state="completed",
                conversation_state="completed",
                status=FollowUpStatus.COMPLETED,
                summary="Managed turn completed after controller restart.",
                turn_id=target_turn_id,
                already_settled=True,
            ):
                fallback_recovered += 1
        async with self._session_factory() as db_session:
            links = list(
                (
                    await db_session.execute(
                        select(ManagedConversationLink).where(
                            ManagedConversationLink.conversation_state == "open",
                            ManagedConversationLink.turn_state == "interrupted",
                            ManagedConversationLink.notify_on_completion.is_(True),
                            ManagedConversationLink.handoff_state.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for link in links:
                if link.active_turn_id:
                    continue
                recovery_turn_id = (
                    "turn_recovery_" + hashlib.sha256(link.link_id.encode()).hexdigest()[:12]
                )
                await queries.assign_managed_conversation_recovery_turn_id(
                    db_session,
                    link.link_id,
                    recovery_turn_id=recovery_turn_id,
                )
                await db_session.refresh(link)
            notifications = [
                (
                    link.target_conversation_id,
                    link.target_session_id or "",
                    link.last_error,
                    link.active_turn_id,
                )
                for link in links
            ]
            await db_session.commit()
        for target_conversation_id, target_session_id, last_error, turn_id in notifications:
            await self._notify_managed_conversation_controller(
                target_conversation_id=target_conversation_id,
                target_session_id=target_session_id,
                turn_state="interrupted",
                conversation_state="open",
                status=FollowUpStatus.CANCELLED,
                summary=last_error,
                turn_id=turn_id,
                error_message=last_error,
                recoverable=True,
            )
        return recovered_join_handoffs + fallback_recovered + len(notifications)

    async def _ensure_follow_up_channel_delivery_intent(
        self,
        *,
        conversation_id: str,
        conversation: Any,
        follow_up: FollowUpMetadata,
        fallback_text: str | None,
    ) -> tuple[str | None, bool, str | None]:
        """Persist a generic channel outbox row for channel-bound follow-up turns."""

        try:
            async with self._session_factory() as db_session:
                route = await queries.get_conversation_channel_route(db_session, conversation_id)
                if route is None:
                    return None, False, fallback_text

                channel_type, account_id, chat_id, thread_id, user_email = route
                delivery_key = f"{conversation_id}:{follow_up.follow_up_id}".encode()
                delivery_id = f"cdel_{hashlib.sha256(delivery_key).hexdigest()[:24]}"
                resolved_fallback = fallback_text or self._build_follow_up_delivery_fallback(
                    follow_up
                )
                existing = await queries.get_channel_delivery_outbox(db_session, delivery_id)
                if existing is not None:
                    return delivery_id, True, existing.fallback_text or resolved_fallback
                await queries.create_channel_delivery_outbox(
                    db_session,
                    delivery_id=delivery_id,
                    user_email=user_email,
                    conversation_id=conversation_id,
                    session_id=getattr(conversation, "active_session_id", None),
                    source_type="follow_up",
                    source_id=follow_up.follow_up_id,
                    channel_type=channel_type,
                    account_id=account_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    fallback_text=resolved_fallback,
                    next_attempt_at=datetime.now(UTC) + timedelta(minutes=2),
                )
                await db_session.commit()
                return delivery_id, True, resolved_fallback
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist channel follow-up delivery intent",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "follow_up_id": follow_up.follow_up_id,
                        "origin_kind": follow_up.origin_kind.value,
                    }
                },
                exc_info=True,
            )
        return None, False, fallback_text

    def _build_follow_up_delivery_fallback(self, follow_up: FollowUpMetadata) -> str:
        """Build a channel fallback used only when the follow-up turn cannot render content."""

        status = follow_up.status.value
        if follow_up.origin_kind in {FollowUpOriginKind.TASK_RESULT, FollowUpOriginKind.SCHEDULE}:
            task_id = getattr(follow_up, "task_id", None)
            task_title = getattr(follow_up, "task_title", None) or "Background task"
            summary = getattr(follow_up, "result_summary", None)
            suffix = f" Summary: {summary}" if summary else ""
            return (
                f'Task "{task_title}" ({task_id}) is {status}.{suffix} '
                "I could not deliver the detailed follow-up reply, so please open the conversation for details."
            )

        if follow_up.origin_kind is FollowUpOriginKind.DELEGATION_RESULT:
            summary = getattr(follow_up, "result_summary", None)
            suffix = f" Summary: {summary}" if summary else ""
            return (
                f"Background work is {status}.{suffix} "
                "I could not deliver the detailed follow-up reply, so please open the conversation for details."
            )

        if follow_up.origin_kind is FollowUpOriginKind.BACKGROUND_TOOL_RESULT:
            description = getattr(follow_up, "description", None)
            tool_name = getattr(follow_up, "tool_name", None) or "tool"
            subject = f"{tool_name} background command"
            if description:
                subject = description
            return (
                f"{subject} is {status}. "
                "I could not deliver the detailed follow-up reply, so please open the conversation for details."
            )

        if follow_up.origin_kind is FollowUpOriginKind.GATE:
            task_id = getattr(follow_up, "task_id", None)
            task_title = getattr(follow_up, "task_title", None) or "Background task"
            return (
                f'Task "{task_title}" ({task_id}) is waiting for input. '
                "I could not deliver the detailed follow-up reply, so please open the conversation for details."
            )

        if follow_up.origin_kind is FollowUpOriginKind.CONTINUATION:
            reason = getattr(follow_up, "reason", None) or "continuation"
            return (
                f"Automatic follow-up turn for {reason} is {status}. "
                "I could not deliver the detailed follow-up reply, so please open the conversation for details."
            )

        title = getattr(follow_up, "title", None) or "Follow-up turn"
        summary = getattr(follow_up, "summary", None)
        suffix = f" Summary: {summary}" if summary else ""
        return (
            f"{title} is {status}.{suffix} "
            "I could not deliver the detailed follow-up reply, so please open the conversation for details."
        )

    async def _suppress_absorbed_channel_delivery_intents(
        self,
        turn_control: _TurnControl,
        *,
        selected_delivery_id: str | None,
    ) -> None:
        delivery_ids = await self._suppress_channel_delivery_ids(
            turn_control.suppressed_channel_delivery_ids,
            selected_delivery_id=selected_delivery_id,
            reason="absorbed into active follow-up turn",
        )
        if not delivery_ids:
            return
        turn_control.suppressed_channel_delivery_ids = [
            delivery_id
            for delivery_id in turn_control.suppressed_channel_delivery_ids
            if delivery_id not in delivery_ids
        ]

    async def _suppress_channel_delivery_ids(
        self,
        delivery_ids: list[str],
        *,
        selected_delivery_id: str | None,
        reason: str,
    ) -> list[str]:
        delivery_ids = [
            delivery_id
            for delivery_id in dict.fromkeys(delivery_ids)
            if delivery_id != selected_delivery_id
        ]
        if not delivery_ids:
            return []
        try:
            async with self._session_factory() as db_session:
                await queries.suppress_channel_delivery_outbox(
                    db_session,
                    delivery_ids=delivery_ids,
                    reason=reason,
                )
                await db_session.commit()
            return delivery_ids
        except Exception:
            logger.warning(
                "turn_scheduler: failed to suppress channel delivery intents",
                extra={
                    "extra_data": {
                        "delivery_ids": delivery_ids,
                        "selected_delivery_id": selected_delivery_id,
                        "reason": reason,
                    }
                },
                exc_info=True,
            )
        return []

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _resolve_attachments_for_turn(
        self,
        *,
        user_email: str,
        attachments: list[dict[str, Any]],
    ) -> tuple[list[AttachmentRef], TurnError | None]:
        if not attachments:
            return [], None
        from cognis.store.queries import get_artifact_record

        prepared: list[dict[str, Any]] = []
        async with self._session_factory() as session:
            for raw in attachments:
                artifact_id = raw.get("artifact_id") if isinstance(raw, dict) else None
                if not isinstance(artifact_id, str) or not artifact_id:
                    return [], TurnError(
                        code="validation_error",
                        message="Invalid attachment reference",
                        recoverable=True,
                    )
                row = await get_artifact_record(session, artifact_id)
                if row is None or row.status == "deleted":
                    return [], TurnError(
                        code="not_found",
                        message="Attachment not found",
                        recoverable=True,
                    )
                if row.owner_email and row.owner_email != user_email:
                    return [], TurnError(
                        code="forbidden",
                        message="Attachment access denied",
                        recoverable=False,
                    )
                prepared.append(
                    {
                        "artifact_id": row.artifact_id,
                        "kind": ArtifactKind(row.kind),
                        "mime_type": row.mime_type,
                        "filename": row.filename,
                        "size_bytes": row.size_bytes,
                        "namespace": row.namespace,
                        "object_id": row.object_id,
                    }
                )
        normalized: list[AttachmentRef] = []
        for artifact in prepared:
            url = await self._artifact_store.async_get_public_url(
                artifact["namespace"],
                artifact["object_id"],
                artifact["filename"],
            )
            normalized.append(
                AttachmentRef(
                    artifact_id=artifact["artifact_id"],
                    kind=artifact["kind"],
                    mime_type=artifact["mime_type"],
                    filename=artifact["filename"],
                    size_bytes=artifact["size_bytes"],
                    url=url,
                )
            )
        return normalized, None

    async def _build_attachment_support_messages(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
        acting_user_email: str | None = None,
    ) -> tuple[str | None, str | None]:
        if not attachments:
            return None, None
        model_override = self._session_cache.get_model_override(session.session_id)
        model_override_provider_id = self._session_cache.get_model_override_provider_id(
            session.session_id
        )
        if model_override:
            explicit_model = model_override
            explicit_provider_id = model_override_provider_id
        else:
            explicit_model = agent.llm_config.model if agent.llm_config else None
            explicit_provider_id = agent.llm_config.provider_id if agent.llm_config else None
        provider_id: str | None = None
        if hasattr(self._providers.llm, "resolve_model_target"):
            try:
                resolved_model, provider_id = await self._providers.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                    acting_user_email=acting_user_email,
                )
            except TypeError:
                resolved_model, provider_id = await self._providers.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                    acting_user_email=acting_user_email,
                )
        else:
            try:
                resolved_model = await self._providers.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                    acting_user_email=acting_user_email,
                )
            except TypeError:
                resolved_model = await self._providers.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                    acting_user_email=acting_user_email,
                )
        if provider_id is not None:
            try:
                model_info = await self._providers.llm.get_model_info(
                    resolved_model,
                    provider_id=provider_id,
                    acting_user_email=acting_user_email,
                )
            except TypeError:
                model_info = await self._providers.llm.get_model_info(
                    resolved_model,
                    acting_user_email=acting_user_email,
                )
        else:
            model_info = await self._providers.llm.get_model_info(
                resolved_model,
                acting_user_email=acting_user_email,
            )
        unsupported: list[str] = []
        pdf_fallbacks: list[str] = []
        audio_transcripts: list[str] = []
        audio_transcript_chars = 0
        omitted_audio_transcripts = False
        for attachment in attachments:
            if attachment.kind == ArtifactKind.IMAGE and supports_native_image_input(
                model_info,
                attachment.mime_type,
                filename=attachment.filename,
            ):
                continue
            if attachment.kind == ArtifactKind.PDF and (
                model_info.supports_pdf_input or model_info.supports_file_input
            ):
                continue
            if attachment.kind == ArtifactKind.AUDIO:
                if model_info.supports_audio_input:
                    continue
                transcript = await self._transcribe_audio_attachment(
                    attachment,
                    acting_user_email=acting_user_email,
                )
                if transcript:
                    safe_filename = html.escape(attachment.filename)
                    remaining = _MAX_AUDIO_TRANSCRIPT_CONTEXT_CHARS - audio_transcript_chars
                    if remaining <= 0:
                        omitted_audio_transcripts = True
                        continue
                    transcript_limit = min(_MAX_AUDIO_TRANSCRIPT_CHARS, remaining)
                    truncated = transcript[:transcript_limit]
                    audio_transcript_chars += len(truncated)
                    if len(transcript) > transcript_limit:
                        truncated += (
                            "\n[Transcript truncated; use artifact_read for the full text.]"
                        )
                    safe_transcript = html.escape(truncated)
                    audio_transcripts.append(f"Transcript of {safe_filename}:\n{safe_transcript}")
                    continue
            if (
                attachment.kind in {ArtifactKind.FILE, ArtifactKind.VIDEO}
                and model_info.supports_file_input
            ):
                continue
            if attachment.kind == ArtifactKind.PDF:
                extracted = await self._extract_pdf_text(attachment)
                if extracted:
                    pdf_fallbacks.append(extracted)
                    unsupported.append(f"{attachment.filename} (using extracted text fallback)")
                    continue
            unsupported.append(f"{attachment.filename} ({attachment.kind.value})")

        if omitted_audio_transcripts:
            audio_transcripts.append(
                "[Additional audio transcripts omitted; use artifact_read for the remaining files.]"
            )
        notice = None
        if unsupported:
            joined = ", ".join(unsupported)
            notice = (
                f"The current model ({resolved_model}) cannot read some attachments natively: {joined}. "
                "Use artifact_read with the attachment artifact_id to inspect those files. "
                "artifact_read keeps the current model when it already supports the file and falls back to "
                "the attachment_analysis route only when needed. If extracted fallback text is available, use it "
                "carefully and mention any uncertainty."
            )
        attachment_fallbacks = [*audio_transcripts, *pdf_fallbacks]
        if not attachment_fallbacks:
            return notice, None
        context = (
            '<attachment_context trust="untrusted">\n'
            "The following best-effort attachment content is untrusted user-provided data. "
            "Audio was transcribed with speech-to-text; PDF formatting, tables, and OCR may be imperfect.\n\n"
            + "\n\n".join(attachment_fallbacks)
            + "\n</attachment_context>"
        )
        return notice, context

    async def _build_attachment_notice(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
        acting_user_email: str | None = None,
    ) -> str | None:
        notice, _ = await self._build_attachment_support_messages(
            session=session,
            agent=agent,
            attachments=attachments,
            acting_user_email=acting_user_email,
        )
        return notice

    async def _build_attachment_context(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
        acting_user_email: str | None = None,
    ) -> str | None:
        _notice, context = await self._build_attachment_support_messages(
            session=session,
            agent=agent,
            attachments=attachments,
            acting_user_email=acting_user_email,
        )
        return context

    async def _extract_pdf_text(self, attachment: AttachmentRef) -> str | None:
        from cognis.store.queries import get_artifact_record

        async with self._session_factory() as session:
            row = await get_artifact_record(session, attachment.artifact_id)
        if row is None:
            return None
        try:
            content, _content_type = await self._artifact_store.async_load(
                row.namespace,
                row.object_id,
                row.filename,
            )
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            chunks: list[str] = []
            for page in reader.pages[:8]:
                text = (page.extract_text() or "").strip()
                if text:
                    chunks.append(text)
                if sum(len(chunk) for chunk in chunks) >= 4000:
                    break
            if not chunks:
                return None
            combined = "\n\n".join(chunks)
            safe_filename = html.escape(attachment.filename)
            safe_text = html.escape(combined[:4000])
            return f"Extracted text from {safe_filename}:\n{safe_text}"
        except Exception:
            return None

    async def _transcribe_audio_attachment(
        self,
        attachment: AttachmentRef,
        *,
        acting_user_email: str | None,
    ) -> str | None:
        from cognis.store.queries import get_artifact_record

        async with self._session_factory() as session:
            row = await get_artifact_record(session, attachment.artifact_id)
        if row is None:
            return None
        try:
            content, _content_type = await self._artifact_store.async_load(
                row.namespace,
                row.object_id,
                row.filename,
            )
            return await transcribe_audio_bytes(
                self._providers.llm,
                content,
                mime_type=attachment.mime_type,
                filename=attachment.filename,
                acting_user_email=acting_user_email,
            )
        except Exception:
            logger.warning(
                "turn_scheduler: automatic audio transcription failed",
                extra={
                    "extra_data": {
                        "artifact_id": attachment.artifact_id,
                        "filename": attachment.filename,
                    }
                },
                exc_info=True,
            )
            return None

    def _launch_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        intention_eligible: bool = True,
        user_message_metadata: dict[str, Any] | None = None,
        contextual_messages: list[dict[str, Any]] | None = None,
        attachments: list[AttachmentRef] | None = None,
        outbound_attachments: list[dict[str, Any]] | None = None,
        attachment_notice: str | None = None,
        attachment_context: str | None = None,
        system_initiated: bool = False,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        bootstrap_wait_for_intention: bool = False,
        turn_observers: Sequence[TurnObserver] = (),
        client_message_id: str | None = None,
        queue_id: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
        channel_default_agent_profile_id: str | None = None,
        is_retry: bool = False,
        retry_source_turn_id: str | None = None,
        retry_reason: RetryReason | str | None = None,
        retry_attempt: int = 1,
        turn_id: str | None = None,
        checkpoint_conversation: ConversationModel | None = None,
        checkpoint_session: SessionModel | None = None,
        durable_request_id: str | None = None,
        durable_lease: Lease | None = None,
        durable_user_append_session_id: str | None = None,
        recovery_context: str | None = None,
        channel_delivery: ChannelDeliveryDescriptor | None = None,
    ) -> None:
        """Launch a turn as a background asyncio.Task."""
        conversation_id = conversation.conversation_id
        control = _TurnControl(turn_observers=list(turn_observers))
        control.active_delivery_id = delivery_id
        control.channel_delivery = channel_delivery
        turn_id = turn_id or self.new_turn_id()
        control.turn_id = turn_id
        self._turn_controls[conversation_id] = control
        self._turn_sessions[conversation_id] = session.session_id
        if not system_initiated:
            self._user_turn_counts[user_email] = self._user_turn_counts.get(user_email, 0) + 1
        task_holder: dict[str, asyncio.Task[None]] = {}

        async def _runner() -> None:
            owner_task = task_holder["task"]
            await self._run_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                content=content,
                user_email=user_email,
                intention_eligible=intention_eligible,
                user_message_metadata=user_message_metadata,
                contextual_messages=contextual_messages,
                attachments=attachments,
                outbound_attachments=outbound_attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                system_initiated=system_initiated,
                follow_up=follow_up,
                channel_deliverable=channel_deliverable,
                delivery_id=delivery_id,
                delivery_fallback_text=delivery_fallback_text,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                cancel_event=control.cancel_event,
                turn_control=control,
                turn_id=turn_id,
                client_message_id=client_message_id,
                queue_id=queue_id,
                one_shot_chat_mode=one_shot_chat_mode,
                channel_default_agent_profile_id=channel_default_agent_profile_id,
                is_retry=is_retry,
                retry_source_turn_id=retry_source_turn_id,
                retry_reason=retry_reason,
                retry_attempt=retry_attempt,
                owner_task=owner_task,
                checkpoint_conversation=checkpoint_conversation,
                checkpoint_session=checkpoint_session,
                durable_request_id=durable_request_id,
                durable_lease=durable_lease,
                durable_user_append_session_id=durable_user_append_session_id,
                recovery_context=recovery_context,
                channel_delivery=channel_delivery,
            )

        task = asyncio.create_task(_runner())
        task_holder["task"] = task
        self._active_turns[conversation_id] = task

    async def _run_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        intention_eligible: bool = True,
        attachments: list[AttachmentRef] | None,
        outbound_attachments: list[dict[str, Any]] | None,
        attachment_notice: str | None,
        attachment_context: str | None,
        system_initiated: bool,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool,
        delivery_id: str | None,
        delivery_fallback_text: str | None,
        bootstrap_wait_for_intention: bool,
        cancel_event: asyncio.Event,
        turn_control: _TurnControl | None = None,
        turn_id: str | None = None,
        turn_observers: tuple[TurnObserver, ...] = (),
        client_message_id: str | None = None,
        queue_id: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
        channel_default_agent_profile_id: str | None = None,
        is_retry: bool = False,
        retry_source_turn_id: str | None = None,
        retry_reason: RetryReason | str | None = None,
        retry_attempt: int = 1,
        owner_task: asyncio.Task[None] | None = None,
        checkpoint_conversation: ConversationModel | None = None,
        checkpoint_session: SessionModel | None = None,
        durable_request_id: str | None = None,
        durable_lease: Lease | None = None,
        durable_user_append_session_id: str | None = None,
        recovery_context: str | None = None,
        channel_delivery: ChannelDeliveryDescriptor | None = None,
        user_message_metadata: dict[str, Any] | None = None,
        contextual_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Execute a single chat turn."""
        conversation_id = conversation.conversation_id
        turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        message_id = turn_id
        _pre_turn_title = conversation.title
        start_time = asyncio.get_running_loop().time()
        turn_type = "system" if system_initiated else "user"
        turn_succeeded = False
        turn_ambiguous = False
        ambiguity_detail: dict[str, Any] | None = None
        turn_failure_transient = False
        durable_retry_pending = False
        durable_retry_notice_error: TurnError | None = None
        durable_user_append_phase = (
            "user_appended" if is_retry or system_initiated else "user_append_pending"
        )
        if turn_control is None:
            turn_control = _TurnControl(turn_observers=list(turn_observers))
        turn_control.active_delivery_id = delivery_id
        if channel_delivery is not None:
            turn_control.channel_delivery = channel_delivery
        turn_control.turn_id = turn_id
        # Keep the mutable control-owned list so observers attached by an
        # in-flight managed wait receive subsequent child progress.
        turn_observers = turn_control.turn_observers
        session.channel_default_agent_profile_id = channel_default_agent_profile_id

        resolved_chat_mode = ResolvedChatMode(mode="default", source="system_default")
        user_message_recorded = False
        user_message_event_seq: int | None = None
        execution_fence = (
            self._durable_fences.get(durable_request_id) if durable_request_id is not None else None
        )
        if (
            execution_fence is None
            and self._direct_turn_store is not None
            and durable_request_id is not None
            and durable_lease is not None
        ):
            execution_fence = DirectTurnExecutionFence(
                self._direct_turn_store,
                durable_request_id,
                durable_lease,
            )
        if execution_fence is not None:
            execution_fence.set_user_append_state(
                durable_user_append_phase,
                session_id=(
                    durable_user_append_session_id
                    or session.intaris_session_id
                    or session.session_id
                ),
            )
        try:
            if execution_fence is not None and durable_user_append_phase == "user_append_pending":
                await execution_fence.checkpoint(
                    durable_user_append_phase,
                    session_id=(
                        durable_user_append_session_id
                        or session.intaris_session_id
                        or session.session_id
                    ),
                )
            # Idle checkpoint compaction may require an LLM call and must not
            # hold the message-admission request open.  The turn is already
            # registered as active before this background preflight starts, so
            # later messages queue behind it and the HTTP endpoint can return
            # 202 immediately.
            if (
                not system_initiated
                and checkpoint_conversation is not None
                and checkpoint_session is not None
            ):
                (
                    conversation,
                    session,
                    checkpoint_cancellation_requested,
                ) = await self._prepare_idle_checkpoint_turn_cancellation_safe(
                    conversation=conversation,
                    session=session,
                    agent=agent,
                    checkpoint_conversation=checkpoint_conversation,
                    checkpoint_session=checkpoint_session,
                )
                session.channel_default_agent_profile_id = channel_default_agent_profile_id
                if checkpoint_cancellation_requested:
                    raise asyncio.CancelledError

            await self._mark_managed_conversation_turn_running(
                target_conversation_id=conversation_id,
                target_session_id=session.session_id,
                turn_id=turn_id,
            )

            current_user_email.set(user_email)
            current_agent_id.set(agent.agent_id)
            current_agent_owner_email.set(getattr(agent, "owner_email", user_email))
            conversation_context = getattr(conversation, "context", None)
            platform_data = getattr(conversation_context, "platform_data", None) or {}
            resolved_chat_mode = resolve_chat_mode(
                conversation=conversation,
                agent=agent,
                one_shot_mode=one_shot_chat_mode,
            )
            turn_control.chat_mode = resolved_chat_mode.mode
            turn_control.chat_mode_source = resolved_chat_mode.source
            current_workspace_root.set(platform_data.get("workspace_root"))
            current_effective_working_directory.set(platform_data.get("working_directory"))
            refresh_policy = getattr(self._session_manager, "refresh_intaris_session_policy", None)
            if refresh_policy is not None:
                managed_policy = platform_data.get("managed_session_policy")
                await refresh_policy(
                    session,
                    session_policy_override=(
                        managed_policy if isinstance(managed_policy, dict) else None
                    ),
                )

            if durable_request_id is not None and (system_initiated or is_retry):
                # These paths enter _run_turn after durable ownership is already
                # established, so their request is no longer queue-visible.
                self._remove_durable_queue_cache_entry(
                    conversation_id,
                    durable_request_id,
                )

            if not system_initiated and not is_retry:
                if execution_fence is not None:
                    durable_user_append_phase = "user_append_uncertain"
                    execution_fence.set_user_append_state(
                        durable_user_append_phase,
                        session_id=(
                            durable_user_append_session_id
                            or session.intaris_session_id
                            or session.session_id
                        ),
                    )
                    await execution_fence.checkpoint(
                        durable_user_append_phase,
                        intaris_timeout_seconds=30,
                        session_id=(
                            durable_user_append_session_id
                            or session.intaris_session_id
                            or session.session_id
                        ),
                    )
                (
                    user_message_recorded,
                    user_message_event_seq,
                ) = await self._persist_admitted_user_message(
                    session=session,
                    agent=agent,
                    user_email=user_email,
                    content=content,
                    intention_eligible=intention_eligible,
                    user_message_metadata=user_message_metadata or message_metadata(),
                    contextual_messages=contextual_messages,
                    attachments=attachments or [],
                    turn_id=turn_id,
                    client_message_id=client_message_id,
                    chat_mode=resolved_chat_mode,
                    cancel_event=cancel_event,
                    intaris_session_id_override=durable_user_append_session_id,
                )
                if execution_fence is not None:
                    durable_user_append_phase = "user_appended"
                    execution_fence.set_user_append_state(
                        durable_user_append_phase,
                        session_id=(
                            durable_user_append_session_id
                            or session.intaris_session_id
                            or session.session_id
                        ),
                    )
                    await execution_fence.checkpoint(durable_user_append_phase)
                    await execution_fence.assert_current()
                    direct_turn_store = self._direct_turn_store
                    if direct_turn_store is None or durable_request_id is None:
                        raise StaleDirectTurnOwner(turn_id)
                    marked_running = await direct_turn_store.mark_running(
                        durable_request_id,
                        lease=durable_lease,
                    )
                    if marked_running is None:
                        raise StaleDirectTurnOwner(durable_request_id)
                    self._remove_durable_queue_cache_entry(
                        conversation_id,
                        durable_request_id,
                    )
                    await self._publish_durable_turn_change(marked_running)
                    await execution_fence.checkpoint("model_wait")

            if attachment_notice:
                await self._notify_observers_system_message(
                    conversation_id,
                    attachment_notice,
                    turn_observers=turn_observers,
                )

            logger.info(
                "turn_scheduler: turn started",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "agent_id": agent.agent_id,
                        "system_initiated": system_initiated,
                    }
                },
            )

            # Publish TURN_STARTED lifecycle event
            await self._event_bus.publish(
                Event(
                    type=EventType.TURN_STARTED,
                    data={
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "chat_mode": resolved_chat_mode.mode,
                        "chat_mode_source": resolved_chat_mode.source,
                        "system_initiated": system_initiated,
                    },
                )
            )

            if is_retry and normalize_retry_reason(retry_reason) is RetryReason.MANUAL_RETRY:
                await self._persist_retry_turn_notice(
                    conversation_id=conversation_id,
                    session=session,
                    agent=agent,
                    user_email=user_email,
                    turn_id=turn_id,
                    retry_source_turn_id=retry_source_turn_id,
                    retry_reason=retry_reason,
                    retry_attempt=retry_attempt,
                    turn_observers=turn_observers,
                )

            if system_initiated and follow_up is not None:
                await self._persist_follow_up_turn_notice(
                    conversation_id=conversation_id,
                    session=session,
                    agent=agent,
                    user_email=user_email,
                    follow_up=follow_up,
                    turn_id=turn_id,
                    turn_observers=turn_observers,
                )

            # Publish USER_MESSAGE so WebSocket clients watching this
            # conversation see channel-originated messages in real time
            # (without waiting for a page refresh / history reload).
            if not system_initiated and not is_retry:
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        data=_user_message_event_payload(
                            conversation_id=conversation_id,
                            session_id=session.session_id,
                            content=content,
                            turn_id=turn_id,
                            chat_mode=resolved_chat_mode.mode,
                            chat_mode_source=resolved_chat_mode.source,
                            attachments=[
                                item.model_dump(mode="json") for item in (attachments or [])
                            ],
                            client_message_id=client_message_id,
                            queue_id=queue_id,
                        ),
                    )
                )

            # Decision engine (skip for system-initiated turns)
            if not system_initiated:
                decision = await self._decision_engine.decide(
                    user_message=content,
                    agent=agent,
                )
            else:
                decision = None

            if decision is not None and getattr(decision, "decision", None) == "delegate":
                workflow_id = await self._select_workflow(
                    agent,
                    content,
                    project_id=getattr(conversation, "project_id", None),
                )
                task = await self._task_queue.submit(
                    created_by=user_email,
                    agent_id=agent.agent_id,
                    title=content[:80],
                    description=content,
                    created_by_agent_id=agent.agent_id,
                    source_type="chat",
                    source_ref=conversation_id,
                    source_session_id=session.session_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=workflow_id,
                    project_id=getattr(conversation, "project_id", None),
                    workspace_root=current_workspace_root.get(),
                    working_directory=current_effective_working_directory.get(),
                    status="queued",
                )
                await self._notify_observers(
                    conversation_id,
                    "on_system_message",
                    conversation_id,
                    "Working on that in the background.",
                    turn_observers=turn_observers,
                )
                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    delegated=True,
                    task_id=task.task_id,
                    system_initiated=system_initiated,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                    attachments=normalize_attachment_refs(outbound_attachments or []) or None,
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                )
                turn_control.settled = True
                if is_retry and retry_source_turn_id:
                    await self._persist_retry_source_consumed(
                        session=session,
                        agent=agent,
                        user_email=user_email,
                        retry_source_turn_id=retry_source_turn_id,
                        retry_turn_id=turn_id,
                    )
                await self._publish_turn_completed(result, turn_observers=turn_observers)
                TURNS_TOTAL.labels(outcome="delegated").inc()
                turn_succeeded = True
                return

            # Build streaming callbacks from observers
            (
                on_token,
                on_thinking,
                on_tool_call,
                on_tool_result,
                on_tool_progress,
                on_tool_output_chunk,
                on_context_usage,
            ) = self._build_callbacks(
                conversation_id,
                session.session_id,
                message_id,
                turn_id,
                turn_observers=turn_observers,
            )

            # Seed retries and automatic continuations from the source turn's
            # successfully-executed non-read-only calls. The ledger object is
            # shared with the agent loop, so successful calls remain available
            # even if this turn is later cancelled or fails after side effects.
            source_turn_id = retry_source_turn_id if is_retry else None
            if source_turn_id is None and isinstance(follow_up, ContinuationFollowUp):
                source_turn_id = follow_up.topic_ref
            same_turn_tool_call_ledger = await self._prepare_tool_call_ledger_for_turn(
                conversation_id=conversation_id,
                session=session,
                turn_id=turn_id,
                source_turn_id=source_turn_id,
            )
            if platform_data.get("kind") == "task_control":
                from cognis.core.task_control import build_task_control_turn_context

                task_id = platform_data.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise PermissionError("Task control conversation is missing task scope")
                task_control_context = await build_task_control_turn_context(
                    self._session_factory,
                    task_id=task_id,
                    conversation_id=conversation_id,
                )
                recovery_context = (
                    f"{task_control_context}\n\n{recovery_context}"
                    if recovery_context
                    else task_control_context
                )

            # Execute the turn
            step_output = await self._workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=content,
                intention_eligible=intention_eligible,
                user_message_metadata=user_message_metadata,
                contextual_messages=contextual_messages,
                user_attachments=attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                system_initiated=system_initiated,
                follow_up=follow_up,
                on_progress=on_token,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                on_tool_progress=on_tool_progress,
                on_tool_output_chunk=on_tool_output_chunk,
                on_context_usage=on_context_usage,
                cancel_event=cancel_event,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                turn_id=turn_id,
                client_message_id=client_message_id,
                chat_mode=resolved_chat_mode,
                is_retry=is_retry,
                user_message_already_recorded=user_message_recorded,
                user_message_event_seq=user_message_event_seq,
                consume_boundary_batch=lambda reason: self._consume_queued_batch_for_active_turn(
                    conversation_id,
                    reason=reason,
                ),
                wait_for_boundary_input=lambda after_generation=None: self.wait_for_boundary_input(
                    conversation_id,
                    after_generation=after_generation,
                ),
                get_boundary_action_generation=lambda: self.boundary_action_generation(
                    conversation_id
                ),
                signal_actionable_boundary_event=lambda: self.signal_actionable_boundary_event(
                    conversation_id
                ),
                get_current_assistant_phase=lambda: self._assistant_phase_by_turn.get(
                    (conversation_id, turn_id), 0
                ),
                # Idempotent per-call phase resolution: the first ask (agent
                # loop persisting tool events, or the on_tool_call observer)
                # assigns the phase and bumps the counter; later asks return
                # the recorded value. Live overlay and persisted events
                # therefore always agree on the tool's phase.
                get_assistant_phase_for_tool=lambda call_id: self._bump_assistant_phase_for_tool(
                    conversation_id, turn_id, call_id
                ),
                same_turn_tool_call_ledger=same_turn_tool_call_ledger,
                execution_fence=execution_fence,
                recovery_context=recovery_context,
                on_absorbed_append_start=(
                    lambda request_id, intaris_session_id: self._checkpoint_durable_absorbed_append(
                        request_id,
                        durable_lease,
                        intaris_session_id,
                    )
                )
                if durable_lease is not None
                else None,
                on_absorbed_persisted=(
                    lambda request_id: self._mark_durable_absorbed(
                        request_id,
                        durable_lease,
                    )
                )
                if durable_lease is not None
                else None,
            )
            continuation_metadata = _automatic_continuation_metadata(step_output)
            continuation_scheduled = False
            if execution_fence is not None:
                await execution_fence.checkpoint(
                    "model_response",
                    session_id=session.intaris_session_id or session.session_id,
                )
            if (
                continuation_metadata is not None
                and not channel_deliverable
                and getattr(conversation, "status", "active") == "active"
            ):
                continuation_scheduled = await self._schedule_automatic_continuation(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    turn_id=turn_id,
                    user_email=user_email,
                    metadata=continuation_metadata,
                    prior_follow_up=follow_up,
                    turn_observers=turn_observers,
                    one_shot_chat_mode=resolved_chat_mode.mode
                    if resolved_chat_mode.source == "one_shot"
                    else None,
                )

            step_error = _turn_error_from_step_output(step_output)
            if step_error is not None and not continuation_scheduled:
                retry_pending, step_error = await self._prepare_durable_transient_retry(
                    request_id=durable_request_id,
                    lease=durable_lease,
                    error=step_error,
                    execution_fence=execution_fence,
                )
                durable_retry_pending = retry_pending
                durable_retry_notice_error = step_error if retry_pending else None
                turn_failure_transient = retry_pending or (
                    step_error.transient and durable_request_id is None
                )
                logger.warning(
                    "turn_scheduler: turn step returned error",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "error_code": step_error.code,
                        }
                    },
                )
                if retry_pending:
                    await self._cleanup_durable_retry_attempt(
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                        turn_id=turn_id,
                    )
                    return
                turn_control.settled = True
                selected_delivery_id = delivery_id or turn_control.absorbed_delivery_id
                await self._suppress_absorbed_channel_delivery_intents(
                    turn_control,
                    selected_delivery_id=selected_delivery_id,
                )
                await self._persist_direct_turn_step_error_delivery(
                    request_id=durable_request_id,
                    lease=durable_lease,
                    descriptor=turn_control.channel_delivery,
                    error=step_error,
                )
                await self._publish_turn_error(
                    conversation_id,
                    session.session_id,
                    step_error,
                    turn_id=turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=selected_delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    turn_observers=turn_observers,
                    durable_session=session,
                    durable_agent=agent,
                    durable_user_email=user_email,
                )
                TURNS_TOTAL.labels(outcome="error").inc()
                return

            queued_continuation_pending = self.queued_count(conversation_id) > 0
            # Post-turn housekeeping
            completed_at = datetime.now(UTC)
            await self._touch_conversation(conversation_id, when=completed_at)

            last_seq = 0
            try:
                entry = await self._post_turn_cache_entry(session)
                last_seq = entry.last_event_seq
            except Exception:
                logger.warning(
                    "turn_scheduler: post-turn session cache refresh failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                        }
                    },
                )
                cached = self._session_cache.get_entry(session.session_id)
                if cached is not None:
                    last_seq = cached.last_event_seq

            context_usage = self._session_cache.get_context_usage(session.session_id)
            last_generation = (
                self._session_cache.get_last_generation_performance(session.session_id)
                if hasattr(self._session_cache, "get_last_generation_performance")
                else None
            )
            runtime = assistant_message_runtime_metadata(
                agent,
                self._tool_runtime_info(session.session_id),
            )

            await self._adopt_late_intaris_title(conversation, session)
            latest_title = await self._load_visible_conversation_title(
                conversation_id,
                conversation.title,
            )
            if latest_title is not None:
                conversation.title = latest_title

            # Check title change against the persisted row because Intaris title
            # sync may commit through a separate DB session inside the agent loop.
            already_published_title = self._published_title_updates.get(conversation_id)
            title_changed = bool(
                latest_title
                and latest_title != _pre_turn_title
                and latest_title != already_published_title
            )

            selected_delivery_id = delivery_id or turn_control.absorbed_delivery_id
            await self._suppress_absorbed_channel_delivery_intents(
                turn_control,
                selected_delivery_id=selected_delivery_id,
            )

            result = TurnResult(
                conversation_id=conversation_id,
                session_id=session.session_id,
                message_id=message_id,
                turn_id=turn_id,
                last_seq=last_seq,
                context_usage=context_usage,
                last_generation=last_generation,
                title_changed=title_changed,
                new_title=latest_title if title_changed else None,
                final_content=(
                    step_output.content.strip()
                    if step_output and step_output.content.strip()
                    else None
                ),
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=selected_delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                attachments=(
                    normalize_attachment_refs(
                        [
                            *(step_output.attachments if step_output else []),
                            *(outbound_attachments or []),
                            *turn_control.absorbed_outbound_attachments,
                        ]
                    )
                    or None
                ),
                final_deliverable_id=(
                    getattr(step_output, "deliverable_id", None)
                    if step_output is not None
                    else None
                ),
                completed_at=completed_at,
                chat_mode=resolved_chat_mode.mode,
                chat_mode_source=resolved_chat_mode.source,
                assistant_phase_index=self._assistant_phase_by_turn.get(
                    (conversation_id, turn_id), 0
                ),
                turn_cycle_index=self._turn_cycle_for_turn(conversation_id, turn_id),
                managed_continuation_pending=queued_continuation_pending,
                runtime=runtime,
            )
            turn_control.settled = True
            if is_retry and retry_source_turn_id:
                await self._persist_retry_source_consumed(
                    session=session,
                    agent=agent,
                    user_email=user_email,
                    retry_source_turn_id=retry_source_turn_id,
                    retry_turn_id=turn_id,
                )
            if durable_request_id is not None and durable_lease is not None:
                await self._persist_direct_turn_terminal_delivery(
                    request_id=durable_request_id,
                    lease=durable_lease,
                    descriptor=turn_control.channel_delivery,
                    content=result.final_content or "",
                    attachments=result.attachments,
                )
            await self._publish_turn_completed(result, turn_observers=turn_observers)
            TURNS_TOTAL.labels(outcome="completed").inc()
            turn_succeeded = True

            logger.info(
                "turn_scheduler: turn completed",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "last_seq": last_seq,
                    }
                },
            )

        except asyncio.CancelledError:
            if (
                durable_request_id is not None
                and durable_request_id in self._interrupted_durable_requests
            ):
                current = (
                    await self._direct_turn_store.get(durable_request_id)
                    if self._direct_turn_store is not None
                    else None
                )
                self._interrupted_durable_requests.discard(durable_request_id)
                if (
                    current is not None
                    and current.status == DirectTurnStatus.RECOVERABLE.value
                    and current.cancel_requested_at is None
                ):
                    durable_retry_pending = True
                    turn_failure_transient = True
                    turn_control.settled = True
                    await self._cleanup_durable_retry_attempt(
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                        turn_id=turn_id,
                    )
                    return
            durable_retry_pending = False
            turn_failure_transient = False
            turn_control.settled = True
            completed_at = datetime.now(UTC)
            selected_delivery_id = delivery_id or turn_control.absorbed_delivery_id
            await self._suppress_absorbed_channel_delivery_intents(
                turn_control,
                selected_delivery_id=selected_delivery_id,
            )
            (
                partial_content,
                last_seq,
                turn_cycle_index,
            ) = await self._persist_cancelled_active_stream(
                conversation_id=conversation_id,
                session=session,
                message_id=message_id,
                turn_id=turn_id,
                user_email=user_email,
                agent=agent,
                chat_mode=resolved_chat_mode.mode,
                chat_mode_source=resolved_chat_mode.source,
            )
            if partial_content is not None:
                await self._touch_conversation(conversation_id, when=completed_at)
                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    last_seq=last_seq,
                    final_content=partial_content,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=selected_delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    attachments=(
                        normalize_attachment_refs(
                            [
                                *(outbound_attachments or []),
                                *turn_control.absorbed_outbound_attachments,
                            ]
                        )
                        or None
                    ),
                    completed_at=completed_at,
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    partial=True,
                    finish_reason="user_cancelled",
                    assistant_phase_index=self._assistant_phase_by_turn.get(
                        (conversation_id, turn_id), 0
                    ),
                    turn_cycle_index=turn_cycle_index,
                    runtime=assistant_message_runtime_metadata(
                        agent,
                        self._tool_runtime_info(session.session_id),
                    ),
                )
                await self._publish_turn_completed(result, turn_observers=turn_observers)
            error = TurnError(
                code="turn_cancelled",
                message="The current turn was cancelled.",
                recoverable=True,
            )
            if partial_content is None:
                await self._publish_turn_error(
                    conversation_id,
                    session.session_id,
                    error,
                    turn_id=turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=selected_delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    turn_observers=turn_observers,
                    durable_session=session,
                    durable_agent=agent,
                    durable_user_email=user_email,
                )
            TURNS_TOTAL.labels(outcome="cancelled").inc()
            logger.info(
                "turn_scheduler: turn cancelled",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "turn_id": turn_id,
                    }
                },
            )

        except StaleDirectTurnOwner:
            logger.warning(
                "turn_scheduler: stale direct-turn owner discarded",
                extra={"extra_data": {"conversation_id": conversation_id, "turn_id": turn_id}},
            )
            return
        except Exception as exc:
            try:
                logger.exception(
                    "turn_scheduler: turn failed",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                )
                error = await self._classify_turn_error(exc)
                if isinstance(exc, AmbiguousToolOutcome):
                    turn_ambiguous = True
                    ambiguity_detail = exc.detail()
                retry_pending, error = await self._prepare_durable_transient_retry(
                    request_id=durable_request_id,
                    lease=durable_lease,
                    error=error,
                    execution_fence=execution_fence,
                )
                durable_retry_pending = retry_pending
                durable_retry_notice_error = error if retry_pending else None
                turn_failure_transient = retry_pending or (
                    error.transient and durable_request_id is None
                )
                if retry_pending:
                    await self._cleanup_durable_retry_attempt(
                        conversation_id=conversation_id,
                        session_id=session.session_id,
                        turn_id=turn_id,
                    )
                    return
            except asyncio.CancelledError:
                durable_retry_pending = False
                turn_failure_transient = False
                cancel_event.set()
                turn_control.settled = True
                selected_delivery_id = delivery_id or turn_control.absorbed_delivery_id
                await self._suppress_absorbed_channel_delivery_intents(
                    turn_control,
                    selected_delivery_id=selected_delivery_id,
                )
                await self._publish_turn_error(
                    conversation_id,
                    session.session_id,
                    TurnError(
                        code="turn_cancelled",
                        message="The current turn was cancelled.",
                        recoverable=True,
                    ),
                    turn_id=turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=selected_delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    turn_observers=turn_observers,
                    durable_session=session,
                    durable_agent=agent,
                    durable_user_email=user_email,
                )
                return
            turn_control.settled = True
            selected_delivery_id = delivery_id or turn_control.absorbed_delivery_id
            await self._suppress_absorbed_channel_delivery_intents(
                turn_control,
                selected_delivery_id=selected_delivery_id,
            )
            if durable_request_id is not None and durable_lease is not None and not error.transient:
                await self._persist_direct_turn_terminal_delivery(
                    request_id=durable_request_id,
                    lease=durable_lease,
                    descriptor=turn_control.channel_delivery,
                    content=error.message,
                    attachments=None,
                    error=True,
                )
            await self._publish_turn_error(
                conversation_id,
                session.session_id,
                error,
                turn_id=turn_id,
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=selected_delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                chat_mode=resolved_chat_mode.mode,
                chat_mode_source=resolved_chat_mode.source,
                turn_observers=turn_observers,
                durable_session=session,
                durable_agent=agent,
                durable_user_email=user_email,
            )
            TURNS_TOTAL.labels(outcome="error").inc()

        finally:
            if (
                execution_fence is not None
                and durable_request_id is not None
                and durable_lease is not None
            ):
                settled_status: DirectTurnStatus | None = None
                with contextlib.suppress(StaleDirectTurnOwner):
                    await execution_fence.assert_current()
                    settled_status = await self._settle_durable_direct_turn(
                        request_id=durable_request_id,
                        lease=durable_lease,
                        turn_id=turn_id,
                        succeeded=turn_succeeded,
                        ambiguous=turn_ambiguous,
                        ambiguity_detail=ambiguity_detail,
                        cancelled=cancel_event.is_set(),
                        transient_failure=turn_failure_transient,
                        transient_phase=durable_user_append_phase,
                        transient_session_id=(
                            durable_user_append_session_id
                            or session.intaris_session_id
                            or session.session_id
                        ),
                        interruption_reason=execution_fence.interruption_reason,
                        source_phase=execution_fence.last_phase,
                        retry_after_seconds=execution_fence.retry_after_seconds,
                    )
                if settled_status is DirectTurnStatus.CANCELLED and durable_retry_pending:
                    durable_retry_pending = False
                    durable_retry_notice_error = None
                    turn_failure_transient = False
                    turn_control.settled = True
                    await self._publish_turn_error(
                        conversation_id,
                        session.session_id,
                        TurnError(
                            code="turn_cancelled",
                            message="The current turn was cancelled.",
                            recoverable=True,
                        ),
                        turn_id=turn_id,
                        system_initiated=system_initiated,
                        channel_deliverable=(
                            channel_deliverable or turn_control.absorbed_channel_deliverable
                        ),
                        delivery_id=delivery_id or turn_control.absorbed_delivery_id,
                        delivery_fallback_text=(
                            turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                        ),
                        chat_mode=resolved_chat_mode.mode,
                        chat_mode_source=resolved_chat_mode.source,
                        turn_observers=turn_observers,
                        durable_session=session,
                        durable_agent=agent,
                        durable_user_email=user_email,
                    )
                    direct_turn_store = self._direct_turn_store
                    if direct_turn_store is None or (
                        await direct_turn_store.mark_terminal(
                            durable_request_id,
                            lease=durable_lease,
                            status=DirectTurnStatus.CANCELLED,
                            outcome={"phase": "cancelled_during_transient_retry"},
                        )
                        is None
                    ):
                        raise StaleDirectTurnOwner(durable_request_id)
                if (
                    settled_status is DirectTurnStatus.RECOVERABLE
                    and durable_retry_pending
                    and durable_retry_notice_error is not None
                ):
                    await self._notify_durable_retry_pending(
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        error=durable_retry_notice_error,
                        turn_observers=turn_observers,
                    )
                if durable_retry_pending:
                    self._durable_turn_observers[durable_request_id] = tuple(
                        turn_control.turn_observers
                    )
                else:
                    self._durable_turn_observers.pop(durable_request_id, None)
                if self._direct_turn_runtime is not None:
                    await self._direct_turn_runtime.wake()
            duration = asyncio.get_running_loop().time() - start_time
            TURN_DURATION.labels(type=turn_type).observe(duration)
            queued_to_drain: _QueuedMessage | None = None

            if follow_up is not None:
                if turn_succeeded:
                    await self._mark_follow_up_handled(conversation_id, follow_up.follow_up_id)
                else:
                    await self._mark_follow_up_intent(
                        conversation_id,
                        follow_up.follow_up_id,
                        status="pending" if turn_failure_transient else "failed",
                        error="Follow-up turn did not complete.",
                    )

            absorbed_follow_up_ids = set(turn_control.absorbed_follow_up_ids)
            for follow_up_id in absorbed_follow_up_ids:
                if turn_succeeded:
                    await self._mark_follow_up_handled(conversation_id, follow_up_id)
                else:
                    await self._mark_follow_up_intent(
                        conversation_id,
                        follow_up_id,
                        status="pending" if turn_failure_transient else "failed",
                        error="Absorbing turn did not complete.",
                    )

            current_task = owner_task or asyncio.current_task()
            async with self._turn_lock(conversation_id):
                registered_active = self._active_turns.get(conversation_id)
                registered_control = self._turn_controls.get(conversation_id)
                active_matches = registered_active is current_task or (
                    owner_task is None and registered_active is None
                )
                control_matches = registered_control is turn_control or (
                    owner_task is None and registered_control is None
                )

                if active_matches:
                    self._active_turns.pop(conversation_id, None)
                if control_matches:
                    self._turn_controls.pop(conversation_id, None)
                    self._turn_sessions.pop(conversation_id, None)
                    self._boundary_action_generations.pop(conversation_id, None)
                if (
                    self._pause_waiter.find_pending(
                        pause_type="escalation",
                        conversation_id=conversation_id,
                    )
                    is None
                ):
                    self._escalation_notice_pause_ids.pop(conversation_id, None)
                if not system_initiated:
                    count = self._user_turn_counts.get(user_email, 1)
                    if count <= 1:
                        self._user_turn_counts.pop(user_email, None)
                    else:
                        self._user_turn_counts[user_email] = count - 1

                # Only the task that still owns the active-turn slot may drain
                # the queue.  A stale/cancelled task must never delete a newer
                # task's ownership metadata or launch work in parallel with it.
                async with self._admission_drain_lock:
                    queue = self._queued_messages.get(conversation_id)
                    if active_matches and control_matches and queue:
                        queued_to_drain = queue.popleft()
                        self._queued_relaunches[queued_to_drain.queue_id] = conversation_id

            while queued_to_drain is not None:
                queued = queued_to_drain
                relaunch_accepted = False
                try:
                    queued_channel_profile_id = (
                        await self._current_channel_default_agent_profile_id(
                            conversation_id=conversation_id,
                            account_id=getattr(queued, "channel_account_id", None),
                        )
                    )
                    queued_error = await self.submit_turn(
                        conversation_id,
                        queued.content,
                        user_email=queued.user_email,
                        intention_eligible=getattr(queued, "intention_eligible", True),
                        user_message_metadata=getattr(queued, "user_message_metadata", None),
                        contextual_messages=getattr(queued, "contextual_messages", None),
                        attachments=queued.attachments,
                        outbound_attachments=queued.outbound_attachments,
                        system_initiated=queued.system_initiated,
                        follow_up=queued.follow_up,
                        channel_deliverable=queued.channel_deliverable,
                        delivery_id=queued.delivery_id,
                        delivery_fallback_text=queued.delivery_fallback_text,
                        turn_observers=queued.turn_observers,
                        client_message_id=queued.client_message_id,
                        queued_message_id=queued.queue_id,
                        prepared_attachment_notice=queued.attachment_notice,
                        prepared_attachment_context=queued.attachment_context,
                        one_shot_chat_mode=queued.one_shot_chat_mode,
                        channel_default_agent_profile_id=queued_channel_profile_id,
                        channel_account_id=getattr(queued, "channel_account_id", None),
                        channel_delivery=getattr(queued, "channel_delivery", None),
                        turn_id=getattr(queued, "turn_id", None),
                        is_retry=getattr(queued, "is_retry", False),
                        retry_source_turn_id=getattr(queued, "retry_source_turn_id", None),
                        retry_reason=getattr(queued, "retry_reason", None),
                        retry_attempt=getattr(queued, "retry_attempt", 1),
                    )
                    relaunch_accepted = queued_error is None
                    await self._notify_queue_updated(
                        conversation_id, turn_observers=queued.turn_observers
                    )
                    if queued_error is not None and queued.follow_up is not None:
                        await self._mark_follow_up_intent(
                            conversation_id,
                            queued.follow_up.follow_up_id,
                            status="pending" if queued_error.transient else "failed",
                            error=queued_error.message,
                        )
                    if queued_error is not None:
                        await self._publish_turn_error(
                            conversation_id,
                            getattr(queued, "session_id", None) or "",
                            queued_error,
                            turn_id=getattr(queued, "turn_id", None),
                            system_initiated=queued.system_initiated,
                            channel_deliverable=queued.channel_deliverable,
                            delivery_id=queued.delivery_id,
                            delivery_fallback_text=queued.delivery_fallback_text,
                            turn_observers=queued.turn_observers,
                        )
                except Exception:
                    if queued.follow_up is not None:
                        await self._mark_follow_up_intent(
                            conversation_id,
                            queued.follow_up.follow_up_id,
                            status="pending",
                            error="Queued follow-up submission failed.",
                        )
                    await self._publish_turn_error(
                        conversation_id,
                        getattr(queued, "session_id", None) or "",
                        TurnError(
                            code="queued_submission_failed",
                            message="Queued turn submission failed.",
                            recoverable=True,
                            turn_id=getattr(queued, "turn_id", None),
                        ),
                        turn_id=getattr(queued, "turn_id", None),
                        system_initiated=queued.system_initiated,
                        channel_deliverable=queued.channel_deliverable,
                        delivery_id=queued.delivery_id,
                        delivery_fallback_text=queued.delivery_fallback_text,
                        turn_observers=queued.turn_observers,
                    )
                    logger.exception(
                        "turn_scheduler: failed to load runtime for queued message",
                        extra={"extra_data": {"conversation_id": conversation_id}},
                    )
                finally:
                    self._queued_relaunches.pop(queued.queue_id, None)
                if relaunch_accepted:
                    break
                async with (
                    self._turn_lock(conversation_id),
                    self._admission_drain_lock,
                ):
                    queue = self._queued_messages.get(conversation_id)
                    queued_to_drain = queue.popleft() if queue else None
                    if queued_to_drain is not None:
                        self._queued_relaunches[queued_to_drain.queue_id] = conversation_id

    async def _current_channel_default_agent_profile_id(
        self,
        *,
        conversation_id: str,
        account_id: str | None,
    ) -> str | None:
        """Reload a queued channel turn's fallback from its verified account binding."""

        if account_id is None:
            return None
        try:
            async with self._session_factory() as db_session:
                conversation = await queries.get_conversation(db_session, conversation_id)
                account = await queries.get_channel_account(db_session, account_id)
                route = await queries.get_conversation_channel_route(db_session, conversation_id)
            if (
                conversation is None
                or account is None
                or route is None
                or route[0] != account.channel_type
                or route[1] != account_id
                or account.user_email != conversation.user_email
                or account.agent_id != conversation.agent_id
            ):
                return None
            return getattr(account, "default_agent_profile_id", None)
        except Exception:
            logger.warning(
                "turn_scheduler: failed to refresh queued channel profile",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "account_id": account_id,
                    }
                },
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Observer notification helpers
    # ------------------------------------------------------------------

    def _build_callbacks(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        *,
        turn_observers: tuple[TurnObserver, ...] = (),
    ) -> tuple[Any, Any, Any, Any, Any, Any, Any]:
        """Build streaming callbacks that fan out to registered observers."""

        async def on_token(delta: str, turn_cycle_index: int | None = None) -> None:
            # Fall back to the last recorded turn cycle (NOT the phase counter)
            # when the loop does not supply one. Phase bumps once per tool call
            # and cycle once per LLM call, so a phase fallback stamps tokens
            # with a value that collides with a later cycle's tool group and
            # makes the client fold the streamed answer into that activity.
            effective_turn_cycle_index = self._effective_turn_cycle_for_turn(
                conversation_id,
                turn_id,
                turn_cycle_index,
            )
            chunk_index, content_offset = await self._append_active_stream_chunk(
                conversation_id=conversation_id,
                session_id=session_id,
                message_id=message_id,
                turn_id=turn_id,
                delta=delta,
                turn_cycle_index=effective_turn_cycle_index,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_token,
                        conversation_id,
                        session_id,
                        message_id,
                        turn_id,
                        delta,
                        chunk_index,
                        content_offset,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_thinking(
            block_id: str,
            delta: str,
            title: str | None,
            complete: bool,
            content: str | None = None,
            started_at: str | None = None,
            completed_at: str | None = None,
            duration_ms: int | None = None,
            source: str | None = None,
            provider_block_index: int | None = None,
            turn_cycle_index: int | None = None,
        ) -> None:
            current_phase = (
                self._assistant_phase_by_turn.get((conversation_id, turn_id), 0)
                if turn_id is not None
                else 0
            )
            # See on_token: fall back to the last recorded turn cycle, never the
            # phase counter, to keep thinking segments aligned with their LLM
            # cycle rather than the tool-call phase.
            effective_turn_cycle_index = self._effective_turn_cycle_for_turn(
                conversation_id,
                turn_id,
                turn_cycle_index,
            )
            if hasattr(self._session_cache, "update_active_thinking"):
                self._session_cache.update_active_thinking(
                    session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    block_id=block_id,
                    delta=delta,
                    title=title,
                    complete=complete,
                    content=content,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    source=source,
                    provider_block_index=provider_block_index,
                    assistant_phase_index=current_phase,
                    turn_cycle_index=effective_turn_cycle_index,
                )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_thinking,
                        conversation_id,
                        session_id,
                        message_id,
                        turn_id,
                        block_id,
                        delta,
                        title,
                        complete,
                        content,
                        started_at,
                        completed_at,
                        duration_ms,
                        source,
                        provider_block_index,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_call(
            tool_name: str,
            call_id: str,
            arguments: dict[str, Any] | None = None,
            turn_cycle_index: int | None = None,
        ) -> None:
            if tool_name == "delegate" and isinstance(arguments, dict):
                arguments = {
                    "title": arguments.get("title")
                    or arguments.get("task_title")
                    or "Delegated work",
                    "agent_id": arguments.get("agent_id") or arguments.get("used_agent_id"),
                    "wait": bool(arguments.get("wait", False)),
                    "input_redacted": True,
                }
            await self._reset_active_stream(conversation_id)
            assistant_phase_index = self._bump_assistant_phase_for_tool(
                conversation_id, turn_id, call_id, tool_name
            )
            # Fallback is the last recorded turn cycle (via _effective_turn_cycle
            # _for_turn), NOT the assistant phase. Phase counts tool calls, not
            # LLM cycles; a phase fallback stamps a tool with a later cycle's
            # index and sets has_tool_activity for that cycle, folding the next
            # cycle's streamed answer into this tool's activity.
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id,
                turn_id,
                call_id,
                turn_cycle_index,
            )
            await self._record_active_tool_arguments(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                arguments=arguments,
                turn_cycle_index=effective_turn_cycle_index,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_call,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        arguments,
                        turn_id,
                        assistant_phase_index,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_progress(
            call_id: str,
            tool_name: str,
            progress: dict[str, Any],
            turn_cycle_index: int | None = None,
        ) -> None:
            await self._reset_active_stream(conversation_id)
            # Idempotent per-call phase assignment (side effect kept so the
            # tool's phase matches on_tool_call); the return is unused here.
            self._bump_assistant_phase_for_tool(conversation_id, turn_id, call_id, tool_name)
            # See on_tool_call: fall back to the last recorded turn cycle.
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id,
                turn_id,
                call_id,
                turn_cycle_index,
            )
            await self._update_active_tool_progress(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                progress=progress,
                turn_cycle_index=effective_turn_cycle_index,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_progress,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        progress,
                        turn_id,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                    if hasattr(observer, "on_tool_progress")
                )
            )

        async def on_tool_result(
            call_id: str,
            tool_name: str,
            result: str,
            is_error: bool,
            duration_ms: int | None,
            evaluation: dict[str, Any] | None = None,
            attachments: list[dict[str, Any]] | None = None,
            file_diffs: list[dict[str, Any]] | None = None,
            presentation: dict[str, Any] | None = None,
            turn_cycle_index: int | None = None,
        ) -> None:
            metadata = (
                presentation.get("tool_output_presentation")
                if isinstance(presentation, dict)
                else None
            )
            if not isinstance(metadata, dict):
                metadata = presentation if isinstance(presentation, dict) else None
            await self._finalize_active_tool_output(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                result=result,
                is_error=is_error,
                metadata=metadata,
                turn_cycle_index=turn_cycle_index,
            )
            assistant_phase_index = self._assistant_phase_for_tool(
                conversation_id, turn_id, call_id
            )
            # See on_tool_call: fall back to the last recorded turn cycle.
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id,
                turn_id,
                call_id,
                turn_cycle_index,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_result,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        result,
                        is_error,
                        duration_ms,
                        evaluation,
                        attachments,
                        file_diffs,
                        turn_id,
                        presentation,
                        assistant_phase_index,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_output_chunk(
            call_id: str,
            tool_name: str,
            delta: str,
            stream: str | None = None,
            turn_cycle_index: int | None = None,
        ) -> None:
            chunk_index, content_offset = await self._append_active_tool_output_chunk(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                delta=delta,
                stream=stream,
                turn_cycle_index=turn_cycle_index,
            )
            effective_turn_cycle_index = self._effective_turn_cycle_for_tool(
                conversation_id, turn_id, call_id, turn_cycle_index
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_output_chunk,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        delta,
                        stream,
                        turn_id,
                        chunk_index,
                        content_offset,
                        effective_turn_cycle_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_context_usage(usage: dict[str, Any]) -> None:
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_context_usage,
                        conversation_id,
                        session_id,
                        usage,
                        turn_id,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                    if hasattr(observer, "on_context_usage")
                )
            )

        return (
            on_token,
            on_thinking,
            on_tool_call,
            on_tool_result,
            on_tool_progress,
            on_tool_output_chunk,
            on_context_usage,
        )

    def _iter_observers(
        self,
        conversation_id: str,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> list[TurnObserver]:
        observers: list[TurnObserver] = []
        for observer in (
            *self._global_observers,
            *self._observers.get(conversation_id, []),
        ):
            if not any(registered is observer for registered in observers):
                observers.append(observer)
        for observer in turn_observers or ():
            if (
                not any(registered is observer for registered in observers)
                and (conversation_id, id(observer)) not in self._disabled_observers
            ):
                observers.append(observer)
        return [
            observer
            for observer in observers
            if (conversation_id, id(observer)) not in self._disabled_observers
        ]

    async def _notify_observers(
        self,
        conversation_id: str,
        method: str,
        *args: Any,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Call a method on all observers for a conversation."""
        await asyncio.gather(
            *(
                self._call_observer(conversation_id, observer, getattr(observer, method), *args)
                for observer in self._iter_observers(conversation_id, turn_observers=turn_observers)
            )
        )

    async def _notify_observers_system_message(
        self,
        conversation_id: str,
        text: str,
        *,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
        retry_reason: str | None = None,
        retry_source_turn_id: str | None = None,
        attempt: int | None = None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Send a system message to all observers."""
        await self._notify_observers(
            conversation_id,
            "on_system_message",
            conversation_id,
            text,
            notice_id,
            kind,
            scope,
            turn_id,
            retry_reason,
            retry_source_turn_id,
            attempt,
            turn_observers=turn_observers,
        )

    async def _publish_turn_completed(
        self,
        result: TurnResult,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
        await self._assert_durable_conversation_fence(result.conversation_id)
        await self._reset_active_stream(result.conversation_id)
        # Clear any lingering active-thinking state for this session so that a
        # subsequent conversation_runtime_snapshot on reconnect never re-emits
        # streaming:true thinking items for a finished turn.  The normal drain
        # path (agent_loop finalize_thinking → on_thinking(complete=True)) clears
        # blocks individually, but the CancelledError path bypasses that drain.
        if hasattr(self._session_cache, "clear_active_thinking"):
            self._session_cache.clear_active_thinking(result.session_id)
        managed_settled = await self._notify_managed_turn_result(result)
        managed_expected = any(
            isinstance(observer, ManagedConversationTurnObserver)
            for observer in self._iter_observers(
                result.conversation_id,
                turn_observers=turn_observers,
            )
        )
        if not managed_settled and managed_expected:
            self._clear_assistant_phase(result.conversation_id, result.turn_id)
            return
        await self._persist_follow_up_result_delivery(result)
        await self._event_bus.publish(
            Event(
                type=EventType.TURN_COMPLETED,
                data={
                    "conversation_id": result.conversation_id,
                    "session_id": result.session_id,
                    "message_id": result.message_id,
                    "turn_id": result.turn_id,
                    "last_seq": result.last_seq,
                    "context_usage": result.context_usage,
                    "last_generation": result.last_generation,
                    "delegated": result.delegated,
                    "task_id": result.task_id,
                    "title_changed": result.title_changed,
                    "new_title": result.new_title,
                    "queued_count": self.queued_count(result.conversation_id),
                    "system_initiated": result.system_initiated,
                    "channel_deliverable": result.channel_deliverable,
                    "delivery_id": result.delivery_id,
                    "delivery_fallback_text": result.delivery_fallback_text,
                    "final_content": result.final_content,
                    "completed_at": (
                        result.completed_at.isoformat()
                        if result.completed_at is not None
                        else datetime.now(UTC).isoformat()
                    ),
                    "chat_mode": result.chat_mode,
                    "chat_mode_source": result.chat_mode_source,
                    "attachments": strip_attachment_payload_bytes(result.attachments or []),
                    "final_deliverable_id": result.final_deliverable_id,
                    "partial": result.partial,
                    "finish_reason": result.finish_reason,
                    "turn_cycle_index": result.turn_cycle_index,
                    "managed_continuation_pending": result.managed_continuation_pending,
                },
            )
        )
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_chat_change(
                result.conversation_id,
                session_id=result.session_id,
                revision=result.last_seq,
            )
        self._settle_turn_waiters(result.conversation_id, result)
        await asyncio.gather(
            *(
                self._call_observer(
                    result.conversation_id,
                    observer,
                    observer.on_turn_complete,
                    result,
                )
                for observer in self._iter_observers(
                    result.conversation_id,
                    turn_observers=turn_observers,
                )
            )
        )
        self._clear_assistant_phase(result.conversation_id, result.turn_id)

    async def _persist_follow_up_result_delivery(self, result: TurnResult) -> None:
        """Persist the terminal channel phase before publishing its live event."""

        if not result.channel_deliverable or not result.delivery_id or not result.turn_id:
            return
        from cognis.store.queries import (
            ensure_follow_up_result_delivery,
            get_channel_delivery_outbox,
        )

        async with self._session_factory() as db_session:
            grace_row = await get_channel_delivery_outbox(db_session, result.delivery_id)
            if grace_row is None or grace_row.source_type not in {
                "follow_up",
                "task_result_follow_up",
            }:
                return
            authorized_attachments = await authorize_outbound_artifact_refs_in_session(
                db_session,
                result.attachments or [],
                user_email=grace_row.user_email,
                conversation_id=result.conversation_id,
            )
            await ensure_follow_up_result_delivery(
                db_session,
                grace_delivery_id=result.delivery_id,
                conversation_id=result.conversation_id,
                session_id=result.session_id,
                turn_id=result.turn_id,
                final_content=result.final_content,
                attachments=authorized_attachments or None,
                deliverable_id=result.final_deliverable_id,
            )
            await db_session.commit()

    async def _notify_managed_turn_result(self, result: TurnResult) -> bool:
        """Update managed-conversation state for a completed scheduler turn."""

        if not callable(self._session_factory):
            return False
        from cognis.store import queries

        async with self._session_factory() as db_session:
            binding = await queries.get_managed_channel_binding_for_target(
                db_session,
                result.conversation_id,
                for_update=True,
            )
            if binding is not None:
                link = await queries.get_managed_conversation_link(
                    db_session,
                    binding.link_id,
                    for_update=True,
                )
            else:
                link = await queries.get_managed_conversation_link_for_target(
                    db_session, result.conversation_id
                )
            if link is not None and link.completion_policy == "explicit":
                if link.conversation_state != "open":
                    return True
                if binding is not None and link.last_result_turn_id == result.turn_id:
                    return True
                next_turn_state = (
                    "waiting_controller" if link.turn_state == "waiting_controller" else "idle"
                )
                link.conversation_state = "open"
                link.turn_state = next_turn_state
                link.active_turn_id = None
                link.notify_on_completion = False
                link.last_result_summary = result.final_content
                link.last_result_turn_id = result.turn_id
                link.updated_at = datetime.now(UTC)
                if binding is not None and next_turn_state == "idle":
                    binding.version += 1
                    now = datetime.now(UTC)
                    if result.final_content or result.attachments:
                        authorized_attachments = await authorize_outbound_artifact_refs_in_session(
                            db_session,
                            result.attachments or [],
                            user_email=binding.user_email,
                            conversation_id=link.target_conversation_id,
                        )
                        stable = hashlib.sha256(
                            (
                                f"{binding.binding_id}:{result.turn_id}:"
                                f"{binding.version}:{link.owner_epoch}"
                            ).encode()
                        ).hexdigest()[:24]
                        await queries.create_or_get_channel_delivery_outbox(
                            db_session,
                            delivery_id=f"cdel_mcf_{stable}",
                            user_email=binding.user_email,
                            conversation_id=link.target_conversation_id,
                            session_id=result.session_id,
                            source_type="managed_channel_final",
                            source_id=str(result.turn_id),
                            channel_type=binding.channel_type,
                            account_id=binding.account_id,
                            chat_id=binding.chat_id,
                            thread_id=binding.thread_key or None,
                            fallback_text=result.final_content,
                            attachments=authorized_attachments,
                            next_attempt_at=now,
                            managed_binding_id=binding.binding_id,
                            managed_binding_version=binding.version,
                            managed_owner_epoch=link.owner_epoch,
                        )
                        binding.state = "delivery_pending"
                    else:
                        binding.state = "waiting_external"
                    binding.updated_at = now
                await db_session.commit()
                return True

        cancelled_partial = result.partial and result.finish_reason == "user_cancelled"
        if cancelled_partial:
            return await self._notify_managed_conversation_controller(
                target_conversation_id=result.conversation_id,
                target_session_id=result.session_id,
                turn_state="interrupted",
                conversation_state="open",
                status=FollowUpStatus.CANCELLED,
                summary=result.final_content,
                turn_id=result.turn_id,
                error_message="The current turn was cancelled.",
                recoverable=True,
                clear_active_turn=True,
                notify_on_completion=True,
                completed=False,
            )
        return await self._notify_managed_conversation_controller(
            target_conversation_id=result.conversation_id,
            target_session_id=result.session_id,
            turn_state="running" if result.managed_continuation_pending else "completed",
            conversation_state="open" if result.managed_continuation_pending else "completed",
            status=FollowUpStatus.COMPLETED,
            summary=result.final_content,
            turn_id=result.turn_id,
            clear_active_turn=not result.managed_continuation_pending,
            notify_on_completion=not result.managed_continuation_pending,
            completed=not result.managed_continuation_pending,
        )

    async def _publish_turn_error(
        self,
        conversation_id: str,
        session_id: str,
        error: TurnError,
        *,
        turn_id: str | None = None,
        system_initiated: bool = False,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        chat_mode: ChatMode = "default",
        chat_mode_source: str = "system_default",
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
        durable_session: SessionModel | None = None,
        durable_agent: AgentDefinition | None = None,
        durable_user_email: str | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
        await self._assert_durable_conversation_fence(conversation_id)
        if getattr(error, "turn_id", None) is None:
            error.turn_id = turn_id
        if (
            turn_id is not None
            and durable_session is not None
            and durable_agent is not None
            and durable_user_email is not None
        ):
            try:
                await self._persist_turn_error_event(
                    session=durable_session,
                    agent=durable_agent,
                    user_email=durable_user_email,
                    turn_id=turn_id,
                    error=error,
                    chat_mode=chat_mode,
                    chat_mode_source=chat_mode_source,
                )
            except Exception:
                logger.exception(
                    "turn_scheduler: failed to persist durable turn error",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "error_code": error.code,
                        }
                    },
                )
        await self._reset_active_stream(conversation_id)
        # Mirror the cleanup done in _publish_turn_completed: clear active-thinking
        # state so reconnect snapshots never re-emit stale streaming items.
        if hasattr(self._session_cache, "clear_active_thinking"):
            self._session_cache.clear_active_thinking(session_id)
        managed_settled = await self._notify_managed_turn_error(
            conversation_id=conversation_id,
            session_id=session_id,
            error=error,
            turn_id=turn_id,
        )
        managed_expected = any(
            isinstance(observer, ManagedConversationTurnObserver)
            for observer in self._iter_observers(
                conversation_id,
                turn_observers=turn_observers,
            )
        )
        if not managed_settled and managed_expected:
            return
        await self._event_bus.publish(
            Event(
                type=EventType.TURN_ERROR,
                data={
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "chat_mode": chat_mode,
                    "chat_mode_source": chat_mode_source,
                    "error_code": error.code,
                    "error_message": error.message,
                    "recoverable": error.recoverable,
                    "system_initiated": system_initiated,
                    "channel_deliverable": channel_deliverable,
                    "delivery_id": delivery_id,
                    "delivery_fallback_text": delivery_fallback_text,
                },
            )
        )
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_chat_change(
                conversation_id,
                session_id=session_id,
                revision=turn_id or datetime.now(UTC),
            )
        self._settle_turn_waiters(conversation_id, error)
        await asyncio.gather(
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_turn_error,
                    conversation_id,
                    error,
                )
                for observer in self._iter_observers(
                    conversation_id,
                    turn_observers=turn_observers,
                )
            )
        )

    async def _notify_managed_turn_error(
        self,
        *,
        conversation_id: str,
        session_id: str,
        error: TurnError,
        turn_id: str | None,
    ) -> bool:
        """Update managed-conversation state for a failed scheduler turn."""

        interrupted = error.code in _CANCELLED_TURN_ERROR_CODES
        return await self._notify_managed_conversation_controller(
            target_conversation_id=conversation_id,
            target_session_id=session_id,
            turn_state="interrupted" if interrupted else "failed",
            conversation_state="open",
            status=FollowUpStatus.CANCELLED if interrupted else FollowUpStatus.FAILED,
            summary=error.message,
            turn_id=turn_id,
            error_message=error.message,
            recoverable=error.recoverable,
        )

    def _settle_turn_waiters(
        self,
        conversation_id: str,
        value: TurnResult | TurnError,
    ) -> bool:
        """Resolve futures waiting for the next visible turn settlement."""

        waiters = self._turn_waiters.pop(conversation_id, [])
        for future in waiters:
            if not future.done():
                future.set_result(value)
        self._signal_turn_scope_change(conversation_id)

    async def _notify_managed_conversation_controller(
        self,
        *,
        target_conversation_id: str,
        target_session_id: str,
        turn_state: str,
        conversation_state: str,
        status: FollowUpStatus,
        summary: str | None,
        turn_id: str | None,
        error_message: str | None = None,
        recoverable: bool | None = None,
        clear_active_turn: bool = True,
        notify_on_completion: bool = True,
        completed: bool | None = None,
        already_settled: bool = False,
    ) -> bool:
        """Update managed-conversation state and notify the controller when requested."""

        if not callable(self._session_factory):
            return True
        follow_up: dict[str, Any] | None = None
        controller_conversation_id: str | None = None
        try:
            async with self._session_factory() as db_session:
                link = await queries.get_managed_conversation_link_for_target(
                    db_session,
                    target_conversation_id,
                )
                if link is None:
                    return True
                if not turn_id:
                    logger.warning(
                        "turn_scheduler: refusing uncorrelated managed conversation settlement",
                        extra={
                            "extra_data": {
                                "target_conversation_id": target_conversation_id,
                                "target_session_id": target_session_id,
                                "turn_state": turn_state,
                            }
                        },
                    )
                    return False
                notify = bool(link.notify_on_completion and notify_on_completion)
                controller_conversation_id = link.controller_conversation_id
                control_metadata = (
                    link.control_metadata if isinstance(link.control_metadata, dict) else {}
                )
                if already_settled:
                    settled = bool(
                        link.handoff_state == "fallback_claimed"
                        and link.handoff_target_turn_id == turn_id
                        and link.last_result_turn_id == turn_id
                    )
                else:
                    settled = await queries.settle_managed_conversation_link(
                        db_session,
                        link.link_id,
                        expected_active_turn_id=turn_id,
                        conversation_state=conversation_state,
                        turn_state=turn_state,
                        target_session_id=target_session_id,
                        last_result_summary=summary,
                        last_error=error_message,
                        clear_active_turn_id=clear_active_turn,
                        clear_notify_on_completion=notify_on_completion,
                        completed=completed
                        if completed is not None
                        else conversation_state == "completed",
                    )
                if not settled:
                    logger.info(
                        "turn_scheduler: ignored stale managed conversation settlement",
                        extra={
                            "extra_data": {
                                "link_id": link.link_id,
                                "target_conversation_id": target_conversation_id,
                                "expected_active_turn_id": turn_id,
                                "durable_active_turn_id": link.active_turn_id,
                                "turn_state": turn_state,
                                "already_settled": already_settled,
                            }
                        },
                    )
                    return False
                await queries.mark_conversation_read(db_session, target_conversation_id)
                if not notify:
                    await db_session.commit()
                    return True
                needs_attention = status in {FollowUpStatus.FAILED, FollowUpStatus.CANCELLED}
                manually_cancelled = bool(control_metadata.get("cancelled_by_user"))
                if error_message and summary and error_message not in summary:
                    raw_summary = f"{error_message}\n\nPartial output:\n{summary}"
                else:
                    raw_summary = error_message or summary
                follow_up_summary = truncate_follow_up_text(raw_summary, max_chars=600)
                if manually_cancelled:
                    description = (
                        "The user manually stopped this managed agent work turn from the "
                        "managed conversation UI. Treat it as a user cancellation, not as an "
                        "agent failure. Review the managed conversation before deciding "
                        "whether to continue."
                    )
                    title = f"Agent work cancelled by user: {link.title or target_conversation_id}"
                elif needs_attention:
                    description = (
                        "A managed agent work turn needs attention. "
                        f"Status: {turn_state}. "
                        "Review the managed conversation, then resume with "
                        "agent_conversation_retry if the last turn is recoverable, or continue "
                        "with agent_conversation_send when new instructions are needed."
                    )
                    title = f"Agent work needs attention: {link.title or target_conversation_id}"
                else:
                    description = (
                        "An agent work turn finished. Integrate the result, "
                        "ask the user if clarification is needed, or continue the managed "
                        "conversation with agent_conversation_send."
                    )
                    title = f"Agent work finished: {link.title or target_conversation_id}"
                follow_up = {
                    "version": 1,
                    "follow_up_id": build_follow_up_id(
                        kind="managed_conversation",
                        conversation_id=link.controller_conversation_id,
                        parts={
                            "link_id": link.link_id,
                            "target_conversation_id": target_conversation_id,
                            "turn_id": turn_id,
                            "status": status.value,
                        },
                    ),
                    "mode": FollowUpMode.INTEGRATE.value,
                    "origin_kind": FollowUpOriginKind.OTHER.value,
                    "relevance_hint": FollowUpRelevanceHint.SAME_THREAD.value,
                    "required_action": (
                        FollowUpRequiredAction.INFORM_FAILURE.value
                        if needs_attention
                        else FollowUpRequiredAction.INTEGRATE_RESULT.value
                    ),
                    "topic_ref": target_conversation_id,
                    "status": status.value,
                    "title": title,
                    "summary": follow_up_summary,
                    "description": description,
                    "metadata": {
                        "link_id": link.link_id,
                        "target_agent_id": link.target_agent_id,
                        "target_conversation_id": target_conversation_id,
                        "target_session_id": target_session_id,
                        "target_turn_id": turn_id,
                        "turn_state": turn_state,
                        "recoverable": recoverable,
                        "control_metadata": control_metadata,
                        "cancelled_by_user": manually_cancelled,
                    },
                }
                await self._persist_follow_up_intent(
                    db_session,
                    conversation_id=link.controller_conversation_id,
                    event_payload={
                        "conversation_id": link.controller_conversation_id,
                        "origin_session_id": link.controller_session_id,
                        "follow_up": follow_up,
                    },
                )
                if already_settled:
                    await queries.clear_claimed_managed_conversation_join_notification(
                        db_session,
                        link.link_id,
                        target_turn_id=turn_id,
                    )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to settle managed conversation",
                extra={"extra_data": {"target_conversation_id": target_conversation_id}},
                exc_info=True,
            )
            return False
        if follow_up is not None and controller_conversation_id is not None:
            try:
                await self._event_bus.publish(
                    Event(
                        type=EventType.FOLLOW_UP_TURN_REQUESTED,
                        data={
                            "conversation_id": controller_conversation_id,
                            "origin_session_id": link.controller_session_id,
                            "follow_up": follow_up,
                        },
                    )
                )
            except Exception:
                logger.warning(
                    "turn_scheduler: failed to publish durable managed follow-up intent",
                    extra={"extra_data": {"target_conversation_id": target_conversation_id}},
                    exc_info=True,
                )
        return True

    async def _call_observer(
        self,
        conversation_id: str,
        observer: TurnObserver,
        callback: Any,
        *args: Any,
    ) -> None:
        try:
            awaitable = callback(*args)
        except TypeError:
            trimmed_args = _trim_callback_args(callback, args)
            if len(trimmed_args) == len(args):
                raise
            awaitable = callback(*trimmed_args)
        try:
            await asyncio.wait_for(awaitable, timeout=1.0)
        except Exception:
            key = (conversation_id, id(observer))
            self._observer_failures[key] += 1
            if self._observer_failures[key] >= 3:
                logger.warning(
                    "turn_scheduler: removing unstable observer",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                    exc_info=True,
                )
                self.remove_observer(conversation_id, observer)
                self._disabled_observers.add(key)
            else:
                logger.debug(
                    "turn_scheduler: observer callback failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "failure_count": self._observer_failures[key],
                        }
                    },
                    exc_info=True,
                )
        else:
            self._observer_failures.pop((conversation_id, id(observer)), None)

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    async def _classify_turn_error(self, error: Exception) -> TurnError:
        """Classify a turn error into a structured TurnError."""
        if isinstance(error, AmbiguousToolOutcome):
            return TurnError(
                code="tool_outcome_ambiguous",
                message=str(error),
                recoverable=True,
                transient=False,
            )
        return await classify_turn_error(self._providers, error)

    # ------------------------------------------------------------------
    # Conversation runtime loading
    # ------------------------------------------------------------------

    async def _load_conversation_runtime(
        self,
        conversation_id: str,
        *,
        user_message: str | None = None,
    ) -> tuple[ConversationModel, SessionModel, AgentDefinition, bool] | None:
        """Load conversation, session, and agent for a turn.

        Handles deferred session creation after compaction and brand-new
        conversations without a session.
        """
        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store.queries import (
            get_agent,
            get_conversation,
            get_session_row,
            update_conversation_active_session,
        )

        async with self._session_factory() as session:
            conversation_row = await get_conversation(session, conversation_id)
            if conversation_row is None:
                return None
            agent_row = await get_agent(session, conversation_row.agent_id)
            if agent_row is None:
                return None
            agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
            conversation_model = _to_conversation_model(conversation_row)

            if conversation_row.active_session_id is None:
                lock = self._deferred_creation_locks.setdefault(conversation_id, asyncio.Lock())
                async with lock:
                    async with self._session_factory() as db_session_check:
                        conv_row_check = await get_conversation(db_session_check, conversation_id)
                        if conv_row_check is None:
                            return None
                        if conv_row_check.active_session_id is not None:
                            new_row = await get_session_row(
                                db_session_check, conv_row_check.active_session_id
                            )
                            if new_row is None:
                                return None
                            conversation_model.active_session_id = conv_row_check.active_session_id
                            return (
                                conversation_model,
                                _to_session_model(new_row),
                                agent_model,
                                _should_bootstrap_wait_for_intention(conversation_model),
                            )

                    intention = user_message or f"Conversation with {agent_row.name}"
                    try:
                        root_session = await self._session_manager.ensure_root_session(
                            conversation_id=conversation_row.conversation_id,
                            user_email=conversation_row.user_email,
                            agent_id=conversation_row.agent_id,
                            intention=intention,
                        )
                    except Exception as exc:
                        raise SessionCreationFailedError("Could not create a session") from exc
                    conversation_model.active_session_id = root_session.session_id
                    return conversation_model, root_session, agent_model, True

            session_row = await get_session_row(session, conversation_row.active_session_id)
            if session_row is None and conversation_row.status == "active":
                await update_conversation_active_session(
                    session, conversation_row.conversation_id, None
                )
                await session.commit()
                conversation_model.active_session_id = None

        if session_row is None:
            if (
                conversation_model.status == "active"
                and conversation_model.active_session_id is None
            ):
                return await self._load_conversation_runtime(
                    conversation_id,
                    user_message=user_message,
                )
            return None

        session_model = _to_session_model(session_row)

        # --- Deferred session creation after /compact ---
        if (
            session_model.status == SessionStatus.COMPLETED
            and session_model.completion_reason == "compacted"
        ):
            lock = self._deferred_creation_locks.setdefault(conversation_id, asyncio.Lock())
            async with lock:
                # Double-check: re-read after acquiring lock
                async with self._session_factory() as db_session_check:
                    conv_row_check = await get_conversation(db_session_check, conversation_id)
                if (
                    conv_row_check is not None
                    and conv_row_check.active_session_id != session_model.session_id
                ):
                    # Another caller already rotated
                    async with self._session_factory() as db_session_reload:
                        new_row = await get_session_row(
                            db_session_reload, conv_row_check.active_session_id
                        )
                    if new_row is not None:
                        conversation_model.active_session_id = conv_row_check.active_session_id
                        return (
                            conversation_model,
                            _to_session_model(new_row),
                            agent_model,
                            _should_bootstrap_wait_for_intention(conversation_model),
                        )

                compaction_summary, tail_start_seq = await self._read_compaction_metadata(
                    session_model
                )
                tail_events = (
                    await self._read_tail_events(session_model, tail_start_seq)
                    if tail_start_seq is not None
                    else None
                )
                intention = user_message[:200] if user_message else "Continued conversation"
                if compaction_summary:
                    intention = f"Continuation: {intention}"
                try:
                    new_session = await self._session_manager.rotate_session(
                        conversation_id=conversation_model.conversation_id,
                        current_session=session_model,
                        intention=intention,
                        completion_reason="compacted",
                        transition=SessionTransition.COMPACT,
                        compaction_summary=compaction_summary,
                        tail_events=tail_events,
                    )
                    ROTATION_TOTAL.labels(trigger="deferred").inc()
                    if tail_events:
                        from cognis.core.compaction import COMPACTION_DEFERRED_TAIL_SEEDED

                        COMPACTION_DEFERRED_TAIL_SEEDED.inc(len(tail_events))
                except Exception as exc:
                    raise SessionCreationFailedError(
                        "Could not create session after compaction"
                    ) from exc
                conversation_model.active_session_id = new_session.session_id

                # Pre-populate session cache — rotate_session already seeds tail
                # events via _seed_rotated_tail_events, so we only need a refresh
                # here (no manual apply_compaction with compaction_seq=0).
                if compaction_summary:
                    await self._session_cache.refresh(new_session)

                logger.info(
                    "turn_scheduler: deferred session created after compaction",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "old_session_id": session_model.session_id,
                            "new_session_id": new_session.session_id,
                        }
                    },
                )
                return (
                    conversation_model,
                    new_session,
                    agent_model,
                    _should_bootstrap_wait_for_intention(conversation_model),
                )

        # Periodic cleanup of deferred creation locks (outside the lock block)
        if len(self._deferred_creation_locks) > _MAX_DEFERRED_LOCKS:
            to_remove = [
                cid for cid, lk in self._deferred_creation_locks.items() if not lk.locked()
            ]
            for cid in to_remove:
                self._deferred_creation_locks.pop(cid, None)

        return (
            conversation_model,
            session_model,
            agent_model,
            _should_bootstrap_wait_for_intention(conversation_model),
        )

    async def _read_compaction_metadata(
        self, session: SessionModel
    ) -> tuple[str | None, int | None]:
        """Read the last compaction_summary event from a completed session.

        Returns ``(summary, tail_start_seq)`` so the deferred-rotation path
        can re-fetch the preserved tail events and seed them into the new
        session via ``rotate_session(tail_events=...)``.
        """
        try:
            result = await self._providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=0,
                allow_missing_stream=True,
            )
            for event in reversed(result.events):
                if event.get("type") == "compaction_summary":
                    data = event.get("data", {})
                    return data.get("summary"), data.get("tail_start_seq")
        except Exception:
            logger.warning(
                "turn_scheduler: failed to read compaction metadata",
                extra={"extra_data": {"session_id": session.session_id}},
            )
        return None, None

    async def _read_tail_events(
        self, session: SessionModel, tail_start_seq: int
    ) -> list[Any] | None:
        """Fetch the preserved tail events from the old session's Intaris stream.

        These are the events that were kept verbatim after compaction.  We
        re-fetch them so ``rotate_session`` can seed them into the new session
        via ``_seed_rotated_tail_events``, restoring continuity that would
        otherwise be lost in the deferred-rotation path.

        Returns a list of simple namespace objects with ``.type``, ``.data``,
        and ``.seq`` attributes, matching what ``_seed_rotated_tail_events``
        expects (it uses ``getattr`` for all three fields).
        """
        # Content event types to carry forward; skip system/compaction markers.
        _TAIL_EVENT_TYPES = {
            "user_message",
            "assistant_message",
            "assistant_thinking",
            "tool_call",
            "tool_result",
            "delegation",
        }

        class _TailEvent:
            """Lightweight wrapper so _seed_rotated_tail_events can use getattr."""

            __slots__ = ("type", "data", "seq")

            def __init__(self, etype: str, data: dict[str, Any], seq: int | None) -> None:
                self.type = etype
                self.data = data
                self.seq = seq

        try:
            result = await self._providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=tail_start_seq - 1,
                allow_missing_stream=True,
            )
            wrapped = [
                _TailEvent(
                    etype=e["type"],
                    data=e.get("data", {}),
                    seq=e.get("seq"),
                )
                for e in result.events
                if isinstance(e, dict) and e.get("type") in _TAIL_EVENT_TYPES
            ]
            return wrapped or None
        except Exception:
            logger.warning(
                "turn_scheduler: failed to read tail events for deferred rotation",
                extra={
                    "extra_data": {
                        "session_id": session.session_id,
                        "tail_start_seq": tail_start_seq,
                    }
                },
            )
            return None

    async def _post_turn_cache_entry(self, session: Any) -> Any:
        """Return authoritative cached state without an unnecessary Intaris read."""
        entry = self._session_cache.get_entry(session.session_id)
        if (
            entry is None
            or not bool(getattr(entry, "initialized", False))
            or bool(getattr(entry, "canonical_stale", False))
        ):
            return await self._session_cache.refresh(session)
        return entry

    async def _touch_conversation(
        self, conversation_id: str, *, when: datetime | None = None
    ) -> None:
        """Update last_message_at on the conversation for unread tracking."""
        try:
            async with self._session_factory() as db_session:
                row = await queries.get_conversation(db_session, conversation_id)
                if row is not None:
                    timestamp = when or datetime.now(UTC)
                    row.last_message_at = timestamp
                    row.updated_at = timestamp
                    await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to update last_message_at",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )


# ---------------------------------------------------------------------------
# Error classification (standalone for testability)
# ---------------------------------------------------------------------------


async def classify_turn_error(providers: Any, error: Exception) -> TurnError:
    """Classify a turn error into a structured TurnError.

    This is a standalone function (not a method) so it can be tested
    independently without constructing a full TurnScheduler.
    """
    lowered = str(error).lower()
    safe_detail = sanitize_client_error_detail(error, fallback="request failed")

    if isinstance(error, SessionCreationFailedError):
        return TurnError(
            code="session_creation_failed",
            message="Could not create a session. Try again or check the diagnostics page.",
            recoverable=True,
            detail={"error_detail": safe_detail},
            transient=True,
        )
    if isinstance(error, ImmutablePrefixUnavailable):
        return TurnError(
            code="immutable_prefix_unavailable",
            message="Immutable prefix is unavailable for this session.",
            recoverable=False,
            detail={"error_detail": safe_detail, "reason": error.reason},
        )
    if isinstance(error, TransientExecutorUnavailable):
        return TurnError(
            code="executor_unavailable",
            message="The selected executor is temporarily unavailable. Try again shortly.",
            recoverable=True,
            detail={
                "error_detail": safe_detail,
                "executor_id": error.executor_id,
                "retry_after_seconds": error.retry_after_seconds,
            },
            transient=True,
        )
    if isinstance(error, ValueError) and "no llm model configured" in lowered:
        return TurnError(
            code="provider_not_configured:llm",
            message="No LLM provider is configured. Go to Settings > Providers to add one.",
            recoverable=True,
            detail={"error_detail": safe_detail},
        )

    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        transient = status_code == 429 or 500 <= status_code < 600
        return TurnError(
            code="provider_error:llm",
            message="A provider request failed while processing this turn.",
            recoverable=transient,
            detail={"error_detail": safe_detail, "status_code": status_code},
            transient=transient,
        )

    provider_checks: list[tuple[str, Any]] = []
    for provider_name in ("guardrails", "llm", "memory"):
        provider = getattr(providers, provider_name, None)
        if provider is None:
            continue
        try:
            provider_checks.append((provider_name, await provider.health()))
        except Exception:
            continue

    for provider_name, health in provider_checks:
        if health.status == "healthy":
            continue
        if provider_name == "guardrails":
            return TurnError(
                code="provider_unreachable:guardrails",
                message="Guardrails service is unreachable — tool calls are blocked until it recovers. Check that Intaris is running.",
                recoverable=True,
                detail={"error_detail": safe_detail},
                transient=True,
            )
        if provider_name == "llm":
            if "no llm model configured" in lowered or "not configured" in lowered:
                return TurnError(
                    code="provider_not_configured:llm",
                    message="No LLM provider is configured. Go to Settings > Providers to add one.",
                    recoverable=True,
                    detail={"error_detail": safe_detail},
                )
            return TurnError(
                code="provider_error:llm",
                message="LLM provider returned an error. Check your provider configuration in Settings.",
                recoverable=True,
                detail={"error_detail": safe_detail},
                transient=True,
            )
        if provider_name == "memory":
            return TurnError(
                code="provider_unreachable:memory",
                message="Memory is currently unavailable — this conversation won't have access to past context.",
                recoverable=True,
                detail={"error_detail": safe_detail},
                transient=True,
            )

    if isinstance(error, (httpx.RequestError, TimeoutError)):
        return TurnError(
            code="provider_error:llm",
            message="A provider request failed while processing this turn.",
            recoverable=True,
            detail={"error_detail": safe_detail},
            transient=True,
        )

    return TurnError(
        code="turn_failed",
        message="Turn execution failed.",
        recoverable=True,
        detail={"error_detail": safe_detail},
    )


# ---------------------------------------------------------------------------
# Follow-up prompt builder
# ---------------------------------------------------------------------------


def _build_follow_up_prompt(
    status: str | None,
    *,
    task_id: str | None = None,
    task_title: str | None = None,
    result_summary: str | None = None,
    description: str | None = None,
    source_type: str | None = None,
    gate_message: str | None = None,
    gate_options: list[dict[str, Any]] | None = None,
) -> str:
    """Build a system prompt for the follow-up turn after a task/delegation completes.

    The prompt provides facts about the completed task and lets the LLM
    decide how to present the result based on the agent's personality and
    the user's preferences (which are already in context via Mnemory).
    """
    status_name = (status or "updated").lower()

    # Task-specific prompts (from workflow engine)
    if task_id:
        is_scheduled = source_type == "scheduler"
        prefix = "Scheduled task" if is_scheduled else "Background task"
        title_str = f'"{task_title}"' if task_title else task_id

        if status_name == "completed":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) has completed."]
            if is_scheduled:
                lines.append("This task runs on a recurring schedule.")
            if description:
                lines.append(f"\nTask description: {description}")
            if result_summary:
                lines.append(f"\nResult summary: {result_summary}")
            lines.append(
                "\nDecide how to handle this result based on the user's preferences "
                "and the context. You may present the summary directly if it is "
                "sufficient, or use the get_task_output tool with "
                f'task_id="{task_id}" to retrieve the full output first if you '
                "need more detail for a complete response."
            )
            return "\n".join(lines)

        if status_name == "failed":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) has failed."]
            if description:
                lines.append(f"\nTask description: {description}")
            if result_summary:
                lines.append(f"\nError details: {result_summary}")
            lines.append(
                "\nInform the user about the failure. Do not attempt to retry "
                "or recreate the task automatically — let the user decide how "
                "to proceed."
            )
            return "\n".join(lines)

        if status_name == "cancelled":
            return (
                f"{prefix} {title_str} (task_id: {task_id}) was cancelled. "
                "Provide a brief follow-up to the user if warranted."
            )

        if status_name == "paused":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) needs your attention."]
            if gate_message:
                lines.append(f"\nReason: {gate_message}")
            if gate_options:
                option_labels = [
                    opt.get("label", opt.get("action", "?"))
                    for opt in gate_options
                    if isinstance(opt, dict)
                ]
                if option_labels:
                    lines.append(f"Available actions: {', '.join(option_labels)}")
            lines.append(
                "\nExplain to the user why the task paused and what their options "
                "are. If the task exhausted its retry attempts, explain what went "
                "wrong. The user can resolve the paused gate with `resolve_task_pause` "
                "(retry, continue, or cancel). Do NOT choose automatically — let the "
                "user decide."
            )
            return "\n".join(lines)

        # Generic task update
        return (
            f"{prefix} {title_str} (task_id: {task_id}) status: {status_name}. "
            f"Summary: {result_summary or 'No summary available.'}."
        )

    # Delegation-specific prompts (from agent_loop async delegations)
    if status_name == "failed":
        return (
            "A delegated sub-session has failed. "
            "Review the recent delegation_failed event in the session history "
            "and provide a concise user-facing follow-up."
        )
    if status_name == "completed":
        return (
            "A delegated sub-session has completed. "
            "Review the recent delegation_completed event in the session history "
            "and present the result to the user."
        )
    return (
        "A background operation has completed. "
        "Review the recent events in the session history and provide a concise follow-up."
    )
