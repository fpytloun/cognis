"""Memory backend: mnemory — wraps the global MnemoryProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cognis.providers.backends import register_backend

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.providers.registry import ProviderRegistry


@register_backend(kind="memory", id="mnemory", display_name="Mnemory")
def _factory(config: CognisConfig, registry: ProviderRegistry) -> Any:
    """Return the global Mnemory provider from the registry."""
    return registry.memory
