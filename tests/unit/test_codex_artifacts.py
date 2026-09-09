from __future__ import annotations

import copy
import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from cognis.models.artifact import ArtifactKind, ArtifactStatus
from cognis.providers.llm import codex_artifacts
from cognis.providers.llm.codex_artifacts import (
    CodexArtifactError,
    CodexImageNormalizationCache,
    materialize_codex_artifact_images,
    strip_cognis_artifact_metadata,
)


def _png(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="PNG")
    return output.getvalue()


def _messages(artifact_id: str = "img_123") -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this image."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://cognis.invalid/signed",
                        "cognis_artifact": {
                            "artifact_id": artifact_id,
                            "filename": "image.png",
                            "mime_type": "image/png",
                            "size_bytes": 64,
                        },
                    },
                },
            ],
        }
    ]


class _Store:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.loads = 0

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        self.loads += 1
        return self.content, "image/png"


@asynccontextmanager
async def _session_factory() -> Any:
    yield object()


def _record(**updates: Any) -> SimpleNamespace:
    values = {
        "artifact_id": "img_123",
        "namespace": "conversation",
        "object_id": "conv_123",
        "filename": "image.png",
        "owner_email": "owner@example.com",
        "conversation_id": "conv_123",
        "kind": ArtifactKind.IMAGE,
        "status": ArtifactStatus.ATTACHED,
        "size_bytes": 64,
        "deleted_at": None,
        "expires_at": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_materialize_codex_artifact_images_uses_data_url_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _messages()
    original = copy.deepcopy(messages)
    store = _Store(_png())

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record()]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )

    result = await materialize_codex_artifact_images(
        messages,
        session_factory=_session_factory,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    image_url = result[0]["content"][1]["image_url"]
    assert image_url["url"].startswith("data:image/png;base64,")
    assert "cognis_artifact" not in image_url
    assert "cognis.invalid" not in str(result)
    assert messages == original
    assert store.loads == 1


@pytest.mark.asyncio
async def test_materialization_reauthorizes_before_using_normalization_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _png(size=(4096, 1024))
    store = _Store(content)
    cache = CodexImageNormalizationCache()
    authorization_calls = 0
    normalization_calls = 0
    threaded_functions: list[str] = []
    original_normalize = codex_artifacts._normalize_image
    original_to_thread = codex_artifacts.asyncio.to_thread

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record()]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    def _normalize(content: bytes, *, artifact_id: str) -> tuple[bytes, str]:
        nonlocal normalization_calls
        normalization_calls += 1
        return original_normalize(content, artifact_id=artifact_id)

    async def _to_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        threaded_functions.append(function.__name__)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )
    monkeypatch.setattr(codex_artifacts, "_normalize_image", _normalize)
    monkeypatch.setattr(codex_artifacts.asyncio, "to_thread", _to_thread)

    for _ in range(2):
        await materialize_codex_artifact_images(
            _messages(),
            session_factory=_session_factory,
            artifact_store=store,
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
            normalization_cache=cache,
        )

    assert authorization_calls == 2
    assert store.loads == 2
    assert normalization_calls == 1
    assert threaded_functions.count("_normalize_data_url") == 1


@pytest.mark.asyncio
async def test_normalization_cache_does_not_bypass_later_authorization_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_png())
    cache = CodexImageNormalizationCache()
    authorization_calls = 0

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record()]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return authorization_calls == 1

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )

    await materialize_codex_artifact_images(
        _messages(),
        session_factory=_session_factory,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
        normalization_cache=cache,
    )
    with pytest.raises(CodexArtifactError, match="unavailable"):
        await materialize_codex_artifact_images(
            _messages(),
            session_factory=_session_factory,
            artifact_store=store,
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
            normalization_cache=cache,
        )

    assert store.loads == 1


