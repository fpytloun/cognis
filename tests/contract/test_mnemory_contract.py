from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


def test_whoami_accepts_cognis_jwt(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    contract_user_email: str,
) -> None:
    token = make_service_jwt("mnemory", agent_id=contract_agent_id)
    response = http_client.get(
        f"{mnemory_url}/api/whoami",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": contract_user_email,
        "agent_id": contract_agent_id,
        "timezone": None,
        "can_switch_user": False,
    }


def test_whoami_rejects_wrong_jwt_audience(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
) -> None:
    token = make_service_jwt("intaris")
    response = http_client.get(
        f"{mnemory_url}/api/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_jwt_subject_is_not_overridden_by_openwebui_header(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    contract_user_email: str,
) -> None:
    token = make_service_jwt("mnemory", agent_id=contract_agent_id)
    response = http_client.get(
        f"{mnemory_url}/api/whoami",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
            "X-OpenWebUI-User-Email": "override@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == contract_user_email


def test_recall_returns_expected_shape_and_session_id(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
    mnemory_cleanup: list[str],
) -> None:
    token = make_service_jwt("mnemory", agent_id=contract_agent_id)
    response = http_client.post(
        f"{mnemory_url}/api/recall",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
        json={
            "query": "Cognis stage-0 contract test",
            "messages": [{"role": "user", "content": "Cognis stage-0 contract test"}],
            "labels": {"contract_run": unique_label},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["session_id"], str) and data["session_id"]
    assert data["instructions"] is None or isinstance(data["instructions"], str)
    assert data["core_memories"] is None or isinstance(data["core_memories"], str)
    assert isinstance(data["search_results"], list)
    assert set(data["stats"].keys()) == {
        "core_count",
        "search_count",
        "new_count",
        "known_skipped",
        "latency_ms",
    }
    mnemory_cleanup.append(data["session_id"])


def test_remember_returns_accepted_true(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
    mnemory_cleanup: list[str],
) -> None:
    token = make_service_jwt("mnemory", agent_id=contract_agent_id)
    recall = http_client.post(
        f"{mnemory_url}/api/recall",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
        json={
            "query": "remember contract",
            "messages": [{"role": "user", "content": "remember contract"}],
            "labels": {"contract_run": unique_label},
        },
    )
    recall.raise_for_status()
    session_id = recall.json()["session_id"]
    mnemory_cleanup.append(session_id)

    response = http_client.post(
        f"{mnemory_url}/api/remember",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
        json={
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "Remember this contract test turn"},
                {"role": "assistant", "content": "Acknowledged"},
            ],
            "labels": {"contract_run": unique_label},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}


def test_remember_assistant_role_requires_agent_identity(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
) -> None:
    token = make_service_jwt("mnemory")
    response = http_client.post(
        f"{mnemory_url}/api/remember",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [{"role": "assistant", "content": "Assistant-only fact"}],
            "role": "assistant",
        },
    )

    assert response.status_code == 422


def test_api_key_auth_still_works_when_configured(
    http_client: httpx.Client,
    mnemory_url: str,
    maybe_mnemory_api_key: str | None,
) -> None:
    if not maybe_mnemory_api_key:
        pytest.skip("COGNIS_TEST_MNEMORY_API_KEY is not configured")

    response = http_client.get(
        f"{mnemory_url}/api/whoami",
        headers={
            "Authorization": f"Bearer {maybe_mnemory_api_key}",
            "X-User-Id": "api-key-user@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "api-key-user@example.com"


def test_memories_list_returns_created_memory(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
) -> None:
    token = make_service_jwt("mnemory", agent_id=contract_agent_id)
    create = http_client.post(
        f"{mnemory_url}/api/memories",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
        json={
            "content": f"contract memory {unique_label}",
            "role": "assistant",
            "pinned": True,
            "labels": {"contract_run": unique_label},
        },
    )
    assert create.status_code == 200
    memory_id = create.json()["memory_id"]

    listed = http_client.get(
        f"{mnemory_url}/api/memories",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
        params={"role": "assistant", "limit": 100},
    )
    assert listed.status_code == 200
    data = listed.json()
    items = data if isinstance(data, list) else data.get("items", [])
    assert isinstance(items, list)
    assert any(item.get("memory_id") == memory_id for item in items if isinstance(item, dict))

    delete = http_client.delete(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
    )
    assert delete.is_success
