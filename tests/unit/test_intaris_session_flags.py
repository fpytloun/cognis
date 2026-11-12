"""Session flags are explicit, durable, and independent of evaluation policy."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from cognis.models.session import IntarisSession
from cognis.providers.backends.guardrails.null import NoGuardrailsProvider
from cognis.providers.guardrails.intaris import IntarisProvider


def provider_for(respond):
    provider = IntarisProvider(
        "http://intaris.test",
        SimpleNamespace(
            sign_service_jwt=lambda *args, **kwargs: "signed-token",
            sign_workflow_step_jwt=lambda *args, **kwargs: "signed-workflow-token",
        ),
    )
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics", [False, True])
@pytest.mark.parametrize("guardrails", [False, True])
async def test_create_and_patch_flags_roundtrip(analytics: bool, guardrails: bool) -> None:
    stored = {}
    requests = []

    def respond(request):
        requests.append(request)
        if request.method != "GET":
            stored.update(json.loads(request.content))
        return httpx.Response(200, json=stored)

    provider = provider_for(respond)
    try:
        selected = provider if guardrails else NoGuardrailsProvider(provider)
        await selected.create_session(
            "session-flags", "bounded task", "agent-flags", analytics_enabled=analytics
        )
        assert stored["analytics_enabled"] is analytics
        assert stored["guardrails_enabled"] is guardrails
        await provider.update_session_policy(
            "session-flags",
            agent_id="agent-flags",
            analytics_enabled=not analytics,
            guardrails_enabled=not guardrails,
        )
        assert stored["analytics_enabled"] is not analytics
        assert stored["guardrails_enabled"] is not guardrails
        assert requests[-1].method == "GET"
        await provider.update_session_policy(
            "session-flags", agent_id="agent-flags", analytics_enabled=None
        )
        patch = json.loads(requests[-1].content)
        assert "analytics_enabled" not in patch
        assert "guardrails_enabled" not in patch
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "conflict", "patch"])
@pytest.mark.parametrize(
    "stored_flags", [{}, {"analytics_enabled": True, "guardrails_enabled": True}]
)
async def test_disabled_flags_require_explicit_acknowledgement(operation, stored_flags) -> None:
    def respond(request):
        if operation == "conflict" and request.method == "POST":
            return httpx.Response(409, json={})
        return httpx.Response(200, json=stored_flags)

    provider = provider_for(respond)
    try:
        with pytest.raises(ValueError, match="did not acknowledge analytics_enabled"):
            if operation == "patch":
                await provider.update_session_policy(
                    "session-flags", agent_id="agent-flags", analytics_enabled=False
                )
            else:
                await provider.create_session(
                    "session-flags", "bounded task", "agent-flags", analytics_enabled=False
                )
    finally:
        await provider.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, "false", None])
async def test_create_rejects_non_boolean_flags(value) -> None:
    provider = provider_for(lambda request: pytest.fail("Must reject before network I/O"))
    try:
        with pytest.raises(ValueError, match="boolean"):
            await provider.create_session(
                "session-flags", "bounded task", "agent-flags", analytics_enabled=value
            )
    finally:
        await provider.client.aclose()


def test_older_read_defaults_preserve_existing_behavior() -> None:
    session = IntarisSession.model_validate(
        {
            "session_id": "session-flags",
            "user_id": "owner@cognis.test",
            "agent_id": "agent-flags",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    assert session.analytics_enabled is True
    assert session.guardrails_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics", [False, True])
@pytest.mark.parametrize("guardrails", [False, True])
async def test_workflow_install_preserves_acknowledged_session_flags(analytics, guardrails) -> None:
    from tests.unit.test_workflow_step_scope import declaration

    item = declaration()
    stored = {}
    operations = []

    def respond(request):
        operations.append(request)
        body = json.loads(request.content) if request.content else {}
        if "workflow_step" in body:
            assert stored["analytics_enabled"] is analytics
            assert stored["guardrails_enabled"] is guardrails
            assert set(body) == {"session_id", "workflow_step"}
            assert body["workflow_step"] == item.model_dump(mode="json")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "declaration_digest": item.declaration_digest,
                    "admission_revision": 1,
                },
            )
        if request.method != "GET":
            stored.update(body)
        return httpx.Response(200, json=stored)

    provider = provider_for(respond)
    try:
        await provider.create_session(
            item.session_id,
            "Bounded workflow",
            "agent_scope",
            analytics_enabled=analytics,
            guardrails_enabled=guardrails,
        )
        await provider.update_session_policy(
            item.session_id,
            agent_id="agent_scope",
            analytics_enabled=analytics,
            guardrails_enabled=guardrails,
        )
        await provider.declare_workflow_step(
            item,
            user_email="owner@test.local",
            agent_id="agent_scope",
        )
        assert [request.method for request in operations] == ["POST", "PATCH", "GET", "POST"]
        assert stored["analytics_enabled"] is analytics
        assert stored["guardrails_enabled"] is guardrails
    finally:
        await provider.client.aclose()
