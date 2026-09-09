"""Ownership and shared-resource helpers."""

from __future__ import annotations

from typing import Literal

SYSTEM_USER_EMAIL = "system@cognis.local"
ExecutorScope = Literal["owner_executor", "grantee_executor"]


def is_shared_owner_email(owner_email: str | None) -> bool:
    """Return True when a resource owner denotes shared system ownership."""

    return owner_email in {None, SYSTEM_USER_EMAIL}


def canonical_shared_owner_email() -> str:
    """Return the canonical owner email used for shared system resources."""

    return SYSTEM_USER_EMAIL


def normalize_executor_scope(value: str | None) -> ExecutorScope:
    """Return a normalized executor scope with a safe fallback."""

    return "grantee_executor" if value == "grantee_executor" else "owner_executor"
