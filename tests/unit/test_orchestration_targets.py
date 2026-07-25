from __future__ import annotations

import pytest

from cognis.core.orchestration_targets import (
    OrchestrationTargetError,
    OrchestrationTargetMode,
    OrchestrationTargetService,
)
from cognis.models.agent import AgentDefinition


def _agent(
    agent_id: str,
    *,
    agent_type: str,
    is_system: bool = False,
    hidden: bool = False,
    disabled: bool = False,
    status: str = "active",
    owner_email: str = "user@example.com",
    description: str | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        owner_email=owner_email,
        name=agent_id,
        description=description,
        agent_type=agent_type,
        is_system=is_system,
        hidden=hidden,
        disabled=disabled,
        status=status,
    )


class _Registry:
    def __init__(
        self,
        agents: list[AgentDefinition],
        *,
        bindings: set[str] | None = None,
    ) -> None:
        self.agents = {agent.agent_id: agent for agent in agents}
        self.bindings = bindings or set()

    async def list_all(self, **kwargs: object) -> list[AgentDefinition]:
        del kwargs
        return list(self.agents.values())

    async def list_secondary_bindings(self, primary_agent_id: str) -> list[str]:
        del primary_agent_id
        return sorted(self.bindings)

    async def get(
        self,
        agent_id: str,
        *,
        owner_email: str | None = None,
    ) -> AgentDefinition | None:
        del owner_email
        return self.agents.get(agent_id)


@pytest.mark.asyncio
async def test_target_snapshot_applies_typed_eligibility_matrix() -> None:
    controller = _agent("laforge", agent_type="primary")
    system_secondary = _agent(
        "system:code-review",
        agent_type="secondary",
        is_system=True,
        description="Findings-first review",
    )
    bound_secondary = _agent("custom-reviewer", agent_type="secondary")
    unbound_secondary = _agent("custom-implementer", agent_type="secondary")
    hidden_secondary = _agent(
        "system:hidden",
        agent_type="secondary",
        is_system=True,
        hidden=True,
    )
    disabled_secondary = _agent(
        "system:disabled",
        agent_type="secondary",
        is_system=True,
        disabled=True,
    )
    inactive_secondary = _agent(
        "inactive-reviewer",
        agent_type="secondary",
        status="suspended",
    )
    other_primary = _agent("lumi", agent_type="primary")
    system_primary = _agent("system:primary", agent_type="primary", is_system=True)
    registry = _Registry(
        [
            controller,
            system_secondary,
            bound_secondary,
            unbound_secondary,
            hidden_secondary,
            disabled_secondary,
            inactive_secondary,
            other_primary,
            system_primary,
        ],
        bindings={"custom-reviewer"},
    )

    snapshot = await OrchestrationTargetService(registry).snapshot(
        controller_agent=controller,
        user_email="user@example.com",
    )

    assert [target.agent_id for target in snapshot.delegate] == [
        "custom-reviewer",
        "system:code-review",
    ]
    assert [target.agent_id for target in snapshot.managed] == ["laforge", "lumi"]
    code_review = next(
        target for target in snapshot.delegate if target.agent_id == "system:code-review"
    )
    assert code_review.description == "Findings-first review"


@pytest.mark.asyncio
async def test_user_secondary_requires_binding() -> None:
    controller = _agent("laforge", agent_type="primary")
    secondary = _agent("custom-reviewer", agent_type="secondary")
    service = OrchestrationTargetService(_Registry([controller, secondary]))

    with pytest.raises(OrchestrationTargetError) as exc_info:
        await service.require(
            OrchestrationTargetMode.DELEGATE,
            target_agent_id=secondary.agent_id,
            controller_agent=controller,
            user_email="user@example.com",
        )

    assert exc_info.value.code == "delegate_target_not_eligible"


@pytest.mark.asyncio
async def test_revalidation_rejects_target_disabled_after_discovery() -> None:
    controller = _agent("laforge", agent_type="primary")
    secondary = _agent("system:code-review", agent_type="secondary", is_system=True)
    registry = _Registry([controller, secondary])
    service = OrchestrationTargetService(registry)
    snapshot = await service.snapshot(
        controller_agent=controller,
        user_email="user@example.com",
    )
    assert [target.agent_id for target in snapshot.delegate] == [secondary.agent_id]

    registry.agents[secondary.agent_id] = secondary.model_copy(
        update={"disabled": True, "status": "disabled"}
    )

    with pytest.raises(OrchestrationTargetError) as exc_info:
        await service.require(
            OrchestrationTargetMode.DELEGATE,
            target_agent_id=secondary.agent_id,
            controller_agent=controller,
            user_email="user@example.com",
        )

    assert exc_info.value.code == "delegate_target_not_eligible"


@pytest.mark.asyncio
async def test_managed_target_preserves_top_level_self_and_rejects_secondary() -> None:
    controller = _agent("laforge", agent_type="primary")
    secondary = _agent("system:architect", agent_type="secondary", is_system=True)
    service = OrchestrationTargetService(_Registry([controller, secondary]))

    resolved = await service.require(
        OrchestrationTargetMode.MANAGED,
        target_agent_id=controller.agent_id,
        controller_agent=controller,
        user_email="user@example.com",
    )
    assert resolved.agent_id == controller.agent_id

    with pytest.raises(OrchestrationTargetError) as exc_info:
        await service.require(
            OrchestrationTargetMode.MANAGED,
            target_agent_id=secondary.agent_id,
            controller_agent=controller,
            user_email="user@example.com",
        )
    assert exc_info.value.code == "managed_target_not_eligible"
