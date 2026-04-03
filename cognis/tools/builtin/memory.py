"""Built-in memory tool definitions and handlers.

These tools call the Mnemory provider to give agents proactive memory
access. They are controller-side tools — handled by the tool router,
not dispatched to the executor.

The user_id and agent_id are injected from session context, not passed
by the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource

logger = get_logger(__name__)

_SOURCE = ToolSource(type="builtin")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

MEMORY_SEARCH_TOOL = ToolDefinition(
    name="memory_search",
    description=(
        "Search memories by semantic similarity with filtering and importance reranking. "
        "Results are ranked by relevance and importance. Memories with artifacts "
        "show has_artifacts: true — use memory_get_artifact to fetch details."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for (natural language)."},
            "memory_type": {
                "type": "string",
                "enum": ["preference", "fact", "episodic", "procedural", "context"],
                "description": "Filter by memory type.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by categories.",
            },
            "role": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": "Filter: 'user' or 'assistant'. Omit for all.",
            },
            "limit": {"type": "integer", "description": "Max results (default 10, max 50)."},
            "include_decayed": {"type": "boolean", "description": "Include expired memories."},
            "labels": {"type": "object", "description": "Filter by label key-value pairs."},
        },
        "required": ["query"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=30,
)

MEMORY_FIND_TOOL = ToolDefinition(
    name="memory_find",
    description=(
        "Find memories relevant to a complex question using AI-powered search. "
        "Generates multiple targeted searches covering different angles and "
        "associations, then reranks results by relevance. Temporal-aware. "
        "Slower than memory_search (2 extra LLM calls) but higher quality "
        "for complex, multi-faceted questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question in natural language."},
            "memory_type": {"type": "string", "description": "Filter by memory type."},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by categories.",
            },
            "role": {"type": "string", "description": "Filter: 'user' or 'assistant'."},
            "limit": {"type": "integer", "description": "Max results (default 10, max 100)."},
            "include_decayed": {"type": "boolean", "description": "Include expired memories."},
            "context": {
                "type": "string",
                "description": "Optional context hint for query generation.",
            },
            "labels": {"type": "object", "description": "Filter by label key-value pairs."},
        },
        "required": ["question"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=60,
)

MEMORY_ASK_TOOL = ToolDefinition(
    name="memory_ask",
    description=(
        "Ask a question and get a human-readable answer based on stored memories. "
        "Uses memory_find internally, then generates a natural language answer. "
        "Most expensive operation (3 LLM calls). Use when you need a synthesized "
        "answer rather than raw memory results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question in natural language."},
            "memory_type": {"type": "string", "description": "Filter by memory type."},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by categories.",
            },
            "role": {"type": "string", "description": "Filter: 'user' or 'assistant'."},
            "limit": {"type": "integer", "description": "Max supporting memories."},
            "include_decayed": {"type": "boolean", "description": "Include expired memories."},
            "context": {"type": "string", "description": "Optional context hint."},
            "include_memories": {
                "type": "boolean",
                "description": "Include supporting memories in response.",
            },
            "labels": {"type": "object", "description": "Filter by label key-value pairs."},
        },
        "required": ["question"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=60,
)

MEMORY_ADD_TOOL = ToolDefinition(
    name="memory_add",
    description=(
        "Store a memory about the user or agent. "
        "Call this whenever the user shares personal information, preferences, "
        "facts, decisions, project context, or anything worth remembering. "
        "Content must be concise (max 1000 chars). For detailed content, store "
        "a summary here and attach the full content with memory_save_artifact. "
        "All metadata fields are OPTIONAL — if omitted, the server auto-classifies "
        "them using an LLM."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The memory content to store (max 1000 chars).",
            },
            "memory_type": {
                "type": "string",
                "enum": ["preference", "fact", "episodic", "procedural", "context"],
                "description": "Memory type.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Categories.",
            },
            "importance": {
                "type": "string",
                "enum": ["low", "normal", "high", "critical"],
                "description": "Importance level.",
            },
            "pinned": {
                "type": "boolean",
                "description": "Pin to load at every conversation start.",
            },
            "infer": {
                "type": "boolean",
                "description": "Extract facts and dedup (default true). False = store verbatim.",
            },
            "role": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": "'user' (default) or 'assistant'.",
            },
            "ttl_days": {"type": "integer", "description": "Time-to-live in days."},
            "labels": {"type": "object", "description": "Key-value metadata for filtering."},
        },
        "required": ["content"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=30,
)

MEMORY_ADD_BATCH_TOOL = ToolDefinition(
    name="memory_add_batch",
    description=(
        "Store multiple memories in a single call (batch operation). "
        "Each memory is processed independently — failures on individual "
        "items do not block the rest."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "categories": {"type": "array", "items": {"type": "string"}},
                        "importance": {"type": "string"},
                        "role": {"type": "string"},
                        "ttl_days": {"type": "integer"},
                        "labels": {"type": "object"},
                    },
                    "required": ["content"],
                },
                "description": "List of memory objects to store.",
            },
        },
        "required": ["memories"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=60,
)

MEMORY_UPDATE_TOOL = ToolDefinition(
    name="memory_update",
    description="Update an existing memory's content or metadata.",
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the memory to update."},
            "content": {"type": "string", "description": "New content text."},
            "memory_type": {"type": "string", "description": "New type."},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New categories.",
            },
            "importance": {"type": "string", "description": "New importance."},
            "pinned": {"type": "boolean", "description": "New pinned state."},
            "ttl_days": {"type": "integer", "description": "New TTL in days."},
            "labels": {"type": "object", "description": "New labels (merged with existing)."},
        },
        "required": ["memory_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=30,
)

MEMORY_DELETE_TOOL = ToolDefinition(
    name="memory_delete",
    description="Delete a specific memory and all its artifacts.",
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the memory to delete."},
        },
        "required": ["memory_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=15,
)

MEMORY_LIST_TOOL = ToolDefinition(
    name="memory_list",
    description="List all stored memories for a user, optionally filtered.",
    parameters={
        "type": "object",
        "properties": {
            "memory_type": {"type": "string", "description": "Filter by type."},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by categories.",
            },
            "role": {"type": "string", "description": "Filter: 'user' or 'assistant'."},
            "limit": {"type": "integer", "description": "Max results (default 50, max 100)."},
            "include_decayed": {"type": "boolean", "description": "Include expired memories."},
            "labels": {"type": "object", "description": "Filter by label key-value pairs."},
        },
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=30,
)

MEMORY_CATEGORIES_TOOL = ToolDefinition(
    name="memory_categories",
    description=(
        "List all available memory categories with descriptions and counts. "
        "Shows predefined categories and any dynamic project:<name> categories. "
        "Categories are PREDEFINED — do not invent new ones."
    ),
    parameters={"type": "object", "properties": {}},
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=15,
)

MEMORY_RECENT_TOOL = ToolDefinition(
    name="memory_recent",
    description="Get recent memories from the last N days. Returns memories of all types ordered by most recent first.",
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many days back to look (default 7)."},
            "scope": {
                "type": "string",
                "enum": ["all", "user", "agent"],
                "description": "Scope filter.",
            },
            "limit": {"type": "integer", "description": "Max results (default 25, max 100)."},
            "include_decayed": {"type": "boolean", "description": "Include expired memories."},
        },
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=30,
)

MEMORY_SAVE_ARTIFACT_TOOL = ToolDefinition(
    name="memory_save_artifact",
    description=(
        "Attach an artifact to a memory (slow memory tier). "
        "Use for detailed content too long for fast memory — research reports, "
        "analysis, logs, notes, code, data, images, PDFs (max 10MB)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the parent memory."},
            "content": {"type": "string", "description": "Text content or base64-encoded binary."},
            "filename": {"type": "string", "description": "Name for the artifact."},
            "content_type": {
                "type": "string",
                "description": "MIME type (default: text/markdown).",
            },
        },
        "required": ["memory_id", "content"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=30,
)

MEMORY_GET_ARTIFACT_TOOL = ToolDefinition(
    name="memory_get_artifact",
    description=(
        "Retrieve artifact content. Text artifacts support pagination. "
        "Binary artifacts >1MB require memory_get_artifact_url instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the parent memory."},
            "artifact_id": {"type": "string", "description": "ID of the artifact."},
            "offset": {"type": "integer", "description": "Character offset (default 0)."},
            "limit": {"type": "integer", "description": "Max characters (default 5000)."},
        },
        "required": ["memory_id", "artifact_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=30,
)

MEMORY_LIST_ARTIFACTS_TOOL = ToolDefinition(
    name="memory_list_artifacts",
    description="List all artifacts attached to a memory.",
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the parent memory."},
        },
        "required": ["memory_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=15,
)

MEMORY_DELETE_ARTIFACT_TOOL = ToolDefinition(
    name="memory_delete_artifact",
    description="Delete an artifact from a memory.",
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the parent memory."},
            "artifact_id": {"type": "string", "description": "ID of the artifact to delete."},
        },
        "required": ["memory_id", "artifact_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    timeout_seconds=15,
)

MEMORY_GET_ARTIFACT_URL_TOOL = ToolDefinition(
    name="memory_get_artifact_url",
    description=(
        "Generate a short-lived signed URL for direct artifact download. "
        "Use instead of memory_get_artifact for binary artifacts (images, PDFs), "
        "large artifacts (>1MB), or when a direct browser URL is needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the parent memory."},
            "artifact_id": {"type": "string", "description": "ID of the artifact."},
            "ttl": {
                "type": "integer",
                "description": "URL lifetime in seconds (default ~3600, max ~86400).",
                "minimum": 60,
            },
        },
        "required": ["memory_id", "artifact_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=True,
    timeout_seconds=15,
)

ALL_MEMORY_TOOLS: list[ToolDefinition] = [
    MEMORY_SEARCH_TOOL,
    MEMORY_FIND_TOOL,
    MEMORY_ASK_TOOL,
    MEMORY_ADD_TOOL,
    MEMORY_ADD_BATCH_TOOL,
    MEMORY_UPDATE_TOOL,
    MEMORY_DELETE_TOOL,
    MEMORY_LIST_TOOL,
    MEMORY_CATEGORIES_TOOL,
    MEMORY_RECENT_TOOL,
    MEMORY_SAVE_ARTIFACT_TOOL,
    MEMORY_GET_ARTIFACT_TOOL,
    MEMORY_GET_ARTIFACT_URL_TOOL,
    MEMORY_LIST_ARTIFACTS_TOOL,
    MEMORY_DELETE_ARTIFACT_TOOL,
]

MEMORY_TOOL_NAMES: set[str] = {t.name for t in ALL_MEMORY_TOOLS}


def memory_tools() -> list[ToolDefinition]:
    """Return built-in memory tool definitions."""
    return list(ALL_MEMORY_TOOLS)


def is_memory_tool(name: str) -> bool:
    """Check if a tool name is a memory tool."""
    return name in MEMORY_TOOL_NAMES


# ---------------------------------------------------------------------------
# Handlers — called by the tool router with the Mnemory provider
# ---------------------------------------------------------------------------


def _json_output(data: Any) -> ToolResult:
    """Format a result as JSON output."""
    return ToolResult(output=json.dumps(data, indent=2, default=str))


async def handle_memory_tool(
    tool_name: str,
    arguments: dict[str, Any],
    memory_provider: Any,
    agent_id: str | None = None,
    user_email: str | None = None,
) -> ToolResult:
    """Dispatch a memory tool call to the Mnemory provider.

    This is the single entry point for all memory tool execution.
    The tool router calls this instead of dispatching to the executor.
    """
    try:
        return await _dispatch(tool_name, arguments, memory_provider, agent_id, user_email)
    except Exception as exc:
        logger.warning(
            "memory tool failed",
            extra={"extra_data": {"tool": tool_name, "error": str(exc)[:200]}},
        )
        return ToolResult(output=f"Memory operation failed: {exc}", is_error=True)


async def _dispatch(
    tool_name: str,
    args: dict[str, Any],
    mem: Any,
    agent_id: str | None,
    user_email: str | None,
) -> ToolResult:
    """Route to the appropriate Mnemory provider method."""
    client = mem.client
    headers = mem._headers(agent_id, user_email)

    if tool_name == "memory_search":
        response = await client.post(
            "/api/memories/search",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_find":
        response = await client.post(
            "/api/memories/find",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_ask":
        response = await client.post(
            "/api/memories/ask",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_add":
        response = await client.post(
            "/api/memories",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_add_batch":
        response = await client.post(
            "/api/memories/batch",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_update":
        memory_id = args.pop("memory_id", "")
        response = await client.put(
            f"/api/memories/{memory_id}",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_delete":
        memory_id = args.get("memory_id", "")
        response = await client.delete(
            f"/api/memories/{memory_id}",
            headers=headers,
        )
        response.raise_for_status()
        return ToolResult(output=f"Memory {memory_id} deleted.")

    if tool_name == "memory_list":
        params: dict[str, Any] = {}
        for key in ("memory_type", "role", "limit", "include_decayed"):
            if args.get(key) is not None:
                params[key] = args[key]
        if args.get("categories"):
            params["categories"] = ",".join(args["categories"])
        response = await client.get(
            "/api/memories",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_categories":
        response = await client.get("/api/categories", headers=headers)
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_recent":
        params = {}
        for key in ("days", "scope", "limit", "include_decayed"):
            if args.get(key) is not None:
                params[key] = args[key]
        response = await client.get(
            "/api/memories/recent",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_save_artifact":
        memory_id = args.pop("memory_id", "")
        response = await client.post(
            f"/api/memories/{memory_id}/artifacts",
            json=_filter_none(args),
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_get_artifact":
        memory_id = args.get("memory_id", "")
        artifact_id = args.get("artifact_id", "")
        params = {}
        if args.get("offset") is not None:
            params["offset"] = args["offset"]
        if args.get("limit") is not None:
            params["limit"] = args["limit"]
        response = await client.get(
            f"/api/memories/{memory_id}/artifacts/{artifact_id}",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_list_artifacts":
        memory_id = args.get("memory_id", "")
        response = await client.get(
            f"/api/memories/{memory_id}/artifacts",
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_get_artifact_url":
        memory_id = args.get("memory_id", "")
        artifact_id = args.get("artifact_id", "")
        payload: dict[str, Any] = {}
        if args.get("ttl") is not None:
            payload["ttl"] = args["ttl"]
        response = await client.post(
            f"/api/memories/{memory_id}/artifacts/{artifact_id}/download-token",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return _json_output(response.json())

    if tool_name == "memory_delete_artifact":
        memory_id = args.get("memory_id", "")
        artifact_id = args.get("artifact_id", "")
        response = await client.delete(
            f"/api/memories/{memory_id}/artifacts/{artifact_id}",
            headers=headers,
        )
        response.raise_for_status()
        return ToolResult(output=f"Artifact {artifact_id} deleted from memory {memory_id}.")

    return ToolResult(output=f"Unknown memory tool: {tool_name}", is_error=True)


def _filter_none(d: dict[str, Any]) -> dict[str, Any]:
    """Remove None values from a dict for clean API payloads."""
    return {k: v for k, v in d.items() if v is not None}
