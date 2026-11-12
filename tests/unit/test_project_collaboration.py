"""Project collaboration: roles, agent assignment, conversation projects, shared reading."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cognis.api.app import create_app
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_management import TOOL_GROUP_DEFINITIONS
from cognis.core.project_access import (
    ManagedProjectError,
    ProjectAssignmentError,
    append_project_instructions,
    can_read_conversation,
    change_conversation_project,
    managed_conversation_project_id,
    normalize_project_role,
    project_assignment_refusal,
    resolve_agent_use_grant,
    resolve_effective_project_id,
    role_satisfies,
)
from cognis.core.project_management import ProjectManagementError, ProjectManagementService
from cognis.models.config import GeneratedImage, ImageGenerationResult
from cognis.models.session import ConversationContext
from cognis.models.tool import stable_tool_id
from cognis.store.database import create_engine, create_session_factory
from cognis.store.project_queries import (
    get_default_project_for_agent,
    list_project_conversations,
    remove_project_agent,
    upsert_project_agent,
)
from cognis.store.queries import (
    create_agent,
    create_artifact_record,
    create_conversation,
    create_deliverable,
    create_project,
    create_project_grant,
    create_step_run,
    create_task,
    create_user,
    get_agent,
    get_conversation,
)
from cognis.tools.builtin.image import handle_image_tool
from cognis.tools.builtin.projects import (
    PROJECT_MANAGE_TOOL_IDS,
    PROJECT_READ_TOOL_IDS,
    build_project_tool_handlers,
    project_tools,
)
from cognis.tools.registry import ToolExecutionContext

OWNER = "owner@example.com"
WIFE = "wife@example.com"
VIEWER = "viewer@example.com"
OUTSIDER = "outsider@example.com"


def _native_context(actor: str = OWNER, agent: str = "butler") -> ToolExecutionContext:
    return ToolExecutionContext(
        executor_handle=None,  # type: ignore[arg-type]
        runtime_metadata={
            "user_email": actor,
            "agent_id": agent,
            "conversation_id": "conv_shared",
        },
    )


def test_native_project_sharing_readback_and_retry_safety(tmp_path: Path) -> None:
    from sqlalchemy import select

    from cognis.store.models import InboxNotificationRow

    async def body(factory: Any) -> None:
        inbox = SimpleNamespace(publish=AsyncMock())
        service = ProjectManagementService(factory, inbox_service=inbox)
        handlers = build_project_tool_handlers(factory, management_service=service)
        context = _native_context()
        recipients = await handlers["find_project_share_recipients"](
            {"project_id": "proj_home", "query": "wife"}, context
        )
        assert recipients == [{"email": WIFE, "name": "wife"}]
        assert (
            await handlers["find_project_share_recipients"](
                {"project_id": "proj_home", "query": "%%%"}, context
            )
            == []
        )
        for actor in (WIFE, VIEWER, OUTSIDER):
            with pytest.raises(ProjectManagementError):
                await handlers["find_project_share_recipients"](
                    {"project_id": "proj_home", "query": "wife"}, _native_context(actor)
                )
            with pytest.raises(ProjectManagementError):
                await handlers["create_project_grant"](
                    {
                        "project_id": "proj_home",
                        "grantee_user_email": OUTSIDER,
                        "permission": "member",
                    },
                    _native_context(actor),
                )
        args = {"project_id": "proj_home", "grantee_user_email": OUTSIDER, "permission": "member"}
        grant = await handlers["create_project_grant"](args, context)
        again = await handlers["create_project_grant"](args, context)
        assert grant["grant_id"] == again["grant_id"]
        with pytest.raises(ProjectManagementError, match="update the existing"):
            await handlers["create_project_grant"]({**args, "permission": "manager"}, context)
        for email in (OWNER, "missing@example.com"):
            with pytest.raises(ProjectManagementError):
                await handlers["create_project_grant"](
                    {**args, "grantee_user_email": email}, context
                )
        with pytest.raises(ProjectManagementError, match="Permission"):
            await handlers["create_project_grant"]({**args, "permission": "owner"}, context)
        read = await handlers["get_project"]({"project_id": "proj_home"}, context)
        assert {g["grantee_user_email"] for g in read["grants"]} == {WIFE, VIEWER, OUTSIDER}
        update = {"project_id": "proj_home", "grant_id": grant["grant_id"], "permission": "viewer"}
        await handlers["update_project_grant"](update, context)
        await handlers["update_project_grant"](update, context)
        with pytest.raises(ProjectManagementError):
            await handlers["revoke_project_grant"](
                {"project_id": "proj_work", "grant_id": grant["grant_id"]}, context
            )
        revoke = {"project_id": "proj_home", "grant_id": grant["grant_id"]}
        await handlers["revoke_project_grant"](revoke, context)
        await handlers["revoke_project_grant"](revoke, context)
        assert len(await handlers["list_project_grants"]({"project_id": "proj_home"}, context)) == 3
        assert (
            len(
                await handlers["list_project_grants"](
                    {"project_id": "proj_home"}, _native_context(WIFE)
                )
            )
            == 2
        )
        with pytest.raises(ProjectManagementError):
            await handlers["update_project_grant"](update, context)
        async with factory() as session:
            notices = (await session.scalars(select(InboxNotificationRow))).all()
        assert len(notices) == 3
        assert all(item.user_email == OUTSIDER for item in notices)
        assert (
            len([call for call in inbox.publish.await_args_list if call.args[0] is not None]) == 3
        )

    _with_db(tmp_path, body)


def test_native_generate_reference_avatar_roundtrip_and_scope(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        buffer = BytesIO()
        Image.new("RGB", (8, 8), "green").save(buffer, format="PNG")
        data = buffer.getvalue()
        store = ArtifactStore(ArtifactStoreConfig(path=str(tmp_path / "artifacts")))
        provider = MagicMock()
        provider.image_generate = AsyncMock(
            return_value=ImageGenerationResult(
                images=[
                    GeneratedImage(
                        b64_json=base64.b64encode(data).decode(), content_type="image/png"
                    )
                ],
                model="test-image-model",
            )
        )
        context = _native_context()
        generated = await handle_image_tool(
            "image_generate",
            {"prompt": "A house"},
            provider,
            artifact_store=store,
            session_factory=factory,
            user_email=OWNER,
            runtime_metadata=context.runtime_metadata,
        )
        assert not generated.is_error, generated.output
        image_id = json.loads(generated.output)["images"][0]["artifact_id"]
        async with factory() as session:
            from cognis.store.queries import get_artifact_record

            row = await get_artifact_record(session, image_id)
            assert row.owner_email == OWNER and row.owner_agent_id == "butler"
            assert row.conversation_id == "conv_shared"
        referenced = await handle_image_tool(
            "image_generate",
            {"prompt": "Match this house", "references": [image_id]},
            provider,
            artifact_store=store,
            session_factory=factory,
            user_email=OWNER,
            runtime_metadata=context.runtime_metadata,
        )
        assert not referenced.is_error, referenced.output
        assert (
            base64.b64decode(provider.image_generate.await_args.kwargs["images"][0].b64_json)
            == data
        )
        service = ProjectManagementService(factory, artifact_store=store)
        handlers = build_project_tool_handlers(factory, management_service=service)
        result = await handlers["set_project_avatar"](
            {"project_id": "proj_home", "content_ref": image_id}, context
        )
        avatar = result["avatar_image_id"]
        assert avatar != image_id and avatar
        assert (await store.async_load("avatars", avatar, "image"))[0] == data
        assert (await store.async_load_metadata("avatars", avatar, "image")).owner_email == OWNER
        read = await handlers["get_project"]({"project_id": "proj_home"}, context)
        assert read["avatar_image_id"] == avatar
        with pytest.raises(ProjectManagementError, match="Image not found"):
            await service.avatar_snapshot(
                image_id,
                actor=OWNER,
                project_owner=OWNER,
                runtime_metadata={"agent_id": "private-bot"},
            )
        for actor in (WIFE, VIEWER, OUTSIDER):
            with pytest.raises(ValueError):
                await handlers["set_project_avatar"](
                    {"project_id": "proj_home", "content_ref": image_id}, _native_context(actor)
                )
        for args in ({"clear": True, "content_ref": image_id}, {}, {"clear": False}):
            with pytest.raises(ValueError, match="exactly one"):
                await handlers["set_project_avatar"]({"project_id": "proj_home", **args}, context)
        cleared = await handlers["set_project_avatar"](
            {"project_id": "proj_home", "clear": True}, context
        )
        assert cleared["avatar_image_id"] is None
        assert (await store.async_load("images", image_id, "image"))[0] == data
        with pytest.raises(ProjectManagementError, match="Image not found"):
            await service.avatar_snapshot(
                "img_missing",
                actor=OWNER,
                project_owner=OWNER,
                runtime_metadata=context.runtime_metadata,
            )
        await store.async_save(
            "images", "img_corrupt", "image", b"not PNG", "image/png", owner_email=OWNER
        )
        async with factory() as session:
            await create_artifact_record(
                session,
                artifact_id="img_corrupt",
                namespace="images",
                object_id="img_corrupt",
                filename="image",
                owner_email=OWNER,
                owner_agent_id="butler",
                purpose="tool_output",
                kind="image",
                mime_type="image/png",
                size_bytes=7,
                status="attached",
            )
            await session.commit()
        with pytest.raises(ProjectManagementError, match="invalid"):
            await service.avatar_snapshot(
                "img_corrupt",
                actor=OWNER,
                project_owner=OWNER,
                runtime_metadata=context.runtime_metadata,
            )

    _with_db(tmp_path, body)


def test_native_project_workflow_ownership_parity(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        registry = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(owner_email=OUTSIDER, is_system=False))
        )
        service = ProjectManagementService(factory, workflow_registry=registry)
        handlers = build_project_tool_handlers(factory, management_service=service)
        context = _native_context()
        for tool, args in (
            (
                "attach_workflow_to_project",
                {"project_id": "proj_home", "workflow_id": "wf_private"},
            ),
            ("update_project", {"project_id": "proj_home", "default_workflow_id": "wf_private"}),
            ("create_project", {"name": "Test", "default_workflow_id": "wf_private"}),
        ):
            with pytest.raises(ProjectManagementError, match="Workflow not found"):
                await handlers[tool](args, context)
        registry.get.return_value = SimpleNamespace(owner_email=None, is_system=True)
        await handlers["update_project"](
            {"project_id": "proj_home", "default_workflow_id": "wf_public"}, context
        )

    _with_db(tmp_path, body)


def test_project_management_rest_preserves_error_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, OWNER)
        duplicate = client.post(
            "/api/v1/projects/proj_home/grants",
            headers=headers,
            json={"grantee_user_email": WIFE, "permission": "member"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "conflict"
        for method in ("patch", "delete"):
            response = getattr(client, method)(
                "/api/v1/projects/proj_home/grants/pgrant_missing",
                headers=headers,
                **({"json": {"permission": "viewer"}} if method == "patch" else {}),
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
        response = client.patch(
            "/api/v1/projects/proj_home",
            headers=headers,
            json={"default_workflow_id": "wf_missing"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_project_avatar_rest_validates_sources_and_shared_visibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                await session.commit()
            buffer = BytesIO()
            Image.new("RGB", (4, 4), "blue").save(buffer, format="PNG")
            store = client.app.state.artifact_store
            await store.async_save(
                "images",
                "img_owner_source",
                "image",
                buffer.getvalue(),
                "image/png",
                owner_email=OWNER,
            )
            await store.async_save(
                "images",
                "img_foreign_source",
                "image",
                buffer.getvalue(),
                "image/png",
                owner_email=OUTSIDER,
            )

        client.portal.call(seed)
        headers = _headers(client.app, OWNER)
        response = client.patch(
            "/api/v1/projects/proj_home",
            headers=headers,
            json={"avatar_image_id": "img_foreign_source"},
        )
        assert response.status_code == 404
        # An administrator has no implicit right to read foreign private images.
        assert (
            client.get(
                "/api/v1/images/img_foreign_source", headers=_headers(client.app, OWNER, "admin")
            ).status_code
            == 404
        )
        response = client.patch(
            "/api/v1/projects/proj_home",
            headers=headers,
            json={"avatar_image_id": "img_owner_source"},
        )
        assert response.status_code == 200, response.text
        avatar_id = response.json()["avatar_image_id"]
        assert avatar_id != "img_owner_source"
        assert (
            client.get(
                f"/api/v1/images/{avatar_id}", headers=_headers(client.app, WIFE)
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/images/img_owner_source", headers=_headers(client.app, WIFE)
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/images/{avatar_id}", headers=_headers(client.app, OUTSIDER)
            ).status_code
            == 404
        )
        assert (
            client.patch(
                "/api/v1/projects/proj_home", headers=headers, json={"avatar_image_id": None}
            ).json()["avatar_image_id"]
            is None
        )
        assert (
            client.get(
                f"/api/v1/images/{avatar_id}", headers=_headers(client.app, WIFE)
            ).status_code
            == 404
        )


def test_project_activity_workflow_deliverable_uses_author_step_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> tuple[str, str]:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                task = await create_task(
                    session,
                    created_by=OWNER,
                    agent_id="butler",
                    title="Project report",
                    project_id="proj_home",
                )
                step = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_name="report",
                    step_type="run",
                    agent_id="butler",
                )
                row = await create_deliverable(
                    session,
                    artifact_store=client.app.state.artifact_store,
                    step_run_id=step.step_run_id,
                    title="Project report",
                    content="Verified summary. " * 100,
                    format="markdown",
                )
                row.status = "approved"
                task.visibility = "project"
                await session.commit()
                return row.deliverable_id, step.step_run_id

        deliverable_id, step_id = client.portal.call(seed)
        for email, readonly in [(OWNER, False), (WIFE, True)]:
            response = client.get(
                "/api/v1/projects/proj_home/activity", headers=_headers(client.app, email)
            )
            assert response.status_code == 200
            item = next(
                item
                for item in response.json()["deliverables"]
                if item["deliverable_id"] == deliverable_id
            )
            assert item["step_run_id"] == step_id
            assert item["is_readonly_for_caller"] is readonly
            assert item["content_preview"].startswith("Verified summary.")
            assert len(item["content_preview"]) == 600
            path = (
                f"/api/v1/deliverables/{deliverable_id}"
                if readonly
                else f"/api/v1/step-runs/{step_id}/deliverables/{deliverable_id}"
            )
            result = client.get(path, headers=_headers(client.app, email))
            assert result.status_code == 200, result.text
        assert (
            client.get(
                f"/api/v1/deliverables/{deliverable_id}", headers=_headers(client.app, OWNER)
            ).status_code
            == 404
        )
        assert client.get(
            "/api/v1/projects/proj_home/activity", headers=_headers(client.app, OUTSIDER)
        ).status_code in (403, 404)


def test_project_conversation_activity_is_only_queried_for_author(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                await session.commit()

        client.portal.call(seed)
        scheduler = SimpleNamespace(
            durable_running_turn_states=AsyncMock(
                return_value={"conv_shared": {"turn_id": "turn_active", "chat_mode": "build"}}
            )
        )
        monkeypatch.setattr(client.app.state, "turn_scheduler", scheduler)
        response = client.get(
            "/api/v1/projects/proj_home/conversations/page", headers=_headers(client.app, OWNER)
        )
        assert response.status_code == 200, response.text
        shared = next(
            item for item in response.json()["items"] if item["conversation_id"] == "conv_shared"
        )
        assert shared["has_active_turn"]
        assert "conv_shared" in scheduler.durable_running_turn_states.call_args.args[0]
        scheduler.durable_running_turn_states.reset_mock()
        response = client.get(
            "/api/v1/projects/proj_home/conversations/page", headers=_headers(client.app, WIFE)
        )
        assert response.status_code == 200, response.text
        assert all(not item["has_active_turn"] for item in response.json()["items"])
        scheduler.durable_running_turn_states.assert_not_called()


def test_project_conversation_attention_matches_normal_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.store.models import NotificationRow

    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                session.add(
                    NotificationRow(
                        notification_id="question-project",
                        conversation_id="conv_shared",
                        user_email=OWNER,
                        notification_type="step_question",
                        status="pending",
                        payload={
                            "question": "Choose a rollout mode.",
                            "questions": [
                                {
                                    "id": "rollout",
                                    "question": "Choose a rollout mode.",
                                    "allow_custom": True,
                                }
                            ],
                        },
                    )
                )
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, OWNER)

        def shared(items: list[dict[str, Any]]) -> dict[str, Any]:
            return next(item for item in items if item["conversation_id"] == "conv_shared")

        project = shared(
            client.get("/api/v1/projects/proj_home/conversations/page", headers=headers).json()[
                "items"
            ]
        )
        response = client.get(
            "/api/v1/conversations",
            headers=headers,
            params={"project_id": "proj_home", "include_attention_actions": True},
        )
        assert response.status_code == 200, response.text
        normal = shared(response.json()["items"])
        assert project["pending_notification_types"] == ["step_question"]
        assert project["pending_notification_types"] == normal["pending_notification_types"]
        assert project["attention_actions"] == normal["attention_actions"]
        assert len(project["attention_actions"]) == 1
        assert project["attention_actions"][0]["can_resolve"]
        assert not project["has_active_turn"]

        readonly = shared(
            client.get(
                "/api/v1/projects/proj_home/conversations/page",
                headers=_headers(client.app, WIFE),
            ).json()["items"]
        )
        assert readonly["pending_notification_types"] == []
        assert readonly["attention_actions"] == []
        assert readonly["is_readonly_for_caller"]

        async def resolve() -> None:
            async with client.app.state.session_factory() as session:
                row = await session.get(NotificationRow, "question-project")
                assert row is not None
                row.status = "resolved"
                await session.commit()

        client.portal.call(resolve)
        resolved = shared(
            client.get("/api/v1/projects/proj_home/conversations/page", headers=headers).json()[
                "items"
            ]
        )
        assert resolved["pending_notification_types"] == []
        assert resolved["attention_actions"] == []


def test_project_conversation_pages_filter_work_before_pagination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                for index in range(35):
                    row = await create_conversation(
                        session,
                        user_email=OWNER,
                        agent_id="butler",
                        conversation_id=f"conv_work_{index}",
                        context_type="task",
                        project_id="proj_home",
                        visibility="project",
                    )
                    row.last_message_at = datetime(2035, 1, 1, tzinfo=UTC)
                row = await create_conversation(
                    session,
                    user_email=OWNER,
                    agent_id="butler",
                    conversation_id="conv_control",
                    context_type="web",
                    project_id="proj_home",
                    visibility="project",
                )
                row.context_data = {"kind": "task_control"}
                row.last_message_at = datetime(2035, 1, 1, tzinfo=UTC)
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, WIFE)
        page = client.get(
            "/api/v1/projects/proj_home/conversations/page",
            headers=headers,
            params={"limit": 1},
        ).json()
        assert len(page["items"]) == 1
        ids = [page["items"][0]["conversation_id"]]
        while page["has_more"]:
            response = client.get(
                "/api/v1/projects/proj_home/conversations/page",
                headers=headers,
                params={"limit": 1, "cursor": page["cursor"]},
            )
            assert response.status_code == 200, response.text
            page = response.json()
            ids.extend(item["conversation_id"] for item in page["items"])
        assert len(ids) == len(set(ids))
        assert not any(item.startswith("conv_work_") or item == "conv_control" for item in ids)
        all_rows = client.get(
            "/api/v1/projects/proj_home/conversations", headers=headers, params={"limit": 100}
        ).json()
        assert any(item["conversation_id"] == "conv_work_0" for item in all_rows)


@pytest.mark.parametrize("resource", ["conversations", "tasks"])
def test_project_keyset_pages_recheck_access_and_preserve_ties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resource: str
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                for index in range(5):
                    if resource == "conversations":
                        row = await create_conversation(
                            session,
                            user_email=OWNER,
                            agent_id="butler",
                            context_type="web",
                            conversation_id=f"conv_page_{index}",
                            project_id="proj_home",
                            visibility="project",
                        )
                        row.last_message_at = datetime(2030, 1, 1, tzinfo=UTC)
                    else:
                        row = await create_task(
                            session,
                            created_by=OWNER,
                            agent_id="butler",
                            title=f"Task {index}",
                            project_id="proj_home",
                        )
                        row.updated_at = datetime(2030, 1, 1, tzinfo=UTC)
                    if index == 4:
                        row.visibility = "private"
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, WIFE)
        url = f"/api/v1/projects/proj_home/{resource}/page"
        first = client.get(url, headers=headers, params={"limit": 2})
        assert first.status_code == 200, first.text
        page = first.json()
        assert page["has_more"]
        cursor = page["cursor"]
        key = "conversation_id" if resource == "conversations" else "task_id"
        ids = [item[key] for item in page["items"]]
        assert all(item["is_readonly_for_caller"] for item in page["items"])
        while page["has_more"]:
            response = client.get(
                url, headers=headers, params={"limit": 2, "cursor": page["cursor"]}
            )
            assert response.status_code == 200, response.text
            page = response.json()
            ids.extend(item[key] for item in page["items"])
        assert len(ids) == len(set(ids))
        assert len(ids) == (6 if resource == "conversations" else 4)
        assert "conv_private" not in ids
        assert "conv_page_4" not in ids
        # Tokens are boundaries, not permission grants.
        assert client.get(
            url, headers=_headers(client.app, OUTSIDER), params={"cursor": cursor}
        ).status_code in (403, 404)
        assert client.get(url, headers=headers, params={"cursor": "not-base64!"}).status_code == 400
        assert (
            client.get(
                url, headers=_headers(client.app, OWNER), params={"cursor": cursor}
            ).status_code
            == 400
        )
        grant = client.get(
            "/api/v1/projects/proj_home/grants", headers=_headers(client.app, OWNER)
        ).json()
        member_grant = next(
            item for item in grant if item["grantee_user_email"] == WIFE and not item["revoked_at"]
        )
        client.delete(
            f"/api/v1/projects/proj_home/grants/{member_grant['grant_id']}",
            headers=_headers(client.app, OWNER),
        )
        assert client.get(url, headers=headers, params={"cursor": cursor}).status_code in (403, 404)


def test_owner_detail_and_mutation_preserve_sidebar_project_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.store.queries import get_project

    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                project = await get_project(session, "proj_home")
                project.metadata_json = {"icon": "cognis"}
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, OWNER)
        rows = client.get("/api/v1/conversations", headers=headers).json()["items"]
        listed = next(row for row in rows if row["conversation_id"] == "conv_shared")
        for response in (
            client.get("/api/v1/conversations/conv_shared?include_state=false", headers=headers),
            client.patch(
                "/api/v1/conversations/conv_shared",
                headers=headers,
                json={"title": "Updated title"},
            ),
        ):
            assert response.status_code == 200, response.text
            detail = response.json()
            for field in (
                "project_name",
                "project_icon",
                "project_color",
                "project_avatar_url",
                "agent_display_name",
                "agent_avatar_url",
            ):
                assert detail[field] == listed[field]
            assert detail["project_name"] == "Home"
            assert detail["project_icon"] == "cognis"


def test_shared_chat_discovery_identity_and_revocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.store.queries import get_agent, get_project, list_project_grants

    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            await client.app.state.artifact_store.async_save(
                "avatars",
                "img_private_avatar",
                "image",
                b"avatar",
                "image/png",
                owner_email=OWNER,
            )
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                agent = await get_agent(session, "private-bot")
                agent.avatar_image_id = "img_private_avatar"
                from cognis.store.queries import get_conversation

                chat = await get_conversation(session, "conv_unassigned_agent")
                chat.active_session_id = "sess_owner_private"
                chat.active_executor_id = "owner_executor"
                chat.active_executor_source = "owner"
                chat.starred_at = datetime.now(UTC)
                chat.last_read_at = datetime.now(UTC)
                chat.context_data = {"private_runtime_hint": "owner-only"}
                chat.memory_labels = {"private_label": "owner-only"}
                project = await get_project(session, "proj_home")
                project.metadata_json = {"color": "#FBBF24"}
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, WIFE)
        assert client.get("/api/v1/conversations", headers=headers).json()["items"] == []
        response = client.get("/api/v1/conversations?include_shared=true", headers=headers)
        assert response.status_code == 200, response.text
        items = {item["conversation_id"]: item for item in response.json()["items"]}
        assert set(items) == {"conv_shared", "conv_unassigned_agent"}
        private_agent_chat = items["conv_unassigned_agent"]
        assert private_agent_chat["agent_display_name"] == "Private"
        assert private_agent_chat["project_name"] == "Home"
        assert private_agent_chat["project_color"] == "#fbbf24"
        assert private_agent_chat["is_readonly_for_caller"]
        assert not private_agent_chat["can_continue"]
        assert items["conv_shared"]["can_continue"]
        assert not private_agent_chat["has_active_turn"]
        assert private_agent_chat["active_session_id"] is None
        assert private_agent_chat["active_executor_id"] is None
        assert private_agent_chat["starred_at"] is None
        assert private_agent_chat["last_read_at"] is None
        # The author's acknowledgement must not clear this reader's unread marker.
        assert private_agent_chat["has_unread"]
        assert client.get("/api/v1/agents/private-bot", headers=headers).status_code == 403
        avatar = private_agent_chat["agent_avatar_url"]
        assert client.get(avatar, headers=headers).status_code == 200
        assert client.get(avatar, headers=headers).headers["cache-control"] == "private, no-store"
        assert client.get("/api/v1/images/img_private_avatar", headers=headers).status_code == 404
        assert (
            client.get(
                avatar.replace("conv_unassigned_agent", "conv_private"), headers=headers
            ).status_code
            == 404
        )
        detail = client.get("/api/v1/conversations/conv_unassigned_agent", headers=headers).json()
        assert detail["agent_avatar_url"] == avatar and not detail["can_continue"]
        assert detail["active_session_id"] is None
        assert detail["context"]["platform_data"] == {}
        assert detail["context"]["memory_labels"] == {}
        side = client.get("/api/v1/conversations/sidebar?include_shared=true", headers=headers)
        assert side.status_code == 200, side.text
        assert {item["conversation_id"] for item in side.json()["conversations"]["items"]} == set(
            items
        )
        assert all(
            item["active_session_id"] is None for item in side.json()["conversations"]["items"]
        )
        first = client.get(
            "/api/v1/conversations?include_shared=true&limit=1", headers=headers
        ).json()
        assert first["has_more"]
        cursor = first["cursor"]

        async def revoke() -> None:
            async with client.app.state.session_factory() as session:
                grant = next(
                    g
                    for g in await list_project_grants(session, "proj_home")
                    if g.grantee_user_email == WIFE
                )
                grant.revoked_at = datetime.now(UTC)
                await create_conversation(
                    session,
                    user_email=WIFE,
                    agent_id="wife-agent",
                    conversation_id="conv_personal",
                    context_type="web",
                )
                await session.commit()

        client.portal.call(revoke)
        assert client.get(avatar, headers=headers).status_code == 404
        assert [
            item["conversation_id"]
            for item in client.get(
                "/api/v1/conversations?include_shared=true", headers=headers
            ).json()["items"]
        ] == ["conv_personal"]
        resumed = client.get(
            "/api/v1/conversations",
            headers=headers,
            params={"include_shared": True, "limit": 1, "cursor": cursor},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["items"] == []


def test_project_activity_order_respects_visible_conversations_tasks_and_deliverables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                await create_project(
                    session, project_id="proj_personal", owner_email=WIFE, name="Personal"
                )
                chat = await create_conversation(
                    session,
                    user_email=WIFE,
                    agent_id="wife-agent",
                    context_type="web",
                    project_id="proj_personal",
                )
                chat.last_message_at = datetime(2030, 1, 1, tzinfo=UTC)
                shared = await get_conversation(session, "conv_shared")
                shared.last_message_at = datetime(2029, 1, 1, tzinfo=UTC)
                private = await get_conversation(session, "conv_private")
                private.last_message_at = datetime(2040, 1, 1, tzinfo=UTC)
                from cognis.store.queries import get_project

                home = await get_project(session, "proj_home")
                home.updated_at = datetime(2035, 1, 1, tzinfo=UTC)
                await session.commit()

        client.portal.call(seed)
        headers = _headers(client.app, WIFE)

        def order() -> list[str]:
            response = client.get("/api/v1/projects?order_by_activity=true", headers=headers)
            assert response.status_code == 200, response.text
            return [item["project_id"] for item in response.json()]

        assert order() == ["proj_personal", "proj_home"]

        async def add_task() -> str:
            async with client.app.state.session_factory() as session:
                task = await create_task(
                    session,
                    created_by=OWNER,
                    agent_id="butler",
                    title="Shared work",
                    project_id="proj_home",
                )
                task.visibility = "project"
                task.updated_at = datetime(2031, 1, 1, tzinfo=UTC)
                step = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_name="report",
                    step_type="run",
                    agent_id="butler",
                )
                await session.commit()
                return step.step_run_id

        step_id = client.portal.call(add_task)
        assert order() == ["proj_home", "proj_personal"]

        async def add_document() -> None:
            async with client.app.state.session_factory() as session:
                from cognis.store.queries import get_project

                personal = await get_project(session, "proj_personal")
                personal.updated_at = datetime(2032, 1, 1, tzinfo=UTC)
                row = await create_deliverable(
                    session,
                    artifact_store=client.app.state.artifact_store,
                    step_run_id=step_id,
                    title="Latest shared document",
                    content="Report",
                    format="markdown",
                )
                row.created_at = datetime(2033, 1, 1, tzinfo=UTC)
                row.status = "approved"
                await session.commit()

        client.portal.call(add_document)
        assert order() == ["proj_home", "proj_personal"]


def test_shared_read_markers_are_personal_and_clear_after_viewing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from cognis.store.models import ConversationReadState

    with _client(monkeypatch, tmp_path) as client:
        app = client.app
        source_message_at = datetime(2026, 1, 2, tzinfo=UTC)
        author_read_at = source_message_at + timedelta(days=1)

        async def seed() -> None:
            async with app.state.session_factory() as session:
                await _seed_home(session)
                row = await get_conversation(session, "conv_shared")
                row.created_at = source_message_at - timedelta(days=1)
                row.last_message_at = source_message_at
                row.last_read_at = author_read_at
                await session.commit()

        client.portal.call(seed)
        wife = _headers(app, WIFE)
        viewer = _headers(app, VIEWER)
        owner = _headers(app, OWNER)

        def detail(headers: dict[str, str]) -> dict[str, Any]:
            response = client.get(
                "/api/v1/conversations/conv_shared?include_state=false",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert not detail(owner)["has_unread"]
        assert detail(wife)["can_continue"]
        assert not detail(viewer)["can_continue"]
        assert client.post(
            "/api/v1/conversations/conv_shared/opened",
            headers=wife,
        ).json()["can_continue"]
        for headers in (wife, viewer):
            assert detail(headers)["has_unread"]
            assert detail(headers)["last_read_at"] is None

        sent = AsyncMock()
        monkeypatch.setattr(app.state.ws_manager, "send_to_user", sent)
        response = client.post(
            "/api/v1/conversations/conv_shared/read",
            headers=wife,
            json={"user_email": OWNER},
        )
        assert response.status_code == 200, response.text
        assert not detail(wife)["has_unread"]
        assert detail(viewer)["has_unread"]
        updates = [
            call.args
            for call in sent.await_args_list
            if call.args[1].get("type") == "conversation_updated"
        ]
        assert len(updates) == 1 and updates[0][0] == WIFE
        assert updates[0][1]["has_unread"] is False
        personal_read_at = detail(wife)["last_read_at"]
        for payload in (detail(wife), updates[0][1]):
            for field in ("last_read_at", "last_message_at"):
                assert datetime.fromisoformat(payload[field]).utcoffset() == timedelta(0)

        for path in (
            "/api/v1/conversations?include_shared=true",
            "/api/v1/projects/proj_home/conversations/page",
        ):
            result = client.get(path, headers=wife)
            assert result.status_code == 200, result.text
            shared = next(
                row for row in result.json()["items"] if row["conversation_id"] == "conv_shared"
            )
            assert shared["last_read_at"] == personal_read_at
            assert not shared["has_unread"]

        assert (
            client.post("/api/v1/conversations/conv_shared/read", headers=viewer).status_code == 200
        )
        assert not detail(viewer)["has_unread"]
        for headers, target in (
            (wife, "conv_private"),
            (_headers(app, OUTSIDER), "conv_shared"),
            (_headers(app, OUTSIDER, "admin"), "conv_shared"),
        ):
            assert (
                client.post(f"/api/v1/conversations/{target}/read", headers=headers).status_code
                == 403
            )

        async def next_message() -> None:
            async with app.state.session_factory() as session:
                row = await get_conversation(session, "conv_shared")
                assert row.last_read_at.replace(tzinfo=UTC) == author_read_at
                markers = list((await session.scalars(select(ConversationReadState))).all())
                assert {marker.user_email for marker in markers} == {WIFE, VIEWER}
                row.last_message_at = max(
                    marker.last_read_at.replace(tzinfo=UTC) for marker in markers
                ) + timedelta(seconds=1)
                await session.commit()

        client.portal.call(next_message)
        assert detail(wife)["has_unread"]
        assert detail(viewer)["has_unread"]


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    return TestClient(create_app())


def _headers(app: Any, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)
    return {"Authorization": f"Bearer {token}"}


async def _seed_home(session: Any, *, password_hash: str = "test") -> None:
    """Owner's Home project shared with a member (wife) and a viewer."""

    for email in (OWNER, WIFE, VIEWER, OUTSIDER):
        await create_user(
            session, email=email, name=email.split("@")[0], password_hash=password_hash
        )
    await create_agent(
        session, agent_id="butler", owner_email=OWNER, name="Butler", status="active"
    )
    await create_agent(
        session, agent_id="private-bot", owner_email=OWNER, name="Private", status="active"
    )
    await create_agent(
        session, agent_id="wife-agent", owner_email=WIFE, name="Wife Agent", status="active"
    )
    await create_project(session, owner_email=OWNER, name="Home", project_id="proj_home")
    await create_project(session, owner_email=OWNER, name="Work", project_id="proj_work")
    await create_project_grant(
        session,
        project_id="proj_home",
        granted_by=OWNER,
        grantee_user_email=WIFE,
        permission="member",
    )
    await create_project_grant(
        session,
        project_id="proj_home",
        granted_by=OWNER,
        grantee_user_email=VIEWER,
        permission="viewer",
    )
    await upsert_project_agent(
        session, project_id="proj_home", agent_id="butler", added_by=OWNER, is_default=True
    )
    await create_conversation(
        session,
        user_email=OWNER,
        agent_id="butler",
        context_type="web",
        conversation_id="conv_shared",
        project_id="proj_home",
        visibility="project",
        title="Kitchen reconstruction",
    )
    await create_conversation(
        session,
        user_email=OWNER,
        agent_id="butler",
        context_type="web",
        conversation_id="conv_private",
        project_id="proj_home",
        visibility="private",
    )
    await create_conversation(
        session,
        user_email=OWNER,
        agent_id="private-bot",
        context_type="web",
        conversation_id="conv_unassigned_agent",
        project_id="proj_home",
        visibility="project",
    )


