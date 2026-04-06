"""Skill parsing for SKILL.md (Claude Code / Agent Skills format) and Cognis YAML.

Parses frontmatter + markdown body into skill domain models.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from cognis.logging import get_logger
from cognis.models.skill import (
    SkillExportData,
    SkillToolRecipe,
    SkillToolSpec,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# SKILL.md parser (Claude Code / Agent Skills standard)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_md(content: str) -> dict[str, Any]:
    """Parse a SKILL.md file into a skill data dict.

    Returns a dict with keys: name, description, instructions, tags,
    tools, prompt_templates, secret_placeholders, and any extra
    frontmatter fields.

    Raises ``ValueError`` on missing required fields.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        # No frontmatter — treat entire content as instructions
        return {
            "name": "",
            "description": None,
            "instructions": content.strip(),
            "tags": [],
            "tools": [],
            "prompt_templates": {},
            "secret_placeholders": [],
        }

    frontmatter_text = match.group(1)
    body = content[match.end() :].strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    name = str(frontmatter.get("name") or "").strip()
    raw_desc = frontmatter.get("description")
    description = raw_desc.strip() or None if isinstance(raw_desc, str) else None

    tags = frontmatter.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    else:
        tags = [str(t).strip() for t in tags if str(t).strip()]

    # Parse tool definitions from frontmatter if present
    raw_tools = frontmatter.get("tools") or []
    tools = _parse_tool_specs(raw_tools)

    # Parse prompt templates
    prompt_templates = frontmatter.get("prompt_templates") or {}
    if not isinstance(prompt_templates, dict):
        prompt_templates = {}

    # Parse secret placeholders
    secret_placeholders = frontmatter.get("secret_placeholders") or []
    if not isinstance(secret_placeholders, list):
        secret_placeholders = []

    return {
        "name": name,
        "description": description,
        "instructions": body,
        "tags": tags,
        "tools": [tool.model_dump(mode="json") for tool in tools],
        "prompt_templates": prompt_templates,
        "secret_placeholders": [str(s) for s in secret_placeholders],
    }


