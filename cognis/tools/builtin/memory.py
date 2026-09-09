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

from cognis.core.memory_aliases import (
    MEMORY_ALIASES_METADATA,
    MemoryAliasBinding,
    MemoryAliasError,
    MemoryAliasState,
    memory_revision_identity,
)
from cognis.logging import get_logger
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolCapability, ToolResult, ToolSource
from cognis.providers.memory.mnemory import MnemoryHTTPStatusError

logger = get_logger(__name__)

_SOURCE = ToolSource(type="builtin")
_MEMORY_REFERENCE_FIELDS = {
    "lineage_id",
    "memory_id",
    "parent_memory_id",
    "source_memory_id",
    "target_memory_id",
    "superseded_by",
    "supersedes",
}
_MEMORY_REFERENCE_LIST_FIELDS = {
    "derived_from",
    "memory_ids",
    "parent_memory_ids",
    "related_memory_ids",
    "source_memory_ids",
    "target_memory_ids",
    "superseded_by_ids",
    "supersedes_ids",
}

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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the memory to update.",
            },
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
    description=(
        "Delete, remove, or forget a specific memory and all its artifacts. "
        "Use only when the user explicitly asks to remove stored memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the memory to delete.",
            },
        },
        "required": ["memory_id"],
    },
    source=_SOURCE,
    category="memory",
    read_only=False,
    capabilities=[ToolCapability.WRITE, ToolCapability.DESTRUCTIVE],
    non_bypassable=True,
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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the parent memory.",
            },
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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the parent memory.",
            },
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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the parent memory.",
            },
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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the parent memory.",
            },
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
            "memory_id": {
                "type": "string",
                "description": "Current-session alias or canonical ID of the parent memory.",
            },
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


def _json_output(data: Any, alias_delta: dict[str, Any] | None = None) -> ToolResult:
    """Format a result as JSON output."""
    metadata = {MEMORY_ALIASES_METADATA: alias_delta} if alias_delta is not None else None
    return ToolResult(output=json.dumps(data, indent=2, default=str), metadata=metadata)


