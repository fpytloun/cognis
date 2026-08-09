"""Provider registry and health aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.config import CognisConfig
from cognis.models.config import ProviderHealth
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.credentials.encrypted_db import EncryptedDBCredentialsProvider
from cognis.providers.executor.composite import CompositeExecutorProvider
from cognis.providers.executor.in_process import InProcessExecutorProvider
from cognis.providers.executor.subprocess import SubprocessExecutorProvider
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.llm.inference_router import InferenceRouter
from cognis.providers.llm.service import LLMService
from cognis.providers.memory.mnemory import MnemoryProvider
from cognis.providers.secrets.encrypted_db import EncryptedDBSecretsProvider


@dataclass
class ProviderRegistry:
    memory: MnemoryProvider
    guardrails: IntarisProvider
    executor: CompositeExecutorProvider
    secrets: EncryptedDBSecretsProvider
    credentials: EncryptedDBCredentialsProvider
    llm: LLMService
    auth: JWTAuthProvider
    # LLMService implements both LLMProvider and ImageGenerationProvider.
    # This alias provides explicit access via the ImageGenerationProvider protocol.
    image_generation: LLMService | None = None
    channel_target_ref_secret: str | None = None
    channel_binding_lookup: Any | None = None
    managed_channel_service: Any | None = None
    recipient_resolution_service: Any | None = None

    async def health(self) -> dict[str, ProviderHealth]:
        return {
            "memory": await self.memory.health(),
            "guardrails": await self.guardrails.health(),
            "executor": await self.executor.health(),
            "secrets": await self.secrets.health(),
            "credentials": await self.credentials.health(),
            "llm": await self.llm.health(),
            "auth": await self.auth.health(),
        }


def build_provider_registry(
    config: CognisConfig,
    session_factory: async_sessionmaker[Any],
    auth_provider: JWTAuthProvider,
) -> ProviderRegistry:
    secrets_provider = EncryptedDBSecretsProvider(session_factory, str(config.secrets_key_path))
    credentials_provider = EncryptedDBCredentialsProvider(
        session_factory, str(config.secrets_key_path)
    )

    guardrails_provider = IntarisProvider(config.intaris_url, auth_provider)

    # Build executor sub-providers
    in_process = InProcessExecutorProvider(
        session_factory=session_factory,
        guardrails_provider=guardrails_provider,
    )
    ws_provider = WebSocketExecutorProvider(
        browser_terminal_state_path=config.data_dir / "browser_terminal_pending.json"
    )
    subprocess_provider = SubprocessExecutorProvider(
        ws_provider=ws_provider,
        auth_provider=auth_provider,
        controller_port=config.port,
    )
    composite_executor = CompositeExecutorProvider(
        in_process=in_process,
        websocket=ws_provider,
        subprocess=subprocess_provider,
        session_factory=session_factory,
    )

    # Build inference router (decouples LLM from executor)
    inference_router = InferenceRouter(ws_provider, session_factory)

    llm_provider = LLMService(
        session_factory,
        secrets_provider=secrets_provider,
        inference_router=inference_router,
        credentials_provider=credentials_provider,
    )

    registry = ProviderRegistry(
        memory=MnemoryProvider(config.mnemory_url, auth_provider),
        guardrails=guardrails_provider,
        executor=composite_executor,
        secrets=secrets_provider,
        credentials=credentials_provider,
        llm=llm_provider,
        auth=auth_provider,
        # LLMService implements ImageGenerationProvider
        image_generation=llm_provider,
    )
    registry._session_factory = session_factory  # type: ignore[attr-defined]
    return registry
