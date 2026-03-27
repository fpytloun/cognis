"""Domain models for agent definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
    avatar_url: str | None = None
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentPermissions(BaseModel):
    """Agent permission configuration."""

    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    allowed_secrets: list[str] = Field(default_factory=list)
    max_delegation_depth: int = 5
    can_delegate: bool = True


class AgentLLMConfig(BaseModel):
    """Per-agent LLM configuration."""

    model: str | None = None
    provider_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    model_routing: dict[str, str] | None = None
