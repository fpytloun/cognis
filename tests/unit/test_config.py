from __future__ import annotations

from pathlib import Path

import pytest

from cognis.config import load_config


def test_load_config_defaults(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.delenv("DATABASE_URL", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("COGNIS_HOST", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("COGNIS_PORT", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("COGNIS_PUBLIC_BASE_URL", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("COGNIS_TRUSTED_PROXY_CIDRS", raising=False)  # type: ignore[attr-defined]

    config = load_config()

    assert config.data_dir == tmp_path
    assert config.host == "0.0.0.0"
    assert config.port == 8080
    assert config.public_base_url == ""
    assert config.database_url == f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}"
    assert config.jwt_private_key_path == tmp_path / "keys" / "private.pem"
    assert config.jwt_public_key_path == tmp_path / "keys" / "public.pem"
    assert config.secrets_key_path == tmp_path / "secrets.key"
    assert config.serve_ui is True
    assert config.mcp_oauth_refresh_timeout_seconds == 30.0
    assert config.trusted_proxy_cidrs == ()


def test_load_config_public_base_url(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_PUBLIC_BASE_URL", "https://cognis.example.com/")  # type: ignore[attr-defined]

    config = load_config()

    assert config.public_base_url == "https://cognis.example.com"


def test_load_config_validates_mcp_oauth_refresh_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_MCP_OAUTH_REFRESH_TIMEOUT_SECONDS", "45")
    assert load_config().mcp_oauth_refresh_timeout_seconds == 45.0

    monkeypatch.setenv("COGNIS_MCP_OAUTH_REFRESH_TIMEOUT_SECONDS", "121")
    with pytest.raises(ValueError, match="must be between 5 and 120"):
        load_config()


def test_load_config_validates_trusted_proxy_cidrs(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_TRUSTED_PROXY_CIDRS", "10.0.0.0/8, 2001:db8::/32")  # type: ignore[attr-defined]

    config = load_config()

    assert config.trusted_proxy_cidrs == ("10.0.0.0/8", "2001:db8::/32")

    monkeypatch.setenv("COGNIS_TRUSTED_PROXY_CIDRS", "not-a-network")  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        load_config()
