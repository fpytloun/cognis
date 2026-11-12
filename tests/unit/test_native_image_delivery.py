"""Exercise image reads, canonical replay, authorization and actual Responses input."""

from __future__ import annotations

import base64
import io
import json
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from cognis.core.artifact_sharing import grant_referenced_inputs
from cognis.core.context import events_to_messages
from cognis.models.config import ModelInfo
from cognis.providers.llm import codex
from cognis.providers.llm import litellm as litellm_provider
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.store.models import LLMProvider
from cognis.store.queries import create_artifact_record, create_session
from cognis.tools.builtin.artifact_tools import handle_artifact_tool

OWNER = "owner@example.com"
MODEL = "gpt-6.1-sol"


@pytest.fixture
async def image_runtime(
    task_continuation_db: Any, monkeypatch: pytest.MonkeyPatch, usage_capture: Any
) -> AsyncIterator[SimpleNamespace]:
    factory = task_continuation_db
    store = factory.artifact_store
    async with factory() as db:
        db.add(
            LLMProvider(
                provider_id="codex",
                display_name="Codex",
                location="controller",
                backend="litellm",
                config={"preset": "chatgpt", "default_model": MODEL},
                status="active",
            )
        )
        parent = await create_session(db, "conv-owner", OWNER, "agent-owner")
        child = await create_session(
            db, "conv-owner", OWNER, "system:explore", parent_session_id=parent.session_id
        )
        sibling = await create_session(
            db, "conv-owner", OWNER, "system:explore", parent_session_id=parent.session_id
        )
        refs = []
        content = io.BytesIO()
        Image.new("RGB", (4, 3), (255, 0, 0)).save(content, format="PNG")
        for prefix in ("att", "art"):
            ref = f"{prefix}_{uuid.uuid4().hex}"
            await store.async_save("attachments", ref, "photo.png", content.getvalue(), "image/png")
            await create_artifact_record(
                db,
                artifact_id=ref,
                namespace="attachments",
                object_id=ref,
                filename="photo.png",
                owner_email=OWNER,
                conversation_id="conv-owner",
                session_id=parent.session_id,
                purpose="chat_input" if prefix == "att" else "tool_output",
                kind="image",
                mime_type="image/png",
                size_bytes=len(content.getvalue()),
                status="attached",
            )
            await grant_referenced_inputs(
                db,
                owner_email=OWNER,
                source_agent_id="agent-owner",
                source_session_id=parent.session_id,
                source_conversation_id="conv-owner",
                target_session_id=child.session_id,
                text=ref,
            )
            refs.append(
                {
                    "artifact_id": ref,
                    "filename": "photo.png",
                    "kind": "image",
                    "mime_type": "image/png",
                    "size_bytes": len(content.getvalue()),
                    "url": await store.async_get_public_url("attachments", ref, "photo.png"),
                }
            )
        await db.commit()

    captured = []

    async def auth(self: Any, row: Any) -> codex.CodexAuth:
        return codex.CodexAuth(access_token="test-token", account_id="test-account")

    async def completed() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "content": []},
        }
        yield {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "done"}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def responses(self: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        captured.append(deepcopy(kwargs))
        return completed()

    monkeypatch.setattr(LiteLLMProvider, "_chatgpt_codex_auth", auth)
    monkeypatch.setattr(litellm_provider.DirectCodexTransport, "responses", responses)
    provider = LiteLLMProvider(factory, artifact_store=store, usage_capture=usage_capture)
    try:
        yield SimpleNamespace(
            factory=factory,
            store=store,
            provider=provider,
            captured=captured,
            refs=refs,
            parent=parent,
            child=child,
            sibling=sibling,
        )
    finally:
        await provider.aclose()


def _initial_events(ref: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "seq": 1,
            "type": "user_message",
            "data": {"content": "Inspect this PNG.", "attachments": [ref]},
        }
    ]


async def _read_events(runtime: SimpleNamespace, ref: dict[str, Any]) -> list[dict[str, Any]]:
    result = await handle_artifact_tool(
        "artifact_read",
        {"artifact_id": ref["artifact_id"], "prompt": "Describe the pixels."},
        llm=runtime.provider,
        artifact_store=runtime.store,
        session_factory=runtime.factory,
        user_email=OWNER,
        current_model=MODEL,
        current_provider_id="codex",
        runtime_metadata={
            "conversation_id": "conv-owner",
            "agent_id": "system:explore",
            "session_id": runtime.child.session_id,
        },
    )
    assert not result.is_error
    assert result.metadata["native_attachment"]
    assert "not confirmation" in result.output
    call = f"call_{uuid.uuid4().hex}"
    return [
        {"seq": 1, "type": "user_message", "data": {"content": "Read the image."}},
        {
            "seq": 2,
            "type": "tool_call",
            "data": {
                "call_id": call,
                "name": "artifact_read",
                "arguments": {"artifact_id": ref["artifact_id"]},
            },
        },
        {
            "seq": 3,
            "type": "tool_result",
            "data": {
                "call_id": call,
                "name": "artifact_read",
                "result": result.output,
                "tool_attachment_context_version": 1,
                "tool_attachment_context_refs": result.attachments,
            },
        },
    ]


