"""Controller-side validation for tool call arguments.

Stage 20+ refactors left controller-intercepted tools without a
validation layer between the raw LLM output and the dedicated
handlers. This module closes the gap by providing a single,
registry-sourced ``jsonschema`` validator that every tool handler can
use before it mutates state.

The validator intentionally uses the same JSON Schema that is sent to
the LLM as its function schema. Keeping the source of truth single and
explicit means validation errors we surface back to the model match the
model's view of the tool contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator, SchemaError, ValidationError
from prometheus_client import Counter

from cognis.logging import get_logger

logger = get_logger(__name__)


MALFORMED_TOOL_CALL_TOTAL = Counter(
    "cognis_tool_call_malformed_total",
    "Tool calls rejected due to invalid/malformed arguments.",
    labelnames=("tool_name", "reason"),
)


@dataclass(slots=True)
class ToolArgumentError:
    """Structured failure returned to the LLM when arguments are invalid."""

    tool_name: str
    reason: str
    message: str
    errors: list[str]

    def as_tool_result(self) -> dict[str, Any]:
        """Produce a machine-readable tool_result payload for the LLM."""

        return {
            "error": "invalid_tool_arguments",
            "tool": self.tool_name,
            "reason": self.reason,
            "message": self.message,
            "errors": self.errors,
        }


def validate_tool_arguments(
    tool_name: str,
    raw_arguments: Any,
    *,
    schema: dict[str, Any] | None,
) -> ToolArgumentError | None:
    """Validate ``raw_arguments`` against ``schema``.

    Returns ``None`` on success. Returns a :class:`ToolArgumentError` on
    failure; the handler should short-circuit, emit the structured
    payload as a tool_result with ``is_error=True``, and continue the
    agent loop so the LLM can correct itself.
    """

    # Stream accumulator wraps unparseable JSON as ``{"_raw": "..."}``.
    if isinstance(raw_arguments, dict) and set(raw_arguments.keys()) == {"_raw"}:
        MALFORMED_TOOL_CALL_TOTAL.labels(tool_name=tool_name, reason="unparseable_json").inc()
        preview = str(raw_arguments.get("_raw", ""))
        return ToolArgumentError(
            tool_name=tool_name,
            reason="unparseable_json",
            message=(
                "Tool call arguments were not valid JSON. Reissue the call "
                "with a complete JSON object that matches the tool schema."
            ),
            errors=[f"raw prefix: {preview[:200]}"],
        )

    if not isinstance(raw_arguments, dict):
        MALFORMED_TOOL_CALL_TOTAL.labels(tool_name=tool_name, reason="not_object").inc()
        return ToolArgumentError(
            tool_name=tool_name,
            reason="not_object",
            message="Tool call arguments must be a JSON object.",
            errors=[f"received type: {type(raw_arguments).__name__}"],
        )

    if schema is None:
        return None

    try:
        validator = Draft7Validator(schema)
    except SchemaError:
        # Fail open on a bad schema — this is a controller programming
        # error; we should not break production tool calls for it.
        logger.warning(
            "tool_arguments: invalid schema for tool; skipping validation",
            extra={"extra_data": {"tool_name": tool_name}},
        )
        return None

    errors: list[ValidationError] = sorted(
        validator.iter_errors(raw_arguments),
        key=lambda err: err.path,
    )
    if not errors:
        return None

    rendered_errors: list[str] = []
    for err in errors[:5]:
        path = ".".join(str(part) for part in err.absolute_path) or "<root>"
        rendered_errors.append(f"{path}: {err.message}")

    MALFORMED_TOOL_CALL_TOTAL.labels(tool_name=tool_name, reason="schema_violation").inc()
    return ToolArgumentError(
        tool_name=tool_name,
        reason="schema_violation",
        message=(
            f"Tool call arguments for {tool_name!r} did not match the expected schema. "
            "Reissue the call with the required fields."
        ),
        errors=rendered_errors,
    )
