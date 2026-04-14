"""Executor-native browser tool definitions."""

from __future__ import annotations

from cognis.models.tool import ToolDefinition, ToolSource

_SOURCE = ToolSource(type="executor")


def browser_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="browser_open",
            description=(
                "Open or reuse a browser session and navigate to a URL. "
                "Use profile_mode='default' unless you specifically need a fresh one-off session. "
                "When persistent_local mode is used, you may omit profile_id and Cognis will derive a stable site profile automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "session_id": {"type": "string"},
                    "headless": {"type": "boolean"},
                    "profile_mode": {
                        "type": "string",
                        "enum": ["default", "ephemeral", "persistent_local"],
                        "description": "Browser profile strategy. Use 'default' unless you specifically need a fresh session.",
                    },
                    "profile_id": {
                        "type": "string",
                        "description": "Optional persistent local profile ID. Only use with persistent_local. Omit to let Cognis derive a stable site profile automatically.",
                    },
                    "auth_state_ref": {
                        "type": "string",
                        "description": "Optional saved browser auth state ref. Must reference a browser_storage_state credential such as $credential:rohlik-browser. Use browser_fill value_ref for raw username/password fields instead.",
                    },
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
            description=(
                "Return a compact structured snapshot of the current browser page, including visibility and editability metadata for interactive elements."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "max_elements": {
                        "type": "integer",
                        "description": "Maximum number of interactive elements to include in the snapshot.",
                    },
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_list_sessions",
            description="List active browser sessions and their metadata so you can resume an existing session.",
            parameters={"type": "object", "properties": {}},
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_list_profiles",
            description="List persistent local browser profiles available on this executor.",
            parameters={"type": "object", "properties": {}},
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_query",
            description="Query page elements by selector and return detailed candidate metadata. Returned refs can be used by later browser actions.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["all", "actionable", "clickable", "fillable"],
                    },
                    "limit": {"type": "integer"},
                    "include_computed": {"type": "boolean"},
                },
                "required": ["session_id", "selector"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_eval",
            description="Evaluate arbitrary JSON-returning JavaScript in the current page context. This is a powerful debug/control tool and can mutate page state.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "script": {"type": "string"},
                    "args": {
                        "type": "array",
                        "items": {},
                        "description": "Optional JSON-serializable arguments passed to the evaluated function.",
                    },
                },
                "required": ["session_id", "script"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_get_console",
            description="Get recent console and page-error events for the current session.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "level": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_get_network",
            description="Get recent network events for the current session.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer"},
                    "resource_types": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
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
            description="Click an element by exact ref or by selector. Selector mode fails if multiple viable candidates match.",
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
            description=(
                "Fill an input by exact ref or by selector using literal value or value_ref. "
                "Selector mode fails if multiple viable candidates match. Example value_ref refs: $credential:rohlik.username or $credential:rohlik.password."
            ),
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
            name="browser_focus",
            description="Focus an element by exact ref or by selector. Selector mode fails if multiple viable candidates match.",
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
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_type",
            description="Focus and type text into an input using key events. Useful for OTP/MFA flows.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "delay_ms": {"type": "integer"},
                },
                "required": ["session_id", "text"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=60,
        ),
        ToolDefinition(
            name="browser_submit_form",
            description="Submit a form relative to an exact ref or selector using native or event mode.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "mode": {"type": "string", "enum": ["native", "event"]},
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
            description="Wait for a CSS selector or a timeout in the current page. For text-based detection, use browser_get_text instead.",
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
