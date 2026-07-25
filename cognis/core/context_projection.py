"""Model-facing projection of rich session history.

This module keeps the durable transcript rich for audit/UI while projecting
older tool groups into deterministic compact placeholders before they are sent
back to the model.

Design principles
-----------------
* **Projection is a pure function** of the message list and policy.  No I/O,
  no side-effects.  Callers own the state.
* **Within-turn re-projection is conditional**.  The first call per turn
  (cross-turn, from context assembly) always runs.  Subsequent within-turn
  calls only run when real pressure exists — see ``should_reproject``.
* **Monotonic preservation**.  Once a tool group has been sent to the model
  in preserved form within a turn, it stays preserved unless the turn enters
  ``critical`` pressure mode.  This prevents "the file I just read" from
  silently becoming a placeholder mid-turn.
* **Token-based budgets**.  All size estimates use a cached per-message token
  count (``_TOKEN_ESTIMATE`` marker) rather than raw JSON byte counts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from cognis.core.message_markers import (
    ANCHOR_NAMES,
    PROJECTED_COMPACTED,
    PROTECTED_TOOL_OUTPUT,
    RECOVERY_CALL_ID,
    SOURCE_CALL_ID,
    TOKEN_ESTIMATE,
    TOOL_ATTACHMENT_CONTEXT,
    TOOL_CALL_ID,
    TOOL_NAME,
    TURN_BOUNDARY,
)
from cognis.core.tool_output_presentation import (
    build_recovery_hint,
)
from cognis.core.tool_output_presentation import (
    lazy_artifact_refs as build_lazy_artifact_refs,
)

# ── Public constants ──────────────────────────────────────────────────────────

DEFAULT_PRESERVED_TOOL_GROUPS = 10
DEFAULT_PRESERVED_TOOL_TOKENS = 50_000  # replaces old byte constant
DEFAULT_MAX_HISTORICAL_TOOL_RESULT_TOKENS = 5_000  # replaces old byte constant
# Backward-compatible alias used by existing call sites.
DEFAULT_COMPACTED_TOOL_GROUPS = DEFAULT_PRESERVED_TOOL_GROUPS
_ARG_CLEAR_THRESHOLD = 6_000
_ARG_STRUCTURED_PREVIEW_CHARS = 500

# Pressure escalation thresholds (fraction of available_prompt_tokens).
# normal → pressure: usage crosses this fraction for one cycle.
PRESSURE_ESCALATE_FRACTION = 0.92
# pressure → critical: usage crosses this fraction for one cycle, OR an
# oversized tool result was just appended.
CRITICAL_ESCALATE_FRACTION = 0.97
# Demotion requires this many consecutive cycles back under the lower band.
PRESSURE_DEMOTION_CYCLES = 2

ProjectionPhase = Literal["cross_turn", "within_turn"]


# ── Pressure mode ─────────────────────────────────────────────────────────────


class PressureMode(StrEnum):
    """Context-window pressure level for projection decisions."""

    normal = "normal"
    pressure = "pressure"
    critical = "critical"


# Keep the old Literal alias for call sites that haven't migrated yet.
ProjectionPressureMode = Literal["normal", "pressure", "critical"]


# ── Budget helpers ────────────────────────────────────────────────────────────


def default_token_estimate(text: str) -> int:
    """Cheap heuristic: 1 token ~= 3.5 chars (slightly tighter than old 4)."""

    return max(1, int(len(text) / 3.5))


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _steady_prompt_cap(max_context_tokens: int) -> int:
    if max_context_tokens <= 150_000:
        return 95_000
    if max_context_tokens <= 300_000:
        return 180_000
    if max_context_tokens <= 500_000:
        return 250_000
    return 320_000


def _burst_prompt_cap(max_context_tokens: int) -> int:
    if max_context_tokens <= 150_000:
        return 115_000
    if max_context_tokens <= 300_000:
        return 245_000
    if max_context_tokens <= 500_000:
        return 360_000
    return 600_000


def _historical_tool_result_tokens(
    steady_target_tokens: int, pressure_mode: PressureMode | ProjectionPressureMode
) -> int:
    base = _clamp(int(steady_target_tokens * 0.08), 1_500, 8_000)
    if pressure_mode == "pressure":
        return max(1_000, int(base * 0.65))
    if pressure_mode == "critical":
        return max(500, int(base * 0.35))
    return base


def _prune_protect_tokens(
    steady_target_tokens: int,
    burst_target_tokens: int,
    phase: ProjectionPhase,
    pressure_mode: PressureMode | ProjectionPressureMode,
) -> int:
    base_tokens = burst_target_tokens if phase == "within_turn" else steady_target_tokens
    if pressure_mode == "normal":
        return _clamp(int(base_tokens * 0.18), 8_000, 80_000)
    if pressure_mode == "pressure":
        return _clamp(int(base_tokens * 0.10), 4_000, 40_000)
    return _clamp(int(base_tokens * 0.04), 1_000, 12_000)


def _prune_minimum_savings_tokens(
    steady_target_tokens: int, pressure_mode: PressureMode | ProjectionPressureMode
) -> int:
    if pressure_mode == "normal":
        return _clamp(int(steady_target_tokens * 0.06), 2_000, 20_000)
    if pressure_mode == "pressure":
        return _clamp(int(steady_target_tokens * 0.02), 1_000, 8_000)
    return 1_000


# ── Placeholder builders ──────────────────────────────────────────────────────


def build_compacted_tool_result_placeholder(message: dict[str, Any]) -> str:
    """Build a deterministic placeholder for a compacted tool result."""

    tool_name = str(message.get(TOOL_NAME) or "tool")
    recovery_call_id = message.get(RECOVERY_CALL_ID)
    if not isinstance(recovery_call_id, str) or not recovery_call_id.strip():
        recovery_call_id = None
    original_call_id = str(message.get("tool_call_id") or "unknown")
    source_call_id = message.get(SOURCE_CALL_ID)
    if not isinstance(source_call_id, str) or not source_call_id.strip():
        source_call_id = None
    output_size = message.get("_output_size")
    size_note = ""
    if isinstance(output_size, int) and output_size > 0:
        size_label = "Original output size"
        if message.get("_agent_visible_truncated") is True:
            size_label = "Raw output size"
        size_note = f" {size_label}: {int(output_size):,} chars."
    source_note = ""
    if source_call_id is not None and source_call_id not in {original_call_id, recovery_call_id}:
        source_note = f" This helper output was derived from source call_id '{source_call_id}'."
    if recovery_call_id is None:
        return (
            "[Tool output omitted from prompt. "
            f"Tool: {tool_name}. Original call_id: {original_call_id}.{size_note}{source_note} "
            "No saved output handle is available.]"
        )
    anchor_names = message.get(ANCHOR_NAMES)
    if not isinstance(anchor_names, list):
        presentation = message.get("_tool_output_presentation")
        anchor_names = presentation.get("anchors", []) if isinstance(presentation, dict) else []
    anchor_names = [name for name in anchor_names if isinstance(name, str) and name.strip()]
    recovery_hint = build_recovery_hint(
        recovery_call_id,
        anchors_available=message.get("_anchors_available") is True,
        anchor_count=message.get("_anchor_count")
        if isinstance(message.get("_anchor_count"), int)
        else None,
        anchor_names=anchor_names,
    )
    presentation = message.get("_tool_output_presentation")
    lazy_refs = presentation.get("lazy_artifact_refs", []) if isinstance(presentation, dict) else []
    allowed_lazy_refs = set(build_lazy_artifact_refs(recovery_call_id, anchor_names))
    lazy_refs = [
        ref
        for ref in lazy_refs
        if isinstance(ref, str) and ref.strip() and ref in allowed_lazy_refs
    ]
    lazy_note = (
        " Materialize lazy artifacts with artifact_read: " + ", ".join(lazy_refs) + "."
        if lazy_refs
        else ""
    )
    return (
        "[Tool output omitted from prompt. "
        f"Tool: {tool_name}. Original call_id: {original_call_id}.{size_note}{source_note} "
        f"{recovery_hint}{lazy_note} Only recover if a specific missing detail is needed.]"
    )


def build_compacted_tool_attachment_placeholder(message: dict[str, Any]) -> str:
    """Build a deterministic placeholder for compacted tool attachment context."""

    recovery_call_id = message.get(RECOVERY_CALL_ID) or message.get(TOOL_CALL_ID)
    if isinstance(recovery_call_id, str) and recovery_call_id.strip():
        return (
            "[Tool attachment context cleared from view. Tool attachment context compacted from prompt. "
            f"Use read_tool_output(call_id='{recovery_call_id}') or the UI attachment viewer "
            "if the missing attachment content matters.]"
        )
    return "[Tool attachment context cleared from view. Tool attachment context compacted from prompt.]"


def clear_large_tool_call_arguments(
    message: dict[str, Any],
    *,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
) -> dict[str, Any]:
    """Clear oversized assistant tool-call arguments in a projected message."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return dict(message)
    cleared_calls: list[Any] = []
    changed = False
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            cleared_calls.append(tool_call)
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            cleared_calls.append(tool_call)
            continue
        raw_args = function.get("arguments", {})
        args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, default=str)
        if len(args_str) <= arg_clear_threshold:
            cleared_calls.append(tool_call)
            continue
        changed = True
        preview: dict[str, Any] = {"_cleared": f"[Arguments cleared - {len(args_str)} chars]"}
        parsed_args: Any | None = None
        if isinstance(raw_args, dict):
            parsed_args = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_args = None
        if isinstance(parsed_args, dict):
            for key in ("file_path", "path", "source_path", "target_path"):
                value = parsed_args.get(key)
                if isinstance(value, str) and value:
                    preview[key] = value
            for key, value in parsed_args.items():
                if not isinstance(value, str) or key in preview:
                    continue
                preview[f"{key}_preview"] = value[:_ARG_STRUCTURED_PREVIEW_CHARS]
                if len(value) > _ARG_STRUCTURED_PREVIEW_CHARS:
                    preview[f"{key}_preview_truncated"] = True
                break
        else:
            preview["arguments_preview"] = args_str[:_ARG_STRUCTURED_PREVIEW_CHARS]
            if len(args_str) > _ARG_STRUCTURED_PREVIEW_CHARS:
                preview["arguments_preview_truncated"] = True
        cleared_calls.append(
            {
                **tool_call,
                "function": {
                    **function,
                    "arguments": json.dumps(preview),
                },
            }
        )
    if not changed:
        return dict(message)
    return {**message, "tool_calls": cleared_calls}


