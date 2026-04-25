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
    """Resolved runtime objects for one step execution."""

    tool_registry: Any
    executor_connection: Any
    cleanup: Callable[[], Awaitable[None]]
    executor_environment: ExecutorEnvironmentSnapshot | None
    runtime_info: dict[str, Any] | None = None


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
