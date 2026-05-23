"""Built-in knowledgebase operation tools."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cognis.knowledgebase.access import KnowledgebaseAccessContext
from cognis.models.knowledgebase import (
    KnowledgebaseAttachRequest,
    KnowledgebaseBulkAttachItem,
    KnowledgebaseBulkAttachRequest,
    KnowledgebaseCreateRequest,
    KnowledgebaseSearchRequest,
    KnowledgebaseSourceContextRequest,
    KnowledgebaseUpdateRequest,
)
from cognis.models.tool import ToolCapability, ToolDefinition, ToolSource
from cognis.tools.registry import ToolExecutionContext

_SOURCE = ToolSource(type="builtin")


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    read_only: bool = False,
    destructive: bool = False,
) -> ToolDefinition:
    capabilities = [ToolCapability.READ] if read_only else [ToolCapability.WRITE]
    if destructive:
        capabilities.append(ToolCapability.DESTRUCTIVE)
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }
    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        source=_SOURCE,
        category="knowledgebase",
        read_only=read_only,
        capabilities=capabilities,
    )


_KB_ID = {"knowledgebase_id": {"type": "string"}}
_ART_ID = {"artifact_id": {"type": "string"}}
_METADATA_VALUE = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "additionalProperties": True},
    ]
}
_METADATA_OBJECT = {
    "type": "object",
    "additionalProperties": _METADATA_VALUE,
}
_BULK_ATTACH_ITEM = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "metadata": _METADATA_OBJECT,
    },
    "required": ["artifact_id"],
}
_FILTER = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "op": {
            "type": "string",
            "enum": ["eq", "in", "contains", "overlap", "gte", "lte", "between"],
        },
        "value": {
            "description": (
                "Filter value. Use a scalar for eq/gte/lte/contains, an array for in/overlap, "
                "and a two-item array for between."
            )
        },
    },
    "required": ["field", "op", "value"],
}
_FILTERS = {
    "type": "array",
    "items": _FILTER,
    "description": "Schema-validated metadata filters applied before/after retrieval as supported.",
}
_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "string",
                            "keyword",
                            "integer",
                            "number",
                            "float",
                            "boolean",
                            "date",
                            "datetime",
                            "array",
                            "string[]",
                        ],
                    },
                    "items": {
                        "type": "object",
                        "properties": {"type": {"type": "string", "enum": ["string"]}},
                    },
                    "filterable": {"type": "boolean"},
                    "facetable": {"type": "boolean"},
                    "display": {"type": "boolean"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}

KNOWLEDGEBASE_TOOLS = [
    _tool(
        "knowledgebase_create",
        (
            "Create a knowledgebase owned by the current actor. When called from an active "
            "agent session owned by the same actor, the knowledgebase is assigned to that "
            "agent automatically."
        ),
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "metadata_schema": _METADATA_SCHEMA,
            "settings": {"type": "object"},
        },
        ["name"],
    ),
    _tool(
        "knowledgebase_list",
        "List knowledgebases available to the current actor and active agent session.",
        {},
        read_only=True,
    ),
    _tool(
        "knowledgebase_get",
        "Get a knowledgebase by id when available to the current actor and active agent session.",
        _KB_ID,
        ["knowledgebase_id"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_update",
        "Update an actor-owned knowledgebase. Requires owner/manage permission.",
        {
            **_KB_ID,
            "name": {"type": "string"},
            "description": {"type": "string"},
            "metadata_schema": _METADATA_SCHEMA,
            "settings": {"type": "object"},
            "status": {"type": "string", "enum": ["active", "archived"]},
        },
        ["knowledgebase_id"],
    ),
    _tool(
        "knowledgebase_list_artifacts",
        "List artifacts attached to an available knowledgebase. Shared-agent sessions may read assigned KB attachments but not manage them.",
        _KB_ID,
        ["knowledgebase_id"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_list_jobs",
        "List indexing jobs for an available knowledgebase.",
        _KB_ID,
        ["knowledgebase_id"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_status",
        "Get aggregate status/diagnostics for an available knowledgebase.",
        _KB_ID,
        ["knowledgebase_id"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_diagnostics",
        "Get diagnostics for an available knowledgebase.",
        _KB_ID,
        ["knowledgebase_id"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_attach_artifact",
        "Attach one artifact to an actor-owned knowledgebase and queue indexing. Requires owner/manage permission.",
        {**_KB_ID, **_ART_ID, "metadata": _METADATA_OBJECT},
        ["knowledgebase_id", "artifact_id"],
    ),
    _tool(
        "knowledgebase_attach_artifacts",
        (
            "Attach multiple artifacts to an actor-owned knowledgebase and queue indexing. "
            "Use artifact_ids with shared metadata, or items when each artifact has distinct "
            "metadata. Requires owner/manage permission."
        ),
        {
            **_KB_ID,
            "artifact_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "metadata": _METADATA_OBJECT,
            "items": {
                "type": "array",
                "items": _BULK_ATTACH_ITEM,
                "minItems": 1,
                "description": (
                    "Per-artifact attach items. Use instead of artifact_ids when documents have "
                    "individual metadata such as lesson_no, category, tags, or source paths."
                ),
            },
        },
        ["knowledgebase_id"],
    ),
    _tool(
        "knowledgebase_delete",
        (
            "Delete an actor-owned knowledgebase and queue cleanup of derived artifact indexes. "
            "Requires owner/manage permission."
        ),
        _KB_ID,
        ["knowledgebase_id"],
        destructive=True,
    ),
    _tool(
        "knowledgebase_detach_artifact",
        "Detach an artifact from an actor-owned knowledgebase and queue derived index cleanup. Requires owner/manage permission.",
        {**_KB_ID, **_ART_ID},
        ["knowledgebase_id", "artifact_id"],
        destructive=True,
    ),
    _tool(
        "knowledgebase_reindex_artifact",
        "Queue reindexing for one attached artifact in an actor-owned knowledgebase. Requires owner/manage permission.",
        {**_KB_ID, **_ART_ID},
        ["knowledgebase_id", "artifact_id"],
    ),
    _tool(
        "knowledgebase_reindex",
        "Queue reindexing for all active attachments in an actor-owned knowledgebase. Requires owner/manage permission.",
        _KB_ID,
        ["knowledgebase_id"],
    ),
    _tool(
        "knowledgebase_retry_job",
        "Retry a failed or cancelled indexing job in an actor-owned knowledgebase. Requires owner/manage permission.",
        {**_KB_ID, "job_id": {"type": "string"}},
        ["knowledgebase_id", "job_id"],
    ),
    _tool(
        "knowledgebase_search",
        "Search a knowledgebase available to the current actor and active agent session using native Qdrant hybrid retrieval.",
        {
            **_KB_ID,
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "filters": _FILTERS,
        },
        ["knowledgebase_id", "query"],
        read_only=True,
    ),
    _tool(
        "knowledgebase_read_source_context",
        (
            "Read source context around a knowledgebase search hit/chunk. Prefer this "
            "over generic artifact_read for KB search citations and chunk IDs; shared-agent "
            "sessions may use it for assigned KBs without broad raw artifact access."
        ),
        {
            **_KB_ID,
            "chunk_id": {"type": "string"},
            "before_chars": {"type": "integer"},
            "after_chars": {"type": "integer"},
        },
        ["knowledgebase_id", "chunk_id"],
        read_only=True,
    ),
]


def knowledgebase_tools() -> list[ToolDefinition]:
    return KNOWLEDGEBASE_TOOLS


def build_knowledgebase_tool_handlers(service: Any | None) -> dict[str, Any]:
    def _service() -> Any:
        if service is None:
            raise ValueError("Knowledgebase support is not available.")
        return service

    def _user(context: ToolExecutionContext) -> str:
        user_email = context.runtime_metadata.get("user_email")
        if not isinstance(user_email, str):
            runtime_access = context.runtime_metadata.get("runtime_access")
            if isinstance(runtime_access, dict):
                user_email = runtime_access.get("user_email")
        if not isinstance(user_email, str) and isinstance(context.shared_runtime_metadata, dict):
            user_email = context.shared_runtime_metadata.get("user_email")
        if not isinstance(user_email, str):
            raise ValueError("User context is unavailable")
        return user_email

    def _access_context(context: ToolExecutionContext) -> KnowledgebaseAccessContext:
        actor = _user(context)
        runtime_access = context.runtime_metadata.get("runtime_access")
        if not isinstance(runtime_access, dict):
            runtime_access = {}
        agent_id = runtime_access.get("agent_id")
        agent_owner_email = runtime_access.get("agent_owner_email")
        if not isinstance(agent_id, str):
            agent_id = None
        if not isinstance(agent_owner_email, str):
            agent_owner_email = None
        return KnowledgebaseAccessContext(
            actor_email=actor,
            agent_id=agent_id,
            agent_owner_email=agent_owner_email,
        )

    def _unavailable(knowledgebase_id: str, operation: str) -> dict[str, Any]:
        return {
            "error": "knowledgebase_not_found_or_unavailable",
            "operation": operation,
            "knowledgebase_id": knowledgebase_id,
            "message": (
                "Knowledgebase was not found, is unavailable, or is not accessible "
                "to the current actor."
            ),
        }

    def _invalid_arguments(operation: str, errors: list[str]) -> dict[str, Any]:
        return {
            "error": "invalid_tool_arguments",
            "operation": operation,
            "message": "Tool call arguments did not match the expected knowledgebase contract.",
            "errors": errors,
        }

    async def create(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        payload = KnowledgebaseCreateRequest(**arguments)
        row = await _service().create(
            owner_email=_user(context),
            name=payload.name,
            description=payload.description,
            metadata_schema=payload.metadata_schema,
            settings=payload.settings,
            access_context=_access_context(context),
        )
        return row.model_dump(mode="json")

    async def list_(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]]:
        rows = await _service().list(
            owner_email=_user(context), access_context=_access_context(context)
        )
        return [row.model_dump(mode="json") for row in rows]

    async def get(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        row = await _service().get(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(context),
        )
        return (
            row.model_dump(mode="json")
            if row is not None
            else _unavailable(knowledgebase_id, "get")
        )

    async def update(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        payload = KnowledgebaseUpdateRequest(
            **{key: value for key, value in arguments.items() if key != "knowledgebase_id"}
        )
        row = await _service().update(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            payload=payload,
        )
        return (
            row.model_dump(mode="json")
            if row is not None
            else _unavailable(knowledgebase_id, "update")
        )

    async def artifacts(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]] | dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        rows = await _service().artifacts(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(context),
        )
        return (
            [row.model_dump(mode="json") for row in rows]
            if rows is not None
            else _unavailable(knowledgebase_id, "list_artifacts")
        )

    async def jobs(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]] | dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        rows = await _service().jobs(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(context),
        )
        return (
            [row.model_dump(mode="json") for row in rows]
            if rows is not None
            else _unavailable(knowledgebase_id, "list_jobs")
        )

    async def diagnostics(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        row = await _service().diagnostics(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            access_context=_access_context(context),
        )
        return (
            row.model_dump(mode="json")
            if row is not None
            else _unavailable(knowledgebase_id, "diagnostics")
        )

    async def attach(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | None:
        payload = KnowledgebaseAttachRequest(
            artifact_id=str(arguments["artifact_id"]), metadata=arguments.get("metadata") or {}
        )
        row = await _service().attach(
            owner_email=_user(context),
            knowledgebase_id=str(arguments["knowledgebase_id"]),
            artifact_id=payload.artifact_id,
            metadata=payload.metadata,
        )
        return row.model_dump(mode="json") if row is not None else None

    async def attach_bulk(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]] | dict[str, Any]:
        try:
            payload = KnowledgebaseBulkAttachRequest(**arguments)
        except ValidationError as exc:
            return _invalid_arguments(
                "attach_artifacts",
                [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ],
            )
        if payload.artifact_ids:
            items = [
                KnowledgebaseBulkAttachItem(artifact_id=artifact_id, metadata=payload.metadata)
                for artifact_id in payload.artifact_ids
            ]
        else:
            items = payload.items
        results = []
        for item in items:
            row = await _service().attach(
                owner_email=_user(context),
                knowledgebase_id=str(arguments["knowledgebase_id"]),
                artifact_id=item.artifact_id,
                metadata=item.metadata,
            )
            if row is not None:
                results.append(row.model_dump(mode="json"))
        return results

    async def delete(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        deleted = await _service().delete(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
        )
        return {"knowledgebase_id": knowledgebase_id, "deleted": deleted}

    async def detach(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | None:
        row = await _service().detach(
            owner_email=_user(context),
            knowledgebase_id=str(arguments["knowledgebase_id"]),
            artifact_id=str(arguments["artifact_id"]),
        )
        return row.model_dump(mode="json") if row is not None else None

    async def reindex_artifact(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | None:
        row = await _service().reindex_artifact(
            owner_email=_user(context),
            knowledgebase_id=str(arguments["knowledgebase_id"]),
            artifact_id=str(arguments["artifact_id"]),
        )
        return row.model_dump(mode="json") if row is not None else None

    async def reindex(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> list[dict[str, Any]] | None:
        rows = await _service().reindex(
            owner_email=_user(context), knowledgebase_id=str(arguments["knowledgebase_id"])
        )
        return [row.model_dump(mode="json") for row in rows] if rows is not None else None

    async def retry_job(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | None:
        row = await _service().retry_job(
            owner_email=_user(context),
            knowledgebase_id=str(arguments["knowledgebase_id"]),
            job_id=str(arguments["job_id"]),
        )
        return row.model_dump(mode="json") if row is not None else None

    async def search(arguments: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        payload = KnowledgebaseSearchRequest(
            query=str(arguments["query"]),
            limit=arguments.get("limit") or 10,
            filters=arguments.get("filters") or [],
        )
        row = await _service().search(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            payload=payload,
            access_context=_access_context(context),
        )
        return (
            row.model_dump(mode="json")
            if row is not None
            else _unavailable(knowledgebase_id, "search")
        )

    async def source_context(
        arguments: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        knowledgebase_id = str(arguments["knowledgebase_id"])
        payload = KnowledgebaseSourceContextRequest(
            chunk_id=str(arguments["chunk_id"]),
            before_chars=arguments.get("before_chars") or 500,
            after_chars=arguments.get("after_chars") or 500,
        )
        row = await _service().source_context(
            owner_email=_user(context),
            knowledgebase_id=knowledgebase_id,
            chunk_id=payload.chunk_id,
            before_chars=payload.before_chars,
            after_chars=payload.after_chars,
            access_context=_access_context(context),
        )
        return (
            row.model_dump(mode="json")
            if row is not None
            else _unavailable(knowledgebase_id, "read_source_context")
        )

    return {
        "knowledgebase_create": create,
        "knowledgebase_list": list_,
        "knowledgebase_get": get,
        "knowledgebase_update": update,
        "knowledgebase_list_artifacts": artifacts,
        "knowledgebase_list_jobs": jobs,
        "knowledgebase_status": diagnostics,
        "knowledgebase_diagnostics": diagnostics,
        "knowledgebase_attach_artifact": attach,
        "knowledgebase_attach_artifacts": attach_bulk,
        "knowledgebase_delete": delete,
        "knowledgebase_detach_artifact": detach,
        "knowledgebase_reindex_artifact": reindex_artifact,
        "knowledgebase_reindex": reindex,
        "knowledgebase_retry_job": retry_job,
        "knowledgebase_search": search,
        "knowledgebase_read_source_context": source_context,
    }
