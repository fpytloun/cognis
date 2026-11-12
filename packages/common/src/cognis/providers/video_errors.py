"""Fail-closed release of opt-in Interactions error messages, never raw bodies."""

from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

# https://ai.google.dev/gemini-api/docs/api-errors
INTERACTIONS_ERROR_CODES = frozenset(
    [
        "invalid_request",
        "failed_precondition",
        "out_of_range",
        "parameter_unknown",
        "authentication",
        "payment_required",
        "permission_denied",
        "not_found",
        "conflict",
        "resource_exhausted",
        "rate_limit_exceeded",
        "internal",
        "unimplemented",
        "service_unavailable",
        "deadline_exceeded",
        "bad_request",
        "unauthorized",
        "forbidden",
        "too_many_requests",
        "internal_server_error",
    ]
)

# Public schema identifiers, not arbitrary identifiers supplied in a request.
_PUBLIC = frozenset(
    [
        "model",
        "input",
        "text",
        "data",
        "mime_type",
        "type",
        "store",
        "stream",
        "previous_interaction_id",
        "response_format",
        "delivery",
        "resolution",
        "aspect_ratio",
        "video",
        "image",
        "uri",
        "inline",
        "tools",
        "function",
        "code_execution",
        "mcp_server",
        "filesystem",
        "google_maps",
        "google_search",
        "bash",
        "computer_use",
        "file_search",
        "url_context",
        "gemini-omni-1.1-flash",
        "360p",
        "720p",
        "1080p",
        "4k",
        "400",
        "401",
        "402",
        "403",
        "404",
        "409",
        "416",
        "429",
        "500",
        "501",
        "503",
        "504",
    ]
)
_WORDS = frozenset(
    [
        "a",
        "access",
        "an",
        "and",
        "are",
        "as",
        "at",
        "available",
        "be",
        "because",
        "been",
        "billing",
        "blocked",
        "by",
        "can",
        "cannot",
        "check",
        "configured",
        "contain",
        "contains",
        "could",
        "currently",
        "denied",
        "disabled",
        "do",
        "does",
        "eligible",
        "eligibility",
        "enable",
        "enabled",
        "endpoint",
        "exceeded",
        "expected",
        "failed",
        "field",
        "fields",
        "for",
        "format",
        "from",
        "has",
        "have",
        "image",
        "images",
        "in",
        "input",
        "inputs",
        "interaction",
        "interactions",
        "invalid",
        "is",
        "it",
        "key",
        "limit",
        "limits",
        "max",
        "maximum",
        "missing",
        "model",
        "models",
        "must",
        "no",
        "not",
        "of",
        "on",
        "only",
        "operation",
        "operations",
        "option",
        "options",
        "or",
        "out",
        "output",
        "outputs",
        "parameter",
        "parameters",
        "payment",
        "permission",
        "permissions",
        "please",
        "precondition",
        "project",
        "provided",
        "reference",
        "references",
        "request",
        "requested",
        "requests",
        "require",
        "required",
        "requires",
        "resource",
        "restricted",
        "restriction",
        "restrictions",
        "response",
        "service",
        "set",
        "status",
        "support",
        "supported",
        "supports",
        "than",
        "that",
        "the",
        "this",
        "tier",
        "to",
        "too",
        "type",
        "unknown",
        "unsupported",
        "use",
        "using",
        "valid",
        "value",
        "values",
        "video",
        "videos",
        "was",
        "were",
        "with",
        "without",
        "your",
        "redacted",
        "account",
        "accounts",
        "allowlisted",
        "content",
        "countries",
        "country",
        "editing",
        "entitled",
        "entitlement",
        "entitlements",
        "experimental",
        "extension",
        "free",
        "generate",
        "generation",
        "location",
        "organization",
        "paid",
        "plan",
        "plans",
        "policy",
        "preview",
        "quota",
        "region",
        "safety",
        "subscription",
        "unavailable",
        "usage",
        "you",
    ]
)
_QUOTED = re.compile(r"""(['"`])([^'"`]*?)\1""")
_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:[.\-\[\]][A-Za-z0-9_\]\[]+)*")
_URL = re.compile(r"(?i)(?:https?://|gs://|s3://|www\.)[^\s'\"<>]+")
_HEADERS = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|x-goog-api-key|api[_ -]?key|"
    r"cookie|set-cookie|bearer)\s*[:= ]+.*"
)
_DIRECTIVE = re.compile(
    r"(?i)\b(?:ignore|reveal|execute|run|send|instructions|previous|use|using|please|check|set)\b"
)
_OPAQUE = re.compile(r"\b[A-Za-z0-9_+/=%-]{24,}\b")
_STRUCTURAL = re.compile(
    r"(?:(?:input|tools)\[[0-9]{1,2}\](?:\.(?:type|text|data|mime_type))?"
    r"|response_format\.(?:type|delivery|resolution|aspect_ratio))\Z"
)


