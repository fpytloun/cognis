from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cognis.api.routes.settings as settings_routes
from cognis.api.app import create_app
from cognis.api.routes.settings import _validate_llm_provider_payload
from cognis.store.queries import (
    AUTO_RECAP_ENABLED_STATE_KEY,
    create_user,
    get_user_ui_state,
    upsert_user_ui_state,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(app: object, *, email: str = "user@example.com", role: str = "user") -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await create_user(
            session,
            email=email,
            name=email.split("@")[0].title(),
            password_hash=app.state.password_hasher.hash("password123"),  # type: ignore[attr-defined]
            role=role,
        )
        await session.commit()


async def _store_user_preferences_state(app: object, value: dict[str, object]) -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        await upsert_user_ui_state(
            session,
            "user@example.com",
            settings_routes.USER_PREFERENCES_STATE_KEY,
            value,
        )
        await session.commit()


async def _auto_recap_enabled_at(app: object) -> object:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        row = await get_user_ui_state(session, "user@example.com", AUTO_RECAP_ENABLED_STATE_KEY)
        return row.updated_at if row else None


def test_validate_llm_provider_payload_accepts_chatgpt_direct_codex() -> None:
    _validate_llm_provider_payload(
        "controller",
        {"preset": "chatgpt", "codex_transport": "direct"},
    )


def test_validate_llm_provider_payload_accepts_controller_anthropic_subscription() -> None:
    _validate_llm_provider_payload(
        "controller",
        {
            "preset": "anthropic",
            "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
        },
    )


def test_validate_llm_provider_payload_rejects_executor_anthropic_subscription() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "executor",
            {
                "preset": "anthropic",
                "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
            },
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_validate_llm_provider_payload_rejects_unknown_codex_transport() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "chatgpt", "codex_transport": "websocket"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_validate_llm_provider_payload_rejects_direct_codex_for_non_chatgpt() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "openai", "codex_transport": "direct"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_validate_llm_provider_payload_accepts_message_projection_policy() -> None:
    _validate_llm_provider_payload(
        "controller",
        {"preset": "openai_compatible", "message_projection_policy": "anthropic_messages"},
    )


def test_validate_llm_provider_payload_rejects_unknown_message_projection_policy() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_llm_provider_payload(
            "controller",
            {"preset": "openai_compatible", "message_projection_policy": "surprising"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "validation_error"


def test_user_preferences_default_and_update(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")

        default_response = client.get("/api/v1/user-preferences", headers=headers)
        assert default_response.status_code == 200
        assert default_response.json()["display"]["dashboard_workspace_windows"] is True
        assert default_response.json()["display"]["pinned_agents_collapsed"] is True
        assert default_response.json()["display"]["pinned_agents_order_by_activity"] is True
        assert default_response.json()["display"]["conversation_sidebar_sections"] is True
        assert default_response.json()["display"]["conversation_sidebar_dense"] is False
        assert default_response.json()["display"]["conversation_project_badge"] == "name"
        assert default_response.json()["chat"] == {
            "enter_to_send": True,
            "adapt_tts": False,
            "show_thinking_blocks": False,
            "group_tool_calls": True,
            "keep_assistant_messages_separate": False,
            "show_internal_tool_calls": False,
            "auto_recap": True,
        }
        assert default_response.json()["notifications"] == {"include_content": True}
        assert (
            client.put(
                "/api/v1/user-preferences",
                headers=headers,
                json={"chat": {"auto_recap": False}},
            ).status_code
            == 200
        )
        assert (
            client.get("/api/v1/user-preferences", headers=headers).json()["chat"]["auto_recap"]
            is False
        )

        payload = {
            "display": {
                "theme": "system",
                "theme_family": "classic",
                "language": "cs-CZ",
                "dashboard_workspace_windows": True,
                "pinned_agents_collapsed": False,
                "pinned_agents_order_by_activity": False,
                "conversation_sidebar_sections": False,
                "conversation_sidebar_dense": True,
                "conversation_project_badge": "name",
            },
            "chat": {
                "enter_to_send": False,
                "adapt_tts": True,
                "show_thinking_blocks": True,
                "group_tool_calls": True,
                "keep_assistant_messages_separate": True,
                "show_internal_tool_calls": True,
                "auto_recap": True,
            },
            "notifications": {"include_content": False},
        }
        update_response = client.put("/api/v1/user-preferences", headers=headers, json=payload)
        assert update_response.status_code == 200
        assert update_response.json() == payload

        persisted_response = client.get("/api/v1/user-preferences", headers=headers)
        assert persisted_response.status_code == 200
        assert persisted_response.json() == payload
        enabled_at = client.portal.call(_auto_recap_enabled_at, client.app)
        assert enabled_at is not None
        payload["display"]["theme"] = "light"
        assert (
            client.put("/api/v1/user-preferences", headers=headers, json=payload).status_code == 200
        )
        assert client.portal.call(_auto_recap_enabled_at, client.app) == enabled_at


def test_user_preferences_reject_invalid_language(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")

        response = client.put(
            "/api/v1/user-preferences",
            headers=headers,
            json={
                "display": {"theme": "system", "language": "../bad"},
                "chat": {
                    "enter_to_send": True,
                    "show_thinking_blocks": True,
                    "group_tool_calls": True,
                    "show_internal_tool_calls": False,
                },
            },
        )

        assert response.status_code == 422


def test_user_preferences_adds_default_for_legacy_persisted_state(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        client.portal.call(
            _store_user_preferences_state,
            client.app,
            {
                "display": {"theme": "dark", "language": "en"},
                "chat": {
                    "show_thinking_blocks": True,
                    "group_tool_calls": True,
                    "show_internal_tool_calls": False,
                },
            },
        )

        response = client.get(
            "/api/v1/user-preferences",
            headers=_auth_headers(client.app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json()["display"]["theme_family"] == "classic"
        assert response.json()["display"]["theme"] == "dark"
        assert response.json()["display"]["dashboard_workspace_windows"] is True
        assert response.json()["display"]["pinned_agents_collapsed"] is True
        assert response.json()["display"]["pinned_agents_order_by_activity"] is True
        assert response.json()["display"]["conversation_sidebar_sections"] is True
        assert response.json()["display"]["conversation_sidebar_dense"] is False
        assert response.json()["chat"]["enter_to_send"] is True
        assert response.json()["chat"]["keep_assistant_messages_separate"] is False


def test_user_theme_families_round_trip_and_reject_unknown(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")
        payload = client.get("/api/v1/user-preferences", headers=headers).json()
        assert payload["display"]["theme_family"] == "classic"
        for family in ["porcelain", "classic", "gruvbox", "paper"]:
            for mode in ["light", "dark", "system"]:
                payload["display"].update(theme_family=family, theme=mode)
                response = client.put("/api/v1/user-preferences", headers=headers, json=payload)
                assert response.status_code == 200
                assert response.json() == payload
                assert client.get("/api/v1/user-preferences", headers=headers).json() == payload
        invalid = {**payload, "display": {**payload["display"], "theme_family": "future-family"}}
        assert (
            client.put("/api/v1/user-preferences", headers=headers, json=invalid).status_code == 422
        )
        assert client.get("/api/v1/user-preferences", headers=headers).json() == payload


@pytest.mark.parametrize("family", ["future-family", None, 42, {}])
def test_user_preferences_normalizes_only_invalid_persisted_family(
    monkeypatch: object, tmp_path: Path, family: object
) -> None:
    async def read_stored(app: object) -> object:
        async with app.state.session_factory() as session:  # type: ignore[attr-defined]
            row = await get_user_ui_state(
                session, "user@example.com", settings_routes.USER_PREFERENCES_STATE_KEY
            )
            assert row is not None
            return row.value

    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")
        original = client.get("/api/v1/user-preferences", headers=headers).json()
        original["display"].update(theme_family=family, theme="dark", language="cs-CZ")
        original["chat"].update(enter_to_send=False, auto_recap=False)
        original["notifications"]["include_content"] = False
        client.portal.call(_store_user_preferences_state, client.app, original)
        expected = {**original, "display": {**original["display"], "theme_family": "classic"}}
        response = client.get("/api/v1/user-preferences", headers=headers)
        assert response.status_code == 200
        assert response.json() == expected
        assert client.portal.call(read_stored, client.app) == original
        # The PUT path reads previous state too; an unknown stored family must
        # not prevent a validated update or reset unrelated preferences.
        expected["display"]["theme_family"] = "classic"
        assert (
            client.put("/api/v1/user-preferences", headers=headers, json=expected).json()
            == expected
        )
        assert client.portal.call(read_stored, client.app) == expected


def test_user_preferences_legacy_display_preserves_family_only(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client.app)
        headers = _auth_headers(client.app, email="user@example.com")
        url = "/api/v1/user-preferences"
        legacy = {"display": {"theme": "dark", "language": "cs-CZ"}}
        # A first legacy save still initializes the new default.
        assert (
            client.put(url, headers=headers, json=legacy).json()["display"]["theme_family"]
            == "classic"
        )
        assert (
            client.put(
                url, headers=headers, json={"display": {"theme_family": "paper", "language": "en"}}
            ).status_code
            == 200
        )
        result = client.put(url, headers=headers, json=legacy)
        assert result.status_code == 200
        assert result.json()["display"]["theme_family"] == "paper"
        assert result.json()["display"]["theme"] == "dark"
        assert result.json()["display"]["language"] == "cs-CZ"
        # An explicitly supplied empty display is also a legacy display body;
        # only the new key is retained, not a generic partial merge.
        result = client.put(url, headers=headers, json={"display": {}})
        assert result.json()["display"]["theme_family"] == "paper"
        assert result.json()["display"]["theme"] == "system"
        assert result.json()["display"]["language"] == "auto"
        for family in ["classic", "porcelain"]:
            result = client.put(url, headers=headers, json={"display": {"theme_family": family}})
            assert result.json()["display"]["theme_family"] == family
            # Explicit Porcelain remains an intentional valid choice, even if
            # it was saved while Porcelain was the product default.
            assert (
                client.put(url, headers=headers, json=legacy).json()["display"]["theme_family"]
                == family
            )
        assert (
            client.put(
                url, headers=headers, json={"display": {"theme_family": "future-family"}}
            ).status_code
            == 422
        )
        assert client.get(url, headers=headers).json()["display"]["theme_family"] == "porcelain"
        # Deliberate default request (no display field) retains existing reset semantics.
        client.put(url, headers=headers, json={"display": {"theme_family": "paper"}})
        assert (
            client.put(url, headers=headers, json={}).json()["display"]["theme_family"] == "classic"
        )


def test_setting_metadata_live_update_and_reset(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="admin@example.com", role="admin"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        detail = client.get(
            "/api/v1/settings/session.step_timeout_seconds",
            headers=headers,
        )
        assert detail.status_code == 200
        assert detail.json() == {
            "key": "session.step_timeout_seconds",
            "value": 14400,
            "category": "session",
            "section": "session",
            "label": "Step Timeout Seconds",
            "description": (
                "Default wall-clock timeout for a workflow step; "
                "agent execution overrides take precedence."
            ),
            "default_value": 14400,
            "value_type": "integer",
            "options": [],
            "minimum": 1,
            "maximum": None,
            "unit": "seconds",
            "is_overridden": False,
            "apply_scope": "hot",
            "updated_by": None,
            "updated_at": detail.json()["updated_at"],
        }

        updated = client.put(
            "/api/v1/settings/session.step_timeout_seconds",
            headers=headers,
            json={"value": 37},
        )
        assert updated.status_code == 200
        assert updated.json()["is_overridden"] is True
        assert client.app.state.agent_loop.default_step_timeout_seconds == 37

        reset = client.delete(
            "/api/v1/settings/session.step_timeout_seconds",
            headers=headers,
        )
        assert reset.status_code == 200
        assert reset.json()["value"] == 14400
        assert reset.json()["default_value"] == 14400
        assert reset.json()["is_overridden"] is False
        assert client.app.state.agent_loop.default_step_timeout_seconds == 14400

        tool_limit = client.put(
            "/api/v1/settings/session.max_tool_calls_per_turn",
            headers=headers,
            json={"value": 41},
        )
        assert tool_limit.status_code == 200
        assert client.app.state.agent_loop.default_max_tool_calls_per_turn == 41

        tool_limit_reset = client.delete(
            "/api/v1/settings/session.max_tool_calls_per_turn",
            headers=headers,
        )
        assert tool_limit_reset.status_code == 200
        assert client.app.state.agent_loop.default_max_tool_calls_per_turn == 500

        cycle_limit = client.put(
            "/api/v1/settings/session.max_llm_cycles_per_turn",
            headers=headers,
            json={"value": 240},
        )
        assert cycle_limit.status_code == 200
        assert client.app.state.agent_loop.default_max_llm_cycles_per_turn == 240

        excessive_cycle_limit = client.put(
            "/api/v1/settings/session.max_llm_cycles_per_turn",
            headers=headers,
            json={"value": 1001},
        )
        assert excessive_cycle_limit.status_code == 400
        assert client.app.state.agent_loop.default_max_llm_cycles_per_turn == 240

        cycle_limit_reset = client.delete(
            "/api/v1/settings/session.max_llm_cycles_per_turn",
            headers=headers,
        )
        assert cycle_limit_reset.status_code == 200
        assert client.app.state.agent_loop.default_max_llm_cycles_per_turn == 150


def test_settings_list_hides_legacy_noop_settings(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="admin@example.com", role="admin"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        response = client.get("/api/v1/settings", headers=headers)
        assert response.status_code == 200
        items = [item for category in response.json() for item in category["items"]]
        keys = {item["key"] for item in items}
        assert "session.max_tool_calls_per_turn" in keys
        assert "session.idle_timeout_seconds" not in keys
        assert "session.max_session_age_seconds" not in keys
        assert "security.max_connections" not in keys

        hidden_detail = client.get(
            "/api/v1/settings/session.idle_timeout_seconds",
            headers=headers,
        )
        assert hidden_detail.status_code == 404


def test_setting_update_restores_previous_value_when_live_apply_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="admin@example.com", role="admin"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")
        original_apply = settings_routes.apply_runtime_setting
        attempts = 0

        async def fail_once(app: object, key: str, value: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("apply failed")
            await original_apply(app, key, value)

        monkeypatch.setattr(settings_routes, "apply_runtime_setting", fail_once)
        response = client.put(
            "/api/v1/settings/session.step_timeout_seconds",
            headers=headers,
            json={"value": 99},
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "runtime_apply_failed"

        detail = client.get(
            "/api/v1/settings/session.step_timeout_seconds",
            headers=headers,
        )
        assert detail.status_code == 200
        assert detail.json()["value"] == 14400
        assert client.app.state.agent_loop.default_step_timeout_seconds == 14400


def test_executor_policy_update_is_non_destructive_and_applies_to_next_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(lambda: _seed_user(client.app, email="admin@example.com", role="admin"))
        headers = _auth_headers(client.app, email="admin@example.com", role="admin")

        async def unexpected_hot_apply(app: object, key: str, value: object) -> None:
            raise AssertionError("next-runtime executor policy must not clean up active executors")

        monkeypatch.setattr(
            settings_routes,
            "apply_runtime_setting",
            unexpected_hot_apply,
        )
        response = client.put(
            "/api/v1/settings/executors.allow_in_process",
            headers=headers,
            json={"value": False},
        )

        assert response.status_code == 200
        assert response.json()["value"] is False
        assert response.json()["apply_scope"] == "next_runtime"
