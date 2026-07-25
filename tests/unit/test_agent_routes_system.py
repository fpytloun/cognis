from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes.agents import _validate_agent_definition_payload
from cognis.store.queries import create_agent, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, email: str = "user@example.com") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role="user",
        )
        await session.commit()


async def _seed_unavailable_backend_agent(app: object) -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_agent(
            session,
            agent_id="future-agent",
            owner_email="user@example.com",
            name="Future Agent",
            capabilities={
                "memory_backend": "future-memory",
                "memory_backend_options": {"future_option": True},
                "guardrails_backend": "intaris",
            },
            agent_profiles={
                "specialist": {
                    "profile_id": "specialist",
                    "memory_backend_options": {"profile_option": "kept"},
                }
            },
            status="draft",
        )
        await session.commit()


def test_memory_backend_descriptors_are_authoritative(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.get("/api/v1/agents/memory-backends", headers=headers)

        assert response.status_code == 200
        items = {item["id"]: item for item in response.json()["items"]}
        assert items["none"]["modes"] == []
        assert items["mnemory"]["defaults"] == {"mode": "full_auto"}
        assert [mode["id"] for mode in items["mnemory"]["modes"]] == [
            "full_auto",
            "proactive",
            "on_demand",
        ]


def test_new_unknown_memory_backend_is_rejected_but_existing_value_can_round_trip() -> None:
    payload: dict[str, object] = {
        "agent_id": "future-agent",
        "owner_email": "owner@example.com",
        "name": "Future agent",
        "capabilities": {
            "memory_backend": "future-memory",
            "memory_backend_options": {"future_option": True},
            "guardrails_backend": "intaris",
        },
    }

    with pytest.raises(HTTPException, match="Unknown memory_backend 'future-memory'"):
        _validate_agent_definition_payload(payload)

    definition = _validate_agent_definition_payload(
        payload,
        allowed_unavailable_memory_backend="future-memory",
        allowed_unavailable_memory_options={"future_option": True},
    )
    assert definition.capabilities.memory_backend == "future-memory"
    assert definition.capabilities.memory_backend_options == {"future_option": True}

    changed_payload = {
        **payload,
        "capabilities": {
            "memory_backend": "future-memory",
            "memory_backend_options": {"future_option": False},
            "guardrails_backend": "intaris",
        },
    }
    with pytest.raises(HTTPException, match="Unknown memory_backend 'future-memory'"):
        _validate_agent_definition_payload(
            changed_payload,
            allowed_unavailable_memory_backend="future-memory",
            allowed_unavailable_memory_options={"future_option": True},
        )


def test_unavailable_backend_profile_options_are_read_only() -> None:
    payload: dict[str, object] = {
        "agent_id": "future-agent",
        "owner_email": "owner@example.com",
        "name": "Future agent",
        "capabilities": {
            "memory_backend": "future-memory",
            "memory_backend_options": {"future_option": True},
            "guardrails_backend": "intaris",
        },
        "agent_profiles": {
            "specialist": {
                "profile_id": "specialist",
                "memory_backend_options": {"profile_option": "kept"},
            }
        },
    }
    allowed_profiles = {"specialist": {"profile_option": "kept"}}

    definition = _validate_agent_definition_payload(
        payload,
        allowed_unavailable_memory_backend="future-memory",
        allowed_unavailable_memory_options={"future_option": True},
        allowed_unavailable_profile_memory_options=allowed_profiles,
    )
    assert definition.agent_profiles["specialist"].memory_backend_options == {
        "profile_option": "kept"
    }

    changed_payload = {
        **payload,
        "agent_profiles": {
            "specialist": {
                "profile_id": "specialist",
                "memory_backend_options": {"profile_option": "changed"},
            }
        },
    }
    with pytest.raises(HTTPException, match="Unknown memory_backend 'future-memory'"):
        _validate_agent_definition_payload(
            changed_payload,
            allowed_unavailable_memory_backend="future-memory",
            allowed_unavailable_memory_options={"future_option": True},
            allowed_unavailable_profile_memory_options=allowed_profiles,
        )

    existing_profiles = payload["agent_profiles"]
    assert isinstance(existing_profiles, dict)
    added_payload = {
        **payload,
        "agent_profiles": {
            **existing_profiles,
            "new-profile": {
                "profile_id": "new-profile",
                "memory_backend_options": {"new_option": True},
            },
        },
    }
    with pytest.raises(HTTPException, match="Unknown memory_backend 'future-memory'"):
        _validate_agent_definition_payload(
            added_payload,
            allowed_unavailable_memory_backend="future-memory",
            allowed_unavailable_memory_options={"future_option": True},
            allowed_unavailable_profile_memory_options=allowed_profiles,
        )


def test_duplicate_preserves_unavailable_backend_fail_closed_configuration(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        asyncio.run(_seed_unavailable_backend_agent(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.post("/api/v1/agents/future-agent/duplicate", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["capabilities"] == {
            "memory_backend": "future-memory",
            "memory_backend_options": {"future_option": True},
            "guardrails_backend": "intaris",
        }
        assert body["agent_profiles"]["specialist"]["memory_backend_options"] == {
            "profile_option": "kept"
        }


def test_system_agent_detail_includes_default_skills(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.get("/api/v1/agents/system:implement", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["skills"] == {"items": [{"skill_id": "cognis-coding", "enabled": True}]}
        assert "skills" in body["editable_fields"]


def test_system_agent_update_accepts_skill_overrides(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )

        assert update.status_code == 200
        body = update.json()
        assert body["has_overrides"] is True
        assert body["skills"] == {"items": [{"skill_id": "cognis-task-manager", "enabled": True}]}

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["skills"] == {
            "items": [{"skill_id": "cognis-task-manager", "enabled": True}]
        }


def test_system_agent_update_accepts_runtime_profile_overrides(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")
        profiles = {
            "junior": {
                "profile_id": "junior",
                "description": "Bounded routine implementation.",
                "model": "test-model",
                "reasoning_effort": "low",
                "agent_switchable": True,
                "enabled": True,
            },
            "senior": {
                "profile_id": "senior",
                "description": "Complex high-risk implementation.",
                "model": "test-model",
                "reasoning_effort": "high",
                "agent_switchable": True,
                "enabled": True,
            },
        }

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "agent_profiles": profiles,
                "default_agent_profile_id": "junior",
            },
        )

        assert update.status_code == 200
        body = update.json()
        assert body["agent_profiles"] == profiles
        assert body["default_agent_profile_id"] == "junior"
        assert body["has_overrides"] is True

        partial = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={"llm_config": {"reasoning_effort": "medium"}},
        )
        assert partial.status_code == 200
        assert partial.json()["agent_profiles"] == profiles
        assert partial.json()["default_agent_profile_id"] == "junior"

        disabled = client.post(
            "/api/v1/agents/system:implement/disable",
            headers=headers,
        )
        assert disabled.status_code == 200
        enabled = client.post(
            "/api/v1/agents/system:implement/enable",
            headers=headers,
        )
        assert enabled.status_code == 200
        assert enabled.json()["agent_profiles"] == profiles
        assert enabled.json()["default_agent_profile_id"] == "junior"

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["agent_profiles"] == profiles
        assert detail.json()["default_agent_profile_id"] == "junior"


def test_system_agent_update_rejects_invalid_runtime_profile_key(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "agent_profiles": {
                    "bad/id": {
                        "description": "Invalid profile key.",
                        "agent_switchable": True,
                    }
                },
                "default_agent_profile_id": "bad/id",
            },
        )

        assert update.status_code == 400
        assert update.json()["error"]["code"] == "validation_error"


def test_reset_system_agent_overrides_restores_default_skills(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )
        assert update.status_code == 200

        reset = client.post("/api/v1/agents/system:implement/reset-overrides", headers=headers)

        assert reset.status_code == 200
        assert reset.json() == {"ok": True}

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["skills"] == {
            "items": [{"skill_id": "cognis-coding", "enabled": True}]
        }


def test_partial_system_agent_override_updates_preserve_existing_fields(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        first = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "skills": {
                    "items": [{"skill_id": "cognis-task-manager", "enabled": True}],
                }
            },
        )
        assert first.status_code == 200

        second = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={"llm_config": {"reasoning_effort": "high"}},
        )

        assert second.status_code == 200
        body = second.json()
        assert body["skills"] == {"items": [{"skill_id": "cognis-task-manager", "enabled": True}]}
        assert body["llm_config"]["reasoning_effort"] == "high"


def test_system_agent_update_accepts_tool_and_permission_overrides(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        asyncio.run(_seed_user(client.app))
        headers = _auth_headers(client.app, email="user@example.com")

        update = client.put(
            "/api/v1/agents/system:implement",
            headers=headers,
            json={
                "tools": {
                    "delegation_tools": True,
                    "disabled_mcp_servers": ["local_mcp:mcp-arr"],
                    "disabled_tools": ["mcp:mcp-arr:arr_status"],
                },
                "permissions": {
                    "tool_permissions": {
                        "mcp:mcp-arr:arr_search_all": "allow",
                    },
                    "can_delegate": True,
                    "max_delegation_depth": 3,
                },
            },
        )

        assert update.status_code == 200
        body = update.json()
        assert body["has_overrides"] is True
        assert body["tools"]["disabled_mcp_servers"] == ["local_mcp:mcp-arr"]
        assert body["tools"]["disabled_tools"] == ["mcp:mcp-arr:arr_status"]
        assert body["permissions"]["tool_permissions"] == {"mcp:mcp-arr:arr_search_all": "allow"}

        detail = client.get("/api/v1/agents/system:implement", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["tools"]["disabled_mcp_servers"] == ["local_mcp:mcp-arr"]
        assert detail.json()["permissions"]["tool_permissions"] == {
            "mcp:mcp-arr:arr_search_all": "allow"
        }
