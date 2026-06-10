"""Harness guards: loop detection and tool-argument sanity checks.

These guards sit between the model and tool dispatch to prevent
pathological behavior observed in production traces:

* The model repeats an identical tool call many times in a row — usually
  because the tool returned only a status and no canonical state.
  ``LoopGuard`` trips on the 2nd consecutive identical call and returns a
  teach-back result instructing the model to make a different, concrete
  action or call ``step_complete``.

* The model emits syntactically valid tool calls with obviously invalid
  arguments lifted straight from prompt placeholders — for example
  ``multiedit(file_path="/dev/null", edits=[])`` or
  ``list_tool_output_anchors(call_id="dummy")``. ``ArgumentSanityGate``
  rejects these at the controller with a teach-back so the executor never
  has to paper over them and the model receives a corrective signal.

Both guards are controller-side. They never reach the executor. Returned
payloads are JSON-encoded error dicts with the shape::

    {
      "status": "rejected",
      "reason": <stable code>,
      "message": <prose teach-back>,
      "received": <argument subset>,
    }

Stability matters because downstream evaluators and telemetry will key on
the ``reason`` codes; do not rename them lightly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LoopGuardState:
    """Per-step tracking of the last (tool_name, args_hash) observed.

    A step-scoped guard is intentional: different steps within the same
    workflow are independent execution contexts, and cross-step repetition
    is not necessarily a pathology (e.g., re-running the same
    ``memory_recent`` call in ``plan`` and ``synthesize`` is fine).
    """

    last_key: tuple[str, str] | None = None
    streak: int = 0
    exemptions: set[str] = field(default_factory=set)


# Tools whose arguments vary only by cursor/page token or are otherwise
# expected to be called repeatedly with the same logical intent (for
# example, polling or searching with pagination). They are exempt from
# the identical-call-in-a-row rule.
#
# We err on the side of being conservative: the list should be small and
# obvious. The step_todo_write case is covered by the "unchanged" flag
# returned from the controller so we keep it under the guard — repeat
# identical writes are exactly the pathology we want to interrupt.
_LOOP_GUARD_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        # Pagination-likely tools commonly called repeatedly.
        "memory_list",
        "memory_recent",
        "memory_search",
        "memory_find",
        # The wait/poll-style controller tools are already exclusive and
        # do not reach this guard, but listing them here is cheap belt-
        # and-suspenders in case they ever do.
        "step_request_questions",
        "request_credential",
        "request_auth_challenge",
    }
)


def _canonical_args(arguments: dict[str, Any] | None) -> str:
    """Return a stable JSON representation for hashing."""

    if not arguments:
        return "{}"
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except TypeError:
        # Extremely rare: arguments contain unjson-able objects. Fall back
        # to a repr hash so the guard still works deterministically.
        return repr(sorted(arguments.items(), key=lambda item: item[0]))


def _args_hash(tool_name: str, arguments: dict[str, Any] | None) -> str:
    canonical = _canonical_args(arguments)
    return hashlib.sha1(f"{tool_name}::{canonical}".encode()).hexdigest()[:16]


def check_loop_guard(
    state: LoopGuardState,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> str | None:
    """Inspect a pending tool call and return a teach-back message or None.

    Call ``record_tool_call`` after the tool has actually been dispatched
    (or after the teach-back is delivered). This split lets callers short-
    circuit execution on a repeat detection.
    """

    if tool_name in _LOOP_GUARD_EXEMPT_TOOLS:
        return None
    key = (tool_name, _args_hash(tool_name, arguments))
    if state.last_key == key and state.streak >= 1:
        return (
            "Detected a repeated identical tool call with no intervening "
            f"change — '{tool_name}' was called twice in a row with the same "
            "arguments and produced no new information. Proceed with a "
            "different concrete action, update the todo list, or call "
            "step_complete if the objective is done. Do not retry the same "
            "call."
        )
    return None


def record_tool_call(
    state: LoopGuardState,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> None:
    """Record that a tool call has been dispatched.

    Increments the streak when the (name, args) tuple matches the previous
    recording; resets otherwise. Must be called for every executed call,
    including calls that were intercepted by the controller (step_complete,
    step_todo_write, etc.) so the guard reflects the ground-truth sequence.
    """

    key = (tool_name, _args_hash(tool_name, arguments))
    if state.last_key == key:
        state.streak += 1
    else:
        state.last_key = key
        state.streak = 1


def loop_guard_rejection_payload(
    tool_name: str,
    arguments: dict[str, Any] | None,
    message: str,
) -> str:
    """Build a JSON payload used as the tool result for a loop-broken call."""

    _ = arguments  # reserved for future diagnostics; kept stable for callers
    return json.dumps(
        {
            "status": "rejected",
            "reason": "loop_detected",
            "message": message,
            "tool": tool_name,
        }
    )


# ---------------------------------------------------------------------------
# Argument sanity gate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ArgumentSanityViolation:
    """A single argument-sanity defect detected on a tool call."""

    reason: str
    message: str


_DISALLOWED_FILE_PATHS: frozenset[str] = frozenset(
    {
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
    }
)

_PLACEHOLDER_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "dummy",
        "placeholder",
        "example",
        "<call_id>",
        "<id>",
        "...",
    }
)


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def check_argument_sanity(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> ArgumentSanityViolation | None:
    """Return a rejection reason when arguments are obviously invalid.

    Rules are deliberately narrow and teach-back oriented. When in doubt we
    allow the tool call through; the executor and guardrails layer remain
    the authoritative gate for real security or correctness decisions.
    """

    if not arguments:
        # An empty dict may still be invalid for some tools, but rejecting
        # all missing-arg calls here would swallow legitimate zero-arg
        # tools. Individual rules below handle their own presence checks.
        arguments = {}

    # Rule: tool-output-reader builtins must not use placeholder call_ids
    # ("dummy", "<call_id>", ...). Observed in the daily-brief regression
    # trace where the model copied example syntax from the tool
    # description verbatim. All four readers accept a call_id the model
    # is expected to pull from a prior tool_call event.
    if tool_name in {
        "list_tool_output_anchors",
        "read_tool_output_anchor",
        "read_tool_output",
        "search_tool_output",
    }:
        call_id = _as_str(arguments.get("call_id")).strip()
        if not call_id:
            return ArgumentSanityViolation(
                reason="invalid_call_id",
                message=(
                    f"{tool_name} requires a real prior tool call_id. "
                    "Omit this call if you do not have one; never pass an "
                    "empty string."
                ),
            )
        if call_id.lower() in _PLACEHOLDER_IDENTIFIERS:
            return ArgumentSanityViolation(
                reason="invalid_call_id",
                message=(
                    f"{tool_name} was called with a placeholder call_id "
                    f"({call_id!r}). Use the actual call_id string from a "
                    "prior tool_call event, or do not call this tool."
                ),
            )

    # Filesystem write/edit tools must not target special device files.
    if tool_name in {"write", "edit", "multiedit", "apply_patch"}:
        file_path = _as_str(arguments.get("file_path")).strip()
        if file_path in _DISALLOWED_FILE_PATHS:
            return ArgumentSanityViolation(
                reason="invalid_file_path",
                message=(
                    f"{tool_name} cannot target special device file "
                    f"{file_path!r}. Supply an actual file path, or do not "
                    "call this tool if no file change is required."
                ),
            )
        if tool_name == "apply_patch":
            operation = arguments.get("operation")
            if isinstance(operation, dict):
                target = _as_str(operation.get("path")).strip()
                if target in _DISALLOWED_FILE_PATHS:
                    return ArgumentSanityViolation(
                        reason="invalid_file_path",
                        message=(
                            f"{tool_name} cannot target special device file "
                            f"{target!r}. Supply an actual file path, or do not "
                            "call this tool if no file change is required."
                        ),
                    )
            patch_text = _as_str(arguments.get("patchText"))
            for line in patch_text.splitlines():
                for prefix in (
                    "*** Add File: ",
                    "*** Update File: ",
                    "*** Delete File: ",
                    "*** Move to: ",
                ):
                    if not line.startswith(prefix):
                        continue
                    target = line[len(prefix) :].strip()
                    if target in _DISALLOWED_FILE_PATHS:
                        return ArgumentSanityViolation(
                            reason="invalid_file_path",
                            message=(
                                f"{tool_name} cannot target special device file "
                                f"{target!r}. Supply an actual file path, or do not "
                                "call this tool if no file change is required."
                            ),
                        )
                for prefix in ("--- ", "+++ "):
                    if not line.startswith(prefix):
                        continue
                    target = line[len(prefix) :].strip()
                    if target.startswith(("a/", "b/")):
                        target = target[2:]
                    if target in _DISALLOWED_FILE_PATHS:
                        return ArgumentSanityViolation(
                            reason="invalid_file_path",
                            message=(
                                f"{tool_name} cannot target special device file "
                                f"{target!r}. Supply an actual file path, or do not "
                                "call this tool if no file change is required."
                            ),
                        )

    # multiedit with an empty edits array is a no-op placeholder call.
    if tool_name == "multiedit":
        edits = arguments.get("edits")
        if not isinstance(edits, list) or len(edits) == 0:
            return ArgumentSanityViolation(
                reason="empty_edits",
                message=(
                    "multiedit requires at least one edit. Pass a non-empty "
                    "edits list, or use the edit tool for a single change, "
                    "or do not call this tool if no change is required."
                ),
            )

    return None


def argument_sanity_rejection_payload(
    tool_name: str,
    arguments: dict[str, Any] | None,
    violation: ArgumentSanityViolation,
) -> str:
    """Build a teach-back tool-result payload for a sanity-gate rejection."""

    # Truncate echoed arguments so the teach-back stays small.
    try:
        received_json = json.dumps(arguments or {}, default=str)
    except TypeError:
        received_json = "{}"
    received_repr = received_json[:500]
    return json.dumps(
        {
            "status": "rejected",
            "reason": violation.reason,
            "message": violation.message,
            "tool": tool_name,
            "received": received_repr,
        }
    )


__all__ = [
    "ArgumentSanityViolation",
    "LoopGuardState",
    "argument_sanity_rejection_payload",
    "check_argument_sanity",
    "check_loop_guard",
    "loop_guard_rejection_payload",
    "record_tool_call",
]
