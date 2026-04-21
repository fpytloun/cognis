"""Domain models for tool execution and guardrails decisions."""

from __future__ import annotations

import hashlib
import re
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


class ToolCapability(StrEnum):
    """Capability labels used by step profiles and tool classification."""

    READ = "read"
    WRITE = "write"
    PRIVILEGED = "privileged"
    DESTRUCTIVE = "destructive"


class ToolSource(BaseModel):
    """Origin metadata for a tool definition."""

    type: str
    server_name: str | None = None
    server_id: str | None = None
    raw_tool_name: str | None = None
    skill_id: str | None = None
    skill_version_id: str | None = None
    skill_content_hash: str | None = None


class ToolDefinition(BaseModel):
    """Tool metadata exposed to the LLM and executor."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    source: ToolSource
    category: str = "general"
    read_only: bool = False
    capabilities: list[ToolCapability] = Field(default_factory=list)
    classification_source: str | None = None
    classification_confidence: float | None = None
    requires_secrets: list[str] = Field(default_factory=list)
    timeout_seconds: int = 30
    non_bypassable: bool = False
    max_result_size: int = 50_000
    risk_level: str | None = None
    # Runtime-only metadata for executable skill tools (recipe, assets, etc.)
    # Not sent to the LLM — used by executor handlers for execution.
    execution_metadata: dict[str, Any] | None = None


def tool_capabilities(tool: ToolDefinition) -> set[ToolCapability]:
    """Return the normalized capability set for a tool."""

    if tool.capabilities:
        return {ToolCapability(capability) for capability in tool.capabilities}
    return {ToolCapability.READ} if tool.read_only else {ToolCapability.WRITE}


def stable_tool_id(tool: ToolDefinition) -> str:
    """Return a stable identifier for a tool definition."""
    if tool.source.type in {"local_mcp", "intaris_mcp"}:
        server_id = tool.source.server_id or tool.source.server_name or "unknown"
        raw_name = tool.source.raw_tool_name or tool.name
        return f"mcp:{server_id}:{raw_name}"
    if tool.source.type == "skill":
        skill_id = tool.source.skill_id or "unknown"
        raw_name = tool.source.raw_tool_name or tool.name
        return f"skill:{skill_id}:{raw_name}"
    return f"builtin:{tool.name}"


def tool_display_name(tool: ToolDefinition) -> str:
    """Return the user-authored display name for a tool when available."""

    if tool.source.type == "skill" and tool.source.raw_tool_name:
        return tool.source.raw_tool_name
    return tool.name


def tool_matches_identifier(tool: ToolDefinition, identifier: str) -> bool:
    """Return whether an identifier matches a tool's stable, internal, or legacy name."""

    return bool(
        identifier
        and (
            tool.name == identifier
            or stable_tool_id(tool) == identifier
            or (
                tool.source.type == "skill"
                and tool.source.raw_tool_name is not None
                and tool.source.raw_tool_name == identifier
            )
        )
    )


_SAFE_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_TOOL_NAME_LENGTH = 64


def sanitize_mcp_tool_name(server_name: str, raw_tool_name: str) -> str:
    """Return a provider-safe MCP tool name.

    The base shape follows the spec (`mcp_<server>__<tool>`). When the raw
    values need normalization or the result would be too long, append a stable
    short hash to keep names deterministic and collision-resistant.
    """

    safe_server = _sanitize_tool_segment(server_name)
    safe_tool = _sanitize_tool_segment(raw_tool_name)
    base_name = f"mcp_{safe_server}__{safe_tool}"
    needs_suffix = (
        safe_server != server_name
        or safe_tool != raw_tool_name
        or len(base_name) > _MAX_TOOL_NAME_LENGTH
    )
    if not needs_suffix:
        return base_name
    suffix = hashlib.sha1(f"{server_name}:{raw_tool_name}".encode()).hexdigest()[:8]
    trimmed = base_name[: _MAX_TOOL_NAME_LENGTH - len(suffix) - 1].rstrip("_")
    return f"{trimmed}_{suffix}"


def _sanitize_tool_segment(value: str) -> str:
    cleaned = _SAFE_TOOL_NAME_PATTERN.sub("_", value).strip("_")
    return cleaned or "tool"


class ToolCall(BaseModel):
    """Normalized tool call request."""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    execution_scope_id: str | None = None
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)


class MCPServerConfig(BaseModel):
    """Configuration for a local MCP server."""

    server_id: str | None = None
    name: str
    transport: str = "stdio"
    command: str | None = None  # Required for stdio transport
    url: str | None = None  # Required for sse/streamable_http transport
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerConfig:
        if self.transport == "stdio":
            if not self.command:
                raise PydanticCustomError(
                    "mcp_stdio_command_required",
                    "command is required for stdio transport",
                )
            if self.headers:
                raise PydanticCustomError(
                    "mcp_stdio_headers_forbidden",
                    "headers are not allowed for stdio transport",
                )
            self.url = None
        elif self.transport in ("sse", "streamable_http"):
            if not self.url:
                raise PydanticCustomError(
                    "mcp_url_required",
                    f"url is required for {self.transport} transport",
                )
            if self.env:
                raise PydanticCustomError(
                    "mcp_http_env_forbidden",
                    f"env is not allowed for {self.transport} transport; use headers",
                )
            self.command = None
            self.args = []
        else:
            raise PydanticCustomError(
                "mcp_transport_invalid",
                f"unsupported MCP transport: {self.transport}",
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
    attachments: list[dict[str, Any]] | None = None


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
