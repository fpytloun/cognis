"""Robust JSON extraction from LLM responses.

Provides multi-layer extraction that handles models which ignore
``response_format={"type": "json_object"}`` (e.g. gpt-oss-120b on Groq).

Layers (tried in order, first success wins):
1. Direct ``json.loads`` after proper code-fence removal
2. Brace-matching extraction (JSON embedded in prose)
3. Regex field extraction (partially structured output)

For evaluator-specific use, ``infer_evaluation_from_text`` provides a
semantic keyword fallback when all JSON extraction layers fail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from prometheus_client import Counter

from cognis.logging import get_logger

logger = get_logger(__name__)

JSON_EXTRACTION_TOTAL = Counter(
    "cognis_json_extraction_total",
    "JSON extraction attempts by method and caller",
    labelnames=("method", "label"),
)

# ---------------------------------------------------------------------------
# LLM response text extraction
# ---------------------------------------------------------------------------


def extract_text_from_response(response: dict[str, Any]) -> str:
    """Extract text content from an LLM response dict.

    Navigates the standard ``choices[0].message.content`` structure
    returned by LiteLLM / OpenAI-compatible APIs.

    For reasoning models (gpt-oss, deepseek, etc.), the litellm client
    may move the response content to ``reasoning_content`` (standardized)
    or ``reasoning`` (raw provider field), leaving ``content`` empty.
    This function checks those fields as fallbacks.
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""

    # Primary: message.content (standard OpenAI format)
    content = _extract_text_payload(message.get("content"), serialize_objects=False)
    if content.strip():
        return content

    # Fallback: reasoning models — litellm client-side processing may
    # move content to reasoning_content, leaving content empty.
    for field in ("reasoning_content", "reasoning"):
        fallback = _extract_text_payload(message.get(field), serialize_objects=True)
        if fallback.strip():
            logger.info(
                "Using reasoning field as response content",
                extra={"extra_data": {"field": field, "content_length": len(fallback)}},
            )
            return fallback

    return content