# --- Pure policy ---


def test_native_schedule_project_assignment_clear_and_revalidation(tmp_path: Path) -> None:
    from cognis.store.queries import get_schedule
    from cognis.tools.builtin.schedule import handle_schedule_tool

    async def body(factory: Any) -> None:
        async def call(actor: str, **arguments: Any) -> Any:
            return await handle_schedule_tool(
                "manage_schedules",
                arguments,
                session_factory=factory,
                scheduler=None,
                user_email=actor,
                agent_id="butler",
            )

        created = await call(
            WIFE,
            action="create",
            name="Project schedule",
            schedule_type="interval",
            interval_seconds=60,
            project_id="proj_home",
        )
        assert not created.is_error, created.output
        listing = await call(WIFE, action="list", project_id="proj_home")
        rows = json.loads(listing.output)
        assert len(rows) == 1 and rows[0]["project_id"] == "proj_home"
        schedule_id = rows[0]["schedule_id"]
        changed = await call(WIFE, action="update", schedule_id=schedule_id, name="Renamed")
        assert not changed.is_error
        assert json.loads(changed.output)["project_id"] == "proj_home"
        # Project-only agent access cannot follow a schedule out of its project.
        denied = await call(WIFE, action="update", schedule_id=schedule_id, project_id=None)
        assert denied.is_error
        denied = await call(OWNER, action="update", schedule_id=schedule_id, project_id=None)
        assert denied.is_error
        assert (
            await call(
                VIEWER,
                action="create",
                name="Denied",
                schedule_type="interval",
                interval_seconds=60,
                project_id="proj_home",
            )
        ).is_error
        owner_created = await call(
            OWNER,
            action="create",
            name="Owner schedule",
            schedule_type="interval",
            interval_seconds=60,
            project_id="proj_home",
        )
        assert not owner_created.is_error, owner_created.output
        rows = json.loads((await call(OWNER, action="list", project_id="proj_home")).output)
        owner_id = rows[0]["schedule_id"]
        cleared = await call(OWNER, action="update", schedule_id=owner_id, project_id=None)
        assert not cleared.is_error, cleared.output
        async with factory() as session:
            assert (await get_schedule(session, owner_id)).project_id is None
            assert (await get_schedule(session, schedule_id)).project_id == "proj_home"

    _with_db(tmp_path, body)


