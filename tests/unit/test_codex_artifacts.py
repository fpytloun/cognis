from __future__ import annotations

import asyncio
import base64
import copy
import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, JpegImagePlugin

from cognis.core.attachment_compat import DEFAULT_NATIVE_IMAGE_MIME_TYPES
from cognis.models.artifact import ArtifactKind, ArtifactStatus
from cognis.providers.llm import codex_artifacts
from cognis.providers.llm.codex_artifacts import (
    CodexImageNormalizationCache,
    materialize_codex_artifact_images,
    strip_cognis_artifact_metadata,
)


def _png(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="PNG")
    return output.getvalue()


def _jpeg(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="JPEG")
    return output.getvalue()


def _webp(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="WEBP")
    return output.getvalue()


def _mpo(*, size: tuple[int, int] = (4, 3), orientation: int = 1) -> bytes:
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    primary = Image.new("RGB", size, "red")
    auxiliary = Image.new("RGB", (4, 3), "blue")
    primary.save(output, format="MPO", save_all=True, append_images=[auxiliary], exif=exif)
    return output.getvalue()


def _gif(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("P", size, 1).save(output, format="GIF")
    return output.getvalue()


def _animated_gif(*, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    frames = [Image.new("P", size, index) for index in (1, 2, 3)]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=40)
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


def _image_parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [part for part in messages[0]["content"] if part.get("type") == "image_url"]


def _text_parts(messages: list[dict[str, Any]]) -> list[str]:
    return [
        str(part.get("text") or "") for part in messages[0]["content"] if part.get("type") == "text"
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

    authorized_result = await materialize_codex_artifact_images(
        _messages(),
        session_factory=_session_factory,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
        normalization_cache=cache,
    )
    assert _image_parts(authorized_result)

    denied_result = await materialize_codex_artifact_images(
        _messages(),
        session_factory=_session_factory,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
        normalization_cache=cache,
    )

    # The cached normalization must not resurrect an image the second
    # authorization check denied.
    assert not _image_parts(denied_result)
    assert any("img_123" in text for text in _text_parts(denied_result))
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
    ("authorized", "record_updates", "content", "category"),
    [
        (False, {}, _png(), "unauthorized"),
        (True, {"status": ArtifactStatus.DELETED}, _png(), "record_unavailable"),
        (True, {}, b"not-an-image", "normalization_failed"),
    ],
)
async def test_materialize_codex_artifact_images_drops_unavailable_or_invalid_images(
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
    record_updates: dict[str, Any],
    content: bytes,
    category: str,
    caplog: pytest.LogCaptureFixture,
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

    # An image Cognis must not or cannot send is replaced by a notice. Failing
    # the whole request instead would make the identical retry fail again.
    result = await materialize_codex_artifact_images(
        _messages(),
        session_factory=_session_factory,
        artifact_store=_Store(content),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    assert not _image_parts(result)
    assert any("img_123" in text for text in _text_parts(result))
    rejection = next(
        record.extra_data
        for record in caplog.records
        if record.getMessage() == "Native image materialization rejected"
    )
    assert rejection["category"] == category
    assert "cognis.invalid" not in str(rejection)
    assert "not-an-image" not in str(rejection)


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

    result = await materialize_codex_artifact_images(
        messages,
        session_factory=_session_factory,
        artifact_store=_Store(content),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    # Duplicate references are counted once per occurrence, so the payload
    # budget is reached and the image is degraded rather than sent twice.
    assert not _image_parts(result)
    assert any("payload limit" in text for text in _text_parts(result))


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

    result = await materialize_codex_artifact_images(
        messages,
        session_factory=_session_factory,
        artifact_store=_Store(b"not-an-image"),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    assert not _image_parts(result)
    assert any("img_invalid" in text for text in _text_parts(result))


@pytest.mark.asyncio
async def test_materialize_codex_artifact_images_converts_unsupported_still_format(
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
        artifact_store=_Store(_gif()),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    images = _image_parts(result)
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_materialize_codex_artifact_images_sends_first_frame_of_animation(
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
        artifact_store=_Store(_animated_gif()),
        owner_email="owner@example.com",
        conversation_id="conv_123",
        agent_id="agent_123",
    )

    images = _image_parts(result)
    assert len(images) == 1
    encoded = images[0]["image_url"]["url"].split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.format == "PNG"
        assert not getattr(image, "is_animated", False)


def test_every_declared_native_image_type_is_deliverable() -> None:
    # Guard against the capability mismatch that made a declared format a
    # poison pill: anything context assembly may project natively must be
    # convertible by this transport.
    samples = {
        "image/png": _png(),
        "image/jpeg": _jpeg(),
        "image/webp": _webp(),
        "image/gif": _gif(),
    }
    assert set(samples) == set(DEFAULT_NATIVE_IMAGE_MIME_TYPES)
    for mime_type, content in samples.items():
        encoded, resolved_mime = codex_artifacts._normalize_image(
            content, artifact_id=f"img_{mime_type.replace('/', '_')}"
        )
        assert encoded
        assert resolved_mime in codex_artifacts._FORMAT_MIME_TYPES.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_factory", [_jpeg, _mpo], ids=["jpeg", "mpo"])
async def test_camera_jpeg_is_resized_once_across_concurrent_materializations(
    monkeypatch: pytest.MonkeyPatch,
    image_factory: Any,
) -> None:
    content = image_factory(size=(5712, 4284))
    store = _Store(content)
    cache = CodexImageNormalizationCache()
    normalization_calls = 0
    authorization_calls = 0
    original_normalize = codex_artifacts._normalize_image

    async def _records(session: object, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [_record(filename="camera.jpeg", size_bytes=len(content))]

    async def _authorized(session: object, **kwargs: Any) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    def _normalize(content: bytes, *, artifact_id: str) -> tuple[bytes, str]:
        nonlocal normalization_calls
        normalization_calls += 1
        return original_normalize(content, artifact_id=artifact_id)

    monkeypatch.setattr(codex_artifacts, "get_artifact_records", _records)
    monkeypatch.setattr(codex_artifacts, "artifact_authorized_for_conversation", _authorized)
    monkeypatch.setattr(codex_artifacts, "_normalize_image", _normalize)
    messages = _messages()
    original = copy.deepcopy(messages)

    async def _materialize() -> list[dict[str, Any]]:
        return await materialize_codex_artifact_images(
            messages,
            session_factory=_session_factory,
            artifact_store=store,
            owner_email="owner@example.com",
            conversation_id="conv_123",
            agent_id="agent_123",
            normalization_cache=cache,
        )

    results = await asyncio.gather(_materialize(), _materialize())
    results.append(await _materialize())
    for result in results:
        url = _image_parts(result)[0]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
            assert image.size == (2048, 1536)
            assert image.format == "JPEG"
            assert getattr(image, "n_frames", 1) == 1
            assert image.getpixel((100, 100))[0] > 240
            image.load()
    assert normalization_calls == 1
    assert authorization_calls == 3
    assert messages == original
    assert store.content == content


@pytest.mark.parametrize("image_factory", [_jpeg, _mpo], ids=["jpeg", "mpo"])
def test_jpeg_draft_must_reduce_actual_decode_size_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    image_factory: Any,
) -> None:
    content = image_factory(size=(5712, 4284))
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", lambda *args: None)

    def _unexpected_load(*args: Any, **kwargs: Any) -> None:
        pytest.fail("The full-resolution JPEG must not be decoded")

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", _unexpected_load)
    with pytest.raises(codex_artifacts.CodexArtifactError, match="decode pixel budget"):
        codex_artifacts._normalize_image(content, artifact_id="img_camera")


@pytest.mark.parametrize("format", ["JPEG", "PNG"])
def test_normalization_rejects_source_over_its_budget(
    monkeypatch: pytest.MonkeyPatch, format: str
) -> None:
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format=format)
    monkeypatch.setattr(codex_artifacts, "MAX_CODEX_JPEG_SOURCE_PIXELS", 1000)
    monkeypatch.setattr(codex_artifacts, "MAX_CODEX_IMAGE_PIXELS", 1000)
    with pytest.raises(codex_artifacts.CodexArtifactError, match="source pixel budget"):
        codex_artifacts._normalize_image(output.getvalue(), artifact_id="img_over_budget")


@pytest.mark.parametrize("size", [(5712, 4284), (400, 300)])
@pytest.mark.parametrize("format", ["JPEG", "MPO"])
def test_normalization_applies_camera_orientation(size: tuple[int, int], format: str) -> None:
    if format == "MPO":
        content = _mpo(size=size, orientation=6)
    else:
        output = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", size, "red").save(output, format="JPEG", exif=exif)
        content = output.getvalue()
    normalized, mime = codex_artifacts._normalize_image(content, artifact_id="img_portrait")
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == ((1536, 2048) if size[0] > 2048 else (300, 400))
        assert image.getexif().get(274, 1) == 1


def test_mpo_source_budget_is_checked_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _mpo(size=(32, 32))
    monkeypatch.setattr(codex_artifacts, "MAX_CODEX_JPEG_SOURCE_PIXELS", 1000)
    with pytest.raises(codex_artifacts.CodexArtifactError, match="source pixel budget"):
        codex_artifacts._normalize_image(content, artifact_id="img_mpo")


def test_strip_cognis_artifact_metadata_preserves_url_and_source() -> None:
    messages = _messages()
    projected = strip_cognis_artifact_metadata(messages)

    assert projected[0]["content"][1]["image_url"] == {"url": "https://cognis.invalid/signed"}
    assert "cognis_artifact" in messages[0]["content"][1]["image_url"]
