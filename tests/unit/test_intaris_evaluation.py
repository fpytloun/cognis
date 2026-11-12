"""Evaluation selection is human-owned and acknowledged at the provider boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from cognis.core.agent_loop import AgentLoop
from cognis.core.intaris_policy import profile_mutation_error, tool_session_policy
from cognis.core.session import _intaris_session_policy
from cognis.models.agent import AgentCapabilities
from cognis.models.tool import ToolCall
from cognis.models.workflow import SessionPolicy, WorkflowDefaults, merge_session_policies
from cognis.providers.guardrails.errors import IntarisHTTPError
from cognis.providers.guardrails.intaris import IntarisProvider, _safe_evaluation_error_detail
from cognis.tools.builtin.agent_management import handle_agent_management_tool
from cognis.tools.builtin.schedule import handle_schedule_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [422, 413])
async def test_evaluate_http_diagnostics_exclude_response_echoes(status: int) -> None:
    secret = "private-mail-and-credential"
    detail = (
        [
            {
                "loc": ["body", "context"],
                "type": "value_error",
                "msg": secret,
                "input": {"context": secret},
                "ctx": {"error": secret},
            },
            {"loc": ["body", secret], "type": secret},
        ]
        if status == 422
        else {
            "code": "context_insufficient",
            "message": secret,
            "model_view": {
                "bytes": 45000,
                "estimated_input_tokens": 11250,
                "input_ceiling_tokens": 8192,
                "framing_tokens": True,
                "margin_tokens": secret,
                "prompt": secret,
            },
        }
    )
    requests = []
    responses = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = httpx.Response(status, json={"detail": detail, "input": secret})
        responses.append(response)
        return response

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await provider.evaluate("session-evaluation", "read", {"path": secret})
        error = caught.value
        assert type(error) is IntarisHTTPError
        assert error.request is requests[0]
        assert error.response is responses[0]
        assert error.response.status_code == status
        assert len(requests) == 1
        assert secret not in str(error)
        assert type(error.__cause__) is httpx.HTTPStatusError
        diagnostic = str(error.__cause__)
        assert secret not in diagnostic
        if status == 422:
            assert '"loc":["body","context"],"type":"value_error"' in diagnostic
            assert '"type":"unknown"' in diagnostic
            assert "input" not in diagnostic
        else:
            assert '"code":"context_insufficient"' in diagnostic
            assert '"estimated_input_tokens":11250' in diagnostic
            assert "framing_tokens" not in diagnostic
            assert "margin_tokens" not in diagnostic
    finally:
        await provider.client.aclose()


@pytest.mark.parametrize("field,limit", [("args", 65536), ("context", 8192)])
def test_size_diagnostics_report_only_field_category_and_valid_counts(field, limit):
    response = httpx.Response(
        422,
        json={
            "detail": [
                {
                    "loc": ["body", field],
                    "type": f"{field}_too_large",
                    "msg": "private-body",
                    "input": {"secret": "private-body"},
                    "ctx": {
                        "size": limit + 100,
                        "limit": limit,
                        "field": field,
                        "other": "private-body",
                    },
                }
            ]
        },
    )
    assert _safe_evaluation_error_detail(response) == {
        "validation": [
            {
                "loc": ["body", field],
                "type": f"{field}_too_large",
                "size": limit + 100,
                "limit": limit,
            }
        ]
    }
    response = httpx.Response(
        422,
        json={
            "detail": [
                {
                    "loc": ["body", field],
                    "type": f"{field}_too_large",
                    "ctx": {"size": True, "limit": "private-body"},
                }
            ]
        },
    )
    assert _safe_evaluation_error_detail(response) == {
        "validation": [{"loc": ["body", field], "type": f"{field}_too_large"}]
    }


@pytest.mark.parametrize("mode", ["legacy", "strict", "balanced", "bounded_autonomy"])
def test_typed_evaluation_and_precedence(mode: str) -> None:
    agent = SimpleNamespace(capabilities=AgentCapabilities(intaris_evaluation=mode))
    policy = _intaris_session_policy("/tmp/work", agent=agent)
    assert policy["evaluation_policy_version"] == ("legacy" if mode == "legacy" else "hybrid_v1")
    assert policy.get("profile") == (None if mode == "legacy" else mode)
    overridden = _intaris_session_policy(
        "/tmp/work",
        agent=agent,
        session_policy={"intaris_evaluation": "strict", "deny_policies": ["No deployment"]},
    )
    assert overridden["profile"] == "strict"
    assert overridden["deny_policies"] == ["No deployment"]
    assert "intaris_evaluation" not in overridden
    inherited = _intaris_session_policy(
        "/tmp/work", agent=agent, session_policy={"intaris_evaluation": None}
    )
    assert inherited == policy


def test_default_validation_and_workflow_has_no_fourth_precedence() -> None:
    assert AgentCapabilities().intaris_evaluation == "balanced"
    assert _intaris_session_policy(None)["evaluation_policy_version"] == "hybrid_v1"
    assert _intaris_session_policy(None)["profile"] == "balanced"
    assert AgentCapabilities(intaris_evaluation="legacy").intaris_evaluation == "legacy"
    for value in (None, "hybrid_v1", "", 1):
        with pytest.raises(ValidationError):
            AgentCapabilities(intaris_evaluation=value)
    with pytest.raises(ValidationError):
        SessionPolicy(intaris_evaluation="unknown")
    with pytest.raises(ValidationError):
        WorkflowDefaults(session_policy={"intaris_evaluation": "legacy"})
    merged = merge_session_policies(
        {"allow_policies": ["Read source"], "deny_policies": ["No production"]},
        {"intaris_evaluation": "balanced", "deny_policies": ["No push"]},
    )
    assert merged == {
        "intaris_evaluation": "balanced",
        "allow_policies": ["Read source"],
        "deny_policies": ["No production", "No push"],
    }


@pytest.mark.parametrize("mode", ["legacy", "strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize("replacement", [None, {}, {"allow_policies": ["Read source"]}])
def test_prose_updates_preserve_human_mode(mode: str, replacement: dict | None) -> None:
    assert tool_session_policy(replacement, {"intaris_evaluation": mode}) == {
        **(replacement or {}),
        "intaris_evaluation": mode,
    }


@pytest.mark.parametrize("mode", [None, "legacy", "bounded_autonomy"])
@pytest.mark.parametrize("nested", [False, True])
def test_nested_metadata_cannot_mutate_selection(mode: str | None, nested: bool) -> None:
    arguments = {"session_policy": {"intaris_evaluation": mode}}
    if nested:
        arguments = {"task": {"template": [arguments]}}
    assert profile_mutation_error(arguments).is_error
    assert profile_mutation_error({"session_policy": {"allow_policies": ["Read source"]}}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_task", "update_task"])
async def test_task_tool_rejects_mode_before_any_database_mutation(tool: str) -> None:
    result = await AgentLoop._handle_task_tool(
        Mock(),
        ToolCall(
            call_id="call-evaluation",
            name=tool,
            arguments={"session_policy": {"intaris_evaluation": None}},
        ),
        ctx=Mock(),
        events_to_record=[],
    )
    assert result.is_error
    assert "human_policy_selection_required" in result.output


@pytest.mark.asyncio
async def test_compose_rejects_nested_task_mode_before_mutation() -> None:
    result = await AgentLoop._handle_composition_tool(
        Mock(),
        ToolCall(
            call_id="call-compose-evaluation",
            name="compose_and_run_workflow",
            arguments={"task": {"session_policy": {"intaris_evaluation": "legacy"}}},
        ),
        ctx=Mock(),
        events_to_record=[],
    )
    assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "update"])
async def test_schedule_tool_rejects_nested_mode(action: str) -> None:
    result = await handle_schedule_tool(
        "manage_schedules",
        {"action": action, "task_template": {"session_policy": {"intaris_evaluation": None}}},
        session_factory=Mock(),
        scheduler=None,
        user_email="owner@test.local",
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_agent_tool_rejects_mode_without_root_owner_context() -> None:
    result = await handle_agent_management_tool(
        tool_name="manage_agents",
        arguments={"action": "settings_update", "settings": {"intaris_evaluation": "legacy"}},
        deps=Mock(),
        user_email="owner@test.local",
        current_agent_id="agent-evaluation",
        self_mutation_approved=True,
    )
    assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "conflict"])
@pytest.mark.parametrize("mode", ["legacy", "strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize("matches", [True, False])
async def test_wire_acknowledgement(operation: str, mode: str, matches: bool) -> None:
    import json

    policy = {"evaluation_policy_version": "legacy" if mode == "legacy" else "hybrid_v1"}
    if mode != "legacy":
        policy["profile"] = mode
    acknowledged = (
        dict(policy) if matches else {"evaluation_policy_version": "hybrid_v1", "profile": "wrong"}
    )
    if mode == "legacy" and matches:
        # de115a5 silently drops unknown fields on both mutations.
        acknowledged = {}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            assert json.loads(request.content)["policy"] == policy
        if operation == "conflict" and request.method == "POST":
            return httpx.Response(409, json={})
        return httpx.Response(
            200,
            json={
                "active_policy": acknowledged,
                "policy": acknowledged,
                "analytics_enabled": True,
                "guardrails_enabled": True,
            },
        )

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:

        async def invoke() -> None:
            if operation == "update":
                await provider.update_session_policy(
                    "session-evaluation", agent_id="agent-evaluation", policy=policy
                )
            else:
                await provider.create_session(
                    "session-evaluation", "Validate evaluation", "agent-evaluation", policy=policy
                )

        if matches:
            await invoke()
        else:
            with pytest.raises(ValueError, match="did not acknowledge"):
                await invoke()
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
async def test_hybrid_cannot_silently_run_on_old_intention_ack() -> None:
    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "ok": True,
                    "analytics_enabled": True,
                    "guardrails_enabled": True,
                },
            )
        ),
    )
    try:
        with pytest.raises(ValueError, match="active_policy"):
            await provider.create_session(
                "session-evaluation",
                "Validate evaluation",
                "agent-evaluation",
                policy={"evaluation_policy_version": "hybrid_v1", "profile": "strict"},
            )
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "conflict"])
async def test_stored_policy_is_not_hybrid_activation_ack(operation: str) -> None:
    policy = {"evaluation_policy_version": "hybrid_v1", "profile": "strict"}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={})
        return httpx.Response(
            200,
            json={
                "policy": policy,
                "analytics_enabled": True,
                "guardrails_enabled": True,
            },
        )

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(ValueError, match="active_policy"):
            if operation == "update":
                await provider.update_session_policy(
                    "session-evaluation", agent_id="agent-evaluation", policy=policy
                )
            else:
                await provider.create_session(
                    "session-evaluation", "Validate evaluation", "agent-evaluation", policy=policy
                )
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("stored_maximum", ["escalate", "approve"])
async def test_hybrid_maximum_outcome_ack_uses_stored_policy(
    operation: str, stored_maximum: str
) -> None:
    """Real Intaris keeps maximum_outcome in ``policy``, not in the snapshot."""
    policy = {
        "evaluation_policy_version": "hybrid_v1",
        "profile": "bounded_autonomy",
        "maximum_outcome": "escalate",
    }
    snapshot = {"evaluation_policy_version": "hybrid_v1", "profile": "bounded_autonomy"}
    stored = {**policy, "maximum_outcome": stored_maximum, "active_policy": snapshot}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # IntentionResponse: snapshot only, no session policy.
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "active_policy": snapshot,
                    "analytics_enabled": True,
                    "guardrails_enabled": True,
                },
            )
        # PATCH and GET return SessionResponse with both.
        return httpx.Response(
            200,
            json={
                "policy": stored,
                "active_policy": snapshot,
                "analytics_enabled": True,
                "guardrails_enabled": True,
            },
        )

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:

        async def invoke() -> None:
            if operation == "update":
                await provider.update_session_policy(
                    "session-evaluation", agent_id="agent-evaluation", policy=policy
                )
            else:
                await provider.create_session(
                    "session-evaluation", "Validate evaluation", "agent-evaluation", policy=policy
                )

        if stored_maximum == "escalate":
            await invoke()
        else:
            with pytest.raises(ValueError, match="maximum_outcome"):
                await invoke()
    finally:
        await provider.client.aclose()


def test_approval_mode_validation_and_default() -> None:
    assert AgentCapabilities().approval_mode == "ask"
    assert AgentCapabilities(approval_mode="unattended").model_dump()["approval_mode"] == (
        "unattended"
    )
    for value in (None, "deny", "", 1):
        with pytest.raises(ValidationError):
            AgentCapabilities(approval_mode=value)
    with pytest.raises(ValidationError):
        AgentCapabilities(approval_mode="unattended", guardrails_backend="none")
    assert "interaction_mode" not in _intaris_session_policy("/tmp/work", agent=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "ask", "unattended"])
@pytest.mark.parametrize("action", ["settings_update", "create", "update"])
async def test_agent_tool_rejects_approval_mode_without_root_context_or_at_opaque_paths(
    value: str | None, action: str
) -> None:
    deps = Mock()
    result = await handle_agent_management_tool(
        tool_name="manage_agents",
        arguments={"action": action, "settings": {"approval_mode": value}},
        deps=deps,
        user_email="owner@test.local",
        current_agent_id="agent-evaluation",
        self_mutation_approved=True,
    )
    assert result.is_error
    if action == "settings_update":
        assert result.metadata["code"] == "agent_management_context_denied"
    else:
        assert "human_policy_selection_required" in result.output
        assert "approval mode" in result.output
    assert deps.mock_calls == []
    assert profile_mutation_error({"capabilities": {"approval_mode": value}}).is_error