def test_native_task_project_clear_and_omission_through_controller(tmp_path: Path) -> None:
    from cognis.core.agent_loop import AgentLoop
    from cognis.core.workflow_engine import StepContext
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import ConversationContext, ConversationModel
    from cognis.models.tool import ToolCall
    from cognis.models.workflow import StepDefinition
    from cognis.store.queries import get_task

    async def body(factory: Any) -> None:
        loop = object.__new__(AgentLoop)
        loop.session_manager = SimpleNamespace(session_factory=factory)
        loop._workflow_registry = None
        loop._event_bus = None
        context = StepContext(
            step_definition=StepDefinition(name="control", type="run", prompt=""),
            session=SimpleNamespace(user_email=OWNER, session_id="sess_project_task"),  # type: ignore[arg-type]
            conversation=ConversationModel(
                conversation_id="conv_project_owner",
                user_email=OWNER,
                agent_id="butler",
                context=ConversationContext(type="web", ref="topic:task-control"),
            ),
            agent=AgentDefinition(agent_id="butler", owner_email=OWNER, name="Butler"),
        )
        async with factory() as session:
            await create_task(
                session,
                task_id="task_project_clear",
                agent_id="butler",
                created_by=OWNER,
                title="Project task",
                description="",
                project_id="proj_home",
                status="draft",
            )
            await session.commit()
        for delta in ({"title": "Renamed"}, {"project_id": None}):
            result = await loop._handle_orchestration_tool(
                ToolCall(
                    call_id="tc_project_task",
                    name="update_task",
                    arguments={"task_id": "task_project_clear", **delta},
                ),
                ctx=context,
                events_to_record=[],
            )
            assert not result.is_error, result.output
            async with factory() as session:
                row = await get_task(session, "task_project_clear")
                assert row.project_id == (None if "project_id" in delta else "proj_home")

    _with_db(tmp_path, body)


