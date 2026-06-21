"""Conversation routes."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response, StreamingResponse

from cognis.api.common import (
    api_exception,
    check_agent_access,
    check_project_access,
    decode_cursor,
    encode_cursor,
    forbid_mutation_for_viewer,
    require_current_user,
    require_resource_owner,
)
from cognis.api.models import (
    AgentDirectChatResponse,
    ConversationCreateRequest,
    ConversationOpenRequest,
    ConversationResolveRequest,
    ConversationResponse,
    ConversationTitleSuggestionResponse,
    ConversationUpdateRequest,
    CursorPage,
    ManagedConversationActionRequest,
    ManagedConversationActionResponse,
    MessageEventResponse,
    MessageHistoryResponse,
    QueuedMessageResponse,
    QueuedMessagesResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionEventsResponse,
    SessionResponse,
    SidebarProjectionResponse,
    TimelineProjectionResponse,
    ToolOutputChunkResponse,
    ToolOutputPageResponse,
    UpdateQueuedMessageRequest,
)
from cognis.api.serializers import (
    agent_to_response,
    conversation_to_response,
    serialize_event_rows,
    session_to_response,
)
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.attachment_utils import hydrate_attachment_refs
from cognis.core.conversation_state import snapshot_for_conversation
from cognis.core.managed_conversations import last_managed_conversation_user_message_for_retry
from cognis.core.title_policy import latest_intaris_title_from_platform_data
from cognis.core.turn_scheduler import TurnError
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, SessionEvent, SessionModel
from cognis.store.queries import (
    create_managed_conversation_link,
    get_agent,
    get_agent_direct_conversation,
    get_conversation,
    get_latest_active_conversation_for_agent,
    get_latest_root_session_for_conversation,
    get_managed_conversation_link_for_target,
    get_project,
    get_root_session_chain,
    get_root_session_chain_page,
    get_session_row,
    get_user_ui_state_value,
    list_conversation_context_types,
    list_conversation_sessions,
    list_conversations,
    list_pending_notification_types_by_conversation,
    list_sessions_by_ids,
    list_visible_agents,
    mark_artifacts_attached,
    mark_conversation_read,
    update_conversation_context_data,
    update_managed_conversation_link,
    upsert_user_ui_state,
)

logger = get_logger(__name__)

_CONVERSATION_MESSAGES_CURSOR_VERSION = 1
_MANAGED_CONVERSATION_CONTEXT_TYPES = {"agent_work", "managed_agent_conversation"}
_CHAT_LAST_OPENED_UI_STATE_PREFIX = "chat.last_opened"


def _agent_definition_from_row(row: object) -> AgentDefinition:
    return AgentDefinition.model_validate(agent_to_response(row).model_dump())


def _encode_messages_cursor(session_id: str, seq: int) -> str:
    payload = {
        "v": _CONVERSATION_MESSAGES_CURSOR_VERSION,
        "sid": session_id,
        "seq": max(0, seq),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_messages_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise api_exception(400, "invalid_cursor", "Invalid history cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != _CONVERSATION_MESSAGES_CURSOR_VERSION:
        raise api_exception(400, "invalid_cursor", "Invalid history cursor")
    session_id = payload.get("sid")
    seq = payload.get("seq")
    if not isinstance(session_id, str) or not session_id:
        raise api_exception(400, "invalid_cursor", "Invalid history cursor")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise api_exception(400, "invalid_cursor", "Invalid history cursor")
    return session_id, seq


def _event_seq(event: dict[str, Any]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


def _messages_cursor_anchor_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the oldest real event usable as a history cursor anchor."""
    for event in events:
        if _event_seq(event) <= 0:
            continue
        data = event.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
            continue
        return event
    return None


def _timeline_event_identity(event: MessageEventResponse) -> tuple[str | None, int | None, str]:
    session_id = event.data.get("session_id")
    sid = session_id if isinstance(session_id, str) and session_id else None
    seq = event.seq
    eid = f"{sid}:{seq}" if sid else str(seq)
    return sid, seq, eid


def _project_event_attachments(data: dict[str, Any]) -> list[Any]:
    attachments = data.get("attachments")
    return attachments if isinstance(attachments, list) else []


def _project_event_turn_id(data: dict[str, Any]) -> str | None:
    turn_id = data.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _project_visible_system_message(data: dict[str, Any]) -> bool:
    if isinstance(data.get("notice_id"), str) and data["notice_id"]:
        return True
    if data.get("kind") == "turn_initiated":
        return True
    return data.get("event") == "turn_initiated"


def _project_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int | float) else None


def _next_assistant_phase_index(items: list[dict[str, Any]], turn_id: str | None) -> int | None:
    if not turn_id:
        return None
    highest = -1
    matching = 0
    for item in items:
        if item.get("kind") != "message" or item.get("role") != "assistant":
            continue
        if item.get("turnId") != turn_id:
            continue
        matching += 1
        phase = item.get("assistantPhaseIndex")
        if isinstance(phase, int):
            highest = max(highest, phase)
    return highest + 1 if highest >= 0 else matching


