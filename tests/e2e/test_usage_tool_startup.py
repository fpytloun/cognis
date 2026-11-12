"""Ordinary full-app startup and authenticated usage discovery, without inference."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import E2EStack

pytestmark = pytest.mark.e2e


def test_usage_tool_registration_allows_full_app_startup(e2e_stack: E2EStack) -> None:
    response = e2e_stack.http.get(f"{e2e_stack.cognis_url}/api/health")
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {e2e_stack.admin_token}"}
    tools = e2e_stack.http.get(f"{e2e_stack.cognis_url}/api/v1/tools", headers=headers)
    assert tools.status_code == 200, tools.text
    usage = next(tool for tool in tools.json() if tool["name"] == "query_usage")
    assert usage["read_only"] is True
    catalog = e2e_stack.http.post(
        f"{e2e_stack.cognis_url}/api/v1/usage/catalog", headers=headers, json={}
    )
    assert catalog.status_code == 200, catalog.text
