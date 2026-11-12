"""Single-file publications using the canonical browser content renderer."""

from __future__ import annotations

import html
import re
from typing import Any

from cognis import json_codec as json
from cognis.rendering.deliverables import DeliverableRenderError
from cognis.rendering.rich_visuals import MediaReference
from cognis.ui_assets import resolve_standalone_manifest


def render_offline_html(
    row: Any, *, payload: dict[str, Any], media_resolver: Any, theme_family: str | None = None
) -> str:
    """Embed the offline browser bundle and authorized media without network URLs."""

    manifest = resolve_standalone_manifest(offline=True)
    if manifest is None:
        raise DeliverableRenderError("offline_assets_unavailable")
    script = (manifest.directory / manifest.script).read_text(encoding="utf-8")
    styles = "\n".join(
        (manifest.directory / path).read_text(encoding="utf-8") for path in manifest.styles
    )
    # Trusted build output still must not terminate its HTML raw-text container.
    script = re.sub(r"</script", r"<\\/script", script, flags=re.IGNORECASE)
    styles = re.sub(r"</style", r"<\\/style", styles, flags=re.IGNORECASE)
    media: dict[str, str] = {}
    for key, item in (payload.get("media_manifest") or {}).items():
        reference = MediaReference(
            ref_id=key, artifact_id=None, alt="", credit=None, mime_type=item.get("mime_type")
        )
        resolved = media_resolver(reference, "html")
        if resolved is not None:
            media[key] = resolved.src
    title = str(getattr(row, "title", "") or "Deliverable")
    data = html.escape(
        json.dumps(
            {
                "content": str(getattr(row, "content", "") or ""),
                "instanceId": str(getattr(row, "deliverable_id", "") or ""),
                "title": title,
                "payload": payload,
                "media": media,
                "themeFamily": theme_family,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        quote=False,
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:;">
  <title>{html.escape(title)}</title>
  <style>{styles}</style>
</head>
<body>
  <template id="cognis-deliverable-payload" data-offline="true">{data}</template>
  <div id="cognis-deliverable-root"></div>
  <noscript>This publication requires JavaScript to render its content offline.</noscript>
  <script>{script}</script>
</body>
</html>"""
