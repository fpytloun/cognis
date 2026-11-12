"""Cognis-owned capability snapshots for delegated execution roles."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from cognis.core.agent_profiles import resolve_conversation_agent_profile
from cognis.models.agent import AgentCapabilities, AgentDefinition
from cognis.models.session import SessionModel

DELEGATE_CONFIGURATION_KEY = "delegate_configuration"


class DelegateConfiguration(BaseModel):
    """Configuration only: never identity, inference routing, or access grants."""

    model_config = ConfigDict(extra="forbid")

    capabilities: AgentCapabilities

    @classmethod
    def capture(
        cls, agent: AgentDefinition, session: SessionModel, conversation: object | None = None
    ) -> DelegateConfiguration:
        """Freeze the caller's effective memory options and capability choices."""
        profile = resolve_conversation_agent_profile(agent, session, conversation)
        capabilities = agent.capabilities.model_copy(deep=True)
        capabilities.memory_backend_options.update(profile.memory_backend_options)
        if profile.memory_enabled is False:
            capabilities.memory_backend = "none"
            capabilities.memory_backend_options = {}
        return cls(capabilities=capabilities)

    def apply(self, specialist: AgentDefinition) -> AgentDefinition:
        """Bind configuration while preserving the specialist and its role restrictions."""
        profiles = {
            key: profile.model_copy(update={"memory_backend_options": {}})
            for key, profile in specialist.agent_profiles.items()
        }
        return specialist.model_copy(
            update={
                "capabilities": self.capabilities.model_copy(deep=True),
                "agent_profiles": profiles,
            }
        )


def delegated_configuration(metadata: dict[str, Any]) -> DelegateConfiguration | None:
    """Decode only controller-written configuration, rejecting malformed snapshots."""
    snapshot = metadata.get(DELEGATE_CONFIGURATION_KEY)
    return DelegateConfiguration.model_validate(snapshot) if snapshot is not None else None
