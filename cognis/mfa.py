"""Focused TOTP MFA persistence and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import (
    BrowserSession,
    MfaAttemptBudget,
    MfaChallenge,
    MfaRecoveryCode,
    NativeSession,
    User,
    UserTotpFactor,
)

MFA_POLICY_KEY = "security.mfa_policy"
CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_MAX_ATTEMPTS = 5
MFA_ATTEMPT_WINDOW = timedelta(minutes=5)
MFA_ATTEMPT_LIMIT = 5
RECOVERY_CODE_COUNT = 10
CHALLENGE_CLEANUP_LIMIT = 100
CONSUMED_CHALLENGE_RETENTION = timedelta(days=1)
_AAD = b"cognis/totp-secret/v1"


class MfaSecretCipher:
    """Encrypt TOTP secrets with the existing Cognis secrets key."""

    def __init__(self, key_path: str | Path) -> None:
        self._key = base64.urlsafe_b64decode(Path(key_path).read_bytes())

    def encrypt(self, secret: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key).encrypt(nonce, secret.encode(), _AAD)

    def decrypt(self, encrypted: bytes) -> str:
        return AESGCM(self._key).decrypt(encrypted[:12], encrypted[12:], _AAD).decode()

    @property
    def key_material(self) -> bytes:
        """Return the process-loaded shared Cognis secrets key material."""
        return self._key


@dataclass(frozen=True)
class ChallengeIssue:
    token: str
    row: MfaChallenge


def hash_mfa_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_recovery_code(code: str) -> str:
    return code.strip().upper().replace("-", "")


def generate_recovery_codes() -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return [
        f"{''.join(secrets.choice(alphabet) for _ in range(5))}-"
        f"{''.join(secrets.choice(alphabet) for _ in range(5))}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]


async def create_mfa_challenge(
    session: AsyncSession,
    *,
    user_email: str,
    purpose: Literal["login_verify", "login_setup", "account_setup"],
    login_mode: Literal["browser", "native"],
    auth_version: int,
    pending_secret: bytes | None = None,
    now: datetime | None = None,
) -> ChallengeIssue:
    issued_at = now or datetime.now(UTC)
    await cleanup_mfa_challenges(session, now=issued_at)
    token = secrets.token_urlsafe(32)
    row = MfaChallenge(
        challenge_id=f"mfc_{uuid.uuid4().hex}",
        token_hash=hash_mfa_token(token),
        user_email=user_email,
        purpose=purpose,
        method="totp",
        login_mode=login_mode,
        auth_version=auth_version,
        pending_secret=pending_secret,
        attempts=0,
        max_attempts=CHALLENGE_MAX_ATTEMPTS,
        expires_at=issued_at + CHALLENGE_TTL,
    )
    session.add(row)
    await session.flush()
    return ChallengeIssue(token=token, row=row)


async def cleanup_mfa_challenges(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = CHALLENGE_CLEANUP_LIMIT,
) -> int:
    """Delete a bounded batch of expired or old consumed MFA challenges."""

    current = now or datetime.now(UTC)
    candidate_ids = (
        select(MfaChallenge.challenge_id)
        .where(
            or_(
                MfaChallenge.expires_at <= current,
                MfaChallenge.consumed_at <= current - CONSUMED_CHALLENGE_RETENTION,
            )
        )
        .order_by(MfaChallenge.created_at)
        .limit(max(1, limit))
    )
    result = await session.execute(
        delete(MfaChallenge).where(MfaChallenge.challenge_id.in_(candidate_ids))
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def get_live_challenge(
    session: AsyncSession,
    token: str,
    *,
    purposes: set[str],
    now: datetime | None = None,
) -> MfaChallenge | None:
    current = now or datetime.now(UTC)
    return (
        await session.execute(
            select(MfaChallenge)
            .join(User, User.email == MfaChallenge.user_email)
            .where(
                MfaChallenge.token_hash == hash_mfa_token(token),
                MfaChallenge.purpose.in_(purposes),
                MfaChallenge.consumed_at.is_(None),
                MfaChallenge.expires_at > current,
                MfaChallenge.attempts < MfaChallenge.max_attempts,
                MfaChallenge.auth_version == User.auth_version,
            )
        )
    ).scalar_one_or_none()


async def reserve_challenge_attempt(
    session: AsyncSession, challenge_id: str, *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    result = await session.execute(
        update(MfaChallenge)
        .where(
            MfaChallenge.challenge_id == challenge_id,
            MfaChallenge.consumed_at.is_(None),
            MfaChallenge.expires_at > current,
            MfaChallenge.attempts < MfaChallenge.max_attempts,
            MfaChallenge.auth_version
            == select(User.auth_version)
            .where(User.email == MfaChallenge.user_email)
            .scalar_subquery(),
        )
        .values(attempts=MfaChallenge.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def mfa_attempts_limited(
    session: AsyncSession, *, user_email: str, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    limited = await session.scalar(
        select(MfaAttemptBudget.user_email).where(
            MfaAttemptBudget.user_email == user_email,
            MfaAttemptBudget.window_started_at > current - MFA_ATTEMPT_WINDOW,
            MfaAttemptBudget.attempts >= MFA_ATTEMPT_LIMIT,
        )
    )
    return limited is not None


async def reserve_user_mfa_attempt(
    session: AsyncSession, *, user_email: str, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    cutoff = current - MFA_ATTEMPT_WINDOW
    dialect = session.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    insert_statement = insert_fn(MfaAttemptBudget).values(
        user_email=user_email,
        attempts=1,
        window_started_at=current,
        updated_at=current,
    )
    statement = insert_statement.on_conflict_do_update(
        index_elements=[MfaAttemptBudget.user_email],
        set_={
            "attempts": case(
                (MfaAttemptBudget.window_started_at <= cutoff, 1),
                else_=MfaAttemptBudget.attempts + 1,
            ),
            "window_started_at": case(
                (MfaAttemptBudget.window_started_at <= cutoff, current),
                else_=MfaAttemptBudget.window_started_at,
            ),
            "updated_at": current,
        },
    ).returning(MfaAttemptBudget.attempts, MfaAttemptBudget.window_started_at)
    result = (await session.execute(statement)).one()
    return int(result.attempts) <= MFA_ATTEMPT_LIMIT


async def clear_user_mfa_attempts(session: AsyncSession, *, user_email: str) -> None:
    await session.execute(delete(MfaAttemptBudget).where(MfaAttemptBudget.user_email == user_email))


async def invalidate_other_setup_challenges(
    session: AsyncSession,
    *,
    user_email: str,
    keep_challenge_id: str,
    now: datetime | None = None,
) -> None:
    await session.execute(
        update(MfaChallenge)
        .where(
            MfaChallenge.user_email == user_email,
            MfaChallenge.challenge_id != keep_challenge_id,
            MfaChallenge.purpose.in_({"login_setup", "account_setup"}),
            MfaChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now or datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def consume_challenge(
    session: AsyncSession, challenge_id: str, *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    result = await session.execute(
        update(MfaChallenge)
        .where(
            MfaChallenge.challenge_id == challenge_id,
            MfaChallenge.consumed_at.is_(None),
            MfaChallenge.expires_at > current,
            MfaChallenge.attempts <= MfaChallenge.max_attempts,
            MfaChallenge.auth_version
            == select(User.auth_version)
            .where(User.email == MfaChallenge.user_email)
            .scalar_subquery(),
        )
        .values(consumed_at=current)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def get_active_totp_factor(session: AsyncSession, user_email: str) -> UserTotpFactor | None:
    return (
        await session.execute(
            select(UserTotpFactor).where(
                UserTotpFactor.user_email == user_email,
                UserTotpFactor.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


def matching_totp_counter(secret: str, code: str, *, now: datetime) -> int | None:
    if len(code) != 6 or not code.isdigit():
        return None
    counter = int(now.timestamp()) // 30
    totp = pyotp.TOTP(secret, digits=6, interval=30, digest=hashlib.sha1)
    for candidate in (counter, counter - 1, counter + 1):
        if secrets.compare_digest(totp.at(candidate * 30), code):
            return candidate
    return None


async def verify_totp_code(
    session: AsyncSession,
    *,
    factor: UserTotpFactor,
    code: str,
    cipher: MfaSecretCipher,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    counter = matching_totp_counter(cipher.decrypt(factor.encrypted_secret), code, now=current)
    if counter is None:
        return False
    result = await session.execute(
        update(UserTotpFactor)
        .where(
            UserTotpFactor.user_email == factor.user_email,
            UserTotpFactor.is_active.is_(True),
            or_(
                UserTotpFactor.last_accepted_counter.is_(None),
                UserTotpFactor.last_accepted_counter < counter,
            ),
        )
        .values(last_accepted_counter=counter)
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def use_recovery_code(
    session: AsyncSession, *, user_email: str, code: str, now: datetime | None = None
) -> bool:
    code_hash = hash_mfa_token(normalize_recovery_code(code))
    result = await session.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.user_email == user_email,
            MfaRecoveryCode.code_hash == code_hash,
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=now or datetime.now(UTC))
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


async def verify_totp_or_recovery(
    session: AsyncSession,
    *,
    user_email: str,
    code: str,
    cipher: MfaSecretCipher,
    now: datetime | None = None,
) -> bool:
    factor = await get_active_totp_factor(session, user_email)
    if factor is None:
        return False
    if await verify_totp_code(session, factor=factor, code=code, cipher=cipher, now=now):
        return True
    return await use_recovery_code(session, user_email=user_email, code=code, now=now)


async def activate_totp_factor(
    session: AsyncSession,
    *,
    user_email: str,
    encrypted_secret: bytes,
    accepted_counter: int,
    now: datetime | None = None,
) -> list[str] | None:
    current = now or datetime.now(UTC)
    try:
        async with session.begin_nested():
            session.add(
                UserTotpFactor(
                    user_email=user_email,
                    encrypted_secret=encrypted_secret,
                    is_active=True,
                    last_accepted_counter=accepted_counter,
                    activated_at=current,
                )
            )
            await session.flush()
    except IntegrityError:
        return None
    return await replace_recovery_codes(session, user_email=user_email)


async def replace_recovery_codes(session: AsyncSession, *, user_email: str) -> list[str]:
    codes = generate_recovery_codes()
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_email == user_email))
    session.add_all(
        [
            MfaRecoveryCode(
                code_id=f"mfr_{uuid.uuid4().hex}",
                user_email=user_email,
                code_hash=hash_mfa_token(normalize_recovery_code(code)),
            )
            for code in codes
        ]
    )
    await session.flush()
    return codes


async def revoke_user_auth_sessions(session: AsyncSession, *, user_email: str) -> None:
    current = datetime.now(UTC)
    await session.execute(
        update(BrowserSession)
        .where(BrowserSession.user_email == user_email, BrowserSession.revoked_at.is_(None))
        .values(revoked_at=current)
    )
    await session.execute(
        update(NativeSession)
        .where(NativeSession.user_email == user_email, NativeSession.revoked_at.is_(None))
        .values(revoked_at=current)
    )
    await session.execute(
        update(User).where(User.email == user_email).values(auth_version=User.auth_version + 1)
    )


async def reset_user_mfa(session: AsyncSession, *, user_email: str) -> None:
    await session.execute(delete(MfaAttemptBudget).where(MfaAttemptBudget.user_email == user_email))
    await session.execute(delete(MfaChallenge).where(MfaChallenge.user_email == user_email))
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_email == user_email))
    await session.execute(delete(UserTotpFactor).where(UserTotpFactor.user_email == user_email))
    await revoke_user_auth_sessions(session, user_email=user_email)
