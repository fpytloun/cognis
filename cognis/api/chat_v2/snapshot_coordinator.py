"""Explicit canonical Chat v2 snapshot context and cache coordination."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from cognis.api.chat_v2.background_event_reads import (
    AdmittedSessionEventStore,
    BackgroundEventReadAdmission,
)
from cognis.api.chat_v2.event_store import RawSessionEvent, SessionEventStore
from cognis.api.chat_v2.event_store_refs import session_read_refs
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
    invalidate_chat_v2_snapshot_projection,
    queue_state_from_messages,
    runtime_input_from_scheduler,
    state_view_from_snapshot,
)
from cognis.api.common import api_exception
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.conversation_state import snapshot_for_conversation
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
        session_rows = await _conversation_session_rows(
            session,
            conversation_id,
            row.active_session_id,
        )
        state_snapshot = await snapshot_for_conversation(
            session,
            user_email=user_email,
            conversation_id=conversation_id,
            turn_scheduler=getattr(app.state, "turn_scheduler", None),
        )

    session_refs = await session_read_refs(
        app,
        session_rows,
        user_email=user_email,
        role="root",
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


async def rebuild_chat_snapshot_coordinated(
    app: Any,
    context: ConversationSnapshotContext,
) -> ChatSnapshot:
    """Invalidate derived event views and rebuild one snapshot from upstream state."""

    cached_event_store = getattr(app.state, "cached_event_store", None)
    shared_snapshot_cache = getattr(app.state, "shared_chat_snapshot_cache", None)
    for ref in context.session_refs:
        if cached_event_store is not None:
            await cached_event_store.invalidate_session(
                ref.store,
                ref.event_store_session_id,
                source="explicit_refresh",
            )
            if shared_snapshot_cache is not None:
                shared_snapshot_cache.invalidate_session_token(
                    cached_event_store.session_token(ref.store, ref.event_store_session_id)
                )
    invalidate_chat_v2_snapshot_projection(context.scope.key)
    fresh = await load_conversation_snapshot_context(
        app,
        user_email=context.owner_email,
        conversation_id=context.conversation_id,
    )
    base = await _build_immutable_snapshot(fresh)
    return await _apply_mutable_snapshot_overlay(app, base, fresh)


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
    )
    if result.snapshot is None:
        return None, result.status
    return (
        await _apply_mutable_snapshot_overlay(
            app,
            result.snapshot,
            context,
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
        return await _build_immutable_snapshot(context)

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
        context = fresh

    return await _apply_mutable_snapshot_overlay(app, base, context), tier


async def _apply_mutable_snapshot_overlay(
    app: Any,
    base: ChatSnapshot,
    context: ConversationSnapshotContext,
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
            "activity_overview": None,
            "server_time": now.isoformat(),
        }
    )
    return await _hydrate_snapshot_attachments(
        app,
        overlaid,
        owner_email=context.owner_email,
        conversation_id=context.conversation_id,
        session_refs=context.session_refs,
    )


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
        return await _build_immutable_snapshot(context)

    result = await cache.get_or_build_result(
        authority_token=_conversation_authority_token(app, context),
        scope_key=scope_key,
        session_refs=context.session_refs,
        cursor_secret=context.cursor_secret,
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
    snapshot = await build_chat_snapshot(
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
    return snapshot.model_copy(update={"activity_overview": None})


async def _conversation_session_rows(
    session: Any,
    conversation_id: str,
    active_session_id: str | None,
) -> list[Any]:
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
    return list(chain)


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
