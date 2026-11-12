from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import event

from cognis.api.chat_v2 import deliverables as inline
from cognis.api.chat_v2.schemas import (
    AssistantDeliverableTimelineItem,
    ChatSyncResponse,
    TimelineBackfillResponse,
    TimelineScope,
    UpsertTimelineItemOp,
)
from cognis.store.models import DeliverableRow
from cognis.store.queries import (
    create_conversation,
    create_deliverable,
    create_step_run,
    create_task,
)


def _response(deliverable_ids: list[str], *, sync: bool = False):
    items = [
        AssistantDeliverableTimelineItem(
            id=f"assistant-deliverable:{id}",
            sort_key=f"{i:020d}",
            source_refs=[],
            stable=True,
            deliverable_id=id,
            format="rich",
        )
        for i, id in enumerate(deliverable_ids)
    ]
    scope = TimelineScope(
        key="conversation:conv-inline", kind="conversation", conversation_id="conv-inline"
    )
    if sync:
        return ChatSyncResponse(
            projection_version="test",
            scope=scope,
            cursor_before="before",
            cursor_after="after",
            ops=[UpsertTimelineItemOp(item=item) for item in items],
            server_time="2026-10-28T00:00:00Z",
        )
    return TimelineBackfillResponse(
        projection_version="test",
        scope=scope,
        items=items,
        server_time="2026-10-28T00:00:00Z",
    )


async def _seed(factory):
    async with factory() as session:
        await create_conversation(
            session,
            "owner@example.com",
            "agent-owner",
            "agent_direct",
            conversation_id="conv-inline",
        )
        await create_deliverable(
            session,
            deliverable_id="dlv_inline",
            conversation_id="conv-inline",
            content="Full fallback",
            format="rich",
            rich={"blocks": [{"type": "markdown", "content": "Full rich body"}]},
            outputs={"ready": True},
            artifact_store=factory.artifact_store,
        )
        await session.commit()
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=factory,
            artifact_store=factory.artifact_store,
        )
    )


async def test_task_document_hydration_keeps_workflow_freshness_and_read_authority(
    task_continuation_db,
):
    app = await _seed(task_continuation_db)
    async with task_continuation_db() as session:
        task = await create_task(
            session,
            created_by="owner@example.com",
            agent_id="agent-owner",
            title="Pending revision",
            status="paused",
        )
        run = await create_step_run(
            session,
            task_id=task.task_id,
            step_name="publish",
            step_type="run",
            agent_id="agent-owner",
            status="running",
            attempt=2,
        )
        document = await session.get(DeliverableRow, "dlv_inline")
        document.step_run_id = run.step_run_id
        document.status = "rejected"
        await session.commit()
    response = await inline.hydrate_chat_deliverables(
        app,
        _response(["dlv_inline"]),
        caller_email="owner@example.com",
        conversation_id="conv-inline",
    )
    document = response.items[0].deliverable
    assert document.content == "Full fallback"
    assert document.status == "rejected"
    assert document.task_freshness.model_dump() == {
        "state": "stale",
        "replacement_status": "paused",
        "label": "Stale — revision 1, revision paused",
    }
    refused = await inline.hydrate_chat_deliverables(
        app,
        _response(["dlv_inline"]),
        caller_email="other@example.com",
        conversation_id="conv-inline",
    )
    assert refused.items[0].deliverable is None


@pytest.mark.parametrize("sync", [False, True])
async def test_render_data_arrives_with_history_without_mutating_projection(
    task_continuation_db,
    sync,
):
    app = await _seed(task_continuation_db)
    original = _response(["dlv_inline"], sync=sync)
    response = await inline.hydrate_chat_deliverables(
        app,
        original,
        caller_email="owner@example.com",
        conversation_id="conv-inline",
    )
    item = response.ops[0].item if sync else response.items[0]
    assert item.deliverable.content == "Full fallback"
    assert item.deliverable.rich_payload["blocks"][0]["content"] == "Full rich body"
    assert item.deliverable.is_latest is True
    assert (original.ops[0].item if sync else original.items[0]).deliverable is None


@pytest.mark.parametrize(
    "caller,conversation,id",
    [
        ("other@example.com", "conv-inline", "dlv_inline"),
        ("other@example.com", "conv-owner", "dlv_owner"),
    ],
)
async def test_references_do_not_grant_inline_payload_access(
    task_continuation_db,
    caller,
    conversation,
    id,
):
    app = await _seed(task_continuation_db)
    response = await inline.hydrate_chat_deliverables(
        app,
        _response([id]),
        caller_email=caller,
        conversation_id=conversation,
    )
    assert response.items[0].deliverable is None


