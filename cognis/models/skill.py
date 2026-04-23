"""Domain models for the skill system.

Skills are DB-managed instruction + tool bundles with versioning,
import/export support, and artifact-backed assets.  Agents reference
skills by ID; runtime resolves to the current published version.
"""

from __future__ import annotations

import posixpath
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


def _validate_skill_relative_path(value: str, *, field_name: str) -> str:
    candidate = value.replace("\\", "/").strip()
    if not candidate:
        raise ValueError(f"{field_name} cannot be empty")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."}:
        raise ValueError(f"{field_name} is invalid")
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"{field_name} must stay within the staged skill workspace")
    if "/../" in f"/{normalized}":
        raise ValueError(f"{field_name} must stay within the staged skill workspace")
    return normalized[2:] if normalized.startswith("./") else normalized

# ---------------------------------------------------------------------------
# Executable skill tool recipe
# ---------------------------------------------------------------------------


class SkillToolRecipe(BaseModel):
    """Execution recipe for a skill-defined tool.

    Recipes define how the executor should run a skill tool.  The
    controller validates recipes at creation/import time but never
    executes them — only executors do.

    Supported modes:
    - ``script``: execute a staged asset script with arguments
    - ``command``: run a declared command with templated args
    """

    mode: str  # "script" | "command"
    entry: str  # asset filename (script mode) or argv[0] (command mode)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 60
    required_assets: list[str] = Field(default_factory=list)
    secret_placeholders: list[str] = Field(default_factory=list)
    working_dir: str | None = None  # relative to staged asset dir

    @model_validator(mode="after")
    def _validate_paths(self) -> SkillToolRecipe:
        if self.mode == "script":
            self.entry = _validate_skill_relative_path(self.entry, field_name="entry")
        if self.working_dir is not None:
            self.working_dir = _validate_skill_relative_path(
                self.working_dir,
                field_name="working_dir",
            )
        self.required_assets = [
            _validate_skill_relative_path(item, field_name="required_assets")
            for item in self.required_assets
        ]
        return self


class SkillToolSpec(BaseModel):
    """Validated specification for a skill-defined tool.

    This is stored in ``SkillVersionRow.tools`` as a list of dicts and
    converted to ``ToolDefinition`` objects at runtime.
    """

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    recipe: SkillToolRecipe | None = None
    read_only: bool = False
    non_bypassable: bool = True  # server-enforced default for executable tools
    timeout_seconds: int = 60
    max_result_size: int = 50_000


# ---------------------------------------------------------------------------
# Skill version and asset models
# ---------------------------------------------------------------------------


class SkillAssetRef(BaseModel):
    """Reference to a skill asset in the artifact store."""

    filename: str
    asset_id: str
    artifact_namespace: str = "skills"
    artifact_object_id: str
    content_hash: str
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    url: str | None = None
    signed_url: str | None = None


class ImportProvenance(BaseModel):
    """Provenance metadata for imported skills."""

    source_url: str | None = None
    resolved_url: str | None = None
    commit_sha: str | None = None
    import_checksum: str | None = None
    imported_at: datetime | None = None
    import_format: str | None = None  # "skill_md" | "cognis_yaml" | "cognis_package"


class SkillVersion(BaseModel):
    """Immutable skill version snapshot."""

    version_id: str
    skill_id: str
    version_number: int
    content_hash: str
    schema_version: int = 1
    instructions: str
    tools: list[SkillToolSpec] = Field(default_factory=list)
    linked_tool_ids: list[str] = Field(default_factory=list)
    prompt_templates: dict[str, str] = Field(default_factory=dict)
    secret_placeholders: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_source_hash: str | None = None
    provenance: ImportProvenance | None = None
    asset_manifest: list[SkillAssetRef] = Field(default_factory=list)
    created_at: datetime | None = None


class SkillSummary(BaseModel):
    """Logical skill record with current version metadata."""

    skill_id: str
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    linked_tool_ids: list[str] = Field(default_factory=list)
    auto_load: bool = False  # Internal flag: attached to all agents
    source: str = "db"
    owner_email: str | None = None
    current_version_id: str | None = None
    current_version_number: int | None = None
    tool_count: int = 0
    asset_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Resolved skill set for runtime
# ---------------------------------------------------------------------------


class ResolvedSkill(BaseModel):
    """A skill resolved to a concrete version for runtime use."""

    skill_id: str
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    linked_tool_ids: list[str] = Field(default_factory=list)
    version_id: str
    version_number: int
    content_hash: str
    instructions: str
    tools: list[SkillToolSpec] = Field(default_factory=list)
    prompt_templates: dict[str, str] = Field(default_factory=dict)
    secret_placeholders: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_source_hash: str | None = None
    asset_manifest: list[SkillAssetRef] = Field(default_factory=list)
    auto_load: bool = False  # Internal flag: attached to all agents
    attached: bool = False


class ResolvedSkillSet(BaseModel):
    """Complete resolved skill set for an agent at a point in time.

    Used by both effective-tools preview and runtime context assembly.
    The ``version_snapshot`` maps skill_id to version_id for
    reproducibility and retry consistency.
    """

    skills: list[ResolvedSkill] = Field(default_factory=list)
    version_snapshot: dict[str, str] = Field(default_factory=dict)

    @property
    def all_instructions(self) -> list[str]:
        """Return ordered instruction blocks from all active skills."""
        return [skill.instructions for skill in self.skills if skill.instructions.strip()]

    @property
    def all_tools(self) -> list[SkillToolSpec]:
        """Return all tool specs from all active skills."""
        tools: list[SkillToolSpec] = []
        for skill in self.skills:
            tools.extend(skill.tools)
        return tools

    @property
    def all_secret_placeholders(self) -> set[str]:
        """Return all secret placeholders from all active skills."""
        placeholders: set[str] = set()
        for skill in self.skills:
            placeholders.update(skill.secret_placeholders)
        return placeholders


# ---------------------------------------------------------------------------
# Import / export payloads
# ---------------------------------------------------------------------------


class SkillImportRequest(BaseModel):
    """Request to import a skill from URL or inline content."""

    url: str | None = None
    content: str | None = None  # inline SKILL.md or YAML content
    content_b64: str | None = None
    filename: str | None = None
    format: str | None = None  # "skill_md" | "cognis_yaml" | "cognis_package"
    name: str | None = None  # override imported name
    tags: list[str] | None = None  # override imported tags
    linked_tool_ids: list[str] | None = None
    auto_load: bool = False


class SkillExportData(BaseModel):
    """Portable skill export payload."""

    schema_version: int = 1
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    linked_tool_ids: list[str] = Field(default_factory=list)
    auto_load: bool = False
    instructions: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    prompt_templates: dict[str, str] = Field(default_factory=dict)
    secret_placeholders: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_source_hash: str | None = None
    provenance: ImportProvenance | None = None
    asset_manifest: list[SkillAssetRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent skill reference
# ---------------------------------------------------------------------------


class AgentSkillRef(BaseModel):
    """Reference to a skill in an agent's configuration.

    Agents store a list of these in ``agent.skills.items``.
    """

    skill_id: str
    enabled: bool = True
