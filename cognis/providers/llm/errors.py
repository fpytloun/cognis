"""Shared LLM error classification and stream failure payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict


class MidStreamErrorCategory(StrEnum):
    """Stable categories for provider stream failures."""

    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    ARTIFACT_FETCH = "artifact_fetch"
    PROVIDER_5XX = "provider_5xx"
    CONNECTION = "connection"
    IDLE_TIMEOUT_RAW = "idle_timeout_raw"
    IDLE_TIMEOUT_ACTIVITY = "idle_timeout_activity"
    IDLE_TIMEOUT_REASONING = "idle_timeout_reasoning"
    CONTENT_POLICY = "content_policy"
    REASONING_SUMMARY_REJECTED = "reasoning_summary_rejected"
    OTHER = "other"


class MidStreamErrorPayload(TypedDict, total=False):
    """Structured error payload emitted by LLM streaming providers."""

    category: str
    code: str | None
    message: str
    provider_event: str | None
    response_id: str | None
    response_status: str | None
    retry_after_seconds: float | None
    details: dict[str, Any] | None
    artifact_urls: list[str]
    artifact_ids: list[str]
    param: str | None


class LLMStreamFailure(RuntimeError):
    """Base class for normalized LLM stream failures."""

    def __init__(self, message: str, *, payload: MidStreamErrorPayload | None = None) -> None:
        self.payload = payload or {
            "category": MidStreamErrorCategory.OTHER.value,
            "message": message,
        }
        super().__init__(message)

    def to_payload(self) -> MidStreamErrorPayload:
        """Return the normalized client-safe payload."""

        return self.payload


class LLMStreamIdleTimeout(TimeoutError, LLMStreamFailure):
    """Raised when an LLM stream is idle longer than allowed."""

    def __init__(self, message: str, *, payload: MidStreamErrorPayload | None = None) -> None:
        self.payload = payload or {
            "category": MidStreamErrorCategory.IDLE_TIMEOUT_ACTIVITY.value,
            "message": message,
        }
        TimeoutError.__init__(self, message)

    def to_payload(self) -> MidStreamErrorPayload:
        """Return the normalized client-safe payload."""

        return self.payload


class LLMStreamProviderError(LLMStreamFailure):
    """Raised when an LLM stream fails at the provider layer."""


class OpenAIToolSearchFallbackRequired(RuntimeError):
    """Signal that native OpenAI Responses tool search must be downgraded."""

    def __init__(self, *, provider_id: str, model_id: str, reason: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.reason = reason
        super().__init__(
            "native OpenAI Responses tool search is unsupported for "
            f"provider={provider_id!r}, model={model_id!r}; reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class ToolArgumentParseFailure:
    """A streamed tool call could not be parsed into JSON arguments."""

    call_id: str
    name: str
    raw: str
    recovery_attempts: tuple[str, ...]
    reason: str = "invalid_json"
    message: str | None = None
    argument_length: int | None = None


def classify_llm_exception(exc: BaseException) -> MidStreamErrorPayload:
    """Classify a provider exception into a stable stream error payload."""

    to_payload = getattr(exc, "to_payload", None)
    if callable(to_payload):
        payload = to_payload()
        if isinstance(payload, dict):
            return payload

    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    exc_name = type(exc).__name__
    category = MidStreamErrorCategory.OTHER
    code: str | None = exc_name
    retry_after: float | None = None

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        retry_after_raw = headers.get("retry-after") if hasattr(headers, "get") else None
        try:
            retry_after = float(retry_after_raw) if retry_after_raw is not None else None
        except (TypeError, ValueError):
            retry_after = None

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        body_code = err.get("code") or err.get("type")
        if body_code:
            code = str(body_code)
        param = err.get("param")
    else:
        param = None

    if "reasoning.summary" in lowered or param == "reasoning.summary":
        category = MidStreamErrorCategory.REASONING_SUMMARY_REJECTED
    elif "context" in lowered and any(token in lowered for token in ("window", "length", "token")):
        category = MidStreamErrorCategory.CONTEXT_OVERFLOW
    elif status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        category = MidStreamErrorCategory.RATE_LIMIT
    elif _looks_like_artifact_fetch_error(lowered, param):
        category = MidStreamErrorCategory.ARTIFACT_FETCH
    elif status in {500, 502, 503, 504} or any(
        marker in lowered for marker in ("server error", "bad gateway", "service unavailable")
    ):
        category = MidStreamErrorCategory.PROVIDER_5XX
    elif any(marker in lowered for marker in ("connection", "timeout", "timed out")):
        category = MidStreamErrorCategory.CONNECTION
    elif any(marker in lowered for marker in ("content policy", "content_filter", "refusal")):
        category = MidStreamErrorCategory.CONTENT_POLICY

    payload: MidStreamErrorPayload = {
        "category": category.value,
        "code": code,
        "message": message[:500],
        "retry_after_seconds": retry_after,
    }
    if param is not None:
        payload["param"] = str(param)
    urls, ids = _artifact_refs_from_exception(exc)
    if urls:
        payload["artifact_urls"] = urls
    if ids:
        payload["artifact_ids"] = ids
    return payload


def classify_response_failure(details: dict[str, Any]) -> MidStreamErrorPayload:
    """Classify a Responses API failure-details payload."""

    message = str(details.get("message") or "Responses stream failed")
    code = details.get("code") or details.get("type")
    param = details.get("param")
    lowered = f"{message} {code or ''} {param or ''} {details.get('details') or ''}".lower()
    category = MidStreamErrorCategory.OTHER
    if param == "reasoning.summary" or "reasoning.summary" in lowered:
        category = MidStreamErrorCategory.REASONING_SUMMARY_REJECTED
    elif _looks_like_artifact_fetch_error(lowered, str(param) if param is not None else None):
        category = MidStreamErrorCategory.ARTIFACT_FETCH
    elif "rate" in lowered and "limit" in lowered:
        category = MidStreamErrorCategory.RATE_LIMIT
    elif any(marker in lowered for marker in ("server_error", "internal_error", "5xx")):
        category = MidStreamErrorCategory.PROVIDER_5XX
    elif "context" in lowered and any(token in lowered for token in ("window", "length", "token")):
        category = MidStreamErrorCategory.CONTEXT_OVERFLOW

    payload: MidStreamErrorPayload = {
        "category": category.value,
        "code": str(code) if code is not None else None,
        "message": message[:500],
        "provider_event": str(details.get("event_type") or "response.failed"),
        "response_id": str(details.get("response_id")) if details.get("response_id") else None,
        "response_status": str(details.get("response_status"))
        if details.get("response_status")
        else None,
        "param": str(param) if param is not None else None,
        "details": details,
    }
    urls, ids = _artifact_refs_from_mapping(details)
    if urls:
        payload["artifact_urls"] = urls
    if ids:
        payload["artifact_ids"] = ids
    return {key: value for key, value in payload.items() if value not in (None, [], {})}


def build_mid_stream_error_chunk(exc: BaseException) -> dict[str, Any]:
    """Return a normalized provider stream error chunk."""

    payload = classify_llm_exception(exc)
    return {
        "error": payload.get("message") or str(exc) or type(exc).__name__,
        "response_error": payload,
        "mid_stream_failure": True,
    }


def reasoning_summary_rejected(payload: dict[str, Any] | None) -> bool:
    """Return True when a stream failure means reasoning.summary is unsupported."""

    if not isinstance(payload, dict):
        return False
    return payload.get("category") == MidStreamErrorCategory.REASONING_SUMMARY_REJECTED.value


def _looks_like_artifact_fetch_error(message: str, param: str | None) -> bool:
    return bool(
        param in {"url", "image_url", "file_url"}
        and any(marker in message for marker in ("download", "fetch", "timeout", "timed out"))
    ) or bool("timeout while downloading" in message and "url" in message)


def _artifact_refs_from_exception(exc: BaseException) -> tuple[list[str], list[str]]:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return _artifact_refs_from_mapping(body)
    return [], []


def _artifact_refs_from_mapping(value: dict[str, Any]) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    ids: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key).lower()
                if isinstance(item, str):
                    if key_text in {"url", "image_url", "file_url"} and item.startswith("http"):
                        urls.append(item)
                    elif key_text in {"artifact_id", "artifact"} and item.strip():
                        ids.append(item)
                else:
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return sorted(set(urls)), sorted(set(ids))
