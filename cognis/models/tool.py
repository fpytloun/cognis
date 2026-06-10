"""Domain models for tool execution and guardrails decisions."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

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
    profile_group: str | None = None
    read_only: bool = False
    capabilities: list[ToolCapability] = Field(default_factory=list)
    classification_status: str | None = None
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


AUTO_PROFILE_GROUPS: tuple[str, ...] = (
    "filesystem",
    "shell",
    "web",
    "browser",
    "development",
    "office",
    "personal",
    "communication",
    "conversations",
)

RESERVED_PROFILE_GROUPS: tuple[str, ...] = ("memory", "system")

ALL_PROFILE_GROUPS: tuple[str, ...] = (*AUTO_PROFILE_GROUPS, *RESERVED_PROFILE_GROUPS)


def tool_profile_group(tool: ToolDefinition) -> str:
    """Return the effective profile group for a tool."""

    if tool.profile_group:
        return tool.profile_group
    if tool.category in {"filesystem", "lsp"}:
        return "filesystem"
    if tool.category == "shell":
        return "shell"
    if tool.category == "web":
        return "web"
    if tool.category == "browser":
        return "browser"
    if tool.category == "memory":
        return "memory"
    if tool.category == "conversations":
        return "conversations"
    if tool.category in {
        "system",
        "context",
        "workflow",
        "orchestration",
        "deliverable",
        "schedule",
        "artifact",
        "datetime",
        "skill",
        "mcp",
        "general",
    }:
        return "system"
    return "development"


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

    The base shape follows the spec (`mcp_<server>__<tool>`). Simple segment
    normalization does not add a suffix by itself; callers that discover an
    actual normalized-name collision should request a suffixed variant.
    """

    safe_server = _sanitize_tool_segment(server_name)
    safe_tool = _sanitize_tool_segment(raw_tool_name)
    base_name = f"mcp_{safe_server}__{safe_tool}"
    if len(base_name) <= _MAX_TOOL_NAME_LENGTH:
        return base_name
    return sanitize_mcp_tool_name_with_suffix(server_name, raw_tool_name)


def sanitize_mcp_tool_name_with_suffix(server_name: str, raw_tool_name: str) -> str:
    """Return a provider-safe MCP tool name with a stable disambiguating suffix."""

    safe_server = _sanitize_tool_segment(server_name)
    safe_tool = _sanitize_tool_segment(raw_tool_name)
    base_name = f"mcp_{safe_server}__{safe_tool}"
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


class MCPOAuth2Config(BaseModel):
    """OAuth 2.1 configuration for HTTP MCP servers."""

    type: Literal["oauth2"] = "oauth2"
    issuer: str | None = None
    authorization_server: str | None = None
    resource: str | None = None
    scopes: list[str] = Field(default_factory=list)
    client_id: str | None = None
    client_secret_ref: str | None = None
    redirect_uri: str | None = None
    dynamic_client_registration: bool = False
    client_metadata_document_url: str | None = None
    authorization_params: dict[str, str] = Field(default_factory=dict)


class MCPAuthConfig(BaseModel):
    """MCP auth mode configuration."""

    type: Literal["none", "static_headers", "oauth2"] = "none"
    issuer: str | None = None
    authorization_server: str | None = None
    resource: str | None = None
    scopes: list[str] = Field(default_factory=list)
    client_id: str | None = None
    client_secret_ref: str | None = None
    redirect_uri: str | None = None
    dynamic_client_registration: bool = False
    client_metadata_document_url: str | None = None
    authorization_params: dict[str, str] = Field(default_factory=dict)


def effective_mcp_auth_config(
    auth_config: dict[str, Any] | MCPAuthConfig | None,
    headers: dict[str, str] | None = None,
) -> MCPAuthConfig:
    """Return explicit auth config or legacy static-header compatibility mode."""

    if isinstance(auth_config, MCPAuthConfig):
        return auth_config
    if isinstance(auth_config, dict) and auth_config:
        return MCPAuthConfig.model_validate(auth_config)
    return MCPAuthConfig(type="static_headers" if headers else "none")


def mcp_headers_have_authorization(headers: dict[str, str] | None) -> bool:
    return any(str(key).lower() == "authorization" for key in (headers or {}))


_RESERVED_OAUTH_AUTHORIZATION_PARAMS = {
    "client_id",
    "code_challenge",
    "code_challenge_method",
    "redirect_uri",
    "response_type",
    "state",
}


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
    auth_config: MCPAuthConfig | None = Field(default_factory=MCPAuthConfig)
    timeout_seconds: int = 30
    connect_timeout_seconds: int = 15

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerConfig:
        self.auth_config = effective_mcp_auth_config(self.auth_config, self.headers)
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
            if self.auth_config.type == "oauth2":
                raise PydanticCustomError(
                    "mcp_oauth_http_transport_required",
                    "OAuth is only supported for HTTP MCP transports",
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
        if self.auth_config.type == "oauth2" and mcp_headers_have_authorization(self.headers):
            raise PydanticCustomError(
                "mcp_oauth_authorization_header_forbidden",
                "Authorization headers are not allowed when OAuth is enabled",
            )
        if self.auth_config.type == "oauth2":
            forbidden_params = _RESERVED_OAUTH_AUTHORIZATION_PARAMS.intersection(
                {str(key).lower() for key in self.auth_config.authorization_params}
            )
            if forbidden_params:
                raise PydanticCustomError(
                    "mcp_oauth_reserved_authorization_param",
                    "authorization_params cannot override reserved OAuth parameters",
                )
        if self.auth_config.type != "oauth2" and self.auth_config.dynamic_client_registration:
            raise PydanticCustomError(
                "mcp_dynamic_client_registration_requires_oauth",
                "dynamic client registration is only valid for OAuth MCP auth",
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
    tool_handlers: dict[str, Any] = Field(default_factory=dict, exclude=True)
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
    user_decision: str | None = None
    user_note: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None


class ExecutorHandle(BaseModel):
    """Handle returned when an executor is spawned."""

    executor_id: str
    executor_type: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: ExecutorCapabilities = Field(default_factory=ExecutorCapabilities)
    status: str = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)
