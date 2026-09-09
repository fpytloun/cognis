"""Compatibility alias for the common MCP runtime."""

from __future__ import annotations

import sys

from cognis import mcp_runtime as _runtime
from cognis.api.mcp_policy import canonicalize_mcp_headers, invalid_mcp_config_reason

_runtime.canonicalize_mcp_headers = canonicalize_mcp_headers  # type: ignore[attr-defined]
_runtime.invalid_mcp_config_reason = invalid_mcp_config_reason  # type: ignore[attr-defined]
sys.modules[__name__] = _runtime
