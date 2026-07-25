"""Memory backend: none — NullMemoryProvider (no-op).

All operations return empty/no-op results. Used for:
- E2E testing agents that must not depend on Mnemory.
- Trusted agents where memory is intentionally disabled.
- Future: agents that use a different memory backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cognis.models.config import ProviderHealth
from cognis.providers.backends import register_backend
from cognis.providers.memory.policy import MemoryRuntimePolicy, disabled_memory_policy

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.providers.registry import ProviderRegistry


class NullMemoryProvider:
    """No-op memory provider — satisfies the MemoryProvider Protocol."""

    async def load_session_identity(
        self,
        *,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def recall(
        self,
        query: str,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        search_mode: str = "find",
        include_instructions: bool = False,
        managed: bool = False,
        instruction_mode: str | None = None,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        return {
            "memories": [],
            "instructions": None,
            "core_memories": None,
        }

    async def remember(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        user_email: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        return

    async def add_memory(
        self,
        content: str,
        memory_type: str | None = None,
        categories: list[str] | None = None,
        importance: str | None = None,
        role: str = "user",
        pinned: bool = False,
        labels: dict[str, Any] | None = None,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> str:
        return "null-memory-id"

    async def search(
        self,
        query: str,
        labels: dict[str, Any] | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def delete_memory(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None:
        return

    async def delete_memory_tool(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None:
        return

    async def bootstrap_agent(self, agent: Any) -> None:
        return

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="null-memory", status="ok", latency_ms=0)

    async def replace_bootstrap_identity(
        self,
        agent: Any,
        previous_content: Any = None,
        allow_legacy_cleanup: bool = False,
    ) -> None:
        return


class NoMemoryOptions:
    """Descriptor contract for the hard-disabled memory backend."""

    defaults: dict[str, Any] = {}
    modes: tuple[Any, ...] = ()

    def validate_options(self, options: object) -> dict[str, Any]:
        if options not in ({}, None):
            raise ValueError("The none memory backend does not accept options")
        return {}

    def resolve_policy(
        self,
        options: dict[str, Any],
        *,
        profile_id: str | None,
    ) -> MemoryRuntimePolicy:
        del options
        return disabled_memory_policy(backend_id="none", profile_id=profile_id)


NONE_MEMORY_OPTIONS = NoMemoryOptions()


@register_backend(
    kind="memory",
    id="none",
    display_name="None",
    description="Stateless operation with no memory instructions, recall, tools, or remembering.",
    memory_options=NONE_MEMORY_OPTIONS,
)
def _factory(config: CognisConfig, registry: ProviderRegistry) -> NullMemoryProvider:
    return NullMemoryProvider()
