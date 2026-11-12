"""Integration-style tests for the TTS / STT API routes.

The TTS route is exercised end-to-end with a stub LLM provider so we can
verify cache hit/miss behavior, signed-URL response shape, and the
``tts.enabled`` master kill-switch without depending on a real provider.

The STT route is exercised similarly: we stub the provider's ``transcribe``
method and feed both the ``file=`` and ``artifact_id=`` paths.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.speech_cache import SpeechGenerationInterrupted
from cognis.models.config import SpeechToTextResult, TextToSpeechResult
from cognis.store.queries import (
    create_user,
    upsert_model_routing,
    upsert_setting,
)


def _create_test_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    return TestClient(create_app())


def _auth_headers(app: Any, email: str = "user@example.com") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, "User", "user")
    return {"Authorization": f"Bearer {token}"}


async def _seed_user(client: TestClient, email: str = "user@example.com") -> None:
    async with client.app.state.session_factory() as session:
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=client.app.state.password_hasher.hash("password123"),
            role="user",
        )
        await session.commit()


async def _seed_tts_routing(client: TestClient) -> None:
    async with client.app.state.session_factory() as session:
        await upsert_model_routing(
            session,
            task_type="text_to_speech",
            provider_id=None,
            model="tts-1",
            config=None,
        )
        await session.commit()


async def _seed_stt_routing(client: TestClient) -> None:
    async with client.app.state.session_factory() as session:
        await upsert_model_routing(
            session,
            task_type="speech_to_text",
            provider_id=None,
            model="whisper-1",
            config=None,
        )
        await session.commit()


class _StubLLMProvider:
    def __init__(self) -> None:
        self.synthesize_calls: list[dict[str, Any]] = []
        self.transcribe_calls: list[dict[str, Any]] = []

    async def discover_voices(self, provider_id: str, *, model: str | None = None):
        return [
            {"voice_id": voice, "name": voice}
            for voice in (
                "alloy",
                "ash",
                "coral",
                "echo",
                "fable",
                "onyx",
                "nova",
                "sage",
                "shimmer",
            )
        ]

    async def resolve_model_target(
        self,
        _explicit: str | None,
        *,
        task_type: str,
        acting_user_email: str | None = None,
        explicit_provider_id: str | None = None,
    ) -> tuple[str, str | None]:
        # Mirror the real LiteLLMProvider contract: returns
        # ``(model_id, provider_id | None)`` — second element is a string id.
        if task_type == "text_to_speech":
            return _explicit or "tts-1", explicit_provider_id or "stub-provider"
        if task_type == "speech_to_text":
            return "whisper-1", "stub-provider"
        raise ValueError(f"unknown task_type {task_type}")

    async def synthesize(self, text: str, *, voice: str, **kwargs: Any) -> TextToSpeechResult:
        self.synthesize_calls.append({"text": text, "voice": voice, **kwargs})
        return TextToSpeechResult(
            audio_bytes=b"fake-audio-bytes",
            content_type="audio/mpeg",
            model="tts-1",
            voice=voice,
            duration_seconds=1.5,
        )

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        filename: str,
        model: str | None = None,
        task_type: str = "speech_to_text",
        prompt: str | None = None,
        language: str | None = None,
        acting_user_email: str | None = None,
    ) -> SpeechToTextResult:
        self.transcribe_calls.append(
            {
                "audio_bytes": audio_bytes,
                "mime_type": mime_type,
                "filename": filename,
                "language": language,
                "acting_user_email": acting_user_email,
            }
        )
        return SpeechToTextResult(
            text="hello world",
            model="whisper-1",
            language="en",
            duration_seconds=1.0,
        )

    async def aclose(self) -> None:
        pass


async def _seed_stub_provider(client: TestClient) -> None:
    """Insert a minimal LLMProvider row that the route can ``session.get`` by id."""
    from cognis.store.models import LLMProvider as LLMProviderRow

    async with client.app.state.session_factory() as session:
        if await session.get(LLMProviderRow, "stub-provider") is not None:
            return
        session.add(
            LLMProviderRow(
                provider_id="stub-provider",
                display_name="Stub",
                location="controller",
                backend="litellm",
                config={"preset": "openai"},
                status="active",
            )
        )
        await session.commit()


def _install_stub_llm(client: TestClient) -> _StubLLMProvider:
    stub = _StubLLMProvider()
    client.app.state.providers.llm = stub  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# TTS route
# ---------------------------------------------------------------------------


def test_tts_resynthesis_survives_old_generation_cleanup(monkeypatch, tmp_path):
    from cognis.core.artifact_maintenance import ArtifactMaintenanceService
    from cognis.core.speech import cache_identity
    from cognis.core.speech_models import SpeechRequest
    from cognis.store.queries import (
        get_artifact_record,
        get_tts_cache_entry,
        mark_artifact_deleted,
    )

    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stub_provider, client)
        client.portal.call(_seed_tts_routing, client)
        _install_stub_llm(client)
        headers = _auth_headers(client.app)
        payload = {"text": "Generation race.", "message_id": "msg_race", "voice": "nova"}
        key = cache_identity(
            "user@example.com", payload["text"], "stub-provider", SpeechRequest(**payload)
        )
        first = client.post("/api/v1/tts/synthesize", headers=headers, json=payload)
        assert first.status_code == 200

        async def tombstone():
            async with client.app.state.session_factory() as db:
                cached = await get_tts_cache_entry(db, message_id=key, voice="nova", model="tts-1")
                old_id = cached.artifact_id
                await mark_artifact_deleted(db, old_id)
                await db.commit()
                return old_id

        old_id = client.portal.call(tombstone)
        second = client.post("/api/v1/tts/synthesize", headers=headers, json=payload)
        assert second.status_code == 200 and not second.json()["cached"]
        service = ArtifactMaintenanceService(
            session_factory=client.app.state.session_factory,
            artifact_store=client.app.state.artifact_store,
        )
        client.portal.call(service.run_once)

        async def verify():
            async with client.app.state.session_factory() as db:
                cached = await get_tts_cache_entry(db, message_id=key, voice="nova", model="tts-1")
                assert cached.artifact_id != old_id
                record = await get_artifact_record(db, cached.artifact_id)
                assert record is not None
                assert await client.app.state.artifact_store.async_exists(
                    record.namespace, record.object_id, record.filename
                )

        client.portal.call(verify)
        third = client.post("/api/v1/tts/synthesize", headers=headers, json=payload)
        assert third.status_code == 200 and third.json()["cached"]


def test_tts_synthesize_cache_miss_then_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stub_provider, client)
        client.portal.call(_seed_tts_routing, client)
        stub = _install_stub_llm(client)
        headers = _auth_headers(client.app)

        # First call — cache miss.
        response = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello there.", "message_id": "msg_123", "voice": "nova"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["voice"] == "nova"
        assert body["model"] == "tts-1"
        assert body["cached"] is False
        assert body["audio_url"].startswith("http://") or body["audio_url"].startswith("/")
        assert len(stub.synthesize_calls) == 1

        # Second call with same key — cache hit; no extra synthesize.
        response2 = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello there.", "message_id": "msg_123", "voice": "nova"},
        )
        assert response2.status_code == 200
        body2 = response2.json()
        assert body2["cached"] is True
        assert len(stub.synthesize_calls) == 1

        catalogue = client.get(
            "/api/v1/tts/voices",
            params={"provider_id": "stub-provider", "model": "gpt-4o-mini-tts"},
            headers=headers,
        )
        assert catalogue.status_code == 200, catalogue.text
        assert catalogue.json()["model"] == "gpt-4o-mini-tts"
        preview = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={
                "text": "Selected provider preview.",
                "provider_id": "stub-provider",
                "model": "gpt-4o-mini-tts",
                "voice": "nova",
            },
        )
        assert preview.status_code == 200, preview.text
        assert stub.synthesize_calls[-1]["provider_id"] == "stub-provider"
        assert stub.synthesize_calls[-1]["model"] == "gpt-4o-mini-tts"


def test_conversation_preflight_uses_authorized_agent_tts_without_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.store.queries import create_agent

    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stub_provider, client)
        stub = _install_stub_llm(client)

        async def seed():
            async with client.app.state.session_factory() as db:
                await create_agent(
                    db,
                    agent_id="narrator",
                    owner_email="user@example.com",
                    name="Narrator",
                    llm_config={"tts_provider_id": "stub-provider", "tts_model": "gpt-4o-mini-tts"},
                    status="active",
                )
                await db.commit()

        async def resolve(explicit, *, task_type, explicit_provider_id=None, **kwargs):
            if task_type == "text_to_speech" and explicit is None:
                raise ValueError("No global TTS route")
            return explicit or "whisper-1", explicit_provider_id or "stub-provider"

        client.portal.call(seed)
        stub.resolve_model_target = AsyncMock(side_effect=resolve)
        stub.discover_voices = AsyncMock(wraps=stub.discover_voices)
        headers = _auth_headers(client.app)
        response = client.get("/api/v1/tts/capabilities?agent_id=narrator", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "tts": True,
            "stt": True,
            "streaming_tts": True,
            "streaming_stt": False,
            "stt_sample_rate": 24000,
            "detail": None,
        }
        stub.discover_voices.assert_not_awaited()
        assert stub.synthesize_calls == []
        assert (
            client.get(
                "/api/v1/tts/capabilities?agent_id=absent-narrator", headers=headers
            ).status_code
            == 404
        )


def test_system_speech_overrides_and_non_retryable_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(lambda: _seed_user(client, email="other@example.com"))
        client.portal.call(_seed_stub_provider, client)
        stub = _install_stub_llm(client)
        headers = _auth_headers(client.app)
        system_id = "system:explore"
        override = {
            "tts_provider_id": "stub-provider",
            "tts_model": "gpt-4o-mini-tts",
            "tts_voice": "nova",
        }
        updated = client.put(
            f"/api/v1/agents/{system_id}", headers=headers, json={"llm_config": override}
        )
        assert updated.status_code == 200, updated.text
        assert "llm_config.tts_model" in updated.json()["editable_fields"]
        capabilities = client.get(f"/api/v1/tts/capabilities?agent_id={system_id}", headers=headers)
        assert capabilities.status_code == 200 and capabilities.json()["tts"] is True
        body = {"text": "Effective system speech.", "agent_id": system_id}
        recording = client.post("/api/v1/tts/synthesize", headers=headers, json=body)
        assert recording.status_code == 200, recording.text
        assert recording.json()["model"] == "gpt-4o-mini-tts"
        assert stub.synthesize_calls[-1]["voice"] == "nova"
        other = client.post(
            "/api/v1/tts/synthesize",
            json=body,
            headers=_auth_headers(client.app, email="other@example.com"),
        )
        assert other.status_code == 200, other.text
        assert other.json()["model"] == "tts-1" and other.json()["voice"] == "alloy"
        cleared = client.put(
            f"/api/v1/agents/{system_id}",
            headers=headers,
            json={"llm_config": dict.fromkeys(override)},
        )
        assert cleared.status_code == 200, cleared.text
        inherited = client.post("/api/v1/tts/synthesize", headers=headers, json=body)
        assert inherited.status_code == 200, inherited.text
        assert inherited.json()["model"] == "tts-1" and inherited.json()["voice"] == "alloy"
        stub.synthesize = AsyncMock(
            side_effect=SpeechGenerationInterrupted("Retry explicitly; it may have been billed.")
        )
        interrupted = client.post(
            "/api/v1/tts/synthesize", headers=headers, json={"text": "Interrupted shared work."}
        )
        assert interrupted.status_code == 409, interrupted.text
        assert interrupted.json()["error"]["code"] == "speech_generation_interrupted"


def test_tts_synthesize_recovers_from_stale_artifact_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: when the ``tts_cache`` row was pruned but the matching
    ``artifacts`` row is still present, a re-synthesize for the same
    deterministic ``artifact_id`` must update the existing row instead of
    crashing with a UniqueViolation on ``artifacts_pkey``.
    """
    from sqlalchemy import delete

    from cognis.store.models import TtsCacheRow

    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stub_provider, client)
        client.portal.call(_seed_tts_routing, client)
        _install_stub_llm(client)
        headers = _auth_headers(client.app)

        first = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert first.status_code == 200, first.text

        # Simulate a TTL prune that wiped the tts_cache row but left the
        # artifacts row in place.
        async def _wipe_cache_only() -> None:
            async with client.app.state.session_factory() as session:
                await session.execute(delete(TtsCacheRow))
                await session.commit()

        client.portal.call(_wipe_cache_only)

        # Second call — must succeed (no IntegrityError on artifacts_pkey).
        second = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["cached"] is False
        # The cache row is freshly inserted; a third call should hit the cache.
        third = client.post(
            "/api/v1/tts/synthesize",
            headers=headers,
            json={"text": "Hello stale.", "message_id": "msg_stale", "voice": "nova"},
        )
        assert third.status_code == 200
        assert third.json()["cached"] is True


