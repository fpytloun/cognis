"""Unit tests for voice mode (TTS / STT) plumbing.

Covers the pure-Python pieces that do not need a running app:

- Voice resolution fallback chain.
- Sentence buffer boundary detection / markdown stripping / code block skip.
- ``TextToSpeechResult`` round-trip and ``ModelRoutingPolicy.text_to_speech``.
- Audio preprocessing helper passthrough behavior.
"""

from __future__ import annotations

from cognis.audio.preprocessing import (
    STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES,
    normalized_audio_filename,
    stt_supported_audio_mime_types,
)
from cognis.core.sentence_buffer import SentenceBuffer, strip_markdown_for_tts
from cognis.core.voice_resolution import (
    HARD_DEFAULT_VOICE,
    agent_voice_from_definition,
    provider_default_voice_from_config,
    resolve_voice,
)
from cognis.models.agent import AgentDefinition, AgentLLMConfig
from cognis.models.config import LLMProviderConfig, ModelRoutingPolicy, TextToSpeechResult

# ---------------------------------------------------------------------------
# Voice resolution
# ---------------------------------------------------------------------------


def test_resolve_voice_explicit_wins() -> None:
    assert (
        resolve_voice(
            explicit="custom",
            agent_voice="agent",
            provider_default_voice="provider",
            system_default_voice="system",
        )
        == "custom"
    )


def test_resolve_voice_falls_back_through_chain() -> None:
    assert resolve_voice(agent_voice="agent") == "agent"
    assert resolve_voice(provider_default_voice="provider") == "provider"
    assert resolve_voice(system_default_voice="system") == "system"
    assert resolve_voice() == HARD_DEFAULT_VOICE


def test_resolve_voice_treats_blank_as_missing() -> None:
    assert (
        resolve_voice(
            explicit="   ",
            agent_voice="",
            provider_default_voice=None,
            system_default_voice="nova",
        )
        == "nova"
    )


def test_agent_voice_from_definition() -> None:
    agent = AgentDefinition(
        agent_id="a",
        name="A",
        owner_email="user@example.com",
        llm_config=AgentLLMConfig(voice="ash"),
    )
    assert agent_voice_from_definition(agent) == "ash"
    assert (
        agent_voice_from_definition(AgentDefinition(agent_id="b", name="B", owner_email="x@x.com"))
        is None
    )


def test_provider_default_voice_from_config() -> None:
    cfg = LLMProviderConfig(
        provider_id="p", display_name="P", location="controller", default_voice="echo"
    )
    assert provider_default_voice_from_config(cfg) == "echo"
    assert provider_default_voice_from_config({"default_voice": "verse"}) == "verse"
    assert provider_default_voice_from_config({"default_voice": " "}) is None
    assert provider_default_voice_from_config(None) is None


# ---------------------------------------------------------------------------
# Sentence buffer
# ---------------------------------------------------------------------------


def test_sentence_buffer_emits_sentences_on_boundary() -> None:
    buf = SentenceBuffer()
    assert buf.feed("Hello") == []
    assert buf.feed(" world.") == []
    # Boundary requires whitespace after the terminator.
    out = buf.feed(" Next sentence here. ")
    assert [text for _, text in out] == ["Hello world.", "Next sentence here."]
    # Indices are monotonically increasing.
    assert [idx for idx, _ in out] == [0, 1]


def test_sentence_buffer_flush_returns_trailing() -> None:
    buf = SentenceBuffer()
    buf.feed("Just a thought ")
    flushed = buf.flush()
    assert flushed is not None
    _, text = flushed
    assert text == "Just a thought"


def test_sentence_buffer_skips_code_blocks() -> None:
    buf = SentenceBuffer()
    buf.feed("Here is code: ```python\nprint('hi')\n```")
    out = buf.feed(" Now I am back. ")
    assert any("Now I am back." in text for _, text in out)
    assert not any("print" in text for _, text in out)


def test_sentence_buffer_strips_markdown() -> None:
    buf = SentenceBuffer()
    out = buf.feed("**Bold** and [linked](https://x.com) code `x` here. ")
    assert out
    _, sentence = out[0]
    assert "**" not in sentence
    assert "[" not in sentence and "]" not in sentence
    assert "https://" not in sentence
    assert "`" not in sentence


def test_strip_markdown_helper() -> None:
    assert strip_markdown_for_tts("# Heading") == "Heading"
    assert strip_markdown_for_tts("- bullet item") == "bullet item"
    assert strip_markdown_for_tts("**word**") == "word"


def test_sentence_buffer_min_length_drops_short_fragments() -> None:
    buf = SentenceBuffer()
    out = buf.feed("Mr. ")
    # "Mr." is shorter than the minimum, so it should not be emitted.
    assert out == []


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


def test_text_to_speech_result_round_trip() -> None:
    result = TextToSpeechResult(
        audio_bytes=b"\x00\x01\x02",
        content_type="audio/mpeg",
        model="tts-1",
        voice="alloy",
        duration_seconds=1.25,
    )
    dumped = result.model_dump()
    loaded = TextToSpeechResult.model_validate(dumped)
    assert loaded == result


def test_model_routing_policy_has_text_to_speech_slot() -> None:
    policy = ModelRoutingPolicy()
    assert hasattr(policy, "text_to_speech")
    assert policy.text_to_speech == {}


def test_llm_provider_config_default_voice_persists() -> None:
    cfg = LLMProviderConfig(
        provider_id="p",
        display_name="P",
        location="controller",
        default_voice="ash",
    )
    dumped = cfg.model_dump()
    assert dumped["default_voice"] == "ash"


def test_agent_llm_config_voice_persists() -> None:
    cfg = AgentLLMConfig(voice="verse")
    dumped = cfg.model_dump()
    assert dumped["voice"] == "verse"


# ---------------------------------------------------------------------------
# Audio preprocessing helpers
# ---------------------------------------------------------------------------


def test_normalized_audio_filename_falls_back() -> None:
    assert normalized_audio_filename("foo.mp3") == "foo.mp3"
    assert normalized_audio_filename("") == "attachment"


def test_stt_supported_audio_mime_types_uses_default_when_unset() -> None:
    types = stt_supported_audio_mime_types()
    assert "audio/mpeg" in types
    assert "audio/wav" in types


def test_stt_default_mime_table_has_expected_entries() -> None:
    assert STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES["audio/mpeg"][1] == ".mp3"
    assert STT_DEFAULT_SUPPORTED_AUDIO_MIME_TYPES["audio/webm"][1] == ".webm"
