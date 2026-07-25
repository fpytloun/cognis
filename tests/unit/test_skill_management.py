from __future__ import annotations

import json

import pytest

import cognis.store.queries as skill_queries
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import create_skill, create_user, get_skill_scoped
from cognis.tools.builtin.skill_management import (
    _handle_skill_get,
    _handle_skill_load,
    _handle_skill_patch,
    _handle_skill_write,
)
from cognis.tools.skill_service import load_skill_asset_refs, resolve_current_skill_version


@pytest.mark.asyncio
async def test_skill_write_persists_decomposition_steps_with_step_profiles(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hashed",
        )
        await session.commit()

    result = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Planner Skill",
            "instructions": "Plan and execute work.",
            "steps": [
                {
                    "name": "research",
                    "type": "run",
                    "prompt": "Research the task.",
                    "step_profile_id": "system:research",
                    "step_profile_mode": "soft",
                },
                {
                    "name": "implement",
                    "type": "run",
                    "prompt": "Implement the solution.",
                    "step_profile_id": "system:coding",
                    "step_profile_mode": "hard",
                    "step_profile": {
                        "matrix": {"development": ["read", "write", "privileged"]},
                        "tool_overrides": {"include": ["bash"], "exclude": []},
                        "allow_tool_search": False,
                    },
                },
            ],
        },
        llm=None,
        artifact_store=artifact_store,
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    skill_id = payload["skill_id"]
    assert payload["step_count"] == 2

    async with session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email="user@example.com")
        assert row is not None
        current_version = await resolve_current_skill_version(session, row)
        assert current_version is not None
        assert current_version.steps[0]["step_profile_id"] == "system:research"
        assert current_version.steps[1]["step_profile_mode"] == "hard"
        assert current_version.steps[1]["step_profile"]["allow_tool_search"] is False

    loaded = await _handle_skill_get(session_factory, "user@example.com", {"skill_id": skill_id})
    loaded_payload = json.loads(loaded.output)
    assert loaded_payload["current_version"]["steps"][0]["step_profile_id"] == "system:research"
    assert loaded_payload["current_version"]["steps"][1]["step_profile"]["matrix"][
        "development"
    ] == [
        "read",
        "write",
        "privileged",
    ]

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_load_accepts_claude_native_skill_argument_by_name(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hashed",
        )
        await create_skill(
            session,
            skill_id="skill_lumilens_alertmanager_ops",
            name="lumilens-alertmanager-ops",
            instructions="Inspect Lumilens Alertmanager safely.",
            owner_email="user@example.com",
        )
        await session.commit()

    load_result = await _handle_skill_load(
        session_factory,
        "user@example.com",
        {"skill": "lumilens-alertmanager-ops"},
    )

    assert load_result.is_error is False
    payload = json.loads(load_result.output)
    assert payload["skill_id"] == "skill_lumilens_alertmanager_ops"

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_write_rejects_stale_decomposition_hash(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hashed",
        )
        await session.commit()

    result = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Planner Skill",
            "instructions": "Plan and execute work.",
            "decomposition_source_hash": "stale-hash",
            "steps": [{"name": "research", "type": "run", "prompt": "Research the task."}],
        },
        llm=None,
        artifact_store=artifact_store,
    )

    assert result.is_error is True
    assert "stale" in result.output

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_write_persists_linked_runtime_tool_ids(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hashed",
        )
        await session.commit()

    result = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Linked Skill",
            "instructions": "Use shell helpers.",
            "linked_tool_ids": ["builtin:bash", "builtin:read"],
        },
        llm=None,
        artifact_store=artifact_store,
    )

    assert result.is_error is False
    payload = json.loads(result.output)

    async with session_factory() as session:
        row = await get_skill_scoped(session, payload["skill_id"], owner_email="user@example.com")
        assert row is not None
        assert row.linked_tool_ids == ["builtin:bash", "builtin:read"]

    loaded = await _handle_skill_get(
        session_factory, "user@example.com", {"skill_id": payload["skill_id"]}
    )
    loaded_payload = json.loads(loaded.output)
    assert loaded_payload["linked_tool_ids"] == ["builtin:bash", "builtin:read"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_write_binds_created_and_updated_skill_to_current_agent(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash="hashed",
        )
        from cognis.store.queries import create_agent, get_agent

        await create_agent(
            session,
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent 1",
            status="active",
        )
        await session.commit()

    create_result = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Bound Skill",
            "instructions": "Use this skill.",
        },
        llm=None,
        artifact_store=artifact_store,
        current_agent_id="agent-1",
    )

    assert create_result.is_error is False
    payload = json.loads(create_result.output)
    skill_id = payload["skill_id"]
    assert create_result.metadata is not None
    assert create_result.metadata["attached_skill_id"] == skill_id

    update_result = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "skill_id": skill_id,
            "name": "Bound Skill",
            "instructions": "Use this updated skill.",
        },
        llm=None,
        artifact_store=artifact_store,
        current_agent_id="agent-1",
    )

    assert update_result.is_error is False
    assert update_result.metadata is not None
    assert update_result.metadata["attached_skill_id"] == skill_id

    async with session_factory() as session:
        agent = await get_agent(session, "agent-1")
        assert agent is not None
        items = agent.skills["items"]
        assert items == [{"skill_id": skill_id, "enabled": True}]

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_patch_preserves_omitted_content_and_edits_nested_items(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(session, email="user@example.com", name="User", password_hash="hashed")
        await session.commit()
    created = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Patchable",
            "description": "preserve me",
            "instructions": "Original",
            "prompt_templates": {"first": "one", "second": "two"},
            "steps": [
                {"name": "research", "type": "run", "prompt": "old"},
                {"name": "finish", "type": "run", "prompt": "keep"},
            ],
        },
        llm=None,
        artifact_store=artifact_store,
    )
    original = json.loads(created.output)

    patched = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "expected_current_version_id": original["version_id"],
            "instructions": "Updated",
            "prompt_templates": {"set": {"first": "changed"}},
            "steps": {"upsert": [{"name": "research", "type": "run", "prompt": "new"}]},
        },
        artifact_store=artifact_store,
    )
    assert patched.is_error is False
    patch_payload = json.loads(patched.output)
    assert patch_payload["version_created"] is True
    assert patch_payload["version_number"] == 2

    loaded = await _handle_skill_get(
        session_factory, "user@example.com", {"skill_id": original["skill_id"]}
    )
    payload = json.loads(loaded.output)
    assert payload["description"] == "preserve me"
    assert payload["current_version"]["instructions"] == "Updated"
    assert payload["current_version"]["prompt_templates"] == {
        "first": "changed",
        "second": "two",
    }
    assert [step["name"] for step in payload["current_version"]["steps"]] == [
        "research",
        "finish",
    ]
    assert [step["prompt"] for step in payload["current_version"]["steps"]] == [
        "new",
        "keep",
    ]

    stale = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "expected_current_version_id": original["version_id"],
            "instructions": "stale",
        },
        artifact_store=artifact_store,
    )
    assert stale.is_error is True
    assert "version_conflict" in stale.output

    no_op = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {"skill_id": original["skill_id"], "instructions": "Updated"},
        artifact_store=artifact_store,
    )
    assert no_op.is_error is True
    assert "no_op_patch" in no_op.output

    invalid = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "steps": {"upsert": [{"type": "run", "prompt": "missing name"}]},
        },
        artifact_store=artifact_store,
    )
    assert invalid.is_error is True
    assert "non-empty 'name'" in invalid.output

    async with session_factory() as session:
        from cognis.store.queries import list_skill_versions

        versions = await list_skill_versions(session, original["skill_id"])
        assert len(versions) == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_patch_cleans_new_asset_blob_after_publication_conflict(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_path = tmp_path / "artifacts"
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(artifact_path))
    )
    async with session_factory() as session:
        await create_user(session, email="user@example.com", name="User", password_hash="hashed")
        await session.commit()
    created = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {
            "name": "Conflict",
            "instructions": "Original",
        },
        llm=None,
        artifact_store=artifact_store,
    )
    original = json.loads(created.output)
    added = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "assets": {"upsert": [{"filename": "historical.txt", "content": "preserve me"}]},
        },
        artifact_store=artifact_store,
    )
    assert added.is_error is False
    async with session_factory() as session:
        row = await get_skill_scoped(
            session,
            original["skill_id"],
            owner_email="user@example.com",
        )
        assert row is not None
        current = await resolve_current_skill_version(session, row)
        assert current is not None
        historical_asset_id = (await load_skill_asset_refs(session, current))[0].asset_id
    removed = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "assets": {"remove": ["historical.txt"]},
        },
        artifact_store=artifact_store,
    )
    assert removed.is_error is False
    published = json.loads(removed.output)
    stored_before = {
        path.relative_to(artifact_path) for path in artifact_path.rglob("*") if path.is_file()
    }

    async def _force_conflict(*args, **kwargs):
        return False

    monkeypatch.setattr(skill_queries, "set_current_version_if_matches", _force_conflict)
    patched = await _handle_skill_patch(
        session_factory,
        "user@example.com",
        {
            "skill_id": original["skill_id"],
            "assets": {
                "upsert": [
                    {
                        "filename": "historical.txt",
                        "existing_asset_id": historical_asset_id,
                    },
                    {
                        "filename": "new.txt",
                        "content": "unpublished",
                        "content_type": "text/plain",
                    },
                ]
            },
        },
        artifact_store=artifact_store,
    )

    assert patched.is_error is True
    assert "version_conflict" in patched.output
    stored_after = {
        path.relative_to(artifact_path) for path in artifact_path.rglob("*") if path.is_file()
    }
    assert stored_after == stored_before
    async with session_factory() as session:
        row = await get_skill_scoped(
            session,
            original["skill_id"],
            owner_email="user@example.com",
        )
        assert row is not None
        assert row.current_version_id == published["version_id"]
        from cognis.store.queries import list_skill_versions

        versions = await list_skill_versions(session, original["skill_id"])
        assert len(versions) == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_write_full_replacement_remains_compatible(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    await run_schema_bootstrap(engine)
    session_factory = create_session_factory(engine)
    artifact_store = ArtifactStore(
        ArtifactStoreConfig(backend="filesystem", path=str(tmp_path / "artifacts"))
    )
    async with session_factory() as session:
        await create_user(session, email="user@example.com", name="User", password_hash="hashed")
        await session.commit()
    created = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {"name": "Replace", "instructions": "first", "prompt_templates": {"old": "value"}},
        llm=None,
        artifact_store=artifact_store,
    )
    skill_id = json.loads(created.output)["skill_id"]
    replaced = await _handle_skill_write(
        session_factory,
        "user@example.com",
        {"skill_id": skill_id, "name": "Replace", "instructions": "second"},
        llm=None,
        artifact_store=artifact_store,
    )
    assert replaced.is_error is False
    loaded = await _handle_skill_get(session_factory, "user@example.com", {"skill_id": skill_id})
    assert json.loads(loaded.output)["current_version"]["prompt_templates"] is None
    await engine.dispose()
