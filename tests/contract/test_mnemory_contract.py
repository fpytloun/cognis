from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress

import httpx
import pytest


@pytest.fixture
def revision_memory_cleanup(
    http_client: httpx.Client,
    mnemory_url: str,
) -> Iterator[list[tuple[dict[str, str], str]]]:
    memories: list[tuple[dict[str, str], str]] = []
    yield memories
    for headers, memory_id in memories:
        with suppress(Exception):
            _delete_current_memory(http_client, mnemory_url, headers, memory_id)


def _auth_headers(
    make_service_jwt: Callable[..., str],
    agent_id: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_service_jwt('mnemory', agent_id=agent_id)}",
        "X-Agent-Id": agent_id,
    }


def _create_revisioned_memory(
    http_client: httpx.Client,
    mnemory_url: str,
    headers: dict[str, str],
    unique_label: str,
    cleanup: list[tuple[dict[str, str], str]],
) -> tuple[str, int]:
    created = http_client.post(
        f"{mnemory_url}/api/memories",
        headers=headers,
        json={
            "content": f"revision contract {unique_label}",
            "role": "user",
            "labels": {"contract_run": unique_label},
            "infer": False,
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    results = payload.get("results", []) if isinstance(payload, dict) else []
    memory_id = payload.get("memory_id") if isinstance(payload, dict) else None
    if not memory_id and results:
        memory_id = results[0].get("id")
    assert isinstance(memory_id, str) and memory_id
    cleanup.append((headers, memory_id))

    return memory_id, _current_revision(http_client, mnemory_url, headers, memory_id)


def _current_revision(
    http_client: httpx.Client,
    mnemory_url: str,
    headers: dict[str, str],
    memory_id: str,
) -> int:
    fetched = http_client.post(
        f"{mnemory_url}/api/memories/by-ids",
        headers=headers,
        json={"ids": [memory_id]},
    )
    assert fetched.status_code == 200
    record = fetched.json()["results"][0]
    revision = (record.get("metadata") or {}).get("revision")
    if not isinstance(revision, int):
        pytest.skip("Mnemory does not expose revision-based mutation control")
    assert revision >= 1
    return revision


def _delete_current_memory(
    http_client: httpx.Client,
    mnemory_url: str,
    headers: dict[str, str],
    memory_id: str,
) -> None:
    revision = _current_revision(http_client, mnemory_url, headers, memory_id)
    deleted = http_client.delete(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers={**headers, "If-Match": str(revision)},
    )
    assert deleted.is_success


def _assert_revision_conflict(response: httpx.Response) -> None:
    assert response.status_code == 409
    detail = response.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "revision_conflict"
    assert isinstance(detail.get("current_revision"), int)


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
    revision_memory_cleanup: list[tuple[dict[str, str], str]],
) -> None:
    headers = _auth_headers(make_service_jwt, contract_agent_id)
    create = http_client.post(
        f"{mnemory_url}/api/memories",
        headers=headers,
        json={
            "content": f"contract memory {unique_label}",
            "role": "user",
            "pinned": True,
            "labels": {"contract_run": unique_label},
            "infer": False,
        },
    )
    assert create.status_code == 200, create.text
    payload = create.json()
    results = payload.get("results", []) if isinstance(payload, dict) else []
    memory_id = payload.get("memory_id") if isinstance(payload, dict) else None
    if not memory_id and results:
        memory_id = results[0].get("id")
    assert isinstance(memory_id, str) and memory_id
    revision_memory_cleanup.append((headers, memory_id))

    listed = http_client.get(
        f"{mnemory_url}/api/memories",
        headers=headers,
        params={"role": "user", "limit": 100},
    )
    assert listed.status_code == 200
    data = listed.json()
    items = data if isinstance(data, list) else data.get("results", data.get("items", []))
    assert isinstance(items, list)
    assert any(
        (item.get("id") or item.get("memory_id")) == memory_id
        for item in items
        if isinstance(item, dict)
    )
    deleted = http_client.delete(f"{mnemory_url}/api/memories/{memory_id}", headers=headers)
    assert deleted.status_code == 200


def test_openapi_exposes_unified_if_match_on_memory_mutations(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
) -> None:
    # Current Mnemory protects schema discovery with the service JWT boundary.
    headers = _auth_headers(make_service_jwt, "contract-openapi-agent")
    schema = http_client.get(f"{mnemory_url}/api/openapi.json", headers=headers)
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    operations = (
        ("/memories/{memory_id}", "put"),
        ("/memories/{memory_id}", "delete"),
        ("/memories/{memory_id}/artifacts", "post"),
        ("/memories/{memory_id}/artifacts/{artifact_id}", "delete"),
    )
    for path, method in operations:
        parameters = paths[path][method].get("parameters", [])
        if_match = [
            parameter
            for parameter in parameters
            if parameter.get("in") == "header" and parameter.get("name", "").lower() == "if-match"
        ]
        if not if_match:
            pytest.skip("Mnemory does not expose revision-based mutation control")
        assert len(if_match) == 1, f"{method.upper()} {path} must expose If-Match"
        assert if_match[0]["name"] == "If-Match"
        assert if_match[0].get("required") is False
        assert if_match[0]["schema"]["type"] == "string"


def test_update_if_match_accepts_current_and_rejects_stale_malformed_and_conflicting(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
    revision_memory_cleanup: list[tuple[dict[str, str], str]],
) -> None:
    headers = _auth_headers(make_service_jwt, contract_agent_id)
    memory_id, revision = _create_revisioned_memory(
        http_client, mnemory_url, headers, unique_label, revision_memory_cleanup
    )
    updated = http_client.put(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers={**headers, "If-Match": str(revision)},
        json={"content": f"updated revision contract {unique_label}"},
    )
    assert updated.status_code == 200

    stale = http_client.put(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers={**headers, "If-Match": str(revision)},
        json={"pinned": True},
    )
    _assert_revision_conflict(stale)

    malformed = http_client.put(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers={**headers, "If-Match": "not-an-integer"},
        json={"pinned": True},
    )
    assert malformed.status_code == 422

    conflicting = http_client.put(
        f"{mnemory_url}/api/memories/{memory_id}",
        headers=[*headers.items(), ("If-Match", "1"), ("If-Match", "2")],
        json={"pinned": True},
    )
    assert conflicting.status_code == 422
    _delete_current_memory(http_client, mnemory_url, headers, memory_id)


@pytest.mark.parametrize(
    ("method", "path_suffix", "json_body"),
    [
        ("delete", "", None),
        (
            "post",
            "/artifacts",
            {
                "content": "contract artifact",
                "filename": "contract.txt",
                "content_type": "text/plain",
            },
        ),
    ],
)
def test_delete_and_artifact_save_reject_stale_if_match(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
    revision_memory_cleanup: list[tuple[dict[str, str], str]],
    method: str,
    path_suffix: str,
    json_body: dict[str, str] | None,
) -> None:
    headers = _auth_headers(make_service_jwt, contract_agent_id)
    memory_id, revision = _create_revisioned_memory(
        http_client, mnemory_url, headers, unique_label, revision_memory_cleanup
    )
    response = http_client.request(
        method,
        f"{mnemory_url}/api/memories/{memory_id}{path_suffix}",
        headers={**headers, "If-Match": str(revision + 1)},
        json=json_body,
    )
    _assert_revision_conflict(response)
    accepted = http_client.request(
        method,
        f"{mnemory_url}/api/memories/{memory_id}{path_suffix}",
        headers={**headers, "If-Match": str(revision)},
        json=json_body,
    )
    assert accepted.is_success
    if method != "delete":
        _delete_current_memory(http_client, mnemory_url, headers, memory_id)


def test_artifact_delete_accepts_current_and_rejects_stale_if_match(
    http_client: httpx.Client,
    mnemory_url: str,
    make_service_jwt: Callable[..., str],
    contract_agent_id: str,
    unique_label: str,
    revision_memory_cleanup: list[tuple[dict[str, str], str]],
) -> None:
    headers = _auth_headers(make_service_jwt, contract_agent_id)
    memory_id, revision = _create_revisioned_memory(
        http_client, mnemory_url, headers, unique_label, revision_memory_cleanup
    )
    created = http_client.post(
        f"{mnemory_url}/api/memories/{memory_id}/artifacts",
        headers={**headers, "If-Match": str(revision)},
        json={
            "content": "contract artifact",
            "filename": "contract.txt",
            "content_type": "text/plain",
        },
    )
    assert created.status_code == 200
    artifact_id = created.json().get("artifact_id") or created.json().get("id")
    assert isinstance(artifact_id, str) and artifact_id

    stale = http_client.delete(
        f"{mnemory_url}/api/memories/{memory_id}/artifacts/{artifact_id}",
        headers={**headers, "If-Match": str(revision)},
    )
    _assert_revision_conflict(stale)
    current_revision = _current_revision(http_client, mnemory_url, headers, memory_id)
    deleted = http_client.delete(
        f"{mnemory_url}/api/memories/{memory_id}/artifacts/{artifact_id}",
        headers={**headers, "If-Match": str(current_revision)},
    )
    assert deleted.is_success
    _delete_current_memory(http_client, mnemory_url, headers, memory_id)
