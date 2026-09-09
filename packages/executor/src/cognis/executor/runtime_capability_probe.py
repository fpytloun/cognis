"""Bounded runtime capability reports for standalone executors.

The report is intentionally a small, allowlisted telemetry contract.  It does
not contain executable paths, probe output, configuration secrets, or plugin
metadata.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

from cognis.executor.component_registry import COMPONENTS, component_available
from cognis.models.runtime_capabilities import (
    BrowserCapabilityReport,
    CapabilityState,
    CapabilityStatus,
    RuntimeCapabilityReport,
)

_MAX_ITEMS = 256
_MAX_NAME_LENGTH = 100
_MAX_TEXT_LENGTH = 180
_KNOWN_CHANNELS = frozenset(
    {
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
        "chromium-headless-shell",
        "chromium-tip-of-tree",
    }
)
_CHANNEL_BINARIES: dict[str, tuple[str, ...]] = {
    "chrome": ("google-chrome-stable", "google-chrome", "chrome", "chromium-browser"),
    "chrome-beta": ("google-chrome-beta", "google-chrome", "chrome"),
    "chrome-dev": ("google-chrome-unstable", "google-chrome", "chrome"),
    "chrome-canary": ("google-chrome-canary", "google-chrome", "chrome"),
    "msedge": ("microsoft-edge-stable", "microsoft-edge", "msedge"),
    "msedge-beta": ("microsoft-edge-beta", "microsoft-edge"),
    "msedge-dev": ("microsoft-edge-dev", "microsoft-edge"),
    "msedge-canary": ("microsoft-edge-canary", "microsoft-edge"),
}
_COMMANDS: dict[str, tuple[str, ...]] = {
    "git": ("git",),
    "node": ("node",),
    "uv": ("uv",),
    "officecli": ("officecli",),
}


def _status(
    state: CapabilityState,
    reason_code: str,
    message: str,
    *,
    version: str | None = None,
) -> CapabilityStatus:
    return CapabilityStatus(
        state=state,
        reason_code=reason_code,
        message=message[:_MAX_TEXT_LENGTH],
        version=version[:64] if isinstance(version, str) else None,
    )


def _package_status(
    package: str,
    *,
    installable: bool = True,
    label: str | None = None,
) -> CapabilityStatus:
    name = label or package
    try:
        present = importlib.util.find_spec(package) is not None
    except Exception:
        present = False
    if present:
        try:
            version = importlib.metadata.version(package)
        except Exception:
            version = None
        return _status("ready", "available", f"{name} runtime is available.", version=version)
    return _status(
        "installable" if installable else "unavailable",
        "missing_dependency",
        f"{name} runtime is not available.",
    )


def _command_status(name: str) -> CapabilityStatus:
    candidates = _COMMANDS[name]
    try:
        available = any(shutil.which(candidate) is not None for candidate in candidates)
    except Exception:
        return _status("unknown", "command_probe_failed", f"{name} command could not be checked.")
    if not available:
        return _status("unavailable", "missing_command", f"{name} command is not available.")
    version: str | None = None
    try:
        result = subprocess.run(
            (candidates[0], "--version"),
            capture_output=True,
            text=True,
            timeout=0.75,
            check=False,
        )
        raw = f"{result.stdout} {result.stderr}"
        match = re.search(r"\b\d+(?:\.\d+){0,3}(?:[-+._][0-9A-Za-z.-]+)?\b", raw)
        version = match.group(0)[:64] if match else None
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return _status("unknown", "version_probe_failed", f"{name} version could not be checked.")
    return _status("ready", "available", f"{name} command is available.", version=version)


def _component_status(name: str) -> CapabilityStatus:
    spec = COMPONENTS[name]
    try:
        packages_available = component_available(name)
    except Exception:
        return _status(
            "unknown",
            "component_probe_failed",
            f"{name} component could not be checked.",
        )
    if not packages_available:
        state: CapabilityState = "installable" if spec.packages else "unavailable"
        return _status(state, "missing_dependency", f"{name} component is not available.")
    try:
        for module in spec.modules:
            __import__(module)
    except Exception:
        return _status(
            "unknown",
            "component_probe_failed",
            f"{name} component could not be checked.",
        )
    else:
        return _status("ready", "available", f"{name} component is available.")


def _officecli_status(runtime_metadata: dict[str, Any] | None) -> CapabilityStatus:
    runtime = (runtime_metadata or {}).get("officecli")
    if not isinstance(runtime, dict):
        return _command_status("officecli")
    version = runtime.get("version")
    if runtime.get("available"):
        return _status(
            "ready",
            "available",
            "Certified OfficeCLI runtime is available.",
            version=version if isinstance(version, str) else None,
        )
    if runtime.get("enabled") is False:
        return _status("unavailable", "disabled", "OfficeCLI support is disabled.")
    return _status(
        "unavailable",
        "runtime_unavailable",
        "Certified OfficeCLI runtime is unavailable.",
    )


def _lsp_status(*, enabled: bool) -> CapabilityStatus:
    if not enabled:
        return _status("unavailable", "disabled", "LSP support is disabled.")
    try:
        from cognis.tools.executor.lsp.servers import BUILTIN_SERVERS

        servers = tuple(BUILTIN_SERVERS)
    except Exception:
        return _status("unknown", "component_probe_failed", "LSP support could not be checked.")
    try:
        available = any(shutil.which(server.command) is not None for server in servers)
    except Exception:
        return _status("unknown", "server_probe_failed", "LSP servers could not be checked.")
    if available:
        return _status("ready", "available", "At least one known LSP server is available.")
    if any(server.install_strategy is not None for server in servers):
        return _status("installable", "missing_server", "Known LSP servers can be installed.")
    return _status("unavailable", "missing_server", "No known LSP server is available.")


def _browser_report(browser_config: dict[str, Any] | None) -> BrowserCapabilityReport:
    config = browser_config or {}
    runtime = config.get("runtime", "patchright")
    channel = config.get("channel", "chrome")
    runtimes = {
        "patchright": _package_status("patchright", label="Patchright"),
        "playwright": _package_status("playwright", label="Playwright"),
    }
    engines: dict[str, CapabilityStatus] = {}
    for name in ("chromium", "firefox", "webkit"):
        package = "patchright" if runtime == "patchright" else "playwright"
        status = runtimes[package].model_copy(deep=True)
        if status.state == "ready":
            status = _status(
                "unknown",
                "browser_binary_unprobed",
                f"{name} browser binary was not probed.",
            )
        engines[name] = status
    channels: dict[str, CapabilityStatus] = {}
    if isinstance(channel, str) and channel in _KNOWN_CHANNELS:
        if channel in _CHANNEL_BINARIES:
            try:
                available = any(
                    shutil.which(candidate) is not None for candidate in _CHANNEL_BINARIES[channel]
                )
            except Exception:
                return BrowserCapabilityReport(
                    runtimes=runtimes,
                    engines=engines,
                    channels={
                        channel: _status(
                            "unknown",
                            "channel_probe_failed",
                            f"Browser channel '{channel}' could not be checked.",
                        )
                    },
                )
            channels[channel] = _status(
                "ready" if available else "unavailable",
                "available" if available else "missing_browser",
                f"Browser channel '{channel}' is "
                + ("available." if available else "not available."),
            )
        else:
            channels[channel] = _status(
                "installable",
                "bundled_browser_unprobed",
                f"Browser channel '{channel}' can use a bundled browser.",
            )
    else:
        channels["unknown"] = _status(
            "unknown", "unknown_channel", "Configured browser channel is not allowlisted."
        )
    return BrowserCapabilityReport(runtimes=runtimes, engines=engines, channels=channels)


def collect_runtime_capabilities(
    *,
    desired_tools: list[str] | None = None,
    observed_tools: list[Any] | None = None,
    browser_config: dict[str, Any] | None = None,
    lsp_enabled: bool = True,
    runtime_metadata: dict[str, Any] | None = None,
) -> RuntimeCapabilityReport:
    """Collect bounded, non-mutating capability telemetry."""
    try:
        executor_version = importlib.metadata.version("cognis-executor")
    except Exception:
        executor_version = "unknown"
    image_variant = os.environ.get("COGNIS_EXECUTOR_VARIANT")

    static_tools: list[str] = []
    try:
        from cognis.tools.executor.definitions import executor_tool_definitions

        static_tools = sorted(
            {
                tool.name
                for tool in executor_tool_definitions()
                if isinstance(tool.name, str) and len(tool.name) <= _MAX_NAME_LENGTH
            }
        )
    except Exception:
        static_tools = []
    observed_names = sorted(
        {
            tool.name
            for tool in (observed_tools or [])
            if hasattr(tool, "name")
            and getattr(getattr(tool, "source", None), "type", None) != "local_mcp"
            and isinstance(tool.name, str)
            and 0 < len(tool.name) <= _MAX_NAME_LENGTH
        }
    )
    supported_names = sorted(set(static_tools) | set(observed_names))
    desired_names = sorted(
        {
            name
            for name in (desired_tools or ())
            if isinstance(name, str) and 0 < len(name) <= _MAX_NAME_LENGTH
        }
    )[:_MAX_ITEMS]
    components = {name: _component_status(name) for name in COMPONENTS}
    officecli_status = _officecli_status(runtime_metadata)
    components["officecli"] = officecli_status
    lsp_status = _lsp_status(enabled=lsp_enabled)
    return RuntimeCapabilityReport(
        observed_at=datetime.now(UTC),
        executor_version=executor_version,
        image_variant=image_variant,
        desired_tools=desired_names,
        observed_tools=observed_names,
        supported_tools=supported_names,
        supported_components=sorted(
            name for name, status in components.items() if status.state == "ready"
        ),
        components=components,
        browser=_browser_report(browser_config),
        officecli=officecli_status,
        mcp_launch=components["mcp"],
        git=_command_status("git"),
        node=_command_status("node"),
        uv=_command_status("uv"),
        lsp=lsp_status,
    )