def test_tts_synthesize_rejects_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_tts_routing, client)
        _install_stub_llm(client)

        async def _disable() -> None:
            async with client.app.state.session_factory() as session:
                await upsert_setting(session, key="tts.enabled", value=False, category="tts")
                await session.commit()

        client.portal.call(_disable)

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hi", "message_id": None, "voice": "alloy"},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "tts_disabled"


def test_tts_synthesize_handles_string_provider_id_from_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``LLMProvider.resolve_model_target`` returns a provider_id
    *string*, not a row. Earlier the route accessed ``.config`` on the second
    element directly and crashed with ``AttributeError``."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stub_provider, client)
        client.portal.call(_seed_tts_routing, client)
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hi there.", "voice": "alloy"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["voice"] == "alloy"
        # No message_id → no cache row, but the synthesize call must succeed.
        assert body["cached"] is False


def test_tts_synthesize_validates_empty_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_tts_routing, client)
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "   ", "voice": "alloy"},
        )
        assert response.status_code == 400


def test_tts_synthesize_no_routing_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        # Intentionally no routing.

        # Use a real LLM provider whose resolve_model_target raises when
        # nothing is configured.
        class _NoRoutingProvider:
            async def resolve_model_target(self, _explicit: str | None, *, task_type: str):
                raise RuntimeError("no routing")

            async def aclose(self) -> None:
                pass

        client.app.state.providers.llm = _NoRoutingProvider()  # type: ignore[attr-defined]

        response = client.post(
            "/api/v1/tts/synthesize",
            headers=_auth_headers(client.app),
            json={"text": "Hello", "voice": "alloy"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "tts_unconfigured"


# ---------------------------------------------------------------------------
# STT route
# ---------------------------------------------------------------------------


def test_stt_transcribe_multipart_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stt_routing, client)
        stub = _install_stub_llm(client)

        files = {"file": ("voice.webm", io.BytesIO(b"fake-audio"), "audio/webm")}
        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            files=files,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["text"] == "hello world"
        assert body["language"] == "en"
        assert body["model"] == "whisper-1"
        assert len(stub.transcribe_calls) == 1
        assert stub.transcribe_calls[0]["mime_type"].startswith("audio/")
        assert stub.transcribe_calls[0]["acting_user_email"] == "user@example.com"


def test_stt_transcribe_requires_file_or_artifact_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stt_routing, client)
        _install_stub_llm(client)

        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            data={},
        )
        assert response.status_code == 400


def test_stt_transcribe_rejects_empty_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        client.portal.call(_seed_user, client)
        client.portal.call(_seed_stt_routing, client)
        _install_stub_llm(client)

        files = {"file": ("voice.webm", io.BytesIO(b""), "audio/webm")}
        response = client.post(
            "/api/v1/stt/transcribe",
            headers=_auth_headers(client.app),
            files=files,
        )
        assert response.status_code == 400
