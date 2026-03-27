from __future__ import annotations

from pathlib import Path

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
