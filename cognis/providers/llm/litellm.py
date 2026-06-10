"""LiteLLM-backed provider wrapper."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from collections import Counter as CollectionsCounter
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import litellm
from litellm.llms.chatgpt.common_utils import GetAccessTokenError
from prometheus_client import Counter, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.core.json_utils import extract_json_object, extract_text_from_response
from cognis.core.tool_exposure import LLMApiMode, ToolDiscoveryMode, ToolExposureContract
from cognis.json_stream import merge_incremental_json_fragment
from cognis.logging import get_logger
from cognis.models.config import (
    DEFAULT_MODEL_INFO,
    Cost,
    GeneratedImage,
    ImageGenerationResult,
    ModelInfo,
    ProviderHealth,
    SpeechToTextResult,
    TextToSpeechResult,
    TokenUsage,
    normalize_reasoning_level,
)
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.chatgpt_oauth import (
    CHATGPT_OAUTH_AUTH_FILE,
)
from cognis.providers.llm.chatgpt_oauth import (
    chatgpt_access_token_expires_at as _chatgpt_access_token_expires_at,
)
from cognis.providers.llm.chatgpt_oauth import (
    chatgpt_account_id_from_tokens as _chatgpt_account_id_from_tokens,
)
from cognis.providers.llm.chatgpt_oauth import (
    oauth_pending_secret_name as _oauth_pending_secret_name,
)
from cognis.providers.llm.chatgpt_oauth import (
    oauth_secret_description as _oauth_secret_description,
)
from cognis.providers.llm.chatgpt_oauth import (
    oauth_secret_owner as _oauth_secret_owner,
)
from cognis.providers.llm.chatgpt_oauth import (
    oauth_token_secret_name as _oauth_token_secret_name,
)
from cognis.providers.llm.chatgpt_oauth import (
    parse_chatgpt_authorized_record as _parse_chatgpt_authorized_record,
)
from cognis.providers.llm.codex import (  # type: ignore[import-not-found]
    CODEX_MODEL_CACHE_TTL_SECONDS,
    CodexAuth,
    bundled_codex_model_entries,
    codex_catalog_model_info,
    codex_unknown_model_info,
    fetch_codex_models,
    fetch_codex_usage,
    test_codex_responses,
)
from cognis.providers.llm.codex_transport import DirectCodexTransport
from cognis.providers.llm.errors import (
    LLMStreamProviderError,
    OpenAIToolSearchFallbackRequired,
    build_mid_stream_error_chunk,
    classify_llm_exception,
    reasoning_summary_rejected,
)
from cognis.providers.llm.reasoning import (
    PreparedReasoningConfig,
    apply_reasoning_config,
    auxiliary_reasoning_effort_for_model,
    looks_like_embedding_model,
    reasoning_efforts_for_model,
)
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    normalize_openai_model_name,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
    should_use_openai_responses,
    split_messages_for_responses,
    split_system_messages_for_responses,
)
from cognis.providers.llm.transport import LiteLLMTransport, ResponsesTransport
from cognis.store.models import LLMProvider as LLMProviderRow
from cognis.store.models import ModelRouting
from cognis.store.queries import get_model_routing

logger = get_logger(__name__)

MODEL_CACHE_TTL_SECONDS = 60.0
PROXY_MODEL_INFO_CACHE_TTL = 300.0  # 5 minutes for successful proxy /model/info fetches
PROXY_MODEL_INFO_NEGATIVE_TTL = 30.0  # 30 seconds negative cache for failures
SAFE_PROVIDER_KWARGS = {"api_base", "api_version", "base_url", "timeout"}
OAUTH_PROVIDER_PRESETS = {"chatgpt"}
CODEX_TRANSPORT_LITELLM = "litellm"
CODEX_TRANSPORT_DIRECT = "direct"
CODEX_TRANSPORT_DEFAULT = CODEX_TRANSPORT_DIRECT
CODEX_TRANSPORTS = {CODEX_TRANSPORT_LITELLM, CODEX_TRANSPORT_DIRECT}
_CACHE_MISS = object()
# Preset-to-litellm model prefix mapping.  LiteLLM uses the prefix to
# determine which provider API to use.  Standard presets (openai, anthropic)
# are recognised by litellm natively and need no prefix.
PRESET_TO_MODEL_PREFIX: dict[str, str] = {
    "litellm_proxy": "litellm_proxy",
    "openai_compatible": "openai",
    "chatgpt": "chatgpt",
}

# Preset-to-image-generation strategy mapping.
# "aimage_generation" uses litellm.aimage_generation() (OpenAI, DALL-E).
# "acompletion_modalities" uses litellm.acompletion() with modalities=["image", "text"] (Gemini).
_IMAGE_GEN_STRATEGY: dict[str, str] = {
    "openai": "aimage_generation",
    "openai_compatible": "aimage_generation",
    "litellm_proxy": "aimage_generation",
    "gemini": "acompletion_modalities",
    "vertex_ai": "acompletion_modalities",
}

# Anthropic model name patterns for prompt caching support
_ANTHROPIC_MODEL_PATTERNS = re.compile(r"(claude|anthropic)", re.IGNORECASE)
_GPT5_MODEL_PATTERN = re.compile(r"(^|/)(gpt-5(?:[.-].*)?)$", re.IGNORECASE)

LLM_REASONING_EFFORT_USED_TOTAL = Counter(
    "cognis_llm_reasoning_effort_used_total",
    "Reasoning effort values sent to providers.",
    labelnames=("family", "level"),
)
LLM_SAMPLING_PARAMS_STRIPPED_TOTAL = Counter(
    "cognis_llm_sampling_params_stripped_total",
    "Sampling parameters stripped from provider requests.",
    labelnames=("reason",),
)
LLM_MAX_TOKENS_TRANSLATED_TOTAL = Counter(
    "cognis_llm_max_tokens_translated_total",
    "Count of max_tokens to max_completion_tokens translations.",
)
LLM_MAX_TOKENS_AUTOFILLED_TOTAL = Counter(
    "cognis_llm_max_tokens_autofilled_total",
    "Count of requests where max_tokens was auto-filled from model metadata.",
    labelnames=("provider_id", "model"),
)
LLM_JSON_MODE_TRANSPORT_FLIP_TOTAL = Counter(
    "cognis_llm_json_mode_transport_flip_total",
    "JSON-mode transport fallbacks (Responses API empty / BadRequest / cached-broken).",
    labelnames=("provider_id", "model", "reason"),
)
LLM_JSON_MODE_NORMALIZATION_TOTAL = Counter(
    "cognis_llm_json_mode_normalization_total",
    "JSON-mode response normalization and fallback outcomes.",
    labelnames=("provider_id", "model", "reason"),
)
LLM_TEXT_TRANSPORT_FLIP_TOTAL = Counter(
    "cognis_llm_text_transport_flip_total",
    "Plain text transport fallbacks for empty Responses API outputs.",
    labelnames=("provider_id", "model", "reason"),
)
LLM_OPENAI_TOOL_SEARCH_FALLBACK_TOTAL = Counter(
    "cognis_llm_openai_tool_search_fallback_total",
    "Cached downgrades from native OpenAI Responses tool search to controller fallback.",
    labelnames=("provider_id", "model", "reason"),
)
LLM_CACHE_CONTROL_APPLIED_TOTAL = Counter(
    "cognis_llm_cache_control_applied_total",
    "Anthropic-style cache_control hints applied to immutable prompt prefix.",
    labelnames=("gated_by",),
)
LLM_TOKENIZER_USED_TOTAL = Counter(
    "cognis_tokenizer_used_total",
    "Tokenizer backend used for model token estimation.",
    labelnames=("provider", "backend"),
)
LLM_REQUESTS_TOTAL = Counter(
    "cognis_llm_requests_total",
    "LLM streaming requests.",
    labelnames=("provider_id", "model", "llm_api", "location", "status"),
)
LLM_REQUEST_DURATION = Histogram(
    "cognis_llm_request_duration_seconds",
    "End-to-end LLM streaming request duration.",
    labelnames=("provider_id", "model", "llm_api", "location", "status"),
)
LLM_TIME_TO_FIRST_TOKEN = Histogram(
    "cognis_llm_time_to_first_token_seconds",
    "Seconds from LLM request start to first emitted content, reasoning, or tool delta.",
    labelnames=("provider_id", "model", "llm_api", "location"),
)
LLM_TIME_TO_FIRST_RAW_CHUNK = Histogram(
    "cognis_llm_time_to_first_raw_chunk_seconds",
    "Seconds from LLM request start to first normalized provider chunk.",
    labelnames=("provider_id", "model", "llm_api", "location"),
)
LLM_TOKENS_TOTAL = Counter(
    "cognis_llm_tokens_total",
    "LLM token usage reported by providers.",
    labelnames=("provider_id", "model", "llm_api", "location", "kind"),
)
LLM_OUTPUT_TOKENS_PER_SECOND = Histogram(
    "cognis_llm_output_tokens_per_second",
    "Reported output tokens per second over completed LLM streams.",
    labelnames=("provider_id", "model", "llm_api", "location"),
)
LLM_PROVIDER_PHASE_DURATION = Histogram(
    "cognis_llm_provider_phase_duration_seconds",
    "Duration of internal LLM provider streaming phases.",
    labelnames=("provider_id", "model", "llm_api", "location", "phase"),
)
LLM_REQUEST_PAYLOAD_BYTES = Histogram(
    "cognis_llm_request_payload_bytes",
    "Serialized LLM request payload sizes.",
    labelnames=("provider_id", "model", "llm_api", "location", "component"),
)
LLM_PROMPT_CACHE_HIT_RATIO = Histogram(
    "cognis_llm_prompt_cache_hit_ratio",
    "Ratio of provider-reported cached input tokens to input tokens.",
    labelnames=("provider_id", "model", "llm_api", "location"),
    buckets=(0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0),
)
LLM_PROMPT_CACHE_KEY_REJECTED_TOTAL = Counter(
    "cognis_llm_prompt_cache_key_rejected_total",
    "Requests where the backend rejected prompt_cache_key/prompt_cache_retention params.",
    labelnames=("provider_id", "model", "reason"),
)
LLM_MAX_TOKENS_CAPPED_TOTAL = Counter(
    "cognis_llm_max_tokens_capped_total",
    "Requests where caller-supplied output token caps are below model metadata.",
    labelnames=("provider_id", "model"),
)
LLM_CAPABILITY_FALLBACK_EXPIRED_TOTAL = Counter(
    "cognis_llm_capability_fallback_expired_total",
    "Runtime capability fallback markers that expired and were retried.",
    labelnames=("marker", "provider_id", "model"),
)
LLM_REASONING_SUMMARY_REJECTED_TOTAL = Counter(
    "cognis_llm_reasoning_summary_rejected_total",
    "Responses requests where reasoning.summary was rejected and retried without it.",
    labelnames=("provider_id", "model"),
)
LLM_ANTHROPIC_DEFER_LOADING_REJECTED_TOTAL = Counter(
    "cognis_llm_anthropic_defer_loading_rejected_total",
    "Chat-completions requests where Anthropic defer_loading/tool-search beta was rejected.",
    labelnames=("provider_id", "model", "reason"),
)

_PROMPT_CACHE_KEY_VERSION = "v1"
_DEFAULT_CAPABILITY_FALLBACK_TTL_SECONDS = 3600.0


class _CapabilityMarkers(dict[tuple[str, str], float]):
    """TTL marker map with a set-like add() for older tests and helpers."""

    def add(self, key: tuple[str, str]) -> None:
        self[key] = 0.0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, set):
            return set(self.keys()) == other
        return dict.__eq__(self, other)


type CapabilityMarkers = _CapabilityMarkers


def _chunk_has_visible_activity(chunk: dict[str, Any]) -> bool:
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                for key in (
                    "content",
                    "reasoning",
                    "reasoning_content",
                    "tool_calls",
                    "function_call",
                ):
                    value = delta.get(key)
                    if value:
                        return True
            message = choice.get("message")
            if isinstance(message, dict) and any(
                message.get(key) for key in ("content", "reasoning", "tool_calls")
            ):
                return True
    return bool(chunk.get("content") or chunk.get("tool_calls") or chunk.get("function_call"))


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int | float):
            return int(value)
    return 0


def _usage_detail_int(usage: dict[str, Any], detail_key: str, value_key: str) -> int:
    details = usage.get(detail_key)
    if isinstance(details, dict):
        value = details.get(value_key)
        if isinstance(value, int | float):
            return int(value)
    return 0


def _stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")


def _payload_size_hash(value: Any) -> tuple[int, str | None]:
    try:
        payload = _stable_json_bytes(value)
    except Exception:
        return 0, None
    return len(payload), hashlib.sha256(payload).hexdigest()[:16]


def _payload_hash(value: Any) -> str | None:
    return _payload_size_hash(value)[1]


def _request_payload_diagnostics(
    messages: list[dict[str, Any]],
    request_kwargs: dict[str, Any],
    *,
    cache_breakpoint_index: int | None,
    provider_preset: str | None = None,
    responses_instructions: str | None = None,
    diagnostics_stage: str | None = None,
) -> dict[str, Any]:
    tools = request_kwargs.get("tools")
    messages_bytes, messages_hash = _payload_size_hash(messages)
    tools_bytes, tools_hash = _payload_size_hash(tools or [])
    store_value = request_kwargs.get("store")
    prompt_cache_key = request_kwargs.get("prompt_cache_key")
    prompt_cache_retention = request_kwargs.get("prompt_cache_retention")
    diag: dict[str, Any] = {
        "message_count": len(messages),
        "messages_bytes": messages_bytes,
        "messages_hash": messages_hash,
        "responses_store_mode": "true"
        if store_value is True
        else "false"
        if store_value is False
        else "omitted/default",
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "tool_schema_bytes": tools_bytes,
        "tool_schema_hash": tools_hash,
        "cache_breakpoint_index": cache_breakpoint_index,
        "prompt_cache_key_present": isinstance(prompt_cache_key, str) and bool(prompt_cache_key),
        "prompt_cache_retention_present": isinstance(prompt_cache_retention, str)
        and bool(prompt_cache_retention),
    }
    if isinstance(prompt_cache_key, str) and prompt_cache_key:
        diag["prompt_cache_key_hash"] = _payload_hash(prompt_cache_key)
    if isinstance(prompt_cache_retention, str) and prompt_cache_retention:
        diag["prompt_cache_retention"] = prompt_cache_retention
    if provider_preset is not None:
        diag["provider_preset"] = provider_preset
    if diagnostics_stage is not None:
        diag["request_diagnostics_stage"] = diagnostics_stage
    if responses_instructions is not None:
        instr_bytes, instr_hash = _payload_size_hash(responses_instructions)
        diag["instructions_bytes"] = instr_bytes
        diag["instructions_hash"] = instr_hash
    return diag


def _response_cache_observation_status(
    *,
    cached_tokens: int,
    explicit_cache_key_present: bool,
) -> str:
    if cached_tokens <= 0:
        return "miss"
    if explicit_cache_key_present:
        return "hit_with_explicit_key"
    return "hit_without_explicit_key"


def _provider_config(provider: LLMProviderRow | None) -> dict[str, Any]:
    if provider is None or not isinstance(provider.config, dict):
        return {}
    return dict(provider.config)


def _model_config_value(
    provider: LLMProviderRow | None, model_id: str, key: str
) -> tuple[bool, Any]:
    config = _provider_config(provider)
    row_models = config.get("models")
    if isinstance(row_models, list):
        for model in row_models:
            if isinstance(model, dict) and model.get("model_id") == model_id and key in model:
                return True, model.get(key)
    if key in config:
        return True, config.get(key)
    return False, None


def _normalize_text_verbosity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else None


def _truthy_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _looks_like_custom_base_url(provider: LLMProviderRow | None) -> bool:
    config = _provider_config(provider)
    raw = config.get("api_base") or config.get("base_url")
    if not isinstance(raw, str) or not raw.strip():
        return False
    lowered = raw.strip().lower().rstrip("/")
    return not lowered.startswith("https://api.anthropic.com")


def _anthropic_defer_loading_setting(provider: LLMProviderRow | None, model_id: str) -> Any:
    for key in (
        "anthropic_defer_loading",
        "supports_defer_loading",
        "enable_anthropic_defer_loading",
    ):
        found, value = _model_config_value(provider, model_id, key)
        if found:
            return value
    return "auto"


def _anthropic_defer_loading_enabled(
    provider: LLMProviderRow | None,
    model_id: str,
    model_info: ModelInfo,
    *,
    broken: bool,
) -> bool:
    if broken or not model_info.supports_defer_loading:
        return False
    setting = _anthropic_defer_loading_setting(provider, model_id)
    if isinstance(setting, bool):
        return setting
    normalized = str(setting or "auto").strip().lower()
    if normalized in {"true", "1", "yes", "on", "enabled"}:
        return True
    if normalized in {"false", "0", "no", "off", "disabled"}:
        return False
    # Custom Anthropic-compatible endpoints often implement normal Messages/Tools
    # but not Anthropic beta tool-search/defer-loading headers. Keep prompt
    # caching enabled, but require explicit opt-in for defer-loading there.
    preset = _provider_config(provider).get("preset")
    return not (preset == "anthropic" and _looks_like_custom_base_url(provider))


def _default_text_verbosity(
    *,
    provider: LLMProviderRow | None,
    resolved_model: str,
    model_info: ModelInfo,
) -> str | None:
    found, configured = _model_config_value(provider, resolved_model, "default_verbosity")
    if found:
        return _normalize_text_verbosity(configured)
    value = _normalize_text_verbosity(model_info.default_verbosity)
    if value is not None:
        return value
    normalized = normalize_openai_model_name(resolved_model)
    if model_info.supports_verbosity and ("gpt-5" in normalized or "codex" in normalized):
        return "low"
    return None


def _sanitize_error_text(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted-api-key]", text)
    text = re.sub(r"key-[A-Za-z0-9_-]+", "[redacted-api-key]", text)
    text = re.sub(r"https?://[^\s:@]+:[^\s@]+@", "https://[redacted]@", text)
    text = re.sub(r"(?i)(api[_ -]?key\s*[=:]\s*)([^\s,;]+)", r"\1[redacted]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)", r"\1[redacted]", text)
    text = re.sub(r"(?i)(access[_ -]?token\s*[=:]\s*)([^\s,;]+)", r"\1[redacted]", text)
    return text


def _responses_prompt_cache_status(
    *,
    responses_kwargs: dict[str, Any],
    provider: LLMProviderRow | None,
    resolved_model: str,
    prompt_cache_key_broken_keys: dict[tuple[str, str], float] | set[tuple[str, str]] | None,
) -> str:
    if "prompt_cache_key" in responses_kwargs:
        return "sent"
    config = (
        dict(provider.config) if provider is not None and isinstance(provider.config, dict) else {}
    )
    is_chatgpt = _looks_like_chatgpt_oauth_provider(provider)
    if config.get("use_prompt_cache_key") is False:
        return "disabled_by_provider_config"
    chatgpt_cache_key_env = os.getenv("COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED")
    if is_chatgpt and config.get("use_prompt_cache_key") is not True:
        if chatgpt_cache_key_env is None:
            return "disabled_by_default"
        if chatgpt_cache_key_env.strip().lower() not in {"true", "1", "yes", "on"}:
            return "disabled_by_env"
    provider_id = provider.provider_id if provider is not None else ""
    if (provider_id, resolved_model) in (prompt_cache_key_broken_keys or set()):
        return "disabled_by_capability_fallback"
    return "not_sent"


def _exception_detail(exc: BaseException) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for attr, key in (
        ("status_code", "status_code"),
        ("code", "error_code"),
        ("type", "provider_error_type"),
        ("param", "provider_error_param"),
    ):
        value = getattr(exc, attr, None)
        if isinstance(value, str | int):
            detail[key] = value

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        detail.setdefault("status_code", status_code)

    body = getattr(exc, "body", None)
    if body is None and response is not None:
        body = getattr(response, "text", None)
    if body is None:
        body = getattr(exc, "message", None)
    if body is None:
        body = str(exc)

    if isinstance(body, bytes):
        body_text = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        body_text = body
    else:
        try:
            body_text = json.dumps(body, ensure_ascii=False, default=str)
        except Exception:
            body_text = str(body)
    if body_text:
        detail["provider_error_body_preview"] = _sanitize_error_text(body_text)[:500]
    return detail


def _record_llm_token_metrics(
    usage: dict[str, Any],
    *,
    provider_id: str,
    model: str,
    llm_api: str,
    location: str,
) -> dict[str, int]:
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    cached_tokens = (
        _usage_int(usage, "cached_tokens")
        or _usage_int(usage, "cache_read_input_tokens")
        or _usage_detail_int(usage, "prompt_tokens_details", "cached_tokens")
        or _usage_detail_int(usage, "input_tokens_details", "cached_tokens")
        or _usage_detail_int(usage, "input_tokens_details", "cache_read_input_tokens")
    )
    cache_creation_tokens = _usage_int(usage, "cache_creation_input_tokens") or _usage_detail_int(
        usage, "input_tokens_details", "cache_creation_input_tokens"
    )
    reasoning_tokens = (
        _usage_int(usage, "reasoning_tokens")
        or _usage_detail_int(usage, "completion_tokens_details", "reasoning_tokens")
        or _usage_detail_int(usage, "output_tokens_details", "reasoning_tokens")
    )

    token_values = {
        "input": prompt_tokens,
        "output": completion_tokens,
        "total": total_tokens,
        "cached": cached_tokens,
        "cache_creation": cache_creation_tokens,
        "reasoning": reasoning_tokens,
    }
    for kind, value in token_values.items():
        if value > 0:
            LLM_TOKENS_TOTAL.labels(
                provider_id=provider_id,
                model=model,
                llm_api=llm_api,
                location=location,
                kind=kind,
            ).inc(value)
    return token_values


@asynccontextmanager
async def _observe_llm_stream_request(
    *,
    llm_request_id: str | None = None,
    provider_id: str,
    model: str,
    llm_api: str,
    location: str,
    request_diagnostics: dict[str, Any] | None = None,
) -> AsyncIterator[Callable[[dict[str, Any]], None]]:
    started_at = monotonic()
    first_token_after: float | None = None
    first_raw_chunk_after: float | None = None
    chunk_count = 0
    provider_event_counts: CollectionsCounter[str] = CollectionsCounter()
    recent_provider_event_types: deque[str] = deque(maxlen=20)
    response_completed_seen = False
    response_failed_seen = False
    meaningful_chunk_count = 0
    reasoning_chunk_count = 0
    usage: dict[str, Any] = {}
    status = "success"
    error_type: str | None = None

    def observe_chunk(chunk: dict[str, Any]) -> None:
        nonlocal chunk_count, first_raw_chunk_after, first_token_after, usage, status, error_type
        nonlocal response_completed_seen, response_failed_seen, meaningful_chunk_count
        nonlocal reasoning_chunk_count
        chunk_count += 1
        provider_event_type = chunk.get("provider_event_type")
        if isinstance(provider_event_type, str) and provider_event_type:
            provider_event_counts[provider_event_type] += 1
            recent_provider_event_types.append(provider_event_type)
            if provider_event_type in {"response.completed", "response.completed.synthetic"}:
                response_completed_seen = True
            elif provider_event_type == "response.failed":
                response_failed_seen = True
        if first_raw_chunk_after is None:
            first_raw_chunk_after = monotonic() - started_at
            LLM_TIME_TO_FIRST_RAW_CHUNK.labels(
                provider_id=provider_id,
                model=model,
                llm_api=llm_api,
                location=location,
            ).observe(first_raw_chunk_after)
        if first_token_after is None and _chunk_has_visible_activity(chunk):
            first_token_after = monotonic() - started_at
            LLM_TIME_TO_FIRST_TOKEN.labels(
                provider_id=provider_id,
                model=model,
                llm_api=llm_api,
                location=location,
            ).observe(first_token_after)
        choices = chunk.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                if delta.get("reasoning") or delta.get("reasoning_content"):
                    reasoning_chunk_count += 1
                if (
                    delta.get("content")
                    or delta.get("tool_calls")
                    or delta.get("function_call")
                    or delta.get("refusal")
                ):
                    meaningful_chunk_count += 1
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            usage = chunk_usage
        if chunk.get("mid_stream_failure") or chunk.get("error"):
            status = "error"
            error_type = str(chunk.get("error") or "stream_error")[:80]

    try:
        yield observe_chunk
    except Exception as exc:
        status = "error"
        error_type = type(exc).__name__
        raise
    finally:
        duration = monotonic() - started_at
        labels = {
            "provider_id": provider_id,
            "model": model,
            "llm_api": llm_api,
            "location": location,
            "status": status,
        }
        LLM_REQUESTS_TOTAL.labels(**labels).inc()
        LLM_REQUEST_DURATION.labels(**labels).observe(duration)
        token_values = _record_llm_token_metrics(
            usage,
            provider_id=provider_id,
            model=model,
            llm_api=llm_api,
            location=location,
        )
        output_tokens = token_values.get("output", 0)
        tokens_per_second = None
        if output_tokens > 0 and duration > 0:
            tokens_per_second = output_tokens / duration
            LLM_OUTPUT_TOKENS_PER_SECOND.labels(
                provider_id=provider_id,
                model=model,
                llm_api=llm_api,
                location=location,
            ).observe(tokens_per_second)
        input_tokens = token_values.get("input", 0)
        cached_tokens = token_values.get("cached", 0)
        cache_hit_ratio = None
        if input_tokens > 0:
            cache_hit_ratio = cached_tokens / input_tokens
            LLM_PROMPT_CACHE_HIT_RATIO.labels(
                provider_id=provider_id,
                model=model,
                llm_api=llm_api,
                location=location,
            ).observe(cache_hit_ratio)
        diagnostics = dict(request_diagnostics or {})
        if provider_event_counts:
            diagnostics["provider_event_counts"] = dict(sorted(provider_event_counts.items()))
            diagnostics["recent_provider_event_types"] = list(recent_provider_event_types)
            diagnostics["response_completed_seen"] = response_completed_seen
            diagnostics["response_failed_seen"] = response_failed_seen
            diagnostics["meaningful_chunk_count"] = meaningful_chunk_count
            diagnostics["reasoning_chunk_count"] = reasoning_chunk_count
        explicit_cache_key_present = diagnostics.get("prompt_cache_key_present") is True
        diagnostics["cache_observation_status"] = _response_cache_observation_status(
            cached_tokens=cached_tokens,
            explicit_cache_key_present=explicit_cache_key_present,
        )
        for component, key in (
            ("messages", "messages_bytes"),
            ("tools", "tool_schema_bytes"),
        ):
            value = diagnostics.get(key)
            if isinstance(value, int) and value > 0:
                LLM_REQUEST_PAYLOAD_BYTES.labels(
                    provider_id=provider_id,
                    model=model,
                    llm_api=llm_api,
                    location=location,
                    component=component,
                ).observe(value)
        logger.info(
            "LLM stream request completed",
            extra={
                "extra_data": {
                    "llm_request_id": llm_request_id,
                    "provider_id": provider_id,
                    "model": model,
                    "llm_api": llm_api,
                    "location": location,
                    "status": status,
                    "error_type": error_type,
                    "duration_seconds": round(duration, 3),
                    "time_to_first_raw_chunk_seconds": round(first_raw_chunk_after, 3)
                    if first_raw_chunk_after is not None
                    else None,
                    "time_to_first_token_seconds": round(first_token_after, 3)
                    if first_token_after is not None
                    else None,
                    "tokens_per_second": round(tokens_per_second, 3)
                    if tokens_per_second is not None
                    else None,
                    "chunk_count": chunk_count,
                    "input_tokens": token_values.get("input", 0),
                    "output_tokens": token_values.get("output", 0),
                    "total_tokens": token_values.get("total", 0),
                    "cached_tokens": token_values.get("cached", 0),
                    "cache_hit_ratio": round(cache_hit_ratio, 3)
                    if cache_hit_ratio is not None
                    else None,
                    "reasoning_tokens": token_values.get("reasoning", 0),
                    **diagnostics,
                }
            },
        )


def _observe_provider_phase(
    *,
    llm_request_id: str | None = None,
    provider_id: str,
    model: str,
    llm_api: str,
    location: str,
    phase: str,
    duration: float,
    extra_data: dict[str, Any] | None = None,
) -> None:
    LLM_PROVIDER_PHASE_DURATION.labels(
        provider_id=provider_id,
        model=model,
        llm_api=llm_api,
        location=location,
        phase=phase,
    ).observe(duration)
    payload = {
        "llm_request_id": llm_request_id,
        "provider_id": provider_id,
        "model": model,
        "llm_api": llm_api,
        "location": location,
        "phase": phase,
        "duration_seconds": round(duration, 3),
    }
    if extra_data:
        payload.update(extra_data)
    logger.info("LLM provider phase completed", extra={"extra_data": payload})


def _register_litellm_chatgpt_model_info(
    *,
    resolved_model: str,
    prefixed_model: str,
    model_info: ModelInfo,
) -> None:
    """Teach LiteLLM about Cognis-discovered ChatGPT/Codex model capabilities."""

    existing = litellm.model_cost.get(prefixed_model)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry.update(
        {
            "litellm_provider": "chatgpt",
            "mode": "responses",
            "supported_endpoints": ["/v1/responses"],
            "max_input_tokens": model_info.max_input_tokens or model_info.context_window,
            "max_output_tokens": model_info.max_output_tokens,
            "max_tokens": model_info.max_output_tokens,
            "supports_native_streaming": True,
            "supports_function_calling": bool(model_info.supports_tools),
            "supports_parallel_function_calling": bool(model_info.supports_tools),
            "supports_response_schema": "response_format" in model_info.supported_openai_params,
            "supports_vision": bool(model_info.supports_vision),
            "supports_reasoning": bool(model_info.supports_reasoning),
            "supports_prompt_caching": bool(model_info.supports_prompt_caching),
        }
    )
    litellm.model_cost[prefixed_model] = entry

    if prefixed_model != resolved_model and resolved_model not in litellm.model_cost:
        litellm.model_cost[resolved_model] = dict(entry, litellm_provider="openai")


_GEMINI_MODEL_PATTERNS = re.compile(r"(gemini|vertex_ai|google)", re.IGNORECASE)

# Auto-fill "auto" max_tokens: when no explicit output cap is requested, we set
# max_tokens = model_info.max_output_tokens so providers that require it
# (Anthropic) or truncate with strict JSON validators (Groq) see a sensible
# ceiling. Matches the pattern used by Claude Code, Codex, aider, etc.
JSON_MODE_AUTOFILL_FALLBACK_MAX_TOKENS = 16384
_RESPONSES_JSON_INPUT_MARKER: dict[str, Any] = {"role": "system", "content": "Return JSON."}

# Substring matches (case-insensitive) that indicate the provider's server-side
# JSON validator rejected the generation because the model produced invalid or
# truncated JSON. We treat these as a signal that response_format doesn't work
# reliably on this (provider, model) and fall back to plain chat-completions
# without response_format for the same call.
_JSON_VALIDATOR_BAD_REQUEST_SIGNATURES: tuple[str, ...] = (
    "json_validate_failed",
    "failed to generate json",
    "invalid json response",
    "response is not valid json",
    "json_validator_failed",
)


class JSONModeGenerationError(RuntimeError):
    """Raised when a JSON-mode generation cannot produce valid JSON."""

    def __init__(self, *, provider_id: str, model_id: str, reason: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.reason = reason
        super().__init__(
            "LLM JSON-mode generation failed for "
            f"provider={provider_id!r}, model={model_id!r}; reason={reason}"
        )


def _raise_context_overflow_if_detected(
    exc: BaseException,
    *,
    provider: LLMProviderRow | None,
    resolved_model: str,
) -> None:
    from cognis.providers.llm.retry import LLMContextOverflowError, context_overflow_reason

    reason = context_overflow_reason(exc)
    if reason is None:
        return
    raise LLMContextOverflowError(
        provider_id=provider.provider_id if provider is not None else None,
        model_id=resolved_model,
        reason=reason,
        original_message=str(exc),
    ) from exc


def _is_json_validator_bad_request(exc: BaseException) -> bool:
    """Return True for BadRequestError messages that look like a JSON validator rejection.

    We match by class name (avoids hard import on litellm error types that may
    move across versions) and by case-insensitive substring against a strict
    allow-list. Any other BadRequestError propagates unchanged.
    """

    if type(exc).__name__ != "BadRequestError":
        return False
    message = str(exc).lower()
    return any(sig in message for sig in _JSON_VALIDATOR_BAD_REQUEST_SIGNATURES)


def _responses_request_wants_json_object(request_kwargs: dict[str, Any]) -> bool:
    text = request_kwargs.get("text")
    if not isinstance(text, dict):
        return False
    text_format = text.get("format")
    return isinstance(text_format, dict) and text_format.get("type") == "json_object"


def _contains_json_word(value: Any) -> bool:
    if isinstance(value, str):
        return "json" in value.lower()
    if isinstance(value, list):
        return any(_contains_json_word(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_json_word(item) for item in value.values())
    return False


def _ensure_responses_json_input_marker(input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure Responses JSON mode satisfies providers that validate input text only.

    Some Responses backends reject ``text.format={"type":"json_object"}`` unless
    the word "json" appears in the input messages. Top-level ``instructions``
    are not always considered by that validator, so direct Codex calls that move
    leading system prompts into ``instructions`` need a tiny input marker.
    """

    if _contains_json_word(input_items):
        return input_items
    return [dict(_RESPONSES_JSON_INPUT_MARKER), *input_items]


