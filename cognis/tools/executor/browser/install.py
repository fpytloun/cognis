"""Playwright browser runtime detection and optional bootstrap."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def get_browser_cache_dir() -> Path:
    data_dir = os.environ.get("COGNIS_DATA_DIR", os.path.expanduser("~/.cognis"))
    path = Path(data_dir) / "cache" / "browser"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def ensure_playwright_browser(
    *, auto_install: bool, engine: str = "chromium"
) -> tuple[bool, str]:
    try:
        __import__("playwright.async_api")
    except Exception as exc:
        return False, f"Playwright Python package unavailable: {type(exc).__name__}"

    if not auto_install:
        return True, "available"

    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(get_browser_cache_dir()))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        engine,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()[:300]
        return False, message or "playwright install failed"
    return True, "installed"
