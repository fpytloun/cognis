"""Built-in conversation search and reading tools."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from cognis.api.serializers import conversation_to_response, serialize_event_rows
from cognis.core.conversation_search import join_session_matches
from cognis.core.long_lived_chat import is_long_lived_chat_context
from cognis.core.session_cache import CachedEvent
from cognis.models.search import SearchRequestFilters, SearchSessionsRequest
from cognis.models.session import ConversationContext, SessionModel
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolCapability, ToolSource
from cognis.store.queries import (
    get_conversation,
    get_session_row,
    get_setting_value,
    list_conversation_sessions,
    list_conversations,
)
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")
_SEARCH_SESSIONS_OVERFETCH_FACTOR = 3
_SEARCH_SESSIONS_MAX_LIMIT = 100
_READ_DEFAULT_TYPES = ["user_message", "assistant_message"]
_READ_ALLOWED_TYPES = frozenset(_READ_DEFAULT_TYPES)
_CONTENT_TRUNCATION_LIMIT = 4_000
_CONTENT_HARD_LIMIT = 32_000


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": properties, "required": required or []},
        source=_SOURCE,
        category="conversations",
        profile_group="conversations",
        read_only=True,
        capabilities=[ToolCapability.READ],
    )


LIST_CONVERSATIONS_TOOL = _tool(
    "list_conversations",
    "List your own conversations filtered by agent, project, status, or time range.",
    {
        "agent_id": {"type": "string"},
        "project_id": {"type": "string"},
        "status": {"type": "string", "enum": ["active", "starred", "archived", "all"]},
        "since": {"type": "string", "description": "ISO 8601 lower bound for activity time."},
        "until": {"type": "string", "description": "ISO 8601 upper bound for activity time."},
        "limit": {"type": "integer", "default": 25, "maximum": 100},
        "cursor": {"type": "string"},
    },
)

SEARCH_CONVERSATIONS_TOOL = _tool(
    "search_conversations",
    "Search your conversations by reasoning, intention, and summary content. Reasoning hits are the most precise.",
    {
        "q": {"type": "string"},
        "agent_id": {"type": "string"},
        "project_id": {"type": "string"},
        "context_type": {
            "type": "string",
            "description": "Restrict to a single conversation channel/context (e.g. 'web', 'signal').",
        },
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["reasoning", "intention", "summary"]},
        },
        "from_ts": {"type": "string"},
        "to_ts": {"type": "string"},
        "mode": {"type": "string", "enum": ["auto", "lexical", "vector", "hybrid"]},
        "limit": {"type": "integer", "default": 20, "maximum": 50},
        "cursor": {"type": "string"},
    },
    ["q"],
)

READ_CONVERSATION_MESSAGES_TOOL = _tool(
    "read_conversation_messages",
    "Read user and assistant messages from one owned conversation with anchor-based pagination. Defaults to the current conversation.",
    {
        "conversation_id": {"type": "string"},
        "anchor": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["latest", "from_start", "around", "after", "before"],
                },
                "session_id": {"type": "string"},
                "seq": {"type": "integer"},
                "before": {"type": "integer", "default": 5},
                "after": {"type": "integer", "default": 5},
            },
        },
        "cursor": {"type": "string", "description": "Opaque cursor returned by a previous read."},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["user_message", "assistant_message"]},
        },
        "include_content_truncation": {"type": "boolean", "default": True},
        "limit": {"type": "integer", "default": 50, "maximum": 200},
    },
)

SUMMARIZE_CONVERSATION_TOOL = _tool(
    "summarize_conversation",
    (
        "Generate a read-only compaction-format summary for a target conversation session. "
        "Defaults to the active session of the current conversation."
    ),
    {
        "conversation_id": {
            "type": "string",
            "description": "Conversation to summarize. Defaults to the current conversation.",
        },
        "session_id": {
            "type": "string",
            "description": (
                "Specific Cognis or Intaris session to summarize. Defaults to the "
                "conversation active session, or the latest session when unavailable."
            ),
        },
        "use_cached": {
            "type": "boolean",
            "default": True,
            "description": "Return an existing compaction summary when available instead of regenerating.",
        },
    },
)


def conversation_tools() -> list[ToolDefinition]:
    return [
        LIST_CONVERSATIONS_TOOL,
        SEARCH_CONVERSATIONS_TOOL,
        READ_CONVERSATION_MESSAGES_TOOL,
        SUMMARIZE_CONVERSATION_TOOL,
    ]


def _user(context: ToolExecutionContext) -> str:
    user_email = context.runtime_metadata.get("user_email")
    runtime_access = context.runtime_metadata.get("runtime_access")
    if not isinstance(user_email, str) and isinstance(runtime_access, dict):
        user_email = runtime_access.get("user_email")
    if not isinstance(user_email, str) and isinstance(context.shared_runtime_metadata, dict):
        user_email = context.shared_runtime_metadata.get("user_email")
    if not isinstance(user_email, str):
        raise ValueError("User context is unavailable")
    return user_email


def _current_conversation_id(context: ToolExecutionContext) -> str | None:
    runtime_access = context.runtime_metadata.get("runtime_access")
    if isinstance(runtime_access, dict) and isinstance(runtime_access.get("conversation_id"), str):
        return runtime_access["conversation_id"]
    value = context.runtime_metadata.get("conversation_id")
    return value if isinstance(value, str) else None


def _event_content(event: dict[str, Any]) -> str:
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"]
    return ""


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid cursor")
    return payload


def _parse_iso_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _as_aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _conversation_activity(row: Any) -> datetime | None:
    candidates = [
        _as_aware(getattr(row, "last_message_at", None)),
        _as_aware(getattr(row, "updated_at", None)),
        _as_aware(getattr(row, "created_at", None)),
    ]
    return max((item for item in candidates if item is not None), default=None)


def _conversation_context(row: Any) -> ConversationContext:
    return ConversationContext(
        type=getattr(row, "context_type", None) or "web",
        ref=getattr(row, "context_ref", None),
        platform_data=getattr(row, "context_data", None) or {},
        memory_labels=getattr(row, "memory_labels", None) or {},
    )


def _session_model(row: Any) -> SessionModel:
    return SessionModel(
        session_id=row.session_id,
        conversation_id=row.conversation_id,
        parent_session_id=getattr(row, "parent_session_id", None),
        previous_session_id=getattr(row, "previous_session_id", None),
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=getattr(row, "delegation_mode", None),
        delegation_task=getattr(row, "delegation_task", None),
        status=getattr(row, "status", "active"),
        completion_reason=getattr(row, "completion_reason", None),
        intaris_session_id=getattr(row, "intaris_session_id", None),
        mnemory_session_id=getattr(row, "mnemory_session_id", None),
        started_at=getattr(row, "started_at", None),
        idle_since=getattr(row, "idle_since", None),
        completed_at=getattr(row, "completed_at", None),
        result_summary=getattr(row, "result_summary", None),
        result_content=getattr(row, "result_content", None),
        updated_at=getattr(row, "updated_at", None),
    )


def _cached_event(raw_event: dict[str, Any]) -> CachedEvent:
    return CachedEvent(
        seq=int(raw_event.get("seq") or 0),
        type=str(raw_event.get("type") or ""),
        data=dict(raw_event.get("data") or {}),
        source=raw_event.get("source"),
        ts=raw_event.get("ts"),
    )


def _latest_compaction_summary(events: list[CachedEvent]) -> tuple[str | None, int]:
    for event in reversed(events):
        if event.type != "compaction_summary":
            continue
        summary = event.data.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary, event.seq
    return None, 0


def _filter_conversations_by_time(
    rows: list[Any], *, since: datetime | None, until: datetime | None
) -> list[Any]:
    if since is None and until is None:
        return rows
    output: list[Any] = []
    for row in rows:
        activity = _conversation_activity(row)
        if activity is None:
            continue
        if since is not None and activity < since:
            continue
        if until is not None and activity > until:
            continue
        output.append(row)
    return output


def _read_types(arguments: dict[str, Any]) -> list[str]:
    raw = arguments.get("kinds")
    if raw is None:
        return list(_READ_DEFAULT_TYPES)
    if not isinstance(raw, list):
        raise ValueError("kinds must be a list")
    values = [str(item) for item in raw if isinstance(item, str) and item.strip()]
    if not values:
        raise ValueError("kinds must include at least one message kind")
    unsupported = [item for item in values if item not in _READ_ALLOWED_TYPES]
    if unsupported:
        allowed = ", ".join(sorted(_READ_ALLOWED_TYPES))
        raise ValueError(f"Unsupported message kind: {unsupported[0]}. Allowed: {allowed}")
    return values


def _event_seq(event: dict[str, Any]) -> int:
    try:
        return int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _non_negative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("anchor window values must be non-negative integers")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("anchor window values must be non-negative integers") from exc
    if parsed < 0:
        raise ValueError("anchor window values must be non-negative integers")
    return parsed


def _tag_session_events(row: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        copied = dict(event)
        data = copied.get("data")
        copied["data"] = dict(data) if isinstance(data, dict) else {}
        copied["data"].setdefault("session_id", row.session_id)
        copied["data"].setdefault("intaris_session_id", row.intaris_session_id or row.session_id)
        tagged.append(copied)
    return tagged


def _session_index(session_rows: list[Any], session_id: str) -> int | None:
    for index, row in enumerate(session_rows):
        if row.session_id == session_id or row.intaris_session_id == session_id:
            return index
    return None


def _validate_anchor(anchor: dict[str, Any], session_rows: list[Any]) -> tuple[str, int | None]:
    anchor_kind = str(anchor.get("kind") or "latest")
    if anchor_kind not in {"latest", "from_start", "around", "after", "before"}:
        raise ValueError("anchor.kind must be one of: latest, from_start, around, after, before")
    if anchor_kind in {"around", "after", "before"}:
        session_id = anchor.get("session_id")
        seq = anchor.get("seq")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"anchor.session_id is required for {anchor_kind}")
        if _session_index(session_rows, session_id) is None:
            raise ValueError("anchor.session_id does not belong to this conversation")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError(f"anchor.seq is required for {anchor_kind}")
    return anchor_kind, _session_index(session_rows, str(anchor.get("session_id") or ""))


def _truncate_content(content: str, *, include_truncation: bool) -> tuple[str, bool]:
    limit = _CONTENT_TRUNCATION_LIMIT if include_truncation else _CONTENT_HARD_LIMIT
    if len(content) <= limit:
        return content, False
    return content[:limit], True


def build_conversation_tool_handlers(
    session_factory: Any,
    intaris: Any,
    compaction_strategy: Any | None = None,
) -> dict[str, Any]:
    async def list_conversations_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit") or 25), 1), 100)
        since = _parse_iso_datetime(arguments.get("since"), "since")
        until = _parse_iso_datetime(arguments.get("until"), "until")
        if since is not None and until is not None and since > until:
            raise ValueError("since must be earlier than until")
        offset = 0
        if arguments.get("cursor"):
            cursor = _decode_cursor(str(arguments["cursor"]))
            if cursor.get("tool") != "list_conversations":
                raise ValueError("Invalid cursor")
            raw_offset = cursor.get("offset")
            if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
                raise ValueError("Invalid cursor")
            offset = raw_offset
        async with session_factory() as session:
            rows = await list_conversations(
                session,
                _user(context),
                agent_id=arguments.get("agent_id"),
                project_id=arguments.get("project_id"),
                status=str(arguments.get("status") or "active"),
            )
        visible_rows = [row for row in rows if getattr(row, "status", None) != "deleted"]
        visible_rows = _filter_conversations_by_time(visible_rows, since=since, until=until)
        page_rows = visible_rows[offset : offset + limit]
        next_offset = offset + limit
        return {
            "conversations": [
                conversation_to_response(row).model_dump(mode="json") for row in page_rows
            ],
            "next_cursor": (
                _encode_cursor({"tool": "list_conversations", "offset": next_offset})
                if next_offset < len(visible_rows)
                else None
            ),
        }

    async def search_conversations_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        user_email = _user(context)
        q = str(arguments.get("q") or "").strip()
        if not q:
            raise ValueError("Search query cannot be empty")
        health = await intaris.search_health(user_email=user_email)
        if not health.enabled:
            return {"error": "search_disabled", "matches": [], "notes": health.notes}
        limit = min(max(int(arguments.get("limit") or 20), 1), 50)
        filters = SearchRequestFilters(
            agent_id=arguments.get("agent_id"),
            from_ts=arguments.get("from_ts"),
            to_ts=arguments.get("to_ts"),
        )
        result = await intaris.search_sessions(
            SearchSessionsRequest(
                q=q,
                filters=filters,
                kinds=arguments.get("kinds"),
                mode=str(arguments.get("mode") or "auto"),
                limit=min(_SEARCH_SESSIONS_MAX_LIMIT, limit * _SEARCH_SESSIONS_OVERFETCH_FACTOR),
                cursor=arguments.get("cursor"),
            ),
            user_email=user_email,
        )
        async with session_factory() as session:
            display_min_score = float(
                await get_setting_value(session, "search.display_min_score", 0.2)
            )
            matches = await join_session_matches(
                session,
                user_email=user_email,
                matches=result.sessions,
                project_id=arguments.get("project_id"),
                status="all",
                context_type=arguments.get("context_type"),
                min_score=display_min_score,
                query=q,
            )
        truncated_after_join = len(matches) > limit
        matches = matches[:limit]
        return {
            "matches": [match.model_dump(mode="json") for match in matches],
            "next_cursor": None if truncated_after_join else result.next_cursor,
            "total_estimated": result.total_estimated,
            "backend": result.backend.model_dump(mode="json"),
        }

    async def read_conversation_messages_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        user_email = _user(context)
        conversation_id = arguments.get("conversation_id") or _current_conversation_id(context)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation_id is required outside an active conversation")
        limit = min(max(int(arguments.get("limit") or 50), 1), 200)
        read_types = _read_types(arguments)
        include_truncation = bool(arguments.get("include_content_truncation", True))
        anchor = arguments.get("anchor") if isinstance(arguments.get("anchor"), dict) else {}
        async with session_factory() as session:
            conversation = await get_conversation(session, conversation_id)
            if (
                conversation is None
                or conversation.user_email != user_email
                or conversation.status == "deleted"
            ):
                raise ValueError("Conversation not found")
            session_rows = await list_conversation_sessions(session, conversation_id)
        if not session_rows:
            return {
                "conversation_id": conversation_id,
                "events": [],
                "ordering": "chronological",
                "page": {"next_cursor": None, "prev_cursor": None, "anchor_used": anchor or None},
            }

        cursor_payload: dict[str, Any] | None = None
        if arguments.get("cursor"):
            cursor_payload = _decode_cursor(str(arguments["cursor"]))
            if cursor_payload.get("tool") != "read_conversation_messages":
                raise ValueError("Invalid cursor")
            cursor_session_id = cursor_payload.get("sid")
            cursor_seq = cursor_payload.get("seq")
            cursor_dir = cursor_payload.get("dir")
            if (
                not isinstance(cursor_session_id, str)
                or _session_index(session_rows, cursor_session_id) is None
            ):
                raise ValueError("Invalid cursor")
            if isinstance(cursor_seq, bool) or not isinstance(cursor_seq, int) or cursor_seq < 0:
                raise ValueError("Invalid cursor")
            if cursor_dir not in {"f", "b"}:
                raise ValueError("Invalid cursor")
            anchor = {
                "kind": "after" if cursor_dir == "f" else "before",
                "session_id": cursor_session_id,
                "seq": cursor_seq,
            }

        anchor_kind, anchor_index = _validate_anchor(anchor, session_rows)
        events: list[dict[str, Any]] = []
        has_more_forward = False
        has_more_backward = False
        if anchor_kind == "latest":
            for row in reversed(session_rows):
                result = await intaris.read_events(
                    row.intaris_session_id or row.session_id,
                    last_n=limit - len(events),
                    types=read_types,
                    allow_missing_stream=True,
                )
                row_events = _tag_session_events(row, list(result.events))
                events = [*row_events, *events]
                if len(row_events) >= limit - len(events) and result.has_more:
                    has_more_backward = True
                if len(events) >= limit:
                    has_more_backward = True
                    break
        else:
            seq = anchor.get("seq") if isinstance(anchor.get("seq"), int) else None
            if anchor_kind == "before" and anchor_index is not None and seq is not None:
                remaining = limit
                collected: list[dict[str, Any]] = []
                for row in reversed(session_rows[: anchor_index + 1]):
                    if remaining <= 0:
                        break
                    if row is session_rows[anchor_index]:
                        after_seq = max(0, seq - remaining - 1)
                        read_limit = remaining + 1
                        result = await intaris.read_events(
                            row.intaris_session_id or row.session_id,
                            after_seq=after_seq,
                            limit=read_limit,
                            types=read_types,
                            allow_missing_stream=True,
                        )
                        row_events = [
                            event for event in list(result.events) if _event_seq(event) < seq
                        ]
                        if row_events and _event_seq(row_events[0]) > 1:
                            has_more_backward = True
                    else:
                        result = await intaris.read_events(
                            row.intaris_session_id or row.session_id,
                            last_n=remaining,
                            types=read_types,
                            allow_missing_stream=True,
                        )
                        row_events = list(result.events)
                        if result.has_more:
                            has_more_backward = True
                    row_events = _tag_session_events(row, row_events)[-remaining:]
                    collected = [*row_events, *collected]
                    remaining = limit - len(collected)
                events = collected[-limit:]
                has_more_forward = True
            else:
                start_index = anchor_index if anchor_index is not None else 0
                for index, row in enumerate(session_rows[start_index:], start=start_index):
                    if len(events) >= limit:
                        has_more_forward = True
                        break
                    row_after_seq = 0
                    read_limit = limit - len(events)
                    if index == start_index and seq is not None:
                        if anchor_kind == "after":
                            row_after_seq = seq
                        elif anchor_kind == "around":
                            before = _non_negative_int(anchor.get("before"), 5)
                            after = _non_negative_int(anchor.get("after"), 5)
                            row_after_seq = max(0, seq - before - 1)
                            read_limit = before + after + 1
                    result = await intaris.read_events(
                        row.intaris_session_id or row.session_id,
                        after_seq=row_after_seq,
                        limit=read_limit,
                        types=read_types,
                        allow_missing_stream=True,
                    )
                    row_events = list(result.events)
                    if seq is not None:
                        if anchor_kind == "after" and index == start_index:
                            row_events = [event for event in row_events if _event_seq(event) > seq]
                        elif anchor_kind == "around" and index == start_index:
                            before = _non_negative_int(anchor.get("before"), 5)
                            after = _non_negative_int(anchor.get("after"), 5)
                            row_events = [
                                event
                                for event in row_events
                                if seq - before <= _event_seq(event) <= seq + after
                            ]
                        elif anchor_kind == "around":
                            row_events = []
                    row_events = _tag_session_events(row, row_events)
                    events.extend(row_events)
                    if result.has_more or len(events) >= limit:
                        has_more_forward = result.has_more or index < len(session_rows) - 1
                        break
                if start_index > 0 or (seq is not None and seq > 0):
                    has_more_backward = True

        events = events[:limit]
        serialized = serialize_event_rows(
            events,
            log_label="conversation_tool_read",
            log_context={"conversation_id": conversation_id},
        )
        response_events: list[dict[str, Any]] = []
        for item in serialized:
            payload = item.model_dump(mode="json")
            data = payload.get("data") if isinstance(payload, dict) else None
            event_data = data if isinstance(data, dict) else {}
            session_id = event_data.get("session_id")
            content, truncated = _truncate_content(
                _event_content(payload),
                include_truncation=include_truncation,
            )
            seq_value = item.seq
            response_events.append(
                {
                    "session_id": session_id,
                    "seq": seq_value,
                    "kind": item.type,
                    "role": item.data.get("role") if isinstance(item.data, dict) else None,
                    "ts": item.timestamp,
                    "content": content,
                    "content_truncated": truncated,
                    "anchor": f"{session_id}:{seq_value}"
                    if session_id and seq_value is not None
                    else None,
                }
            )
        first_event = response_events[0] if response_events else None
        last_event = response_events[-1] if response_events else None
        return {
            "conversation_id": conversation_id,
            "events": response_events,
            "ordering": "chronological",
            "page": {
                "next_cursor": (
                    _encode_cursor(
                        {
                            "tool": "read_conversation_messages",
                            "sid": last_event["session_id"],
                            "seq": last_event["seq"],
                            "dir": "f",
                        }
                    )
                    if has_more_forward and last_event is not None
                    else None
                ),
                "prev_cursor": (
                    _encode_cursor(
                        {
                            "tool": "read_conversation_messages",
                            "sid": first_event["session_id"],
                            "seq": first_event["seq"],
                            "dir": "b",
                        }
                    )
                    if has_more_backward and first_event is not None
                    else None
                ),
                "anchor_used": {"cursor": cursor_payload}
                if cursor_payload is not None
                else anchor or None,
            },
        }

    async def summarize_conversation_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        strategy = compaction_strategy
        if strategy is None and isinstance(context.shared_runtime_metadata, dict):
            strategy = context.shared_runtime_metadata.get("compaction_strategy")
        if strategy is None:
            raise ValueError("Compaction strategy is unavailable")

        user_email = _user(context)
        conversation_id = arguments.get("conversation_id") or _current_conversation_id(context)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation_id is required outside an active conversation")
        requested_session_id = arguments.get("session_id")
        if requested_session_id is not None and not isinstance(requested_session_id, str):
            raise ValueError("session_id must be a string")

        async with session_factory() as session:
            conversation = await get_conversation(session, conversation_id)
            if (
                conversation is None
                or conversation.user_email != user_email
                or conversation.status == "deleted"
            ):
                raise ValueError("Conversation not found")
            session_rows = await list_conversation_sessions(session, conversation_id)
            session_row = None
            if requested_session_id:
                for row in session_rows:
                    if (
                        row.session_id == requested_session_id
                        or row.intaris_session_id == requested_session_id
                    ):
                        session_row = row
                        break
                if session_row is None:
                    raise ValueError("session_id does not belong to this conversation")
            elif getattr(conversation, "active_session_id", None):
                session_row = await get_session_row(session, conversation.active_session_id)
                if session_row is not None and session_row.conversation_id != conversation_id:
                    session_row = None
            if session_row is None and session_rows:
                session_row = session_rows[-1]

        if session_row is None:
            return {
                "conversation_id": conversation_id,
                "session_id": None,
                "intaris_session_id": None,
                "summary": None,
                "format": "compaction_summary_v1",
                "generated": False,
                "method": "noop",
                "message": "Conversation has no sessions to summarize.",
            }

        session_model = _session_model(session_row)
        intaris_session_id = session_model.intaris_session_id or session_model.session_id
        raw_event_read = await intaris.read_events(
            intaris_session_id,
            after_seq=0,
            allow_missing_stream=True,
        )
        raw_events = [event for event in list(raw_event_read.events) if isinstance(event, dict)]
        events = [_cached_event(event) for event in raw_events]
        last_compaction_summary, last_compaction_seq = _latest_compaction_summary(events)
        use_cached = bool(arguments.get("use_cached", True))
        if use_cached and last_compaction_summary:
            return {
                "conversation_id": conversation_id,
                "session_id": session_model.session_id,
                "intaris_session_id": intaris_session_id,
                "summary": last_compaction_summary,
                "format": "compaction_summary_v1",
                "generated": False,
                "method": "cached_compaction_summary",
                "turns_summarized": 0,
                "tokens_before": 0,
                "tokens_after": 0,
                "tail_start_seq": None,
            }

        result = await strategy.preview_summary_from_events(
            session_model,
            events=[event for event in events if event.seq > last_compaction_seq],
            last_compaction_summary=last_compaction_summary,
            trigger="tool_preview",
            long_lived_chat=is_long_lived_chat_context(_conversation_context(conversation)),
        )
        return {
            "conversation_id": conversation_id,
            "session_id": session_model.session_id,
            "intaris_session_id": session_model.intaris_session_id or session_model.session_id,
            "summary": result.summary,
            "format": "compaction_summary_v1",
            "generated": result.compacted,
            "method": result.method,
            "turns_summarized": result.turns_compacted,
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "tail_start_seq": result.tail_start_seq,
        }

    return {
        "list_conversations": list_conversations_handler,
        "search_conversations": search_conversations_handler,
        "read_conversation_messages": read_conversation_messages_handler,
        "summarize_conversation": summarize_conversation_handler,
    }
