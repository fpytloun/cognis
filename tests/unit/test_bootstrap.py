from __future__ import annotations

from pathlib import Path

import pytest

from cognis.bootstrap import DEFAULT_SETTINGS, bootstrap_runtime
from cognis.config import load_config
from cognis.security import create_password_hasher
from cognis.store.queries import list_settings


@pytest.mark.asyncio
async def test_bootstrap_creates_keys_db_and_settings(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()

    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)

    assert config.jwt_private_key_path.exists()
    assert config.jwt_public_key_path.exists()
    assert config.secrets_key_path.exists()
    assert (tmp_path / "cognis.db").exists()

    async with session_factory() as session:
        settings = await list_settings(session)
    assert len(settings) == len(DEFAULT_SETTINGS)

    await engine.dispose()
