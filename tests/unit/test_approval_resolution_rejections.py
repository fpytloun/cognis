"""Rejected approvals terminate invalid waits without granting tool execution."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.core.agent_loop import PendingPause
from cognis.core.events import EventType
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.providers.guardrails.resolution_errors import ResolutionRejected
from tests.unit.test_notifications import (
    NotificationService,
    _FakeEventBus,
    _FakeSessionFactory,
    _notification_row,
)
from tests.unit.test_notifications_routes import _auth_headers, _create_test_client


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
@pytest.mark.parametrize("structured", [True, False])
async def test_provider_resolution_rejection_no_retry_or_raw_content(status, structured) -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "detail": {"code": "read_only_ceiling", "message": "SECRET"}
                if structured
                else "SECRET"
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
        with pytest.raises(ResolutionRejected) as caught:
            await provider.submit_decision("call-1", "approve")
        assert len(calls) == 1
        assert caught.value.status == status
        assert "SECRET" not in str(caught.value) + str(caught.value.api_detail())
        assert caught.value.permanent is (structured and status != 404)
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["read_only_ceiling", "mandatory_denial", "stale_approval", "stale_resolution", "unknown"],
)
@pytest.mark.parametrize("structured", [True, False])
async def test_service_commits_only_permanent_denial_and_wakes_waiter(code, structured) -> None:
    from cognis.core.agent_loop import PauseWaiter

    row = _notification_row()
    waiter = PauseWaiter()
    waiter.register(PendingPause(pause_id=row.notification_id, pause_type="escalation"))
    bus = _FakeEventBus()
    guardrails = SimpleNamespace(
        submit_decision=AsyncMock(
            side_effect=ResolutionRejected(409, {"detail": {"code": code} if structured else code})
        )
    )
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=waiter,
        event_bus=bus,
        providers=SimpleNamespace(guardrails=guardrails),
    )
    with pytest.raises(ResolutionRejected) as caught:
        await service.resolve(row.notification_id, "approve", user_email=row.user_email)
    terminal = code != "unknown"
    decision = "cancel" if code.startswith("stale_") else "deny"
    assert row.status == ("resolved" if terminal else "pending")
    assert caught.value.resolved_locally is terminal
    guardrails.submit_decision.assert_awaited_once()
    if terminal:
        assert row.resolution["decision"] == decision
        assert row.resolution["reason"] == code
        resolution = await waiter.wait(row.notification_id, timeout=0.1)
        assert resolution.decision == decision
        assert resolution.data["note"] == caught.value.message
        assert any(
            e.type == EventType.NOTIFICATION_RESOLVED and e.data["decision"] == decision
            for e in bus.events
        )
    else:
        assert row.resolution is None
    await asyncio.sleep(0)


@pytest.mark.parametrize("route", ["notifications", "escalations"])
@pytest.mark.parametrize(
    "code", ["read_only_ceiling", "mandatory_denial", "stale_approval", "stale_resolution"]
)
@pytest.mark.parametrize("structured", [True, False])
def test_resolution_routes_expose_safe_reason_and_terminal_denial(
    monkeypatch, tmp_path, route, code, structured
):
    with _create_test_client(monkeypatch, tmp_path) as client:
        from cognis.store.queries import create_user

        svc = client.app.state.notification_service

        async def seed():
            async with client.app.state.session_factory() as db:
                await create_user(db, email="user@example.com", name="User", password_hash="unused")
                await db.commit()
            await svc.create(
                notification_type="escalation",
                conversation_id="conv-approval",
                user_email="user@example.com",
                notification_id="approval-rejected",
                payload={"tool_name": "bash"},
            )

        client.portal.call(seed)
        client.app.state.providers.guardrails.submit_decision = AsyncMock(
            side_effect=ResolutionRejected(
                409, {"detail": {"code": code, "message": "SECRET"} if structured else code}
            )
        )
        response = client.post(
            f"/api/v1/{route}/approval-rejected/resolve",
            headers=_auth_headers(client.app, email="user@example.com"),
            json={"decision": "approve"},
        )
        assert response.status_code == 409
        detail = response.json()["error"]
        assert detail["code"] == code
        assert "SECRET" not in response.text
        assert detail["details"]["resolved"] is True
        current = client.portal.call(svc.get, "approval-rejected")
        assert current.status == "resolved"
        assert current.resolution["decision"] == ("cancel" if code.startswith("stale_") else "deny")


@pytest.mark.parametrize(
    ("detail", "code", "permanent"),
    [
        ("read_only_ceiling", "read_only_ceiling", True),
        ("mandatory_denial", "mandatory_denial", True),
        ("stale_approval", "stale_approval", False),
        ("Unattended Judge review cannot be resolved by a human", "mandatory_denial", True),
        ("Call call-1 already resolved by user (user_decision=approve)", "already_resolved", False),
        ("Call call-1 already resolved (user_decision=deny)", "already_resolved", False),
        ("SECRET read_only_ceiling already resolved", "resolution_rejected", False),
        ("Cannot approve SECRET action", "resolution_rejected", False),
    ],
)
@pytest.mark.parametrize("status", [400, 403, 409, 422])
def test_legacy_details_are_narrowly_classified_without_echoing_text(
    detail, code, permanent, status
) -> None:
    error = ResolutionRejected(status, {"detail": detail})
    assert error.code == code
    assert error.permanent is permanent
    assert "SECRET" not in str(error) + str(error.api_detail())


@pytest.mark.asyncio
async def test_repeated_legacy_policy_rejections_do_not_retry_or_trip_breaker() -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(409, json={"detail": "read_only_ceiling"})

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=lambda *a, **kw: "signed-token")
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        for _ in range(8):
            with pytest.raises(ResolutionRejected) as caught:
                await provider.submit_decision("call-1", "approve")
            assert caught.value.code == "read_only_ceiling"
            assert caught.value.permanent
        assert len(calls) == 8
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
async def test_restart_recovered_stale_approval_returns_immediately_without_denial():
    from cognis.core.agent_loop import PauseWaiter
    from cognis.core.notifications import _user_interaction_display
    from cognis.core.turn_scheduler import _recovered_escalation_tool_result_event
    from cognis.providers.guardrails.resolution_errors import stale_approval_result

    row = _notification_row()
    guardrails = SimpleNamespace(
        submit_decision=AsyncMock(
            side_effect=ResolutionRejected(409, {"detail": {"code": "stale_resolution"}})
        )
    )
    service = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=PauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=guardrails),
    )
    with pytest.raises(ResolutionRejected) as caught:
        await service.resolve(row.notification_id, "approve", user_email=row.user_email)
    assert caught.value.api_detail()["details"] == {"resolved": True, "decision": "cancel"}
    # Simulate another process with no local waiter state.
    recovered = NotificationService(
        session_factory=_FakeSessionFactory(row),
        pause_waiter=PauseWaiter(),
        event_bus=_FakeEventBus(),
        providers=SimpleNamespace(guardrails=guardrails),
    )
    resolution = await asyncio.wait_for(
        recovered.wait_for_resolution(row.notification_id, timeout=300), timeout=1
    )
    event = _recovered_escalation_tool_result_event(
        descriptor={"call_id": "tool-call-1", "tool_name": "bash"},
        resolution=resolution,
        turn_id="turn-1",
    )
    output = json.loads(event.data["result"])
    assert output == stale_approval_result(resolution.decision, resolution.data)
    assert output["code"] == "stale_approval" and output["retryable"] is True
    assert event.data["status"] == "failed"
    assert output["code"] != "tool_denied_by_user"
    display = _user_interaction_display(
        notification_type="escalation",
        notification_payload={},
        resolution_data=resolution.data,
        decision=resolution.decision,
    )
    assert display["title"] == "Approval expired"
    assert display["status"] == "cancelled"
    guardrails.submit_decision.assert_awaited_once()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_live_stale_approval_does_not_execute_tool_or_submit_a_human_denial():
    from cognis.core.agent_loop import AgentLoop, PauseResolution, ToolCall, ToolResult
    from cognis.models.agent import AgentCapabilities

    resolution = PauseResolution(
        decision="cancel",
        data={"reason": "stale_resolution", "requires_reevaluation": True},
    )
    loop = SimpleNamespace(
        session_manager=SimpleNamespace(session_factory=lambda: AsyncMock()),
        notification_service=SimpleNamespace(create=AsyncMock()),
        _wait_for_escalation_resolution=AsyncMock(return_value=resolution),
        _execute_tool_call=AsyncMock(),
        providers=SimpleNamespace(guardrails=SimpleNamespace(submit_decision=AsyncMock())),
        event_bus=_FakeEventBus(),
    )
    context = SimpleNamespace(
        task_id=None,
        interaction_mode="explicit_gates",
        agent=SimpleNamespace(capabilities=AgentCapabilities(escalation_timeout_seconds=300)),
        session=SimpleNamespace(session_id="sess-1", user_email="user@example.com"),
        conversation=SimpleNamespace(conversation_id="conv-1"),
    )
    original = ToolResult(
        output="Approval required",
        is_error=True,
        metadata={"evaluation": {"decision": "escalate", "call_id": "call-1"}},
    )
    result = await AgentLoop._handle_escalation(
        loop,
        original,
        ToolCall(call_id="tool-call-1", name="bash", arguments={}),
        context,
        [],
        None,
    )
    assert json.loads(result.output)["code"] == "stale_approval"
    assert result.metadata["evaluation"]["resolution"] == "stale"
    assert result.metadata["evaluation"]["pending"] is False
    loop._execute_tool_call.assert_not_awaited()
    loop.providers.guardrails.submit_decision.assert_not_awaited()
