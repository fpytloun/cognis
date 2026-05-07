"""Audio preprocessing for STT pipelines.

Lifted from ``cognis/channels/inbound.py`` so the channel inbound flow and
the new web STT route share one implementation.

These helpers normalize incoming audio (any browser/channel format) into a
MIME type and filename a transcription provider will accept. Formats already
in the supported set pass through; unsupported formats are converted to WAV
via ``ffmpeg``.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from cognis.logging import get_logger

logger = get_logger(__name__)


STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES: dict[str, tuple[str, str]] = {
    "audio/mpeg": ("audio/mpeg", ".mp3"),
    "audio/mp3": ("audio/mpeg", ".mp3"),
    "audio/mp4": ("audio/mp4", ".m4a"),
    "audio/x-m4a": ("audio/mp4", ".m4a"),
    "audio/wav": ("audio/wav", ".wav"),
    "audio/x-wav": ("audio/wav", ".wav"),
    "audio/webm": ("audio/webm", ".webm"),
    "audio/ogg": ("audio/ogg", ".ogg"),
    "audio/oga": ("audio/ogg", ".oga"),
    "audio/flac": ("audio/flac", ".flac"),
}

_STT_AUDIO_EXTENSION_BY_MIME: dict[str, tuple[str, str]] = {
    **STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES,
    "audio/aac": ("audio/aac", ".aac"),
    "audio/x-aac": ("audio/aac", ".aac"),
    "audio/opus": ("audio/opus", ".opus"),
    "audio/x-opus": ("audio/opus", ".opus"),
    "audio/amr": ("audio/amr", ".amr"),
}

_STT_TRANSCODE_TARGET_MIME = "audio/wav"
_STT_TRANSCODE_TARGET_EXTENSION = ".wav"


def normalized_audio_filename(filename: str, default_stem: str = "attachment") -> str:
    """Return a sanitized filename, falling back to ``default_stem`` if empty."""
    candidate = Path(filename).name if filename else default_stem
    if candidate:
        return candidate
    return default_stem


def normalize_audio_mime_type(mime_type: str | None) -> str:
    """Return a lower-case audio MIME type without parameters."""

    if not isinstance(mime_type, str):
        return ""
    return mime_type.split(";", 1)[0].strip().lower()


def stt_supported_audio_mime_types(
    *,
    model: str | None = None,
    model_info: Any | None = None,
) -> list[str]:
    """Return the list of MIME types the STT provider/model accepts."""
    configured = getattr(model_info, "supported_audio_mime_types", None)
    if isinstance(configured, list) and configured:
        return [str(item).strip().lower() for item in configured if str(item).strip()]
    return sorted(STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES)


def _stt_passthrough_target(
    mime_type: str,
    filename: str,
    *,
    supported_mime_types: list[str] | None = None,
) -> tuple[str, str] | None:
    supported = {item.strip().lower() for item in (supported_mime_types or []) if item.strip()}
    supported_map = (
        {
            mime: _STT_AUDIO_EXTENSION_BY_MIME.get(mime, (mime, Path(filename).suffix or ".bin"))
            for mime in supported
        }
        if supported
        else STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES
    )
    normalized = supported_map.get(normalize_audio_mime_type(mime_type))
    if normalized is None:
        return None
    normalized_mime, extension = normalized
    path = Path(normalized_audio_filename(filename))
    if path.suffix.lower() == extension:
        return normalized_mime, path.name
    return normalized_mime, f"{path.stem or 'attachment'}{extension}"


async def transcode_audio_for_stt(
    content: bytes,
    *,
    mime_type: str,
    filename: str,
) -> tuple[bytes, str, str]:
    """Run ``ffmpeg`` to convert *content* to WAV.

    Raises ``RuntimeError`` with a user-facing message when ffmpeg is missing
    or fails. Callers can surface the message verbatim.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "audio preprocessing: ffmpeg required but unavailable",
            extra={"extra_data": {"mime_type": mime_type, "filename": filename}},
        )
        raise RuntimeError(
            "I couldn't transcribe that voice message because its audio format requires ffmpeg "
            "conversion and ffmpeg is not installed on the host. Install ffmpeg or send "
            "MP3/M4A/WAV/OGG/WebM audio."
        )

    input_suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="cognis_stt_") as tmp_dir:
        input_path = Path(tmp_dir) / f"input{input_suffix}"
        output_path = Path(tmp_dir) / f"output{_STT_TRANSCODE_TARGET_EXTENSION}"
        input_path.write_bytes(content)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists():
            logger.warning(
                "audio preprocessing: ffmpeg failed to normalize audio",
                extra={
                    "extra_data": {
                        "mime_type": mime_type,
                        "filename": filename,
                        "returncode": proc.returncode,
                    }
                },
            )
            raise RuntimeError(
                "I couldn't transcribe that voice message because its audio format could not be "
                "converted. The recording may be corrupted, empty, or unsupported."
            )
        return output_path.read_bytes(), _STT_TRANSCODE_TARGET_MIME, "voice-input.wav"


async def prepare_audio_for_stt(
    content: bytes,
    *,
    mime_type: str,
    filename: str,
    supported_mime_types: list[str] | None = None,
) -> tuple[bytes, str, str]:
    """Return ``(audio_bytes, mime_type, filename)`` ready for STT.

    Pass-through when the input MIME is already supported; otherwise
    transcode via ``ffmpeg``.
    """
    passthrough = _stt_passthrough_target(
        mime_type,
        filename,
        supported_mime_types=supported_mime_types,
    )
    if passthrough is not None:
        normalized_mime, normalized_filename = passthrough
        return content, normalized_mime, normalized_filename
    return await transcode_audio_for_stt(content, mime_type=mime_type, filename=filename)
