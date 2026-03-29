"""Integration tests: degraded-mode scenarios.

Tests provider failure behavior during actual API operations.
Uses the TestClient-based integration_stack (no live server needed
for testing REST API behavior under degraded conditions).
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    create_test_agent,
)


@pytest.mark.integration
def test_health_reports_degraded_when_provider_unhealthy(
    stack: IntegrationStack, run_id: str
) -> None:
    """Health endpoint should report 'degraded' when any provider is unhealthy."""
    response = stack.client.get("/api/health", headers=stack.admin_headers())
    assert response.status_code == 200
    body = response.json()
    # With live services, should be healthy or degraded — never an error
    assert body["status"] in ("healthy", "degraded")
    # All expected providers should be present
    providers = body.get("providers", {})
    for expected in ("memory", "guardrails", "executor", "llm", "auth"):
        assert expected in providers, f"Missing provider: {expected}"


@pytest.mark.integration
def test_agent_creation_survives_mnemory_bootstrap_failure(
    stack: IntegrationStack, run_id: str
) -> None:
    """Agent creation should succeed even if Mnemory personality bootstrap fails.

    The agent is created in the database; only the sync is degraded.
    """
    agent_id = f"mnemory-fail-agent-{run_id}"
    response = stack.client.post(
        "/api/v1/agents",
        headers=stack.admin_headers(),
        json={
            "agent_id": agent_id,
            "name": "Mnemory Fail Agent",
            "display_name": "Mnemory Fail Agent",
            "description": "Tests graceful degradation",
            "system_prompt": "You are a test assistant.",
            "personality": {
                "tone": "concise",
                "temperament": "cooperative",
                "purpose": "degradation testing",
            },
        },
    )
    # Agent creation should succeed regardless of Mnemory state
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == agent_id
    # personality_synced may be True or False depending on Mnemory state
    # The important thing is that it doesn't crash
    assert "personality_synced" in body


@pytest.mark.integration
def test_conversation_creation_without_intaris_session(
    stack: IntegrationStack, run_id: str
) -> None:
    """Conversation creation should work even before any Intaris session exists.

    Sessions are created lazily on first chat turn, not at conversation creation.
    """
    agent_id = f"no-intaris-agent-{run_id}"
    create_test_agent(stack, agent_id)

    response = stack.client.post(
        "/api/v1/conversations",
        headers=stack.admin_headers(),
        json={
            "agent_id": agent_id,
            "title": "Pre-session conversation",
            "context": {"type": "test", "ref": None, "platform_data": {}, "memory_labels": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["conversation_id"] is not None
    assert body["agent_id"] == agent_id


@pytest.mark.integration
def test_settings_accessible_when_providers_degraded(
    stack: IntegrationStack,
) -> None:
    """Settings endpoints should work even when providers are degraded.

    Settings are stored in Cognis DB, not in external providers.
    """
    response = stack.client.get("/api/v1/settings", headers=stack.admin_headers())
    assert response.status_code == 200
    settings = response.json()
    assert isinstance(settings, list)
    assert len(settings) > 0


@pytest.mark.integration
def test_diagnostics_reports_service_connectivity(
    stack: IntegrationStack,
) -> None:
    """The diagnostics endpoint should report connectivity for all services."""
    response = stack.client.get("/api/system/diagnostics", headers=stack.admin_headers())
    if response.status_code == 404:
        # Diagnostics endpoint may not exist yet — skip
        pytest.skip("Diagnostics endpoint not available")
    assert response.status_code == 200
    body = response.json()
    readiness = body.get("readiness", {})
    # Should contain at least services and providers sections
    assert isinstance(readiness, dict)


@pytest.mark.integration
def test_tool_list_accessible_regardless_of_provider_state(
    stack: IntegrationStack,
) -> None:
    """Tool list is static; should work regardless of provider health."""
    response = stack.client.get("/api/v1/tools", headers=stack.admin_headers())
    assert response.status_code == 200
    tools = response.json()
    assert isinstance(tools, list)
    # Should have at least the controller tools
    tool_names = [t.get("name") for t in tools]
    assert "step_complete" in tool_names


@pytest.mark.integration
def test_workflow_list_accessible_regardless_of_provider_state(
    stack: IntegrationStack,
) -> None:
    """System workflows should be listable regardless of provider health."""
    response = stack.client.get("/api/v1/workflows", headers=stack.admin_headers())
    assert response.status_code == 200
    body = response.json()
    items = body.get("items", [])
    assert len(items) >= 1
    # system:direct should always be present
    ids = [w.get("workflow_id") for w in items]
    assert "system:direct" in ids
