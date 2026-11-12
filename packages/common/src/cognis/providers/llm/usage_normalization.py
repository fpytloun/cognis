"""Bounded provider usage/error projection without payload serialization."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import Any, Literal

from cognis.models.usage import (
    NormalizedUsage,
    UsageCount,
    UsageNumericExtra,
    UsageOperation,
    UsageProviderCost,
    UsageSafeError,
)

_EXTRA_UNITS = {
    "input_audio_tokens": "tokens",
    "output_audio_tokens": "tokens",
    "input_image_tokens": "tokens",
    "output_image_tokens": "tokens",
    "input_text_tokens": "tokens",
    "output_text_tokens": "tokens",
    "cache_read_image_tokens": "tokens",
    "cache_read_audio_tokens": "tokens",
    "input_audio_duration_ms": "milliseconds",
    "output_audio_duration_ms": "milliseconds",
    "input_video_duration_ms": "milliseconds",
    "output_video_duration_ms": "milliseconds",
    "input_image_count": "images",
    "output_image_count": "images",
    "input_characters": "characters",
    "output_characters": "characters",
    "billed_characters": "characters",
}


def field(value: Any, name: str, default: Any = None) -> Any:
    """Read one known field without serializing an SDK response."""
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def safe_error(exc: BaseException) -> UsageSafeError:
    """Map status/class to fixed templates, never inspect the exception message."""
    status = field(exc, "status_code")
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        status = field(field(exc, "response"), "status_code")
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    name = type(exc).__name__
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        category, code = "cancelled", "request_cancelled"
    elif isinstance(exc, TimeoutError) or name in (
        "Timeout",
        "APITimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
    ):
        category, code = "timeout", "provider_timeout"
    elif status == 429:
        category, code = "rate_limit", "provider_rate_limited"
    elif status == 401:
        category, code = "authentication", "provider_authentication_failed"
    elif status == 403:
        category, code = "permission", "provider_permission_denied"
    elif status is not None and status >= 500:
        category, code = "provider_unavailable", "provider_unavailable"
    elif status is not None and 400 <= status < 500:
        category, code = "invalid_request", "provider_request_rejected"
    elif isinstance(exc, ConnectionError) or name in (
        "APIConnectionError",
        "ConnectError",
        "ReadError",
        "RemoteProtocolError",
    ):
        category, code = "transport", "transport_failed"
    else:
        category, code = "unknown", "provider_error"
    # Even syntactically safe provider error strings can contain secrets.
    return UsageSafeError.model_validate(
        {
            "category": category,
            "summary_code": code,
            "http_status": status,
        }
    )


def meaningful(chunk: Any) -> bool:
    """Recognize generated text/reasoning/tool data, not headers or keepalives."""
    kind = field(chunk, "type")
    if kind in (
        "response.output_text.delta",
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
        "response.function_call_arguments.delta",
    ):
        return bool(field(chunk, "delta"))
    if kind == "content_block_delta":
        delta = field(chunk, "delta")
        return any(bool(field(delta, key)) for key in ("text", "thinking", "partial_json"))
    if kind == "content_block_start":
        block = field(chunk, "content_block")
        return any(bool(field(block, key)) for key in ("text", "thinking", "name"))
    if kind == "response.output_item.added":
        item = field(chunk, "item")
        return field(item, "type") == "function_call" and bool(field(item, "name"))
    for choice in field(chunk, "choices", ()) or ():
        delta = field(choice, "delta") or field(choice, "message")
        if any(bool(field(delta, key)) for key in ("content", "reasoning_content", "thinking")):
            return True
        for tool in field(delta, "tool_calls", ()) or ():
            function = field(tool, "function")
            if field(function, "name") or field(function, "arguments"):
                return True
    for block in field(chunk, "content", ()) or ():
        if isinstance(block, Mapping) and (
            field(block, "text") or field(block, "thinking") or field(block, "type") == "tool_use"
        ):
            return True
    return False


class UsageAccumulator:
    """Accumulate cumulative snapshots; absent fields never erase prior evidence."""

    def __init__(
        self,
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
        *,
        operation: UsageOperation = "native",
    ):
        self.semantic_profile = semantic_profile
        self.operation = operation
        self.values: dict[str, int] = {}
        self.reported_model: str | None = None
        self.request_id: str | None = None
        self.extras: dict[str, UsageNumericExtra] = {}
        self.provider_cost: UsageProviderCost | None = None
        self.gateway_cache_hit: bool | None = None

    def report_extra(
        self, name: str, value: Any, *, source: str = "provider", derived: bool = False
    ) -> None:
        """Retain an explicitly measured/reported unit, never a text-token proxy."""
        if name not in _EXTRA_UNITS or type(value) is not int or not 0 <= value <= 2**63 - 1:
            return
        self.extras[name] = UsageNumericExtra.model_validate(
            {
                "name": name,
                "value": value,
                "unit": _EXTRA_UNITS[name],
                "source": source,
                "status": "derived" if derived else "reported",
            }
        )

    def observe(self, response: Any) -> None:
        """Merge only allowlisted numeric usage, including native stream events."""
        nested = field(response, "response") or field(response, "message") or response
        model = field(nested, "model")
        if isinstance(model, str):
            self.reported_model = model
        request_id = field(nested, "_request_id") or field(nested, "request_id")
        if isinstance(request_id, str):
            self.request_id = request_id
        hidden = field(response, "_hidden_params")
        headers = field(hidden, "additional_headers") or field(response, "response_headers")
        request_id = field(headers, "x-request-id") or field(headers, "request-id")
        if isinstance(request_id, str):
            self.request_id = request_id
        cache_hit = field(hidden, "cache_hit")
        if type(cache_hit) is bool:
            self.gateway_cache_hit = cache_hit
        usage = field(nested, "usage") or field(response, "usage")
        if self.operation in {"video_generate", "video_extend"}:
            cost = field(usage, "cost")
            if cost is not None:
                self.provider_cost = UsageProviderCost.model_validate({"amount": cost})
        for name in _EXTRA_UNITS:
            self.report_extra(name, field(usage, name))
        for direction, detail_names in (
            ("input", ("input_tokens_details", "prompt_tokens_details")),
            ("output", ("output_tokens_details", "completion_tokens_details")),
        ):
            for details_name in detail_names:
                details = field(usage, details_name)
                for kind in ("audio", "image", "text"):
                    self.report_extra(
                        f"{direction}_{kind}_tokens", field(details, f"{kind}_tokens")
                    )
                cached = field(details, "cached_tokens_details")
                for kind in ("audio", "image"):
                    self.report_extra(f"cache_read_{kind}_tokens", field(cached, f"{kind}_tokens"))
        if self.operation == "transcribe":
            seconds = field(usage, "seconds")
            if type(seconds) in (float, int) and math.isfinite(seconds) and 0 <= seconds < 2**53:
                self.report_extra("input_audio_duration_ms", round(seconds * 1000))
        data = field(nested, "data")
        if (
            isinstance(data, list)
            and data
            and all(field(item, "url") or field(item, "b64_json") for item in data)
        ):
            self.report_extra("output_image_count", len(data), source="adapter")
            if self.operation in {"image_generate", "image_edit"}:
                self.report_extra("output_image_tokens", field(usage, "output_tokens"))
        image_count = 0
        for choice in field(nested, "choices", ()) or ():
            message = field(choice, "message") or field(choice, "delta")
            images = field(message, "images")
            if isinstance(images, list):
                image_count += len(images)
        if image_count:
            self.report_extra("output_image_count", image_count, source="adapter")
        duration = field(nested, "duration")
        if (
            isinstance(field(nested, "text"), str)
            and type(duration) in (float, int)
            and 0 <= duration < 2**53
            and math.isfinite(duration)
        ):
            self.report_extra("input_audio_duration_ms", round(duration * 1000))
        if usage is None:
            return
        for target, names in (
            ("input_tokens", ("prompt_tokens", "input_tokens")),
            ("output_tokens", ("completion_tokens", "output_tokens")),
            ("provider_total_tokens", ("total_tokens",)),
            ("cache_read_tokens", ("cache_read_input_tokens",)),
            ("cache_write_tokens", ("cache_creation_input_tokens",)),
        ):
            for name in names:
                value = field(usage, name)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    self.values[target] = value
                    break
        for outer, name, target in (
            ("prompt_tokens_details", "cached_tokens", "cache_read_tokens"),
            ("input_tokens_details", "cached_tokens", "cache_read_tokens"),
            ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
            ("output_tokens_details", "reasoning_tokens", "reasoning_tokens"),
            ("cache_creation", "ephemeral_5m_input_tokens", "cache_write_5m_tokens"),
            ("cache_creation", "ephemeral_1h_input_tokens", "cache_write_1h_tokens"),
        ):
            value = field(field(usage, outer), name)
            if type(value) is int and 0 <= value <= 2**63 - 1:
                self.values[target] = value

    def normalized(self, *, partial: bool, input_only: bool = False) -> NormalizedUsage:
        """Keep cache and reasoning semantics explicit rather than summing subsets.

        ``input_only`` marks operations without generated output (embeddings),
        whose usage is complete once input is reported.
        """
        status = "partial" if partial else "reported"
        counts = {
            key: UsageCount(value=value, status=status, source="provider")
            for key, value in self.values.items()
        }
        if input_only and "input_tokens" in counts:
            for key in ("output_tokens", "reasoning_tokens"):
                if self.values.get(key, 0) == 0:
                    counts[key] = UsageCount(status="not_applicable")
        if (
            self.semantic_profile == "openai"
            and counts
            and not {
                "cache_write_tokens",
                "cache_write_5m_tokens",
                "cache_write_1h_tokens",
            }
            & counts.keys()
        ):
            # OpenAI usage has no cache-write counter: cached_tokens is part of
            # input and writes are not metered, so fresh input stays derivable.
            counts["cache_write_tokens"] = UsageCount(status="not_applicable")
        if (
            self.semantic_profile == "openai"
            and "input_tokens" in counts
            and "cache_read_tokens" not in counts
        ):
            # No cached_tokens detail means no cache hit was reported. As in
            # LiteLLM, all input bills at the full rate (never an understatement).
            counts["cache_read_tokens"] = UsageCount(status="not_applicable")
        completeness = (
            status
            if ("input_tokens" in counts and ("output_tokens" in counts or input_only))
            or self.extras
            and self.operation
            in {
                "image_generate",
                "image_edit",
                "transcribe",
                "synthesize",
                "video_generate",
                "video_extend",
            }
            else "partial"
            if counts
            else "unknown"
        )
        return NormalizedUsage.model_validate(
            {
                "semantic_profile": self.semantic_profile,
                "semantic_version": 1,
                "completeness": completeness,
                "cache_read_semantics": "additional_to_input"
                if self.semantic_profile == "anthropic"
                else "included_in_input"
                if self.semantic_profile == "openai"
                else "unknown",
                "cache_write_semantics": "additional_to_input"
                if self.semantic_profile == "anthropic"
                else "unknown",
                "reasoning_semantics": "included_in_output"
                if self.semantic_profile in ("anthropic", "openai")
                else "unknown",
                **counts,
                "gateway_response_cache_hit": self.gateway_cache_hit,
                "provider_cost": self.provider_cost,
                "extras": tuple(
                    extra.model_copy(update={"status": "partial"}) if partial else extra
                    for extra in self.extras.values()
                ),
            }
        )
