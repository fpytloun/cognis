from __future__ import annotations

from pathlib import Path

import pytest

import cognis.providers.credentials.encrypted_db as encrypted_db_module
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.credential import CredentialAccessError
from cognis.providers.credentials.encrypted_db import EncryptedDBCredentialsProvider
from cognis.security import create_password_hasher
from cognis.store.queries import create_user


@pytest.mark.asyncio
async def test_credentials_provider_roundtrip_and_resolution(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = EncryptedDBCredentialsProvider(session_factory, str(config.secrets_key_path))

    async with session_factory() as session:
        await create_user(
            session,
            email="user@example.com",
            name="User",
            password_hash=password_hasher.hash("password123"),
            role="user",
        )
        await create_user(
            session,
            email="other@example.com",
            name="Other",
            password_hash=password_hasher.hash("password123"),
            role="user",
        )
        await session.commit()

    await provider.upsert_credential(
        credential_id="github_work",
        user_email="user@example.com",
        kind="username_password",
        label="GitHub Work",
        payload={"username": "alice", "password": "secret"},
        metadata={"origin": "https://github.com"},
    )
    created = await provider.upsert_credential(
        credential_id="github_work_2",
        user_email="user@example.com",
        kind="token",
        label="GitHub Token",
        payload={"token": "abc"},
    )
    assert created.field_names == ["token"]

    record = await provider.get_credential("github_work", "user@example.com")
    assert record is not None
    assert record.kind == "username_password"
    assert record.field_names == ["password", "username"]
    assert record.metadata["origin"] == "https://github.com"

    claude_profile = await provider.upsert_credential(
        credential_id="claude-profile-claude-code",
        user_email="user@example.com",
        kind="claude_code_profile",
        label="Claude Code Profile",
        payload={"profile": {"oauth_token": "profile-token"}},
        metadata={"domain": "github.com", "source": "claude_agent_sdk_writeback"},
    )
    assert claude_profile.kind == "claude_code_profile"
    assert claude_profile.field_names == ["profile"]

    rows = await provider.list_credentials("user@example.com")
    rows_by_id = {row.credential_id: row for row in rows}
    assert rows_by_id["github_work"].field_names == ["password", "username"]
    assert rows_by_id["github_work_2"].field_names == ["token"]
    assert rows_by_id["claude-profile-claude-code"].kind == "claude_code_profile"
    assert rows_by_id["claude-profile-claude-code"].field_names == ["profile"]

    resolved = await provider.resolve_ref(
        "$credential:github_work.password",
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            permissions=AgentPermissions(allowed_credentials=["github_work"]),
        ),
        user_email="user@example.com",
    )
    assert resolved.value == "secret"

    assert await provider.revoke_credential("github_work", "user@example.com") is True
    with pytest.raises(CredentialAccessError, match="Credential is not active"):
        await provider.resolve_ref(
            "$credential:github_work.password",
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["github_work"]),
            ),
            user_email="user@example.com",
        )

    second = await provider.upsert_credential(
        credential_id="github_work",
        user_email="other@example.com",
        kind="token",
        label="GitHub Other",
        payload={"token": "other-token"},
    )
    assert second.user_email == "other@example.com"

    await engine.dispose()


@pytest.mark.asyncio
async def test_credentials_provider_generates_totp_from_seed(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr(encrypted_db_module.time, "time", lambda: 59)  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = EncryptedDBCredentialsProvider(session_factory, str(config.secrets_key_path))
    try:
        async with session_factory() as session:
            await create_user(
                session,
                email="user@example.com",
                name="User",
                password_hash=password_hasher.hash("password123"),
                role="user",
            )
            await session.commit()

        await provider.upsert_credential(
            credential_id="reddit_mfa",
            user_email="user@example.com",
            kind="totp_seed",
            label="Reddit MFA",
            payload={
                "secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
                "digits": 6,
                "period": 30,
                "algorithm": "sha1",
            },
        )
        resolved = await provider.resolve_ref(
            "$credential:reddit_mfa.otp",
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent",
                permissions=AgentPermissions(allowed_credentials=["reddit_mfa"]),
            ),
            user_email="user@example.com",
        )

        assert resolved.value == "287082"
    finally:
        await engine.dispose()
