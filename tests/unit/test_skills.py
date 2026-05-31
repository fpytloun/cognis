"""Unit tests for the skill system: parsing, resolution, and tool conversion."""

from __future__ import annotations

from cognis.models.agent import AgentDefinition
from cognis.models.skill import (
    AgentSkillRef,
    ResolvedSkill,
    ResolvedSkillSet,
    SkillAssetRef,
    SkillToolSpec,
)
from cognis.tools.skill_parser import (
    compute_content_hash,
    detect_format,
    export_cognis_yaml,
    export_skill_md,
    parse_skill_content,
    parse_skill_md,
    resolve_github_url,
)
from cognis.tools.skill_service import (
    export_cognis_package,
    normalize_linked_tool_ids,
    normalize_skill_asset_filename,
    parse_cognis_package,
)
from cognis.tools.skills import (
    attached_skill_tool_ids_by_skill,
    build_available_skills_metadata,
    discoverable_skill_tools_to_definitions,
    extract_agent_skill_refs,
    load_skill_tool_names,
    skill_tools_to_definitions,
)


def _agent(skills: dict[str, object] | None) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-a",
        owner_email="user@example.com",
        name="Agent A",
        skills=skills,
    )


# ---------------------------------------------------------------------------
# Legacy MVP loader (backward compatibility)
# ---------------------------------------------------------------------------


def test_load_skill_tool_names_returns_inline_tool_names() -> None:
    agent = _agent(
        {
            "items": [
                {
                    "skill_id": "research-core",
                    "name": "Research Core",
                    "tool_names": ["list_agents", "get_status"],
                }
            ]
        }
    )

    assert load_skill_tool_names(agent) == {"list_agents", "get_status"}


def test_load_skill_tool_names_skips_malformed_entries() -> None:
    agent = _agent({"items": ["bad", {"skill_id": "missing-name"}, {"name": "Missing ID"}]})

    assert load_skill_tool_names(agent) == set()


def test_load_skill_tool_names_returns_empty_for_missing_skill_config() -> None:
    assert load_skill_tool_names(_agent(None)) == set()


# ---------------------------------------------------------------------------
# Agent skill ref extraction
# ---------------------------------------------------------------------------


def test_extract_agent_skill_refs_new_style() -> None:
    agent = _agent(
        {
            "items": [
                {"skill_id": "skill-a", "enabled": True},
                {"skill_id": "skill-b", "enabled": False},
                {"skill_id": "skill-c", "auto_load_instructions": True},
            ]
        }
    )
    refs = extract_agent_skill_refs(agent)
    assert len(refs) == 3
    assert refs[0] == AgentSkillRef(skill_id="skill-a", enabled=True)
    assert refs[1] == AgentSkillRef(skill_id="skill-b", enabled=False)
    assert refs[2] == AgentSkillRef(
        skill_id="skill-c",
        enabled=True,
        auto_load_instructions=True,
    )


def test_extract_agent_skill_refs_skips_legacy_entries() -> None:
    agent = _agent(
        {
            "items": [
                {"skill_id": "legacy", "name": "Legacy", "tool_names": ["read"]},
                {"skill_id": "new-style", "enabled": True},
            ]
        }
    )
    refs = extract_agent_skill_refs(agent)
    assert len(refs) == 1
    assert refs[0].skill_id == "new-style"


def test_extract_agent_skill_refs_empty() -> None:
    assert extract_agent_skill_refs(_agent(None)) == []
    assert extract_agent_skill_refs(_agent({})) == []
    assert extract_agent_skill_refs(_agent({"items": []})) == []


# ---------------------------------------------------------------------------
# SKILL.md parser
# ---------------------------------------------------------------------------


def test_parse_skill_md_with_frontmatter() -> None:
    content = """---
name: my-skill
description: A test skill
tags: [test, demo]
---

# My Skill

Instructions here.
"""
    result = parse_skill_md(content)
    assert result["name"] == "my-skill"
    assert result["description"] == "A test skill"
    assert result["tags"] == ["test", "demo"]
    assert "Instructions here." in result["instructions"]


