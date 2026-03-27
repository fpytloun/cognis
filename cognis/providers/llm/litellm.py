"""LiteLLM-backed provider wrapper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
        if explicit_model is not None:
            return explicit_model
        cached_model = await self._get_cached_resolved_model(task_type)
        if cached_model is not None:
            return cached_model
        async with self.session_factory() as session:
            route = await session.get(ModelRouting, task_type)
            if route is not None:
                resolved = cast(str, route.model)
                await self._set_cached_resolved_model(task_type, resolved)
                return resolved
            provider = await session.get(LLMProviderRow, "default")
            if provider is not None:
                config = dict(provider.config)
                default_model = config.get("default_model")
                if isinstance(default_model, str):
                    await self._set_cached_resolved_model(task_type, default_model)
                    return default_model
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
        resolved_model = await self.resolve_model(model, task_type=task_type)
        response = await litellm.acompletion(
            model=resolved_model, messages=messages, stream=False, **kwargs
        )
        return dict(response)

    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_model = await self.resolve_model(model, task_type=task_type)
        stream = await litellm.acompletion(
            model=resolved_model, messages=messages, stream=True, **kwargs
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
                            models.append(cast(dict[str, Any], model))
            return models

    async def get_cost(self, usage: TokenUsage, model: str) -> Cost:
        return Cost(
            model=model, provider="litellm", total_cost=0.0, input_cost=0.0, output_cost=0.0
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="llm", status="healthy")

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