def _openai_tool_search_bad_request_reason(
    exc: BaseException, request_kwargs: dict[str, Any]
) -> str | None:
    """Return a stable reason code when native OpenAI tool search is rejected."""

    if type(exc).__name__ != "BadRequestError":
        return None
    tool_choice = request_kwargs.get("tool_choice")
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "allowed_tools":
        return None
    message = str(exc).lower()
    if "unknown parameter" in message and "tool_choice.tools" in message:
        return "tool_choice_tools_unknown"
    if "unknown parameter" in message and "tool_choice.mode" in message:
        return "tool_choice_mode_unknown"
    if "unknown parameter" in message and "tool_choice" in message:
        return "tool_choice_unknown"
    if "allowed_tools" in message and any(token in message for token in ("unsupported", "invalid")):
        return "allowed_tools_unsupported"
    if "tool_search" in message and any(token in message for token in ("unsupported", "invalid")):
        return "tool_search_unsupported"
    return None


def _is_prompt_cache_key_rejected(exc: BaseException) -> bool:
    """Return True when the backend rejected prompt_cache_key or prompt_cache_retention.

    Matches "Unknown parameter: 'prompt_cache_key'" and similar messages from
    OpenAI-compatible backends that do not support explicit cache params.
    """

    message = str(exc).lower()
    if "unknown parameter" not in message and "unsupported parameter" not in message:
        return False
    return "prompt_cache_key" in message or "prompt_cache_retention" in message


def _anthropic_defer_loading_rejection_reason(
    exc: BaseException, request_kwargs: dict[str, Any]
) -> str | None:
    """Return a stable reason code when Anthropic defer_loading beta is rejected."""

    headers = request_kwargs.get("extra_headers")
    has_beta = isinstance(headers, dict) and "anthropic-beta" in {
        str(key).lower() for key in headers
    }
    tools = request_kwargs.get("tools")
    has_defer_loading = False
    if isinstance(tools, list):
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and function.get("defer_loading") is True:
                has_defer_loading = True
                break
    if not has_beta and not has_defer_loading:
        return None
    message = str(exc).lower()
    if "defer_loading" in message:
        return "defer_loading_rejected"
    if "tool-search-tool" in message or (
        "anthropic-beta" in message
        and any(token in message for token in ("unknown", "unsupported", "invalid"))
    ):
        return "anthropic_beta_rejected"
    return None


def _is_empty_json_mode_response(response_dict: dict[str, Any]) -> bool:
    """Return True when a JSON-mode response has no usable content.

    Used to detect the "Responses API returned empty output" failure mode that
    some providers (notably OpenAI Responses via litellm_proxy / codex) exhibit
    for structured JSON calls. Refusals, tool calls, length-capped, and
    content-filtered outputs are treated as non-empty because they are valid
    terminal outcomes that we must not silently retry.
    """

    from cognis.core.json_utils import extract_text_from_response

    choices = response_dict.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    first = choices[0]
    if not isinstance(first, dict):
        return True
    finish_reason = str(first.get("finish_reason") or "stop")
    if finish_reason in {"length", "content_filter"}:
        return False
    message = first.get("message")
    if not isinstance(message, dict):
        return True
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return False
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return False
    return not extract_text_from_response(response_dict).strip()


def _normalized_json_mode_response(
    response_dict: dict[str, Any], *, label: str
) -> tuple[dict[str, Any] | None, str]:
    """Return a response with canonical JSON content, or a stable failure reason."""

    choices = response_dict.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "empty_choices"
    first = choices[0]
    if not isinstance(first, dict):
        return None, "invalid_choice"
    finish_reason = str(first.get("finish_reason") or "stop")
    if finish_reason == "length":
        return None, "length"
    message = first.get("message")
    if not isinstance(message, dict):
        return None, "invalid_message"
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return None, "refusal"
    content = extract_text_from_response(response_dict)
    if not content.strip():
        return None, "empty_response"
    try:
        payload = extract_json_object(content, label=label)
    except ValueError:
        return None, "invalid_json"
    normalized = dict(response_dict)
    normalized_choices = list(choices)
    normalized_first = dict(first)
    normalized_message = dict(message)
    normalized_message["content"] = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    normalized_first["message"] = normalized_message
    normalized_choices[0] = normalized_first
    normalized["choices"] = normalized_choices
    return normalized, "normalized"


def _supports_image_response_format(model: str) -> bool:
    normalized = model.rsplit("/", 1)[-1].lower()
    return not normalized.startswith("gpt-image-")


def _iter_gemini_image_content_parts(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str) and content.startswith("data:image/"):
        return [content]
    return []


def _generated_image_from_content_part(part: Any) -> GeneratedImage | None:
    if isinstance(part, str):
        return _generated_image_from_url(part)
    if not isinstance(part, dict):
        return None

    image_url = part.get("image_url")
    if isinstance(image_url, str):
        return _generated_image_from_url(image_url)
    if isinstance(image_url, dict):
        url = image_url.get("url")
        if isinstance(url, str):
            return _generated_image_from_url(url)

    inline_data = part.get("inline_data") or part.get("inlineData")
    if isinstance(inline_data, dict):
        data = inline_data.get("data")
        if isinstance(data, str) and data:
            mime_type = inline_data.get("mime_type") or inline_data.get("mimeType")
            return GeneratedImage(
                b64_json=data,
                content_type=str(mime_type) if isinstance(mime_type, str) else "image/png",
            )

    if part.get("type") in {"image", "image_url"}:
        data = part.get("data") or part.get("b64_json") or part.get("base64")
        if isinstance(data, str) and data:
            content_type = part.get("mime_type") or part.get("mimeType") or "image/png"
            return GeneratedImage(b64_json=data, content_type=str(content_type))
        url = part.get("url")
        if isinstance(url, str):
            return _generated_image_from_url(url)

    return None


def _generated_image_from_url(url: str) -> GeneratedImage | None:
    if not url:
        return None
    if url.startswith("data:"):
        header, separator, data = url.partition(",")
        if separator and data:
            content_type = "image/png"
            if header.startswith("data:") and ";" in header:
                content_type = header.split(":", 1)[1].split(";", 1)[0] or content_type
            return GeneratedImage(b64_json=data, content_type=content_type)
    return GeneratedImage(url=url, content_type="image/png")


def _metadata_floor_for_model(model_id: str) -> dict[str, int] | None:
    """Return conservative fallback metadata floors for known model families."""

    normalized = normalize_openai_model_name(model_id).rsplit("/", 1)[-1].lower()
    if re.match(r"^gpt-5\.5(?:-pro)?$", normalized) or re.match(r"^gpt-5\.4(?:-pro)?$", normalized):
        return {
            "context_window": 1_050_000,
            "max_context_window": 1_050_000,
            "max_input_tokens": 922_000,
            "max_output_tokens": 128_000,
        }
    if normalized == "gpt-5.3-codex-spark":
        return {
            "context_window": 128_000,
            "max_context_window": 128_000,
            "max_input_tokens": 100_000,
            "max_output_tokens": 32_000,
        }
    if (
        re.match(r"^gpt-5\.4-(?:mini|nano)$", normalized)
        or re.match(r"^gpt-5\.3-codex$", normalized)
        or re.match(r"^gpt-5\.2(?:$|-pro|-codex$)", normalized)
    ):
        return {
            "context_window": 400_000,
            "max_context_window": 400_000,
            "max_input_tokens": 272_000,
            "max_output_tokens": 128_000,
        }
    return None


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _apply_message_cache_hints(
    messages: list[dict[str, Any]],
    model: str,
    model_info: ModelInfo,
    cache_breakpoint_index: int | None,
) -> list[dict[str, Any]]:
    """Apply provider-specific prompt cache hints to the immutable prefix.

    For Anthropic models, adds ``cache_control`` breakpoint to the last
    message in the immutable prefix so that everything up to (and including)
    that message is cached across requests.

    For other providers (OpenAI uses automatic prefix caching), the messages
    are returned unchanged.
    """

    if cache_breakpoint_index is None or cache_breakpoint_index < 0:
        return messages
    if not model_info.supports_prompt_caching:
        return messages
    if cache_breakpoint_index >= len(messages):
        return messages

    # Deep-copy only the breakpoint message to avoid mutating the original
    result = list(messages)
    breakpoint_msg = dict(result[cache_breakpoint_index])

    # LiteLLM passes cache_control through to the Anthropic API
    content = breakpoint_msg.get("content")
    if isinstance(content, str):
        breakpoint_msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
    elif isinstance(content, list):
        # Content is already a list of blocks — add cache_control to the last one
        content = [dict(block) if isinstance(block, dict) else block for block in content]
        if content:
            last_block = dict(content[-1]) if isinstance(content[-1], dict) else content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                content[-1] = last_block
        breakpoint_msg["content"] = content

    result[cache_breakpoint_index] = breakpoint_msg
    LLM_CACHE_CONTROL_APPLIED_TOTAL.labels(gated_by="capability_flag").inc()
    return result