def test_parse_skill_md_without_frontmatter() -> None:
    content = "# Just Instructions\n\nNo frontmatter here."
    result = parse_skill_md(content)
    assert result["name"] == ""
    assert result["instructions"] == "# Just Instructions\n\nNo frontmatter here."


def test_parse_skill_md_with_tools() -> None:
    content = """---
name: tool-skill
description: Skill with tools
tools:
  - name: my_tool
    description: Does something
    parameters:
      type: object
      properties:
        input:
          type: string
    recipe:
      mode: script
      entry: run.sh
---

Instructions.
"""
    result = parse_skill_md(content)
    assert len(result["tools"]) == 1
    assert result["tools"][0]["name"] == "my_tool"
    assert result["tools"][0]["recipe"]["mode"] == "script"


def test_parse_skill_md_with_secret_placeholders() -> None:
    content = """---
name: api-skill
secret_placeholders:
  - API_KEY
  - API_SECRET
---

Use the API.
"""
    result = parse_skill_md(content)
    assert result["secret_placeholders"] == ["API_KEY", "API_SECRET"]


def test_parse_skill_md_with_linked_tool_ids() -> None:
    content = """---
name: linked-skill
linked_tool_ids:
  - builtin:bash
  - builtin:read
---

Use linked runtime tools.
"""
    result = parse_skill_md(content)
    assert result["linked_tool_ids"] == ["builtin:bash", "builtin:read"]


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detect_format_skill_md() -> None:
    assert detect_format("---\nname: test\n---\n\nInstructions") == "skill_md"
    assert detect_format("# Just markdown") == "skill_md"


def test_detect_format_cognis_yaml() -> None:
    assert detect_format("schema_version: 1\nname: test\ninstructions: hello") == "cognis_yaml"


def test_parse_skill_content_auto_detect() -> None:
    md = "---\nname: test\n---\n\nHello"
    result = parse_skill_content(md)
    assert result["name"] == "test"
    assert "Hello" in result["instructions"]


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def test_compute_content_hash_deterministic() -> None:
    h1 = compute_content_hash("instructions", [{"name": "t"}], None, {"k": "v"})
    h2 = compute_content_hash("instructions", [{"name": "t"}], None, {"k": "v"})
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_content_hash_differs_on_change() -> None:
    h1 = compute_content_hash("instructions A")
    h2 = compute_content_hash("instructions B")
    assert h1 != h2


def test_compute_content_hash_differs_for_assets_and_placeholders() -> None:
    asset_manifest = [
        {
            "filename": "tool.py",
            "asset_id": "sa_123",
            "artifact_namespace": "skills",
            "artifact_object_id": "ska_123",
            "content_hash": "abc",
            "size_bytes": 10,
            "content_type": "text/x-python",
        }
    ]
    h1 = compute_content_hash(
        "instructions",
        [{"name": "t"}],
        None,
        {"k": "v"},
        ["API_KEY"],
        asset_manifest,
    )
    h2 = compute_content_hash(
        "instructions",
        [{"name": "t"}],
        None,
        {"k": "v"},
        ["API_KEY"],
        [],
    )
    assert h1 != h2


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_skill_md_round_trip() -> None:
    from cognis.models.skill import SkillExportData

    data = SkillExportData(
        name="test-skill",
        description="A test",
        tags=["demo"],
        instructions="# Hello\n\nWorld",
    )
    exported = export_skill_md(data)
    assert "---" in exported
    assert "test-skill" in exported
    assert "# Hello" in exported

    # Parse it back
    parsed = parse_skill_md(exported)
    assert parsed["name"] == "test-skill"
    assert "Hello" in parsed["instructions"]