def _strip_none_values(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _project_timeline_events(
    events: list[MessageEventResponse],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    tool_index_by_call_id: dict[str, int] = {}
    system_notice_index_by_id: dict[str, int] = {}
    open_assistant_index_by_turn: dict[str, int] = {}

    def close_assistant_phase(turn_id: str | None) -> None:
        if turn_id:
            open_assistant_index_by_turn.pop(turn_id, None)
        else:
            open_assistant_index_by_turn.clear()

    def append_notice(
        *,
        item_id: str,
        title: str,
        description: str,
        timestamp: Any,
        tone: str = "info",
    ) -> None:
        close_assistant_phase(turn_id)
        items.append(
            {
                "id": item_id,
                "kind": "notice",
                "title": title,
                "description": description,
                "tone": tone,
                "timestamp": timestamp,
            }
        )

    def upsert_delegation(item: dict[str, Any]) -> None:
        def status_rank(status: Any) -> int:
            return {
                "queued": 0,
                "started": 1,
                "running": 2,
                "paused": 3,
                "completed": 4,
                "failed": 4,
                "cancelled": 4,
            }.get(str(status or ""), -1)

        def is_generic_label(label: Any) -> bool:
            return str(label or "") in {"", "Sub-session", "Background task"}

        close_assistant_phase(turn_id)
        item_id = item.get("id")
        existing_index = next(
            (
                index
                for index, existing in enumerate(items)
                if existing.get("id") == item_id and existing.get("kind") == "delegation"
            ),
            None,
        )
        if existing_index is None:
            items.append(_strip_none_values(item))
            return
        existing = dict(items[existing_index])
        incoming = _strip_none_values(item)
        existing_status = existing.get("status")
        incoming_status = incoming.get("status")
        existing_is_terminal = status_rank(existing_status) >= 4
        incoming_is_non_terminal = status_rank(incoming_status) < 4
        for key, value in incoming.items():
            if key == "status":
                if status_rank(incoming_status) >= status_rank(existing_status):
                    existing[key] = value
                continue
            if key == "result" and status_rank(existing_status) >= 4 and not value:
                continue
            if key == "taskLabel":
                if (
                    is_generic_label(existing.get("taskLabel")) and not is_generic_label(value)
                ) or not existing.get("taskLabel"):
                    existing[key] = value
                continue
            if existing_is_terminal and incoming_is_non_terminal:
                if key in {"agentId", "usedAgentId"} and not existing.get(key):
                    existing[key] = value
                continue
            existing[key] = value
        items[existing_index] = _strip_none_values(existing)

    def shift_projected_indices(insert_index: int) -> None:
        for index_by_id in (
            tool_index_by_call_id,
            system_notice_index_by_id,
            open_assistant_index_by_turn,
        ):
            for key, index in list(index_by_id.items()):
                if index >= insert_index:
                    index_by_id[key] = index + 1

    def find_open_thinking_index(message_id: str) -> int | None:
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if item.get("kind") == "thinking" and item.get("messageId") == message_id:
                return index
            if item.get("kind") == "message" and item.get("role") == "assistant":
                if item.get("messageId") != message_id:
                    return None
                continue
            if item.get("kind") == "tool_call":
                return None
        return None

    def append_thinking_item(item: dict[str, Any], *, turn_id: str | None) -> None:
        message_id = item.get("messageId")
        existing_index = find_open_thinking_index(str(message_id)) if message_id else None
        if existing_index is not None:
            existing_blocks = items[existing_index].setdefault("blocks", [])
            new_blocks = item.get("blocks")
            if isinstance(existing_blocks, list) and isinstance(new_blocks, list):
                existing_blocks.extend(new_blocks)
            return
        assistant_index = open_assistant_index_by_turn.get(turn_id or "") if turn_id else None
        if assistant_index is not None and 0 <= assistant_index < len(items):
            items.insert(assistant_index, _strip_none_values(item))
            shift_projected_indices(assistant_index)
            return
        items.append(_strip_none_values(item))

    for event in events:
        data = event.data or {}
        sid, seq, eid = _timeline_event_identity(event)
        timestamp = event.timestamp
        turn_id = _project_event_turn_id(data)
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        attachments = _project_event_attachments(data)

        if event.type == "user_message":
            close_assistant_phase(turn_id)
            item: dict[str, Any] = {
                "id": f"event:{eid}:user",
                "kind": "message",
                "sessionId": sid,
                "role": "user",
                "content": content,
                "seq": seq,
                "timestamp": timestamp,
                "turnId": turn_id,
                "attachments": attachments,
            }
            if isinstance(data.get("chat_mode"), str):
                item["chatMode"] = data["chat_mode"]
            if isinstance(data.get("chat_mode_source"), str):
                item["chatModeSource"] = data["chat_mode_source"]
            items.append(_strip_none_values(item))
            continue

        if event.type == "assistant_message":
            if content.strip() or attachments:
                message_id = (
                    data.get("message_id") if isinstance(data.get("message_id"), str) else turn_id
                )
                existing_index = open_assistant_index_by_turn.get(turn_id or "")
                existing = (
                    items[existing_index]
                    if existing_index is not None
                    and 0 <= existing_index < len(items)
                    and items[existing_index].get("kind") == "message"
                    and items[existing_index].get("role") == "assistant"
                    else None
                )
                if existing is not None and existing.get("messageId") == message_id:
                    existing_content = existing.get("content")
                    if isinstance(existing_content, str) and existing_content:
                        existing["content"] = (
                            content
                            if content.startswith(existing_content)
                            else f"{existing_content}\n\n{content}"
                        )
                    else:
                        existing["content"] = content
                    existing["seq"] = seq
                    existing["timestamp"] = timestamp
                    existing["attachments"] = attachments or existing.get("attachments", [])
                    if isinstance(data.get("runtime"), dict):
                        existing["runtime"] = data["runtime"]
                    if isinstance(data.get("finish_reason"), str):
                        existing["finishReason"] = data["finish_reason"]
                    existing["partial"] = data.get("partial") is True
                    continue

                phase = _next_assistant_phase_index(items, turn_id)
                item = {
                    "id": f"event:{eid}:assistant",
                    "kind": "message",
                    "sessionId": sid,
                    "role": "assistant",
                    "content": content,
                    "seq": seq,
                    "timestamp": timestamp,
                    "turnId": turn_id,
                    "messageId": message_id,
                    "attachments": attachments,
                    "partial": data.get("partial") is True,
                    "finishReason": data.get("finish_reason")
                    if isinstance(data.get("finish_reason"), str)
                    else None,
                    "assistantPhaseIndex": phase,
                }
                if isinstance(data.get("chat_mode"), str):
                    item["chatMode"] = data["chat_mode"]
                if isinstance(data.get("chat_mode_source"), str):
                    item["chatModeSource"] = data["chat_mode_source"]
                if isinstance(data.get("runtime"), dict):
                    item["runtime"] = data["runtime"]
                items.append(_strip_none_values(item))
                if turn_id:
                    open_assistant_index_by_turn[turn_id] = len(items) - 1
            continue

        if event.type == "system_message":
            close_assistant_phase(turn_id)
            if not _project_visible_system_message(data):
                continue
            message = content or data.get("text")
            if isinstance(message, str) and message:
                notice_id = (
                    data.get("notice_id") if isinstance(data.get("notice_id"), str) else None
                )
                item = _strip_none_values(
                    {
                        "id": f"system:{notice_id}" if notice_id else f"system:{eid}",
                        "kind": "system_message",
                        "text": message,
                        "noticeId": notice_id,
                        "noticeKind": data.get("kind")
                        if isinstance(data.get("kind"), str)
                        else None,
                        "noticeScope": data.get("scope")
                        if isinstance(data.get("scope"), str)
                        else None,
                        "timestamp": timestamp,
                    }
                )
                if notice_id and notice_id in system_notice_index_by_id:
                    items[system_notice_index_by_id[notice_id]] = item
                else:
                    if notice_id:
                        system_notice_index_by_id[notice_id] = len(items)
                    items.append(item)
            continue

        if event.type == "tool_call":
            close_assistant_phase(turn_id)
            tool_name = str(data.get("name") or data.get("tool_name") or "unknown")
            if tool_name in {"delegate", "fork"}:
                continue
            call_id = str(data.get("call_id") or f"tc-{eid}")
            arguments = data.get("arguments")
            visible_name = data.get("visible_name")
            canonical_name = data.get("canonical_name")
            item = {
                "id": f"tool:{call_id}",
                "kind": "tool_call",
                "callId": call_id,
                "sessionId": sid,
                "turnId": turn_id,
                "toolName": tool_name,
                "status": data.get("status") if isinstance(data.get("status"), str) else "started",
                "timestamp": timestamp,
            }
            if isinstance(visible_name, str) and visible_name:
                item["displayToolName"] = visible_name
            if isinstance(canonical_name, str) and canonical_name:
                item["canonicalToolName"] = canonical_name
            if isinstance(arguments, dict):
                item["arguments"] = arguments
            elif isinstance(arguments, str):
                try:
                    parsed_arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed_arguments = None
                item["arguments"] = (
                    parsed_arguments if isinstance(parsed_arguments, dict) else {"_raw": arguments}
                )
            tool_index_by_call_id[call_id] = len(items)
            items.append(_strip_none_values(item))
            continue

        if event.type == "tool_result":
            close_assistant_phase(turn_id)
            call_id = str(data.get("call_id") or f"tc-{eid}")
            existing_index = tool_index_by_call_id.get(call_id)
            base: dict[str, Any]
            if existing_index is not None and 0 <= existing_index < len(items):
                base = dict(items[existing_index])
            else:
                base = {
                    "id": f"tool:{call_id}",
                    "kind": "tool_call",
                    "callId": call_id,
                    "sessionId": sid,
                    "toolName": str(data.get("name") or data.get("tool_name") or "unknown"),
                    "timestamp": timestamp,
                    "reconstructed": True,
                }
                tool_index_by_call_id[call_id] = len(items)
                items.append(base)
                existing_index = len(items) - 1
            base.update(
                {
                    "turnId": base.get("turnId") or turn_id,
                    "sessionId": base.get("sessionId") or sid,
                    "status": "failed" if data.get("is_error") else "completed",
                    "result": data.get("result") if isinstance(data.get("result"), str) else None,
                    "isError": data.get("is_error")
                    if isinstance(data.get("is_error"), bool)
                    else None,
                    "durationMs": _project_number(data.get("duration_ms")),
                    "attachments": _project_event_attachments(data) or base.get("attachments"),
                    "fileDiffs": data.get("file_diffs")
                    if isinstance(data.get("file_diffs"), list)
                    else base.get("fileDiffs"),
                    "outputSize": data.get("output_size")
                    if isinstance(data.get("output_size"), int)
                    else None,
                    "truncated": data.get("truncated")
                    if isinstance(data.get("truncated"), bool)
                    else None,
                    "agentVisibleTruncated": data.get("agent_visible_truncated")
                    if isinstance(data.get("agent_visible_truncated"), bool)
                    else None,
                    "transportTruncated": data.get("transport_truncated")
                    if isinstance(data.get("transport_truncated"), bool)
                    else None,
                    "hasFullOutput": data.get("has_full_output")
                    if isinstance(data.get("has_full_output"), bool)
                    else None,
                    "recoveryCallId": data.get("recovery_call_id")
                    if isinstance(data.get("recovery_call_id"), str)
                    else None,
                    "toolOutputArtifactId": data.get("tool_output_artifact_id")
                    if isinstance(data.get("tool_output_artifact_id"), str)
                    else None,
                    "anchorsAvailable": data.get("anchors_available")
                    if isinstance(data.get("anchors_available"), bool)
                    else None,
                    "anchorCount": data.get("anchor_count")
                    if isinstance(data.get("anchor_count"), int)
                    else None,
                    "evaluation": data.get("evaluation")
                    if isinstance(data.get("evaluation"), dict)
                    else None,
                    "tool_output_presentation": data.get("tool_output_presentation")
                    if isinstance(data.get("tool_output_presentation"), dict)
                    else None,
                }
            )
            items[existing_index] = _strip_none_values(base)
            continue

        if event.type == "history_gap":
            close_assistant_phase(turn_id)
            reason = str(data.get("reason") or "unknown")
            descriptions = {
                "stream_missing": "A session event stream was missing in Intaris, so part of this history could not be loaded.",
                "read_failed": "A session event stream could not be read from Intaris, so part of this history may be incomplete.",
                "lineage_truncated": "Older conversation lineage was truncated during history bootstrap. Load the session directly for more detail.",
                "bootstrap_cap_reached": "History bootstrap reached the configured safety cap. Refresh or inspect the session directly to load more.",
            }
            items.append(
                {
                    "id": f"history-gap:{eid}:{reason}",
                    "kind": "notice",
                    "title": "History incomplete",
                    "description": descriptions.get(
                        reason,
                        "Some persisted history could not be loaded completely.",
                    ),
                    "tone": "warning",
                    "timestamp": timestamp,
                }
            )
            continue

        if event.type == "compaction_summary":
            close_assistant_phase(turn_id)
            marker_role = (
                data.get("marker_role") if isinstance(data.get("marker_role"), str) else None
            )
            method = data.get("method") if isinstance(data.get("method"), str) else "unknown"
            if (
                data.get("timeline_visible") is False
                or marker_role == "context_seed"
                or method == "rotation"
            ):
                continue
            summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
            session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else sid
            source_session_id = (
                data.get("source_session_id")
                if isinstance(data.get("source_session_id"), str)
                else None
            )
            items.append(
                _strip_none_values(
                    {
                        "id": f"compaction:{source_session_id}:{session_id}"
                        if session_id and source_session_id
                        else f"compaction:{eid}",
                        "kind": "compaction",
                        "status": "compacted",
                        "sessionId": session_id,
                        "previousSessionId": source_session_id,
                        "summaryPreview": summary[:500],
                        "summary": summary,
                        "method": method,
                        "turnsCompacted": data.get("turns_compacted")
                        if isinstance(data.get("turns_compacted"), int)
                        else 0,
                        "trigger": data.get("trigger")
                        if isinstance(data.get("trigger"), str)
                        else None,
                        "reason": data.get("reason")
                        if isinstance(data.get("reason"), str)
                        else None,
                        "previousUsagePercentage": _project_number(
                            data.get("previous_usage_percentage")
                        ),
                        "effectiveUsagePercentage": _project_number(
                            data.get("effective_usage_percentage")
                        ),
                        "hardPressureExceeded": data.get("hard_pressure_exceeded") is True,
                        "usedTimeoutFallback": data.get("used_timeout_fallback") is True,
                        "timestamp": timestamp,
                    }
                )
            )
            continue

        if event.type in {
            "workflow_composed",
        }:
            close_assistant_phase(turn_id)
            workflow_id = str(data.get("workflow_id") or "")
            items.append(
                _strip_none_values(
                    {
                        "id": f"workflow-composed:{eid}",
                        "kind": "workflow_composed",
                        "workflowId": workflow_id,
                        "workflowName": str(data.get("workflow_name") or workflow_id or "Workflow"),
                        "lifecycle": str(data.get("lifecycle") or "ephemeral"),
                        "taskId": data.get("task_id")
                        if isinstance(data.get("task_id"), str)
                        else None,
                        "scheduleId": data.get("schedule_id")
                        if isinstance(data.get("schedule_id"), str)
                        else None,
                        "steps": [step for step in data.get("steps", []) if isinstance(step, str)]
                        if isinstance(data.get("steps"), list)
                        else [],
                        "timestamp": timestamp,
                    }
                )
            )
            continue

        if event.type == "assistant_thinking":
            thinking_content = data.get("content") if isinstance(data.get("content"), str) else ""
            thinking_source = (
                data.get("reasoning_source")
                if isinstance(data.get("reasoning_source"), str)
                else "summary"
            )
            thinking_title = (
                data.get("title")
                if isinstance(data.get("title"), str) and data.get("title")
                else "Thinking"
            )
            if (
                thinking_source == "reasoning"
                and thinking_title == "Reasoning"
                and thinking_content.lstrip().startswith("User message")
            ):
                continue
            message_id = (
                data.get("message_id")
                if isinstance(data.get("message_id"), str)
                else turn_id or eid
            )
            block_id = (
                data.get("block_id") if isinstance(data.get("block_id"), str) else f"thk_{eid}"
            )
            append_thinking_item(
                _strip_none_values(
                    {
                        "id": f"thinking:{message_id}:{block_id}",
                        "kind": "thinking",
                        "sessionId": sid,
                        "messageId": message_id,
                        "turnId": turn_id,
                        "blocks": [
                            _strip_none_values(
                                {
                                    "block_id": block_id,
                                    "title": thinking_title,
                                    "content": thinking_content,
                                    "source": thinking_source,
                                    "complete": True,
                                    "startedAt": data.get("started_at")
                                    if isinstance(data.get("started_at"), str)
                                    else None,
                                    "completedAt": data.get("completed_at")
                                    if isinstance(data.get("completed_at"), str)
                                    else None,
                                    "durationMs": _project_number(data.get("duration_ms")),
                                    "providerBlockIndex": data.get("provider_block_index")
                                    if isinstance(data.get("provider_block_index"), int)
                                    else None,
                                }
                            )
                        ],
                        "streaming": False,
                        "activeTitle": None,
                        "timestamp": timestamp,
                    }
                ),
                turn_id=turn_id,
            )
            continue

        if event.type == "reasoning":
            # Generic Intaris reasoning records are searchable diagnostics, not
            # assistant-visible thinking. Provider/model thinking is persisted
            # as assistant_thinking and projected above.
            continue

        if event.type == "developer_message":
            continue

        if event.type == "context_snapshot":
            continue

        if event.type == "delegation":
            child_id = str(data.get("child_session_id") or data.get("call_id") or f"del-{eid}")
            status = str(data.get("status") or "started")
            if status == "success":
                status = "completed"
            elif status not in {
                "queued",
                "started",
                "running",
                "completed",
                "failed",
                "cancelled",
                "paused",
            }:
                status = "started"
            result = None
            if status == "completed":
                result = data.get("result_summary") or data.get("result_content")
            elif status == "failed":
                result = data.get("error") or "Failed"
            upsert_delegation(
                {
                    "id": f"delegation:{child_id}",
                    "kind": "delegation",
                    "taskId": child_id,
                    "taskLabel": str(
                        data.get("label")
                        or data.get("title")
                        or data.get("task_title")
                        or data.get("task")
                        or "Sub-session"
                    ),
                    "agentId": data.get("agent_id")
                    if isinstance(data.get("agent_id"), str)
                    else None,
                    "usedAgentId": data.get("used_agent_id")
                    if isinstance(data.get("used_agent_id"), str)
                    else None,
                    "status": status,
                    "result": str(result) if result else None,
                    "timestamp": timestamp,
                    "todos": data.get("todos") if isinstance(data.get("todos"), list) else None,
                    "toolCallCount": data.get("tool_call_count")
                    if isinstance(data.get("tool_call_count"), int)
                    else data.get("toolCallCount")
                    if isinstance(data.get("toolCallCount"), int)
                    else None,
                    "maxToolCalls": data.get("max_tool_calls")
                    if isinstance(data.get("max_tool_calls"), int)
                    else data.get("maxToolCalls")
                    if isinstance(data.get("maxToolCalls"), int)
                    else None,
                    "lastTool": data.get("last_tool")
                    if isinstance(data.get("last_tool"), str)
                    else data.get("lastTool")
                    if isinstance(data.get("lastTool"), str)
                    else None,
                }
            )
            continue

        if event.type in {"task_result", "task_failed", "task_cancelled"}:
            task_id = str(data.get("task_id") or eid)
            status = {
                "task_result": "completed",
                "task_failed": "failed",
                "task_cancelled": "cancelled",
            }[event.type]
            upsert_delegation(
                {
                    "id": f"delegation:{task_id}",
                    "kind": "delegation",
                    "taskId": task_id,
                    "taskLabel": str(
                        data.get("task_title") or data.get("task_id") or "Background task"
                    ),
                    "agentId": data.get("agent_id")
                    if isinstance(data.get("agent_id"), str)
                    else None,
                    "usedAgentId": data.get("used_agent_id")
                    if isinstance(data.get("used_agent_id"), str)
                    else None,
                    "status": status,
                    "result": data.get("result_summary")
                    if isinstance(data.get("result_summary"), str)
                    else None,
                    "toolCallCount": data.get("tool_call_count")
                    if isinstance(data.get("tool_call_count"), int)
                    else data.get("toolCallCount")
                    if isinstance(data.get("toolCallCount"), int)
                    else None,
                    "maxToolCalls": data.get("max_tool_calls")
                    if isinstance(data.get("max_tool_calls"), int)
                    else data.get("maxToolCalls")
                    if isinstance(data.get("maxToolCalls"), int)
                    else None,
                    "lastTool": data.get("last_tool")
                    if isinstance(data.get("last_tool"), str)
                    else data.get("lastTool")
                    if isinstance(data.get("lastTool"), str)
                    else None,
                    "timestamp": timestamp,
                }
            )
            continue

        if event.type == "lifecycle":
            lifecycle_event = str(data.get("event") or "")
            if lifecycle_event in {"task_result", "task_failed", "task_cancelled"}:
                task_id = str(data.get("task_id") or eid)
                status = {
                    "task_result": "completed",
                    "task_failed": "failed",
                    "task_cancelled": "cancelled",
                }[lifecycle_event]
                upsert_delegation(
                    {
                        "id": f"delegation:{task_id}",
                        "kind": "delegation",
                        "taskId": task_id,
                        "taskLabel": str(
                            data.get("title")
                            or data.get("task_title")
                            or data.get("task_id")
                            or "Background task"
                        ),
                        "agentId": data.get("agent_id")
                        if isinstance(data.get("agent_id"), str)
                        else None,
                        "usedAgentId": data.get("used_agent_id")
                        if isinstance(data.get("used_agent_id"), str)
                        else None,
                        "status": status,
                        "result": data.get("result_summary")
                        if isinstance(data.get("result_summary"), str)
                        else None,
                        "timestamp": timestamp,
                    }
                )
            elif lifecycle_event == "system_notice":
                message = data.get("message") if isinstance(data.get("message"), str) else ""
                if message:
                    close_assistant_phase(turn_id)
                    notice_id = (
                        data.get("notice_id") if isinstance(data.get("notice_id"), str) else None
                    )
                    item = _strip_none_values(
                        {
                            "id": f"system:{notice_id}" if notice_id else f"system:{eid}",
                            "kind": "system_message",
                            "text": message,
                            "noticeId": notice_id,
                            "noticeKind": data.get("kind")
                            if isinstance(data.get("kind"), str)
                            else None,
                            "noticeScope": data.get("scope")
                            if isinstance(data.get("scope"), str)
                            else None,
                            "timestamp": timestamp,
                        }
                    )
                    if notice_id and notice_id in system_notice_index_by_id:
                        items[system_notice_index_by_id[notice_id]] = item
                    else:
                        if notice_id:
                            system_notice_index_by_id[notice_id] = len(items)
                        items.append(item)
            elif lifecycle_event == "workflow_composed":
                close_assistant_phase(turn_id)
                workflow_id = str(data.get("workflow_id") or "")
                items.append(
                    _strip_none_values(
                        {
                            "id": f"workflow-composed:{eid}",
                            "kind": "workflow_composed",
                            "workflowId": workflow_id,
                            "workflowName": str(
                                data.get("workflow_name") or workflow_id or "Workflow"
                            ),
                            "lifecycle": str(data.get("lifecycle") or "ephemeral"),
                            "taskId": data.get("task_id")
                            if isinstance(data.get("task_id"), str)
                            else None,
                            "scheduleId": data.get("schedule_id")
                            if isinstance(data.get("schedule_id"), str)
                            else None,
                            "steps": [
                                step for step in data.get("steps", []) if isinstance(step, str)
                            ]
                            if isinstance(data.get("steps"), list)
                            else [],
                            "timestamp": timestamp,
                        }
                    )
                )
            elif lifecycle_event in {
                "intention_updated",
                "intention_cleared",
                "session_created",
                "session_status_changed",
                "tool_discovery",
            }:
                continue
            else:
                append_notice(
                    item_id=f"event-notice:{eid}",
                    title="Conversation event",
                    description=f"Lifecycle event: {lifecycle_event or 'unknown'}",
                    timestamp=timestamp,
                )
            continue

        if event.type == "evaluation":
            eval_event = str(data.get("event") or "")
            if eval_event == "evaluation_feedback":
                decision = str(data.get("decision") or "unknown")
                feedback = str(data.get("feedback") or "")
                attempt = data.get("attempt") if data.get("attempt") is not None else "?"
                tone = (
                    "info"
                    if decision in {"approved", "approve"}
                    else "error"
                    if decision in {"failed", "reject"}
                    else "warning"
                )
                append_notice(
                    item_id=f"eval:{eid}",
                    title=f"Step Evaluation (attempt {attempt})",
                    description=f"{decision} — {feedback}",
                    timestamp=timestamp,
                    tone=tone,
                )
            else:
                continue
            continue

        if event.type == "session_recovered":
            close_assistant_phase(turn_id)
            session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else eid
            items.append(
                {
                    "id": f"session-recovered:{session_id}",
                    "kind": "system_message",
                    "text": "The controller recovered this conversation after a restart.",
                    "timestamp": timestamp,
                }
            )
            continue

        append_notice(
            item_id=f"event-notice:{eid}",
            title="Conversation event",
            description=f"Unsupported persisted event: {event.type}",
            timestamp=timestamp,
        )

    return items


def project_timeline_events(events: list[MessageEventResponse]) -> list[dict[str, Any]]:
    """Project message events into canonical timeline items."""

    return _project_timeline_events(events)


def _event_timestamp(event: dict[str, Any]) -> datetime:
    raw = event.get("ts") or event.get("timestamp")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def _sort_session_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda event: (_event_seq(event), _event_timestamp(event)))


def _filter_orphan_tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_tool_calls: set[str] = set()
    filtered: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "tool_call":
            data = event.get("data")
            if isinstance(data, dict):
                call_id = data.get("call_id")
                if isinstance(call_id, str) and call_id:
                    seen_tool_calls.add(call_id)
            filtered.append(event)
            continue
        if event.get("type") == "tool_result":
            data = event.get("data")
            call_id = data.get("call_id") if isinstance(data, dict) else None
            if isinstance(call_id, str) and call_id and call_id not in seen_tool_calls:
                continue
        filtered.append(event)
    return filtered


def _has_compaction_summary(events: list[dict[str, Any]]) -> bool:
    return any(event.get("type") == "compaction_summary" for event in events)


def _tag_session_events(session_row: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in events:
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if isinstance(data, dict) and "session_id" not in data:
            data["session_id"] = session_row.session_id
    return events


async def _read_compaction_summary_marker(
    guardrails: Any,
    session_row: Any,
) -> dict[str, Any] | None:
    """Read the durable rotation compaction marker when a tail page omitted it."""

    try:
        result = await guardrails.read_events(
            session_id=session_row.intaris_session_id or session_row.session_id,
            after_seq=0,
            limit=25,
            allow_missing_stream=True,
        )
    except Exception:
        logger.warning(
            "Failed to read session compaction marker during lineage page",
            extra={"extra_data": {"session_id": session_row.session_id}},
            exc_info=True,
        )
        return None
    if result.missing_stream_fallback_used:
        return None
    for event in _sort_session_events(list(result.events)):
        if event.get("type") == "compaction_summary":
            return event
    return None


def _require_visible_conversation(request: Request, row: Any) -> Any:
    if row is None or getattr(row, "status", None) == "deleted":
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    return row


async def _conversation_attention_context(
    session: Any,
    rows: list[Any],
    user_email: str,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    conversation_ids = [row.conversation_id for row in rows]
    active_session_ids = [
        row.active_session_id for row in rows if getattr(row, "active_session_id", None)
    ]
    active_sessions = await list_sessions_by_ids(session, active_session_ids)
    pending_notifications = await list_pending_notification_types_by_conversation(
        session,
        user_email,
        conversation_ids,
    )
    return active_sessions, pending_notifications


async def _conversation_response(
    request: Request,
    row: Any,
    *,
    has_active_turn: bool | None = None,
) -> ConversationResponse:
    active_session = None
    managed_link = None
    pending_notifications: list[str] = []
    active_turn_state = request.app.state.turn_scheduler.running_turn_state(row.conversation_id)
    resolved_has_active_turn = (
        active_turn_state is not None if has_active_turn is None else has_active_turn
    )
    async with request.app.state.session_factory() as session:
        if getattr(row, "active_session_id", None):
            active_session = await get_session_row(session, row.active_session_id)
        row_platform_data = getattr(row, "context_data", None) or {}
        if getattr(row, "context_type", None) in {
            "agent_work",
            "managed_agent_conversation",
        } or row_platform_data.get("kind") in {"agent_work", "managed_agent_conversation"}:
            managed_link = await get_managed_conversation_link_for_target(
                session,
                row.conversation_id,
                user_email=row.user_email,
            )
        pending_notifications = (
            await list_pending_notification_types_by_conversation(
                session,
                row.user_email,
                [row.conversation_id],
            )
        ).get(row.conversation_id, [])
        conversation_state = await snapshot_for_conversation(
            session,
            user_email=row.user_email,
            conversation_id=row.conversation_id,
            turn_scheduler=getattr(request.app.state, "turn_scheduler", None),
        )
    return conversation_to_response(
        row,
        has_active_turn=resolved_has_active_turn,
        active_turn_state=active_turn_state,
        active_session=active_session,
        pending_notification_types=pending_notifications,
        conversation_state=conversation_state,
        managed_link=managed_link,
    )


async def _require_mutable_conversation(
    request: Request,
    conversation_id: str,
    *,
    allow_managed_conversation: bool = False,
) -> Any:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None or row.status == "deleted":
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    if row.status == "archived":
        raise api_exception(409, "conflict", "Conversation is not active")
    if row.context_type in _MANAGED_CONVERSATION_CONTEXT_TYPES and not allow_managed_conversation:
        raise api_exception(
            409,
            "managed_conversation_read_only",
            "Managed conversations are read-only from the target chat; use managed actions from the controller conversation.",
        )
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, row.agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, agent, required="use")
    return user


def _queued_messages_response(messages: list[dict[str, Any]]) -> QueuedMessagesResponse:
    items = [QueuedMessageResponse.model_validate(item) for item in messages]
    return QueuedMessagesResponse(messages=items, queued_count=len(items))


def _agent_direct_sort_key(item: AgentDirectChatResponse) -> datetime:
    return (
        item.conversation.last_message_at
        or item.conversation.updated_at
        or item.conversation.created_at
        or datetime.min.replace(tzinfo=UTC)
    )


async def _conversation_page_projection(
    request: Request,
    *,
    user_email: str,
    cursor: str | None = None,
    limit: int,
    context_type: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    status: str = "active",
    include_agent_direct: bool = False,
) -> CursorPage[ConversationResponse]:
    cursor_payload = decode_cursor(cursor)
    cursor_id = str(cursor_payload.get("id", "")) if cursor_payload is not None else None
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    async with request.app.state.session_factory() as session:
        rows = await list_conversations(
            session,
            user_email,
            context_type=context_type,
            agent_id=agent_id,
            project_id=project_id,
            status=status,
            include_agent_direct=include_agent_direct,
            cursor_id=cursor_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        active_sessions, pending_notifications = await _conversation_attention_context(
            session,
            page_rows,
            user_email,
        )
        active_turn_states = (
            {
                row.conversation_id: turn_scheduler.running_turn_state(row.conversation_id)
                for row in page_rows
            }
            if turn_scheduler is not None
            else {row.conversation_id: None for row in page_rows}
        )
        conversation_states = {}
        managed_links = {}
        for row in page_rows:
            if active_turn_states.get(row.conversation_id) is not None:
                snapshot = await snapshot_for_conversation(
                    session,
                    user_email=user_email,
                    conversation_id=row.conversation_id,
                    turn_scheduler=turn_scheduler,
                )
                if snapshot is not None:
                    conversation_states[row.conversation_id] = snapshot
            if (row.context_data or {}).get("kind") in {"agent_work", "managed_agent_conversation"}:
                link = await get_managed_conversation_link_for_target(
                    session,
                    row.conversation_id,
                    user_email=user_email,
                )
                if link is not None:
                    managed_links[row.conversation_id] = link
    items: list[ConversationResponse] = []
    for row in page_rows:
        active_turn_state = active_turn_states.get(row.conversation_id)
        items.append(
            conversation_to_response(
                row,
                has_active_turn=active_turn_state is not None,
                active_session=active_sessions.get(row.active_session_id),
                active_turn_state=active_turn_state,
                pending_notification_types=pending_notifications.get(row.conversation_id, []),
                conversation_state=conversation_states.get(row.conversation_id),
                managed_link=managed_links.get(row.conversation_id),
            )
        )
    next_cursor = encode_cursor({"id": items[-1].conversation_id}) if has_more and items else None
    return CursorPage(items=items, cursor=next_cursor, has_more=has_more)


def _is_openable_chat_conversation(
    conversation: Any,
    *,
    user_email: str,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None = None,
) -> bool:
    platform_data = conversation.context_data or {}
    return (
        conversation.user_email == user_email
        and conversation.agent_id == agent_id
        and (agent_profile_id is None or conversation.agent_profile_id == agent_profile_id)
        and conversation.status == "active"
        and conversation.context_type == context_type
        and platform_data.get("kind") != "agent_direct"
    )


def _chat_last_opened_scope_key(
    *,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
) -> str:
    return f"{agent_id}\x1f{agent_profile_id or ''}\x1f{context_type}"


def _chat_last_opened_state_key(
    *,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
) -> str:
    return (
        f"{_CHAT_LAST_OPENED_UI_STATE_PREFIX}:"
        f"{_chat_last_opened_scope_key(agent_id=agent_id, context_type=context_type, agent_profile_id=agent_profile_id)}"
    )


def _chat_last_opened_conversation_id(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    conversation_id = state.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


async def _remember_chat_last_opened(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str,
    context_type: str,
    agent_profile_id: str | None,
    conversation_id: str,
) -> None:
    scope_keys = [
        _chat_last_opened_state_key(
            agent_id=agent_id,
            context_type=context_type,
            agent_profile_id=agent_profile_id,
        )
    ]
    if agent_profile_id is not None:
        scope_keys.append(
            _chat_last_opened_state_key(
                agent_id=agent_id,
                context_type=context_type,
                agent_profile_id=None,
            )
        )
    for scope_key in scope_keys:
        await upsert_user_ui_state(
            session,
            user_email,
            scope_key,
            {
                "conversation_id": conversation_id,
                "agent_id": agent_id,
                "agent_profile_id": agent_profile_id,
                "context_type": context_type,
            },
        )


async def _agent_direct_chat_projection(
    request: Request,
    *,
    user_email: str,
    agent_id: str | None = None,
    status: str = "active",
) -> list[AgentDirectChatResponse]:
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    rows: list[tuple[Any, Any]] = []
    async with request.app.state.session_factory() as session:
        visible_agents = await list_visible_agents(session, user_email)
        for agent, _grant in visible_agents:
            if agent.agent_type != "primary" or agent.status != "active":
                continue
            if agent_id is not None and agent.agent_id != agent_id:
                continue
            existing = await get_agent_direct_conversation(session, user_email, agent.agent_id)
            rows.append((agent, existing))
        active_sessions, pending_notifications = await _conversation_attention_context(
            session,
            [conversation for _agent, conversation in rows if conversation is not None],
            user_email,
        )
        active_turn_states = (
            {
                conversation.conversation_id: turn_scheduler.running_turn_state(
                    conversation.conversation_id
                )
                for _agent, conversation in rows
                if conversation is not None
            }
            if turn_scheduler is not None
            else {
                conversation.conversation_id: None
                for _agent, conversation in rows
                if conversation is not None
            }
        )
        conversation_states = {}
        for _agent, conversation in rows:
            if conversation is None:
                continue
            if active_turn_states.get(conversation.conversation_id) is None:
                continue
            snapshot = await snapshot_for_conversation(
                session,
                user_email=user_email,
                conversation_id=conversation.conversation_id,
                turn_scheduler=turn_scheduler,
            )
            if snapshot is not None:
                conversation_states[conversation.conversation_id] = snapshot

    responses: list[AgentDirectChatResponse] = []
    for agent, conversation in rows:
        if conversation is None:
            continue
        if status == "active" and conversation.status != "active":
            continue
        if status == "archived" and conversation.status != "archived":
            continue
        if status == "starred" and not conversation.starred_at:
            continue
        active_turn_state = active_turn_states.get(conversation.conversation_id)
        responses.append(
            AgentDirectChatResponse(
                agent=agent_to_response(agent),
                conversation=conversation_to_response(
                    conversation,
                    has_active_turn=active_turn_state is not None,
                    active_session=active_sessions.get(conversation.active_session_id),
                    active_turn_state=active_turn_state,
                    pending_notification_types=pending_notifications.get(
                        conversation.conversation_id,
                        [],
                    ),
                    conversation_state=conversation_states.get(conversation.conversation_id),
                ),
            )
        )

    responses.sort(key=_agent_direct_sort_key, reverse=True)
    return responses


async def _hydrate_event_attachments(
    request: Request,
    events: list[dict[str, Any]],
    *,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> None:
    artifact_store = request.app.state.artifact_store
    current_user = require_current_user(request)
    async with request.app.state.session_factory() as artifact_session:
        for event in events:
            data = event.get("data") if isinstance(event, dict) else None
            attachments = data.get("attachments") if isinstance(data, dict) else None
            if not isinstance(attachments, list):
                continue
            data["attachments"] = await hydrate_attachment_refs(
                artifact_session,
                artifact_store,
                attachments,
                owner_email=current_user.email,
                conversation_id=conversation_id,
                session_id=session_id,
            )


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=CursorPage[ConversationResponse])
async def conversation_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    context_type: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
    include_agent_direct: bool = Query(default=False),
) -> CursorPage[ConversationResponse]:
    user = require_current_user(request)
    return await _conversation_page_projection(
        request,
        user_email=user.email,
        cursor=cursor,
        limit=limit,
        context_type=context_type,
        agent_id=agent_id,
        project_id=project_id,
        status=status,
        include_agent_direct=include_agent_direct,
    )


@router.get("/context-types", response_model=list[str])
async def conversation_context_types(
    request: Request,
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> list[str]:
    """Return distinct conversation context types for sidebar filters."""

    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        return await list_conversation_context_types(
            session,
            user.email,
            status=status,
            include_agent_direct=False,
        )


@router.get("/agent-direct", response_model=list[AgentDirectChatResponse])
async def agent_direct_chats(
    request: Request,
    agent_id: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> list[AgentDirectChatResponse]:
    """Return sticky web direct chats for visible primary agents."""

    user = require_current_user(request)
    return await _agent_direct_chat_projection(
        request,
        user_email=user.email,
        agent_id=agent_id,
        status=status,
    )


@router.get("/sidebar", response_model=SidebarProjectionResponse)
async def sidebar_projection(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    context_type: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|starred|archived|all)$"),
) -> SidebarProjectionResponse:
    """Return the UI-shaped sidebar projection in one request."""

    user = require_current_user(request)
    agents = [
        agent_to_response(agent)
        for agent in await request.app.state.agent_registry.list_all(
            owner_email=user.email,
            include_hidden=False,
            include_system=True,
            include_disabled=False,
        )
    ]
    conversations = await _conversation_page_projection(
        request,
        user_email=user.email,
        cursor=cursor,
        limit=limit,
        context_type=context_type,
        agent_id=agent_id,
        project_id=project_id,
        status=status,
        include_agent_direct=False,
    )
    direct_chats = (
        await _agent_direct_chat_projection(
            request,
            user_email=user.email,
            agent_id=agent_id,
            status="active",
        )
        if context_type in {None, "web"}
        else []
    )
    async with request.app.state.session_factory() as session:
        context_types = await list_conversation_context_types(
            session,
            user.email,
            status=status,
            include_agent_direct=False,
        )
    return SidebarProjectionResponse(
        agents=agents,
        agent_direct_chats=direct_chats,
        conversations=conversations,
        context_types=context_types,
    )


@router.post("/open", response_model=ConversationResponse)
async def open_conversation(
    request: Request,
    payload: ConversationOpenRequest,
) -> ConversationResponse:
    """Resolve the best chat conversation to open for the selected agent/channel.

    Browser-local last-opened IDs are treated only as ordered hints. The server
    validates each hint against ownership, selected agent, context, active
    status, and direct-chat exclusion before falling back to latest/create.
    """
    user = require_current_user(request)
    context_type = payload.context_type or "web"
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc

        async def return_opened(conversation: Any) -> ConversationResponse:
            if user.role != "viewer":
                await _remember_chat_last_opened(
                    session,
                    user_email=user.email,
                    agent_id=payload.agent_id,
                    context_type=context_type,
                    agent_profile_id=payload.agent_profile_id,
                    conversation_id=conversation.conversation_id,
                )
                await session.commit()
            return await _conversation_response(request, conversation)

        seen_candidates: set[str] = set()
        persisted_state_key = _chat_last_opened_state_key(
            agent_id=payload.agent_id,
            context_type=context_type,
            agent_profile_id=payload.agent_profile_id,
        )
        persisted_state = await get_user_ui_state_value(
            session,
            user.email,
            persisted_state_key,
        )
        candidate_conversation_ids: list[str] = []
        persisted_conversation_id = _chat_last_opened_conversation_id(persisted_state)
        if persisted_conversation_id:
            candidate_conversation_ids.append(persisted_conversation_id)
        candidate_conversation_ids.extend(payload.candidate_conversation_ids)

        for conversation_id in candidate_conversation_ids[:10]:
            conversation_id = conversation_id.strip()
            if not conversation_id or conversation_id in seen_candidates:
                continue
            seen_candidates.add(conversation_id)
            candidate = await get_conversation(session, conversation_id)
            if candidate is None:
                continue
            if _is_openable_chat_conversation(
                candidate,
                user_email=user.email,
                agent_id=payload.agent_id,
                context_type=context_type,
                agent_profile_id=payload.agent_profile_id,
            ):
                return await return_opened(candidate)

        fallback_rows = await list_conversations(
            session,
            user.email,
            context_type=context_type,
            agent_id=payload.agent_id,
            status="active",
            include_agent_direct=False,
            limit=1 if payload.agent_profile_id is None else None,
        )
        for existing in fallback_rows:
            if _is_openable_chat_conversation(
                existing,
                user_email=user.email,
                agent_id=payload.agent_id,
                context_type=context_type,
                agent_profile_id=payload.agent_profile_id,
            ):
                return await return_opened(existing)

    forbid_mutation_for_viewer(request)
    context_ref = f"{context_type}:user:{user.email}:default"
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        agent_profile_id=payload.agent_profile_id,
        context=ConversationContext(
            type=context_type,
            ref=context_ref,
            platform_data={},
            memory_labels={},
        ),
    )
    async with request.app.state.session_factory() as session:
        await _remember_chat_last_opened(
            session,
            user_email=user.email,
            agent_id=payload.agent_id,
            context_type=context_type,
            agent_profile_id=payload.agent_profile_id,
            conversation_id=conversation.conversation_id,
        )
        await session.commit()
    return await _conversation_response(request, conversation)


@router.post("/resolve", response_model=ConversationResponse)
async def resolve_conversation(
    request: Request,
    payload: ConversationResolveRequest,
) -> ConversationResponse:
    """Find an existing conversation for the given agent and context type, or create one.

    This is the "persistent channel" endpoint: the web UI calls it to ensure
    there is always a default conversation for a given agent.
    """
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc
        if payload.scope == "agent_direct":
            if payload.context_type != "web":
                raise api_exception(
                    400,
                    "invalid_request",
                    "Agent direct conversations are only supported for web context.",
                )
            existing = None
        else:
            existing = await get_latest_active_conversation_for_agent(
                session,
                user.email,
                payload.agent_id,
                context_type=payload.context_type,
            )
    if existing is not None:
        return await _conversation_response(request, existing)
    if payload.scope == "agent_direct":
        conversation = (
            await request.app.state.session_manager.get_or_create_agent_direct_conversation(
                user_email=user.email,
                agent_id=payload.agent_id,
                agent_profile_id=payload.agent_profile_id,
                agent_profile_explicit=payload.agent_profile_id is not None,
            )
        )
    else:
        context_ref = f"{payload.context_type}:user:{user.email}:default"
        conversation = await request.app.state.session_manager.create_conversation(
            user_email=user.email,
            agent_id=payload.agent_id,
            agent_profile_id=payload.agent_profile_id,
            context=ConversationContext(
                type=payload.context_type,
                ref=context_ref,
                platform_data={},
                memory_labels={},
            ),
            title=None,
            title_source="unset",
        )
    return await _conversation_response(request, conversation)


@router.post("", response_model=ConversationResponse)
async def create_conversation(
    request: Request,
    payload: ConversationCreateRequest,
) -> ConversationResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, payload.agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        await check_agent_access(request, agent, required="use")
        agent_definition = _agent_definition_from_row(agent)
        try:
            resolve_agent_profile(agent_definition, payload.agent_profile_id, source="api")
        except ValueError as exc:
            raise api_exception(400, "invalid_agent_profile", str(exc)) from exc
    await _validate_project_access(request, payload.project_id)
    conversation = await request.app.state.session_manager.create_conversation(
        user_email=user.email,
        agent_id=payload.agent_id,
        agent_profile_id=payload.agent_profile_id,
        context=ConversationContext(
            type=payload.context.type,
            ref=payload.context.ref,
            platform_data=payload.context.platform_data,
            memory_labels=payload.context.memory_labels,
        ),
        title=payload.title,
        title_source="manual" if payload.title else "unset",
        project_id=payload.project_id,
    )
    return await _conversation_response(request, conversation)


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def conversation_detail(request: Request, conversation_id: str) -> ConversationResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    row = _require_visible_conversation(request, row)
    return await _conversation_response(request, row)


@router.post("/{conversation_id}/opened", response_model=ConversationResponse)
async def remember_opened_conversation(
    request: Request,
    conversation_id: str,
) -> ConversationResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        if user.role != "viewer" and _is_openable_chat_conversation(
            row,
            user_email=user.email,
            agent_id=row.agent_id,
            context_type=row.context_type,
            agent_profile_id=row.agent_profile_id,
        ):
            await _remember_chat_last_opened(
                session,
                user_email=user.email,
                agent_id=row.agent_id,
                context_type=row.context_type,
                agent_profile_id=row.agent_profile_id,
                conversation_id=row.conversation_id,
            )
            await session.commit()
    return await _conversation_response(request, row)


@router.get(
    "/{conversation_id}/title-suggestion", response_model=ConversationTitleSuggestionResponse
)
async def conversation_title_suggestion(
    request: Request,
    conversation_id: str,
) -> ConversationTitleSuggestionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    row = _require_visible_conversation(request, row)
    platform_data = row.context_data or {}
    suggestion = latest_intaris_title_from_platform_data(platform_data)
    if suggestion:
        generated_at = platform_data.get("intaris_latest_title_at")
        return ConversationTitleSuggestionResponse(
            title=suggestion,
            generated_at=generated_at if isinstance(generated_at, str) else None,
            available=True,
        )

    active_session_id = getattr(row, "active_session_id", None)
    if active_session_id:
        async with request.app.state.session_factory() as session:
            session_row = await get_session_row(session, active_session_id)
        if session_row is not None:
            try:
                intaris_sid = session_row.intaris_session_id or session_row.session_id
                intaris_session = await request.app.state.providers.guardrails.get_session(
                    intaris_sid
                )
                title = (intaris_session.title or "").strip()
                if title:
                    return ConversationTitleSuggestionResponse(title=title, available=True)
            except Exception:
                logger.debug(
                    "conversation: failed to fetch latest Intaris title suggestion",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                    exc_info=True,
                )

    return ConversationTitleSuggestionResponse(
        available=False,
        reason="No Intaris title suggestion is available yet.",
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    request: Request,
    conversation_id: str,
    payload: ConversationUpdateRequest,
) -> ConversationResponse:
    forbid_mutation_for_viewer(request)
    manager = request.app.state.session_manager
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        if row is None:
            raise api_exception(404, "not_found", "Conversation not found")
        require_resource_owner(request, row.user_email)
        if row.status == "deleted":
            raise api_exception(404, "not_found", "Conversation not found")
        if payload.archived is True:
            await manager.archive_conversation(conversation_id)
        elif payload.archived is False and row.status == "archived":
            row.status = "active"
        if payload.title is not None:
            row.title = payload.title
            row.title_source = "manual" if payload.title.strip() else "unset"
        if payload.project_id is not None:
            await _validate_project_access(request, payload.project_id)
            row.project_id = payload.project_id
        if "starred_at" in payload.model_fields_set:
            row.starred_at = payload.starred_at
        await session.commit()
        await session.refresh(row)
        active_session = (
            await get_session_row(session, row.active_session_id) if row.active_session_id else None
        )
        pending_notifications = (
            await list_pending_notification_types_by_conversation(
                session,
                row.user_email,
                [row.conversation_id],
            )
        ).get(row.conversation_id, [])
        active_turn_state = request.app.state.turn_scheduler.running_turn_state(row.conversation_id)
        return conversation_to_response(
            row,
            has_active_turn=active_turn_state is not None,
            active_turn_state=active_turn_state,
            active_session=active_session,
            pending_notification_types=pending_notifications,
        )


@router.post("/{conversation_id}/read", response_model=dict)
async def mark_read(request: Request, conversation_id: str) -> dict[str, bool]:
    """Mark a conversation as read (sets last_read_at to now)."""
    require_current_user(request)
    payload: dict[str, Any] | None = None
    user_email: str | None = None
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        user_email = row.user_email
        was_unread = row.last_message_at is not None and (
            row.last_read_at is None or row.last_message_at > row.last_read_at
        )
        await mark_conversation_read(session, conversation_id)
        if was_unread:
            payload = {
                "type": "conversation_updated",
                "conversation_id": row.conversation_id,
                "has_unread": False,
                "last_read_at": row.last_read_at.isoformat() if row.last_read_at else None,
            }
        await session.commit()
    if payload is not None:
        ws_manager = getattr(request.app.state, "ws_manager", None)
        send_to_user = getattr(ws_manager, "send_to_user", None)
        send_to_user_func = (
            cast(Callable[[str, dict[str, Any]], Awaitable[None]], send_to_user)
            if callable(send_to_user)
            else None
        )
        if send_to_user_func is not None and user_email is not None:
            try:
                await send_to_user_func(user_email, payload)
            except Exception:
                logger.warning(
                    "Failed to fan out conversation read state",
                    extra={
                        "extra_data": {"conversation_id": conversation_id, "user_email": user_email}
                    },
                    exc_info=True,
                )
    return {"ok": True}


async def _validate_project_access(request: Request, project_id: str | None) -> None:
    if project_id is None:
        return
    async with request.app.state.session_factory() as session:
        project = await get_project(session, project_id)
    if project is None or project.status != "active":
        raise api_exception(404, "not_found", "Project not found")
    await check_project_access(request, project, required="use")


@router.delete("/{conversation_id}", response_model=dict)
async def delete_conversation(request: Request, conversation_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    ok = await request.app.state.session_manager.soft_delete_conversation(conversation_id)
    return {"ok": ok}


@router.delete("/{conversation_id}/purge", response_model=dict)
async def purge_conversation(request: Request, conversation_id: str) -> dict[str, object]:
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        sessions = (
            await list_conversation_sessions(session, conversation_id) if row is not None else []
        )
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    ok = await request.app.state.session_manager.purge_conversation(conversation_id)
    delete_session = getattr(request.app.state.providers.guardrails, "delete_session", None)
    if not callable(delete_session):
        return {
            "ok": ok,
            "intaris_cascade": False,
            "warning": "Intaris purge is not supported by the current provider contract.",
        }

    cascade_ok = True
    for session_row in sessions:
        intaris_session_id = session_row.intaris_session_id or session_row.session_id
        try:
            await delete_session(intaris_session_id)
        except Exception:
            cascade_ok = False
            break
    return {
        "ok": ok,
        "intaris_cascade": cascade_ok,
        "warning": None if cascade_ok else "Intaris session purge failed for one or more sessions.",
    }


@router.get("/{conversation_id}/messages", response_model=MessageHistoryResponse)
async def conversation_messages(
    request: Request,
    conversation_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    anchor: Literal["oldest", "latest"] = Query(default="oldest"),
    before: str | None = Query(default=None),
) -> MessageHistoryResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)

        history_anchor_id = row.active_session_id
        if history_anchor_id is None and after_seq == 0:
            latest_root = await get_latest_root_session_for_conversation(session, conversation_id)
            history_anchor_id = latest_root.session_id if latest_root is not None else None
        if history_anchor_id is None:
            return MessageHistoryResponse(items=[], last_seq=0, has_more=False)

        before_session_id: str | None = None
        before_seq: int | None = None
        if before:
            if after_seq > 0:
                raise api_exception(
                    400,
                    "invalid_request",
                    "History cursor cannot be combined with active-session incremental replay.",
                )
            before_session_id, before_seq = _decode_messages_cursor(before)
            cursor_row = await get_session_row(session, before_session_id)
            if cursor_row is None or cursor_row.conversation_id != conversation_id:
                raise api_exception(400, "invalid_cursor", "Invalid history cursor")
            if cursor_row.parent_session_id is not None:
                raise api_exception(400, "invalid_cursor", "Invalid history cursor")

        # Incremental fetch (after_seq > 0): read only the active session.
        # Latest-page fetch: walk the root-session lineage but read only as
        # many tail events as the client requested.
        # Legacy full load (after_seq == 0, anchor=oldest): keep historical
        # behavior for existing clients.
        lineage_truncated = False
        if after_seq > 0:
            session_row = await get_session_row(session, history_anchor_id)
            session_rows = [session_row] if session_row is not None else []
        elif anchor == "latest" or before:
            session_rows, lineage_truncated = await get_root_session_chain_page(
                session,
                conversation_id,
                history_anchor_id,
                before_session_id=before_session_id,
            )
        else:
            session_rows, lineage_truncated = await get_root_session_chain(
                session, conversation_id, history_anchor_id
            )

    if not session_rows:
        return MessageHistoryResponse(items=[], last_seq=0, has_more=False)

    guardrails = request.app.state.providers.guardrails

    # Read events from each session in the chain (parallel for full load)
    all_events: list[Any] = []
    last_seq_value = 0
    has_more = False
    active_session_id = history_anchor_id
    active_session_last_seq = 0
    history_truncated = False
    truncation_reason: str | None = None
    older_cursor: str | None = None

    if after_seq > 0:
        # Incremental: single session read
        sr = session_rows[0]
        event_result = await guardrails.read_events(
            session_id=sr.intaris_session_id or sr.session_id,
            after_seq=after_seq,
            limit=limit,
            allow_missing_stream=True,
        )
        if event_result.missing_stream_fallback_used:
            logger.warning(
                "Conversation history missing in Intaris; returning empty history",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": sr.session_id,
                        "intaris_session_id": sr.intaris_session_id or sr.session_id,
                    }
                },
            )
        all_events = _tag_session_events(sr, list(event_result.events))
        last_seq_value = event_result.last_seq
        has_more = event_result.has_more
        active_session_last_seq = event_result.last_seq
    elif anchor == "latest" or before:
        # Latest-first bootstrap / older-page fetch across root-session
        # lineage. Read only enough events to fill the requested page, moving
        # backwards across compacted root sessions when necessary.
        remaining = limit
        page_events: list[tuple[Any, list[dict[str, Any]]]] = []
        has_older = False
        for index in range(len(session_rows) - 1, -1, -1):
            if remaining <= 0:
                has_older = True
                break
            sr = session_rows[index]
            result_last_seq = 0
            result_missing_stream = False
            if before_session_id == sr.session_id and before_seq is not None and before_seq > 0:
                read_limit = remaining + 1
                read_after_seq = max(0, before_seq - read_limit)
                result = await guardrails.read_events(
                    session_id=sr.intaris_session_id or sr.session_id,
                    after_seq=read_after_seq,
                    limit=read_limit,
                    allow_missing_stream=True,
                )
                row_events = [
                    event for event in list(result.events) if _event_seq(event) < before_seq
                ]
                if row_events and _event_seq(row_events[0]) > 1:
                    has_older = True
                if len(row_events) > remaining:
                    row_events = row_events[-remaining:]
                    has_older = True
                result_last_seq = result.last_seq
                result_missing_stream = result.missing_stream_fallback_used
            else:
                result = await guardrails.read_events(
                    session_id=sr.intaris_session_id or sr.session_id,
                    last_n=remaining,
                    allow_missing_stream=True,
                )
                row_events = list(result.events)
                if result.has_more:
                    has_older = True
                result_last_seq = result.last_seq
                result_missing_stream = result.missing_stream_fallback_used
            if result_missing_stream:
                logger.warning(
                    "Session stream missing in Intaris during paginated lineage read",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": sr.session_id,
                        }
                    },
                )
                row_events = [
                    {
                        "type": "history_gap",
                        "data": {"reason": "stream_missing", "session_id": sr.session_id},
                        "seq": 0,
                        "ts": None,
                    }
                ]
            row_events = _filter_orphan_tool_results(
                _sort_session_events(_tag_session_events(sr, row_events))
            )
            if (
                sr.previous_session_id
                and not _has_compaction_summary(row_events)
                and before_session_id is None
                and getattr(row, "context_type", None) == "web"
            ):
                marker = await _read_compaction_summary_marker(guardrails, sr)
                if marker is not None:
                    row_events = _sort_session_events(
                        _tag_session_events(sr, [marker, *row_events])
                    )
            if sr.session_id == active_session_id:
                active_session_last_seq = result_last_seq
            page_events.insert(0, (sr, row_events))
            remaining -= len(row_events)
            if result_missing_stream:
                break
        all_events = [event for _, events in page_events for event in events]
        if active_session_last_seq == 0 and active_session_id:
            active_row = next(
                (item for item in session_rows if item.session_id == active_session_id),
                None,
            )
            if active_row is None:
                async with request.app.state.session_factory() as session:
                    active_row = await get_session_row(session, active_session_id)
            if active_row is not None:
                active_session_last_seq = await guardrails.get_last_seq(
                    active_row.intaris_session_id or active_row.session_id
                )
        last_seq_value = active_session_last_seq
        has_more = has_older or lineage_truncated
        if has_more and all_events:
            first_event = _messages_cursor_anchor_event(all_events)
            first_session_id = None
            if first_event is not None:
                data = first_event.get("data")
                if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                    first_session_id = data["session_id"]
                first_seq = _event_seq(first_event)
                if first_session_id:
                    older_cursor = _encode_messages_cursor(first_session_id, first_seq)
        if lineage_truncated:
            history_truncated = True
            truncation_reason = "lineage_truncated"
    else:
        # Full load: read all sessions in parallel
        import asyncio as _asyncio

        async def _read_session(sr: Any) -> tuple[Any, list[dict[str, Any]], int]:
            try:
                result = await guardrails.read_events(
                    session_id=sr.intaris_session_id or sr.session_id,
                    after_seq=0,
                    limit=0,
                    allow_missing_stream=True,
                )
                if result.missing_stream_fallback_used:
                    logger.warning(
                        "Session stream missing in Intaris during lineage read",
                        extra={
                            "extra_data": {
                                "conversation_id": conversation_id,
                                "session_id": sr.session_id,
                            }
                        },
                    )
                    return (
                        sr,
                        [
                            {
                                "type": "history_gap",
                                "data": {
                                    "reason": "stream_missing",
                                    "session_id": sr.session_id,
                                },
                                "seq": 0,
                                "ts": None,
                            }
                        ],
                        result.last_seq,
                    )
                return sr, _sort_session_events(list(result.events)), result.last_seq
            except Exception:
                logger.warning(
                    "Failed to read session events during lineage walk",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": sr.session_id,
                        }
                    },
                    exc_info=True,
                )
                return (
                    sr,
                    [
                        {
                            "type": "history_gap",
                            "data": {
                                "reason": "read_failed",
                                "session_id": sr.session_id,
                            },
                            "seq": 0,
                            "ts": None,
                        }
                    ],
                    0,
                )

        results = await _asyncio.gather(*[_read_session(sr) for sr in session_rows])

        if lineage_truncated:
            history_truncated = True
            truncation_reason = "lineage_truncated"
            all_events.append(
                {
                    "type": "history_gap",
                    "data": {"reason": "lineage_truncated"},
                    "seq": 0,
                    "ts": None,
                }
            )

        for sr, events, session_last_seq in results:
            # Tag each event with session_id so the UI can build
            # lineage-safe timeline item IDs (seq is session-local).
            sid = sr.session_id
            if sid == active_session_id:
                active_session_last_seq = session_last_seq
            _tag_session_events(sr, events)
            all_events.extend(events)

        # For full loads, return the full lineage history and let the
        # client switch to incremental mode afterward using the active
        # session's seq space. Avoid single-session cursor semantics here.
        has_more = False

    await _hydrate_event_attachments(request, all_events, conversation_id=conversation_id)
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    has_active_turn = bool(turn_scheduler and turn_scheduler.has_running_turn(conversation_id))
    active_streams = (
        await turn_scheduler.active_stream_snapshots(conversation_id)
        if turn_scheduler is not None
        else []
    )
    active_tool_outputs = (
        await turn_scheduler.active_tool_output_snapshots(conversation_id)
        if turn_scheduler is not None
        else []
    )
    async with request.app.state.session_factory() as state_session:
        state_snapshot = await snapshot_for_conversation(
            state_session,
            user_email=row.user_email,
            conversation_id=conversation_id,
            turn_scheduler=turn_scheduler,
            active_session_last_seq=active_session_last_seq,
        )

    return MessageHistoryResponse(
        items=serialize_event_rows(
            list(all_events),
            log_label="conversation_messages",
            log_context={
                "conversation_id": conversation_id,
                "session_id": session_rows[-1].session_id if session_rows else "",
            },
        ),
        last_seq=last_seq_value,
        has_more=has_more,
        older_cursor=older_cursor,
        has_active_turn=has_active_turn,
        active_streams=active_streams,
        active_tool_outputs=active_tool_outputs,
        active_session_id=active_session_id,
        active_session_last_seq=active_session_last_seq,
        history_truncated=history_truncated,
        truncation_reason=truncation_reason,
        state_snapshot=state_snapshot,
    )