async def test_unavailable_slow_and_oversized_payloads_keep_reference_loader(
    task_continuation_db,
    monkeypatch,
):
    app = await _seed(task_continuation_db)
    load = AsyncMock(side_effect=FileNotFoundError)
    monkeypatch.setattr(inline, "hydrate_deliverable_payload", load)
    for error in (
        FileNotFoundError,
        TimeoutError,
        ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"),
        EndpointConnectionError(endpoint_url="https://storage.test"),
    ):
        load.side_effect = error
        response = await inline.hydrate_chat_deliverables(
            app,
            _response(["dlv_inline"]),
            caller_email="owner@example.com",
            conversation_id="conv-inline",
        )
        assert response.items[0].deliverable is None
    monkeypatch.setattr(inline, "MAX_INLINE_TOTAL_BYTES", 1)
    load.reset_mock()
    await inline.hydrate_chat_deliverables(
        app,
        _response(["dlv_inline"]),
        caller_email="owner@example.com",
        conversation_id="conv-inline",
    )
    load.assert_not_called()


@pytest.mark.parametrize("transport", ["sync", "backfill", "catch_up"])
async def test_render_data_survives_production_response_boundaries(
    task_continuation_db,
    monkeypatch,
    transport,
):
    from cognis.api import websocket
    from cognis.api.chat_v2 import routes, snapshot_coordinator, sync

    app = await _seed(task_continuation_db)
    response = _response(["dlv_inline"], sync=transport != "backfill")
    context = SimpleNamespace(
        scope=response.scope,
        conversation=None,
        session_refs=[],
        event_store=None,
        cursor_secret="secret",
        queue=None,
        state=None,
        runtime_input=None,
        event_post_processor=None,
        session_cache=None,
    )
    request = SimpleNamespace(app=app)
    monkeypatch.setattr(routes, "_load_read_context", AsyncMock(return_value=context))
    monkeypatch.setattr(
        routes, "require_current_user", lambda request: SimpleNamespace(email="owner@example.com")
    )
    if transport == "sync":
        monkeypatch.setattr(routes, "build_chat_sync_response", AsyncMock(return_value=response))
        result = await routes.chat_v2_sync(request, "conv-inline", cursor="cursor", limit=None)
        document = result.ops[0].item.deliverable
    elif transport == "backfill":
        monkeypatch.setattr(
            routes, "build_timeline_backfill_response", AsyncMock(return_value=response)
        )
        result = await routes.chat_v2_timeline(request, "conv-inline", before="cursor", limit=50)
        document = result.items[0].deliverable
    else:
        monkeypatch.setattr(
            snapshot_coordinator,
            "load_conversation_snapshot_context",
            AsyncMock(return_value=context),
        )
        monkeypatch.setattr(sync, "build_chat_sync_response", AsyncMock(return_value=response))
        connection = SimpleNamespace(user_email="owner@example.com", send_json=AsyncMock())
        await websocket._send_chat_v2_catch_up(app, connection, response.scope, "cursor")
        frame = connection.send_json.call_args.args[0]
        assert frame["sync"]["ops"][0]["item"]["deliverable"]["content"] == "Full fallback"
        return
    assert document.content == "Full fallback"
    assert document.rich_payload["blocks"][0]["content"] == "Full rich body"