def test_native_conversation_project_target_and_privacy(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        handler = build_project_tool_handlers(factory)["set_conversation_project"]
        async with factory() as session:
            await create_conversation(
                session,
                conversation_id="conv_move_target",
                agent_id="butler",
                user_email=OWNER,
                context_type="web",
                context_ref="topic:move",
                visibility="private",
            )
            await session.commit()
        args = {"conversation_id": "conv_move_target", "project_id": "proj_home"}
        result = await handler({**args, "visibility": "private"}, _native_context())
        assert result["project_id"] == "proj_home" and result["visibility"] == "private"
        await handler({**args, "visibility": "project"}, _native_context())
        with pytest.raises(ValueError):
            await handler(args, _native_context(WIFE))
        with pytest.raises(ValueError):
            await handler(args, _native_context(VIEWER))
        cleared = await handler(
            {**args, "project_id": None, "visibility": "private"}, _native_context()
        )
        assert cleared["project_id"] is None and cleared["visibility"] == "private"
        with pytest.raises(ValueError):
            await handler(
                {"conversation_id": "conv_dm", "project_id": "proj_home"}, _native_context()
            )

    _with_db(tmp_path, body)


def test_native_conversation_assignment_runtime_changes_on_next_turn(tmp_path: Path) -> None:
    from cognis.core.agent_loop import AgentLoop
    from cognis.core.workflow_engine import StepContext
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import ConversationContext, ConversationModel
    from cognis.models.workflow import StepDefinition

    async def body(factory: Any) -> None:
        loop = object.__new__(AgentLoop)
        loop._session_factory = factory
        async with factory() as session:
            await create_conversation(
                session,
                conversation_id="conv_runtime_move",
                agent_id="butler",
                user_email=OWNER,
                context_type="web",
                context_ref="topic:runtime",
            )
            await session.commit()

        async def next_context() -> StepContext:
            async with factory() as session:
                row = await get_conversation(session, "conv_runtime_move")
            return StepContext(
                step_definition=StepDefinition(name="control", type="run", prompt=""),
                session=SimpleNamespace(user_email=OWNER, session_id="sess_runtime_move"),  # type: ignore[arg-type]
                conversation=ConversationModel(
                    conversation_id=row.conversation_id,
                    user_email=OWNER,
                    agent_id="butler",
                    project_id=row.project_id,
                    context=ConversationContext(type="web", ref="topic:runtime"),
                ),
                agent=AgentDefinition(agent_id="butler", owner_email=OWNER, name="Butler"),
            )

        handler = build_project_tool_handlers(factory)["set_conversation_project"]
        context = _native_context()
        context.runtime_metadata["conversation_id"] = "conv_runtime_move"
        first = await next_context()
        assert await loop._effective_project_id(first) is None
        await handler({"project_id": "proj_home"}, context)
        assert await loop._effective_project_id(first) is None
        second = await next_context()
        assert await loop._effective_project_id(second) == "proj_home"
        await handler({"project_id": None}, context)
        assert await loop._effective_project_id(second) == "proj_home"
        third = await next_context()
        assert await loop._effective_project_id(third) is None

    _with_db(tmp_path, body)


def test_leave_project_cancellation_rolls_back_privacy_and_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognis.core.project_access import resolve_project_role_by_id
    from cognis.core.project_management import ProjectManagementService
    from cognis.store import project_queries

    async def body(factory: Any) -> None:
        async with factory() as session:
            await create_conversation(
                session,
                conversation_id="conv_cancel_leave",
                agent_id="butler",
                user_email=WIFE,
                context_type="web",
                context_ref="topic:cancel-leave",
                project_id="proj_home",
                visibility="project",
            )
            await session.commit()
        original = project_queries.revoke_active_project_grants_for_user

        async def revoke_then_cancel(*args: Any, **kwargs: Any) -> Any:
            await original(*args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(
            project_queries, "revoke_active_project_grants_for_user", revoke_then_cancel
        )
        with pytest.raises(asyncio.CancelledError):
            await ProjectManagementService(factory).leave("proj_home", WIFE, make_private=True)
        async with factory() as session:
            row = await get_conversation(session, "conv_cancel_leave")
            assert row.visibility == "project"
            assert (await resolve_project_role_by_id(session, "proj_home", WIFE))[
                1
            ].role == "member"

    _with_db(tmp_path, body)


def test_native_project_restore_leave_and_shared_task_metadata(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        handlers = build_project_tool_handlers(factory)
        args = {"project_id": "proj_home"}
        await handlers["delete_project"](args, _native_context())
        with pytest.raises(ValueError):
            await handlers["restore_project"](args, _native_context(WIFE))
        restored = await handlers["restore_project"](args, _native_context())
        assert restored["status"] == "active"
        context = _native_context(WIFE)
        context.runtime_metadata["runtime_access"] = {
            "project_id": "proj_home",
            "agent_id": "butler",
            "agent_owner_email": OWNER,
        }
        async with factory() as session:
            private_task = await create_task(
                session,
                task_id="task_private_owner",
                title="Private",
                description="",
                agent_id="butler",
                created_by=OWNER,
                project_id="proj_home",
            )
            private_task.visibility = "private"
            shared_task = await create_task(
                session,
                task_id="task_shared_owner",
                title="Shared",
                description="",
                agent_id="butler",
                created_by=OWNER,
                project_id="proj_home",
            )
            shared_task.visibility = "project"
            await create_conversation(
                session,
                conversation_id="conv_leave_authored",
                agent_id="butler",
                user_email=WIFE,
                context_type="web",
                context_ref="topic:leave",
                project_id="proj_home",
                visibility="project",
            )
            await session.commit()
        page = await handlers["list_project_tasks"]({**args, "limit": 1}, context)
        ids = [row["task_id"] for row in page["items"]]
        while page["next_cursor"]:
            page = await handlers["list_project_tasks"](
                {**args, "limit": 1, "cursor": page["next_cursor"]}, context
            )
            ids.extend(row["task_id"] for row in page["items"])
        assert "task_shared_owner" in ids and "task_private_owner" not in ids
        with pytest.raises(ProjectManagementError):
            await handlers["leave_project"](args, _native_context())
        left = await handlers["leave_project"]({**args, "make_private": True}, context)
        assert left["ok"] and left["made_private"] >= 1
        async with factory() as session:
            assert (await get_conversation(session, "conv_leave_authored")).visibility == "private"
        with pytest.raises(ValueError):
            await handlers["get_project"](args, context)

    _with_db(tmp_path, body)


def test_native_project_knowledge_tools_and_retrieval_revocation(tmp_path: Path) -> None:
    from cognis.knowledgebase.access import (
        KnowledgebaseAccessContext,
        resolve_knowledgebase_access,
    )
    from cognis.store.models import ProjectKnowledgebaseRow
    from cognis.store.queries import create_knowledgebase

    async def body(factory: Any) -> None:
        async with factory() as session:
            owned = await create_knowledgebase(session, owner_email=OWNER, name="Egypt")
            foreign = await create_knowledgebase(session, owner_email=WIFE, name="Private Egypt")
            await session.commit()
            kb_id, foreign_id = owned.knowledgebase_id, foreign.knowledgebase_id
        handlers = build_project_tool_handlers(factory)
        args = {"project_id": "proj_home", "knowledgebase_id": kb_id}
        assignment = handlers["assign_knowledgebase_to_project"]
        removal = handlers["unassign_knowledgebase_from_project"]
        for actor in (WIFE, VIEWER, OUTSIDER):
            with pytest.raises(PermissionError):
                await assignment(args, _native_context(actor))
        with pytest.raises(PermissionError):
            await assignment({**args, "knowledgebase_id": foreign_id}, _native_context())
        with pytest.raises(ValueError):
            await assignment({**args, "knowledgebase_id": "kb_missing"}, _native_context())
        results = await asyncio.gather(
            assignment(args, _native_context()), assignment(args, _native_context())
        )
        assert results == [{**args, "assigned": True}] * 2
        async with factory() as session:
            assert await session.get(ProjectKnowledgebaseRow, ("proj_home", kb_id))
        for actor in (OWNER, WIFE, VIEWER):
            rows = await handlers["list_project_knowledgebases"](
                {"project_id": "proj_home", "query": "EGYPT"}, _native_context(actor)
            )
            assert [row["knowledgebase_id"] for row in rows] == [kb_id]
            project = await handlers["get_project"](
                {"project_id": "proj_home"}, _native_context(actor)
            )
            assert project["knowledgebases"] == rows
        assert (
            await handlers["list_project_knowledgebases"](
                {"project_id": "proj_home", "query": "unmatched"}, _native_context()
            )
            == []
        )
        with pytest.raises(ValueError):
            await handlers["get_project"]({"project_id": "proj_home"}, _native_context(OUTSIDER))
        context = KnowledgebaseAccessContext(
            actor_email=WIFE, agent_id="butler", agent_owner_email=OWNER, project_id="proj_home"
        )
        async with factory() as session:
            assert await resolve_knowledgebase_access(
                session, knowledgebase_id=kb_id, context=context, mode="use"
            )
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id=kb_id, context=context, mode="manage"
                )
                is None
            )
            for other in (
                KnowledgebaseAccessContext(
                    actor_email=WIFE, agent_id="butler", agent_owner_email=OWNER
                ),
                KnowledgebaseAccessContext(
                    actor_email=WIFE, agent_id="private-bot", project_id="proj_home"
                ),
                KnowledgebaseAccessContext(
                    actor_email=OUTSIDER, agent_id="butler", project_id="proj_home"
                ),
            ):
                assert (
                    await resolve_knowledgebase_access(
                        session, knowledgebase_id=kb_id, context=other, mode="use"
                    )
                    is None
                )
        with pytest.raises(PermissionError):
            await removal(args, _native_context(WIFE))
        await removal(args, _native_context())
        await removal(args, _native_context())
        async with factory() as session:
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id=kb_id, context=context, mode="use"
                )
                is None
            )
            assert await session.get(ProjectKnowledgebaseRow, ("proj_home", kb_id)) is None
        # KB owners can withdraw their own content even after losing project membership.
        async with factory() as session:
            from cognis.store.project_queries import revoke_active_project_grants_for_user
            from cognis.store.queries import get_active_project_grant

            grant = await get_active_project_grant(session, "proj_home", WIFE)
            assert grant is not None
            grant.permission = "manager"
            await session.commit()
        foreign_args = {**args, "knowledgebase_id": foreign_id}
        await assignment(foreign_args, _native_context(WIFE))
        async with factory() as session:
            await revoke_active_project_grants_for_user(session, "proj_home", WIFE)
            await session.commit()
        await removal(foreign_args, _native_context(WIFE))

    _with_db(tmp_path, body)


