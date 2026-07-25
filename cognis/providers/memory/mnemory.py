"""Mnemory HTTP provider with retry and circuit breaker protection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar

import httpx
from prometheus_client import Counter

from cognis.core.truncation import middle_truncate
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.config import ProviderHealth
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.retry import with_retry
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email

logger = get_logger(__name__)

MNEMORY_SESSION_FORGED_TOTAL = Counter(
    "cognis_mnemory_session_forged_total",
    "Mnemory recalls that returned a different session id than requested.",
)

T = TypeVar("T")


class MnemoryHTTPStatusError(httpx.HTTPStatusError):
    """HTTP error from Mnemory that preserves its machine-readable detail."""

    def __init__(self, error: httpx.HTTPStatusError) -> None:
        response = error.response
        self.status_code = response.status_code
        self.detail = _response_error_detail(response)
        super().__init__(
            f"Mnemory request failed with HTTP {self.status_code}: {_detail_text(self.detail)}",
            request=error.request,
            response=response,
        )


def _response_error_detail(response: httpx.Response) -> Any:
    """Extract Mnemory's structured error detail without losing plain-text failures."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    if payload is not None:
        return payload
    return response.text.strip() or f"HTTP {response.status_code}"


def _detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    return repr(detail)


# Mnemory's RecallRequest schema enforces max_length=10_000 on both
# ``query`` and ``context``.  Keep a small safety margin below that limit
# so we never hit a 422 Unprocessable Content from the server.
_MAX_RECALL_QUERY_CHARS = 9_500


def _truncate_recall_text(text: str) -> tuple[str, bool]:
    normalized = text.strip()
    if not normalized:
        return "", False
    return middle_truncate(normalized, _MAX_RECALL_QUERY_CHARS)


