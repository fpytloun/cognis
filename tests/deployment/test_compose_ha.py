from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from cryptography.fernet import Fernet

from cognis.providers.secrets.encrypted_db import EncryptedDBSecretsProvider

ROOT = Path(__file__).resolve().parents[2]


def _load_prepare_module() -> ModuleType:
    path = ROOT / "scripts" / "prepare_ha_e2e.py"
    spec = importlib.util.spec_from_file_location("prepare_ha_e2e_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ha_overlay_has_two_controllers_one_lb_and_no_redis() -> None:
    source = (ROOT / "compose.ha-e2e.yml").read_text()
    assert ".local/cognis-ha-e2e/compose.env" not in source
    overlay = yaml.safe_load(source.replace("!reset", ""))
    services = overlay["services"]
    assert {
        "cognis",
        "cognis-2",
        "cognis-lb",
        "cognis-executor",
        "cognis-executor-2",
        "postgres",
        "minio",
    } <= services.keys()
    assert "redis" not in services
    for service in ("qdrant", "mock-llm", "mnemory", "intaris", "cognis"):
        assert services[service]["ports"] == []
    assert len(services["cognis-lb"]["ports"]) == 1
    assert services["cognis-executor"]["environment"]["COGNIS_CONTROLLER_URL"].startswith(
        "ws://cognis:"
    )
    assert services["cognis-executor-2"]["environment"]["COGNIS_CONTROLLER_URL"].startswith(
        "ws://cognis-2:"
    )


def test_opt_in_redis_overlay_is_shared_and_health_checked() -> None:
    overlay = yaml.safe_load((ROOT / "compose.redis-ha-e2e.yml").read_text())
    services = overlay["services"]
    redis = services["redis"]
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]
    assert redis.get("ports") is None
    assert "--maxmemory" in redis["command"]
    assert "--maxmemory-policy" in redis["command"]
    for controller in ("cognis", "cognis-2"):
        assert services[controller]["environment"]["COGNIS_REDIS_URL"] == ("redis://redis:6379/0")
        assert services[controller]["depends_on"]["redis"]["condition"] == "service_healthy"


