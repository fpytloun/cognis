"""Focused tests for the platform-neutral executor service log command."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cognis.executor import __main__ as executor_main
from cognis.executor import service


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    ("arguments", "lines", "follow"),
    [
        (["logs"], 100, False),
        (["logs", "--lines", "25", "--follow"], 25, True),
        (["logs", "-n", "25", "-f"], 25, True),
    ],
)
def test_logs_parser_long_and_short_options(
    arguments: list[str],
    lines: int,
    follow: bool,
) -> None:
    args = executor_main._build_parser().parse_args(arguments)
    assert args.command == "logs"
    assert args.lines == lines
    assert args.follow is follow


def test_logs_help_documents_short_and_long_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        executor_main._build_parser().parse_args(["logs", "--help"])
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "-n" in output
    assert "--lines" in output
    assert "-f" in output
    assert "--follow" in output


@pytest.mark.parametrize("value", ["0", "10001", "not-a-number"])
def test_logs_parser_rejects_unsafe_line_counts(value: str) -> None:
    with pytest.raises(SystemExit):
        executor_main._build_parser().parse_args(["logs", "--lines", value])


def test_macos_logs_show_stderr_first_and_limit_each_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "var" / "log"
    log_dir.mkdir(parents=True)
    stderr_path = log_dir / service.STDERR_LOG_NAME
    stderr_path.write_text("error-1\nerror-2\nerror-3\n", encoding="utf-8")

    def fake_run(command: tuple[str, ...]) -> SimpleNamespace:
        if command[-1] == "--prefix":
            return _completed(stdout=f"{tmp_path}\n")
        return _completed(stdout="cognis-executor 0.13.0\n")

    monkeypatch.setattr(service, "_run", fake_run)
    result = service.MacOSServiceManager(brew="/opt/homebrew/bin/brew").logs(
        lines=2,
        follow=False,
    )
    assert result.returncode == 0
    output = capsys.readouterr().out
    assert output.index("stderr:") < output.index("stdout:")
    assert "error-1" not in output
    assert "error-2\nerror-3\n" in output
    assert f"stdout: {log_dir / service.STDOUT_LOG_NAME}" in output
    assert output.rstrip().endswith("[missing]")


def test_macos_logs_handle_empty_and_missing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "var" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / service.STDERR_LOG_NAME).touch()
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            _completed(stdout=f"{tmp_path}\n")
            if command[-1] == "--prefix"
            else _completed(stdout="cognis-executor 0.13.0\n")
        ),
    )
    result = service.MacOSServiceManager(brew="/usr/local/bin/brew").logs(
        lines=100,
        follow=False,
    )
    assert result.returncode == 0
    output = capsys.readouterr().out
    assert "[empty]" in output
    assert "[missing]" in output


@pytest.mark.parametrize("prefix", ["/opt/homebrew", "/usr/local"])
def test_macos_logs_derive_both_homebrew_prefixes(
    prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            _completed(stdout=f"{prefix}\n")
            if command[-1] == "--prefix"
            else _completed(stdout="cognis-executor 0.13.0\n")
        ),
    )
    monkeypatch.setattr(
        service,
        "_tail_file",
        lambda path, *, lines: observed.append(path) or [],
    )
    result = service.MacOSServiceManager(brew=f"{prefix}/bin/brew").logs(
        lines=10,
        follow=False,
    )
    assert result.returncode == 0
    assert observed == [
        Path(prefix) / "var/log" / service.STDERR_LOG_NAME,
        Path(prefix) / "var/log" / service.STDOUT_LOG_NAME,
    ]


def test_macos_follow_uses_both_logs_and_returns_process_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            _completed(stdout="/opt/homebrew\n")
            if command[-1] == "--prefix"
            else _completed(stdout="cognis-executor 0.13.0\n")
        ),
    )
    monkeypatch.setattr(service.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        service,
        "_run_stream",
        lambda command: commands.append(list(command)) or 7,
    )
    result = service.MacOSServiceManager(brew="/opt/homebrew/bin/brew").logs(
        lines=42,
        follow=True,
    )
    assert result.returncode == 7
    assert commands == [
        [
            "/usr/bin/tail",
            "-n",
            "42",
            "-F",
            "/opt/homebrew/var/log/cognis-executor.error.log",
            "/opt/homebrew/var/log/cognis-executor.log",
        ]
    ]


def test_macos_follow_exits_cleanly_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            _completed(stdout="/opt/homebrew\n")
            if command[-1] == "--prefix"
            else _completed(stdout="cognis-executor 0.13.0\n")
        ),
    )
    monkeypatch.setattr(service.shutil, "which", lambda _command: "/usr/bin/tail")
    monkeypatch.setattr(
        service,
        "_run_stream",
        lambda _command: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    result = service.MacOSServiceManager(brew="/opt/homebrew/bin/brew").logs(
        lines=100,
        follow=True,
    )
    assert result.returncode == 130


@pytest.mark.parametrize("follow", [False, True])
def test_linux_logs_preserve_journalctl_arguments_and_exit_status(
    follow: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(service, "_run", lambda _command: _completed())
    monkeypatch.setattr(
        service.shutil,
        "which",
        lambda command: "/usr/bin/journalctl" if command == "journalctl" else None,
    )
    monkeypatch.setattr(
        service,
        "_run_stream",
        lambda command: commands.append(list(command)) or 6,
    )
    result = service.LinuxSystemdServiceManager(systemctl="/usr/bin/systemctl").logs(
        lines=15, follow=follow
    )
    expected = [
        "/usr/bin/journalctl",
        "--user",
        "-u",
        "cognis-executor.service",
        "-n",
        "15",
    ]
    if follow:
        expected.append("-f")
    assert commands == [expected]
    assert result.returncode == 6


def test_not_installed_and_unsupported_logs_return_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            _completed(stdout="/usr/local\n")
            if command[-1] == "--prefix"
            else _completed(returncode=1)
        ),
    )
    mac = service.MacOSServiceManager(brew="/usr/local/bin/brew").logs(
        lines=100,
        follow=False,
    )
    assert mac.returncode != 0
    assert "/usr/local/var/log/cognis-executor.error.log" in mac.message

    unsupported = service.UnsupportedServiceManager().logs(lines=100, follow=False)
    assert unsupported.returncode != 0
    assert "foreground" in unsupported.message


def test_logs_cli_does_not_read_enrollment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeManager:
        def logs(self, *, lines: int, follow: bool) -> service.LogResult:
            assert (lines, follow) == (3, False)
            return service.LogResult(0)

    monkeypatch.setattr(
        service,
        "get_service_manager",
        lambda: FakeManager(),
    )
    monkeypatch.setattr(
        executor_main,
        "load_config",
        lambda: pytest.fail("logs must not read executor enrollment"),
    )
    args = executor_main._build_parser().parse_args(["logs", "-n", "3"])
    assert executor_main._logs(args) == 0


def test_logs_cli_preserves_manager_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeManager:
        def logs(self, *, lines: int, follow: bool) -> service.LogResult:
            return service.LogResult(9, "journal failed")

    monkeypatch.setattr(service, "get_service_manager", lambda: FakeManager())
    args = executor_main._build_parser().parse_args(["logs"])
    assert executor_main._logs(args) == 9
    assert capsys.readouterr().err == "ERROR: journal failed\n"
