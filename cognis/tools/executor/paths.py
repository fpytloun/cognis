"""Shared path resolution for executor tools."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_path(raw: str) -> Path:
    """Resolve a user-provided path, expanding ``~`` and environment variables."""
    return Path(os.path.expandvars(os.path.expanduser(raw)))
