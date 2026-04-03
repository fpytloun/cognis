"""Mnemory HTTP provider."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.config import ProviderHealth
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.runtime_context import current_agent_id, current_user_email

logger = get_logger(__name__)


class MnemoryProvider:
    """HTTP client for Mnemory with graceful degradation."""

    def __init__(
        self, base_url: str, auth_provider: Any, user_email: str = "system@example.com"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=60)
        self.breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    def _headers(
        self, agent_id: str | None = None, user_email: str | None = None
    ) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        resolved_agent_id = agent_id or current_agent_id.get()
        headers = {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, resolved_agent_id or 'system', ['mnemory'])}",
        }
        if resolved_agent_id is not None:
            headers["X-Agent-Id"] = resolved_agent_id
        return headers

    async def recall(
        self,
        query: str,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        search_mode: str = "find",
        include_instructions: bool = False,
        managed: bool = False,
        instruction_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "query": query,
            "messages": [{"role": "user", "content": query}],
            "include_instructions": include_instructions,
            "managed": managed,
            "search_mode": search_mode,
            "context": context,
            "labels": labels or {},
            "ttl": 86400,
        }
        if instruction_mode is not None:
            payload["instruction_mode"] = instruction_mode
        logger.info(
            "mnemory: recall started",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "search_mode": search_mode,
                    "query_len": len(query),
                }
            },
        )
        try:
            response = await self.breaker.call(
                lambda: self.client.post("/api/recall", json=payload, headers=self._headers())
            )
            response.raise_for_status()
            result = dict(response.json())
            stats = result.get("stats", {})
            logger.info(
                "mnemory: recall complete",
                extra={
                    "extra_data": {
                        "session_id": session_id,
                        "mnemory_session": result.get("session_id"),
                        "has_instructions": bool(result.get("instructions")),
                        "has_core": bool(result.get("core_memories")),
                        "core_count": stats.get("core_count", 0),
                        "search_count": stats.get("search_count", 0),
                        "new_count": stats.get("new_count", 0),
                        "latency_ms": stats.get("latency_ms", 0),
                    }
                },
            )
            return result
        except Exception:
            logger.warning(
                "mnemory: recall failed",
                extra={"extra_data": {"session_id": session_id, "search_mode": search_mode}},
                exc_info=True,
            )
            return {
                "session_id": session_id or "",
                "instructions": None,
                "core_memories": None,
                "search_results": [],
                "stats": {
                    "core_count": 0,
                    "search_count": 0,
                    "new_count": 0,
                    "known_skipped": 0,
                    "latency_ms": 0,
                },
            }

    async def remember(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        user_email: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        logger.info(
            "mnemory: remember",
            extra={"extra_data": {"session_id": session_id, "message_count": len(messages)}},
        )
        payload = {
            "session_id": session_id,
            "messages": messages,
            "role": role,
            "labels": labels or {},
            "context": context,
        }
        try:
            response = await self.breaker.call(
                lambda: self.client.post(
                    "/api/remember",
                    json=payload,
                    headers=self._headers(agent_id=agent_id, user_email=user_email),
                )
            )
            response.raise_for_status()
        except Exception:
            logger.warning(
                "mnemory: remember failed",
                extra={"extra_data": {"session_id": session_id}},
                exc_info=True,
            )
            raise

    async def add_memory(
        self,
        content: str,
        memory_type: str | None = None,
        categories: list[str] | None = None,
        importance: str | None = None,
        role: str = "user",
        pinned: bool = False,
        labels: dict[str, Any] | None = None,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> str:
        payload = {
            "content": content,
            "memory_type": memory_type,
            "categories": categories,
            "importance": importance,
            "role": role,
            "pinned": pinned,
            "labels": labels or {},
        }
        response = await self.client.post(
            "/api/memories", json=payload, headers=self._headers(agent_id, user_email)
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("memory_id", ""))

    async def search(
        self,
        query: str,
        labels: dict[str, Any] | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            response = await self.breaker.call(
                lambda: self.client.post(
                    "/api/search",
                    json={
                        "query": query,
                        "labels": labels or {},
                        "categories": categories or [],
                        "limit": limit,
                    },
                    headers=self._headers(agent_id, user_email),
                )
            )
            response.raise_for_status()
            data = response.json()
            return list(data.get("results", []))
        except Exception:
            logger.warning("Mnemory search degraded")
            return []

    async def bootstrap_agent(self, agent: AgentDefinition) -> None:
        logger.info(
            "mnemory: bootstrap started",
            extra={
                "extra_data": {
                    "agent_id": agent.agent_id,
                    "has_system_prompt": bool(agent.system_prompt),
                }
            },
        )
        if agent.system_prompt:
            await self.add_memory(
                content=agent.system_prompt,
                role="assistant",
                pinned=True,
                agent_id=agent.agent_id,
                user_email=agent.owner_email,
            )
            logger.info(
                "mnemory: bootstrap complete",
                extra={"extra_data": {"agent_id": agent.agent_id}},
            )
        else:
            logger.info(
                "mnemory: bootstrap skipped (no system_prompt)",
                extra={"extra_data": {"agent_id": agent.agent_id}},
            )

    async def health(self) -> ProviderHealth:
        start = perf_counter()
        try:
            response = await self.client.get("/health", headers=self._headers())
            latency_ms = (perf_counter() - start) * 1000
            if response.is_success:
                return ProviderHealth(
                    name="mnemory",
                    status="healthy",
                    latency_ms=latency_ms,
                    circuit_state=self.breaker.state,
                )
        except Exception as exc:
            return ProviderHealth(
                name="mnemory", status="degraded", error=str(exc), circuit_state=self.breaker.state
            )
        return ProviderHealth(name="mnemory", status="degraded", circuit_state=self.breaker.state)