def test_export_cognis_yaml_round_trip() -> None:
    from cognis.models.skill import SkillExportData

    data = SkillExportData(
        name="yaml-skill",
        description="YAML test",
        instructions="Do things",
        tags=["yaml"],
    )
    exported = export_cognis_yaml(data)
    assert "yaml-skill" in exported

    from cognis.tools.skill_parser import parse_cognis_yaml

    parsed = parse_cognis_yaml(exported)
    assert parsed["name"] == "yaml-skill"
    assert parsed["instructions"] == "Do things"


def test_export_cognis_package_round_trip() -> None:
    from cognis.models.skill import SkillAssetRef, SkillExportData

    data = SkillExportData(
        name="packaged-skill",
        description="Package test",
        instructions="Use the packaged asset.",
        asset_manifest=[
            SkillAssetRef(
                filename="scripts/run.py",
                asset_id="sa_1",
                artifact_namespace="skills",
                artifact_object_id="ska_1",
                content_hash="abc",
                size_bytes=14,
                content_type="text/x-python",
            )
        ],
    )
    exported = export_cognis_package(data, {"scripts/run.py": b"print('hello')\n"})
    parsed, _member = parse_cognis_package(exported)
    assert parsed["name"] == "packaged-skill"
    assert parsed["instructions"] == "Use the packaged asset."
    assert parsed["assets"][0]["filename"] == "scripts/run.py"


def test_normalize_linked_tool_ids_deduplicates() -> None:
    assert normalize_linked_tool_ids(["builtin:bash", "builtin:bash", " builtin:read "]) == [
        "builtin:bash",
        "builtin:read",
    ]


def test_normalize_skill_asset_filename_preserves_dotfiles() -> None:
    assert normalize_skill_asset_filename(".env") == ".env"
    assert normalize_skill_asset_filename("./.config/toolrc") == ".config/toolrc"


# ---------------------------------------------------------------------------
# GitHub URL resolution
# ---------------------------------------------------------------------------


def test_resolve_github_blob_url() -> None:
    url = "https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md"
    raw_url, sha = resolve_github_url(url)
    assert (
        raw_url
        == "https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/SKILL.md"
    )
    assert sha is None


def test_resolve_github_tree_url() -> None:
    url = "https://github.com/anthropics/skills/tree/main/skills/skill-creator"
    raw_url, sha = resolve_github_url(url)
    assert (
        raw_url
        == "https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/SKILL.md"
    )
    assert sha is None


def test_resolve_github_raw_url_passthrough() -> None:
    url = "https://raw.githubusercontent.com/anthropics/skills/main/skills/skill-creator/SKILL.md"
    raw_url, sha = resolve_github_url(url)
    assert raw_url == url
    assert sha is None


def test_resolve_github_commit_sha() -> None:
    sha_ref = "a" * 40
    url = f"https://github.com/user/repo/blob/{sha_ref}/SKILL.md"
    raw_url, sha = resolve_github_url(url)
    assert sha == sha_ref


# ---------------------------------------------------------------------------
# Resolved skill set
# ---------------------------------------------------------------------------


def test_resolved_skill_set_all_instructions() -> None:
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="a",
                name="A",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Do A",
            ),
            ResolvedSkill(
                skill_id="b",
                name="B",
                version_id="v2",
                version_number=1,
                content_hash="h2",
                instructions="Do B",
            ),
        ]
    )
    assert skill_set.all_instructions == ["Do A", "Do B"]


def test_resolved_skill_set_all_tools() -> None:
    tool = SkillToolSpec(name="my_tool", description="test")
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="a",
                name="A",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Do A",
                tools=[tool],
            ),
        ]
    )
    assert len(skill_set.all_tools) == 1
    assert skill_set.all_tools[0].name == "my_tool"


# ---------------------------------------------------------------------------
# Skill tools to ToolDefinitions
# ---------------------------------------------------------------------------


