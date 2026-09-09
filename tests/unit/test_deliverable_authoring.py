from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.deliverable_authoring import (
    MAX_RICH_PAYLOAD_BYTES,
    DeliverableAuthoringError,
    resolve_deliverable_authoring,
    rich_action_for_presentation,
)
from cognis.models.artifact import ArtifactStatus
from cognis.models.deliverable import DASHBOARD_SKELETON

_ARTIFACT_ID = "art_0123456789abcdef0123456789abcdef"
_PAYLOAD = {
    "title": "Status report",
    "blocks": [{"type": "markdown", "content": "## Ready\nAll checks passed."}],
    "outputs": {"status": "ready"},
    "metadata": {},
}


class _Store:
    def __init__(self, content: bytes, mime_type: str = "application/json") -> None:
        self.content = content
        self.mime_type = mime_type

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        return self.content, self.mime_type


@asynccontextmanager
async def _session_factory() -> Any:
    yield object()


def _record(**updates: object) -> SimpleNamespace:
    values = {
        "artifact_id": _ARTIFACT_ID,
        "namespace": "attachments",
        "object_id": _ARTIFACT_ID,
        "filename": "payload.json",
        "owner_email": "owner@example.com",
        "conversation_id": None,
        "purpose": "artifact_publish",
        "mime_type": "application/json",
        "size_bytes": len(json.dumps(_PAYLOAD).encode()),
        "content_hash": hashlib.sha256(json.dumps(_PAYLOAD).encode()).hexdigest(),
        "status": ArtifactStatus.ATTACHED,
        "deleted_at": None,
        "expires_at": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.fixture
def artifact_access(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = {"record": _record(), "authorized": True}

    async def get_record(session: object, artifact_id: str) -> object:
        return state["record"]

    async def authorize(*args: object, **kwargs: object) -> bool:
        return bool(state["authorized"])

    monkeypatch.setattr("cognis.core.deliverable_authoring.get_artifact_record", get_record)
    monkeypatch.setattr(
        "cognis.core.deliverable_authoring.artifact_authorized_for_conversation", authorize
    )
    return state


async def _resolve_artifact(store: _Store, **kwargs: object) -> Any:
    return await resolve_deliverable_authoring(
        {
            "action": "rich",
            "payload_artifact": {"artifact_id": _ARTIFACT_ID},
        },
        session_factory=_session_factory,
        artifact_store=store,
        owner_email="owner@example.com",
        conversation_id="conv_1",
        agent_id="agent_1",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_text_authoring_needs_only_content() -> None:
    resolved = await resolve_deliverable_authoring(
        {"content": "# Result"},
        session_factory=None,
        artifact_store=None,
        owner_email="owner@example.com",
        conversation_id="conv_1",
        agent_id="agent_1",
    )

    assert resolved.content == "# Result"
    assert resolved.format == "markdown"
    assert resolved.rich is None


@pytest.mark.asyncio
async def test_inline_and_artifact_rich_resolve_identically(
    artifact_access: dict[str, Any],
) -> None:
    raw = json.dumps(_PAYLOAD).encode()
    inline = await resolve_deliverable_authoring(
        {"action": "rich", "payload": deepcopy(_PAYLOAD)},
        session_factory=_session_factory,
        artifact_store=_Store(raw),
        owner_email="owner@example.com",
        conversation_id="conv_1",
        agent_id="agent_1",
    )
    artifact = await _resolve_artifact(_Store(raw))

    assert artifact.content == inline.content
    assert artifact.title == inline.title
    assert artifact.outputs == inline.outputs
    assert artifact.rich == inline.rich
    assert "presentation" not in artifact.rich["metadata"]


@pytest.mark.asyncio
async def test_dashboard_action_injects_and_normalizes_presentation() -> None:
    payload = deepcopy(DASHBOARD_SKELETON)
    payload["metadata"].pop("presentation")

    resolved = await resolve_deliverable_authoring(
        {"action": "rich:dashboard", "payload": payload},
        session_factory=None,
        artifact_store=None,
        owner_email="owner@example.com",
        conversation_id="conv_1",
        agent_id="agent_1",
    )

    assert resolved.format == "rich"
    assert resolved.title == DASHBOARD_SKELETON["title"]
    assert resolved.rich is not None
    assert resolved.rich["metadata"]["presentation"] == "dashboard"
    assert resolved.rich["metadata"]["canvas"] == "wide"
    assert resolved.rich["metadata"]["density"] == "compact"
    assert rich_action_for_presentation("dashboard") == "rich:dashboard"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\xff", "invalid_utf8"),
        (b"{", "invalid_json"),
        (b'{"title":"A","title":"B","blocks":[]}', "invalid_json"),
        (b'{"title":"A","blocks":[],"value":NaN}', "invalid_json"),
        (b"[]", "invalid_payload"),
    ],
)
async def test_artifact_json_failures_are_closed(
    artifact_access: dict[str, Any], content: bytes, code: str
) -> None:
    artifact_access["record"] = _record(size_bytes=len(content))
    artifact_access["record"].content_hash = hashlib.sha256(content).hexdigest()
    with pytest.raises(DeliverableAuthoringError, match="Rich payload") as exc_info:
        await _resolve_artifact(_Store(content))

    assert exc_info.value.code == code
    assert exc_info.value.path == "$.payload_artifact.artifact_id"


@pytest.mark.asyncio
async def test_artifact_access_and_lifecycle_fail_closed(
    artifact_access: dict[str, Any],
) -> None:
    artifact_access["authorized"] = False
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(json.dumps(_PAYLOAD).encode()))
    assert exc_info.value.code == "artifact_unavailable"

    artifact_access["authorized"] = True
    artifact_access["record"] = _record(status=ArtifactStatus.DELETED)
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(json.dumps(_PAYLOAD).encode()))
    assert exc_info.value.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_artifact_mime_size_and_digest_fail_closed(
    artifact_access: dict[str, Any],
) -> None:
    raw = json.dumps(_PAYLOAD).encode()
    artifact_access["record"] = _record(mime_type="text/plain")
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(raw))
    assert exc_info.value.code == "invalid_artifact_mime"

    artifact_access["record"] = _record(size_bytes=MAX_RICH_PAYLOAD_BYTES + 1)
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(raw))
    assert exc_info.value.code == "payload_too_large"

    artifact_access["record"] = _record()
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(raw), expected_digest="0" * 64)
    assert exc_info.value.code == "artifact_digest_changed"

    artifact_access["record"] = _record(content_hash=None)
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await _resolve_artifact(_Store(raw))
    assert exc_info.value.code == "artifact_integrity_error"


