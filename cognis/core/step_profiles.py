"""Step-profile registry and tool matching helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cognis.models.tool import (
    ALL_PROFILE_GROUPS,
    ToolCapability,
    ToolDefinition,
    tool_capabilities,
    tool_matches_identifier,
    tool_profile_group,
)
from cognis.models.workflow import (
    StepDefinition,
    StepProfileConfig,
    StepProfileMode,
    StepToolOverrides,
)

STEP_PROFILE_OVERRIDES_SETTING_KEY = "workflow.step_profile_overrides"
STEP_PROFILE_CUSTOM_SETTING_KEY = "workflow.step_profiles_custom"
LEGACY_PROFILE_GROUP_MAP: dict[str, str] = {
    "lsp": "filesystem",
    "orchestration": "system",
    "workflow": "system",
    "tool_output": "system",
    "context": "system",
    "deliverable": "system",
    "artifact": "system",
    "schedule": "system",
    "datetime": "system",
}


@dataclass(frozen=True, slots=True)
class StepProfileDefinition:
    """A seeded step-profile definition."""

    profile_id: str
    name: str
    mode: StepProfileMode
    config: StepProfileConfig


@dataclass(frozen=True, slots=True)
class ResolvedStepProfile:
    """Effective profile policy for a step."""

    profile_id: str | None
    mode: StepProfileMode
    config: StepProfileConfig | None


def _caps(*values: ToolCapability) -> list[ToolCapability]:
    return list(values)


STEP_PROFILES: dict[str, StepProfileDefinition] = {
    "system:direct-default": StepProfileDefinition(
        profile_id="system:direct-default",
        name="Direct default",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "datetime": _caps(ToolCapability.READ),
                "filesystem": _caps(ToolCapability.READ),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "development": _caps(ToolCapability.READ),
            }
        ),
    ),
    "system:general-task": StepProfileDefinition(
        profile_id="system:general-task",
        name="General task",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "datetime": _caps(ToolCapability.READ),
                "filesystem": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "shell": _caps(ToolCapability.WRITE, ToolCapability.PRIVILEGED),
                "system": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "development": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "communication": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "office": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "personal": _caps(ToolCapability.READ, ToolCapability.WRITE),
            }
        ),
    ),
    "system:research": StepProfileDefinition(
        profile_id="system:research",
        name="Research",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "datetime": _caps(ToolCapability.READ),
                "filesystem": _caps(ToolCapability.READ),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "development": _caps(ToolCapability.READ),
                "office": _caps(ToolCapability.READ),
                "communication": _caps(ToolCapability.READ),
                "personal": _caps(ToolCapability.READ),
            }
        ),
    ),
    "system:coding": StepProfileDefinition(
        profile_id="system:coding",
        name="Coding",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "browser": _caps(
                    ToolCapability.READ, ToolCapability.WRITE, ToolCapability.PRIVILEGED
                ),
                "filesystem": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "shell": _caps(ToolCapability.WRITE, ToolCapability.PRIVILEGED),
                "system": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "development": _caps(ToolCapability.READ, ToolCapability.WRITE, ToolCapability.PRIVILEGED),
            }
        ),
    ),
    "system:review": StepProfileDefinition(
        profile_id="system:review",
        name="Review",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "filesystem": _caps(ToolCapability.READ),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "development": _caps(ToolCapability.READ),
            }
        ),
    ),
}


def list_step_profile_definitions() -> list[StepProfileDefinition]:
    """Return seeded step-profile definitions in stable order."""

    return list(STEP_PROFILES.values())


class StepProfileRegistry:
    """Cached registry for seeded step profiles plus persisted overrides."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        self._overrides: dict[str, dict[str, Any]] = {}
        self._custom: dict[str, StepProfileDefinition] = {}

    @classmethod
    async def from_session_factory(cls, session_factory: Any) -> StepProfileRegistry:
        registry = cls(session_factory)
        await registry.refresh()
        return registry

    async def refresh(self) -> None:
        from cognis.store.queries import get_setting_value

        async with self._session_factory() as session:
            raw = await get_setting_value(session, STEP_PROFILE_OVERRIDES_SETTING_KEY, {})
            raw_custom = await get_setting_value(session, STEP_PROFILE_CUSTOM_SETTING_KEY, {})
        if not isinstance(raw, dict):
            raw = {}
        validated: dict[str, dict[str, Any]] = {}
        for profile_id, payload in raw.items():
            if not isinstance(profile_id, str) or profile_id not in STEP_PROFILES:
                continue
            if not isinstance(payload, dict):
                continue
            validated[profile_id] = _normalize_override_payload(payload)
        self._overrides = validated
        self._custom = _normalize_custom_profiles(raw_custom)

    def list_definitions(self) -> list[StepProfileDefinition]:
        seeded = [self.get_definition(profile_id) for profile_id in STEP_PROFILES]
        return [*seeded, *[self._custom[key] for key in sorted(self._custom)]]

    def get_definition(self, profile_id: str) -> StepProfileDefinition | None:
        custom = self._custom.get(profile_id)
        if custom is not None:
            return custom
        base = STEP_PROFILES.get(profile_id)
        if base is None:
            return None
        override = self._overrides.get(profile_id)
        if override is None:
            return base
        return _apply_definition_override(base, override)

    def has_override(self, profile_id: str) -> bool:
        return profile_id in self._overrides

    def is_custom(self, profile_id: str) -> bool:
        return profile_id in self._custom

    def current_overrides(self) -> dict[str, dict[str, Any]]:
        return dict(self._overrides)

    def resolve_step_profile(self, step: StepDefinition) -> ResolvedStepProfile:
        base = self.get_definition(step.step_profile_id or "")
        if base is None and step.step_profile is None:
            return ResolvedStepProfile(profile_id=None, mode=step.step_profile_mode, config=None)
        merged = _merge_profile_config(base.config if base is not None else None, step.step_profile)
        mode = step.step_profile_mode or (base.mode if base is not None else StepProfileMode.SOFT)
        return ResolvedStepProfile(
            profile_id=step.step_profile_id if base is not None else None,
            mode=mode,
            config=merged,
        )


