"""Skill resolution keeps visibility and legacy fallbacks with bounded SQL reads."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from cognis.models.agent import AgentDefinition
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, SkillAssetRow, SkillRow, SkillVersionRow, User
from cognis.tools.skills import resolve_skills_for_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 67])
async def test_resolution_batches_versions_and_assets_without_changing_visibility(tmp_path, count):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/skills.db")
    sessions = create_session_factory(engine)
    owner = "user@example.com"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        User.__table__,
                        SkillRow.__table__,
                        SkillVersionRow.__table__,
                        SkillAssetRow.__table__,
                    ],
                )
            )
        async with sessions() as session:
            session.add_all(
                [
                    User(email=owner, name="Owner", password_hash="fixture-hash"),
                    User(email="other@example.com", name="Other", password_hash="fixture-hash"),
                ]
            )
            await session.flush()
            for index in range(count):
                session.add(
                    SkillRow(
                        skill_id=f"batch-{index}",
                        name=f"Skill {index:03d}",
                        instructions="Legacy",
                        owner_email=owner,
                        current_version_id=f"batch-version-{index}",
                    )
                )
            session.add_all(
                [
                    SkillRow(
                        skill_id="global",
                        name="Global",
                        instructions="Global instructions",
                        auto_load=True,
                        owner_email=None,
                    ),
                    SkillRow(
                        skill_id="missing-version",
                        name="Missing",
                        instructions="Legacy fallback",
                        owner_email=owner,
                        current_version_id="removed-version",
                    ),
                    SkillRow(
                        skill_id="hidden",
                        name="Hidden",
                        instructions="Private",
                        owner_email="other@example.com",
                        current_version_id="hidden-version",
                    ),
                    SkillRow(
                        skill_id="cognis-orchestrator",
                        name="Scoped",
                        instructions="Scoped",
                        owner_email=None,
                        current_version_id="scoped-version",
                    ),
                ]
            )
            await session.flush()
            for index in range(count):
                session.add(
                    SkillVersionRow(
                        version_id=f"batch-version-{index}",
                        skill_id=f"batch-{index}",
                        version_number=1,
                        content_hash=f"hash-{index}",
                        instructions=f"Version {index}",
                        asset_manifest=[
                            {
                                "filename": "manifest.py",
                                "asset_id": f"manifest-{index}",
                                "artifact_object_id": f"manifest-object-{index}",
                                "content_hash": "manifest-hash",
                                "size_bytes": 1,
                            }
                        ],
                    )
                )
            session.add(
                SkillVersionRow(
                    version_id="hidden-version",
                    skill_id="hidden",
                    version_number=1,
                    content_hash="hidden-hash",
                    instructions="Private version",
                )
            )
            await session.flush()
            for name in ["z.py", "a.py"]:
                session.add(
                    SkillAssetRow(
                        asset_id=f"asset-{name}",
                        skill_version_id="batch-version-0",
                        filename=name,
                        artifact_object_id=f"object-{name}",
                        content_hash=f"hash-{name}",
                        size_bytes=1,
                    )
                )
            await session.commit()

        queries = []

        def count_query(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        agent = AgentDefinition(
            agent_id="batch-agent",
            name="Batch",
            owner_email=owner,
            skills={"items": [{"skill_id": "batch-0", "auto_load_instructions": True}]},
        )
        async with sessions() as session:
            resolved = await resolve_skills_for_agent(session, agent, owner_email=owner)
        assert len(queries) == 3
        assert [skill.skill_id for skill in resolved.skills[:2]] == ["batch-0", "global"]
        by_id = {skill.skill_id: skill for skill in resolved.skills}
        assert len(by_id) == count + 2
        assert "hidden" not in by_id and "cognis-orchestrator" not in by_id
        assert by_id["batch-0"].instructions == "Version 0"
        assert by_id["batch-0"].auto_load_instructions
        assert [asset.filename for asset in by_id["batch-0"].asset_manifest] == ["a.py", "z.py"]
        if count > 1:
            assert by_id["batch-1"].asset_manifest[0].filename == "manifest.py"
        assert by_id["missing-version"].instructions == "Legacy fallback"
        assert "missing-version" not in resolved.version_snapshot
        assert len(resolved.version_snapshot) == count

        # A fresh resolution must still observe owner changes.
        async with sessions() as session:
            row = await session.get(SkillRow, "batch-0")
            row.owner_email = "other@example.com"
            await session.commit()
        async with sessions() as session:
            refreshed = await resolve_skills_for_agent(session, agent, owner_email=owner)
        assert "batch-0" not in {skill.skill_id for skill in refreshed.skills}
    finally:
        await engine.dispose()