@router.get("/{conversation_id}/timeline", response_model=TimelineProjectionResponse)
async def conversation_timeline(
    request: Request,
    conversation_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    anchor: Literal["oldest", "latest"] = Query(default="oldest"),
    before: str | None = Query(default=None),
) -> TimelineProjectionResponse:
    history = await conversation_messages(
        request,
        conversation_id,
        after_seq=after_seq,
        limit=limit,
        anchor=anchor,
        before=before,
    )
    timeline_items = _project_timeline_events(history.items)
    return TimelineProjectionResponse(
        **history.model_dump(),
        timeline_items=timeline_items,
    )


def _event_data(event: Any) -> dict[str, Any] | None:
    data = event.get("data") if isinstance(event, dict) else getattr(event, "data", None)
    return data if isinstance(data, dict) else None


def _tool_call_belongs_to_events(
    events: list[Any], call_id: str
) -> tuple[dict[str, Any], str | None] | None:
    fallback: tuple[dict[str, Any], str | None] | None = None
    for event in events:
        data = _event_data(event)
        if not data or data.get("call_id") != call_id:
            continue
        event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
        if event_type == "tool_result":
            session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
            return data, session_id
        if (
            event_type in {"tool_call", "tool_result_chunk", "tool_output_chunk"}
            and fallback is None
        ):
            session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
            fallback = data, session_id
    return fallback


