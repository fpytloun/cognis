"""Runtime profile resolution for per-agent inference variants."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from cognis.models.agent import AgentDefinition, AgentRuntimeProfile


def agent_profile_options(agent: AgentDefinition) -> list[dict[str, str | bool]]:
    """Return enabled runtime profiles in a tool-friendly discovery shape."""

    options = [
        {
            "profile_id": profile_id,
            "description": profile.description,
            "is_default": profile_id == agent.default_agent_profile_id,
            "synthetic": False,
        }
        for profile_id, profile in sorted(agent.agent_profiles.items())
        if profile.enabled
    ]
    if options:
        return options
    return [
        {
            "profile_id": "default",
            "description": "Synthetic default profile derived from the agent LLM configuration.",
            "is_default": True,
            "synthetic": True,
        }
    ]


def format_available_agent_profiles(agent: AgentDefinition) -> str:
    """Format discoverable profile IDs and descriptions for validation errors."""

    return "; ".join(
        f"{option['profile_id']}: {option['description']}"
        for option in agent_profile_options(agent)
    )


@dataclass(frozen=True, slots=True)
class ResolvedAgentProfile:
    """Effective runtime profile for one agent execution."""

    requested_profile_id: str | None
    profile_id: str
    source: str
    provider_id: str | None
    model: str | None
    reasoning_effort: str | None
    fast_mode: bool | None
    system_prompt_extra: str | None
    memory_enabled: bool | None
    memory_backend_options: dict[str, object]
    description: str
    synthetic: bool = False

    @property
    def system_prompt_extra_hash(self) -> str | None:
        if not self.system_prompt_extra:
            return None
        return sha256(self.system_prompt_extra.encode("utf-8")).hexdigest()

    def audit_metadata(self) -> dict[str, str | bool | None]:
        """Return non-secret profile metadata for runtime/audit records."""

        return {
            "requested_agent_profile_id": self.requested_profile_id,
            "resolved_agent_profile_id": self.profile_id,
            "agent_profile_source": self.source,
            "agent_profile_provider_id": self.provider_id,
            "agent_profile_model": self.model,
            "agent_profile_reasoning_effort": self.reasoning_effort,
            "agent_profile_fast_mode": self.fast_mode,
            "agent_profile_prompt_extra_hash": self.system_prompt_extra_hash,
            "agent_profile_synthetic": self.synthetic,
        }


def requested_agent_profile_id(session: object, conversation: object | None = None) -> str | None:
    """Return the effective explicit or turn-scoped profile request."""

    return requested_agent_profile_selection(session, conversation)[0]


def requested_agent_profile_selection(
    session: object,
    conversation: object | None = None,
) -> tuple[str | None, str | None]:
    """Return the profile request and provenance for an interactive turn."""

    session_profile = getattr(session, "agent_profile_id", None)
    if isinstance(session_profile, str) and session_profile.strip():
        return session_profile.strip(), "session"
    session_agent_id = getattr(session, "agent_id", None)
    conversation_agent_id = getattr(conversation, "agent_id", None)
    if (
        conversation is not None
        and isinstance(session_agent_id, str)
        and isinstance(conversation_agent_id, str)
        and session_agent_id != conversation_agent_id
    ):
        return None, None
    conversation_profile = getattr(conversation, "agent_profile_id", None)
    if isinstance(conversation_profile, str) and conversation_profile.strip():
        return conversation_profile.strip(), "conversation"
    channel_profile = getattr(session, "channel_default_agent_profile_id", None)
    if isinstance(channel_profile, str) and channel_profile.strip():
        return channel_profile.strip(), "channel_default"
    return None, None


def resolve_conversation_agent_profile(
    agent: AgentDefinition,
    session: object,
    conversation: object | None = None,
) -> ResolvedAgentProfile:
    """Resolve an interactive turn using persisted overrides before channel fallback."""

    profile_id, source = requested_agent_profile_selection(session, conversation)
    return resolve_agent_profile(agent, profile_id, source=source or "explicit")


def normalize_agent_profile_id(value: object) -> str | None:
    """Normalize optional agent profile identifiers from API/tool input."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("agent_profile_id must be a string or null")
    normalized = value.strip()
    if not normalized:
        return None
    if "/" in normalized:
        raise ValueError("agent_profile_id must not contain '/'")
    return normalized


def validate_agent_profile_configuration(agent: AgentDefinition) -> None:
    """Validate configured default profile integrity without changing fallback semantics."""

    default_id = normalize_agent_profile_id(agent.default_agent_profile_id)
    if default_id is None:
        return
    default = agent.agent_profiles.get(default_id)
    if default is None:
        raise ValueError("default_agent_profile_id must reference an existing runtime profile")
    if not default.enabled:
        raise ValueError("default_agent_profile_id must reference an enabled runtime profile")