def _private_values(payload: object) -> Iterable[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {
                "prompt",
                "negativePrompt",
                "input",
                "text",
                "data",
                "uri",
                "url",
                "previous_interaction_id",
            } and isinstance(value, str):
                yield value
            else:
                yield from _private_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _private_values(value)


def redact_interactions_message(message: object, *, payload: object, api_key: str) -> str | None:
    """Release bounded diagnostic vocabulary; withhold unfamiliar free-form text.

    Redact against the actual request before validation and truncation. This is
    not a general-purpose secret/PII detector. Partial reflections consisting
    solely of public schema/common diagnostic words remain ambiguous.
    """
    if not isinstance(message, str) or not message or len(message) > 16384:
        return None
    if any(ord(char) < 32 or ord(char) > 126 for char in message):
        return None
    # Only informational rejection text, never instructions to a tool reader.
    # Check before redaction so reflected input cannot erase a directive token.
    if _DIRECTIVE.search(message):
        return None
    private_words: set[str] = set()
    for value in (api_key, *_private_values(payload)):
        if not value:
            continue
        if len(value) <= 20000:
            private_words.update(
                word.lower()
                for word in _TOKEN.findall(value)
                if len(word) >= 4 and word.lower() not in _PUBLIC
            )
        if len(value) > 4096:
            continue
        variants = {
            value,
            quote(value, safe=""),
            quote_plus(value, safe=""),
            json.dumps(value, ensure_ascii=True)[1:-1],
            html.escape(value),
            base64.b64encode(value.encode()).decode(),
            base64.urlsafe_b64encode(value.encode()).decode(),
            value.encode().hex(),
        }
        for variant in sorted(variants, key=len, reverse=True):
            message = re.sub(
                r"(?<!\w)" + re.escape(variant) + r"(?!\w)",
                "[redacted]",
                message,
                flags=re.IGNORECASE,
            )
    message = _URL.sub("[redacted]", message)
    message = _HEADERS.sub("[redacted]", message)
    message = _OPAQUE.sub("[redacted]", message)

    def quoted(match: re.Match[str]) -> str:
        value = match[2]
        if value in _PUBLIC or _STRUCTURAL.fullmatch(value):
            return match[0]
        return "[redacted]"

    message = _QUOTED.sub(quoted, message)

    def private_word(match: re.Match[str]) -> str:
        return "[redacted]" if match[0].lower() in private_words else match[0]

    message = _TOKEN.sub(private_word, message)
    if re.search(r"""[^A-Za-z0-9_ .,\-:;!?'"`()\[\]/]""", message):
        return None
    tokens = _TOKEN.findall(message)
    if not tokens or any(
        token.lower() not in _WORDS and token not in _PUBLIC and not _STRUCTURAL.fullmatch(token)
        for token in tokens
    ):
        return None
    if not any(
        word in message.lower()
        for word in (
            "model",
            "field",
            "parameter",
            "billing",
            "permission",
            "support",
            "access",
            "region",
            "country",
            "quota",
            "account",
            "policy",
        )
    ):
        return None
    # Validate the entire message: a malicious suffix must not be truncated away.
    return message if len(message) <= 512 else message[:509] + "..."
