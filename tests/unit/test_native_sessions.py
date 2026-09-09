from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from cognis.bootstrap import run_schema_bootstrap
from cognis.mfa import create_mfa_challenge, get_live_challenge, revoke_user_auth_sessions
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import NativeSession
from cognis.store.queries import (
    NativeSessionRotation,
    create_browser_session,
    create_native_session,
    create_user,
    get_browser_session_by_token,
    get_user,
    rotate_native_session,
)


@pytest.mark.asyncio
async def test_concurrent_rotation_replay_revokes_winning_successor(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'native-rotation.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    expires_at = datetime.now(UTC) + timedelta(days=1)
    async with factory() as session:
        await create_user(
            session,
            email="native@example.com",
            name="Native",
            password_hash="hash",
            role="user",
        )
        _, refresh_token = await create_native_session(
            session,
            user_email="native@example.com",
            expires_at=expires_at,
            auth_version=0,
        )
        await session.commit()

    async def rotate() -> NativeSessionRotation:
        async with factory() as session:
            result = await rotate_native_session(
                session,
                refresh_token=refresh_token,
                expires_at=expires_at,
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(rotate(), rotate())
    winner = first if first.status == "rotated" else second
    assert winner.status == "rotated"
    assert {first.status, second.status} == {"rotated", "replay"}
    assert winner.refresh_token is not None

    async with factory() as session:
        rows = (await session.execute(select(NativeSession))).scalars().all()
        assert len({row.family_id for row in rows}) == 1
        assert all(row.revoked_at is not None for row in rows)

    async with factory() as session:
        successor_result = await rotate_native_session(
            session,
            refresh_token=winner.refresh_token,
            expires_at=expires_at,
        )
        await session.commit()
    assert successor_result.status == "replay"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_concurrent_sessions_and_challenges_cannot_authenticate(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth-version-race.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    expires_at = datetime.now(UTC) + timedelta(days=1)
    async with factory() as seed_session:
        await create_user(
            seed_session,
            email="race@example.com",
            name="Race",
            password_hash="hash",
            role="user",
        )
        await seed_session.commit()

    async with factory() as stale_session:
        stale_user = await get_user(stale_session, "race@example.com")
        assert stale_user is not None
        stale_version = stale_user.auth_version

    async with factory() as reset_session:
        await revoke_user_auth_sessions(reset_session, user_email="race@example.com")
        await reset_session.commit()

    async with factory() as stale_insert_session:
        native_row, native_token = await create_native_session(
            stale_insert_session,
            user_email="race@example.com",
            expires_at=expires_at,
            auth_version=stale_version,
        )
        family_id = native_row.family_id
        _, browser_token = await create_browser_session(
            stale_insert_session,
            user_email="race@example.com",
            expires_at=expires_at,
            auth_version=stale_version,
        )
        challenge = await create_mfa_challenge(
            stale_insert_session,
            user_email="race@example.com",
            purpose="login_verify",
            login_mode="browser",
            auth_version=stale_version,
        )
        await stale_insert_session.commit()

    async with factory() as verification_session:
        assert await get_browser_session_by_token(verification_session, browser_token) is None
        assert (
            await get_live_challenge(
                verification_session,
                challenge.token,
                purposes={"login_verify"},
            )
            is None
        )
        rotation = await rotate_native_session(
            verification_session,
            refresh_token=native_token,
            expires_at=expires_at,
        )
        assert rotation.status == "stale"
        await verification_session.commit()

    async with factory() as inspect_session:
        family_rows = (
            (
                await inspect_session.execute(
                    select(NativeSession).where(NativeSession.family_id == family_id)
                )
            )
            .scalars()
            .all()
        )
        assert family_rows
        assert all(row.revoked_at is not None for row in family_rows)
    await engine.dispose()
