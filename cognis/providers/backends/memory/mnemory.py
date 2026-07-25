"""Memory backend: mnemory — wraps the global MnemoryProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cognis.providers.backends import register_backend
from cognis.providers.memory.policy import (
    MemoryModeDescriptor,
    MemoryRuntimePolicy,
    fingerprint_policy,
)

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.providers.registry import ProviderRegistry


_PROACTIVE_INSTRUCTIONS = """Mnemory is available as explicit tools.

Your core memories are already loaded into this conversation. Do not repeat automatic recall.
Search or find memories when they would materially improve the answer, and store durable user
facts, decisions, preferences, or reusable conclusions when useful. Avoid storing transient work."""

_ON_DEMAND_INSTRUCTIONS = """Mnemory is available on demand.

Use memory tools only when the user explicitly asks or when memory is clearly necessary to answer
correctly. Search before asking for previously known context. Store information only when requested
or when it is clearly durable and valuable."""


class MnemoryOptions:
    """Strict provider-owned Mnemory mode configuration."""

    defaults = {"mode": "full_auto"}
    modes = (
        MemoryModeDescriptor(
            id="full_auto",
            label="Full auto",
            description="Automatic recall and remembering with managed personality.",
            recommended_for="Personal assistants and long-lived general agents.",
            tooltip="Bootstrap instructions and core, recall every turn, and remember completed turns.",
            behavior={
                "core_bootstrap": True,
                "auto_recall": True,
                "auto_remember": True,
                "tools": True,
            },
        ),
        MemoryModeDescriptor(
            id="proactive",
            label="Proactive",
            description="Core context is loaded once; the agent uses memory tools when useful.",
            recommended_for="Coding agents and focused specialists.",
            tooltip="No automatic recall or remembering. Explicit memory tools remain available.",
            behavior={
                "core_bootstrap": True,
                "auto_recall": False,
                "auto_remember": False,
                "tools": True,
            },
        ),
        MemoryModeDescriptor(
            id="on_demand",
            label="On demand",
            description="Compact guidance only; memory is used only when clearly needed.",
            recommended_for="Small-context agents and occasional memory access.",
            tooltip="No core bootstrap, automatic recall, or automatic remembering.",
            behavior={
                "core_bootstrap": False,
                "auto_recall": False,
                "auto_remember": False,
                "tools": True,
            },
        ),
    )

    def validate_options(self, options: object) -> dict[str, Any]:
        if not isinstance(options, dict):
            raise ValueError("memory_backend_options must be an object")
        unknown = sorted(set(options) - {"mode"})
        if unknown:
            raise ValueError(f"Unknown Mnemory option(s): {', '.join(unknown)}")
        mode = options.get("mode", "full_auto")
        if mode not in {item.id for item in self.modes}:
            raise ValueError("Mnemory mode must be one of: full_auto, proactive, on_demand")
        return {"mode": mode}

    def resolve_policy(
        self,
        options: dict[str, Any],
        *,
        profile_id: str | None,
    ) -> MemoryRuntimePolicy:
        mode = str(options["mode"])
        settings = {
            "full_auto": (True, True, True, True, True, None),
            "proactive": (True, True, False, False, True, _PROACTIVE_INSTRUCTIONS),
            "on_demand": (True, False, False, False, True, _ON_DEMAND_INSTRUCTIONS),
        }[mode]
        enabled, bootstrap_instructions, auto_recall, auto_remember, tools, instructions = settings
        bootstrap_core = mode in {"full_auto", "proactive"}
        fingerprint = fingerprint_policy(
            backend_id="mnemory",
            options=options,
            enabled=enabled,
            bootstrap_instructions=bootstrap_instructions,
            bootstrap_core=bootstrap_core,
            auto_recall=auto_recall,
            auto_remember=auto_remember,
            tools_enabled=tools,
            instructions=instructions,
        )
        return MemoryRuntimePolicy(
            backend_id="mnemory",
            enabled=enabled,
            bootstrap_instructions=bootstrap_instructions,
            bootstrap_core=bootstrap_core,
            auto_recall=auto_recall,
            auto_remember=auto_remember,
            tools_enabled=tools,
            instructions=instructions,
            policy_fingerprint=fingerprint,
            mode_id=mode,
            profile_id=profile_id,
        )


MNEMORY_OPTIONS = MnemoryOptions()


@register_backend(
    kind="memory",
    id="mnemory",
    display_name="Mnemory",
    description="Persistent personal and agent memory backed by Mnemory.",
    memory_options=MNEMORY_OPTIONS,
)
def _factory(config: CognisConfig, registry: ProviderRegistry) -> Any:
    """Return the global Mnemory provider from the registry."""
    return registry.memory
