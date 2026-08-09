"""Artifact reading and multimodal analysis tools."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.audio.transcription import transcribe_audio_bytes
from cognis.core.attachment_compat import supports_native_image_input
from cognis.core.content_refs import (
    build_deliverable_public_url,
    continuation_scope_task_id,
    deliverable_metadata_item,
    get_accessible_deliverable_ref,
    is_deliverable_ref,
    record_deliverable_access,
)
from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource
from cognis.store.queries import (
    create_artifact_record,
    find_tool_artifact_record,
    find_tool_output_artifact_record,
    get_artifact_record,
    get_model_routing,
    list_recent_artifact_records,
    search_artifact_records,
)

_SOURCE = ToolSource(type="builtin")
logger = get_logger(__name__)

_TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-yaml",
    "application/xml",
    "image/svg+xml",
    "text/csv",
    "text/html",
    "text/javascript",
    "text/markdown",
    "text/plain",
    "text/x-python",
    "text/xml",
}

_MAX_READ_LINES = 2000
_MAX_LINE_LENGTH = 2000
_TOOL_ARTIFACT_PREFIX = "tool_artifact:"
_MAX_REMOTE_ARTIFACT_BYTES = 25 * 1024 * 1024
_MAX_AUDIO_TRANSCRIPT_OUTPUT_CHARS = 100_000
# Tool artifact materialization intentionally avoids a new per-candidate table.
# Only selected candidates become normal artifacts. Until artifact metadata has a
# generic JSON provenance field, the materialized-artifact lookup stores stable
# source identity in existing low-impact metadata columns:
#   purpose="tool_artifact", conversation_id=<source call_id>,
#   session_id=<source anchor>, content_hash=<source fingerprint>.
_TOOL_ARTIFACT_PURPOSE = "tool_artifact"
_REMOTE_ARTIFACT_MAX_REDIRECTS = 5


def _is_expired_artifact_row(row: object, *, now: datetime | None = None) -> bool:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC))


def _clamp_ttl_to_artifact_expiry(row: object, requested_ttl_seconds: int) -> int:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return requested_ttl_seconds
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    remaining_seconds = int((expires_at - datetime.now(UTC)).total_seconds())
    return max(60, min(requested_ttl_seconds, remaining_seconds))


def _is_html_content_type(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "text/html"


ARTIFACT_READ_TOOL = ToolDefinition(
    name="artifact_read",
    description=(
        "Read an artifact-compatible content ref by artifact_id, including saved Cognis "
        "artifact IDs, authorized task or managed-descendant deliverable IDs (dlv_*), "
        "and lazy tool artifact refs "
        "(tool_artifact:<call_id>:<anchor>). Text content returns line-numbered content. "
        "Images, PDFs, audio, and supported saved artifacts are analyzed with the current model "
        "when possible and fall back to the configured attachment_analysis route."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Artifact-compatible content ref to inspect (saved artifact ID, task deliverable ID dlv_*, or tool_artifact:<call_id>:<anchor>).",
            },
            "prompt": {
                "type": "string",
                "description": "Optional question or instruction for the artifact analysis.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start from for text artifacts (1-indexed).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to return for text artifacts.",
            },
        },
        "required": ["artifact_id"],
    },
    source=_SOURCE,
    category="artifact",
    read_only=True,
    timeout_seconds=120,
    max_result_size=100_000,
)

ARTIFACT_LIST_RECENT_TOOL = ToolDefinition(
    name="artifact_list_recent",
    description=(
        "List your most recent saved Cognis artifacts by metadata. Use this to browse older "
        "generated files or attachments when you do not know the artifact_id. Call "
        "artifact_get_url for download links or UI attachments."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default 10, max 50).",
            },
            "kind": {
                "type": "string",
                "enum": [kind.value for kind in ArtifactKind],
                "description": "Optional artifact kind filter.",
            },
            "purpose": {
                "type": "string",
                "description": "Optional purpose filter, such as tool_output or chat_input.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Optional conversation filter.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session filter.",
            },
        },
    },
    source=_SOURCE,
    category="artifact",
    read_only=True,
    timeout_seconds=30,
    max_result_size=50_000,
)

ARTIFACT_SEARCH_TOOL = ToolDefinition(
    name="artifact_search",
    description=(
        "Search your saved Cognis artifacts by metadata such as filename, artifact_id, purpose, "
        "kind, conversation, session, and creation time. This is metadata-only search, not file "
        "content search. Call artifact_get_url for download links or UI attachments."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional metadata search text for filename, artifact_id, or purpose.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default 10, max 50).",
            },
            "kind": {
                "type": "string",
                "enum": [kind.value for kind in ArtifactKind],
                "description": "Optional artifact kind filter.",
            },
            "purpose": {
                "type": "string",
                "description": "Optional purpose filter, such as tool_output or chat_input.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Optional conversation filter.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session filter.",
            },
            "created_after": {
                "type": "string",
                "description": "Optional inclusive lower bound ISO timestamp.",
            },
            "created_before": {
                "type": "string",
                "description": "Optional inclusive upper bound ISO timestamp.",
            },
        },
    },
    source=_SOURCE,
    category="artifact",
    read_only=True,
    timeout_seconds=30,
    max_result_size=50_000,
)

ARTIFACT_GET_METADATA_TOOL = ToolDefinition(
    name="artifact_get_metadata",
    description=(
        "Get metadata for one artifact-compatible content ref by artifact_id, including saved "
        "Cognis artifact IDs and authorized task or managed-descendant deliverable IDs (dlv_*). "
        "Use this after artifact_search "
        "or artifact_list_recent when you need full stored metadata before reading a saved "
        "artifact. Call artifact_get_url when the user asks for a download URL or wants to view the content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Artifact-compatible content ref to inspect (saved artifact ID or task deliverable ID dlv_*).",
            }
        },
        "required": ["artifact_id"],
    },
    source=_SOURCE,
    category="artifact",
    read_only=True,
    timeout_seconds=15,
    max_result_size=30_000,
)

ARTIFACT_GET_URL_TOOL = ToolDefinition(
    name="artifact_get_url",
    description=(
        "Generate a short-lived download URL for an artifact-compatible content ref by "
        "artifact_id, including saved Cognis artifact IDs and authorized task or "
        "managed-descendant deliverable IDs (dlv_*). "
        "Use this when the user asks for download links, wants to view artifacts, or wants "
        "images/files returned as direct UI attachments. When another tool needs an artifact "
        "URL directly in its arguments, use an exact artifact value ref such as "
        "$artifact:<artifact_id>.signed_url or $artifact:<artifact_id>.public_url instead of "
        "calling this tool just to copy the URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Artifact-compatible content ref to download or attach (saved artifact ID or task deliverable ID dlv_*).",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": "Signed URL lifetime in seconds (default 3600, max 604800).",
                "minimum": 60,
                "maximum": 604800,
            },
            "mode": {
                "type": "string",
                "enum": ["download", "view"],
                "description": "URL serving mode. Default download serves files as attachments; view serves supported HTML artifacts inline.",
            },
        },
        "required": ["artifact_id"],
    },
    source=_SOURCE,
    category="artifact",
    read_only=True,
    timeout_seconds=15,
    max_result_size=30_000,
)

ARTIFACT_TOOL_NAMES = frozenset(
    {
        ARTIFACT_READ_TOOL.name,
        ARTIFACT_LIST_RECENT_TOOL.name,
        ARTIFACT_SEARCH_TOOL.name,
        ARTIFACT_GET_METADATA_TOOL.name,
        ARTIFACT_GET_URL_TOOL.name,
    }
)


def artifact_tools() -> list[ToolDefinition]:
    """Return artifact tool definitions."""

    return [
        ARTIFACT_READ_TOOL,
        ARTIFACT_LIST_RECENT_TOOL,
        ARTIFACT_SEARCH_TOOL,
        ARTIFACT_GET_METADATA_TOOL,
        ARTIFACT_GET_URL_TOOL,
    ]


def is_artifact_tool(name: str) -> bool:
    """Return whether a tool belongs to the artifact builtin set."""

    return name in ARTIFACT_TOOL_NAMES


def attachment_supports_model(attachment: AttachmentRef, model_info: Any) -> bool:
    """Return whether a model can inspect an attachment natively."""

    if attachment.kind == ArtifactKind.IMAGE:
        return supports_native_image_input(
            model_info,
            attachment.mime_type,
            filename=attachment.filename,
        )
    if attachment.kind == ArtifactKind.PDF:
        return bool(
            getattr(model_info, "supports_pdf_input", False)
            or getattr(model_info, "supports_file_input", False)
        )
    if attachment.kind == ArtifactKind.AUDIO:
        return bool(
            getattr(model_info, "supports_audio_input", False)
            or getattr(model_info, "supports_file_input", False)
        )
    return bool(getattr(model_info, "supports_file_input", False))


def _deliverable_accessor(
    runtime_metadata: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not isinstance(runtime_metadata, dict):
        return None, None
    conversation_id = runtime_metadata.get("conversation_id")
    agent_id = runtime_metadata.get("agent_id")
    return (
        conversation_id if isinstance(conversation_id, str) and conversation_id else None,
        agent_id if isinstance(agent_id, str) and agent_id else None,
    )


async def handle_artifact_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    llm: Any | None,
    artifact_store: Any | None,
    session_factory: Any | None,
    user_email: str | None,
    current_model: str | None = None,
    current_provider_id: str | None = None,
    owner_email: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """Handle artifact inspection tools."""

    scope_task_id = continuation_scope_task_id(runtime_metadata)
    accessor_conversation_id, accessor_agent_id = _deliverable_accessor(runtime_metadata)
    if tool_name == ARTIFACT_READ_TOOL.name:
        return await _handle_artifact_read(
            arguments,
            llm=llm,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            current_model=current_model,
            current_provider_id=current_provider_id,
            owner_email=owner_email or user_email,
            scope_task_id=scope_task_id,
            accessor_conversation_id=accessor_conversation_id,
            accessor_agent_id=accessor_agent_id,
            runtime_metadata=runtime_metadata,
        )
    if tool_name == ARTIFACT_LIST_RECENT_TOOL.name:
        return await _handle_artifact_list_recent(
            arguments,
            session_factory=session_factory,
            user_email=user_email,
        )
    if tool_name == ARTIFACT_SEARCH_TOOL.name:
        return await _handle_artifact_search(
            arguments,
            session_factory=session_factory,
            user_email=user_email,
        )
    if tool_name == ARTIFACT_GET_METADATA_TOOL.name:
        return await _handle_artifact_get_metadata(
            arguments,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            scope_task_id=scope_task_id,
            accessor_conversation_id=accessor_conversation_id,
            accessor_agent_id=accessor_agent_id,
        )
    if tool_name == ARTIFACT_GET_URL_TOOL.name:
        return await _handle_artifact_get_url(
            arguments,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            scope_task_id=scope_task_id,
            accessor_conversation_id=accessor_conversation_id,
            accessor_agent_id=accessor_agent_id,
        )
    return ToolResult(output=f"Unknown artifact tool: {tool_name}", is_error=True)


async def _handle_artifact_read(
    arguments: dict[str, Any],
    *,
    llm: Any | None,
    artifact_store: Any | None,
    session_factory: Any | None,
    user_email: str | None,
    current_model: str | None,
    current_provider_id: str | None,
    owner_email: str | None = None,
    scope_task_id: str | None = None,
    accessor_conversation_id: str | None = None,
    accessor_agent_id: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> ToolResult:
    if artifact_store is None or session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)

    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(output="artifact_id is required.", is_error=True)

    if _is_tool_artifact_ref(artifact_id):
        resolved = await materialize_tool_artifact_ref(
            artifact_id,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=owner_email or user_email,
            runtime_metadata=runtime_metadata,
        )
        if resolved.is_error:
            return resolved
        resolved_id = str((resolved.metadata or {}).get("artifact_id") or "")
        if not resolved_id:
            return ToolResult(output=f"Failed to materialize {artifact_id}.", is_error=True)
        arguments = dict(arguments)
        arguments["artifact_id"] = resolved_id
        nested = await _handle_artifact_read(
            arguments,
            llm=llm,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            current_model=current_model,
            current_provider_id=current_provider_id,
            owner_email=owner_email,
            scope_task_id=scope_task_id,
            accessor_conversation_id=accessor_conversation_id,
            accessor_agent_id=accessor_agent_id,
            runtime_metadata=runtime_metadata,
        )
        metadata = dict(resolved.metadata or {})
        metadata.update(nested.metadata or {})
        metadata["tool_artifact_ref"] = artifact_id
        metadata["materialized_artifact_id"] = resolved_id
        output = f"Materialized {artifact_id} as artifact {resolved_id}.\n\n{nested.output}"
        return nested.model_copy(update={"output": output, "metadata": metadata})

    offset = max(1, int(arguments.get("offset", 1)))
    limit = int(arguments.get("limit", _MAX_READ_LINES))
    prompt = str(arguments.get("prompt") or "").strip() or None

    if is_deliverable_ref(artifact_id):
        async with session_factory() as session:
            ref = await get_accessible_deliverable_ref(
                session,
                artifact_store,
                artifact_id,
                user_email,
                scope_task_id=scope_task_id,
                accessor_conversation_id=accessor_conversation_id,
                accessor_agent_id=accessor_agent_id,
            )
        if ref is None:
            return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
        await record_deliverable_access(session_factory, ref)
        return ToolResult(
            output=_render_text_excerpt(ref.deliverable.content, offset, limit),
            metadata={
                "artifact_id": ref.artifact_id,
                "deliverable_id": ref.artifact_id,
                "filename": ref.filename,
                "mime_type": ref.mime_type,
                "kind": "file",
                "source": "deliverable",
                "virtual": True,
            },
        )

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted" or _is_expired_artifact_row(row):
        return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
    effective_owner_email = owner_email or user_email
    if row.owner_email and effective_owner_email and row.owner_email != effective_owner_email:
        return ToolResult(output=f"Artifact access denied: {artifact_id}", is_error=True)

    try:
        content, content_type = await artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
    except Exception as exc:
        return ToolResult(output=f"Failed to load artifact {artifact_id}: {exc}", is_error=True)

    attachment = await _hydrate_attachment_ref(
        row=row,
        artifact_store=artifact_store,
        content_type=content_type,
    )

    if _is_text_artifact(attachment.mime_type, content):
        return ToolResult(
            output=_render_text_excerpt(content.decode("utf-8", errors="replace"), offset, limit),
            metadata={
                "artifact_id": attachment.artifact_id,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "kind": attachment.kind.value,
                "url": attachment.url,
            },
        )

    if current_model and attachment.url:
        model_info = await _get_model_info(
            llm,
            current_model,
            current_provider_id,
            acting_user_email=owner_email or user_email,
        )
        if attachment_supports_model(attachment, model_info):
            safe_attachment = _attachment_ref_tool_payload(attachment)
            # Mark as inspection-only so the agent loop injects it into the next
            # LLM cycle but does NOT echo it back to the channel as outbound media.
            safe_attachment = {**safe_attachment, "native_inspection_only": True}
            prompt_note = f"\nRequested analysis prompt: {prompt}" if prompt else ""
            return ToolResult(
                output=(
                    f"Prepared artifact '{attachment.filename}' for native model inspection. "
                    "The next model cycle receives it as an attachment; use the attachment "
                    "content directly to answer the user's request."
                    f"{prompt_note}"
                ),
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "url": attachment.url,
                    "native_attachment": True,
                    "analysis_model": current_model,
                    "analysis_provider_id": current_provider_id,
                },
                attachments=[safe_attachment],
            )

    return await analyze_attachment_ref(
        attachment=attachment,
        content=content,
        prompt=prompt,
        llm=llm,
        artifact_store=artifact_store,
        session_factory=session_factory,
        current_model=current_model,
        current_provider_id=current_provider_id,
        owner_email=effective_owner_email,
        text_offset=offset,
        text_limit=limit,
    )


async def _handle_artifact_list_recent(
    arguments: dict[str, Any],
    *,
    session_factory: Any | None,
    user_email: str | None,
) -> ToolResult:
    if session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)
    owner_email = _require_user_email(user_email)
    if owner_email is None:
        return ToolResult(output="Artifact lookup requires an authenticated user.", is_error=True)

    limit = _coerce_limit(arguments.get("limit"), default=10, maximum=50)
    kind, error = _coerce_artifact_kind(arguments.get("kind"))
    if error is not None:
        return ToolResult(output=error, is_error=True)
    purpose = _optional_string(arguments.get("purpose"))
    conversation_id = _optional_string(arguments.get("conversation_id"))
    session_id = _optional_string(arguments.get("session_id"))

    async with session_factory() as session:
        rows = await list_recent_artifact_records(
            session,
            owner_email=owner_email,
            limit=limit,
            kind=kind,
            purpose=purpose,
            conversation_id=conversation_id,
            session_id=session_id,
        )

    items = [_artifact_list_item(row) for row in rows]
    if not items:
        return ToolResult(
            output="No artifacts found for the requested filters.",
            metadata={"items": [], "count": 0},
        )
    return ToolResult(
        output=_render_artifact_list(
            items,
            heading=f"Found {len(items)} recent artifact(s).",
        ),
        metadata={"items": items, "count": len(items)},
    )


async def _handle_artifact_search(
    arguments: dict[str, Any],
    *,
    session_factory: Any | None,
    user_email: str | None,
) -> ToolResult:
    if session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)
    owner_email = _require_user_email(user_email)
    if owner_email is None:
        return ToolResult(output="Artifact lookup requires an authenticated user.", is_error=True)

    query = _optional_string(arguments.get("query"))
    limit = _coerce_limit(arguments.get("limit"), default=10, maximum=50)
    kind, error = _coerce_artifact_kind(arguments.get("kind"))
    if error is not None:
        return ToolResult(output=error, is_error=True)
    purpose = _optional_string(arguments.get("purpose"))
    conversation_id = _optional_string(arguments.get("conversation_id"))
    session_id = _optional_string(arguments.get("session_id"))
    created_after, error = _parse_optional_datetime(arguments.get("created_after"), "created_after")
    if error is not None:
        return ToolResult(output=error, is_error=True)
    created_before, error = _parse_optional_datetime(
        arguments.get("created_before"), "created_before"
    )
    if error is not None:
        return ToolResult(output=error, is_error=True)
    if not any(
        value is not None
        for value in (
            query,
            kind,
            purpose,
            conversation_id,
            session_id,
            created_after,
            created_before,
        )
    ):
        return ToolResult(
            output=(
                "Provide at least one search filter. Use artifact_list_recent to browse without "
                "a query."
            ),
            is_error=True,
        )

    async with session_factory() as session:
        rows = await search_artifact_records(
            session,
            owner_email=owner_email,
            query=query,
            limit=limit,
            kind=kind,
            purpose=purpose,
            conversation_id=conversation_id,
            session_id=session_id,
            created_after=created_after,
            created_before=created_before,
        )

    items = [_artifact_list_item(row) for row in rows]
    if not items:
        return ToolResult(
            output="No artifacts matched the search filters.",
            metadata={"items": [], "count": 0},
        )
    return ToolResult(
        output=_render_artifact_list(
            items,
            heading=f"Found {len(items)} artifact(s) matching the search filters.",
        ),
        metadata={"items": items, "count": len(items)},
    )


async def _handle_artifact_get_metadata(
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
    session_factory: Any | None,
    user_email: str | None,
    scope_task_id: str | None = None,
    accessor_conversation_id: str | None = None,
    accessor_agent_id: str | None = None,
) -> ToolResult:
    if artifact_store is None or session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)

    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(output="artifact_id is required.", is_error=True)

    if is_deliverable_ref(artifact_id):
        async with session_factory() as session:
            ref = await get_accessible_deliverable_ref(
                session,
                artifact_store,
                artifact_id,
                user_email,
                scope_task_id=scope_task_id,
                accessor_conversation_id=accessor_conversation_id,
                accessor_agent_id=accessor_agent_id,
            )
        if ref is None:
            return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
        await record_deliverable_access(session_factory, ref)
        item = deliverable_metadata_item(ref)
        item["download_url_tool"] = ARTIFACT_GET_URL_TOOL.name
        return ToolResult(output=json.dumps(item, indent=2, sort_keys=True), metadata=item)

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted" or _is_expired_artifact_row(row):
        return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
    if row.owner_email and user_email and row.owner_email != user_email:
        return ToolResult(output=f"Artifact access denied: {artifact_id}", is_error=True)

    item = _artifact_metadata_item(row)
    item["download_url_tool"] = ARTIFACT_GET_URL_TOOL.name
    return ToolResult(output=json.dumps(item, indent=2, sort_keys=True), metadata=item)


async def _handle_artifact_get_url(
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
    session_factory: Any | None,
    user_email: str | None,
    scope_task_id: str | None = None,
    accessor_conversation_id: str | None = None,
    accessor_agent_id: str | None = None,
) -> ToolResult:
    if artifact_store is None or session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)

    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(output="artifact_id is required.", is_error=True)
    ttl_seconds = _coerce_limit(arguments.get("ttl_seconds"), default=3600, maximum=604800)
    ttl_seconds = max(60, ttl_seconds)
    mode = str(arguments.get("mode") or "download").strip().lower()
    if mode not in {"download", "view"}:
        return ToolResult(output="mode must be 'download' or 'view'.", is_error=True)

    if is_deliverable_ref(artifact_id):
        async with session_factory() as session:
            ref = await get_accessible_deliverable_ref(
                session,
                artifact_store,
                artifact_id,
                user_email,
                scope_task_id=scope_task_id,
                accessor_conversation_id=accessor_conversation_id,
                accessor_agent_id=accessor_agent_id,
            )
        if ref is None:
            return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
        await record_deliverable_access(session_factory, ref)
        if mode == "view" and not _is_html_content_type(ref.mime_type):
            return ToolResult(
                output=f"Artifact view is only supported for HTML artifacts: {artifact_id}",
                is_error=True,
            )
        try:
            url = build_deliverable_public_url(
                artifact_store,
                ref,
                ttl_seconds=ttl_seconds,
                mode=mode,
            )
        except Exception as exc:
            return ToolResult(
                output=f"Failed to create download URL for artifact {artifact_id}: {exc}",
                is_error=True,
            )
        item = {
            "artifact_id": ref.artifact_id,
            "deliverable_id": ref.artifact_id,
            "content_ref": ref.artifact_id,
            "source": "deliverable",
            "virtual": True,
            "url": url,
            "mode": mode,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
            "filename": ref.filename,
            "kind": "file",
            "mime_type": ref.mime_type,
            "size_bytes": ref.size_bytes,
        }
        return ToolResult(
            output=json.dumps(item, indent=2, sort_keys=True),
            metadata=item,
        )

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted" or _is_expired_artifact_row(row):
        return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
    if row.owner_email and user_email and row.owner_email != user_email:
        return ToolResult(output=f"Artifact access denied: {artifact_id}", is_error=True)
    if mode == "view" and not _is_html_content_type(str(row.mime_type)):
        return ToolResult(
            output=f"Artifact view is only supported for HTML artifacts: {artifact_id}",
            is_error=True,
        )

    try:
        ttl_seconds = _clamp_ttl_to_artifact_expiry(row, ttl_seconds)
        url = await artifact_store.async_get_public_url(
            row.namespace,
            row.object_id,
            row.filename,
            ttl_seconds=ttl_seconds,
            mode=mode,
        )
    except Exception as exc:
        return ToolResult(
            output=f"Failed to create download URL for artifact {artifact_id}: {exc}",
            is_error=True,
        )

    item = _artifact_url_item(row, url=url, ttl_seconds=ttl_seconds, mode=mode)
    # artifact_get_url returns a signed URL for the artifact.  The LLM and the
    # tool-call block UI already have the URL from the JSON output; returning the
    # artifact as an attachment would echo a file the user already uploaded into the
    # assistant message bubble.  The assistant bubble should only carry genuinely new
    # artifacts (e.g. generated files), not re-deliveries of existing ones.
    return ToolResult(
        output=json.dumps(item, indent=2, sort_keys=True),
        metadata=item,
    )


async def analyze_attachment_ref(
    *,
    attachment: AttachmentRef,
    content: bytes,
    prompt: str | None,
    llm: Any | None,
    artifact_store: Any,
    session_factory: Any,
    current_model: str | None,
    current_provider_id: str | None,
    owner_email: str | None = None,
    text_offset: int = 1,
    text_limit: int = _MAX_READ_LINES,
) -> ToolResult:
    """Analyze one attachment using the active model or fallback route."""

    if llm is None:
        return ToolResult(
            output="LLM provider not available for attachment analysis.", is_error=True
        )

    if attachment.kind == ArtifactKind.AUDIO:
        try:
            transcript = await transcribe_audio_bytes(
                llm,
                content,
                mime_type=attachment.mime_type,
                filename=attachment.filename,
                acting_user_email=owner_email,
            )
        except Exception as exc:
            detail = _safe_analysis_error(exc)
            logger.warning(
                "Audio artifact transcription failed",
                extra={
                    "extra_data": {
                        "artifact_id": attachment.artifact_id,
                        "filename": attachment.filename,
                        "error": detail,
                    }
                },
            )
            return ToolResult(
                output=f"Audio transcription failed for {attachment.filename}: {detail}",
                is_error=True,
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_task_type": "speech_to_text",
                    "analysis_error": detail,
                },
            )
        if not transcript:
            return ToolResult(
                output=f"Audio transcription returned no text for {attachment.filename}.",
                is_error=True,
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_task_type": "speech_to_text",
                },
            )
        output = transcript[:_MAX_AUDIO_TRANSCRIPT_OUTPUT_CHARS]
        truncated = len(transcript) > _MAX_AUDIO_TRANSCRIPT_OUTPUT_CHARS
        if truncated:
            output += "\n[Transcript truncated at 100,000 characters.]"
        return ToolResult(
            output=output,
            metadata={
                "artifact_id": attachment.artifact_id,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "kind": attachment.kind.value,
                "analysis_task_type": "speech_to_text",
                "fallback": "audio_transcription",
                "truncated": truncated,
            },
        )

    model_info: Any | None = None
    selected_model = current_model
    selected_provider_id = current_provider_id
    selected_task_type = "default"
    used_fallback_route = False
    route_error: str | None = None

    if current_model:
        model_info = await _get_model_info(
            llm,
            current_model,
            current_provider_id,
            acting_user_email=owner_email,
        )
        if not attachment_supports_model(attachment, model_info):
            model_info = None

    if model_info is None:
        route_model, route_provider_id = await _get_attachment_analysis_route(
            session_factory, owner_email=owner_email
        )
        if route_model:
            selected_model = route_model
            selected_provider_id = route_provider_id
            selected_task_type = "attachment_analysis"
            used_fallback_route = True
            model_info = await _get_model_info(
                llm,
                selected_model,
                selected_provider_id,
                acting_user_email=owner_email,
            )
            if not attachment_supports_model(attachment, model_info):
                route_error = (
                    "The configured attachment_analysis model "
                    f"({selected_model}) cannot inspect {attachment.kind.value} attachments."
                )
                model_info = None

    if attachment.kind == ArtifactKind.PDF and model_info is None:
        extracted = _extract_pdf_text(content, attachment.filename)
        if extracted is not None:
            return ToolResult(
                output=_render_text_excerpt(extracted, text_offset, text_limit),
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_model": selected_model,
                    "analysis_task_type": selected_task_type,
                    "used_attachment_analysis_route": used_fallback_route,
                    "fallback": "pdf_text_extraction",
                },
            )

    if model_info is None or not selected_model:
        if route_error is not None:
            return ToolResult(output=route_error, is_error=True)
        return ToolResult(
            output=(
                f"The current model cannot inspect {attachment.kind.value} attachments. "
                "Configure the attachment_analysis route in Settings -> Routing or switch to "
                "a compatible chat model."
            ),
            is_error=True,
        )

    file_url = attachment.url if isinstance(attachment.url, str) and attachment.url else None
    if attachment.kind != ArtifactKind.IMAGE and file_url is None:
        return ToolResult(
            output=f"Could not obtain a signed URL for artifact {attachment.artifact_id}.",
            is_error=True,
        )

    analysis_prompt = prompt or _default_analysis_prompt(attachment)
    inline_messages = None
    if attachment.kind == ArtifactKind.IMAGE and file_url is not None:
        inline_messages = _analysis_messages(
            prompt=analysis_prompt,
            attachment=attachment,
            file_url=file_url,
            blocks=_analysis_blocks(attachment, file_url=None, content=content),
        )
    blocks = _analysis_blocks(attachment, file_url=file_url, content=content)
    messages = _analysis_messages(
        prompt=analysis_prompt,
        attachment=attachment,
        file_url=file_url,
        blocks=blocks,
    )
    analysis_payload = "url" if file_url is not None else "inline"
    try:
        response, analysis_payload = await _generate_analysis(
            llm,
            messages=messages,
            inline_messages=inline_messages,
            primary_payload=analysis_payload,
            model=selected_model,
            task_type=selected_task_type,
            provider_id=selected_provider_id,
        )
    except Exception as exc:
        if used_fallback_route:
            detail = _safe_analysis_error(exc)
            logger.warning(
                "Attachment analysis route failed",
                extra={
                    "extra_data": {
                        "artifact_id": attachment.artifact_id,
                        "filename": attachment.filename,
                        "kind": attachment.kind.value,
                        "analysis_model": selected_model,
                        "analysis_provider_id": selected_provider_id,
                        "analysis_task_type": selected_task_type,
                        "error": detail,
                    }
                },
            )
            return ToolResult(
                output=(
                    "Attachment analysis failed using the configured "
                    f"attachment_analysis route ({selected_model}): {detail}"
                ),
                is_error=True,
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_model": selected_model,
                    "analysis_task_type": selected_task_type,
                    "analysis_error": detail,
                    "used_attachment_analysis_route": True,
                },
            )
        try:
            fallback = await _fallback_analysis_response_after_error(
                attachment=attachment,
                messages=messages,
                inline_messages=inline_messages,
                llm=llm,
                session_factory=session_factory,
                owner_email=owner_email,
                original_model=selected_model,
                original_provider_id=selected_provider_id,
                original_error=exc,
            )
        except _FallbackAnalysisFailed as fallback_exc:
            original_detail = _safe_analysis_error(exc)
            fallback_detail = _safe_analysis_error(fallback_exc.original_error)
            return ToolResult(
                output=(
                    f"Attachment analysis failed using the current model ({selected_model}) "
                    f"and the configured attachment_analysis route: {fallback_detail}"
                ),
                is_error=True,
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_model": selected_model,
                    "analysis_task_type": selected_task_type,
                    "fallback_analysis_model": fallback_exc.model,
                    "fallback_analysis_provider_id": fallback_exc.provider_id,
                    "fallback_analysis_task_type": "attachment_analysis",
                    "analysis_error": original_detail,
                    "fallback_analysis_error": fallback_detail,
                    "used_attachment_analysis_route": True,
                },
            )
        if fallback is None:
            detail = _safe_analysis_error(exc)
            logger.warning(
                "Native attachment analysis failed and no fallback route was available",
                extra={
                    "extra_data": {
                        "artifact_id": attachment.artifact_id,
                        "filename": attachment.filename,
                        "kind": attachment.kind.value,
                        "analysis_model": selected_model,
                        "analysis_provider_id": selected_provider_id,
                        "analysis_task_type": selected_task_type,
                        "error": detail,
                    }
                },
            )
            return ToolResult(
                output=(
                    f"Attachment analysis failed using the current model ({selected_model}): "
                    f"{detail}. Configure a compatible attachment_analysis route or retry with "
                    "a different model."
                ),
                is_error=True,
                metadata={
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind.value,
                    "analysis_model": selected_model,
                    "analysis_task_type": selected_task_type,
                    "analysis_error": detail,
                    "used_attachment_analysis_route": False,
                },
            )
        response = fallback["response"]
        selected_model = fallback["model"]
        selected_provider_id = fallback["provider_id"]
        selected_task_type = "attachment_analysis"
        analysis_payload = fallback["payload"]
        used_fallback_route = True
    output = extract_text_from_response(response).strip()
    if not output:
        diagnostics = _analysis_response_diagnostics(
            response,
            attachment=attachment,
            analysis_model=selected_model,
            analysis_provider_id=selected_provider_id,
            analysis_task_type=selected_task_type,
            used_attachment_analysis_route=used_fallback_route,
        )
        logger.warning(
            "Attachment analysis returned empty response",
            extra={"extra_data": diagnostics},
        )
        if used_fallback_route and selected_model:
            message = (
                f"Attachment analysis route model '{selected_model}' returned no content for "
                f"'{attachment.filename}'."
            )
        elif selected_model:
            message = (
                f"Current model '{selected_model}' returned no content while inspecting "
                f"'{attachment.filename}'."
            )
        else:
            message = "Attachment analysis returned no content."
        return ToolResult(
            output=message,
            is_error=True,
            metadata=diagnostics,
        )
    return ToolResult(
        output=output,
        metadata={
            "artifact_id": attachment.artifact_id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "kind": attachment.kind.value,
            "url": attachment.url,
            "analysis_model": selected_model,
            "analysis_task_type": selected_task_type,
            "analysis_payload": analysis_payload,
            "used_attachment_analysis_route": used_fallback_route,
        },
    )


async def _hydrate_attachment_ref(
    *, row: Any, artifact_store: Any, content_type: str
) -> AttachmentRef:
    try:
        url = await artifact_store.async_get_public_url(row.namespace, row.object_id, row.filename)
    except Exception:
        url = None
    mime_type = str(getattr(row, "mime_type", None) or content_type or "application/octet-stream")
    kind_value = str(getattr(row, "kind", None) or _kind_for_mime_type(mime_type).value)
    return AttachmentRef(
        artifact_id=row.artifact_id,
        kind=ArtifactKind(kind_value),
        mime_type=mime_type,
        filename=str(row.filename),
        size_bytes=int(getattr(row, "size_bytes", 0) or 0),
        url=url,
    )


async def _get_attachment_analysis_route(
    session_factory: Any, *, owner_email: str | None = None
) -> tuple[str | None, str | None]:
    async with session_factory() as session:
        route = None
        if owner_email:
            route = await get_model_routing(session, "attachment_analysis", owner_email=owner_email)
        if route is None:
            route = await get_model_routing(session, "attachment_analysis")
    if route is None:
        return None, None
    model = str(getattr(route, "model", "") or "").strip()
    if not model:
        return None, None
    provider_id = getattr(route, "provider_id", None)
    return model, str(provider_id) if isinstance(provider_id, str) and provider_id else None


async def _get_model_info(
    llm: Any,
    model: str,
    provider_id: str | None,
    *,
    acting_user_email: str | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if acting_user_email is not None:
        kwargs["acting_user_email"] = acting_user_email
    if provider_id is not None:
        kwargs["provider_id"] = provider_id
        try:
            return await llm.get_model_info(model, **kwargs)
        except TypeError:
            if acting_user_email is not None:
                try:
                    return await llm.get_model_info(
                        model,
                        provider_id=provider_id,
                    )
                except TypeError:
                    return await llm.get_model_info(model)
            return await llm.get_model_info(model)
    try:
        return await llm.get_model_info(model, **kwargs)
    except TypeError:
        return await llm.get_model_info(model)


def _analysis_blocks(
    attachment: AttachmentRef,
    *,
    file_url: str | None,
    content: bytes,
) -> list[dict[str, Any]]:
    if attachment.kind == ArtifactKind.IMAGE:
        if file_url is not None:
            return [{"type": "image_url", "image_url": {"url": file_url}}]
        encoded = base64.b64encode(content).decode("ascii")
        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{attachment.mime_type};base64,{encoded}"},
            }
        ]
    if file_url is None:
        return []
    return [{"type": "file", "file": {"file_url": file_url, "filename": attachment.filename}}]


def _analysis_messages(
    *,
    prompt: str,
    attachment: AttachmentRef,
    file_url: str | None,
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "text", "text": _analysis_artifact_context(attachment, file_url)},
                *blocks,
            ],
        }
    ]


async def _fallback_analysis_response_after_error(
    *,
    attachment: AttachmentRef,
    messages: list[dict[str, Any]],
    inline_messages: list[dict[str, Any]] | None,
    llm: Any,
    session_factory: Any,
    owner_email: str | None,
    original_model: str | None,
    original_provider_id: str | None,
    original_error: Exception,
) -> dict[str, Any] | None:
    route_model, route_provider_id = await _get_attachment_analysis_route(
        session_factory, owner_email=owner_email
    )
    if not route_model:
        return None
    if _same_model_route(route_model, route_provider_id, original_model, original_provider_id):
        logger.warning(
            "Native attachment analysis failed and fallback route resolves to the same model",
            extra={
                "extra_data": {
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "kind": attachment.kind.value,
                    "original_model": original_model,
                    "original_provider_id": original_provider_id,
                    "fallback_model": route_model,
                    "fallback_provider_id": route_provider_id,
                    "original_error": _safe_analysis_error(original_error),
                }
            },
        )
        return None
    model_info = await _get_model_info(
        llm,
        route_model,
        route_provider_id,
        acting_user_email=owner_email,
    )
    if not attachment_supports_model(attachment, model_info):
        logger.warning(
            "Native attachment analysis failed and configured fallback route is incompatible",
            extra={
                "extra_data": {
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "kind": attachment.kind.value,
                    "original_model": original_model,
                    "original_provider_id": original_provider_id,
                    "fallback_model": route_model,
                    "fallback_provider_id": route_provider_id,
                    "original_error": _safe_analysis_error(original_error),
                }
            },
        )
        return None

    logger.info(
        "Retrying failed native attachment analysis with attachment_analysis route",
        extra={
            "extra_data": {
                "artifact_id": attachment.artifact_id,
                "filename": attachment.filename,
                "kind": attachment.kind.value,
                "original_model": original_model,
                "original_provider_id": original_provider_id,
                "fallback_model": route_model,
                "fallback_provider_id": route_provider_id,
                "original_error": _safe_analysis_error(original_error),
            }
        },
    )
    try:
        response, payload = await _generate_analysis(
            llm,
            messages=messages,
            inline_messages=inline_messages,
            primary_payload="url",
            model=route_model,
            task_type="attachment_analysis",
            provider_id=route_provider_id,
        )
    except Exception as exc:
        logger.warning(
            "Attachment analysis fallback route failed",
            extra={
                "extra_data": {
                    "artifact_id": attachment.artifact_id,
                    "filename": attachment.filename,
                    "kind": attachment.kind.value,
                    "original_model": original_model,
                    "original_provider_id": original_provider_id,
                    "fallback_model": route_model,
                    "fallback_provider_id": route_provider_id,
                    "original_error": _safe_analysis_error(original_error),
                    "fallback_error": _safe_analysis_error(exc),
                }
            },
        )
        raise _FallbackAnalysisFailed(
            exc, model=route_model, provider_id=route_provider_id
        ) from exc
    return {
        "response": response,
        "model": route_model,
        "provider_id": route_provider_id,
        "payload": payload,
    }


async def _generate_analysis(
    llm: Any,
    *,
    messages: list[dict[str, Any]],
    inline_messages: list[dict[str, Any]] | None,
    primary_payload: str,
    model: str | None,
    task_type: str,
    provider_id: str | None,
) -> tuple[Any, str]:
    try:
        response = await llm.generate(
            messages=messages,
            model=model,
            task_type=task_type,
            provider_id=provider_id,
        )
        return response, primary_payload
    except Exception as exc:
        if inline_messages is None:
            raise
        logger.info(
            "Retrying URL-based image analysis with inline image payload",
            extra={
                "extra_data": {
                    "model": model,
                    "provider_id": provider_id,
                    "task_type": task_type,
                    "error": _safe_analysis_error(exc),
                }
            },
        )
        response = await llm.generate(
            messages=inline_messages,
            model=model,
            task_type=task_type,
            provider_id=provider_id,
        )
        return response, "inline"


class _FallbackAnalysisFailed(RuntimeError):
    def __init__(
        self,
        error: Exception,
        *,
        model: str | None,
        provider_id: str | None,
    ) -> None:
        super().__init__(str(error))
        self.original_error = error
        self.model = model
        self.provider_id = provider_id


def _safe_analysis_error(error: Exception) -> str:
    return sanitize_client_error_detail(error, fallback="provider request failed")


def _attachment_ref_tool_payload(attachment: AttachmentRef) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_id": attachment.artifact_id,
        "kind": attachment.kind.value,
        "mime_type": attachment.mime_type,
        "filename": attachment.filename,
        "size_bytes": attachment.size_bytes,
    }
    if attachment.url:
        payload["url"] = attachment.url
    return payload


def _same_model_route(
    left_model: str | None,
    left_provider_id: str | None,
    right_model: str | None,
    right_provider_id: str | None,
) -> bool:
    if (left_model or "").strip() != (right_model or "").strip():
        return False
    left_provider = (left_provider_id or "").strip()
    right_provider = (right_provider_id or "").strip()
    return not left_provider or not right_provider or left_provider == right_provider


def _analysis_artifact_context(attachment: AttachmentRef, file_url: str | None) -> str:
    details = [
        f"artifact_id={attachment.artifact_id}",
        f"filename={attachment.filename}",
        f"kind={attachment.kind.value}",
        f"mime_type={attachment.mime_type}",
        f"size_bytes={attachment.size_bytes}",
    ]
    if file_url is not None:
        details.append(f"url={file_url}")
    return "Artifact metadata: " + ", ".join(details)


def _default_analysis_prompt(attachment: AttachmentRef) -> str:
    return (
        f"Inspect the attached {attachment.kind.value} '{attachment.filename}'. "
        "Describe the important content faithfully. Include any visible or embedded text when "
        "present, and clearly mention uncertainty when the content is ambiguous."
    )


def _analysis_response_diagnostics(
    response: dict[str, Any],
    *,
    attachment: AttachmentRef,
    analysis_model: str | None,
    analysis_provider_id: str | None,
    analysis_task_type: str,
    used_attachment_analysis_route: bool,
) -> dict[str, Any]:
    choices = response.get("choices")
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    content_part_types: list[str] = []
    if isinstance(content, list):
        content_part_types = [
            str(part.get("type") or "")
            for part in content
            if isinstance(part, dict) and str(part.get("type") or "")
        ]
    tool_calls = message.get("tool_calls")
    return {
        "artifact_id": attachment.artifact_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "kind": attachment.kind.value,
        "url": attachment.url,
        "analysis_model": analysis_model,
        "analysis_provider_id": analysis_provider_id,
        "analysis_task_type": analysis_task_type,
        "used_attachment_analysis_route": used_attachment_analysis_route,
        "response_status": response.get("response_status"),
        "finish_reason": first_choice.get("finish_reason")
        if isinstance(first_choice, dict)
        else None,
        "message_content_type": type(content).__name__ if content is not None else None,
        "content_part_types": content_part_types,
        "has_content": bool(extract_text_from_response(response).strip()),
        "has_reasoning_content": bool(str(message.get("reasoning_content") or "").strip()),
        "has_reasoning_summary": bool(str(message.get("reasoning") or "").strip()),
        "has_refusal": bool(str(message.get("refusal") or "").strip()),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
    }


def _extract_pdf_text(content: bytes, filename: str) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        chunks: list[str] = []
        for page in reader.pages[:8]:
            text = (page.extract_text() or "").strip()
            if text:
                chunks.append(text)
            if sum(len(chunk) for chunk in chunks) >= 4000:
                break
        if not chunks:
            return None
        header = (
            f"Best-effort extracted text from {filename}. "
            "Formatting, tables, and OCR may be imperfect.\n\n"
        )
        return header + "\n\n".join(chunks)
    except Exception:
        return None


def _is_text_artifact(mime_type: str, content: bytes) -> bool:
    normalized = mime_type.lower()
    if normalized.startswith("text/") or normalized in _TEXT_MIME_TYPES:
        return True
    if normalized.startswith(("image/", "audio/", "video/")) or normalized == "application/pdf":
        return False
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _render_text_excerpt(content: str, offset: int, limit: int) -> str:
    lines = content.splitlines(keepends=True)
    total = len(lines)
    selected = lines[offset - 1 : offset - 1 + limit]
    output_lines: list[str] = []
    for index, line in enumerate(selected, start=offset):
        rendered = line.rstrip("\n\r")
        if len(rendered) > _MAX_LINE_LENGTH:
            rendered = rendered[:_MAX_LINE_LENGTH] + "..."
        output_lines.append(f"{index}: {rendered}")
    result = "\n".join(output_lines)
    if offset + limit - 1 < total:
        result += (
            f"\n\n(Showing lines {offset}-{offset + len(selected) - 1} of {total}. "
            f"Use offset={offset + limit} to continue.)"
        )
    return result


def _kind_for_mime_type(mime_type: str) -> ArtifactKind:
    if mime_type.startswith("image/"):
        return ArtifactKind.IMAGE
    if mime_type.startswith("audio/"):
        return ArtifactKind.AUDIO
    if mime_type.startswith("video/"):
        return ArtifactKind.VIDEO
    if mime_type == "application/pdf":
        return ArtifactKind.PDF
    return ArtifactKind.FILE


def _is_tool_artifact_ref(value: str) -> bool:
    return value.startswith(_TOOL_ARTIFACT_PREFIX)


def _parse_tool_artifact_ref(value: str) -> tuple[str, str] | None:
    if not _is_tool_artifact_ref(value):
        return None
    rest = value[len(_TOOL_ARTIFACT_PREFIX) :]
    call_id, sep, anchor = rest.partition(":")
    if not sep or not call_id.strip() or not anchor.strip():
        return None
    return call_id.strip(), anchor.strip()


async def materialize_tool_artifact_ref(
    tool_artifact_ref: str,
    *,
    artifact_store: Any,
    session_factory: Any,
    user_email: str | None,
    runtime_metadata: dict[str, Any] | None,
) -> ToolResult:
    owner_email = _require_user_email(user_email)
    if owner_email is None:
        return ToolResult(
            output="Tool artifact lookup requires an authenticated user.", is_error=True
        )
    parsed = _parse_tool_artifact_ref(tool_artifact_ref)
    if parsed is None:
        return ToolResult(output=f"Invalid tool artifact ref: {tool_artifact_ref}", is_error=True)
    call_id, anchor_name = parsed
    authorized_refs = _runtime_authorized_lazy_artifact_refs(runtime_metadata)
    runtime_authorized = tool_artifact_ref in (authorized_refs or set())
    persisted_authorized = False
    if not runtime_authorized:
        conversation_id = _runtime_conversation_id(runtime_metadata)
        if conversation_id is not None:
            async with session_factory() as session:
                source_row = await find_tool_output_artifact_record(
                    session,
                    owner_email=owner_email,
                    source_tool_call_id=call_id,
                )
            persisted_authorized = (
                source_row is not None and source_row.conversation_id == conversation_id
            )
    if not runtime_authorized and not persisted_authorized:
        return ToolResult(output=f"Tool artifact access denied: {tool_artifact_ref}", is_error=True)
    tool_output_store = _runtime_tool_output_store(runtime_metadata)
    if tool_output_store is None:
        return ToolResult(output="Tool output store is not available.", is_error=True)
    anchors = await tool_output_store.list_anchors(call_id)
    if not anchors:
        return ToolResult(output=f"Tool output anchors not found: {call_id}", is_error=True)
    anchor = next(
        (item for item in anchors if getattr(item, "anchor", None) == anchor_name),
        None,
    )
    if anchor is None:
        return ToolResult(
            output=f"Tool output anchor not found: {tool_artifact_ref}",
            is_error=True,
        )
    candidate = getattr(anchor, "artifact_candidate", None)
    if not isinstance(candidate, dict):
        return ToolResult(
            output=f"Tool output anchor is not materializable: {tool_artifact_ref}",
            is_error=True,
        )
    if candidate.get("source_type") == "artifact_id":
        artifact_id = _optional_string(candidate.get("artifact_id"))
        if artifact_id is None:
            return ToolResult(
                output=f"Tool artifact saved artifact ID missing: {tool_artifact_ref}",
                is_error=True,
            )
        async with session_factory() as session:
            row = await get_artifact_record(session, artifact_id)
        if row is None or row.status == "deleted" or _is_expired_artifact_row(row):
            return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
        if row.owner_email and row.owner_email != owner_email:
            return ToolResult(output=f"Artifact access denied: {artifact_id}", is_error=True)
        metadata = _artifact_metadata_item(row)
        metadata.update({"tool_artifact_ref": tool_artifact_ref})
        return ToolResult(
            output=f"Resolved {tool_artifact_ref} to artifact {artifact_id}.",
            metadata=metadata,
        )
    if candidate.get("source_type") != "remote_url":
        return ToolResult(
            output=(
                "Unsupported tool artifact source_type: "
                f"{candidate.get('source_type') or 'unknown'}"
            ),
            is_error=True,
        )
    url = _optional_string(candidate.get("url"))
    if url is None:
        return ToolResult(
            output=f"Tool artifact source URL missing: {tool_artifact_ref}", is_error=True
        )
    candidate_metadata = candidate.get("metadata")
    source_page_url = (
        _optional_string(candidate_metadata.get("source_page_url"))
        if isinstance(candidate_metadata, dict)
        else None
    )
    evidence_source_url = source_page_url or url

    async with session_factory() as session:
        existing = await find_tool_artifact_record(
            session,
            owner_email=owner_email,
            source_tool_call_id=call_id,
            source_anchor=anchor_name,
        )
    if existing is not None:
        metadata = _artifact_metadata_item(existing)
        metadata.update(
            {
                "tool_artifact_ref": tool_artifact_ref,
                "source_url": evidence_source_url,
                "asset_url": url,
            }
        )
        return ToolResult(
            output=f"Resolved {tool_artifact_ref} to existing artifact {existing.artifact_id}.",
            metadata=metadata,
        )

    fetched = await _fetch_remote_artifact_candidate(url)
    if fetched.is_error:
        return fetched
    fetch_meta = fetched.metadata or {}
    content = fetch_meta.get("content")
    if not isinstance(content, bytes):
        return ToolResult(output=f"Failed to fetch {tool_artifact_ref}.", is_error=True)
    mime_type = str(
        fetch_meta.get("mime_type") or candidate.get("mime_hint") or "application/octet-stream"
    )
    filename = sanitize_tool_artifact_filename(
        str(candidate.get("filename_hint") or fetch_meta.get("filename") or "remote-artifact")
    )
    kind = _kind_for_mime_type(mime_type)
    content_hash = hashlib.sha256(content).hexdigest()
    conversation_id = _runtime_conversation_id(runtime_metadata)
    session_id = _runtime_session_id(runtime_metadata)
    artifact_id = artifact_store.generate_id("doc" if kind is ArtifactKind.PDF else "att")
    namespace = "documents" if kind is ArtifactKind.PDF else "attachments"
    await artifact_store.async_save(
        namespace,
        artifact_id,
        filename,
        content,
        mime_type,
        owner_email=owner_email,
    )
    async with session_factory() as session:
        row = await create_artifact_record(
            session,
            artifact_id=artifact_id,
            namespace=namespace,
            object_id=artifact_id,
            filename=filename,
            owner_email=owner_email,
            purpose=_TOOL_ARTIFACT_PURPOSE,
            kind=kind.value,
            mime_type=mime_type,
            size_bytes=len(content),
            status="attached",
            expires_at=None,
            conversation_id=conversation_id,
            session_id=session_id,
            message_role="assistant",
            content_hash=content_hash,
            source_tool_call_id=call_id,
            source_anchor=anchor_name,
        )
        await session.commit()
    metadata = _artifact_metadata_item(row)
    metadata.update(
        {
            "tool_artifact_ref": tool_artifact_ref,
            "source_url": evidence_source_url,
            "asset_url": url,
        }
    )
    return ToolResult(
        output=f"Materialized {tool_artifact_ref} as artifact {artifact_id}.",
        metadata=metadata,
    )


async def _fetch_remote_artifact_candidate(url: str) -> ToolResult:
    try:
        current_url, pinned_ip = _resolve_remote_artifact_url(url)
        for _hop in range(_REMOTE_ARTIFACT_MAX_REDIRECTS + 1):
            transport = httpx.AsyncHTTPTransport(retries=0)
            transport._pool._network_backend = _PinnedNetworkBackend(  # type: ignore[attr-defined]
                host=urlparse(current_url).hostname or "",
                ip_address=pinned_ip,
            )
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
                transport=transport,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    "GET",
                    current_url,
                    headers={"User-Agent": "Cognis artifact materializer"},
                )
                response = await client.send(request, stream=True)
                if response.is_redirect:
                    redirect_url = response.headers.get("location")
                    await response.aclose()
                    if not redirect_url:
                        return ToolResult(
                            output="Remote artifact candidate redirected without Location.",
                            is_error=True,
                        )
                    current_url, pinned_ip = _resolve_remote_artifact_url(
                        urljoin(str(response.request.url), redirect_url)
                    )
                    continue

                response.raise_for_status()
                mime_type = (
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                )
                if not mime_type.startswith("image/") and mime_type != "application/pdf":
                    await response.aclose()
                    return ToolResult(
                        output=(
                            f"Remote artifact candidate has unsupported content type: {mime_type}"
                        ),
                        is_error=True,
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_REMOTE_ARTIFACT_BYTES:
                        await response.aclose()
                        return ToolResult(
                            output="Remote artifact candidate exceeds maximum size.",
                            is_error=True,
                        )
                    chunks.append(chunk)
                final_url = str(response.url)
                await response.aclose()
                content = b"".join(chunks)
                filename = (
                    urlparse(final_url).path.rstrip("/").rsplit("/", 1)[-1] or "remote-artifact"
                )
                return ToolResult(
                    output="Fetched remote artifact candidate.",
                    metadata={
                        "content": content,
                        "mime_type": mime_type,
                        "filename": filename,
                    },
                )
        else:
            return ToolResult(
                output="Remote artifact candidate exceeded redirect limit.",
                is_error=True,
            )
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(output=f"Failed to fetch remote artifact candidate: {exc}", is_error=True)
    return ToolResult(output="Remote artifact candidate returned no response.", is_error=True)


def _validate_remote_artifact_url(url: str) -> str:
    validated, _ip_address = _resolve_remote_artifact_url(url)
    return validated


def _resolve_remote_artifact_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are supported")
    host = parsed.hostname
    if not host:
        raise ValueError("URL must have a hostname")
    addresses = _resolved_host_addresses(host)
    if not addresses:
        raise ValueError("URL hostname did not resolve")
    if any(_is_blocked_ip_address(address) for address in addresses):
        raise ValueError("URL resolves to a blocked network address")
    return url, sorted(addresses)[0]


class _PinnedNetworkBackend(httpcore.AnyIOBackend):
    """Connect one validated hostname to its controller-resolved public IP."""

    def __init__(self, *, host: str, ip_address: str) -> None:
        self._host = host
        self._ip_address = ip_address

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self._host:
            raise OSError("Unexpected hostname for pinned remote artifact transport")
        return await super().connect_tcp(
            self._ip_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _host_resolves_to_blocked_address(host: str) -> bool:
    addresses = _resolved_host_addresses(host)
    return not addresses or any(_is_blocked_ip_address(address) for address in addresses)


def _resolved_host_addresses(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return set()
    if not infos:
        return set()
    addresses: set[str] = set()
    for info in infos:
        try:
            addresses.add(str(ipaddress.ip_address(info[4][0])))
        except ValueError:
            return set()
    return addresses


def _is_blocked_ip_address(raw: str) -> bool:
    ip = ipaddress.ip_address(raw)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _runtime_tool_output_store(runtime_metadata: dict[str, Any] | None) -> Any | None:
    if not isinstance(runtime_metadata, dict):
        return None
    store = runtime_metadata.get("tool_output_store")
    if store is not None:
        return store
    shared = runtime_metadata.get("shared_runtime_metadata")
    if isinstance(shared, dict):
        return shared.get("tool_output_store")
    return None


def _runtime_authorized_lazy_artifact_refs(
    runtime_metadata: dict[str, Any] | None,
) -> set[str] | None:
    """Return controller-authorized lazy refs, or None for legacy callers."""

    if not isinstance(runtime_metadata, dict):
        return None
    value = runtime_metadata.get("authorized_lazy_artifact_refs")
    if value is None:
        shared = runtime_metadata.get("shared_runtime_metadata")
        if isinstance(shared, dict):
            value = shared.get("authorized_lazy_artifact_refs")
    if value is None:
        return None
    if not isinstance(value, list):
        return set()
    return {ref for ref in value if isinstance(ref, str) and ref.strip()}


def _runtime_conversation_id(runtime_metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(runtime_metadata, dict):
        return None
    access = runtime_metadata.get("runtime_access")
    if not isinstance(access, dict):
        shared = runtime_metadata.get("shared_runtime_metadata")
        if isinstance(shared, dict):
            access = shared.get("runtime_access")
    if isinstance(access, dict):
        value = access.get("conversation_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    context = runtime_metadata.get("conversation_context")
    if not isinstance(context, dict):
        shared = runtime_metadata.get("shared_runtime_metadata")
        if isinstance(shared, dict):
            context = shared.get("conversation_context")
    if not isinstance(context, dict):
        return None
    value = context.get("conversation_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _runtime_session_id(runtime_metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(runtime_metadata, dict):
        return None
    access = runtime_metadata.get("runtime_access")
    if not isinstance(access, dict):
        shared = runtime_metadata.get("shared_runtime_metadata")
        if isinstance(shared, dict):
            access = shared.get("runtime_access")
    if not isinstance(access, dict):
        return None
    value = access.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def sanitize_tool_artifact_filename(filename: str) -> str:
    import re

    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename).strip("._-")
    return safe[:160] or "remote-artifact"


def _require_user_email(user_email: str | None) -> str | None:
    if not isinstance(user_email, str):
        return None
    value = user_email.strip()
    return value or None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        limit = int(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def _coerce_artifact_kind(value: Any) -> tuple[str | None, str | None]:
    kind = _optional_string(value)
    if kind is None:
        return None, None
    try:
        return ArtifactKind(kind).value, None
    except ValueError:
        allowed = ", ".join(kind.value for kind in ArtifactKind)
        return None, f"Invalid artifact kind '{kind}'. Expected one of: {allowed}."


def _parse_optional_datetime(value: Any, field_name: str) -> tuple[datetime | None, str | None]:
    text = _optional_string(value)
    if text is None:
        return None, None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None, f"{field_name} must be a valid ISO timestamp."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, None


def _artifact_list_item(row: Any) -> dict[str, Any]:
    return {
        "artifact_id": str(row.artifact_id),
        "filename": str(row.filename),
        "kind": str(row.kind),
        "mime_type": str(row.mime_type),
        "purpose": str(row.purpose),
        "size_bytes": int(row.size_bytes or 0),
        "status": str(row.status),
        "created_at": _serialize_datetime(getattr(row, "created_at", None)),
        "conversation_id": getattr(row, "conversation_id", None),
        "session_id": getattr(row, "session_id", None),
    }


def _artifact_metadata_item(row: Any) -> dict[str, Any]:
    return {
        "artifact_id": str(row.artifact_id),
        "namespace": str(row.namespace),
        "object_id": str(row.object_id),
        "filename": str(row.filename),
        "owner_email": getattr(row, "owner_email", None),
        "conversation_id": getattr(row, "conversation_id", None),
        "session_id": getattr(row, "session_id", None),
        "message_role": getattr(row, "message_role", None),
        "purpose": str(row.purpose),
        "kind": str(row.kind),
        "mime_type": str(row.mime_type),
        "size_bytes": int(row.size_bytes or 0),
        "status": str(row.status),
        "created_at": _serialize_datetime(getattr(row, "created_at", None)),
        "expires_at": _serialize_datetime(getattr(row, "expires_at", None)),
        "deleted_at": _serialize_datetime(getattr(row, "deleted_at", None)),
    }


def _artifact_url_item(
    row: Any, *, url: str, ttl_seconds: int, mode: str = "download"
) -> dict[str, Any]:
    return {
        "artifact_id": str(row.artifact_id),
        "url": url,
        "mode": mode,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
        "filename": str(row.filename),
        "kind": str(row.kind),
        "mime_type": str(row.mime_type),
        "size_bytes": int(row.size_bytes or 0),
    }


def _artifact_attachment_item(row: Any, *, url: str) -> dict[str, Any]:
    return {
        "artifact_id": str(row.artifact_id),
        "url": url,
        "filename": str(row.filename),
        "kind": str(row.kind),
        "mime_type": str(row.mime_type),
        "size_bytes": int(row.size_bytes or 0),
    }


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _render_artifact_list(items: list[dict[str, Any]], *, heading: str) -> str:
    lines = [heading]
    for index, item in enumerate(items, start=1):
        details = [
            item["kind"],
            f"artifact_id={item['artifact_id']}",
            f"purpose={item['purpose']}",
            f"created_at={item['created_at'] or 'unknown'}",
        ]
        lines.append(f"{index}. {item['filename']} ({', '.join(details)})")
    return "\n".join(lines)
