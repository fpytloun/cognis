"""Intaris HTTP provider with retry and circuit breaker protection."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar, cast

import httpx

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.search import (
    SearchHealth,
    SearchRequest,
    SearchResponse,
    SearchSessionsRequest,
    SearchSessionsResponse,
)
from cognis.models.session import (
    EventAppendResult,
    EventReadResult,
    IntarisSession,
    IntarisSessionSummaries,
    ReasoningReportResult,
    SessionEvent,
)
from cognis.models.tool import EscalationRecord, EvaluationResult, ToolResult
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.guardrails.events import (
    EventAppendListener,
    EventAppendNotification,
    EventStoreAuthority,
)
from cognis.providers.retry import is_retryable_http_error, with_retry
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email

logger = get_logger(__name__)

T = TypeVar("T")
MAX_EVENT_APPEND_LISTENERS = 16
DEFAULT_EVENT_APPEND_LISTENER_TIMEOUT = 0.1


def _coerce_attachment_b64(value: str) -> str:
    try:
        base64.b64decode(value, validate=True)
        return value
    except Exception:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")


class IntarisProvider:
    """HTTP client for Intaris with fail-closed evaluate semantics.

    All methods are protected by retry with exponential backoff and a
    circuit breaker.  ``evaluate()`` uses a dedicated circuit breaker
    to prevent data-plane failures (e.g. large event batch timeouts)
    from blocking tool safety evaluation.
    """

    def __init__(
        self,
        base_url: str,
        auth_provider: Any,
        user_email: str = SYSTEM_USER_EMAIL,
        *,
        event_append_listener_timeout: float = DEFAULT_EVENT_APPEND_LISTENER_TIMEOUT,
    ) -> None:
        if event_append_listener_timeout <= 0:
            raise ValueError("event_append_listener_timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        # Separate circuit breakers by endpoint family so a failure in one
        # operational path does not unnecessarily poison unrelated calls.
        self.eval_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.reasoning_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.session_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.events_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            should_trip=is_retryable_http_error,
        )
        self.mcp_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.search_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self._event_append_listeners: list[EventAppendListener] = []
        self._event_append_listener_timeout = event_append_listener_timeout

    def add_event_append_listener(self, listener: EventAppendListener) -> bool:
        """Register one listener by identity within the fixed provider bound."""
        if any(registered is listener for registered in self._event_append_listeners):
            return False
        if len(self._event_append_listeners) >= MAX_EVENT_APPEND_LISTENERS:
            raise RuntimeError("maximum event append listener count reached")
        self._event_append_listeners.append(listener)
        return True

    def remove_event_append_listener(self, listener: EventAppendListener) -> bool:
        """Remove one listener by identity."""
        for index, registered in enumerate(self._event_append_listeners):
            if registered is listener:
                del self._event_append_listeners[index]
                return True
        return False

    @staticmethod
    def _event_store_authority(
        *,
        user_email: str | None,
        agent_id: str | None,
        agent_owner_email: str | None,
    ) -> EventStoreAuthority | None:
        values = (
            user_email if user_email is not None else current_user_email.get(),
            agent_id if agent_id is not None else current_agent_id.get(),
            (
                agent_owner_email
                if agent_owner_email is not None
                else current_agent_owner_email.get()
            ),
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            return None
        try:
            complete_values = cast(tuple[str, str, str], values)
            return EventStoreAuthority(*complete_values)
        except ValueError:
            return None

    async def _notify_event_append(self, notification: EventAppendNotification) -> None:
        listeners = tuple(self._event_append_listeners)
        if not listeners:
            return

        async def _invoke(listener: EventAppendListener) -> str:
            try:
                await asyncio.wait_for(
                    listener(notification),
                    timeout=self._event_append_listener_timeout,
                )
            except TimeoutError:
                return "timeout"
            except Exception:
                return "error"
            return "ok"

        statuses = await asyncio.gather(*(_invoke(listener) for listener in listeners))
        failed = sum(status != "ok" for status in statuses)
        if failed:
            logger.warning(
                "intaris: event append listener notification incomplete",
                extra={
                    "extra_data": {
                        "listener_count": len(listeners),
                        "failed_count": failed,
                        "timeout_count": statuses.count("timeout"),
                    }
                },
            )

    async def _call_with_retry(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        max_retries: int = 3,
        base_delay: float = 1.0,
        operation: str = "intaris call",
        breaker: CircuitBreaker | None = None,
        **kwargs: Any,
    ) -> T:
        """Execute a provider call with retry inside circuit breaker.

        Retry handles transient failures (timeouts, 5xx).  The circuit
        breaker trips after repeated exhausted-retry sequences to avoid
        hammering a down service.
        """

        async def _with_retries() -> T:
            return await with_retry(
                fn,
                *args,
                max_retries=max_retries,
                base_delay=base_delay,
                operation=operation,
                **kwargs,
            )

        if breaker is not None:
            return await breaker.call(_with_retries)
        return await _with_retries()

    def _headers(
        self,
        agent_id: str = "system",
        user_email: str | None = None,
        agent_owner_email: str | None = None,
    ) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        resolved_agent_id = (
            agent_id if agent_id != "system" else (current_agent_id.get() or "system")
        )
        resolved_agent_owner = agent_owner_email or current_agent_owner_email.get() or subject
        return {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, resolved_agent_id, ['intaris'], agent_owner_email=resolved_agent_owner)}",
            "X-Agent-Id": resolved_agent_id,
            "X-Agent-Owner": resolved_agent_owner,
        }

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        logger.info(
            "intaris: create_session",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "parent_session_id": parent_session_id,
                }
            },
        )

        async def _verify_existing_session() -> None:
            response = await self.client.get(
                f"/api/v1/session/{session_id}",
                headers=self._headers(agent_id, user_id),
            )
            response.raise_for_status()

        async def _do() -> None:
            response = await self.client.post(
                "/api/v1/intention",
                json={
                    "session_id": session_id,
                    "intention": intention,
                    "details": details or {},
                    "policy": policy or {},
                    "parent_session_id": parent_session_id,
                },
                headers=self._headers(agent_id, user_id),
            )
            if response.status_code == 409:
                # A retry may follow a timeout after Intaris durably created
                # the requested session. Verify that exact session rather
                # than turning an ambiguous create into workflow failure.
                await self._call_with_retry(
                    _verify_existing_session,
                    max_retries=2,
                    operation=f"intaris verify existing session({session_id})",
                )
                logger.info(
                    "intaris: session already existed after create retry",
                    extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
                )
                return
            if not response.is_success:
                logger.error(
                    "intaris: create_session failed",
                    extra={
                        "extra_data": {
                            "session_id": session_id,
                            "status_code": response.status_code,
                        }
                    },
                )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris create_session({session_id})",
            breaker=self.session_breaker,
        )
        logger.info(
            "intaris: session created",
            extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
        )

    async def update_session_policy(
        self,
        session_id: str,
        *,
        agent_id: str,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        logger.info(
            "intaris: update_session_policy",
            extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
        )

        async def _do() -> None:
            response = await self.client.patch(
                f"/api/v1/session/{session_id}",
                json={"details": details or {}, "policy": policy or {}},
                headers=self._headers(agent_id, user_id),
            )
            if not response.is_success:
                logger.error(
                    "intaris: update_session_policy failed",
                    extra={
                        "extra_data": {
                            "session_id": session_id,
                            "status_code": response.status_code,
                        }
                    },
                )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris update_session_policy({session_id})",
            breaker=self.session_breaker,
        )

    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        logger.debug(
            "intaris: evaluate",
            extra={"extra_data": {"session_id": session_id, "tool_name": tool_name}},
        )

        async def _do() -> EvaluationResult:
            response = await self.client.post(
                "/api/v1/evaluate",
                json={
                    "session_id": session_id,
                    "tool": tool_name,
                    "args": arguments,
                    "context": context or {},
                },
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            return EvaluationResult.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            base_delay=0.5,
            operation=f"intaris evaluate({tool_name})",
            breaker=self.eval_breaker,
        )

    async def report_reasoning(
        self,
        session_id: str,
        content: str = "",
        context: str | None = None,
        *,
        from_events: bool = False,
        wait_for_intention: bool = False,
        wait_timeout_ms: int | None = None,
    ) -> ReasoningReportResult:
        body: dict[str, Any] = {"session_id": session_id, "content": content}
        if context is not None:
            body["context"] = context
        if from_events:
            body["from_events"] = True
        if wait_for_intention:
            body["wait_for_intention"] = True
        if wait_for_intention and wait_timeout_ms is not None:
            body["wait_timeout_ms"] = wait_timeout_ms

        async def _do() -> ReasoningReportResult:
            response = await self.client.post(
                "/api/v1/reasoning",
                json=body,
                headers=self._headers(user_email=current_user_email.get()),
            )
            if wait_for_intention and response.status_code in {400, 422}:
                fallback_body = dict(body)
                fallback_body.pop("wait_for_intention", None)
                fallback_body.pop("wait_timeout_ms", None)
                fallback = await self.client.post(
                    "/api/v1/reasoning",
                    json=fallback_body,
                    headers=self._headers(user_email=current_user_email.get()),
                )
                fallback.raise_for_status()
                return ReasoningReportResult.model_validate(fallback.json())
            response.raise_for_status()
            return ReasoningReportResult.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris report_reasoning",
            breaker=self.reasoning_breaker,
        )

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status}
        if status_reason is not None:
            body["status_reason"] = status_reason

        async def _do() -> None:
            response = await self.client.patch(
                f"/api/v1/session/{session_id}/status",
                json=body,
                headers=self._headers(user_email=user_email or current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris update_session_status({session_id})",
            breaker=self.session_breaker,
        )

    async def checkpoint(self, session_id: str, content: str) -> None:
        async def _do() -> None:
            response = await self.client.post(
                f"/api/v1/session/{session_id}/checkpoint",
                json={"content": content},
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris checkpoint({session_id})",
            breaker=self.session_breaker,
        )

    async def get_session(self, session_id: str) -> IntarisSession:
        async def _do() -> IntarisSession:
            response = await self.client.get(
                f"/api/v1/session/{session_id}",
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            return IntarisSession.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris get_session({session_id})",
            breaker=self.session_breaker,
        )

    async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
        async def _do() -> IntarisSessionSummaries:
            response = await self.client.get(
                f"/api/v1/session/{session_id}/summary",
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            return IntarisSessionSummaries.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris get_session_summaries({session_id})",
            breaker=self.session_breaker,
        )

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        async def _do() -> None:
            response = await self.client.post(
                "/api/v1/decision",
                json={"call_id": call_id, "decision": decision, "note": note},
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris submit_decision",
            breaker=self.session_breaker,
        )

    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]:
        params = {"decision": "escalate", "resolved": "false"}
        if session_id is not None:
            params["session_id"] = session_id

        async def _do() -> list[EscalationRecord]:
            response = await self.client.get(
                "/api/v1/audit",
                params=params,
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            return [EscalationRecord.model_validate(item) for item in items]

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris list_pending_escalations",
            breaker=self.session_breaker,
        )

    async def get_escalation(self, call_id: str) -> EscalationRecord | None:
        async def _do() -> EscalationRecord | None:
            response = await self.client.get(
                "/api/v1/audit",
                params={"call_id": call_id},
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None
            for item in items:
                record = EscalationRecord.model_validate(item)
                if record.call_id == call_id:
                    return record
            return None

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris get_escalation",
            breaker=self.session_breaker,
        )

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        retry_missing_session: bool = False,
        user_email: str | None = None,
        agent_id: str | None = None,
        agent_owner_email: str | None = None,
    ) -> EventAppendResult:
        authority = self._event_store_authority(
            user_email=user_email,
            agent_id=agent_id,
            agent_owner_email=agent_owner_email,
        )
        headers = {
            **self._headers(
                agent_id=authority.agent_id if authority else (agent_id or "system"),
                user_email=authority.user_email if authority else user_email,
                agent_owner_email=(authority.agent_owner_email if authority else agent_owner_email),
            ),
            "X-Intaris-Source": source,
        }
        params = {"idempotency_key": idempotency_key} if idempotency_key is not None else None
        missing_session_delays = (0.05, 0.1, 0.25) if retry_missing_session else ()
        missing_session_attempt = 0

        async def _do() -> EventAppendResult:
            nonlocal missing_session_attempt
            response = await self.client.post(
                f"/api/v1/session/{session_id}/events",
                json=[event.model_dump() for event in events],
                headers=headers,
                params=params,
                timeout=30.0,
            )
            if response.status_code == 404:
                if missing_session_attempt < len(missing_session_delays):
                    delay = missing_session_delays[missing_session_attempt]
                    missing_session_attempt += 1
                    logger.warning(
                        "intaris: record_events 404 — session not found, retrying",
                        extra={
                            "extra_data": {
                                "event_count": len(events),
                                "attempt": missing_session_attempt,
                                "delay_seconds": delay,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
                    return await _do()
                logger.warning(
                    "intaris: record_events 404 — session not found",
                    extra={"extra_data": {"event_count": len(events)}},
                )
                return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)
            response.raise_for_status()
            return EventAppendResult.model_validate(response.json())

        result = await self._call_with_retry(
            _do,
            max_retries=3,
            operation="intaris record_events",
            breaker=self.events_breaker,
        )
        logger.debug(
            "intaris: events recorded",
            extra={
                "extra_data": {
                    "count": result.count,
                    "last_seq": result.last_seq,
                }
            },
        )
        if result.ok and authority is not None:
            try:
                notification = EventAppendNotification(
                    authority=authority,
                    session_id=session_id,
                    first_seq=result.first_seq,
                    last_seq=result.last_seq,
                    event_count=result.count,
                    events=tuple(events) if len(events) == result.count else (),
                )
            except ValueError:
                logger.warning(
                    "intaris: event append notification skipped",
                    extra={
                        "extra_data": {
                            "listener_count": len(self._event_append_listeners),
                            "reason": "invalid_append_range",
                        }
                    },
                )
            else:
                await self._notify_event_append(notification)
        elif result.ok and self._event_append_listeners:
            logger.warning(
                "intaris: event append notification skipped",
                extra={
                    "extra_data": {
                        "listener_count": len(self._event_append_listeners),
                        "reason": "authority_unavailable",
                    }
                },
            )
        return result

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
        before_seq: int | None = None,
        seqs: list[int] | None = None,
        allow_missing_stream: bool = False,
    ) -> EventReadResult:
        params: dict[str, Any] = {}
        if seqs:
            params["seqs"] = ",".join(str(seq) for seq in seqs)
        elif last_n is not None:
            params["last_n"] = last_n
        elif before_seq is not None:
            params["before_seq"] = before_seq
            if limit:
                params["limit"] = limit
        else:
            params["after_seq"] = after_seq
            if limit:
                params["limit"] = limit
        if types:
            params["type"] = ",".join(types)
        logger.debug(
            "intaris: read_events",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "seqs": seqs,
                    "allow_missing_stream": allow_missing_stream,
                }
            },
        )

        async def _do() -> EventReadResult:
            response = await self.client.get(
                f"/api/v1/session/{session_id}/events",
                params=params,
                headers=self._headers(user_email=current_user_email.get()),
                timeout=15.0,
            )
            if response.status_code == 404:
                if allow_missing_stream:
                    session_response = await self.client.get(
                        f"/api/v1/session/{session_id}",
                        headers=self._headers(user_email=current_user_email.get()),
                        timeout=15.0,
                    )
                    session_response.raise_for_status()
                    logger.debug(
                        "intaris: read_events 404 — verified empty session stream",
                        extra={"extra_data": {"session_id": session_id}},
                    )
                    return EventReadResult(
                        events=[],
                        last_seq=0,
                        has_more=False,
                        missing_stream_fallback_used=True,
                    )
                logger.warning(
                    "intaris: read_events 404 — event stream not found",
                    extra={"extra_data": {"session_id": session_id, "after_seq": after_seq}},
                )
            response.raise_for_status()
            return EventReadResult.model_validate(response.json())

        result = await self._call_with_retry(
            _do,
            max_retries=3,
            operation=f"intaris read_events({session_id})",
            breaker=self.events_breaker,
        )
        logger.debug(
            "intaris: read_events complete",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "event_count": len(result.events),
                    "last_seq": result.last_seq,
                }
            },
        )
        return result

    async def get_last_seq(self, session_id: str, *, allow_missing_stream: bool = False) -> int:
        result = await self.read_events(
            session_id=session_id,
            last_n=1,
            allow_missing_stream=allow_missing_stream,
        )
        return result.last_seq

    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        async def _do() -> ToolResult:
            response = await self.client.post(
                "/api/v1/mcp/call",
                json={
                    "session_id": session_id,
                    "server": server_name,
                    "tool": tool_name,
                    "arguments": arguments,
                    "context": context or {},
                },
                headers=self._headers(user_email=current_user_email.get()),
                timeout=60.0,
            )
            response.raise_for_status()
            return self._normalize_mcp_tool_result(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris call_mcp_tool({tool_name})",
            breaker=self.mcp_breaker,
        )

    def _normalize_mcp_tool_result(self, payload: Any) -> ToolResult:
        """Normalize Intaris MCP REST responses into Cognis ToolResult."""

        if isinstance(payload, dict) and "output" in payload:
            return ToolResult.model_validate(payload)

        if not isinstance(payload, dict):
            return ToolResult(output=str(payload), is_error=True)

        content = payload.get("content")
        text_chunks: list[str] = []
        attachments: list[dict[str, Any]] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    text_chunks.append(str(block))
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_chunks.append(text)
                elif block_type == "image":
                    attachment = self._normalize_mcp_image_block(block)
                    if attachment is not None:
                        attachments.append(attachment)
                        text_chunks.append("[image attachment returned]")
                elif block_type == "resource":
                    attachment = self._normalize_mcp_resource_block(block)
                    if attachment is not None:
                        attachments.append(attachment)
                        text_chunks.append("[resource attachment returned]")
                    else:
                        text_chunks.append(str(block))
                else:
                    text_chunks.append(str(block))
        elif isinstance(content, str):
            text_chunks.append(content)

        output = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        if not output:
            output = "[No content returned from Intaris MCP proxy]"

        metadata = {
            key: value for key, value in payload.items() if key not in {"content", "isError"}
        }
        return ToolResult(
            output=output,
            is_error=bool(payload.get("isError", False)),
            duration_ms=(
                int(payload["latency_ms"])
                if isinstance(payload.get("latency_ms"), (int, float))
                else None
            ),
            metadata=metadata or None,
            attachments=attachments or None,
        )

    def _normalize_mcp_image_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        data = block.get("data") or block.get("image") or block.get("bytes")
        if isinstance(data, bytes):
            content_b64 = base64.b64encode(data).decode("ascii")
        elif isinstance(data, str):
            content_b64 = _coerce_attachment_b64(data)
        else:
            return None
        mime_type = str(block.get("mimeType") or block.get("mime_type") or "image/png")
        return {
            "content_b64": content_b64,
            "mime_type": mime_type,
            "filename": str(block.get("filename") or "mcp-image"),
            "kind": "image",
        }

    def _normalize_mcp_resource_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        resource = block.get("resource")
        if isinstance(resource, dict):
            data = resource.get("blob") or resource.get("data")
            mime_type = str(
                resource.get("mimeType") or resource.get("mime_type") or "application/octet-stream"
            )
            filename = str(resource.get("filename") or block.get("filename") or "mcp-resource")
        else:
            data = block.get("data")
            mime_type = str(
                block.get("mimeType") or block.get("mime_type") or "application/octet-stream"
            )
            filename = str(block.get("filename") or "mcp-resource")
        if isinstance(data, bytes):
            content_b64 = base64.b64encode(data).decode("ascii")
        elif isinstance(data, str):
            content_b64 = _coerce_attachment_b64(data)
        else:
            return None
        return {
            "content_b64": content_b64,
            "mime_type": mime_type,
            "filename": filename,
            "kind": "pdf" if mime_type == "application/pdf" else "file",
        }

    async def list_mcp_servers(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        """List available MCP servers from Intaris."""
        params: dict[str, str] = {}
        if enabled_only:
            params["enabled_only"] = "true"
        try:

            async def _do() -> list[dict[str, Any]]:
                response = await self.client.get(
                    "/api/v1/mcp/servers",
                    params=params,
                    headers=self._headers(user_email=current_user_email.get()),
                )
                response.raise_for_status()
                data = response.json()
                items: list[dict[str, Any]] = (
                    data.get("items", []) if isinstance(data, dict) else list(data)
                )
                return items

            return await self._call_with_retry(
                _do,
                max_retries=1,
                operation="intaris list_mcp_servers",
                breaker=self.mcp_breaker,
            )
        except Exception:
            logger.warning("intaris: list_mcp_servers failed", exc_info=True)
            return []

    async def list_mcp_tools(self) -> list[dict[str, Any]]:
        """List all aggregated MCP tools from Intaris."""
        try:

            async def _do() -> list[dict[str, Any]]:
                response = await self.client.get(
                    "/api/v1/mcp/tools",
                    headers=self._headers(user_email=current_user_email.get()),
                )
                response.raise_for_status()
                data = response.json()
                tools: list[dict[str, Any]] = (
                    data.get("tools", []) if isinstance(data, dict) else list(data)
                )
                return tools

            return await self._call_with_retry(
                _do,
                max_retries=1,
                operation="intaris list_mcp_tools",
                breaker=self.mcp_breaker,
            )
        except Exception:
            logger.warning("intaris: list_mcp_tools failed", exc_info=True)
            return []

    async def search_health(self, user_email: str | None = None) -> SearchHealth:
        """Return Intaris search health, treating missing search routes as disabled."""

        try:

            async def _do() -> SearchHealth:
                response = await self.client.get(
                    "/api/v1/search/health",
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=5.0,
                )
                if response.status_code == 404:
                    return SearchHealth(enabled=False, notes=["search_disabled"])
                response.raise_for_status()
                return SearchHealth.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search_health",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search_health failed", exc_info=True)
            return SearchHealth(enabled=False, notes=["search_unavailable"])

    async def search(
        self, request: SearchRequest, *, user_email: str | None = None
    ) -> SearchResponse:
        """Run flat Intaris search with fail-soft semantics."""

        try:

            async def _do() -> SearchResponse:
                response = await self.client.post(
                    "/api/v1/search",
                    json=request.model_dump(mode="json", exclude_none=True),
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=15.0,
                )
                if response.status_code == 404:
                    return SearchResponse()
                response.raise_for_status()
                return SearchResponse.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search failed", exc_info=True)
            return SearchResponse()

    async def search_sessions(
        self, request: SearchSessionsRequest, *, user_email: str | None = None
    ) -> SearchSessionsResponse:
        """Run aggregated Intaris search with fail-soft semantics."""

        try:

            async def _do() -> SearchSessionsResponse:
                response = await self.client.post(
                    "/api/v1/search/sessions",
                    json=request.model_dump(mode="json", exclude_none=True),
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=15.0,
                )
                if response.status_code == 404:
                    return SearchSessionsResponse()
                response.raise_for_status()
                return SearchSessionsResponse.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search_sessions",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search_sessions failed", exc_info=True)
            return SearchSessionsResponse()

    async def health(self) -> ProviderHealth:
        start = perf_counter()
        try:
            response = await self.client.get("/health", headers=self._headers())
            latency_ms = (perf_counter() - start) * 1000
            if response.is_success:
                return ProviderHealth(
                    name="intaris",
                    status="healthy",
                    latency_ms=latency_ms,
                    circuit_state=self.eval_breaker.state,
                    details={
                        "reasoning_circuit_state": self.reasoning_breaker.state,
                        "session_circuit_state": self.session_breaker.state,
                        "events_circuit_state": self.events_breaker.state,
                        "mcp_circuit_state": self.mcp_breaker.state,
                        "search_circuit_state": self.search_breaker.state,
                    },
                )
        except Exception as exc:
            return ProviderHealth(
                name="intaris",
                status="degraded",
                error=str(exc),
                circuit_state=self.eval_breaker.state,
                details={
                    "reasoning_circuit_state": self.reasoning_breaker.state,
                    "session_circuit_state": self.session_breaker.state,
                    "events_circuit_state": self.events_breaker.state,
                    "mcp_circuit_state": self.mcp_breaker.state,
                    "search_circuit_state": self.search_breaker.state,
                },
            )
        return ProviderHealth(
            name="intaris",
            status="degraded",
            circuit_state=self.eval_breaker.state,
            details={
                "reasoning_circuit_state": self.reasoning_breaker.state,
                "session_circuit_state": self.session_breaker.state,
                "events_circuit_state": self.events_breaker.state,
                "mcp_circuit_state": self.mcp_breaker.state,
                "search_circuit_state": self.search_breaker.state,
            },
        )
