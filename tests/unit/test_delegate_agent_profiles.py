from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cognis.models.agent import AgentDefinition
from cognis.models.tool import ToolCall
from cognis.tools.builtin.orchestration import handle_delegate_tool_call


class _FakeSessionManager:
    def __init__(self) -> None:
        self.created_kwargs: dict[str, object] | None = None

    async def create_child_session(self, **kwargs: object) -> SimpleNamespace:
        self.created_kwargs = dict(kwargs)
        return SimpleNamespace(
            session_id="child-session",
            agent_profile_id=kwargs.get("agent_profile_id"),
        )


class _FakeAgentRegistry:
    def __init__(self, target: AgentDefinition) -> None:
        self.target = target

    async def get(self, agent_id: str, *, owner_email: str | None = None) -> AgentDefinition | None:
        if agent_id == self.target.agent_id:
            return self.target
        return None

    async def is_secondary_bound(self, parent_agent_id: str, secondary_agent_id: str) -> bool:
        return True


def _agent(
    agent_id: str, *, profiles: dict[str, dict[str, object]] | None = None
) -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "agent_id": agent_id,
            "owner_email": "user@example.com",
            "name": agent_id,
            "agent_profiles": profiles or {},
            "default_agent_profile_id": next(iter(profiles), None) if profiles else None,
        }
    )


@pytest.mark.asyncio
async def test_delegate_does_not_leak_parent_profile_to_different_agent() -> None:
    parent_agent = _agent(
        "laforge",
        profiles={"smart": {"profile_id": "smart", "description": "Careful"}},
    )
    target_agent = _agent("system:explore")
    session_manager = _FakeSessionManager()

    result, child_session = await handle_delegate_tool_call(
        ToolCall(
            call_id="call_1",
            name="delegate",
            arguments={
                "task": "inspect code",
                "agent_id": "system:explore",
                # Simulates caller/runtime leakage of the parent's current profile.
                "agent_profile_id": "smart",
                "expected_output": "summary",
            },
        ),
        session_manager=session_manager,
        session=SimpleNamespace(
            session_id="parent-session",
            user_email="user@example.com",
            agent_id="laforge",
            agent_profile_id="smart",
        ),
        agent=parent_agent,
        agent_registry=_FakeAgentRegistry(target_agent),
        wait=True,
    )

    payload = json.loads(result.output)

    assert result.is_error is False
    assert payload["status"] == "accepted"
    assert child_session is not None
    assert child_session.agent_profile_id is None
    assert session_manager.created_kwargs is not None
    assert session_manager.created_kwargs["agent_id"] == "system:explore"
    assert session_manager.created_kwargs["agent_profile_id"] is None
