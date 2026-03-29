"""Intaris HTTP provider."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.session import EventAppendResult, EventReadResult, IntarisSession, SessionEvent
from cognis.models.tool import EscalationRecord, EvaluationResult, ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.runtime_context import current_user_email

logger = get_logger(__name__)


class IntarisProvider:
    """HTTP client for Intaris with fail-closed evaluate semantics."""

    def __init__(
        self, base_url: str, auth_provider: Any, user_email: str = "system@example.com"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        self.breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=15.0)

    def _headers(self, agent_id: str = "system", user_email: str | None = None) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        return {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, agent_id, ['intaris'])}",
            "X-Agent-Id": agent_id,
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
                    "extra_data": {"session_id": session_id, "status_code": response.status_code}
                },
            )
        response.raise_for_status()
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
        response = await self.breaker.call(
            lambda: self.client.post(
                "/api/v1/evaluate",
                json={
                    "session_id": session_id,
                    "tool": tool_name,
                    "args": arguments,
                    "context": context or {},
                },
                headers=self._headers(user_email=current_user_email.get()),
            )
        )
        response.raise_for_status()
        return EvaluationResult.model_validate(response.json())

    async def report_reasoning(
        self, session_id: str, content: str, context: str | None = None
    ) -> None:
        response = await self.client.post(
            "/api/v1/reasoning",
            json={"session_id": session_id, "content": content, "context": context},
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()

    async def checkpoint(self, session_id: str, content: str) -> None:
        response = await self.client.post(
            f"/api/v1/session/{session_id}/checkpoint",
            json={"content": content},
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()

    async def get_session(self, session_id: str) -> IntarisSession:
        response = await self.client.get(
            f"/api/v1/session/{session_id}",
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()
        return IntarisSession.model_validate(response.json())

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        response = await self.client.post(
            "/api/v1/decision",
            json={"call_id": call_id, "decision": decision, "note": note},
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()

    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]:
        params = {"decision": "escalate", "resolved": "false"}
        if session_id is not None:
            params["session_id"] = session_id
        response = await self.client.get(
            "/api/v1/audit",
            params=params,
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        return [EscalationRecord.model_validate(item) for item in items]

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
        response = await self.client.post(
            f"/api/v1/session/{session_id}/events",
            json=[event.model_dump() for event in events],
            headers=headers,
        )
        if response.status_code == 404:
            logger.warning(
                "intaris: record_events 404 — session not found",
                extra={"extra_data": {"session_id": session_id, "event_count": len(events)}},
            )
            return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)
        response.raise_for_status()
        result = EventAppendResult.model_validate(response.json())
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
        response = await self.client.get(
            f"/api/v1/session/{session_id}/events",
            params=params,
            headers=self._headers(user_email=current_user_email.get()),
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
        result = EventReadResult.model_validate(response.json())
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
        response = await self.client.post(
            "/api/v1/mcp/call",
            json={
                "session_id": session_id,
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            headers=self._headers(user_email=current_user_email.get()),
        )
        response.raise_for_status()
        return ToolResult.model_validate(response.json())

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
                    circuit_state=self.breaker.state,
                )
        except Exception as exc:
            return ProviderHealth(
                name="intaris", status="degraded", error=str(exc), circuit_state=self.breaker.state
            )
        return ProviderHealth(name="intaris", status="degraded", circuit_state=self.breaker.state)
