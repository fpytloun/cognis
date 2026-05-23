from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cognis.config import CognisConfig
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm import codex as codex_support
from cognis.providers.llm.service import LLMService
from cognis.providers.registry import build_provider_registry
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider


def _write_test_keys(tmp_path: Path) -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _test_config(tmp_path: Path, database_url: str) -> CognisConfig:
    private_key_path, public_key_path = _write_test_keys(tmp_path)
    secrets_key_path = tmp_path / "secrets.key"
    secrets_key_path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    return CognisConfig(
        data_dir=tmp_path,
        host="127.0.0.1",
        port=8080,
        mnemory_url="http://mnemory.invalid",
        intaris_url="http://intaris.invalid",
        public_mnemory_ui_url="",
        public_intaris_ui_url="",
        public_base_url="http://localhost:8080",
        database_url=database_url,
        jwt_private_key_path=private_key_path,
        jwt_public_key_path=public_key_path,
        secrets_key_path=secrets_key_path,
        log_level="info",
        log_format="json",
        serve_ui=False,
        cors_origins=["http://localhost:5173"],
        browser_session_ttl_seconds=3600,
        session_cookie_domain="",
        session_cookie_samesite="lax",
        lsp_enabled=False,
        lsp_auto_install=False,
        lsp_diagnostics_timeout_ms=10000,
        lsp_idle_timeout_seconds=600,
        lsp_max_concurrent_servers=8,
        artifact_backend="filesystem",
        artifact_path=tmp_path / "artifacts",
        artifact_s3_endpoint="",
        artifact_s3_access_key="",
        artifact_s3_secret_key="",
        artifact_s3_bucket="",
        artifact_s3_region="",
        artifact_max_size_bytes=50 * 1024 * 1024,
        artifact_signed_url_ttl_seconds=3600,
        artifact_signing_secret="test",
        require_external_crypto=False,
        vapid_private_key="",
        vapid_public_key="",
        vapid_private_key_path=tmp_path / "vapid-private.pem",
        vapid_subject="mailto:test@example.com",
        redis_url="",
        tool_output_backend="filesystem",
        tool_output_s3_endpoint="",
        tool_output_s3_access_key="",
        tool_output_s3_secret_key="",
        tool_output_s3_bucket="",
        tool_output_s3_region="",
        tool_output_ttl_hours=24,
        tool_output_max_size_mb=10,
        initial_admin_email=None,
        initial_admin_password=None,
    )


@pytest.mark.asyncio
async def test_provider_registry_llm_service_supports_direct_codex_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    session_factory = create_session_factory(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    config = _test_config(tmp_path, f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    auth_provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    registry = build_provider_registry(config, session_factory, auth_provider)

    assert isinstance(registry.llm, LLMService)

    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="chatgpt",
                display_name="ChatGPT Subscription",
                location="controller",
                backend="litellm",
                config={
                    "preset": "chatgpt",
                    "default_model": "gpt-5.3-codex",
                },
                status="active",
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def _fake_auth(self: LLMService, row: LLMProvider) -> codex_support.CodexAuth:
        captured["auth_provider_id"] = row.provider_id
        return codex_support.CodexAuth(access_token="token", account_id="account")

    async def _fake_responses(self: Any, **kwargs: object) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
        }

    monkeypatch.setattr(LLMService, "_chatgpt_codex_auth", _fake_auth)
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.DirectCodexTransport.responses", _fake_responses
    )

    result = await registry.llm.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-5.3-codex",
        provider_id="chatgpt",
        cognis_session_id="session-123",
    )

    assert captured["auth_provider_id"] == "chatgpt"
    assert captured["model"] == "gpt-5.3-codex"
    assert captured["input"] == [{"role": "user", "content": "hi"}]
    assert result["choices"][0]["message"]["content"] == "hello"
    await engine.dispose()