def test_skill_tools_to_definitions() -> None:
    from cognis.models.skill import SkillToolRecipe

    recipe = SkillToolRecipe(mode="script", entry="run.sh")
    tool = SkillToolSpec(name="my_tool", description="test tool", read_only=True, recipe=recipe)
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="skill-a",
                name="A",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Do A",
                tools=[tool],
                attached=True,
            ),
        ]
    )
    definitions = skill_tools_to_definitions(skill_set)
    assert len(definitions) == 1
    assert definitions[0].name == "skill_skill-a__my_tool"
    assert definitions[0].source.type == "skill"
    assert definitions[0].source.skill_id == "skill-a"
    assert definitions[0].source.raw_tool_name == "my_tool"
    assert definitions[0].source.skill_version_id == "v1"
    assert definitions[0].source.skill_content_hash == "h1"
    assert definitions[0].read_only is True
    assert definitions[0].category == "skill"
    # Execution metadata should carry the recipe
    assert definitions[0].execution_metadata is not None
    assert definitions[0].execution_metadata["recipe"]["mode"] == "script"
    assert definitions[0].execution_metadata["recipe"]["entry"] == "run.sh"


def test_skill_tools_to_definitions_excludes_unattached_by_default() -> None:
    tool = SkillToolSpec(name="my_tool", description="test tool")
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="skill-a",
                name="A",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Do A",
                tools=[tool],
                attached=False,
            )
        ]
    )

    assert skill_tools_to_definitions(skill_set) == []
    assert len(discoverable_skill_tools_to_definitions(skill_set)) == 1


def test_attached_skill_tool_ids_by_skill_includes_linked_and_bundled_tools() -> None:
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="skill_release",
                name="Release",
                linked_tool_ids=["builtin:bash"],
                version_id="sv_1",
                version_number=1,
                content_hash="abc",
                instructions="Run the release helper.",
                tools=[
                    SkillToolSpec(
                        name="run_release",
                        description="Run the release workflow.",
                    )
                ],
                attached=True,
            )
        ]
    )

    assert attached_skill_tool_ids_by_skill(skill_set) == {
        "skill_release": ["builtin:bash", "skill:skill_release:run_release"]
    }


# ---------------------------------------------------------------------------
# Available skills metadata (compact prompt block)
# ---------------------------------------------------------------------------


def test_build_available_skills_metadata_empty() -> None:
    skill_set = ResolvedSkillSet(skills=[])
    assert build_available_skills_metadata(skill_set) == ""


def test_build_available_skills_metadata_basic() -> None:
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="git-release",
                name="Git Release",
                description="Automate git release workflows",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Automate git release workflows with tagging and changelog generation.",
                tools=[SkillToolSpec(name="tag_release", description="Tag a release")],
                attached=True,
            ),
        ]
    )
    metadata = build_available_skills_metadata(skill_set)
    assert "<available_skills>" in metadata
    assert "<name>Git Release</name>" in metadata
    assert "<skill_id>git-release</skill_id>" in metadata
    assert "<tools>tag_release</tools>" in metadata
    # Description should use the real description field
    assert "Automate git release workflows" in metadata
    assert "<attached>true</attached>" in metadata
    # Should NOT contain version ids (for prompt caching stability)
    assert "v1" not in metadata
    assert "h1" not in metadata


def test_build_available_skills_metadata_fallback_description() -> None:
    """When description is None, fall back to truncated instruction snippet."""
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="no-desc",
                name="No Description",
                description=None,
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="This skill does something specific and useful.",
            ),
        ]
    )
    metadata = build_available_skills_metadata(skill_set)
    assert "This skill does something specific" in metadata


def test_build_available_skills_metadata_attach_to_all_agents() -> None:
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="always-on",
                name="Always On",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Always active skill.",
                auto_load=True,
                attached=True,
            ),
        ]
    )
    metadata = build_available_skills_metadata(skill_set)
    assert "<attach_to_all_agents>true</attach_to_all_agents>" in metadata


