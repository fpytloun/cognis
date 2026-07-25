"""Domain models for agent definitions."""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from cognis.logging import get_logger
from cognis.models.config import NORMALIZED_REASONING_LEVELS, normalize_reasoning_level
from cognis.models.tool import Permission

logger = get_logger(__name__)

# Guardrails backends do not yet expose provider-owned options.
_KNOWN_GUARDRAILS_BACKENDS = {"intaris", "none"}


class AgentCapabilities(BaseModel):
    """Per-agent backend selection.

    Controls which memory and guardrails backends are used for this agent's
    turns.  Defaults preserve existing behaviour (mnemory + intaris).

    memory_backend:
      "mnemory"  — Mnemory HTTP provider (default)
      "none"     — No memory (NullMemoryProvider); no recall/remember calls

    guardrails_backend:
      "intaris"  — Intaris HTTP provider (default)
      "none"     — No guardrails (NoGuardrailsProvider); all tools auto-approved,
                   including non-bypassable ones.  Intaris is still used as the
                   session/event store regardless of this setting.

    Both fields are open strings (not Literal) so new backends can be added
    without schema changes. Unavailable memory backends are preserved for
    forward-compatible reads and fail closed at runtime.
    """

    memory_backend: str = "mnemory"
    memory_backend_options: dict[str, Any] = Field(default_factory=dict)
    guardrails_backend: str = "intaris"

    @field_validator("memory_backend")
    @classmethod
    def _validate_memory_backend(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("memory_backend must be a non-empty trimmed backend id")
        return value

    @model_validator(mode="after")
    def _validate_memory_options(self) -> AgentCapabilities:
        from cognis.providers.backends import get_backend

        try:
            descriptor = get_backend("memory", self.memory_backend)
        except ValueError:
            # Preserve unavailable future/provider plugin configurations. The
            # runtime resolver treats them as disabled until the provider is
            # installed; API mutation paths reject newly selected unknown ids.
            return self
        if descriptor.memory_options is None:
            if self.memory_backend_options:
                raise ValueError(f"Memory backend {self.memory_backend!r} does not accept options")
            return self
        self.memory_backend_options = descriptor.memory_options.validate_options(
            self.memory_backend_options
        )
        return self

    @field_validator("guardrails_backend")
    @classmethod
    def _validate_guardrails_backend(cls, value: str) -> str:
        if value not in _KNOWN_GUARDRAILS_BACKENDS:
            raise ValueError(
                f"Unknown guardrails_backend {value!r}. "
                f"Known: {sorted(_KNOWN_GUARDRAILS_BACKENDS)}. "
                "Add a new backend in cognis/providers/backends/guardrails/."
            )
        return value

    @property
    def memory_enabled(self) -> bool:
        if self.memory_backend == "none":
            return False
        from cognis.providers.backends import get_backend

        try:
            get_backend("memory", self.memory_backend)
        except ValueError:
            return False
        return True

    @property
    def guardrails_enabled(self) -> bool:
        return self.guardrails_backend != "none"


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
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: object) -> object:
        """Coerce None or empty dict to default AgentCapabilities."""
        if value is None:
            return AgentCapabilities()
        return value

    agent_profiles: dict[str, AgentRuntimeProfile] = Field(default_factory=dict)
    default_agent_profile_id: str | None = None
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
    is_shared_with_me: bool = False
    shared_by_email: str | None = None
    granted_permission: str | None = None
    executor_scope: str | None = None
    is_readonly_for_caller: bool = False
    # Metadata
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("agent_profiles", mode="before")
    @classmethod
    def _none_agent_profiles_are_empty(cls, value: Any) -> Any:
        """Normalize legacy profile config before strict profile validation."""
        if value is None:
            return {}
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for profile_id, profile in value.items():
                if isinstance(profile, dict):
                    normalized[profile_id] = {
                        key: item for key, item in profile.items() if key != "metadata"
                    }
                else:
                    normalized[profile_id] = profile
            return normalized
        return value

    @field_validator("agent_profiles")
    @classmethod
    def _validate_agent_profile_keys(
        cls, value: dict[str, AgentRuntimeProfile]
    ) -> dict[str, AgentRuntimeProfile]:
        for profile_id, profile in value.items():
            if not profile_id.strip() or profile_id != profile_id.strip() or "/" in profile_id:
                raise ValueError(
                    "runtime profile keys must be non-empty, trimmed, and must not contain '/'"
                )
            if profile.profile_id is not None and profile.profile_id != profile_id:
                raise ValueError(
                    f"runtime profile key '{profile_id}' does not match embedded "
                    f"profile_id '{profile.profile_id}'"
                )
        return value

    @model_validator(mode="after")
    def _validate_profile_memory_options(self) -> AgentDefinition:
        from cognis.providers.backends import get_backend

        try:
            descriptor = get_backend("memory", self.capabilities.memory_backend)
        except ValueError:
            return self
        options_provider = descriptor.memory_options
        for profile_id, profile in self.agent_profiles.items():
            if profile.memory_backend_options and options_provider is None:
                raise ValueError(
                    f"Runtime profile {profile_id!r} configures memory options, "
                    f"but backend {self.capabilities.memory_backend!r} does not accept them"
                )
            if options_provider is not None:
                # Validate the effective shallow merge, while preserving only
                # the explicit profile fields in persisted configuration.
                merged = dict(options_provider.defaults)
                merged.update(self.capabilities.memory_backend_options)
                merged.update(profile.memory_backend_options)
                options_provider.validate_options(merged)
        return self

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
    allowed_knowledgebases: list[str] = Field(default_factory=list)
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
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    model_routing: dict[str, str] | None = None
    voice: str | None = None  # Per-agent TTS voice override

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: str | None) -> str | None:
        """Reject reasoning_effort values outside the normalised set."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reasoning_effort must be a string or null")
        normalized = normalize_reasoning_level(value)
        if normalized is None:
            if not value.strip():
                return None
            allowed = ", ".join(NORMALIZED_REASONING_LEVELS)
            raise ValueError(f"reasoning_effort must be one of {allowed}; got {value!r}")
        return normalized


class AgentRuntimeProfile(BaseModel):
    """Per-agent runtime variant for provider/model/reasoning and prompt tuning."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    profile_id: str | None = None
    description: str = ""
    provider_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("provider_id", "provider"),
    )
    model: str | None = None
    reasoning_effort: str | None = Field(
        default=None,
        validation_alias=AliasChoices("reasoning_effort", "thinking_effort"),
    )
    system_prompt_extra: str | None = None
    memory_enabled: bool | None = None
    memory_backend_options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    agent_switchable: bool = False

    @model_validator(mode="before")
    @classmethod
    def _strip_legacy_metadata(cls, value: Any) -> Any:
        """Ignore stale UI/runtime-profile metadata emitted before the schema was tightened."""
        if isinstance(value, dict) and "metadata" in value:
            return {key: item for key, item in value.items() if key != "metadata"}
        return value

    @field_validator("profile_id")
    @classmethod
    def _validate_profile_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if "/" in normalized:
            raise ValueError("agent profile IDs must not contain '/'")
        return normalized

    @field_validator("provider_id", "model", "system_prompt_extra", "description", mode="before")
    @classmethod
    def _empty_strings_are_none_or_empty(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if info.field_name == "description":
                return stripped
            return stripped or None
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reasoning_effort must be a string or null")
        normalized = normalize_reasoning_level(value)
        if normalized is None:
            if not value.strip():
                return None
            allowed = ", ".join(NORMALIZED_REASONING_LEVELS)
            raise ValueError(f"reasoning_effort must be one of {allowed}; got {value!r}")
        return normalized

    @model_validator(mode="after")
    def _require_switchable_profile_description(self) -> AgentRuntimeProfile:
        if self.agent_switchable and not self.description:
            raise ValueError(
                "agent-switchable runtime profiles require description routing guidance"
            )
        return self


def _matches_any(tool_name: str, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    if "*" in patterns:
        return True
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