@router.get("/{conversation_id}/tool-outputs/{call_id}", response_model=ToolOutputPageResponse)
async def conversation_tool_output_page(
    request: Request,
    conversation_id: str,
    call_id: str,
    session_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = Query(default=False),
) -> ToolOutputPageResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        active_session_id = row.active_session_id
        session_rows, _ = (
            await get_root_session_chain(session, conversation_id, active_session_id)
            if active_session_id
            else ([], False)
        )

    guardrails = request.app.state.providers.guardrails
    all_events: list[Any] = []
    for sr in session_rows:
        result = await guardrails.read_events(
            session_id=sr.intaris_session_id or sr.session_id,
            after_seq=0,
            limit=0,
            allow_missing_stream=True,
        )
        events = list(result.events)
        for event in events:
            data = _event_data(event)
            if data is not None and "session_id" not in data:
                data["session_id"] = sr.session_id
        all_events.extend(events)

    ownership = _tool_call_belongs_to_events(all_events, call_id)
    if ownership is None:
        # Active streamed chunks can exist before the final persisted event.
        snapshots = []
        turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
        if turn_scheduler is not None:
            snapshots = await turn_scheduler.active_tool_output_snapshots(conversation_id)
        for snapshot in snapshots:
            if snapshot.get("call_id") == call_id:
                ownership = (snapshot, snapshot.get("session_id"))
                break
    if ownership is None:
        raise api_exception(404, "not_found", "Tool output not found")

    event_data, event_session_id = ownership
    resolved_session_id = session_id or event_session_id
    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    if turn_scheduler is not None and resolved_session_id:
        live_page = turn_scheduler.read_live_tool_output_page(
            conversation_id=conversation_id,
            session_id=resolved_session_id,
            call_id=call_id,
            offset=offset,
            limit=limit,
            latest=latest,
        )
        if live_page is not None and live_page.status == "running":
            return ToolOutputPageResponse(
                conversation_id=conversation_id,
                session_id=resolved_session_id,
                call_id=call_id,
                status=live_page.status,
                source="live_spool",
                content=live_page.content,
                chunks=[
                    ToolOutputChunkResponse(
                        index=chunk.index,
                        offset=chunk.offset,
                        stream=chunk.stream,
                        text=chunk.text,
                    )
                    for chunk in live_page.chunks
                ],
                offset=live_page.offset,
                limit=live_page.limit,
                next_offset=live_page.next_offset,
                prev_offset=live_page.prev_offset,
                has_more_before=live_page.has_more_before,
                has_more_after=live_page.has_more_after,
                output_size=live_page.output_size,
                recoverable=True,
                truncated=live_page.truncated,
                spool_truncated=live_page.truncated,
            )

    tool_output_store = getattr(request.app.state, "tool_output_store", None)
    if tool_output_store is not None and bool(event_data.get("has_full_output")):
        stored = await tool_output_store.read(call_id, offset=max(1, offset or 1), limit=limit)
        if stored is not None:
            next_offset = stored.offset + stored.limit if stored.has_more else None
            prev_offset = max(1, stored.offset - stored.limit) if stored.offset > 1 else None
            return ToolOutputPageResponse(
                conversation_id=conversation_id,
                session_id=resolved_session_id,
                call_id=call_id,
                status="completed",
                source="stored_output",
                content=stored.content,
                offset=stored.offset,
                limit=stored.limit,
                next_offset=next_offset,
                prev_offset=prev_offset,
                has_more_before=stored.offset > 1,
                has_more_after=stored.has_more,
                output_size=int(event_data.get("output_size") or len(stored.content)),
                total_lines=stored.total_lines,
                recoverable=True,
                truncated=bool(
                    event_data.get("truncated") or event_data.get("agent_visible_truncated")
                ),
            )

    preview = str(event_data.get("result") or "")
    return ToolOutputPageResponse(
        conversation_id=conversation_id,
        session_id=resolved_session_id,
        call_id=call_id,
        status=str(event_data.get("status") or "completed"),
        source="event_preview",
        content=preview,
        offset=0,
        limit=limit,
        output_size=len(preview),
        recoverable=False,
    )


