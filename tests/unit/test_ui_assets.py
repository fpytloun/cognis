from __future__ import annotations

from pathlib import Path

from cognis.ui_assets import SPAMiddleware


async def _unused_app(
    scope, receive, send
) -> None:  # pragma: no cover - not called by these unit tests
    raise AssertionError("app should not be called")


def _middleware(directory: Path) -> SPAMiddleware:
    return SPAMiddleware(_unused_app, directory=directory)


def test_spa_static_service_worker_is_not_http_cached(tmp_path: Path) -> None:
    (tmp_path / "service-worker.js").write_text("self.skipWaiting();", encoding="utf-8")

    response = _middleware(tmp_path)._resolve_file("/service-worker.js")

    assert response is not None
    assert response.headers["cache-control"] == "no-cache"


def test_spa_static_html_shell_is_not_http_cached(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")

    response = _middleware(tmp_path)._resolve_file("/")

    assert response is not None
    assert response.headers["cache-control"] == "no-cache"


def test_spa_static_immutable_assets_use_long_cache(tmp_path: Path) -> None:
    asset = tmp_path / "_app" / "immutable" / "entry" / "app.abc123.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("console.log('ok');", encoding="utf-8")

    response = _middleware(tmp_path)._resolve_file("/_app/immutable/entry/app.abc123.js")

    assert response is not None
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
