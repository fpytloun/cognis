"""Provider registry and health aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.config import CognisConfig
from cognis.models.config import ProviderHealth
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.executor.in_process import InProcessExecutorProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.providers.secrets.encrypted_db import EncryptedDBSecretsProvider


@dataclass
class ProviderRegistry:
    memory: MnemoryProvider
    guardrails: IntarisProvider
    executor: InProcessExecutorProvider
    secrets: EncryptedDBSecretsProvider
    llm: LiteLLMProvider
    auth: JWTAuthProvider

    async def health(self) -> dict[str, ProviderHealth]:
        return {
            "memory": await self.memory.health(),
            "guardrails": await self.guardrails.health(),
            "executor": await self.executor.health(),
            "secrets": await self.secrets.health(),
            "llm": await self.llm.health(),
            "auth": await self.auth.health(),
        }


def build_provider_registry(
    config: CognisConfig,
    session_factory: async_sessionmaker[Any],
    auth_provider: JWTAuthProvider,
) -> ProviderRegistry:
    secrets_provider = EncryptedDBSecretsProvider(session_factory, str(config.secrets_key_path))
    return ProviderRegistry(
        memory=MnemoryProvider(config.mnemory_url, auth_provider),
        guardrails=IntarisProvider(config.intaris_url, auth_provider),
        executor=InProcessExecutorProvider(session_factory=session_factory),
        secrets=secrets_provider,
        llm=LiteLLMProvider(session_factory),
        auth=auth_provider,
    )