# ── Core result types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectionResult:
    """Projected model transcript and its mutable tail boundary."""

    messages: list[dict[str, Any]]
    mutable_start_index: int

    def append_tail(self, new_tail: list[dict[str, Any]]) -> ProjectionResult:
        """Return a new result with *new_tail* appended verbatim.

        Used by the skip-reproject path to cheaply extend the projection
        without re-running the full compaction logic.  ``mutable_start_index``
        is unchanged — the tail is always mutable by definition.
        """
        return ProjectionResult(
            messages=list(self.messages) + list(new_tail),
            mutable_start_index=self.mutable_start_index,
        )


# ── Projection policy ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectionPolicy:
    """Budget-derived policy for model-facing transcript projection.

    Large context windows are treated as safety margin first. Cross-turn replay
    remains conservative, while within-turn projection may spend more budget on
    active evidence before falling back to recoverable placeholders.
    """

    phase: ProjectionPhase
    pressure_mode: PressureMode | ProjectionPressureMode
    max_context_tokens: int
    available_prompt_tokens: int
    steady_target_tokens: int
    burst_target_tokens: int
    hard_prompt_tokens: int
    cross_turn_tool_budget_tokens: int
    within_turn_tool_budget_tokens: int
    preserve_recent_completed_tool_groups: int
    preserve_recent_completed_tool_tokens: int
    max_historical_tool_result_tokens: int
    prune_protect_tokens: int
    prune_minimum_savings_tokens: int
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD

    # ── Backward-compat aliases ───────────────────────────────────────────────

    @property
    def preserve_recent_completed_tool_bytes(self) -> int:
        """Backward-compat: token budget expressed as approximate bytes."""
        return self.preserve_recent_completed_tool_tokens * 4

    @property
    def max_historical_tool_result_bytes(self) -> int:
        """Backward-compat: token limit expressed as approximate bytes."""
        return self.max_historical_tool_result_tokens * 4

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_budget(
        cls,
        *,
        max_context_tokens: int,
        available_prompt_tokens: int | None = None,
        phase: ProjectionPhase = "cross_turn",
        pressure_mode: PressureMode | ProjectionPressureMode = PressureMode.normal,
    ) -> ProjectionPolicy:
        window = max(1, int(max_context_tokens or 1))
        available = max(1, int(available_prompt_tokens or window))
        steady_cap = _steady_prompt_cap(window)
        steady_target = max(1, min(int(available * 0.88), steady_cap))
        burst_cap = _burst_prompt_cap(window)
        burst_target = max(steady_target, min(int(available * 0.95), burst_cap))
        hard_prompt = min(
            available,
            max(burst_target, min(int(available * 0.98), max(int(burst_cap * 1.1), burst_target))),
        )

        cross_tool_tokens = _clamp(int(steady_target * 0.18), 8_000, 70_000)
        within_tool_tokens = _clamp(int(burst_target * 0.38), 25_000, 250_000)
        active_tool_tokens = within_tool_tokens if phase == "within_turn" else cross_tool_tokens
        if pressure_mode == "pressure":
            active_tool_tokens = int(active_tool_tokens * 0.65)
        elif pressure_mode == "critical":
            active_tool_tokens = int(active_tool_tokens * 0.35)
        active_tool_tokens = max(4_000, active_tool_tokens)

        preserved_groups = _clamp(steady_target // 25_000, 3, 16)
        if phase == "within_turn":
            preserved_groups = _clamp(burst_target // 25_000, 4, 20)
        if pressure_mode == "pressure":
            preserved_groups = max(2, preserved_groups // 2)
        elif pressure_mode == "critical":
            preserved_groups = 2

        historical_tokens = _historical_tool_result_tokens(steady_target, pressure_mode)
        protect_tokens = _prune_protect_tokens(steady_target, burst_target, phase, pressure_mode)
        minimum_savings = _prune_minimum_savings_tokens(steady_target, pressure_mode)

        return cls(
            phase=phase,
            pressure_mode=pressure_mode,
            max_context_tokens=window,
            available_prompt_tokens=available,
            steady_target_tokens=steady_target,
            burst_target_tokens=burst_target,
            hard_prompt_tokens=hard_prompt,
            cross_turn_tool_budget_tokens=cross_tool_tokens,
            within_turn_tool_budget_tokens=within_tool_tokens,
            preserve_recent_completed_tool_groups=preserved_groups,
            preserve_recent_completed_tool_tokens=active_tool_tokens,
            max_historical_tool_result_tokens=historical_tokens,
            prune_protect_tokens=protect_tokens,
            prune_minimum_savings_tokens=minimum_savings,
        )

    def as_metadata(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "pressure_mode": str(self.pressure_mode),
            "steady_target_tokens": self.steady_target_tokens,
            "burst_target_tokens": self.burst_target_tokens,
            "hard_prompt_tokens": self.hard_prompt_tokens,
            "cross_turn_tool_budget_tokens": self.cross_turn_tool_budget_tokens,
            "within_turn_tool_budget_tokens": self.within_turn_tool_budget_tokens,
            "preserve_recent_tool_groups": self.preserve_recent_completed_tool_groups,
            "preserve_recent_tool_tokens": self.preserve_recent_completed_tool_tokens,
            "max_historical_tool_result_tokens": self.max_historical_tool_result_tokens,
            "prune_protect_tokens": self.prune_protect_tokens,
            "prune_minimum_savings_tokens": self.prune_minimum_savings_tokens,
        }


# ── Pressure snapshot & decision ─────────────────────────────────────────────


@dataclass(slots=True)
class PressureSnapshot:
    """Token-budget snapshot used for pressure-mode decisions.

    Replaces the old ``ContextPressureSnapshot`` in ``agent_loop.py`` as the
    canonical type for projection-related pressure tracking.  The agent loop
    still has its own ``ContextPressureSnapshot`` for compaction telemetry;
    this type is purely for projection decisions.
    """

    prompt_tokens: int
    available_prompt_tokens: int
    steady_target_tokens: int
    hard_prompt_tokens: int
    oversized_result_appended: bool = False
    cycle_index: int = 0


def decide_pressure_mode(
    snapshot: PressureSnapshot,
    prior_mode: PressureMode,
    *,
    under_threshold_cycles: int,
) -> tuple[PressureMode, int]:
    """Return the new pressure mode and updated under-threshold cycle count.

    Hysteresis rules
    ----------------
    * ``normal → pressure``: usage ≥ ``PRESSURE_ESCALATE_FRACTION`` for 1 cycle.
    * ``pressure → critical``: usage ≥ ``CRITICAL_ESCALATE_FRACTION`` for 1 cycle.
      Oversized tool results trigger a re-projection, but do not by themselves
      imply total prompt pressure.
    * Demotion (any → lower): requires ``PRESSURE_DEMOTION_CYCLES`` consecutive
      cycles back under the lower band.

    Returns the new mode and the updated ``under_threshold_cycles`` counter
    (reset to 0 on escalation, incremented on each under-threshold cycle).
    """
    if snapshot.available_prompt_tokens <= 0:
        return prior_mode, under_threshold_cycles

    usage = snapshot.prompt_tokens / snapshot.available_prompt_tokens

    # Escalation paths (immediate, no hysteresis needed for going up).
    if usage >= CRITICAL_ESCALATE_FRACTION:
        return PressureMode.critical, 0

    if usage >= PRESSURE_ESCALATE_FRACTION:
        if prior_mode == PressureMode.normal:
            return PressureMode.pressure, 0
        # Already at pressure or critical — stay.
        return prior_mode, 0

    # Under threshold — count consecutive cycles for demotion hysteresis.
    new_under = under_threshold_cycles + 1
    if new_under >= PRESSURE_DEMOTION_CYCLES:
        if prior_mode == PressureMode.critical:
            return PressureMode.pressure, 0
        if prior_mode == PressureMode.pressure:
            return PressureMode.normal, 0
    return prior_mode, new_under


# ── Re-projection decision ────────────────────────────────────────────────────


class ReprojectDecision(StrEnum):
    """Whether and how to re-run ``project_messages`` for a within-turn cycle."""

    skip = "skip"
    """Reuse the previous result; append new tail messages verbatim."""

    reproject = "reproject"
    """Re-run projection; respect committed_preservations (monotonic)."""

    critical_reproject = "critical_reproject"
    """Re-run projection; may demote previously committed groups."""


def should_reproject(
    *,
    new_message_count: int,
    last_message_count: int,
    new_token_estimate: int,
    steady_target_tokens: int,
    pressure_mode: PressureMode,
    prior_pressure_mode: PressureMode,
    oversized_appended: bool,
    prefix_fingerprint_unchanged: bool = True,
) -> ReprojectDecision:
    """Decide whether within-turn re-projection is needed.

    Rules (in priority order)
    -------------------------
    1. Critical pressure → ``critical_reproject`` only when the append is
       oversized, the provider-sensitive prefix mutated, or the projected
       estimate is still at/above target.
    2. Oversized tool result just appended → ``reproject``.
    3. Pressure mode escalated this cycle → ``reproject``.
    4. New token estimate crosses ``steady_target_tokens`` → ``reproject``.
    5. No new messages since last projection → ``skip``.
    6. Otherwise → ``skip`` (new messages appended verbatim).
    """
    if pressure_mode == PressureMode.critical:
        if (
            not oversized_appended
            and prefix_fingerprint_unchanged
            and new_token_estimate < steady_target_tokens
        ):
            return ReprojectDecision.skip
        return ReprojectDecision.critical_reproject

    if oversized_appended:
        return ReprojectDecision.reproject

    if pressure_mode != prior_pressure_mode:
        # Mode escalated (normal→pressure or pressure→critical already handled).
        return ReprojectDecision.reproject

    if new_token_estimate >= steady_target_tokens:
        return ReprojectDecision.reproject

    # No pressure, no oversized result, no mode change — skip.
    return ReprojectDecision.skip


# ── Per-turn projection state ─────────────────────────────────────────────────


@dataclass
class ProjectionTurnState:
    """Mutable per-turn state for the conditional projection pipeline.

    Owned by ``StepContext`` (one instance per step execution).  Tracks
    committed group preservations, pressure history, and the last projection
    result so that ``should_reproject`` can make an informed decision each
    cycle.
    """

    turn_id: str
    policy: ProjectionPolicy

    # Pressure tracking
    pressure_mode: PressureMode = PressureMode.normal
    prior_pressure_mode: PressureMode = PressureMode.normal
    under_threshold_cycles: int = 0

    # Projection cache
    last_result: ProjectionResult | None = None
    last_message_count: int = 0
    last_projected_token_estimate: int = 0
    last_group_anchor: str | None = None  # hash of last tool group call_ids
    last_prefix_fingerprint: str | None = None

    # Monotonic preservation: anchors of groups already sent to the model.
    # Under non-critical pressure these are never demoted.
    committed_preservations: set[str] = field(default_factory=set)
    # Prefix stability: once a group has been projected as a placeholder during
    # this turn, never promote it back to verbatim content.  The placeholder
    # builder is deterministic, so this keeps the already-demoted prefix
    # byte-stable across pressure cycles.
    demoted_anchors: set[str] = field(default_factory=set)

    # Telemetry
    reproject_count: int = 0
    skip_count: int = 0
    forced_critical_count: int = 0
    recovery_loop_detected_count: int = 0
    last_projected_prefix_fingerprint: str | None = None

    # Recovery pinning: call_ids for same-turn recovery helper results, plus
    # per-source recovery counts used to detect repeated recovery loops.
    recovery_result_call_ids: set[str] = field(default_factory=set)
    recovered_source_call_ids: set[str] = field(default_factory=set)
    recovery_per_source_counts: dict[str, int] = field(default_factory=dict)

    def update_pressure(self, snapshot: PressureSnapshot) -> None:
        """Update pressure mode from a fresh snapshot (with hysteresis)."""
        self.prior_pressure_mode = self.pressure_mode
        new_mode, new_under = decide_pressure_mode(
            snapshot,
            self.pressure_mode,
            under_threshold_cycles=self.under_threshold_cycles,
        )
        self.pressure_mode = new_mode
        self.under_threshold_cycles = new_under

    def commit_preservations(self, preserved_anchors: set[str]) -> None:
        """Record group anchors that were sent to the model as preserved."""
        self.committed_preservations.update(preserved_anchors)

    def prune_committed_preservations(self, demoted_anchors: set[str]) -> None:
        """Forget committed anchors that critical projection demoted."""
        self.committed_preservations.difference_update(demoted_anchors)

    def record_demotions(self, demoted_anchors: set[str]) -> None:
        """Record group anchors projected as compact placeholders this turn."""
        self.demoted_anchors.update(demoted_anchors)

    def fork_for_projection_attempt(self) -> ProjectionTurnState:
        """Return an isolated copy for speculative projection attempts."""

        return ProjectionTurnState(
            turn_id=self.turn_id,
            policy=self.policy,
            pressure_mode=self.pressure_mode,
            prior_pressure_mode=self.prior_pressure_mode,
            under_threshold_cycles=self.under_threshold_cycles,
            last_result=self.last_result,
            last_message_count=self.last_message_count,
            last_projected_token_estimate=self.last_projected_token_estimate,
            last_group_anchor=self.last_group_anchor,
            last_prefix_fingerprint=self.last_prefix_fingerprint,
            committed_preservations=set(self.committed_preservations),
            demoted_anchors=set(self.demoted_anchors),
            reproject_count=self.reproject_count,
            skip_count=self.skip_count,
            forced_critical_count=self.forced_critical_count,
            recovery_loop_detected_count=self.recovery_loop_detected_count,
            last_projected_prefix_fingerprint=self.last_projected_prefix_fingerprint,
            recovery_result_call_ids=set(self.recovery_result_call_ids),
            recovered_source_call_ids=set(self.recovered_source_call_ids),
            recovery_per_source_counts=dict(self.recovery_per_source_counts),
        )

    def apply_projection_attempt(self, attempt_state: ProjectionTurnState) -> None:
        """Commit state changes from the selected speculative projection attempt."""

        self.committed_preservations = set(attempt_state.committed_preservations)
        self.demoted_anchors = set(attempt_state.demoted_anchors)

    def record_recovery_result(
        self,
        *,
        result_call_id: str | None,
        source_call_id: str | None,
    ) -> int:
        """Record a same-turn recovery helper result.

        Returns the recovery count for ``source_call_id`` after this result.
        A count >= 2 means the source has entered a recovery loop and the
        newest recovery result should be protected for the rest of the turn.
        """

        if isinstance(result_call_id, str) and result_call_id.strip():
            self.recovery_result_call_ids.add(result_call_id)
        if not isinstance(source_call_id, str) or not source_call_id.strip():
            return 0
        count = self.recovery_per_source_counts.get(source_call_id, 0) + 1
        self.recovery_per_source_counts[source_call_id] = count
        self.recovered_source_call_ids.add(source_call_id)
        if count >= 2:
            self.recovery_loop_detected_count += 1
        return count

    def seed_from_cross_turn_result(
        self,
        result: ProjectionResult,
        messages: list[dict[str, Any]],
        *,
        compacted_anchors: set[str] | None = None,
    ) -> None:
        """Initialise state from the cross-turn projection done at assembly time."""
        self.last_result = result
        self.last_message_count = len(messages)
        self.last_prefix_fingerprint = tool_transcript_prefix_fingerprint(messages)
        # Seed committed_preservations from the groups that were preserved in
        # the cross-turn projection (i.e. not compacted).
        groups = _collect_tool_groups(messages)
        known_compacted_anchors = (
            compacted_tool_group_anchors(result.messages)
            if compacted_anchors is None
            else compacted_anchors
        )
        for group in groups:
            anchor = _group_anchor(group)
            if anchor in known_compacted_anchors:
                self.demoted_anchors.add(anchor)
                continue
            self.committed_preservations.add(anchor)
        self.last_projected_prefix_fingerprint = projected_prefix_fingerprint(
            result.messages, result.mutable_start_index
        )


# ── Tool group helpers ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ToolGroup:
    assistant_index: int
    message_indices: tuple[int, ...]
    call_ids: frozenset[str]
    completed: bool
    protected: bool


def _group_anchor(group: _ToolGroup) -> str:
    """Stable hash of a tool group's call_ids — used as a preservation key."""
    key = ",".join(sorted(group.call_ids))
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]


def tool_transcript_prefix_fingerprint(messages: list[dict[str, Any]]) -> str:
    """Return a stable fingerprint for provider-sensitive tool transcript shape.

    Projection skip reuses a previously projected prefix and appends only the new
    tail. The rich transcript can mutate existing assistant messages after a
    continuation/tool-call cycle, so the skip path must verify that tool-call
    structure in the cached prefix is still current before reusing it.
    """

    entries: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            calls: list[dict[str, str]] = []
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                calls.append(
                    {
                        "id": str(tool_call.get("id") or tool_call.get("call_id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or ""),
                    }
                )
            entries.append({"i": index, "role": "assistant", "tool_calls": calls})
        elif role == "tool":
            entries.append(
                {
                    "i": index,
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                }
            )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


def projected_prefix_fingerprint(messages: list[dict[str, Any]], mutable_start_index: int) -> str:
    """Return a stable fingerprint of projected bytes before the mutable frontier."""

    prefix_end = _clamp(mutable_start_index, 0, len(messages))
    payload = json.dumps(
        messages[:prefix_end],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


def compacted_tool_group_anchors(messages: list[dict[str, Any]]) -> set[str]:
    """Return anchors for tool groups whose result content is currently compacted."""

    compacted_call_ids = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool" and message.get(PROJECTED_COMPACTED)
    } - {""}
    if not compacted_call_ids:
        return set()
    return {
        _group_anchor(group)
        for group in _collect_tool_groups(messages)
        if bool(group.call_ids & compacted_call_ids)
    }


def projection_result_from_messages(messages: list[dict[str, Any]]) -> ProjectionResult:
    """Reconstruct projection metadata from a post-prune transcript."""

    compacted_anchors = compacted_tool_group_anchors(messages)
    if not compacted_anchors:
        return ProjectionResult(messages=list(messages), mutable_start_index=0)
    mutable_start_index = min(
        (
            group.assistant_index
            for group in _collect_tool_groups(messages)
            if _group_anchor(group) not in compacted_anchors
        ),
        default=len(messages),
    )
    return ProjectionResult(
        messages=list(messages),
        mutable_start_index=mutable_start_index,
    )


def _collect_tool_groups(messages: list[dict[str, Any]]) -> list[_ToolGroup]:
    groups: list[_ToolGroup] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        call_ids = {
            str(tool_call.get("id") or tool_call.get("call_id") or "")
            for tool_call in message["tool_calls"]
            if isinstance(tool_call, dict)
        } - {""}
        if not call_ids:
            continue
        message_indices = [index]
        seen_results: set[str] = set()
        protected = False
        for follow_index in range(index + 1, len(messages)):
            follow = messages[follow_index]
            if follow.get("role") == "tool" and follow.get("tool_call_id") in call_ids:
                message_indices.append(follow_index)
                seen_results.add(str(follow.get("tool_call_id")))
                if follow.get(PROTECTED_TOOL_OUTPUT):
                    protected = True
                continue
            if follow.get(TOOL_ATTACHMENT_CONTEXT) and (
                follow.get(TOOL_CALL_ID) in call_ids or follow.get("tool_call_id") in call_ids
            ):
                message_indices.append(follow_index)
                continue
            break
        groups.append(
            _ToolGroup(
                assistant_index=index,
                message_indices=tuple(message_indices),
                call_ids=frozenset(call_ids),
                completed=seen_results == call_ids,
                protected=protected,
            )
        )
    return groups


# ── Token estimation ──────────────────────────────────────────────────────────


def _get_message_token_estimate(
    message: dict[str, Any],
    token_counter: Callable[[str], int] | None,
) -> int:
    """Return a cached token estimate for *message*, computing it if needed.

    The estimate is stamped onto the message dict under ``TOKEN_ESTIMATE`` so
    subsequent projection cycles reuse it without re-counting.  Only the
    content fields that actually reach the LLM are counted.
    """
    cached = message.get(TOKEN_ESTIMATE)
    if isinstance(cached, int) and cached >= 0:
        return cached
    count = token_counter or default_token_estimate
    # Estimate from the serialised content that the LLM will see.
    content = message.get("content")
    if isinstance(content, str):
        tokens = count(content)
    elif isinstance(content, list):
        # Multi-block content (vision, etc.)
        tokens = count(json.dumps(content, default=str))
    else:
        tokens = 0
    # Add tool_calls argument tokens for assistant messages.
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                args = tc.get("function", {}).get("arguments", "")
                tokens += count(args if isinstance(args, str) else json.dumps(args, default=str))
    message[TOKEN_ESTIMATE] = tokens
    return tokens


def _estimated_group_tokens(
    messages: list[dict[str, Any]],
    group: _ToolGroup,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    total = 0
    for index in group.message_indices:
        if 0 <= index < len(messages):
            total += _get_message_token_estimate(messages[index], token_counter)
    return total


def _estimated_group_compaction_savings(
    messages: list[dict[str, Any]],
    group: _ToolGroup,
    *,
    arg_clear_threshold: int,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    """Estimate tokens saved by projecting one complete tool group."""

    original_tokens = _estimated_group_tokens(messages, group, token_counter)
    projected_messages: list[dict[str, Any]] = []
    for index in group.message_indices:
        message = messages[index]
        projected_message = dict(message)
        if index == group.assistant_index:
            projected_message = clear_large_tool_call_arguments(
                message,
                arg_clear_threshold=arg_clear_threshold,
            )
        elif message.get("role") == "tool":
            projected_message["content"] = build_compacted_tool_result_placeholder(message)
            projected_message[PROJECTED_COMPACTED] = True
        elif message.get(TOOL_ATTACHMENT_CONTEXT):
            projected_message["role"] = "system"
            projected_message["content"] = build_compacted_tool_attachment_placeholder(message)
            projected_message[PROJECTED_COMPACTED] = True
        projected_message.pop(TOKEN_ESTIMATE, None)
        projected_messages.append(projected_message)
    projected_tokens = _estimated_messages_tokens(projected_messages, token_counter)
    return max(1, original_tokens - projected_tokens)


def _token_capped_groups(
    messages: list[dict[str, Any]],
    groups: list[_ToolGroup],
    *,
    token_budget: int,
    token_counter: Callable[[str], int] | None = None,
) -> list[_ToolGroup]:
    """Return a newest-biased slice capped by tokens; oldest groups yield first."""

    if not groups or token_budget <= 0:
        return []
    kept = list(groups)
    while (
        kept
        and sum(_estimated_group_tokens(messages, group, token_counter) for group in kept)
        > token_budget
    ):
        kept = kept[1:]
    return kept


def _estimated_messages_tokens(
    messages: list[dict[str, Any]],
    token_counter: Callable[[str], int] | None = None,
) -> int:
    return sum(_get_message_token_estimate(m, token_counter) for m in messages)


# ── Turn boundary detection ───────────────────────────────────────────────────


def _is_real_turn_boundary(message: dict[str, Any]) -> bool:
    """Return True when *message* marks the start of a real user/workflow turn.

    Uses the ``TURN_BOUNDARY`` marker (set by context assembly) instead of
    string-sniffing message content.  Falls back to ``role == "user"`` for
    messages that pre-date the marker (e.g. replayed history from old sessions).

    Tool attachment context messages are projected as user messages but are NOT
    turn boundaries.
    """
    role = message.get("role")
    # Explicit marker wins regardless of role.
    if message.get(TURN_BOUNDARY):
        return True
    if role == "user":
        return not bool(message.get(TOOL_ATTACHMENT_CONTEXT))
    return False


def _latest_real_user_index(messages: list[dict[str, Any]]) -> int:
    """Return the start index of the latest real user/workflow turn."""
    for index in range(len(messages) - 1, -1, -1):
        if _is_real_turn_boundary(messages[index]):
            return index
    return len(messages)


# ── Oversized result detection ────────────────────────────────────────────────


def _has_recovery_handle(message: dict[str, Any]) -> bool:
    recovery_call_id = message.get(RECOVERY_CALL_ID)
    return isinstance(recovery_call_id, str) and bool(recovery_call_id.strip())


def _group_has_oversized_recoverable_tool_result(
    messages: list[dict[str, Any]],
    group: _ToolGroup,
    *,
    max_historical_tool_result_tokens: int,
    token_counter: Callable[[str], int] | None = None,
) -> bool:
    if max_historical_tool_result_tokens <= 0:
        return False
    for index in group.message_indices:
        if index < 0 or index >= len(messages):
            continue
        message = messages[index]
        if message.get("role") != "tool":
            continue
        if message.get(PROTECTED_TOOL_OUTPUT):
            continue
        if not _has_recovery_handle(message):
            continue
        tokens = _get_message_token_estimate(message, token_counter)
        if tokens > max_historical_tool_result_tokens:
            return True
    return False


def _group_has_recoverable_tool_results(
    messages: list[dict[str, Any]],
    group: _ToolGroup,
) -> bool:
    """Return true when every tool result in a group has a recovery source."""

    saw_result = False
    for index in group.message_indices:
        message = messages[index]
        if message.get("role") != "tool":
            continue
        saw_result = True
        if not message.get(PROJECTED_COMPACTED) and not _has_recovery_handle(message):
            return False
    return saw_result


# ── Newest completed tool index ───────────────────────────────────────────────


def _newest_completed_latest_turn_tool_index(
    messages: list[dict[str, Any]], latest_turn_start: int
) -> int | None:
    """Return newest same-turn tool result index to preserve under pressure."""
    if latest_turn_start >= len(messages):
        return None
    completed_call_ids: set[str] = set()
    for index in range(len(messages) - 1, latest_turn_start - 1, -1):
        message = messages[index]
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                completed_call_ids.add(call_id)
            if message.get(PROTECTED_TOOL_OUTPUT):
                continue
            return index
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            call_ids = {
                str(tool_call.get("id") or tool_call.get("call_id") or "")
                for tool_call in message["tool_calls"]
                if isinstance(tool_call, dict)
            } - {""}
            if call_ids and not call_ids.issubset(completed_call_ids):
                return None
    return None


def _compact_prunable_delegation_replays(
    messages: list[dict[str, Any]],
    *,
    max_historical_tool_result_tokens: int,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if max_historical_tool_result_tokens <= 0:
        return messages, False
    projected: list[dict[str, Any]] = []
    changed = False
    for message in messages:
        if not message.get("_delegation_result_replay"):
            projected.append(message)
            continue
        if _get_message_token_estimate(message, token_counter) <= max_historical_tool_result_tokens:
            projected.append(message)
            continue
        call_id = message.get("_recovery_call_id")
        session_id = message.get("_source_session_id")
        handle_parts: list[str] = []
        if isinstance(call_id, str) and call_id:
            handle_parts.append(f"read_tool_output(call_id={call_id!r})")
        if isinstance(session_id, str) and session_id:
            handle_parts.append(f"get_subsession(session_id={session_id!r})")
        handle = " or ".join(handle_parts) or "the original delegation event"
        compacted = dict(message)
        compacted["content"] = (
            "<delegation_result_compacted>\n"
            "Prior delegation result replay was compacted under context pressure. "
            f"Recover the full result with {handle}.\n"
            "</delegation_result_compacted>"
        )
        compacted[PROJECTED_COMPACTED] = True
        projected.append(compacted)
        changed = True
    return projected, changed


def _latest_turn_has_unresolved_tool_call(
    messages: list[dict[str, Any]], latest_turn_start: int
) -> bool:
    """Return true when latest turn has assistant tool calls without all results."""
    if latest_turn_start >= len(messages):
        return False
    call_ids: set[str] = set()
    completed_call_ids: set[str] = set()
    for index in range(latest_turn_start, len(messages)):
        message = messages[index]
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            call_ids.update(
                str(tool_call.get("id") or tool_call.get("call_id") or "")
                for tool_call in message["tool_calls"]
                if isinstance(tool_call, dict)
            )
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str):
                completed_call_ids.add(call_id)
    call_ids.discard("")
    return bool(call_ids - completed_call_ids)


# ── Main projection function ──────────────────────────────────────────────────


def project_messages(
    messages: list[dict[str, Any]],
    *,
    preserve_recent_completed_tool_groups: int = DEFAULT_PRESERVED_TOOL_GROUPS,
    preserve_recent_completed_tool_bytes: int | None = None,  # deprecated, ignored
    max_historical_tool_result_bytes: int | None = None,  # deprecated, ignored
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
    pressure_mode: PressureMode | ProjectionPressureMode = PressureMode.normal,
    policy: ProjectionPolicy | None = None,
    prior_state: ProjectionTurnState | None = None,
    token_counter: Callable[[str], int] | None = None,
    required_savings_tokens: int | None = None,
    allow_unrecoverable_projection: bool = False,
) -> ProjectionResult:
    """Project a rich transcript into a compact model-facing view.

    Parameters
    ----------
    prior_state:
        When provided, ``committed_preservations`` from the prior cycle are
        respected — groups already sent to the model are not demoted unless
        ``pressure_mode == "critical"``.  On success the caller should call
        ``prior_state.commit_preservations(preserved_anchors)`` with the
        anchors of groups that were kept preserved in this result.
    token_counter:
        Optional callable ``(text: str) -> int``.  Defaults to
        ``default_token_estimate``.  Results are cached on message dicts.
    required_savings_tokens:
        When provided for a non-critical projection, compact the oldest
        eligible groups only until this savings target is met.
    allow_unrecoverable_projection:
        Permit savings-targeted compaction without a recovery handle. Keep
        false for normal pressure; hard pruning remains the provider-safety
        fallback.
    """
    if policy is not None:
        preserve_recent_completed_tool_groups = policy.preserve_recent_completed_tool_groups
        preserve_recent_completed_tool_tokens = policy.preserve_recent_completed_tool_tokens
        max_historical_tool_result_tokens = policy.max_historical_tool_result_tokens
        arg_clear_threshold = policy.arg_clear_threshold
        pressure_mode = policy.pressure_mode
    else:
        # Legacy callers may pass byte-based values; convert with heuristic.
        preserve_recent_completed_tool_tokens = (
            DEFAULT_PRESERVED_TOOL_TOKENS
            if preserve_recent_completed_tool_bytes is None
            else preserve_recent_completed_tool_bytes // 4
        )
        max_historical_tool_result_tokens = (
            DEFAULT_MAX_HISTORICAL_TOOL_RESULT_TOKENS
            if max_historical_tool_result_bytes is None
            else max_historical_tool_result_bytes // 4
        )

    committed = prior_state.committed_preservations if prior_state is not None else set()
    demoted = prior_state.demoted_anchors if prior_state is not None else set()
    is_critical = pressure_mode == "critical" or pressure_mode == PressureMode.critical
    savings_target = (
        None if required_savings_tokens is None else max(0, int(required_savings_tokens))
    )
    if policy is None and is_critical:
        preserve_recent_completed_tool_groups = min(preserve_recent_completed_tool_groups, 2)

    messages, delegation_replays_compacted = _compact_prunable_delegation_replays(
        list(messages),
        max_historical_tool_result_tokens=max_historical_tool_result_tokens,
        token_counter=token_counter,
    )
    groups = _collect_tool_groups(messages)
    if not groups:
        return ProjectionResult(messages=messages, mutable_start_index=0)

    preserve_recent_completed_tool_groups = max(0, int(preserve_recent_completed_tool_groups))
    latest_turn_start = _latest_real_user_index(messages)
    allow_latest_turn_compaction = pressure_mode in {"pressure", "critical"} or pressure_mode in {
        PressureMode.pressure,
        PressureMode.critical,
    }
    completed_groups = [
        group
        for group in groups
        if group.completed
        and not group.protected
        and (group.assistant_index < latest_turn_start or allow_latest_turn_compaction)
    ]
    latest_turn_completed_groups = [
        group for group in completed_groups if group.assistant_index >= latest_turn_start
    ]
    latest_turn_preserved_indices: set[int] = set()
    if latest_turn_completed_groups and not _latest_turn_has_unresolved_tool_call(
        messages, latest_turn_start
    ):
        latest_turn_preserved_indices.add(latest_turn_completed_groups[-1].assistant_index)

    preserved_slice = (
        completed_groups[-preserve_recent_completed_tool_groups:]
        if preserve_recent_completed_tool_groups > 0
        else []
    )
    preserve_recent_completed_tool_tokens = max(0, int(preserve_recent_completed_tool_tokens))
    while (
        preserve_recent_completed_tool_tokens > 0
        and len(preserved_slice) > 1
        and sum(
            _estimated_group_tokens(messages, group, token_counter) for group in preserved_slice
        )
        > preserve_recent_completed_tool_tokens
    ):
        preserved_slice = preserved_slice[1:]

    max_historical_tool_result_tokens = max(0, int(max_historical_tool_result_tokens))
    if savings_target is not None and not is_critical:
        preserved_assistant_indices = {
            group.assistant_index for group in completed_groups
        } | latest_turn_preserved_indices
    else:
        oversized_preserved_indices = {
            group.assistant_index
            for group in preserved_slice
            if _group_has_oversized_recoverable_tool_result(
                messages,
                group,
                max_historical_tool_result_tokens=max_historical_tool_result_tokens,
                token_counter=token_counter,
            )
        }
        preserved_assistant_indices = {
            group.assistant_index
            for group in preserved_slice
            if group.assistant_index not in oversized_preserved_indices
        } | latest_turn_preserved_indices

    recovery_pin_groups: list[_ToolGroup] = []
    if prior_state is not None and prior_state.recovery_result_call_ids:
        recovery_pin_groups = [
            group
            for group in completed_groups
            if group.call_ids & prior_state.recovery_result_call_ids
        ]
    recovery_pin_budget_tokens = max(0, int(preserve_recent_completed_tool_tokens * 0.40))
    recovery_pin_groups = _token_capped_groups(
        messages,
        recovery_pin_groups,
        token_budget=recovery_pin_budget_tokens,
        token_counter=token_counter,
    )
    recovery_pin_indices = {group.assistant_index for group in recovery_pin_groups}

    if is_critical:
        # Critical: keep token-capped same-turn recency plus recovery pins.
        latest_turn_critical_groups: list[_ToolGroup] = []
        if latest_turn_completed_groups and not _latest_turn_has_unresolved_tool_call(
            messages, latest_turn_start
        ):
            latest_turn_critical_groups = latest_turn_completed_groups[
                -preserve_recent_completed_tool_groups:
            ]
        latest_turn_critical_groups = _token_capped_groups(
            messages,
            latest_turn_critical_groups,
            token_budget=preserve_recent_completed_tool_tokens,
            token_counter=token_counter,
        )
        preserved_assistant_indices = {
            group.assistant_index for group in latest_turn_critical_groups
        } | recovery_pin_indices
    else:
        # Non-critical: honour committed_preservations — never demote a group
        # that has already been sent to the model in preserved form.
        committed_indices = {
            group.assistant_index for group in groups if _group_anchor(group) in committed
        }
        preserved_assistant_indices = (
            preserved_assistant_indices | committed_indices | recovery_pin_indices
        )
    if demoted:
        demoted_indices = {
            group.assistant_index for group in groups if _group_anchor(group) in demoted
        }
        preserved_assistant_indices.difference_update(demoted_indices)

    if savings_target is not None and not is_critical and savings_target > 0:
        accumulated_savings = 0
        for group in completed_groups:
            if accumulated_savings >= savings_target:
                break
            if group.assistant_index not in preserved_assistant_indices:
                continue
            anchor = _group_anchor(group)
            if anchor in committed or group.assistant_index in recovery_pin_indices:
                continue
            if not allow_unrecoverable_projection and not _group_has_recoverable_tool_results(
                messages, group
            ):
                continue
            preserved_assistant_indices.remove(group.assistant_index)
            accumulated_savings += _estimated_group_compaction_savings(
                messages,
                group,
                arg_clear_threshold=arg_clear_threshold,
                token_counter=token_counter,
            )

    compacted_by_index = {
        group.assistant_index: group
        for group in completed_groups
        if group.assistant_index not in preserved_assistant_indices
    }
    if demoted:
        # A prior placeholder must stay a placeholder even after pressure eases
        # or latest-turn protection would otherwise remove it from the normal
        # compaction candidate set.
        compacted_by_index.update(
            {
                group.assistant_index: group
                for group in groups
                if group.completed and not group.protected and _group_anchor(group) in demoted
            }
        )
    compacted_groups = [compacted_by_index[index] for index in sorted(compacted_by_index)]
    if prior_state is not None and compacted_groups:
        demoted_anchors = {_group_anchor(group) for group in compacted_groups}
        prior_state.record_demotions(demoted_anchors)
        if is_critical:
            prior_state.prune_committed_preservations(demoted_anchors)
    if not compacted_groups:
        # Nothing to compact — record preserved anchors and return.
        if prior_state is not None:
            preserved_anchors = {
                _group_anchor(g) for g in groups if g.assistant_index in preserved_assistant_indices
            }
            prior_state.commit_preservations(preserved_anchors)
        return ProjectionResult(messages=list(messages), mutable_start_index=0)

    compacted_assistant_indices = {group.assistant_index for group in compacted_groups}
    compacted_call_ids = {call_id for group in compacted_groups for call_id in group.call_ids}

    projected: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index in compacted_assistant_indices:
            projected.append(
                clear_large_tool_call_arguments(message, arg_clear_threshold=arg_clear_threshold)
            )
            continue
        if message.get("role") == "tool" and message.get("tool_call_id") in compacted_call_ids:
            projected.append(
                {
                    **message,
                    "content": build_compacted_tool_result_placeholder(message),
                    PROJECTED_COMPACTED: True,
                }
            )
            continue
        if message.get(TOOL_ATTACHMENT_CONTEXT) and (
            message.get(TOOL_CALL_ID) in compacted_call_ids
            or message.get("tool_call_id") in compacted_call_ids
        ):
            projected.append(
                {
                    **message,
                    "role": "system",
                    "content": build_compacted_tool_attachment_placeholder(message),
                    PROJECTED_COMPACTED: True,
                }
            )
            continue
        projected.append(dict(message))

    mutable_start_index = min(
        (
            group.assistant_index
            for group in groups
            if group.assistant_index not in compacted_assistant_indices
        ),
        default=len(projected),
    )
    if mutable_start_index >= len(projected):
        mutable_start_index = len(projected)

    # Record which groups were preserved so the next cycle can honour them.
    if prior_state is not None:
        preserved_anchors = {
            _group_anchor(g) for g in groups if g.assistant_index in preserved_assistant_indices
        }
        prior_state.commit_preservations(preserved_anchors)

    return ProjectionResult(messages=projected, mutable_start_index=mutable_start_index)


# ── Fallback pruning ──────────────────────────────────────────────────────────


def prune_projected_messages(
    messages: list[dict[str, Any]],
    *,
    protect_tokens: int,
    minimum_savings: int,
    min_index_to_modify: int = 0,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
    token_counter: Callable[[str], int] | None = None,
    pressure_mode: PressureMode | ProjectionPressureMode = PressureMode.normal,
    recovery_result_call_ids: set[str] | None = None,
    recovery_pin_budget_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Fallback pruning for the mutable tail after normal projection."""

    count = token_counter or default_token_estimate
    result = list(messages)
    latest_turn_start = _latest_real_user_index(result)
    allow_latest_turn_pruning = pressure_mode in {
        "pressure",
        "critical",
        PressureMode.pressure,
        PressureMode.critical,
    } and not _latest_turn_has_unresolved_tool_call(result, latest_turn_start)
    protected_tokens = 0
    pruneable_result_indices: list[int] = []
    pruneable_call_ids: set[str] = set()
    pruneable_attachment_indices: list[int] = []
    recovery_pin_ids = {call_id for call_id in (recovery_result_call_ids or set()) if call_id}
    recovery_pin_result_indices: set[int] = set()
    if recovery_pin_ids:
        pin_candidates: list[tuple[int, int]] = []
        for index, message in enumerate(result):
            if message.get("role") != "tool" or message.get("tool_call_id") not in recovery_pin_ids:
                continue
            content = message.get("content", "")
            pin_candidates.append((index, count(content) if isinstance(content, str) else 0))
        pin_budget = (
            max(0, int(recovery_pin_budget_tokens))
            if recovery_pin_budget_tokens is not None
            else max(0, int(protect_tokens * 0.40))
        )
        while pin_candidates and sum(tokens for _, tokens in pin_candidates) > pin_budget:
            pin_candidates = pin_candidates[1:]
        recovery_pin_result_indices = {index for index, _ in pin_candidates}
    newest_latest_turn_tool_index = _newest_completed_latest_turn_tool_index(
        result, latest_turn_start
    )

    for index in range(len(result) - 1, min_index_to_modify - 1, -1):
        if index >= latest_turn_start and not allow_latest_turn_pruning:
            continue
        if index == newest_latest_turn_tool_index:
            continue
        message = result[index]
        if message.get("role") != "tool":
            continue
        if message.get(PROTECTED_TOOL_OUTPUT) or message.get(PROJECTED_COMPACTED):
            continue
        if index in recovery_pin_result_indices:
            continue
        content = message.get("content", "")
        tokens = count(content) if isinstance(content, str) else 0
        if protected_tokens + tokens <= protect_tokens:
            protected_tokens += tokens
        else:
            pruneable_result_indices.append(index)
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                pruneable_call_ids.add(call_id)

    pruneable_call_indices: list[int] = []
    max_call_index = len(result) if allow_latest_turn_pruning else latest_turn_start
    for index in range(min_index_to_modify, max_call_index):
        message = result[index]
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        tool_calls = message["tool_calls"]
        if not any(
            isinstance(tc, dict) and tc.get("id") in pruneable_call_ids for tc in tool_calls
        ):
            continue
        total_arg_size = sum(
            len(
                json.dumps(
                    tc.get("function", {}).get("arguments", {}),
                    default=str,
                )
            )
            for tc in tool_calls
            if isinstance(tc, dict)
        )
        if total_arg_size > arg_clear_threshold:
            pruneable_call_indices.append(index)

    for index in range(min_index_to_modify, max_call_index):
        message = result[index]
        if not message.get(TOOL_ATTACHMENT_CONTEXT) or message.get(PROJECTED_COMPACTED):
            continue
        call_id = message.get(TOOL_CALL_ID) or message.get("tool_call_id")
        if call_id in pruneable_call_ids:
            pruneable_attachment_indices.append(index)

    total_savings = 0
    for index in pruneable_result_indices:
        content = result[index].get("content", "")
        total_savings += count(content) if isinstance(content, str) else 0
    for index in pruneable_call_indices:
        for tool_call in result[index].get("tool_calls", []):
            if isinstance(tool_call, dict):
                args = tool_call.get("function", {}).get("arguments", {})
                total_savings += count(json.dumps(args, default=str))
    for index in pruneable_attachment_indices:
        total_savings += count(json.dumps(result[index].get("content", ""), default=str))
    if total_savings < minimum_savings:
        return result

    # Apply oldest-first so the demotion frontier advances forward.  Candidate
    # collection still walks newest-first to preserve the protect-token semantics
    # (newer results spend the protected budget first).
    for index in sorted(pruneable_result_indices):
        message = result[index]
        result[index] = {
            **message,
            "content": build_compacted_tool_result_placeholder(message),
            PROJECTED_COMPACTED: True,
        }
    for index in pruneable_call_indices:
        result[index] = clear_large_tool_call_arguments(
            result[index], arg_clear_threshold=arg_clear_threshold
        )
    for index in pruneable_attachment_indices:
        message = result[index]
        result[index] = {
            **message,
            "role": "system",
            "content": build_compacted_tool_attachment_placeholder(message),
            PROJECTED_COMPACTED: True,
        }
    return result


# ── Legacy byte-based helpers (kept for backward compat) ─────────────────────


def _estimated_group_bytes(messages: list[dict[str, Any]], group: _ToolGroup) -> int:
    """Deprecated: use ``_estimated_group_tokens`` instead."""
    return _estimated_group_tokens(messages, group) * 4
