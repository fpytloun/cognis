"""Read-only executor diagnostics and bounded controller handshake checks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cognis.executor.config import ConfigError, ExecutorSettings, validate_controller_url


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """One diagnostic check result."""

    name: str
    status: str
    category: str
    message: str


def _safe_message(message: str, secret: str = "") -> str:
    if secret:
        message = message.replace(secret, "[redacted]")
    return " ".join(message.split())[:300]


def classify_connection_error(exc: BaseException) -> str:
    """Classify a connection failure without exposing its payload."""
    status_code = getattr(exc, "status_code", None)
    received = getattr(exc, "rcvd", None)
    received_code = getattr(received, "code", None)
    if status_code in {401, 403, 4401, 4403} or received_code in {401, 403, 4401, 4403}:
        return "authentication"
    if isinstance(exc, (socket.gaierror,)):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError)):
        return "connectivity"
    text = str(exc).lower()
    if any(term in text for term in ("401", "403", "unauthor", "forbidden", "authentication")):
        return "authentication"
    if any(term in text for term in ("ssl", "certificate", "tls", "handshake")):
        return "tls"
    if any(term in text for term in ("resolve", "name or service", "nodename")):
        return "dns"
    if isinstance(exc, OSError) or any(
        term in text for term in ("connect", "timed out", "websocket", "network")
    ):
        return "connectivity"
    return "connectivity"


async def check_controller_connection(
    settings: ExecutorSettings,
    *,
    timeout_seconds: float = 10.0,
    connector: Callable[..., Any] | None = None,
) -> DiagnosticResult:
    """Perform the authentication/registration half of the executor handshake."""
    try:
        validate_controller_url(
            settings.controller_url,
            allow_insecure=_allow_insecure_ws(),
        )
        if not settings.token:
            raise ConfigError("Authentication token is missing")
    except ConfigError as exc:
        return DiagnosticResult("controller", "fail", "configuration", str(exc))

    if connector is None:
        import websockets

        connector = websockets.connect
    request_id = uuid.uuid4().hex
    try:
        async with connector(
            settings.controller_url,
            open_timeout=timeout_seconds,
            close_timeout=timeout_seconds,
            compression="deflate",
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "executor.probe",
                        "params": {
                            "token": settings.token,
                        },
                        "id": request_id,
                    }
                )
            )
            response = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
            payload = json.loads(response)
            if not isinstance(payload, dict):
                raise TypeError("Controller returned an invalid handshake response")
            if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
                raise TypeError("Controller returned an unrelated probe response")
            error = payload.get("error")
            if error is not None and not isinstance(error, dict):
                raise TypeError("Controller returned a malformed probe error")
            if isinstance(error, dict):
                code = error.get("code")
                if code in {-32000, 401, 403, 4401, 4403}:
                    return DiagnosticResult(
                        "controller",
                        "fail",
                        "authentication",
                        "Controller rejected the executor credentials",
                    )
                if code == -32004:
                    return DiagnosticResult(
                        "controller",
                        "fail",
                        "configuration",
                        "Executor is not configured on the controller",
                    )
                if code == -32005:
                    return DiagnosticResult(
                        "controller",
                        "fail",
                        "configuration",
                        "Executor is inactive on the controller",
                    )
                if code == -32006:
                    return DiagnosticResult(
                        "controller",
                        "fail",
                        "configuration",
                        "Executor type is disabled by controller policy",
                    )
                return DiagnosticResult(
                    "controller",
                    "fail",
                    "connectivity",
                    "Controller rejected the executor handshake",
                )
            result = payload.get("result")
            if not isinstance(result, dict) or result.get("status") != "authenticated":
                raise TypeError("Controller returned an unexpected probe result")
            return DiagnosticResult("controller", "pass", "connectivity", "Authenticated probe OK")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        category = classify_connection_error(exc)
        return DiagnosticResult(
            "controller",
            "fail",
            category,
            _safe_message(str(exc), settings.token) or "Controller connection failed",
        )


def check_dependencies() -> list[DiagnosticResult]:
    """Check optional local runtimes used by executor tools."""
    results: list[DiagnosticResult] = []
    for command, _purpose in (("uvx", "MCP and package runtime"), ("npx", "Node-based tools")):
        path = shutil.which(command)
        if path:
            results.append(DiagnosticResult(command, "pass", "dependency", f"available at {path}"))
        else:
            results.append(
                DiagnosticResult(
                    command,
                    "info",
                    "dependency",
                    f"{command} is not installed; related optional tools may be unavailable",
                )
            )
    chrome_names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    chrome = next((shutil.which(name) for name in chrome_names if shutil.which(name)), None)
    if sys.platform == "darwin":
        mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        chrome = chrome or (str(mac_chrome) if mac_chrome.is_file() else None)
    results.append(
        DiagnosticResult(
            "system-chrome",
            "pass" if chrome else "info",
            "dependency",
            f"available at {chrome}"
            if chrome
            else (
                "system Chrome was not found; install Google Chrome or provision "
                "a compatible browser runtime"
            ),
        )
    )
    return results


async def run_doctor(
    settings: ExecutorSettings,
    *,
    test_connection: bool = False,
    connector: Callable[..., Any] | None = None,
) -> list[DiagnosticResult]:
    """Run configuration, workspace, dependency, and optional network checks."""
    results: list[DiagnosticResult] = []
    try:
        validate_controller_url(settings.controller_url, allow_insecure=_allow_insecure_ws())
        results.append(DiagnosticResult("controller-url", "pass", "configuration", "URL is valid"))
    except ConfigError as exc:
        results.append(DiagnosticResult("controller-url", "fail", "configuration", str(exc)))
    if settings.token:
        results.append(DiagnosticResult("token", "pass", "configuration", "Token is configured"))
    else:
        results.append(
            DiagnosticResult("token", "fail", "configuration", "Authentication token is missing")
        )
    workspace = Path(settings.workspace).expanduser()
    if workspace.is_dir():
        results.append(
            DiagnosticResult("workspace", "pass", "configuration", f"directory exists: {workspace}")
        )
    else:
        results.append(
            DiagnosticResult("workspace", "fail", "configuration", "workspace is not a directory")
        )
    results.extend(check_dependencies())
    if test_connection:
        results.append(
            await check_controller_connection(
                settings,
                connector=connector,
            )
        )
    return results


def _allow_insecure_ws() -> bool:
    return os.environ.get("COGNIS_EXECUTOR_ALLOW_INSECURE_WS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
