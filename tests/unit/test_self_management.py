from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core import agent_management
from cognis.providers.backends.guardrails.null import NoGuardrailsProvider


@pytest.mark.asyncio
async def test_owned_self_read_and_unapproved_mutation(monkeypatch):
    @asynccontextmanager
    async def session_factory():
        yield object()

    row = SimpleNamespace(owner_email="owner@example.com", is_system=False)
    monkeypatch.setattr(agent_management, "get_agent", AsyncMock(return_value=row))
    deps = SimpleNamespace(session_factory=session_factory)
    arguments = {"action": "get", "agent_id": "reader"}
    assert (
        await agent_management._require_owned_target(deps, "owner@example.com", "reader", arguments)
        is row
    )
    with pytest.raises(agent_management.AgentManagementError, match="Resource access denied"):
        await agent_management._require_owned_target(deps, "other@example.com", "reader", arguments)
    with pytest.raises(agent_management.AgentManagementError, match="explicit user approval"):
        await agent_management.handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="reader",
            arguments={"action": "update", "agent_id": "reader", "name": "Changed"},
        )


@pytest.mark.asyncio
async def test_disabled_guardrails_forward_mandatory_floor():
    intaris = SimpleNamespace(evaluate=AsyncMock())
    provider = NoGuardrailsProvider(intaris)
    context = {"minimum_outcome": "escalate"}
    await provider.evaluate("session", "manage_agents", {}, context)
    intaris.evaluate.assert_awaited_once_with("session", "manage_agents", {}, context)
