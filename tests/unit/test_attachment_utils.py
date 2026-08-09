from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cognis.core.attachment_utils import (
    hydrate_attachment_ref_groups,
    hydrate_attachment_refs,
)


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class _Session:
    def __init__(self, state: SimpleNamespace, rows: list[Any]) -> None:
        self._state = state
        self._rows = rows

    async def __aenter__(self) -> _Session:
        self._state.closed = False
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._state.closed = True

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._rows)


class _SessionFactory:
    def __init__(self, state: SimpleNamespace, rows: list[Any]) -> None:
        self._state = state
        self._rows = rows

    def __call__(self) -> _Session:
        return _Session(self._state, self._rows)


class _ArtifactStore:
    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state
        self.calls = 0

    async def async_get_public_url(self, namespace: str, object_id: str, filename: str) -> str:
        assert self._state.closed is True
        self.calls += 1
        return f"https://files.invalid/{namespace}/{object_id}/{filename}"


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id="art-1",
        namespace="attachments",
        object_id="obj-1",
        filename="report.pdf",
        kind="pdf",
        mime_type="application/pdf",
        size_bytes=42,
        owner_email="user@example.com",
        conversation_id="conv-1",
        session_id="session-1",
        status="attached",
        deleted_at=None,
    )


@pytest.mark.asyncio
async def test_attachment_url_generation_starts_after_database_session_closes() -> None:
    state = SimpleNamespace(closed=False)
    store = _ArtifactStore(state)

    hydrated = await hydrate_attachment_refs(
        _SessionFactory(state, [_row()]),  # type: ignore[arg-type]
        store,
        [{"artifact_id": "art-1"}],
        owner_email="user@example.com",
        conversation_id="conv-1",
        session_id="session-1",
    )

    assert hydrated == [
        {
            "artifact_id": "art-1",
            "kind": "pdf",
            "mime_type": "application/pdf",
            "filename": "obj-1",
            "size_bytes": 42,
            "url": "https://files.invalid/attachments/obj-1/report.pdf",
        }
    ]


@pytest.mark.asyncio
async def test_group_hydration_closes_session_and_reuses_generated_url() -> None:
    state = SimpleNamespace(closed=False)
    store = _ArtifactStore(state)

    hydrated = await hydrate_attachment_ref_groups(
        _SessionFactory(state, [_row()]),  # type: ignore[arg-type]
        store,
        [[{"artifact_id": "art-1"}], [{"artifact_id": "art-1"}]],
        owner_email="user@example.com",
        conversation_id="conv-1",
        session_id="session-1",
    )

    assert len(hydrated) == 2
    assert hydrated[0] == hydrated[1]
    assert store.calls == 1
