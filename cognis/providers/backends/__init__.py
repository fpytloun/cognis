"""Pluggable provider backend registry.

Backends are self-registering modules. Adding a new backend requires only:
1. Create ``cognis/providers/backends/{kind}/{id}.py``.
2. Implement the relevant Provider Protocol.
3. Decorate the factory with ``@register_backend(kind="...", id="...")``.

No core edits are required. The registry is shaped so that external packages
can also register backends via Python entrypoints without breaking changes.

Current backends
----------------
memory:
  mnemory  — Mnemory HTTP provider (default)
  none     — NullMemoryProvider (no-op, for testing / trusted agents)

guardrails:
  intaris  — Intaris HTTP provider (default)
  none     — NoGuardrailsProvider (auto-approve all, event store still used)
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry internals
# ---------------------------------------------------------------------------

@dataclass
class BackendDescriptor:
    kind: str  # "memory" | "guardrails"
    id: str    # e.g. "mnemory", "none", "native"
    factory: Callable[..., Any]
    display_name: str = ""


_registry: dict[tuple[str, str], BackendDescriptor] = {}


def register_backend(
    kind: str,
    id: str,
    display_name: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that registers a backend factory.

    Usage::

        @register_backend(kind="memory", id="none", display_name="No memory")
        def _factory(config: CognisConfig, registry: ProviderRegistry):
            return NullMemoryProvider()
    """
    def decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        key = (kind, id)
        if key in _registry:
            logger.warning(
                "Backend %s/%s already registered — overwriting with %s",
                kind, id, factory,
            )
        _registry[key] = BackendDescriptor(
            kind=kind,
            id=id,
            factory=factory,
            display_name=display_name or id,
        )
        return factory
    return decorator


def get_backend(kind: str, id: str) -> BackendDescriptor:
    """Return the descriptor for a backend, raising ValueError if unknown."""
    _ensure_loaded()
    key = (kind, id)
    if key not in _registry:
        known = sorted(bid for (k, bid) in _registry if k == kind)
        raise ValueError(
            f"Unknown {kind} backend {id!r}. "
            f"Known backends: {known}. "
            f"To add a new backend, create cognis/providers/backends/{kind}/{id}.py "
            f"and decorate the factory with @register_backend(kind={kind!r}, id={id!r})."
        )
    return _registry[key]


def list_backends(kind: str) -> list[str]:
    """Return sorted list of registered backend ids for a given kind."""
    _ensure_loaded()
    return sorted(bid for (k, bid) in _registry if k == kind)


# ---------------------------------------------------------------------------
# Lazy load — import all built-in backend modules on first use
# ---------------------------------------------------------------------------

_loaded = False

_BUILTIN_MODULES = [
    "cognis.providers.backends.memory.mnemory",
    "cognis.providers.backends.memory.null",
    "cognis.providers.backends.guardrails.intaris",
    "cognis.providers.backends.guardrails.null",
]


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module_path in _BUILTIN_MODULES:
        try:
            importlib.import_module(module_path)
        except Exception:
            logger.exception("Failed to load backend module %s", module_path)


# ---------------------------------------------------------------------------
# Per-turn backend resolution
# ---------------------------------------------------------------------------

def resolve_agent_backends(
    agent: Any,
    config: CognisConfig,
    registry: ProviderRegistry,
) -> tuple[Any, Any]:
    """Return (memory_provider, guardrails_provider) for a given agent turn.

    Reads ``agent.capabilities.memory_backend`` and
    ``agent.capabilities.guardrails_backend``, falling back to the system
    defaults from ``config``.

    Returns the effective providers — either the real providers from the
    global registry or null/no-op providers when the backend is ``"none"``.
    """
    capabilities = getattr(agent, "capabilities", None)

    memory_id = (
        capabilities.memory_backend
        if capabilities and capabilities.memory_backend
        else config.default_memory_backend
    )
    guardrails_id = (
        capabilities.guardrails_backend
        if capabilities and capabilities.guardrails_backend
        else config.default_guardrails_backend
    )

    memory_backend = get_backend("memory", memory_id)
    guardrails_backend = get_backend("guardrails", guardrails_id)

    memory = memory_backend.factory(config, registry)
    guardrails = guardrails_backend.factory(config, registry)

    if memory_id != "mnemory" or guardrails_id != "intaris":
        logger.debug(
            "Agent %s using non-default backends: memory=%s guardrails=%s",
            getattr(agent, "agent_id", "?"),
            memory_id,
            guardrails_id,
        )

    return memory, guardrails
