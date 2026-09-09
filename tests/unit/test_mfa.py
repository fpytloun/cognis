from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import select

from cognis.bootstrap import run_schema_bootstrap
from cognis.mfa import (
    MfaSecretCipher,
    activate_totp_factor,
    cleanup_mfa_challenges,
    create_mfa_challenge,
    reserve_user_mfa_attempt,
    use_recovery_code,
    verify_totp_code,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import MfaChallenge, UserTotpFactor
from cognis.store.queries import create_user


@pytest.mark.asyncio
async def test_totp_counter_and_recovery_code_are_concurrency_safe(tmp_path: Path) -> None:
    key_path = tmp_path / "secrets.key"
    import base64
    import os

    key_path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    cipher = MfaSecretCipher(key_path)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'mfa.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    secret = pyotp.random_base32()
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    counter = int(now.timestamp()) // 30
    async with factory() as session:
        await create_user(
            session,
            email="mfa@example.com",
            name="MFA",
            password_hash="hash",
            role="user",
        )
        recovery_codes = await activate_totp_factor(
            session,
            user_email="mfa@example.com",
            encrypted_secret=cipher.encrypt(secret),
            accepted_counter=counter - 1,
            now=now,
        )
        assert recovery_codes is not None
        await session.commit()

    code = pyotp.TOTP(secret).at(now)

    async def verify_totp() -> bool:
        async with factory() as session:
            factor = await session.get(UserTotpFactor, "mfa@example.com")
            assert factor is not None
            result = await verify_totp_code(
                session, factor=factor, code=code, cipher=cipher, now=now
            )
            await session.commit()
            return result

    assert sorted(await asyncio.gather(verify_totp(), verify_totp())) == [False, True]

    async def recover() -> bool:
        async with factory() as session:
            result = await use_recovery_code(
                session,
                user_email="mfa@example.com",
                code=recovery_codes[0],
                now=now,
            )
            await session.commit()
            return result

    assert sorted(await asyncio.gather(recover(), recover())) == [False, True]

    async def reserve_attempt() -> bool:
        async with factory() as session:
            allowed = await reserve_user_mfa_attempt(
                session,
                user_email="mfa@example.com",
                now=now,
            )
            await session.commit()
            return allowed

    reservations = await asyncio.gather(*(reserve_attempt() for _ in range(8)))
    assert reservations.count(True) == 5
    assert reservations.count(False) == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_challenge_cleanup_is_bounded_and_preserves_live_rows(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'mfa-cleanup.db'}")
    factory = create_session_factory(engine)
    await run_schema_bootstrap(engine)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    async with factory() as session:
        await create_user(
            session,
            email="cleanup@example.com",
            name="Cleanup",
            password_hash="hash",
            role="user",
        )
        session.add_all(
            [
                MfaChallenge(
                    challenge_id="expired",
                    token_hash="expired-hash",
                    user_email="cleanup@example.com",
                    purpose="login_verify",
                    method="totp",
                    login_mode="browser",
                    attempts=0,
                    max_attempts=5,
                    expires_at=now - timedelta(minutes=1),
                    created_at=now - timedelta(days=3),
                ),
                MfaChallenge(
                    challenge_id="old-consumed",
                    token_hash="old-consumed-hash",
                    user_email="cleanup@example.com",
                    purpose="login_verify",
                    method="totp",
                    login_mode="browser",
                    attempts=1,
                    max_attempts=5,
                    expires_at=now + timedelta(days=1),
                    consumed_at=now - timedelta(days=2),
                    created_at=now - timedelta(days=2),
                ),
                MfaChallenge(
                    challenge_id="recent-consumed",
                    token_hash="recent-consumed-hash",
                    user_email="cleanup@example.com",
                    purpose="login_verify",
                    method="totp",
                    login_mode="browser",
                    attempts=1,
                    max_attempts=5,
                    expires_at=now + timedelta(days=1),
                    consumed_at=now - timedelta(minutes=1),
                    created_at=now - timedelta(minutes=2),
                ),
                MfaChallenge(
                    challenge_id="live",
                    token_hash="live-hash",
                    user_email="cleanup@example.com",
                    purpose="login_verify",
                    method="totp",
                    login_mode="browser",
                    attempts=0,
                    max_attempts=5,
                    expires_at=now + timedelta(minutes=5),
                    created_at=now,
                ),
            ]
        )
        await session.commit()
        assert await cleanup_mfa_challenges(session, now=now, limit=1) == 1
        await session.commit()
        assert await cleanup_mfa_challenges(session, now=now, limit=1) == 1
        await session.commit()
        remaining = {
            row.challenge_id
            for row in (await session.execute(select(MfaChallenge))).scalars().all()
        }
    assert remaining == {"recent-consumed", "live"}
    async with factory() as session:
        session.add(
            MfaChallenge(
                challenge_id="expired-opportunistic",
                token_hash="expired-opportunistic-hash",
                user_email="cleanup@example.com",
                purpose="login_verify",
                method="totp",
                login_mode="browser",
                attempts=0,
                max_attempts=5,
                expires_at=now - timedelta(seconds=1),
                created_at=now,
            )
        )
        await session.commit()
        issued = await create_mfa_challenge(
            session,
            user_email="cleanup@example.com",
            purpose="login_verify",
            login_mode="browser",
            auth_version=0,
            now=now,
        )
        await session.commit()
        challenge_ids = {
            row.challenge_id
            for row in (await session.execute(select(MfaChallenge))).scalars().all()
        }
    assert issued.row.challenge_id in challenge_ids
    assert "expired-opportunistic" not in challenge_ids
    assert {"recent-consumed", "live"} <= challenge_ids
    await engine.dispose()
