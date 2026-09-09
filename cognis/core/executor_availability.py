"""Controller-owned executor package and type availability checks."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution

EXECUTOR_DISTRIBUTION_NAME = "cognis-executor"
LOCAL_EXECUTOR_TYPES = frozenset({"in_process", "subprocess"})
ALL_EXECUTOR_TYPES = ("in_process", "subprocess", "websocket")
KNOWN_EXECUTOR_TYPES = frozenset(ALL_EXECUTOR_TYPES)
EXECUTOR_PACKAGE_UNAVAILABLE_REASON = (
    "cognis-executor package is not installed; local executors are unavailable"
)


@lru_cache(maxsize=1)
def is_executor_package_available() -> bool:
    """Return process-lifetime package availability.

    Installing or removing ``cognis-executor`` requires a controller restart.
    """

    try:
        distribution(EXECUTOR_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return False
    return True


def is_executor_type_available(executor_type: str) -> bool:
    """Return whether the controller can use an executor type."""

    return executor_type in KNOWN_EXECUTOR_TYPES and (
        executor_type not in LOCAL_EXECUTOR_TYPES or is_executor_package_available()
    )


def unavailable_executor_reason(executor_type: str) -> str | None:
    """Return a bounded reason when an executor type is unavailable."""

    if executor_type in LOCAL_EXECUTOR_TYPES and not is_executor_package_available():
        return EXECUTOR_PACKAGE_UNAVAILABLE_REASON
    return None


def available_executor_types() -> list[str]:
    """Return executor types available for API creation and routing."""

    if is_executor_package_available():
        return list(ALL_EXECUTOR_TYPES)
    return ["websocket"]