@router.get("/{conversation_id}/queue", response_model=QueuedMessagesResponse)
async def get_queued_messages(request: Request, conversation_id: str) -> QueuedMessagesResponse:
    await _require_mutable_conversation(
        request,
        conversation_id,
        allow_managed_conversation=True,
    )
    return _queued_messages_response(
        request.app.state.turn_scheduler.queued_messages(conversation_id)
    )


@router.patch("/{conversation_id}/queue/{queue_id}", response_model=QueuedMessageResponse)
async def update_queued_message(
    request: Request,
    conversation_id: str,
    queue_id: str,
    payload: UpdateQueuedMessageRequest,
) -> QueuedMessageResponse:
    await _require_mutable_conversation(request, conversation_id)
    updated = await request.app.state.turn_scheduler.update_queued_message(
        conversation_id,
        queue_id,
        content=payload.content.strip(),
    )
    if updated is None:
        raise api_exception(404, "not_found", "Queued message not found")
    return QueuedMessageResponse.model_validate(updated)


@router.delete("/{conversation_id}/queue/{queue_id}", status_code=204)
async def delete_queued_message(request: Request, conversation_id: str, queue_id: str) -> Response:
    await _require_mutable_conversation(request, conversation_id)
    cancelled = await request.app.state.turn_scheduler.cancel_queued_message(
        conversation_id, queue_id
    )
    if not cancelled:
        raise api_exception(404, "not_found", "Queued message not found")
    return Response(status_code=204)