def resolve_agent_profile(
    agent: AgentDefinition,
    requested_profile_id: str | None = None,
    *,
    source: str = "explicit",
) -> ResolvedAgentProfile:
    """Resolve an agent-local runtime profile with a safe synthetic fallback.

    Profiles tune only inference/runtime behavior. They never change agent
    identity, memory scope, ownership, permissions, tool rights, or audit agent.
    """

    requested = normalize_agent_profile_id(requested_profile_id)
    profiles = agent.agent_profiles or {}
    if requested is not None:
        profile = profiles.get(requested)
        if profile is None:
            if requested == "default":
                return _synthetic_default(agent, requested_profile_id=requested, source=source)
            raise ValueError(
                f"Agent profile '{requested}' does not exist for agent '{agent.agent_id}'. "
                f"Available profiles: {format_available_agent_profiles(agent)}"
            )
        return _resolved_from_profile(requested, profile, requested, source)

    default_id = normalize_agent_profile_id(agent.default_agent_profile_id)
    if default_id is not None:
        profile = profiles.get(default_id)
        if profile is not None and profile.enabled:
            return _resolved_from_profile(None, profile, default_id, "agent_default")

    return _synthetic_default(agent)


def _synthetic_default(
    agent: AgentDefinition,
    *,
    requested_profile_id: str | None = None,
    source: str = "synthetic_default",
) -> ResolvedAgentProfile:
    llm_config = agent.llm_config
    return ResolvedAgentProfile(
        requested_profile_id=requested_profile_id,
        profile_id="default",
        source=source if requested_profile_id is not None else "synthetic_default",
        provider_id=llm_config.provider_id if llm_config else None,
        model=llm_config.model if llm_config else None,
        reasoning_effort=llm_config.reasoning_effort if llm_config else None,
        fast_mode=llm_config.fast_mode if llm_config else None,
        system_prompt_extra=None,
        memory_enabled=None,
        memory_backend_options={},
        description="Synthetic default profile derived from the agent LLM configuration.",
        synthetic=True,
    )


def _resolved_from_profile(
    requested_profile_id: str | None,
    profile: AgentRuntimeProfile,
    profile_id: str,
    source: str,
) -> ResolvedAgentProfile:
    if not profile.enabled:
        raise ValueError(f"Agent profile '{profile_id}' is disabled")
    embedded_id = normalize_agent_profile_id(profile.profile_id)
    if embedded_id is not None and embedded_id != profile_id:
        raise ValueError(
            f"Agent profile key '{profile_id}' does not match embedded profile_id '{embedded_id}'"
        )
    return ResolvedAgentProfile(
        requested_profile_id=requested_profile_id,
        profile_id=profile_id,
        source=source,
        provider_id=profile.provider_id,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        fast_mode=profile.fast_mode,
        system_prompt_extra=profile.system_prompt_extra,
        memory_enabled=profile.memory_enabled,
        memory_backend_options=dict(profile.memory_backend_options),
        description=profile.description,
        synthetic=False,
    )


def agent_switch_eligible_profiles(agent: AgentDefinition) -> list[tuple[str, str]]:
    """Return enabled profiles an agent may select dynamically."""

    return [
        (profile_id, profile.description)
        for profile_id, profile in sorted((getattr(agent, "agent_profiles", {}) or {}).items())
        if profile.enabled and profile.agent_switchable
    ]


def render_agent_profile_context(
    resolved: ResolvedAgentProfile,
    *,
    switch_eligible_profiles: list[tuple[str, str]] | None = None,
) -> str | None:
    """Render the dynamic prompt block for a resolved runtime profile."""

    eligible_profiles = switch_eligible_profiles or []
    if not resolved.system_prompt_extra and resolved.synthetic and not eligible_profiles:
        return None
    lines = [
        "<agent_runtime_profile>",
        f"Profile: {resolved.profile_id}",
        f"Source: {resolved.source}",
    ]
    if resolved.description:
        lines.append(f"Description: {resolved.description}")
    lines.extend(
        [
            "This profile may tune runtime behavior, inference, and provider-owned memory "
            "behavior only. It does not redefine identity, ownership, permissions, tool "
            "allowlists, or audit identity. Memory changes apply on the next logical turn.",
        ]
    )
    if resolved.system_prompt_extra:
        lines.extend(
            [
                "",
                "<profile_instructions>",
                resolved.system_prompt_extra.strip(),
                "</profile_instructions>",
            ]
        )
    if eligible_profiles:
        lines.extend(["", "<switch_eligible_profiles>"])
        alternatives = 0
        for profile_id, description in eligible_profiles:
            current = profile_id == resolved.profile_id
            if not current:
                alternatives += 1
            suffix = " (current)" if current else ""
            lines.append(f"- {profile_id}{suffix}: {description}")
        lines.append("</switch_eligible_profiles>")
        lines.extend(
            [
                "",
                "<profile_routing_guidance>",
                "The profiles above are the only runtime profiles you may select with "
                "switch_agent_profile. Keep the current profile when it is adequate. "
                "Upgrade for complex, uncertain, high-impact, or failure-prone remaining work; "
                "downgrade for bounded, routine, low-risk remaining work. Base the decision on "
                "task needs, not stylistic preference. A profile switch cannot change identity, "
                "memory scope, ownership, permissions, tools, or audit identity. Call "
                "switch_agent_profile alone and provide a concise operational reason, not private "
                "chain-of-thought. At most one successful switch is allowed per logical turn.",
            ]
        )
        if alternatives == 0:
            lines.append(
                "No alternative profile is currently eligible, so do not call switch_agent_profile."
            )
        lines.append("</profile_routing_guidance>")
    lines.append("</agent_runtime_profile>")
    return "\n".join(lines)
