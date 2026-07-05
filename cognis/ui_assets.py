"""Helpers for locating and serving bundled UI assets.

Provides two mechanisms:
- ``SPAStaticFiles``: Starlette StaticFiles subclass with SPA fallback
  (used by ``app.mount``).
- ``SPAMiddleware``: ASGI middleware that serves the SPA *before* the
  FastAPI router runs, avoiding the problem where FastAPI's exception
  handlers intercept 404s before mounted sub-applications can respond.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


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