class MnemoryProvider:
    """HTTP client for Mnemory with explicit identity requirements.

    All methods are protected by retry with exponential backoff and a
    circuit breaker. Callers must provide an explicit user identity via
    ``scoped_runtime_context`` or a direct ``user_email`` argument. Read and
    write failures raise so the caller can apply the correct policy.
    """

    def __init__(
        self, base_url: str, auth_provider: Any, user_email: str = SYSTEM_USER_EMAIL
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self.service_user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=60)
        self.breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    async def _call_with_retry(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        max_retries: int = 2,
        base_delay: float = 1.0,
        operation: str = "mnemory call",
        use_breaker: bool = True,
        **kwargs: Any,
    ) -> T:
        """Execute a provider call with retry inside circuit breaker."""

        async def _with_retries() -> T:
            return await with_retry(
                fn,
                *args,
                max_retries=max_retries,
                base_delay=base_delay,
                operation=operation,
                **kwargs,
            )

        if use_breaker:
            return await self.breaker.call(_with_retries)
        return await _with_retries()

    def _headers(
        self,
        agent_id: str | None = None,
        user_email: str | None = None,
        *,
        agent_owner_email: str | None = None,
        allow_system_fallback: bool = False,
    ) -> dict[str, str]:
        subject = user_email or current_user_email.get()
        if subject is None:
            if not allow_system_fallback:
                raise RuntimeError(
                    "Mnemory call requires explicit user identity or scoped_runtime_context"
                )
            subject = self.service_user_email
        resolved_agent_id = agent_id or current_agent_id.get()
        resolved_agent_owner = agent_owner_email or current_agent_owner_email.get() or subject
        headers = {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, resolved_agent_id or 'system', ['mnemory'], agent_owner_email=resolved_agent_owner)}",
        }
        if resolved_agent_id is not None:
            headers["X-Agent-Id"] = resolved_agent_id
            headers["X-Agent-Owner"] = resolved_agent_owner
        return headers

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        agent_id: str | None = None,
        user_email: str | None = None,
        operation: str,
        max_retries: int = 2,
    ) -> Any:
        async def _do() -> Any:
            response = await self.client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._headers(agent_id=agent_id, user_email=user_email),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MnemoryHTTPStatusError(exc) from exc
            return response.json()

        return await self._call_with_retry(
            _do,
            max_retries=max_retries,
            operation=operation,
        )

    async def _request_no_content(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        agent_id: str | None = None,
        user_email: str | None = None,
        operation: str,
        max_retries: int = 2,
    ) -> None:
        async def _do() -> None:
            response = await self.client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._headers(agent_id=agent_id, user_email=user_email),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MnemoryHTTPStatusError(exc) from exc

        await self._call_with_retry(
            _do,
            max_retries=max_retries,
            operation=operation,
        )

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
        ttl: int | None = None,
    ) -> dict[str, Any]:
        bounded_query, query_truncated = _truncate_recall_text(query)
        bounded_context, context_truncated = _truncate_recall_text(context or "")
        payload: dict[str, Any] = {
            "session_id": session_id,
            "query": bounded_query,
            "messages": [{"role": "user", "content": bounded_query}],
            "include_instructions": include_instructions,
            "managed": managed,
            "search_mode": search_mode,
            "context": bounded_context or None,
            "labels": labels or {},
            "ttl": ttl if isinstance(ttl, int) and ttl > 0 else 86400,
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
                    "query_truncated": query_truncated,
                    "context_truncated": context_truncated,
                }
            },
        )

        async def _do() -> dict[str, Any]:
            response = await self.client.post("/api/recall", json=payload, headers=self._headers())
            response.raise_for_status()
            return dict(response.json())

        result = await self._call_with_retry(
            _do,
            max_retries=2,
            operation="mnemory recall",
        )
        returned_session_id = str(result.get("session_id") or "").strip()
        if session_id and returned_session_id and returned_session_id != session_id:
            MNEMORY_SESSION_FORGED_TOTAL.inc()
            logger.warning(
                "mnemory: recall returned different session id than requested",
                extra={
                    "extra_data": {
                        "requested_session_id": session_id,
                        "returned_session_id": returned_session_id,
                    }
                },
            )
            result["_session_forged"] = True
        else:
            result["_session_forged"] = False
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

    async def load_session_identity(
        self,
        *,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Load immutable session identity from Mnemory using the recall contract."""

        return await self.recall(
            query="",
            session_id=session_id,
            labels=labels,
            context=context,
            search_mode="find",
            include_instructions=True,
            managed=True,
            instruction_mode="personality",
        )

    async def remember(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        user_email: str | None = None,
        agent_id: str | None = None,
        agent_owner_email: str | None = None,
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

            async def _do() -> None:
                response = await self.client.post(
                    "/api/remember",
                    json=payload,
                    headers=self._headers(
                        agent_id=agent_id,
                        user_email=user_email,
                        agent_owner_email=agent_owner_email,
                    ),
                )
                response.raise_for_status()

            await self._call_with_retry(
                _do,
                max_retries=2,
                operation="mnemory remember",
            )
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

        async def _do() -> str:
            response = await self.client.post(
                "/api/memories", json=payload, headers=self._headers(agent_id, user_email)
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("memory_id", ""))

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="mnemory add_memory",
        )

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

            async def _do() -> list[dict[str, Any]]:
                response = await self.client.post(
                    "/api/search",
                    json={
                        "query": query,
                        "labels": labels or {},
                        "categories": categories or [],
                        "limit": limit,
                    },
                    headers=self._headers(agent_id, user_email),
                )
                response.raise_for_status()
                data = response.json()
                return list(data.get("results", []))

            return await self._call_with_retry(
                _do,
                max_retries=2,
                operation="mnemory search",
            )
        except Exception:
            logger.warning("Mnemory search degraded")
            return []

    async def search_memories_tool(
        self,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            "/api/memories/search",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_search",
        )

    async def find_memories_tool(
        self,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            "/api/memories/find",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_find",
        )

    async def ask_memories_tool(
        self,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            "/api/memories/ask",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_ask",
        )

    async def add_memory_tool(
        self,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            "/api/memories",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_add",
        )

    async def add_memory_batch_tool(
        self,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            "/api/memories/batch",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_add_batch",
        )

    async def update_memory_tool(
        self,
        memory_id: str,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "PUT",
            f"/api/memories/{memory_id}",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_update",
        )

    async def delete_memory_tool(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None:
        await self._request_no_content(
            "DELETE",
            f"/api/memories/{memory_id}",
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_delete_tool",
        )

    async def list_memories_tool(
        self,
        *,
        params: dict[str, Any],
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "GET",
            "/api/memories",
            params=params,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_list",
        )

    async def memory_categories_tool(
        self,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "GET",
            "/api/categories",
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_categories",
        )

    async def recent_memories_tool(
        self,
        *,
        params: dict[str, Any],
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "GET",
            "/api/memories/recent",
            params=params,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_recent",
        )

    async def save_memory_artifact_tool(
        self,
        memory_id: str,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            f"/api/memories/{memory_id}/artifacts",
            json_body=arguments,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_save_artifact",
        )

    async def get_memory_artifact_tool(
        self,
        memory_id: str,
        artifact_id: str,
        *,
        params: dict[str, Any],
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "GET",
            f"/api/memories/{memory_id}/artifacts/{artifact_id}",
            params=params,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_get_artifact",
        )

    async def list_memory_artifacts_tool(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "GET",
            f"/api/memories/{memory_id}/artifacts",
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_list_artifacts",
        )

    async def get_memory_artifact_url_tool(
        self,
        memory_id: str,
        artifact_id: str,
        *,
        payload: dict[str, Any],
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> Any:
        return await self._request_json(
            "POST",
            f"/api/memories/{memory_id}/artifacts/{artifact_id}/download-token",
            json_body=payload,
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_get_artifact_url",
        )

    async def delete_memory_artifact_tool(
        self,
        memory_id: str,
        artifact_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None:
        await self._request_no_content(
            "DELETE",
            f"/api/memories/{memory_id}/artifacts/{artifact_id}",
            agent_id=agent_id,
            user_email=user_email,
            operation="mnemory memory_delete_artifact",
        )

    async def list_memories(
        self,
        *,
        role: str | None = None,
        limit: int = 100,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> list[dict[str, Any]]:
        """List memories for an agent/user scope."""

        async def _do() -> list[dict[str, Any]]:
            params: dict[str, Any] = {"limit": limit}
            if role is not None:
                params["role"] = role
            response = await self.client.get(
                "/api/memories", params=params, headers=self._headers(agent_id, user_email)
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                items = data.get("items") or data.get("results") or []
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
            return []

        return await self._call_with_retry(_do, max_retries=2, operation="mnemory list_memories")

    async def delete_memory(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None:
        """Delete a memory by ID."""

        async def _do() -> None:
            response = await self.client.delete(
                f"/api/memories/{memory_id}", headers=self._headers(agent_id, user_email)
            )
            response.raise_for_status()

        await self._call_with_retry(_do, max_retries=2, operation="mnemory delete_memory")

    async def replace_bootstrap_identity(
        self,
        agent: AgentDefinition,
        previous_content: str | None = None,
        *,
        allow_legacy_cleanup: bool = False,
    ) -> None:
        """Replace previously bootstrapped identity memories for an agent.

        Deletes known bootstrap memories (via labels) and best-effort deletes any
        pinned assistant memory whose content matches *previous_content* to clean
        up legacy bootstrap entries created before labels were added.
        """
        existing = await self.list_memories(
            role="assistant",
            limit=1000,
            agent_id=agent.agent_id,
            user_email=agent.owner_email,
        )
        labeled_bootstrap_ids: list[str] = []
        legacy_match_ids: list[str] = []
        for memory in existing:
            labels = memory.get("labels") if isinstance(memory.get("labels"), dict) else {}
            is_bootstrap = labels.get("cognis_bootstrap") == "agent_identity"
            matches_previous = (
                previous_content is not None and memory.get("content") == previous_content
            )
            if not memory.get("pinned"):
                continue
            memory_id = str(memory.get("memory_id", ""))
            if not memory_id:
                continue
            if is_bootstrap:
                labeled_bootstrap_ids.append(memory_id)
            elif matches_previous:
                legacy_match_ids.append(memory_id)

        if allow_legacy_cleanup and len(legacy_match_ids) > 1:
            logger.warning(
                "mnemory: skipping ambiguous legacy bootstrap cleanup",
                extra={
                    "extra_data": {
                        "agent_id": agent.agent_id,
                        "match_count": len(legacy_match_ids),
                    }
                },
            )

        content = agent.compose_personality() or agent.system_prompt
        if content:
            await self.add_memory(
                content=content,
                role="assistant",
                pinned=True,
                labels={"cognis_bootstrap": "agent_identity"},
                agent_id=agent.agent_id,
                user_email=agent.owner_email,
            )

        for memory_id in labeled_bootstrap_ids:
            await self.delete_memory(
                memory_id, agent_id=agent.agent_id, user_email=agent.owner_email
            )

        if allow_legacy_cleanup and len(legacy_match_ids) == 1:
            await self.delete_memory(
                legacy_match_ids[0], agent_id=agent.agent_id, user_email=agent.owner_email
            )

    async def bootstrap_agent(self, agent: AgentDefinition) -> None:
        """Bootstrap agent identity into Mnemory as a pinned core memory.

        Sends the structured personality fields (purpose, tone, temperament,
        behavioral rules) as the evolution seed.  Falls back to the raw
        system_prompt when no personality fields are set, so agents that
        only have a system_prompt still get bootstrapped.
        """
        content = agent.compose_personality() or agent.system_prompt
        logger.info(
            "mnemory: bootstrap started",
            extra={
                "extra_data": {
                    "agent_id": agent.agent_id,
                    "has_personality": bool(agent.compose_personality()),
                    "has_system_prompt": bool(agent.system_prompt),
                }
            },
        )
        if content:
            await self.add_memory(
                content=content,
                role="assistant",
                pinned=True,
                labels={"cognis_bootstrap": "agent_identity"},
                agent_id=agent.agent_id,
                user_email=agent.owner_email,
            )
            logger.info(
                "mnemory: bootstrap complete",
                extra={"extra_data": {"agent_id": agent.agent_id}},
            )
        else:
            logger.info(
                "mnemory: bootstrap skipped (no personality or system_prompt)",
                extra={"extra_data": {"agent_id": agent.agent_id}},
            )

    async def health(self) -> ProviderHealth:
        start = perf_counter()
        try:
            response = await self.client.get(
                "/health", headers=self._headers(allow_system_fallback=True)
            )
            latency_ms = (perf_counter() - start) * 1000
            if response.is_success:
                return ProviderHealth(
                    name="mnemory",
                    status="healthy",
                    latency_ms=latency_ms,
                    circuit_state=self.breaker.state,
                )
            body = response.text[:500]
            return ProviderHealth(
                name="mnemory",
                status="degraded",
                latency_ms=latency_ms,
                circuit_state=self.breaker.state,
                error=f"HTTP {response.status_code}",
                details={"status_code": response.status_code, "body": body},
            )
        except Exception as exc:
            return ProviderHealth(
                name="mnemory", status="degraded", error=str(exc), circuit_state=self.breaker.state
            )
