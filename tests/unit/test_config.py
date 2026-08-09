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
    assert config.runtime_mode == "simple"
    assert config.schema_mode == "auto"
    assert config.redis_url == ""
    assert config.event_cache_ttl_seconds == 3600
    assert config.event_cache_sliding_ttl is True
    assert config.event_cache_compression_enabled is True
    assert config.event_cache_compression_threshold_bytes == 65536
    assert config.event_cache_max_value_bytes == 2 * 1024 * 1024
    assert config.shutdown_drain_timeout_seconds == 30.0
    assert config.shutdown_cancel_timeout_seconds == 10.0


def test_load_config_event_cache_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_EVENT_CACHE_TTL_SECONDS", "7200")
    monkeypatch.setenv("COGNIS_EVENT_CACHE_SLIDING_TTL", "false")
    monkeypatch.setenv("COGNIS_EVENT_CACHE_COMPRESSION_ENABLED", "false")
    monkeypatch.setenv("COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES", "131072")
    monkeypatch.setenv("COGNIS_EVENT_CACHE_MAX_VALUE_BYTES", "1048576")

    config = load_config()

    assert config.event_cache_ttl_seconds == 7200
    assert config.event_cache_sliding_ttl is False
    assert config.event_cache_compression_enabled is False
    assert config.event_cache_compression_threshold_bytes == 131072
    assert config.event_cache_max_value_bytes == 1048576


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("COGNIS_EVENT_CACHE_TTL_SECONDS", "0", "must be between"),
        ("COGNIS_EVENT_CACHE_TTL_SECONDS", "86401", "must be between"),
        ("COGNIS_EVENT_CACHE_SLIDING_TTL", "sometimes", "must be a boolean"),
        ("COGNIS_EVENT_CACHE_COMPRESSION_ENABLED", "sometimes", "must be a boolean"),
        ("COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES", "0", "must be between"),
        ("COGNIS_EVENT_CACHE_MAX_VALUE_BYTES", "2097153", "must be between"),
    ],
)
def test_load_config_rejects_invalid_event_cache_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    error: str,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=error):
        load_config()


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


def test_ha_config_requires_shared_authorities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "ha")
    with pytest.raises(ValueError, match="DATABASE_URL must use PostgreSQL"):
        load_config()


def test_ha_config_accepts_postgres_s3_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "ha")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db/cognis")
    monkeypatch.setenv("COGNIS_ARTIFACT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_TOOL_OUTPUT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_REQUIRE_EXTERNAL_CRYPTO", "true")
    monkeypatch.setenv("COGNIS_ARTIFACT_SIGNING_SECRET", "stable-secret")
    monkeypatch.setenv("COGNIS_CONTROLLER_ID", "controller-a")
    monkeypatch.setenv("COGNIS_CONTROLLER_INTERNAL_URL", "http://controller-a.cognis:8080")
    monkeypatch.delenv("COGNIS_REDIS_URL", raising=False)

    config = load_config()

    assert config.runtime_mode == "ha"
    assert config.schema_mode == "validate"
    assert config.controller_id == "controller-a"
    assert config.controller_internal_url == "http://controller-a.cognis:8080"
    assert config.redis_url == ""


def test_simple_config_does_not_require_controller_internal_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COGNIS_CONTROLLER_INTERNAL_URL", raising=False)

    assert load_config().controller_internal_url == ""


@pytest.mark.parametrize(
    "value",
    [
        "controller-a:8080",
        "ftp://controller-a",
        "http://user@controller-a",
        "http://:8080",
        "http://controller-a:abc",
        "http://controller a:8080",
        "http://-controller:8080",
        "http://controller-a/path",
        "http://controller-a?target=other",
    ],
)
def test_controller_internal_url_requires_safe_http_origin(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("COGNIS_CONTROLLER_INTERNAL_URL", value)

    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) origin"):
        load_config()


@pytest.mark.parametrize(
    "value",
    [
        "http://controller-a.cognis:8080",
        "http://127.0.0.1:8080",
        "https://[::1]:8443",
    ],
)
def test_controller_internal_url_accepts_valid_origins(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("COGNIS_CONTROLLER_INTERNAL_URL", value)

    assert load_config().controller_internal_url == value


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://db/cognis",
        "postgresql+psycopg://db/cognis",
    ],
)
def test_ha_config_rejects_synchronous_postgres_driver(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "ha")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("COGNIS_ARTIFACT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_TOOL_OUTPUT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_REQUIRE_EXTERNAL_CRYPTO", "true")
    monkeypatch.setenv("COGNIS_ARTIFACT_SIGNING_SECRET", "stable-secret")
    monkeypatch.setenv("COGNIS_CONTROLLER_ID", "controller-a")

    with pytest.raises(ValueError, match="asyncpg"):
        load_config()


def test_ha_config_rejects_auto_schema_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNIS_RUNTIME_MODE", "ha")
    monkeypatch.setenv("COGNIS_SCHEMA_MODE", "auto")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db/cognis")
    monkeypatch.setenv("COGNIS_ARTIFACT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_TOOL_OUTPUT_BACKEND", "s3")
    monkeypatch.setenv("COGNIS_REQUIRE_EXTERNAL_CRYPTO", "true")
    monkeypatch.setenv("COGNIS_ARTIFACT_SIGNING_SECRET", "stable-secret")
    monkeypatch.setenv("COGNIS_CONTROLLER_ID", "controller-a")

    with pytest.raises(ValueError, match="COGNIS_SCHEMA_MODE must be 'validate'"):
        load_config()