async def maybe_fallback_to_plain_json_response(
    response: dict[str, Any],
    *,
    generate_response: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    label: str,
    logger_obj: Any,
    warning_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retry without structured output when JSON-mode response is unusable.

    The fallback is intended for small JSON-only tasks where prompt-level
    constraints are usually enough and transport-level structured output may
    fail on some provider/model paths.
    """

    should_fallback, reason = should_fallback_to_plain_json_response(response, label=label)
    if not should_fallback:
        return response

    extra_data = {"label": label, "reason": reason}
    if warning_context:
        extra_data.update(warning_context)
    logger_obj.warning(
        "Structured JSON response unusable, retrying with plain-text JSON fallback",
        extra={"extra_data": extra_data},
    )
    return await generate_response({})


def should_fallback_to_plain_json_response(
    response: dict[str, Any], *, label: str
) -> tuple[bool, str]:
    """Return whether a structured JSON call should retry without response_format."""

    if _extract_refusal_text(response):
        return False, "refusal"

    content = extract_text_from_response(response)
    if not content.strip():
        return True, "empty_response"

    try:
        extract_json_object(content, label=label)
    except ValueError:
        return True, "json_parse_failed"
    return False, ""


def _extract_refusal_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    refusal = message.get("refusal")
    return refusal.strip() if isinstance(refusal, str) else ""


def _extract_text_payload(value: Any, *, serialize_objects: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _extract_text_payload(item, serialize_objects=serialize_objects)
            if text:
                parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        if serialize_objects and any(
            key in value for key in ("decision", "feedback", "workflow_id", "confidence", "reason")
        ):
            try:
                return json.dumps(value, ensure_ascii=True, sort_keys=True)
            except TypeError:
                return str(value)
        for key in ("text", "content", "summary", "reasoning", "value"):
            text = _extract_text_payload(value.get(key), serialize_objects=serialize_objects)
            if text:
                return text
        if serialize_objects:
            try:
                return json.dumps(value, ensure_ascii=True, sort_keys=True)
            except TypeError:
                return str(value)
        return ""
    return ""


# ---------------------------------------------------------------------------
# Multi-layer JSON extraction
# ---------------------------------------------------------------------------

# Regex for markdown code fences: ```<optional lang>\n...\n```
_CODE_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL,
)


def extract_json_object(content: str, *, label: str = "unknown") -> dict[str, Any]:
    """Extract a JSON object from LLM output using multiple strategies.

    Tries three layers in order.  Returns the first successfully parsed
    ``dict``.  Raises ``ValueError`` if all layers fail.

    Args:
        content: Raw text content from the LLM response.
        label: Caller label for Prometheus metrics (e.g. "evaluator",
            "classifier").

    Raises:
        ValueError: If no JSON object could be extracted.
    """
    if not content or not content.strip():
        raise ValueError("Empty content")

    # Layer 1: direct parse (with code-fence handling)
    result = _try_direct_parse(content)
    if result is not None:
        JSON_EXTRACTION_TOTAL.labels(method="direct", label=label).inc()
        return result

    # Layer 2: brace-matching extraction
    result = _try_extract_json_block(content)
    if result is not None:
        JSON_EXTRACTION_TOTAL.labels(method="json_block", label=label).inc()
        logger.debug(
            "JSON extracted via brace-matching",
            extra={"extra_data": {"label": label}},
        )
        return result

    # Layer 3: regex field extraction
    result = _try_regex_fields(content)
    if result is not None:
        JSON_EXTRACTION_TOTAL.labels(method="regex_fields", label=label).inc()
        logger.info(
            "JSON extracted via regex field matching",
            extra={"extra_data": {"label": label}},
        )
        return result

    JSON_EXTRACTION_TOTAL.labels(method="failed", label=label).inc()
    raise ValueError("Could not extract JSON object from LLM response")


def _try_direct_parse(content: str) -> dict[str, Any] | None:
    """Layer 1: direct json.loads with proper code-fence removal."""
    cleaned = content.strip()

    # Handle markdown code fences via regex (not strip)
    fence_match = _CODE_FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return cast(dict[str, Any], result)
    except json.JSONDecodeError:
        pass
    return None


def _try_extract_json_block(content: str) -> dict[str, Any] | None:
    """Layer 2: find the first balanced ``{...}`` block via brace matching."""
    start = content.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(content)):
        char = content[i]

        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            if in_string:
                escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = content[start : i + 1]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, dict):
                        return cast(dict[str, Any], result)
                except json.JSONDecodeError:
                    pass
                # Matched braces but invalid JSON — stop trying
                return None

    return None


def _try_regex_fields(content: str) -> dict[str, Any] | None:
    """Layer 3: extract key-value pairs via regex patterns.

    Looks for ``"key": "value"`` (JSON-style) and ``key: value``
    (relaxed) patterns for common field names.
    """
    result: dict[str, Any] = {}

    common_fields = [
        "decision",
        "reasoning",
        "feedback",
        "workflow_id",
        "confidence",
        "reason",
    ]

    for field in common_fields:
        # JSON-style: "field": "value"
        match = re.search(rf'"{field}"\s*:\s*"([^"]*)"', content)
        if match:
            result[field] = match.group(1)
            continue

        # JSON-style: "field": number
        match = re.search(rf'"{field}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', content)
        if match:
            result[field] = _coerce_number(match.group(1))
            continue

        # Relaxed: field: value or field = value (until delimiter)
        match = re.search(
            rf"(?:^|\n)\s*{field}\s*[:=]\s*[\"']?([^\"',\n}}]+)",
            content,
            re.IGNORECASE,
        )
        if match:
            result[field] = match.group(1).strip().strip("\"'")

    return result if result else None


def _coerce_number(value: str) -> int | float:
    """Coerce a numeric string to int or float."""
    if "." in value:
        return float(value)
    return int(value)


# ---------------------------------------------------------------------------
# Evaluator-specific semantic inference
# ---------------------------------------------------------------------------

_FAILED_KEYWORDS = (
    "cannot succeed",
    "impossible",
    "fatal",
    "fundamentally cannot",
    "fundamentally broken",
    "cannot be completed",
    "cannot be done",
    "unrecoverable",
)

_REVISE_KEYWORDS = (
    "revise",
    "revision needed",
    "incomplete",
    "not met",
    "not satisfied",
    "not satisf",
    "missing",
    "insufficient",
    "doesn't mention",
    "does not mention",
    "doesn't include",
    "does not include",
    "lacks",
    "needs more",
    "not complete",
    "partially",
    "not fully",
    "should also",
    "failed to",
    "did not",
    "didn't",
    "no evidence",
)

_APPROVED_KEYWORDS = (
    "approved",
    "complete",
    "satisf",
    "met the objective",
    "well done",
    "successfully",
    "all requirements",
    "fully met",
    "looks good",
    "objective is met",
)


def infer_evaluation_from_text(content: str) -> dict[str, Any]:
    """Infer an evaluator decision from unstructured text.

    Used as a last-resort fallback when all JSON extraction layers fail.
    Scans for domain-specific keywords to determine the most likely
    decision.  Defaults to ``"approved"`` (fail-open) when ambiguous.

    Returns a dict matching the evaluator JSON schema:
    ``{"decision": "...", "reasoning": "...", "feedback": None}``.
    """
    if not content or not content.strip():
        return {
            "decision": "approved",
            "reasoning": "Empty evaluator response — defaulting to approved",
            "feedback": None,
        }

    lower = content.lower()
    truncated = content[:500]

    # Check failure indicators first (most specific)
    if any(keyword in lower for keyword in _FAILED_KEYWORDS):
        JSON_EXTRACTION_TOTAL.labels(method="semantic_inference", label="evaluator").inc()
        logger.info(
            "Evaluator decision inferred from text: failed",
            extra={"extra_data": {}},
        )
        return {
            "decision": "failed",
            "reasoning": f"Inferred from text: {truncated}",
            "feedback": truncated,
        }

    # Check revision indicators (second priority)
    if any(keyword in lower for keyword in _REVISE_KEYWORDS):
        JSON_EXTRACTION_TOTAL.labels(method="semantic_inference", label="evaluator").inc()
        logger.info(
            "Evaluator decision inferred from text: revise",
            extra={"extra_data": {}},
        )
        return {
            "decision": "revise",
            "reasoning": f"Inferred from text: {truncated}",
            "feedback": truncated,
        }

    # Check approval indicators
    if any(keyword in lower for keyword in _APPROVED_KEYWORDS):
        JSON_EXTRACTION_TOTAL.labels(method="semantic_inference", label="evaluator").inc()
        logger.info(
            "Evaluator decision inferred from text: approved",
            extra={"extra_data": {}},
        )
        return {
            "decision": "approved",
            "reasoning": f"Inferred from text: {truncated}",
            "feedback": None,
        }

    # Ambiguous — fail-open
    JSON_EXTRACTION_TOTAL.labels(method="semantic_inference", label="evaluator").inc()
    logger.warning(
        "Could not infer evaluator decision from text, defaulting to approved",
        extra={"extra_data": {}},
    )
    return {
        "decision": "approved",
        "reasoning": f"Could not determine decision from response: {truncated}",
        "feedback": None,
    }
