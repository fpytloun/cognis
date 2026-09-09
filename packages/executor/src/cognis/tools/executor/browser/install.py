"""Browser runtime detection and optional bootstrap for Playwright/Patchright.

Design notes on auto-install
------------------------------
Playwright/Patchright downloads browser binaries into a user-writable cache
directory (``PLAYWRIGHT_BROWSERS_PATH``).  For the bundled *chromium* engine
and a handful of Chromium-based headless shells this requires no elevated
privileges and is safe to run as a non-root daemon process.

When the user pins a *system-browser channel* (``chrome``, ``msedge``,
``chrome-beta``, ``chrome-dev``, ``chrome-canary``) Playwright's install
command tries to install the real browser via ``sudo apt-get`` (on Debian/
Ubuntu systems).  That requires an interactive terminal and root access, both
of which are unavailable inside a daemon process.  We therefore skip
auto-install for those channels entirely and instead probe whether the binary
is already present on the ``PATH`` / at the expected location.

If the system browser is not found we return a clear actionable error rather
than hanging on a sudo password prompt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

# Supported runtime identifiers.
RUNTIME_PLAYWRIGHT = "playwright"
RUNTIME_PATCHRIGHT = "patchright"

# Engines accepted by Playwright/Patchright.
SUPPORTED_RUNTIMES: tuple[str, ...] = (RUNTIME_PLAYWRIGHT, RUNTIME_PATCHRIGHT)
SUPPORTED_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")

# Channels that can be auto-installed without root because they are bundled
# Chromium-based builds that download into a writable user-space cache.
_INSTALLABLE_CHANNELS: frozenset[str] = frozenset(
    {
        "chromium-headless-shell",
        "chromium-tip-of-tree",
    }
)

# Channels that require a system-level package manager (apt/rpm) and therefore
# cannot be auto-installed without sudo.
_SYSTEM_BROWSER_CHANNELS: frozenset[str] = frozenset(
    {
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    }
)

# Candidate binaries to probe for each system-browser channel when the user
# has it configured but auto-install is skipped.
_SYSTEM_BROWSER_CANDIDATES: dict[str, list[str]] = {
    "chrome": ["google-chrome-stable", "google-chrome", "chrome", "chromium-browser"],
    "chrome-beta": ["google-chrome-beta", "google-chrome", "chrome"],
    "chrome-dev": ["google-chrome-unstable", "google-chrome", "chrome"],
    "chrome-canary": ["google-chrome-canary", "google-chrome", "chrome"],
    "msedge": ["microsoft-edge-stable", "microsoft-edge", "msedge"],
    "msedge-beta": ["microsoft-edge-beta", "microsoft-edge"],
    "msedge-dev": ["microsoft-edge-dev", "microsoft-edge"],
    "msedge-canary": ["microsoft-edge-canary", "microsoft-edge"],
}

# Stderr patterns that indicate the install tried and failed to acquire sudo.
_SUDO_FAIL_FRAGMENTS: tuple[str, ...] = (
    "sudo: a terminal is required",
    "sudo: a password is required",
    "sudo: no tty present",
    "sudo: unable to open session",
)


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


def _install_target(*, engine: str, channel: str | None) -> str | None:
    """Return the install target for *non-system* channels, or None to skip.

    * No channel → install the engine (e.g. ``chromium``).
    * Installable channel (e.g. ``chromium-headless-shell``) → install by channel name.
    * System-browser channel (``chrome``, ``msedge``, …) → return ``None``; the
      binary must already be present on the system.
    """
    lowered = (channel or "").strip().lower()
    if not lowered:
        return engine
    if lowered in _SYSTEM_BROWSER_CHANNELS:
        return None  # skip auto-install; require pre-installed system browser
    if lowered in _INSTALLABLE_CHANNELS:
        return lowered
    # Unknown / future channel — try installing by channel name and let the
    # subprocess fail cleanly if it's not supported.
    return lowered


def _probe_system_browser(channel: str) -> bool:
    """Return True when a system browser binary is found for *channel*."""
    lowered = channel.strip().lower()
    candidates = _SYSTEM_BROWSER_CANDIDATES.get(lowered, [])
    return any(shutil.which(candidate) is not None for candidate in candidates)


def _system_browser_unavailable_reason(channel: str) -> str:
    lowered = channel.strip().lower()
    candidates = _SYSTEM_BROWSER_CANDIDATES.get(lowered, [lowered])
    example = candidates[0] if candidates else lowered
    return (
        f"Channel '{channel}' requires a system browser installed outside of Cognis. "
        f"Auto-install is skipped for this channel (it would require sudo). "
        f"Install it via your OS package manager (e.g. 'apt install {example}') "
        f"or change the channel setting to '' to use the bundled Chromium instead."
    )


def _is_sudo_failure(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return any(fragment in lowered for fragment in _SUDO_FAIL_FRAGMENTS)


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

    Install behaviour
    -----------------
    * **No channel / installable channel**: ``python -m <runtime> install <target>``
      downloads a pre-built binary into ``PLAYWRIGHT_BROWSERS_PATH``.  No sudo.
    * **System-browser channel** (``chrome``, ``msedge``, …): auto-install is
      skipped; we probe for the binary on ``PATH`` instead.  A clear error is
      returned when it is absent.
    """
    runtime_norm = _normalize_runtime(runtime)
    engine_norm = _normalize_engine(engine)
    channel_norm = (channel or "").strip().lower() or None
    cache_key = _cache_key(runtime_norm, engine_norm, channel_norm)

    try:
        _import_module_for_runtime(runtime_norm)
    except Exception as exc:
        return (
            False,
            f"{runtime_norm.capitalize()} Python package unavailable: {type(exc).__name__}",
        )

    install_target = _install_target(engine=engine_norm, channel=channel_norm)

    # --- System-browser channel: probe, never auto-install ---
    if install_target is None:
        assert channel_norm is not None
        if _probe_system_browser(channel_norm):
            return True, "available"
        return False, _system_browser_unavailable_reason(channel_norm)

    # --- Bundled/downloadable binary path ---
    if not auto_install or cache_key in _PREPARED:
        return True, "available"

    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(get_browser_cache_dir()))
    # Prevent Playwright's own host-requirements validation step from calling
    # sudo dpkg-query or similar tools and hanging on a password prompt.
    env.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
    # Ensure any accidental sudo invocation fails fast rather than hanging.
    env.setdefault("SUDO_ASKPASS", "/bin/false")
    env.setdefault("DEBIAN_FRONTEND", "noninteractive")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        runtime_norm,
        "install",
        install_target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr_bytes = await proc.communicate()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        if _is_sudo_failure(stderr_text):
            return (
                False,
                (
                    f"Auto-install of '{install_target}' requires root/sudo privileges "
                    "which are not available in headless/daemon mode. "
                    "Either install the browser binary manually "
                    "(run 'python -m {runtime_norm} install {install_target}' as root or with sudo), "
                    "or disable auto-install in executor settings."
                ),
            )
        message = stderr_text[:400] or f"{runtime_norm} install failed"
        return False, message

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
