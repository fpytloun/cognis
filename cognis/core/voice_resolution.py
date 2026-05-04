"""Resolve a TTS voice through the agent → provider → system fallback chain.

This is the canonical resolver. New voice-aware surfaces (notifications,
channel adapters, speaker buttons) MUST go through this helper.
"""

from __future__ import annotations

from typing import Any

# Hard last-resort fallback when no other source is configured. OpenAI's
# ``alloy`` voice is acceptable across openai/openai_compatible/litellm_proxy
# providers and is the documented default for ``tts-1``.
HARD_DEFAULT_VOICE = "alloy"


def resolve_voice(
    *,
    explicit: str | None = None,
    agent_voice: str | None = None,
    provider_default_voice: str | None = None,
    system_default_voice: str | None = None,
) -> str:
    """Return the first non-empty voice from the fallback chain.

    Order: explicit → agent → provider → system → hard default.
    """
    for candidate in (
        explicit,
        agent_voice,
        provider_default_voice,
        system_default_voice,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return HARD_DEFAULT_VOICE


def agent_voice_from_definition(agent: Any) -> str | None:
    """Extract the per-agent voice override from an ``AgentDefinition``-like object."""
    llm_config = getattr(agent, "llm_config", None)
    if llm_config is None:
        return None
    voice = getattr(llm_config, "voice", None)
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return None


def provider_default_voice_from_config(provider_config: Any) -> str | None:
    """Extract the provider-level default voice from an ``LLMProviderConfig``-like object."""
    if provider_config is None:
        return None
    voice: Any
    if isinstance(provider_config, dict):
        voice = provider_config.get("default_voice")
    else:
        voice = getattr(provider_config, "default_voice", None)
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return None
