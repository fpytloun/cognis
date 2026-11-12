"""Outcome policy synchronization preserves the authenticated agent identity."""

from types import SimpleNamespace

import httpx
import pytest

from cognis.providers.guardrails.intaris import IntarisProvider


@pytest.mark.asyncio
async def test_yolo_read_and_patch_use_shared_agent_identity() -> None:
    identities: list[tuple[str, str, str]] = []
    policies: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/session/session-1"
        identities.append(
            (
                request.headers["X-Agent-Id"],
                request.headers["X-Agent-Owner"],
                request.headers["Authorization"],
            )
        )
        if request.method == "PATCH":
            import json

            policies.append(json.loads(request.content)["policy"])
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "user_id": "participant@example.com",
                "agent_id": "shared-agent",
                "status": "active",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "policy": policies[-1] if policies else {"maximum_outcome": "escalate"},
            },
        )

    def sign_service_jwt(
        subject: str, agent_id: str, audience: list[str], *, agent_owner_email: str
    ) -> str:
        assert audience == ["intaris"]
        return f"{subject}:{agent_id}:{agent_owner_email}"

    provider = IntarisProvider(
        "http://intaris.test", SimpleNamespace(sign_service_jwt=sign_service_jwt)
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        await provider.get_session(
            "session-1",
            user_email="participant@example.com",
            agent_id="shared-agent",
            agent_owner_email="owner@example.com",
        )
        await provider.update_session_policy(
            "session-1",
            user_id="participant@example.com",
            agent_id="shared-agent",
            agent_owner_email="owner@example.com",
            policy={"maximum_outcome": "approve"},
        )
    finally:
        await provider.client.aclose()

    assert policies == [{"maximum_outcome": "approve"}]
    assert (
        identities
        == [
            (
                "shared-agent",
                "owner@example.com",
                "Bearer participant@example.com:shared-agent:owner@example.com",
            )
        ]
        * 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_maximum", ["approve", None])
async def test_create_verifies_stored_maximum_not_intention_ack(
    stored_maximum: str | None,
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "POST":
            assert request.url.path == "/api/v1/intention"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "analytics_enabled": True,
                    "guardrails_enabled": True,
                },
            )
        assert request.url.path == "/api/v1/session/session-1"
        return httpx.Response(
            200,
            json={
                "policy": {"maximum_outcome": stored_maximum} if stored_maximum else {},
                "analytics_enabled": True,
                "guardrails_enabled": True,
            },
        )

    provider = IntarisProvider(
        "http://intaris.test",
        SimpleNamespace(sign_service_jwt=lambda *args, **kwargs: "signed-token"),
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        if stored_maximum is None:
            with pytest.raises(ValueError, match="did not acknowledge"):
                await provider.create_session(
                    "session-1",
                    "task",
                    "agent-1",
                    "user@example.com",
                    policy={"maximum_outcome": "approve"},
                )
        else:
            await provider.create_session(
                "session-1",
                "task",
                "agent-1",
                "user@example.com",
                policy={"maximum_outcome": "approve"},
            )
    finally:
        await provider.client.aclose()

    assert requests[0:2] == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum", ["escalate", "deny"])
async def test_hybrid_create_reads_persisted_minimum_missing_from_compact_ack(minimum):
    requests = []
    active = {"evaluation_policy_version": "hybrid_v1", "profile": "balanced"}
    policy = {**active, "minimum_outcome": minimum}

    def respond(request):
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "active_policy": active,
                    "analytics_enabled": True,
                    "guardrails_enabled": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "policy": policy,
                "active_policy": active,
                "analytics_enabled": True,
                "guardrails_enabled": True,
            },
        )

    provider = IntarisProvider(
        "http://intaris.test",
        SimpleNamespace(sign_service_jwt=lambda *args, **kwargs: "signed-token"),
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    try:
        await provider.create_session(
            "session-minimum", "Review", "agent-1", "user@example.com", policy=policy
        )
    finally:
        await provider.client.aclose()
    assert requests == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_mode", ["strict", "legacy"])
async def test_nested_creation_and_refresh_accept_canonical_changed_parent_mode(canonical_mode):
    calls = []
    version = "legacy" if canonical_mode == "legacy" else "hybrid_v1"
    canonical = {"evaluation_policy_version": version}
    if version == "hybrid_v1":
        canonical["profile"] = canonical_mode
    stored = {
        "policy": canonical,
        "parent_session_id": "nested-parent",
        "analytics_enabled": True,
        "guardrails_enabled": True,
    }
    if version == "hybrid_v1":
        stored["active_policy"] = canonical

    def respond(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "analytics_enabled": True,
                    "guardrails_enabled": True,
                },
            )
        return httpx.Response(200, json=stored)

    provider = IntarisProvider(
        "http://intaris.test",
        SimpleNamespace(sign_service_jwt=lambda *args, **kwargs: "signed-token"),
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://intaris.test", transport=httpx.MockTransport(respond)
    )
    stale = {"evaluation_policy_version": "hybrid_v1", "profile": "balanced"}
    try:
        await provider.create_session(
            "grandchild",
            "Research",
            "agent-1",
            "user@example.com",
            parent_session_id="nested-parent",
            policy=stale,
        )
        refreshed = await provider.update_session_policy(
            "grandchild", policy=stale, agent_id="agent-1", user_id="user@example.com"
        )
        assert refreshed == canonical
    finally:
        await provider.client.aclose()
    assert calls == ["POST", "GET", "PATCH"]