def test_build_available_skills_metadata_marks_auto_loaded_skills_loaded() -> None:
    skill_set = ResolvedSkillSet(
        skills=[
            ResolvedSkill(
                skill_id="coding",
                name="Coding",
                version_id="v1",
                version_number=1,
                content_hash="h1",
                instructions="Coding discipline.",
                attached=True,
                auto_load_instructions=True,
            ),
        ]
    )

    metadata = build_available_skills_metadata(skill_set)
    assert "<loaded>true</loaded>" in metadata
    assert "<auto_load_instructions>true</auto_load_instructions>" in metadata


# ---------------------------------------------------------------------------
# skill_load tool
# ---------------------------------------------------------------------------


def test_skill_load_tool_exists() -> None:
    from cognis.tools.builtin.skill_management import skill_management_tools

    tools = skill_management_tools()
    names = {t.name for t in tools}
    assert "skill_load" in names
    # skill_load should be read-only
    skill_load = next(t for t in tools if t.name == "skill_load")
    assert skill_load.read_only is True


def test_resolved_skill_tool_ids_supports_legacy_dict_payloads() -> None:
    from cognis.tools.builtin.skill_management import _resolved_skill_tool_ids

    tool_ids = _resolved_skill_tool_ids(
        "legacy-skill",
        "Legacy Skill",
        None,
        False,
        "Instructions",
        {
            "run": {
                "name": "run",
                "description": "Run tool",
                "parameters": {"type": "object", "properties": {}},
            }
        },
    )

    assert tool_ids == {"skill:legacy-skill:run"}


def test_skill_management_tool_count() -> None:
    from cognis.tools.builtin.skill_management import skill_management_tools

    tools = skill_management_tools()
    assert len(tools) == 11


def test_skill_load_runtime_summaries_include_callable_names() -> None:
    from cognis.tools.builtin.skill_management import _skill_tool_runtime_summaries

    summaries = _skill_tool_runtime_summaries(
        "youtube-transcript",
        [
            {
                "name": "get_transcript",
                "description": "Fetch a transcript",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                "recipe": {
                    "mode": "script",
                    "entry": "assets/youtube_transcript.py",
                    "required_assets": ["assets/youtube_transcript.py"],
                },
            }
        ],
    )

    assert summaries == [
        {
            "name": "get_transcript",
            "callable_name": "skill_youtube-transcript__get_transcript",
            "stable_tool_id": "skill:youtube-transcript:get_transcript",
            "description": "Fetch a transcript",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            "recipe": {
                "mode": "script",
                "entry": "assets/youtube_transcript.py",
                "required_assets": ["assets/youtube_transcript.py"],
            },
        }
    ]


def test_skill_load_asset_manifest_strips_internal_references() -> None:
    from cognis.tools.builtin.skill_management import _skill_asset_llm_manifest

    manifest = _skill_asset_llm_manifest(
        [
            SkillAssetRef(
                filename="assets/youtube_transcript.py",
                asset_id="sa-script",
                artifact_namespace="skills",
                artifact_object_id="ska-private",
                content_hash="a" * 64,
                size_bytes=12,
                content_type="text/x-python",
                url="https://controller.test/signed",
                signed_url="https://storage.test/private",
            )
        ]
    )

    assert manifest == [
        {
            "filename": "assets/youtube_transcript.py",
            "asset_id": "sa-script",
            "content_hash": "a" * 64,
            "size_bytes": 12,
            "content_type": "text/x-python",
        }
    ]


# ---------------------------------------------------------------------------
# URL import security
# ---------------------------------------------------------------------------


def test_validate_import_url_blocks_private_ips() -> None:
    import pytest

    from cognis.tools.skill_import import validate_import_url

    # localhost is allowed for development
    validate_import_url("http://localhost:8080/skill.md")

    # HTTPS required for non-localhost
    with pytest.raises(ValueError, match="HTTPS"):
        validate_import_url("http://example.com/skill.md")


def test_validate_import_url_requires_hostname() -> None:
    import pytest

    from cognis.tools.skill_import import validate_import_url

    with pytest.raises(ValueError):
        validate_import_url("file:///etc/passwd")
