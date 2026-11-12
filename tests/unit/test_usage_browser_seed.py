"""Qualify only the synthetic ledger helper, not real app/browser acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from argon2.exceptions import VerifyMismatchError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.security import create_password_hasher
from cognis.store.database import create_engine
from cognis.store.models import Base, Conversation, ProjectGrantRow, ProjectRow, User
from cognis.store.queries import create_agent, create_user
from cognis.store.usage.models import UsageAttemptRow
from tests.e2e.usage_checkpoint import seed as seed_module
from tests.e2e.usage_checkpoint.seed import (
    MEMBER,
    PROJECT,
    revoke,
    runtime,
    seed,
    validate_sign_in_identities,
)


@pytest.mark.asyncio
async def test_canonical_seed_prices_unknowns_and_scoped_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.setenv("COGNIS_USAGE_RUN_DIR", str(tmp_path))
    database = tmp_path / "cognis.db"
    actor = "usage-browser-seed-admin@cognis-test.example.com"
    password = "synthetic-usage-seed-unit-password"
    agent = "agent_usage_browser_seed_unit"
    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            await create_user(
                session, actor, "Seed admin", create_password_hasher().hash(password), role="admin"
            )
            await create_agent(session, agent_id=agent, owner_email=actor, name="Usage seed unit")
        (tmp_path / "runtime.json").write_text(
            json.dumps(
                {
                    "database": str(database),
                    "actor": actor,
                    "agent": agent,
                    "fixture_password": password,
                }
            )
        )
        result = await seed()
        assert result["priced_count"] == 10 and result["unpriced_count"] == 2
        assert result["canonical_writer"] and result["canonical_projector"]
        assert result["final_capture_qualification"] is False
        assert result["member"] == MEMBER
        assert result["sign_in_preflight"] == [
            {
                "email": email,
                "role": role,
                "active": True,
                "login_request_valid": True,
                "password_verified": True,
            }
            for email, role in ((actor, "admin"), (MEMBER, "user"))
        ]
        assert password not in json.dumps(result)
        await revoke()
        async with sessions() as session:
            member = await session.get(User, MEMBER)
            assert member is not None and member.is_active and member.role == "user"
            assert create_password_hasher().verify(member.password_hash, password)
            project = await session.get(ProjectRow, PROJECT)
            assert project is not None and project.owner_email == MEMBER
            owners = set(
                await session.scalars(
                    select(Conversation.user_email).where(Conversation.project_id == PROJECT)
                )
            )
            assert owners == {actor, MEMBER}
            attempts = list(await session.scalars(select(UsageAttemptRow)))
            assert {
                row.context_payload["attribution"]["identity"]["principal_subject"]
                for row in attempts
            } == {actor, MEMBER}
            grant = (
                await session.scalars(
                    select(ProjectGrantRow).where(
                        ProjectGrantRow.project_id == PROJECT,
                        ProjectGrantRow.grantee_user_email == actor,
                    )
                )
            ).one()
            assert grant.revoked_at is not None
            assert grant.granted_by == MEMBER
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault, expected",
    [
        ("invalid_admin_email", ValidationError),
        ("invalid_member_email", ValidationError),
        ("inactive_member", AssertionError),
        ("wrong_role_member", AssertionError),
        ("wrong_password_member", VerifyMismatchError),
    ],
)
async def test_sign_in_preflight_rejects_unusable_seeded_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, expected: type[Exception]
) -> None:
    actor = (
        "usage-browser-seed-admin@cognis.test"
        if fault == "invalid_admin_email"
        else "usage-browser-seed-admin@cognis-test.example.com"
    )
    member = "usage-browser-member@cognis.test" if fault == "invalid_member_email" else MEMBER
    monkeypatch.setattr(seed_module, "MEMBER", member)
    password = "synthetic-usage-seed-unit-password"
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            hasher = create_password_hasher()
            await create_user(session, actor, "Seed admin", hasher.hash(password), role="admin")
            user = await create_user(
                session,
                member,
                "Seed member",
                hasher.hash(password + fault if fault == "wrong_password_member" else password),
                role="admin" if fault == "wrong_role_member" else "user",
            )
            user.is_active = fault != "inactive_member"
        with pytest.raises(expected):
            await validate_sign_in_identities(sessions, actor, password)
    finally:
        await engine.dispose()


def test_seed_refuses_database_outside_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.setenv("COGNIS_USAGE_RUN_DIR", str(output))
    (output / "runtime.json").write_text(
        json.dumps(
            {
                "database": str(tmp_path / "cognis.db"),
            }
        )
    )
    with pytest.raises(AssertionError):
        runtime()
