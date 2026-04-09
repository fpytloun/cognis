"""Image generation and editing tools.

Controller-side builtin tools that call the ImageGenerationProvider.
Routed via ToolRoute.IMAGE in the tool router.
"""

from __future__ import annotations

import json
import mimetypes
from typing import Any

import httpx

from cognis.models.config import ImageGenerationResult
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource
from cognis.runtime_context import current_user_email
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
        "Edit an existing image using a text prompt. Provide the image ID "
        "(img_* format) of a previously generated or uploaded image and a "
        "description of the desired changes."
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
                    "Image ID (img_* format) of the image to edit. "
                    "Must be a previously generated or uploaded image."
                ),
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
        "required": ["prompt", "image"],
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
        image_ref = arguments.get("image", "")
        if not image_ref:
            return ToolResult(output="Error: image ID is required for editing.", is_error=True)

        # Resolve image ID to base64 from artifact store
        if not image_ref.startswith("img_"):
            return ToolResult(
                output="Error: image must be an image ID (img_* format), not raw base64.",
                is_error=True,
            )
        if artifact_store is None:
            return ToolResult(
                output="Error: artifact store not available for image resolution.",
                is_error=True,
            )
        try:
            content, _ct = await artifact_store.async_load("images", image_ref, "image")
            import base64

            image_b64 = base64.b64encode(content).decode("ascii")
        except FileNotFoundError:
            return ToolResult(output=f"Error: image {image_ref} not found.", is_error=True)

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

    # Save generated images to artifact store and return IDs
    output_images: list[dict[str, Any]] = []
    outbound_attachments: list[dict[str, Any]] = []
    for img in result.images:
        if artifact_store is not None:
            image_id = artifact_store.generate_id("img")
            try:
                image_bytes = await _image_bytes(img)
                filename = _image_filename(image_id, img.content_type)
                await artifact_store.async_save(
                    "images",
                    image_id,
                    "image",
                    image_bytes,
                    img.content_type,
                    owner_email=current_user_email.get(),
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
                if signed_url:
                    outbound_attachments.append(
                        {
                            "artifact_id": image_id,
                            "url": signed_url,
                            "mime_type": img.content_type,
                            "filename": filename,
                            "size_bytes": len(image_bytes),
                        }
                    )
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
        return await artifact_store.async_get_signed_url("images", image_id, "image")
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
