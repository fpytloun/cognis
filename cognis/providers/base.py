"""Provider protocol definitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from cognis.models.agent import AgentDefinition
from cognis.models.config import Cost, ProviderHealth, TokenUsage
from cognis.models.session import EventAppendResult, EventReadResult, IntarisSession, SessionEvent
from cognis.models.tool import EscalationRecord, EvaluationResult, ExecutorHandle, ToolResult


class MemoryProvider(Protocol):
    async def recall(
        self,
        query: str,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        search_mode: str = "find",
        include_instructions: bool = False,
    ) -> dict[str, Any]: ...

    async def remember(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> None: ...

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
    ) -> str: ...

    async def search(
        self,
        query: str,
        labels: dict[str, Any] | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def bootstrap_agent(self, agent: AgentDefinition) -> None: ...
    async def health(self) -> ProviderHealth: ...


class GuardrailsProvider(Protocol):
    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> EvaluationResult: ...

    async def report_reasoning(
        self, session_id: str, content: str, context: str | None = None
    ) -> None: ...
    async def checkpoint(self, session_id: str, content: str) -> None: ...
    async def get_session(self, session_id: str) -> IntarisSession: ...
    async def submit_decision(
        self, call_id: str, decision: str, note: str | None = None
    ) -> None: ...
    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]: ...
    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
    ) -> EventAppendResult: ...
    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
    ) -> EventReadResult: ...
    async def get_last_seq(self, session_id: str) -> int: ...
    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult: ...
    async def health(self) -> ProviderHealth: ...


class ExecutorProvider(Protocol):
    async def spawn(self, labels: dict[str, str] | None = None) -> ExecutorHandle: ...
    async def cleanup(self, executor_id: str) -> None: ...
    async def health(self) -> ProviderHealth: ...


class SecretsProvider(Protocol):
    async def get_secret(self, name: str, user_id: str, agent_id: str | None = None) -> str: ...
    async def set_secret(
        self,
        name: str,
        value: str,
        user_id: str,
        scope: str = "user",
        agent_id: str | None = None,
        description: str | None = None,
    ) -> None: ...
    async def delete_secret(
        self, name: str, user_id: str, scope: str = "user", agent_id: str | None = None
    ) -> bool: ...
    async def list_secrets(self, user_id: str) -> list[dict[str, Any]]: ...
    async def resolve_for_execution(
        self, agent: AgentDefinition, user_id: str
    ) -> dict[str, str]: ...
    async def health(self) -> ProviderHealth: ...


class LLMProvider(Protocol):
    async def generate(
        self, messages: list[dict[str, Any]], model: str | None = None, **kwargs: Any
    ) -> dict[str, Any]: ...
    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]: ...
    def count_tokens(self, text: str, model: str) -> int: ...
    async def list_models(self) -> list[dict[str, Any]]: ...
    async def get_cost(self, usage: TokenUsage, model: str) -> Cost: ...
    async def health(self) -> ProviderHealth: ...


class AuthProvider(Protocol):
    def sign_access_token(self, subject: str, name: str | None, role: str) -> str: ...
    def sign_refresh_token(self, subject: str) -> str: ...
    def sign_service_jwt(self, subject: str, agent_id: str, audience: list[str]) -> str: ...
    def sign_exchange_token(self, subject: str, target: str) -> str: ...
    def verify_jwt(self, token: str, audience: list[str] | None = None) -> dict[str, Any]: ...
    def revoke_token(self, jti: str) -> None: ...
    def consume_exchange_token(self, jti: str) -> bool: ...
    def jwks(self) -> dict[str, Any]: ...
    async def health(self) -> ProviderHealth: ...
