from __future__ import annotations

import html
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from starlette.testclient import TestClient

from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes import deliverables as deliverable_routes
from cognis.rendering import deliverables as rendering
from cognis.ui_assets import (
    StandaloneAssetManifest,
    resolve_standalone_asset,
    resolve_standalone_manifest,
)


def _row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "content": "Semantic fallback",
        "deliverable_id": "dlv_standalone",
        "format": "rich",
        "rich_payload": {
            "metadata": {"subtitle": "A concise description"},
            "blocks": [{"type": "card", "title": "Finding", "content": "Safe body"}],
        },
        "title": "Standalone report",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manifest(directory: Path) -> StandaloneAssetManifest:
    return StandaloneAssetManifest(
        directory=directory,
        script="assets/standalone-8ALe6GL-.js",
        styles=("assets/standalone-DjsgwGF9.css",),
    )


def test_standalone_shell_uses_escaped_inert_payload_external_assets_and_semantic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(rendering, "resolve_standalone_manifest", lambda: _manifest(tmp_path))
    row = _row(
        title='Report </template><script id="payload-xss">alert(1)</script>',
        content="Fallback </template><script>alert(2)</script>",
    )

    document = rendering.render_standalone_shell(
        row,
        media_base="/api/v1/deliverables/dlv_standalone/media",
        standalone_url="/api/v1/deliverables/dlv_standalone/view",
        pdf_url="/api/v1/deliverables/dlv_standalone/download.pdf",
    )
    soup = BeautifulSoup(document, "html.parser")
    template = soup.select_one("template#cognis-deliverable-payload")

    assert template is not None
    assert len(soup.select("template#cognis-deliverable-payload")) == 1
    assert soup.select("#payload-xss") == []
    assert "<\\/template" not in document
    assert "&lt;/template&gt;" in document
    parsed = json.loads(html.unescape(template.decode_contents()))
    assert parsed["title"] == row.title
    assert parsed["content"] == row.content
    assert template["data-media-base"].endswith("/media")
    assert soup.select_one('script[type="module"]')["src"].endswith(
        "/assets/standalone-8ALe6GL-.js"
    )
    assert soup.select_one("script").string is None
    assert soup.select_one('link[rel="stylesheet"]')["href"].endswith(
        "/assets/standalone-DjsgwGF9.css"
    )
    assert soup.select_one('meta[property="og:title"]')["content"] == row.title
    assert soup.select_one('meta[property="og:description"]')["content"] == (
        "A concise description"
    )
    assert "Finding" in soup.select_one("noscript").get_text(" ", strip=True)
    assert "Safe body" in soup.select_one("noscript").get_text(" ", strip=True)


def test_standalone_manifest_resolution_and_asset_confinement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    build_dir = tmp_path / "standalone-build"
    manifest_dir = build_dir / ".vite"
    assets_dir = build_dir / "assets"
    manifest_dir.mkdir(parents=True)
    assets_dir.mkdir()
    (assets_dir / "standalone-8ALe6GL-.js").write_text("export {};", encoding="utf-8")
    (assets_dir / "standalone-DjsgwGF9.css").write_text(":root{}", encoding="utf-8")
    (assets_dir / "chunk-Bg_95ya4.js").write_text("export {};", encoding="utf-8")
    (assets_dir / "stable.js").write_text("export {};", encoding="utf-8")
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "src/standalone.ts": {
                    "file": "assets/standalone-8ALe6GL-.js",
                    "isEntry": True,
                    "css": ["assets/standalone-DjsgwGF9.css"],
                },
                "_chunk-Bg_95ya4.js": {"file": "assets/chunk-Bg_95ya4.js"},
                "_stable.js": {"file": "assets/stable.js"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("cognis.ui_assets.resolve_standalone_build_dir", lambda: build_dir)

    manifest = resolve_standalone_manifest()

    assert manifest is not None
    assert manifest.script == "assets/standalone-8ALe6GL-.js"
    assert resolve_standalone_asset(manifest.script) == assets_dir / manifest.script.removeprefix(
        "assets/"
    )
    assert resolve_standalone_asset("../manifest.json") is None
    assert resolve_standalone_asset(".vite/manifest.json") is None
    assert resolve_standalone_asset("assets/unhashed.js") is None
    assert resolve_standalone_asset("assets/stable.js") is None
    assert resolve_standalone_asset("assets/chunk-Bg_95ya4.js") == (
        assets_dir / "chunk-Bg_95ya4.js"
    )


def test_standalone_asset_is_pre_auth_and_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset = tmp_path / "standalone-12345678.js"
    asset.write_text("export const ready = true;", encoding="utf-8")
    monkeypatch.setattr(deliverable_routes, "resolve_standalone_asset", lambda _path: asset)
    app = FastAPI()
    app.include_router(deliverable_routes.router)
    app.add_middleware(AuthenticationMiddleware)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/deliverables/standalone-assets/assets/standalone-12345678.js"
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_standalone_shell_headers_use_exact_csp_and_token_no_store() -> None:
    headers = deliverable_routes._html_headers(
        _row(),
        128,
        cache_control="no-store",
        standalone=True,
    )

    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Content-Security-Policy"] == deliverable_routes.STANDALONE_CSP
    assert headers["Content-Security-Policy"] == (
        "sandbox allow-scripts allow-same-origin allow-downloads; default-src 'none'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; font-src 'self' data:; media-src 'self' data:"
    )


def test_standalone_falls_back_when_ui_disabled_or_manifest_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(serve_ui=False)))
    assert deliverable_routes._standalone_assets_available(request) is False

    request.app.state.serve_ui = True
    monkeypatch.setattr(deliverable_routes, "resolve_standalone_manifest", lambda: None)
    assert deliverable_routes._standalone_assets_available(request) is False


