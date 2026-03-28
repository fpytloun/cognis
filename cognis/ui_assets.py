"""Helpers for locating and serving bundled UI assets."""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


def resolve_ui_build_dir() -> Path | None:
    """Return the preferred built UI directory if bundled assets exist."""

    package_dir = Path(__file__).resolve().parent / "ui_dist"
    if (package_dir / "index.html").exists():
        return package_dir

    repo_dir = Path(__file__).resolve().parents[1] / "ui" / "build"
    if (repo_dir / "index.html").exists():
        return repo_dir

    return None


class SPAStaticFiles(StaticFiles):
    """Static file handler with SPA fallback to index.html."""

    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            method = scope.get("method")
            if method not in {"GET", "HEAD"}:
                raise
            if "." in Path(path).name:
                raise
            return await super().get_response("index.html", scope)
