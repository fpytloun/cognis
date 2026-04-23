"""Artifact reading and multimodal analysis tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource
from cognis.store.models import ModelRouting
from cognis.store.queries import (
    get_artifact_record,
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

ARTIFACT_READ_TOOL = ToolDefinition(
    name="artifact_read",
    description=(
        "Read a saved Cognis artifact by artifact_id. Text artifacts return line-numbered "
        "content. Images, PDFs, audio, and supported files are analyzed with the current "
        "model when possible and fall back to the configured attachment_analysis route."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Artifact id to inspect.",
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
        "generated files or attachments when you do not know the artifact_id."
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
        "content search."
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
        "Get metadata for one saved Cognis artifact by artifact_id. Use this after artifact_search "
        "or artifact_list_recent when you need the full stored metadata before reading the file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "Artifact id to inspect.",
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

ARTIFACT_TOOL_NAMES = frozenset(
    {
        ARTIFACT_READ_TOOL.name,
        ARTIFACT_LIST_RECENT_TOOL.name,
        ARTIFACT_SEARCH_TOOL.name,
        ARTIFACT_GET_METADATA_TOOL.name,
    }
)


def artifact_tools() -> list[ToolDefinition]:
    """Return artifact tool definitions."""

    return [
        ARTIFACT_READ_TOOL,
        ARTIFACT_LIST_RECENT_TOOL,
        ARTIFACT_SEARCH_TOOL,
        ARTIFACT_GET_METADATA_TOOL,
    ]


def is_artifact_tool(name: str) -> bool:
    """Return whether a tool belongs to the artifact builtin set."""

    return name in ARTIFACT_TOOL_NAMES


def attachment_supports_model(attachment: AttachmentRef, model_info: Any) -> bool:
    """Return whether a model can inspect an attachment natively."""

    if attachment.kind == ArtifactKind.IMAGE:
        return bool(getattr(model_info, "supports_vision", False))
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
) -> ToolResult:
    """Handle artifact inspection tools."""

    if tool_name == ARTIFACT_READ_TOOL.name:
        return await _handle_artifact_read(
            arguments,
            llm=llm,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            current_model=current_model,
            current_provider_id=current_provider_id,
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
            session_factory=session_factory,
            user_email=user_email,
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
) -> ToolResult:
    if artifact_store is None or session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)

    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(output="artifact_id is required.", is_error=True)

    offset = max(1, int(arguments.get("offset", 1)))
    limit = int(arguments.get("limit", _MAX_READ_LINES))
    prompt = str(arguments.get("prompt") or "").strip() or None

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted":
        return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
    if row.owner_email and user_email and row.owner_email != user_email:
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
            },
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
    session_factory: Any | None,
    user_email: str | None,
) -> ToolResult:
    if session_factory is None:
        return ToolResult(output="Artifact support is not available.", is_error=True)

    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolResult(output="artifact_id is required.", is_error=True)

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted":
        return ToolResult(output=f"Artifact not found: {artifact_id}", is_error=True)
    if row.owner_email and user_email and row.owner_email != user_email:
        return ToolResult(output=f"Artifact access denied: {artifact_id}", is_error=True)

    item = _artifact_metadata_item(row)
    return ToolResult(output=json.dumps(item, indent=2, sort_keys=True), metadata=item)


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
    text_offset: int = 1,
    text_limit: int = _MAX_READ_LINES,
) -> ToolResult:
    """Analyze one attachment using the active model or fallback route."""

    if llm is None:
        return ToolResult(
            output="LLM provider not available for attachment analysis.", is_error=True
        )

    model_info: Any | None = None
    selected_model = current_model
    selected_provider_id = current_provider_id
    selected_task_type = "default"
    used_fallback_route = False
    route_error: str | None = None

    if current_model:
        model_info = await _get_model_info(llm, current_model, current_provider_id)
        if not attachment_supports_model(attachment, model_info):
            model_info = None

    if model_info is None:
        route_model, route_provider_id = await _get_attachment_analysis_route(session_factory)
        if route_model:
            selected_model = route_model
            selected_provider_id = route_provider_id
            selected_task_type = "attachment_analysis"
            used_fallback_route = True
            model_info = await _get_model_info(llm, selected_model, selected_provider_id)
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

    file_url = attachment.url
    if not isinstance(file_url, str) or not file_url:
        return ToolResult(
            output=f"Could not obtain a signed URL for artifact {attachment.artifact_id}.",
            is_error=True,
        )

    analysis_prompt = prompt or _default_analysis_prompt(attachment)
    blocks = _analysis_blocks(attachment, file_url)
    response = await llm.generate(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": analysis_prompt},
                    *blocks,
                ],
            }
        ],
        model=selected_model,
        task_type=selected_task_type,
        provider_id=selected_provider_id,
    )
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
        return ToolResult(
            output="Attachment analysis returned no content.",
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
            "analysis_model": selected_model,
            "analysis_task_type": selected_task_type,
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


async def _get_attachment_analysis_route(session_factory: Any) -> tuple[str | None, str | None]:
    async with session_factory() as session:
        route = await session.get(ModelRouting, "attachment_analysis")
    if route is None:
        return None, None
    model = str(getattr(route, "model", "") or "").strip()
    if not model:
        return None, None
    provider_id = getattr(route, "provider_id", None)
    return model, str(provider_id) if isinstance(provider_id, str) and provider_id else None


async def _get_model_info(llm: Any, model: str, provider_id: str | None) -> Any:
    if provider_id is not None:
        try:
            return await llm.get_model_info(model, provider_id=provider_id)
        except TypeError:
            return await llm.get_model_info(model)
    return await llm.get_model_info(model)


def _analysis_blocks(attachment: AttachmentRef, url: str) -> list[dict[str, Any]]:
    if attachment.kind == ArtifactKind.IMAGE:
        return [{"type": "image_url", "image_url": {"url": url}}]
    return [{"type": "file", "file": {"file_url": url, "filename": attachment.filename}}]


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
    if normalized.startswith(("image/", "audio/", "video/")) or normalized == "application/pdf":
        return False
    if normalized.startswith("text/") or normalized in _TEXT_MIME_TYPES:
        return True
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