def test_native_project_conversation_listing_scoped_pagination(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from cognis.store.project_queries import revoke_active_project_grants_for_user
    from cognis.tools.builtin.conversations import build_conversation_tool_handlers

    async def body(factory: Any) -> None:
        handlers = build_conversation_tool_handlers(factory, SimpleNamespace())
        context = _native_context(WIFE)
        context.runtime_metadata["runtime_access"] = {
            "project_id": "proj_home",
            "agent_id": "butler",
            "agent_owner_email": OWNER,
        }
        args = {"project_id": "proj_home", "limit": 1}
        page = await handlers["list_project_conversations"](args, context)
        assert len(page["items"]) == 1
        assert page["next_cursor"]
        second = await handlers["list_project_conversations"](
            {**args, "cursor": page["next_cursor"]}, context
        )
        assert second["next_cursor"] is None
        items = page["items"] + second["items"]
        assert {row["conversation_id"] for row in items} == {"conv_shared", "conv_unassigned_agent"}
        assert all(
            set(row)
            == {
                "conversation_id",
                "title",
                "agent_id",
                "visibility",
                "status",
                "last_message_at",
                "created_at",
            }
            for row in items
        )
        filtered = await handlers["list_project_conversations"](
            {**args, "agent_id": "butler"}, context
        )
        assert [row["conversation_id"] for row in filtered["items"]] == ["conv_shared"]
        with pytest.raises(HTTPException):
            await handlers["list_project_conversations"](
                {**args, "agent_id": "butler", "cursor": page["next_cursor"]}, context
            )
        for actor, agent, project in (
            (OUTSIDER, "butler", "proj_home"),
            (WIFE, "private-bot", "proj_home"),
            (WIFE, "butler", "proj_work"),
            (VIEWER, "butler", "proj_home"),
        ):
            denied = _native_context(actor)
            denied.runtime_metadata["runtime_access"] = {
                "project_id": project,
                "agent_id": agent,
                "agent_owner_email": OWNER,
            }
            with pytest.raises(ValueError):
                await handlers["list_project_conversations"](args, denied)
        async with factory() as session:
            await revoke_active_project_grants_for_user(session, "proj_home", WIFE)
            await session.commit()
        with pytest.raises(ValueError):
            await handlers["list_project_conversations"](
                {**args, "cursor": page["next_cursor"]}, context
            )

    _with_db(tmp_path, body)


@pytest.mark.parametrize("restriction", ["inactive", "global_viewer"])
def test_project_knowledge_mutation_rejects_disabled_or_global_viewer_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restriction: str
) -> None:
    from cognis.store.models import ProjectKnowledgebaseRow
    from cognis.store.queries import create_knowledgebase, get_user

    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> str:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                kb = await create_knowledgebase(session, owner_email=OWNER, name="Restricted KB")
                await session.commit()
                return kb.knowledgebase_id

        kb_id = client.portal.call(seed)
        factory = client.app.state.session_factory
        handlers = build_project_tool_handlers(factory)
        args = {"project_id": "proj_home", "knowledgebase_id": kb_id}
        client.portal.call(handlers["assign_knowledgebase_to_project"], args, _native_context())

        async def restrict() -> None:
            async with factory() as session:
                user = await get_user(session, OWNER)
                assert user is not None
                if restriction == "inactive":
                    user.is_active = False
                else:
                    user.role = "viewer"
                await session.commit()

        client.portal.call(restrict)
        for native in ("assign_knowledgebase_to_project", "unassign_knowledgebase_from_project"):
            with pytest.raises(PermissionError):
                client.portal.call(handlers[native], args, _native_context())
        endpoint = f"/api/v1/projects/proj_home/knowledgebases/{kb_id}"
        for method in ("put", "delete"):
            response = getattr(client, method)(endpoint, headers=_headers(client.app, OWNER))
            assert response.status_code in (401, 403)

        async def unchanged() -> None:
            async with factory() as session:
                assert await session.get(ProjectKnowledgebaseRow, ("proj_home", kb_id))

        client.portal.call(unchanged)


