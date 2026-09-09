from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from cognis.api.chat_v2.schemas import FileDiffRef, TimelineScope, ToolCallTimelineItem
from cognis.api.chat_v2.work_graph import AuthorizedWorkGraph
from cognis.api.common import AuthenticatedUser
from cognis.api.routes.work import (
    _cursor,
    _decode_cursor,
    _project_exact_file_diff,
    work_file_history,
)


def _call(item_id: str, paths: list[str]) -> ToolCallTimelineItem:
    return ToolCallTimelineItem(
        id=item_id,
        sort_key=item_id,
        source_refs=[],
        call_id=f"call-{item_id}",
        tool_name="write",
        arguments={},
        file_diffs=[FileDiffRef(path=path, diff=f"+{index}") for index, path in enumerate(paths)],
        status="complete",
    )


def test_file_history_resolves_exact_source_item_and_file_ordinal() -> None:
    scope = TimelineScope(key="session:session-1", kind="session", session_id="session-1")
    selected = _project_exact_file_diff(
        items=[_call("first", ["same.py"]), _call("second", ["same.py", "same.py"])],
        source_item_id="second",
        file_ordinal=1,
        fact=SimpleNamespace(
            path="same.py",
            path_id="root:same.py",
            path_generation_id="generation-1",
            old_path=None,
            additions=1,
            deletions=0,
            status="modified",
            binary=False,
            generated=False,
            truncated=False,
        ),
        scope=scope,
        projection_version="work-v7",
    )
    assert selected is not None
    assert selected.path.endswith("same.py")
    assert selected.diff == "+1"
    assert selected.source_item_id == "second"
    assert selected.path_generation_id == "generation-1"


def test_file_history_cursor_preserves_total_member_order_and_rejects_tampering() -> None:
    payload = {
        "identity": ["scope", "epoch", "work-v6", "work-files-v3", "generation"],
        "before": [3, "session-b", 9, 2, 1, "row-id"],
    }
    encoded = _cursor(payload, "secret")
    assert _decode_cursor(encoded, "secret") == payload
    with pytest.raises(HTTPException) as exc_info:
        _decode_cursor(encoded, "other-secret")
    assert exc_info.value.status_code == 409


class _EmptyRows:
    def all(self) -> list[Any]:
        return []


class _RouteDb:
    def __init__(self, ordinal: int) -> None:
        self.ordinal = ordinal

    async def scalar(self, _statement: Any) -> str | None:
        return "root" if self.ordinal == 1 else None

    async def execute(self, _statement: Any) -> _EmptyRows:
        return _EmptyRows()


class _BoundedSessionFactory:
    def __init__(self) -> None:
        self.active = 0
        self.created = 0

    @asynccontextmanager
    async def __call__(self) -> Any:
        assert self.active == 0
        self.active += 1
        self.created += 1
        try:
            yield _RouteDb(self.created)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_file_history_releases_preflight_session_before_graph_resolution() -> None:
    factory = _BoundedSessionFactory()

    class _Resolver:
        async def resolve(self, **_kwargs: Any) -> AuthorizedWorkGraph:
            assert factory.active == 0
            return AuthorizedWorkGraph(
                nodes=(),
                session_rows=(SimpleNamespace(session_id="session-1"),),
                fingerprint="graph",
                truncated=False,
            )

    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="owner@example.com", role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=factory,
                work_graph_resolver=_Resolver(),
                chat_v2_cursor_secret="secret",
            )
        ),
    )
    payload = SimpleNamespace(
        scope=TimelineScope(
            key="conversation:root",
            kind="conversation",
            conversation_id="root",
        ),
        path_generation_id="generation-1",
        before=None,
        limit=20,
    )

    with pytest.raises(HTTPException) as exc_info:
        await work_file_history(request, payload)

    assert exc_info.value.status_code == 404
    assert factory.created == 3
    assert factory.active == 0
