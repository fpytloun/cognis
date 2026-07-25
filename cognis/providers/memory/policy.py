"""Provider-owned memory configuration resolved into a generic runtime policy."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from cognis.core.agent_profiles import ResolvedAgentProfile
    from cognis.models.agent import AgentDefinition


@dataclass(frozen=True, slots=True)
class MemoryModeDescriptor:
    """Server-owned UI metadata for one provider-specific memory mode."""

    id: str
    label: str
    description: str
    recommended_for: str
    tooltip: str
    behavior: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MemoryRuntimePolicy:
    """Frozen, provider-neutral memory behavior for one logical turn."""

    backend_id: str
    enabled: bool
    bootstrap_instructions: bool
    bootstrap_core: bool
    auto_recall: bool
    auto_remember: bool
    tools_enabled: bool
    instructions: str | None
    policy_fingerprint: str
    mode_id: str | None = None
    profile_id: str | None = None

    def audit_metadata(self) -> dict[str, str | bool | None]:
        return {
            "memory_backend": self.backend_id,
            "memory_mode": self.mode_id,
            "memory_profile_id": self.profile_id,
            "memory_policy_fingerprint": self.policy_fingerprint,
            "memory_enabled": self.enabled,
        }


class MemoryBackendOptionsProvider(Protocol):
    """Provider-owned validation, descriptor, and policy contract."""

    defaults: dict[str, Any]
    modes: tuple[MemoryModeDescriptor, ...]

    def validate_options(self, options: object) -> dict[str, Any]: ...

    def resolve_policy(
        self,
        options: dict[str, Any],
        *,
        profile_id: str | None,
    ) -> MemoryRuntimePolicy: ...


def fingerprint_policy(
    *,
    backend_id: str,
    options: dict[str, Any],
    enabled: bool,
    bootstrap_instructions: bool,
    bootstrap_core: bool,
    auto_recall: bool,
    auto_remember: bool,
    tools_enabled: bool,
    instructions: str | None,
) -> str:
    """Return a stable fingerprint without exposing raw provider options."""

    payload = {
        "backend_id": backend_id,
        "options": options,
        "enabled": enabled,
        "bootstrap_instructions": bootstrap_instructions,
        "bootstrap_core": bootstrap_core,
        "auto_recall": auto_recall,
        "auto_remember": auto_remember,
        "tools_enabled": tools_enabled,
        "instructions_sha256": (
            sha256(instructions.encode("utf-8")).hexdigest() if instructions else None
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def disabled_memory_policy(
    *,
    backend_id: str,
    profile_id: str | None,
) -> MemoryRuntimePolicy:
    """Build the generic hard-disabled policy."""

    fingerprint = fingerprint_policy(
        backend_id=backend_id,
        options={},
        enabled=False,
        bootstrap_instructions=False,
        bootstrap_core=False,
        auto_recall=False,
        auto_remember=False,
        tools_enabled=False,
        instructions=None,
    )
    return MemoryRuntimePolicy(
        backend_id=backend_id,
        enabled=False,
        bootstrap_instructions=False,
        bootstrap_core=False,
        auto_recall=False,
        auto_remember=False,
        tools_enabled=False,
        instructions=None,
        policy_fingerprint=fingerprint,
        profile_id=profile_id,
    )


def resolve_memory_policy(
    agent: AgentDefinition,
    resolved_profile: ResolvedAgentProfile,
) -> MemoryRuntimePolicy:
    """Resolve backend defaults -> agent options -> profile options, shallowly."""

    from cognis.providers.backends import get_backend

    capabilities = agent.capabilities
    backend_id = capabilities.memory_backend
    if backend_id == "none" or resolved_profile.memory_enabled is False:
        return disabled_memory_policy(
            backend_id=backend_id,
            profile_id=resolved_profile.profile_id,
        )

    try:
        descriptor = get_backend("memory", backend_id)
    except ValueError:
        return disabled_memory_policy(
            backend_id=backend_id,
            profile_id=resolved_profile.profile_id,
        )
    options_provider = descriptor.memory_options
    if options_provider is None:
        raise ValueError(f"Memory backend {backend_id!r} does not expose a runtime policy")

    # Provider options intentionally use shallow field-level override semantics.
    merged = dict(options_provider.defaults)
    merged.update(capabilities.memory_backend_options)
    merged.update(resolved_profile.memory_backend_options)
    validated = options_provider.validate_options(merged)
    return cast(
        MemoryRuntimePolicy,
        options_provider.resolve_policy(
            validated,
            profile_id=resolved_profile.profile_id,
        ),
    )


def memory_backend_descriptors() -> list[dict[str, Any]]:
    """Return authoritative memory backend metadata for API/UI consumers."""

    from cognis.providers.backends import get_backend, list_backends

    payload: list[dict[str, Any]] = []
    for backend_id in list_backends("memory"):
        descriptor = get_backend("memory", backend_id)
        options = descriptor.memory_options
        payload.append(
            {
                "id": descriptor.id,
                "display_name": descriptor.display_name,
                "description": descriptor.description,
                "defaults": dict(options.defaults) if options is not None else {},
                "merge_semantics": "shallow_field_override",
                "modes": [mode.to_dict() for mode in options.modes] if options is not None else [],
            }
        )
    return payload