def test_project_knowledge_read_access_and_revocation(tmp_path: Path) -> None:
    from cognis.core.project_knowledge import set_project_knowledgebase
    from cognis.knowledgebase.access import (
        KnowledgebaseAccessContext,
        list_available_knowledgebases,
        resolve_knowledgebase_access,
    )
    from cognis.store.queries import create_knowledgebase, list_knowledgebases_for_user

    async def run(factory: Any) -> None:
        async with factory() as session:
            kb = await create_knowledgebase(session, owner_email=OWNER, name="Home manual")
            await set_project_knowledgebase(
                session,
                actor_email=OWNER,
                project_id="proj_home",
                knowledgebase_id=kb.knowledgebase_id,
                assigned=True,
            )
            await session.commit()
            kb_id = kb.knowledgebase_id
        async with factory() as session:
            for email in (WIFE, VIEWER):
                access = await resolve_knowledgebase_access(
                    session,
                    knowledgebase_id=kb_id,
                    context=KnowledgebaseAccessContext(actor_email=email),
                    mode="view",
                )
                assert access and access.is_project_grantee and not access.is_owner
                assert (
                    await resolve_knowledgebase_access(
                        session,
                        knowledgebase_id=kb_id,
                        context=KnowledgebaseAccessContext(actor_email=email),
                        mode="manage",
                    )
                    is None
                )
                assert kb_id in [
                    row.knowledgebase_id
                    for row in await list_knowledgebases_for_user(session, email)
                ]
            context = KnowledgebaseAccessContext(
                actor_email=WIFE,
                agent_id="butler",
                agent_owner_email=OWNER,
                project_id="proj_home",
            )
            assert await resolve_knowledgebase_access(
                session, knowledgebase_id=kb_id, context=context, mode="use"
            )
            assert kb_id in [
                row.knowledgebase_id
                for row in await list_available_knowledgebases(session, context=context)
            ]
            for denied in (
                KnowledgebaseAccessContext(actor_email=OUTSIDER),
                KnowledgebaseAccessContext(
                    actor_email=WIFE,
                    agent_id="butler",
                    project_id="proj_work",
                ),
                KnowledgebaseAccessContext(
                    actor_email=WIFE,
                    agent_id="private-bot",
                    project_id="proj_home",
                ),
                KnowledgebaseAccessContext(actor_email=WIFE, agent_id="butler"),
            ):
                assert (
                    await resolve_knowledgebase_access(
                        session, knowledgebase_id=kb_id, context=denied, mode="use"
                    )
                    is None
                )
            with pytest.raises(PermissionError):
                await set_project_knowledgebase(
                    session,
                    actor_email=WIFE,
                    project_id="proj_work",
                    knowledgebase_id=kb_id,
                    assigned=True,
                )
            await set_project_knowledgebase(
                session,
                actor_email=OWNER,
                project_id="proj_home",
                knowledgebase_id=kb_id,
                assigned=False,
            )
            await session.commit()
            assert (
                await resolve_knowledgebase_access(
                    session, knowledgebase_id=kb_id, context=context, mode="use"
                )
                is None
            )
            assert await list_available_knowledgebases(session, context=context) == []

    _with_db(tmp_path, run)


