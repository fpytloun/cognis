"""Executor-native tool definitions and handler registry."""

from __future__ import annotations

from typing import Any

from cognis.executor.component_registry import load_component
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource
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
from cognis.tools.executor.search import handle_glob, handle_grep
from cognis.tools.executor.shell import handle_bash, handle_bash_kill, handle_bash_output

_EXECUTOR_SOURCE = ToolSource(type="executor")

# -- Filesystem tools ----------------------------------------------------------

READ_TOOL = ToolDefinition(
    name="read",
    description=(
        "Preferred tool for reading files and directories from the filesystem, especially "
        "source files, configs, logs, and structured text. Use this instead of bash commands "
        "such as cat, head, tail, sed -n, or ls for file/code inspection. Text files return "
        "line-numbered content; offset and limit apply to text files only. Supported binary "
        "files are routed through attachment analysis."
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
    content_trust="untrusted",
    timeout_seconds=30,
)

WRITE_TOOL = ToolDefinition(
    name="write",
    description=(
        "Write content to a file, creating it and parent directories if needed. "
        "Overwrites the whole file; for existing files you must use read first so "
        "the executor can verify freshness before writing. Prefer edit/apply_patch "
        "for focused source changes."
    ),
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
    timeout_seconds=60,
)

ARTIFACT_SAVE_TOOL = ToolDefinition(
    name="artifact_save",
    description=(
        "Save an artifact-compatible content ref, including saved artifact IDs and authorized "
        "task or managed-descendant deliverable IDs (dlv_*), to a local executor file path. "
        "Use this when you need "
        "a saved image, PDF, deliverable, or other artifact-like source as a real "
        "filesystem file for subsequent tools."
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
                "description": "Saved artifact ID or task deliverable ID (dlv_*) to save to the executor filesystem.",
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
            "asset_id": {
                "type": "string",
                "description": "Asset ID from skill_load asset_manifest.",
            },
            "filename": {
                "type": "string",
                "description": "Optional asset filename to disambiguate when asset_id is not known.",
            },
            "target_path": {
                "type": "string",
                "description": (
                    "Optional path under the managed skill asset cache to write. "
                    "Defaults to a stable managed cache path."
                ),
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
    description=(
        "Edit a file by replacing an exact text match with new text. You must call "
        "read first and copy text from the file content, never including the read "
        "line-number prefix such as '12:'. Preserve the exact whitespace after that "
        "prefix. old_string must be unique unless replace_all=true; prefer larger "
        "disambiguating blocks when nearby text repeats."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file. Use ~ for home directory.",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Exact text to find. Copy from read output after the line-number "
                    "prefix; do not include the leading 'N:'."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text; must differ from old_string.",
            },
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
    timeout_seconds=60,
)

APPLY_PATCH_TOOL = ToolDefinition(
    name="apply_patch",
    description=(
        "Apply a strict patch to one or more text files. Supports the apply_patch "
        "envelope grammar: *** Begin Patch, then one or more *** Add File, "
        "*** Delete File, or *** Update File sections, ending with *** End Patch. "
        "Unsupported operations such as chmod, binary patches, and arbitrary shell "
        "commands are rejected."
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
    timeout_seconds=60,
)

MULTIEDIT_TOOL = ToolDefinition(
    name="multiedit",
    description=(
        "Apply multiple sequential text replacements to a single file. You must call "
        "read first and copy old_string from the file content without read-tool "
        "line-number prefixes such as '12:'. Each edit is applied in order."
    ),
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
                        "old_string": {
                            "type": "string",
                            "description": (
                                "Exact text to find. Copy from read output after the "
                                "line-number prefix; do not include the leading 'N:'."
                            ),
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text; must differ from old_string.",
                        },
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
    timeout_seconds=60,
)

LIST_DIRECTORY_TOOL = ToolDefinition(
    name="list_directory",
    description=(
        "Preferred tool for listing files and subdirectories in a directory. Use this instead "
        "of bash ls when available."
    ),
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
    description=(
        "Preferred tool for discovering files by name/path patterns. Use this instead of bash "
        "commands such as find or ls when available. Returns absolute file paths sorted by "
        "modification time."
    ),
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
    description=(
        "Preferred tool for searching file contents using regex. Use this instead of bash "
        "commands such as grep or rg when available. Returns absolute matching file paths "
        "and line numbers."
    ),
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
                "description": "Optional file pattern filter when path is a directory. Use brace syntax or comma-separated globs for multiple patterns (e.g. '*.py', '*.{ts,tsx}', '*.ts,*.svelte').",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive regex search (default false).",
            },
            "context_lines": {
                "type": "integer",
                "description": "Number of context lines before and after each match in content mode (default 0).",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode: content (default), files_with_matches, or count.",
            },
            "max_per_file": {
                "type": "integer",
                "description": "Maximum content-mode matches to show per file. Defaults to 10 for directories and no small per-file cap for single-file searches.",
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
        "Execute a shell command and return its output. Use for shell-native operations: git, "
        "build/test/package-manager commands, process management, permissions, background "
        "processes, and atomic filesystem operations such as mv, cp, rm, mkdir, and chmod. Do "
        "not use for routine file/code inspection when dedicated tools are visible: use read "
        "instead of cat/head/tail/sed -n, grep instead of grep/rg, glob instead of find, and "
        "list_directory instead of ls. Commands are parsed by the shell, so quote literal paths "
        "containing spaces, parentheses, globs, $, or other shell metacharacters. For background "
        "commands, provide a concise description so completion follow-ups and per-turn reminders "
        "identify the job; running jobs are summarized in prompt reminders and completion triggers "
        "a follow-up turn. Each call runs in a fresh shell: cd/export do not persist; use workdir "
        "and env parameters."
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
                "description": (
                    "Brief description of what this command does and why shell execution is "
                    "needed instead of a dedicated tool. For background commands this is used "
                    "as the human-readable job identifier in reminders and completion follow-ups."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds. Foreground commands default to 120000 ms and may run up to 3600000 ms; use run_in_background=true for longer operations. For background commands, this only controls the initial preview wait.",
            },
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
                "description": "Start the command in the background and return a shell_id for later inspection. Background commands continue until completion, bash_kill, or executor cleanup. While running, brief status is injected on later turns; when the command completes, the conversation receives a follow-up event.",
            },
        },
        "required": ["command"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=False,
    non_bypassable=True,
    content_trust="untrusted",
    timeout_seconds=3605,
)

BASH_OUTPUT_TOOL = ToolDefinition(
    name="bash_output",
    description="Read new output from a background bash session created with bash(run_in_background=true). Use the shell_id from the start result, reminder, or completion follow-up. If the job is on a non-active executor, include target_executor when available.",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell session id."},
            "cursor": {
                "type": "integer",
                "description": "Optional output cursor from the previous bash_output call. Defaults to 0.",
            },
            "target_executor": {
                "type": "string",
                "description": "Optional executor id that owns the background shell session.",
            },
            "filter_regex": {
                "type": "string",
                "description": "Optional case-insensitive regex used to return only matching output lines.",
            },
        },
        "required": ["shell_id"],
    },
    source=_EXECUTOR_SOURCE,
    category="shell",
    read_only=True,
    content_trust="untrusted",
    timeout_seconds=30,
)

BASH_KILL_TOOL = ToolDefinition(
    name="bash_kill",
    description="Stop a background bash session created with bash(run_in_background=true). If the job is on a non-active executor, include target_executor when available.",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell session id."},
            "target_executor": {
                "type": "string",
                "description": "Optional executor id that owns the background shell session.",
            },
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
            "timeout": {
                "type": "integer",
                "description": (
                    "Timeout in seconds (default: 60, max 120). "
                    "Use 90 or more for sites that can require browser fallback."
                ),
            },
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
    timeout_seconds=130,
)

WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description=(
        "Search the web for information. Returns relevant results with titles, "
        "URLs, and content snippets. Backends: 'direct' (DDGS metasearch, free), "
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
                    "Backend: 'direct' (DDGS metasearch, free), 'tavily', 'brave'. "
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


def _component_definitions(name: str) -> list[ToolDefinition]:
    """Load definitions for an optional component when its extra is present."""
    modules = load_component(name)
    if not modules:
        return []
    if name == "browser":
        return list(modules[0].browser_tool_definitions())
    if name == "documents":
        return [modules[0].DOCUMENT_GENERATE_TOOL, modules[0].ARTIFACT_PUBLISH_TOOL]
    if name == "lsp":
        return [LSP_TOOL]
    return []


_CORE_EXECUTOR_TOOLS: list[ToolDefinition] = [
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
]
# Kept for compatibility with callers that inspect the static core inventory.
ALL_EXECUTOR_TOOLS = list(_CORE_EXECUTOR_TOOLS)
_office_modules = load_component("officecli")
OFFICE_EXECUTOR_TOOLS: list[ToolDefinition] = (
    _office_modules[1].office_tool_definitions() if _office_modules else []
)


def executor_tool_definitions() -> list[ToolDefinition]:
    """Return core definitions plus definitions from available components."""
    tools = list(_CORE_EXECUTOR_TOOLS)
    for component in ("browser", "documents"):
        tools.extend(_component_definitions(component))
    return tools


def office_executor_tool_definitions(runtime_metadata: dict[str, Any]) -> list[ToolDefinition]:
    """Return Office tools only for a certified, available OfficeCLI runtime."""
    officecli = runtime_metadata.get("officecli") or {}
    if not isinstance(officecli, dict) or not officecli.get("available"):
        return []
    modules = load_component("officecli")
    if not modules:
        return []
    definitions = modules[1].office_tool_definitions()
    allowed = modules[2].certified_tool_names(str(officecli.get("version") or ""))
    return [tool for tool in definitions if tool.name in allowed]


def executor_tool_handlers() -> dict[str, Any]:
    """Return core handlers plus handlers from available components."""
    handlers: dict[str, Any] = {
        "read": handle_read,
        "write": handle_write,
        "artifact_save": handle_artifact_save,
        "skill_asset_materialize": handle_skill_asset_materialize,
        "edit": handle_edit,
        "apply_patch": handle_apply_patch,
        "multiedit": handle_multiedit,
        "list_directory": handle_list_directory,
        "glob": handle_glob,
        "grep": handle_grep,
        "bash": handle_bash,
        "bash_output": handle_bash_output,
        "bash_kill": handle_bash_kill,
    }
    component_handlers = {
        "browser": (
            "browser_open",
            "browser_snapshot",
            "browser_list_sessions",
            "browser_inspect_session",
            "browser_list_profiles",
            "browser_claim_profile",
            "browser_query",
            "browser_eval",
            "browser_get_console",
            "browser_get_focus",
            "browser_get_network",
            "browser_get_text",
            "browser_click",
            "browser_fill",
            "browser_focus",
            "browser_type",
            "browser_submit_form",
            "browser_select",
            "browser_upload",
            "browser_download_wait",
            "browser_scroll",
            "browser_hover",
            "browser_drag_drop",
            "browser_press",
            "browser_wait_for",
            "browser_screenshot",
            "browser_close",
            "browser_save_auth_state",
        ),
        "documents": ("document_generate", "artifact_publish"),
        "lsp": ("lsp",),
        "officecli": (
            "office_read",
            "office_get",
            "office_query",
            "office_validate",
            "office_render",
            "office_create",
            "office_patch",
        ),
        "web": ("web_fetch", "web_search", "web_crawl", "web_map", "web_research"),
    }
    for component, names in component_handlers.items():
        modules = load_component(component)
        if not modules:
            continue
        handler_module = (
            modules[-1]
            if component in {"lsp", "officecli"}
            else modules[1]
            if component in {"browser", "web"}
            else modules[0]
        )
        for name in names:
            handler = getattr(handler_module, f"handle_{name}", None)
            if handler is not None:
                handlers[name] = handler
    return handlers
