"""Tests for AgentDefinition.compose_personality() and personality injection."""

from __future__ import annotations

from typing import Any

from cognis.models.agent import AgentDefinition
from cognis.providers.memory.mnemory import MnemoryProvider


def _agent(**kwargs: object) -> AgentDefinition:
    defaults: dict[str, object] = {
        "agent_id": "test-agent",
        "owner_email": "user@example.com",
        "name": "Test Agent",
    }
    defaults.update(kwargs)
    return AgentDefinition(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compose_personality
# ---------------------------------------------------------------------------


class TestComposePersonality:
    def test_none_personality(self) -> None:
        agent = _agent(personality=None)
        assert agent.compose_personality() is None

    def test_empty_dict(self) -> None:
        agent = _agent(personality={})
        assert agent.compose_personality() is None

    def test_empty_fields(self) -> None:
        agent = _agent(personality={"purpose": "", "tone": "", "temperament": ""})
        assert agent.compose_personality() is None

    def test_whitespace_only_fields(self) -> None:
        agent = _agent(personality={"purpose": "   ", "tone": "\t", "temperament": "\n"})
        assert agent.compose_personality() is None

    def test_purpose_only(self) -> None:
        agent = _agent(personality={"purpose": "code review assistant"})
        assert agent.compose_personality() == "Purpose: code review assistant"

    def test_tone_only(self) -> None:
        agent = _agent(personality={"tone": "formal, precise"})
        assert agent.compose_personality() == "Tone: formal, precise"

    def test_temperament_only(self) -> None:
        agent = _agent(personality={"temperament": "patient, methodical"})
        assert agent.compose_personality() == "Temperament: patient, methodical"

    def test_behavioral_rules_only(self) -> None:
        agent = _agent(personality={"behavioral_rules": ["Always cite sources", "Be concise"]})
        expected = "Behavioral rules:\n- Always cite sources\n- Be concise"
        assert agent.compose_personality() == expected

    def test_empty_behavioral_rules(self) -> None:
        agent = _agent(personality={"behavioral_rules": []})
        assert agent.compose_personality() is None

    def test_invalid_behavioral_rules_type_ignored(self) -> None:
        agent = _agent(personality={"behavioral_rules": "Always cite sources"})
        assert agent.compose_personality() is None

    def test_mixed_behavioral_rules_filters_non_strings_and_whitespace(self) -> None:
        agent = _agent(
            personality={"behavioral_rules": ["Always cite sources", "  ", 123, "Be concise"]}
        )
        expected = "Behavioral rules:\n- Always cite sources\n- Be concise"
        assert agent.compose_personality() == expected

    def test_full_personality(self) -> None:
        agent = _agent(
            personality={
                "purpose": "research specialist",
                "tone": "casual, witty",
                "temperament": "bold, decisive",
                "behavioral_rules": ["Always verify facts", "Prefer primary sources"],
            }
        )
        result = agent.compose_personality()
        assert result is not None
        assert result.startswith("Purpose: research specialist")
        assert "Tone: casual, witty" in result
        assert "Temperament: bold, decisive" in result
        assert "- Always verify facts" in result
        assert "- Prefer primary sources" in result

    def test_partial_fields(self) -> None:
        agent = _agent(personality={"purpose": "helper", "temperament": "calm"})
        result = agent.compose_personality()
        assert result is not None
        assert "Purpose: helper" in result
        assert "Temperament: calm" in result
        assert "Tone:" not in result
        assert "Behavioral rules:" not in result

    def test_field_order(self) -> None:
        """Purpose, tone, temperament, rules — in that order."""
        agent = _agent(
            personality={
                "temperament": "calm",
                "tone": "formal",
                "purpose": "helper",
                "behavioral_rules": ["rule1"],
            }
        )
        result = agent.compose_personality()
        assert result is not None
        lines = result.split("\n")
        purpose_idx = next(i for i, line in enumerate(lines) if line.startswith("Purpose:"))
        tone_idx = next(i for i, line in enumerate(lines) if line.startswith("Tone:"))
        temp_idx = next(i for i, line in enumerate(lines) if line.startswith("Temperament:"))
        rules_idx = next(i for i, line in enumerate(lines) if line.startswith("Behavioral rules:"))
        assert purpose_idx < tone_idx < temp_idx < rules_idx


# ---------------------------------------------------------------------------
# Context assembly: identity message composition
# ---------------------------------------------------------------------------


class TestIdentityComposition:
    """Verify that personality + system_prompt compose correctly for context."""

    def test_personality_and_system_prompt(self) -> None:
        agent = _agent(
            personality={"purpose": "helper", "tone": "casual"},
            system_prompt="Be helpful.",
        )
        personality = agent.compose_personality()
        parts = [p for p in [personality, agent.system_prompt] if p]
        identity = "\n\n".join(parts)
        assert identity.startswith("Purpose: helper")
        assert "Tone: casual" in identity
        assert identity.endswith("Be helpful.")

    def test_personality_only_no_system_prompt(self) -> None:
        agent = _agent(
            personality={"purpose": "reviewer"},
            system_prompt=None,
        )
        personality = agent.compose_personality()
        parts = [p for p in [personality, agent.system_prompt] if p]
        identity = "\n\n".join(parts)
        assert identity == "Purpose: reviewer"

    def test_system_prompt_only_no_personality(self) -> None:
        agent = _agent(
            personality=None,
            system_prompt="You are a code reviewer.",
        )
        personality = agent.compose_personality()
        parts = [p for p in [personality, agent.system_prompt] if p]
        identity = "\n\n".join(parts)
        assert identity == "You are a code reviewer."

    def test_neither_personality_nor_system_prompt(self) -> None:
        agent = _agent(personality=None, system_prompt=None)
        personality = agent.compose_personality()
        parts = [p for p in [personality, agent.system_prompt] if p]
        assert parts == []

    def test_bootstrap_content_prefers_personality(self) -> None:
        """bootstrap_agent sends personality when available, falls back to system_prompt."""
        agent = _agent(
            personality={"purpose": "helper"},
            system_prompt="You are helpful.",
        )
        content = agent.compose_personality() or agent.system_prompt
        assert content == "Purpose: helper"

    def test_bootstrap_content_falls_back_to_system_prompt(self) -> None:
        agent = _agent(personality=None, system_prompt="You are helpful.")
        content = agent.compose_personality() or agent.system_prompt
        assert content == "You are helpful."

    def test_bootstrap_content_none_when_both_absent(self) -> None:
        agent = _agent(personality=None, system_prompt=None)
        content = agent.compose_personality() or agent.system_prompt
        assert content is None


class _RecordingMnemoryProvider(MnemoryProvider):
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def add_memory(self, **kwargs: Any) -> str:  # type: ignore[override]
        self.recorded.append(kwargs)
        return "memory-1"


class TestBootstrapAgent:
    def _provider(self) -> _RecordingMnemoryProvider:
        return _RecordingMnemoryProvider()

    async def test_bootstrap_agent_uses_structured_personality(self) -> None:
        provider = self._provider()
        agent = _agent(
            personality={"purpose": "helper", "behavioral_rules": ["Always cite sources"]},
            system_prompt="Fallback prompt",
        )

        await provider.bootstrap_agent(agent)

        assert (
            provider.recorded[0]["content"]
            == "Purpose: helper\nBehavioral rules:\n- Always cite sources"
        )

    async def test_bootstrap_agent_falls_back_to_system_prompt(self) -> None:
        provider = self._provider()
        agent = _agent(personality=None, system_prompt="Fallback prompt")

        await provider.bootstrap_agent(agent)

        assert provider.recorded[0]["content"] == "Fallback prompt"

    async def test_bootstrap_agent_ignores_malformed_rules(self) -> None:
        provider = self._provider()
        agent = _agent(personality={"behavioral_rules": "not-a-list"}, system_prompt="Fallback")

        await provider.bootstrap_agent(agent)

        assert provider.recorded[0]["content"] == "Fallback"

    async def test_bootstrap_agent_skips_empty_identity(self) -> None:
        provider = self._provider()
        agent = _agent(personality={"purpose": "   "}, system_prompt=None)

        await provider.bootstrap_agent(agent)

        assert provider.recorded == []