@router.post("/{conversation_id}/messages")
async def send_message(
    request: Request,
    conversation_id: str,
    payload: SendMessageRequest,
) -> Response:
    """Send a chat message to a conversation.

    Supports two delivery modes via the ``Accept`` header:

    - ``Accept: text/event-stream`` — SSE streaming response with real-time
      token deltas, tool calls, and turn completion events.
    - ``Accept: application/json`` (default) — fire-and-forget 202 Accepted.
      Poll ``GET /conversations/{id}/messages`` for the response.

    Slash commands (``/compact``, ``/new``, ``/model``, etc.) are dispatched
    through the ``CommandDispatcher`` and return their result directly.
    """
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)

    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
    if row is None:
        raise api_exception(404, "not_found", "Conversation not found")
    require_resource_owner(request, row.user_email)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, row.agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, agent, required="use")
    if row.status == "deleted":
        raise api_exception(404, "not_found", "Conversation not found")
    if row.status == "archived":
        raise api_exception(409, "conflict", "Conversation is not active")
    if row.context_type in _MANAGED_CONVERSATION_CONTEXT_TYPES:
        raise api_exception(
            409,
            "managed_conversation_read_only",
            "Managed conversations are read-only from the target chat; use managed actions from the controller conversation.",
        )

    # --- Slash command dispatch ---
    command_result = await _try_command_dispatch(request, conversation_id, payload.content, user)
    if command_result is not None:
        return JSONResponse(
            status_code=200,
            content={"status": "command_executed", "result": command_result},
        )

    # --- Turn submission ---
    accept = request.headers.get("accept", "application/json")
    wants_sse = "text/event-stream" in accept
    turn_scheduler = request.app.state.turn_scheduler

    if wants_sse:
        from cognis.api.sse import SSETurnObserver

        observer = SSETurnObserver(conversation_id)
        error = await turn_scheduler.submit_turn(
            conversation_id,
            payload.content,
            user_email=user.email,
            attachments=[item.model_dump(mode="json") for item in payload.attachments],
            turn_observers=[observer],
            client_message_id=payload.client_message_id,
        )
        if error is not None:
            raise _turn_error_to_http(error)
        try:
            async with request.app.state.session_factory() as session:
                latest_row = await get_conversation(session, conversation_id)
                await mark_artifacts_attached(
                    session,
                    [item.artifact_id for item in payload.attachments],
                    owner_email=user.email,
                    conversation_id=conversation_id,
                    session_id=latest_row.active_session_id if latest_row else None,
                )
                await session.commit()
        except Exception:
            logger.warning(
                "Failed to persist post-submit attachment association",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )

        async def _cleanup_generator():  # type: ignore[return]
            try:
                async for event in observer.event_generator():
                    yield event
            finally:
                turn_scheduler.remove_observer(conversation_id, observer)

        return StreamingResponse(
            _cleanup_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        error = await turn_scheduler.submit_turn(
            conversation_id,
            payload.content,
            user_email=user.email,
            attachments=[item.model_dump(mode="json") for item in payload.attachments],
            client_message_id=payload.client_message_id,
        )
        if error is not None:
            raise _turn_error_to_http(error)
        try:
            async with request.app.state.session_factory() as session:
                latest_row = await get_conversation(session, conversation_id)
                await mark_artifacts_attached(
                    session,
                    [item.artifact_id for item in payload.attachments],
                    owner_email=user.email,
                    conversation_id=conversation_id,
                    session_id=latest_row.active_session_id if latest_row else None,
                )
                await session.commit()
        except Exception:
            logger.warning(
                "Failed to persist post-submit attachment association",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
        return JSONResponse(
            status_code=202,
            content=SendMessageResponse(status="accepted").model_dump(),
        )


async def _require_managed_conversation(
    request: Request,
    conversation_id: str,
) -> tuple[Any, Any]:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        if row.context_type not in {"agent_work", "managed_agent_conversation"}:
            raise api_exception(409, "not_managed_conversation", "Conversation is not managed")
        link = await get_managed_conversation_link_for_target(
            session,
            conversation_id,
            user_email=user.email,
        )
        if link is None:
            raise api_exception(404, "not_found", "Agent work link not found")
    return user, link


async def _managed_action_response(
    request: Request,
    conversation_id: str,
    status: str,
    result: dict[str, Any] | None = None,
) -> ManagedConversationActionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        response = await _conversation_response(request, row)
    return ManagedConversationActionResponse(
        status=status,
        conversation_id=conversation_id,
        managed_agent=response.managed_agent,
        result=result,
    )


async def _last_agent_work_user_message(request: Request, link: Any) -> str | None:
    return await last_managed_conversation_user_message_for_retry(
        session_cache=request.app.state.session_cache,
        guardrails=request.app.state.providers.guardrails,
        session_factory=request.app.state.session_factory,
        link=link,
    )


async def _record_agent_work_context(
    request: Request,
    *,
    session_model: SessionModel,
    controller_agent_id: str,
    controller_conversation_id: str,
    controller_session_id: str,
    target_agent_id: str,
) -> None:
    content = "\n".join(
        [
            "Agent work context:",
            f"- This session is managed by Cognis agent `{controller_agent_id}` on behalf of the user.",
            "- Treat user messages in this session as instructions from that authenticated internal agent.",
            "- Do not mention this management context unless it is operationally relevant.",
            f"- Controller conversation: {controller_conversation_id}",
            f"- Controller session: {controller_session_id}",
        ]
    )
    event = SessionEvent(
        type="developer_message",
        data={
            "role": "developer",
            "content": content,
            "content_type": "text",
            "source": "agent_work_context",
            "target_agent_id": target_agent_id,
        },
    )
    append_result = await request.app.state.providers.guardrails.record_events(
        session_model.session_id,
        [event],
        source="cognis_agent_work",
        user_email=session_model.user_email,
        agent_id=session_model.agent_id,
    )
    await request.app.state.session_cache.append_recorded_events(
        session_model,
        [event],
        append_result,
    )


@router.post("/{conversation_id}/managed/send", response_model=ManagedConversationActionResponse)
async def managed_conversation_send(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    if link.conversation_state == "closed":
        raise api_exception(409, "closed", "Agent work is closed")
    message = (payload.message or "").strip()
    if not message:
        raise api_exception(400, "invalid_request", "Message is required")
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            conversation_state="open",
            turn_state="running",
            notify_on_completion=not payload.wait,
            last_error=None,
        )
        await session.commit()
    error = await request.app.state.turn_scheduler.submit_turn(
        conversation_id,
        message,
        user_email=user.email,
    )
    active_turn_id = request.app.state.turn_scheduler.active_turn_id(conversation_id)
    if error is not None or active_turn_id is not None:
        async with request.app.state.session_factory() as session:
            await update_managed_conversation_link(
                session,
                link.link_id,
                conversation_state="open",
                turn_state="failed" if error is not None else "running",
                active_turn_id=active_turn_id,
                last_error=error.message if error is not None else None,
            )
            await session.commit()
    if error is not None:
        raise _turn_error_to_http(error)
    result = None
    if payload.wait:
        waited = await request.app.state.turn_scheduler.wait_for_turn(conversation_id)
        result = {
            "kind": waited.__class__.__name__ if waited is not None else "idle",
            "message": getattr(waited, "message", None),
            "result_summary": getattr(waited, "result_summary", None),
        }
    return await _managed_action_response(request, conversation_id, "sent", result)


@router.post("/{conversation_id}/managed/wait", response_model=ManagedConversationActionResponse)
async def managed_conversation_wait(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    await _require_managed_conversation(request, conversation_id)
    waited = await request.app.state.turn_scheduler.wait_for_turn(
        conversation_id,
        timeout_seconds=30 if payload.wait else 0,
    )
    result = {
        "kind": waited.__class__.__name__ if waited is not None else "idle",
        "message": getattr(waited, "message", None),
        "result_summary": getattr(waited, "result_summary", None),
    }
    return await _managed_action_response(request, conversation_id, "waited", result)


@router.post(
    "/{conversation_id}/managed/interrupt", response_model=ManagedConversationActionResponse
)
async def managed_conversation_interrupt(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    cancelled = await request.app.state.turn_scheduler.cancel_turn(conversation_id)
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            turn_state="interrupted" if cancelled else "idle",
            clear_active_turn_id=True,
            last_error=payload.reason or "Interrupted from web UI",
        )
        await session.commit()
    return await _managed_action_response(
        request,
        conversation_id,
        "interrupted" if cancelled else "idle",
    )


@router.post("/{conversation_id}/managed/retry", response_model=ManagedConversationActionResponse)
async def managed_conversation_retry(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    if link.turn_state not in {"failed", "interrupted"}:
        raise api_exception(
            409,
            "not_retryable",
            "Agent work retry is only available after a failed or interrupted turn.",
        )
    message = await _last_agent_work_user_message(request, link)
    if not message:
        raise api_exception(409, "not_retryable", "No previous user message is available to retry")
    return await managed_conversation_send(
        request,
        conversation_id,
        ManagedConversationActionRequest(message=message, wait=payload.wait),
    )


@router.post("/{conversation_id}/managed/close", response_model=ManagedConversationActionResponse)
async def managed_conversation_close(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    _, link = await _require_managed_conversation(request, conversation_id)
    await request.app.state.turn_scheduler.cancel_turn(conversation_id)
    async with request.app.state.session_factory() as session:
        await update_managed_conversation_link(
            session,
            link.link_id,
            conversation_state="closed",
            turn_state="interrupted",
            clear_active_turn_id=True,
            last_error=payload.reason or "Closed from web UI",
            closed=True,
        )
        await session.commit()
    return await _managed_action_response(request, conversation_id, "closed")


@router.post("/{conversation_id}/managed/fork", response_model=ManagedConversationActionResponse)
async def managed_conversation_fork(
    request: Request,
    conversation_id: str,
    payload: ManagedConversationActionRequest,
) -> ManagedConversationActionResponse:
    user, link = await _require_managed_conversation(request, conversation_id)
    active_turn_id = request.app.state.turn_scheduler.active_turn_id(conversation_id)
    from cognis.core.session import _to_conversation_model, _to_session_model
    from cognis.models.agent import AgentDefinition

    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        session_row = (
            await get_session_row(session, link.target_session_id)
            if link.target_session_id
            else None
        )
        agent_row = await get_agent(session, link.target_agent_id)
    if conversation_row is None or session_row is None or agent_row is None:
        raise api_exception(404, "not_found", "Agent work runtime not found")

    target_agent = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
    managed_context_data = {
        "kind": "agent_work",
        "controller_agent_id": link.controller_agent_id,
        "controller_conversation_id": link.controller_conversation_id,
        "controller_session_id": link.controller_session_id,
        "target_agent_id": link.target_agent_id,
        "forked_from_conversation_id": link.target_conversation_id,
        "forked_from_session_id": link.target_session_id,
        "provenance_in_prefix": True,
    }
    managed_context = ConversationContext(
        type="agent_work",
        platform_data=managed_context_data,
    )
    fork_title = f"{link.title or 'Agent work'} (fork)"
    fork_intention = payload.message or f"Forked agent work with {target_agent.name}"
    if active_turn_id:
        (
            new_conversation,
            new_session,
            copied,
        ) = await request.app.state.session_manager.fork_active_turn_checkpoint_into_new_conversation(
            source_session=_to_session_model(session_row),
            source_conversation=_to_conversation_model(conversation_row),
            agent=target_agent,
            user_email=user.email,
            active_turn_id=active_turn_id,
            title=fork_title,
            intention=fork_intention,
            context=managed_context,
            snapshot_extras={"trigger": "managed_conversation_fork"},
        )
    else:
        (
            new_conversation,
            new_session,
            copied,
        ) = await request.app.state.session_manager.fork_into_new_conversation(
            source_session=_to_session_model(session_row),
            source_conversation=_to_conversation_model(conversation_row),
            agent=target_agent,
            user_email=user.email,
            title=fork_title,
            intention=fork_intention,
            context=managed_context,
            snapshot_extras={"trigger": "managed_conversation_fork"},
        )
    if not copied:
        raise api_exception(500, "fork_failed", "Agent work fork did not copy context")
    async with request.app.state.session_factory() as session:
        new_link = await create_managed_conversation_link(
            session,
            user_email=user.email,
            controller_agent_id=link.controller_agent_id,
            controller_conversation_id=link.controller_conversation_id,
            controller_session_id=link.controller_session_id,
            target_agent_id=link.target_agent_id,
            target_conversation_id=new_conversation.conversation_id,
            target_session_id=new_session.session_id,
            title=fork_title,
        )
        await update_conversation_context_data(
            session,
            new_conversation.conversation_id,
            context_data={**managed_context_data, "link_id": new_link.link_id},
        )
        await session.commit()
    await _record_agent_work_context(
        request,
        session_model=new_session,
        controller_agent_id=link.controller_agent_id,
        controller_conversation_id=link.controller_conversation_id,
        controller_session_id=link.controller_session_id,
        target_agent_id=link.target_agent_id,
    )
    if payload.message:
        error = await request.app.state.turn_scheduler.submit_turn(
            new_conversation.conversation_id,
            payload.message,
            user_email=user.email,
        )
        if error is not None:
            raise _turn_error_to_http(error)
    return await _managed_action_response(request, new_conversation.conversation_id, "forked")


async def _try_command_dispatch(
    request: Request,
    conversation_id: str,
    content: str,
    user: Any,
) -> dict[str, Any] | None:
    """Try to dispatch a slash command. Returns result dict or None."""
    command_dispatcher = getattr(request.app.state, "command_dispatcher", None)
    if command_dispatcher is None:
        return None
    if not content.strip().startswith("/"):
        return None

    from cognis.api.serializers import agent_to_response
    from cognis.core.session import _to_conversation_model, _to_session_model
    from cognis.models.agent import AgentDefinition

    session_manager = getattr(request.app.state, "session_manager", None)
    async with request.app.state.session_factory() as session:
        conversation_row = await get_conversation(session, conversation_id)
        if conversation_row is None:
            return None
        agent_row = await get_agent(session, conversation_row.agent_id)
        if agent_row is None:
            return None
        agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        conversation_model = _to_conversation_model(conversation_row)
        session_row = (
            await get_session_row(session, conversation_row.active_session_id)
            if conversation_row.active_session_id
            else None
        )

    if session_row is None:
        if session_manager is None:
            return None
        session_model = await session_manager.ensure_root_session(
            conversation_id=conversation_id,
            user_email=user.email,
            agent_id=conversation_model.agent_id,
            intention=content,
        )
        conversation_model = conversation_model.model_copy(
            update={"active_session_id": session_model.session_id}
        )
    else:
        session_model = _to_session_model(session_row)

    turn_scheduler = getattr(request.app.state, "turn_scheduler", None)
    has_active = turn_scheduler.has_running_turn(conversation_id) if turn_scheduler else False
    has_busy = turn_scheduler.has_active_turn(conversation_id) if turn_scheduler else False

    cmd_result = await command_dispatcher.dispatch(
        content,
        conversation=conversation_model,
        session=session_model,
        agent=agent_model,
        user_email=user.email,
        has_active_turn=has_active,
        has_busy_turn=has_busy,
    )
    if cmd_result is None:
        return None

    return {
        "type": cmd_result.type,
        "text": cmd_result.text,
        "data": cmd_result.data,
    }


def _turn_error_to_http(error: TurnError) -> Exception:
    """Map a TurnError to an HTTP exception."""
    status_map: dict[str, int] = {
        "not_found": 404,
        "forbidden": 403,
        "session_ended": 409,
        "session_suspended": 409,
        "conflict": 409,
        "rate_limited": 429,
        "queue_full": 429,
    }
    status = status_map.get(error.code, 500)
    return api_exception(status, error.code, error.message)


@router.get("/{conversation_id}/sessions", response_model=list[SessionResponse])
async def conversation_sessions(
    request: Request,
    conversation_id: str,
    root_only: bool = Query(default=False),
    active_only: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=500),
    order: Literal["asc", "desc"] = Query(default="asc"),
) -> list[SessionResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        sessions = await list_conversation_sessions(
            session,
            conversation_id,
            root_only=root_only,
            statuses=["active"] if active_only else None,
            order=order,
            limit=limit,
        )
    return [session_to_response(item) for item in sessions]


@router.get("/{conversation_id}/delegations", response_model=list[SessionResponse])
async def active_delegations(request: Request, conversation_id: str) -> list[SessionResponse]:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        sessions = await list_conversation_sessions(
            session,
            conversation_id,
            parent_only=True,
            statuses=["active"],
            order="asc",
            limit=200,
        )
    return [session_to_response(item) for item in sessions]


@router.get(
    "/{conversation_id}/subsessions/{session_id}",
    response_model=SessionResponse,
)
async def conversation_subsession(
    request: Request,
    conversation_id: str,
    session_id: str,
) -> SessionResponse:
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        del row
        session_row = await get_session_row(session, session_id)
    if session_row is None or session_row.conversation_id != conversation_id:
        raise api_exception(404, "not_found", "Session not found in this conversation")
    if session_row.parent_session_id is None:
        raise api_exception(404, "not_found", "Session is not a sub-session")
    return session_to_response(session_row, include_result_content=True)


@router.get(
    "/{conversation_id}/sessions/{session_id}/events",
    response_model=SessionEventsResponse,
)
async def session_events(
    request: Request,
    conversation_id: str,
    session_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> SessionEventsResponse:
    """Read events for a specific session within a conversation.

    Used by the sub-session panel to fetch a child session's event stream.
    """
    async with request.app.state.session_factory() as session:
        row = await get_conversation(session, conversation_id)
        row = _require_visible_conversation(request, row)
        session_row = await get_session_row(session, session_id)
    if session_row is None or session_row.conversation_id != conversation_id:
        raise api_exception(404, "not_found", "Session not found in this conversation")
    event_result = await request.app.state.providers.guardrails.read_events(
        session_id=session_row.intaris_session_id or session_row.session_id,
        after_seq=after_seq,
        limit=limit,
        allow_missing_stream=True,
    )
    if event_result.missing_stream_fallback_used:
        logger.warning(
            "Conversation session history missing in Intaris; returning empty history",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "session_id": session_row.session_id,
                    "intaris_session_id": session_row.intaris_session_id or session_row.session_id,
                }
            },
        )
    await _hydrate_event_attachments(
        request,
        event_result.events,
        conversation_id=conversation_id,
        session_id=session_id,
    )
    return SessionEventsResponse(
        session_id=session_id,
        items=serialize_event_rows(
            event_result.events,
            log_label="conversation_session_events",
            log_context={
                "conversation_id": conversation_id,
                "session_id": session_row.session_id,
            },
        ),
        last_seq=event_result.last_seq,
        has_more=event_result.has_more,
        active_thinking=(
            request.app.state.session_cache.active_thinking_snapshots(session_row.session_id)
            if getattr(request.app.state, "session_cache", None) is not None
            else []
        ),
    )
