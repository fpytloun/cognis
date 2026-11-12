"""Portable contracts for provider speech streams and executor transport."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PCM_SAMPLE_RATE = 24_000
MAX_SPEECH_FRAME_BYTES = 32_768
MAX_SPEECH_INPUT_BYTES = 25 * 1024 * 1024


class SpeechStreamEvent(BaseModel):
    """One ordered observation; only ``done`` confirms provider completion."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["start", "audio", "partial", "transcript", "done"]
    model: str = ""
    voice: str = ""
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: int = Field(default=PCM_SAMPLE_RATE, ge=8_000, le=48_000)
    channels: Literal[1] = 1
    audio_base64: str = ""
    sample_offset: int = Field(default=0, ge=0)
    text: str = ""
    utterance_id: str = ""
    duration_seconds: float | None = Field(default=None, ge=0)


class SpeechStreamingCapabilities(BaseModel):
    """Native streaming support, independent of buffered speech support."""

    tts: bool = False
    stt: bool = False
    sample_rate: int = PCM_SAMPLE_RATE


def speech_streaming_capabilities(
    provider_preset: str | None, model: str
) -> SpeechStreamingCapabilities:
    """Recognize only native protocols with a characterized wire contract."""
    name = model.rsplit("/", 1)[-1]
    if provider_preset == "elevenlabs":
        return SpeechStreamingCapabilities(
            tts=name.startswith("eleven_"),
            stt=name == "scribe_v2_realtime",
            sample_rate=16_000 if name == "scribe_v2_realtime" else PCM_SAMPLE_RATE,
        )
    if provider_preset == "openai":
        return SpeechStreamingCapabilities(
            tts=name in {"tts-1", "tts-1-hd"} or name.startswith("gpt-4o-mini-tts"),
            stt=name.startswith(("gpt-4o-transcribe", "gpt-4o-mini-transcribe"))
            and "diarize" not in name
            or name in {"gpt-live-transcribe", "gpt-transcribe"},
        )
    return SpeechStreamingCapabilities()
