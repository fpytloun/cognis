"""Domain models for agent definitions."""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, Field

from cognis.logging import get_logger
from cognis.models.tool import Permission

logger = get_logger(__name__)


class AgentDefinition(BaseModel):
    """Agent definition as stored in the database."""

    agent_id: str
    owner_email: str
    name: str
    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    personality: dict[str, Any] | None = None
    skills: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    permissions: AgentPermissions | None = None
    llm_config: AgentLLMConfig | None = None
    execution: dict[str, Any] | None = None
    avatar_url: str | None = None  # deprecated — computed from avatar_image_id
    avatar_image_id: str | None = None
    # Type system
    agent_type: str = "primary"  # "primary" | "secondary"
    is_system: bool = False
    hidden: bool = False
    allow_user_override: bool = Field(default=False, exclude=True)
    allow_user_disable: bool = Field(default=False, exclude=True)
    editable_fields: list[str] = Field(default_factory=list, exclude=True)
    has_overrides: bool = Field(default=False, exclude=True)
    disabled: bool = Field(default=False, exclude=True)
    # Metadata
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def compose_personality(self) -> str | None:
        """Compose structured personality fields into a text block.

        Returns a formatted string with purpose, tone, temperament, and
        behavioral rules.  Returns ``None`` if *personality* is ``None``
        or contains no non-empty fields.
        """
        if not self.personality:
            return None
        p = self.personality

        def _clean_text(value: object) -> str | None:
            return value.strip() if isinstance(value, str) and value.strip() else None

        def _clean_rules(value: object) -> list[str]:
            if not isinstance(value, list):
                return []
            return [rule.strip() for rule in value if isinstance(rule, str) and rule.strip()]

        lines: list[str] = []
        purpose = _clean_text(p.get("purpose"))
        tone = _clean_text(p.get("tone"))
        temperament = _clean_text(p.get("temperament"))
        rules = _clean_rules(p.get("behavioral_rules"))
        if purpose:
            lines.append(f"Purpose: {purpose}")
        if tone:
            lines.append(f"Tone: {tone}")
        if temperament:
            lines.append(f"Temperament: {temperament}")
        if rules:
            lines.append("Behavioral rules:\n" + "\n".join(f"- {r}" for r in rules))
        return "\n".join(lines) if lines else None


class AgentPermissions(BaseModel):
    """Agent permission configuration."""

    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    tool_permissions: dict[str, Permission] | None = None
    allowed_secrets: list[str] = Field(default_factory=list)
    allowed_credentials: list[str] = Field(default_factory=list)
    max_delegation_depth: int = 5
    can_delegate: bool = True

    def resolve_permission(self, tool_name: str, *, tool_id: str | None = None) -> Permission:
        """Resolve permission for a tool using new rules, then legacy fallback."""

        if self.tool_permissions:
            if self.allowed_tools or self.denied_tools:
                logger.warning(
                    "AgentPermissions uses both tool_permissions and legacy tool lists; tool_permissions take precedence"
                )
            if tool_id and tool_id in self.tool_permissions:
                return self.tool_permissions[tool_id]
            return _resolve_from_map(self.tool_permissions, tool_name)
        if _matches_any(tool_name, self.denied_tools):
            return Permission.DENY
        if _matches_any(tool_name, self.allowed_tools):
            return Permission.ALLOW
        return Permission.EVALUATE


class AgentLLMConfig(BaseModel):
    """Per-agent LLM configuration."""

    model: str | None = None
    provider_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    model_routing: dict[str, str] | None = None


def _matches_any(tool_name: str, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns)


def _resolve_from_map(tool_permissions: dict[str, Permission], tool_name: str) -> Permission:
    if tool_name in tool_permissions:
        return tool_permissions[tool_name]

    matches = [
        (pattern, permission)
        for pattern, permission in tool_permissions.items()
        if pattern != tool_name and fnmatchcase(tool_name, pattern)
    ]
    if matches:
        pattern, permission = max(matches, key=lambda item: len(item[0]))
        if pattern == "*":
            return permission
        return permission
    return Permission.EVALUATE