def test_merged_ha_config_publishes_only_dedicated_lb_port(tmp_path: Path) -> None:
    env_file = tmp_path / "compose.env"
    env_file.write_text(
        "\n".join(
            (
                "COGNIS_HA_POSTGRES_PASSWORD=deployment-test-password",
                "COGNIS_HA_MINIO_ACCESS_KEY=deployment-test-access",
                "COGNIS_HA_MINIO_SECRET_KEY=deployment-test-secret",
                "COGNIS_HA_ARTIFACT_SIGNING_SECRET=deployment-test-signing",
            )
        )
        + "\n"
    )
    environment = {**os.environ, "COGNIS_HA_E2E_PORT": "18081"}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "cognis-ha-e2e-test",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "compose.local.yml"),
            "-f",
            str(ROOT / "compose.e2e.yml"),
            "-f",
            str(ROOT / "compose.ha-e2e.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)
    publications = [
        (service_name, port)
        for service_name, service in config["services"].items()
        for port in service.get("ports", [])
    ]
    assert len(publications) == 1
    assert publications[0][0] == "cognis-lb"
    assert publications[0][1]["published"] == "18081"
    assert publications[0][1]["target"] == 8080
    assert not any(port["published"] in {"8080", "8090"} for _, port in publications)


def test_controllers_wait_for_migration_and_use_distinct_identity() -> None:
    overlay = yaml.safe_load((ROOT / "compose.ha-e2e.yml").read_text().replace("!reset", ""))
    services = overlay["services"]
    for name in ("cognis", "cognis-2"):
        assert services[name]["depends_on"]["cognis-db-upgrade"]["condition"] == (
            "service_completed_successfully"
        )
        assert services[name]["environment"]["COGNIS_SCHEMA_MODE"] == "validate"
        assert services[name]["environment"]["COGNIS_RUNTIME_MODE"] == "ha"
    assert services["cognis"]["environment"]["COGNIS_CONTROLLER_ID"] == "controller-1"
    assert services["cognis-2"]["environment"]["COGNIS_CONTROLLER_ID"] == "controller-2"
    assert services["mnemory"]["environment"]["MNEMORY_JWKS_URL"].startswith("http://cognis-lb:")
    assert services["intaris"]["environment"]["INTARIS_JWKS_URL"].startswith("http://cognis-lb:")
    assert services["cognis-db-upgrade"]["command"] == [
        "cognis-controller",
        "db",
        "upgrade",
    ]
    assert services["cognis-db-upgrade"]["environment"]["COGNIS_CONTROLLER_ID"] == "migration"


def test_ha_credentials_are_generated_outside_repository_sources() -> None:
    script = (ROOT / "scripts" / "prepare_ha_e2e.py").read_text()
    assert '".local" / "cognis-ha-e2e"' in script
    assert "secrets.token_urlsafe" in script
    assert "ec.generate_private_key" in script
    assert "if ENV_FILE.exists() and not force" in script
    assert "os.umask(0o077)" in script
    assert "os.O_EXCL, 0o600" in script
    assert "os.replace(temporary, path)" in script
    assert 'CURRENT_LINK / "executors"' in script


def test_atomic_secret_write_is_restrictive(tmp_path: Path) -> None:
    module = _load_prepare_module()
    target = tmp_path / "credential"
    module._atomic_write(target, b"first")
    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    module._atomic_write(target, b"replacement")
    assert target.read_bytes() == b"replacement"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_generated_secrets_key_is_fernet_compatible(tmp_path: Path) -> None:
    module = _load_prepare_module()
    module.ROOT = tmp_path
    module.OUTPUT_DIR = tmp_path / "ha"
    module.BUNDLE_DIR = module.OUTPUT_DIR / "bundles"
    module.CURRENT_LINK = module.OUTPUT_DIR / "current"
    module.KEY_DIR = module.CURRENT_LINK / "keys"
    module.ENV_FILE = module.CURRENT_LINK / "compose.env"
    module.prepare(force=False)

    key = (module.KEY_DIR / "secrets.key").read_bytes()
    Fernet(key)
    assert len(module.base64.urlsafe_b64decode(key)) == 32
    provider = EncryptedDBSecretsProvider(
        None,  # type: ignore[arg-type]
        str(module.KEY_DIR / "secrets.key"),
    )
    assert len(provider.key) == 32


def test_failed_bundle_rotation_keeps_previous_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_prepare_module()
    module.ROOT = tmp_path
    module.OUTPUT_DIR = tmp_path / "ha"
    module.BUNDLE_DIR = module.OUTPUT_DIR / "bundles"
    module.CURRENT_LINK = module.OUTPUT_DIR / "current"
    module.KEY_DIR = module.CURRENT_LINK / "keys"
    module.ENV_FILE = module.CURRENT_LINK / "compose.env"
    module.prepare(force=False)

    original_target = module.CURRENT_LINK.readlink()
    original_env = module.ENV_FILE.read_bytes()
    original_write = module._atomic_write
    calls = 0

    def fail_during_staging(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected staging failure")
        original_write(path, content)

    monkeypatch.setattr(module, "_atomic_write", fail_during_staging)
    with pytest.raises(RuntimeError, match="injected staging failure"):
        module.prepare(force=True)

    assert module.CURRENT_LINK.readlink() == original_target
    assert module.ENV_FILE.read_bytes() == original_env


def test_replace_then_raise_keeps_activated_bundle_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_prepare_module()
    module.ROOT = tmp_path
    module.OUTPUT_DIR = tmp_path / "ha"
    module.BUNDLE_DIR = module.OUTPUT_DIR / "bundles"
    module.CURRENT_LINK = module.OUTPUT_DIR / "current"
    module.KEY_DIR = module.CURRENT_LINK / "keys"
    module.ENV_FILE = module.CURRENT_LINK / "compose.env"
    module.prepare(force=False)
    original_env = module.ENV_FILE.read_bytes()
    original_replace = module.os.replace

    def replace_then_raise(source: Path, destination: Path) -> None:
        original_replace(source, destination)
        if Path(destination) == module.CURRENT_LINK:
            raise RuntimeError("injected interruption after activation")

    monkeypatch.setattr(module.os, "replace", replace_then_raise)
    with pytest.raises(RuntimeError, match="injected interruption after activation"):
        module.prepare(force=True)

    assert module.CURRENT_LINK.is_symlink()
    assert module.ENV_FILE.is_file()
    assert module.ENV_FILE.read_bytes()
    assert module.ENV_FILE.read_bytes() != original_env


def test_live_qualification_requires_sustained_controller_two_api_success() -> None:
    script = (ROOT / "scripts" / "qualify_ha_e2e.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()
    assert "PROJECT_NAME=${COGNIS_HA_E2E_PROJECT:-cognis-ha-e2e}" in script
    assert "PUBLIC_PORT=${COGNIS_HA_E2E_PORT:-18080}" in script
    assert '--project-name "$PROJECT_NAME"' in script
    assert "HA_E2E_PROJECT ?= cognis-ha-e2e" in makefile
    assert "ha-e2e-down:\n\t$(COMPOSE_HA_E2E) down" in makefile
    assert "curl --fail --silent http://localhost:8080" not in script
    assert "compose stop cognis" in script
    assert "compose exec -T cognis-2" in script
    assert "verify_ha_assembled.py" in script
    assert "compose run --rm seed-e2e" in script
    assert 'while [ "$iteration" -le 20 ]' in script
    assert "/api/readyz" in script
    assert "/api/livez" in script
    assert "/.well-known/jwks.json" in script


def test_test_proxy_supports_isolated_deterministic_controller_routes() -> None:
    nginx = (ROOT / "docker" / "ha-e2e" / "nginx.conf").read_text()
    assert "$http_x_cognis_ha_controller" in nginx
    assert "controller-1 cognis:8080" in nginx
    assert "controller-2 cognis-2:8080" in nginx
    assert "X-Cognis-HA-Upstream" in nginx
    helm_sources = "\n".join(
        path.read_text()
        for path in (ROOT / "deploy" / "helm" / "cognis").rglob("*")
        if path.is_file()
    )
    assert "X-Cognis-HA-Controller" not in helm_sources
