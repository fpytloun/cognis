"""Runtime metadata models shared across execution layers."""

from __future__ import annotations

import datetime as dt
import getpass
import os
import platform
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cognis.core.executor_pool import ExecutorPoolRefresher


class ExecutorConfigurationError(RuntimeError):
    """Executor configuration must be corrected before execution can start."""


def require_explicit_executor(execution: dict[str, Any] | None) -> None:
    """Reject execution without a configured primary executor target."""
    config = execution or {}
    if not config.get("executor_id") and not config.get("executor_selector"):
        raise ExecutorConfigurationError(
            "Agent must explicitly configure executor_id or executor_selector. "
            "Configure an executor on the agent page before activating it or "
            "sharing it with owner_executor scope."
        )


class TransientExecutorUnavailable(RuntimeError):
    """Selected executor is temporarily unavailable and may recover shortly."""

    def __init__(
        self,
        message: str,
        *,
        executor_id: str | None = None,
        retry_after_seconds: int = 5,
    ) -> None:
        super().__init__(message)
        self.executor_id = executor_id
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class ExecutorEnvironmentSnapshot:
    """Observed environment information for the selected tool executor."""

    available: bool
    executor_id: str | None = None
    executor_type: str | None = None
    user: str | None = None
    home: str | None = None
    cwd: str | None = None
    hostname: str | None = None
    platform_os: str | None = None
    platform_arch: str | None = None
    platform_python: str | None = None
    source: str | None = None
    observed_at: str | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        executor_id: str | None = None,
        executor_type: str | None = None,
        source: str | None = None,
    ) -> ExecutorEnvironmentSnapshot:
        """Create an unavailable snapshot with executor identity only."""

        return cls(
            available=False,
            executor_id=executor_id,
            executor_type=executor_type,
            source=source,
        )


@dataclass(slots=True)
class ResolvedStepRuntime:
    """Resolved runtime objects for one step execution.

    Stage 36 (multi-executor agents): ``executor_pool`` carries the agent's
    full assigned executor pool (primary + additional) when available, and
    ``active_executor_id`` is the executor selected for the *active*
    routing slot. Tool calls without an explicit ``target_executor`` go to
    the active executor; tool calls with ``target_executor=X`` resolve X
    against ``executor_pool``. Both fields default to None for backward
    compatibility with single-executor callers.
    """

    tool_registry: Any
    executor_connection: Any
    cleanup: Callable[[], Awaitable[None]]
    executor_environment: ExecutorEnvironmentSnapshot | None
    runtime_info: dict[str, Any] | None = None
    executor_pool: Any = None  # Optional[ExecutorPool] (avoid forward ref)
    active_executor_id: str | None = None
    rebuild_tool_registry: Callable[[Any], Awaitable[Any]] | None = None
    program_policy: Callable[[Any], Awaitable[bool]] | None = None
    refresh_executor_pool: ExecutorPoolRefresher | None = None


def build_local_executor_environment(
    *,
    executor_id: str | None = None,
    executor_type: str = "in_process",
    source: str = "local_runtime",
) -> ExecutorEnvironmentSnapshot:
    """Capture the local process environment for an in-process executor."""

    try:
        user = getpass.getuser()
    except Exception:
        user = None

    return ExecutorEnvironmentSnapshot(
        available=True,
        executor_id=executor_id,
        executor_type=executor_type,
        user=user,
        home=str(Path.home()),
        cwd=os.getcwd(),
        hostname=platform.node(),
        platform_os=sys.platform,
        platform_arch=platform.machine(),
        platform_python=platform.python_version(),
        source=source,
        observed_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def environment_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    executor_id: str | None = None,
    executor_type: str | None = None,
    fallback_source: str = "executor_metadata",
) -> ExecutorEnvironmentSnapshot:
    """Build a normalized executor environment snapshot from metadata."""

    payload = dict(metadata or {})
    environment = payload.get("environment")
    platform_data = payload.get("platform") or {}
    if not isinstance(environment, dict):
        return ExecutorEnvironmentSnapshot.unavailable(
            executor_id=executor_id,
            executor_type=executor_type,
            source=fallback_source,
        )

    return ExecutorEnvironmentSnapshot(
        available=True,
        executor_id=executor_id,
        executor_type=executor_type,
        user=_string_or_none(environment.get("user")),
        home=_string_or_none(environment.get("home")),
        cwd=_string_or_none(environment.get("cwd")),
        hostname=_string_or_none(environment.get("hostname")),
        platform_os=_string_or_none(platform_data.get("os")),
        platform_arch=_string_or_none(platform_data.get("arch")),
        platform_python=_string_or_none(platform_data.get("python")),
        source=_string_or_none(environment.get("source")) or fallback_source,
        observed_at=_string_or_none(environment.get("observed_at")),
    )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
