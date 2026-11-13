"""Internal message marker keys used across the context assembly pipeline.

These string constants are stamped onto message dicts as they flow through
context assembly, projection, and pruning.  They are stripped before the
messages are sent to any LLM provider.

Centralising them here prevents typo-driven bugs and makes it easy to grep
for every site that reads or writes a given marker.
"""

from __future__ import annotations

# ── Prefix / structure markers ────────────────────────────────────────────────

# Set on the consolidated system message that forms the immutable prefix.
# Used by _find_cache_breakpoint to locate the provider cache-hint boundary.
IMMUTABLE_PREFIX = "_immutable_prefix"

# Set on system messages that represent a workflow-task or follow-up turn
# boundary.  Replaces the fragile "## Workflow Task" string sniff in
# _is_real_turn_boundary / _latest_real_user_index.
TURN_BOUNDARY = "_turn_boundary"

# Set on system messages injected as per-turn routing reminders.
# Recorded canonically with explicit request-only scope; stripped at dispatch.
ROUTING_REMINDER = "_routing_reminder"

# Set on messages that carry prior-step context (cross-step continuation).
PRIOR_CONTEXT = "_prior_context"

# Ordinary prompt content not yet represented by a canonical assistant event.
PROMPT_TRANSIENT = "_prompt_transient"

# Set on messages that carry follow-up context blocks.
FOLLOW_UP_CONTEXT = "_follow_up_context"

# Set on project-context system messages.
PROJECT_CONTEXT = "_project_context"

# Set on the latest mutable Mnemory recall block. Older replayed recall blocks
# intentionally do not carry this marker so context pruning can drop them under
# pressure.
LATEST_MEMORY_RECALL_CONTEXT = "_latest_memory_recall_context"

# Set on tail reminders that describe the current executor pool.
EXECUTOR_POOL_REMINDER = "_executor_pool_reminder"

# Set on agent-loop tail reminders.
EXECUTOR_REMINDER = "_executor_reminder"
BACKGROUND_SHELL_STATUS_REMINDER = "_background_shell_status_reminder"
BACKGROUND_WORK_STATUS_REMINDER = "_background_work_status_reminder"
WORKFLOW_STEP_REMINDER = "_workflow_step_reminder"
# Set on the one-off notice that the user moved a running tool call to the
# background. The durable copy is a recorded system_notice lifecycle event.
TOOL_BACKGROUND_NOTICE = "_tool_background_notice"
FORCE_SUMMARY = "_force_summary"

# ── Audit markers ─────────────────────────────────────────────────────────────

# Source label for audit collection (e.g. "memory_search", "tool_result").
AUDIT_SOURCE = "_audit_source"

# Role override used during audit collection.
AUDIT_ROLE = "_audit_role"

# Extra metadata copied onto persisted audit events.
AUDIT_METADATA = "_audit_metadata"

# ── Tool output / projection markers ─────────────────────────────────────────

# Marks a tool result message as protected (never compacted by projection).
PROTECTED_TOOL_OUTPUT = "_protected_tool_output"

# True when the full tool output is available (not truncated for the model).
HAS_FULL_OUTPUT = "_has_full_output"

# call_id of the recoverable tool-output store entry for this result.
RECOVERY_CALL_ID = "_recovery_call_id"

# Artifact store ID for the tool output (used by the recovery path).
TOOL_OUTPUT_ARTIFACT_ID = "_tool_output_artifact_id"

# Original call_id when this result was derived from another call.
SOURCE_CALL_ID = "_source_call_id"

# Raw byte/char size of the tool output before any truncation.
OUTPUT_SIZE = "_output_size"

# True when the agent-visible content was truncated relative to the raw output.
AGENT_VISIBLE_TRUNCATED = "_agent_visible_truncated"

# Presentation metadata dict for the tool output (anchors, format, etc.).
TOOL_OUTPUT_PRESENTATION = "_tool_output_presentation"