def _merge_request_kwargs(
    base_kwargs: dict[str, Any], override_kwargs: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(base_kwargs)
    for key, value in override_kwargs.items():
        if key == "extra_headers":
            base_headers = merged.get("extra_headers") or {}
            if isinstance(base_headers, dict) and isinstance(value, dict):
                merged["extra_headers"] = {**base_headers, **value}
                continue
        merged[key] = value
    return merged


def _apply_chatgpt_affinity_headers(
    request_kwargs: dict[str, Any],
    *,
    provider: LLMProviderRow | None,
    session_id: str | None,
) -> dict[str, Any]:
    """Attach Codex/ChatGPT session affinity headers for automatic cache routing."""

    if not _looks_like_chatgpt_oauth_provider(provider):
        return request_kwargs
    if not isinstance(session_id, str) or not session_id.strip():
        return request_kwargs
    result = dict(request_kwargs)
    headers = result.get("extra_headers")
    merged_headers = dict(headers) if isinstance(headers, dict) else {}
    value = session_id.strip()
    merged_headers["x-session-affinity"] = value
    merged_headers["session_id"] = value
    result["extra_headers"] = merged_headers
    return result


def _json_safe_model_value(value: Any) -> Any:
    import warnings

    if hasattr(value, "model_dump"):
        with warnings.catch_warnings():
            # LiteLLM's model_construct() can leave nested fields (e.g.
            # ``usage``) as raw dicts instead of Pydantic model instances,
            # triggering a harmless serialisation warning.  Suppress it.
            warnings.filterwarnings("ignore", message=".*Pydantic serializer.*")
            try:
                dumped = value.model_dump(mode="json", warnings=False)
            except TypeError:
                try:
                    dumped = value.model_dump(mode="json")
                except TypeError:
                    dumped = value.model_dump()
        return _json_safe_model_value(dumped)
    if isinstance(value, dict):
        return {str(key): _json_safe_model_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe_model_value(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _model_dump(value: Any) -> dict[str, Any]:
    dumped = _json_safe_model_value(value)
    if isinstance(dumped, dict):
        return dumped
    return {}


async def _responses_stream_to_chat_response(stream: AsyncIterator[Any]) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_content_parts: list[str] = []
    reasoning_summary_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish_reason = "stop"
    response_status = "completed"

    async for chunk in responses_stream_to_chat_chunks(stream):
        if chunk.get("mid_stream_failure") or chunk.get("error"):
            payload = chunk.get("response_error")
            error_message = str(chunk.get("error") or "Responses stream failed")
            raise LLMStreamProviderError(
                error_message,
                payload=payload if isinstance(payload, dict) else None,
            )
        if isinstance(chunk.get("usage"), dict):
            usage = dict(chunk["usage"])
        if isinstance(chunk.get("response_status"), str):
            response_status = str(chunk["response_status"])
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
            finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            content_parts.append(content)
        reasoning_content = delta.get("reasoning_content")
        if isinstance(reasoning_content, str):
            reasoning_content_parts.append(reasoning_content)
        reasoning_summary = delta.get("reasoning")
        if isinstance(reasoning_summary, str):
            reasoning_summary_parts.append(reasoning_summary)
        for tool_delta in delta.get("tool_calls") or []:
            if not isinstance(tool_delta, dict):
                continue
            index = int(tool_delta.get("index") or 0)
            entry = tool_calls.setdefault(
                index,
                {
                    "id": str(tool_delta.get("id") or f"call_{index}"),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tool_delta.get("id"):
                entry["id"] = str(tool_delta["id"])
            function_delta = tool_delta.get("function")
            if not isinstance(function_delta, dict):
                continue
            function = entry["function"]
            if function_delta.get("name"):
                function["name"] = str(function_delta["name"])
            if function_delta.get("arguments"):
                merged = merge_incremental_json_fragment(
                    str(function["arguments"]),
                    str(function_delta["arguments"]),
                )
                function["arguments"] = merged.merged

    content = "".join(content_parts)
    reasoning_content = "".join(reasoning_content_parts)
    reasoning_summary = "".join(reasoning_summary_parts)
    normalized_tool_calls = [
        tool_call
        for _index, tool_call in sorted(tool_calls.items())
        if (tool_call.get("function") or {}).get("name")
    ]
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": normalized_tool_calls or None,
                    "reasoning_content": reasoning_content or None,
                    "reasoning": reasoning_summary or None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
        "response_status": response_status,
    }


async def _call_responses_generate(
    transport: Any,
    *,
    stream: bool,
    max_retries: int,
    operation: str,
    **kwargs: Any,
) -> dict[str, Any]:
    from cognis.providers.llm.retry import with_llm_retry

    response = await with_llm_retry(
        transport.responses,
        stream=stream,
        max_retries=max_retries,
        operation=operation,
        **kwargs,
    )
    if stream:
        if isinstance(response, dict):
            return responses_to_chat_response(response)
        return await _responses_stream_to_chat_response(cast(AsyncIterator[Any], response))
    return cast(dict[str, Any], response)


def _provider_preset(provider: LLMProviderRow | None) -> str:
    if provider is None:
        return ""
    return str(dict(provider.config or {}).get("preset", "") or "").strip().lower()


def _looks_like_chatgpt_oauth_provider(provider: LLMProviderRow | None) -> bool:
    return _provider_preset(provider) in OAUTH_PROVIDER_PRESETS


def _codex_transport(provider: LLMProviderRow | None) -> str:
    if not _looks_like_chatgpt_oauth_provider(provider):
        return CODEX_TRANSPORT_LITELLM
    value = (
        str(_provider_config(provider).get("codex_transport") or CODEX_TRANSPORT_DEFAULT)
        .strip()
        .lower()
    )
    if value in CODEX_TRANSPORTS:
        return value
    return CODEX_TRANSPORT_DEFAULT


def _uses_direct_codex_transport(provider: LLMProviderRow | None) -> bool:
    return _codex_transport(provider) == CODEX_TRANSPORT_DIRECT


def _executor_backend_for_provider(provider: Any, config: dict[str, Any] | None) -> str:
    """Resolve the executor-local inference backend for a provider."""

    if isinstance(config, dict):
        configured = config.get("executor_backend") or config.get("cognis_inference_backend")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    return "litellm"


def _provider_visible_to_user(provider: Any, acting_user_email: str | None) -> bool:
    owner_email = getattr(provider, "owner_email", SYSTEM_USER_EMAIL) or SYSTEM_USER_EMAIL
    if owner_email == SYSTEM_USER_EMAIL:
        return True
    return acting_user_email == owner_email


def _owner_scope_cache_key(acting_user_email: str | None) -> str:
    return acting_user_email or SYSTEM_USER_EMAIL


class _ScopedEnv:
    """Temporarily set process env vars around a synchronous LiteLLM call."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self._previous: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._values.items():
            self._previous[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *_exc: object) -> None:
        for key, previous in self._previous.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _disable_litellm_chatgpt_device_login() -> Any:
    """Patch LiteLLM's ChatGPT authenticator so it never starts device login."""

    from litellm.llms.chatgpt.authenticator import Authenticator

    original = Authenticator._login_device_code

    def _non_interactive_login(self: Any) -> Any:  # noqa: ANN401
        raise GetAccessTokenError(
            message="ChatGPT OAuth is not authorized; complete Cognis provider OAuth first",
            status_code=401,
        )

    Authenticator._login_device_code = _non_interactive_login
    return original


def _restore_litellm_chatgpt_device_login(original: Any) -> None:
    from litellm.llms.chatgpt.authenticator import Authenticator

    Authenticator._login_device_code = original


def _responses_prompt_cache_key(
    *, provider: LLMProviderRow | None, resolved_model: str, instructions: str
) -> str:
    provider_id = provider.provider_id if provider is not None else "default"
    material = (
        f"{_PROMPT_CACHE_KEY_VERSION}\0{provider_id}\0{resolved_model}\0{instructions}".encode()
    )
    return f"cognis-{hashlib.sha256(material).hexdigest()[:48]}"


def _apply_responses_request_defaults(
    responses_kwargs: dict[str, Any],
    *,
    provider: LLMProviderRow | None,
    resolved_model: str,
    instructions: str | None,
    prompt_cache_key_broken_keys: dict[tuple[str, str], float] | set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Apply Cognis defaults for OpenAI Responses requests.

    Cognis owns durable history in Intaris, so Responses storage is disabled by
    default. Prompt cache affinity is keyed by the immutable instructions prefix
    unless the caller or provider explicitly supplies a key.

    For ChatGPT/Codex OAuth providers the ``store`` field is intentionally
    omitted — the LiteLLM ChatGPT transform forces ``store=False`` regardless,
    and sending it explicitly causes a redundant override. Explicit cache key
    params are also omitted by default because ChatGPT/Codex rejects them today;
    stream calls use session-affinity headers for automatic cache routing.

    ``prompt_cache_key_broken_keys`` is the provider-level capability-fallback
    set.  When ``(provider_id, resolved_model)`` is present in that set the
    cache params are omitted for this request.
    """

    result = dict(responses_kwargs)
    config = (
        dict(provider.config) if provider is not None and isinstance(provider.config, dict) else {}
    )
    is_chatgpt = _looks_like_chatgpt_oauth_provider(provider)

    # Direct Codex requires store=false. LiteLLM's ChatGPT transform forces
    # store=false, but the direct transport bypasses that transform.
    if _uses_direct_codex_transport(provider):
        result["store"] = False
        if not isinstance(result.get("instructions"), str) or not result["instructions"].strip():
            if isinstance(instructions, str) and instructions.strip():
                result["instructions"] = instructions
            else:
                result["instructions"] = (
                    "You are a helpful assistant. Follow the user's instructions precisely."
                )
    elif not is_chatgpt and "store" not in result:
        configured_store = config.get("responses_store")
        result["store"] = configured_store if isinstance(configured_store, bool) else False

    # Prompt cache params: apply for all providers unless the capability
    # fallback has marked this (provider, model) pair as unsupported. ChatGPT
    # Codex rejects explicit prompt_cache_key/prompt_cache_retention today, so
    # it uses automatic cache routing via session-affinity headers by default.
    chatgpt_cache_key_env = os.getenv("COGNIS_CHATGPT_PROMPT_CACHE_KEY_ENABLED")
    chatgpt_cache_key_enabled = (
        chatgpt_cache_key_env.strip().lower() in {"true", "1", "yes", "on"}
        if isinstance(chatgpt_cache_key_env, str)
        else False
    )
    provider_id = provider.provider_id if provider is not None else ""
    broken = prompt_cache_key_broken_keys or set()
    provider_cache_key_setting = config.get("use_prompt_cache_key")
    provider_cache_key_enabled = provider_cache_key_setting is not False
    if is_chatgpt:
        provider_cache_key_enabled = provider_cache_key_setting is True or (
            provider_cache_key_enabled and chatgpt_cache_key_enabled
        )
    cache_key_supported = provider_cache_key_enabled and (provider_id, resolved_model) not in broken

    if not cache_key_supported:
        result.pop("prompt_cache_key", None)
        result.pop("prompt_cache_retention", None)
        return result

    if "prompt_cache_key" not in result:
        configured_key = config.get("prompt_cache_key")
        if isinstance(configured_key, str) and configured_key.strip():
            result["prompt_cache_key"] = configured_key.strip()
        elif isinstance(instructions, str):
            stripped_instructions = instructions.strip()
            if stripped_instructions:
                result["prompt_cache_key"] = _responses_prompt_cache_key(
                    provider=provider,
                    resolved_model=resolved_model,
                    instructions=stripped_instructions,
                )

    if "prompt_cache_retention" not in result and "prompt_cache_key" in result:
        retention = config.get("prompt_cache_retention")
        if not (isinstance(retention, str) and retention.strip()) and is_chatgpt:
            retention = os.getenv("COGNIS_CHATGPT_PROMPT_CACHE_RETENTION", "1h")
        if isinstance(retention, str) and retention.strip():
            result["prompt_cache_retention"] = retention.strip()

    return result


def _without_reasoning_summary(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return Responses kwargs with reasoning.summary removed."""

    retry_kwargs = dict(kwargs)
    reasoning = retry_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        updated = dict(reasoning)
        updated.pop("summary", None)
        if updated:
            retry_kwargs["reasoning"] = updated
        else:
            retry_kwargs.pop("reasoning", None)
    return retry_kwargs


def _looks_like_image_generation_model(model_name: str) -> bool:
    normalized = model_name.strip().lower().replace("_", "-")
    return any(
        token in normalized
        for token in (
            "gpt-image",
            "dall-e",
            "image-generation",
            "imagen",
        )
    )


def _supports_openai_tool_search_model(model_name: str) -> bool:
    """Return whether a normalized OpenAI-family model supports Responses tool search."""

    normalized = model_name.strip().lower()
    if normalized.startswith(("gpt-5-mini", "gpt-5-nano")):
        return False
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:$|[-._])", normalized)
    if match is None:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) >= (5, 4)


def _looks_like_openai_apply_patch_model(model_name: str) -> bool:
    """Conservative fallback for OpenAI Responses native apply_patch support."""

    normalized = model_name.strip().lower()
    if "gpt-oss" in normalized:
        return False
    return "codex" in normalized or normalized.startswith(
        ("gpt-5.1", "openai/gpt-5.1", "gpt-5.5", "openai/gpt-5.5")
    )


def _normalize_proxy_model_info(info: dict[str, Any]) -> dict[str, Any]:
    """Convert litellm proxy ``model_info`` fields to Cognis ``ModelInfo`` fields.

    The litellm proxy ``/model/info`` endpoint returns a dict per model with
    keys like ``max_input_tokens``, ``supports_function_calling``, etc.  This
    helper maps them to the field names used by :class:`ModelInfo`.
    """
    normalized: dict[str, Any] = {}
    # Context / output limits
    max_context_window = (
        info.get("max_context_window_tokens")
        or info.get("max_context_window")
        or info.get("context_window")
    )
    if max_context_window:
        normalized["context_window"] = int(max_context_window)
        normalized["max_context_window"] = int(max_context_window)
    if info.get("max_input_tokens"):
        normalized["max_input_tokens"] = int(info["max_input_tokens"])
        normalized.setdefault("context_window", int(info["max_input_tokens"]))
    elif info.get("max_tokens"):
        normalized["context_window"] = int(info["max_tokens"])
    if info.get("max_output_tokens"):
        normalized["max_output_tokens"] = int(info["max_output_tokens"])
    # Capability flags
    if "supports_function_calling" in info:
        normalized["supports_tools"] = bool(info["supports_function_calling"])
    if "supports_vision" in info:
        normalized["supports_vision"] = bool(info["supports_vision"])
    if "supports_audio_input" in info:
        normalized["supports_audio_input"] = bool(info["supports_audio_input"])
    if "supports_image_generation" in info:
        normalized["supports_image_generation"] = bool(info["supports_image_generation"])
    if isinstance(info.get("supported_audio_mime_types"), list):
        normalized["supported_audio_mime_types"] = [
            str(item) for item in info["supported_audio_mime_types"] if str(item).strip()
        ]
    if "supports_pdf_input" in info:
        normalized["supports_pdf_input"] = bool(info["supports_pdf_input"])
    if "supports_embedding" in info:
        normalized["supports_embedding"] = bool(info["supports_embedding"])
    if "supports_reasoning" in info:
        normalized["supports_reasoning"] = bool(info["supports_reasoning"])
    if "supports_extended_thinking" in info:
        normalized["supports_extended_thinking"] = bool(info["supports_extended_thinking"])
    if "supports_verbosity" in info:
        normalized["supports_verbosity"] = bool(info["supports_verbosity"])
    elif "support_verbosity" in info:
        normalized["supports_verbosity"] = bool(info["support_verbosity"])
    if "default_verbosity" in info and info["default_verbosity"] is not None:
        normalized["default_verbosity"] = str(info["default_verbosity"])
    if "supports_prompt_caching" in info:
        normalized["supports_prompt_caching"] = bool(info["supports_prompt_caching"])
    if "supports_openai_namespace_tools" in info:
        normalized["supports_openai_namespace_tools"] = bool(
            info["supports_openai_namespace_tools"]
        )
    if "supports_openai_apply_patch" in info:
        normalized["supports_openai_apply_patch"] = bool(info["supports_openai_apply_patch"])
    if "openai_apply_patch_tool_type" in info and info["openai_apply_patch_tool_type"] is not None:
        normalized["openai_apply_patch_tool_type"] = str(info["openai_apply_patch_tool_type"])
    if "supports_tool_choice" in info and info.get("supports_function_calling"):
        normalized["supports_tools"] = True
    # Cost conversion: per-token → per-million-tokens (rounded to avoid float drift)
    if "input_cost_per_token" in info and info["input_cost_per_token"] is not None:
        normalized["input_cost_per_mtok"] = round(
            float(info["input_cost_per_token"]) * 1_000_000, 6
        )
    if "output_cost_per_token" in info and info["output_cost_per_token"] is not None:
        normalized["output_cost_per_mtok"] = round(
            float(info["output_cost_per_token"]) * 1_000_000, 6
        )
    return normalized


def _looks_like_extended_thinking_model(model_id: str, preset: str) -> bool:
    normalized = normalize_openai_model_name(model_id)
    if preset != "anthropic" and not _ANTHROPIC_MODEL_PATTERNS.search(normalized):
        return False
    return any(
        token in normalized
        for token in (
            "claude-3-7",
            "sonnet-4",
            "sonnet-4.5",
            "sonnet-4-5",
            "opus-4",
            "opus-4.5",
            "opus-4-5",
        )
    )


def _merge_live_bool(
    live: dict[str, Any], merged: dict[str, Any], key: str, *, fallback: bool = False
) -> bool:
    if key in live and live.get(key) is not None:
        return bool(live.get(key))
    return bool(merged.get(key, fallback) or fallback)


class LiteLLMProvider:
    """Load provider/model config from DB and route through LiteLLM."""

    def __init__(
        self,
        session_factory: async_sessionmaker[Any],
        secrets_provider: Any | None = None,
        inference_router: Any | None = None,
        credentials_provider: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._secrets = secrets_provider
        self._inference_router = inference_router
        self._credentials = credentials_provider
        self._litellm_transport = LiteLLMTransport()
        self._cache_lock = asyncio.Lock()
        self._oauth_env_lock = asyncio.Lock()
        self._oauth_locks_lock = asyncio.Lock()
        self._oauth_locks: dict[str, asyncio.Lock] = {}
        self._resolved_model_cache: dict[str, tuple[tuple[str, str | None], float]] = {}
        self._model_info_cache: dict[str, tuple[ModelInfo, float]] = {}
        self._model_provider_cache: dict[str, tuple[str | None, float]] = {}
        self._proxy_model_info_cache: dict[str, tuple[dict[str, dict[str, Any]], float]] = {}
        self._codex_model_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self._tokenizer_backend_cache: dict[str, tuple[str, str]] = {}
        # In-process cache of (provider_id, resolved_model) pairs for which
        # response_format-style structured JSON has been observed to break
        # (either via empty Responses API reply or a JSON-validator
        # BadRequestError). Only consulted when response_format is present in
        # the outgoing kwargs; non-JSON calls are unaffected. Cleared on
        # provider config update via invalidate_json_mode_cache_for_provider.
        self._json_mode_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        # In-process cache of (provider_id, resolved_model) pairs where the
        # provider rejects or cannot satisfy response_format itself. Future
        # JSON calls still require JSON, but go straight to plain chat text.
        self._json_response_format_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        # In-process cache of (provider_id, resolved_model) pairs where plain
        # text no-tool generations through Responses completed with no content.
        self._plain_text_responses_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        # In-process cache of (provider_id, resolved_model) pairs for which
        # native OpenAI Responses tool_search/allowed_tools handling was
        # observed to fail. These calls should keep using Responses transport
        # but downgrade exposure to controller-managed search for the rest of
        # the process lifetime (or until provider config changes).
        self._openai_tool_search_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        # In-process cache of (provider_id, resolved_model) pairs for which the
        # Responses API reported hosted/server-side instructions that differed
        # from the instructions Cognis supplied. This is diagnostic only and is
        # surfaced via /info so operators can spot proxy/endpoint drift.
        self._hosted_instruction_drift_keys: dict[tuple[str, str], str] = {}
        # In-process cache of (provider_id, resolved_model) pairs for which the
        # backend rejected prompt_cache_key / prompt_cache_retention params with
        # an "Unknown parameter" or "Unsupported parameter" error. Once marked,
        # cache params are omitted for that pair for the rest of the process
        # lifetime (or until provider config changes). Cleared by
        # invalidate_runtime_capability_cache_for_provider.
        self._prompt_cache_key_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        self._reasoning_summary_broken_keys: CapabilityMarkers = _CapabilityMarkers()
        self._anthropic_defer_loading_broken_keys: CapabilityMarkers = _CapabilityMarkers()

    @staticmethod
    def _tokenizer_family(model: str) -> str:
        normalized = model.rsplit("/", 1)[-1].lower()
        if _ANTHROPIC_MODEL_PATTERNS.search(normalized):
            return "anthropic"
        if _GEMINI_MODEL_PATTERNS.search(normalized):
            return "gemini"
        if normalized.startswith(("gpt-", "o1", "o3", "o4")) or "openai" in normalized:
            return "openai"
        return "unknown"

    def _record_tokenizer_backend(self, model: str, family: str, backend: str) -> None:
        cached = self._tokenizer_backend_cache.get(model)
        if cached == (family, backend):
            return
        self._tokenizer_backend_cache[model] = (family, backend)
        LLM_TOKENIZER_USED_TOTAL.labels(provider=family, backend=backend).inc()

    async def _preflight_litellm_chatgpt_auth(self) -> tuple[str, str | None]:
        """Ensure LiteLLM's ChatGPT authenticator can use Cognis-managed OAuth."""

        from litellm.llms.chatgpt.authenticator import Authenticator

        authenticator = Authenticator()
        try:
            access_token = await asyncio.to_thread(authenticator.get_access_token)
            account_id = await asyncio.to_thread(authenticator.get_account_id)
        except GetAccessTokenError as exc:
            raise RuntimeError(
                "ChatGPT OAuth is not authorized; complete Cognis provider OAuth first"
            ) from exc
        if not access_token:
            raise RuntimeError(
                "ChatGPT OAuth is not authorized; complete Cognis provider OAuth first"
            )
        return access_token, account_id

    async def _persist_chatgpt_oauth_auth_file(
        self,
        *,
        auth_path: Path,
        token_secret_name: str,
        previous: str,
        provider: LLMProviderRow,
    ) -> str:
        if self._secrets is None:
            raise RuntimeError("ChatGPT OAuth requires the encrypted secrets provider")
        if not await asyncio.to_thread(auth_path.exists):
            return previous
        value = await asyncio.to_thread(auth_path.read_text, encoding="utf-8")
        if not value.strip():
            return previous
        self._validate_chatgpt_authorized_record(value)
        if value != previous:
            await self._secrets.set_secret(
                token_secret_name,
                value,
                _oauth_secret_owner(provider),
                scope="system",
                description=_oauth_secret_description(provider),
            )
        return value

    async def resolve_model(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> str:
        resolved_model, _ = await self._resolve_model_target(
            explicit_model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
            acting_user_email=acting_user_email,
        )
        return resolved_model

    async def resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> tuple[str, str | None]:
        resolved_model, provider = await self._resolve_model_target(
            explicit_model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
            acting_user_email=acting_user_email,
        )
        return resolved_model, (provider.provider_id if provider is not None else None)

    async def _oauth_lock_for_provider(self, provider_id: str) -> asyncio.Lock:
        async with self._oauth_locks_lock:
            lock = self._oauth_locks.get(provider_id)
            if lock is None:
                lock = asyncio.Lock()
                self._oauth_locks[provider_id] = lock
            return lock

    @asynccontextmanager
    async def _provider_oauth_token_context(
        self, provider: LLMProviderRow | None
    ) -> AsyncIterator[None]:
        """Hydrate LiteLLM file-backed OAuth state from encrypted DB storage.

        LiteLLM's ChatGPT provider stores OAuth tokens in ``CHATGPT_TOKEN_DIR``.
        Cognis keeps the durable source of truth in encrypted DB secrets so the
        controller can remain stateless; token files are temporary shims only.
        """

        if provider is None or not _looks_like_chatgpt_oauth_provider(provider):
            yield
            return
        self._ensure_controller_side_oauth_provider(provider)
        if self._secrets is None:
            raise RuntimeError("ChatGPT OAuth requires the encrypted secrets provider")

        provider_id = provider.provider_id
        token_secret_name = _oauth_token_secret_name(provider)
        lock = await self._oauth_lock_for_provider(provider_id)
        async with (
            self._oauth_env_lock,
            lock,
            self._postgres_advisory_lock(f"llm-oauth:{provider_id}"),
        ):
            with tempfile.TemporaryDirectory(prefix=f"cognis-{provider_id}-oauth-") as tmpdir:
                auth_path = Path(tmpdir) / CHATGPT_OAUTH_AUTH_FILE
                try:
                    existing = await self._secrets.get_secret(
                        token_secret_name, _oauth_secret_owner(provider), None
                    )
                except KeyError as exc:
                    raise RuntimeError(
                        "ChatGPT OAuth is not authorized; complete the provider OAuth device flow first"
                    ) from exc
                if not existing.strip():
                    raise RuntimeError(
                        "ChatGPT OAuth is not authorized; complete the provider OAuth device flow first"
                    )
                self._validate_chatgpt_authorized_record(existing)
                await asyncio.to_thread(auth_path.write_text, existing, encoding="utf-8")
                with _ScopedEnv(
                    {
                        "CHATGPT_TOKEN_DIR": tmpdir,
                        "CHATGPT_AUTH_FILE": CHATGPT_OAUTH_AUTH_FILE,
                    }
                ):
                    original_device_login = _disable_litellm_chatgpt_device_login()
                    try:
                        await self._preflight_litellm_chatgpt_auth()
                        current = await self._persist_chatgpt_oauth_auth_file(
                            auth_path=auth_path,
                            token_secret_name=token_secret_name,
                            previous=existing,
                            provider=provider,
                        )
                        try:
                            yield
                        finally:
                            await self._persist_chatgpt_oauth_auth_file(
                                auth_path=auth_path,
                                token_secret_name=token_secret_name,
                                previous=current,
                                provider=provider,
                            )
                    finally:
                        _restore_litellm_chatgpt_device_login(original_device_login)

    async def _chatgpt_codex_auth(self, provider: LLMProviderRow) -> CodexAuth:
        """Return fresh ChatGPT OAuth headers for Codex backend endpoints."""

        self._ensure_controller_side_oauth_provider(provider)
        if self._secrets is None:
            raise RuntimeError("ChatGPT OAuth requires the encrypted secrets provider")

        provider_id = provider.provider_id
        token_secret_name = _oauth_token_secret_name(provider)
        lock = await self._oauth_lock_for_provider(provider_id)
        async with (
            lock,
            self._postgres_advisory_lock(f"llm-oauth:{provider_id}"),
        ):
            try:
                existing = await self._secrets.get_secret(
                    token_secret_name, _oauth_secret_owner(provider), None
                )
            except KeyError as exc:
                raise RuntimeError(
                    "ChatGPT OAuth is not authorized; complete the device flow first"
                ) from exc
            auth_record = _parse_chatgpt_authorized_record(existing)
            access_token = auth_record["access_token"]
            expires_at = _chatgpt_access_token_expires_at(access_token)
            if expires_at is None:
                expires_at = _positive_int(auth_record.get("expires_at"), 0) or None
            if expires_at is None or datetime.now(UTC).timestamp() >= expires_at - 60:
                auth_record = await self._refresh_chatgpt_auth_record(auth_record, provider)
                access_token = auth_record["access_token"]
            account_id = auth_record.get("account_id") or _chatgpt_account_id_from_tokens(
                auth_record.get("id_token"), access_token
            )
            return CodexAuth(access_token=access_token, account_id=account_id)

    async def _responses_transport(self, provider: LLMProviderRow | None) -> ResponsesTransport:
        """Resolve the controller-side Responses transport for a provider."""

        if not _uses_direct_codex_transport(provider):
            return self._litellm_transport
        if provider is None:
            raise RuntimeError("Direct Codex transport requires a ChatGPT provider")
        return DirectCodexTransport(await self._chatgpt_codex_auth(provider))

    async def _refresh_chatgpt_auth_record(
        self, auth_record: dict[str, str], provider: LLMProviderRow
    ) -> dict[str, str]:
        from litellm.llms.chatgpt.common_utils import CHATGPT_CLIENT_ID, CHATGPT_OAUTH_TOKEN_URL

        refresh_token = auth_record["refresh_token"]
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                CHATGPT_OAUTH_TOKEN_URL,
                json={
                    "client_id": CHATGPT_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError("ChatGPT OAuth expired; re-authorize the provider")
        data = response.json()
        access_token = data.get("access_token")
        id_token = data.get("id_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("ChatGPT OAuth refresh response missing access_token")
        if not isinstance(id_token, str) or not id_token:
            raise RuntimeError("ChatGPT OAuth refresh response missing id_token")
        refreshed = {
            "access_token": access_token,
            "refresh_token": str(data.get("refresh_token") or refresh_token),
            "id_token": id_token,
        }
        expires_at = _chatgpt_access_token_expires_at(access_token)
        if expires_at is not None:
            refreshed["expires_at"] = str(expires_at)
        account_id = _chatgpt_account_id_from_tokens(id_token, access_token)
        if account_id:
            refreshed["account_id"] = account_id
        if self._secrets is None:
            raise RuntimeError("ChatGPT OAuth requires the encrypted secrets provider")
        await self._secrets.set_secret(
            _oauth_token_secret_name(provider),
            json.dumps(refreshed, ensure_ascii=True, sort_keys=True),
            _oauth_secret_owner(provider),
            scope="system",
            description=_oauth_secret_description(provider),
        )
        return refreshed

    async def _test_chatgpt_codex_provider(
        self,
        provider: LLMProviderRow,
        *,
        model_id: str,
        timeout_seconds: int,
    ) -> None:
        """Run a non-interactive health check for ChatGPT/Codex providers."""

        auth = await self._chatgpt_codex_auth(provider)
        await test_codex_responses(auth, model=model_id, timeout=float(timeout_seconds))

    @asynccontextmanager
    async def _postgres_advisory_lock(self, key: str) -> AsyncIterator[None]:
        """Hold a PostgreSQL transaction-scoped advisory lock when available."""

        async with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name != "postgresql":
                yield
                return
            from sqlalchemy import text

            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
            )
            try:
                yield
            finally:
                await session.commit()

    async def start_chatgpt_oauth(self, provider_id: str) -> dict[str, Any]:
        """Start ChatGPT device-code OAuth and persist pending state encrypted."""

        provider = await self._get_chatgpt_oauth_provider(provider_id)
        owner_email = _oauth_secret_owner(provider)
        lock = await self._oauth_lock_for_provider(provider_id)
        async with lock, self._postgres_advisory_lock(f"llm-oauth:{provider_id}"):
            device_code = await self._request_chatgpt_device_code()
            now = datetime.now(UTC).timestamp()
            interval = _positive_int(device_code.get("interval"), 5)
            pending = {
                "status": "pending",
                "device_auth_id": device_code["device_auth_id"],
                "user_code": device_code["user_code"],
                "verification_url": self._chatgpt_device_verify_url(),
                "interval": interval,
                "expires_at": now + 15 * 60,
                "created_at": now,
            }
            await self._secrets.set_secret(
                _oauth_pending_secret_name(provider),
                json.dumps(pending, ensure_ascii=True, sort_keys=True),
                owner_email,
                scope="system",
                description=f"Pending {_oauth_secret_description(provider)}",
            )
            return self._chatgpt_oauth_public_status(pending)

    async def get_chatgpt_oauth_status(self, provider_id: str) -> dict[str, Any]:
        """Return OAuth status and complete pending device-code authorization if ready."""

        provider = await self._get_chatgpt_oauth_provider(provider_id)
        owner_email = _oauth_secret_owner(provider)
        lock = await self._oauth_lock_for_provider(provider_id)
        async with lock, self._postgres_advisory_lock(f"llm-oauth:{provider_id}"):
            token_secret_name = _oauth_token_secret_name(provider)
            pending_secret_name = _oauth_pending_secret_name(provider)
            try:
                raw = await self._secrets.get_secret(pending_secret_name, owner_email, None)
            except KeyError:
                with contextlib.suppress(KeyError):
                    token_raw = await self._secrets.get_secret(token_secret_name, owner_email, None)
                    self._validate_chatgpt_authorized_record(token_raw)
                    return {"status": "authorized", "provider_id": provider_id}
                return {"status": "not_started", "provider_id": provider_id}
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return {"status": "invalid", "provider_id": provider_id}
            if not isinstance(payload, dict):
                return {"status": "invalid", "provider_id": provider_id}
            if payload.get("status") != "pending":
                return self._chatgpt_oauth_public_status(payload)
            if float(payload.get("expires_at") or 0) <= datetime.now(UTC).timestamp():
                expired = {**payload, "status": "expired"}
                await self._secrets.set_secret(
                    pending_secret_name,
                    json.dumps(expired, ensure_ascii=True, sort_keys=True),
                    owner_email,
                    scope="system",
                    description=_oauth_secret_description(provider),
                )
                return self._chatgpt_oauth_public_status(expired)
            auth_code = await self._poll_chatgpt_device_code_once(payload)
            if auth_code is None:
                return self._chatgpt_oauth_public_status(payload)
            tokens = await self._exchange_chatgpt_auth_code(auth_code)
            auth_record = await asyncio.to_thread(self._build_chatgpt_auth_record, tokens)
            await self._secrets.set_secret(
                token_secret_name,
                json.dumps(auth_record, ensure_ascii=True, sort_keys=True),
                owner_email,
                scope="system",
                description=_oauth_secret_description(provider),
            )
            await self._secrets.delete_secret(
                pending_secret_name,
                owner_email,
                scope="system",
                agent_id=None,
            )
            return {"status": "authorized", "provider_id": provider_id}

    async def clear_chatgpt_oauth(self, provider_id: str) -> bool:
        provider = await self._get_chatgpt_oauth_provider(provider_id)
        owner_email = _oauth_secret_owner(provider)
        lock = await self._oauth_lock_for_provider(provider_id)
        async with lock, self._postgres_advisory_lock(f"llm-oauth:{provider_id}"):
            token_deleted = await self._secrets.delete_secret(
                _oauth_token_secret_name(provider),
                owner_email,
                scope="system",
                agent_id=None,
            )
            pending_deleted = await self._secrets.delete_secret(
                _oauth_pending_secret_name(provider),
                owner_email,
                scope="system",
                agent_id=None,
            )
            return token_deleted or pending_deleted

    async def _get_chatgpt_oauth_provider(self, provider_id: str) -> LLMProviderRow:
        if self._secrets is None:
            raise RuntimeError("ChatGPT OAuth requires the encrypted secrets provider")
        async with self.session_factory() as session:
            provider = await session.get(LLMProviderRow, provider_id)
        if provider is None:
            raise ValueError("LLM provider not found")
        if not _looks_like_chatgpt_oauth_provider(provider):
            raise ValueError("LLM provider is not configured for ChatGPT OAuth")
        self._ensure_controller_side_oauth_provider(provider)
        return provider

    @staticmethod
    def _ensure_controller_side_oauth_provider(provider: LLMProviderRow) -> None:
        if provider.location == "executor":
            raise RuntimeError(
                "ChatGPT OAuth providers must run on the controller; executor-side token "
                "hydration is not implemented"
            )

    @staticmethod
    def _validate_chatgpt_authorized_record(raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ChatGPT OAuth token cache is invalid; restart OAuth") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("ChatGPT OAuth token cache is invalid; restart OAuth")
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise RuntimeError("ChatGPT OAuth is not authorized; complete the device flow first")

    @staticmethod
    def _chatgpt_oauth_public_status(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": str(payload.get("status") or "unknown"),
            "verification_url": payload.get("verification_url"),
            "user_code": payload.get("user_code"),
            "interval": payload.get("interval"),
            "expires_at": payload.get("expires_at"),
        }

    @staticmethod
    def _chatgpt_device_verify_url() -> str:
        from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

        return CHATGPT_DEVICE_VERIFY_URL

    @staticmethod
    async def _request_chatgpt_device_code() -> dict[str, Any]:
        from litellm.llms.chatgpt.common_utils import CHATGPT_CLIENT_ID, CHATGPT_DEVICE_CODE_URL

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                CHATGPT_DEVICE_CODE_URL,
                json={"client_id": CHATGPT_CLIENT_ID},
            )
            response.raise_for_status()
        data = response.json()
        device_auth_id = data.get("device_auth_id")
        user_code = data.get("user_code") or data.get("usercode")
        if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
            raise RuntimeError("ChatGPT device-code response missing required fields")
        return {
            "device_auth_id": device_auth_id,
            "user_code": user_code,
            "interval": data.get("interval") or 5,
        }

    @staticmethod
    async def _poll_chatgpt_device_code_once(payload: dict[str, Any]) -> dict[str, str] | None:
        from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_TOKEN_URL

        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
            raise RuntimeError("ChatGPT OAuth pending state is incomplete")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                CHATGPT_DEVICE_TOKEN_URL,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
            )
        if response.status_code in {403, 404}:
            return None
        response.raise_for_status()
        data = response.json()
        required = ("authorization_code", "code_challenge", "code_verifier")
        if not all(isinstance(data.get(key), str) and data.get(key) for key in required):
            return None
        return {key: str(data[key]) for key in required}

    @staticmethod
    async def _exchange_chatgpt_auth_code(code_data: dict[str, str]) -> dict[str, str]:
        from litellm.llms.chatgpt.common_utils import (
            CHATGPT_AUTH_BASE,
            CHATGPT_CLIENT_ID,
            CHATGPT_OAUTH_TOKEN_URL,
        )

        redirect_uri = f"{CHATGPT_AUTH_BASE}/deviceauth/callback"
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code_data["authorization_code"],
                "redirect_uri": redirect_uri,
                "client_id": CHATGPT_CLIENT_ID,
                "code_verifier": code_data["code_verifier"],
            }
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                CHATGPT_OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                content=body,
            )
            response.raise_for_status()
        data = response.json()
        required = ("access_token", "refresh_token", "id_token")
        if not all(isinstance(data.get(key), str) and data.get(key) for key in required):
            raise RuntimeError("ChatGPT OAuth token response missing required fields")
        return {key: str(data[key]) for key in required}

    @staticmethod
    def _build_chatgpt_auth_record(tokens: dict[str, str]) -> dict[str, Any]:
        from litellm.llms.chatgpt.authenticator import Authenticator

        return Authenticator()._build_auth_record(tokens)

    async def _resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> tuple[str, LLMProviderRow | None]:
        if explicit_provider_id is not None:
            async with self.session_factory() as session:
                provider = await session.get(LLMProviderRow, explicit_provider_id)
            if provider is None:
                raise ValueError(f"LLM provider {explicit_provider_id!r} not found")
            if not _provider_visible_to_user(provider, acting_user_email):
                raise ValueError(f"LLM provider {explicit_provider_id!r} is not visible")
            if explicit_model is not None:
                return explicit_model, provider
            default_model = dict(provider.config).get("default_model")
            if isinstance(default_model, str) and default_model:
                return default_model, provider
            raise ValueError(
                f"LLM provider {explicit_provider_id!r} does not define a default_model"
            )
        if explicit_model is not None:
            async with self.session_factory() as session:
                provider = await self._find_provider_for_model(
                    session, explicit_model, acting_user_email=acting_user_email
                )
            return explicit_model, provider
        cache_scope = _owner_scope_cache_key(acting_user_email)
        cache_key = f"{cache_scope}:{task_type}"
        cached_target = await self._get_cached_resolved_model(cache_key)
        if cached_target is not None:
            cached_model, cached_provider_id = cached_target
            async with self.session_factory() as session:
                provider = (
                    await session.get(LLMProviderRow, cached_provider_id)
                    if cached_provider_id is not None
                    else await self._find_provider_for_model(
                        session, cached_model, acting_user_email=acting_user_email
                    )
                )
            if cached_provider_id is None or provider is not None:
                return cached_model, provider
            async with self._cache_lock:
                self._resolved_model_cache.pop(cache_key, None)
        async with self.session_factory() as session:
            route = None
            if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL:
                route = (
                    await session.execute(
                        select(ModelRouting).where(
                            ModelRouting.owner_email == acting_user_email,
                            ModelRouting.task_type == task_type,
                        )
                    )
                ).scalar_one_or_none()
            if route is None:
                route = (
                    await session.execute(
                        select(ModelRouting).where(
                            ModelRouting.owner_email == SYSTEM_USER_EMAIL,
                            ModelRouting.task_type == task_type,
                        )
                    )
                ).scalar_one_or_none()
            if route is not None:
                resolved = cast(str, route.model)
                if task_type == "compaction" and resolved == "__same_session_model__":
                    await self._set_cached_resolved_model(cache_key, resolved, None)
                    return resolved, None
                provider = None
                if route.provider_id is not None:
                    provider = await session.get(LLMProviderRow, route.provider_id)
                    if provider is None:
                        raise ValueError(
                            f"Model routing for task_type={task_type!r} references missing provider "
                            f"{route.provider_id!r}"
                        )
                    if getattr(provider, "status", None) != "active":
                        provider = None
                if provider is None:
                    provider = await self._find_provider_for_model(
                        session, resolved, acting_user_email=acting_user_email
                    )
                if provider is not None and not _provider_visible_to_user(
                    provider, acting_user_email
                ):
                    provider = None
                await self._set_cached_resolved_model(
                    cache_key,
                    resolved,
                    provider.provider_id if provider is not None else None,
                )
                return resolved, provider
            if task_type == "compaction":
                resolved = "__same_session_model__"
                await self._set_cached_resolved_model(cache_key, resolved, None)
                return resolved, None
            # Try provider marked as default (is_default=True)
            default_provider = (
                await session.execute(
                    select(LLMProviderRow)
                    .where(
                        LLMProviderRow.is_default.is_(True),
                        LLMProviderRow.status == "active",
                        LLMProviderRow.owner_email.in_(
                            [acting_user_email, SYSTEM_USER_EMAIL]
                            if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL
                            else [SYSTEM_USER_EMAIL]
                        ),
                    )
                    .order_by(
                        (LLMProviderRow.owner_email == SYSTEM_USER_EMAIL).asc(),
                        LLMProviderRow.provider_id.asc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            # Fall back to provider with ID "default" for backward compat
            if default_provider is None:
                default_provider = (
                    await session.execute(
                        select(LLMProviderRow).where(
                            LLMProviderRow.provider_id == "default",
                            LLMProviderRow.status == "active",
                            LLMProviderRow.owner_email.in_(
                                [acting_user_email, SYSTEM_USER_EMAIL]
                                if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL
                                else [SYSTEM_USER_EMAIL]
                            ),
                        )
                    )
                ).scalar_one_or_none()
            if default_provider is not None:
                config = dict(default_provider.config)
                default_model = config.get("default_model")
                if isinstance(default_model, str):
                    await self._set_cached_resolved_model(
                        cache_key,
                        default_model,
                        default_provider.provider_id,
                    )
                    return default_model, default_provider
        raise ValueError("No LLM model configured")

    async def get_model_info(
        self,
        model_id: str,
        provider_id: str | None = None,
        acting_user_email: str | None = None,
    ) -> ModelInfo:
        cache_provider_key = provider_id
        if cache_provider_key is None:
            cache_provider_key = f"scope:{_owner_scope_cache_key(acting_user_email)}"
        cache_key = self._model_info_cache_key(model_id, cache_provider_key)
        cached_model_info = await self._get_cached_model_info(cache_key)
        if cached_model_info is not None:
            return cached_model_info

        async with self.session_factory() as session:
            provider = await session.get(LLMProviderRow, provider_id) if provider_id else None
            if provider_id is not None and provider is None:
                logger.warning(
                    "Requested model metadata for missing provider",
                    extra={"extra_data": {"provider_id": provider_id, "model_id": model_id}},
                )
                await self._set_cached_model_info(cache_key, DEFAULT_MODEL_INFO)
                return DEFAULT_MODEL_INFO
            if provider is not None and not _provider_visible_to_user(provider, acting_user_email):
                logger.warning(
                    "Requested model metadata for provider outside actor visibility",
                    extra={"extra_data": {"provider_id": provider_id, "model_id": model_id}},
                )
                await self._set_cached_model_info(cache_key, DEFAULT_MODEL_INFO)
                return DEFAULT_MODEL_INFO
            if provider is not None:
                config = dict(provider.config)
                row_models = config.get("models", [])
                if isinstance(row_models, list):
                    for model in row_models:
                        if isinstance(model, dict) and model.get("model_id") == model_id:
                            model_info = await self._merge_litellm_model_info(
                                model_id, provider, model
                            )
                            await self._set_cached_model_info(cache_key, model_info)
                            return model_info
                model_info = await self._merge_litellm_model_info(model_id, provider, {})
                await self._set_cached_model_info(cache_key, model_info)
                return model_info
            visible_owners = [SYSTEM_USER_EMAIL]
            if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL:
                visible_owners.insert(0, acting_user_email)
            rows = (
                (
                    await session.execute(
                        select(LLMProviderRow).where(LLMProviderRow.owner_email.in_(visible_owners))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if provider_id is not None and row.provider_id != provider_id:
                    continue
                config = dict(row.config)
                row_models = config.get("models", [])
                if not isinstance(row_models, list):
                    continue
                for model in row_models:
                    if not isinstance(model, dict):
                        continue
                    if model.get("model_id") != model_id:
                        continue
                    model_info = await self._merge_litellm_model_info(model_id, row, model)
                    await self._set_cached_model_info(cache_key, model_info)
                    return model_info

            if provider is None:
                provider = await self._find_provider_for_model(
                    session, model_id, acting_user_email=acting_user_email
                )
            if provider is not None:
                model_info = await self._merge_litellm_model_info(model_id, provider, {})
                await self._set_cached_model_info(cache_key, model_info)
                return model_info

        logger.warning(
            "LLM model metadata missing; using conservative defaults",
            extra={"extra_data": {"model_id": model_id}},
        )
        await self._set_cached_model_info(cache_key, DEFAULT_MODEL_INFO)
        return DEFAULT_MODEL_INFO

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        filename: str,
        model: str | None = None,
        task_type: str = "speech_to_text",
        prompt: str | None = None,
        language: str | None = None,
    ) -> SpeechToTextResult:
        resolved_model, provider = await self._resolve_model_target(model, task_type=task_type)
        if provider is None:
            raise ValueError(f"No LLM provider found for transcription model {resolved_model!r}")
        provider_preset = str(dict(provider.config).get("preset", "")).lower()
        model_name = self._transcription_wire_model(resolved_model, provider_preset)
        model_info = await self.get_model_info(resolved_model, provider.provider_id)
        from cognis.audio.preprocessing import (
            prepare_audio_for_stt as _prepare_audio_for_stt,
        )
        from cognis.audio.preprocessing import (
            stt_supported_audio_mime_types as _stt_supported_audio_mime_types,
        )

        supported_audio_mime_types = _stt_supported_audio_mime_types(
            model=resolved_model,
            model_info=model_info,
        )
        logger.debug(
            "llm: speech-to-text request prepared",
            extra={
                "extra_data": {
                    "resolved_model": resolved_model,
                    "wire_model": model_name,
                    "provider_preset": provider_preset,
                    "executor_routed": self._should_route_to_executor(provider),
                }
            },
        )
        if self._should_route_to_executor(provider):
            if self._inference_router is None:
                raise RuntimeError("Speech-to-text executor routing is unavailable")
            request_kwargs = await self._resolve_provider_kwargs(provider)
            return await self._inference_router.route_transcribe(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                filename=filename,
                model=model_name,
                provider_preset=provider_preset,
                executor_id=dict(provider.config).get("executor_id"),
                executor_labels=dict(provider.config).get("executor_labels"),
                supported_audio_mime_types=supported_audio_mime_types,
                request_kwargs=request_kwargs,
                prompt=prompt,
                language=language,
            )

        audio_bytes, mime_type, filename = await _prepare_audio_for_stt(
            audio_bytes,
            mime_type=mime_type,
            filename=filename,
            supported_mime_types=supported_audio_mime_types,
        )
        request_kwargs = await self._resolve_provider_kwargs(provider)
        api_base = request_kwargs.get("api_base") or request_kwargs.get("base_url")
        if not isinstance(api_base, str) or not api_base:
            api_base = "https://api.openai.com"
        api_key = request_kwargs.get("api_key")
        extra_headers = request_kwargs.get("extra_headers")
        timeout = request_kwargs.get("timeout", 120)
        data: dict[str, str] = {"model": model_name}
        if prompt:
            data["prompt"] = prompt
        if language:
            data["language"] = language

        headers: dict[str, str] = {}
        if isinstance(api_key, str) and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if isinstance(extra_headers, dict):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})

        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = filename
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{api_base.rstrip('/')}/v1/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files={"file": (filename, file_obj, mime_type)},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._sanitize_http_error_detail(exc)
            raise RuntimeError(f"Speech-to-text request failed: {detail}") from exc
        except Exception as exc:
            detail = self._sanitize_error_detail(exc)
            raise RuntimeError(f"Speech-to-text request failed: {detail}") from exc

        payload = response.json()
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Speech-to-text returned an empty transcript")
        duration = payload.get("duration")
        return SpeechToTextResult(
            text=text.strip(),
            model=resolved_model,
            language=payload.get("language") if isinstance(payload.get("language"), str) else None,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        )

    async def synthesize(
        self,
        text: str,
        *,
        voice: str,
        model: str | None = None,
        task_type: str = "text_to_speech",
        response_format: str = "mp3",
        speed: float = 1.0,
        low_latency: bool = False,
    ) -> TextToSpeechResult:
        """Synthesize speech via LiteLLM (OpenAI, ElevenLabs, Azure, etc.).

        When the resolved provider has ``location="executor"`` the call is
        routed through a matching remote executor; otherwise it is performed
        locally via ``litellm.aspeech()`` with a direct-HTTP fallback for
        backends LiteLLM does not yet support.
        """
        if not text or not text.strip():
            raise ValueError("Text-to-speech requires non-empty text")
        resolved_model, provider = await self._resolve_model_target(model, task_type=task_type)
        if provider is None:
            raise ValueError(f"No LLM provider found for synthesis model {resolved_model!r}")
        provider_preset = str(dict(provider.config).get("preset", "")).lower()
        wire_model = self._transcription_wire_model(resolved_model, provider_preset)
        normalized_format = (response_format or "mp3").strip().lower()
        if normalized_format not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
            normalized_format = "mp3"
        logger.debug(
            "llm: text-to-speech request prepared",
            extra={
                "extra_data": {
                    "resolved_model": resolved_model,
                    "wire_model": wire_model,
                    "provider_preset": provider_preset,
                    "voice": voice,
                    "format": normalized_format,
                    "executor_routed": self._should_route_to_executor(provider),
                }
            },
        )
        request_kwargs = await self._resolve_provider_kwargs(provider)
        if low_latency:
            configured_timeout = request_kwargs.get("timeout", 120)
            request_kwargs["timeout"] = (
                min(configured_timeout, 20) if isinstance(configured_timeout, int | float) else 20
            )

        if self._should_route_to_executor(provider):
            if self._inference_router is None:
                raise RuntimeError("Text-to-speech executor routing is unavailable")
            return await self._inference_router.route_synthesize(
                text=text,
                voice=voice,
                model=wire_model,
                provider_preset=provider_preset,
                executor_id=dict(provider.config).get("executor_id"),
                executor_labels=dict(provider.config).get("executor_labels"),
                response_format=normalized_format,
                speed=speed,
                request_kwargs=request_kwargs,
                low_latency=low_latency,
            )

        return await _run_synthesize_local(
            text=text,
            voice=voice,
            wire_model=wire_model,
            response_format=normalized_format,
            speed=speed,
            request_kwargs=request_kwargs,
            resolved_model=resolved_model,
            provider_preset=provider_preset,
            sanitize_http=self._sanitize_http_error_detail,
            sanitize_general=self._sanitize_error_detail,
            prefer_direct_http=low_latency,
        )

    async def enrich_model_info(
        self,
        model_id: str,
        *,
        provider_id: str | None = None,
        preset: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> ModelInfo:
        """Enrich a model ID with metadata from a provider and/or litellm.

        Can be called with either ``provider_id`` (for saved providers) or
        ``preset``/``base_url``/``api_key`` (for preview mode before the
        provider is saved).
        """
        if provider_id is not None:
            async with self.session_factory() as session:
                provider = await session.get(LLMProviderRow, provider_id)
            if provider is None:
                raise ValueError(f"Provider {provider_id!r} not found")
            return await self._merge_litellm_model_info(
                model_id, provider, {}, api_key_override=api_key
            )

        # Preview mode: construct a temporary provider row so the merge
        # chain can resolve preset, base_url, and model prefix correctly.
        temp_config: dict[str, Any] = {}
        if preset:
            temp_config["preset"] = preset
        if base_url:
            temp_config["base_url"] = base_url
            temp_config["api_base"] = base_url
        temp_provider = LLMProviderRow(
            provider_id="__preview__",
            display_name="Preview",
            location="controller",
            backend="litellm",
            config=temp_config,
            status="active",
        )
        return await self._merge_litellm_model_info(
            model_id, temp_provider, {}, api_key_override=api_key
        )

    async def find_provider_for_model(
        self, model_id: str, acting_user_email: str | None = None
    ) -> str | None:
        """Return the deterministic ``provider_id`` that owns *model_id*."""
        async with self.session_factory() as session:
            visible_owners = [SYSTEM_USER_EMAIL]
            if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL:
                visible_owners.insert(0, acting_user_email)
            rows = (
                (
                    await session.execute(
                        select(LLMProviderRow).where(
                            LLMProviderRow.status == "active",
                            LLMProviderRow.owner_email.in_(visible_owners),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return self._select_provider_id_for_model(rows, model_id)

    async def _get_route_reasoning_effort(
        self, task_type: str, acting_user_email: str | None = None
    ) -> str | None:
        async with self.session_factory() as session:
            user_owner = acting_user_email or SYSTEM_USER_EMAIL
            route = await get_model_routing(session, task_type, owner_email=user_owner)
            if route is None and user_owner != SYSTEM_USER_EMAIL:
                route = await get_model_routing(session, task_type, owner_email=SYSTEM_USER_EMAIL)
        if route is None or not isinstance(route.config, dict):
            return None
        normalized = normalize_reasoning_level(route.config.get("reasoning_effort"))
        if normalized == "default":
            return None
        return normalized

    async def _merge_litellm_model_info(
        self,
        model_id: str,
        provider: LLMProviderRow | None,
        configured: dict[str, Any],
        *,
        api_key_override: str | None = None,
    ) -> ModelInfo:
        """Build a :class:`ModelInfo` by merging multiple metadata sources.

        Merge order (later wins):
        ``DEFAULT_MODEL_INFO`` -> capability defaults -> litellm static ->
        **Codex/proxy live metadata** -> user-configured overrides from DB.

        ``api_key_override`` is used in preview mode where the API key is
        not yet persisted in the provider's ``auth_config``.
        """
        merged: dict[str, Any] = dict(DEFAULT_MODEL_INFO.model_dump())
        preset = (
            str(dict(provider.config).get("preset", "")).lower() if provider is not None else ""
        )
        capability_defaults = self._infer_model_capabilities(model_id, provider)
        merged.update(capability_defaults)

        # ChatGPT/Codex management paths must stay independent of LiteLLM's
        # ChatGPT transport/auth stack. Runtime generation still uses LiteLLM;
        # metadata comes from the bundled/remote Codex catalog plus overrides.
        if preset != "chatgpt":
            try:
                provider_kwargs = await self._resolve_provider_kwargs(provider)
                live = litellm.get_model_info(
                    model=self._apply_model_prefix(model_id, provider),
                    custom_llm_provider=(
                        dict(provider.config).get("preset") if provider is not None else None
                    ),
                    api_base=provider_kwargs.get("api_base"),
                )
                if isinstance(live, dict):
                    live_dict = cast(dict[str, Any], live)
                    live_context_window = (
                        live_dict.get("max_context_window_tokens")
                        or live_dict.get("max_context_window")
                        or live_dict.get("context_window")
                        or live_dict.get("max_input_tokens")
                    )
                    merged.update(
                        {
                            "context_window": live_context_window or merged.get("context_window"),
                            "max_context_window": live_context_window
                            or merged.get("max_context_window"),
                            "max_input_tokens": live_dict.get("max_input_tokens")
                            or merged.get("max_input_tokens"),
                            "max_output_tokens": live_dict.get("max_output_tokens")
                            or merged.get("max_output_tokens"),
                            "supports_tools": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_tools",
                                fallback=bool(
                                    live_dict.get("supports_function_calling")
                                    or "tools" in (live_dict.get("supported_openai_params") or [])
                                ),
                            ),
                            "supports_streaming": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_streaming",
                                fallback="stream"
                                in (live_dict.get("supported_openai_params") or []),
                            ),
                            "supports_vision": _merge_live_bool(
                                live_dict, merged, "supports_vision"
                            ),
                            "supports_audio_input": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_audio_input",
                            ),
                            "supports_image_generation": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_image_generation",
                            ),
                            "supports_embedding": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_embedding",
                                fallback=looks_like_embedding_model(model_id),
                            ),
                            "supported_audio_mime_types": list(
                                live_dict.get("supported_audio_mime_types")
                                or merged.get("supported_audio_mime_types")
                                or []
                            ),
                            "supports_pdf_input": _merge_live_bool(
                                live_dict, merged, "supports_pdf_input"
                            ),
                            "supports_file_input": _merge_live_bool(
                                live_dict, merged, "supports_file_input"
                            ),
                            "supports_reasoning": _merge_live_bool(
                                live_dict, merged, "supports_reasoning"
                            ),
                            "supports_extended_thinking": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_extended_thinking",
                                fallback=_looks_like_extended_thinking_model(model_id, preset),
                            ),
                            "supports_verbosity": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_verbosity",
                            ),
                            "default_verbosity": live_dict.get("default_verbosity")
                            or merged.get("default_verbosity"),
                            "supports_prompt_caching": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_prompt_caching",
                            ),
                            "supports_tool_search": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_tool_search",
                                fallback=bool(live_dict.get("supports_builtin_tool_search")),
                            ),
                            "supports_defer_loading": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_defer_loading",
                            ),
                            "supports_responses_api": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_responses_api",
                            ),
                            "supports_openai_namespace_tools": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_openai_namespace_tools",
                            ),
                            "supports_openai_allowed_tools": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_openai_allowed_tools",
                            ),
                            "supports_openai_apply_patch": _merge_live_bool(
                                live_dict,
                                merged,
                                "supports_openai_apply_patch",
                            ),
                            "supported_openai_params": list(
                                live_dict.get("supported_openai_params") or []
                            ),
                            "max_tools": live_dict.get("max_tools") or merged.get("max_tools"),
                        }
                    )
            except Exception:
                logger.debug(
                    "LLM model metadata lookup via LiteLLM failed",
                    extra={"extra_data": {"model_id": model_id}},
                    exc_info=True,
                )

        # For litellm_proxy preset, fetch live metadata from the proxy's
        # /model/info endpoint.  This overrides the (potentially stale)
        # litellm static data but is itself overridden by user-configured
        # values from the DB.
        if preset == "litellm_proxy" and provider is not None:
            prov_config = dict(provider.config)
            proxy_base = prov_config.get("base_url") or prov_config.get("api_base") or ""
            proxy_key = api_key_override or (await self._resolve_provider_kwargs(provider)).get(
                "api_key", ""
            )
            if proxy_base:
                proxy_info_map = await self._fetch_proxy_model_info(proxy_base, proxy_key)
                proxy_info = proxy_info_map.get(model_id, {})
                if proxy_info:
                    merged.update(proxy_info)

        if preset == "chatgpt":
            codex_info = codex_catalog_model_info(model_id) or codex_unknown_model_info(model_id)
            existing_efforts = merged.get("reasoning_efforts")
            merged.update(codex_info)
            if configured.get("reasoning_efforts") is None and isinstance(existing_efforts, list):
                merged["reasoning_efforts"] = (
                    codex_info.get("reasoning_efforts") or existing_efforts
                )

        metadata_floor = _metadata_floor_for_model(model_id)
        if metadata_floor is not None:
            applied_floor: dict[str, int] = {}
            raise_known_floor = preset in {"openai", "azure"}
            context_window = int(merged.get("context_window") or 0)
            if context_window <= DEFAULT_MODEL_INFO.context_window or (
                raise_known_floor and context_window < metadata_floor["context_window"]
            ):
                merged["context_window"] = metadata_floor["context_window"]
                applied_floor["context_window"] = metadata_floor["context_window"]
            max_context_window = int(merged.get("max_context_window") or 0)
            if max_context_window <= DEFAULT_MODEL_INFO.context_window or (
                raise_known_floor and max_context_window < metadata_floor["max_context_window"]
            ):
                merged["max_context_window"] = metadata_floor["max_context_window"]
                applied_floor["max_context_window"] = metadata_floor["max_context_window"]
            max_input_tokens = int(merged.get("max_input_tokens") or 0)
            if max_input_tokens <= 0 or (
                raise_known_floor and max_input_tokens < metadata_floor["max_input_tokens"]
            ):
                merged["max_input_tokens"] = metadata_floor["max_input_tokens"]
                applied_floor["max_input_tokens"] = metadata_floor["max_input_tokens"]
            max_output_tokens = int(merged.get("max_output_tokens") or 0)
            if max_output_tokens <= DEFAULT_MODEL_INFO.max_output_tokens or (
                raise_known_floor and max_output_tokens < metadata_floor["max_output_tokens"]
            ):
                merged["max_output_tokens"] = metadata_floor["max_output_tokens"]
                applied_floor["max_output_tokens"] = metadata_floor["max_output_tokens"]
            if applied_floor:
                logger.warning(
                    "Applied conservative model metadata floor",
                    extra={
                        "extra_data": {
                            "model_id": model_id,
                            "provider_id": provider.provider_id if provider is not None else None,
                            **applied_floor,
                        }
                    },
                )
        merged.update(configured)
        merged["model_id"] = model_id
        if preset != "chatgpt" or not merged.get("reasoning_efforts"):
            profile_preview = ModelInfo.model_validate(merged)
            merged["reasoning_efforts"] = reasoning_efforts_for_model(
                model_id,
                provider_preset=preset,
                model_info=profile_preview,
                supports_reasoning=bool(merged.get("supports_reasoning")),
            )
        return ModelInfo.model_validate(merged)

    def _infer_model_capabilities(
        self, model_id: str, provider: LLMProviderRow | None
    ) -> dict[str, Any]:
        model_name = normalize_openai_model_name(self._apply_model_prefix(model_id, provider))
        preset = (
            str(dict(provider.config).get("preset", "")).lower() if provider is not None else ""
        )
        is_anthropic = bool(_ANTHROPIC_MODEL_PATTERNS.search(model_name)) or preset == "anthropic"
        is_openai_like = (
            model_name.startswith("gpt-")
            or model_name.startswith("openai/")
            or preset in {"openai", "litellm_proxy", "openai_compatible", "chatgpt"}
        )
        supports_responses_api = bool(
            is_openai_like
            and (
                model_name.startswith("gpt-5")
                or model_name.startswith("openai/gpt-5")
                or model_name.startswith("gpt-4.1")
                or model_name.startswith("openai/gpt-4.1")
                or model_name.startswith("gpt-4o")
                or model_name.startswith("openai/gpt-4o")
            )
        )
        supports_openai_tool_search = bool(
            supports_responses_api and _supports_openai_tool_search_model(model_name)
        )
        supports_openai_apply_patch = bool(
            supports_responses_api
            and preset in {"openai", "chatgpt"}
            and _looks_like_openai_apply_patch_model(model_name)
        )
        supports_image_generation = _looks_like_image_generation_model(model_name)
        supports_embedding = looks_like_embedding_model(model_name)
        return {
            "supports_defer_loading": is_anthropic,
            "supports_prompt_caching": is_anthropic,
            "supports_tool_search": supports_openai_tool_search,
            "supports_responses_api": supports_responses_api,
            "supports_extended_thinking": False,
            "supports_openai_namespace_tools": supports_openai_tool_search,
            "supports_openai_allowed_tools": supports_openai_tool_search,
            "supports_openai_apply_patch": supports_openai_apply_patch,
            "supports_image_generation": supports_image_generation,
            "supports_embedding": supports_embedding,
            "max_tools": 128 if is_openai_like else None,
        }

    def _responses_rollout_mode(self) -> str:
        value = os.getenv("COGNIS_OPENAI_RESPONSES_MODE", "auto").strip().lower()
        if value in {"on", "off", "auto"}:
            return value
        return "auto"

    def _capability_fallback_ttl_seconds(self, provider: LLMProviderRow | None = None) -> float:
        config = dict(provider.config or {}) if provider is not None else {}
        raw = config.get("llm_capability_fallback_ttl_seconds")
        if raw is None:
            raw = os.getenv("COGNIS_LLM_CAPABILITY_FALLBACK_TTL_SECONDS")
        try:
            value = float(raw) if raw is not None else _DEFAULT_CAPABILITY_FALLBACK_TTL_SECONDS
        except (TypeError, ValueError):
            value = _DEFAULT_CAPABILITY_FALLBACK_TTL_SECONDS
        return max(0.0, value)

    def _capability_is_broken(
        self,
        markers: dict[tuple[str, str], float],
        key: tuple[str, str],
        *,
        marker_name: str,
        provider: LLMProviderRow | None = None,
    ) -> bool:
        expires_at = markers.get(key)
        if expires_at is None:
            return False
        if expires_at == 0.0 or monotonic() <= expires_at:
            return True
        markers.pop(key, None)
        LLM_CAPABILITY_FALLBACK_EXPIRED_TOTAL.labels(
            marker=marker_name,
            provider_id=key[0],
            model=key[1],
        ).inc()
        return False

    def _mark_capability_broken(
        self,
        markers: dict[tuple[str, str], float],
        key: tuple[str, str],
        *,
        marker_name: str,
        provider: LLMProviderRow | None,
    ) -> bool:
        ttl = self._capability_fallback_ttl_seconds(provider)
        expires_at = 0.0 if ttl == 0 else monotonic() + ttl
        newly_marked = not self._capability_is_broken(
            markers,
            key,
            marker_name=marker_name,
            provider=provider,
        )
        markers[key] = expires_at
        return newly_marked

    def _active_capability_keys(
        self,
        markers: dict[tuple[str, str], float],
        *,
        marker_name: str,
        provider: LLMProviderRow | None = None,
    ) -> set[tuple[str, str]]:
        keys = list(markers)
        return {
            key
            for key in keys
            if self._capability_is_broken(
                markers,
                key,
                marker_name=marker_name,
                provider=provider,
            )
        }

    def _default_reasoning_summary(
        self,
        *,
        provider: LLMProviderRow | None,
        resolved_model: str,
        model_info: ModelInfo,
    ) -> str | None:
        if provider is not None and self._capability_is_broken(
            self._reasoning_summary_broken_keys,
            (provider.provider_id, resolved_model),
            marker_name="reasoning_summary",
            provider=provider,
        ):
            return "none"
        value = model_info.default_reasoning_summary
        found, configured = _model_config_value(
            provider, resolved_model, "default_reasoning_summary"
        )
        if found and isinstance(configured, str) and configured.strip():
            return configured.strip()
        if isinstance(value, str) and value.strip().lower() == "none":
            return "none"
        if (
            model_info.supports_responses_api
            and model_info.supports_reasoning
            and (
                model_info.reasoning_summary_format
                or "gpt-5" in normalize_openai_model_name(resolved_model)
            )
        ):
            return "auto"
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "auto"

    def _mark_reasoning_summary_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._reasoning_summary_broken_keys,
            key,
            marker_name="reasoning_summary",
            provider=provider,
        )
        LLM_REASONING_SUMMARY_REJECTED_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
        ).inc()
        if newly_marked:
            logger.warning(
                "Reasoning summary rejected by backend; disabling temporarily for this provider/model",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _should_use_responses_api(
        self,
        model_id: str,
        model_info: ModelInfo,
        provider: LLMProviderRow | None,
        *,
        is_json_mode_request: bool = False,
    ) -> bool:
        if _looks_like_chatgpt_oauth_provider(provider):
            return True
        if provider is not None and dict(provider.config).get("use_responses_api") is False:
            return False
        if (
            is_json_mode_request
            and provider is not None
            and self._capability_is_broken(
                self._json_mode_broken_keys,
                (provider.provider_id, model_id),
                marker_name="json_mode",
                provider=provider,
            )
        ):
            LLM_JSON_MODE_TRANSPORT_FLIP_TOTAL.labels(
                provider_id=provider.provider_id,
                model=model_id,
                reason="cached_broken",
            ).inc()
            return False
        return should_use_openai_responses(
            model=self._apply_model_prefix(model_id, provider),
            model_info=model_info,
            rollout_mode=self._responses_rollout_mode(),
        )

    def _native_apply_patch_setting(
        self, model_id: str, provider: LLMProviderRow | None
    ) -> str | bool:
        """Return provider/model native apply_patch setting: auto, true, or false."""

        if provider is None:
            return "auto"
        config = dict(provider.config or {})
        setting: str | bool = config.get("use_native_apply_patch", "auto")
        row_models = config.get("models", [])
        if isinstance(row_models, list):
            for model in row_models:
                if isinstance(model, dict) and model.get("model_id") == model_id:
                    if "use_native_apply_patch" in model:
                        model_setting = model.get("use_native_apply_patch")
                        if model_setting is not None:
                            setting = model_setting
                    break
        if isinstance(setting, bool):
            return setting
        normalized = str(setting or "auto").strip().lower()
        if normalized in {"true", "on", "enabled", "yes"}:
            return True
        if normalized in {"false", "off", "disabled", "no"}:
            return False
        return "auto"

    def _resolve_native_apply_patch_contract(
        self,
        *,
        model_id: str,
        model_info: ModelInfo,
        provider: LLMProviderRow | None,
        use_responses_api: bool,
    ) -> tuple[bool, str]:
        setting = self._native_apply_patch_setting(model_id, provider)
        if setting is False:
            return False, "disabled_by_config"
        if not use_responses_api:
            return False, "responses_api_not_active"
        if setting is True:
            return True, "enabled_by_config"
        if not bool(model_info.supports_openai_apply_patch):
            return False, "model_capability_missing"
        return True, "model_capability"

    async def resolve_tool_exposure_contract(
        self,
        *,
        model_id: str,
        model_info: ModelInfo,
        provider_id: str | None,
        allow_tool_search: bool,
    ) -> ToolExposureContract:
        """Resolve the runtime transport + discovery contract for one turn."""

        provider: LLMProviderRow | None = None
        if provider_id is not None:
            async with self.session_factory() as session:
                provider = await session.get(LLMProviderRow, provider_id)

        use_responses_api = self._should_use_responses_api(
            model_id,
            model_info,
            provider,
            is_json_mode_request=False,
        )
        llm_api = LLMApiMode.RESPONSES if use_responses_api else LLMApiMode.CHAT_COMPLETIONS
        native_apply_patch, native_apply_patch_reason = self._resolve_native_apply_patch_contract(
            model_id=model_id,
            model_info=model_info,
            provider=provider,
            use_responses_api=use_responses_api,
        )
        native_apply_patch_tool_type = (
            model_info.openai_apply_patch_tool_type if native_apply_patch else None
        )

        # Cognis uses controller-managed `search_tools` as the canonical
        # model-facing discovery interface. Native provider discovery may still
        # exist internally in the future, but it should not change the tool name
        # or prompt contract presented to the model.
        discovery_mode = (
            ToolDiscoveryMode.CONTROLLER_SEARCH if allow_tool_search else ToolDiscoveryMode.NONE
        )

        return ToolExposureContract(
            llm_api=llm_api,
            discovery_mode=discovery_mode,
            native_apply_patch=native_apply_patch,
            native_apply_patch_reason=native_apply_patch_reason,
            native_apply_patch_tool_type=native_apply_patch_tool_type,
            anthropic_defer_loading=_anthropic_defer_loading_enabled(
                provider,
                model_id,
                model_info,
                broken=provider_id is not None
                and self._capability_is_broken(
                    self._anthropic_defer_loading_broken_keys,
                    (provider_id, model_id),
                    marker_name="anthropic_defer_loading",
                    provider=provider,
                ),
            ),
        )

    def invalidate_json_mode_cache_for_provider(self, provider_id: str) -> None:
        """Clear any JSON-mode broken-key entries matching the given provider.

        Called after a provider config update so that admins can force a
        re-probe without restarting the controller (e.g. after fixing a proxy
        endpoint or flipping use_responses_api).
        """

        self._json_mode_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._json_mode_broken_keys.items()
                if key[0] != provider_id
            }
        )
        self._json_response_format_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._json_response_format_broken_keys.items()
                if key[0] != provider_id
            }
        )
        self._plain_text_responses_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._plain_text_responses_broken_keys.items()
                if key[0] != provider_id
            }
        )

    def apply_tool_exposure_runtime_fallbacks(
        self,
        model_info: ModelInfo,
        *,
        provider_id: str | None,
        model_id: str,
    ) -> ModelInfo:
        """Mask native OpenAI tool-search capabilities for cached-broken models."""

        if provider_id is None or not self._capability_is_broken(
            self._openai_tool_search_broken_keys,
            (provider_id, model_id),
            marker_name="openai_tool_search",
        ):
            update: dict[str, Any] = {}
        else:
            if model_info.supports_openai_allowed_tools:
                logger.debug(
                    "Using cached controller fallback for OpenAI Responses tool discovery",
                    extra={
                        "extra_data": {
                            "provider_id": provider_id,
                            "model": model_id,
                        }
                    },
                )
                update = {
                    "supports_openai_allowed_tools": False,
                    "supports_openai_namespace_tools": False,
                }
            else:
                update = {}
        if provider_id is not None and self._capability_is_broken(
            self._anthropic_defer_loading_broken_keys,
            (provider_id, model_id),
            marker_name="anthropic_defer_loading",
        ):
            update["supports_defer_loading"] = False
        return model_info.model_copy(update=update) if update else model_info

    def invalidate_openai_tool_search_cache_for_provider(self, provider_id: str) -> None:
        """Clear any cached native-tool-search downgrade entries for a provider."""

        self._openai_tool_search_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._openai_tool_search_broken_keys.items()
                if key[0] != provider_id
            }
        )

    def invalidate_runtime_capability_cache_for_provider(self, provider_id: str) -> None:
        """Clear cached per-provider runtime capability downgrades."""

        self.invalidate_json_mode_cache_for_provider(provider_id)
        self.invalidate_openai_tool_search_cache_for_provider(provider_id)
        self.invalidate_hosted_instruction_drift_cache_for_provider(provider_id)
        self.invalidate_prompt_cache_key_broken_for_provider(provider_id)

    def invalidate_prompt_cache_key_broken_for_provider(self, provider_id: str) -> None:
        """Clear cached prompt-cache-key rejection entries for a provider."""

        self._prompt_cache_key_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._prompt_cache_key_broken_keys.items()
                if key[0] != provider_id
            }
        )
        self._reasoning_summary_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._reasoning_summary_broken_keys.items()
                if key[0] != provider_id
            }
        )
        self._anthropic_defer_loading_broken_keys = _CapabilityMarkers(
            {
                key: value
                for key, value in self._anthropic_defer_loading_broken_keys.items()
                if key[0] != provider_id
            }
        )

    def _mark_prompt_cache_key_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._prompt_cache_key_broken_keys,
            key,
            marker_name="prompt_cache_key",
            provider=provider,
        )
        LLM_PROMPT_CACHE_KEY_REJECTED_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Prompt cache key rejected by backend; disabling for this provider/model",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def invalidate_hosted_instruction_drift_cache_for_provider(self, provider_id: str) -> None:
        """Clear cached hosted-instruction drift diagnostics for a provider."""

        self._hosted_instruction_drift_keys = {
            key: value
            for key, value in self._hosted_instruction_drift_keys.items()
            if key[0] != provider_id
        }

    def has_hosted_instruction_drift(self, provider_id: str, model_id: str) -> bool:
        """Return whether a provider/model has reported hosted instruction drift."""

        return (provider_id, model_id) in self._hosted_instruction_drift_keys

    def hosted_instruction_drift_reason(self, provider_id: str, model_id: str) -> str | None:
        """Return the cached hosted-instruction drift reason when present."""

        return self._hosted_instruction_drift_keys.get((provider_id, model_id))

    def _maybe_note_hosted_instruction_drift(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        sent_instructions: str | None,
        response_instructions: Any,
    ) -> None:
        if provider is None or not isinstance(response_instructions, str):
            return
        normalized_response = response_instructions.strip()
        if not normalized_response:
            return
        normalized_sent = sent_instructions.strip() if isinstance(sent_instructions, str) else None
        reason: str | None = None
        if normalized_sent is not None and normalized_response != normalized_sent:
            reason = "server_returned_different_instructions"
        elif normalized_sent is None and "codex" in normalized_response.lower():
            reason = "server_injected_hosted_instructions"
        if reason is None:
            return
        key = (provider.provider_id, resolved_model)
        if key in self._hosted_instruction_drift_keys:
            return
        self._hosted_instruction_drift_keys[key] = reason
        logger.warning(
            "Responses API reported hosted instructions that differ from Cognis instructions",
            extra={
                "extra_data": {
                    "provider_id": provider.provider_id,
                    "model": resolved_model,
                    "reason": reason,
                }
            },
        )

    def _mark_openai_tool_search_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._openai_tool_search_broken_keys,
            key,
            marker_name="openai_tool_search",
            provider=provider,
        )
        LLM_OPENAI_TOOL_SEARCH_FALLBACK_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Marking model as broken for native OpenAI Responses tool search; "
                "future requests will use controller fallback until provider config changes",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _mark_anthropic_defer_loading_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._anthropic_defer_loading_broken_keys,
            key,
            marker_name="anthropic_defer_loading",
            provider=provider,
        )
        LLM_ANTHROPIC_DEFER_LOADING_REJECTED_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Anthropic defer_loading/tool-search beta rejected by backend; disabling "
                "temporarily for this provider/model",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _mark_json_mode_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._json_mode_broken_keys,
            key,
            marker_name="json_mode",
            provider=provider,
        )
        LLM_JSON_MODE_TRANSPORT_FLIP_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Marking model as broken for JSON mode (response_format); "
                "future JSON-mode calls will bypass Responses API / response_format",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _mark_json_response_format_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._json_response_format_broken_keys,
            key,
            marker_name="json_response_format",
            provider=provider,
        )
        LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Marking model as broken for response_format JSON mode; "
                "future JSON-mode calls will use plain chat text",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _mark_plain_text_responses_broken(
        self,
        provider: LLMProviderRow | None,
        resolved_model: str,
        *,
        reason: str,
    ) -> None:
        if provider is None:
            return
        key = (provider.provider_id, resolved_model)
        newly_marked = self._mark_capability_broken(
            self._plain_text_responses_broken_keys,
            key,
            marker_name="plain_text_responses",
            provider=provider,
        )
        LLM_TEXT_TRANSPORT_FLIP_TOTAL.labels(
            provider_id=provider.provider_id,
            model=resolved_model,
            reason=reason,
        ).inc()
        if newly_marked:
            logger.warning(
                "Marking model as broken for plain-text Responses generation; "
                "future no-tool text calls will use chat completions",
                extra={
                    "extra_data": {
                        "provider_id": provider.provider_id,
                        "model": resolved_model,
                        "reason": reason,
                    }
                },
            )

    def _autofill_max_tokens(
        self,
        request_kwargs: dict[str, Any],
        *,
        model_info: ModelInfo,
        provider: LLMProviderRow | None,
        resolved_model: str,
    ) -> None:
        """Fill request_kwargs["max_tokens"] with the model's max output budget
        when the caller hasn't specified any output cap.

        This matches the pattern used by Claude Code / Codex / aider: "use the
        model's full advertised output budget by default". For providers that
        require max_tokens (Anthropic) or strictly validate JSON output (Groq),
        this avoids truncation and unhelpful provider defaults. Models stop
        generating when they're done, so raising the ceiling does not increase
        token cost.

        Respects caller overrides (max_tokens / max_completion_tokens /
        max_output_tokens) and the optional per-provider config.max_tokens_ceiling
        override for tier-capped endpoints.
        """

        if (
            "max_tokens" in request_kwargs
            or "max_completion_tokens" in request_kwargs
            or "max_output_tokens" in request_kwargs
        ):
            supplied = (
                request_kwargs.get("max_tokens")
                or request_kwargs.get("max_completion_tokens")
                or request_kwargs.get("max_output_tokens")
            )
            learned_max = int(model_info.max_output_tokens or 0)
            if (
                isinstance(supplied, int | float)
                and learned_max > 0
                and int(supplied) < learned_max
            ):
                LLM_MAX_TOKENS_CAPPED_TOTAL.labels(
                    provider_id=provider.provider_id if provider is not None else "unknown",
                    model=resolved_model,
                ).inc()
                logger.warning(
                    "llm: caller output token cap is below model max output tokens",
                    extra={
                        "extra_data": {
                            "provider_id": provider.provider_id if provider is not None else None,
                            "model": resolved_model,
                            "supplied_max_tokens": int(supplied),
                            "model_max_output_tokens": learned_max,
                        }
                    },
                )
            return
        if _looks_like_chatgpt_oauth_provider(provider):
            return
        learned_max = int(model_info.max_output_tokens or 0)
        if learned_max > DEFAULT_MODEL_INFO.max_output_tokens:
            auto_max = learned_max
        else:
            auto_max = JSON_MODE_AUTOFILL_FALLBACK_MAX_TOKENS
        if provider is not None:
            raw_ceiling = dict(provider.config).get("max_tokens_ceiling")
            if isinstance(raw_ceiling, int) and raw_ceiling > 0:
                auto_max = min(auto_max, raw_ceiling)
            elif isinstance(raw_ceiling, str):
                try:
                    parsed_ceiling = int(raw_ceiling)
                except ValueError:
                    parsed_ceiling = 0
                if parsed_ceiling > 0:
                    auto_max = min(auto_max, parsed_ceiling)
        if auto_max <= 0:
            return
        request_kwargs["max_tokens"] = auto_max
        LLM_MAX_TOKENS_AUTOFILLED_TOTAL.labels(
            provider_id=provider.provider_id if provider is not None else "unknown",
            model=resolved_model,
        ).inc()

    async def _json_mode_fallback_chat_completions(
        self,
        *,
        prefixed_model: str,
        prepared_messages: list[dict[str, Any]],
        request_kwargs: dict[str, Any],
        retry_count: int,
        provider: LLMProviderRow | None,
        resolved_model: str,
        reason: str,
        strip_response_format: bool,
    ) -> dict[str, Any]:
        """Retry a failed JSON-mode call via ``litellm.acompletion``.

        Called from ``generate()`` when either the Responses API returned an
        empty payload (``strip_response_format=False`` — keep JSON mode but
        switch transport) or a provider JSON validator rejected the generation
        with a 400 (``strip_response_format=True`` — drop response_format so
        the server stops validating server-side JSON).

        Always marks the (provider, model) pair as broken for JSON mode so
        subsequent calls route directly to chat-completions without the first
        wasted Responses attempt.
        """

        from cognis.providers.llm.retry import with_llm_retry

        self._mark_json_mode_broken(provider, resolved_model, reason=reason)
        fallback_kwargs = dict(request_kwargs)
        fallback_kwargs.pop("cognis_llm_api", None)
        if strip_response_format:
            fallback_kwargs.pop("response_format", None)
        response = await with_llm_retry(
            self._litellm_transport.completion,
            model=prefixed_model,
            messages=prepared_messages,
            stream=False,
            max_retries=retry_count,
            operation=f"generate.json_fallback({prefixed_model})",
            **fallback_kwargs,
        )
        return cast(dict[str, Any], response.model_dump())

    def _record_reasoning_metrics(self, prepared: PreparedReasoningConfig) -> None:
        if prepared.effective_effort:
            LLM_REASONING_EFFORT_USED_TOTAL.labels(
                family=prepared.family,
                level=prepared.effective_effort,
            ).inc()
        if prepared.stripped_params:
            for _ in prepared.stripped_params:
                LLM_SAMPLING_PARAMS_STRIPPED_TOTAL.labels(reason="reasoning_model").inc()
        if prepared.translated_max_tokens:
            LLM_MAX_TOKENS_TRANSLATED_TOTAL.inc()

    def _prepare_generation_request_kwargs(
        self,
        request_kwargs: dict[str, Any],
        *,
        model_id: str,
        provider: LLMProviderRow | None,
        model_info: ModelInfo,
    ) -> dict[str, Any]:
        request_kwargs = dict(request_kwargs)
        request_kwargs.pop("max_retries", None)
        request_kwargs.pop("num_retries", None)
        if model_info.openai_apply_patch_tool_type:
            request_kwargs.setdefault(
                "cognis_openai_apply_patch_tool_type",
                model_info.openai_apply_patch_tool_type,
            )
        prepared = apply_reasoning_config(
            request_kwargs,
            model_id=model_id,
            provider_preset=(
                str(dict(provider.config).get("preset", "")).lower() if provider else ""
            ),
            model_info=model_info,
        )
        self._record_reasoning_metrics(prepared)
        return prepared.request_kwargs

    async def generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        cache_breakpoint_index: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from cognis.providers.llm.retry import with_llm_retry

        cognis_session_id = cast(str | None, kwargs.pop("cognis_session_id", None))
        explicit_provider_id = cast(str | None, kwargs.pop("provider_id", None))
        acting_user_email = cast(str | None, kwargs.pop("acting_user_email", None))
        resolved_model, provider = await self._resolve_model_target(
            model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
            acting_user_email=acting_user_email,
        )
        if provider is None:
            raise ValueError(f"No LLM provider found for model {resolved_model!r}")
        if _looks_like_chatgpt_oauth_provider(provider):
            self._ensure_controller_side_oauth_provider(provider)

        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        model_info = await self.get_model_info(
            resolved_model,
            provider_id=provider.provider_id if provider is not None else None,
            acting_user_email=acting_user_email,
        )
        if _looks_like_chatgpt_oauth_provider(provider):
            _register_litellm_chatgpt_model_info(
                resolved_model=resolved_model,
                prefixed_model=prefixed_model,
                model_info=model_info,
            )
        request_kwargs = _merge_request_kwargs(
            await self._resolve_provider_kwargs(provider), kwargs
        )
        existing_reasoning = normalize_reasoning_level(
            request_kwargs.get("reasoning_effort")
            if isinstance(request_kwargs.get("reasoning_effort"), str)
            else None
        )
        if (
            model is None
            and explicit_provider_id is None
            and existing_reasoning in {None, "default"}
        ):
            routed_reasoning = await self._get_route_reasoning_effort(
                task_type, acting_user_email=acting_user_email
            )
            if routed_reasoning is not None:
                request_kwargs["reasoning_effort"] = routed_reasoning
        retry_count = request_kwargs.pop("max_retries", None)
        if retry_count is None:
            retry_count = request_kwargs.pop("num_retries", None)
        # Auto-fill max_tokens before reasoning translation so that reasoning
        # models pick up the translated max_completion_tokens.
        self._autofill_max_tokens(
            request_kwargs,
            model_info=model_info,
            provider=provider,
            resolved_model=resolved_model,
        )
        request_kwargs = self._prepare_generation_request_kwargs(
            request_kwargs,
            model_id=resolved_model,
            provider=provider,
            model_info=model_info,
        )
        prepared_messages = _apply_message_cache_hints(
            messages, resolved_model, model_info, cache_breakpoint_index
        )
        explicit_llm_api = str(request_kwargs.pop("cognis_llm_api", "") or "").strip().lower()
        wants_json_response = "response_format" in request_kwargs
        cache_key = (provider.provider_id, resolved_model)
        response_format_cache_broken = self._capability_is_broken(
            self._json_response_format_broken_keys,
            cache_key,
            marker_name="json_response_format",
            provider=provider,
        )
        if wants_json_response and response_format_cache_broken:
            request_kwargs = dict(request_kwargs)
            request_kwargs.pop("response_format", None)
            LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
                provider_id=provider.provider_id,
                model=resolved_model,
                reason="cached_response_format_broken",
            ).inc()

        use_responses_api = self._should_use_responses_api(
            resolved_model,
            model_info,
            provider,
            is_json_mode_request=wants_json_response,
        )
        if explicit_llm_api == "responses":
            use_responses_api = True
        elif explicit_llm_api in {"chat_completions", "completion", "completions"}:
            use_responses_api = False
        elif (
            not wants_json_response
            and not request_kwargs.get("tools")
            and self._capability_is_broken(
                self._plain_text_responses_broken_keys,
                cache_key,
                marker_name="plain_text_responses",
                provider=provider,
            )
        ):
            use_responses_api = False
            LLM_TEXT_TRANSPORT_FLIP_TOTAL.labels(
                provider_id=provider.provider_id,
                model=resolved_model,
                reason="cached_broken",
            ).inc()

        if use_responses_api:
            request_kwargs = dict(request_kwargs)
            request_kwargs["cognis_llm_api"] = "responses"
            request_kwargs = _apply_chatgpt_affinity_headers(
                request_kwargs,
                provider=provider,
                session_id=cognis_session_id,
            )
            # store: omit for ChatGPT (backend forces False); set for all others.
            if not _looks_like_chatgpt_oauth_provider(provider) and "store" not in request_kwargs:
                configured_store = dict(provider.config or {}).get("responses_store")
                request_kwargs["store"] = (
                    configured_store if isinstance(configured_store, bool) else False
                )
        logger.debug(
            "LLM generate",
            extra={
                "extra_data": {
                    "model": prefixed_model,
                    "task_type": task_type,
                    "llm_api": "responses" if use_responses_api else "chat_completions",
                    "tool_count": len(request_kwargs.get("tools") or []),
                    "extra_header_keys": sorted((request_kwargs.get("extra_headers") or {}).keys()),
                }
            },
        )
        retry_count_int = int(retry_count) if isinstance(retry_count, int) else 3

        async def _generate_chat(chat_kwargs: dict[str, Any]) -> dict[str, Any]:
            chat_kwargs = dict(chat_kwargs)
            chat_kwargs.pop("cognis_llm_api", None)
            if self._should_route_to_executor(provider):
                if isinstance(retry_count, int):
                    chat_kwargs["max_retries"] = retry_count
                return await self._executor_generate(
                    prefixed_model,
                    prepared_messages,
                    provider,
                    request_kwargs=chat_kwargs,
                )
            try:
                response = await with_llm_retry(
                    self._litellm_transport.completion,
                    model=prefixed_model,
                    messages=prepared_messages,
                    stream=False,
                    max_retries=retry_count_int,
                    operation=f"generate({prefixed_model})",
                    **chat_kwargs,
                )
            except Exception as exc:
                _raise_context_overflow_if_detected(
                    exc,
                    provider=provider,
                    resolved_model=resolved_model,
                )
                anthropic_defer_reason = _anthropic_defer_loading_rejection_reason(exc, chat_kwargs)
                if anthropic_defer_reason is not None:
                    self._mark_anthropic_defer_loading_broken(
                        provider,
                        resolved_model,
                        reason=anthropic_defer_reason,
                    )
                raise
            dumped = response.model_dump()
            return cast(dict[str, Any], dumped)

        async def _generate_responses(responses_source_kwargs: dict[str, Any]) -> dict[str, Any]:
            responses_source_kwargs = dict(responses_source_kwargs)
            responses_source_kwargs["cognis_llm_api"] = "responses"
            if self._should_route_to_executor(provider):
                if isinstance(retry_count, int):
                    responses_source_kwargs["max_retries"] = retry_count
                return await self._executor_generate(
                    prefixed_model,
                    prepared_messages,
                    provider,
                    request_kwargs=responses_source_kwargs,
                )
            if _uses_direct_codex_transport(provider) and cache_breakpoint_index is None:
                responses_instructions, responses_input_messages = (
                    split_system_messages_for_responses(prepared_messages)
                )
            else:
                responses_instructions, responses_input_messages = split_messages_for_responses(
                    prepared_messages, cache_breakpoint_index
                )
            responses_kwargs = responses_request_kwargs(
                responses_source_kwargs,
                default_reasoning_summary=self._default_reasoning_summary(
                    provider=provider,
                    resolved_model=resolved_model,
                    model_info=model_info,
                ),
                default_text_verbosity=_default_text_verbosity(
                    provider=provider,
                    resolved_model=resolved_model,
                    model_info=model_info,
                ),
                include_encrypted_reasoning=bool(getattr(model_info, "supports_reasoning", False)),
            )
            if responses_instructions is not None:
                responses_kwargs["instructions"] = responses_instructions
            responses_kwargs = _apply_responses_request_defaults(
                responses_kwargs,
                provider=provider,
                resolved_model=resolved_model,
                instructions=responses_instructions,
                prompt_cache_key_broken_keys=self._active_capability_keys(
                    self._prompt_cache_key_broken_keys,
                    marker_name="prompt_cache_key",
                    provider=provider,
                ),
            )
            responses_input = messages_to_responses_input(responses_input_messages)
            if _responses_request_wants_json_object(responses_kwargs):
                responses_input = _ensure_responses_json_input_marker(responses_input)
            transport = await self._responses_transport(provider)
            transport_model = (
                resolved_model if _uses_direct_codex_transport(provider) else prefixed_model
            )
            use_streaming_generate = _uses_direct_codex_transport(provider)
            try:
                response = await _call_responses_generate(
                    transport,
                    model=transport_model,
                    input=responses_input,
                    stream=use_streaming_generate,
                    max_retries=retry_count_int,
                    operation=f"generate.responses({prefixed_model})",
                    **responses_kwargs,
                )
            except Exception as exc:
                if reasoning_summary_rejected(classify_llm_exception(exc)):
                    self._mark_reasoning_summary_broken(
                        provider, resolved_model, reason="backend_rejected"
                    )
                    response = await _call_responses_generate(
                        transport,
                        model=transport_model,
                        input=responses_input,
                        stream=use_streaming_generate,
                        max_retries=retry_count_int,
                        operation=f"generate.responses.no_reasoning_summary({prefixed_model})",
                        **_without_reasoning_summary(responses_kwargs),
                    )
                elif _is_prompt_cache_key_rejected(exc):
                    self._mark_prompt_cache_key_broken(
                        provider, resolved_model, reason="backend_rejected"
                    )
                    # Retry once without cache params.
                    retry_kwargs = {
                        k: v
                        for k, v in responses_kwargs.items()
                        if k not in {"prompt_cache_key", "prompt_cache_retention"}
                    }
                    try:
                        response = await _call_responses_generate(
                            transport,
                            model=transport_model,
                            input=responses_input,
                            stream=use_streaming_generate,
                            max_retries=retry_count_int,
                            operation=f"generate.responses.no_cache_key({prefixed_model})",
                            **retry_kwargs,
                        )
                    except Exception as retry_exc:
                        _raise_context_overflow_if_detected(
                            retry_exc,
                            provider=provider,
                            resolved_model=resolved_model,
                        )
                        raise
                else:
                    _raise_context_overflow_if_detected(
                        exc,
                        provider=provider,
                        resolved_model=resolved_model,
                    )
                    raise

            raw_response_dict = _model_dump(response)
            self._maybe_note_hosted_instruction_drift(
                provider,
                resolved_model,
                sent_instructions=responses_instructions,
                response_instructions=raw_response_dict.get("instructions"),
            )
            if use_streaming_generate:
                return raw_response_dict
            return responses_to_chat_response(raw_response_dict)

        async def _generate_json() -> dict[str, Any]:
            last_reason = "unknown"
            if use_responses_api:
                try:
                    response_dict = await _generate_responses(request_kwargs)
                    normalized, reason = _normalized_json_mode_response(
                        response_dict, label=task_type
                    )
                    if normalized is not None:
                        if reason == "normalized":
                            LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
                                provider_id=provider.provider_id,
                                model=resolved_model,
                                reason="responses_normalized",
                            ).inc()
                        return normalized
                    last_reason = reason
                    self._mark_json_mode_broken(
                        provider, resolved_model, reason=f"responses_{reason}"
                    )
                except Exception as exc:
                    openai_tool_search_reason = _openai_tool_search_bad_request_reason(
                        exc, request_kwargs
                    )
                    if openai_tool_search_reason is not None:
                        self._mark_openai_tool_search_broken(
                            provider,
                            resolved_model,
                            reason=openai_tool_search_reason,
                        )
                    if _is_json_validator_bad_request(exc):
                        last_reason = "bad_request_json_validator"
                        self._mark_json_mode_broken(provider, resolved_model, reason=last_reason)
                        self._mark_json_response_format_broken(
                            provider, resolved_model, reason=last_reason
                        )
                    else:
                        raise

            chat_kwargs = dict(request_kwargs)
            chat_kwargs.pop("cognis_llm_api", None)
            if response_format_cache_broken:
                chat_kwargs.pop("response_format", None)
            try:
                response_dict = await _generate_chat(chat_kwargs)
            except Exception as exc:
                if "response_format" in chat_kwargs and _is_json_validator_bad_request(exc):
                    last_reason = "bad_request_json_validator"
                    self._mark_json_response_format_broken(
                        provider, resolved_model, reason=last_reason
                    )
                else:
                    raise
            else:
                normalized, reason = _normalized_json_mode_response(response_dict, label=task_type)
                if normalized is not None:
                    LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
                        provider_id=provider.provider_id,
                        model=resolved_model,
                        reason="chat_normalized",
                    ).inc()
                    return normalized
                last_reason = reason
                if "response_format" in chat_kwargs:
                    self._mark_json_response_format_broken(
                        provider, resolved_model, reason=f"chat_{reason}"
                    )

            plain_kwargs = dict(request_kwargs)
            plain_kwargs.pop("cognis_llm_api", None)
            plain_kwargs.pop("response_format", None)
            response_dict = await _generate_chat(plain_kwargs)
            normalized, reason = _normalized_json_mode_response(response_dict, label=task_type)
            if normalized is not None:
                LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
                    provider_id=provider.provider_id,
                    model=resolved_model,
                    reason="plain_chat_normalized",
                ).inc()
                return normalized
            last_reason = reason or last_reason
            LLM_JSON_MODE_NORMALIZATION_TOTAL.labels(
                provider_id=provider.provider_id,
                model=resolved_model,
                reason=f"failed_{last_reason}",
            ).inc()
            raise JSONModeGenerationError(
                provider_id=provider.provider_id,
                model_id=resolved_model,
                reason=last_reason,
            )

        oauth_context = (
            contextlib.nullcontext()
            if use_responses_api and _uses_direct_codex_transport(provider)
            else self._provider_oauth_token_context(provider)
        )
        async with oauth_context:
            if wants_json_response:
                return await _generate_json()

            if use_responses_api:
                response_dict = await _generate_responses(request_kwargs)
                if (
                    _is_empty_json_mode_response(response_dict)
                    and not request_kwargs.get("tools")
                    and not _uses_direct_codex_transport(provider)
                ):
                    self._mark_plain_text_responses_broken(
                        provider, resolved_model, reason="empty_responses_detected"
                    )
                    chat_kwargs = dict(request_kwargs)
                    chat_kwargs.pop("cognis_llm_api", None)
                    response_dict = await _generate_chat(chat_kwargs)
            else:
                response_dict = await _generate_chat(request_kwargs)

        # Diagnostic: log response structure for debugging reasoning model issues
        choices = response_dict.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                reasoning_content = msg.get("reasoning_content")
                logger.debug(
                    "LLM response structure",
                    extra={
                        "extra_data": {
                            "model": prefixed_model,
                            "task_type": task_type,
                            "has_content": isinstance(content, str) and bool(content.strip()),
                            "content_length": len(content) if isinstance(content, str) else 0,
                            "has_reasoning_content": isinstance(reasoning_content, str)
                            and bool(reasoning_content.strip()),
                            "reasoning_content_length": len(reasoning_content)
                            if isinstance(reasoning_content, str)
                            else 0,
                        }
                    },
                )

        return cast(dict[str, Any], response_dict)

    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        cache_breakpoint_index: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from cognis.providers.llm.retry import with_llm_retry

        pre_request_started_at = monotonic()
        phase_started_at = pre_request_started_at
        llm_request_id = cast(str | None, kwargs.pop("cognis_llm_request_id", None))
        if not llm_request_id:
            llm_request_id = f"llmr_{uuid.uuid4().hex[:12]}"
        cognis_session_id = cast(str | None, kwargs.pop("cognis_session_id", None))
        explicit_provider_id = cast(str | None, kwargs.pop("provider_id", None))
        acting_user_email = cast(str | None, kwargs.pop("acting_user_email", None))
        resolved_model, provider = await self._resolve_model_target(
            model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
            acting_user_email=acting_user_email,
        )
        if provider is None:
            raise ValueError(f"No LLM provider found for model {resolved_model!r}")
        provider_id = provider.provider_id
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api="unknown",
            location="controller",
            phase="resolve_model_target",
            duration=monotonic() - phase_started_at,
        )
        if _looks_like_chatgpt_oauth_provider(provider):
            self._ensure_controller_side_oauth_provider(provider)

        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        phase_started_at = monotonic()
        model_info = await self.get_model_info(
            resolved_model,
            provider_id=provider_id,
            acting_user_email=acting_user_email,
        )
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api="unknown",
            location="controller",
            phase="get_model_info",
            duration=monotonic() - phase_started_at,
        )
        if _looks_like_chatgpt_oauth_provider(provider):
            _register_litellm_chatgpt_model_info(
                resolved_model=resolved_model,
                prefixed_model=prefixed_model,
                model_info=model_info,
            )
        phase_started_at = monotonic()
        request_kwargs = _merge_request_kwargs(
            await self._resolve_provider_kwargs(provider), kwargs
        )
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api="unknown",
            location="controller",
            phase="resolve_provider_kwargs",
            duration=monotonic() - phase_started_at,
        )
        existing_reasoning = normalize_reasoning_level(
            request_kwargs.get("reasoning_effort")
            if isinstance(request_kwargs.get("reasoning_effort"), str)
            else None
        )
        if (
            model is None
            and explicit_provider_id is None
            and existing_reasoning in {None, "default"}
        ):
            phase_started_at = monotonic()
            routed_reasoning = await self._get_route_reasoning_effort(
                task_type, acting_user_email=acting_user_email
            )
            _observe_provider_phase(
                llm_request_id=llm_request_id,
                provider_id=provider_id,
                model=resolved_model,
                llm_api="unknown",
                location="controller",
                phase="route_reasoning_effort",
                duration=monotonic() - phase_started_at,
            )
            if routed_reasoning is not None:
                request_kwargs["reasoning_effort"] = routed_reasoning
        retry_count = request_kwargs.pop("max_retries", None)
        if retry_count is None:
            retry_count = request_kwargs.pop("num_retries", None)
        self._autofill_max_tokens(
            request_kwargs,
            model_info=model_info,
            provider=provider,
            resolved_model=resolved_model,
        )
        phase_started_at = monotonic()
        request_kwargs = self._prepare_generation_request_kwargs(
            request_kwargs,
            model_id=resolved_model,
            provider=provider,
            model_info=model_info,
        )
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api="unknown",
            location="controller",
            phase="prepare_generation_kwargs",
            duration=monotonic() - phase_started_at,
        )
        phase_started_at = monotonic()
        prepared_messages = _apply_message_cache_hints(
            messages, resolved_model, model_info, cache_breakpoint_index
        )
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api="unknown",
            location="controller",
            phase="apply_cache_hints",
            duration=monotonic() - phase_started_at,
        )
        explicit_llm_api = str(request_kwargs.pop("cognis_llm_api", "") or "").strip().lower()
        is_json_mode_request = "response_format" in request_kwargs
        use_responses_api = self._should_use_responses_api(
            resolved_model,
            model_info,
            provider,
            is_json_mode_request=is_json_mode_request,
        )
        if explicit_llm_api == "responses":
            use_responses_api = True
        elif explicit_llm_api in {"chat_completions", "completion", "completions"}:
            use_responses_api = False
        if use_responses_api:
            request_kwargs = dict(request_kwargs)
            request_kwargs["cognis_llm_api"] = "responses"
            request_kwargs = _apply_chatgpt_affinity_headers(
                request_kwargs,
                provider=provider,
                session_id=cognis_session_id,
            )
            # store: omit for ChatGPT (backend forces False); set for all others.
            if not _looks_like_chatgpt_oauth_provider(provider) and "store" not in request_kwargs:
                configured_store = dict(provider.config or {}).get("responses_store")
                request_kwargs["store"] = (
                    configured_store if isinstance(configured_store, bool) else False
                )
        request_diagnostics = _request_payload_diagnostics(
            prepared_messages,
            request_kwargs,
            cache_breakpoint_index=cache_breakpoint_index,
            provider_preset=_provider_preset(provider) or None,
            diagnostics_stage="pre_responses_defaults"
            if use_responses_api
            else "chat_completions_request",
        )
        llm_api_label = "responses" if use_responses_api else "chat_completions"
        _observe_provider_phase(
            llm_request_id=llm_request_id,
            provider_id=provider_id,
            model=resolved_model,
            llm_api=llm_api_label,
            location="controller",
            phase="pre_request_total",
            duration=monotonic() - pre_request_started_at,
            extra_data=request_diagnostics,
        )
        if self._should_route_to_executor(provider):
            if isinstance(retry_count, int):
                request_kwargs["max_retries"] = retry_count
            async with _observe_llm_stream_request(
                llm_request_id=llm_request_id,
                provider_id=provider_id,
                model=resolved_model,
                llm_api=llm_api_label,
                location="executor",
                request_diagnostics=request_diagnostics,
            ) as observe_chunk:
                async for chunk in self._executor_stream_generate(
                    prefixed_model,
                    prepared_messages,
                    provider,
                    request_kwargs=request_kwargs,
                ):
                    observe_chunk(chunk)
                    yield chunk
            return
        logger.debug(
            "LLM stream_generate",
            extra={
                "extra_data": {
                    "model": prefixed_model,
                    "task_type": task_type,
                    "llm_api": llm_api_label,
                    "tool_count": len(request_kwargs.get("tools") or []),
                    "extra_header_keys": sorted((request_kwargs.get("extra_headers") or {}).keys()),
                    "llm_request_id": llm_request_id,
                }
            },
        )
        if use_responses_api:
            oauth_context = (
                contextlib.nullcontext()
                if _uses_direct_codex_transport(provider)
                else self._provider_oauth_token_context(provider)
            )
            async with oauth_context:
                request_build_started_at = monotonic()
                responses_instructions, responses_input_messages = split_messages_for_responses(
                    prepared_messages, cache_breakpoint_index
                )
                responses_input = messages_to_responses_input(responses_input_messages)
                responses_kwargs = responses_request_kwargs(
                    request_kwargs,
                    default_reasoning_summary=self._default_reasoning_summary(
                        provider=provider,
                        resolved_model=resolved_model,
                        model_info=model_info,
                    ),
                    default_text_verbosity=_default_text_verbosity(
                        provider=provider,
                        resolved_model=resolved_model,
                        model_info=model_info,
                    ),
                    include_encrypted_reasoning=bool(
                        getattr(model_info, "supports_reasoning", False)
                    ),
                )
                if _responses_request_wants_json_object(responses_kwargs):
                    responses_input = _ensure_responses_json_input_marker(responses_input)
                if responses_instructions is not None:
                    responses_kwargs["instructions"] = responses_instructions
                active_prompt_cache_key_broken_keys = self._active_capability_keys(
                    self._prompt_cache_key_broken_keys,
                    marker_name="prompt_cache_key",
                    provider=provider,
                )
                responses_kwargs = _apply_responses_request_defaults(
                    responses_kwargs,
                    provider=provider,
                    resolved_model=resolved_model,
                    instructions=responses_instructions,
                    prompt_cache_key_broken_keys=active_prompt_cache_key_broken_keys,
                )
                responses_input_bytes, responses_input_hash = _payload_size_hash(responses_input)
                responses_instructions_bytes, responses_instructions_hash = _payload_size_hash(
                    responses_instructions
                )
                prompt_cache_key = responses_kwargs.get("prompt_cache_key")
                prompt_cache_retention = responses_kwargs.get("prompt_cache_retention")
                prompt_cache_key_hash = (
                    _payload_hash(prompt_cache_key)
                    if isinstance(prompt_cache_key, str) and prompt_cache_key
                    else None
                )
                cacheable_prefix_hash = _payload_hash(
                    {
                        "instructions": responses_instructions_hash,
                        "prompt_cache_key": prompt_cache_key_hash,
                        "tools": request_diagnostics.get("tool_schema_hash"),
                    }
                )
                request_diagnostics.update(
                    {
                        "responses_transport": "direct_codex"
                        if _uses_direct_codex_transport(provider)
                        else "litellm_http_sse",
                        "request_diagnostics_stage": "responses_build",
                        "responses_input_item_count": len(responses_input),
                        "responses_input_bytes": responses_input_bytes,
                        "responses_input_hash": responses_input_hash,
                        "responses_instructions_bytes": responses_instructions_bytes,
                        "responses_instructions_hash": responses_instructions_hash,
                        "responses_store_mode": "true"
                        if responses_kwargs.get("store") is True
                        else "false"
                        if responses_kwargs.get("store") is False
                        else "omitted/default",
                        "prompt_cache_key_present": prompt_cache_key_hash is not None,
                        "prompt_cache_key_status": _responses_prompt_cache_status(
                            responses_kwargs=responses_kwargs,
                            provider=provider,
                            resolved_model=resolved_model,
                            prompt_cache_key_broken_keys=active_prompt_cache_key_broken_keys,
                        ),
                        "prompt_cache_retention_present": isinstance(prompt_cache_retention, str)
                        and bool(prompt_cache_retention),
                        "cacheable_prefix_bytes_estimate": responses_instructions_bytes
                        + int(request_diagnostics.get("tool_schema_bytes") or 0),
                        "cacheable_prefix_hash": cacheable_prefix_hash,
                    }
                )
                if prompt_cache_key_hash is not None:
                    request_diagnostics["prompt_cache_key_hash"] = prompt_cache_key_hash
                if isinstance(prompt_cache_retention, str) and prompt_cache_retention:
                    request_diagnostics["prompt_cache_retention"] = prompt_cache_retention
                _observe_provider_phase(
                    llm_request_id=llm_request_id,
                    provider_id=provider.provider_id if provider is not None else "default",
                    model=resolved_model,
                    llm_api="responses",
                    location="controller",
                    phase="request_build",
                    duration=monotonic() - request_build_started_at,
                    extra_data=request_diagnostics,
                )
                retry_count_int_stream = int(retry_count) if isinstance(retry_count, int) else 3
                transport = await self._responses_transport(provider)
                transport_model = (
                    resolved_model if _uses_direct_codex_transport(provider) else prefixed_model
                )
                try:
                    api_call_started_at = monotonic()
                    stream = await with_llm_retry(
                        transport.responses,
                        model=transport_model,
                        input=responses_input,
                        stream=True,
                        max_retries=retry_count_int_stream,
                        operation=f"stream_generate.responses({prefixed_model})",
                        **responses_kwargs,
                    )
                    _observe_provider_phase(
                        llm_request_id=llm_request_id,
                        provider_id=provider.provider_id if provider is not None else "default",
                        model=resolved_model,
                        llm_api="responses",
                        location="controller",
                        phase="stream_open",
                        duration=monotonic() - api_call_started_at,
                    )
                except Exception as exc:
                    if reasoning_summary_rejected(classify_llm_exception(exc)):
                        self._mark_reasoning_summary_broken(
                            provider, resolved_model, reason="backend_rejected"
                        )
                        responses_kwargs = _without_reasoning_summary(responses_kwargs)
                        api_call_started_at = monotonic()
                        stream = await with_llm_retry(
                            transport.responses,
                            model=transport_model,
                            input=responses_input,
                            stream=True,
                            max_retries=retry_count_int_stream,
                            operation=f"stream_generate.responses.no_reasoning_summary({prefixed_model})",
                            **responses_kwargs,
                        )
                        _observe_provider_phase(
                            llm_request_id=llm_request_id,
                            provider_id=provider.provider_id if provider is not None else "default",
                            model=resolved_model,
                            llm_api="responses",
                            location="controller",
                            phase="stream_open_no_reasoning_summary",
                            duration=monotonic() - api_call_started_at,
                        )
                    elif _is_prompt_cache_key_rejected(exc):
                        self._mark_prompt_cache_key_broken(
                            provider, resolved_model, reason="backend_rejected"
                        )
                        # Retry once without cache params.
                        retry_kwargs = {
                            k: v
                            for k, v in responses_kwargs.items()
                            if k not in {"prompt_cache_key", "prompt_cache_retention"}
                        }
                        request_diagnostics["prompt_cache_key_present"] = False
                        request_diagnostics["prompt_cache_retention_present"] = False
                        request_diagnostics["prompt_cache_key_status"] = (
                            "disabled_after_backend_rejection"
                        )
                        request_diagnostics.pop("prompt_cache_key_hash", None)
                        request_diagnostics.pop("prompt_cache_retention", None)
                        api_call_started_at = monotonic()
                        try:
                            stream = await with_llm_retry(
                                transport.responses,
                                model=transport_model,
                                input=responses_input,
                                stream=True,
                                max_retries=retry_count_int_stream,
                                operation=f"stream_generate.responses.no_cache_key({prefixed_model})",
                                **retry_kwargs,
                            )
                        except Exception as retry_exc:
                            _raise_context_overflow_if_detected(
                                retry_exc,
                                provider=provider,
                                resolved_model=resolved_model,
                            )
                            openai_tool_search_reason = _openai_tool_search_bad_request_reason(
                                retry_exc, request_kwargs
                            )
                            if openai_tool_search_reason is not None:
                                self._mark_openai_tool_search_broken(
                                    provider,
                                    resolved_model,
                                    reason=openai_tool_search_reason,
                                )
                                raise OpenAIToolSearchFallbackRequired(
                                    provider_id=provider.provider_id
                                    if provider is not None
                                    else "unknown",
                                    model_id=resolved_model,
                                    reason=openai_tool_search_reason,
                                ) from retry_exc
                            raise
                        _observe_provider_phase(
                            llm_request_id=llm_request_id,
                            provider_id=provider.provider_id if provider is not None else "default",
                            model=resolved_model,
                            llm_api="responses",
                            location="controller",
                            phase="stream_open_no_cache_key",
                            duration=monotonic() - api_call_started_at,
                        )
                    else:
                        _raise_context_overflow_if_detected(
                            exc,
                            provider=provider,
                            resolved_model=resolved_model,
                        )
                        openai_tool_search_reason = _openai_tool_search_bad_request_reason(
                            exc, request_kwargs
                        )
                        if openai_tool_search_reason is not None:
                            self._mark_openai_tool_search_broken(
                                provider,
                                resolved_model,
                                reason=openai_tool_search_reason,
                            )
                            raise OpenAIToolSearchFallbackRequired(
                                provider_id=provider.provider_id
                                if provider is not None
                                else "unknown",
                                model_id=resolved_model,
                                reason=openai_tool_search_reason,
                            ) from exc
                        raise

                async with _observe_llm_stream_request(
                    llm_request_id=llm_request_id,
                    provider_id=provider.provider_id if provider is not None else "default",
                    model=resolved_model,
                    llm_api="responses",
                    location="controller",
                    request_diagnostics=request_diagnostics,
                ) as observe_chunk:
                    first_normalized_chunk_at: float | None = None
                    try:
                        retried_without_reasoning_summary = False
                        while True:
                            retry_stream_without_reasoning_summary = False
                            async for chunk in responses_stream_to_chat_chunks(stream):
                                if (
                                    chunk.get("mid_stream_failure")
                                    and reasoning_summary_rejected(chunk.get("response_error"))
                                    and not retried_without_reasoning_summary
                                ):
                                    retried_without_reasoning_summary = True
                                    retry_stream_without_reasoning_summary = True
                                    self._mark_reasoning_summary_broken(
                                        provider,
                                        resolved_model,
                                        reason="stream_rejected",
                                    )
                                    responses_kwargs = _without_reasoning_summary(responses_kwargs)
                                    break
                                if first_normalized_chunk_at is None:
                                    first_normalized_chunk_at = monotonic()
                                    _observe_provider_phase(
                                        llm_request_id=llm_request_id,
                                        provider_id=provider.provider_id
                                        if provider is not None
                                        else "default",
                                        model=resolved_model,
                                        llm_api="responses",
                                        location="controller",
                                        phase="first_normalized_chunk",
                                        duration=first_normalized_chunk_at - api_call_started_at,
                                    )
                                if "response_instructions" in chunk:
                                    self._maybe_note_hosted_instruction_drift(
                                        provider,
                                        resolved_model,
                                        sent_instructions=responses_instructions,
                                        response_instructions=chunk.pop(
                                            "response_instructions", None
                                        ),
                                    )
                                observe_chunk(chunk)
                                yield chunk
                            if not retry_stream_without_reasoning_summary:
                                break
                            api_call_started_at = monotonic()
                            stream = await with_llm_retry(
                                transport.responses,
                                model=transport_model,
                                input=responses_input,
                                stream=True,
                                max_retries=retry_count_int_stream,
                                operation=f"stream_generate.responses.no_reasoning_summary({prefixed_model})",
                                **responses_kwargs,
                            )
                            _observe_provider_phase(
                                llm_request_id=llm_request_id,
                                provider_id=provider.provider_id
                                if provider is not None
                                else "default",
                                model=resolved_model,
                                llm_api="responses",
                                location="controller",
                                phase="stream_reopen_no_reasoning_summary",
                                duration=monotonic() - api_call_started_at,
                            )
                    except Exception as exc:
                        failure_detail = _exception_detail(exc)
                        logger.warning(
                            "LLM Responses stream failed mid-generation",
                            extra={
                                "extra_data": {
                                    "model": prefixed_model,
                                    "llm_request_id": llm_request_id,
                                    "provider_id": provider.provider_id
                                    if provider is not None
                                    else "default",
                                    "resolved_model": resolved_model,
                                    "request_diagnostics_stage": "responses_stream_failure",
                                    "first_normalized_chunk_seen": first_normalized_chunk_at
                                    is not None,
                                    **request_diagnostics,
                                    **failure_detail,
                                }
                            },
                            exc_info=True,
                        )
                        error_chunk = build_mid_stream_error_chunk(exc)
                        observe_chunk(error_chunk)
                        yield error_chunk
            return
        # Retry pre-stream errors (connection refused, rate limit, etc.)
        # with exponential backoff.  Once the stream is established,
        # mid-stream failures are caught and yielded as error markers.
        async with self._provider_oauth_token_context(provider):
            try:
                api_call_started_at = monotonic()
                stream = await with_llm_retry(
                    self._litellm_transport.completion,
                    model=prefixed_model,
                    messages=prepared_messages,
                    stream=True,
                    max_retries=int(retry_count) if isinstance(retry_count, int) else 3,
                    operation=f"stream_generate({prefixed_model})",
                    **request_kwargs,
                )
                _observe_provider_phase(
                    llm_request_id=llm_request_id,
                    provider_id=provider.provider_id if provider is not None else "default",
                    model=resolved_model,
                    llm_api="chat_completions",
                    location="controller",
                    phase="stream_open",
                    duration=monotonic() - api_call_started_at,
                    extra_data=request_diagnostics,
                )
            except Exception as exc:
                _raise_context_overflow_if_detected(
                    exc,
                    provider=provider,
                    resolved_model=resolved_model,
                )
                anthropic_defer_reason = _anthropic_defer_loading_rejection_reason(
                    exc, request_kwargs
                )
                if anthropic_defer_reason is not None:
                    self._mark_anthropic_defer_loading_broken(
                        provider,
                        resolved_model,
                        reason=anthropic_defer_reason,
                    )
                raise
            async with _observe_llm_stream_request(
                llm_request_id=llm_request_id,
                provider_id=provider.provider_id if provider is not None else "default",
                model=resolved_model,
                llm_api="chat_completions",
                location="controller",
                request_diagnostics=request_diagnostics,
            ) as observe_chunk:
                first_normalized_chunk_at: float | None = None
                try:
                    async for chunk in stream:
                        if first_normalized_chunk_at is None:
                            first_normalized_chunk_at = monotonic()
                            _observe_provider_phase(
                                llm_request_id=llm_request_id,
                                provider_id=provider.provider_id
                                if provider is not None
                                else "default",
                                model=resolved_model,
                                llm_api="chat_completions",
                                location="controller",
                                phase="first_normalized_chunk",
                                duration=first_normalized_chunk_at - api_call_started_at,
                            )
                        chunk_dict = _model_dump(chunk)
                        observe_chunk(chunk_dict)
                        yield chunk_dict
                except Exception as exc:
                    anthropic_defer_reason = _anthropic_defer_loading_rejection_reason(
                        exc, request_kwargs
                    )
                    if anthropic_defer_reason is not None:
                        self._mark_anthropic_defer_loading_broken(
                            provider,
                            resolved_model,
                            reason=anthropic_defer_reason,
                        )
                    # Mid-stream failures (e.g. LiteLLM MidStreamFallbackError,
                    # Anthropic tool_use_failed) should not crash the caller.
                    # Yield an error marker so the agent loop can handle it.
                    logger.warning(
                        "LLM stream failed mid-generation",
                        extra={"extra_data": {"model": prefixed_model}},
                        exc_info=True,
                    )
                    error_chunk = build_mid_stream_error_chunk(exc)
                    observe_chunk(error_chunk)
                    yield error_chunk

    def count_tokens(self, text: str, model: str) -> int:
        family = self._tokenizer_family(model)
        try:
            if family == "openai":
                import tiktoken

                encoding = tiktoken.encoding_for_model(model)
                count = len(encoding.encode(text))
                self._record_tokenizer_backend(model, family, "tiktoken")
                return count
            if family in {"anthropic", "gemini"}:
                messages = [{"role": "user", "content": text}]
                count = int(litellm.token_counter(model=model, messages=messages))
                self._record_tokenizer_backend(model, family, "litellm_native")
                return count
        except Exception:
            pass
        self._record_tokenizer_backend(model, family, "chars_div_4")
        return max(1, len(text) // 4)

    def count_messages_tokens(self, messages: list[dict[str, Any]], model: str) -> int:
        try:
            return int(litellm.token_counter(model=model, messages=messages))
        except Exception:
            serialized = "\n".join(
                f"{message.get('role', 'unknown')}: {message.get('content', '')}"
                for message in messages
            )
            return int(self.count_tokens(serialized, model) * 1.1)

    async def list_models(self) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (await session.execute(select(LLMProviderRow))).scalars().all()
            models: list[dict[str, Any]] = []
            for row in rows:
                config = dict(row.config)
                row_models = config.get("models", [])
                if isinstance(row_models, list):
                    for model in row_models:
                        if isinstance(model, dict):
                            models.append(
                                {
                                    **cast(dict[str, Any], model),
                                    "provider_id": row.provider_id,
                                    "provider_display_name": row.display_name,
                                }
                            )
            return models

    async def list_model_ids(self) -> list[str]:
        """Return all configured model IDs across all providers."""
        models = await self.list_models()
        return sorted({m["model_id"] for m in models if "model_id" in m})

    async def discover_models(self, provider_id: str) -> list[dict[str, Any]]:
        """Query the remote provider for available models.

        Uses the provider's configured base_url and credentials to call
        the OpenAI-compatible ``/v1/models`` endpoint.  For Ollama,
        calls ``/api/tags`` instead.
        """
        async with self.session_factory() as session:
            provider = await session.get(LLMProviderRow, provider_id)
        if provider is None:
            raise ValueError("LLM provider not found")
        if _looks_like_chatgpt_oauth_provider(provider):
            self._ensure_controller_side_oauth_provider(provider)

        config = dict(provider.config)
        if provider.location == "executor":
            raise ValueError("Model discovery is only supported for controller-side providers")
        request_kwargs = await self._resolve_provider_kwargs(provider)
        api_key = request_kwargs.get("api_key", "")
        base_url = request_kwargs.get("api_base") or request_kwargs.get("base_url") or ""
        preset = str(config.get("preset", ""))

        if _looks_like_chatgpt_oauth_provider(provider):
            return await self._discover_codex_models(provider)

        return await self._discover_models_remote(preset, base_url, api_key)

    async def discover_models_preview(
        self,
        preset: str,
        base_url: str,
        api_key: str | None = None,
        secret_name: str | None = None,
        env_var: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover models without a saved provider (preview mode).

        Accepts inline credentials so the user can discover models before
        saving the provider configuration.
        """
        import contextlib

        resolved_key = api_key or ""
        if not resolved_key and env_var:
            resolved_key = os.environ.get(env_var, "")
        if not resolved_key and secret_name and self._secrets:
            with contextlib.suppress(Exception):
                resolved_key = await self._secrets.get_secret(secret_name, "system", None)
        if preset == "chatgpt":
            return bundled_codex_model_entries()
        return await self._discover_models_remote(preset, base_url, resolved_key)

    async def _discover_codex_models(self, provider: LLMProviderRow) -> list[dict[str, Any]]:
        config = dict(provider.config)
        configured_models = [m for m in config.get("models", []) if isinstance(m, dict)]
        fallback_entries = bundled_codex_model_entries(configured_models)
        cache_key = provider.provider_id
        now = monotonic()
        cached = self._codex_model_cache.get(cache_key)
        if cached is not None and now < cached[1]:
            return cached[0]
        try:
            auth = await self._chatgpt_codex_auth(provider)
            remote_entries = await fetch_codex_models(auth)
        except Exception:
            logger.debug(
                "Codex model discovery failed, using bundled catalog",
                extra={"extra_data": {"provider_id": provider.provider_id}},
                exc_info=True,
            )
            return fallback_entries
        merged = {entry["model_id"]: entry for entry in fallback_entries if entry.get("model_id")}
        for entry in remote_entries:
            model_id = entry.get("model_id")
            if isinstance(model_id, str) and model_id:
                merged[model_id] = {**merged.get(model_id, {}), **entry}
        entries = list(merged.values())
        self._codex_model_cache[cache_key] = (entries, now + CODEX_MODEL_CACHE_TTL_SECONDS)
        return entries

    async def get_codex_usage(self, provider_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            provider = await session.get(LLMProviderRow, provider_id)
        if provider is None:
            raise ValueError("LLM provider not found")
        if not _looks_like_chatgpt_oauth_provider(provider):
            raise ValueError("Codex usage is only available for ChatGPT OAuth providers")
        auth = await self._chatgpt_codex_auth(provider)
        return await fetch_codex_usage(auth)

    async def _discover_models_remote(
        self, preset: str, base_url: str, api_key: str
    ) -> list[dict[str, Any]]:
        """Shared implementation for model discovery."""
        import httpx

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=15) as client:
            if preset == "ollama" or "ollama" in base_url.lower():
                # Ollama: GET /api/tags
                ollama_url = base_url.rstrip("/") or "http://localhost:11434"
                response = await client.get(f"{ollama_url}/api/tags", headers=headers)
                response.raise_for_status()
                data = response.json()
                return [
                    {"model_id": f"ollama/{m['name']}", "name": m.get("name", "")}
                    for m in data.get("models", [])
                ]

            if preset == "anthropic" and not base_url:
                # Anthropic doesn't have a /v1/models endpoint;
                # return well-known models
                return [
                    {"model_id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
                    {"model_id": "claude-3-7-sonnet-latest", "name": "Claude 3.7 Sonnet"},
                    {"model_id": "claude-3-5-haiku-latest", "name": "Claude 3.5 Haiku"},
                    {"model_id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
                ]

            # litellm_proxy: prefer /model/info for enriched metadata
            if preset == "litellm_proxy":
                proxy_url = base_url.rstrip("/") if base_url else "http://localhost:4000"
                try:
                    proxy_info_map = await self._fetch_proxy_model_info(
                        proxy_url, api_key, bypass_cache=True
                    )
                    if proxy_info_map:
                        return [
                            {"model_id": name, "name": name, **info}
                            for name, info in proxy_info_map.items()
                        ]
                except Exception:
                    logger.debug(
                        "Proxy /model/info failed during discovery, falling back to /v1/models",
                        exc_info=True,
                    )
                # Fall through to /v1/models below

            # OpenAI-compatible (incl. litellm_proxy fallback): GET /v1/models
            openai_url = base_url.rstrip("/") if base_url else "https://api.openai.com"
            response = await client.get(f"{openai_url}/v1/models", headers=headers)
            response.raise_for_status()
            data = response.json()
            raw_models = data.get("data", [])

            # Enrich each model with litellm static metadata when available
            enriched: list[dict[str, Any]] = []
            for m in raw_models:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mid = str(m["id"])
                entry: dict[str, Any] = {"model_id": mid, "name": mid}
                try:
                    live = litellm.get_model_info(model=mid)
                    if isinstance(live, dict):
                        entry.update(_normalize_proxy_model_info(live))
                except Exception:
                    pass
                metadata_floor = _metadata_floor_for_model(mid)
                if metadata_floor is not None and preset in {"openai", "azure"}:
                    for key, value in metadata_floor.items():
                        current = entry.get(key)
                        if not isinstance(current, int | float) or int(current) < value:
                            entry[key] = value
                enriched.append(entry)
            return enriched

    async def get_cost(self, usage: TokenUsage, model: str) -> Cost:
        model_info = await self.get_model_info(model)
        input_rate = (model_info.input_cost_per_mtok or 0.0) / 1_000_000
        output_rate = (model_info.output_cost_per_mtok or 0.0) / 1_000_000
        input_cost = round(usage.prompt_tokens * input_rate, 6)
        output_cost = round(usage.completion_tokens * output_rate, 6)
        return Cost(
            model=model,
            provider="litellm",
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=round(input_cost + output_cost, 6),
        )

    async def health(self) -> ProviderHealth:
        async with self.session_factory() as session:
            provider_count = len((await session.execute(select(LLMProviderRow))).scalars().all())
        if provider_count == 0:
            return ProviderHealth(
                name="llm", status="unhealthy", error="No LLM providers configured"
            )
        try:
            resolved_model = await self.resolve_model(task_type="default")
        except Exception as exc:
            return ProviderHealth(
                name="llm", status="degraded", error=self._sanitize_error_detail(exc)
            )
        return ProviderHealth(
            name="llm",
            status="healthy",
            details={"model_resolved": resolved_model},
        )

    async def test_provider(self, provider_id: str, timeout_seconds: int = 15) -> dict[str, Any]:
        async with self.session_factory() as session:
            provider = await session.get(LLMProviderRow, provider_id)
        if provider is None:
            raise ValueError("LLM provider not found")

        config = dict(provider.config)
        default_model = config.get("default_model")
        if not isinstance(default_model, str) or not default_model:
            raise ValueError("Provider default_model is not configured")

        prefixed_model = self._apply_model_prefix(default_model, provider)
        started_at = monotonic()
        tested_at = datetime.now(UTC)
        executor_routed = self._should_route_to_executor(provider)
        executor_backend = (
            _executor_backend_for_provider(provider, config) if executor_routed else None
        )
        executor_id = config.get("executor_id") if executor_routed else None

        def _test_result(ok: bool, **extra: Any) -> dict[str, Any]:
            return {
                "ok": ok,
                "model_resolved": default_model,
                "model_sent": prefixed_model,
                "latency_ms": int((monotonic() - started_at) * 1000),
                "tested_at": tested_at,
                "executor_routed": executor_routed,
                "executor_id": executor_id,
                "executor_backend": executor_backend,
                **extra,
            }

        if _looks_like_chatgpt_oauth_provider(provider):
            try:
                await self._test_chatgpt_codex_provider(
                    provider,
                    model_id=default_model,
                    timeout_seconds=timeout_seconds,
                )
                return _test_result(True, error_type=None, error_detail=None)
            except TimeoutError as exc:
                return _test_result(
                    False, error_type="timeout", error_detail=self._sanitize_error_detail(exc)
                )
            except Exception as exc:
                return _test_result(
                    False,
                    error_type=self._classify_provider_error(exc),
                    error_detail=self._sanitize_error_detail(exc),
                )

        model_info = await self.get_model_info(default_model, provider_id=provider.provider_id)
        request_kwargs = await self._resolve_provider_kwargs(provider)
        configured_timeout = request_kwargs.get("timeout")
        request_kwargs["timeout"] = (
            min(timeout_seconds, configured_timeout)
            if isinstance(configured_timeout, int)
            else timeout_seconds
        )
        self._autofill_max_tokens(
            request_kwargs,
            model_info=model_info,
            provider=provider,
            resolved_model=default_model,
        )
        if model_info.supports_reasoning:
            auxiliary_effort = auxiliary_reasoning_effort_for_model(
                default_model,
                provider_preset=str(config.get("preset", "")).lower(),
                model_info=model_info,
                supports_reasoning=model_info.supports_reasoning,
            )
            if auxiliary_effort is not None:
                request_kwargs["reasoning_effort"] = auxiliary_effort
        request_kwargs = self._prepare_generation_request_kwargs(
            request_kwargs,
            model_id=default_model,
            provider=provider,
            model_info=model_info,
        )
        try:
            test_messages = [{"role": "user", "content": "Say hello."}]
            async with self._provider_oauth_token_context(provider):
                if self._should_route_to_executor(provider):
                    if self._inference_router is None:
                        raise RuntimeError("Inference router is not configured")
                    await self._inference_router.route_generate(
                        messages=test_messages,
                        model=prefixed_model,
                        executor_id=config.get("executor_id") if isinstance(config, dict) else None,
                        executor_labels=config.get("executor_labels")
                        if isinstance(config, dict)
                        else None,
                        request_kwargs=request_kwargs,
                        backend=executor_backend or "litellm",
                        provider_id=provider.provider_id,
                        owner_email=provider.owner_email,
                    )
                else:
                    await self._litellm_transport.completion(
                        model=prefixed_model,
                        messages=test_messages,
                        stream=False,
                        **request_kwargs,
                    )
        except TimeoutError as exc:
            return _test_result(
                False, error_type="timeout", error_detail=self._sanitize_error_detail(exc)
            )
        except Exception as exc:
            return _test_result(
                False,
                error_type=self._classify_provider_error(exc),
                error_detail=self._sanitize_error_detail(exc),
            )
        return _test_result(True, error_type=None, error_detail=None)

    async def _find_provider_for_model(
        self, session: Any, model_id: str, acting_user_email: str | None = None
    ) -> LLMProviderRow | None:
        cache_scope = _owner_scope_cache_key(acting_user_email)
        cached_provider_id = await self._get_cached_provider_id(model_id, cache_scope)
        if cached_provider_id is not _CACHE_MISS:
            if cached_provider_id is None:
                return None
            cached_provider = await session.get(LLMProviderRow, cached_provider_id)
            if (
                cached_provider is not None
                and cached_provider.status == "active"
                and _provider_visible_to_user(cached_provider, acting_user_email)
            ):
                return cached_provider
        visible_owners = [SYSTEM_USER_EMAIL]
        if acting_user_email and acting_user_email != SYSTEM_USER_EMAIL:
            visible_owners.insert(0, acting_user_email)
        rows = (
            (
                await session.execute(
                    select(LLMProviderRow).where(
                        LLMProviderRow.status == "active",
                        LLMProviderRow.owner_email.in_(visible_owners),
                    )
                )
            )
            .scalars()
            .all()
        )
        provider_id = self._select_provider_id_for_model(rows, model_id)
        await self._set_cached_provider_id(model_id, cache_scope, provider_id)
        if provider_id is None:
            return None
        return await session.get(LLMProviderRow, provider_id)

    @staticmethod
    def _provider_matches_model(row: LLMProviderRow, model_id: str) -> bool:
        config = dict(row.config)
        if config.get("default_model") == model_id:
            return True
        row_models = config.get("models", [])
        if not isinstance(row_models, list):
            return False
        return any(
            isinstance(model, dict) and model.get("model_id") == model_id for model in row_models
        )

    @classmethod
    def _select_provider_id_for_model(cls, rows: list[LLMProviderRow], model_id: str) -> str | None:
        matches = [row for row in rows if cls._provider_matches_model(row, model_id)]
        if not matches:
            return None
        matches.sort(
            key=lambda row: (
                0 if bool(getattr(row, "is_default", False)) else 1,
                0 if _provider_preset(row) == "chatgpt" else 1,
                row.provider_id,
            )
        )
        return matches[0].provider_id

    @staticmethod
    def _apply_model_prefix(model: str, provider: LLMProviderRow | None) -> str:
        """Prefix model name based on provider preset for correct litellm routing.

        LiteLLM uses model name prefixes to determine which provider API to
        use.  For standard providers (``openai``, ``anthropic``), litellm
        recognises model names natively.  For OpenAI-compatible endpoints and
        LiteLLM proxies, a prefix is required so litellm routes correctly:
        ``openai/model`` or ``litellm_proxy/model``.

        Models that already contain a ``/`` (e.g. ``ollama/llama3``) are
        returned unchanged to avoid double-prefixing.
        """
        if provider is None or "/" in model:
            return model
        preset = dict(provider.config).get("preset", "")
        prefix = PRESET_TO_MODEL_PREFIX.get(preset)
        if prefix:
            return f"{prefix}/{model}"
        return model

    @staticmethod
    def _transcription_wire_model(model: str, provider_preset: str) -> str:
        if "/" not in model:
            return model
        if provider_preset == "litellm_proxy":
            return model
        if provider_preset in {"openai", "openai_compatible"}:
            return model.split("/", 1)[1]
        return model

    def _sanitize_http_error_detail(self, error: httpx.HTTPStatusError) -> str:
        detail = self._sanitize_error_detail(error)
        try:
            payload = error.response.json()
        except Exception:
            return detail
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = err.get("message")
                if isinstance(message, str) and message:
                    return f"{detail}; provider_error={message[:250]}"
        return detail

    def _provider_request_kwargs(self, provider: LLMProviderRow | None) -> dict[str, Any]:
        if provider is None:
            return {}
        config = dict(provider.config)
        request_kwargs: dict[str, Any] = {}
        for key in SAFE_PROVIDER_KWARGS:
            value = config.get(key)
            if value is not None:
                request_kwargs[key] = value
        if "base_url" in request_kwargs and "api_base" not in request_kwargs:
            request_kwargs["api_base"] = request_kwargs["base_url"]
        extra_headers = config.get("extra_headers")
        if isinstance(extra_headers, dict):
            request_kwargs["extra_headers"] = {
                str(key): str(value) for key, value in extra_headers.items()
            }

        # Resolve API key from auth_config
        api_key = self._resolve_api_key(config)
        if api_key:
            request_kwargs["api_key"] = api_key
        return request_kwargs

    def _resolve_api_key(self, config: dict[str, Any]) -> str | None:
        """Resolve API key from provider auth configuration.

        Supports three modes via config.auth_config:
        - ``secret``: read from the encrypted secrets store by secret_name
        - ``env``: read from an environment variable by env_var name
        - fallback: if auth_config is absent, return None and let LiteLLM
          use its own standard env-var lookup
        """
        auth_config = config.get("auth_config")
        if not isinstance(auth_config, dict):
            return None

        mode = auth_config.get("mode")
        if mode == "secret":
            secret_name = auth_config.get("secret_name")
            if not isinstance(secret_name, str) or not secret_name:
                return None
            if self._secrets is None:
                logger.warning(
                    "Provider auth_config references secret but secrets provider unavailable"
                )
                return None
            # _resolve_api_key_from_secret is async; callers should use
            # resolve_provider_kwargs instead for the async path.
            return None
        elif mode == "env":
            env_var = auth_config.get("env_var")
            if isinstance(env_var, str) and env_var:
                return os.environ.get(env_var)
        return None

    async def _resolve_api_key_async(
        self, config: dict[str, Any], owner_email: str | None = None
    ) -> str | None:
        """Async version of API key resolution (supports secrets store)."""
        auth_config = config.get("auth_config")
        if not isinstance(auth_config, dict):
            return None

        mode = auth_config.get("mode")
        if mode == "secret":
            secret_name = auth_config.get("secret_name")
            if not isinstance(secret_name, str) or not secret_name:
                return None
            if self._secrets is None:
                return None
            secret_owner = owner_email or SYSTEM_USER_EMAIL
            try:
                value = await self._secrets.get_secret(secret_name, secret_owner, None)
                return str(value)
            except Exception:
                logger.warning("Failed to read secret for LLM provider auth")
                return None
        elif mode == "env":
            env_var = auth_config.get("env_var")
            if isinstance(env_var, str) and env_var:
                return os.environ.get(env_var)
        return None

    async def _resolve_provider_kwargs(self, provider: LLMProviderRow | None) -> dict[str, Any]:
        """Async version of _provider_request_kwargs — resolves secrets."""
        if provider is None:
            return {}
        config = dict(provider.config)
        request_kwargs: dict[str, Any] = {}
        for key in SAFE_PROVIDER_KWARGS:
            value = config.get(key)
            if value is not None:
                request_kwargs[key] = value
        if "base_url" in request_kwargs and "api_base" not in request_kwargs:
            request_kwargs["api_base"] = request_kwargs["base_url"]
        extra_headers = config.get("extra_headers")
        if isinstance(extra_headers, dict):
            request_kwargs["extra_headers"] = {
                str(key): str(value) for key, value in extra_headers.items()
            }
        api_key = await self._resolve_api_key_async(
            config, provider.owner_email or SYSTEM_USER_EMAIL
        )
        if api_key:
            request_kwargs["api_key"] = api_key
        return request_kwargs

    async def resolve_stream_idle_config(
        self,
        *,
        provider_id: str | None,
        model_id: str,
        default_idle_timeout_seconds: int,
        default_max_retries: int,
    ) -> dict[str, int]:
        """Resolve LLM stream idle watchdog settings for a provider/model."""

        idle_timeout = default_idle_timeout_seconds
        max_retries = default_max_retries
        provider: LLMProviderRow | None = None
        if provider_id is not None:
            async with self.session_factory() as session:
                provider = await session.get(LLMProviderRow, provider_id)
        idle_timeout_configured = False
        max_retries_configured = False
        if provider is not None:
            config = dict(provider.config or {})
            if "stream_idle_timeout_seconds" in config:
                idle_timeout_configured = True
                idle_timeout = _positive_int(
                    config.get("stream_idle_timeout_seconds"), idle_timeout
                )
            if "stream_max_retries" in config:
                max_retries_configured = True
                max_retries = _positive_int(config.get("stream_max_retries"), max_retries)
            models = config.get("models")
            if isinstance(models, list):
                for raw_model in models:
                    if not isinstance(raw_model, dict):
                        continue
                    if raw_model.get("model_id") != model_id:
                        continue
                    if "stream_idle_timeout_seconds" in raw_model:
                        idle_timeout_configured = True
                        idle_timeout = _positive_int(
                            raw_model.get("stream_idle_timeout_seconds"), idle_timeout
                        )
                    if "stream_max_retries" in raw_model:
                        max_retries_configured = True
                        max_retries = _positive_int(
                            raw_model.get("stream_max_retries"), max_retries
                        )
                    break
            if _looks_like_chatgpt_oauth_provider(provider):
                if not idle_timeout_configured:
                    idle_timeout = min(idle_timeout, 90)
                if not max_retries_configured:
                    max_retries = min(max_retries, 3)
        return {
            "idle_timeout_seconds": max(1, idle_timeout),
            "max_retries": max(0, max_retries),
        }

    def _classify_provider_error(self, error: Exception) -> str:
        message = str(error).lower()
        if "auth" in message or "unauthorized" in message or "api key" in message:
            return "auth_failed"
        if "not found" in message or "unknown model" in message or "invalid model" in message:
            return "model_not_found"
        if "timeout" in message:
            return "timeout"
        if "connection" in message or "refused" in message:
            return "connection_refused"
        return "unknown"

    def _sanitize_error_detail(self, error: Exception) -> str:
        message = _sanitize_error_text(str(error))
        return f"{error.__class__.__name__}: {message}"[:500]

    async def _get_cached_resolved_model(self, task_type: str) -> tuple[str, str | None] | None:
        async with self._cache_lock:
            cached = self._resolved_model_cache.get(task_type)
            if cached is None:
                return None
            value, expires_at = cached
            if expires_at < monotonic():
                self._resolved_model_cache.pop(task_type, None)
                return None
            return value

    async def _set_cached_resolved_model(
        self, task_type: str, model_id: str, provider_id: str | None
    ) -> None:
        async with self._cache_lock:
            self._resolved_model_cache[task_type] = (
                (model_id, provider_id),
                monotonic() + MODEL_CACHE_TTL_SECONDS,
            )

    async def _get_cached_model_info(self, model_id: str) -> ModelInfo | None:
        async with self._cache_lock:
            cached = self._model_info_cache.get(model_id)
            if cached is None:
                return None
            value, expires_at = cached
            if expires_at < monotonic():
                self._model_info_cache.pop(model_id, None)
                return None
            return value

    async def _set_cached_model_info(self, model_id: str, model_info: ModelInfo) -> None:
        async with self._cache_lock:
            self._model_info_cache[model_id] = (
                model_info,
                monotonic() + MODEL_CACHE_TTL_SECONDS,
            )

    @staticmethod
    def _model_info_cache_key(model_id: str, provider_id: str | None) -> str:
        return f"{provider_id or '*'}::{model_id}"

    async def _get_cached_provider_id(self, model_id: str, owner_scope: str) -> str | None | object:
        cache_key = f"{owner_scope}:{model_id}"
        async with self._cache_lock:
            cached = self._model_provider_cache.get(cache_key)
            if cached is None:
                return _CACHE_MISS
            value, expires_at = cached
            if expires_at < monotonic():
                self._model_provider_cache.pop(cache_key, None)
                return _CACHE_MISS
            return value

    async def _set_cached_provider_id(
        self, model_id: str, owner_scope: str, provider_id: str | None
    ) -> None:
        cache_key = f"{owner_scope}:{model_id}"
        async with self._cache_lock:
            self._model_provider_cache[cache_key] = (
                provider_id,
                monotonic() + MODEL_CACHE_TTL_SECONDS,
            )

    # ------------------------------------------------------------------
    # Proxy model info fetching
    # ------------------------------------------------------------------

    async def _fetch_proxy_model_info(
        self,
        base_url: str,
        api_key: str,
        *,
        bypass_cache: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Fetch model metadata from a litellm proxy ``/model/info`` endpoint.

        Returns a dict mapping ``model_name`` → normalised model info dict.
        Results are cached in-memory with a 5-minute TTL keyed by
        ``base_url``.  Failures are negatively cached for 30 seconds to
        avoid repeated timeouts on the hot path.

        Pass ``bypass_cache=True`` (e.g. during explicit discovery) to
        force a fresh fetch.
        """
        import httpx

        api_hash = hashlib.sha256(api_key.encode()).hexdigest()[:12] if api_key else "anonymous"
        cache_key = f"{base_url.rstrip('/')}#{api_hash}"

        if not bypass_cache:
            async with self._cache_lock:
                cached = self._proxy_model_info_cache.get(cache_key)
                if cached is not None:
                    value, expires_at = cached
                    if expires_at >= monotonic():
                        return value
                    self._proxy_model_info_cache.pop(cache_key, None)

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base_url.rstrip('/')}/model/info", headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception:
            logger.warning(
                "Failed to fetch proxy model info",
                extra={
                    "extra_data": {
                        "base_url": re.sub(r"://[^@/]+@", "://[redacted]@", base_url.rstrip("/")),
                        "api_hash": api_hash,
                    }
                },
                exc_info=True,
            )
            # Negative cache: store empty dict for 30 s to avoid repeated
            # timeouts on the hot path.
            async with self._cache_lock:
                self._proxy_model_info_cache[cache_key] = (
                    {},
                    monotonic() + PROXY_MODEL_INFO_NEGATIVE_TTL,
                )
            return {}

        result: dict[str, dict[str, Any]] = {}
        for entry in data.get("data", []):
            model_name = entry.get("model_name", "")
            if not model_name:
                continue
            info = entry.get("model_info", {})
            if not isinstance(info, dict):
                continue
            result[model_name] = _normalize_proxy_model_info(info)

        async with self._cache_lock:
            self._proxy_model_info_cache[cache_key] = (
                result,
                monotonic() + PROXY_MODEL_INFO_CACHE_TTL,
            )
        logger.info(
            "Populated proxy model info cache",
            extra={
                "extra_data": {
                    "base_url": re.sub(r"://[^@/]+@", "://[redacted]@", base_url.rstrip("/")),
                    "api_hash": api_hash,
                    "model_count": len(result),
                }
            },
        )
        return result

    # ------------------------------------------------------------------
    # Executor-side inference routing
    # ------------------------------------------------------------------

    def _should_route_to_executor(self, provider: Any | None) -> bool:
        """Check if a provider is configured for executor-side inference."""
        if provider is None or self._inference_router is None:
            return False
        return getattr(provider, "location", None) == "executor"

    async def _executor_generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        provider: Any,
        *,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Route a non-streaming request to executor-side inference."""
        config = provider.config if hasattr(provider, "config") else {}
        executor_id = config.get("executor_id") if isinstance(config, dict) else None
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        executor_backend = _executor_backend_for_provider(provider, config)
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        result = await self._inference_router.route_generate(
            messages=messages,
            model=model,
            executor_id=executor_id,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
            backend=executor_backend,
            provider_id=getattr(provider, "provider_id", None),
            owner_email=getattr(provider, "owner_email", None),
        )
        return cast(dict[str, Any], result)

    async def _executor_stream_generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        provider: Any,
        *,
        request_kwargs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Route a streaming request to executor-side inference."""
        config = provider.config if hasattr(provider, "config") else {}
        executor_id = config.get("executor_id") if isinstance(config, dict) else None
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        executor_backend = _executor_backend_for_provider(provider, config)
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        async for chunk in self._inference_router.route_stream(
            messages=messages,
            model=model,
            executor_id=executor_id,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
            backend=executor_backend,
            provider_id=getattr(provider, "provider_id", None),
            owner_email=getattr(provider, "owner_email", None),
        ):
            yield chunk

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
        task_type: str = "embedding",
        **kwargs: Any,
    ) -> list[list[float]]:
        if not texts:
            return []
        explicit_provider_id = cast(str | None, kwargs.pop("provider_id", None))
        acting_user_email = cast(str | None, kwargs.pop("acting_user_email", None))
        resolved_model, provider = await self._resolve_model_target(
            model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
            acting_user_email=acting_user_email,
        )
        if provider is None:
            raise ValueError(f"No LLM provider found for embedding model {resolved_model!r}")
        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        request_kwargs = _merge_request_kwargs(
            await self._resolve_provider_kwargs(provider), kwargs
        )
        async with self._provider_oauth_token_context(provider):
            response = await litellm.aembedding(
                model=prefixed_model,
                input=texts,
                **request_kwargs,
            )
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        vectors: list[list[float]] = []
        for item in data or []:
            embedding = (
                item.get("embedding")
                if isinstance(item, dict)
                else getattr(item, "embedding", None)
            )
            if embedding is None:
                continue
            vectors.append([float(value) for value in embedding])
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding provider returned an unexpected number of vectors")
        return vectors

    # ------------------------------------------------------------------
    # Image generation (ImageGenerationProvider)
    # ------------------------------------------------------------------

    async def image_generate(
        self,
        prompt: str,
        model: str | None = None,
        task_type: str = "image_generation",
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        **kwargs: Any,
    ) -> ImageGenerationResult:
        """Generate or edit an image using the configured LLM provider.

        Uses preset-based strategy dispatch:
        - OpenAI/DALL-E: litellm.aimage_generation()
        - Gemini: litellm.acompletion() with modalities=["image", "text"]
        """

        resolved_model, provider = await self._resolve_model_target(model, task_type=task_type)
        if provider is None:
            raise ValueError(f"No LLM provider found for image generation model {resolved_model!r}")
        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        request_kwargs = await self._resolve_provider_kwargs(provider)

        # Determine strategy from provider preset
        preset = ""
        if provider is not None:
            config = dict(provider.config) if hasattr(provider, "config") else {}
            preset = config.get("preset", "") if isinstance(config, dict) else ""
        strategy = _IMAGE_GEN_STRATEGY.get(preset, "aimage_generation")
        # Proxy/compatible presets are pass-through — detect Gemini by model name
        if strategy == "aimage_generation" and "gemini" in prefixed_model.lower():
            strategy = "acompletion_modalities"

        # Route to executor if configured
        if self._should_route_to_executor(provider):
            return await self._executor_image_generate(
                prefixed_model,
                prompt,
                provider,
                strategy=strategy,
                n=n,
                size=size,
                quality=quality,
                response_format=response_format,
                image=image,
                request_kwargs=request_kwargs,
                **kwargs,
            )

        logger.debug(
            "LLM image_generate",
            extra={
                "extra_data": {
                    "model": prefixed_model,
                    "strategy": strategy,
                    "task_type": task_type,
                }
            },
        )

        if strategy == "acompletion_modalities":
            result = await self._image_generate_via_completion(
                prefixed_model,
                prompt,
                request_kwargs,
                n=n,
                size=size,
                image=image,
                **kwargs,
            )
        else:
            result = await self._image_generate_via_api(
                prefixed_model,
                prompt,
                request_kwargs,
                n=n,
                size=size,
                quality=quality,
                response_format=response_format,
                image=image,
                **kwargs,
            )
        if not result.images:
            raise RuntimeError(
                f"Image generation returned no image data for model {result.model!r}"
            )
        return result

    async def _image_generate_via_api(
        self,
        model: str,
        prompt: str,
        request_kwargs: dict[str, Any],
        *,
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        **kwargs: Any,
    ) -> ImageGenerationResult:
        """Generate image using litellm.aimage_generation (OpenAI path)."""
        from cognis.providers.llm.retry import with_llm_retry

        gen_kwargs: dict[str, Any] = {}
        if request_kwargs.get("api_key"):
            gen_kwargs["api_key"] = request_kwargs["api_key"]
        if request_kwargs.get("api_base"):
            gen_kwargs["api_base"] = request_kwargs["api_base"]

        if image is not None:
            # Edit mode — pass the source image when the backend supports it.
            try:
                image_kwargs: dict[str, Any] = {}
                if _supports_image_response_format(model):
                    image_kwargs["response_format"] = response_format
                response = await with_llm_retry(
                    litellm.aimage_generation,
                    prompt=prompt,
                    model=model,
                    n=n,
                    size=size,
                    quality=quality,
                    image=image,
                    operation=f"image_edit({model})",
                    **image_kwargs,
                    **gen_kwargs,
                    **kwargs,
                )
            except Exception:
                # Fall back to regular generation if edit not supported
                image_kwargs = {}
                if _supports_image_response_format(model):
                    image_kwargs["response_format"] = response_format
                response = await with_llm_retry(
                    litellm.aimage_generation,
                    prompt=prompt,
                    model=model,
                    n=n,
                    size=size,
                    quality=quality,
                    operation=f"image_generate({model})",
                    **image_kwargs,
                    **gen_kwargs,
                    **kwargs,
                )
        else:
            image_kwargs = {}
            if _supports_image_response_format(model):
                image_kwargs["response_format"] = response_format
            response = await with_llm_retry(
                litellm.aimage_generation,
                prompt=prompt,
                model=model,
                n=n,
                size=size,
                quality=quality,
                operation=f"image_generate({model})",
                **image_kwargs,
                **gen_kwargs,
                **kwargs,
            )

        return self._normalize_image_response(response, model)

    async def _image_generate_via_completion(
        self,
        model: str,
        prompt: str,
        request_kwargs: dict[str, Any],
        *,
        n: int = 1,
        size: str | None = None,
        image: str | None = None,
        **kwargs: Any,
    ) -> ImageGenerationResult:
        """Generate image using litellm.acompletion with modalities (Gemini path)."""
        from cognis.providers.llm.retry import with_llm_retry

        # Build messages
        content: list[dict[str, Any]] | str
        if image is not None:
            # Edit mode — include image in messages
            content = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt

        messages = [{"role": "user", "content": content}]

        # Filter kwargs — don't pass image-specific params to acompletion
        completion_kwargs = {k: v for k, v in request_kwargs.items() if k not in ("size",)}
        if size:
            completion_kwargs.setdefault("extra_body", {})
            if isinstance(completion_kwargs["extra_body"], dict):
                completion_kwargs["extra_body"]["image_size"] = size

        response = await with_llm_retry(
            litellm.acompletion,
            model=model,
            messages=messages,
            modalities=["image", "text"],
            stream=False,
            n=n,
            operation=f"image_generate_completion({model})",
            **completion_kwargs,
            **kwargs,
        )

        return self._normalize_gemini_image_response(response, model)

    @staticmethod
    def _normalize_image_response(response: Any, model: str) -> ImageGenerationResult:
        """Normalize litellm ImageResponse to ImageGenerationResult."""
        images: list[GeneratedImage] = []
        data = getattr(response, "data", []) or []
        for item in data:
            if isinstance(item, dict):
                b64 = item.get("b64_json") or None
                url = item.get("url") or None
                revised = item.get("revised_prompt")
            else:
                b64 = getattr(item, "b64_json", None) or None
                url = getattr(item, "url", None) or None
                revised = getattr(item, "revised_prompt", None)
            if b64 or url:
                images.append(
                    GeneratedImage(
                        b64_json=b64,
                        url=url,
                        content_type="image/png",
                        revised_prompt=revised,
                    )
                )

        usage = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage = TokenUsage(
                prompt_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
                completion_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
                total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
            )

        return ImageGenerationResult(images=images, model=model, usage=usage)

    @staticmethod
    def _normalize_gemini_image_response(response: Any, model: str) -> ImageGenerationResult:
        """Normalize Gemini completion response with images to ImageGenerationResult."""
        images: list[GeneratedImage] = []
        response_dict = response.model_dump() if hasattr(response, "model_dump") else dict(response)

        choices = response_dict.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            # Gemini returns images in message.images (list of dicts with image_url.url)
            msg_images = message.get("images", [])
            for img in msg_images:
                url = ""
                if isinstance(img, dict):
                    image_url = img.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    elif isinstance(img.get("url"), str):
                        url = img["url"]

                # Extract base64 from data URL
                b64 = ""
                content_type = "image/png"
                if url.startswith("data:"):
                    # data:image/png;base64,<data>
                    parts = url.split(",", 1)
                    if len(parts) == 2:
                        b64 = parts[1]
                        header = parts[0]  # data:image/png;base64
                        if ":" in header and ";" in header:
                            content_type = header.split(":")[1].split(";")[0]
                elif url:
                    b64 = url

                if b64 or url:
                    images.append(
                        GeneratedImage(
                            b64_json=b64 or None,
                            url=None if b64 else (url or None),
                            content_type=content_type,
                        )
                    )

            # LiteLLM/Gemini image previews may return generated media as
            # content parts instead of message.images.  Accept the common
            # OpenAI-ish image_url shape, Gemini inline_data shape, and direct
            # data URLs so a valid image response is not silently normalized to
            # an empty result.
            for part in _iter_gemini_image_content_parts(message.get("content")):
                generated = _generated_image_from_content_part(part)
                if generated is not None:
                    images.append(generated)

        usage_dict = response_dict.get("usage", {})
        usage = None
        if usage_dict:
            usage = TokenUsage(
                prompt_tokens=usage_dict.get("prompt_tokens", 0),
                completion_tokens=usage_dict.get("completion_tokens", 0),
                total_tokens=usage_dict.get("total_tokens", 0),
            )

        return ImageGenerationResult(images=images, model=model, usage=usage)

    async def _executor_image_generate(
        self,
        model: str,
        prompt: str,
        provider: Any,
        *,
        strategy: str,
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        request_kwargs: dict[str, Any],
        **kwargs: Any,
    ) -> ImageGenerationResult:
        """Route image generation to executor-side inference."""
        config = provider.config if hasattr(provider, "config") else {}
        executor_id = config.get("executor_id") if isinstance(config, dict) else None
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        result = await self._inference_router.route_image_generate(
            prompt=prompt,
            model=model,
            strategy=strategy,
            executor_id=executor_id,
            executor_labels=executor_labels,
            n=n,
            size=size,
            quality=quality,
            response_format=response_format,
            image=image,
            request_kwargs=request_kwargs,
        )
        return cast(ImageGenerationResult, result)


