from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


def test_whoami_accepts_cognis_jwt(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    contract_user_email: str,
) -> None:
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    response = http_client.get(
        f"{intaris_url}/api/v1/whoami",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": contract_agent_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": contract_user_email,
        "agent_id": contract_agent_id,
        "can_switch_user": False,
    }


def test_whoami_rejects_wrong_jwt_audience(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
) -> None:
    token = make_service_jwt("mnemory")
    response = http_client.get(
        f"{intaris_url}/api/v1/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_create_session_and_duplicate_conflict(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-contract")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
    }
    payload = {"session_id": session_id, "intention": "Contract test session"}

    first = http_client.post(f"{intaris_url}/api/v1/intention", headers=headers, json=payload)
    duplicate = http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json=payload,
    )

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert duplicate.status_code == 409


def test_evaluate_response_shape(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-eval")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
    }
    http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json={"session_id": session_id, "intention": "Evaluate contract"},
    ).raise_for_status()

    response = http_client.post(
        f"{intaris_url}/api/v1/evaluate",
        headers=headers,
        json={"session_id": session_id, "tool": "read", "args": {}, "context": {}},
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["call_id"], str)
    assert data["decision"] in {"approve", "deny", "escalate"}
    assert "reasoning" in data
    assert "risk" in data
    assert isinstance(data["path"], str)
    assert isinstance(data["latency_ms"], int)
    assert isinstance(data["injection_detected"], bool)
    assert "session_status" in data
    assert "status_reason" in data


def test_reasoning_response_shape(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-reasoning")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
    }
    http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json={"session_id": session_id, "intention": "Reasoning contract"},
    ).raise_for_status()

    response = http_client.post(
        f"{intaris_url}/api/v1/reasoning",
        headers=headers,
        json={"session_id": session_id, "content": "User message: Hello there"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert isinstance(data["call_id"], str)


def test_events_last_n_and_last_seq_shape(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-events")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
        "X-Intaris-Source": "cognis",
    }
    http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json={"session_id": session_id, "intention": "Events contract"},
    ).raise_for_status()

    for payload in (
        {"type": "user_message", "data": {"content": "hello", "role": "user"}},
        {"type": "assistant_message", "data": {"content": "world", "role": "assistant"}},
        {"type": "delegation", "data": {"mode": "worker", "task": "research"}},
    ):
        response = http_client.post(
            f"{intaris_url}/api/v1/session/{session_id}/events",
            headers=headers,
            json=[payload],
        )
        response.raise_for_status()

    response = http_client.get(
        f"{intaris_url}/api/v1/session/{session_id}/events?last_n=2",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["last_seq"] >= 3
    assert data["has_more"] in {True, False}
    assert [event["type"] for event in data["events"]] == [
        "assistant_message",
        "delegation",
    ]


def test_filtered_empty_read_still_reports_real_last_seq(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-empty-filter")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
        "X-Intaris-Source": "cognis",
    }
    http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json={"session_id": session_id, "intention": "Filtered read contract"},
    ).raise_for_status()
    http_client.post(
        f"{intaris_url}/api/v1/session/{session_id}/events",
        headers=headers,
        json=[{"type": "user_message", "data": {"content": "hello", "role": "user"}}],
    ).raise_for_status()

    response = http_client.get(
        f"{intaris_url}/api/v1/session/{session_id}/events?type=evaluation",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["events"] == []
    assert data["last_seq"] >= 1


def test_event_idempotency_key_replay_returns_success_without_duplicate_append(
    http_client: httpx.Client,
    intaris_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_session_id: Callable[[str], str],
) -> None:
    session_id = unique_session_id("intaris-idempotency")
    token = make_service_jwt("intaris", agent_id=contract_agent_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": contract_agent_id,
        "X-Intaris-Source": "cognis",
    }
    http_client.post(
        f"{intaris_url}/api/v1/intention",
        headers=headers,
        json={"session_id": session_id, "intention": "Idempotency contract"},
    ).raise_for_status()

    url = f"{intaris_url}/api/v1/session/{session_id}/events?idempotency_key={session_id}:1:0"
    payload = {"type": "user_message", "data": {"content": "hello", "role": "user"}}

    first = http_client.post(url, headers=headers, json=[payload])
    replay = http_client.post(url, headers=headers, json=[payload])

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()

    events = http_client.get(
        f"{intaris_url}/api/v1/session/{session_id}/events",
        headers=headers,
    )
    events.raise_for_status()
    persisted_events = events.json()["events"]
    user_messages = [event for event in persisted_events if event["type"] == "user_message"]
    assert len(user_messages) == 1


def test_api_key_auth_still_works_when_configured(
    http_client: httpx.Client,
    intaris_url: str,
    maybe_intaris_api_key: str | None,
) -> None:
    if not maybe_intaris_api_key:
        pytest.skip("COGNIS_TEST_INTARIS_API_KEY is not configured")

    response = http_client.get(
        f"{intaris_url}/api/v1/whoami",
        headers={
            "Authorization": f"Bearer {maybe_intaris_api_key}",
            "X-User-Id": "api-key-user@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "api-key-user@example.com"
