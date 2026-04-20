from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.models import ModelRouting
from cognis.store.queries import create_llm_provider, create_user, list_model_routing


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
                        ],
                    },
                    status="active",
                )
                session.add(ModelRouting(task_type="simple_inline", provider_id="openai", model="gpt-5.4"))
                session.add(ModelRouting(task_type="legacy_custom", provider_id="openai", model="gpt-5.4"))
                await session.commit()

        asyncio.run(_seed())

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
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["default"] == {"model": "gpt-5.4", "reasoning_effort": "xhigh"}
        assert payload["speech_to_text"] == {
            "model": "gpt-4o-transcribe",
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

        asyncio.run(_assert_db())


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

        asyncio.run(_seed())

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={
                "speech_to_text": {"model": "gpt-4o-transcribe", "reasoning_effort": "low"}
            },
        )

        assert response.status_code == 422
        assert "does not support reasoning_effort" in response.json()["error"]["message"]


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

        asyncio.run(_seed())

        headers = _auth_headers(app, email="admin@example.com", role="admin")
        response = client.put(
            "/api/v1/model-routing",
            headers=headers,
            json={"classifier": {"model": "gpt-5.4", "reasoning_effort": "medum"}},
        )

        assert response.status_code == 422
        assert "is invalid" in response.json()["error"]["message"]
