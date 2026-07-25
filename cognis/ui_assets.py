"""Helpers for locating and serving bundled UI assets.

Provides two mechanisms:
- ``SPAStaticFiles``: Starlette StaticFiles subclass with SPA fallback
  (used by ``app.mount``).
- ``SPAMiddleware``: ASGI middleware that serves the SPA *before* the
  FastAPI router runs, avoiding the problem where FastAPI's exception
  handlers intercept 404s before mounted sub-applications can respond.
"""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

STANDALONE_ASSET_URL_PREFIX = "/api/v1/deliverables/standalone-assets"
_STANDALONE_ENTRY_KEY = "src/standalone.ts"
_VITE_HASHED_ASSET = re.compile(r".*-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")


@dataclass(frozen=True)
class StandaloneAssetManifest:
    """Resolved standalone client entry and its directly loaded styles."""

    directory: Path
    script: str
    styles: tuple[str, ...]


def resolve_standalone_build_dir() -> Path | None:
    """Return the preferred standalone Vite build directory."""

    repo_dir = Path(__file__).resolve().parents[1] / "ui" / "standalone-build"
    if (repo_dir / ".vite" / "manifest.json").is_file():
        return repo_dir

    package_dir = Path(__file__).resolve().parent / "ui_dist" / "standalone"
    if (package_dir / ".vite" / "manifest.json").is_file():
        return package_dir

    return None


def resolve_standalone_manifest() -> StandaloneAssetManifest | None:
    """Resolve and validate the standalone Vite entry manifest."""

    directory = resolve_standalone_build_dir()
    if directory is None:
        return None
    raw = _load_standalone_manifest(directory)
    if not isinstance(raw, dict):
        return None
    entry = raw.get(_STANDALONE_ENTRY_KEY)
    if not isinstance(entry, dict) or entry.get("isEntry") is not True:
        return None
    script = entry.get("file")
    styles = entry.get("css", [])
    if (
        not isinstance(script, str)
        or not isinstance(styles, list)
        or not all(isinstance(style, str) for style in styles)
    ):
        return None
    paths = (script, *styles)
    if any(_resolve_confined_standalone_asset(directory, path) is None for path in paths):
        return None
    return StandaloneAssetManifest(directory=directory, script=script, styles=tuple(styles))


def standalone_asset_url(relative_path: str) -> str:
    """Return the same-origin URL for one standalone Vite asset."""

    return f"{STANDALONE_ASSET_URL_PREFIX}/{relative_path}"


def resolve_standalone_asset(
    relative_path: str,
    *,
    directory: Path | None = None,
) -> Path | None:
    """Resolve one manifest-listed standalone asset without allowing traversal."""

    build_dir = directory or resolve_standalone_build_dir()
    if build_dir is None:
        return None
    manifest = _load_standalone_manifest(build_dir)
    if manifest is None or relative_path not in _standalone_manifest_assets(manifest):
        return None
    return _resolve_confined_standalone_asset(build_dir, relative_path)


def _load_standalone_manifest(directory: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((directory / ".vite" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _standalone_manifest_assets(manifest: dict[str, Any]) -> set[str]:
    assets: set[str] = set()
    for item in manifest.values():
        if not isinstance(item, dict):
            continue
        file = item.get("file")
        if isinstance(file, str):
            assets.add(file)
        for key in ("css", "assets"):
            values = item.get(key)
            if isinstance(values, list):
                assets.update(value for value in values if isinstance(value, str))
    return assets


def _resolve_confined_standalone_asset(directory: Path, relative_path: str) -> Path | None:
    if not relative_path or "\\" in relative_path:
        return None
    parts = Path(relative_path).parts
    if any(part in {"", ".", ".."} for part in parts) or parts[0] != "assets":
        return None
    if _VITE_HASHED_ASSET.fullmatch(parts[-1]) is None:
        return None
    try:
        resolved = (directory / relative_path).resolve()
        resolved.relative_to(directory.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def resolve_ui_build_dir() -> Path | None:
    """Return the preferred built UI directory if bundled assets exist."""

    repo_dir = Path(__file__).resolve().parents[1] / "ui" / "build"
    if (repo_dir / "index.html").exists():
        return repo_dir

    package_dir = Path(__file__).resolve().parent / "ui_dist"
    if (package_dir / "index.html").exists():
        return package_dir

    return None


# Prefixes that should always be handled by the FastAPI router, never
# by the SPA middleware.
_API_PREFIXES = ("/api/", "/api", "/.well-known/")


def _cache_control_for_path(path: Path, relative_path: str) -> str | None:
    """Return explicit cache policy for bundled UI assets."""

    normalized = relative_path.lstrip("/")
    if normalized == "service-worker.js" or path.suffix == ".html":
        return "no-cache"
    if normalized.startswith("_app/immutable/"):
        return "public, max-age=31536000, immutable"
    return None


def _file_response(path: Path, *, media_type: str, relative_path: str) -> FileResponse:
    headers: dict[str, str] = {}
    cache_control = _cache_control_for_path(path, relative_path)
    if cache_control:
        headers["Cache-Control"] = cache_control
    return FileResponse(path, media_type=media_type, headers=headers)


class SPAMiddleware:
    """ASGI middleware that serves the SPA for non-API GET/HEAD requests.

    For requests whose path does NOT start with ``/api/`` or
    ``/.well-known/``, the middleware tries to serve a matching static
    file from *directory*.  If no file is found and the path has no file
    extension, ``index.html`` is served (SPA client-side routing).

    All other requests (API calls, WebSocket upgrades, POST/PUT/…) pass
    straight through to the inner application.
    """

    def __init__(self, app: ASGIApp, *, directory: Path) -> None:
        self.app = app
        self.directory = directory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "/")

        # Let the FastAPI router handle API and well-known routes.
        if any(path.startswith(prefix) for prefix in _API_PREFIXES):
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET")
        if method not in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return

        # Try to serve a static file.
        response = self._resolve_file(path)
        if response is not None:
            await response(scope, receive, send)
            return

        # No static file found — fall through to the app router so that
        # any remaining non-SPA routes (e.g. root health redirect) still
        # work.  Only serve index.html for paths that look like SPA
        # routes (no file extension).
        if "." not in Path(path).name:
            index = self.directory / "index.html"
            if index.is_file():
                response = _file_response(index, media_type="text/html", relative_path="index.html")
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)

    def _resolve_file(self, path: str) -> Response | None:
        """Try to find a static file for the given URL path."""

        # Normalise: strip leading slash, reject path traversal.
        relative = path.lstrip("/")
        if not relative:
            # Root — serve index.html.
            index = self.directory / "index.html"
            return (
                _file_response(index, media_type="text/html", relative_path="index.html")
                if index.is_file()
                else None
            )

        # Security: reject anything that escapes the build directory.
        try:
            resolved = (self.directory / relative).resolve()
            if not str(resolved).startswith(str(self.directory.resolve())):
                return None
        except (ValueError, OSError):
            return None

        # Exact file match (e.g. /favicon.svg, /_app/immutable/…).
        if resolved.is_file():
            media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            return _file_response(resolved, media_type=media_type, relative_path=relative)

        # SvelteKit adapter-static generates <route>.html files
        # (e.g. setup.html for /setup).
        html_path = self.directory / f"{relative}.html"
        if html_path.is_file():
            return _file_response(
                html_path, media_type="text/html", relative_path=f"{relative}.html"
            )

        # Try <route>/index.html (directory index).
        dir_index = self.directory / relative / "index.html"
        if dir_index.is_file():
            return _file_response(
                dir_index, media_type="text/html", relative_path=f"{relative}/index.html"
            )

        return None
