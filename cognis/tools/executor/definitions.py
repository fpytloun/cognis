"""Executor-native tool definitions and handler registry."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.executor.filesystem import (
    handle_edit,
    handle_list_directory,
    handle_multiedit,
    handle_patch,
    handle_read,
    handle_write,
)
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import handle_bash
from cognis.tools.executor.web import handle_web_fetch

_EXECUTOR_SOURCE = ToolSource(type="executor")

# -- Filesystem tools ----------------------------------------------------------

READ_TOOL = ToolDefinition(
    name="read",
    description="Read a file or directory from the filesystem. Returns line-numbered content.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to file or directory"},
            "offset": {
                "type": "integer",
                "description": "Line number to start from (1-indexed, default 1)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read (default 2000)",
            },
        },
        "required": ["file_path"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)

WRITE_TOOL = ToolDefinition(
    name="write",
    description="Write content to a file, creating it and parent directories if needed.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["file_path", "content"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

EDIT_TOOL = ToolDefinition(
    name="edit",
    description="Edit a file by replacing an exact text match with new text.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "old_string": {"type": "string", "description": "Exact text to find and replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default false)",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

PATCH_TOOL = ToolDefinition(
    name="patch",
    description="Apply a unified diff patch to one or more files.",
    parameters={
        "type": "object",
        "properties": {
            "patch_text": {"type": "string", "description": "Unified diff patch text"},
        },
        "required": ["patch_text"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

MULTIEDIT_TOOL = ToolDefinition(
    name="multiedit",
    description="Apply multiple sequential text replacements to a single file.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Text to find"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
                "description": "List of edit operations to apply sequentially",
            },
        },
        "required": ["file_path", "edits"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

LIST_DIRECTORY_TOOL = ToolDefinition(
    name="list_directory",
    description="List files and subdirectories in a directory.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the directory"},
            "ignore": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Glob patterns to ignore",
            },
        },
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)

# -- Search tools --------------------------------------------------------------

GLOB_TOOL = ToolDefinition(
    name="glob",
    description="Find files matching a glob pattern. Returns paths sorted by modification time.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
            "path": {"type": "string", "description": "Directory to search in"},
        },
        "required": ["pattern"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)

GREP_TOOL = ToolDefinition(
    name="grep",
    description="Search file contents using regex. Returns matching file paths and line numbers.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory to search in"},
            "include": {
                "type": "string",
                "description": "File pattern filter (e.g. '*.py', '*.{ts,tsx}')",
            },
        },
        "required": ["pattern"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)

# -- Shell tools ---------------------------------------------------------------

BASH_TOOL = ToolDefinition(
    name="bash",
    description="Execute a shell command and return its output.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "description": {
                "type": "string",
                "description": "Brief description of what this command does",
            },
            "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
            "workdir": {"type": "string", "description": "Working directory for the command"},
        },
        "required": ["command"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=120,
)

# -- Web tools -----------------------------------------------------------------

WEB_FETCH_TOOL = ToolDefinition(
    name="web_fetch",
    description="Fetch content from a URL and return it as text or markdown.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "Output format (default: markdown)",
            },
            "timeout": {"type": "integer", "description": "Timeout in seconds (max 120)"},
        },
        "required": ["url"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=60,
)

# -- Public API ----------------------------------------------------------------

ALL_EXECUTOR_TOOLS: list[ToolDefinition] = [
    READ_TOOL,
    WRITE_TOOL,
    EDIT_TOOL,
    PATCH_TOOL,
    MULTIEDIT_TOOL,
    LIST_DIRECTORY_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    BASH_TOOL,
    WEB_FETCH_TOOL,
]

_HANDLER_MAP: dict[
    str,
    Any,
] = {
    "read": handle_read,
    "write": handle_write,
    "edit": handle_edit,
    "patch": handle_patch,
    "multiedit": handle_multiedit,
    "list_directory": handle_list_directory,
    "glob": handle_glob,
    "grep": handle_grep,
    "bash": handle_bash,
    "web_fetch": handle_web_fetch,
}


def executor_tool_definitions() -> list[ToolDefinition]:
    """Return all executor-native tool definitions."""
    return list(ALL_EXECUTOR_TOOLS)


def executor_tool_handlers() -> dict[str, Any]:
    """Return a mapping of tool name to async handler function.

    Each handler has the signature:
        async def handler(arguments: dict, context: ToolExecutionContext) -> ToolHandlerResult
    """
    return dict(_HANDLER_MAP)
