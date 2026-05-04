"""Provider protocol definitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from cognis.models.agent import AgentDefinition
from cognis.models.config import (
    Cost,
    ImageGenerationResult,
    ModelInfo,
    ProviderHealth,
    SpeechToTextResult,
    TextToSpeechResult,
    TokenUsage,
)
from cognis.models.session import (
    EventAppendResult,
    EventReadResult,
    IntarisSession,
    ReasoningReportResult,
    SessionEvent,
)
from cognis.models.tool import (
    EscalationRecord,
    EvaluationResult,
    ExecutorConfig,
    ExecutorHandle,
    ToolCall,
    ToolResult,
)


class MemoryProvider(Protocol):
    async def load_session_identity(
        self,
        *,
        session_id: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def remember(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        role: str | None = None,
        labels: dict[str, Any] | None = None,
        context: str | None = None,
        user_email: str | None = None,
        agent_id: str | None = None,
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

    async def delete_memory(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None: ...

    async def delete_memory_tool(
        self,
        memory_id: str,
        *,
        agent_id: str | None = None,
        user_email: str | None = None,
    ) -> None: ...

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
        self,
        session_id: str,
        content: str = "",
        context: str | None = None,
        *,
        from_events: bool = False,
        wait_for_intention: bool = False,
        wait_timeout_ms: int | None = None,
    ) -> ReasoningReportResult: ...
    async def checkpoint(self, session_id: str, content: str) -> None: ...
    async def get_session(self, session_id: str) -> IntarisSession: ...
    async def submit_decision(
        self, call_id: str, decision: str, note: str | None = None
    ) -> None: ...
    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]: ...
    async def get_escalation(self, call_id: str) -> EscalationRecord | None: ...
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
        allow_missing_stream: bool = False,
    ) -> EventReadResult: ...
    async def get_last_seq(self, session_id: str) -> int: ...
    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult: ...
    async def list_mcp_servers(self, enabled_only: bool = True) -> list[dict[str, Any]]: ...
    async def list_mcp_tools(self) -> list[dict[str, Any]]: ...
    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None: ...
    async def health(self) -> ProviderHealth: ...


class ExecutorProvider(Protocol):
    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle: ...
    async def get_executor(self, handle: ExecutorHandle) -> ExecutorConnection: ...
    async def cancel(self, handle: ExecutorHandle) -> None: ...
    async def list_active(self) -> list[ExecutorHandle]: ...
    async def cleanup(self) -> None: ...
    async def health(self) -> ProviderHealth: ...


class ExecutorConnection(Protocol):
    async def rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def tool_execute(
        self, tool_call: ToolCall, timeout_seconds: int | None = None
    ) -> ToolResult: ...
    async def cancel_call(self, call_id: str) -> None: ...


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


class CredentialsProvider(Protocol):
    async def list_credentials(self, user_email: str) -> list[Any]: ...
    async def get_credential(self, credential_id: str, user_email: str) -> Any | None: ...
    async def upsert_credential(self, **kwargs: Any) -> Any: ...
    async def revoke_credential(self, credential_id: str, user_email: str) -> bool: ...
    async def delete_credential(self, credential_id: str, user_email: str) -> bool: ...
    async def resolve_ref(self, ref: str, *, agent: AgentDefinition, user_email: str) -> Any: ...
    async def health(self) -> ProviderHealth: ...


class LLMProvider(Protocol):
    async def generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: Any,
    ) -> dict[str, Any]: ...
    async def stream_generate(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        task_type: str = "default",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def resolve_model(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
    ) -> str: ...
    async def resolve_model_target(
        self,
        explicit_model: str | None = None,
        task_type: str = "default",
        explicit_provider_id: str | None = None,
    ) -> tuple[str, str | None]: ...
    async def get_model_info(self, model_id: str, provider_id: str | None = None) -> ModelInfo: ...
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        filename: str,
        model: str | None = None,
        task_type: str = "speech_to_text",
        prompt: str | None = None,
        language: str | None = None,
    ) -> SpeechToTextResult: ...
    async def synthesize(
        self,
        text: str,
        *,
        voice: str,
        model: str | None = None,
        task_type: str = "text_to_speech",
        response_format: str = "mp3",
        speed: float = 1.0,
    ) -> TextToSpeechResult: ...
    def count_tokens(self, text: str, model: str) -> int: ...
    def count_messages_tokens(self, messages: list[dict[str, Any]], model: str) -> int: ...
    async def list_models(self) -> list[dict[str, Any]]: ...
    async def get_cost(self, usage: TokenUsage, model: str) -> Cost: ...
    async def health(self) -> ProviderHealth: ...


class ImageGenerationProvider(Protocol):
    """Protocol for image generation capabilities.

    Separate from LLMProvider to avoid breaking existing implementations.
    LiteLLMProvider implements both protocols.
    """

    async def image_generate(
        self,
        prompt: str,
        model: str | None = None,
        task_type: str = "image_generation",
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        response_format: str = "b64_json",
        image: str | None = None,
        **kwargs: Any,
    ) -> ImageGenerationResult: ...


class AuthProvider(Protocol):
    def sign_access_token(self, subject: str, name: str | None, role: str) -> str: ...
    def sign_refresh_token(self, subject: str) -> str: ...
    def sign_service_jwt(
        self,
        subject: str,
        agent_id: str,
        audience: list[str],
        *,
        agent_owner_email: str | None = None,
    ) -> str: ...
    def sign_exchange_token(self, subject: str, target: str) -> str: ...
    def verify_jwt(self, token: str, audience: list[str] | None = None) -> dict[str, Any]: ...
    def revoke_token(self, jti: str) -> None: ...
    def consume_exchange_token(self, jti: str) -> bool: ...
    def jwks(self) -> dict[str, Any]: ...
    async def health(self) -> ProviderHealth: ...
