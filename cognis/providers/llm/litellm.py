"""LiteLLM-backed provider wrapper."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.logging import get_logger
from cognis.models.config import DEFAULT_MODEL_INFO, Cost, ModelInfo, ProviderHealth, TokenUsage
from cognis.store.models import LLMProvider as LLMProviderRow
from cognis.store.models import ModelRouting

logger = get_logger(__name__)

MODEL_CACHE_TTL_SECONDS = 60.0
SAFE_PROVIDER_KWARGS = {"api_base", "api_version", "base_url", "max_retries", "timeout"}


class LiteLLMProvider:
    """Load provider/model config from DB and route through LiteLLM."""

    def __init__(self, session_factory: async_sessionmaker[Any]) -> None:
        self.session_factory = session_factory
        self._cache_lock = asyncio.Lock()
        self._resolved_model_cache: dict[str, tuple[str, float]] = {}
        self._model_info_cache: dict[str, tuple[ModelInfo, float]] = {}

    async def resolve_model(
        self, explicit_model: str | None = None, task_type: str = "default"
    ) -> str:
        resolved_model, _ = await self._resolve_model_target(explicit_model, task_type=task_type)
        return resolved_model

    async def _resolve_model_target(
        self, explicit_model: str | None = None, task_type: str = "default"
    ) -> tuple[str, LLMProviderRow | None]:
        if explicit_model is not None:
            async with self.session_factory() as session:
                provider = await self._find_provider_for_model(session, explicit_model)
            return explicit_model, provider
        cached_model = await self._get_cached_resolved_model(task_type)
        if cached_model is not None:
            async with self.session_factory() as session:
                provider = await self._find_provider_for_model(session, cached_model)
            return cached_model, provider
        async with self.session_factory() as session:
            route = await session.get(ModelRouting, task_type)
            if route is not None:
                resolved = cast(str, route.model)
                await self._set_cached_resolved_model(task_type, resolved)
                provider = None
                if route.provider_id is not None:
                    provider = await session.get(LLMProviderRow, route.provider_id)
                if provider is None:
                    provider = await self._find_provider_for_model(session, resolved)
                return resolved, provider
            provider = await session.get(LLMProviderRow, "default")
            if provider is not None:
                config = dict(provider.config)
                default_model = config.get("default_model")
                if isinstance(default_model, str):
                    await self._set_cached_resolved_model(task_type, default_model)
                    return default_model, provider
        raise ValueError("No LLM model configured")

    async def get_model_info(self, model_id: str) -> ModelInfo:
        cached_model_info = await self._get_cached_model_info(model_id)
        if cached_model_info is not None:
            return cached_model_info

        async with self.session_factory() as session:
            rows = (await session.execute(select(LLMProviderRow))).scalars().all()
            for row in rows:
                config = dict(row.config)
                row_models = config.get("models", [])
                if not isinstance(row_models, list):
                    continue
                for model in row_models:
                    if not isinstance(model, dict):
                        continue
                    if model.get("model_id") != model_id:
                        continue
                    model_info = ModelInfo.model_validate(model)
                    await self._set_cached_model_info(model_id, model_info)
                    return model_info

        logger.warning(
            "LLM model metadata missing; using conservative defaults",
            extra={"extra_data": {"model_id": model_id}},
        )
        await self._set_cached_model_info(model_id, DEFAULT_MODEL_INFO)
        return DEFAULT_MODEL_INFO

    async def generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_model, provider = await self._resolve_model_target(model, task_type=task_type)
        request_kwargs = {**self._provider_request_kwargs(provider), **kwargs}
        response = await litellm.acompletion(
            model=resolved_model, messages=messages, stream=False, **request_kwargs
        )
        return dict(response)

    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_model, provider = await self._resolve_model_target(model, task_type=task_type)
        request_kwargs = {**self._provider_request_kwargs(provider), **kwargs}
        stream = await litellm.acompletion(
            model=resolved_model, messages=messages, stream=True, **request_kwargs
        )
        async for chunk in stream:
            yield dict(chunk)

    def count_tokens(self, text: str, model: str) -> int:
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(model)
            return len(encoding.encode(text))
        except Exception:
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

    async def get_cost(self, usage: TokenUsage, model: str) -> Cost:
        return Cost(
            model=model, provider="litellm", total_cost=0.0, input_cost=0.0, output_cost=0.0
        )

    async def health(self) -> ProviderHealth:
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

        request_kwargs = self._provider_request_kwargs(provider)
        configured_timeout = request_kwargs.get("timeout")
        request_kwargs["timeout"] = (
            min(timeout_seconds, configured_timeout)
            if isinstance(configured_timeout, int)
            else timeout_seconds
        )
        started_at = monotonic()
        tested_at = datetime.now(UTC)
        try:
            await litellm.acompletion(
                model=default_model,
                messages=[{"role": "user", "content": "Say hello."}],
                max_tokens=5,
                stream=False,
                **request_kwargs,
            )
        except TimeoutError as exc:
            return {
                "ok": False,
                "model_resolved": default_model,
                "latency_ms": int((monotonic() - started_at) * 1000),
                "error_type": "timeout",
                "error_detail": self._sanitize_error_detail(exc),
                "tested_at": tested_at,
            }
        except Exception as exc:
            return {
                "ok": False,
                "model_resolved": default_model,
                "latency_ms": int((monotonic() - started_at) * 1000),
                "error_type": self._classify_provider_error(exc),
                "error_detail": self._sanitize_error_detail(exc),
                "tested_at": tested_at,
            }
        return {
            "ok": True,
            "model_resolved": default_model,
            "latency_ms": int((monotonic() - started_at) * 1000),
            "error_type": None,
            "error_detail": None,
            "tested_at": tested_at,
        }

    async def _find_provider_for_model(self, session: Any, model_id: str) -> LLMProviderRow | None:
        rows = (await session.execute(select(LLMProviderRow))).scalars().all()
        for row in rows:
            config = dict(row.config)
            if config.get("default_model") == model_id:
                return cast(LLMProviderRow, row)
            row_models = config.get("models", [])
            if not isinstance(row_models, list):
                continue
            for model in row_models:
                if isinstance(model, dict) and model.get("model_id") == model_id:
                    return cast(LLMProviderRow, row)
        return None

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

    async def _get_cached_resolved_model(self, task_type: str) -> str | None:
        async with self._cache_lock:
            cached = self._resolved_model_cache.get(task_type)
            if cached is None:
                return None
            value, expires_at = cached
            if expires_at < monotonic():
                self._resolved_model_cache.pop(task_type, None)
                return None
            return value

    async def _set_cached_resolved_model(self, task_type: str, model_id: str) -> None:
        async with self._cache_lock:
            self._resolved_model_cache[task_type] = (
                model_id,
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