@pytest.mark.asyncio
async def test_normalization_cache_enforces_byte_bound_and_lru_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CodexImageNormalizationCache(max_bytes=14)
    normalization_calls = 0

    def _data_url(content: bytes, *, artifact_id: str) -> str:
        nonlocal normalization_calls
        normalization_calls += 1
        return f"data:,{content.decode()}"

    monkeypatch.setattr(codex_artifacts, "_normalize_data_url", _data_url)

    assert await cache.data_url(b"a", artifact_id="img_a") == "data:,a"
    assert await cache.data_url(b"b", artifact_id="img_b") == "data:,b"
    assert await cache.data_url(b"a", artifact_id="img_a") == "data:,a"
    assert await cache.data_url(b"c", artifact_id="img_c") == "data:,c"
    assert await cache.data_url(b"b", artifact_id="img_b") == "data:,b"

    assert normalization_calls == 4
    assert cache._size_bytes <= 14

    oversized = CodexImageNormalizationCache(max_bytes=1)
    assert await oversized.data_url(b"d", artifact_id="img_d") == "data:,d"
    assert await oversized.data_url(b"d", artifact_id="img_d") == "data:,d"
    assert normalization_calls == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorized", "record_updates", "content"),
    [
        (False, {}, _png()),
        (True, {"status": ArtifactStatus.DELETED}, _png()),
        (True, {}, b"not-an-image"),
    ],
)
async def test_materialize_codex_artifact_images_rejects_unavailable_or_invalid_images(
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
    record_updates: dict[str, Any],
    content: bytes,
) -> None:
    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record(**record_updates)]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        return authorized

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )

    with pytest.raises(CodexArtifactError):
        await materialize_codex_artifact_images(
            _messages(),
            session_factory=_session_factory,
            artifact_store=_Store(content),
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
        )


@pytest.mark.asyncio
async def test_materialize_codex_artifact_images_resizes_large_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record()]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )
    result = await materialize_codex_artifact_images(
        _messages(),
        session_factory=_session_factory,
        artifact_store=_Store(_png(size=(4096, 1024))),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )
    encoded = result[0]["content"][1]["image_url"]["url"].split(",", 1)[1]
    import base64

    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (2048, 512)


@pytest.mark.asyncio
async def test_materialize_codex_artifact_images_counts_duplicate_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _png()
    single_data_url_size = len("data:image/png;base64,") + 4 * ((len(content) + 2) // 3)
    messages = _messages()
    messages[0]["content"].append(copy.deepcopy(messages[0]["content"][1]))

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        assert artifact_ids == ["img_123"]
        return [_record()]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )
    monkeypatch.setattr(
        codex_artifacts,
        "MAX_CODEX_ENCODED_IMAGE_BYTES",
        single_data_url_size * 2 - 1,
    )

    with pytest.raises(CodexArtifactError, match="Encoded image payload exceeds"):
        await materialize_codex_artifact_images(
            messages,
            session_factory=_session_factory,
            artifact_store=_Store(content),
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
        )


@pytest.mark.asyncio
async def test_normalization_failure_identifies_only_the_invalid_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _messages("img_invalid")

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record(artifact_id="img_invalid")]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("cognis.providers.llm.codex_artifacts.get_artifact_records", _records)
    monkeypatch.setattr(
        "cognis.providers.llm.codex_artifacts.artifact_authorized_for_conversation",
        _authorized,
    )

    with pytest.raises(CodexArtifactError) as error:
        await materialize_codex_artifact_images(
            messages,
            session_factory=_session_factory,
            artifact_store=_Store(b"not-an-image"),
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
        )

    assert error.value.artifact_ids == ["img_invalid"]
    assert error.value.to_payload()["artifact_ids"] == ["img_invalid"]


def test_strip_cognis_artifact_metadata_preserves_url_and_source() -> None:
    messages = _messages()
    projected = strip_cognis_artifact_metadata(messages)

    assert projected[0]["content"][1]["image_url"] == {"url": "https://cognis.invalid/signed"}
    assert "cognis_artifact" in messages[0]["content"][1]["image_url"]
