"""Image generation and editing tools.

Controller-side builtin tools that call the ImageGenerationProvider.
Routed via ToolRoute.IMAGE in the tool router.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
from typing import Any

import httpx

from cognis.models.config import ImageGenerationResult
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource
from cognis.runtime_context import current_user_email
from cognis.store.queries import create_artifact_record
from cognis.tools.argument_normalization import strip_empty_optional_values

_SOURCE = ToolSource(type="builtin")

IMAGE_GENERATE_TOOL = ToolDefinition(
    name="image_generate",
    description=(
        "Generate an image from a text prompt. Returns a JSON object with "
        "the generated image ID and URL. Use descriptive, detailed prompts "
        "for best results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed text description of the image to generate.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model to use (e.g. 'gpt-image-1', 'dall-e-3'). "
                    "If omitted, uses the configured default image generation model."
                ),
            },
            "size": {
                "type": "string",
                "description": "Image size (e.g. '1024x1024', '1536x1024'). Model-dependent.",
            },
            "quality": {
                "type": "string",
                "description": "Image quality ('standard', 'hd', 'high', 'medium', 'low').",
            },
            "n": {
                "type": "integer",
                "description": "Number of images to generate (default 1).",
                "default": 1,
            },
        },
        "required": ["prompt"],
    },
    source=_SOURCE,
    category="image",
    read_only=True,
    timeout_seconds=120,
    max_result_size=100_000,
)

IMAGE_EDIT_TOOL = ToolDefinition(
    name="image_edit",
    description=(
        "Edit an existing image using a text prompt. Provide one image source: "
        "an existing image/artifact id, a remote URL, "
        "or an inline base64 payload."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Description of the desired changes to the image.",
            },
            "image": {
                "type": "string",
                "description": (
                    "Existing image or artifact id to edit. Supports img_* generated images "
                    "and other Cognis artifact ids when artifact storage is available."
                ),
            },
            "source_artifact_id": {
                "type": "string",
                "description": "Artifact id of an existing stored image to edit.",
            },
            "source_url": {
                "type": "string",
                "description": "Remote HTTP(S) image URL to edit.",
            },
            "image_b64": {
                "type": "string",
                "description": "Inline base64-encoded image payload to edit.",
            },
            "model": {
                "type": "string",
                "description": "Optional model to use for editing.",
            },
            "size": {
                "type": "string",
                "description": "Output image size.",
            },
        },
        "required": ["prompt"],
    },
    source=_SOURCE,
    category="image",
    read_only=True,
    timeout_seconds=120,
    max_result_size=100_000,
)

IMAGE_TOOL_NAMES = {"image_generate", "image_edit"}


def image_tools() -> list[ToolDefinition]:
    """Return all image tool definitions."""
    return [IMAGE_GENERATE_TOOL, IMAGE_EDIT_TOOL]


def is_image_tool(name: str) -> bool:
    """Check if a tool name is an image tool."""
    return name in IMAGE_TOOL_NAMES


async def handle_image_tool(
    tool_name: str,
    arguments: dict[str, Any],
    image_generation_provider: Any,
    artifact_store: Any | None = None,
    session_factory: Any | None = None,
) -> ToolResult:
    """Handle an image generation or edit tool call.

    Returns a JSON string with image details. If an artifact store is
    available, the generated image is saved and an image_id + URL are
    returned. Otherwise, the raw base64 data is returned.
    """
    arguments = _normalize_image_arguments(tool_name, arguments)
    prompt = arguments.get("prompt", "")
    if not prompt:
        return ToolResult(output="Error: prompt is required.", is_error=True)

    model = arguments.get("model")
    size = arguments.get("size")
    quality = arguments.get("quality")
    n = arguments.get("n", 1)
    image_b64: str | None = None

    if tool_name == "image_edit":
        try:
            image_b64 = await _resolve_edit_source_to_b64(
                arguments,
                artifact_store=artifact_store,
                session_factory=session_factory,
            )
        except DocumentedImageSourceError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)

    try:
        result: ImageGenerationResult = await image_generation_provider.image_generate(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            image=image_b64,
        )
    except Exception as exc:
        return ToolResult(output=f"Image generation failed: {exc}", is_error=True)

    if not result.images:
        return ToolResult(
            output=(
                "Image generation failed: provider returned no image data "
                f"for model {result.model!r}."
            ),
            is_error=True,
        )

    # Save generated images to artifact store and return IDs
    output_images: list[dict[str, Any]] = []
    outbound_attachments: list[dict[str, Any]] = []
    for img in result.images:
        if artifact_store is not None:
            image_id = artifact_store.generate_id("img")
            try:
                image_bytes = await _image_bytes(img)
                display_filename = _image_filename(image_id, img.content_type)
                await artifact_store.async_save(
                    "images",
                    image_id,
                    "image",
                    image_bytes,
                    img.content_type,
                    owner_email=current_user_email.get(),
                )
                if session_factory is not None:
                    try:
                        async with session_factory() as session:
                            await create_artifact_record(
                                session,
                                artifact_id=image_id,
                                namespace="images",
                                object_id=image_id,
                                filename="image",
                                owner_email=current_user_email.get(),
                                purpose="tool_output",
                                kind="image",
                                mime_type=img.content_type,
                                size_bytes=len(image_bytes),
                                status="attached",
                            )
                            await session.commit()
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            await artifact_store.async_delete("images", image_id, "image")
                        return ToolResult(
                            output=f"Image artifact registration failed: {exc}",
                            is_error=True,
                        )
                signed_url = await _resolve_image_url(artifact_store, image_id)
                output_images.append(
                    {
                        "image_id": image_id,
                        "url": signed_url or f"/api/v1/images/{image_id}",
                        "content_type": img.content_type,
                        "revised_prompt": img.revised_prompt,
                    }
                )
                attachment: dict[str, Any] = {
                    "artifact_id": image_id,
                    "mime_type": img.content_type,
                    "filename": display_filename,
                    "size_bytes": len(image_bytes),
                    "kind": "image",
                    "url": signed_url or f"/api/v1/images/{image_id}",
                    "content_b64": _encode_b64(image_bytes),
                }
                outbound_attachments.append(attachment)
            except Exception:
                # Fall back to inline base64 if save fails
                output_images.append(
                    {
                        "b64_json": (img.b64_json or "")[:100] + "...",
                        "url": img.url,
                        "content_type": img.content_type,
                        "revised_prompt": img.revised_prompt,
                    }
                )
        else:
            output_images.append(
                {
                    "b64_json": (img.b64_json or "")[:100] + "...",
                    "url": img.url,
                    "content_type": img.content_type,
                    "revised_prompt": img.revised_prompt,
                }
            )

    output = json.dumps(
        {"images": output_images, "model": result.model},
        sort_keys=True,
        default=str,
    )
    return ToolResult(output=output, attachments=outbound_attachments or None)


def _normalize_image_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    schema = (
        IMAGE_EDIT_TOOL.parameters if tool_name == "image_edit" else IMAGE_GENERATE_TOOL.parameters
    )
    return strip_empty_optional_values(arguments, schema)


def _image_filename(image_id: str, content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type) or ".png"
    return f"{image_id}{ext}"


async def _resolve_image_url(artifact_store: Any, image_id: str) -> str | None:
    try:
        return await artifact_store.async_get_public_url("images", image_id, "image")
    except Exception:
        return None


async def _image_bytes(img: Any) -> bytes:
    """Resolve generated image content to bytes from base64 or URL."""
    import base64

    if getattr(img, "b64_json", None):
        return base64.b64decode(img.b64_json)
    image_url = getattr(img, "url", None)
    if image_url:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            return response.content
    raise ValueError("Generated image does not contain base64 data or URL")


class DocumentedImageSourceError(ValueError):
    pass


async def _resolve_edit_source_to_b64(
    arguments: dict[str, Any],
    *,
    artifact_store: Any | None,
    session_factory: Any | None,
) -> str:
    source_keys = [
        key
        for key in ("image", "source_artifact_id", "source_url", "image_b64")
        if arguments.get(key)
    ]
    if arguments.get("source_path"):
        raise DocumentedImageSourceError(
            "Local source_path is not supported for image_edit because image tools run on the controller. "
            "Publish the file first with artifact_publish, then use source_artifact_id."
        )
    if len(source_keys) != 1:
        raise DocumentedImageSourceError(
            "Provide exactly one image source: image, source_artifact_id, source_url, or image_b64."
        )
    key = source_keys[0]
    value = str(arguments[key])
    if key == "image_b64":
        return value
    if key == "source_url":
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(value)
            response.raise_for_status()
            return _encode_b64(response.content)
    if artifact_store is None:
        raise DocumentedImageSourceError("artifact store not available for image resolution.")
    if key == "image" and value.startswith("img_"):
        try:
            content, _ct = await artifact_store.async_load("images", value, "image")
        except FileNotFoundError as exc:
            raise DocumentedImageSourceError(f"image {value} not found.") from exc
        return _encode_b64(content)
    artifact_id = value if key == "image" else str(arguments["source_artifact_id"])
    if session_factory is None:
        raise DocumentedImageSourceError(
            "artifact resolution requires database access for non-image artifact ids."
        )
    from cognis.store.queries import get_artifact_record

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted":
        raise DocumentedImageSourceError(f"artifact {artifact_id} not found.")
    content, _ct = await artifact_store.async_load(row.namespace, row.object_id, row.filename)
    return _encode_b64(content)


def _encode_b64(content: bytes) -> str:
    import base64

    return base64.b64encode(content).decode("ascii")
