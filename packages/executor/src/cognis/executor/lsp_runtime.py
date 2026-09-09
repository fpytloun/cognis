"""Shared LSP runtime helpers and typed status models."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from cognis.logging import get_logger
from cognis.tools.executor.lsp.manager import LSPManager
from cognis.tools.executor.lsp.runtime import (
    LSP_STATUS_CAPABILITY as LSP_STATUS_CAPABILITY,
)
from cognis.tools.executor.lsp.runtime import (
    LSPActiveServerStatus,
    LSPAvailableServerStatus,
    LSPBrokenServerStatus,
    LSPRuntimeConfig,
    LSPStatusConfig,
    LSPStatusState,
    LSPStatusTotals,
)
from cognis.tools.executor.lsp.runtime import (
    LSPStatusReport as LSPStatusReport,
)

logger = get_logger(__name__)


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
    enabled = (
        data["lsp_enabled"]
        if "lsp_enabled" in data and data["lsp_enabled"] is not None
        else enabled_default
    )
    auto_install = (
        data["lsp_auto_install"]
        if "lsp_auto_install" in data and data["lsp_auto_install"] is not None
        else auto_install_default
    )
    diagnostics_timeout_ms = data.get("lsp_diagnostics_timeout_ms")
    idle_timeout_seconds = data.get("lsp_idle_timeout_seconds")
    max_concurrent_servers = data.get("lsp_max_concurrent_servers")
    return LSPRuntimeConfig(
        enabled=bool(enabled),
        auto_install=bool(auto_install),
        diagnostics_timeout_ms=int(
            diagnostics_timeout_ms or os.environ.get("COGNIS_LSP_DIAGNOSTICS_TIMEOUT_MS", "10000")
        ),
        idle_timeout_seconds=int(
            idle_timeout_seconds or os.environ.get("COGNIS_LSP_IDLE_TIMEOUT_SECONDS", "600")
        ),
        max_concurrent_servers=int(
            max_concurrent_servers or os.environ.get("COGNIS_LSP_MAX_CONCURRENT_SERVERS", "8")
        ),
    )


def build_lsp_manager(source: Mapping[str, Any] | None = None) -> LSPManager | None:
    """Create an LSP manager from runtime config, or None when disabled."""
    config = resolve_lsp_runtime_config(source)
    if not config.enabled:
        return None
    return LSPManager(
        enabled=True,
        auto_install=config.auto_install,
        diagnostics_timeout_ms=config.diagnostics_timeout_ms,
        idle_timeout_seconds=config.idle_timeout_seconds,
        max_concurrent_servers=config.max_concurrent_servers,
    )


async def cleanup_lsp_manager(
    manager: LSPManager | None, *, executor_id: str | None = None
) -> None:
    """Best-effort cleanup for an LSP manager."""
    if manager is None:
        return
    try:
        await manager.cleanup()
    except Exception:
        logger.debug(
            "lsp: cleanup error",
            extra={"extra_data": {"executor_id": executor_id}},
            exc_info=True,
        )


async def build_lsp_status_report(
    *,
    manager: LSPManager | None,
    executor_id: str | None,
    executor_type: str | None,
    source: Mapping[str, Any] | None = None,
    state: LSPStatusState | None = None,
    warnings: list[str] | None = None,
) -> LSPStatusReport:
    """Build a sanitized typed LSP status report."""
    runtime_config = resolve_lsp_runtime_config(source)
    config = LSPStatusConfig(
        enabled=runtime_config.enabled,
        auto_install=runtime_config.auto_install,
        diagnostics_timeout_ms=runtime_config.diagnostics_timeout_ms,
        idle_timeout_seconds=runtime_config.idle_timeout_seconds,
        max_concurrent_servers=runtime_config.max_concurrent_servers,
    )
    report_state: LSPStatusState = state or ("ready" if manager is not None else "disabled")
    combined_warnings = list(warnings or [])
    source_data = dict(source or {})
    init_warning = source_data.get("lsp_warning")
    if isinstance(init_warning, str) and init_warning not in combined_warnings:
        combined_warnings.append(init_warning)
    if manager is None:
        report_state = state or (
            "unavailable"
            if config.enabled and bool(source_data.get("lsp_init_failed"))
            else "disabled"
        )
        return LSPStatusReport(
            supported=True,
            enabled=config.enabled,
            executor_id=executor_id,
            executor_type=executor_type,
            state=report_state,
            config=config,
            warnings=combined_warnings,
        )

    raw_status = manager.status()
    available = await manager.available_servers()
    active_servers = [
        LSPActiveServerStatus(
            server_id=str(item["server_id"]),
            server_name=str(item["server_name"]),
            alive=bool(item["alive"]),
            file_count=int(item["file_count"]),
            error_count=int(item["error_count"]),
            warning_count=int(item["warning_count"]),
            idle_seconds=int(item["idle_seconds"]),
            diagnostics=dict(item.get("diagnostics", {})),
        )
        for item in raw_status.get("active_servers", [])
    ]
    broken_servers = [
        LSPBrokenServerStatus(
            client_key=str(item["client_key"]),
            retry_in_seconds=int(item["retry_in_seconds"]),
        )
        for item in raw_status.get("broken_servers", [])
    ]
    available_servers = [
        LSPAvailableServerStatus(
            server_id=str(item["server_id"]),
            extensions=str(item["extensions"]),
            available=bool(item["available"]),
            has_auto_install=bool(item["has_auto_install"]),
            active=bool(item["active"]),
        )
        for item in available
    ]
    totals = LSPStatusTotals.model_validate(raw_status.get("totals", {}))
    return LSPStatusReport(
        supported=True,
        enabled=config.enabled,
        executor_id=executor_id,
        executor_type=executor_type,
        state=report_state,
        config=config,
        active_servers=active_servers,
        broken_servers=broken_servers,
        spawning_count=int(raw_status.get("spawning_count", 0)),
        totals=totals,
        available_servers=available_servers,
        warnings=combined_warnings,
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