def test_project_knowledge_api_enforces_ownership_and_member_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cognis.store.queries import create_knowledgebase

    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> str:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                kb = await create_knowledgebase(
                    session, owner_email=OWNER, name="Project knowledge"
                )
                await session.commit()
                return kb.knowledgebase_id

        kb_id = client.portal.call(seed)
        url = f"/api/v1/projects/proj_home/knowledgebases/{kb_id}"
        assert client.put(url, headers=_headers(client.app, WIFE)).status_code == 403
        assert client.put(url, headers=_headers(client.app, OWNER)).status_code == 200
        response = client.get(
            "/api/v1/projects/proj_home/knowledgebases", headers=_headers(client.app, WIFE)
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["access_level"] == "shared"
        assert client.get(
            "/api/v1/projects/proj_home/knowledgebases", headers=_headers(client.app, OUTSIDER)
        ).status_code in (403, 404)
        assert client.delete(url, headers=_headers(client.app, WIFE)).status_code == 403
        assert client.delete(url, headers=_headers(client.app, OWNER)).status_code == 200
        assert (
            client.get(
                "/api/v1/projects/proj_home/knowledgebases", headers=_headers(client.app, WIFE)
            ).json()
            == []
        )


def test_project_agent_shared_conversation_policy(tmp_path: Path) -> None:
    from cognis.core.project_conversation_access import readable_project_conversation

    async def run(factory: Any) -> None:
        access = {"project_id": "proj_home", "agent_id": "butler", "agent_owner_email": OWNER}
        async with factory() as session:
            row = await readable_project_conversation(
                session, actor=WIFE, conversation_id="conv_shared", runtime_access=access
            )
            assert row.user_email == OWNER
            for actor, conv_id, runtime in (
                (WIFE, "conv_private", access),
                (OUTSIDER, "conv_shared", access),
                (WIFE, "conv_shared", {**access, "project_id": "proj_work"}),
                (WIFE, "conv_shared", {**access, "agent_id": "private-bot"}),
                (WIFE, "conv_shared", {}),
            ):
                with pytest.raises(ValueError):
                    await readable_project_conversation(
                        session, actor=actor, conversation_id=conv_id, runtime_access=runtime
                    )
            await remove_project_agent(session, project_id="proj_home", agent_id="butler")
            await session.commit()
            with pytest.raises(ValueError):
                await readable_project_conversation(
                    session, actor=WIFE, conversation_id="conv_shared", runtime_access=access
                )

    _with_db(tmp_path, run)


def test_roles_and_legacy_use_permission() -> None:
    assert normalize_project_role("use") == "member"
    assert normalize_project_role("manager") == "manager"
    assert normalize_project_role("bogus") is None
    assert role_satisfies("viewer", "view")
    assert not role_satisfies("viewer", "use")
    assert role_satisfies("member", "use")
    assert not role_satisfies("member", "manage")
    assert role_satisfies("manager", "manage")
    assert not role_satisfies("manager", "share")
    assert role_satisfies("owner", "share")
    assert not role_satisfies(None, "view")


def test_append_instructions_keeps_existing_text() -> None:
    assert append_project_instructions(None, " New rule ") == "New rule"
    assert append_project_instructions("Rule A\n", "Rule B") == "Rule A\n\nRule B"
    assert append_project_instructions("Rule A", "  ") == "Rule A"


@pytest.mark.parametrize(
    ("context", "assignable"),
    [
        (ConversationContext(type="web", ref=None), True),
        (ConversationContext(type="web", ref="web:user:owner@example.com:default"), False),
        (ConversationContext(type="web", ref="web:agent_direct:butler"), False),
        (ConversationContext(type="slack", ref="C123"), False),
        (ConversationContext(type="task", ref="task_1"), False),
        (ConversationContext(type="web", platform_data={"kind": "task_control"}), False),
        (ConversationContext(type="agent_work", ref=None), False),
    ],
)
def test_project_assignment_only_for_topic_conversations(
    context: ConversationContext, assignable: bool
) -> None:
    assert (project_assignment_refusal(SimpleNamespace(context=context)) is None) is assignable


def test_project_tool_groups_cover_all_tools() -> None:
    groups = {group.group_id: group for group in TOOL_GROUP_DEFINITIONS}
    assert set(groups["projects_read"].tool_ids) == set(PROJECT_READ_TOOL_IDS)
    assert set(groups["projects_manage"].tool_ids) == {
        stable_tool_id(tool) for tool in project_tools()
    }
    assert "builtin:set_conversation_project" in groups["projects_manage"].tool_ids
    assert set(PROJECT_READ_TOOL_IDS) < set(PROJECT_MANAGE_TOOL_IDS)


# --- Store / core policy against a real schema ---


def _with_db(tmp_path: Path, body: Any) -> None:
    async def run() -> None:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/projects.db")
        await run_schema_bootstrap(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            await _seed_home(session)
            await session.commit()
        try:
            await body(factory)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_project_agent_grant_is_scoped_to_its_project(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            in_project = await resolve_agent_use_grant(
                session, agent_id="butler", user_email=WIFE, project_id="proj_home"
            )
            outside = await resolve_agent_use_grant(
                session, agent_id="butler", user_email=WIFE, project_id=None
            )
            other_project = await resolve_agent_use_grant(
                session, agent_id="butler", user_email=WIFE, project_id="proj_work"
            )
            viewer = await resolve_agent_use_grant(
                session, agent_id="butler", user_email=VIEWER, project_id="proj_home"
            )
            outsider = await resolve_agent_use_grant(
                session, agent_id="butler", user_email=OUTSIDER, project_id="proj_home"
            )
            unassigned = await resolve_agent_use_grant(
                session, agent_id="private-bot", user_email=WIFE, project_id="proj_home"
            )
        assert in_project is not None
        assert in_project.executor_scope == "owner_executor"
        assert in_project.project_id == "proj_home"
        assert outside is None
        assert other_project is None
        assert viewer is None
        assert outsider is None
        assert unassigned is None

    _with_db(tmp_path, body)


def test_single_default_project_per_agent(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            await upsert_project_agent(
                session, project_id="proj_work", agent_id="butler", added_by=OWNER, is_default=True
            )
            await session.commit()
            default = await get_default_project_for_agent(session, "butler")
        assert default is not None and default.project_id == "proj_work"

    _with_db(tmp_path, body)


def test_shared_conversation_readability(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            shared = await get_conversation(session, "conv_shared")
            private = await get_conversation(session, "conv_private")
            assert await can_read_conversation(session, shared, WIFE)
            assert await can_read_conversation(session, shared, VIEWER)
            assert not await can_read_conversation(session, shared, OUTSIDER)
            assert not await can_read_conversation(session, private, WIFE)
            listed = await list_project_conversations(session, "proj_home", viewer_email=WIFE)
        assert {row.conversation_id for row in listed} == {"conv_shared", "conv_unassigned_agent"}

    _with_db(tmp_path, body)


def test_long_lived_chat_derives_agent_default_project(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            slack = await create_conversation(
                session, user_email=OWNER, agent_id="butler", context_type="slack", context_ref="C1"
            )
            topic = await create_conversation(
                session, user_email=OWNER, agent_id="butler", context_type="web"
            )
            await session.commit()
            assert (
                await resolve_effective_project_id(
                    session, conversation=slack, agent_id="butler", user_email=OWNER
                )
                == "proj_home"
            )
            # Topic chats only use their stored project.
            assert (
                await resolve_effective_project_id(
                    session, conversation=topic, agent_id="butler", user_email=OWNER
                )
                is None
            )
            # Non-members never derive another user's project.
            assert (
                await resolve_effective_project_id(
                    session, conversation=slack, agent_id="butler", user_email=OUTSIDER
                )
                is None
            )

    _with_db(tmp_path, body)


def test_managed_conversation_project_resolution(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            butler = await get_agent(session, "butler")
            wife_agent = await get_agent(session, "wife-agent")

            async def resolve(**kwargs: Any) -> str | None:
                return await managed_conversation_project_id(
                    session,
                    **{
                        "requested_project_id": None,
                        "controller_project_id": None,
                        "target_agent": butler,
                        "user_email": OWNER,
                        **kwargs,
                    },
                )

            # A controller without a project hands the work to the target's default project.
            assert await resolve() == "proj_home"
            # The controlling conversation's project wins over the target default.
            assert await resolve(controller_project_id="proj_work") == "proj_work"
            # An explicit project wins over both.
            assert (
                await resolve(requested_project_id="proj_work", controller_project_id="proj_home")
                == "proj_work"
            )
            with pytest.raises(ManagedProjectError):
                await resolve(requested_project_id="proj_missing")
            # The user must be able to use the explicit project.
            with pytest.raises(ManagedProjectError):
                await resolve(
                    requested_project_id="proj_work", target_agent=wife_agent, user_email=WIFE
                )
            # A default project the user cannot use is not applied.
            assert await resolve(user_email=OUTSIDER) is None

    _with_db(tmp_path, body)


def test_change_conversation_project_rules(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            wife_chat = await create_conversation(
                session,
                user_email=WIFE,
                agent_id="butler",
                context_type="web",
                project_id="proj_home",
            )
            own_chat = await create_conversation(
                session, user_email=WIFE, agent_id="wife-agent", context_type="web"
            )
            main_chat = await create_conversation(
                session,
                user_email=WIFE,
                agent_id="wife-agent",
                context_type="web",
                context_ref=f"web:user:{WIFE}:default",
            )
            await session.commit()

            # A project-only agent cannot leave its project.
            with pytest.raises(ProjectAssignmentError) as exc:
                await change_conversation_project(
                    session, wife_chat, project_id=None, user_email=WIFE
                )
            assert exc.value.code == "agent_not_available_in_project"
            # Not a member of Work.
            with pytest.raises(ProjectAssignmentError) as exc:
                await change_conversation_project(
                    session, own_chat, project_id="proj_work", user_email=WIFE
                )
            assert exc.value.status_code == 404
            # Long-lived chats refuse.
            with pytest.raises(ProjectAssignmentError) as exc:
                await change_conversation_project(
                    session, main_chat, project_id="proj_home", user_email=WIFE
                )
            assert exc.value.code == "project_not_assignable"
            # Only the author changes it.
            with pytest.raises(ProjectAssignmentError):
                await change_conversation_project(
                    session, own_chat, project_id="proj_home", user_email=OWNER
                )
            await change_conversation_project(
                session, own_chat, project_id="proj_home", user_email=WIFE
            )
            assert own_chat.project_id == "proj_home"
            await change_conversation_project(session, own_chat, project_id=None, user_email=WIFE)
            assert own_chat.project_id is None

    _with_db(tmp_path, body)


def test_task_visibility_follows_source_conversation(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            task = await create_task(
                session,
                created_by=OWNER,
                agent_id="butler",
                title="Order tiles",
                project_id="proj_home",
            )
            plain = await create_task(
                session, created_by=OWNER, agent_id="butler", title="Personal"
            )
        assert task.visibility == "project"
        assert plain.visibility == "private"

    _with_db(tmp_path, body)


def test_project_tools_enforce_roles(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        handlers = build_project_tool_handlers(factory)

        def ctx(email: str, conversation_id: str | None = None) -> ToolExecutionContext:
            metadata: dict[str, Any] = {"user_email": email}
            if conversation_id:
                metadata["conversation_id"] = conversation_id
            return ToolExecutionContext(executor_handle=None, runtime_metadata=metadata)  # type: ignore[arg-type]

        updated = await handlers["update_project"](
            {"project_id": "proj_home", "instructions": "Boiler service in March."},
            ctx(OWNER),
        )
        assert updated["instructions"] == "Boiler service in March."
        appended = await handlers["update_project"](
            {
                "project_id": "proj_home",
                "instructions": "Budget is fixed.",
                "instructions_mode": "append",
            },
            ctx(OWNER),
        )
        assert appended["instructions"] == "Boiler service in March.\n\nBudget is fixed."
        with pytest.raises(ValueError, match="does not allow"):
            await handlers["update_project"](
                {"project_id": "proj_home", "instructions": "x"}, ctx(WIFE)
            )
        with pytest.raises(ValueError, match="does not allow"):
            await handlers["delete_project"]({"project_id": "proj_home"}, ctx(WIFE))
        with pytest.raises(ValueError, match="Project not found"):
            await handlers["get_project"]({"project_id": "proj_home"}, ctx(OUTSIDER))
        detail = await handlers["get_project"]({"project_id": "proj_home"}, ctx(VIEWER))
        assert detail["caller_role"] == "viewer"
        assert [agent["agent_id"] for agent in detail["agents"]] == ["butler"]
        # Members cannot assign; owners only assign their own agents.
        with pytest.raises(ValueError, match="does not allow"):
            await handlers["assign_agent_to_project"](
                {"project_id": "proj_home", "agent_id": "wife-agent"}, ctx(WIFE)
            )
        assigned = await handlers["assign_agent_to_project"](
            {
                "project_id": "proj_home",
                "agent_id": "private-bot",
                "executor_scope": "grantee_executor",
            },
            ctx(OWNER),
        )
        assert assigned["executor_scope"] == "grantee_executor"
        # set_conversation_project works on the current topic conversation only.
        result = await handlers["set_conversation_project"](
            {"project_id": "none"}, ctx(OWNER, "conv_unassigned_agent")
        )
        assert result["project_id"] is None

    _with_db(tmp_path, body)


# --- REST integration ---


def test_rest_roles_sharing_and_shared_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(
                    session, password_hash=client.app.state.password_hasher.hash("password123")
                )
                await session.commit()

        client.portal.call(seed)
        app = client.app

        # Member (wife) can read the shared conversation, but not the private one.
        detail = client.get("/api/v1/conversations/conv_shared", headers=_headers(app, WIFE))
        assert detail.status_code == 200, detail.text
        assert detail.json()["is_readonly_for_caller"] is True
        assert (
            client.get(
                "/api/v1/conversations/conv_private", headers=_headers(app, WIFE)
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/conversations/conv_shared", headers=_headers(app, OUTSIDER)
            ).status_code
            == 403
        )
        # Admin has no bypass.
        assert (
            client.get(
                "/api/v1/conversations/conv_shared", headers=_headers(app, OUTSIDER, "admin")
            ).status_code
            == 403
        )
        # Readers cannot mutate the author's conversation.
        assert (
            client.patch(
                "/api/v1/conversations/conv_shared",
                headers=_headers(app, WIFE),
                json={"title": "hijack"},
            ).status_code
            == 403
        )
        send = client.put(
            "/api/v1/chat/v2/conversations/conv_shared/messages/txn-reader",
            headers=_headers(app, WIFE),
            json={"client_message_id": "cmsg-reader", "content": "hi"},
        )
        assert send.status_code in {403, 404}

        # Project listings.
        conversations = client.get(
            "/api/v1/projects/proj_home/conversations", headers=_headers(app, WIFE)
        )
        assert conversations.status_code == 200
        assert {row["conversation_id"] for row in conversations.json()} == {
            "conv_shared",
            "conv_unassigned_agent",
        }
        assert (
            client.get(
                "/api/v1/projects/proj_home/conversations", headers=_headers(app, OUTSIDER)
            ).status_code
            == 403
        )
        activity = client.get("/api/v1/projects/proj_home/activity", headers=_headers(app, VIEWER))
        assert activity.status_code == 200
        assert set(activity.json()) == {"deliverables", "artifacts"}

        # Member creates a chat with the project agent inside the project only.
        created = client.post(
            "/api/v1/conversations",
            headers=_headers(app, WIFE),
            json={"agent_id": "butler", "project_id": "proj_home", "context": {"type": "web"}},
        )
        assert created.status_code == 200, created.text
        assert created.json()["visibility"] == "project"
        outside = client.post(
            "/api/v1/conversations",
            headers=_headers(app, OUTSIDER),
            json={"agent_id": "butler", "context": {"type": "web"}},
        )
        assert outside.status_code == 403
        # Default project applies when no project is given (butler defaults to Home).
        defaulted = client.post(
            "/api/v1/conversations",
            headers=_headers(app, WIFE),
            json={"agent_id": "butler", "context": {"type": "web"}},
        )
        assert defaulted.status_code == 200, defaulted.text
        assert defaulted.json()["project_id"] == "proj_home"
        # Viewers cannot start project work.
        viewer_create = client.post(
            "/api/v1/conversations",
            headers=_headers(app, VIEWER),
            json={"agent_id": "butler", "project_id": "proj_home", "context": {"type": "web"}},
        )
        assert viewer_create.status_code == 403

        # Clearing and refusals through PATCH.
        own = client.post(
            "/api/v1/conversations",
            headers=_headers(app, OWNER),
            json={"agent_id": "private-bot", "context": {"type": "web"}},
        ).json()
        assert own["project_id"] is None
        moved = client.patch(
            f"/api/v1/conversations/{own['conversation_id']}",
            headers=_headers(app, OWNER),
            json={"project_id": "proj_work"},
        )
        assert moved.status_code == 200 and moved.json()["project_id"] == "proj_work"
        cleared = client.patch(
            f"/api/v1/conversations/{own['conversation_id']}",
            headers=_headers(app, OWNER),
            json={"project_id": None, "visibility": "private"},
        )
        assert cleared.status_code == 200
        assert cleared.json()["project_id"] is None
        assert cleared.json()["visibility"] == "private"

        # Roles on project routes.
        assert (
            client.patch(
                "/api/v1/projects/proj_home",
                headers=_headers(app, WIFE),
                json={"instructions": "x"},
            ).status_code
            == 403
        )
        grants = client.get("/api/v1/projects/proj_home/grants", headers=_headers(app, WIFE))
        assert grants.status_code == 200
        wife_grant = next(row for row in grants.json() if row["grantee_user_email"] == WIFE)
        assert wife_grant["permission"] == "member"
        assert (
            client.patch(
                f"/api/v1/projects/proj_home/grants/{wife_grant['grant_id']}",
                headers=_headers(app, WIFE),
                json={"permission": "manager"},
            ).status_code
            == 403
        )
        promoted = client.patch(
            f"/api/v1/projects/proj_home/grants/{wife_grant['grant_id']}",
            headers=_headers(app, OWNER),
            json={"permission": "manager"},
        )
        assert promoted.status_code == 200 and promoted.json()["permission"] == "manager"
        # A manager edits instructions and assigns only her own agent.
        assert (
            client.patch(
                "/api/v1/projects/proj_home",
                headers=_headers(app, WIFE),
                json={"instructions": "Quiet hours after 22:00.", "instructions_mode": "append"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/v1/projects/proj_home/agents/private-bot",
                headers=_headers(app, WIFE),
                json={},
            ).status_code
            == 403
        )
        assigned = client.put(
            "/api/v1/projects/proj_home/agents/wife-agent",
            headers=_headers(app, WIFE),
            json={"executor_scope": "grantee_executor"},
        )
        assert assigned.status_code == 200, assigned.text
        # Managers still cannot share or archive.
        assert (
            client.post(
                "/api/v1/projects/proj_home/grants",
                headers=_headers(app, WIFE),
                json={"grantee_user_email": OUTSIDER},
            ).status_code
            == 403
        )
        assert (
            client.delete("/api/v1/projects/proj_home", headers=_headers(app, WIFE)).status_code
            == 403
        )
        # Agent listing marks project-only agents.
        listed = client.get("/api/v1/agents?limit=100", headers=_headers(app, OWNER)).json()
        wife_agent = next(item for item in listed["items"] if item["agent_id"] == "wife-agent")
        assert wife_agent["shared_via_project_ids"] == ["proj_home"]

        # Leaving with make_private hides the leaver's project work.
        left = client.post(
            "/api/v1/projects/proj_home/leave",
            headers=_headers(app, WIFE),
            json={"make_private": True},
        )
        assert left.status_code == 200 and left.json()["ok"] is True
        assert (
            client.get(
                f"/api/v1/conversations/{created.json()['conversation_id']}",
                headers=_headers(app, OWNER),
            ).status_code
            == 403
        )
        assert (
            client.get("/api/v1/projects/proj_home", headers=_headers(app, WIFE)).status_code == 403
        )


# --- Review regressions ---


def test_update_project_tool_ignores_status_and_unknown_fields(tmp_path: Path) -> None:
    async def body(factory: Any) -> None:
        async with factory() as session:
            await create_project_grant(
                session,
                project_id="proj_work",
                granted_by=OWNER,
                grantee_user_email=WIFE,
                permission="manager",
            )
            await session.commit()
        handlers = build_project_tool_handlers(factory)
        ctx = ToolExecutionContext(
            executor_handle=None,  # type: ignore[arg-type]
            runtime_metadata={"user_email": WIFE},
        )
        result = await handlers["update_project"](
            {"project_id": "proj_work", "status": "archived", "owner_email": WIFE, "name": "W"},
            ctx,
        )
        assert result["status"] == "active"
        assert result["owner_email"] == OWNER
        assert result["name"] == "W"

    _with_db(tmp_path, body)


def test_project_metadata_applies_only_to_current_project() -> None:
    from cognis.core.context import _project_metadata_applies

    entry_a = SimpleNamespace(project_id="proj_a", source_id=None)
    path_entry = SimpleNamespace(project_id="proj_x", source_id="psrc_1")
    topic = SimpleNamespace(
        project_id="proj_b", context=ConversationContext(type="web"), effective_project_id=None
    )
    assert not _project_metadata_applies(entry_a, topic)
    assert _project_metadata_applies(path_entry, topic)
    slack = SimpleNamespace(
        project_id=None,
        context=ConversationContext(type="slack", ref="C1"),
        effective_project_id="proj_b",
    )
    assert not _project_metadata_applies(entry_a, slack)
    slack.effective_project_id = "proj_a"
    assert _project_metadata_applies(entry_a, slack)
    slack.effective_project_id = None
    assert not _project_metadata_applies(entry_a, slack)


def test_revoked_project_membership_fails_executor_resolution(tmp_path: Path) -> None:
    from cognis.api.runtime_support import _resolve_eligible_executor_config
    from cognis.core.executor_policy import ExecutorPolicy
    from cognis.models.agent import AgentDefinition
    from cognis.store.project_queries import revoke_active_project_grants_for_user

    async def body(factory: Any) -> None:
        async with factory() as session:
            await revoke_active_project_grants_for_user(session, "proj_home", WIFE)
            await session.commit()
        agent = AgentDefinition(
            agent_id="butler", owner_email=OWNER, name="Butler", status="active"
        )
        providers = SimpleNamespace(_session_factory=factory)
        with pytest.raises(RuntimeError, match="not accessible"):
            await _resolve_eligible_executor_config(
                providers,
                agent,
                WIFE,
                ExecutorPolicy(),
                conversation_id="conv_shared",
            )

    _with_db(tmp_path, body)


def test_shared_reader_snapshot_strips_owner_runtime_and_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:

        async def seed() -> None:
            async with client.app.state.session_factory() as session:
                await _seed_home(session)
                await session.commit()

        client.portal.call(seed)
        app = client.app
        scheduler = app.state.turn_scheduler

        async def fake_queue(conversation_id: str) -> list[dict[str, Any]]:
            if conversation_id != "conv_shared":
                return []
            return [{"queue_id": "q1", "content": "author private draft", "position": 0}]

        monkeypatch.setattr(scheduler, "get_queued_messages", fake_queue)
        # Cold build, then a second (possibly cached) read, must both stay stripped.
        for _ in range(2):
            snapshot = client.get(
                "/api/v1/chat/v2/conversations/conv_shared/snapshot", headers=_headers(app, WIFE)
            )
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["queue"]["messages"] == []
            assert "author private draft" not in snapshot.text
            sync = client.get(
                "/api/v1/chat/v2/conversations/conv_shared/sync",
                params={"cursor": snapshot.json()["cursor"]},
                headers=_headers(app, WIFE),
            )
            assert sync.status_code == 200, sync.text
            assert "author private draft" not in sync.text
            assert not any(op.get("op", "").startswith("queue") for op in sync.json()["ops"])
        author = client.get(
            "/api/v1/chat/v2/conversations/conv_shared/snapshot", headers=_headers(app, OWNER)
        )
        assert author.status_code == 200
        assert [item["content"] for item in author.json()["queue"]["messages"]] == [
            "author private draft"
        ]
        assert (
            client.get(
                "/api/v1/chat/v2/conversations/conv_shared/snapshot",
                headers=_headers(app, OUTSIDER),
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/chat/v2/conversations/conv_shared/work", headers=_headers(app, WIFE)
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/chat/v2/conversations/conv_shared/work", headers=_headers(app, OWNER)
            ).status_code
            == 200
        )

        async def seed_artifacts() -> None:
            async with client.app.state.session_factory() as session:
                for artifact_id, conversation_id in (
                    ("art_shared", "conv_shared"),
                    ("art_private", "conv_private"),
                ):
                    await create_artifact_record(
                        session,
                        artifact_id=artifact_id,
                        namespace="artifacts",
                        object_id=artifact_id,
                        filename=f"{artifact_id}.txt",
                        owner_email=OWNER,
                        purpose="tool_artifact",
                        kind="file",
                        mime_type="text/plain",
                        size_bytes=3,
                        status="persistent",
                        conversation_id=conversation_id,
                    )
                await session.commit()

        client.portal.call(seed_artifacts)

        def signed(artifact_id: str, email: str, role: str = "user") -> int:
            return client.get(
                f"/api/v1/artifacts/{artifact_id}/signed-url",
                headers=_headers(app, email, role),
            ).status_code

        assert signed("art_shared", WIFE) == 200
        assert signed("art_private", WIFE) == 404
        assert signed("art_shared", OUTSIDER, "admin") == 404
        assert signed("art_private", OWNER) == 200
        activity = client.get(
            "/api/v1/projects/proj_home/activity", headers=_headers(app, WIFE)
        ).json()
        assert [item["artifact_id"] for item in activity["artifacts"]] == ["art_shared"]


def test_snapshot_rebuild_keeps_shared_reader_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    from cognis.api.chat_v2 import snapshot_coordinator as coordinator

    calls: list[bool] = []

    async def fake_load(app: Any, **kwargs: Any) -> Any:
        calls.append(bool(kwargs.get("shared_reader")))
        return context

    async def fake_build(ctx: Any) -> Any:
        return SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))

    async def fake_hydrate(app: Any, snapshot: Any, **kwargs: Any) -> Any:
        return snapshot

    context = SimpleNamespace(
        session_refs=[],
        scope=SimpleNamespace(key="conversation:conv_shared"),
        conversation=None,
        queue=coordinator.queue_state_from_messages(
            [{"queue_id": "q1", "content": "secret", "position": 0}]
        ),
        state=coordinator.state_view_from_snapshot(None),
        runtime_input=coordinator.RuntimeOverlayInput(runtime_epoch="e", runtime_revision=1),
        owner_email=OWNER,
        conversation_id="conv_shared",
        shared_reader=True,
    )
    monkeypatch.setattr(coordinator, "load_conversation_snapshot_context", fake_load)
    monkeypatch.setattr(coordinator, "_build_immutable_snapshot", fake_build)
    monkeypatch.setattr(coordinator, "_hydrate_snapshot_attachments", fake_hydrate)
    monkeypatch.setattr(
        coordinator, "invalidate_chat_v2_snapshot_projection", lambda *_args, **_kw: None
    )
    app = SimpleNamespace(state=SimpleNamespace())
    result = asyncio.run(coordinator.rebuild_chat_snapshot_coordinated(app, context))  # type: ignore[arg-type]
    assert calls == [True]
    assert result.queue.messages == []
