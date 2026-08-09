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

from cognis.models.config import ImageGenerationResult, ImageInput
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource
from cognis.runtime_context import current_user_email
from cognis.store.queries import create_artifact_record
from cognis.tools.argument_normalization import strip_empty_optional_values

_SOURCE = ToolSource(type="builtin")

IMAGE_GENERATE_TOOL = ToolDefinition(
    name="image_generate",
    description=(
        "Generate an image from a text prompt, optionally using ordered image "
        "artifacts as visual references. Describe each reference's intended use "
        "in the prompt. Returns a JSON object with the generated image ID and URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed text description of the image to generate.",
            },
            "references": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": (
                    "Optional ordered image or artifact IDs to use as visual references. "
                    "Describe each reference's intended use in the prompt."
                ),
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
        "Edit or combine one or more existing images using a text prompt. "
        "Provide ordered image or artifact IDs and describe how each should be used."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Description of the desired changes to the image.",
            },
            "images": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": (
                    "Ordered image or artifact IDs supplied to the model. "
                    "Describe how each image should be used in the prompt."
                ),
            },
            "mask_artifact_id": {
                "type": "string",
                "description": (
                    "Optional image mask artifact. Supported only by models/providers "
                    "that offer masked editing."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional model to use for editing.",
            },
            "size": {
                "type": "string",
                "description": "Output image size. Model-dependent.",
            },
            "quality": {
                "type": "string",
                "description": "Image quality. Model-dependent.",
            },
            "n": {
                "type": "integer",
                "description": "Number of images to generate (default 1).",
                "default": 1,
            },
        },
        "required": ["prompt", "images"],
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
    user_email: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """Handle an image generation or edit tool call.

    Returns a JSON string with image details. If an artifact store is
    available, the generated image is saved and an image_id + URL are
    returned. Otherwise, the raw base64 data is returned.
    """
    array_argument_name = "images" if tool_name == "image_edit" else "references"
    supplied_empty_images = arguments.get(array_argument_name) == []
    arguments = _normalize_image_arguments(tool_name, arguments)
    prompt = arguments.get("prompt", "")
    if not prompt:
        return ToolResult(output="Error: prompt is required.", is_error=True)

    model = arguments.get("model")
    size = arguments.get("size")
    quality = arguments.get("quality")
    n = arguments.get("n", 1)
    image_ids = (
        arguments.get("images") if tool_name == "image_edit" else arguments.get("references")
    )
    argument_name = "images" if tool_name == "image_edit" else "references"
    if supplied_empty_images:
        return ToolResult(
            output=f"Error: {argument_name} must contain at least one image artifact ID.",
            is_error=True,
        )
    try:
        images_b64 = await _resolve_image_artifacts_to_b64(
            image_ids,
            artifact_store=artifact_store,
            session_factory=session_factory,
            argument_name=argument_name,
            user_email=user_email,
            runtime_metadata=runtime_metadata,
        )
        mask_artifact_id = arguments.get("mask_artifact_id")
        mask_b64 = (
            await _resolve_image_artifact_to_b64(
                mask_artifact_id,
                artifact_store=artifact_store,
                session_factory=session_factory,
                argument_name="mask_artifact_id",
                user_email=user_email,
                runtime_metadata=runtime_metadata,
            )
            if mask_artifact_id is not None
            else None
        )
    except ImageSourceError as exc:
        return ToolResult(output=f"Error: {exc}", is_error=True)
    if image_ids is not None and not images_b64:
        return ToolResult(
            output=f"Error: {argument_name} must contain at least one image artifact ID.",
            is_error=True,
        )

    try:
        result: ImageGenerationResult = await image_generation_provider.image_generate(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            images=images_b64,
            mask=mask_b64,
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
    normalized = strip_empty_optional_values(arguments, schema)
    if tool_name == "image_generate":
        references = normalized.get("references")
        if isinstance(references, list):
            references = [
                reference
                for reference in references
                if not isinstance(reference, str) or reference.strip()
            ]
            if references:
                normalized["references"] = references
            else:
                normalized.pop("references", None)
    return normalized


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


class ImageSourceError(ValueError):
    pass


async def _resolve_image_artifacts_to_b64(
    artifact_ids: Any,
    *,
    artifact_store: Any | None,
    session_factory: Any | None,
    argument_name: str,
    user_email: str | None,
    runtime_metadata: dict[str, Any] | None,
) -> list[ImageInput] | None:
    if artifact_ids is None:
        return None
    if not isinstance(artifact_ids, list):
        raise ImageSourceError(f"{argument_name} must be an array of image artifact IDs.")

    return [
        await _resolve_image_artifact_to_b64(
            artifact_id,
            artifact_store=artifact_store,
            session_factory=session_factory,
            argument_name=f"{argument_name}[{index}]",
            user_email=user_email,
            runtime_metadata=runtime_metadata,
        )
        for index, artifact_id in enumerate(artifact_ids)
    ]


async def _resolve_image_artifact_to_b64(
    artifact_id: Any,
    *,
    artifact_store: Any | None,
    session_factory: Any | None,
    argument_name: str,
    user_email: str | None,
    runtime_metadata: dict[str, Any] | None,
) -> ImageInput:
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ImageSourceError(f"{argument_name} must be an image artifact ID.")
    if artifact_store is None:
        raise ImageSourceError("Artifact store not available for image resolution.")
    if artifact_id.startswith("tool_artifact:"):
        if session_factory is None:
            raise ImageSourceError(
                f"{argument_name} requires database access for lazy artifact references."
            )
        from cognis.tools.builtin.artifact_tools import materialize_tool_artifact_ref

        resolved = await materialize_tool_artifact_ref(
            artifact_id,
            artifact_store=artifact_store,
            session_factory=session_factory,
            user_email=user_email,
            runtime_metadata=runtime_metadata,
        )
        if resolved.is_error:
            raise ImageSourceError(resolved.output)
        resolved_artifact_id = str((resolved.metadata or {}).get("artifact_id") or "")
        if not resolved_artifact_id:
            raise ImageSourceError(f"{argument_name} could not resolve {artifact_id}.")
        artifact_id = resolved_artifact_id
    if artifact_id.startswith("img_"):
        try:
            content, content_type = await artifact_store.async_load("images", artifact_id, "image")
        except FileNotFoundError as exc:
            raise ImageSourceError(f"{argument_name} image {artifact_id} not found.") from exc
        if not content_type.startswith("image/"):
            raise ImageSourceError(f"{argument_name} must be an image artifact ID.")
        return ImageInput(b64_json=_encode_b64(content), content_type=content_type)
    if session_factory is None:
        raise ImageSourceError(
            f"{argument_name} requires database access for non-image artifact IDs."
        )
    from cognis.store.queries import get_artifact_record

    async with session_factory() as session:
        row = await get_artifact_record(session, artifact_id)
    if row is None or row.status == "deleted":
        raise ImageSourceError(f"{argument_name} artifact {artifact_id} not found.")
    content, content_type = await artifact_store.async_load(
        row.namespace, row.object_id, row.filename
    )
    if not content_type.startswith("image/"):
        raise ImageSourceError(f"{argument_name} must be an image artifact ID.")
    return ImageInput(b64_json=_encode_b64(content), content_type=content_type)


def _encode_b64(content: bytes) -> str:
    import base64

    return base64.b64encode(content).decode("ascii")
