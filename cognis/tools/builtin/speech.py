"""Agent speech tools using the same routed service as interactive playback."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from cognis.core.artifact_revisions import ContentActor
from cognis.core.speech import SpeechService
from cognis.core.speech_models import SpeechRequest
from cognis.models.tool import NativeToolDefinition, ToolResult, ToolSource


class SynthesizeArguments(BaseModel):
    """Explicit agent generation, independent of the human playback preference."""

    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=120_000)
    voice: str | None = None
    format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    speed: float = Field(default=1, ge=0.25, le=4)


class TranscribeArguments(BaseModel):
    model_config = {"extra": "forbid"}

    artifact_id: str = Field(pattern=r"^art_[0-9a-f]{32}$")
    language: str | None = None
    prompt: str | None = Field(default=None, max_length=2000)


_TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "speech_synthesize": (
        SynthesizeArguments,
        "Generate speech from text through the configured text_to_speech route. "
        "Returns ordered durable audio artifact references. Does not play or send audio. "
        "Long text is split into ordered parts. Voice IDs are provider-specific.",
    ),
    "speech_transcribe": (
        TranscribeArguments,
        "Transcribe an authorized audio artifact through the speech_to_text route. "
        "Returns text, detected language and duration when available. Maximum audio size is 25MB.",
    ),
}
SPEECH_TOOL_NAMES = frozenset(_TOOLS)


def speech_tools() -> list[NativeToolDefinition]:
    """Expose one canonical schema per operation to discovery and validation."""
    return [
        NativeToolDefinition(
            name=name,
            description=description,
            parameters=model.model_json_schema(),
            source=ToolSource(type="builtin"),
            category="audio",
            profile_group="development",
            read_only=name == "speech_transcribe",
            timeout_seconds=600,
            side_effects=["Uses a paid speech provider; synthesis creates audio artifacts."],
            examples=[
                {"text": "Read this sentence aloud."}
                if name == "speech_synthesize"
                else {"artifact_id": "art_00000000000000000000000000000000"}
            ],
        )
        for name, (model, description) in _TOOLS.items()
    ]


async def handle_speech_tool(
    name: str, arguments: dict[str, Any], *, service: SpeechService, actor: ContentActor
) -> ToolResult:
    """Apply model validation before inference and preserve the normal result boundary."""
    try:
        if name == "speech_synthesize":
            value = SynthesizeArguments.model_validate(arguments)
            output = await service.synthesize(
                actor, SpeechRequest(**value.model_dump(), agent_id=actor.agent_id), durable=True
            )
        else:
            transcription = TranscribeArguments.model_validate(arguments)
            output = await service.transcribe(actor, **transcription.model_dump())
        data = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
        return ToolResult(output=json.dumps(data, ensure_ascii=False, default=str))
    except PermissionError:
        return ToolResult(output="Audio artifact not found or not accessible.", is_error=True)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)
    except Exception:
        return ToolResult(
            output="Speech provider request failed. Check the configured speech route.",
            is_error=True,
        )