@pytest.mark.asyncio
async def test_rich_rejects_removed_shapes_and_authored_presentation() -> None:
    for arguments in (
        {"action": "rich", "payload": deepcopy(_PAYLOAD), "content": "legacy"},
        {"action": "rich", "payload": deepcopy(_PAYLOAD), "format": "rich"},
        {"action": "rich", "payload": deepcopy(_PAYLOAD), "rich": {}},
        {
            "action": "rich",
            "payload": deepcopy(_PAYLOAD),
            "payload_artifact": {"artifact_id": _ARTIFACT_ID},
        },
    ):
        with pytest.raises(DeliverableAuthoringError):
            await resolve_deliverable_authoring(
                arguments,
                session_factory=None,
                artifact_store=None,
                owner_email="owner@example.com",
                conversation_id="conv_1",
                agent_id="agent_1",
            )

    payload = deepcopy(_PAYLOAD)
    payload["metadata"]["presentation"] = "pulse"
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await resolve_deliverable_authoring(
            {"action": "rich", "payload": payload},
            session_factory=None,
            artifact_store=None,
            owner_email="owner@example.com",
            conversation_id="conv_1",
            agent_id="agent_1",
        )
    assert exc_info.value.code == "author_selected_presentation"

    payload = deepcopy(_PAYLOAD)
    payload["metadata"] = []
    with pytest.raises(DeliverableAuthoringError) as exc_info:
        await resolve_deliverable_authoring(
            {"action": "rich", "payload": payload},
            session_factory=None,
            artifact_store=None,
            owner_email="owner@example.com",
            conversation_id="conv_1",
            agent_id="agent_1",
        )
    assert exc_info.value.code == "invalid_metadata"
