"""Artifact reading and multimodal analysis tools."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from cognis.core.json_utils import extract_text_from_response
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource
from cognis.store.models import ModelRouting
from cognis.store.queries import get_artifact_record

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


def artifact_tools() -> list[ToolDefinition]:
    """Return artifact tool definitions."""

    return [ARTIFACT_READ_TOOL]


def is_artifact_tool(name: str) -> bool:
    """Return whether a tool belongs to the artifact builtin set."""

    return name == ARTIFACT_READ_TOOL.name


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

    if tool_name != ARTIFACT_READ_TOOL.name:
        return ToolResult(output=f"Unknown artifact tool: {tool_name}", is_error=True)
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
        content, content_type = await artifact_store.async_load(row.namespace, row.object_id, row.filename)
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
        return ToolResult(output="LLM provider not available for attachment analysis.", is_error=True)

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
        max_tokens=1200,
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


async def _hydrate_attachment_ref(*, row: Any, artifact_store: Any, content_type: str) -> AttachmentRef:
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
        "finish_reason": first_choice.get("finish_reason") if isinstance(first_choice, dict) else None,
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
