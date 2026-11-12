"""Portable payload preparation for image generation and edit APIs."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from cognis.models.config import ImageInput


def _image_bytes(image: ImageInput) -> bytes:
    try:
        data = base64.b64decode(image.b64_json, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Image input must contain valid base64 data") from exc
    if not data:
        raise ValueError("Image input must not be empty")
    return data


def image_api_kwargs(
    model: str,
    *,
    response_format: str,
    images: list[ImageInput] | None,
    mask: ImageInput | None,
) -> dict[str, Any]:
    """Prepare replayable upload bytes without dropping or reordering inputs.

    Reference-guided generation uses the edit API, just like explicit editing.
    Byte values avoid file-path interpretation and consumed streams on retries.
    """
    if images == []:
        raise ValueError("Image editing requires at least one image")
    if mask is not None and images is None:
        raise ValueError("An image mask requires input images")

    kwargs: dict[str, Any] = {}
    if not model.rsplit("/", 1)[-1].lower().startswith("gpt-image-"):
        kwargs["response_format"] = response_format
    if images is not None:
        kwargs["image"] = [_image_bytes(image) for image in images]
    if mask is not None:
        kwargs["mask"] = _image_bytes(mask)
    return kwargs
