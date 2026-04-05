"""Shared path resolution for executor tools."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_path(raw: str | None, *, default_to_home: bool = False) -> Path:
    """Resolve a user-provided path, expanding ``~`` and environment variables."""

    if raw is None or raw == "":
        if default_to_home:
            return Path.home()
        return Path("")
    return Path(os.path.expandvars(os.path.expanduser(raw)))