def _parse_tool_specs(raw_tools: Any) -> list[SkillToolSpec]:
    """Parse raw tool definitions from frontmatter into validated specs."""
    if not isinstance(raw_tools, list):
        return []

    specs: list[SkillToolSpec] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            logger.warning("Skipping non-dict tool spec in skill frontmatter")
            continue
        try:
            recipe = None
            raw_recipe = raw.get("recipe")
            if isinstance(raw_recipe, dict):
                recipe = SkillToolRecipe.model_validate(raw_recipe)

            spec = SkillToolSpec(
                name=str(raw.get("name", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                parameters=raw.get("parameters") or {"type": "object", "properties": {}},
                recipe=recipe,
                read_only=bool(raw.get("read_only", False)),
                non_bypassable=True,  # server-enforced
                timeout_seconds=int(raw.get("timeout_seconds", 60)),
                max_result_size=int(raw.get("max_result_size", 50_000)),
            )
            if not spec.name:
                logger.warning("Skipping tool spec without name")
                continue
            specs.append(spec)
        except Exception:
            logger.warning("Skipping invalid tool spec in skill frontmatter", exc_info=True)
    return specs


# ---------------------------------------------------------------------------
# Cognis YAML parser
# ---------------------------------------------------------------------------


def parse_cognis_yaml(content: str) -> dict[str, Any]:
    """Parse a Cognis skill YAML export into a skill data dict.

    Raises ``ValueError`` on invalid content.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Skill YAML must be a mapping")

    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("Skill YAML must have a 'name' field")

    tools = _parse_tool_specs(data.get("tools") or [])

    return {
        "name": name,
        "description": data.get("description"),
        "instructions": str(data.get("instructions") or ""),
        "tags": data.get("tags") or [],
        "tools": [tool.model_dump(mode="json") for tool in tools],
        "prompt_templates": data.get("prompt_templates") or {},
        "secret_placeholders": data.get("secret_placeholders") or [],
        "schema_version": data.get("schema_version", 1),
    }


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(content: str) -> str:
    """Detect whether content is SKILL.md or Cognis YAML.

    Returns ``"skill_md"`` or ``"cognis_yaml"``.
    """
    stripped = content.strip()
    if stripped.startswith("---"):
        # Could be either — check if body after frontmatter looks like markdown
        match = _FRONTMATTER_RE.match(stripped)
        if match:
            try:
                fm = yaml.safe_load(match.group(1))
                if isinstance(fm, dict) and "schema_version" in fm:
                    return "cognis_yaml"
            except yaml.YAMLError:
                pass
        return "skill_md"

    # Try YAML parse
    try:
        data = yaml.safe_load(stripped)
        if isinstance(data, dict) and ("name" in data or "instructions" in data):
            return "cognis_yaml"
    except yaml.YAMLError:
        pass

    return "skill_md"


def parse_skill_content(content: str, format: str | None = None) -> dict[str, Any]:
    """Parse skill content in auto-detected or specified format.

    Returns a normalized skill data dict.
    """
    if format is None:
        format = detect_format(content)

    if format == "cognis_yaml":
        return parse_cognis_yaml(content)
    return parse_skill_md(content)


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def compute_content_hash(
    instructions: str,
    tools: list[dict[str, Any]] | None = None,
    prompt_templates: dict[str, Any] | None = None,
) -> str:
    """Compute SHA-256 hash of canonical skill content."""
    import json

    canonical = json.dumps(
        {
            "instructions": instructions,
            "tools": tools or [],
            "prompt_templates": prompt_templates or {},
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_skill_md(data: SkillExportData) -> str:
    """Export a skill as SKILL.md format."""
    frontmatter: dict[str, Any] = {
        "name": data.name,
    }
    if data.description:
        frontmatter["description"] = data.description
    if data.tags:
        frontmatter["tags"] = data.tags
    if data.tools:
        frontmatter["tools"] = data.tools
    if data.prompt_templates:
        frontmatter["prompt_templates"] = data.prompt_templates
    if data.secret_placeholders:
        frontmatter["secret_placeholders"] = data.secret_placeholders

    fm_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n\n{data.instructions}\n"


def export_cognis_yaml(data: SkillExportData) -> str:
    """Export a skill as Cognis YAML format."""
    export_dict: dict[str, Any] = {
        "schema_version": data.schema_version,
        "name": data.name,
    }
    if data.description:
        export_dict["description"] = data.description
    if data.tags:
        export_dict["tags"] = data.tags
    export_dict["auto_load"] = data.auto_load
    export_dict["instructions"] = data.instructions
    if data.tools:
        export_dict["tools"] = data.tools
    if data.prompt_templates:
        export_dict["prompt_templates"] = data.prompt_templates
    if data.secret_placeholders:
        export_dict["secret_placeholders"] = data.secret_placeholders
    if data.provenance:
        export_dict["provenance"] = data.provenance.model_dump(mode="json", exclude_none=True)
    if data.asset_manifest:
        export_dict["asset_manifest"] = [a.model_dump(mode="json") for a in data.asset_manifest]

    return yaml.dump(export_dict, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# GitHub URL helpers
# ---------------------------------------------------------------------------

_GITHUB_BLOB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
_GITHUB_TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$")
_GITHUB_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")


def resolve_github_url(url: str) -> tuple[str, str | None]:
    """Resolve a GitHub URL to a raw content URL and optional commit SHA.

    Returns (raw_url, commit_sha_or_none).
    """
    # Already a raw URL
    match = _GITHUB_RAW_RE.match(url)
    if match:
        owner, repo, ref, path = match.groups()
        sha = ref if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref) else None
        return url, sha

    # GitHub blob URL → raw
    match = _GITHUB_BLOB_RE.match(url)
    if match:
        owner, repo, ref, path = match.groups()
        sha = ref if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref) else None
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return raw_url, sha

    # GitHub tree URL (folder) → try SKILL.md inside
    match = _GITHUB_TREE_RE.match(url)
    if match:
        owner, repo, ref, path = match.groups()
        sha = ref if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref) else None
        skill_path = path.rstrip("/") + "/SKILL.md"
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{skill_path}"
        return raw_url, sha

    return url, None
