"""Executor-native tool definitions and handler registry."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.executor.browser.definitions import browser_tool_definitions
from cognis.tools.executor.browser.handlers import (
    handle_browser_click,
    handle_browser_close,
    handle_browser_eval,
    handle_browser_fill,
    handle_browser_focus,
    handle_browser_get_console,
    handle_browser_get_focus,
    handle_browser_get_network,
    handle_browser_get_text,
    handle_browser_list_profiles,
    handle_browser_list_sessions,
    handle_browser_open,
    handle_browser_press,
    handle_browser_query,
    handle_browser_save_auth_state,
    handle_browser_screenshot,
    handle_browser_snapshot,
    handle_browser_submit_form,
    handle_browser_type,
    handle_browser_wait_for,
)
from cognis.tools.executor.document import (
    ARTIFACT_PUBLISH_TOOL,
    DOCUMENT_GENERATE_TOOL,
    handle_artifact_publish,
    handle_document_generate,
)
from cognis.tools.executor.filesystem import (
    handle_apply_patch,
    handle_artifact_save,
    handle_edit,
    handle_list_directory,
    handle_multiedit,
    handle_read,
    handle_skill_asset_materialize,
    handle_write,
)
from cognis.tools.executor.lsp.tool import handle_lsp
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import handle_bash, handle_bash_kill, handle_bash_output
from cognis.tools.executor.web import (
    handle_web_crawl,
    handle_web_fetch,
    handle_web_map,
    handle_web_research,
    handle_web_search,
)

_EXECUTOR_SOURCE = ToolSource(type="executor")

# -- Filesystem tools ----------------------------------------------------------

READ_TOOL = ToolDefinition(
    name="read",
    description=(
        "Read a file or directory from the filesystem. Text files return line-numbered "
        "content; offset and limit apply to text files only. Supported binary files are "
        "routed through attachment analysis."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to file or directory. Use ~ for home directory.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start from for text files (1-indexed, default 1)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read for text files (default 2000)",
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
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file. Use ~ for home directory.",
            },
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

ARTIFACT_SAVE_TOOL = ToolDefinition(
    name="artifact_save",
    description=(
        "Save a Cognis artifact to a local executor file path. Use this when you need a saved "
        "image, PDF, or other artifact as a real filesystem file for subsequent tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to write on the executor filesystem.",
            },
            "source_artifact_id": {
                "type": "string",
                "description": "Cognis artifact id to save to the executor filesystem.",
            },
        },
        "required": ["file_path", "source_artifact_id"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

SKILL_ASSET_MATERIALIZE_TOOL = ToolDefinition(
    name="skill_asset_materialize",
    description=(
        "Materialize an attached skill asset onto the executor filesystem and return "
        "its local_path. Prefer calling available skill tools directly for runnable "
        "skill behavior; use this for asset-only scripts or asset inspection."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "Skill ID that owns the asset."},
            "asset_id": {"type": "string", "description": "Asset ID from skill_load asset_manifest."},
            "filename": {
                "type": "string",
                "description": "Optional asset filename to disambiguate when asset_id is not known.",
            },
            "target_path": {
                "type": "string",
                "description": "Optional absolute executor path to write. Defaults to a stable temp-cache path.",
            },
        },
        "required": ["skill_id"],
    },
    source=_EXECUTOR_SOURCE,
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

EDIT_TOOL = ToolDefinition(
    name="edit",
    description="Edit a file by replacing an exact text match with new text.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file. Use ~ for home directory.",
            },
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

APPLY_PATCH_TOOL = ToolDefinition(
    name="apply_patch",
    description=(
        "Apply a strict patch to one or more text files using the apply_patch "
        "envelope or supported unified diff update subset."
    ),
    parameters={
        "type": "object",
        "description": "Provide patchText.",
        "properties": {
            "patchText": {
                "type": "string",
                "description": "Patch text in apply_patch envelope syntax or the supported unified diff update subset",
            },
        },
        "required": ["patchText"],
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
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file. Use ~ for home directory.",
            },
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
            "path": {
                "type": "string",
                "description": "Absolute path to the directory. Use ~ for home directory. Defaults to the executor home directory if omitted.",
            },
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

LSP_TOOL = ToolDefinition(
    name="lsp",
    description=(
        "Query language-server features like definition, references, hover, and symbols. "
        "Position-based operations require line and character. workspaceSymbol requires a non-empty query."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "goToDefinition",
                    "findReferences",
                    "hover",
                    "documentSymbol",
                    "workspaceSymbol",
                    "goToImplementation",
                ],
                "description": "The LSP operation to perform.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file. Use ~ for home directory.",
            },
            "line": {
                "type": "integer",
                "description": "1-based line number for position-based operations like definition, references, hover, and implementation.",
            },
            "character": {
                "type": "integer",
                "description": "1-based character offset for position-based operations like definition, references, hover, and implementation.",
            },
            "query": {
                "type": "string",
                "description": "Required non-empty workspace symbol query. Used only for workspaceSymbol.",
            },
        },
        "required": ["operation", "file_path"],
    },
    source=_EXECUTOR_SOURCE,
    category="lsp",
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
            "path": {
                "type": "string",
                "description": "Directory to search in. Use ~ for home directory. Defaults to the executor home directory if omitted.",
            },
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
            "path": {
                "type": "string",
                "description": "Directory or single file to search in. Use ~ for home directory. Defaults to the executor home directory if omitted.",
            },
            "include": {
                "type": "string",
                "description": "Optional file pattern filter when path is a directory (e.g. '*.py', '*.{ts,tsx}').",
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
    description=(
        "Execute a shell command and return its output. Use for terminal-native "
        "operations such as git, build/test/package-manager commands, and atomic "
        "filesystem operations like mv, cp, rm, and mkdir. Prefer dedicated file "
        "tools for reading and editing file contents. Commands are parsed by the "
        "shell, so quote literal paths containing spaces, parentheses, globs, $, "
        "or other shell metacharacters."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute. Commands are shell-parsed, so quote literal paths or arguments that contain shell metacharacters.",
            },
            "description": {
                "type": "string",
                "description": "Brief description of what this command does",
            },
            "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
            "workdir": {
                "type": "string",
                "description": "Working directory for the command. Use ~ for home directory. Defaults to the executor home directory if omitted.",
            },
            "env": {
                "type": "object",
                "description": "Optional environment variables for the command. Values may be resolved transiently from credential refs.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Start the command in the background and return a shell_id for later polling.",
            },
        },
        "required": ["command"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=120,
)

BASH_OUTPUT_TOOL = ToolDefinition(
    name="bash_output",
    description="Read new output from a background bash session created with bash(run_in_background=true).",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell session id."},
            "cursor": {
                "type": "integer",
                "description": "Optional output cursor from the previous bash_output call. Defaults to 0.",
            },
        },
        "required": ["shell_id"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=True,
    timeout_seconds=30,
)

BASH_KILL_TOOL = ToolDefinition(
    name="bash_kill",
    description="Stop a background bash session created with bash(run_in_background=true).",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell session id."},
        },
        "required": ["shell_id"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=30,
)

# -- Web tools -----------------------------------------------------------------

WEB_FETCH_TOOL = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch content from a URL and return it as text or markdown. "
        "Supports configurable backends. Use 'direct' for simple page fetching "
        "(free) and 'tavily' for higher-quality extraction with content "
        "reranking. Omit the 'backend' parameter unless you need to override "
        "the configured system default."
    ),
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
            "backend": {
                "type": "string",
                "description": (
                    "Backend to use: 'direct' (free) or 'tavily' "
                    "(higher quality extraction). Overrides the configured system default."
                ),
            },
        },
        "required": ["url"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=60,
)

WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description=(
        "Search the web for information. Returns relevant results with titles, "
        "URLs, and content snippets. Backends: 'direct' (DuckDuckGo, free), "
        "'tavily' (AI-optimized, supports answer generation), "
        "'brave' (large index, freshness filters). "
        "The 'backend' parameter overrides the system default."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num_results": {
                "type": "integer",
                "description": "Number of results (default: 8, max varies by backend)",
            },
            "backend": {
                "type": "string",
                "description": (
                    "Backend: 'direct' (DuckDuckGo, free), 'tavily', 'brave'. "
                    "Overrides system default."
                ),
            },
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced", "fast", "ultra-fast"],
                "description": "Tavily: search depth (default: basic)",
            },
            "topic": {
                "type": "string",
                "enum": ["general", "news", "finance"],
                "description": "Tavily: topic category (default: general)",
            },
            "include_answer": {
                "type": "boolean",
                "description": "Tavily: generate LLM answer from results",
            },
            "time_range": {
                "type": "string",
                "description": (
                    "Recency filter. Tavily: 'day','week','month','year'. "
                    "Brave: 'pd','pw','pm','py' or 'YYYY-MM-DDtoYYYY-MM-DD'."
                ),
            },
            "country": {
                "type": "string",
                "description": "Country filter (Tavily: full name, Brave: 2-letter code)",
            },
        },
        "required": ["query"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=60,
)

WEB_CRAWL_TOOL = ToolDefinition(
    name="web_crawl",
    description=(
        "Crawl a website starting from a URL. Extracts content from pages "
        "with configurable depth and breadth. Requires Tavily backend."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Root URL to begin crawl"},
            "max_depth": {
                "type": "integer",
                "description": "How deep to crawl (1-5, default: 1)",
            },
            "max_breadth": {
                "type": "integer",
                "description": "Max links per level (1-500, default: 20)",
            },
            "limit": {
                "type": "integer",
                "description": "Total pages to process (default: 50)",
            },
            "instructions": {
                "type": "string",
                "description": "Natural language instructions for the crawler",
            },
            "extract_depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "Extraction depth (default: basic)",
            },
        },
        "required": ["url"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=120,
)

WEB_MAP_TOOL = ToolDefinition(
    name="web_map",
    description=(
        "Map a website's structure. Returns a list of URLs found starting "
        "from the base URL. Useful for discovering site structure before "
        "crawling. Requires Tavily backend."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Root URL to map"},
            "max_depth": {
                "type": "integer",
                "description": "Mapping depth (1-5, default: 1)",
            },
            "max_breadth": {
                "type": "integer",
                "description": "Links per level (1-500, default: 20)",
            },
            "limit": {
                "type": "integer",
                "description": "Total pages to map (default: 50)",
            },
            "instructions": {
                "type": "string",
                "description": "Natural language instructions for the mapper",
            },
        },
        "required": ["url"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=150,
)

WEB_RESEARCH_TOOL = ToolDefinition(
    name="web_research",
    description=(
        "Perform comprehensive research on a topic. Uses multiple searches "
        "and source analysis to produce a detailed report. "
        "Requires Tavily backend."
    ),
    parameters={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Research task description"},
            "model": {
                "type": "string",
                "enum": ["mini", "pro", "auto"],
                "description": (
                    "Research depth: 'mini' for narrow tasks, 'pro' for broad, "
                    "'auto' selects automatically (default: auto)"
                ),
            },
        },
        "required": ["input"],
    },
    source=_EXECUTOR_SOURCE,
    category="web",
    read_only=True,
    timeout_seconds=300,
)

# -- Public API ----------------------------------------------------------------

# Web tool definitions (WEB_FETCH_TOOL, WEB_SEARCH_TOOL, WEB_CRAWL_TOOL,
# WEB_MAP_TOOL, WEB_RESEARCH_TOOL) are generated dynamically based on
# available backends — see cognis.tools.executor.web.definitions.
# They are NOT included in ALL_EXECUTOR_TOOLS.  The handler map below
# still registers all web handlers so calls are routed correctly.

ALL_EXECUTOR_TOOLS: list[ToolDefinition] = [
    READ_TOOL,
    WRITE_TOOL,
    ARTIFACT_SAVE_TOOL,
    SKILL_ASSET_MATERIALIZE_TOOL,
    EDIT_TOOL,
    APPLY_PATCH_TOOL,
    MULTIEDIT_TOOL,
    LIST_DIRECTORY_TOOL,
    LSP_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    BASH_TOOL,
    BASH_OUTPUT_TOOL,
    BASH_KILL_TOOL,
    DOCUMENT_GENERATE_TOOL,
    ARTIFACT_PUBLISH_TOOL,
    *browser_tool_definitions(),
]

_HANDLER_MAP: dict[
    str,
    Any,
] = {
    "read": handle_read,
    "write": handle_write,
    "artifact_save": handle_artifact_save,
    "skill_asset_materialize": handle_skill_asset_materialize,
    "edit": handle_edit,
    "apply_patch": handle_apply_patch,
    "multiedit": handle_multiedit,
    "list_directory": handle_list_directory,
    "lsp": handle_lsp,
    "glob": handle_glob,
    "grep": handle_grep,
    "bash": handle_bash,
    "bash_output": handle_bash_output,
    "bash_kill": handle_bash_kill,
    "document_generate": handle_document_generate,
    "artifact_publish": handle_artifact_publish,
    "browser_open": handle_browser_open,
    "browser_snapshot": handle_browser_snapshot,
    "browser_list_sessions": handle_browser_list_sessions,
    "browser_list_profiles": handle_browser_list_profiles,
    "browser_query": handle_browser_query,
    "browser_eval": handle_browser_eval,
    "browser_get_console": handle_browser_get_console,
    "browser_get_focus": handle_browser_get_focus,
    "browser_get_network": handle_browser_get_network,
    "browser_get_text": handle_browser_get_text,
    "browser_click": handle_browser_click,
    "browser_fill": handle_browser_fill,
    "browser_focus": handle_browser_focus,
    "browser_type": handle_browser_type,
    "browser_submit_form": handle_browser_submit_form,
    "browser_press": handle_browser_press,
    "browser_wait_for": handle_browser_wait_for,
    "browser_screenshot": handle_browser_screenshot,
    "browser_close": handle_browser_close,
    "browser_save_auth_state": handle_browser_save_auth_state,
    "web_fetch": handle_web_fetch,
    "web_search": handle_web_search,
    "web_crawl": handle_web_crawl,
    "web_map": handle_web_map,
    "web_research": handle_web_research,
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
