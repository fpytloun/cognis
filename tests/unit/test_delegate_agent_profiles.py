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


class _FailingSessionManager:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.create_child_session_called = False

    async def create_child_session(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        self.create_child_session_called = True
        raise self.error


class _FakeAgentRegistry:
    def __init__(
        self,
        *targets: AgentDefinition,
        bindings: set[str] | None = None,
    ) -> None:
        self.targets = {target.agent_id: target for target in targets}
        self.bindings = bindings or set()

    async def get(self, agent_id: str, *, owner_email: str | None = None) -> AgentDefinition | None:
        del owner_email
        return self.targets.get(agent_id)

    async def list_all(self, **kwargs: object) -> list[AgentDefinition]:
        del kwargs
        return list(self.targets.values())

    async def list_secondary_bindings(self, parent_agent_id: str) -> list[str]:
        del parent_agent_id
        return sorted(self.bindings)


def _agent(
    agent_id: str,
    *,
    agent_type: str = "primary",
    is_system: bool = False,
    profiles: dict[str, dict[str, object]] | None = None,
) -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "agent_id": agent_id,
            "owner_email": "user@example.com",
            "name": agent_id,
            "agent_type": agent_type,
            "is_system": is_system,
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
    target_agent = _agent("system:explore", agent_type="secondary", is_system=True)
    session_manager = _FakeSessionManager()

    result, child_session = await handle_delegate_tool_call(
        ToolCall(
            call_id="call_1",
            name="delegate",
            arguments={
                "task": "inspect code",
                "agent_id": "system:explore",
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


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_agent_before_creating_child_session() -> None:
    parent_agent = _agent("laforge")
    session_manager = _FakeSessionManager()

    result, child_session = await handle_delegate_tool_call(
        ToolCall(
            call_id="call_unknown_agent",
            name="delegate",
            arguments={"task": "inspect code", "agent_id": "system:design-review"},
        ),
        session_manager=session_manager,
        session=SimpleNamespace(session_id="parent-session", user_email="user@example.com"),
        agent=parent_agent,
        agent_registry=_FakeAgentRegistry(
            _agent("system:explore", agent_type="secondary", is_system=True)
        ),
        wait=True,
    )

    assert child_session is None
    assert result.is_error is True
    assert json.loads(result.output) == {
        "call_id": "call_unknown_agent",
        "code": "delegate_target_not_eligible",
        "message": (
            "Agent 'system:design-review' is not an eligible delegate target. "
            "Use an eligible secondary specialist returned by the delegate target catalog."
        ),
        "mode": "delegate",
        "status": "error",
    }
    assert session_manager.created_kwargs is None


@pytest.mark.asyncio
async def test_delegate_maps_unknown_agent_value_error_to_safe_structured_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    parent_agent = _agent("laforge")
    session_manager = _FailingSessionManager(ValueError("Unknown agent: system:design-review"))

    with caplog.at_level("ERROR"):
        result, child_session = await handle_delegate_tool_call(
            ToolCall(
                call_id="call_child_creation",
                name="delegate",
                arguments={"task": "inspect code", "agent_id": "system:explore"},
            ),
            session_manager=session_manager,
            session=SimpleNamespace(session_id="parent-session", user_email="user@example.com"),
            agent=parent_agent,
            agent_registry=_FakeAgentRegistry(
                _agent("system:explore", agent_type="secondary", is_system=True)
            ),
            wait=True,
        )

    assert child_session is None
    assert result.is_error is True
    assert json.loads(result.output) == {
        "call_id": "call_child_creation",
        "code": "delegate_agent_not_found",
        "message": "Agent 'system:explore' not found.",
        "mode": "delegate",
        "status": "error",
    }
    assert session_manager.create_child_session_called is True
    assert (
        "call_id=call_child_creation parent_session_id=parent-session "
        "requested_agent_id=system:explore" in (caplog.text)
    )
    assert "ValueError: Unknown agent: system:design-review" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        (
            ValueError("invalid delegated child state"),
            "delegate_child_session_invalid",
            "Delegated child session request is invalid.",
        ),
        (
            RuntimeError("database unavailable"),
            "delegate_child_session_creation_failed",
            "Unable to create delegated child session.",
        ),
    ],
)
async def test_delegate_maps_child_creation_errors_to_safe_structured_results(
    error: Exception,
    expected_code: str,
    expected_message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_manager = _FailingSessionManager(error)

    with caplog.at_level("ERROR"):
        result, child_session = await handle_delegate_tool_call(
            ToolCall(
                call_id="call_child_creation_error",
                name="delegate",
                arguments={"task": "inspect code", "agent_id": "system:explore"},
            ),
            session_manager=session_manager,
            session=SimpleNamespace(session_id="parent-session", user_email="user@example.com"),
            agent=_agent("laforge"),
            agent_registry=_FakeAgentRegistry(
                _agent("system:explore", agent_type="secondary", is_system=True)
            ),
            wait=True,
        )

    assert child_session is None
    assert result.is_error is True
    payload = json.loads(result.output)
    assert payload["code"] == expected_code
    assert payload["message"] == expected_message
    assert session_manager.create_child_session_called is True
    assert "call_id=call_child_creation_error parent_session_id=parent-session" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("target_agent_id", [None, "", "laforge", "lumi"])
async def test_delegate_requires_eligible_secondary_target(
    target_agent_id: str | None,
) -> None:
    parent_agent = _agent("laforge")
    targets = [
        parent_agent,
        _agent("lumi"),
        _agent("system:explore", agent_type="secondary", is_system=True),
    ]
    session_manager = _FakeSessionManager()
    arguments: dict[str, object] = {"task": "inspect code"}
    if target_agent_id is not None:
        arguments["agent_id"] = target_agent_id

    result, child_session = await handle_delegate_tool_call(
        ToolCall(call_id="call_invalid_target", name="delegate", arguments=arguments),
        session_manager=session_manager,
        session=SimpleNamespace(session_id="parent-session", user_email="user@example.com"),
        agent=parent_agent,
        agent_registry=_FakeAgentRegistry(*targets),
        wait=True,
    )

    assert child_session is None
    assert result.is_error is True
    assert json.loads(result.output)["code"] in {
        "delegate_target_required",
        "delegate_target_not_eligible",
    }
    assert session_manager.created_kwargs is None
