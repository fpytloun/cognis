"""Human authoring and sharing through the actual HTTP and persistence boundary."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa

from cognis.api.routes.artifact_documents import router
from cognis.api.routes.artifacts import router as artifact_router
from cognis.store.models import Agent, ArtifactGrantRow, ArtifactRecordRow
from cognis.store.queries import create_agent
from tests.unit.test_library import OUTSIDER, OWNER, READER
from tests.unit.test_library import library_app as library_app


@pytest.fixture
async def authoring_app(library_app):
    app, client = library_app
    app.include_router(router)
    app.include_router(artifact_router)
    async with app.state.session_factory() as session:
        await create_agent(
            session,
            agent_id="library-recipient",
            owner_email=OWNER,
            name="Recipient",
            status="active",
        )
        await create_agent(
            session,
            agent_id="foreign-recipient",
            owner_email=READER,
            name="Foreign",
            status="active",
        )
        await session.commit()
    yield app, client


async def _create(client, **kwargs):
    return await client.post(
        "/api/v1/library/artifacts",
        headers={"x-library-user": OWNER},
        json={
            "filename": "config.json",
            "mime_type": "application/json",
            "content": '{"ready":true}',
            **kwargs,
        },
    )


async def test_agent_choices_and_access_use_display_names_and_avatars(authoring_app):
    app, client = authoring_app
    async with app.state.session_factory() as session:
        recipient = await session.get(Agent, "library-recipient")
        assert recipient is not None
        recipient.display_name = "Library editor"
        recipient.avatar_image_id = "img_library_recipient"
        recipient.avatar_url = "https://images.test/older-avatar.png"
        await session.commit()
    headers = {"x-library-user": OWNER}
    choices = (await client.get("/api/v1/library/agents", headers=headers)).json()
    choice = next(agent for agent in choices if agent["agent_id"] == "library-recipient")
    assert choice["name"] == "Library editor"
    assert choice["avatar_url"] == "/api/v1/images/img_library_recipient"
    created = (await _create(client, agent_ids=["library-recipient"])).json()
    access = (
        await client.get(f"/api/v1/library/items/artifact/{created['id']}/sharing", headers=headers)
    ).json()
    shared = next(agent for agent in access["agents"] if agent["agent_id"] == "library-recipient")
    assert shared["name"] == choice["name"]
    assert shared["avatar_url"] == choice["avatar_url"]
    assert shared["explicit"] is True


async def test_create_edit_immutable_revision_and_agent_access(authoring_app):
    app, client = authoring_app
    created = await _create(client, agent_ids=["library-recipient"])
    assert created.status_code == 200, created.text
    first = created.json()
    assert first["origin"] == "user" and first["revision"] == 1
    ref = first["id"]
    owner = {"x-library-user": OWNER}
    source = await client.get(f"/api/v1/library/artifacts/{ref}/edit", headers=owner)
    assert source.status_code == 200 and source.json()["content"] == '{"ready":true}'
    changed = await client.post(
        f"/api/v1/library/artifacts/{ref}/revisions",
        headers=owner,
        json={
            "filename": "renamed.json",
            "mime_type": "application/json",
            "content": '{"ready":false}',
        },
    )
    assert changed.status_code == 200, changed.text
    second = changed.json()
    assert second["id"] != ref and second["revision"] == 2 and second["lineage_id"] == ref
    assert (await client.get(f"/api/v1/library/artifacts/{ref}/edit", headers=owner)).json()[
        "content"
    ] == '{"ready":true}'
    access = (
        await client.get(f"/api/v1/library/items/artifact/{second['id']}/sharing", headers=owner)
    ).json()
    assert next(a for a in access["agents"] if a["agent_id"] == "library-recipient")["explicit"]
    revoked = await client.put(
        f"/api/v1/library/items/artifact/{second['id']}/sharing",
        headers=owner,
        json={"agent_ids": []},
    )
    assert revoked.status_code == 200
    async with app.state.session_factory() as session:
        assert not list(
            await session.scalars(
                sa.select(ArtifactGrantRow).where(ArtifactGrantRow.source == "explicit")
            )
        )
        row = await session.get(ArtifactRecordRow, second["id"])
        assert row.expires_at is None and row.message_role == "user"
    stale = await client.post(
        f"/api/v1/library/artifacts/{ref}/revisions",
        headers=owner,
        json={
            "filename": "config.json",
            "mime_type": "application/json",
            "content": "{}",
        },
    )
    assert stale.status_code == 409
    files = (await client.get("/api/v1/library/items?kind=artifact", headers=owner)).json()["items"]
    assert [r["id"] for r in files] == [second["id"]]


async def test_validation_and_forbidden_writes_leave_no_documents(authoring_app):
    app, client = authoring_app
    for body in ({"content": '{"unfinished":'}, {"agent_ids": ["foreign-recipient"]}):
        result = await _create(client, **body)
        assert result.status_code == 400
    created = (await _create(client)).json()
    ref = created["id"]
    for person in (READER, OUTSIDER):
        headers = {"x-library-user": person}
        assert (
            await client.get(f"/api/v1/library/artifacts/{ref}/edit", headers=headers)
        ).status_code == 404
        assert (
            await client.put(
                f"/api/v1/library/items/artifact/{ref}/sharing",
                headers=headers,
                json={"agent_ids": []},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/library/items/artifact/{ref}/public-link", headers=headers, json={}
            )
        ).status_code == 404
    async with app.state.session_factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        sa.select(ArtifactRecordRow).where(
                            ArtifactRecordRow.purpose == "user_artifact"
                        )
                    )
                )
            )
            == 1
        )


async def test_upload_durable_renamed_and_shared(authoring_app):
    _, client = authoring_app
    result = await client.post(
        "/api/v1/library/artifacts/upload",
        headers={"x-library-user": OWNER},
        files={"file": ("source.txt", b"Library file", "text/plain")},
        data={"filename": "brief.txt", "agent_ids": json.dumps(["library-recipient"])},
    )
    assert result.status_code == 200, result.text
    assert result.json()["title"] == "brief.txt" and result.json()["origin"] == "user"


@pytest.mark.parametrize("kind", ["artifact", "deliverable"])
async def test_public_view_download_and_expiration_options(
    authoring_app, kind, standalone_ui_build
):
    app, client = authoring_app
    owner = {"x-library-user": OWNER}
    ref = (
        (
            await _create(
                client, filename="page.html", mime_type="text/html", content="<h1>Shared page</h1>"
            )
        ).json()["id"]
        if kind == "artifact"
        else app.state.ids["private"]
    )
    base = f"/api/v1/library/items/{kind}/{ref}/public-link"
    viewed = await client.post(base, headers=owner, json={"ttl_seconds": 3600})
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["mode"] == "view"
    link = urlsplit(viewed.json()["url"])
    response = await client.get(
        link.path + ("?" + link.query if link.query else ""), follow_redirects=True
    )
    assert response.status_code == 200, response.text
    assert "attachment" not in response.headers.get("content-disposition", "")
    downloaded = (
        await client.post(base, headers=owner, json={"mode": "download", "ttl_seconds": 60})
    ).json()
    assert downloaded["mode"] == "download"
    link = urlsplit(downloaded["url"])
    response = await client.get(link.path + ("?" + link.query if link.query else ""))
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert (await client.post(base, headers=owner, json={"ttl_seconds": 0})).status_code == 422


def test_json_public_preview_uses_shared_json_renderer():
    from cognis.artifacts.preview import artifact_preview_payload

    payload = artifact_preview_payload(
        filename="settings.json", content_type="application/json", content=b'{"enabled":true}'
    )
    assert payload["content"] == '{"enabled":true}'
    assert payload["rich_payload"]["blocks"][0]["language"] == "json"


@pytest.mark.parametrize("mode", ["view", "download"])
async def test_artifact_public_link_follows_the_latest_revision(authoring_app, mode):
    """A shared link keeps serving the document as it is revised; tampering fails closed."""
    _, client = authoring_app
    owner = {"x-library-user": OWNER}
    first = (
        await _create(client, filename="notes.txt", mime_type="text/plain", content="one")
    ).json()
    created = await client.post(
        f"/api/v1/library/items/artifact/{first['id']}/public-link",
        headers=owner,
        json={"mode": mode, "ttl_seconds": 600},
    )
    assert created.status_code == 200, created.text
    link = urlsplit(created.json()["url"])
    assert link.path.startswith("/api/v1/artifacts/latest/")
    url = f"{link.path}?{link.query}"

    revised = await client.post(
        f"/api/v1/library/artifacts/{first['id']}/revisions",
        headers=owner,
        json={"filename": "notes.txt", "mime_type": "text/plain", "content": "two"},
    )
    assert revised.status_code == 200, revised.text
    opened = await client.get(url + ("&raw=1" if mode == "view" else ""))
    assert opened.status_code == 200, opened.text
    assert opened.text == "two"

    # A forged signature, another mode, or another document never verifies.
    assert (await client.get(url.replace("sig=", "sig=0"))).status_code == 403
    other_mode = "download" if mode == "view" else "view"
    assert (await client.get(url.replace(f"/{mode}?", f"/{other_mode}?"))).status_code == 403
    assert (await client.get(url.replace(first["id"], revised.json()["id"]))).status_code == 403
