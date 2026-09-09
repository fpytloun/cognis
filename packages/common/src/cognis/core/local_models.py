"""Validation and state-machine rules for declarative local models."""

from __future__ import annotations

import re

from cognis.models.local_models import (
    LocalModelOperationState,
    LocalModelSource,
    ParsedLocalModelReference,
)

_OLLAMA_SEGMENT = r"[a-z0-9][a-z0-9._-]{0,95}"
_OLLAMA_TAG = r"[a-z0-9][a-z0-9._-]{0,63}"
_OLLAMA_NATIVE_RE = re.compile(
    rf"^(?:(?:registry\.ollama\.ai|ollama\.com)/)?"
    rf"{_OLLAMA_SEGMENT}(?:/{_OLLAMA_SEGMENT})?(?::({_OLLAMA_TAG}))?$"
)
_LOCAL_MODEL_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)\b(api[_ -]?key|token|password|secret)\s*[=:]\s*[^\s,;]+"),
    re.compile(r"https?://[^\s:@/]+:[^\s@/]+@"),
    re.compile(r"(?i)([?&](?:token|key|api_key|password|secret)=)[^&\s]+"),
)
_HF_RE = re.compile(
    r"^hf\.co/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,95})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,95}):"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)

LOCAL_MODEL_OPERATION_TRANSITIONS: dict[LocalModelOperationState, set[LocalModelOperationState]] = {
    LocalModelOperationState.QUEUED: {
        LocalModelOperationState.RUNNING,
        LocalModelOperationState.CANCEL_REQUESTED,
        LocalModelOperationState.CANCELLED,
        LocalModelOperationState.INTERRUPTED,
    },
    LocalModelOperationState.RUNNING: {
        LocalModelOperationState.CANCEL_REQUESTED,
        LocalModelOperationState.SUCCEEDED,
        LocalModelOperationState.FAILED,
        LocalModelOperationState.INTERRUPTED,
    },
    LocalModelOperationState.CANCEL_REQUESTED: {
        LocalModelOperationState.SUCCEEDED,
        LocalModelOperationState.FAILED,
        LocalModelOperationState.CANCELLED,
        LocalModelOperationState.INTERRUPTED,
    },
    LocalModelOperationState.INTERRUPTED: {
        LocalModelOperationState.QUEUED,
        LocalModelOperationState.CANCEL_REQUESTED,
        LocalModelOperationState.SUCCEEDED,
        LocalModelOperationState.FAILED,
        LocalModelOperationState.CANCELLED,
    },
    LocalModelOperationState.SUCCEEDED: set(),
    LocalModelOperationState.FAILED: set(),
    LocalModelOperationState.CANCELLED: set(),
}


def parse_local_model_reference(raw_reference: str) -> ParsedLocalModelReference:
    """Validate and canonicalize an Ollama-native or Hugging Face model reference."""

    requested = raw_reference.strip()
    if not requested or requested != raw_reference or len(requested) > 255:
        raise ValueError("model reference must be a non-empty trimmed value")
    if any(ord(character) < 32 or ord(character) == 127 for character in requested):
        raise ValueError("model reference must not contain control characters")
    if any(character.isspace() for character in requested):
        raise ValueError("model reference must not contain whitespace")
    if "://" in requested or "\\" in requested or requested.startswith(("/", "./", "../")):
        raise ValueError("URLs and filesystem paths are not valid model references")
    if "//" in requested or any(segment in {".", ".."} for segment in requested.split("/")):
        raise ValueError("model reference contains an invalid path segment")
    if any(character in requested for character in {"?", "#", "@", "%"}):
        raise ValueError("model reference contains an unsupported character")

    hf_match = _HF_RE.fullmatch(requested)
    if hf_match is not None:
        organization, repository, quantization = hf_match.groups()
        canonical = f"hf.co/{organization}/{repository}:{quantization}"
        return ParsedLocalModelReference(
            requested_ref=requested,
            canonical_name=canonical,
            runtime_name=canonical,
            source=LocalModelSource.HUGGINGFACE,
            revision=quantization,
        )
    if requested.startswith("hf.co/"):
        raise ValueError("Hugging Face references must use hf.co/org/repo:quant")

    first_segment = requested.split("/", 1)[0]
    if (
        "/" in requested
        and "." in first_segment
        and first_segment not in {"registry.ollama.ai", "ollama.com"}
    ):
        raise ValueError("unknown model registry")
    native_match = _OLLAMA_NATIVE_RE.fullmatch(requested)
    if native_match is None:
        raise ValueError("invalid Ollama model reference")
    revision = native_match.group(1) or "latest"
    canonical = requested if native_match.group(1) else f"{requested}:latest"
    return ParsedLocalModelReference(
        requested_ref=requested,
        canonical_name=canonical,
        runtime_name=canonical,
        source=LocalModelSource.OLLAMA,
        revision=revision,
    )


def validate_local_model_operation_transition(
    current: LocalModelOperationState,
    target: LocalModelOperationState,
) -> None:
    """Raise when a requested durable operation transition is invalid."""

    if target == current:
        return
    if target not in LOCAL_MODEL_OPERATION_TRANSITIONS[current]:
        raise ValueError(f"invalid local-model operation transition: {current} -> {target}")


def sanitize_local_model_error(raw_error: str | None) -> str | None:
    """Return a bounded single-line error safe for persistence and API output."""

    if raw_error is None:
        return None
    normalized = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in raw_error
    )
    for pattern in _LOCAL_MODEL_SECRET_PATTERNS:
        normalized = pattern.sub(
            lambda match: f"{match.group(1)}[redacted]" if match.lastindex else "[redacted]",
            normalized,
        )
    normalized = " ".join(normalized.split())
    return normalized[:1000] or None
