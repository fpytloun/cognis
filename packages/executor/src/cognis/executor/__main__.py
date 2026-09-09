"""Entry point for the standalone executor and its management commands."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import os
import signal
import sys
from pathlib import Path

from cognis.executor.config import (
    ConfigError,
    ExecutorSettings,
    get_config_paths,
    load_config,
    normalized_settings,
    resolve_settings,
    save_config,
)


def _setup_logging(level_name: str) -> None:
    """Configure logging for the executor process."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _run_browser_install(args: argparse.Namespace) -> int:
    """Pre-install browser runtimes/binaries on the local host."""
    log_level = args.log_level or os.environ.get("COGNIS_LOG_LEVEL", "info")
    _setup_logging(log_level)

    from cognis.tools.executor.browser.install import (
        SUPPORTED_RUNTIMES,
        ensure_browser_runtime,
    )

    if args.all_defaults:
        targets: list[tuple[str, str, str | None]] = [
            ("playwright", "chromium", None),
            ("playwright", "chromium", "chrome"),
            ("patchright", "chromium", "chrome"),
        ]
    elif args.runtime == "all":
        targets = [(rt, args.engine, args.channel or None) for rt in SUPPORTED_RUNTIMES]
    else:
        targets = [(args.runtime, args.engine, args.channel or None)]

    async def _install_all() -> int:
        failures = 0
        for rt, eng, ch in targets:
            target_label = f"{rt}/{eng}" + (f"@{ch}" if ch else "")
            sys.stdout.write(f"Installing {target_label} ...\n")
            sys.stdout.flush()
            ok, reason = await ensure_browser_runtime(
                runtime=rt,
                engine=eng,
                channel=ch,
                auto_install=True,
            )
            if ok:
                sys.stdout.write(f"  -> {reason}\n")
            else:
                sys.stderr.write(f"  -> FAILED: {reason}\n")
                failures += 1
        return failures

    try:
        return asyncio.run(_install_all())
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


def _add_connection_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--controller-url", default=default, help="Controller WebSocket URL")
    parser.add_argument("--token", default=default, help="JWT authentication token")
    parser.add_argument(
        "--workspace",
        "--workdir",
        dest="workspace",
        default=default,
        help="Executor workspace",
    )
    parser.add_argument(
        "--log-level", default=default, help="Log level: debug, info, warning, or error"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cognis executor runner")
    _add_connection_options(parser)
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "browser-install",
        help="Pre-install browser runtimes/binaries on this host (fleet pre-warm).",
    )
    install_parser.add_argument("--runtime", default="all", help="playwright, patchright, or all")
    install_parser.add_argument("--engine", default="chromium", help="chromium, firefox, or webkit")
    install_parser.add_argument("--channel", default="", help="Browser channel")
    install_parser.add_argument(
        "--all-defaults", action="store_true", help="Install the default matrix"
    )
    install_parser.add_argument("--log-level", default="", help="Log level")

    configure_parser = subparsers.add_parser("configure", help="Save executor connection settings")
    _add_connection_options(configure_parser, suppress_defaults=True)
    configure_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; use flags, environment, and existing configuration only",
    )
    connection_group = configure_parser.add_mutually_exclusive_group()
    connection_group.add_argument(
        "--test-connection", dest="test_connection", action="store_true", default=None
    )
    connection_group.add_argument(
        "--no-test-connection", dest="test_connection", action="store_false", default=None
    )
    configure_parser.set_defaults(test_connection=None)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Validate executor configuration and dependencies"
    )
    _add_connection_options(doctor_parser, suppress_defaults=True)
    doctor_parser.add_argument(
        "--test-connection", action="store_true", help="Perform an authenticated handshake"
    )

    config_parser = subparsers.add_parser("config", help="Inspect executor configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("show", help="Show effective settings with the token redacted")
    for command in ("start", "stop", "restart", "status"):
        lifecycle_parser = subparsers.add_parser(
            command, help=f"{command.capitalize()} the installed executor service"
        )
        _add_connection_options(lifecycle_parser, suppress_defaults=True)
    logs_parser = subparsers.add_parser("logs", help="Read installed executor service logs")
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=_log_line_count,
        default=100,
        metavar="N",
        help="Show the last N lines from each log (default: 100; maximum: 10000)",
    )
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow service logs until interrupted",
    )
    return parser


