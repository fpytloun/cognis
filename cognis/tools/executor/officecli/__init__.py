"""Executor-native OfficeCLI runtime and Office document tools."""

from cognis.tools.executor.officecli.install import (
    OFFICECLI_RUNTIME_METADATA_KEY,
    OfficeCliRuntimeConfig,
    OfficeCliStatus,
    ensure_officecli,
    resolve_officecli_runtime_config,
)

__all__ = [
    "OFFICECLI_RUNTIME_METADATA_KEY",
    "OfficeCliRuntimeConfig",
    "OfficeCliStatus",
    "ensure_officecli",
    "resolve_officecli_runtime_config",
]
