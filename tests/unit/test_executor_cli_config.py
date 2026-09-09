"""Focused tests for executor configuration, diagnostics, and lifecycle commands."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognis.executor import __main__ as executor_main
from cognis.executor import config, diagnostics, service


def test_settings_precedence_cli_environment_file_defaults(tmp_path: Path) -> None:
    file_settings = config.ExecutorSettings(
        controller_url="wss://file.example/ws",
        token="file-token",
        workspace=str(tmp_path / "file"),
        log_level="warning",
    )
    resolved = config.resolve_settings(
        {"controller_url": "ws://localhost/ws", "token": "cli-token", "workspace": None},
        environ={
            "COGNIS_CONTROLLER_URL": "wss://env.example/ws",
            "COGNIS_EXECUTOR_TOKEN": "env-token",
            "COGNIS_EXECUTOR_WORKSPACE": str(tmp_path / "env"),
            "COGNIS_LOG_LEVEL": "debug",
        },
        file_settings=file_settings,
    )
    assert resolved == config.ExecutorSettings(
        controller_url="ws://localhost/ws",
        token="cli-token",
        workspace=str(tmp_path / "env"),
        log_level="debug",
    )


def test_config_paths_use_native_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "user_config_dir", lambda name: f"/config/{name}")
    monkeypatch.setattr(config, "user_data_dir", lambda name: f"/data/{name}")
    monkeypatch.setattr(config, "user_log_dir", lambda name: f"/logs/{name}")
    paths = config.get_config_paths()
    assert paths.config_file == Path("/config/cognis-executor/config.json")
    assert paths.data_dir == Path("/data/cognis-executor")
    assert paths.log_dir == Path("/logs/cognis-executor")


def test_malformed_config_is_rejected_without_echoing_secret(tmp_path: Path) -> None:
    secret = "secret-value"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 1, "unexpected": secret}), encoding="utf-8")
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_config(path)
    assert secret not in str(exc_info.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_config_write_is_atomic_private_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config" / "config.json"
    settings = config.ExecutorSettings(
        controller_url="wss://controller.example/ws",
        token="super-secret-token",
        workspace=str(tmp_path),
    )
    replaced: list[tuple[Path, Path]] = []
    original_replace = os.replace
    monkeypatch.setattr(
        config.os,
        "replace",
        lambda source, destination: (
            replaced.append((Path(source), Path(destination))),
            original_replace(source, destination),
        )[1],
    )
    config.save_config(settings, path)
    assert replaced and replaced[0][1] == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert settings.token not in json.dumps(settings.redacted())
    assert config.load_config(path) == settings


def test_noninteractive_configure_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    saved: list[config.ExecutorSettings] = []
    monkeypatch.setattr(executor_main, "_read_file_settings", config.ExecutorSettings)
    monkeypatch.setattr(
        executor_main,
        "save_config",
        lambda settings: saved.append(settings) or tmp_path / "config.json",
    )
    args = executor_main._build_parser().parse_args(
        [
            "configure",
            "--non-interactive",
            "--controller-url",
            "ws://localhost:8080/ws",
            "--token",
            "configured-token",
            "--workspace",
            str(tmp_path),
        ]
    )
    assert executor_main._configure(args) == 0
    assert saved[0].token == "configured-token"
    assert "configured-token" not in capsys.readouterr().out


def test_interactive_configure_prompts_for_connection_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[config.ExecutorSettings] = []
    answers = iter(["ws://localhost:8080/ws", str(tmp_path), "n"])
    monkeypatch.setattr(executor_main, "_read_file_settings", config.ExecutorSettings)
    monkeypatch.delenv("COGNIS_CONTROLLER_URL", raising=False)
    monkeypatch.delenv("COGNIS_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKSPACE", raising=False)
    monkeypatch.delenv("COGNIS_EXECUTOR_WORKDIR", raising=False)
    monkeypatch.setattr(
        executor_main, "save_config", lambda settings: saved.append(settings) or tmp_path
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(executor_main.getpass, "getpass", lambda _prompt: "interactive-token")
    args = executor_main._build_parser().parse_args(["configure"])
    assert executor_main._configure(args) == 0
    assert saved[0] == config.ExecutorSettings(
        "ws://localhost:8080/ws", "interactive-token", str(tmp_path), "info"
    )


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (socket.gaierror("name resolution failed"), "dns"),
        (ssl.SSLError("certificate verify failed"), "tls"),
        (TimeoutError("timed out"), "connectivity"),
        (PermissionError("HTTP 401 unauthorized"), "authentication"),
    ],
)
def test_connection_failure_classification(error: BaseException, category: str) -> None:
    assert diagnostics.classify_connection_error(error) == category


def test_authenticated_handshake_uses_fake_connector() -> None:
    sent: list[str] = []

    class FakeSocket:
        request_id = ""

        async def __aenter__(self) -> FakeSocket:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str) -> None:
            sent.append(payload)
            self.request_id = json.loads(payload)["id"]

        async def recv(self) -> str:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "result": {"status": "authenticated"},
                }
            )

    result = asyncio.run(
        diagnostics.check_controller_connection(
            config.ExecutorSettings("ws://localhost:8080/ws", "handshake-secret", str(Path.cwd())),
            connector=lambda *_args, **_kwargs: FakeSocket(),
        )
    )
    assert result.status == "pass"
    assert json.loads(sent[0])["method"] == "executor.probe"
    assert json.loads(sent[0])["params"]["token"] == "handshake-secret"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"jsonrpc": "2.0", "id": "wrong", "result": {"status": "authenticated"}},
        {"jsonrpc": "2.0", "error": "failure"},
        {"jsonrpc": "2.0", "result": {"status": "registered"}},
    ],
)
def test_executor_probe_rejects_unproven_success(response: dict[str, object]) -> None:
    class FakeSocket:
        request_id = ""

        async def __aenter__(self) -> FakeSocket:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str) -> None:
            self.request_id = json.loads(payload)["id"]

        async def recv(self) -> str:
            payload = dict(response)
            payload.setdefault("id", self.request_id)
            return json.dumps(payload)

    result = asyncio.run(
        diagnostics.check_controller_connection(
            config.ExecutorSettings(
                "wss://controller.example/api/executor/ws",
                "probe-token",
                str(Path.cwd()),
            ),
            connector=lambda *_args, **_kwargs: FakeSocket(),
        )
    )
    assert result.status == "fail"
    assert result.category == "connectivity"


@pytest.mark.parametrize(
    ("code", "message", "category"),
    [
        (-32000, "Authentication failed", "authentication"),
        (-32000, "Expired token", "authentication"),
        (-32000, "Executor token has been revoked", "authentication"),
        (-32004, "Executor not found", "configuration"),
        (-32005, "Executor is inactive", "configuration"),
        (-32006, "Executor type is disabled by policy", "configuration"),
    ],
)
def test_executor_probe_response_codes_are_classified(
    code: int,
    message: str,
    category: str,
) -> None:
    class FakeSocket:
        request_id = ""

        async def __aenter__(self) -> FakeSocket:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str) -> None:
            self.request_id = json.loads(payload)["id"]

        async def recv(self) -> str:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "error": {"code": code, "message": message},
                }
            )

    result = asyncio.run(
        diagnostics.check_controller_connection(
            config.ExecutorSettings(
                "wss://controller.example/api/executor/ws",
                "probe-token",
                str(Path.cwd()),
            ),
            connector=lambda *_args, **_kwargs: FakeSocket(),
        )
    )
    assert result.status == "fail"
    assert result.category == category


def test_lifecycle_managers_are_mocked_and_do_not_run_real_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...]) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr(service, "_run", fake_run)
    manager = service.LinuxSystemdServiceManager(systemctl="/usr/bin/systemctl")
    assert manager.status().state == service.ServiceState.RUNNING
    assert manager.restart().ok
    assert commands == [
        ("/usr/bin/systemctl", "--user", "cat", "cognis-executor.service"),
        ("/usr/bin/systemctl", "--user", "is-active", "cognis-executor.service"),
        ("/usr/bin/systemctl", "--user", "cat", "cognis-executor.service"),
        ("/usr/bin/systemctl", "--user", "restart", "cognis-executor.service"),
    ]


@pytest.mark.parametrize("command", ["start", "restart"])
def test_service_start_refuses_environment_only_enrollment(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(executor_main, "load_config", lambda: config.ExecutorSettings())
    monkeypatch.setenv("COGNIS_CONTROLLER_URL", "wss://controller.example/ws")
    monkeypatch.setenv("COGNIS_EXECUTOR_TOKEN", "shell-only-token")
    called = False

    def fail_if_selected() -> object:
        nonlocal called
        called = True
        pytest.fail("start selected a service manager before enrollment validation")

    monkeypatch.setattr(service, "get_service_manager", fail_if_selected)
    args = executor_main._build_parser().parse_args([command])
    assert executor_main._lifecycle(args) == 1
    assert not called
    assert "configure" in capsys.readouterr().err


def test_service_status_preserves_manager_failure_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_run",
        lambda _command: SimpleNamespace(
            returncode=0,
            stdout='[{"name":"cognis-executor","status":"error 1"}]',
            stderr="",
        ),
    )
    mac_result = service.MacOSServiceManager(brew="/opt/homebrew/bin/brew").status()
    assert not mac_result.ok
    assert mac_result.state == service.ServiceState.UNKNOWN

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=3, stdout="failed\n", stderr=""),
        ]
    )
    monkeypatch.setattr(service, "_run", lambda _command: next(responses))
    linux_result = service.LinuxSystemdServiceManager(systemctl="/usr/bin/systemctl").status()
    assert not linux_result.ok
    assert linux_result.state == service.ServiceState.UNKNOWN


def test_macos_manager_uses_brew_services_only(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...]) -> SimpleNamespace:
        commands.append(command)
        if command[-2:] == ("list", "--json"):
            return SimpleNamespace(
                returncode=0,
                stdout='[{"name":"cognis-executor","status":"started"}]',
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_run", fake_run)
    manager = service.MacOSServiceManager(brew="/opt/homebrew/bin/brew")
    assert manager.status().state == service.ServiceState.RUNNING
    assert manager.stop().state == service.ServiceState.STOPPED
    assert commands == [
        ("/opt/homebrew/bin/brew", "services", "list", "--json"),
        ("/opt/homebrew/bin/brew", "services", "stop", "cognis-executor"),
    ]


def test_unsupported_lifecycle_includes_foreground_command() -> None:
    result = service.UnsupportedServiceManager().start()
    assert result.state == service.ServiceState.UNSUPPORTED
    assert "cognis-executor" in result.message
