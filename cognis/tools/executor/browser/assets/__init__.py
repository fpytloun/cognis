"""Vendored browser-side asset loader.

These assets are JavaScript snippets injected into every browser context as
init scripts. They live next to the runtime so the package can ship without
a network dependency and so contents are deterministic across deployments.

Use :func:`load_asset` to read an asset by filename. Results are cached after
first read; missing assets log a warning and return ``None`` so the manager
falls back to launch-arg-only evasions instead of crashing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from cognis.logging import get_logger

logger = get_logger(__name__)

_ASSET_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=32)
def load_asset(filename: str) -> str | None:
    """Read an asset file from this package; cached after first read."""
    path = _ASSET_DIR / filename
    if not path.is_file():
        logger.warning("browser: missing asset %s; skipping injection", filename)
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "browser: failed to read asset %s (%s); skipping injection",
            filename,
            type(exc).__name__,
        )
        return None


def asset_dir() -> Path:
    """Return the directory containing vendored browser assets."""
    return _ASSET_DIR
