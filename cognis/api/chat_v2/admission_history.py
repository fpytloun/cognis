"""Positive-only canonical input confirmation for owned message receipts."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from cognis.core.historical_tool_output import source_session_authority
from cognis.logging import get_logger
from cognis.providers.circuit_breaker import CircuitBreakerError
from cognis.runtime_context import scoped_runtime_context
from cognis.store.chat_admissions import MessageAdmissionState
from cognis.store.queries import get_conversation, get_root_session_chain

logger = get_logger(__name__)
HISTORY_PAGE_SIZE = 200
HISTORY_MAX_PAGES = 8
HISTORY_MAX_STREAMS = 8
HISTORY_TIMEOUT_SECONDS = 5


async def confirm_admitted_input(
    app: Any,
    *,
    conversation_id: str,
    principal_id: str,
    admission: MessageAdmissionState,
) -> bool:
    """Confirm exact user-event identity; false means unknown, never absent.

    The DB owns receipt/lineage metadata only. Its session ends before Intaris
    reads, which reuse source-session authority and never replay execution.
    """
    try:
        async with asyncio.timeout(HISTORY_TIMEOUT_SECONDS):
            async with app.state.session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                if (
                    conversation is None
                    or conversation.status == "deleted"
                    or conversation.user_email != principal_id
                    or not conversation.active_session_id
                ):
                    return False
                rows, truncated = await get_root_session_chain(
                    session, conversation_id, conversation.active_session_id, max_depth=64
                )
            if (
                truncated
                or not rows
                or rows[0].previous_session_id
                or any(row.user_email != principal_id for row in rows)
            ):
                return False
            candidates = sorted(
                reversed(rows), key=lambda row: row.session_id != admission.session_id
            )[:HISTORY_MAX_STREAMS]
            pages_left = HISTORY_MAX_PAGES
            for row in candidates:
                authority = await source_session_authority(
                    app.state.registry, row, user_email=principal_id
                )
                storage_id = row.intaris_session_id or row.session_id
                after_seq = 0
                while pages_left:
                    pages_left -= 1
                    with scoped_runtime_context(
                        user_email=authority.user_email,
                        agent_id=authority.agent_id,
                        agent_owner_email=authority.agent_owner_email,
                    ):
                        result = await app.state.providers.guardrails.read_events(
                            storage_id,
                            after_seq=after_seq,
                            limit=HISTORY_PAGE_SIZE,
                            types=["user_message", "message"],
                            allow_missing_stream=True,
                        )
                    next_seq = after_seq
                    for event in result.events:
                        seq = event.get("seq")
                        if (
                            not isinstance(seq, int)
                            or isinstance(seq, bool)
                            or seq <= next_seq
                            or event.get("session_id", storage_id) != storage_id
                        ):
                            return False
                        next_seq = seq
                        data = event.get("data")
                        if not isinstance(data, dict) or data.get("synthetic"):
                            continue
                        if not (
                            event.get("type") == "user_message"
                            and data.get("role", "user") == "user"
                            or event.get("type") == "message"
                            and data.get("role") == "user"
                        ):
                            continue
                        if (
                            data.get("client_message_id") == admission.client_message_id
                            and data.get("turn_id") == admission.turn_id
                        ):
                            return True
                    if not result.has_more:
                        break
                    if next_seq <= after_seq:
                        return False
                    after_seq = next_seq
            return False
    except (TimeoutError, httpx.HTTPError, CircuitBreakerError, ValueError) as exc:
        logger.warning(
            "Canonical admission confirmation unavailable",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "turn_id": admission.turn_id,
                    "error_type": type(exc).__name__,
                }
            },
        )
        return False
