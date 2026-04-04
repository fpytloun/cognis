"""Domain models for tool execution and guardrails decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_core import PydanticCustomError


class Permission(StrEnum):
    """Tool permission decision."""

    ALLOW = "allow"
    EVALUATE = "evaluate"
    DENY = "deny"


class ToolSource(BaseModel):
    """Origin metadata for a tool definition."""

    type: str
    server_name: str | None = None
    skill_id: str | None = None


class ToolDefinition(BaseModel):
    """Tool metadata exposed to the LLM and executor."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    source: ToolSource
    category: str = "general"
    read_only: bool = False
    requires_secrets: list[str] = Field(default_factory=list)
    timeout_seconds: int = 30
    non_bypassable: bool = False
    max_result_size: int = 50_000
    risk_level: str | None = None


class ToolCall(BaseModel):
    """Normalized tool call request."""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPServerConfig(BaseModel):
    """Configuration for a local MCP server."""

    name: str
    transport: str = "stdio"
    command: str | None = None  # Required for stdio transport
    url: str | None = None  # Required for sse/streamable_http transport
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerConfig:
        if self.transport == "stdio" and not self.command:
            raise PydanticCustomError(
                "mcp_stdio_command_required",
                "command is required for stdio transport",
            )
        if self.transport in ("sse", "streamable_http") and not self.url:
            raise PydanticCustomError(
                "mcp_url_required",
                f"url is required for {self.transport} transport",
            )
        return self


# Key used in executor config JSON for MCP server ID references.
MCP_SERVER_IDS_KEY = "mcp_server_ids"


class ExecutorCapabilities(BaseModel):
    """Capabilities advertised by an executor."""

    tools: list[str] = Field(default_factory=list)
    inference: bool = False
    inference_models: list[str] = Field(default_factory=list)
    inference_type: str | None = None
    channels: bool = False  # Can host channel adapters


class InferenceConfig(BaseModel):
    """Optional executor-side inference configuration."""

    type: str = "openai_compatible"
    endpoint: str | None = None
    api_key_secret: str | None = None
    default_model: str | None = None
    models: list[str] = Field(default_factory=list)
    provider_hint: str | None = None


class ResourceLimits(BaseModel):
    """Optional executor resource limits."""

    cpu: str | None = None
    memory: str | None = None
    timeout_seconds: int | None = None


class ExecutorConfig(BaseModel):
    """Configuration passed to an executor at spawn time."""

    executor_id: str
    tools: list[ToolDefinition] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    secrets: dict[str, str] = Field(default_factory=dict)
    inference: InferenceConfig | None = None
    controller_url: str | None = None
    controller_token: str | None = None
    resource_limits: ResourceLimits | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    """Intaris evaluate response."""

    call_id: str
    decision: str
    reasoning: str | None = None
    risk: str | None = None
    path: str | None = None
    latency_ms: int = 0
    injection_detected: bool = False
    session_status: str | None = None
    status_reason: str | None = None


class ToolResult(BaseModel):
    """Executor or Intaris MCP tool result."""

    output: str
    is_error: bool = False
    duration_ms: int | None = None
    metadata: dict[str, Any] | None = None


class EscalationRecord(BaseModel):
    """Pending escalation record."""

    call_id: str
    session_id: str | None = None
    tool_name: str | None = None
    decision: str = "escalate"
    resolved: bool = False
    reasoning: str | None = None
    risk: str | None = None


class ExecutorHandle(BaseModel):
    """Handle returned when an executor is spawned."""

    executor_id: str
    executor_type: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: ExecutorCapabilities = Field(default_factory=ExecutorCapabilities)
    status: str = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)
