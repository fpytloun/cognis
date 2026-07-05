from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.store.models import ModelRouting
from cognis.store.queries import create_llm_provider, create_user, list_model_routing


class _ChatGptOAuthStub:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.status_checked: list[str] = []
        self.cleared: list[str] = []

    async def start_chatgpt_oauth(self, provider_id: str) -> dict[str, object]:
        self.started.append(provider_id)
        return {
            "status": "pending",
            "verification_url": "https://example.test/verify",
            "user_code": "ABCD-EFGH",
            "interval": 5,
            "expires_at": 1234567890,
        }

    async def get_chatgpt_oauth_status(self, provider_id: str) -> dict[str, object]:
        self.status_checked.append(provider_id)
        return {"provider_id": provider_id, "status": "authorized"}

    async def clear_chatgpt_oauth(self, provider_id: str) -> bool:
        self.cleared.append(provider_id)
        return True


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(  # type: ignore[attr-defined]
        email, email.split("@")[0].title(), role
    )
    return {"Authorization": f"Bearer {token}"}


def test_model_routing_put_round_trips_nested_entries_and_deletes_legacy_rows(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}")  # type: ignore[attr-defined]
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [
                            {
                                "model_id": "gpt-5.4",
                                "supports_reasoning": True,
                                "reasoning_efforts": [
                                    "default",
                                    "none",
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                ],
                            },
                            {"model_id": "gpt-4o-transcribe", "supports_reasoning": False},
                            {
                                "model_id": "gpt-image-1",
                                "supports_reasoning": False,
                                "supports_image_generation": True,
                            },
                            {
                                "model_id": "gpt-4o",
                                "supports_vision": True,
                                "supports_file_input": True,
                            },
                            {
                                "model_id": "text-embedding-3-small",
                                "supports_embedding": True,
                            },
                        ],
                    },
                    status="active",
                )
                session.add(
                    ModelRouting(
                        route_id="legacy_simple_inline",
                        task_type="simple_inline",
                        provider_id="openai",
                        model="gpt-5.4",
                    )
                )
                session.add(
                    ModelRouting(
                        route_id="legacy_custom",
                        task_type="legacy_custom",
                        provider_id="openai",
                        model="gpt-5.4",
                    )
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={
                "default": {"model": "gpt-5.4", "reasoning_effort": "xhigh"},
                "classifier": {"model": "gpt-5.4", "reasoning_effort": "low"},
                "compaction": {"model": "gpt-5.4", "reasoning_effort": "medium"},
                "evaluator": {"model": "gpt-5.4", "reasoning_effort": "high"},
                "speech_to_text": {"model": "gpt-4o-transcribe", "reasoning_effort": None},
                "image_generation": {"model": "gpt-image-1", "reasoning_effort": None},
                "attachment_analysis": {"model": "gpt-4o", "reasoning_effort": None},
                "embedding": {"model": "text-embedding-3-small", "reasoning_effort": None},
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["default"] == {"model": "gpt-5.4", "reasoning_effort": "xhigh"}
        assert payload["speech_to_text"] == {
            "model": "gpt-4o-transcribe",
            "reasoning_effort": None,
        }
        assert payload["attachment_analysis"] == {
            "model": "gpt-4o",
            "reasoning_effort": None,
        }
        assert payload["embedding"] == {
            "model": "text-embedding-3-small",
            "reasoning_effort": None,
        }
        assert "simple_inline" not in payload

        async def _assert_db() -> None:
            async with app.state.session_factory() as session:
                rows = await list_model_routing(session)
            rows_by_task = {row.task_type: row for row in rows}
            assert "simple_inline" not in rows_by_task
            assert "legacy_custom" not in rows_by_task
            assert rows_by_task["classifier"].config == {"reasoning_effort": "low"}
            assert rows_by_task["speech_to_text"].config is None
            assert rows_by_task["embedding"].model == "text-embedding-3-small"
            assert rows_by_task["default"].provider_id is None

        client.portal.call(_assert_db)


def test_model_routing_get_does_not_invent_reasoning_defaults(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.get("/api/v1/model-routing", headers=headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["classifier"] == {"model": None, "reasoning_effort": None}
        assert payload["compaction"] == {"model": None, "reasoning_effort": None}
        assert payload["evaluator"] == {"model": None, "reasoning_effort": None}


def test_model_routing_put_rejects_models_not_visible_to_system_routing(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="private",
                    display_name="Private",
                    location="controller",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={"preset": "openai", "models": [{"model_id": "private-model"}]},
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"default": {"model": "private-model", "reasoning_effort": None}},
        )

        assert response.status_code == 422
        assert "not present in configured providers" in response.text


def test_non_admin_can_create_and_manage_user_owned_anthropic_executor_provider(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="owner@example.com", role="user")
        payload = {
            "provider_id": "meridian-claude",
            "display_name": "Meridian Claude",
            "location": "executor",
            "backend": "litellm",
            "config": {
                "preset": "anthropic",
                "default_model": "claude-opus-4-7",
                "models": [{"model_id": "claude-opus-4-7", "supports_tools": True}],
                "auth_config": {"mode": "none"},
                "executor_id": "maitrea",
                "base_url": "http://127.0.0.1:8090",
                "api_base": "http://127.0.0.1:8090",
            },
        }

        response = client.post("/api/v1/llm-providers", headers=headers, json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["provider_id"] == "meridian-claude"
        assert body["owner_email"] == "owner@example.com"

        response = client.post(
            "/api/v1/llm-providers/meridian-claude/set-default",
            headers=headers,
        )
        assert response.status_code == 200

        response = client.get("/api/v1/llm-providers", headers=headers)
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(
            item["provider_id"] == "meridian-claude" and item["is_default"] for item in items
        )


def test_executor_provider_requires_explicit_executor_target(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        headers = _auth_headers(app, email="admin@example.com", role="admin")

        payload = {
            "provider_id": "remote-openai",
            "display_name": "Remote OpenAI",
            "location": "executor",
            "backend": "litellm",
            "config": {
                "preset": "openai_compatible",
                "default_model": "gpt-4o-mini",
                "models": [{"model_id": "gpt-4o-mini"}],
                "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
            },
        }

        response = client.post("/api/v1/llm-providers", headers=headers, json=payload)
        assert response.status_code == 400
        assert "executor_id or executor_labels" in response.text

        payload["config"] = {**payload["config"], "executor_id": "maitrea"}
        response = client.post("/api/v1/llm-providers", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json()["config"]["executor_id"] == "maitrea"


def test_admin_provider_create_defaults_to_user_owned(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        headers = _auth_headers(app, email="admin@example.com", role="admin")

        response = client.post(
            "/api/v1/llm-providers",
            headers=headers,
            json={
                "provider_id": "admin-personal",
                "display_name": "Admin Personal",
                "location": "controller",
                "backend": "litellm",
                "config": {
                    "preset": "openai_compatible",
                    "default_model": "gpt-4o-mini",
                    "models": [{"model_id": "gpt-4o-mini"}],
                    "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["owner_email"] == "admin@example.com"


def test_admin_provider_create_can_explicitly_create_shared_system_provider(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        headers = _auth_headers(app, email="admin@example.com", role="admin")

        response = client.post(
            "/api/v1/llm-providers",
            headers=headers,
            json={
                "provider_id": "shared-provider",
                "display_name": "Shared Provider",
                "location": "controller",
                "backend": "litellm",
                "config": {
                    "scope": "system",
                    "preset": "openai_compatible",
                    "default_model": "gpt-4o-mini",
                    "models": [{"model_id": "gpt-4o-mini"}],
                    "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["owner_email"] == "system@cognis.local"


def test_admin_can_reassign_existing_provider_between_personal_and_system_scope(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="scoped-provider",
                    display_name="Scoped Provider",
                    location="controller",
                    backend="litellm",
                    owner_email="admin@example.com",
                    config={
                        "scope": "user",
                        "preset": "openai_compatible",
                        "default_model": "gpt-4o-mini",
                        "models": [{"model_id": "gpt-4o-mini"}],
                        "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="admin@example.com", role="admin")

        response = client.put(
            "/api/v1/llm-providers/scoped-provider",
            headers=headers,
            json={
                "display_name": "Scoped Provider",
                "location": "controller",
                "backend": "litellm",
                "owner_scope": "system",
                "status": "active",
                "config": {
                    "scope": "system",
                    "preset": "openai_compatible",
                    "default_model": "gpt-4o-mini",
                    "models": [{"model_id": "gpt-4o-mini"}],
                    "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["owner_email"] == SYSTEM_USER_EMAIL

        response = client.put(
            "/api/v1/llm-providers/scoped-provider",
            headers=headers,
            json={
                "display_name": "Scoped Provider",
                "location": "controller",
                "backend": "litellm",
                "owner_scope": "user",
                "status": "active",
                "config": {
                    "scope": "user",
                    "preset": "openai_compatible",
                    "default_model": "gpt-4o-mini",
                    "models": [{"model_id": "gpt-4o-mini"}],
                    "auth_config": {"mode": "env", "env_var": "OPENAI_API_KEY"},
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["owner_email"] == "admin@example.com"


def test_llm_provider_list_includes_disabled_providers(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="disabled-provider",
                    display_name="Disabled Provider",
                    location="controller",
                    backend="litellm",
                    owner_email=SYSTEM_USER_EMAIL,
                    config={"preset": "openai", "models": [{"model_id": "gpt-4o-mini"}]},
                    status="disabled",
                )
                await session.commit()

        client.portal.call(_seed)
        response = client.get(
            "/api/v1/llm-providers",
            headers=_auth_headers(app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 200
        providers = response.json()["items"]
        disabled = [item for item in providers if item["provider_id"] == "disabled-provider"]
        assert len(disabled) == 1
        assert disabled[0]["status"] == "disabled"


def test_non_admin_can_create_provider_for_themselves(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="owner@example.com", role="user")

        response = client.post(
            "/api/v1/llm-providers",
            headers=headers,
            json={
                "provider_id": "local-openai",
                "display_name": "Local OpenAI Compatible",
                "location": "controller",
                "backend": "litellm",
                "config": {
                    "preset": "openai_compatible",
                    "base_url": "https://llm.example.test/v1",
                    "auth_config": {"mode": "env", "env_var": "LOCAL_OPENAI_API_KEY"},
                    "models": [{"model_id": "local-model", "supports_tools": True}],
                    "default_model": "local-model",
                },
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["provider_id"] == "local-openai"
        assert body["owner_email"] == "owner@example.com"

        response = client.post("/api/v1/llm-providers/local-openai/set-default", headers=headers)
        assert response.status_code == 200

        response = client.get("/api/v1/llm-providers", headers=headers)
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(item["provider_id"] == "local-openai" and item["is_default"] for item in items)


def test_non_admin_can_manage_own_chatgpt_oauth_provider(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        oauth_stub = _ChatGptOAuthStub()
        app.state.providers.llm.start_chatgpt_oauth = oauth_stub.start_chatgpt_oauth
        app.state.providers.llm.get_chatgpt_oauth_status = oauth_stub.get_chatgpt_oauth_status
        app.state.providers.llm.clear_chatgpt_oauth = oauth_stub.clear_chatgpt_oauth

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_llm_provider(
                    session,
                    provider_id="chatgpt-owner",
                    display_name="Owner ChatGPT",
                    location="controller",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "chatgpt",
                        "auth_config": {"mode": "oauth"},
                        "models": [{"model_id": "gpt-5"}],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="owner@example.com", role="user")

        response = client.post(
            "/api/v1/llm-providers/chatgpt-owner/oauth/chatgpt/start", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert oauth_stub.started == ["chatgpt-owner"]

        response = client.get(
            "/api/v1/llm-providers/chatgpt-owner/oauth/chatgpt/status", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "authorized"
        assert oauth_stub.status_checked == ["chatgpt-owner"]

        response = client.delete(
            "/api/v1/llm-providers/chatgpt-owner/oauth/chatgpt", headers=headers
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert oauth_stub.cleared == ["chatgpt-owner"]


def test_non_admin_anthropic_oauth_start_state_is_readable(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_llm_provider(
                    session,
                    provider_id="anthropic-owner",
                    display_name="Owner Anthropic",
                    location="controller",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={
                        "preset": "anthropic",
                        "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
                        "models": [{"model_id": "claude-sonnet-4-5"}],
                        "default_model": "claude-sonnet-4-5",
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="owner@example.com", role="user")

        response = client.post(
            "/api/v1/llm-providers/anthropic-owner/oauth/anthropic/start",
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert body["authorization_url"].startswith("https://claude.ai/oauth/authorize?")

        response = client.get(
            "/api/v1/llm-providers/anthropic-owner/oauth/anthropic/status",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"


def test_non_admin_cannot_manage_other_users_chatgpt_oauth_provider(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        oauth_stub = _ChatGptOAuthStub()
        app.state.providers.llm.start_chatgpt_oauth = oauth_stub.start_chatgpt_oauth

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="other@example.com",
                    name="Other",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_llm_provider(
                    session,
                    provider_id="chatgpt-owner",
                    display_name="Owner ChatGPT",
                    location="controller",
                    backend="litellm",
                    owner_email="owner@example.com",
                    config={"preset": "chatgpt", "auth_config": {"mode": "oauth"}},
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)
        headers = _auth_headers(app, email="other@example.com", role="user")

        response = client.post(
            "/api/v1/llm-providers/chatgpt-owner/oauth/chatgpt/start", headers=headers
        )

        assert response.status_code == 404
        assert oauth_stub.started == []


def test_model_routing_put_rejects_reasoning_effort_for_non_text_routes(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [{"model_id": "gpt-4o-transcribe", "supports_reasoning": False}],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"speech_to_text": {"model": "gpt-4o-transcribe", "reasoning_effort": "low"}},
        )

        assert response.status_code == 422
        assert "does not support reasoning_effort" in response.json()["error"]["message"]


def test_model_routing_put_rejects_non_transcription_model_for_speech_to_text(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [{"model_id": "gpt-5.4", "supports_reasoning": True}],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"speech_to_text": {"model": "gpt-5.4", "reasoning_effort": None}},
        )

        assert response.status_code == 422
        assert "not eligible" in response.json()["error"]["message"]


def test_model_routing_put_rejects_non_image_model_for_image_generation(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-image-1",
                        "models": [
                            {"model_id": "gpt-5.4", "supports_reasoning": True},
                            {"model_id": "gpt-image-1", "supports_image_generation": True},
                            {"model_id": "gpt-4o", "supports_vision": True},
                        ],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"image_generation": {"model": "gpt-5.4", "reasoning_effort": None}},
        )

        assert response.status_code == 422
        assert "not eligible" in response.json()["error"]["message"]


def test_model_routing_put_rejects_non_multimodal_model_for_attachment_analysis(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "text-only-model",
                        "models": [
                            {"model_id": "text-only-model", "supports_reasoning": True},
                            {"model_id": "gpt-4o", "supports_vision": True},
                        ],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"attachment_analysis": {"model": "text-only-model", "reasoning_effort": None}},
        )

        assert response.status_code == 422
        assert "not eligible" in response.json()["error"]["message"]


def test_model_routing_put_accepts_only_declared_embedding_models(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}")  # type: ignore[attr-defined]
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [
                            {"model_id": "text-embedding-ish", "supports_embedding": False},
                            {"model_id": "text-embedding-3-small", "supports_embedding": True},
                        ],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        rejected = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"embedding": {"model": "text-embedding-ish", "reasoning_effort": None}},
        )
        accepted = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"embedding": {"model": "text-embedding-3-small", "reasoning_effort": None}},
        )

        assert rejected.status_code == 422
        assert "not eligible" in rejected.json()["error"]["message"]
        assert accepted.status_code == 200
        assert accepted.json()["embedding"] == {
            "model": "text-embedding-3-small",
            "reasoning_effort": None,
        }


def test_model_routing_put_accepts_inferred_embedding_models(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}")  # type: ignore[attr-defined]
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [{"model_id": "text-embedding-3-small"}],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"embedding": {"model": "text-embedding-3-small", "reasoning_effort": None}},
        )

        assert response.status_code == 200
        assert response.json()["embedding"] == {
            "model": "text-embedding-3-small",
            "reasoning_effort": None,
        }


def test_model_routing_put_accepts_same_session_model_for_compaction_only(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}")  # type: ignore[attr-defined]
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        accepted = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={
                "compaction": {
                    "model": "__same_session_model__",
                    "reasoning_effort": None,
                }
            },
        )
        rejected = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={
                "classifier": {
                    "model": "__same_session_model__",
                    "reasoning_effort": None,
                }
            },
        )

        assert accepted.status_code == 200
        assert accepted.json()["compaction"] == {
            "model": "__same_session_model__",
            "reasoning_effort": None,
        }
        assert rejected.status_code == 422
        assert "same-session model sentinel" in rejected.json()["error"]["message"]


def test_model_routing_put_rejects_invalid_reasoning_effort_value(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await create_llm_provider(
                    session,
                    provider_id="openai",
                    display_name="OpenAI",
                    location="controller",
                    backend="litellm",
                    config={
                        "preset": "openai",
                        "default_model": "gpt-5.4",
                        "models": [
                            {
                                "model_id": "gpt-5.4",
                                "supports_reasoning": True,
                                "reasoning_efforts": [
                                    "default",
                                    "none",
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                ],
                            }
                        ],
                    },
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"classifier": {"model": "gpt-5.4", "reasoning_effort": "medum"}},
        )

        assert response.status_code == 422
        assert "is invalid" in response.json()["error"]["message"]
