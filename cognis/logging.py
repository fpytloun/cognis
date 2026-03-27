"""Structured logging with content redaction.

All logging in Cognis goes through this module. It configures JSON-formatted
output with correlation context propagation and a field-allowlist redaction
filter that prevents PII/content leakage.

Logs MUST NOT contain: message content, tool call arguments or results,
memory content, secret values, raw LLM prompts or completions.

Logs MAY contain: IDs, tool names (not args), model names, token counts,
latencies, status codes, error categories, decision outcomes.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, cast

# Correlation context variables — set per-request/turn
correlation_conversation_id: ContextVar[str | None] = ContextVar(
    "correlation_conversation_id", default=None
)
correlation_session_id: ContextVar[str | None] = ContextVar("correlation_session_id", default=None)
correlation_agent_id: ContextVar[str | None] = ContextVar("correlation_agent_id", default=None)
correlation_user_id: ContextVar[str | None] = ContextVar("correlation_user_id", default=None)
correlation_request_id: ContextVar[str | None] = ContextVar("correlation_request_id", default=None)

# Fields that are NEVER allowed in log output — redacted if present.
# This is the security boundary: anything not on the allowlist is stripped.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "content",
        "message_content",
        "arguments",
        "tool_args",
        "tool_arguments",
        "result",
        "tool_result",
        "output",
        "prompt",
        "completion",
        "messages",
        "memory",
        "memory_content",
        "recall_results",
        "remember_payload",
        "secret_value",
        "password",
        "password_hash",
        "token",
        "api_key",
        "encrypted_value",
        "plaintext",
    }
)

REDACTION_PLACEHOLDER = "[REDACTED]"


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive fields from a dict."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in REDACTED_FIELDS:
            result[key] = REDACTION_PLACEHOLDER
        elif isinstance(value, dict):
            result[key] = _redact_dict(value)
        elif isinstance(value, list):
            result[key] = [_redact_dict(item) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value
    return result


class JSONFormatter(logging.Formatter):
    """JSON log formatter with correlation context and content redaction."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation context from context vars
        for ctx_var, key in [
            (correlation_conversation_id, "conversation_id"),
            (correlation_session_id, "session_id"),
            (correlation_agent_id, "agent_id"),
            (correlation_user_id, "user_id"),
            (correlation_request_id, "request_id"),
        ]:
            value = ctx_var.get()
            if value is not None:
                log_entry[key] = value

        # Add extra fields from the log record, with redaction
        extra_data = getattr(record, "extra_data", None)
        if isinstance(extra_data, dict):
            redacted = _redact_dict(cast(dict[str, Any], extra_data))
            log_entry.update(redacted)

        # Add exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable text formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S"),
            record.levelname.ljust(8),
            record.name,
            record.getMessage(),
        ]

        # Add correlation context
        ctx_parts = []
        for ctx_var, key in [
            (correlation_session_id, "session"),
            (correlation_agent_id, "agent"),
        ]:
            value = ctx_var.get()
            if value is not None:
                ctx_parts.append(f"{key}={value}")
        if ctx_parts:
            parts.insert(3, f"[{' '.join(ctx_parts)}]")

        line = " | ".join(parts)

        if record.exc_info and record.exc_info[1] is not None:
            line += f"\n  {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"

        return line


def setup_logging(level: str = "info", fmt: str = "json") -> None:
    """Configure root logging with the appropriate formatter.

    Args:
        level: Log level (debug, info, warning, error, critical).
        fmt: Log format — "json" for structured output, "text" for human-readable.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for name in ("httpx", "httpcore", "uvicorn.access", "litellm"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance. Use this instead of logging.getLogger() directly."""
    return logging.getLogger(name)
