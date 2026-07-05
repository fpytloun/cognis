"""Guardrails backend: intaris — wraps the global IntarisProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cognis.providers.backends import register_backend

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.providers.registry import ProviderRegistry


@register_backend(kind="guardrails", id="intaris", display_name="Intaris")
def _factory(config: CognisConfig, registry: ProviderRegistry) -> Any:
    """Return the global Intaris provider from the registry."""
    return registry.guardrails
