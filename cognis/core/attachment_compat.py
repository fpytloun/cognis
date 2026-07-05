"""Attachment compatibility helpers for provider-native model input."""

from __future__ import annotations

import mimetypes
from typing import Any

DEFAULT_NATIVE_IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    }
)

_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
}


def normalize_mime_type(value: object) -> str | None:
    """Return a normalized MIME type, ignoring parameters."""

    if not isinstance(value, str):
        return None
    mime_type = value.split(";", 1)[0].strip().lower()
    if not mime_type:
        return None
    return _MIME_ALIASES.get(mime_type, mime_type)


def infer_mime_type(value: object, *, filename: object = None) -> str | None:
    """Return a normalized MIME type from explicit metadata or filename."""

    mime_type = normalize_mime_type(value)
    if mime_type is not None:
        return mime_type
    if isinstance(filename, str) and filename.strip():
        guessed, _encoding = mimetypes.guess_type(filename)
        return normalize_mime_type(guessed)
    return None


def native_image_mime_types(model_info: Any) -> frozenset[str]:
    """Return MIME types accepted for provider-native image input.

    The model capability flag says whether image input exists at all; this
    helper describes which image encodings are safe to project into native
    model message parts.  Providers may override the default with
    ``supported_image_mime_types`` when they explicitly support additional
    formats.
    """

    configured = getattr(model_info, "supported_image_mime_types", None)
    if isinstance(configured, list | tuple | set | frozenset):
        values = {
            normalized
            for item in configured
            if (normalized := normalize_mime_type(item)) is not None
        }
        if values:
            return frozenset(values)
    return DEFAULT_NATIVE_IMAGE_MIME_TYPES


def supports_native_image_input(
    model_info: Any,
    mime_type: object,
    *,
    filename: object = None,
) -> bool:
    """Return whether a model should receive this image as native input."""

    if not bool(getattr(model_info, "supports_vision", False)):
        return False
    normalized = infer_mime_type(mime_type, filename=filename)
    if normalized is None:
        return False
    return normalized in native_image_mime_types(model_info)
