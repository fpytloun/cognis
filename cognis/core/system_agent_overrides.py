"""Per-user override operations for shipped ``system:*`` agents.

Shipped system agents are code constants. Users can tune their runtime
behavior through one ``system_agent_overrides`` row per owner and agent. This
module is the transport-neutral owner of override validation and persistence;
REST routes and the ``manage_agents`` tool adapt it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.agent_registry import SYSTEM_AGENTS
from cognis.models.agent import (
    AgentDefinition,
    AgentLLMConfig,
    AgentPermissions,
    AgentRuntimeProfile,
)
from cognis.store.models import SystemAgentOverride
from cognis.store.queries import (
    delete_system_agent_override,
    get_system_agent_override,
    upsert_system_agent_override,
)

SYSTEM_AGENT_OVERRIDE_FIELDS = frozenset(
    {
        "llm_config",
        "skills",
        "tools",
        "permissions",
        "agent_profiles",
        "default_agent_profile_id",
    }
)
SYSTEM_AGENT_LLM_FIELDS = frozenset(
    {
        "provider_id",
        "model",
        "temperature",
        "top_p",
        "max_tokens",
        "reasoning_effort",
        "fast_mode",
        "tts_provider_id",
        "tts_model",
        "tts_voice",
    }
)


class SystemAgentOverrideError(ValueError):
    """Expected system-agent override failure with a transport-neutral category."""

    def __init__(self, message: str, *, code: str = "validation_error") -> None:
        super().__init__(message)
        self.code = code

    @property
    def status_code(self) -> int:
        """HTTP status equivalent for REST adapters."""

        return {"not_found": 404, "forbidden": 403, "conflict": 409}.get(self.code, 400)


def require_system_agent(agent_id: str) -> AgentDefinition:
    """Return the shipped base definition or raise ``not_found``."""

    base = SYSTEM_AGENTS.get(agent_id)
    if base is None:
        raise SystemAgentOverrideError("Agent not found", code="not_found")
    return base


def require_overridable_system_agent(agent_id: str) -> AgentDefinition:
    """Return a shipped base definition that accepts per-user overrides."""

    base = require_system_agent(agent_id)
    if not base.allow_user_override:
        raise SystemAgentOverrideError("This system agent cannot be overridden", code="forbidden")
    return base


def system_override_payload(row: SystemAgentOverride | None) -> dict[str, Any] | None:
    """Serialize the stored override overlay, or ``None`` when absent."""

    if row is None:
        return None
    return {
        "disabled": bool(row.disabled),
        "llm_config": row.llm_config_override,
        "skills": row.skills_override,
        "tools": row.tools_override,
        "permissions": row.permissions_override,
        "agent_profiles": row.agent_profiles_override,
        "default_agent_profile_id": row.default_agent_profile_id_override,
        "updated_at": _iso(row.updated_at),
    }


def check_expected_override_revision(
    row: SystemAgentOverride | None, expected_updated_at: str | None
) -> None:
    """Compare an optional caller revision with the stored override revision.

    An empty string means the caller observed no override row.
    """

    if expected_updated_at is None:
        return
    actual = row.updated_at if row is not None else None
    if expected_updated_at == "" or actual is None:
        matches = expected_updated_at == "" and actual is None
    else:
        try:
            expected = datetime.fromisoformat(expected_updated_at)
        except ValueError as exc:
            raise SystemAgentOverrideError(
                "expected_updated_at must be an ISO-8601 string"
            ) from exc
        matches = _as_utc(expected) == _as_utc(actual)
    if not matches:
        raise SystemAgentOverrideError(
            "System agent override conflict; re-read the override and retry",
            code="conflict",
        )


async def update_system_agent_override(
    session: AsyncSession,
    *,
    base: AgentDefinition,
    owner_email: str,
    updates: dict[str, Any],
    expected_updated_at: str | None = None,
) -> SystemAgentOverride:
    """Validate and persist a patch of runtime-tuning override fields.

    Omitted top-level fields preserve their stored override. A supplied
    ``None`` clears that override so the shipped value applies again. The
    caller owns the transaction commit.
    """

    if not base.allow_user_override:
        raise SystemAgentOverrideError("This system agent cannot be overridden", code="forbidden")
    forbidden = sorted(key for key in updates if key not in SYSTEM_AGENT_OVERRIDE_FIELDS)
    if forbidden:
        raise SystemAgentOverrideError(
            f"System agent overrides only allow runtime tuning fields: {', '.join(forbidden)}",
            code="forbidden",
        )

    raw_llm = updates.get("llm_config")
    if raw_llm is not None and not isinstance(raw_llm, dict):
        raise SystemAgentOverrideError(
            "System agent llm_config overrides must be an object", code="forbidden"
        )
    llm_override = raw_llm if isinstance(raw_llm, dict) else {}
    invalid_llm = sorted(key for key in llm_override if key not in SYSTEM_AGENT_LLM_FIELDS)
    if invalid_llm:
        raise SystemAgentOverrideError(
            f"Unsupported system agent override fields: {', '.join(invalid_llm)}",
            code="forbidden",
        )

    for field, label in (("skills", "skill"), ("tools", "tool"), ("permissions", "permission")):
        value = updates.get(field)
        if value is not None and not isinstance(value, dict):
            raise SystemAgentOverrideError(
                f"System agent {label} overrides must be an object", code="forbidden"
            )
    raw_skills = updates.get("skills")
    raw_tools = updates.get("tools")
    raw_permissions = updates.get("permissions")
    if isinstance(raw_permissions, dict):
        try:
            AgentPermissions.model_validate(raw_permissions)
        except ValidationError as exc:
            raise SystemAgentOverrideError(str(exc)) from exc

    profiles_override = _validated_profiles_override(updates.get("agent_profiles"))

    raw_default_profile_id = updates.get("default_agent_profile_id")
    if raw_default_profile_id is not None and not isinstance(raw_default_profile_id, str):
        raise SystemAgentOverrideError("default_agent_profile_id must be a string or null")
    default_profile_override = (
        raw_default_profile_id.strip()
        if isinstance(raw_default_profile_id, str) and raw_default_profile_id.strip()
        else None
    )
    if default_profile_override and "/" in default_profile_override:
        raise SystemAgentOverrideError("default_agent_profile_id must not contain '/'")

    existing = await _load_checked(session, base, owner_email, expected_updated_at)

    def _pick(field: str, supplied: Any, stored_attr: str) -> Any:
        if field in updates:
            return supplied
        return getattr(existing, stored_attr) if existing is not None else None

    llm_to_store = _pick("llm_config", llm_override or None, "llm_config_override")
    skills_to_store = _pick(
        "skills", raw_skills if isinstance(raw_skills, dict) else None, "skills_override"
    )
    tools_to_store = _pick(
        "tools", raw_tools if isinstance(raw_tools, dict) else None, "tools_override"
    )
    permissions_to_store = _pick(
        "permissions",
        raw_permissions if isinstance(raw_permissions, dict) else None,
        "permissions_override",
    )
    profiles_to_store = _pick("agent_profiles", profiles_override, "agent_profiles_override")
    default_to_store = _pick(
        "default_agent_profile_id",
        default_profile_override,
        "default_agent_profile_id_override",
    )

    candidate_profiles = (
        profiles_to_store
        if isinstance(profiles_to_store, dict)
        else {
            profile_id: profile.model_dump(mode="json", exclude_none=True)
            for profile_id, profile in base.agent_profiles.items()
        }
    )
    candidate_default = default_to_store or base.default_agent_profile_id
    if candidate_default and candidate_default not in candidate_profiles:
        raise SystemAgentOverrideError(
            f"Default runtime profile '{candidate_default}' is not defined"
        )
    # Persist only overlays that resolve to a valid effective configuration;
    # an invalid stored overlay would break registry resolution for this user.
    try:
        base_llm = base.llm_config.model_dump(exclude_none=True) if base.llm_config else {}
        AgentLLMConfig.model_validate({**base_llm, **(llm_to_store or {})})
    except ValidationError as exc:
        raise SystemAgentOverrideError(f"Invalid llm_config override: {exc}") from exc

    return await _upsert(
        session,
        existing,
        owner_email=owner_email,
        agent_id=base.agent_id,
        llm_config_override=llm_to_store,
        skills_override=skills_to_store,
        tools_override=tools_to_store,
        permissions_override=permissions_to_store,
        execution_override=existing.execution_override if existing is not None else None,
        agent_profiles_override=profiles_to_store,
        default_agent_profile_id_override=default_to_store,
    )


async def set_system_agent_disabled(
    session: AsyncSession,
    *,
    base: AgentDefinition,
    owner_email: str,
    disabled: bool,
    expected_updated_at: str | None = None,
) -> SystemAgentOverride:
    """Disable or enable a shipped agent for one user, preserving other overrides.

    The caller owns the transaction commit.
    """

    if disabled and not base.allow_user_disable:
        raise SystemAgentOverrideError("This system agent cannot be disabled", code="forbidden")
    existing = await _load_checked(session, base, owner_email, expected_updated_at)
    return await _upsert(
        session,
        existing,
        owner_email=owner_email,
        agent_id=base.agent_id,
        disabled=disabled,
        llm_config_override=existing.llm_config_override if existing else None,
        skills_override=existing.skills_override if existing else None,
        tools_override=existing.tools_override if existing else None,
        permissions_override=existing.permissions_override if existing else None,
        execution_override=existing.execution_override if existing else None,
        agent_profiles_override=existing.agent_profiles_override if existing else None,
        default_agent_profile_id_override=(
            existing.default_agent_profile_id_override if existing else None
        ),
    )


async def reset_system_agent_override(
    session: AsyncSession,
    *,
    base: AgentDefinition,
    owner_email: str,
    expected_updated_at: str | None = None,
) -> bool:
    """Delete the user's override row, restoring shipped behavior.

    The caller owns the transaction commit.
    """

    existing = await _load_checked(session, base, owner_email, expected_updated_at)
    if existing is None:
        # Never delete a row this transaction did not observe and check.
        return False
    return await delete_system_agent_override(
        session, owner_email=owner_email, agent_id=base.agent_id
    )


async def _load_checked(
    session: AsyncSession,
    base: AgentDefinition,
    owner_email: str,
    expected_updated_at: str | None,
) -> SystemAgentOverride | None:
    """Load the override, locking it for compare-and-swap when a revision is supplied.

    The row lock serializes concurrent revision-checked writers on an existing
    row until the caller's transaction ends. A missing row is inserted with
    ``create_only`` by ``_upsert``, so a concurrent first insert fails on the
    unique constraint instead of being overwritten.
    """

    existing = await get_system_agent_override(
        session,
        owner_email=owner_email,
        agent_id=base.agent_id,
        for_update=expected_updated_at is not None,
    )
    check_expected_override_revision(existing, expected_updated_at)
    return existing


async def _upsert(
    session: AsyncSession, existing: SystemAgentOverride | None, **kwargs: Any
) -> SystemAgentOverride:
    try:
        return await upsert_system_agent_override(session, create_only=existing is None, **kwargs)
    except IntegrityError as exc:
        raise SystemAgentOverrideError(
            "System agent override conflict; re-read the override and retry",
            code="conflict",
        ) from exc


def _validated_profiles_override(raw_profiles: Any) -> dict[str, dict[str, Any]] | None:
    if raw_profiles is None:
        return None
    if not isinstance(raw_profiles, dict):
        raise SystemAgentOverrideError(
            "System agent runtime profile overrides must be an object", code="forbidden"
        )
    for profile_id in raw_profiles:
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or profile_id != profile_id.strip()
            or "/" in profile_id
        ):
            raise SystemAgentOverrideError(
                "Runtime profile keys must be non-empty, trimmed, and must not contain '/'"
            )
    try:
        profiles = {
            profile_id: AgentRuntimeProfile.model_validate(profile).model_dump(
                mode="json", exclude_none=True
            )
            for profile_id, profile in raw_profiles.items()
        }
    except ValidationError as exc:
        raise SystemAgentOverrideError(str(exc)) from exc
    for profile_id, profile in profiles.items():
        embedded_id = profile.get("profile_id")
        if embedded_id is not None and embedded_id != profile_id:
            raise SystemAgentOverrideError(
                f"Runtime profile key '{profile_id}' does not match embedded "
                f"profile_id '{embedded_id}'"
            )
    return profiles


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None
