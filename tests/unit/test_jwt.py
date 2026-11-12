from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
import pytest
from jose import JWTError

from cognis.bootstrap import ensure_data_dir, ensure_jwt_keypair, ensure_secrets_key
from cognis.config import load_config
from cognis.providers.auth.jwt import JWTAuthProvider


def test_jwt_sign_verify_and_jwks(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    ensure_secrets_key(config)

    provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    token = provider.sign_access_token("user@example.com", "User", "admin")
    claims = provider.verify_jwt(token, audience=["cognis"])
    jwks = provider.jwks()

    assert claims["sub"] == "user@example.com"
    assert claims["role"] == "admin"
    assert jwks["keys"][0]["alg"] == "ES256"
    assert jwks["keys"][0]["kid"]


@pytest.mark.parametrize("receiver_offset", [-0.02, -0.999, 0.0])
def test_service_jwt_clock_boundary_preserves_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, receiver_offset: float
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    config = load_config()
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with patch("cognis.providers.auth.jwt.datetime") as issuer:
        issuer.now.return_value = now
        token = provider.sign_service_jwt("user@example.com", "agent-1", ["intaris"])
        access = provider.sign_access_token("user@example.com", "User", "user")
    with patch("jwt.api_jwt.datetime") as receiver:
        receiver.now.return_value = now + timedelta(seconds=receiver_offset)
        claims = pyjwt.decode(
            token,
            config.jwt_public_key_path.read_text(),
            algorithms=["ES256"],
            audience="intaris",
            issuer="cognis",
        )
    assert claims["iat"] == int(now.timestamp()) - 1
    assert claims["exp"] == int(now.timestamp()) + provider.token_ttl_seconds
    assert claims["typ"] == "service"
    assert claims["agent_id"] == "agent-1"
    assert pyjwt.decode(access, options={"verify_signature": False})["iat"] == int(now.timestamp())
    with patch("jwt.api_jwt.datetime") as receiver:
        receiver.now.return_value = now - timedelta(seconds=1.01)
        with pytest.raises(pyjwt.ImmatureSignatureError):
            pyjwt.decode(
                token,
                config.jwt_public_key_path.read_text(),
                algorithms=["ES256"],
                audience="intaris",
            )
    with patch("jwt.api_jwt.datetime") as receiver:
        receiver.now.return_value = now + timedelta(seconds=provider.token_ttl_seconds + 1)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            pyjwt.decode(
                token,
                config.jwt_public_key_path.read_text(),
                algorithms=["ES256"],
                audience="intaris",
            )
        receiver.now.return_value = now
        with pytest.raises(pyjwt.InvalidAudienceError):
            pyjwt.decode(
                token,
                config.jwt_public_key_path.read_text(),
                algorithms=["ES256"],
                audience="mnemory",
            )
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        other_key = (
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        with pytest.raises(pyjwt.InvalidSignatureError):
            pyjwt.decode(token, other_key, algorithms=["ES256"], audience="intaris")


def test_executor_tokens_do_not_expire_by_default(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    ensure_secrets_key(config)

    provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    token = provider.sign_executor_token("exec-1", token_version=3)
    claims = provider.verify_executor_token(token)

    assert claims["sub"] == "exec-1"
    assert claims["typ"] == "executor"
    assert claims["etv"] == 3
    assert "exp" not in claims


def test_executor_verifier_accepts_legacy_expired_executor_tokens(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    ensure_data_dir(config)
    ensure_jwt_keypair(config)
    ensure_secrets_key(config)

    provider = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    token = provider.sign_executor_token("exec-1", ttl_seconds=-1)

    claims = provider.verify_executor_token(token)
    assert claims["sub"] == "exec-1"
    with pytest.raises(JWTError):
        provider.verify_jwt(token, audience=["cognis-executor"])
