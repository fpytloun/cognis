"""Executor-native browser tool definitions."""

from __future__ import annotations

from cognis.models.tool import ToolDefinition, ToolSource

_SOURCE = ToolSource(type="executor")


def browser_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="browser_open",
            description="Open or reuse a browser session and navigate to a URL.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "session_id": {"type": "string"},
                    "headless": {"type": "boolean"},
                    "auth_state_ref": {"type": "string"},
                },
                "required": ["url", "session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=90,
        ),
        ToolDefinition(
            name="browser_snapshot",
            description="Return a compact structured snapshot of the current browser page.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_get_text",
            description="Extract bounded visible text from the current page.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}, "max_chars": {"type": "integer"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element by ref or selector.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_fill",
            description="Fill an input by ref or selector using literal value or value_ref.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "value_ref": {"type": "string"},
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_press",
            description="Press a keyboard key in the current page.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}, "key": {"type": "string"}},
                "required": ["session_id", "key"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_wait_for",
            description="Wait for a selector or a timeout in the current page.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_screenshot",
            description="Capture a screenshot of the current page as an attachment artifact.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_close",
            description="Close a browser session.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_save_auth_state",
            description="Persist the current browser auth state as an encrypted credential record.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "credential_id": {"type": "string"},
                    "label": {"type": "string"},
                    "origin": {"type": "string"},
                },
                "required": ["session_id", "credential_id", "label"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=60,
        ),
    ]