# True when anchored output is available for this result.
ANCHORS_AVAILABLE = "_anchors_available"

# Number of anchors in the anchored output.
ANCHOR_COUNT = "_anchor_count"

# Concrete persisted anchor names available for tool-output recovery.
ANCHOR_NAMES = "_anchor_names"

# Safe lazy artifact refs derived from persisted artifact-candidate anchors.
LAZY_ARTIFACT_REFS = "_lazy_artifact_refs"

# True when the result was already pruned to a compact view.
PRUNED_VIEW = "_pruned_view"

# Marks a message as a tool attachment context block (media produced by tools).
TOOL_ATTACHMENT_CONTEXT = "_tool_attachment_context"

# call_id associated with a tool attachment context block.
TOOL_CALL_ID = "_tool_call_id"

# Tool name associated with a tool result (used in placeholder generation).
TOOL_NAME = "_tool_name"

# Set by the projection pass when a tool result has been replaced with a
# compact placeholder.  Prevents double-compaction on subsequent passes.
PROJECTED_COMPACTED = "_projected_compacted"

# Intaris event sequence ids that contributed to a replayed message. Used only
# to persist deterministic omission decisions when the hard prompt cap drops
# complete history groups.
PROJECTION_EVENT_SEQS = "_projection_event_seqs"

# Replay-local file references depend on their source presentation message.
PRESENTATION_SOURCE = "_presentation_source"
PRESENTATION_REFERENCE = "_presentation_reference"
PRESENTATION_REFERENCE_TEXT = "_presentation_reference_text"

# ── Token estimate cache ──────────────────────────────────────────────────────

# Cached token count for a message dict (set by projection, reused on
# subsequent cycles to avoid re-counting unchanged messages).
TOKEN_ESTIMATE = "_token_estimate"

# ── All internal marker keys (for bulk strip before LLM dispatch) ─────────────

#: Complete set of marker keys that must be stripped before sending messages
#: to any LLM provider.  Add new markers here when they are introduced.
ALL_INTERNAL_MARKERS: frozenset[str] = frozenset(
    {
        PRESENTATION_SOURCE,
        PRESENTATION_REFERENCE,
        PRESENTATION_REFERENCE_TEXT,
        IMMUTABLE_PREFIX,
        TURN_BOUNDARY,
        ROUTING_REMINDER,
        PRIOR_CONTEXT,
        PROMPT_TRANSIENT,
        FOLLOW_UP_CONTEXT,
        PROJECT_CONTEXT,
        LATEST_MEMORY_RECALL_CONTEXT,
        EXECUTOR_POOL_REMINDER,
        EXECUTOR_REMINDER,
        BACKGROUND_SHELL_STATUS_REMINDER,
        BACKGROUND_WORK_STATUS_REMINDER,
        WORKFLOW_STEP_REMINDER,
        TOOL_BACKGROUND_NOTICE,
        FORCE_SUMMARY,
        AUDIT_SOURCE,
        AUDIT_ROLE,
        AUDIT_METADATA,
        PROTECTED_TOOL_OUTPUT,
        HAS_FULL_OUTPUT,
        RECOVERY_CALL_ID,
        TOOL_OUTPUT_ARTIFACT_ID,
        SOURCE_CALL_ID,
        OUTPUT_SIZE,
        AGENT_VISIBLE_TRUNCATED,
        TOOL_OUTPUT_PRESENTATION,
        ANCHORS_AVAILABLE,
        ANCHOR_COUNT,
        ANCHOR_NAMES,
        LAZY_ARTIFACT_REFS,
        PRUNED_VIEW,
        TOOL_ATTACHMENT_CONTEXT,
        TOOL_CALL_ID,
        TOOL_NAME,
        PROJECTED_COMPACTED,
        PROJECTION_EVENT_SEQS,
        TOKEN_ESTIMATE,
    }
)
