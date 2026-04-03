"""Intaris HTTP provider with retry and circuit breaker protection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar

import httpx

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.session import EventAppendResult, EventReadResult, IntarisSession, SessionEvent
from cognis.models.tool import EscalationRecord, EvaluationResult, ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.retry import with_retry
from cognis.runtime_context import current_agent_id, current_user_email

logger = get_logger(__name__)

T = TypeVar("T")


class IntarisProvider:
    """HTTP client for Intaris with fail-closed evaluate semantics.

    All methods are protected by retry with exponential backoff and a
    circuit breaker.  ``evaluate()`` uses a dedicated circuit breaker
    to prevent data-plane failures (e.g. large event batch timeouts)
    from blocking tool safety evaluation.
    """

    def __init__(
        self, base_url: str, auth_provider: Any, user_email: str = "system@example.com"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        # Separate circuit breakers: evaluate is critical-path (fail-closed),
        # data-plane methods (record/read events) have different failure modes.
        self.eval_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.data_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

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

    def _headers(self, agent_id: str = "system", user_email: str | None = None) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        resolved_agent_id = (
            agent_id if agent_id != "system" else (current_agent_id.get() or "system")
        )
        return {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, resolved_agent_id, ['intaris'])}",
            "X-Agent-Id": resolved_agent_id,
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
            breaker=self.data_breaker,
        )
        logger.info(
            "intaris: session created",
            extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
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
    ) -> None:
        body: dict[str, Any] = {"session_id": session_id, "content": content}
        if context is not None:
            body["context"] = context
        if from_events:
            body["from_events"] = True

        async def _do() -> None:
            response = await self.client.post(
                "/api/v1/reasoning",
                json=body,
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris report_reasoning",
            breaker=self.data_breaker,
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
            breaker=self.data_breaker,
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
            breaker=self.data_breaker,
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
            breaker=self.data_breaker,
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
            breaker=self.data_breaker,
        )

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
    ) -> EventAppendResult:
        headers = {**self._headers(user_email=current_user_email.get()), "X-Intaris-Source": source}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        async def _do() -> EventAppendResult:
            response = await self.client.post(
                f"/api/v1/session/{session_id}/events",
                json=[event.model_dump() for event in events],
                headers=headers,
                timeout=30.0,
            )
            if response.status_code == 404:
                logger.warning(
                    "intaris: record_events 404 — session not found",
                    extra={"extra_data": {"session_id": session_id, "event_count": len(events)}},
                )
                return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)
            response.raise_for_status()
            return EventAppendResult.model_validate(response.json())

        result = await self._call_with_retry(
            _do,
            max_retries=3,
            operation=f"intaris record_events({session_id})",
            breaker=self.data_breaker,
        )
        logger.debug(
            "intaris: events recorded",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "count": result.count,
                    "last_seq": result.last_seq,
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
        allow_missing_stream: bool = False,
    ) -> EventReadResult:
        params: dict[str, Any] = {"after_seq": after_seq}
        if limit:
            params["limit"] = limit
        if types:
            params["type"] = ",".join(types)
        if last_n is not None:
            params["last_n"] = last_n
        logger.debug(
            "intaris: read_events",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "after_seq": after_seq,
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
                    logger.debug(
                        "intaris: read_events 404 — new session, returning empty",
                        extra={"extra_data": {"session_id": session_id}},
                    )
                    return EventReadResult(events=[], last_seq=0, has_more=False)
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
            breaker=self.data_breaker,
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

    async def get_last_seq(self, session_id: str) -> int:
        result = await self.read_events(session_id=session_id, last_n=1)
        return result.last_seq

    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        async def _do() -> ToolResult:
            response = await self.client.post(
                "/api/v1/mcp/call",
                json={
                    "session_id": session_id,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
                headers=self._headers(user_email=current_user_email.get()),
                timeout=60.0,
            )
            response.raise_for_status()
            return ToolResult.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris call_mcp_tool({tool_name})",
            breaker=self.data_breaker,
        )

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
                breaker=self.data_breaker,
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
                breaker=self.data_breaker,
            )
        except Exception:
            logger.warning("intaris: list_mcp_tools failed", exc_info=True)
            return []

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
                    details={"data_circuit_state": self.data_breaker.state},
                )
        except Exception as exc:
            return ProviderHealth(
                name="intaris",
                status="degraded",
                error=str(exc),
                circuit_state=self.eval_breaker.state,
                details={"data_circuit_state": self.data_breaker.state},
            )
        return ProviderHealth(
            name="intaris",
            status="degraded",
            circuit_state=self.eval_breaker.state,
            details={"data_circuit_state": self.data_breaker.state},
        )
