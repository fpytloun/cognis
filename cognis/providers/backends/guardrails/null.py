"""Guardrails backend: none — NoGuardrailsProvider.

Wraps the real IntarisProvider and passes through all event-store operations
(record_events, read_events, create_session, get_session, etc.) while
short-circuiting the LLM-backed safety operations:

  evaluate()         → always approve (decision="approve", source="capability-disabled")
  report_reasoning() → no-op

This means:
  - Session content (events, timeline) is still stored in Intaris.
  - No LLM calls are made by Intaris for this agent's sessions.
  - ALL tools are approved, including non-bypassable ones.
    (guardrails=none means no guardrails, period — this is intentional
    and should only be used for trusted/test agents.)

Used for:
  - E2E testing agents that must not depend on Intaris LLM.
  - Future: agents that use a different guardrails backend (e.g. "native").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from cognis.models.session import (
    EventAppendResult,
    EventReadResult,
    IntarisSession,
    IntarisSessionSummaries,
    ReasoningReportResult,
    SessionEvent,
)
from cognis.models.tool import EscalationRecord, EvaluationResult, ToolResult
from cognis.providers.backends import register_backend

if TYPE_CHECKING:
    from cognis.config import CognisConfig
    from cognis.models.search import (
        SearchHealth,
        SearchRequest,
        SearchResponse,
        SearchSessionsRequest,
        SearchSessionsResponse,
    )
    from cognis.providers.guardrails.intaris import IntarisProvider
    from cognis.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class NoGuardrailsProvider:
    """Guardrails provider that approves everything and skips LLM analysis.

    Delegates all event-store operations to the wrapped IntarisProvider.
    Short-circuits evaluate() and report_reasoning() without LLM calls.
    """

    def __init__(self, intaris: IntarisProvider) -> None:
        self._intaris = intaris

    # ------------------------------------------------------------------
    # Short-circuited safety operations (no LLM)
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        """Auto-approve all tool calls — no LLM evaluation."""
        logger.debug(
            "guardrails=none: auto-approving tool %s (session=%s)",
            tool_name,
            session_id,
        )
        return EvaluationResult(
            decision="approve",
            reasoning="Guardrails disabled for this agent (capability-disabled).",
            risk=None,
            path=None,
            latency_ms=0,
            call_id="capability-disabled",
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
        """No-op — skip LLM-backed intention analysis."""
        return ReasoningReportResult(ok=True, intention=None)

    # ------------------------------------------------------------------
    # Event store operations — delegated to IntarisProvider
    # ------------------------------------------------------------------

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
        return await self._intaris.create_session(
            session_id,
            intention,
            agent_id,
            user_id=user_id,
            parent_session_id=parent_session_id,
            policy=policy,
            details=details,
        )

    async def checkpoint(self, session_id: str, content: str) -> None:
        return await self._intaris.checkpoint(session_id, content)

    async def get_session(self, session_id: str) -> IntarisSession:
        return await self._intaris.get_session(session_id)

    async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
        return await self._intaris.get_session_summaries(session_id)

    async def submit_decision(
        self, call_id: str, decision: str, note: str | None = None
    ) -> None:
        return await self._intaris.submit_decision(call_id, decision, note)

    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]:
        return await self._intaris.list_pending_escalations(session_id)

    async def get_escalation(self, call_id: str) -> EscalationRecord | None:
        return await self._intaris.get_escalation(call_id)

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
        return await self._intaris.record_events(
            session_id,
            events,
            source=source,
            idempotency_key=idempotency_key,
            retry_missing_session=retry_missing_session,
            user_email=user_email,
            agent_id=agent_id,
            agent_owner_email=agent_owner_email,
        )

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
        return await self._intaris.read_events(
            session_id,
            after_seq=after_seq,
            limit=limit,
            types=types,
            last_n=last_n,
            before_seq=before_seq,
            seqs=seqs,
            allow_missing_stream=allow_missing_stream,
        )

    async def get_last_seq(
        self, session_id: str, *, allow_missing_stream: bool = False
    ) -> int:
        return await self._intaris.get_last_seq(
            session_id,
            allow_missing_stream=allow_missing_stream,
        )

    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        return await self._intaris.call_mcp_tool(
            session_id, server_name, tool_name, arguments, context
        )

    async def list_mcp_servers(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        return await self._intaris.list_mcp_servers(enabled_only)

    async def list_mcp_tools(self) -> list[dict[str, Any]]:
        return await self._intaris.list_mcp_tools()

    async def search_health(self, user_email: str | None = None) -> SearchHealth:
        return await self._intaris.search_health(user_email)

    async def search(
        self, request: SearchRequest, *, user_email: str | None = None
    ) -> SearchResponse:
        return await self._intaris.search(request, user_email=user_email)

    async def search_sessions(
        self, request: SearchSessionsRequest, *, user_email: str | None = None
    ) -> SearchSessionsResponse:
        return await self._intaris.search_sessions(request, user_email=user_email)

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None:
        return await self._intaris.update_session_status(
            session_id, status, status_reason, user_email
        )

    async def health(self) -> Any:
        return await self._intaris.health()


@register_backend(kind="guardrails", id="none", display_name="No guardrails")
def _factory(config: CognisConfig, registry: ProviderRegistry) -> NoGuardrailsProvider:
    return NoGuardrailsProvider(registry.guardrails)
