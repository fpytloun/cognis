"""Executor-native browser tool definitions."""

from __future__ import annotations

from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource

_SOURCE = ToolSource(type="executor")


def browser_tool_definitions() -> list[ToolDefinition]:
    definitions = [
        ToolDefinition(
            name="browser_open",
            description=(
                "Open or reuse a browser session and navigate to a URL. "
                "Use profile_mode='default' unless you specifically need a fresh one-off session. "
                "When persistent_local mode is used, you may omit profile_id and Cognis will derive a stable site profile automatically. "
                "For sites that reject headless automation or return a WAF/vendor block, close the blocked session and retry with headless=false when headed mode is available. "
                "Use browser_settings to override per-session behavior such as auto_consent='off' for fragile SSO/login flows."
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
                    "browser_settings": {
                        "type": "object",
                        "additionalProperties": False,
                        "description": "Optional behavior overrides for newly created sessions. Overrides cannot change an already-open browser context; use a new session_id or close/reopen to change them.",
                        "properties": {
                            "auto_consent": {
                                "type": "string",
                                "enum": ["accept", "reject", "off"],
                                "description": "Cookie-consent automation action. Use 'off' for fragile SSO/login shells.",
                            },
                            "stealth_enabled": {
                                "type": "boolean",
                                "description": "Override stealth/fingerprint-realism init behavior for this session.",
                            },
                            "fingerprint_hardening": {
                                "type": "boolean",
                                "description": "Override fingerprint hardening init scripts for this session.",
                            },
                            "humanize_input": {
                                "type": "boolean",
                                "description": "Override realistic mouse/keyboard input behavior for this session.",
                            },
                        },
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
            description="List active browser sessions owned by the current execution or its directly managed descendants. Returns safe owner, conversation, relationship, and lifecycle state metadata.",
            parameters={"type": "object", "properties": {}},
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_inspect_session",
            description="Inspect one active browser session visible to the current execution. Unrelated sessions are never exposed.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_list_profiles",
            description="Inspect persistent local browser profiles visible to the current execution. On an executor privately owned by the current user, set include_unclaimed=true to discover legacy profiles that require an explicit verified claim. Optionally retry conservative stale SingletonLock recovery.",
            parameters={
                "type": "object",
                "properties": {
                    "reclaim_stale": {
                        "type": "boolean",
                        "description": "Retry conservative orphaned SingletonLock recovery before listing profiles.",
                    },
                    "include_unclaimed": {
                        "type": "boolean",
                        "description": "Include non-empty legacy profiles with no ownership metadata. These remain unusable until explicitly claimed.",
                    },
                },
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
        ),
        ToolDefinition(
            name="browser_claim_profile",
            description=(
                "Claim a non-empty legacy persistent profile that has no ownership metadata. "
                "Claims are allowed only on an executor privately owned by the current user. "
                "Use only after the user or operator verifies that the profile belongs to the current Cognis user and Chromium is stopped. "
                "This never transfers a profile already claimed by another user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "profile_id": {"type": "string"},
                    "confirm_profile_id": {
                        "type": "string",
                        "description": "Must exactly repeat profile_id as an explicit claim confirmation.",
                    },
                    "reclaim_stale": {
                        "type": "boolean",
                        "description": "Conservatively remove a confirmed orphaned SingletonLock before claiming.",
                    },
                },
                "required": ["profile_id", "confirm_profile_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
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
            description=(
                "Evaluate arbitrary JSON-returning JavaScript in the current page context. "
                "This is a powerful debug/control tool and can mutate page state. "
                "Arguments may include {value_ref: '$credential:id.field'} or deferred "
                "{value_ref: '$auth_challenge:id.code', auth_challenge: {...}} markers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "script": {"type": "string"},
                    "args": {
                        "type": "array",
                        "items": {},
                        "description": "Optional JSON-serializable arguments passed to the evaluated function. Use value_ref marker objects for credentials or deferred auth challenge values.",
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
            name="browser_get_focus",
            description="Return the currently focused frame and focused element metadata without exposing field values.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=True,
            timeout_seconds=30,
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
                    "intensity": {
                        "type": "string",
                        "enum": ["off", "low", "medium", "high"],
                        "description": (
                            "Mouse-movement humanization intensity for this click. "
                            "Defaults to the executor's configured value. Set to 'off' "
                            "for the fastest possible click."
                        ),
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after the click, in milliseconds.",
                    },
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
                "Selector mode fails if multiple viable candidates match. Example value_ref refs: $credential:rohlik.username, $credential:rohlik.password, or deferred $auth_challenge:reddit.code with auth_challenge metadata."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "value_ref": {"type": "string"},
                    "auth_challenge": {
                        "type": "object",
                        "description": "Optional deferred auth challenge metadata used when value_ref starts with $auth_challenge:.",
                    },
                    "intensity": {
                        "type": "string",
                        "enum": ["off", "low", "medium", "high"],
                        "description": (
                            "Humanization intensity for the clear+type sequence. "
                            "Defaults to the executor's configured value."
                        ),
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after filling, in milliseconds.",
                    },
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
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after focusing, in milliseconds.",
                    },
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
                    "value_ref": {"type": "string"},
                    "auth_challenge": {
                        "type": "object",
                        "description": "Optional deferred auth challenge metadata used when value_ref starts with $auth_challenge:.",
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": (
                            "Per-key delay in milliseconds. Only honoured when "
                            "intensity='off'; otherwise the humanizer's distribution applies."
                        ),
                    },
                    "intensity": {
                        "type": "string",
                        "enum": ["off", "low", "medium", "high"],
                        "description": (
                            "Keystroke-cadence humanization intensity. Defaults to the "
                            "executor's configured value."
                        ),
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after typing, in milliseconds.",
                    },
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
            name="browser_submit_form",
            description="Submit a form relative to an exact ref or selector using native or event mode.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "mode": {"type": "string", "enum": ["native", "event"]},
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after submitting, in milliseconds.",
                    },
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
            name="browser_select",
            description="Select one or more options in a native <select> element by value, label, or index.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option values to select.",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option labels to select.",
                    },
                    "indexes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Zero-based option indexes to select.",
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after selecting, in milliseconds.",
                    },
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
            name="browser_upload",
            description=(
                "Attach one or more files to a page. Use mode='input' for a file input "
                "or mode='file_chooser' for an attachment button that opens a chooser. "
                "Use source_artifact_ids for Cognis artifacts or file_paths for executor-local files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "mode": {"type": "string", "enum": ["input", "file_chooser"]},
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Executor-local absolute paths to attach.",
                    },
                    "source_artifact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Cognis artifact ids to materialize directly into the browser upload.",
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after uploading, in milliseconds.",
                    },
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=90,
        ),
        ToolDefinition(
            name="browser_download_wait",
            description="Click a target and wait for a browser download, returning the downloaded file as a Cognis attachment artifact.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after the download starts, in milliseconds.",
                    },
                },
                "required": ["session_id"],
            },
            source=_SOURCE,
            category="browser",
            read_only=False,
            non_bypassable=True,
            timeout_seconds=120,
        ),
        ToolDefinition(
            name="browser_scroll",
            description="Scroll the page or a target element by a pixel delta to reveal lazy-loaded or off-screen controls.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "delta_x": {"type": "integer"},
                    "delta_y": {"type": "integer"},
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after scrolling, in milliseconds.",
                    },
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
            name="browser_hover",
            description="Hover a visible enabled target to reveal menus, toolbars, or drag handles.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "selector": {"type": "string"},
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after hovering, in milliseconds.",
                    },
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
            name="browser_drag_drop",
            description="Drag one visible enabled element onto another, useful for drag-and-drop upload zones or sortable UIs.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "source_selector": {"type": "string"},
                    "target_ref": {"type": "string"},
                    "target_selector": {"type": "string"},
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after drag-and-drop, in milliseconds.",
                    },
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
            description="Press a keyboard key or type text/value_ref into the currently focused element or frame.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "key": {"type": "string"},
                    "text": {"type": "string"},
                    "value_ref": {"type": "string"},
                    "auth_challenge": {
                        "type": "object",
                        "description": "Optional deferred auth challenge metadata used when value_ref starts with $auth_challenge:.",
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": "Per-key delay in milliseconds when typing text.",
                    },
                    "intensity": {
                        "type": "string",
                        "enum": ["off", "low", "medium", "high"],
                        "description": "Keystroke-cadence humanization intensity for text typing.",
                    },
                    "wait_after_ms": {
                        "type": "integer",
                        "description": "Optional delay after pressing or typing, in milliseconds.",
                    },
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
            name="browser_wait_for",
            description="Wait for a CSS selector or a timeout in the current page. For text-based detection, use browser_get_text instead.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "state": {
                        "type": "string",
                        "enum": ["attached", "visible", "hidden", "detached"],
                        "description": "Selector state to wait for. Defaults to visible.",
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
            description="Idempotently close a browser session owned by the current execution. A controlling parent may reclaim a directly managed descendant only after Cognis has verified that descendant is terminal; active descendants remain protected.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "release_managed_descendant": {
                        "type": "boolean",
                        "description": "Allow reclaim of a directly managed terminal descendant session. Ownership lineage and terminal state are verified by the executor.",
                    },
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
    for definition in definitions:
        definition.content_trust = "untrusted"
    return definitions
