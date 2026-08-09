"""Explicit canonical Chat v2 snapshot context and cache coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, cast

from sqlalchemy import select

from cognis.api.chat_v2.background_event_reads import (
    AdmittedSessionEventStore,
    BackgroundEventReadAdmission,
)
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventStore
from cognis.api.chat_v2.schemas import (
    ChatSnapshot,
    ConversationStateView,
    ConversationSummary,
    QueueState,
    TimelineScope,
)
from cognis.api.chat_v2.shared_snapshot_cache import CachedSnapshotStatus, SnapshotRequestTrace
from cognis.api.chat_v2.snapshot_metrics import (
    SnapshotRequestTier,
    WarmFailureReason,
)
from cognis.api.chat_v2.sync import (
    ConversationSessionRef,
    EventPostProcessor,
    RuntimeOverlayInput,
    _runtime_overlay,
    build_chat_snapshot,
    conversation_summary_from_row,
    queue_state_from_messages,
    runtime_input_from_scheduler,
    state_view_from_snapshot,
)
from cognis.api.chat_v2.work_graph import (
    WORK_GRAPH_MAX_SECONDS,
    AuthorizedWorkRootNotReadyError,
    resolve_authorized_work_graph,
)
from cognis.api.chat_v2.work_materializer import WORK_MATERIALIZER_VERSION
from cognis.api.chat_v2.work_repository import read_activity_overview
from cognis.api.common import api_exception
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.conversation_state import snapshot_for_conversation
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import WorkSessionProjectionRow
from cognis.store.queries import (
    get_conversation,
    get_root_session_chain,
    list_conversation_sessions,
)


@dataclass(frozen=True, slots=True)
class ConversationSnapshotContext:
    scope: TimelineScope
    conversation: ConversationSummary
    session_refs: list[ConversationSessionRef]
    event_store: SessionEventStore | None
    cursor_secret: str
    queue: QueueState
    state: ConversationStateView
    runtime_input: RuntimeOverlayInput
    session_cache: Any
    event_post_processor: EventPostProcessor
    owner_email: str
    conversation_id: str
    work_overview_fence: str = ""
    work_overview_coverage: tuple[tuple[str, int], ...] | None = None


async def load_conversation_snapshot_context(
    app: Any,
    *,
    user_email: str,
    conversation_id: str,
) -> ConversationSnapshotContext:
    """Load an explicitly authorized conversation snapshot context without HTTP coupling."""

    async with app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None or getattr(row, "status", None) == "deleted" or row.user_email != user_email:
            raise api_exception(404, "not_found", "Conversation not found")
        session_refs = await _conversation_session_refs(
            app,
            session,
            conversation_id,
            row.active_session_id,
            user_email=user_email,
        )
        work_overview_fence, work_overview_coverage = await _work_overview_state(
            session,
            session_refs,
        )
        state_snapshot = await snapshot_for_conversation(
            session,
            user_email=user_email,
            conversation_id=conversation_id,
            turn_scheduler=getattr(app.state, "turn_scheduler", None),
        )

    turn_scheduler = getattr(app.state, "turn_scheduler", None)
    queued_messages = (
        await turn_scheduler.get_queued_messages(conversation_id) if turn_scheduler else []
    )
    conversation = conversation_summary_from_row(row)
    scope = TimelineScope(
        key=f"conversation:{conversation_id}",
        kind="conversation",
        conversation_id=conversation_id,
        session_id=conversation.active_session_id,
        label=conversation.title,
        status=conversation.status,
    )
    return ConversationSnapshotContext(
        scope=scope,
        conversation=conversation,
        session_refs=session_refs,
        event_store=None,
        cursor_secret=_cursor_secret(app),
        queue=queue_state_from_messages(queued_messages),
        state=state_view_from_snapshot(state_snapshot),
        runtime_input=await runtime_input_from_scheduler(
            conversation_id=conversation_id,
            scope_key=scope.key,
            active_session_id=conversation.active_session_id,
            turn_scheduler=turn_scheduler,
            session_cache=getattr(app.state, "session_cache", None),
        ),
        session_cache=getattr(app.state, "session_cache", None),
        event_post_processor=event_attachment_post_processor(
            app,
            owner_email=user_email,
            conversation_id=conversation_id,
        ),
        owner_email=user_email,
        conversation_id=conversation_id,
        work_overview_fence=work_overview_fence,
        work_overview_coverage=work_overview_coverage,
    )


async def build_chat_snapshot_coordinated(
    app: Any,
    context: ConversationSnapshotContext,
    *,
    request_trace: SnapshotRequestTrace | None = None,
) -> ChatSnapshot:
    """Build/read the immutable projection, then overlay mutable authorized metadata."""

    snapshot, _tier = await _build_chat_snapshot_coordinated(
        app,
        context,
        request_trace=request_trace,
    )
    return snapshot


def admit_background_snapshot_reads(
    context: ConversationSnapshotContext,
    admission: BackgroundEventReadAdmission,
) -> ConversationSnapshotContext:
    """Apply controller-local admission only to one background snapshot build."""

    return replace(
        context,
        session_refs=[
            ref.model_copy(
                update={
                    "reader": AdmittedSessionEventStore(ref.reader, admission)
                    if ref.reader is not None
                    else None
                }
            )
            for ref in context.session_refs
        ],
    )


async def get_cached_chat_snapshot_coordinated(
    app: Any,
    context: ConversationSnapshotContext,
) -> tuple[ChatSnapshot | None, CachedSnapshotStatus | Literal["ineligible"]]:
    """Read a warmed immutable snapshot and apply fresh request-time metadata."""

    cache = getattr(app.state, "shared_chat_snapshot_cache", None)
    tokens = [ref.authority_token for ref in context.session_refs]
    if cache is None:
        return None, "unavailable"
    if any(token is None for token in tokens):
        return None, "ineligible"
    result = await cache.get_cached_result(
        authority_token=_conversation_authority_token(app, context),
        scope_key=context.scope.key,
        session_refs=context.session_refs,
        cursor_secret=context.cursor_secret,
        overview_fence=context.work_overview_fence,
        overview_coverage=context.work_overview_coverage,
    )
    if result.snapshot is None:
        return None, result.status
    if result.snapshot.activity_overview is None:
        return None, "miss"
    return (
        await _apply_mutable_snapshot_overlay(
            app,
            result.snapshot,
            context,
            refresh_activity=False,
        ),
        result.status,
    )


async def _build_chat_snapshot_coordinated(
    app: Any,
    context: ConversationSnapshotContext,
    *,
    _lineage_retry: bool = False,
    request_trace: SnapshotRequestTrace | None = None,
) -> tuple[ChatSnapshot, SnapshotRequestTier]:
    cache = getattr(app.state, "shared_chat_snapshot_cache", None)
    tokens = [ref.authority_token for ref in context.session_refs]
    built = False

    async def build() -> ChatSnapshot:
        nonlocal built
        built = True
        snapshot = await _build_immutable_snapshot(context)
        overview = (
            await _read_snapshot_activity_overview_bounded(app, context)
            if hasattr(app.state, "session_factory")
            else snapshot.activity_overview
        )
        return snapshot.model_copy(update={"activity_overview": overview})

    if cache is None or any(token is None for token in tokens):
        if request_trace is not None:
            request_trace.select("bypass")
        base = await build()
        tier: SnapshotRequestTier = "bypass"
    else:
        cache_result = await cache.get_or_build_result(
            authority_token=_conversation_authority_token(app, context),
            scope_key=context.scope.key,
            session_refs=context.session_refs,
            cursor_secret=context.cursor_secret,
            overview_fence=context.work_overview_fence,
            overview_coverage=context.work_overview_coverage,
            build=build,
            request_trace=request_trace,
        )
        base = cache_result.snapshot
        tier = cache_result.tier
        if base is None:
            base = await build()
            tier = "bypass"

    if built:
        fresh = await load_conversation_snapshot_context(
            app,
            user_email=context.owner_email,
            conversation_id=context.conversation_id,
        )
        original_lineage = [
            (ref.event_store_session_id, ref.authority_token, ref.ordinal)
            for ref in context.session_refs
        ]
        fresh_lineage = [
            (ref.event_store_session_id, ref.authority_token, ref.ordinal)
            for ref in fresh.session_refs
        ]
        if fresh_lineage != original_lineage:
            if not _lineage_retry:
                return await _build_chat_snapshot_coordinated(
                    app,
                    fresh,
                    _lineage_retry=True,
                    request_trace=request_trace,
                )
            base = await _build_immutable_snapshot(fresh)
            overview = (
                await _read_snapshot_activity_overview_bounded(app, fresh)
                if hasattr(app.state, "session_factory")
                else None
            )
            base = base.model_copy(update={"activity_overview": overview})
        context = fresh

    return await _apply_mutable_snapshot_overlay(
        app,
        base,
        context,
        refresh_activity=not built,
    ), tier


async def _apply_mutable_snapshot_overlay(
    app: Any,
    base: ChatSnapshot,
    context: ConversationSnapshotContext,
    *,
    refresh_activity: bool = True,
) -> ChatSnapshot:
    """Apply authorized mutable state and request-time attachment hydration."""

    now = datetime.now(UTC)
    overlaid = base.model_copy(
        update={
            "scope": context.scope,
            "conversation": context.conversation,
            "queue": context.queue,
            "state": context.state,
            "runtime": _runtime_overlay(context.runtime_input, generated_at=now),
            "server_time": now.isoformat(),
        }
    )
    overview = base.activity_overview
    if refresh_activity and hasattr(app.state, "session_factory"):
        overview = await _read_snapshot_activity_overview_bounded(
            app,
            context,
            stale=base.activity_overview,
        )
    overlaid = overlaid.model_copy(update={"activity_overview": overview})
    return await _hydrate_snapshot_attachments(
        app,
        overlaid,
        owner_email=context.owner_email,
        conversation_id=context.conversation_id,
        session_refs=context.session_refs,
    )


async def _read_snapshot_activity_overview(
    app: Any,
    context: ConversationSnapshotContext,
) -> Any:
    registry = getattr(app.state, "tool_registry", None)
    definitions = {
        definition.name: definition
        for definition in (registry.list_tools() if registry is not None else [])
    }
    async with app.state.session_factory() as db:
        graph = await resolve_authorized_work_graph(
            db,
            user_email=context.owner_email,
            scope=context.scope,
            deadline=monotonic() + WORK_GRAPH_MAX_SECONDS,
        )
        return await read_activity_overview(
            db,
            owner_email=context.owner_email,
            scope=context.scope,
            session_rows=list(graph.session_rows),
            workstreams=list(graph.nodes),
            graph_fingerprint=graph.fingerprint,
            graph_truncated=graph.truncated,
            tool_definitions=definitions,
            detail="lightweight",
        )


async def _read_snapshot_activity_overview_bounded(
    app: Any,
    context: ConversationSnapshotContext,
    *,
    stale: Any = None,
) -> Any:
    """Read the complete overview within one deadline and fail open to stale data."""

    try:
        async with asyncio.timeout(WORK_GRAPH_MAX_SECONDS):
            return await _read_snapshot_activity_overview(app, context)
    except TimeoutError:
        return stale
    except AuthorizedWorkRootNotReadyError:
        if (
            context.scope.kind == "conversation"
            and context.scope.conversation_id == context.conversation_id
        ):
            return None
        raise


async def warm_chat_snapshot_coordinated(
    app: Any, context: ConversationSnapshotContext
) -> tuple[Literal["succeeded", "skipped", "retry"], WarmFailureReason | None]:
    """Warm only shared Redis; never fail open into background Intaris projection reads."""

    cache = getattr(app.state, "shared_chat_snapshot_cache", None)
    if cache is None:
        return "skipped", None
    if not cache.warming_configured:
        return "skipped", None
    tokens = [ref.authority_token for ref in context.session_refs]
    if any(token is None for token in tokens):
        return "skipped", None
    scope_key = context.scope.key

    async def build() -> ChatSnapshot:
        snapshot = await _build_immutable_snapshot(context)
        overview = (
            await _read_snapshot_activity_overview_bounded(app, context)
            if hasattr(app.state, "session_factory")
            else snapshot.activity_overview
        )
        return snapshot.model_copy(update={"activity_overview": overview})

    result = await cache.get_or_build_result(
        authority_token=_conversation_authority_token(app, context),
        scope_key=scope_key,
        session_refs=context.session_refs,
        cursor_secret=context.cursor_secret,
        overview_fence=context.work_overview_fence,
        overview_coverage=context.work_overview_coverage,
        build=build,
        fail_open=False,
    )
    return cache.warm_outcome(scope_key), result.warm_failure


def event_attachment_post_processor(
    app: Any,
    *,
    owner_email: str,
    conversation_id: str,
) -> EventPostProcessor:
    async def hydrate(events: list[RawSessionEvent]) -> list[RawSessionEvent]:
        if not any(isinstance(event.data.get("attachments"), list) for event in events):
            return list(events)
        hydrated_events = []
        for event in events:
            attachments = event.data.get("attachments")
            if not isinstance(attachments, list):
                hydrated_events.append(event)
                continue
            source_session_id = event.data.get("cognis_session_id")
            hydrated = await hydrate_attachment_refs(
                app.state.session_factory,
                app.state.artifact_store,
                attachments,
                owner_email=owner_email,
                conversation_id=conversation_id,
                session_id=(str(source_session_id) if source_session_id is not None else None),
            )
            hydrated_events.append(
                event.model_copy(update={"data": {**event.data, "attachments": hydrated}})
            )
        return hydrated_events

    return hydrate


def _conversation_authority_token(
    app: Any,
    context: ConversationSnapshotContext,
) -> str:
    return cast(
        str,
        app.state.cached_event_store.derived_key_digest(
            "snapshot-conversation-authority",
            context.owner_email,
        ),
    )


async def _build_immutable_snapshot(
    context: ConversationSnapshotContext,
) -> ChatSnapshot:
    return await build_chat_snapshot(
        scope=context.scope,
        conversation=None,
        session_refs=context.session_refs,
        event_store=cast(SessionEventStore, context.event_store),
        cursor_secret=context.cursor_secret,
        queue=None,
        state=None,
        runtime_input=None,
        event_post_processor=None,
        event_post_processor_cache_key=None,
        session_cache=context.session_cache,
    )


async def _conversation_session_refs(
    app: Any,
    session: Any,
    conversation_id: str,
    active_session_id: str | None,
    *,
    user_email: str,
) -> list[ConversationSessionRef]:
    if active_session_id is None:
        latest_roots = await list_conversation_sessions(
            session,
            conversation_id,
            root_only=True,
            order="desc",
            limit=1,
        )
        active_session_id = latest_roots[0].session_id if latest_roots else None
    if active_session_id is None:
        return []
    chain, _truncated = await get_root_session_chain(
        session,
        conversation_id,
        active_session_id,
    )
    return [
        await _session_ref(app, row, user_email=user_email, ordinal=index)
        for index, row in enumerate(chain)
    ]


async def _work_overview_state(
    session: Any,
    session_refs: list[ConversationSessionRef],
) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Return the Work fence and source coverage used by the warmed overview."""

    session_ids = [ref.session_id for ref in session_refs]
    rows = (
        (
            await session.scalars(
                select(WorkSessionProjectionRow).where(
                    WorkSessionProjectionRow.session_id.in_(session_ids),
                    WorkSessionProjectionRow.materializer_version == WORK_MATERIALIZER_VERSION,
                )
            )
        ).all()
        if session_ids
        else []
    )
    by_session = {row.session_id: row for row in rows}
    values = []
    coverage: list[tuple[str, int]] = []
    for ref in session_refs:
        row = by_session.get(ref.session_id)
        coverage.append(
            (
                ref.event_store_session_id,
                row.covered_through_seq if row is not None else -1,
            )
        )
        values.append(
            (
                ref.session_id,
                None
                if row is None
                else (
                    row.materializer_version,
                    row.state,
                    row.target_seq,
                    row.covered_through_seq,
                ),
            )
        )
    encoded = json.dumps(values, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), tuple(coverage)