def _log_line_count(value: str) -> int:
    try:
        lines = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("line count must be an integer") from exc
    if not 1 <= lines <= 10_000:
        raise argparse.ArgumentTypeError("line count must be between 1 and 10000")
    return lines


def _read_file_settings() -> ExecutorSettings:
    try:
        return load_config()
    except ConfigError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        raise SystemExit(1) from exc


def _settings_from_args(
    args: argparse.Namespace, file_settings: ExecutorSettings
) -> ExecutorSettings:
    return resolve_settings(
        {
            "controller_url": args.controller_url,
            "token": args.token,
            "workspace": args.workspace,
            "log_level": args.log_level,
        },
        file_settings=file_settings,
    )


def _configure(args: argparse.Namespace) -> int:
    file_settings = _read_file_settings()
    settings = _settings_from_args(args, file_settings)
    if not args.non_interactive:
        if not settings.controller_url:
            settings = ExecutorSettings(
                controller_url=input("Controller WebSocket URL: ").strip(),
                token=settings.token,
                workspace=settings.workspace,
                log_level=settings.log_level,
            )
        if not settings.token:
            token = getpass.getpass("Controller token: ").strip()
            settings = ExecutorSettings(
                settings.controller_url,
                token,
                settings.workspace,
                settings.log_level,
            )
        if (
            args.workspace is None
            and not os.environ.get("COGNIS_EXECUTOR_WORKSPACE")
            and not os.environ.get("COGNIS_EXECUTOR_WORKDIR")
        ):
            workspace = input(f"Workspace [{settings.workspace}]: ").strip() or settings.workspace
            settings = ExecutorSettings(
                settings.controller_url,
                settings.token,
                workspace,
                settings.log_level,
            )
        if args.test_connection is None:
            args.test_connection = input(
                "Test controller connection now? [y/N]: "
            ).strip().lower() in {"y", "yes"}
    if not settings.controller_url:
        sys.stderr.write(
            "ERROR: Non-interactive configure requires --controller-url or COGNIS_CONTROLLER_URL.\n"
        )
        return 1
    if not settings.token:
        sys.stderr.write(
            "ERROR: Non-interactive configure requires --token or COGNIS_EXECUTOR_TOKEN.\n"
        )
        return 1
    try:
        settings = normalized_settings(settings)
        path = save_config(settings)
    except (ConfigError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    sys.stdout.write(f"Saved executor configuration to {path}\n")
    if args.test_connection:
        from cognis.executor.diagnostics import check_controller_connection

        result = asyncio.run(check_controller_connection(settings))
        sys.stdout.write(f"{result.name}: {result.status} ({result.category}) - {result.message}\n")
        return 0 if result.status == "pass" else 1
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from cognis.executor.diagnostics import run_doctor

    settings = _settings_from_args(args, _read_file_settings())
    results = asyncio.run(run_doctor(settings, test_connection=args.test_connection))
    for result in results:
        sys.stdout.write(f"{result.name}: {result.status} ({result.category}) - {result.message}\n")
    return 1 if any(result.status == "fail" for result in results) else 0


def _show_config(args: argparse.Namespace) -> int:
    import json

    settings = _settings_from_args(args, _read_file_settings())
    paths = get_config_paths()
    output = {
        "config_file": str(paths.config_file),
        "data_dir": str(paths.data_dir),
        "log_dir": str(paths.log_dir),
        "settings": settings.redacted(),
    }
    sys.stdout.write(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return 0


def _service_is_configured() -> tuple[ExecutorSettings | None, int]:
    try:
        settings = normalized_settings(load_config())
    except (ConfigError, OSError) as exc:
        sys.stderr.write(f"Configuration invalid: {exc}\n")
        return None, 1
    if not settings.controller_url or not settings.token:
        sys.stderr.write(
            "Configuration invalid: executor is not enrolled; run `cognis-executor configure` first.\n"
        )
        return None, 1
    return settings, 0


def _lifecycle(args: argparse.Namespace) -> int:
    from cognis.executor.service import get_service_manager

    if args.command in {"start", "restart"}:
        _, code = _service_is_configured()
        if code:
            return code
    elif args.command == "status":
        try:
            settings = _settings_from_args(args, load_config())
            normalized_settings(settings)
        except (ConfigError, OSError) as exc:
            sys.stdout.write(f"configuration: invalid — {exc}\n")
        else:
            if settings.controller_url and settings.token:
                sys.stdout.write("configuration: valid\n")
            else:
                sys.stdout.write("configuration: invalid — executor is not enrolled\n")
    manager = get_service_manager()
    result = getattr(manager, args.command)()
    sys.stdout.write(f"service: {result.state.value} — {result.message}\n")
    if result.state in {"unsupported", "not installed"}:
        sys.stdout.write(f"Foreground command: {result.foreground_command}\n")
    return 0 if result.ok else 1


def _logs(args: argparse.Namespace) -> int:
    from cognis.executor.service import get_service_manager

    result = get_service_manager().logs(lines=args.lines, follow=args.follow)
    if result.message:
        sys.stderr.write(f"ERROR: {result.message}\n")
    return result.returncode


def _run(args: argparse.Namespace) -> int:
    file_settings = _read_file_settings()
    settings = _settings_from_args(args, file_settings)
    stdin_token = ""
    if not sys.stdin.isatty():
        with contextlib.suppress(OSError):
            stdin_token = sys.stdin.read().strip()
    if not settings.token:
        settings = ExecutorSettings(
            settings.controller_url,
            stdin_token,
            settings.workspace,
            settings.log_level,
        )
    if not settings.controller_url:
        sys.stderr.write("ERROR: Provide --controller-url or set COGNIS_CONTROLLER_URL.\n")
        return 1
    if not settings.token:
        sys.stderr.write("ERROR: Provide --token or set COGNIS_EXECUTOR_TOKEN.\n")
        return 1
    try:
        settings = normalized_settings(settings)
        os.chdir(settings.workspace)
    except (ConfigError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig

    runner = ExecutorRunner(
        ExecutorConfig(
            executor_id="remote",
            controller_url=settings.controller_url,
            controller_token=settings.token,
        )
    )
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(_run_foreground(runner))
    return 0


async def _run_foreground(runner: object) -> None:
    """Run the foreground runner and turn SIGTERM into orderly runner cleanup."""
    loop = asyncio.get_running_loop()
    runner_task = asyncio.create_task(runner.run())  # type: ignore[attr-defined]

    def request_shutdown() -> None:
        stop = getattr(runner, "stop", None)
        if callable(stop):
            stop()
        runner_task.cancel()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
        installed = True
    except (NotImplementedError, RuntimeError):
        # Signal handlers are unavailable on a few embedded/event-loop hosts.
        pass
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    """Run the executor or one of its management commands."""
    args = _build_parser().parse_args()
    if args.command == "browser-install":
        rc = _run_browser_install(args)
        if rc:
            raise SystemExit(rc)
        return
    if args.command == "configure":
        rc = _configure(args)
        if rc:
            raise SystemExit(rc)
        return
    if args.command == "doctor":
        rc = _doctor(args)
        if rc:
            raise SystemExit(rc)
        return
    if args.command == "config" and args.config_command == "show":
        rc = _show_config(args)
        if rc:
            raise SystemExit(rc)
        return
    if args.command in {"start", "stop", "restart", "status"}:
        rc = _lifecycle(args)
        if rc:
            raise SystemExit(rc)
        return
    if args.command == "logs":
        rc = _logs(args)
        if rc:
            raise SystemExit(rc)
        return
    _setup_logging(args.log_level or os.environ.get("COGNIS_LOG_LEVEL", "info"))
    rc = _run(args)
    if rc:
        raise SystemExit(rc)


def _is_localhost(url: str) -> bool:
    """Return whether a URL uses a localhost WebSocket endpoint."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url)
        return parsed.scheme == "ws" and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
        }
    except ValueError:
        return False


def _allow_insecure_ws() -> bool:
    """Return the explicit local-network insecure WebSocket opt-in."""
    return os.environ.get("COGNIS_EXECUTOR_ALLOW_INSECURE_WS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_workdir(cli_workdir: str | None) -> str:
    """Resolve the legacy workdir option for callers that import this helper."""
    raw = cli_workdir or os.environ.get("COGNIS_EXECUTOR_WORKDIR") or str(Path.home())
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve(strict=False)
    if not path.is_dir():
        raise ValueError(f"Executor working directory does not exist or is not a directory: {raw}")
    return str(path)


if __name__ == "__main__":
    main()
