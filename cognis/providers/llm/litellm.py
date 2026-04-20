"""LiteLLM-backed provider wrapper."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

import httpx
import litellm
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.logging import get_logger
from cognis.models.config import (
    DEFAULT_MODEL_INFO,
    Cost,
    GeneratedImage,
    ImageGenerationResult,
    ModelInfo,
    ProviderHealth,
    SpeechToTextResult,
    TokenUsage,
    normalize_reasoning_level,
)
from cognis.providers.llm.reasoning import (
    PreparedReasoningConfig,
    apply_reasoning_config,
    auxiliary_reasoning_effort_for_model,
    reasoning_efforts_for_model,
)
from cognis.providers.llm.responses_bridge import (
    messages_to_responses_input,
    normalize_openai_model_name,
    responses_request_kwargs,
    responses_stream_to_chat_chunks,
    responses_to_chat_response,
    should_use_openai_responses,
)
from cognis.store.models import LLMProvider as LLMProviderRow
from cognis.store.models import ModelRouting

logger = get_logger(__name__)

MODEL_CACHE_TTL_SECONDS = 60.0
PROXY_MODEL_INFO_CACHE_TTL = 300.0  # 5 minutes for successful proxy /model/info fetches
PROXY_MODEL_INFO_NEGATIVE_TTL = 30.0  # 30 seconds negative cache for failures
SAFE_PROVIDER_KWARGS = {"api_base", "api_version", "base_url", "timeout"}
_CACHE_MISS = object()
# Preset-to-litellm model prefix mapping.  LiteLLM uses the prefix to
# determine which provider API to use.  Standard presets (openai, anthropic)
# are recognised by litellm natively and need no prefix.
PRESET_TO_MODEL_PREFIX: dict[str, str] = {
    "litellm_proxy": "litellm_proxy",
    "openai_compatible": "openai",
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


_GEMINI_MODEL_PATTERNS = re.compile(r"(gemini|vertex_ai|google)", re.IGNORECASE)


def _supports_image_response_format(model: str) -> bool:
    normalized = model.rsplit("/", 1)[-1].lower()
    return normalized != "gpt-image-1"


def _metadata_floor_for_model(model_id: str) -> dict[str, int] | None:
    """Return conservative fallback metadata floors for known model families."""

    normalized = normalize_openai_model_name(model_id)
    if _GPT5_MODEL_PATTERN.search(normalized):
        return {"context_window": 1_048_576, "max_output_tokens": 65_536}
    return None


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


def _model_dump(value: Any) -> dict[str, Any]:
    import warnings

    if hasattr(value, "model_dump"):
        with warnings.catch_warnings():
            # LiteLLM's model_construct() can leave nested fields (e.g.
            # ``usage``) as raw dicts instead of Pydantic model instances,
            # triggering a harmless serialisation warning.  Suppress it.
            warnings.filterwarnings("ignore", message=".*Pydantic serializer.*")
            dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(value, dict):
        return value
    return {}


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


def _normalize_proxy_model_info(info: dict[str, Any]) -> dict[str, Any]:
    """Convert litellm proxy ``model_info`` fields to Cognis ``ModelInfo`` fields.

    The litellm proxy ``/model/info`` endpoint returns a dict per model with
    keys like ``max_input_tokens``, ``supports_function_calling``, etc.  This
    helper maps them to the field names used by :class:`ModelInfo`.
    """
    normalized: dict[str, Any] = {}
    # Context / output limits
    if info.get("max_input_tokens"):
        normalized["context_window"] = int(info["max_input_tokens"])
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
    if "supports_pdf_input" in info:
        normalized["supports_pdf_input"] = bool(info["supports_pdf_input"])
    if "supports_reasoning" in info:
        normalized["supports_reasoning"] = bool(info["supports_reasoning"])
    if "supports_extended_thinking" in info:
        normalized["supports_extended_thinking"] = bool(info["supports_extended_thinking"])
    if "supports_prompt_caching" in info:
        normalized["supports_prompt_caching"] = bool(info["supports_prompt_caching"])
    if "supports_openai_namespace_tools" in info:
        normalized["supports_openai_namespace_tools"] = bool(
            info["supports_openai_namespace_tools"]
        )
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
    ) -> None:
        self.session_factory = session_factory
        self._secrets = secrets_provider
        self._inference_router = inference_router
        self._cache_lock = asyncio.Lock()
        self._resolved_model_cache: dict[str, tuple[tuple[str, str | None], float]] = {}
        self._model_info_cache: dict[str, tuple[ModelInfo, float]] = {}
        self._model_provider_cache: dict[str, tuple[str | None, float]] = {}
        self._proxy_model_info_cache: dict[str, tuple[dict[str, dict[str, Any]], float]] = {}
        self._tokenizer_backend_cache: dict[str, tuple[str, str]] = {}

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

    async def resolve_model(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
    ) -> str:
        resolved_model, _ = await self._resolve_model_target(
            explicit_model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
        )
        return resolved_model

    async def resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
    ) -> tuple[str, str | None]:
        resolved_model, provider = await self._resolve_model_target(
            explicit_model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
        )
        return resolved_model, (provider.provider_id if provider is not None else None)

    async def _resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
    ) -> tuple[str, LLMProviderRow | None]:
        if explicit_provider_id is not None:
            async with self.session_factory() as session:
                provider = await session.get(LLMProviderRow, explicit_provider_id)
            if provider is None:
                raise ValueError(f"LLM provider {explicit_provider_id!r} not found")
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
                provider = await self._find_provider_for_model(session, explicit_model)
            return explicit_model, provider
        cached_target = await self._get_cached_resolved_model(task_type)
        if cached_target is not None:
            cached_model, cached_provider_id = cached_target
            async with self.session_factory() as session:
                provider = (
                    await session.get(LLMProviderRow, cached_provider_id)
                    if cached_provider_id is not None
                    else await self._find_provider_for_model(session, cached_model)
                )
            if cached_provider_id is None or provider is not None:
                return cached_model, provider
            async with self._cache_lock:
                self._resolved_model_cache.pop(task_type, None)
        async with self.session_factory() as session:
            route = await session.get(ModelRouting, task_type)
            if route is not None:
                resolved = cast(str, route.model)
                provider = None
                if route.provider_id is not None:
                    provider = await session.get(LLMProviderRow, route.provider_id)
                    if provider is None:
                        raise ValueError(
                            f"Model routing for task_type={task_type!r} references missing provider "
                            f"{route.provider_id!r}"
                        )
                if provider is None:
                    provider = await self._find_provider_for_model(session, resolved)
                await self._set_cached_resolved_model(
                    task_type,
                    resolved,
                    provider.provider_id if provider is not None else None,
                )
                return resolved, provider
            # Try provider marked as default (is_default=True)
            default_provider = (
                await session.execute(
                    select(LLMProviderRow)
                    .where(LLMProviderRow.is_default.is_(True))
                    .order_by(LLMProviderRow.provider_id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            # Fall back to provider with ID "default" for backward compat
            if default_provider is None:
                default_provider = await session.get(LLMProviderRow, "default")
            if default_provider is not None:
                config = dict(default_provider.config)
                default_model = config.get("default_model")
                if isinstance(default_model, str):
                    await self._set_cached_resolved_model(
                        task_type,
                        default_model,
                        default_provider.provider_id,
                    )
                    return default_model, default_provider
        raise ValueError("No LLM model configured")

    async def get_model_info(self, model_id: str, provider_id: str | None = None) -> ModelInfo:
        cache_key = self._model_info_cache_key(model_id, provider_id)
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
            rows = (await session.execute(select(LLMProviderRow))).scalars().all()
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
                provider = await self._find_provider_for_model(session, model_id)
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
                executor_labels=dict(provider.config).get("executor_labels"),
                request_kwargs=request_kwargs,
                prompt=prompt,
                language=language,
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

    async def find_provider_for_model(self, model_id: str) -> str | None:
        """Return the deterministic ``provider_id`` that owns *model_id*."""
        async with self.session_factory() as session:
            rows = (await session.execute(select(LLMProviderRow))).scalars().all()
        return self._select_provider_id_for_model(rows, model_id)

    async def _get_route_reasoning_effort(self, task_type: str) -> str | None:
        async with self.session_factory() as session:
            route = await session.get(ModelRouting, task_type)
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
        ``DEFAULT_MODEL_INFO`` → capability defaults → litellm static →
        **proxy /model/info** → user-configured overrides from DB.

        ``api_key_override`` is used in preview mode where the API key is
        not yet persisted in the provider's ``auth_config``.
        """
        merged: dict[str, Any] = dict(DEFAULT_MODEL_INFO.model_dump())
        preset = (
            str(dict(provider.config).get("preset", "")).lower() if provider is not None else ""
        )
        try:
            provider_kwargs = await self._resolve_provider_kwargs(provider)
            capability_defaults = self._infer_model_capabilities(model_id, provider)
            live = litellm.get_model_info(
                model=self._apply_model_prefix(model_id, provider),
                custom_llm_provider=(
                    dict(provider.config).get("preset") if provider is not None else None
                ),
                api_base=provider_kwargs.get("api_base"),
            )
            merged.update(capability_defaults)
            if isinstance(live, dict):
                merged.update(
                    {
                        "context_window": live.get("max_input_tokens")
                        or live.get("context_window")
                        or merged.get("context_window"),
                        "max_output_tokens": live.get("max_output_tokens")
                        or merged.get("max_output_tokens"),
                        "supports_tools": _merge_live_bool(
                            live,
                            merged,
                            "supports_tools",
                            fallback=bool(
                                live.get("supports_function_calling")
                                or "tools" in (live.get("supported_openai_params") or [])
                            ),
                        ),
                        "supports_streaming": _merge_live_bool(
                            live,
                            merged,
                            "supports_streaming",
                            fallback="stream" in (live.get("supported_openai_params") or []),
                        ),
                        "supports_vision": _merge_live_bool(live, merged, "supports_vision"),
                        "supports_audio_input": _merge_live_bool(
                            live,
                            merged,
                            "supports_audio_input",
                        ),
                        "supports_image_generation": _merge_live_bool(
                            live,
                            merged,
                            "supports_image_generation",
                        ),
                        "supports_pdf_input": _merge_live_bool(live, merged, "supports_pdf_input"),
                        "supports_file_input": _merge_live_bool(
                            live, merged, "supports_file_input"
                        ),
                        "supports_reasoning": _merge_live_bool(live, merged, "supports_reasoning"),
                        "supports_extended_thinking": _merge_live_bool(
                            live,
                            merged,
                            "supports_extended_thinking",
                            fallback=_looks_like_extended_thinking_model(model_id, preset),
                        ),
                        "supports_prompt_caching": _merge_live_bool(
                            live,
                            merged,
                            "supports_prompt_caching",
                        ),
                        "supports_tool_search": _merge_live_bool(
                            live,
                            merged,
                            "supports_tool_search",
                            fallback=bool(live.get("supports_builtin_tool_search")),
                        ),
                        "supports_defer_loading": _merge_live_bool(
                            live,
                            merged,
                            "supports_defer_loading",
                        ),
                        "supports_responses_api": _merge_live_bool(
                            live,
                            merged,
                            "supports_responses_api",
                        ),
                        "supports_openai_namespace_tools": _merge_live_bool(
                            live,
                            merged,
                            "supports_openai_namespace_tools",
                        ),
                        "supported_openai_params": list(live.get("supported_openai_params") or []),
                        "max_tools": live.get("max_tools") or merged.get("max_tools"),
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

        metadata_floor = _metadata_floor_for_model(model_id)
        if metadata_floor is not None:
            applied_floor: dict[str, int] = {}
            context_window = int(merged.get("context_window") or 0)
            if context_window <= DEFAULT_MODEL_INFO.context_window:
                merged["context_window"] = metadata_floor["context_window"]
                applied_floor["context_window"] = metadata_floor["context_window"]
            max_output_tokens = int(merged.get("max_output_tokens") or 0)
            if max_output_tokens <= DEFAULT_MODEL_INFO.max_output_tokens:
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
            or preset in {"openai", "litellm_proxy", "openai_compatible"}
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
        supports_image_generation = _looks_like_image_generation_model(model_name)
        return {
            "supports_defer_loading": is_anthropic,
            "supports_prompt_caching": is_anthropic,
            "supports_tool_search": supports_responses_api,
            "supports_responses_api": supports_responses_api,
            "supports_extended_thinking": False,
            "supports_openai_namespace_tools": False,
            "supports_image_generation": supports_image_generation,
            "max_tools": 128 if is_openai_like else None,
        }

    def _responses_rollout_mode(self) -> str:
        value = os.getenv("COGNIS_OPENAI_RESPONSES_MODE", "auto").strip().lower()
        if value in {"on", "off", "auto"}:
            return value
        return "auto"

    def _should_use_responses_api(
        self, model_id: str, model_info: ModelInfo, provider: LLMProviderRow | None
    ) -> bool:
        if provider is not None and dict(provider.config).get("use_responses_api") is False:
            return False
        return should_use_openai_responses(
            model=self._apply_model_prefix(model_id, provider),
            model_info=model_info,
            rollout_mode=self._responses_rollout_mode(),
        )

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

        explicit_provider_id = cast(str | None, kwargs.pop("provider_id", None))
        resolved_model, provider = await self._resolve_model_target(
            model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
        )
        if provider is None:
            raise ValueError(f"No LLM provider found for model {resolved_model!r}")

        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        model_info = await self.get_model_info(
            resolved_model,
            provider_id=provider.provider_id if provider is not None else None,
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
            routed_reasoning = await self._get_route_reasoning_effort(task_type)
            if routed_reasoning is not None:
                request_kwargs["reasoning_effort"] = routed_reasoning
        retry_count = request_kwargs.pop("max_retries", None)
        if retry_count is None:
            retry_count = request_kwargs.pop("num_retries", None)
        request_kwargs = self._prepare_generation_request_kwargs(
            request_kwargs,
            model_id=resolved_model,
            provider=provider,
            model_info=model_info,
        )
        prepared_messages = _apply_message_cache_hints(
            messages, resolved_model, model_info, cache_breakpoint_index
        )
        use_responses_api = self._should_use_responses_api(resolved_model, model_info, provider)
        if use_responses_api:
            request_kwargs = dict(request_kwargs)
            request_kwargs["cognis_llm_api"] = "responses"
        if self._should_route_to_executor(provider):
            if isinstance(retry_count, int):
                request_kwargs["max_retries"] = retry_count
            return await self._executor_generate(
                prefixed_model,
                prepared_messages,
                provider,
                request_kwargs=request_kwargs,
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
        if use_responses_api:
            response = await with_llm_retry(
                litellm.aresponses,
                model=prefixed_model,
                input=messages_to_responses_input(prepared_messages),
                stream=False,
                max_retries=int(retry_count) if isinstance(retry_count, int) else 3,
                operation=f"generate.responses({prefixed_model})",
                **responses_request_kwargs(request_kwargs),
            )
            return responses_to_chat_response(_model_dump(response))
        response = await with_llm_retry(
            litellm.acompletion,
            model=prefixed_model,
            messages=prepared_messages,
            stream=False,
            max_retries=int(retry_count) if isinstance(retry_count, int) else 3,
            operation=f"generate({prefixed_model})",
            **request_kwargs,
        )
        response_dict = response.model_dump()

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

        explicit_provider_id = cast(str | None, kwargs.pop("provider_id", None))
        resolved_model, provider = await self._resolve_model_target(
            model,
            task_type=task_type,
            explicit_provider_id=explicit_provider_id,
        )
        if provider is None:
            raise ValueError(f"No LLM provider found for model {resolved_model!r}")

        prefixed_model = self._apply_model_prefix(resolved_model, provider)
        model_info = await self.get_model_info(
            resolved_model,
            provider_id=provider.provider_id if provider is not None else None,
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
            routed_reasoning = await self._get_route_reasoning_effort(task_type)
            if routed_reasoning is not None:
                request_kwargs["reasoning_effort"] = routed_reasoning
        retry_count = request_kwargs.pop("max_retries", None)
        if retry_count is None:
            retry_count = request_kwargs.pop("num_retries", None)
        request_kwargs = self._prepare_generation_request_kwargs(
            request_kwargs,
            model_id=resolved_model,
            provider=provider,
            model_info=model_info,
        )
        prepared_messages = _apply_message_cache_hints(
            messages, resolved_model, model_info, cache_breakpoint_index
        )
        use_responses_api = self._should_use_responses_api(resolved_model, model_info, provider)
        if use_responses_api:
            request_kwargs = dict(request_kwargs)
            request_kwargs["cognis_llm_api"] = "responses"
        if self._should_route_to_executor(provider):
            if isinstance(retry_count, int):
                request_kwargs["max_retries"] = retry_count
            async for chunk in self._executor_stream_generate(
                prefixed_model,
                prepared_messages,
                provider,
                request_kwargs=request_kwargs,
            ):
                yield chunk
            return
        logger.debug(
            "LLM stream_generate",
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
        if use_responses_api:
            stream = await with_llm_retry(
                litellm.aresponses,
                model=prefixed_model,
                input=messages_to_responses_input(prepared_messages),
                stream=True,
                max_retries=int(retry_count) if isinstance(retry_count, int) else 3,
                operation=f"stream_generate.responses({prefixed_model})",
                **responses_request_kwargs(request_kwargs),
            )
            try:
                async for chunk in responses_stream_to_chat_chunks(stream):
                    yield chunk
            except Exception as exc:
                logger.warning(
                    "LLM Responses stream failed mid-generation",
                    extra={"extra_data": {"model": prefixed_model}},
                    exc_info=True,
                )
                yield {"error": str(exc), "mid_stream_failure": True}
            return
        # Retry pre-stream errors (connection refused, rate limit, etc.)
        # with exponential backoff.  Once the stream is established,
        # mid-stream failures are caught and yielded as error markers.
        stream = await with_llm_retry(
            litellm.acompletion,
            model=prefixed_model,
            messages=prepared_messages,
            stream=True,
            max_retries=int(retry_count) if isinstance(retry_count, int) else 3,
            operation=f"stream_generate({prefixed_model})",
            **request_kwargs,
        )
        try:
            async for chunk in stream:
                yield dict(chunk)
        except Exception as exc:
            # Mid-stream failures (e.g. LiteLLM MidStreamFallbackError,
            # Anthropic tool_use_failed) should not crash the caller.
            # Yield an error marker so the agent loop can handle it.
            logger.warning(
                "LLM stream failed mid-generation",
                extra={"extra_data": {"model": prefixed_model}},
                exc_info=True,
            )
            yield {"error": str(exc), "mid_stream_failure": True}

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

        config = dict(provider.config)
        if provider.location == "executor":
            raise ValueError("Model discovery is only supported for controller-side providers")
        request_kwargs = await self._resolve_provider_kwargs(provider)
        api_key = request_kwargs.get("api_key", "")
        base_url = request_kwargs.get("api_base") or request_kwargs.get("base_url") or ""
        preset = str(config.get("preset", ""))

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
        return await self._discover_models_remote(preset, base_url, resolved_key)

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

            if preset == "anthropic":
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
        model_info = await self.get_model_info(default_model, provider_id=provider.provider_id)
        request_kwargs = await self._resolve_provider_kwargs(provider)
        configured_timeout = request_kwargs.get("timeout")
        request_kwargs["timeout"] = (
            min(timeout_seconds, configured_timeout)
            if isinstance(configured_timeout, int)
            else timeout_seconds
        )
        base_max_tokens = 1024 if model_info.supports_reasoning else 256
        request_kwargs["max_tokens"] = max(
            base_max_tokens, int(request_kwargs.get("max_tokens", 0) or 0)
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
        started_at = monotonic()
        tested_at = datetime.now(UTC)
        try:
            test_messages = [{"role": "user", "content": "Say hello."}]
            if self._should_route_to_executor(provider):
                if self._inference_router is None:
                    raise RuntimeError("Inference router is not configured")
                await self._inference_router.route_generate(
                    messages=test_messages,
                    model=prefixed_model,
                    executor_labels=config.get("executor_labels")
                    if isinstance(config, dict)
                    else None,
                    request_kwargs=request_kwargs,
                )
            else:
                await litellm.acompletion(
                    model=prefixed_model,
                    messages=test_messages,
                    stream=False,
                    **request_kwargs,
                )
        except TimeoutError as exc:
            return {
                "ok": False,
                "model_resolved": default_model,
                "model_sent": prefixed_model,
                "latency_ms": int((monotonic() - started_at) * 1000),
                "error_type": "timeout",
                "error_detail": self._sanitize_error_detail(exc),
                "tested_at": tested_at,
            }
        except Exception as exc:
            return {
                "ok": False,
                "model_resolved": default_model,
                "model_sent": prefixed_model,
                "latency_ms": int((monotonic() - started_at) * 1000),
                "error_type": self._classify_provider_error(exc),
                "error_detail": self._sanitize_error_detail(exc),
                "tested_at": tested_at,
            }
        return {
            "ok": True,
            "model_resolved": default_model,
            "model_sent": prefixed_model,
            "latency_ms": int((monotonic() - started_at) * 1000),
            "error_type": None,
            "error_detail": None,
            "tested_at": tested_at,
        }

    async def _find_provider_for_model(self, session: Any, model_id: str) -> LLMProviderRow | None:
        cached_provider_id = await self._get_cached_provider_id(model_id)
        if cached_provider_id is not _CACHE_MISS:
            if cached_provider_id is None:
                return None
            cached_provider = await session.get(LLMProviderRow, cached_provider_id)
            if cached_provider is not None:
                return cached_provider
        rows = (await session.execute(select(LLMProviderRow))).scalars().all()
        provider_id = self._select_provider_id_for_model(rows, model_id)
        await self._set_cached_provider_id(model_id, provider_id)
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
            key=lambda row: (0 if bool(getattr(row, "is_default", False)) else 1, row.provider_id)
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

    async def _resolve_api_key_async(self, config: dict[str, Any]) -> str | None:
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
            try:
                value = await self._secrets.get_secret(secret_name, "system", None)
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
        api_key = await self._resolve_api_key_async(config)
        if api_key:
            request_kwargs["api_key"] = api_key
        return request_kwargs

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
        message = str(error)
        message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted-api-key]", message)
        message = re.sub(r"key-[A-Za-z0-9_-]+", "[redacted-api-key]", message)
        message = re.sub(r"https?://[^\s:@]+:[^\s@]+@", "https://[redacted]@", message)
        message = re.sub(r"(?i)(api[_ -]?key\s*[=:]\s*)([^\s,;]+)", r"\1[redacted]", message)
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

    async def _get_cached_provider_id(self, model_id: str) -> str | None | object:
        async with self._cache_lock:
            cached = self._model_provider_cache.get(model_id)
            if cached is None:
                return _CACHE_MISS
            value, expires_at = cached
            if expires_at < monotonic():
                self._model_provider_cache.pop(model_id, None)
                return _CACHE_MISS
            return value

    async def _set_cached_provider_id(self, model_id: str, provider_id: str | None) -> None:
        async with self._cache_lock:
            self._model_provider_cache[model_id] = (
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
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        result = await self._inference_router.route_generate(
            messages=messages,
            model=model,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
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
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        async for chunk in self._inference_router.route_stream(
            messages=messages,
            model=model,
            executor_labels=executor_labels,
            request_kwargs=request_kwargs,
        ):
            yield chunk

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
            return await self._image_generate_via_completion(
                prefixed_model,
                prompt,
                request_kwargs,
                n=n,
                size=size,
                image=image,
                **kwargs,
            )
        return await self._image_generate_via_api(
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
        executor_labels = config.get("executor_labels") if isinstance(config, dict) else None
        if self._inference_router is None:
            raise RuntimeError("Inference router is not configured")
        result = await self._inference_router.route_image_generate(
            prompt=prompt,
            model=model,
            strategy=strategy,
            executor_labels=executor_labels,
            n=n,
            size=size,
            quality=quality,
            response_format=response_format,
            image=image,
            request_kwargs=request_kwargs,
        )
        return cast(ImageGenerationResult, result)
