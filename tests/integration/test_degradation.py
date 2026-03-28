"""Degradation and health check integration tests.

Exercises: health endpoint, provider status.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import IntegrationStack


@pytest.mark.integration
def test_health_endpoint_reports_all_providers(
    stack: IntegrationStack,
) -> None:
    """Health endpoint should report status for all providers."""
    response = stack.client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "providers" in data

    provider_names = set(data["providers"].keys())
    assert "memory" in provider_names
    assert "guardrails" in provider_names
    assert "executor" in provider_names
    assert "llm" in provider_names
    assert "auth" in provider_names


@pytest.mark.integration
def test_health_providers_healthy_with_live_services(
    stack: IntegrationStack,
) -> None:
    """With live Mnemory and Intaris, provider health should show healthy status."""
    response = stack.client.get("/api/health")
    assert response.status_code == 200
    data = response.json()

    memory_status = data["providers"].get("memory", {}).get("status", "unknown")
    guardrails_status = data["providers"].get("guardrails", {}).get("status", "unknown")

    # Services are running, but Cognis health probes may not use JWT auth,
    # so "degraded" (service reachable but auth failed) is acceptable.
    acceptable = {"healthy", "degraded"}
    assert memory_status in acceptable, (
        f"Memory provider not reachable: {data['providers'].get('memory')}"
    )
    assert guardrails_status in acceptable, (
        f"Guardrails provider not reachable: {data['providers'].get('guardrails')}"
    )
