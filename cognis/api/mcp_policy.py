"""Controller-owned MCP configuration policy."""

from __future__ import annotations

from typing import Any

from cognis.mcp_runtime import HTTP_MCP_TRANSPORTS
from cognis.models.tool import effective_mcp_auth_config, mcp_headers_have_authorization


def invalid_mcp_config_reason(
    *,
    transport: str,
    command: str | None,
    url: str | None,
    env: dict[str, str] | None,
    headers: dict[str, str] | None,
    auth_config: dict[str, Any] | None = None,
) -> str | None:
    """Return a user-visible invalidity reason for persisted config."""

    env = env or {}
    headers = headers or {}
    effective_auth = effective_mcp_auth_config(auth_config, headers)
    if transport == "stdio":
        if not command:
            return "Stdio MCP servers must define a command."
        if headers:
            return "Stdio MCP servers cannot define HTTP headers."
        if effective_auth.type == "oauth2":
            return "OAuth is only supported for HTTP MCP transports."
        return None
    if transport in HTTP_MCP_TRANSPORTS:
        if not url:
            return "HTTP MCP servers must define a URL."
        if env:
            return "HTTP MCP servers must use headers instead of environment variables."
        if effective_auth.type == "oauth2" and mcp_headers_have_authorization(headers):
            return "Authorization headers are not allowed when OAuth is enabled."
        return None
    return f"Unsupported MCP transport: {transport}"


def canonicalize_mcp_headers(headers: dict[str, str]) -> dict[str, str]:
    """Normalize header names and reject case-insensitive duplicates."""

    canonical: dict[str, str] = {}
    seen_lower: dict[str, str] = {}
    for raw_key, value in headers.items():
        key = "-".join(part[:1].upper() + part[1:] for part in raw_key.strip().split("-"))
        if not key:
            raise ValueError("HTTP header names cannot be empty")
        lowered = key.lower()
        previous = seen_lower.get(lowered)
        if previous is not None and previous != key:
            raise ValueError(
                f"Duplicate HTTP header differs only by case: {previous} and {raw_key.strip()}"
            )
        seen_lower[lowered] = key
        canonical[key] = value
    return canonical
