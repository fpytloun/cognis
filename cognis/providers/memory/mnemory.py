"""Mnemory HTTP provider."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.config import ProviderHealth
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.runtime_context import current_user_email

logger = get_logger(__name__)


class MnemoryProvider:
    """HTTP client for Mnemory with graceful degradation."""

    def __init__(
        self, base_url: str, auth_provider: Any, user_email: str = "system@example.com"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        self.breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    def _headers(
        self, agent_id: str | None = None, user_email: str | None = None
    ) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        headers = {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, agent_id or 'system', ['mnemory'])}",
        }
        if agent_id is not None:
            headers["X-Agent-Id"] = agent_id
        return headers

    async def recall(
        self,
        query: str,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        search_mode: str = "find",
        include_instructions: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "query": query,
            "messages": [{"role": "user", "content": query}],
            "include_instructions": include_instructions,
            "search_mode": search_mode,
            "context": context,
            "labels": labels or {},
            "ttl": 86400,
        }
        try:
            response = await self.breaker.call(
                lambda: self.client.post("/api/recall", json=payload, headers=self._headers())
            )
            response.raise_for_status()
            return dict(response.json())
        except Exception:
            logger.warning("Mnemory recall degraded")
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
    ) -> None:
        payload = {
            "session_id": session_id,
            "messages": messages,
            "role": role,
            "labels": labels or {},
            "context": context,
        }
        response = await self.client.post(
            "/api/remember", json=payload, headers=self._headers(user_email=user_email)
        )
        response.raise_for_status()

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
        if agent.system_prompt:
            await self.add_memory(
                content=agent.system_prompt,
                role="assistant",
                pinned=True,
                agent_id=agent.agent_id,
                user_email=agent.owner_email,
            )

    async def health(self) -> ProviderHealth:
        start = perf_counter()
        try:
            response = await self.client.get("/health")
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