def resolve_step_profile(step: StepDefinition) -> ResolvedStepProfile:
    """Resolve the effective step profile for a step definition."""

    base = STEP_PROFILES.get(step.step_profile_id or "")
    if base is None and step.step_profile is None:
        return ResolvedStepProfile(profile_id=None, mode=step.step_profile_mode, config=None)
    merged = _merge_profile_config(base.config if base is not None else None, step.step_profile)
    mode = step.step_profile_mode or (base.mode if base is not None else StepProfileMode.SOFT)
    return ResolvedStepProfile(
        profile_id=step.step_profile_id if base is not None else None,
        mode=mode,
        config=merged,
    )


def serialize_step_profile_override(
    *,
    name: str | None,
    mode: StepProfileMode,
    config: StepProfileConfig,
) -> dict[str, Any]:
    """Serialize an override payload for settings persistence."""

    return {
        "name": name,
        "mode": str(mode),
        "config": config.model_dump(mode="json"),
    }


def profile_matches_tool(tool: ToolDefinition, profile: ResolvedStepProfile) -> bool:
    """Return whether a tool matches a profile matrix."""

    if profile.config is None:
        return True
    allowed = profile.config.matrix.get(tool_profile_group(tool))
    if not allowed:
        return False
    return bool(tool_capabilities(tool) & set(allowed))


def apply_profile_overrides(
    tool: ToolDefinition,
    profile: ResolvedStepProfile,
) -> bool:
    """Return whether a tool should be included after explicit overrides."""

    config = profile.config
    if config is None:
        return True
    overrides = config.tool_overrides
    if any(tool_matches_identifier(tool, identifier) for identifier in overrides.exclude):
        return False
    if any(tool_matches_identifier(tool, identifier) for identifier in overrides.include):
        return True
    return profile_matches_tool(tool, profile)


def step_profile_allows_tool(tool: ToolDefinition, profile: ResolvedStepProfile) -> bool:
    """Return whether a tool remains in the step inventory."""

    if profile.config is None:
        return True
    if profile.mode == StepProfileMode.SOFT:
        return not any(
            tool_matches_identifier(tool, identifier)
            for identifier in profile.config.tool_overrides.exclude
        )
    return apply_profile_overrides(tool, profile)


def step_profile_visible_by_default(tool: ToolDefinition, profile: ResolvedStepProfile) -> bool:
    """Return whether a tool should be pre-exposed before search discovery."""

    if profile.config is None:
        return True
    return apply_profile_overrides(tool, profile)


def _merge_profile_config(
    base: StepProfileConfig | None,
    override: StepProfileConfig | None,
) -> StepProfileConfig | None:
    if base is None and override is None:
        return None
    if base is None:
        return override
    if override is None:
        return base
    matrix = dict(base.matrix)
    matrix.update(override.matrix)
    return StepProfileConfig(
        matrix=matrix,
        tool_overrides=StepToolOverrides(
            include=[*base.tool_overrides.include, *override.tool_overrides.include],
            exclude=[*base.tool_overrides.exclude, *override.tool_overrides.exclude],
        ),
        allow_tool_search=override.allow_tool_search,
    )


def _normalize_override_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode")
    raw_config = payload.get("config")
    config = StepProfileConfig.model_validate(raw_config if isinstance(raw_config, dict) else {})
    return {
        "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
        "mode": str(StepProfileMode(mode)) if isinstance(mode, str) and mode else str(StepProfileMode.SOFT),
        "config": _normalize_profile_config(config).model_dump(mode="json"),
    }


def _apply_definition_override(
    base: StepProfileDefinition, override: dict[str, Any]
) -> StepProfileDefinition:
    config = StepProfileConfig.model_validate(override.get("config") or {})
    mode = StepProfileMode(str(override.get("mode") or base.mode))
    name = str(override.get("name") or base.name)
    return StepProfileDefinition(
        profile_id=base.profile_id,
        name=name,
        mode=mode,
        config=_normalize_profile_config(config),
    )


def _normalize_custom_profiles(raw: Any) -> dict[str, StepProfileDefinition]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, StepProfileDefinition] = {}
    for profile_id, payload in raw.items():
        if not isinstance(profile_id, str) or not profile_id:
            continue
        if profile_id in STEP_PROFILES or not isinstance(payload, dict):
            continue
        try:
            name = str(payload.get("name") or profile_id)
            mode = StepProfileMode(str(payload.get("mode") or StepProfileMode.SOFT))
            config = _normalize_profile_config(
                StepProfileConfig.model_validate(payload.get("config") or {})
            )
        except Exception:
            continue
        normalized[profile_id] = StepProfileDefinition(
            profile_id=profile_id,
            name=name,
            mode=mode,
            config=config,
        )
    return normalized


def _normalize_profile_config(config: StepProfileConfig) -> StepProfileConfig:
    merged: dict[str, list[ToolCapability]] = {}
    for key, capabilities in config.matrix.items():
        normalized_key = LEGACY_PROFILE_GROUP_MAP.get(key, key)
        if normalized_key not in ALL_PROFILE_GROUPS or not capabilities:
            continue
        existing = merged.get(normalized_key, [])
        merged[normalized_key] = [*existing, *[cap for cap in capabilities if cap not in existing]]
    return StepProfileConfig(
        matrix=merged,
        tool_overrides=config.tool_overrides,
        allow_tool_search=config.allow_tool_search,
    )
