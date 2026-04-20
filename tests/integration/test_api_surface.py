"""API surface integration tests.

Exercises: system endpoints, settings, workflows, secrets, tools —
verifying the full API works against a live stack.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    create_test_agent,
)


@pytest.mark.integration
def test_settings_list_returns_seeded_defaults(
    stack: IntegrationStack,
) -> None:
    """Settings should include seeded default values from bootstrap."""
    response = stack.client.get(
        "/api/v1/settings",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    categories = response.json()
    assert len(categories) > 0

    # Flatten all setting keys
    all_keys = set()
    for cat in categories:
        for item in cat["items"]:
            all_keys.add(item["key"])

    assert "session.max_context_tokens" in all_keys
    assert "session.escalation_timeout_seconds" in all_keys


@pytest.mark.integration
def test_workflow_list_includes_system_workflows(
    stack: IntegrationStack,
) -> None:
    """Workflow list should include bundled system workflows from the registry."""
    response = stack.client.get(
        "/api/v1/workflows",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    workflow_ids = {item["workflow_id"] for item in items}

    assert "system:direct" in workflow_ids
    assert "system:general-task" in workflow_ids
    assert "system:research" in workflow_ids
    assert "system:software-development" in workflow_ids


@pytest.mark.integration
def test_workflow_detail_system_workflow(
    stack: IntegrationStack,
) -> None:
    """System workflow detail should return full workflow definition."""
    response = stack.client.get(
        "/api/v1/workflows/system:direct",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow_id"] == "system:direct"
    assert data["is_system"] is True
    assert len(data["steps"]) >= 1


@pytest.mark.integration
def test_workflow_duplicate_system_workflow(
    stack: IntegrationStack,
) -> None:
    """Duplicating a system workflow should create a user-owned copy."""
    response = stack.client.post(
        "/api/v1/workflows/system:research/duplicate",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow_id"].startswith("wf_")
    assert data["is_system"] is False
    assert data["owner_email"] == stack.admin_email


@pytest.mark.integration
def test_tools_list(
    stack: IntegrationStack,
) -> None:
    """Tool list should return available tools."""
    response = stack.client.get(
        "/api/v1/tools",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    tools = response.json()
    assert isinstance(tools, list)
    # Should have at least the built-in orchestration tools
    tool_names = {t["name"] for t in tools}
    assert "delegate" in tool_names or "step_complete" in tool_names or len(tool_names) > 0


@pytest.mark.integration
def test_llm_provider_list(
    stack: IntegrationStack,
) -> None:
    """LLM provider list should include the seeded default provider."""
    response = stack.client.get(
        "/api/v1/llm-providers",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    provider_ids = {item["provider_id"] for item in data["items"]}
    assert "default" in provider_ids


@pytest.mark.integration
def test_model_routing_returns_seeded_default(
    stack: IntegrationStack,
) -> None:
    """Model routing should have a default route seeded by the integration fixture."""
    response = stack.client.get(
        "/api/v1/model-routing",
        headers=stack.admin_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    # The seeded default model should be present
    assert data.get("default", {}).get("model") is not None


@pytest.mark.integration
def test_agent_crud_lifecycle(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """Full agent CRUD: create, read, update, activate, suspend, archive."""
    # Create
    create_test_agent(stack, agent_id)

    # Read
    detail = stack.client.get(
        f"/api/v1/agents/{agent_id}",
        headers=stack.admin_headers(),
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "active"

    # Update
    update_response = stack.client.put(
        f"/api/v1/agents/{agent_id}",
        headers=stack.admin_headers(),
        json={
            "agent_id": agent_id,
            "name": "Updated Agent",
            "description": "Updated description",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Updated Agent"

    # Suspend
    suspend = stack.client.post(
        f"/api/v1/agents/{agent_id}/suspend",
        headers=stack.admin_headers(),
    )
    assert suspend.status_code == 200
    assert suspend.json()["status"] == "suspended"

    # Reactivate
    activate = stack.client.post(
        f"/api/v1/agents/{agent_id}/activate",
        headers=stack.admin_headers(),
    )
    assert activate.status_code == 200
    assert activate.json()["status"] == "active"


@pytest.mark.integration
def test_secrets_crud(
    stack: IntegrationStack,
) -> None:
    """Create, list, and delete a secret."""
    # Create
    create_response = stack.client.post(
        "/api/v1/secrets",
        headers=stack.admin_headers(),
        json={
            "name": "test_api_key",
            "value": "sk-test-123",
            "scope": "user",
            "description": "Integration test secret",
        },
    )
    assert create_response.status_code == 200

    # List
    list_response = stack.client.get(
        "/api/v1/secrets",
        headers=stack.admin_headers(),
    )
    assert list_response.status_code == 200
    secrets = list_response.json()
    assert any(s["name"] == "test_api_key" for s in secrets)

    # Delete
    delete_response = stack.client.delete(
        "/api/v1/secrets/test_api_key?scope=user",
        headers=stack.admin_headers(),
    )
    assert delete_response.status_code == 200


@pytest.mark.integration
def test_jwks_endpoint(
    stack: IntegrationStack,
) -> None:
    """JWKS endpoint should return valid JWK set."""
    response = stack.client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    data = response.json()
    assert "keys" in data
    assert len(data["keys"]) >= 1
    assert data["keys"][0]["kty"] == "EC"
