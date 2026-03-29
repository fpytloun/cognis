from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cognis.main import app

runner = CliRunner()


def test_cli_create_user_succeeds(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "password123")

    result = runner.invoke(app, ["admin", "create-user", "user@example.com", "--name", "User"])

    assert result.exit_code == 0
    assert "Created user user@example.com" in result.output


def test_cli_create_user_duplicate_fails(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "password123")

    first = runner.invoke(app, ["admin", "create-user", "user@example.com"])
    second = runner.invoke(app, ["admin", "create-user", "user@example.com"])

    assert first.exit_code == 0
    assert second.exit_code != 0


def test_cli_reset_password_updates_existing_user(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "password123")
    runner.invoke(app, ["admin", "create-user", "user@example.com"])
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "new-password")

    result = runner.invoke(app, ["admin", "reset-password", "user@example.com"])

    assert result.exit_code == 0
    assert "Updated password for user@example.com" in result.output


def test_cli_reset_password_unknown_user_exits_nonzero(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "new-password")

    result = runner.invoke(app, ["admin", "reset-password", "missing@example.com"])

    assert result.exit_code != 0


def test_cli_api_key_create_and_list(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "password123")
    runner.invoke(app, ["admin", "create-user", "user@example.com"])

    created = runner.invoke(
        app, ["admin", "api-key", "create", "user@example.com", "--name", "cli-key"]
    )
    listed = runner.invoke(app, ["admin", "api-key", "list", "user@example.com"])

    assert created.exit_code == 0
    assert created.output.startswith("cognis_ck")
    assert listed.exit_code == 0
    assert "cli-key" in listed.output


def test_cli_config_init_prints_template() -> None:
    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    assert "COGNIS_DATA_DIR" in result.output


def test_cli_status_prints_health_response(monkeypatch: object) -> None:
    monkeypatch.setattr(
        "cognis.main.httpx.get",
        lambda *args, **kwargs: SimpleNamespace(text='{"status":"healthy"}'),
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert '"status":"healthy"' in result.output
