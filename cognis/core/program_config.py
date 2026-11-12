"""Executor eligibility for the non-sandboxed Python program runtime."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import ToolDefinition

PROGRAM_RUNTIME_TOOL_NAMES = frozenset(
    {"python_execute", "python_kernel_status", "python_kernel_reset"}
)
PROGRAM_RUNTIME_PROFILE_LABEL = "cognis_runtime_profile"


def program_runtime_executor_enabled(executor_config: dict[str, Any] | None) -> bool:
    """Allow configured pod identities by default; require explicit opt-in otherwise."""
    if not isinstance(executor_config, dict):
        return False
    runtime_config = executor_config.get("config")
    if runtime_config is not None and not isinstance(runtime_config, dict):
        return False
    program_runtime = (
        runtime_config.get("program_runtime") if isinstance(runtime_config, dict) else None
    )
    if program_runtime is not None and not isinstance(program_runtime, dict):
        return False
    program_runtime = program_runtime if isinstance(program_runtime, dict) else {}
    if program_runtime.get("enabled") is False:
        return False
    labels = executor_config.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    executor_id = executor_config.get("executor_id")
    is_configured_pod = (
        isinstance(executor_id, str)
        and bool(executor_id)
        and labels.get(PROGRAM_RUNTIME_PROFILE_LABEL) == "pod"
    )
    if is_configured_pod:
        return True
    return program_runtime.get("host_enabled") is True


def program_tool_allowed(tool: ToolDefinition, enabled: bool) -> bool:
    """Reserve native program names; leave all unrelated definitions untouched."""
    return enabled or tool.name not in PROGRAM_RUNTIME_TOOL_NAMES


__all__ = [
    "PROGRAM_RUNTIME_PROFILE_LABEL",
    "PROGRAM_RUNTIME_TOOL_NAMES",
    "program_runtime_executor_enabled",
    "program_tool_allowed",
]
