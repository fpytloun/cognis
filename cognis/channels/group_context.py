"""Strict policy parsing for bounded preceding group-chat context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GROUP_CONTEXT_MAX_MESSAGES = 10
GROUP_CONTEXT_MAX_BYTES = 8 * 1024
GROUP_CONTEXT_MAX_AGE_SECONDS = 15 * 60
GROUP_CONTEXT_MAX_RETENTION_SECONDS = 24 * 60 * 60
GROUP_CONTEXT_RESERVATION_SECONDS = 5 * 60


class GroupContextSettingsError(ValueError):
    """A group-context account setting has an invalid type or range."""


@dataclass(frozen=True, slots=True)
class GroupContextPolicy:
    enabled: bool = False
    max_messages: int = GROUP_CONTEXT_MAX_MESSAGES
    max_bytes: int = GROUP_CONTEXT_MAX_BYTES
    max_age_seconds: int = GROUP_CONTEXT_MAX_AGE_SECONDS
    retention_seconds: int = GROUP_CONTEXT_MAX_RETENTION_SECONDS


def _strict_bool(settings: dict[str, Any], name: str, default: bool) -> bool:
    value = settings.get(name, default)
    if type(value) is not bool:
        raise GroupContextSettingsError(f"{name} must be a boolean")
    return value


def _strict_int(
    settings: dict[str, Any],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    value = settings.get(name, default)
    if type(value) is not int:
        raise GroupContextSettingsError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise GroupContextSettingsError(f"{name} must be between 1 and {maximum}")
    return value


def group_context_policy(settings: dict[str, Any]) -> GroupContextPolicy:
    """Parse account settings without coercing strings or booleans to integers."""

    return GroupContextPolicy(
        enabled=_strict_bool(settings, "group_context_enabled", False),
        max_messages=_strict_int(
            settings,
            "group_context_max_messages",
            GROUP_CONTEXT_MAX_MESSAGES,
            maximum=GROUP_CONTEXT_MAX_MESSAGES,
        ),
        max_bytes=_strict_int(
            settings,
            "group_context_max_bytes",
            GROUP_CONTEXT_MAX_BYTES,
            maximum=GROUP_CONTEXT_MAX_BYTES,
        ),
        max_age_seconds=_strict_int(
            settings,
            "group_context_max_age_seconds",
            GROUP_CONTEXT_MAX_AGE_SECONDS,
            maximum=GROUP_CONTEXT_MAX_AGE_SECONDS,
        ),
        retention_seconds=_strict_int(
            settings,
            "group_context_retention_seconds",
            GROUP_CONTEXT_MAX_RETENTION_SECONDS,
            maximum=GROUP_CONTEXT_MAX_RETENTION_SECONDS,
        ),
    )
