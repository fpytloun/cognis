"""Step-profile registry and tool matching helpers."""

from __future__ import annotations

from dataclasses import dataclass

from cognis.models.tool import (
    ToolCapability,
    ToolDefinition,
    tool_capabilities,
    tool_matches_identifier,
)
from cognis.models.workflow import (
    StepDefinition,
    StepProfileConfig,
    StepProfileMode,
    StepToolOverrides,
)


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
                "orchestration": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "tool_output": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "workflow": _caps(ToolCapability.READ, ToolCapability.WRITE),
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
                "orchestration": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "shell": _caps(ToolCapability.WRITE, ToolCapability.PRIVILEGED),
                "system": _caps(ToolCapability.READ),
                "tool_output": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "workflow": _caps(ToolCapability.READ, ToolCapability.WRITE),
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
                "orchestration": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "tool_output": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "workflow": _caps(ToolCapability.READ, ToolCapability.WRITE),
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
                "datetime": _caps(ToolCapability.READ),
                "filesystem": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "lsp": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "orchestration": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "shell": _caps(ToolCapability.WRITE, ToolCapability.PRIVILEGED),
                "system": _caps(ToolCapability.READ),
                "tool_output": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "workflow": _caps(ToolCapability.READ, ToolCapability.WRITE),
            }
        ),
    ),
    "system:review": StepProfileDefinition(
        profile_id="system:review",
        name="Review",
        mode=StepProfileMode.SOFT,
        config=StepProfileConfig(
            matrix={
                "datetime": _caps(ToolCapability.READ),
                "filesystem": _caps(ToolCapability.READ),
                "lsp": _caps(ToolCapability.READ),
                "memory": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "orchestration": _caps(ToolCapability.READ, ToolCapability.WRITE),
                "system": _caps(ToolCapability.READ),
                "tool_output": _caps(ToolCapability.READ),
                "web": _caps(ToolCapability.READ),
                "workflow": _caps(ToolCapability.READ, ToolCapability.WRITE),
            }
        ),
    ),
}


def list_step_profile_definitions() -> list[StepProfileDefinition]:
    """Return seeded step-profile definitions in stable order."""

    return list(STEP_PROFILES.values())


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


def profile_matches_tool(tool: ToolDefinition, profile: ResolvedStepProfile) -> bool:
    """Return whether a tool matches a profile matrix."""

    if profile.config is None:
        return True
    allowed = profile.config.matrix.get(tool.category)
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