async def _request(
    runtime: SimpleNamespace,
    messages: list[dict[str, Any]],
    *,
    streaming: bool,
    session: str | None = None,
    owner: str = OWNER,
) -> dict[str, Any]:
    kwargs = {
        "model": MODEL,
        "provider_id": "codex",
        "acting_user_email": owner,
        "cognis_agent_id": "system:explore",
        "cognis_conversation_id": "conv-owner",
        "cognis_session_id": session or runtime.child.session_id,
        "cognis_llm_request_id": "llmr_image_delivery",
    }
    if streaming:
        async for _ in runtime.provider.stream_generate(messages, **kwargs):
            pass
    else:
        await runtime.provider.generate(messages, **kwargs)
    return runtime.captured[-1]


def _wire_images(request: dict[str, Any]) -> list[str]:
    return [
        part["image_url"]
        for item in request["input"]
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]


def _assert_red_png(request: dict[str, Any]) -> None:
    images = _wire_images(request)
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(images[0].split(",", 1)[1]))) as image:
        assert image.size == (4, 3)
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert "cognis_artifact" not in json.dumps(request["input"])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("entry", ["initial", "user_read", "saved_read"])
async def test_authorized_image_is_serialized_after_initial_or_reread(
    image_runtime: SimpleNamespace, streaming: bool, entry: str
) -> None:
    runtime = image_runtime
    ref = runtime.refs[1 if entry == "saved_read" else 0]
    events = _initial_events(ref) if entry == "initial" else await _read_events(runtime, ref)
    for index in range(2):
        session_id = runtime.child.session_id
        if index:
            async with runtime.factory() as db:
                rotated = await create_session(
                    db,
                    "conv-owner",
                    OWNER,
                    "system:explore",
                    previous_session_id=session_id,
                    parent_session_id=runtime.parent.session_id,
                )
                await db.commit()
                session_id = rotated.session_id
        # Rebuild from canonical references, never from a previous data URL.
        messages = events_to_messages(
            deepcopy(events), model_info=ModelInfo(model_id=MODEL, supports_vision=True)
        )
        _assert_red_png(await _request(runtime, messages, streaming=streaming, session=session_id))


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["wrong_session", "wrong_user", "missing", "unreadable"])
async def test_undeliverable_images_never_reach_wire_or_leak_diagnostics(
    image_runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    streaming: bool,
    failure: str,
) -> None:
    runtime = image_runtime
    ref = deepcopy(runtime.refs[0])
    session, owner = runtime.child.session_id, OWNER
    if failure == "wrong_session":
        session = runtime.sibling.session_id
    elif failure == "wrong_user":
        owner = "other@example.com"
    elif failure == "missing":
        ref["artifact_id"] = f"att_{uuid.uuid4().hex}"
    else:

        async def unreadable(*args: Any, **kwargs: Any) -> None:
            raise OSError("private-storage-url?signature=do-not-log")

        monkeypatch.setattr(runtime.store, "async_load", unreadable)
    messages = events_to_messages(
        _initial_events(ref), model_info=ModelInfo(model_id=MODEL, supports_vision=True)
    )
    request = await _request(runtime, messages, streaming=streaming, session=session, owner=owner)
    assert not _wire_images(request)
    assert "was not sent to the model because it is unavailable" in json.dumps(request["input"])
    logs = [
        r.extra_data
        for r in caplog.records
        if r.getMessage() == "Native image materialization rejected"
    ]
    expected = {
        "wrong_session": "unauthorized",
        "wrong_user": "unauthorized",
        "missing": "record_missing",
        "unreadable": "storage_load_failed",
    }
    assert logs == [
        {
            "artifact_id": ref["artifact_id"],
            "conversation_id": "conv-owner",
            "session_id": session,
            "llm_request_id": "llmr_image_delivery",
            "category": expected[failure],
            "exception_type": "OSError" if failure == "unreadable" else None,
        }
    ]
    assert "private-storage-url" not in caplog.text
    assert "do-not-log" not in json.dumps(logs)
    assert ref["url"] not in json.dumps(logs)
