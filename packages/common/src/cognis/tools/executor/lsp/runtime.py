"""Portable LSP status DTOs and configuration normalization."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

LSP_STATUS_CAPABILITY = "lsp_status_v1"
LSPStatusState = Literal["ready", "disabled", "unsupported", "unavailable"]


class LSPRuntimeConfig(BaseModel):
    enabled: bool = True
    auto_install: bool = False
    diagnostics_timeout_ms: int = 10_000
    idle_timeout_seconds: int = 600
    max_concurrent_servers: int = 8


class LSPStatusConfig(BaseModel):
    enabled: bool
    auto_install: bool
    diagnostics_timeout_ms: int
    idle_timeout_seconds: int
    max_concurrent_servers: int


class LSPActiveServerStatus(BaseModel):
    server_id: str
    server_name: str
    alive: bool
    file_count: int
    error_count: int
    warning_count: int
    idle_seconds: int
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class LSPBrokenServerStatus(BaseModel):
    client_key: str
    retry_in_seconds: int


class LSPAvailableServerStatus(BaseModel):
    server_id: str
    extensions: str
    available: bool
    has_auto_install: bool
    active: bool


class LSPStatusTotals(BaseModel):
    active_server_count: int = 0
    files_tracked: int = 0
    total_errors: int = 0
    total_warnings: int = 0


class LSPStatusReport(BaseModel):
    supported: bool
    enabled: bool
    executor_id: str | None = None
    executor_type: str | None = None
    state: LSPStatusState
    config: LSPStatusConfig
    active_servers: list[LSPActiveServerStatus] = Field(default_factory=list)
    broken_servers: list[LSPBrokenServerStatus] = Field(default_factory=list)
    spawning_count: int = 0
    totals: LSPStatusTotals = Field(default_factory=LSPStatusTotals)
    available_servers: list[LSPAvailableServerStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def resolve_lsp_runtime_config(source: Mapping[str, Any] | None = None) -> LSPRuntimeConfig:
    """Merge environment defaults with runtime overrides."""
    data = dict(source or {})
    enabled_default = os.environ.get("COGNIS_LSP_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    auto_install_default = os.environ.get(
        "COGNIS_LSP_AUTO_INSTALL", "true"
    ).strip().lower() not in {"0", "false", "no", "off"}
    return LSPRuntimeConfig(
        enabled=bool(data.get("lsp_enabled", enabled_default)),
        auto_install=bool(data.get("lsp_auto_install", auto_install_default)),
        diagnostics_timeout_ms=int(
            data.get("lsp_diagnostics_timeout_ms")
            or os.environ.get("COGNIS_LSP_DIAGNOSTICS_TIMEOUT_MS", "10000")
        ),
        idle_timeout_seconds=int(
            data.get("lsp_idle_timeout_seconds")
            or os.environ.get("COGNIS_LSP_IDLE_TIMEOUT_SECONDS", "600")
        ),
        max_concurrent_servers=int(
            data.get("lsp_max_concurrent_servers")
            or os.environ.get("COGNIS_LSP_MAX_CONCURRENT_SERVERS", "8")
        ),
    )


def build_lsp_unavailable_report(
    *,
    executor_id: str | None,
    executor_type: str | None,
    source: Mapping[str, Any] | None = None,
    state: LSPStatusState,
    warning: str | None = None,
    supported: bool = True,
) -> LSPStatusReport:
    """Build a normalized status for disabled/unsupported/unavailable states."""
    runtime_config = resolve_lsp_runtime_config(source)
    config = LSPStatusConfig(
        enabled=runtime_config.enabled,
        auto_install=runtime_config.auto_install,
        diagnostics_timeout_ms=runtime_config.diagnostics_timeout_ms,
        idle_timeout_seconds=runtime_config.idle_timeout_seconds,
        max_concurrent_servers=runtime_config.max_concurrent_servers,
    )
    return LSPStatusReport(
        supported=supported,
        enabled=config.enabled,
        executor_id=executor_id,
        executor_type=executor_type,
        state=state,
        config=config,
        warnings=[warning] if warning else [],
    )
