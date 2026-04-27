"""SSE (Server-Sent Events) transport adapter for REST chat streaming.

Implements ``TurnObserver`` to bridge TurnScheduler streaming callbacks
into an SSE event stream delivered via ``StreamingResponse``.

Usage::

    observer = SSETurnObserver(conversation_id)
    turn_scheduler.add_observer(conversation_id, observer)
    error = await turn_scheduler.submit_turn(...)
    if error:
        turn_scheduler.remove_observer(conversation_id, observer)
        raise ...
    return StreamingResponse(
        observer.event_generator(),
        media_type="text/event-stream",
    )
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from prometheus_client import Gauge

from cognis.core.attachment_utils import strip_attachment_payload_bytes
from cognis.core.turn_scheduler import TurnError, TurnResult
from cognis.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

SSE_CONNECTIONS_ACTIVE = Gauge(
    "cognis_sse_connections_active",
    "Active SSE streaming connections",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEEPALIVE_INTERVAL_SECONDS = 15


# ---------------------------------------------------------------------------
# SSETurnObserver
# ---------------------------------------------------------------------------


class SSETurnObserver:
    """TurnObserver that pipes streaming events into an SSE response.

    Each observer method pushes an SSE event dict onto an internal
    ``asyncio.Queue``. The ``event_generator()`` async generator yields
    formatted SSE lines until the turn completes (signaled by a ``None``
    sentinel on the queue).

    Events for other conversations are silently ignored (the observer
    filters by ``conversation_id``).

    A keepalive comment (``: keepalive``) is emitted every 15 seconds
    to prevent proxy/load-balancer idle disconnections.
    """

    def __init__(self, conversation_id: str) -> None:
        self._conversation_id = conversation_id
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._done = False

    # ------------------------------------------------------------------
    # TurnObserver protocol implementation
    # ------------------------------------------------------------------

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        await self._queue.put(
            {
                "event": "token",
                "data": {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "turn_id": turn_id,
                    "delta": delta,
                    "index": chunk_index if chunk_index is not None else 0,
                    "content_offset": content_offset if content_offset is not None else 0,
                },
            }
        )

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        turn_id: str | None,
    ) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        await self._queue.put(
            {
                "event": "tool_call",
                "data": {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "status": "started",
                    "turn_id": turn_id,
                },
            }
        )

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None = None,
        turn_id: str | None = None,
    ) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        await self._queue.put(
            {
                "event": "tool_result",
                "data": {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "result": result,
                    "is_error": is_error,
                    "duration_ms": duration_ms,
                    "attachments": strip_attachment_payload_bytes(attachments or []),
                    "turn_id": turn_id,
                },
            }
        )

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        block_id: str,
        delta: str,
        title: str | None,
        complete: bool,
        content: str | None = None,
    ) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        if delta:
            await self._queue.put(
                {
                    "event": "thinking",
                    "data": {
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "block_id": block_id,
                        "delta": delta,
                        "title": title,
                        "complete": complete,
                    },
                }
            )
        if complete:
            await self._queue.put(
                {
                    "event": "thinking_block",
                    "data": {
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "block_id": block_id,
                        "title": title,
                        "complete": True,
                        "content": content,
                    },
                }
            )

    async def on_turn_complete(self, result: TurnResult) -> None:
        if result.conversation_id != self._conversation_id or self._done:
            return
        self._done = True
        await self._queue.put(
            {
                "event": "complete",
                "data": {
                    "conversation_id": result.conversation_id,
                    "session_id": result.session_id,
                    "message_id": result.message_id,
                    "turn_id": result.turn_id,
                    "last_seq": result.last_seq,
                    "context_usage": result.context_usage,
                    "delegated": result.delegated,
                    "task_id": result.task_id,
                    "attachments": strip_attachment_payload_bytes(result.attachments or []),
                },
            }
        )
        await self._queue.put(None)  # sentinel

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        self._done = True
        await self._queue.put(
            {
                "event": "error",
                "data": {
                    "code": error.code,
                    "message": error.message,
                    "recoverable": error.recoverable,
                },
            }
        )
        await self._queue.put(None)  # sentinel

    async def on_system_message(self, conversation_id: str, text: str) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        await self._queue.put(
            {
                "event": "system",
                "data": {
                    "conversation_id": conversation_id,
                    "text": text,
                },
            }
        )

    async def on_queued(self, conversation_id: str, queued_count: int) -> None:
        if conversation_id != self._conversation_id or self._done:
            return
        await self._queue.put(
            {
                "event": "queued",
                "data": {
                    "conversation_id": conversation_id,
                    "queued_count": queued_count,
                },
            }
        )

    # ------------------------------------------------------------------
    # SSE event generator
    # ------------------------------------------------------------------

    async def event_generator(self) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted events until the turn completes.

        Emits ``: keepalive`` comment frames every 15 seconds to prevent
        proxy/load-balancer idle disconnections.
        """
        SSE_CONNECTIONS_ACTIVE.inc()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=_KEEPALIVE_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    # Emit keepalive comment (SSE spec: lines starting with
                    # ":" are comments and ignored by EventSource clients)
                    yield ": keepalive\n\n"
                    continue

                if item is None:
                    break

                event_type = item.get("event", "message")
                data = json.dumps(item.get("data", {}), separators=(",", ":"))
                yield f"event: {event_type}\ndata: {data}\n\n"
        finally:
            SSE_CONNECTIONS_ACTIVE.dec()
