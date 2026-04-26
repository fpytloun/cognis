"""Browser runtime detection and optional bootstrap for Playwright/Patchright."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Supported runtime identifiers.
RUNTIME_PLAYWRIGHT = "playwright"
RUNTIME_PATCHRIGHT = "patchright"

# Engines accepted by Playwright/Patchright. ``patchright`` only ships the
# Chromium engine in practice; the others are accepted but logged as a
# best-effort fallthrough to vanilla Playwright behaviour.
SUPPORTED_RUNTIMES: tuple[str, ...] = (RUNTIME_PLAYWRIGHT, RUNTIME_PATCHRIGHT)
SUPPORTED_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")


def get_browser_cache_dir() -> Path:
    data_dir = os.environ.get("COGNIS_DATA_DIR", os.path.expanduser("~/.cognis"))
    path = Path(data_dir) / "cache" / "browser"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_runtime(runtime: str | None) -> str:
    if not runtime:
        return RUNTIME_PLAYWRIGHT
    value = runtime.strip().lower()
    if value not in SUPPORTED_RUNTIMES:
        raise ValueError(
            f"Unsupported browser runtime: {runtime!r}. "
            f"Expected one of {', '.join(SUPPORTED_RUNTIMES)}."
        )
    return value


def _normalize_engine(engine: str | None) -> str:
    if not engine:
        return "chromium"
    value = engine.strip().lower()
    if value not in SUPPORTED_ENGINES:
        raise ValueError(
            f"Unsupported browser engine: {engine!r}. "
            f"Expected one of {', '.join(SUPPORTED_ENGINES)}."
        )
    return value


def _install_target(*, engine: str, channel: str | None) -> str:
    """Map an (engine, channel) pair to the install target argument.

    ``python -m playwright install <target>`` accepts engine names
    (``chromium``, ``firefox``, ``webkit``) or channel names (``chrome``,
    ``msedge``, ``chrome-beta``, ``chrome-canary``, ``chrome-dev``,
    ``chromium-headless-shell``).
    """
    if channel:
        return channel.strip().lower()
    return engine


def _import_module_for_runtime(runtime: str) -> None:
    """Import the appropriate ``async_api`` module to verify availability."""
    if runtime == RUNTIME_PATCHRIGHT:
        __import__("patchright.async_api")
    else:
        __import__("playwright.async_api")


# Cache of successfully-prepared (runtime, engine, channel) tuples so that we
# only run ``-m <pkg> install`` once per executor lifetime per combination.
_PREPARED: set[tuple[str, str, str]] = set()


def _cache_key(runtime: str, engine: str, channel: str | None) -> tuple[str, str, str]:
    return (runtime, engine, (channel or "").strip().lower())


async def ensure_browser_runtime(
    *,
    runtime: str = RUNTIME_PLAYWRIGHT,
    engine: str = "chromium",
    channel: str | None = None,
    auto_install: bool = False,
) -> tuple[bool, str]:
    """Ensure the requested browser runtime + browser binary is available.

    Returns ``(ok, reason)``. ``ok=False`` means the runtime cannot be used
    (e.g., the Python package is missing or the install command failed).
    """
    runtime_norm = _normalize_runtime(runtime)
    engine_norm = _normalize_engine(engine)
    cache_key = _cache_key(runtime_norm, engine_norm, channel)

    try:
        _import_module_for_runtime(runtime_norm)
    except Exception as exc:
        return (
            False,
            f"{runtime_norm.capitalize()} Python package unavailable: {type(exc).__name__}",
        )

    if not auto_install:
        return True, "available"

    if cache_key in _PREPARED:
        return True, "available"

    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(get_browser_cache_dir()))
    target = _install_target(engine=engine_norm, channel=channel)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        runtime_norm,
        "install",
        target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()[:300]
        return False, message or f"{runtime_norm} install failed"
    _PREPARED.add(cache_key)
    return True, "installed"


async def ensure_playwright_browser(
    *, auto_install: bool, engine: str = "chromium"
) -> tuple[bool, str]:
    """Backwards-compatible wrapper around :func:`ensure_browser_runtime`.

    Retained so older call sites and tests that monkeypatch this symbol keep
    working. New code should prefer :func:`ensure_browser_runtime`.
    """
    return await ensure_browser_runtime(
        runtime=RUNTIME_PLAYWRIGHT,
        engine=engine,
        channel=None,
        auto_install=auto_install,
    )
