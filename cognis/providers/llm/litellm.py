"""LiteLLM-backed provider wrapper."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.models.config import Cost, ProviderHealth, TokenUsage
from cognis.store.models import LLMProvider as LLMProviderRow
from cognis.store.models import ModelRouting


class LiteLLMProvider:
    """Load provider/model config from DB and route through LiteLLM."""

    def __init__(self, session_factory: async_sessionmaker[Any]) -> None:
        self.session_factory = session_factory

    async def _resolve_model(self, explicit_model: str | None, task_type: str = "default") -> str:
        if explicit_model is not None:
            return explicit_model
        async with self.session_factory() as session:
            route = await session.get(ModelRouting, task_type)
            if route is not None:
                return cast(str, route.model)
            provider = await session.get(LLMProviderRow, "default")
            if provider is not None:
                config = dict(provider.config)
                default_model = config.get("default_model")
                if isinstance(default_model, str):
                    return default_model
        raise ValueError("No LLM model configured")

    async def generate(
        self, messages: list[dict[str, Any]], model: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        resolved_model = await self._resolve_model(model)
        response = await litellm.acompletion(
            model=resolved_model, messages=messages, stream=False, **kwargs
        )
        return dict(response)

    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_model = await self._resolve_model(model)
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
