from __future__ import annotations

from pathlib import Path

from cognis.config import load_config


def test_load_config_defaults(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.delenv("DATABASE_URL", raising=False)  # type: ignore[attr-defined]

    config = load_config()

    assert config.data_dir == tmp_path
    assert config.host == "0.0.0.0"
    assert config.port == 8080
    assert config.database_url == f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}"
    assert config.jwt_private_key_path == tmp_path / "keys" / "private.pem"
    assert config.jwt_public_key_path == tmp_path / "keys" / "public.pem"
    assert config.secrets_key_path == tmp_path / "secrets.key"
    assert config.serve_ui is True