@pytest.mark.parametrize("sync", [False, True])
async def test_cold_page_delivers_more_than_four_bodies_with_constant_metadata_queries(
    task_continuation_db, sync
):
    factory = task_continuation_db
    app = await _seed(factory)
    ids = ["dlv_inline"] + [f"dlv_cold_{index}" for index in range(8)]
    async with factory() as session:
        for id in ids[1:]:
            await create_deliverable(
                session,
                deliverable_id=id,
                conversation_id="conv-inline",
                content=f"Body for {id}",
                artifact_store=factory.artifact_store,
            )
        await session.commit()
    statements = []
    engine = factory.kw["bind"].sync_engine
    checked_out = 0
    original_load = app.state.artifact_store.async_load

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def checkout(*_args):
        nonlocal checked_out
        checked_out += 1

    def checkin(*_args):
        nonlocal checked_out
        checked_out -= 1

    async def load_without_db_lease(*args):
        assert checked_out == 0
        return await original_load(*args)

    app.state.artifact_store = SimpleNamespace(async_load=load_without_db_lease)
    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        original = _response(ids + [ids[0]], sync=sync)
        response = await inline.hydrate_chat_deliverables(
            app, original, caller_email="owner@example.com", conversation_id="conv-inline"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    items = [op.item for op in response.ops] if sync else response.items
    assert all(item.deliverable is not None for item in items)
    assert {item.deliverable.deliverable_id for item in items} == set(ids)
    assert len(statements) <= 5
    originals = [op.item for op in original.ops] if sync else original.items
    assert all(item.deliverable is None for item in originals)


async def test_aggregate_envelope_replaces_per_document_cutoff(task_continuation_db):
    app = await _seed(task_continuation_db)
    ids = [f"dlv_large_{index}" for index in range(4)]
    async with task_continuation_db() as session:
        for id in ids:
            await create_deliverable(
                session,
                deliverable_id=id,
                conversation_id="conv-inline",
                content="A" * (600 * 1024),
                artifact_store=task_continuation_db.artifact_store,
            )
        await session.commit()
    response = await inline.hydrate_chat_deliverables(
        app, _response(ids), caller_email="owner@example.com", conversation_id="conv-inline"
    )
    assert response.items[0].deliverable is None
    assert all(item.deliverable is not None for item in response.items[1:])
    assert (
        sum(
            len(item.deliverable.model_dump_json().encode("utf-8"))
            for item in response.items
            if item.deliverable is not None
        )
        <= inline.MAX_INLINE_TOTAL_BYTES
    )


async def test_owner_cross_conversation_reads_match_detail_policy(
    task_continuation_db, monkeypatch
):
    from cognis.api.routes import deliverables as routes

    app = await _seed(task_continuation_db)
    monkeypatch.setattr(
        routes, "require_current_user", lambda request: SimpleNamespace(email="owner@example.com")
    )
    detail = await routes._authorized_deliverable(
        SimpleNamespace(app=app, state=SimpleNamespace()),
        "dlv_inline",
        accessor_conversation_id="conv-owner",
    )
    response = await inline.hydrate_chat_deliverables(
        app,
        _response(["dlv_inline"]),
        caller_email="owner@example.com",
        conversation_id="conv-owner",
    )
    assert response.items[0].deliverable.content == detail.content


async def test_task_and_managed_descendant_bodies_arrive_with_controller_history(
    task_continuation_db,
):
    from tests.unit.test_artifact_virtual_deliverable_refs import _seed_managed_deliverables

    await _seed_managed_deliverables(task_continuation_db)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=task_continuation_db, artifact_store=task_continuation_db.artifact_store
        )
    )
    ids = ["dlv_owner", "dlv_child", "dlv_grandchild", "dlv_published"]
    async with task_continuation_db() as session:
        source = await session.get(DeliverableRow, "dlv_child")
        assert source is not None
        for index in range(6):
            id = f"dlv_managed_page_{index}"
            await create_deliverable(
                session,
                deliverable_id=id,
                conversation_id=source.conversation_id,
                session_id=source.session_id,
                content=f"Managed result {index}",
                artifact_store=task_continuation_db.artifact_store,
            )
            ids.append(id)
        await session.commit()
    response = await inline.hydrate_chat_deliverables(
        app, _response(ids), caller_email="owner@example.com", conversation_id="conv-controller"
    )
    assert all(item.deliverable is not None for item in response.items)


async def test_document_free_history_does_no_deliverable_work():
    factory = AsyncMock(side_effect=AssertionError("Document-free history must not query"))
    app = SimpleNamespace(state=SimpleNamespace(session_factory=factory))
    original = _response([])
    assert (
        await inline.hydrate_chat_deliverables(
            app, original, caller_email="owner@example.com", conversation_id="conv-owner"
        )
        is original
    )
    factory.assert_not_called()


@pytest.mark.parametrize("cancel", [False, True])
async def test_payload_parts_load_concurrently_and_join_cancelled_siblings(
    task_continuation_db, cancel
):
    from cognis.store.deliverable_storage import hydrate_deliverable_payload
    from cognis.store.queries import get_deliverable

    app = await _seed(task_continuation_db)
    async with task_continuation_db() as session:
        row = await get_deliverable(session, "dlv_inline")
    started, finished = set(), set()
    ready, release = asyncio.Event(), asyncio.Event()
    original_load = app.state.artifact_store.async_load

    async def load(namespace, object_id, filename):
        started.add(filename)
        if len(started) == 3:
            ready.set()
        try:
            await release.wait()
            return await original_load(namespace, object_id, filename)
        finally:
            finished.add(filename)

    store = SimpleNamespace(async_load=load)
    task = asyncio.create_task(hydrate_deliverable_payload(row, store))
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            assert (await task).content == "Full fallback"
        assert len(started) == len(finished) == 3
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_thread_backed_storage_cancel_discards_results_but_does_not_stop_io(
    task_continuation_db, monkeypatch
):
    from cognis.store.deliverable_storage import hydrate_deliverable_payload
    from cognis.store.queries import get_deliverable

    app = await _seed(task_continuation_db)
    async with task_continuation_db() as session:
        row = await get_deliverable(session, "dlv_inline")
    before = row.content
    store = app.state.artifact_store
    original_load = store.load
    lock = threading.Lock()
    started, completed = set(), set()
    ready, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_load(namespace, object_id, filename):
        with lock:
            started.add(filename)
            if len(started) == 3:
                ready.set()
        try:
            assert release.wait(timeout=5)
            return original_load(namespace, object_id, filename)
        finally:
            with lock:
                completed.add(filename)
                if len(completed) == 3:
                    finished.set()

    monkeypatch.setattr(store, "load", blocked_load)
    task = asyncio.create_task(hydrate_deliverable_payload(row, store))
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # asyncio cancellation joins wrappers, not running to_thread calls.
        assert not finished.is_set()
        assert row.content == before
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)
    assert started == completed
    assert row.content == before
