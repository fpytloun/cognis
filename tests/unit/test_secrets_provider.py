from __future__ import annotations

from pathlib import Path

import pytest

from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.providers.secrets.encrypted_db import EncryptedDBSecretsProvider
from cognis.security import create_password_hasher


@pytest.mark.asyncio
async def test_secrets_provider_roundtrip(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    config = load_config()
    password_hasher = create_password_hasher()
    _, engine, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    provider = EncryptedDBSecretsProvider(session_factory, str(config.secrets_key_path))

    await provider.set_secret("API_TOKEN", "secret-value", user_id="user@example.com")
    value = await provider.get_secret("API_TOKEN", user_id="user@example.com")

    assert value == "secret-value"
    await engine.dispose()