# ---------------------------------------------------------------------------
# Text-to-speech helpers (controller-side and executor-side share this)
# ---------------------------------------------------------------------------


_TTS_FORMAT_TO_CONTENT_TYPE: dict[str, str] = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def _content_type_for_tts_format(format_name: str) -> str:
    return _TTS_FORMAT_TO_CONTENT_TYPE.get(format_name.strip().lower(), "application/octet-stream")


async def _run_synthesize_local(
    *,
    text: str,
    voice: str,
    wire_model: str,
    response_format: str,
    speed: float,
    request_kwargs: dict[str, Any],
    resolved_model: str,
    provider_preset: str,
    sanitize_http: Any | None = None,
    sanitize_general: Any | None = None,
    prefer_direct_http: bool = False,
) -> TextToSpeechResult:
    """Run a TTS call against an OpenAI-compatible endpoint.

    Tries ``litellm.aspeech()`` first when available; falls back to direct
    HTTP against ``/v1/audio/speech`` so providers that LiteLLM does not yet
    abstract (or older LiteLLM versions) still work. Returns the audio bytes
    and a content type derived from ``response_format``.
    """
    content_type = _content_type_for_tts_format(response_format)
    direct_first = prefer_direct_http and provider_preset in {
        "openai",
        "openai_compatible",
        "litellm_proxy",
    }

    aspeech = getattr(litellm, "aspeech", None)
    if callable(aspeech) and not direct_first:
        try:
            result = await aspeech(
                model=wire_model,
                input=text,
                voice=voice,
                response_format=response_format,
                speed=speed,
                api_key=request_kwargs.get("api_key"),
                api_base=request_kwargs.get("api_base") or request_kwargs.get("base_url"),
                timeout=request_kwargs.get("timeout", 120),
            )
            audio_bytes = _extract_tts_bytes(result)
            if audio_bytes:
                return TextToSpeechResult(
                    audio_bytes=audio_bytes,
                    content_type=content_type,
                    model=resolved_model,
                    voice=voice,
                    duration_seconds=None,
                )
        except Exception as exc:  # noqa: BLE001 — fall back to direct HTTP
            logger.debug(
                "litellm.aspeech failed; falling back to direct HTTP",
                extra={"extra_data": {"error": str(exc)[:200], "model": wire_model}},
            )

    # Direct HTTP fallback against an OpenAI-compatible /v1/audio/speech.
    api_base = request_kwargs.get("api_base") or request_kwargs.get("base_url")
    if not isinstance(api_base, str) or not api_base:
        api_base = "https://api.openai.com"
    api_key = request_kwargs.get("api_key")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if isinstance(api_key, str) and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    extra_headers = request_kwargs.get("extra_headers")
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})

    body = {
        "model": wire_model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
        "speed": speed,
    }
    timeout = request_kwargs.get("timeout", 120)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{api_base.rstrip('/')}/v1/audio/speech",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = sanitize_http(exc) if callable(sanitize_http) else str(exc)
        raise RuntimeError(f"Text-to-speech request failed: {detail}") from exc
    except Exception as exc:
        detail = sanitize_general(exc) if callable(sanitize_general) else str(exc)
        raise RuntimeError(f"Text-to-speech request failed: {detail}") from exc

    audio_bytes = response.content
    if not audio_bytes:
        raise RuntimeError("Text-to-speech returned an empty audio payload")
    response_content_type = response.headers.get("content-type")
    if isinstance(response_content_type, str) and response_content_type.startswith("audio/"):
        content_type = response_content_type.split(";", 1)[0].strip()
    return TextToSpeechResult(
        audio_bytes=audio_bytes,
        content_type=content_type,
        model=resolved_model,
        voice=voice,
        duration_seconds=None,
    )


def _extract_tts_bytes(result: Any) -> bytes:
    """Pull bytes from a litellm aspeech result, accommodating SDK shapes."""
    if isinstance(result, bytes | bytearray):
        return bytes(result)
    for attr in ("content", "audio_bytes"):
        value = getattr(result, attr, None)
        if isinstance(value, bytes | bytearray) and value:
            return bytes(value)
    read = getattr(result, "read", None)
    if callable(read):
        data = read()
        if asyncio.iscoroutine(data):
            data = asyncio.get_event_loop().run_until_complete(data)
        if isinstance(data, bytes | bytearray):
            return bytes(data)
    iter_bytes = getattr(result, "iter_bytes", None)
    if callable(iter_bytes):
        chunks: list[bytes] = []
        for chunk in iter_bytes():
            if isinstance(chunk, bytes | bytearray):
                chunks.append(bytes(chunk))
        if chunks:
            return b"".join(chunks)
    return b""
