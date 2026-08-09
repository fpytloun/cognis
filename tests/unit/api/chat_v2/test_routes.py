"""Route contract tests for Chat v2."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from cognis.api.app import create_app
from cognis.api.chat_v2 import routes as chat_v2_routes
from cognis.api.chat_v2 import shared_snapshot_cache as snapshot_cache_module
from cognis.api.chat_v2.cursors import (
    CursorLineageEntry,
    CursorSessionWatermark,
    InternalChatCursorPayload,
    encode_cursor,
)
from cognis.api.chat_v2.event_store import RawSessionEvent
from cognis.api.chat_v2.routes import (
    _cursor_secret,
    _load_session_context,
    _load_task_step_context,
    _scoped_tool_output_page,
    _session_read_ref,
    chat_v2_cancel_turn,
    chat_v2_client_performance,
    chat_v2_conversation_work,
    chat_v2_delete_queued_message,
    chat_v2_send_message,
    chat_v2_session_snapshot,
    chat_v2_session_sync,
    chat_v2_session_timeline,
    chat_v2_session_work,
    chat_v2_snapshot,
    chat_v2_snapshot_cache_only,
    chat_v2_task_step_snapshot,
    chat_v2_task_step_sync,
    chat_v2_task_step_timeline,
    chat_v2_task_step_work,
    chat_v2_update_queued_message,
    router,
)
from cognis.api.chat_v2.schemas import (
    ClientPerformanceRequest,
    ControlMutationV2Request,
    QueueUpdateV2Request,
    SendMessageV2Request,
    TimelineScope,
)
from cognis.api.chat_v2.shared_snapshot_cache import SnapshotRequestTrace
from cognis.api.chat_v2.snapshot_coordinator import ConversationSnapshotContext
from cognis.api.chat_v2.sync import PROJECTION_VERSION, RuntimeOverlayInput
from cognis.api.common import AuthenticatedUser
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.turn_scheduler import TurnError
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User
from tests.unit.api.chat_v2.test_cached_event_store import (
    AUTHORITY,
    Delegate,
    FakeClock,
    FakeRedis,
    build_test_snapshot,
    make_cache,
    make_snapshot_cache,
)
from tests.unit.api.chat_v2.test_sync import FakeEventStore


def test_chat_v2_read_routes_are_registered() -> None:
    routes = {
        (next(iter(route.methods)), route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
    }

    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/snapshot") in routes
    assert (
        "GET",
        "/api/v1/chat/v2/conversations/{conversation_id}/snapshot/cache-only",
    ) in routes
    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/sync") in routes
    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/timeline") in routes
    assert ("POST", "/api/v1/chat/v2/client-performance") in routes
    assert ("GET", "/api/v1/chat/v2/conversations/{conversation_id}/work") in routes
    assert ("GET", "/api/v1/chat/v2/sessions/{session_id}/work") in routes
    assert ("GET", "/api/v1/chat/v2/task-steps/{step_run_id}/work") in routes
    assert (
        "GET",
        "/api/v1/chat/v2/conversations/{conversation_id}/tool-outputs/{call_id}",
    ) in routes
    assert ("GET", "/api/v1/chat/v2/sessions/{session_id}/tool-outputs/{call_id}") in routes
    assert ("GET", "/api/v1/chat/v2/task-steps/{step_run_id}/tool-outputs/{call_id}") in routes
    assert (
        "PUT",
        "/api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}",
    ) in routes
    assert (
        "PUT",
        "/api/v1/chat/v2/conversations/{conversation_id}/commands/{client_txn_id}",
    ) in routes
    assert (
        "POST",
        "/api/v1/chat/v2/conversations/{conversation_id}/assistant-messages/fork",
    ) in routes
    assert ("POST", "/api/v1/chat/v2/conversations/{conversation_id}/cancel") in routes
    assert (
        "DELETE",
        "/api/v1/chat/v2/conversations/{conversation_id}/queue/{queue_id}",
    ) in routes
    assert (
        "PATCH",
        "/api/v1/chat/v2/conversations/{conversation_id}/queue/{queue_id}",
    ) in routes
    assert not any("/e2e/" in route.path for route in router.routes)
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path.endswith("/sync"):
            assert {parameter.name for parameter in route.dependant.query_params} == {
                "cursor",
                "limit",
            }


@pytest.mark.asyncio
async def test_cache_only_route_authorizes_before_cache_read_and_returns_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = cast(Any, SimpleNamespace())
    snapshot = cast(Any, SimpleNamespace())
    calls: list[str] = []

    async def load(_request: Any, _conversation_id: str) -> Any:
        calls.append("authorize")
        return context

    async def cached(_app: Any, received: Any) -> tuple[Any, str]:
        calls.append("cache")
        assert received is context
        return snapshot, "hit_l1"

    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load)
    monkeypatch.setattr(chat_v2_routes, "get_cached_chat_snapshot_coordinated", cached)
    response = SimpleNamespace(headers={})
    request = SimpleNamespace(app=SimpleNamespace())

    result = await chat_v2_snapshot_cache_only(cast(Any, request), "conv-1", cast(Any, response))

    assert result is snapshot
    assert calls == ["authorize", "cache"]
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.asyncio
async def test_cache_only_route_miss_returns_empty_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_v2_routes,
        "_load_read_context",
        AsyncMock(return_value=cast(Any, SimpleNamespace())),
    )
    monkeypatch.setattr(
        chat_v2_routes,
        "get_cached_chat_snapshot_coordinated",
        AsyncMock(return_value=(None, "miss")),
    )
    response = SimpleNamespace(headers={})

    result = await chat_v2_snapshot_cache_only(
        cast(Any, SimpleNamespace(app=SimpleNamespace())),
        "conv-1",
        cast(Any, response),
    )

    assert result.status_code == 204
    assert result.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_conversation_work_route_uses_bounded_typed_backfill_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ConversationSnapshotContext(
        scope=cast(Any, SimpleNamespace(key="conversation:conv-1")),
        conversation=cast(Any, SimpleNamespace()),
        session_refs=[],
        event_store=None,
        cursor_secret="secret",
        queue=cast(Any, SimpleNamespace()),
        state=cast(Any, SimpleNamespace()),
        runtime_input=cast(Any, SimpleNamespace()),
        session_cache=None,
        event_post_processor=cast(Any, AsyncMock()),
        owner_email="owner@example.com",
        conversation_id="conv-1",
    )
    expected = SimpleNamespace()
    load_context = AsyncMock(return_value=context)
    build_graph = AsyncMock(return_value=expected)
    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load_context)
    monkeypatch.setattr(chat_v2_routes, "_build_work_graph_projection", build_graph)

    result = await chat_v2_conversation_work(
        SimpleNamespace(app=SimpleNamespace()),
        "conv-1",
        limit=100,
    )

    assert result is expected
    build_graph.assert_awaited_once()
    assert build_graph.await_args.args[1] is context
    assert build_graph.await_args.kwargs == {
        "before": None,
        "limit": 100,
        "category": None,
        "from_time": None,
        "to_time": None,
        "exact_session_id": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "loader_name", "identifier"),
    [
        (chat_v2_conversation_work, "_load_read_context", "conv-1"),
        (chat_v2_session_work, "_load_session_context", "session-1"),
        (chat_v2_task_step_work, "_load_task_step_context", "step-1"),
    ],
)
async def test_work_route_families_forward_exact_session_filter(
    monkeypatch: pytest.MonkeyPatch,
    route: Any,
    loader_name: str,
    identifier: str,
) -> None:
    context = SimpleNamespace()
    monkeypatch.setattr(chat_v2_routes, loader_name, AsyncMock(return_value=context))
    build_graph = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(chat_v2_routes, "_build_work_graph_projection", build_graph)

    await route(
        SimpleNamespace(app=SimpleNamespace()),
        identifier,
        limit=10,
        filter_session_id="session-exact",
    )

    assert build_graph.await_args.kwargs["exact_session_id"] == "session-exact"


def test_activity_overview_route_families_are_in_generated_openapi() -> None:
    document = create_app().openapi()

    for path in (
        "/api/v1/chat/v2/conversations/{conversation_id}/activity-overview",
        "/api/v1/chat/v2/sessions/{session_id}/activity-overview",
        "/api/v1/chat/v2/task-steps/{step_run_id}/activity-overview",
    ):
        operation = document["paths"][path]["get"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ActivityOverviewResponse"}
        detail = next(item for item in operation["parameters"] if item["name"] == "detail")
        assert detail["schema"]["default"] == "lightweight"
        assert detail["schema"]["enum"] == ["lightweight", "full"]
    snapshot_schema = document["components"]["schemas"]["ChatSnapshot"]
    assert snapshot_schema["properties"]["activity_overview"]["anyOf"][0] == {
        "$ref": "#/components/schemas/ActivityOverviewResponse"
    }
    overview_schema = document["components"]["schemas"]["ActivityOverviewResponse"]
    assert "overview_revision" in overview_schema["required"]
    assert overview_schema["properties"]["detail"]["default"] == "lightweight"
    assert overview_schema["properties"]["recent_work"]["$ref"] == (
        "#/components/schemas/ActivityRecentWork"
    )
    workstream_schema = document["components"]["schemas"]["WorkstreamRef"]
    for field in (
        "agent_display_name",
        "agent_avatar_url",
        "backing_session_count",
        "backing_session_ids",
    ):
        assert field in workstream_schema["properties"]
    conversation_schema = document["components"]["schemas"]["ConversationResponse"]
    assert "root_controller_conversation_id" in conversation_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session", "task_step"])
async def test_scoped_snapshots_embed_the_same_activity_overview_service(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    snapshot = SimpleNamespace(model_copy=lambda *, update: SimpleNamespace(**update))
    overview = SimpleNamespace(overview_revision=f"revision-{kind}")
    monkeypatch.setattr(chat_v2_routes, "build_chat_snapshot", AsyncMock(return_value=snapshot))
    build_overview = AsyncMock(return_value=overview)
    monkeypatch.setattr(chat_v2_routes, "_build_activity_overview", build_overview)
    scope = (
        TimelineScope(key="session:id", kind="session", session_id="id")
        if kind == "session"
        else TimelineScope(
            key="task_step:id",
            kind="task_step",
            task_id="task-id",
            step_run_id="id",
        )
    )
    context = {"scope": scope}
    request = cast(Any, SimpleNamespace())

    result = await chat_v2_routes._build_scoped_snapshot(request, context)

    assert result.activity_overview is overview
    assert build_overview.await_args.args == (request, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_name", "loader_name"),
    [
        ("chat_v2_conversation_activity_overview", "_load_read_context"),
        ("chat_v2_session_activity_overview", "_load_session_context"),
        ("chat_v2_task_step_activity_overview", "_load_task_step_context"),
    ],
)
async def test_activity_overview_routes_forward_explicit_full_detail(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
    loader_name: str,
) -> None:
    context = SimpleNamespace()
    expected = SimpleNamespace(detail="full")
    monkeypatch.setattr(chat_v2_routes, loader_name, AsyncMock(return_value=context))
    build_overview = AsyncMock(return_value=expected)
    monkeypatch.setattr(chat_v2_routes, "_build_activity_overview", build_overview)

    result = await getattr(chat_v2_routes, route_name)(
        cast(Any, SimpleNamespace()),
        "scope-id",
        detail="full",
    )

    assert result is expected
    assert build_overview.await_args.args[1] is context
    assert build_overview.await_args.kwargs == {"detail": "full"}


@pytest.mark.asyncio
async def test_work_route_total_deadline_has_retryable_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def block(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_v2_routes, "WORK_REQUEST_MAX_SECONDS", 0.01)
    monkeypatch.setattr(chat_v2_routes, "_build_work_graph_projection_with_stages", block)

    started = asyncio.get_running_loop().time()
    with pytest.raises(HTTPException) as raised:
        await chat_v2_routes._build_work_graph_projection(
            cast(Any, SimpleNamespace()),
            cast(Any, {}),
            before=None,
            limit=10,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "work_request_timeout"
    assert asyncio.get_running_loop().time() - started < 0.2


def test_work_request_deadline_covers_database_projection_stage() -> None:
    assert chat_v2_routes.WORK_REQUEST_MAX_SECONDS >= (chat_v2_routes.WORK_GRAPH_MAX_SECONDS + 1.0)
    assert not hasattr(chat_v2_routes, "WORK_WATERMARK_MAX_SECONDS")
    assert not hasattr(chat_v2_routes, "WORK_SCAN_MAX_SECONDS")


@pytest.mark.asyncio
async def test_conversation_work_route_reads_database_without_event_store_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work-route.db'}")
    session_factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        session.add(User(email="alice@example.com", name="Alice", password_hash="x", role="user"))
        session.add(User(email="bob@example.com", name="Bob", password_hash="x", role="user"))
        await session.flush()
        session.add_all(
            [
                Agent(
                    agent_id="agent-alice",
                    owner_email="alice@example.com",
                    name="Alice agent",
                    description="Alice",
                ),
                Agent(
                    agent_id="agent-bob",
                    owner_email="bob@example.com",
                    name="Bob agent",
                    description="Bob",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Conversation(
                    conversation_id="conv-root",
                    user_email="alice@example.com",
                    agent_id="agent-alice",
                    context_type="web",
                    title_source="unset",
                    active_session_id="session-root",
                ),
                Conversation(
                    conversation_id="conv-child",
                    user_email="alice@example.com",
                    agent_id="agent-alice",
                    context_type="web",
                    title_source="unset",
                    active_session_id="session-child",
                    lineage_kind="conversation",
                    fork_source_conversation_id="conv-root",
                    fork_source_session_id="session-root",
                ),
                Conversation(
                    conversation_id="conv-foreign",
                    user_email="bob@example.com",
                    agent_id="agent-bob",
                    context_type="web",
                    title_source="unset",
                    active_session_id="session-foreign",
                    lineage_kind="conversation",
                    fork_source_conversation_id="conv-root",
                    fork_source_session_id="session-root",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Session(
                    session_id="session-root",
                    conversation_id="conv-root",
                    user_email="alice@example.com",
                    agent_id="agent-alice",
                    intaris_session_id="session-root",
                    delegation_metadata={},
                ),
                Session(
                    session_id="session-child",
                    conversation_id="conv-child",
                    user_email="alice@example.com",
                    agent_id="agent-alice",
                    intaris_session_id="session-child",
                    source_session_id="session-root",
                    delegation_metadata={},
                ),
                Session(
                    session_id="session-foreign",
                    conversation_id="conv-foreign",
                    user_email="bob@example.com",
                    agent_id="agent-bob",
                    intaris_session_id="session-foreign",
                    source_session_id="session-root",
                    delegation_metadata={},
                ),
            ]
        )
        await session.commit()

    event_store = FakeEventStore(
        {
            "session-root": [],
            "session-child": [
                RawSessionEvent(
                    store_id="intaris",
                    session_id="session-child",
                    seq=1,
                    type="user_message",
                    data={"content": "Create the file", "turn_id": "turn-child"},
                ),
                RawSessionEvent(
                    store_id="intaris",
                    session_id="session-child",
                    seq=2,
                    type="tool_call",
                    data={
                        "call_id": "call-child-1",
                        "name": "write",
                        "arguments": {"path": "child.txt"},
                        "turn_id": "turn-child",
                    },
                ),
                RawSessionEvent(
                    store_id="intaris",
                    session_id="session-child",
                    seq=3,
                    type="tool_result",
                    data={
                        "call_id": "call-child-1",
                        "name": "write",
                        "result": "saved",
                        "turn_id": "turn-child",
                    },
                ),
            ],
        }
    )
    event_store.authority_token = "a" * 64
    event_store.read_session_events = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("normal Work GET must not read Intaris")
    )
    event_store.read_session_high_watermark = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("normal Work GET must not read Intaris")
    )

    class _CachedEventStore:
        def bind(self, authority: EventStoreAuthority) -> FakeEventStore:
            assert authority.user_email == "alice@example.com"
            return event_store

    class _AgentRegistry:
        async def get(self, agent_id: str, **_: Any) -> Any:
            if agent_id == "agent-alice":
                return SimpleNamespace(owner_email="alice@example.com")
            return None

    context = ConversationSnapshotContext(
        scope=TimelineScope(
            key="conversation:conv-root",
            kind="conversation",
            conversation_id="conv-root",
            session_id="session-root",
        ),
        conversation=cast(Any, SimpleNamespace()),
        session_refs=[],
        event_store=event_store,
        cursor_secret="route-lineage-secret",
        queue=cast(Any, SimpleNamespace()),
        state=cast(Any, SimpleNamespace()),
        runtime_input=cast(Any, SimpleNamespace()),
        session_cache=None,
        event_post_processor=cast(Any, None),
        owner_email="alice@example.com",
        conversation_id="conv-root",
    )
    monkeypatch.setattr(chat_v2_routes, "_load_read_context", AsyncMock(return_value=context))
    request = SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email="alice@example.com", role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=session_factory,
                cached_event_store=_CachedEventStore(),
                agent_registry=_AgentRegistry(),
                artifact_store=None,
                tool_registry=None,
            )
        ),
    )

    result = await chat_v2_conversation_work(request, "conv-root", limit=2)

    workstream_ids = {item.session_id for item in result.workstreams}
    assert workstream_ids == {"session-root"}
    assert result.summary.mutations == 0
    assert result.graph_fingerprint
    assert result.before_cursor is None
    assert result.materialization.state == "materializing"
    event_store.read_session_events.assert_not_awaited()
    event_store.read_session_high_watermark.assert_not_awaited()

    await engine.dispose()


def test_client_performance_contract_is_strict_and_bounded() -> None:
    assert (
        ClientPerformanceRequest.model_validate(
            {"metric": "cached_restore_ms", "duration_ms": 12.5}
        ).duration_ms
        == 12.5
    )
    for payload in (
        {"metric": "other", "duration_ms": 1.0},
        {"metric": "timeline_fresh_ms", "duration_ms": -1.0},
        {"metric": "timeline_fresh_ms", "duration_ms": 300_001.0},
        {"metric": "timeline_fresh_ms", "duration_ms": float("inf")},
        {"metric": "timeline_fresh_ms", "duration_ms": 1.0, "conversation_id": "forbidden"},
    ):
        with pytest.raises(ValidationError):
            ClientPerformanceRequest.model_validate(payload)


@pytest.mark.anyio
async def test_client_performance_endpoint_requires_session_auth_and_bounds_body() -> None:
    async def stream():
        yield b'{"metric":"timeline_fresh_ms","duration_ms":42.0}'

    request = SimpleNamespace(
        state=SimpleNamespace(
            user=AuthenticatedUser(
                email="user@example.com",
                role="user",
                name="User",
                auth_type="session",
            )
        ),
        headers={"content-length": "51"},
        stream=stream,
    )
    assert await chat_v2_client_performance(cast(Any, request)) is None

    request.headers = {"content-length": "257"}
    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_client_performance(cast(Any, request))
    assert exc_info.value.status_code == 413

    request.state = SimpleNamespace()
    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_client_performance(cast(Any, request))
    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_snapshot_boundary_records_selected_tier_once_after_hydration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str, str]] = []
    context = object()

    async def load_context(_request, _conversation_id):
        return context

    async def fail_after_cache(_app, loaded, *, request_trace):
        assert loaded is context
        request_trace.select("l1")
        raise RuntimeError("attachment hydration failed")

    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load_context)
    monkeypatch.setattr(chat_v2_routes, "build_chat_snapshot_coordinated", fail_after_cache)
    monkeypatch.setattr(
        chat_v2_routes.SNAPSHOT_CACHE_METRICS,
        "request",
        lambda tier, outcome, _seconds: observations.append((tier, outcome)),
    )
    request = SimpleNamespace(app=SimpleNamespace())

    with pytest.raises(RuntimeError, match="attachment hydration failed"):
        await chat_v2_snapshot(cast(Any, request), "conversation-a")

    assert observations == [("l1", "error")]


@pytest.mark.anyio
async def test_snapshot_boundary_records_success_once_after_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str, str]] = []
    snapshot = object()

    async def load_context(_request, _conversation_id):
        return object()

    async def build_snapshot(_app, _context, *, request_trace):
        request_trace.select("redis")
        return snapshot

    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load_context)
    monkeypatch.setattr(chat_v2_routes, "build_chat_snapshot_coordinated", build_snapshot)
    monkeypatch.setattr(
        chat_v2_routes.SNAPSHOT_CACHE_METRICS,
        "request",
        lambda tier, outcome, _seconds: observations.append((tier, outcome)),
    )

    result = await chat_v2_snapshot(
        cast(Any, SimpleNamespace(app=SimpleNamespace())),
        "conversation-a",
    )

    assert result is snapshot
    assert observations == [("redis", "success")]


@pytest.mark.anyio
async def test_snapshot_boundary_uses_unknown_for_pre_cache_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str, str]] = []

    async def fail_context(_request, _conversation_id):
        raise RuntimeError("context failed")

    monkeypatch.setattr(chat_v2_routes, "_load_read_context", fail_context)
    monkeypatch.setattr(
        chat_v2_routes.SNAPSHOT_CACHE_METRICS,
        "request",
        lambda tier, outcome, _seconds: observations.append((tier, outcome)),
    )

    with pytest.raises(RuntimeError, match="context failed"):
        await chat_v2_snapshot(cast(Any, SimpleNamespace(app=SimpleNamespace())), "conversation-a")

    assert observations == [("unknown", "error")]


@pytest.mark.anyio
async def test_snapshot_boundary_records_bypass_once_after_exhausted_fence_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    events = make_cache(Delegate(), redis, clock)
    bound = events.bind(AUTHORITY)
    cache = make_snapshot_cache(events, redis, clock)
    refs = [
        chat_v2_routes.ConversationSessionRef(
            session_id="session-a",
            event_store_session_id="session-a",
            ordinal=0,
            reader=bound,
            authority_token=bound.authority_token,
        )
    ]
    attempts = 0
    observations: list[tuple[str, str]] = []

    async def reject_fence(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise snapshot_cache_module._FenceRejected

    async def load_context(_request, _conversation_id):
        return object()

    async def build_snapshot(_app, _context, *, request_trace: SnapshotRequestTrace):
        result = await cache.get_or_build_result(
            authority_token=bound.authority_token,
            scope_key="conversation:conversation-a",
            session_refs=refs,
            cursor_secret="cursor-secret",
            build=lambda: build_test_snapshot(bound),
            request_trace=request_trace,
        )
        assert result.snapshot is not None
        return result.snapshot

    monkeypatch.setattr(cache, "_coordinate_fill", reject_fence)
    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load_context)
    monkeypatch.setattr(chat_v2_routes, "build_chat_snapshot_coordinated", build_snapshot)
    monkeypatch.setattr(
        chat_v2_routes.SNAPSHOT_CACHE_METRICS,
        "request",
        lambda tier, outcome, _seconds: observations.append((tier, outcome)),
    )

    await chat_v2_snapshot(cast(Any, SimpleNamespace(app=SimpleNamespace())), "conversation-a")

    assert attempts == 3
    assert observations == [("bypass", "success")]
    await cache.aclose()


def test_cursor_secret_uses_app_state_secret() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(chat_v2_cursor_secret="s")))

    assert _cursor_secret(cast(Any, request)) == "s"


def test_cursor_secret_fails_closed_when_missing() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc_info:
        _cursor_secret(cast(Any, request))

    assert exc_info.value.status_code == 500
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "cursor_secret_unavailable"


def test_chat_v2_request_paths_do_not_construct_direct_intaris_readers() -> None:
    chat_v2_dir = Path(chat_v2_routes.__file__).parent
    for path in (chat_v2_dir / "routes.py", chat_v2_dir / "sync.py"):
        source = path.read_text(encoding="utf-8")
        assert "IntarisSessionEventStore(" not in source
    sync_source = (chat_v2_dir / "sync.py").read_text(encoding="utf-8")
    assert "_warm_session_cache_entry" not in sync_source
    assert "_IMMUTABLE_WATERMARK_CACHE" not in sync_source
    assert "_IMMUTABLE_WINDOW_CACHE" not in sync_source


@pytest.mark.asyncio
async def test_session_read_refs_bind_each_lineage_to_exact_authority() -> None:
    bound: list[EventStoreAuthority] = []
    authority_tokens = iter(("a" * 64, "b" * 64))
    request = _scoped_request("alice@example.com")
    request.app.state.agent_registry.get = AsyncMock(
        side_effect=lambda agent_id, **_kwargs: SimpleNamespace(
            agent_id=agent_id,
            owner_email=f"{agent_id}@owner.test",
        )
    )

    request.app.state.cached_event_store.bind = lambda authority: (
        bound.append(authority)
        or SimpleNamespace(authority=authority, authority_token=next(authority_tokens))
    )

    first = await _session_read_ref(
        request,
        _session(
            "session-1",
            owner="alice@example.com",
            conversation_id="conv-1",
            agent_id="agent-one",
        ),
        user_email="alice@example.com",
        role="root",
        ordinal=0,
    )
    second = await _session_read_ref(
        request,
        _session(
            "session-2",
            owner="alice@example.com",
            conversation_id="conv-1",
            agent_id="agent-two",
        ),
        user_email="alice@example.com",
        role="root",
        ordinal=1,
    )

    assert bound == [
        EventStoreAuthority(
            user_email="alice@example.com",
            agent_id="agent-one",
            agent_owner_email="agent-one@owner.test",
        ),
        EventStoreAuthority(
            user_email="alice@example.com",
            agent_id="agent-two",
            agent_owner_email="agent-two@owner.test",
        ),
    ]
    assert first.reader.authority == bound[0]
    assert second.reader.authority == bound[1]
    assert first.authority_token == "a" * 64
    assert second.authority_token == "b" * 64
    assert "alice@example.com" not in repr((first.authority_token, second.authority_token))
    assert "agent-one" not in repr((first.authority_token, second.authority_token))
    assert "agent-one@owner.test" not in repr((first.authority_token, second.authority_token))


@pytest.mark.anyio
async def test_conversation_sync_and_backfill_forward_fresh_attachment_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = AsyncMock()
    context = SimpleNamespace(
        scope=object(),
        conversation=object(),
        session_refs=[],
        event_store=object(),
        cursor_secret="secret",
        queue=object(),
        state=object(),
        runtime_input=object(),
        session_cache=object(),
        event_post_processor=processor,
    )
    sync_response = object()
    timeline_response = object()
    load = AsyncMock(return_value=context)
    build_sync = AsyncMock(return_value=sync_response)
    build_timeline = AsyncMock(return_value=timeline_response)
    monkeypatch.setattr(chat_v2_routes, "_load_read_context", load)
    monkeypatch.setattr(chat_v2_routes, "build_chat_sync_response", build_sync)
    monkeypatch.setattr(
        chat_v2_routes,
        "build_timeline_backfill_response",
        build_timeline,
    )

    assert (
        await chat_v2_routes.chat_v2_sync(
            cast(Any, object()),
            "conversation-a",
            "cursor",
            500,
        )
        is sync_response
    )
    assert (
        await chat_v2_routes.chat_v2_timeline(
            cast(Any, object()),
            "conversation-a",
            None,
            200,
        )
        is timeline_response
    )
    assert build_sync.await_args.kwargs["event_post_processor"] is processor
    assert build_timeline.await_args.kwargs["event_post_processor"] is processor


@pytest.mark.asyncio
async def test_session_read_ref_rejects_authority_user_mismatch_before_binding() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.cached_event_store.bind = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _session_read_ref(
            request,
            _session("session-1", owner="bob@example.com", conversation_id="conv-1"),
            user_email="alice@example.com",
            role="root",
            ordinal=0,
        )

    assert exc_info.value.detail["code"] == "event_store_authority_unavailable"
    request.app.state.cached_event_store.bind.assert_not_called()


def test_retry_completion_marker_ignores_unrelated_completed_events() -> None:
    tool_event = SimpleNamespace(
        type="tool_result",
        data={"turn_id": "turn-1", "status": "completed"},
    )
    turn_event = SimpleNamespace(
        type="lifecycle",
        data={"turn_id": "turn-1", "event": "turn_completed"},
    )

    assert chat_v2_routes._is_completed_turn_marker(cast(Any, tool_event)) is False
    assert chat_v2_routes._is_completed_turn_marker(cast(Any, turn_event)) is True


@pytest.mark.asyncio
async def test_send_message_claims_transaction_and_submits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        cast(Any, request),
        "conv-1",
        "txn-1",
        SendMessageV2Request(client_message_id="client-1", content="hello"),
    )

    assert response.status == "accepted"
    assert scheduler.submitted == [
        {
            "conversation_id": "conv-1",
            "content": "hello",
            "client_message_id": "client-1",
            "chat_mode": None,
            "idempotency_scope": "chat-v2:conv-1:user@test.com",
            "idempotency_key": "txn-1",
        }
    ]


@pytest.mark.asyncio
async def test_send_message_rejects_known_system_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(
            request,
            "conv-1",
            "txn-1",
            SendMessageV2Request(
                client_message_id="msg-1",
                content="/plan investigate this",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "slash_command_not_supported"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_accepts_unknown_slash_prefixed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        request,
        "conv-1",
        "txn-1",
        SendMessageV2Request(
            client_message_id="msg-1",
            content="/not-a-command should be plain text",
        ),
    )

    assert response.status == "accepted"
    assert scheduler.submitted == [
        {
            "conversation_id": "conv-1",
            "content": "/not-a-command should be plain text",
            "client_message_id": "msg-1",
            "chat_mode": None,
            "idempotency_scope": "chat-v2:conv-1:user@test.com",
            "idempotency_key": "txn-1",
        }
    ]


@pytest.mark.asyncio
async def test_send_message_duplicate_replays_without_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    payload_hash = chat_v2_routes._payload_hash(
        "send_message",
        {
            "content": payload.content,
            "attachments": [],
            "client_message_id": payload.client_message_id,
            "chat_mode": payload.chat_mode,
        },
    )
    tx = _tx(
        "txn-1",
        payload_hash=payload_hash,
        result={
            "status": "accepted",
            "client_txn_id": "txn-1",
            "client_message_id": "client-1",
            "conversation_id": "conv-1",
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_send_message(cast(Any, request), "conv-1", "txn-1", payload)

    assert response.status == "duplicate"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_duplicate_pending_reconciles_durable_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    tx = _tx(
        "txn-1",
        payload_hash=chat_v2_routes._payload_hash(
            "send_message",
            {
                "content": payload.content,
                "attachments": [],
                "client_message_id": payload.client_message_id,
                "chat_mode": payload.chat_mode,
            },
        ),
        status="pending",
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        cast(Any, request),
        "conv-1",
        "txn-1",
        payload,
    )

    assert response.status == "accepted"
    assert tx.status == "accepted"
    assert len(scheduler.submitted) == 1
    assert scheduler.submitted[0]["idempotency_scope"] == "chat-v2:conv-1:user@test.com"
    assert scheduler.submitted[0]["idempotency_key"] == "txn-1"


@pytest.mark.asyncio
async def test_send_message_duplicate_failed_replays_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload = SendMessageV2Request(client_message_id="client-1", content="hello")
    tx = _tx(
        "txn-1",
        payload_hash=chat_v2_routes._payload_hash(
            "send_message",
            {
                "content": payload.content,
                "attachments": [],
                "client_message_id": payload.client_message_id,
                "chat_mode": payload.chat_mode,
            },
        ),
        status="failed",
        error={"code": "queue_full", "message": "Queue is full"},
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(cast(Any, request), "conv-1", "txn-1", payload)

    assert exc_info.value.status_code == 429
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "queue_full"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_send_message_failed_retry_preserves_unknown_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(error=TurnError("session_creation_failed", "Session failed", False))
    request = _request(scheduler)
    tx = _tx("txn-1", status="pending")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    with pytest.raises(HTTPException) as first_exc:
        await chat_v2_send_message(
            cast(Any, request),
            "conv-1",
            "txn-1",
            SendMessageV2Request(client_message_id="client-1", content="hello"),
        )

    assert first_exc.value.status_code == 500
    assert tx.status == "failed"
    assert tx.error == {
        "code": "session_creation_failed",
        "message": "Session failed",
        "http_status": 500,
    }

    with pytest.raises(HTTPException) as retry_exc:
        chat_v2_routes._ensure_replayable_transaction(tx, tx.payload_hash)

    assert retry_exc.value.status_code == first_exc.value.status_code
    detail = cast(dict[str, Any], retry_exc.value.detail)
    assert detail["code"] == "session_creation_failed"


@pytest.mark.asyncio
async def test_send_message_records_queued_status(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = _Scheduler(queued=[{"queue_id": "queue-1", "client_message_id": "client-1"}])
    request = _request(scheduler)
    tx = _tx("txn-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        return tx

    async def _mark(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)
    monkeypatch.setattr(chat_v2_routes, "_mark_attachments_attached", _mark)

    response = await chat_v2_send_message(
        cast(Any, request),
        "conv-1",
        "txn-1",
        SendMessageV2Request(client_message_id="client-1", content="hello"),
    )

    assert response.status == "queued"
    assert response.queue_id == "queue-1"
    assert tx.status == "queued"


@pytest.mark.asyncio
async def test_send_message_conflicting_retry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    tx = _tx("txn-1", payload_hash="different")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_send_message(
            cast(Any, request),
            "conv-1",
            "txn-1",
            SendMessageV2Request(client_message_id="client-1", content="hello"),
        )

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["code"] == "client_txn_conflict"
    assert scheduler.submitted == []


@pytest.mark.asyncio
async def test_cancel_turn_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = _Scheduler(cancelled=True)
    request = _request(scheduler)
    tx = _tx("cancel-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    response = await chat_v2_cancel_turn(
        cast(Any, request),
        "conv-1",
        ControlMutationV2Request(client_txn_id="cancel-1"),
    )

    assert response.status == "cancelled"
    assert scheduler.cancel_calls == [("conv-1", False)]


@pytest.mark.asyncio
async def test_delete_queued_message_replays_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload_hash = chat_v2_routes._payload_hash("delete_queued_message", {"queue_id": "queue-1"})
    tx = _tx(
        "delete-1",
        operation="delete_queued_message",
        payload_hash=payload_hash,
        result={
            "conversation_id": "conv-1",
            "client_txn_id": "delete-1",
            "status": "deleted",
            "queue": {"messages": [], "queued_count": 0},
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_delete_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        client_txn_id="delete-1",
    )

    assert response.status == "duplicate"
    assert scheduler.deleted == []


@pytest.mark.asyncio
async def test_update_queued_message_updates_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(
        queued=[
            {
                "queue_id": "queue-1",
                "client_message_id": "client-1",
                "content": "old",
                "attachments": [],
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    request = _request(scheduler)
    tx = _tx("update-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    response = await chat_v2_update_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        QueueUpdateV2Request(client_txn_id="update-1", content=" updated "),
    )

    assert response.status == "updated"
    assert scheduler.updated == [("conv-1", "queue-1", "updated")]
    assert response.queue.queued_count == 1
    assert response.queue.messages[0].content == "updated"


@pytest.mark.asyncio
async def test_update_queued_message_replays_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler()
    request = _request(scheduler)
    payload_hash = chat_v2_routes._payload_hash(
        "update_queued_message",
        {"queue_id": "queue-1", "content": "updated"},
    )
    tx = _tx(
        "update-1",
        operation="update_queued_message",
        payload_hash=payload_hash,
        result={
            "conversation_id": "conv-1",
            "client_txn_id": "update-1",
            "status": "updated",
            "queue": {
                "messages": [
                    {
                        "queue_id": "queue-1",
                        "content": "updated",
                        "attachments": [],
                        "position": 0,
                    }
                ],
                "queued_count": 1,
            },
            "server_time": "2026-01-01T00:00:00+00:00",
        },
    )

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, False

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)

    response = await chat_v2_update_queued_message(
        cast(Any, request),
        "conv-1",
        "queue-1",
        QueueUpdateV2Request(client_txn_id="update-1", content="updated"),
    )

    assert response.status == "duplicate"
    assert scheduler.updated == []
    assert response.queue.messages[0].content == "updated"


@pytest.mark.asyncio
async def test_update_queued_message_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _Scheduler(update_result=None)
    request = _request(scheduler)
    tx = _tx("update-1")

    async def _require(_request: object, conversation_id: str) -> tuple[Any, Any]:
        return SimpleNamespace(email="user@test.com"), SimpleNamespace(
            conversation_id=conversation_id
        )

    async def _claim(*_args: object, **_kwargs: object) -> tuple[Any, bool]:
        return tx, True

    async def _complete(_request: object, _transaction_id: str, **kwargs: object) -> Any:
        tx.status = kwargs["status"]
        tx.result = kwargs.get("result")
        tx.error = kwargs.get("error")
        return tx

    monkeypatch.setattr(chat_v2_routes, "_require_mutable_conversation", _require)
    monkeypatch.setattr(chat_v2_routes, "_claim_transaction", _claim)
    monkeypatch.setattr(chat_v2_routes, "_complete_transaction", _complete)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_update_queued_message(
            cast(Any, request),
            "conv-1",
            "missing",
            QueueUpdateV2Request(client_txn_id="update-1", content="updated"),
        )

    assert exc_info.value.status_code == 404
    assert scheduler.updated == [("conv-1", "missing", "updated")]


@pytest.mark.asyncio
async def test_session_snapshot_route_denies_cross_user_and_preserves_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="bob@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="bob@example.com")
    _patch_scope_queries(monkeypatch, session_row=session_row, conversation_row=conversation_row)

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_session_snapshot(request, "session-1")

    assert exc_info.value.status_code == 403

    monkeypatch.setattr(chat_v2_routes, "_build_scoped_snapshot", _return_context_scope)
    request = _scoped_request("bob@example.com")
    result = await chat_v2_session_snapshot(request, "session-1")
    assert result.key == "session:session-1"
    assert result.conversation_id == "conv-1"
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_session_context_rejects_session_conversation_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="alice@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="bob@example.com")
    _patch_scope_queries(monkeypatch, session_row=session_row, conversation_row=conversation_row)

    with pytest.raises(HTTPException) as exc_info:
        await _load_session_context(request, "session-1")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_child_session_context_reads_complete_rotation_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    original = _session(
        "child-original",
        owner="alice@example.com",
        conversation_id="conv-1",
        parent_session_id="root-1",
    )
    original.status = "completed"
    successor = _session(
        "child-successor",
        owner="alice@example.com",
        conversation_id="conv-1",
        parent_session_id="root-1",
    )
    successor.previous_session_id = original.session_id
    successor.status = "completed"
    conversation_row = _conversation("conv-1", owner="alice@example.com")
    _patch_scope_queries(
        monkeypatch,
        session_row=original,
        conversation_row=conversation_row,
    )

    async def get_child_chain(_session: Any, _session_id: str) -> tuple[list[Any], bool]:
        return [original, successor], False

    async def session_read_ref(
        _request: Any,
        row: Any,
        **_kwargs: Any,
    ) -> str:
        return row.session_id

    async def runtime_input(**_kwargs: Any) -> RuntimeOverlayInput:
        return RuntimeOverlayInput(
            runtime_epoch="child-lineage",
            runtime_revision=0,
            active_turn=None,
        )

    monkeypatch.setattr(chat_v2_routes, "get_child_session_continuation_chain", get_child_chain)
    monkeypatch.setattr(chat_v2_routes, "_session_read_ref", session_read_ref)
    monkeypatch.setattr(chat_v2_routes, "runtime_input_from_scheduler", runtime_input)

    context = await _load_session_context(request, original.session_id)

    assert context["session_refs"] == [
        "child-original",
        "child-successor",
    ]
    assert context["scope"].session_id == "child-original"
    assert context["scope"].status == "completed"


@pytest.mark.asyncio
async def test_task_step_route_denies_task_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="bob@example.com"),
        session_row=_session("session-1", owner="alice@example.com", conversation_id="conv-1"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_v2_task_step_snapshot(request, "step-1")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_task_step_context_preserves_previous_session_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=2,
        status="completed",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        session_row=_session(
            "session-1",
            owner="alice@example.com",
            conversation_id="conv-1",
            parent_session_id="session-parent",
        ),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")
    assert context["scope"].parent_session_id == "session-parent"
    assert context["scope"].session_id == "session-1"


@pytest.mark.asyncio
async def test_task_step_context_uses_linked_session_conversation_when_step_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id=None,
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        session_row=_session("session-1", owner="alice@example.com", conversation_id="conv-1"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")

    assert context["scope"].conversation_id == "conv-1"
    assert context["scope"].session_id == "session-1"


@pytest.mark.asyncio
async def test_task_step_context_allows_missing_stream_without_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id=None,
        conversation_id=None,
        step_name="build",
        attempt_number=1,
        status="pending",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
    )

    context = await _load_task_step_context(request, "step-1")

    assert context["scope"].conversation_id is None
    assert context["scope"].missing_stream is True
    assert context["session_refs"] == []


@pytest.mark.asyncio
async def test_task_step_snapshot_explicitly_marks_missing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _scoped_request("alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id=None,
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="pending",
    )
    _patch_scope_queries(
        monkeypatch,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
        conversation_row=_conversation("conv-1", owner="alice@example.com"),
    )
    monkeypatch.setattr(chat_v2_routes, "_build_scoped_snapshot", _return_context_scope)

    result = await chat_v2_task_step_snapshot(request, "step-1")
    assert result.missing_stream is True
    assert result.kind == "task_step"


@pytest.mark.asyncio
async def test_task_step_tool_output_prefers_final_result_in_authorized_step_session() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = None

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            assert kwargs["session_id"] == "intaris-historical-step"
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_call",
                        data={
                            "call_id": "call-historical",
                            "result": "stale preview",
                        },
                    ),
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call-historical",
                            "result": "historical full output",
                            "status": "completed",
                        },
                    ),
                ],
                has_more_before=False,
                first_seq=1,
            )

    context = {
        "scope": SimpleNamespace(
            kind="task_step",
            conversation_id="conv-1",
            session_id="session-historical-step",
            step_run_id="step-historical",
        ),
        "session_refs": [
            SimpleNamespace(
                session_id="session-historical-step",
                event_store_session_id="intaris-historical-step",
            )
        ],
        "event_store": _EventStore(),
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call-historical",
        offset=0,
        limit=200,
        latest=False,
    )

    assert result.conversation_id == "conv-1"
    assert result.session_id == "session-historical-step"
    assert result.content == "historical full output"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ["session", "task_step"])
async def test_scoped_tool_output_finds_call_beyond_ten_thousand_events(
    scope_kind: str,
) -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = None

    class _LongEventStore:
        pages = 0

        async def read_session_events(self, **kwargs: Any) -> Any:
            self.pages += 1
            found = self.pages == 22
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call-old",
                            "result": "older than 10k events",
                            "status": "completed",
                        },
                    )
                ]
                if found
                else [
                    SimpleNamespace(
                        type="message",
                        data={"index": (self.pages - 1) * 500 + index},
                    )
                    for index in range(500)
                ],
                has_more_before=not found,
                first_seq=max(1, 20_000 - self.pages * 500),
            )

    event_store = _LongEventStore()
    context = {
        "scope": SimpleNamespace(kind=scope_kind, conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-old", event_store_session_id="intaris-old")
        ],
        "event_store": event_store,
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call-old",
        offset=0,
        limit=1000,
        latest=False,
    )
    assert result.content == "older than 10k events"
    assert event_store.pages == 22


@pytest.mark.asyncio
async def test_scoped_tool_output_authorizes_recovery_id_and_reads_saved_key() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = SimpleNamespace(
        read=AsyncMock(
            return_value=SimpleNamespace(
                content="saved output",
                offset=1,
                limit=1000,
                has_more=False,
                total_lines=1,
            )
        )
    )

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        type="tool_result",
                        data={
                            "call_id": "call_orig",
                            "recovery_call_id": "call_saved",
                            "result": "preview",
                            "has_full_output": True,
                        },
                    )
                ],
                has_more_before=False,
                first_seq=1,
            )

    context = {
        "scope": SimpleNamespace(conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-1", event_store_session_id="intaris-1")
        ],
        "event_store": _EventStore(),
    }
    result = await _scoped_tool_output_page(
        request,
        context=context,
        call_id="call_saved",
        offset=0,
        limit=1000,
        latest=False,
    )
    assert result.call_id == "call_saved"
    assert result.content == "saved output"
    request.app.state.tool_output_store.read.assert_awaited_once_with(
        "call_saved", offset=1, limit=1000
    )


@pytest.mark.asyncio
async def test_scoped_tool_output_denies_call_from_other_session() -> None:
    request = _scoped_request("alice@example.com")
    request.app.state.turn_scheduler = None
    request.app.state.tool_output_store = SimpleNamespace(read=AsyncMock())

    class _EventStore:
        async def read_session_events(self, **kwargs: Any) -> Any:
            return SimpleNamespace(events=[], has_more_before=False, first_seq=None)

    context = {
        "scope": SimpleNamespace(conversation_id="conv-1"),
        "session_refs": [
            SimpleNamespace(session_id="session-1", event_store_session_id="intaris-1")
        ],
        "event_store": _EventStore(),
    }
    with pytest.raises(HTTPException) as exc_info:
        await _scoped_tool_output_page(
            request,
            context=context,
            call_id="call-other-session",
            offset=0,
            limit=1000,
            latest=False,
        )
    assert exc_info.value.status_code == 404
    request.app.state.tool_output_store.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "argument"),
    [
        (chat_v2_session_sync, "session-1"),
        (chat_v2_session_timeline, "session-1"),
        (chat_v2_task_step_sync, "step-1"),
        (chat_v2_task_step_timeline, "step-1"),
    ],
)
async def test_scoped_route_rejects_cursor_from_different_scope(
    monkeypatch: pytest.MonkeyPatch,
    route: Any,
    argument: str,
) -> None:
    request = _scoped_request("alice@example.com")
    session_row = _session("session-1", owner="alice@example.com", conversation_id="conv-1")
    conversation_row = _conversation("conv-1", owner="alice@example.com")
    step = SimpleNamespace(
        step_run_id="step-1",
        task_id="task-1",
        session_id="session-1",
        conversation_id="conv-1",
        step_name="build",
        attempt_number=1,
        status="running",
    )
    _patch_scope_queries(
        monkeypatch,
        session_row=session_row,
        conversation_row=conversation_row,
        step_run=step,
        task=SimpleNamespace(task_id="task-1", created_by="alice@example.com"),
    )

    wrong_cursor = _cursor("conversation:other-conversation")
    if route in (chat_v2_session_sync, chat_v2_task_step_sync):
        with pytest.raises(HTTPException) as exc_info:
            await route(request, argument, wrong_cursor, limit=1)
    else:
        with pytest.raises(HTTPException) as exc_info:
            await route(request, argument, before=wrong_cursor, limit=1)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "cursor_invalid"


def _request(scheduler: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(turn_scheduler=scheduler)))


def _scoped_request(email: str) -> SimpleNamespace:
    reader = SimpleNamespace(
        read_session_events=AsyncMock(),
        read_session_high_watermark=AsyncMock(),
        authority_token="c" * 64,
    )
    return SimpleNamespace(
        state=SimpleNamespace(user=AuthenticatedUser(email=email, role="user")),
        app=SimpleNamespace(
            state=SimpleNamespace(
                chat_v2_cursor_secret="route-test-secret",
                providers=SimpleNamespace(guardrails=SimpleNamespace()),
                session_factory=lambda: _SessionContext(),
                turn_scheduler=None,
                session_cache=None,
                agent_registry=SimpleNamespace(
                    get=AsyncMock(
                        return_value=SimpleNamespace(
                            agent_id="agent",
                            owner_email=email,
                        )
                    )
                ),
                cached_event_store=SimpleNamespace(bind=lambda _authority: reader),
            )
        ),
    )


def _session(
    session_id: str,
    *,
    owner: str,
    conversation_id: str,
    parent_session_id: str | None = None,
    agent_id: str = "agent",
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        user_email=owner,
        conversation_id=conversation_id,
        parent_session_id=parent_session_id,
        intaris_session_id=session_id,
        delegation_task="delegation",
        agent_id=agent_id,
        status="active",
        completion_reason=None,
    )


def _conversation(conversation_id: str, *, owner: str) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=conversation_id,
        user_email=owner,
        agent_id="agent",
        title="Conversation",
        status="active",
        active_session_id="session-1",
    )


def _patch_scope_queries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_row: Any | None = None,
    conversation_row: Any | None = None,
    step_run: Any | None = None,
    task: Any | None = None,
) -> None:
    async def get_session(_session: Any, session_id: str) -> Any:
        return (
            session_row
            if session_row is not None and session_row.session_id == session_id
            else None
        )

    async def get_conversation(_session: Any, conversation_id: str) -> Any:
        return (
            conversation_row
            if conversation_row is not None and conversation_row.conversation_id == conversation_id
            else None
        )

    async def get_step(_session: Any, _step_run_id: str) -> Any:
        return step_run

    async def get_child_chain(_session: Any, session_id: str) -> tuple[list[Any], bool]:
        return (
            [session_row]
            if session_row is not None and session_row.session_id == session_id
            else []
        ), False

    async def get_task(_session: Any, _task_id: str) -> Any:
        return task

    monkeypatch.setattr(chat_v2_routes, "get_session_row", get_session)
    monkeypatch.setattr(chat_v2_routes, "get_conversation", get_conversation)
    monkeypatch.setattr(chat_v2_routes, "get_child_session_continuation_chain", get_child_chain)
    monkeypatch.setattr(chat_v2_routes, "get_step_run", get_step)
    monkeypatch.setattr(chat_v2_routes, "get_task", get_task)


async def _return_context_scope(_request: Any, context: dict[str, Any]) -> Any:
    return context["scope"]


def _cursor(scope_key: str) -> str:
    return encode_cursor(
        InternalChatCursorPayload(
            scope_key=scope_key,
            conversation_id=None,
            projection_version=PROJECTION_VERSION,
            session_watermarks=[
                CursorSessionWatermark(store="intaris", session_id="session-1", last_seq=0)
            ],
            lineage=[
                CursorLineageEntry(
                    store="intaris", session_id="session-1", role="session", ordinal=0
                )
            ],
            view_revision=0,
            issued_at="2026-01-01T00:00:00Z",
        ),
        "route-test-secret",
    )


class _SessionContext:
    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _tx(
    client_txn_id: str,
    *,
    operation: str = "send_message",
    payload_hash: str = "hash",
    status: str = "accepted",
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=f"chat_txn_{client_txn_id}",
        conversation_id="conv-1",
        principal_id="user@test.com",
        client_txn_id=client_txn_id,
        operation=operation,
        payload_hash=payload_hash,
        status=status,
        result=result,
        error=error,
    )


class _Scheduler:
    def __init__(
        self,
        *,
        cancelled: bool = False,
        queued: list[dict[str, Any]] | None = None,
        error: TurnError | None = None,
        update_result: dict[str, Any] | None | bool = True,
    ) -> None:
        self.cancelled = cancelled
        self.queued = queued or []
        self.error = error
        self.update_result = update_result
        self.submitted: list[dict[str, Any]] = []
        self.cancel_calls: list[tuple[str, bool]] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str, str]] = []

    async def submit_turn(
        self,
        conversation_id: str,
        content: str,
        **kwargs: Any,
    ) -> TurnError | None:
        self.submitted.append(
            {
                "conversation_id": conversation_id,
                "content": content,
                "client_message_id": kwargs.get("client_message_id"),
                "chat_mode": kwargs.get("one_shot_chat_mode"),
                "idempotency_scope": kwargs.get("idempotency_scope"),
                "idempotency_key": kwargs.get("idempotency_key"),
            }
        )
        return self.error

    async def cancel_turn(self, conversation_id: str, *, clear_queue: bool) -> bool:
        self.cancel_calls.append((conversation_id, clear_queue))
        return self.cancelled

    async def cancel_queued_message(self, conversation_id: str, queue_id: str) -> bool:
        self.deleted.append((conversation_id, queue_id))
        return True

    async def update_queued_message(
        self,
        conversation_id: str,
        queue_id: str,
        *,
        content: str,
    ) -> dict[str, Any] | None:
        self.updated.append((conversation_id, queue_id, content))
        if self.update_result is None:
            return None
        for item in self.queued:
            if item.get("queue_id") == queue_id:
                item["content"] = content
                return item
        return {"queue_id": queue_id, "content": content}

    def queued_messages(self, _conversation_id: str) -> list[Any]:
        return self.queued

    async def get_queued_messages(self, _conversation_id: str) -> list[Any]:
        return self.queued