async def _session_ref(
    app: Any,
    session_row: Any,
    *,
    user_email: str,
    ordinal: int,
) -> ConversationSessionRef:
    if session_row.user_email != user_email:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session event-store authority does not match the authorized user",
        )
    agent = await app.state.agent_registry.get(
        session_row.agent_id,
        owner_email=user_email,
        include_disabled=True,
    )
    agent_owner_email = agent.owner_email if agent is not None else None
    if not agent_owner_email:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session agent authority is unavailable",
        )
    reader = app.state.cached_event_store.bind(
        EventStoreAuthority(
            user_email=user_email,
            agent_id=session_row.agent_id,
            agent_owner_email=agent_owner_email,
        )
    )
    return ConversationSessionRef(
        session_id=session_row.session_id,
        event_store_session_id=session_row.intaris_session_id or session_row.session_id,
        store="intaris",
        role="root",
        ordinal=ordinal,
        status=session_row.status,
        completion_reason=session_row.completion_reason,
        reader=reader,
        authority_token=reader.authority_token,
    )


async def _hydrate_snapshot_attachments(
    app: Any,
    snapshot: ChatSnapshot,
    *,
    owner_email: str,
    conversation_id: str,
    session_refs: list[ConversationSessionRef],
) -> ChatSnapshot:
    items = snapshot.timeline.items
    if not any(getattr(item, "attachments", None) for item in items):
        return snapshot
    session_ids = {ref.event_store_session_id: ref.session_id for ref in session_refs}
    hydrated_items = []
    for item in items:
        attachments = getattr(item, "attachments", None)
        if not attachments:
            hydrated_items.append(item)
            continue
        source_session_id = (
            session_ids.get(item.source_refs[0].session_id) if item.source_refs else None
        )
        hydrated = await hydrate_attachment_refs(
            app.state.session_factory,
            app.state.artifact_store,
            [attachment.model_dump(mode="json") for attachment in attachments],
            owner_email=owner_email,
            conversation_id=conversation_id,
            session_id=source_session_id,
        )
        hydrated_items.append(
            item.__class__.model_validate({**item.model_dump(mode="json"), "attachments": hydrated})
        )
    return snapshot.model_copy(
        update={"timeline": snapshot.timeline.model_copy(update={"items": hydrated_items})}
    )


def _cursor_secret(app: Any) -> str:
    secret = getattr(app.state, "chat_v2_cursor_secret", None)
    if isinstance(secret, str) and secret:
        return secret
    raise api_exception(
        500,
        "cursor_secret_unavailable",
        "Chat v2 cursor signing secret is not configured",
    )
