"""Built-in conversation search and reading tools."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.serializers import conversation_to_response, serialize_event_rows
from cognis.core.conversation_search import join_session_matches
from cognis.models.search import SearchRequestFilters, SearchSessionsRequest
from cognis.models.tool import ToolCapability, ToolDefinition, ToolSource
from cognis.store.queries import (
    get_conversation,
    list_conversation_sessions,
    list_conversations,
)
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")
_SEARCH_SESSIONS_OVERFETCH_FACTOR = 3
_SEARCH_SESSIONS_MAX_LIMIT = 100


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
        "status": {"type": "string", "enum": ["active", "archived", "all"]},
        "limit": {"type": "integer", "default": 25, "maximum": 100},
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
    "Read user and assistant messages from one owned conversation. Defaults to the current conversation.",
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
        "limit": {"type": "integer", "default": 50, "maximum": 200},
    },
)


def conversation_tools() -> list[ToolDefinition]:
    return [LIST_CONVERSATIONS_TOOL, SEARCH_CONVERSATIONS_TOOL, READ_CONVERSATION_MESSAGES_TOOL]


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


def build_conversation_tool_handlers(
    session_factory: async_sessionmaker[AsyncSession],
    intaris: Any,
) -> dict[str, Any]:
    async def list_conversations_handler(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        limit = min(max(int(arguments.get("limit") or 25), 1), 100)
        async with session_factory() as session:
            rows = await list_conversations(
                session,
                _user(context),
                agent_id=arguments.get("agent_id"),
                project_id=arguments.get("project_id"),
                status=str(arguments.get("status") or "active"),
            )
        visible_rows = [row for row in rows if getattr(row, "status", None) != "deleted"]
        return {
            "conversations": [
                conversation_to_response(row).model_dump(mode="json")
                for row in visible_rows[:limit]
            ],
            "next_cursor": None,
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
            matches = await join_session_matches(
                session,
                user_email=user_email,
                matches=result.sessions,
                project_id=arguments.get("project_id"),
                status="all",
                context_type=arguments.get("context_type"),
            )
        truncated_after_join = len(matches) > limit
        matches = matches[:limit]
        return {
            "matches": [match.model_dump(mode="json") for match in matches],
            "next_cursor": None if truncated_after_join else result.next_cursor,
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
        anchor = arguments.get("anchor") if isinstance(arguments.get("anchor"), dict) else {}
        anchor_kind = str(anchor.get("kind") or "latest")
        async with session_factory() as session:
            conversation = await get_conversation(session, conversation_id)
            if (
                conversation is None
                or conversation.user_email != user_email
                or conversation.status == "deleted"
            ):
                raise ValueError("Conversation not found")
            session_rows = await list_conversation_sessions(session, conversation_id)
        selected_rows = session_rows
        if anchor.get("session_id"):
            selected_rows = [
                row
                for row in session_rows
                if row.session_id == anchor.get("session_id")
                or row.intaris_session_id == anchor.get("session_id")
            ]
        events: list[dict[str, Any]] = []
        if anchor_kind == "latest":
            for row in reversed(session_rows):
                result = await intaris.read_events(
                    row.intaris_session_id or row.session_id,
                    last_n=limit - len(events),
                    types=["user_message", "assistant_message"],
                    allow_missing_stream=True,
                )
                events = [*list(result.events), *events]
                if len(events) >= limit:
                    break
        else:
            for row in selected_rows:
                seq = anchor.get("seq") if isinstance(anchor.get("seq"), int) else None
                before = int(anchor.get("before") or 5)
                after = int(anchor.get("after") or 5)
                if anchor_kind == "after" and seq is not None:
                    after_seq = seq
                    read_limit = limit
                elif anchor_kind == "before" and seq is not None:
                    after_seq = max(0, seq - limit - 1)
                    read_limit = limit + 1
                elif anchor_kind == "around" and seq is not None:
                    after_seq = max(0, seq - before - 1)
                    read_limit = before + after + 1
                else:
                    after_seq = 0
                    read_limit = limit - len(events)
                result = await intaris.read_events(
                    row.intaris_session_id or row.session_id,
                    after_seq=after_seq,
                    limit=read_limit,
                    types=["user_message", "assistant_message"],
                    allow_missing_stream=True,
                )
                row_events = list(result.events)
                if seq is not None and anchor_kind in {"around", "after", "before"}:
                    if anchor_kind == "after":
                        row_events = [
                            event for event in row_events if int(event.get("seq") or 0) > seq
                        ]
                    elif anchor_kind == "before":
                        row_events = [
                            event for event in row_events if int(event.get("seq") or 0) < seq
                        ]
                    else:
                        before = int(anchor.get("before") or 5)
                        after = int(anchor.get("after") or 5)
                        row_events = [
                            event
                            for event in row_events
                            if seq - before <= int(event.get("seq") or 0) <= seq + after
                        ]
                events.extend(row_events)
                if len(events) >= limit:
                    break
        serialized = serialize_event_rows(events[:limit], log_label="conversation_tool_read")
        return {
            "conversation_id": conversation_id,
            "events": [
                {
                    "seq": item.seq,
                    "kind": item.type,
                    "role": item.data.get("role") if isinstance(item.data, dict) else None,
                    "ts": item.timestamp,
                    "content": _event_content(item.model_dump(mode="json")),
                    "content_truncated": False,
                }
                for item in serialized
            ],
            "ordering": "chronological",
            "page": {"next_cursor": None, "prev_cursor": None, "anchor_used": anchor or None},
        }

    return {
        "list_conversations": list_conversations_handler,
        "search_conversations": search_conversations_handler,
        "read_conversation_messages": read_conversation_messages_handler,
    }
