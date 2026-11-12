"""Controller speech contracts shared by HTTP, tools, and background jobs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

PodcastLength = Literal["shorter", "default", "longer"]


class PodcastLengthEstimate(BaseModel):
    """A source-derived word budget and approximate playback range, not a promise."""

    length: PodcastLength
    target_words: int
    seconds_min: int
    seconds_max: int


class SpeechRequest(BaseModel):
    """Synthesize supplied text; a message ID is an opaque cache hint, not authority."""

    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=120_000)
    message_id: str | None = Field(default=None, max_length=512)
    agent_id: str | None = None
    voice: str | None = None
    provider_id: str | None = None
    model: str | None = None
    format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    low_latency: bool = False
    use_user_preferences: bool = False
    adaptation: Literal["as_is", "listenable"] = "as_is"


class SpeechPart(BaseModel):
    """One independently playable immutable audio artifact."""

    artifact_id: str
    audio_url: str
    content_type: str
    duration_seconds: float | None = None


class SpeechResponse(BaseModel):
    """The first URL preserves the existing single-part playback contract."""

    audio_url: str
    content_type: str
    duration_seconds: float | None = None
    voice: str
    model: str
    cached: bool = False
    parts: list[SpeechPart] = Field(default_factory=list)
    adapted: bool = False
    script_artifact_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AudioJobCreate(BaseModel):
    """Create audio for one exact content revision, never its mutable head."""

    model_config = {"extra": "forbid"}

    # Retained for existing tools and clients; new document viewers use content_ref.
    deliverable_id: str | None = Field(default=None, pattern=r"^dlv_[0-9a-f]{32}$")
    content_ref: str | None = Field(default=None, pattern=r"^(dlv|art|att|doc|img)_[0-9a-f]{32}$")
    mode: Literal["narration", "podcast"] = "narration"
    idempotency_key: str = Field(min_length=1, max_length=128)
    voice: str | None = None
    length: PodcastLength | None = None
    # Explicit minute budgets remain supported for deployed API clients.
    target_minutes: int | None = Field(default=None, ge=2, le=20)

    @model_validator(mode="after")
    def validate_source(self) -> AudioJobCreate:
        if not (self.content_ref or self.deliverable_id):
            raise ValueError("An exact document content_ref is required.")
        if self.content_ref and self.deliverable_id and self.content_ref != self.deliverable_id:
            raise ValueError("content_ref and deliverable_id must identify the same revision.")
        if self.length is not None and self.target_minutes is not None:
            raise ValueError("Choose a relative length or target_minutes, not both.")
        return self

    @property
    def source_ref(self) -> str:
        """Resolve the supported legacy request field at the API boundary."""
        ref = self.content_ref or self.deliverable_id
        assert ref is not None
        return ref


class SpeechCapabilities(BaseModel):
    """Configuration-only preflight; does not issue a paid speech request."""

    tts: bool
    stt: bool
    streaming_tts: bool = False
    streaming_stt: bool = False
    stt_sample_rate: int = 24_000
    detail: str | None = None


class SpeechVoice(BaseModel):
    """A provider-scoped, selectable voice identifier."""

    voice_id: str
    name: str


class SpeechVoices(BaseModel):
    """Voice discovery bound to the current authorized TTS route."""

    provider_id: str
    model: str
    default_voice: str
    voices: list[SpeechVoice]


class AudioJobView(BaseModel):
    """Durable progress with freshly signed URLs only after authorization."""

    job_id: str
    deliverable_id: str
    content_ref: str | None = None
    mode: str
    length: PodcastLength | None = None
    target_words: int | None = None
    status: str
    completed_parts: int = 0
    total_parts: int = 0
    parts: list[SpeechPart] = Field(default_factory=list)
    script_artifact_id: str | None = None
    script_url: str | None = None
    error_code: str | None = None
    created_at: str
    warnings: list[str] = Field(default_factory=list)


class DocumentAudioContext(BaseModel):
    """Source capabilities resolved by the authenticated or bounded public transport."""

    supported: bool = True
    can_estimate: bool = False
    can_generate: bool = False
    can_cancel: bool = False
    detail: str | None = None
    jobs: list[AudioJobView] = Field(default_factory=list)
