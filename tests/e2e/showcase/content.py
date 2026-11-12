"""Illustrative, fictional Lumen Autumn document fixtures; no inference is performed."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.config import load_config
from cognis.store.queries import create_artifact_record, create_deliverable


async def seed_content(
    factory,
    provider,
    owner,
    reviewer,
    agent_id,
    project_id,
    conversation_id,
    session_id,
    fixture_dir,
) -> dict:
    """Write two Rich revisions and a separate HTML artifact to canonical storage."""
    config = load_config()
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend=config.artifact_backend,
            path=str(config.artifact_path),
            s3_endpoint=config.artifact_s3_endpoint,
            s3_access_key=config.artifact_s3_access_key,
            s3_secret_key=config.artifact_s3_secret_key,
            s3_bucket=config.artifact_s3_bucket,
            s3_region=config.artifact_s3_region,
            max_size_bytes=config.artifact_max_size_bytes,
        )
    )
    fixture_dir = Path(fixture_dir)
    html = (fixture_dir / "autumn-planner.html").read_bytes()

    # Rich inline media accepts raster images only.
    image = Image.new("RGB", (960, 400), "#f3e8d7")
    draw = ImageDraw.Draw(image)
    draw.ellipse((590, 41, 804, 255), fill="#edbd83")
    draw.ellipse((-11, 158, 387, 556), fill="#c66f4b")
    draw.ellipse((212, 193, 692, 673), fill="#9a4d3d")
    draw.ellipse((573, 212, 1033, 672), fill="#556e5a")
    draw.ellipse((575, 255, 895, 575), fill="#789076")
    draw.rectangle((74, 61, 323, 67), fill="#9a4d3d")
    draw.rectangle((74, 82, 233, 88), fill="#c66f4b")
    output = BytesIO()
    image.save(output, format="PNG")
    figure = output.getvalue()

    figure_id = f"art_{uuid4().hex}"
    html_id = f"art_{uuid4().hex}"
    await store.async_save("artifacts", figure_id, "autumn-figure.png", figure, "image/png")
    await store.async_save("artifacts", html_id, "autumn-planner.html", html, "text/html")

    title = "Lumen Autumn · launch overview"
    rich = {
        "title": title,
        "blocks": [
            {
                "type": "hero",
                "eyebrow": "Lumen / Autumn",
                "title": "Make room for the glow.",
                "subtitle": "A thoughtful launch, from first invitation to autumn light.",
                "badges": ["Launch plan", "Autumn collection"],
            },
            {
                "type": "grid",
                "blocks": [
                    {"type": "metric", "label": "Audience", "value": "12,000"},
                    {"type": "metric", "label": "Email allocation", "value": "60%"},
                    {"type": "metric", "label": "Expected reach", "value": "7,200"},
                ],
            },
            {
                "type": "figure",
                "media": {
                    "ref": figure_id,
                    "alt": "Abstract autumn sun above overlapping terracotta and sage hills",
                },
                "caption": "The autumn palette: warm light, grounded colors, room to breathe.",
            },
            {
                "type": "chart",
                "title": "A measured rollout",
                "description": "Planned audience reach by launch phase.",
                "source": "Lumen launch model",
                "observed_at": datetime.now(UTC).isoformat(),
                "spec_version": "cognis.chart.v1",
                "chart_type": "bar",
                "x_axis": {"type": "category", "label": "Phase"},
                "y_axis": {"type": "linear", "label": "People", "min": 0},
                "series": [
                    {
                        "id": "reach",
                        "label": "Planned reach",
                        "points": [
                            {"x": "Early access", "y": 2400},
                            {"x": "Launch", "y": 4800},
                            {"x": "Follow-up", "y": 7200},
                        ],
                    }
                ],
            },
            {
                "type": "markdown",
                "content": (
                    "## The plan\n\nBegin with a warm invitation to returning customers, "
                    "then open the wider launch. Keep the message simple and the next step clear."
                ),
            },
        ],
    }
    async with factory() as db:
        await create_artifact_record(
            db,
            artifact_id=figure_id,
            namespace="artifacts",
            object_id=figure_id,
            filename="autumn-figure.png",
            mime_type="image/png",
            size_bytes=len(figure),
            owner_email=owner,
            owner_agent_id=agent_id,
            conversation_id=conversation_id,
            session_id=session_id,
            project_id=project_id,
            purpose="artifact_write",
            kind="file",
            status="attached",
        )
        await create_artifact_record(
            db,
            artifact_id=html_id,
            namespace="artifacts",
            object_id=html_id,
            filename="autumn-planner.html",
            mime_type="text/html",
            size_bytes=len(html),
            owner_email=owner,
            owner_agent_id=agent_id,
            conversation_id=conversation_id,
            session_id=session_id,
            project_id=project_id,
            purpose="artifact_write",
            kind="file",
            status="attached",
        )
        original = await create_deliverable(
            db,
            conversation_id=conversation_id,
            session_id=session_id,
            project_id=project_id,
            content="Lumen Autumn launch overview.",
            title=title,
            format="rich",
            rich=rich,
            published_owner_email=owner,
            artifact_store=store,
        )
        rich["blocks"][-1]["content"] = (
            "## The plan\n\nInvite returning customers to early access first. "
            "Make the invitation explicit: **explore the collection before the wider launch**. "
            "Follow with a concise launch announcement and a considered reminder."
        )
        revised = await create_deliverable(
            db,
            conversation_id=conversation_id,
            session_id=session_id,
            project_id=project_id,
            content="Lumen Autumn launch overview; revised early-access invitation.",
            title=title,
            format="rich",
            rich=rich,
            published_owner_email=owner,
            supersedes_deliverable_id=original.deliverable_id,
            artifact_store=store,
        )
        await db.commit()
        original_id, revised_id = original.deliverable_id, revised.deliverable_id

    return {
        "rich_authoring": rich,
        "resources": {
            "rich_title": title,
            "deliverable_id": revised_id,
            "original_deliverable_id": original_id,
            "html_artifact_id": html_id,
            "figure_artifact_id": figure_id,
        },
        "routes": {
            "rich": f"/library/deliverable/{revised_id}",
            "html": f"/library/artifact/{html_id}",
        },
        "usage": {},
    }
