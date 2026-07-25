"""Canonical executor-side inference request and payload helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

SENSITIVE_INFERENCE_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "credentials_json",
    "settings_json",
    "x-api-key",
}


@dataclass(slots=True)
class CognisInferenceRequest:
    """Structured boundary between executor RPC handling and inference backends."""

    model: str
    messages: list[dict[str, Any]]
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    backend: str = "litellm"
    stream: bool = True
    provider_id: str | None = None
    owner_email: str | None = None
    backend_metadata: dict[str, Any] = field(default_factory=dict)


def redact_inference_payload(value: Any) -> Any:
    """Return a log-safe copy of inference data with credentials removed."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in SENSITIVE_INFERENCE_KEYS:
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_inference_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_inference_payload(item) for item in value]
    return value


def json_safe_inference_payload(value: Any) -> Any:
    """Return a JSON-serializable copy of inference stream data."""

    if hasattr(value, "model_dump"):
        try:
            return json_safe_inference_payload(value.model_dump(mode="json", warnings=False))
        except TypeError:
            try:
                return json_safe_inference_payload(value.model_dump(mode="json"))
            except TypeError:
                return json_safe_inference_payload(value.model_dump())
    if isinstance(value, dict):
        return {str(key): json_safe_inference_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe_inference_payload(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def redact_inference_request(request: CognisInferenceRequest) -> dict[str, Any]:
    """Summarize an inference request without exposing prompt or credentials."""

    return {
        "request_id": request.request_id,
        "model": request.model,
        "backend": request.backend,
        "provider_id": request.provider_id,
        "owner_email": request.owner_email,
        "message_count": len(request.messages),
        "request_kwargs": redact_inference_payload(request.request_kwargs),
    }