def _project_memory_records(
    data: Any, aliases: MemoryAliasState | None
) -> tuple[Any, dict[str, Any] | None]:
    if aliases is None:
        return data, None
    records: list[dict[str, Any]] = []
    targets: list[tuple[dict[str, Any], str]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and isinstance(value.get("memory"), str):
                records.append(value)
                targets.append((value, "id"))
            elif isinstance(value.get("_cognis_alias_record"), dict):
                records.append(dict(value["_cognis_alias_record"]))
                targets.append((value, "memory_id"))
            for key, child in value.items():
                if key != "_cognis_alias_record":
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    projected, delta = aliases.plan_records(records)
    replacements = {
        (id(source), key): target["id"]
        for (source, key), target in zip(targets, projected, strict=True)
    }
    canonical_aliases = {
        str(record.get("id")): str(target.get("id"))
        for record, target in zip(records, projected, strict=True)
        if isinstance(record.get("id"), str) and isinstance(target.get("id"), str)
    }

    def project_reference(memory_id: str) -> str | None:
        return canonical_aliases.get(memory_id) or aliases.active_alias(memory_id)

    def replace(value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if key == "_cognis_alias_record":
                    continue
                if key in _MEMORY_REFERENCE_FIELDS and isinstance(child, str):
                    reference = replacements.get((id(value), key)) or project_reference(child)
                    if reference is not None:
                        result[key] = reference
                    continue
                if key in _MEMORY_REFERENCE_LIST_FIELDS and isinstance(child, list):
                    result[key] = [
                        alias
                        for item in child
                        if isinstance(item, str) and (alias := project_reference(item)) is not None
                    ]
                    continue
                result[key] = replace(child)
            for key in ("id", "memory_id"):
                replacement = replacements.get((id(value), key))
                if replacement is not None:
                    result[key] = replacement
            return result
        if isinstance(value, list):
            return [replace(child) for child in value]
        return value

    return replace(data), delta


def _alias_json_output(data: Any, aliases: MemoryAliasState | None) -> ToolResult:
    projected, delta = _project_memory_records(data, aliases)
    return _json_output(projected, delta)


def _resolve_memory_id(args: dict[str, Any], aliases: MemoryAliasState | None) -> str:
    memory_id = str(args.get("memory_id", "")).strip()
    if not memory_id:
        raise ValueError("memory_id is required.")
    return aliases.resolve(memory_id) if aliases is not None else memory_id


async def _resolve_mutation_target(
    args: dict[str, Any],
    aliases: MemoryAliasState | None,
    mem: Any,
    *,
    agent_id: str | None,
    user_email: str | None,
) -> tuple[str, MemoryAliasBinding | None]:
    """Resolve and verify an alias against the current Mnemory revision."""

    value = str(args.get("memory_id", "")).strip()
    memory_id = _resolve_memory_id(args, aliases)
    binding = aliases.resolve_binding(value) if aliases is not None else None
    if binding is None:
        return memory_id, None
    fetched = await mem.get_memories_by_ids_tool(
        [memory_id], agent_id=agent_id, user_email=user_email
    )
    records = fetched.get("results") if isinstance(fetched, dict) else None
    current = records[0] if isinstance(records, list) and records else None
    if (
        not isinstance(current, dict)
        or str(current.get("id") or "") != memory_id
        or memory_revision_identity(current) != binding.revision
    ):
        raise MemoryAliasError(value, "stale")
    if binding.expected_revision is None:
        raise MemoryAliasError(value, "precondition_unavailable")
    return memory_id, binding


def _revision_precondition(binding: MemoryAliasBinding | None) -> dict[str, str]:
    if binding is None or binding.expected_revision is None:
        return {}
    return {"expected_revision": binding.expected_revision}


def _remove_canonical_memory_id(value: Any, memory_id: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_canonical_memory_id(child, memory_id)
            for key, child in value.items()
            if key != "memory_id"
        }
    if isinstance(value, list):
        return [_remove_canonical_memory_id(child, memory_id) for child in value]
    if value == memory_id:
        return None
    return value


async def _canonical_records(
    mem: Any,
    data: Any,
    *,
    agent_id: str | None,
    user_email: str | None,
) -> tuple[Any, bool]:
    ids: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            memory_id = value.get("memory_id")
            if memory_id is None and isinstance(value.get("memory"), str):
                memory_id = value.get("id")
            if isinstance(memory_id, str):
                ids.append(memory_id)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    if not ids or not hasattr(mem, "get_memories_by_ids_tool"):
        return data, not ids
    try:
        fetched = await mem.get_memories_by_ids_tool(
            list(dict.fromkeys(ids)),
            agent_id=agent_id,
            user_email=user_email,
        )
    except Exception:
        logger.warning("memory creation revision refresh failed", exc_info=True)
        return data, False
    records = fetched.get("results") if isinstance(fetched, dict) else None
    if not isinstance(records, list):
        return data, False
    by_id = {
        str(record["id"]): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    if set(ids) - by_id.keys():
        return data, False

    def enrich(value: Any) -> Any:
        if isinstance(value, dict):
            memory_id = value.get("id") or value.get("memory_id")
            if isinstance(memory_id, str) and memory_id in by_id:
                if "memory_id" in value and "id" not in value:
                    return {
                        **{key: enrich(child) for key, child in value.items()},
                        "_cognis_alias_record": dict(by_id[memory_id]),
                    }
                return dict(by_id[memory_id])
            return {key: enrich(child) for key, child in value.items()}
        if isinstance(value, list):
            return [enrich(child) for child in value]
        return value

    return enrich(data), True


def _remove_canonical_memory_ids(value: Any, memory_ids: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_canonical_memory_ids(child, memory_ids)
            for key, child in value.items()
            if not (key in {"id", "memory_id"} and isinstance(child, str) and child in memory_ids)
        }
    if isinstance(value, list):
        return [_remove_canonical_memory_ids(child, memory_ids) for child in value]
    return None if isinstance(value, str) and value in memory_ids else value


def _memory_ids_in_result(value: Any) -> set[str]:
    memory_ids: set[str] = set()
    if isinstance(value, dict):
        record_id = value.get("id")
        if isinstance(record_id, str) and isinstance(value.get("memory"), str):
            memory_ids.add(record_id)
        for key, child in value.items():
            if key == "memory_id" and isinstance(child, str):
                memory_ids.add(child)
            elif key != "id":
                memory_ids.update(_memory_ids_in_result(child))
    elif isinstance(value, list):
        for child in value:
            memory_ids.update(_memory_ids_in_result(child))
    return memory_ids


async def _refresh_memory_after_mutation(
    mem: Any,
    memory_id: str,
    *,
    agent_id: str | None,
    user_email: str | None,
) -> dict[str, Any] | None:
    """Fetch the committed revision without obscuring a completed mutation."""

    try:
        fetched = await mem.get_memories_by_ids_tool(
            [memory_id], agent_id=agent_id, user_email=user_email
        )
    except Exception:
        logger.warning(
            "memory mutation revision refresh failed",
            extra={"extra_data": {"memory_id": memory_id}},
            exc_info=True,
        )
        return None
    records = fetched.get("results") if isinstance(fetched, dict) else None
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return None
    return dict(fetched)


async def handle_memory_tool(
    tool_name: str,
    arguments: dict[str, Any],
    memory_provider: Any,
    agent_id: str | None = None,
    user_email: str | None = None,
    aliases: MemoryAliasState | None = None,
) -> ToolResult:
    """Dispatch a memory tool call to the Mnemory provider.

    This is the single entry point for all memory tool execution.
    The tool router calls this instead of dispatching to the executor.
    """
    try:
        return await _dispatch(tool_name, arguments, memory_provider, agent_id, user_email, aliases)
    except MemoryAliasError as exc:
        return ToolResult(
            output=json.dumps(
                {
                    "error": {
                        "code": f"memory_alias_{exc.reason}",
                        "detail": str(exc),
                        "retry": {"automatic": False, "action": "search_memory_again"},
                    }
                }
            ),
            is_error=True,
        )
    except MnemoryHTTPStatusError as exc:
        return _mnemory_http_error_result(exc)
    except Exception as exc:
        logger.warning(
            "memory tool failed",
            extra={"extra_data": {"tool": tool_name, "error": str(exc)[:200]}},
        )
        return ToolResult(output=f"Memory operation failed: {exc}", is_error=True)


def _mnemory_http_error_result(error: MnemoryHTTPStatusError) -> ToolResult:
    """Return Mnemory client errors in a form an agent can correct and retry."""

    is_validation_error = error.status_code == 422
    payload: dict[str, Any] = {
        "error": {
            "code": "memory_validation_error" if is_validation_error else "memory_api_error",
            "status_code": error.status_code,
            "detail": error.detail,
            "retry": {
                "automatic": False,
                "action": (
                    "correct_arguments_then_retry"
                    if is_validation_error
                    else "retry_after_resolving_error"
                ),
                "guidance": (
                    "Correct the invalid argument values described in detail, then retry the "
                    "same memory tool call."
                    if is_validation_error
                    else "Resolve the reported Mnemory API error before retrying."
                ),
            },
        }
    }
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False, default=str),
        is_error=True,
    )


async def _dispatch(
    tool_name: str,
    args: dict[str, Any],
    mem: Any,
    agent_id: str | None,
    user_email: str | None,
    aliases: MemoryAliasState | None,
) -> ToolResult:
    """Route to the appropriate Mnemory provider method."""
    if tool_name == "memory_search":
        return _alias_json_output(
            await mem.search_memories_tool(
                _filter_none(args),
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_find":
        return _alias_json_output(
            await mem.find_memories_tool(
                _filter_none(args),
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_ask":
        return _alias_json_output(
            await mem.ask_memories_tool(
                _filter_none(args),
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_add":
        data = await mem.add_memory_tool(
            _filter_none(args),
            agent_id=agent_id,
            user_email=user_email,
        )
        canonical, complete = await _canonical_records(
            mem, data, agent_id=agent_id, user_email=user_email
        )
        if not complete:
            ids = _memory_ids_in_result(data)
            return _json_output(_remove_canonical_memory_ids(data, ids)).model_copy(
                update={
                    "output": (
                        json.dumps(_remove_canonical_memory_ids(data, ids), indent=2, default=str)
                        + "\nMemory stored. Search again to get its current alias."
                    )
                }
            )
        return _alias_json_output(canonical, aliases)

    if tool_name == "memory_add_batch":
        data = await mem.add_memory_batch_tool(
            _filter_none(args),
            agent_id=agent_id,
            user_email=user_email,
        )
        canonical, complete = await _canonical_records(
            mem, data, agent_id=agent_id, user_email=user_email
        )
        if not complete:
            ids = _memory_ids_in_result(data)
            return ToolResult(
                output=(
                    json.dumps(_remove_canonical_memory_ids(data, ids), indent=2, default=str)
                    + "\nMemories stored. Search again to get their current aliases."
                )
            )
        return _alias_json_output(canonical, aliases)

    if tool_name == "memory_update":
        memory_id, binding = await _resolve_mutation_target(
            args, aliases, mem, agent_id=agent_id, user_email=user_email
        )
        payload = _filter_none({k: v for k, v in args.items() if k != "memory_id"})
        data = await mem.update_memory_tool(
            memory_id,
            payload,
            agent_id=agent_id,
            user_email=user_email,
            **_revision_precondition(binding),
        )
        if aliases is None:
            return _json_output(data)
        refreshed = await _refresh_memory_after_mutation(
            mem, memory_id, agent_id=agent_id, user_email=user_email
        )
        if refreshed is None:
            delta = aliases.invalidate_memory(memory_id)
            return ToolResult(
                output="Memory updated. Search again to get its current alias.",
                metadata={MEMORY_ALIASES_METADATA: delta} if delta is not None else None,
            )
        return _alias_json_output(refreshed, aliases)

    if tool_name == "memory_delete":
        memory_id, binding = await _resolve_mutation_target(
            args, aliases, mem, agent_id=agent_id, user_email=user_email
        )
        await mem.delete_memory_tool(
            memory_id,
            agent_id=agent_id,
            user_email=user_email,
            **_revision_precondition(binding),
        )
        delta = aliases.invalidate_memory(memory_id) if aliases is not None else None
        return ToolResult(
            output="Memory deleted.",
            metadata={MEMORY_ALIASES_METADATA: delta} if delta is not None else None,
        )

    if tool_name == "memory_list":
        params: dict[str, Any] = {}
        for key in ("memory_type", "role", "limit", "include_decayed"):
            if args.get(key) is not None:
                params[key] = args[key]
        if args.get("categories"):
            params["categories"] = ",".join(args["categories"])
        return _alias_json_output(
            await mem.list_memories_tool(
                params=params,
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_categories":
        return _json_output(
            await mem.memory_categories_tool(agent_id=agent_id, user_email=user_email)
        )

    if tool_name == "memory_recent":
        params = {}
        for key in ("days", "scope", "limit", "include_decayed"):
            if args.get(key) is not None:
                params[key] = args[key]
        return _json_output(
            await mem.recent_memories_tool(
                params=params,
                agent_id=agent_id,
                user_email=user_email,
            )
        )

    if tool_name == "memory_save_artifact":
        memory_id, binding = await _resolve_mutation_target(
            args, aliases, mem, agent_id=agent_id, user_email=user_email
        )
        payload = _filter_none({k: v for k, v in args.items() if k != "memory_id"})
        data = await mem.save_memory_artifact_tool(
            memory_id,
            payload,
            agent_id=agent_id,
            user_email=user_email,
            **_revision_precondition(binding),
        )
        if aliases is None:
            return _json_output(data)
        refreshed = await _refresh_memory_after_mutation(
            mem, memory_id, agent_id=agent_id, user_email=user_email
        )
        if refreshed is None:
            delta = aliases.invalidate_memory(memory_id)
            return ToolResult(
                output=(
                    json.dumps(
                        _remove_canonical_memory_id(data, memory_id),
                        indent=2,
                        default=str,
                    )
                    + "\nParent memory changed. Search again to get its current alias."
                ),
                metadata={MEMORY_ALIASES_METADATA: delta} if delta is not None else None,
            )
        projected, delta = _project_memory_records(refreshed, aliases)
        artifact_result = dict(data) if isinstance(data, dict) else {"result": data}
        if isinstance(projected, dict):
            records = projected.get("results")
            if isinstance(records, list) and records and isinstance(records[0], dict):
                artifact_result["memory_id"] = records[0].get("id")
        return _json_output(artifact_result, delta)

    if tool_name == "memory_get_artifact":
        memory_id = _resolve_memory_id(args, aliases)
        artifact_id = str(args.get("artifact_id", "")).strip()
        params = {}
        if args.get("offset") is not None:
            params["offset"] = args["offset"]
        if args.get("limit") is not None:
            params["limit"] = args["limit"]
        return _alias_json_output(
            await mem.get_memory_artifact_tool(
                memory_id,
                artifact_id,
                params=params,
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_list_artifacts":
        memory_id = _resolve_memory_id(args, aliases)
        return _alias_json_output(
            await mem.list_memory_artifacts_tool(
                memory_id,
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_get_artifact_url":
        memory_id = _resolve_memory_id(args, aliases)
        artifact_id = str(args.get("artifact_id", "")).strip()
        artifact_url_payload: dict[str, Any] = {}
        if args.get("ttl") is not None:
            artifact_url_payload["ttl"] = args["ttl"]
        return _alias_json_output(
            await mem.get_memory_artifact_url_tool(
                memory_id,
                artifact_id,
                payload=artifact_url_payload,
                agent_id=agent_id,
                user_email=user_email,
            ),
            aliases,
        )

    if tool_name == "memory_delete_artifact":
        memory_id, binding = await _resolve_mutation_target(
            args, aliases, mem, agent_id=agent_id, user_email=user_email
        )
        artifact_id = str(args.get("artifact_id", "")).strip()
        await mem.delete_memory_artifact_tool(
            memory_id,
            artifact_id,
            agent_id=agent_id,
            user_email=user_email,
            **_revision_precondition(binding),
        )
        if aliases is None:
            return ToolResult(output=f"Artifact {artifact_id} deleted from memory {memory_id}.")
        refreshed = await _refresh_memory_after_mutation(
            mem, memory_id, agent_id=agent_id, user_email=user_email
        )
        if refreshed is None:
            delta = aliases.invalidate_memory(memory_id)
            return ToolResult(
                output=(
                    f"Artifact {artifact_id} deleted. "
                    "Search again to get the current parent memory alias."
                ),
                metadata={MEMORY_ALIASES_METADATA: delta} if delta is not None else None,
            )
        projected, delta = _project_memory_records(refreshed, aliases)
        alias = None
        if isinstance(projected, dict):
            records = projected.get("results")
            if isinstance(records, list) and records and isinstance(records[0], dict):
                alias = records[0].get("id")
        suffix = f" Parent memory is now {alias}." if isinstance(alias, str) else ""
        return ToolResult(
            output=f"Artifact {artifact_id} deleted.{suffix}",
            metadata={MEMORY_ALIASES_METADATA: delta} if delta is not None else None,
        )

    return ToolResult(output=f"Unknown memory tool: {tool_name}", is_error=True)


def _filter_none(d: dict[str, Any]) -> dict[str, Any]:
    """Remove None values from a dict for clean API payloads."""
    return {k: v for k, v in d.items() if v is not None}
