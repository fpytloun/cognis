"""Domain models for configuration, LLM providers, and model routing."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

NORMALIZED_REASONING_LEVELS: tuple[str, ...] = (
    "default",
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

LEGACY_REASONING_LEVEL_ALIASES: dict[str, str] = {
    "minimal": "low",
}


def normalize_reasoning_level(value: str | None) -> str | None:
    """Return the canonical Cognis thinking-effort level or ``None``."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"off", "reset"}:
        return "default"
    normalized = LEGACY_REASONING_LEVEL_ALIASES.get(normalized, normalized)
    if normalized in NORMALIZED_REASONING_LEVELS:
        return normalized
    return None


class UserRole(StrEnum):
    """User roles for authorization."""

    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"
    SERVICE = "service"


class LLMProviderConfig(BaseModel):
    """A configured LLM provider."""

    provider_id: str
    display_name: str
    location: str  # "controller" | "executor"
    backend: str = "litellm"  # "litellm" | "direct" | "passthrough" | "executor"
    litellm_provider: str | None = None
    sdk: str | None = None
    api_base: str | None = None
    api_key_secret: str | None = None
    executor_labels: dict[str, str] | None = None
    models: list[ModelInfo] = Field(default_factory=list)
    default_model: str | None = None
    default_voice: str | None = None  # Provider-level fallback voice for TTS
    status: str = "active"


class ModelInfo(BaseModel):
    """A model exposed by a provider."""

    model_id: str
    display_name: str | None = None
    context_window: int = 250000
    max_output_tokens: int = 16384
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_pdf_input: bool = False
    supports_file_input: bool = False
    supports_reasoning: bool = False
    reasoning_efforts: list[str] = Field(default_factory=list)
    supports_prompt_caching: bool = False
    supports_tool_search: bool = False
    supports_defer_loading: bool = False
    supports_responses_api: bool = False
    supports_extended_thinking: bool = False
    supports_openai_namespace_tools: bool = False
    supports_openai_allowed_tools: bool = False
    supports_openai_apply_patch: bool = False
    supports_image_generation: bool = False
    supported_audio_mime_types: list[str] = Field(default_factory=list)
    supported_openai_params: list[str] = Field(default_factory=list)
    max_tools: int | None = None
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    tier: str = "standard"


DEFAULT_MODEL_INFO = ModelInfo(
    model_id="unknown",
    display_name="Unknown model",
    context_window=8192,
    max_output_tokens=4096,
)


class ModelRoutingPolicy(BaseModel):
    """Which models for which task types."""

    default: dict[str, str | None] = Field(default_factory=dict)
    classifier: dict[str, str | None] = Field(default_factory=dict)
    compaction: dict[str, str | None] = Field(default_factory=dict)
    evaluator: dict[str, str | None] = Field(default_factory=dict)
    speech_to_text: dict[str, str | None] = Field(default_factory=dict)
    text_to_speech: dict[str, str | None] = Field(default_factory=dict)
    image_generation: dict[str, str | None] = Field(default_factory=dict)
    attachment_analysis: dict[str, str | None] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    """Health status for a single provider."""

    name: str
    status: str  # "healthy", "degraded", "unhealthy", "unknown"
    latency_ms: float | None = None
    circuit_state: str | None = None  # "closed", "open", "half_open"
    error: str | None = None
    details: dict[str, Any] | None = None


class TokenUsage(BaseModel):
    """Token usage from an LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Cost(BaseModel):
    """Cost of an LLM call."""

    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    model: str = ""
    provider: str = ""


class SpeechToTextResult(BaseModel):
    """Result of a speech-to-text transcription call."""

    text: str
    model: str
    language: str | None = None
    duration_seconds: float | None = None


class TextToSpeechResult(BaseModel):
    """Result of a text-to-speech synthesis call."""

    audio_bytes: bytes
    content_type: str
    model: str
    voice: str
    duration_seconds: float | None = None


class GeneratedImage(BaseModel):
    """A single generated image."""

    b64_json: str | None = None
    url: str | None = None
    content_type: str = "image/png"
    revised_prompt: str | None = None


class ImageGenerationResult(BaseModel):
    """Result of an image generation call."""

    images: list[GeneratedImage]
    model: str
    usage: TokenUsage | None = None
