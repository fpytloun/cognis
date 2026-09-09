"""Compatibility alias for the common MCP runtime."""

from __future__ import annotations

import sys

from cognis import mcp_runtime as _runtime

sys.modules[__name__] = _runtime
