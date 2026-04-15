"""Image serving, upload, and generation routes."""

from __future__ import annotations

import base64
from typing import Any

import httpx
from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from cognis.api.common import api_exception, forbid_mutation_for_viewer, require_current_user
from cognis.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/images", tags=["images"])

_ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
_MAX_UPLOAD_SIZE = 2 * 1024 * 1024  # 2 MB


class ImageGenerateRequest(BaseModel):
    """Request body for image generation."""

    prompt: str
    size: str = "1024x1024"
    quality: str | None = None


class AvatarPromptRequest(BaseModel):
    """Request body for avatar prompt generation."""

    name: str = ""
    description: str = ""
    personality: dict[str, Any] | None = None


@router.get("/{image_id}")
async def serve_image(request: Request, image_id: str) -> Response:
    """Serve an image from the artifact store.

    Validates ownership: the requesting user must own the image or be admin.
    Returns 404 for non-existent or non-owned images (anti-enumeration).
    """
    user = require_current_user(request)
    artifact_store = _get_artifact_store(request)

    # Try loading metadata to check ownership
    meta = await artifact_store.async_load_metadata("avatars", image_id, "image")
    if meta is None:
        # Also check "images" namespace (tool-generated images)
        meta = await artifact_store.async_load_metadata("images", image_id, "image")
        if meta is None:
            raise api_exception(404, "not_found", "Image not found")

    # Ownership check (admin bypasses)
    if meta.owner_email and meta.owner_email != user.email and getattr(user, "role", "") != "admin":
        raise api_exception(404, "not_found", "Image not found")

    # Determine namespace
    namespace = "avatars"
    if not await artifact_store.async_exists("avatars", image_id, "image"):
        namespace = "images"

    try:
        content, content_type = await artifact_store.async_load(namespace, image_id, "image")
    except FileNotFoundError:
        raise api_exception(404, "not_found", "Image not found") from None

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Length": str(len(content)),
        },
    )


@router.post("/upload")
async def upload_image(request: Request, file: UploadFile) -> dict[str, str]:
    """Upload an image file.

    Accepts PNG, JPEG, or WebP images up to 2MB.
    Returns the image_id and URL for use in agent avatar or other contexts.
    """
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    artifact_store = _get_artifact_store(request)

    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise api_exception(
            400,
            "validation_error",
            f"Unsupported image type: {file.content_type}. Allowed: PNG, JPEG, WebP.",
        )

    content = await file.read()
    if len(content) > _MAX_UPLOAD_SIZE:
        raise api_exception(
            400,
            "validation_error",
            f"Image too large ({len(content)} bytes). Maximum: {_MAX_UPLOAD_SIZE} bytes.",
        )

    image_id = artifact_store.generate_id("img")
    await artifact_store.async_save(
        "avatars",
        image_id,
        "image",
        content,
        file.content_type or "image/png",
        owner_email=user.email,
    )

    return {"image_id": image_id, "url": f"/api/v1/images/{image_id}"}


@router.post("/generate")
async def generate_image(request: Request, payload: ImageGenerateRequest) -> dict[str, Any]:
    """Generate an image from a text prompt using the configured image generation model.

    Returns the image_id and URL of the generated image.
    """
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    artifact_store = _get_artifact_store(request)
    image_gen = _get_image_generation_provider(request)

    try:
        result = await image_gen.image_generate(
            prompt=payload.prompt,
            task_type="image_generation",
            n=1,
            size=payload.size,
            quality=payload.quality,
        )
    except Exception as exc:
        logger.warning("Image generation failed", exc_info=True)
        raise api_exception(502, "provider_error", f"Image generation failed: {exc}") from exc

    if not result.images:
        raise api_exception(502, "provider_error", "No images returned by the model")

    # Save the first generated image
    img = result.images[0]
    image_id = artifact_store.generate_id("img")
    try:
        image_bytes = await _image_bytes(img)
    except Exception:
        raise api_exception(502, "provider_error", "Invalid image data from model") from None

    await artifact_store.async_save(
        "avatars",
        image_id,
        "image",
        image_bytes,
        img.content_type,
        owner_email=user.email,
    )

    return {
        "image_id": image_id,
        "url": f"/api/v1/images/{image_id}",
        "prompt_used": img.revised_prompt or payload.prompt,
    }


@router.post("/generate-prompt")
async def generate_avatar_prompt(request: Request, payload: AvatarPromptRequest) -> dict[str, str]:
    """Generate a creative image generation prompt from agent details.

    Accepts agent name, description, personality, and purpose.
    Returns a creative prompt suitable for image generation.
    """
    forbid_mutation_for_viewer(request)
    require_current_user(request)
    llm = request.app.state.providers.llm

    personality = payload.personality or {}
    purpose = personality.get("purpose", "") if isinstance(personality, dict) else ""
    tone = personality.get("tone", "") if isinstance(personality, dict) else ""

    # Build context for prompt generation
    context_parts = [f"Agent name: {payload.name}"]
    if payload.description:
        context_parts.append(f"Description: {payload.description}")
    if purpose:
        context_parts.append(f"Purpose: {purpose}")
    if tone:
        context_parts.append(f"Tone: {tone}")
    context = "\n".join(context_parts)

    system_msg = (
        "You are a creative image prompt generator. Given details about an AI agent, "
        "generate a single creative, detailed prompt for generating a professional "
        "avatar image for this agent. The prompt should describe a visually appealing "
        "avatar that reflects the agent's personality and purpose. "
        "Output ONLY the image generation prompt, nothing else. "
        "Keep it under 200 words. Focus on visual elements: style, colors, mood, "
        "composition. Do not include text or words in the image."
    )

    try:
        response = await llm.generate(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": context},
            ],
            task_type="default",
            temperature=1.0,
            max_tokens=300,
        )
        choices = response.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content")
            prompt = content.strip() if isinstance(content, str) else ""
            if prompt:
                return {"prompt": prompt}
    except Exception:
        logger.warning("Avatar prompt generation failed", exc_info=True)

    # Fallback: generate a simple prompt
    fallback = (
        f"A professional, modern avatar for an AI assistant named '{payload.name}'. "
        f"Clean, minimalist design with a futuristic feel. "
        f"Abstract geometric shapes or a stylized robot/AI face. "
        f"Cool blue and purple color palette. Digital art style."
    )
    return {"prompt": fallback}


def _get_artifact_store(request: Request) -> Any:
    """Get the artifact store from app state."""
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        raise api_exception(503, "service_unavailable", "Artifact store not configured")
    return store


def _get_image_generation_provider(request: Request) -> Any:
    """Get the image generation provider from app state."""
    provider = getattr(request.app.state.providers, "image_generation", None)
    if provider is None:
        raise api_exception(
            503,
            "service_unavailable",
            "Image generation not available. Configure an image generation model in Settings.",
        )
    return provider


async def _image_bytes(img: Any) -> bytes:
    """Resolve generated image data from base64 or URL."""
    if getattr(img, "b64_json", None):
        return base64.b64decode(img.b64_json)
    image_url = getattr(img, "url", None)
    if image_url:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            return response.content
    raise ValueError("Generated image does not contain base64 data or URL")
