"""Runtime profile resolution for per-agent inference variants."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from cognis.models.agent import AgentDefinition, AgentRuntimeProfile


@dataclass(frozen=True, slots=True)
class ResolvedAgentProfile:
    """Effective runtime profile for one agent execution."""

    requested_profile_id: str | None
    profile_id: str
    source: str
    provider_id: str | None
    model: str | None
    reasoning_effort: str | None
    system_prompt_extra: str | None
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
            "agent_profile_prompt_extra_hash": self.system_prompt_extra_hash,
            "agent_profile_synthetic": self.synthetic,
        }


def requested_agent_profile_id(session: object, conversation: object | None = None) -> str | None:
    """Return the persisted profile request for a session/conversation pair."""

    session_profile = getattr(session, "agent_profile_id", None)
    if isinstance(session_profile, str) and session_profile.strip():
        return session_profile.strip()
    session_agent_id = getattr(session, "agent_id", None)
    conversation_agent_id = getattr(conversation, "agent_id", None)
    if (
        conversation is not None
        and isinstance(session_agent_id, str)
        and isinstance(conversation_agent_id, str)
        and session_agent_id != conversation_agent_id
    ):
        return None
    conversation_profile = getattr(conversation, "agent_profile_id", None)
    if isinstance(conversation_profile, str) and conversation_profile.strip():
        return conversation_profile.strip()
    return None


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
                f"Agent profile '{requested}' does not exist for agent '{agent.agent_id}'"
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
        system_prompt_extra=None,
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
        system_prompt_extra=profile.system_prompt_extra,
        description=profile.description,
        synthetic=False,
    )


def render_agent_profile_context(resolved: ResolvedAgentProfile) -> str | None:
    """Render the dynamic prompt block for a resolved runtime profile."""

    if not resolved.system_prompt_extra and resolved.synthetic:
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
            "This profile may tune runtime behavior and inference only. It does not redefine "
            "identity, memory scope, ownership, permissions, tools, or audit identity.",
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
    lines.append("</agent_runtime_profile>")
    return "\n".join(lines)