def test_standalone_shell_wraps_markdown_deliverables_with_toc_when_substantial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every format now renders through the same SvelteKit-hydrated shell.

    Markdown/plain/html used to fall back to the older, more limited
    `render_standalone_html` renderer entirely -- they now get the exact
    same unified RichDeliverable shell (TOC, full-view, hero, etc.) as rich
    payloads by wrapping their content as a single block, mirroring
    `AssistantDeliverableBlock.svelte`'s client-side wrap.
    """

    monkeypatch.setattr(rendering, "resolve_standalone_manifest", lambda: _manifest(tmp_path))
    row = _row(
        format="markdown",
        rich_payload=None,
        content="# Report\n\n## One\n\nA.\n\n## Two\n\nB.\n\n## Three\n\nC.",
    )

    response = deliverable_routes._try_render_standalone_response(
        row,
        media_base="/api/v1/deliverables/dlv_standalone/media",
        standalone_url="/api/v1/deliverables/dlv_standalone/view",
        pdf_url="/api/v1/deliverables/dlv_standalone/download.pdf",
    )

    assert response is not None
    soup = BeautifulSoup(response.decode("utf-8"), "html.parser")
    template = soup.select_one("template#cognis-deliverable-payload")
    assert template is not None
    parsed = json.loads(html.unescape(template.decode_contents()))
    assert parsed["payload"]["blocks"] == [{"type": "markdown", "content": row.content}]
    assert parsed["payload"]["metadata"]["toc"] == {"enabled": True, "depth": 4}


def test_standalone_shell_wraps_short_markdown_without_toc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(rendering, "resolve_standalone_manifest", lambda: _manifest(tmp_path))
    row = _row(format="markdown", rich_payload=None, content="# Just one heading\n\nShort body.")

    response = deliverable_routes._try_render_standalone_response(
        row,
        media_base="/api/v1/deliverables/dlv_standalone/media",
        standalone_url="/api/v1/deliverables/dlv_standalone/view",
        pdf_url="/api/v1/deliverables/dlv_standalone/download.pdf",
    )

    assert response is not None
    template = BeautifulSoup(response.decode("utf-8"), "html.parser").select_one(
        "template#cognis-deliverable-payload"
    )
    assert template is not None
    parsed = json.loads(html.unescape(template.decode_contents()))
    assert parsed["payload"]["metadata"]["toc"] == {"enabled": False, "depth": 4}


def test_standalone_shell_wraps_plain_as_code_block_and_html_as_raw_html_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(rendering, "resolve_standalone_manifest", lambda: _manifest(tmp_path))

    plain_row = _row(format="plain", rich_payload=None, content="literal [not a link](x)")
    plain_response = deliverable_routes._try_render_standalone_response(
        plain_row,
        media_base="/media",
        standalone_url="/view",
        pdf_url="/download.pdf",
    )
    assert plain_response is not None
    plain_template = BeautifulSoup(plain_response.decode("utf-8"), "html.parser").select_one(
        "template#cognis-deliverable-payload"
    )
    assert plain_template is not None
    plain_parsed = json.loads(html.unescape(plain_template.decode_contents()))
    assert plain_parsed["payload"]["blocks"] == [{"type": "code", "content": plain_row.content}]
    assert plain_parsed["payload"]["metadata"] == {}

    html_row = _row(format="html", rich_payload=None, content="<p>Already HTML</p>")
    html_response = deliverable_routes._try_render_standalone_response(
        html_row,
        media_base="/media",
        standalone_url="/view",
        pdf_url="/download.pdf",
    )
    assert html_response is not None
    html_template = BeautifulSoup(html_response.decode("utf-8"), "html.parser").select_one(
        "template#cognis-deliverable-payload"
    )
    assert html_template is not None
    html_parsed = json.loads(html.unescape(html_template.decode_contents()))
    assert html_parsed["payload"]["blocks"] == [{"type": "raw_html", "content": html_row.content}]


def test_standalone_shell_uses_rich_payload_as_is_for_rich_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_render_standalone_shell(row: object, **kwargs: object) -> str:
        captured["rich_payload_override"] = kwargs.get("rich_payload_override")
        return "<html></html>"

    monkeypatch.setattr(
        deliverable_routes, "render_standalone_shell", _fake_render_standalone_shell
    )

    deliverable_routes._try_render_standalone_response(
        _row(format="rich"),
        media_base="/media",
        standalone_url="/view",
        pdf_url="/download.pdf",
    )

    assert captured["rich_payload_override"] is None
