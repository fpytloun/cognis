from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.runtime_support import select_static_tools
from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
)
from cognis.models.agent import AgentDefinition
from cognis.models.tool import stable_tool_id
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.queries import (
    create_agent,
    create_agent_grant,
    create_conversation,
    create_executor,
    create_knowledgebase,
    create_llm_provider,
    create_schedule,
    create_user,
    create_workflow,
    update_schedule,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL, handle_agent_management_tool
from cognis.tools.builtin.schedule import MANAGE_SCHEDULES_TOOL, handle_schedule_tool
from cognis.tools.introspection import (
    audit_native_tool_domains,
    validate_available_tool_call_with_context,
)
from cognis.tools.native_validation import NativeValidationContext


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _agent_management_test_deps(tmp_path: Path) -> AgentManagementDependencies:
    from cognis.api.runtime_support import static_tool_definitions

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/agent-management.db")
    await run_schema_bootstrap(engine)
    return AgentManagementDependencies(
        session_factory=create_session_factory(engine),
        assignable_tools=static_tool_definitions(knowledgebase_enabled=True),
    )


def test_grantee_can_list_and_view_shared_agent(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        list_response = client.get(
            "/api/v1/agents",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        detail_response = client.get(
            "/api/v1/agents/shared-agent",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )

        assert list_response.status_code == 200
        items = list_response.json()["items"]
        assert any(
            item["agent_id"] == "shared-agent" and item["is_shared_with_me"] for item in items
        )

        assert detail_response.status_code == 200
        payload = detail_response.json()
        assert payload["shared_by_email"] == "owner@example.com"
        assert payload["executor_scope"] == "owner_executor"
        assert payload["is_readonly_for_caller"] is True


def test_grantee_can_load_shared_agent_avatar(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            image_id = "img_shared_avatar"
            await client.app.state.artifact_store.async_save(
                "avatars",
                image_id,
                "image",
                b"avatar-bytes",
                "image/png",
                owner_email="owner@example.com",
            )
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_user(
                    session, email="other@example.com", name="Other", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    avatar_image_id=image_id,
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        list_response = client.get(
            "/api/v1/agents",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        assert list_response.status_code == 200
        shared = next(
            item for item in list_response.json()["items"] if item["agent_id"] == "shared-agent"
        )
        assert shared["avatar_url"] == "/api/v1/images/img_shared_avatar"

        image_response = client.get(
            "/api/v1/images/img_shared_avatar",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )
        assert image_response.status_code == 200
        assert image_response.content == b"avatar-bytes"

        other_response = client.get(
            "/api/v1/images/img_shared_avatar",
            headers=_auth_headers(client.app, email="other@example.com"),
        )
        assert other_response.status_code == 404


def test_grantee_can_update_own_executor_override(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="grantee_executor",
                    granted_by="owner@example.com",
                )
                await create_executor(
                    session,
                    executor_id="guest_exec",
                    name="Guest Exec",
                    executor_type="websocket",
                    owner_email="guest@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        response = client.patch(
            "/api/v1/agents/shared-agent/my-share",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"execution": {"executor_id": "guest_exec"}},
        )

        assert response.status_code == 200
        assert response.json()["grantee_overrides"] == {"execution": {"executor_id": "guest_exec"}}

        owner_response = client.get(
            "/api/v1/agents/shared-agent/shares",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        assert owner_response.status_code == 200
        assert owner_response.json()[0]["grantee_overrides"] is None


def test_owner_executor_share_rejects_grantee_executor_override(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        response = client.patch(
            "/api/v1/agents/shared-agent/my-share",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"execution": {"executor_id": "guest_exec"}},
        )

        assert response.status_code == 400


def test_admin_has_no_bypass_for_shared_agent_access(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=password_hash,
                    role="admin",
                )
                await create_agent(
                    session,
                    agent_id="private-agent",
                    owner_email="owner@example.com",
                    name="Private Agent",
                    display_name="Private Agent",
                    status="active",
                )
                await session.commit()

        client.portal.call(_seed)

        response = client.get(
            "/api/v1/agents/private-agent",
            headers=_auth_headers(client.app, email="admin@example.com", role="admin"),
        )

        assert response.status_code == 403


def test_grantee_cannot_mutate_shared_agent_bindings(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        response = client.put(
            "/api/v1/agents/shared-agent/bindings",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json=[],
        )

        assert response.status_code == 403


def test_grantee_tool_listing_hides_owner_agent_management_opt_in(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    tools={"opt_in_builtin_tools": ["manage_agents"]},
                    status="active",
                )
                await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()

        client.portal.call(_seed)

        response = client.get(
            "/api/v1/agents/shared-agent/tools",
            headers=_auth_headers(client.app, email="guest@example.com"),
        )

        assert response.status_code == 200
        assert all(item["name"] != "manage_agents" for item in response.json())


def test_revoked_share_blocks_future_messages(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                grant = await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await create_conversation(
                    session,
                    user_email="guest@example.com",
                    agent_id="shared-agent",
                    context_type="direct",
                    conversation_id="conv-shared",
                )
                await session.commit()
                client.app.state._test_grant_id = grant.grant_id

        client.portal.call(_seed)

        revoke_response = client.delete(
            f"/api/v1/agents/shared-agent/shares/{client.app.state._test_grant_id}",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        message_response = client.put(
            "/api/v1/chat/v2/conversations/conv-shared/messages/txn-revoked-share",
            headers=_auth_headers(client.app, email="guest@example.com"),
            json={"client_message_id": "cmsg-revoked-share", "content": "hello"},
        )

        assert revoke_response.status_code == 200
        assert message_response.status_code == 403


def test_revoked_share_can_be_granted_again(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _seed() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="shared-agent",
                    owner_email="owner@example.com",
                    name="Shared Agent",
                    display_name="Shared Agent",
                    status="active",
                )
                grant = await create_agent_grant(
                    session,
                    agent_id="shared-agent",
                    grantee_user_email="guest@example.com",
                    executor_scope="owner_executor",
                    granted_by="owner@example.com",
                )
                await session.commit()
                client.app.state._test_regrant_id = grant.grant_id

        client.portal.call(_seed)

        revoke_response = client.delete(
            f"/api/v1/agents/shared-agent/shares/{client.app.state._test_regrant_id}",
            headers=_auth_headers(client.app, email="owner@example.com"),
        )
        regrant_response = client.post(
            "/api/v1/agents/shared-agent/shares",
            headers=_auth_headers(client.app, email="owner@example.com"),
            json={"grantee_email": "guest@example.com", "executor_scope": "grantee_executor"},
        )

        assert revoke_response.status_code == 200
        assert regrant_response.status_code == 201
        assert regrant_response.json()["executor_scope"] == "grantee_executor"


def test_agent_management_tool_denies_shared_grantee_runtime() -> None:
    result = asyncio.run(
        handle_agent_management_tool(
            tool_name="manage_agents",
            arguments={"action": "list"},
            deps=AgentManagementDependencies(session_factory=lambda: None),
            user_email="guest@example.com",
            current_agent_id="shared-agent",
            runtime_access=RuntimeAccessContext(
                user_email="guest@example.com",
                agent_id="shared-agent",
                agent_owner_email="owner@example.com",
            ),
        )
    )

    assert result.is_error is True
    assert result.metadata == {"code": "agent_management_context_denied"}


def test_agent_management_service_can_create_and_revoke_share(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:

        async def _run() -> None:
            async with client.app.state.session_factory() as session:
                password_hash = client.app.state.password_hasher.hash("password123")
                await create_user(
                    session, email="owner@example.com", name="Owner", password_hash=password_hash
                )
                await create_user(
                    session, email="guest@example.com", name="Guest", password_hash=password_hash
                )
                await create_agent(
                    session,
                    agent_id="managed-agent",
                    owner_email="owner@example.com",
                    name="Managed Agent",
                    display_name="Managed Agent",
                    status="active",
                )
                await session.commit()

            deps = AgentManagementDependencies(session_factory=client.app.state.session_factory)
            created = await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id="controller-agent",
                arguments={
                    "action": "share_create",
                    "agent_id": "managed-agent",
                    "grantee_email": "guest@example.com",
                    "executor_scope": "owner_executor",
                },
            )
            grant_id = created["share"]["grant_id"]
            revoked = await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id="controller-agent",
                arguments={
                    "action": "share_revoke",
                    "agent_id": "managed-agent",
                    "grant_id": grant_id,
                },
            )

            assert created["status"] == "shared"
            assert revoked == {"status": "revoked", "ok": True, "grant_id": grant_id}

        client.portal.call(_run)


def test_agent_management_settings_get_schema_and_update(
    monkeypatch: object, tmp_path: Path
) -> None:
    del monkeypatch

    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            await create_workflow(
                session,
                workflow_id="software-development",
                owner_email="owner@example.com",
                name="Software Development",
                definition={"steps": []},
            )
            await session.commit()

        initial = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={"action": "settings_get", "agent_id": "managed-agent"},
        )
        updated = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "settings_update",
                "agent_id": "managed-agent",
                "settings": {
                    "available_workflow_ids": ["software-development"],
                    "default_workflow_id": "software-development",
                    "workflow_selection_mode": "use_default",
                },
            },
        )
        reread = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={"action": "settings_get", "agent_id": "managed-agent"},
        )
        assert initial["settings"]["tools"] is None
        assert initial["settings"]["tools_state"]["config_state"] == "default_inherited"
        settings = updated["settings"]
        assert settings["workflow"]["default_workflow_id"] == "software-development"
        assert settings["workflow"]["workflow_selection_mode"] == "use_default"
        assert settings["executor"]["executor_id"] is None
        assert settings["enabled_skills"] == []
        assert settings["tools_state"]["config_state"] == "default_inherited"
        assert reread["settings"] == updated["settings"]

    asyncio.run(_run())


def test_agent_management_list_includes_available_profiles(tmp_path: Path) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                agent_profiles={
                    "quality": {
                        "profile_id": "quality",
                        "description": "Maximum implementation quality.",
                    },
                    "fast": {
                        "profile_id": "fast",
                        "description": "Low-latency routine work.",
                    },
                },
                default_agent_profile_id="quality",
                status="active",
            )
            await session.commit()

        result = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={"action": "list"},
        )

        assert result["agents"] == [
            {
                "agent_id": "managed-agent",
                "name": "Managed Agent",
                "description": None,
                "agent_type": "primary",
                "status": "active",
                "manageable": True,
                "default_agent_profile_id": "quality",
                "agent_profiles": [
                    {
                        "profile_id": "fast",
                        "description": "Low-latency routine work.",
                        "is_default": False,
                        "synthetic": False,
                    },
                    {
                        "profile_id": "quality",
                        "description": "Maximum implementation quality.",
                        "is_default": True,
                        "synthetic": False,
                    },
                ],
            }
        ]

    asyncio.run(_run())


def test_agent_management_settings_update_rejects_invalid_values(
    monkeypatch: object, tmp_path: Path
) -> None:
    del monkeypatch

    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            await session.commit()

        try:
            await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id="controller-agent",
                arguments={
                    "action": "settings_update",
                    "agent_id": "managed-agent",
                    "settings": {"default_workflow_id": "missing-workflow"},
                },
            )
        except ValueError as exc:
            assert "Invalid default_workflow_id: missing-workflow" in str(exc)
        else:
            raise AssertionError("Expected settings_update to reject an invalid workflow ID")

    asyncio.run(_run())


def test_agent_management_tool_assignment_crud_and_validation(tmp_path: Path) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            kb = await create_knowledgebase(
                session,
                owner_email="owner@example.com",
                name="Docs",
            )
            await session.commit()

        invalid = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "tools_set",
                "agent_id": "managed-agent",
                "tool_groups": ["missing"],
            },
        )
        assert invalid["status"] == "invalid"
        assert invalid["errors"][0]["reason"] == "Unknown tool group"

        updated = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "tools_set",
                "agent_id": "managed-agent",
                "tool_groups": ["knowledgebase_read"],
                "deny_tools": ["builtin:knowledgebase_status"],
            },
        )
        assert updated["tools"]["configured"]["tool_groups"] == ["knowledgebase_read"]
        assert "builtin:knowledgebase_search" in updated["tools"]["effective_tools"]
        assert "builtin:knowledgebase_status" not in updated["tools"]["effective_tools"]
        assert updated["tools"]["validation"]["warnings"][0]["field"] == "allowed_knowledgebases"

        kb_updated = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "knowledgebases_set",
                "agent_id": "managed-agent",
                "knowledgebase_ids": [kb.knowledgebase_id],
            },
        )
        assert kb_updated["assigned_knowledgebases"] == [kb.knowledgebase_id]

        reread = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={"action": "knowledgebases_get", "agent_id": "managed-agent"},
        )
        assert reread["assigned_knowledgebases"] == [kb.knowledgebase_id]

        created_with_kb = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "create",
                "name": "Knowledge agent",
                "assigned_knowledgebases": [kb.knowledgebase_id],
            },
        )
        assert created_with_kb["agent"]["permissions"]["allowed_knowledgebases"] == [
            kb.knowledgebase_id
        ]
        created_kb_state = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "knowledgebases_get",
                "agent_id": created_with_kb["agent"]["agent_id"],
            },
        )
        assert created_kb_state["assigned_knowledgebases"] == [kb.knowledgebase_id]

    asyncio.run(_run())


def test_restricted_primary_cannot_expand_tool_assignment(tmp_path: Path) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        deps.assignable_tools = [
            tool
            for tool in deps.assignable_tools or []
            if tool.name in {"list_agents", "manage_agents"}
        ]
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            allowed_kb = await create_knowledgebase(
                session,
                owner_email="owner@example.com",
                name="Allowed KB",
            )
            denied_kb = await create_knowledgebase(
                session,
                owner_email="owner@example.com",
                name="Denied KB",
            )
            await session.commit()
        deps.assignable_knowledgebase_ids = {allowed_kb.knowledgebase_id}

        direct = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "tools_set",
                "agent_id": "managed-agent",
                "allow_tools": ["builtin:write_deliverable"],
            },
        )
        grouped = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={
                "action": "tools_set",
                "agent_id": "managed-agent",
                "tool_groups": ["knowledgebase_read"],
            },
        )

        assert direct["status"] == "invalid"
        assert direct["errors"][0]["reason"] == "Unknown tool"
        assert grouped["status"] == "invalid"
        assert "exceeds caller-effective" in grouped["errors"][0]["reason"]

        created = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            arguments={"action": "create", "name": "Restricted child"},
        )
        assert set(created["agent"]["tools"]["allow_tools"]) == {
            stable_tool_id(tool) for tool in deps.assignable_tools or []
        }
        assert created["agent"]["permissions"] is None
        assert created["agent"]["execution"] is None
        created_definition = AgentDefinition.model_validate(created["agent"])
        assert {
            stable_tool_id(tool)
            for tool in select_static_tools(
                created_definition,
                knowledgebase_enabled=True,
            )
        } <= {stable_tool_id(tool) for tool in deps.assignable_tools or []}

        try:
            await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id="controller-agent",
                arguments={
                    "action": "knowledgebases_add",
                    "agent_id": "managed-agent",
                    "knowledgebase_ids": [denied_kb.knowledgebase_id],
                },
            )
        except AgentManagementError as exc:
            assert "Invalid knowledgebase_ids" in str(exc)
        else:
            raise AssertionError("caller-denied knowledgebase assignment should fail")

    asyncio.run(_run())


def test_settings_preflight_validates_provider_model_and_workflow_domains(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            workflow = await create_workflow(
                session,
                workflow_id="allowed-workflow",
                owner_email="owner@example.com",
                name="Allowed workflow",
                definition={"steps": []},
            )
            await create_llm_provider(
                session,
                provider_id="provider-a",
                display_name="Provider A",
                location="local",
                backend="litellm",
                owner_email="owner@example.com",
                config={"models": [{"model_id": "model-a"}]},
            )
            await session.commit()

        context = NativeValidationContext(
            actor_email="owner@example.com",
            current_agent_id="controller-agent",
            agent_management_deps=deps,
        )
        valid = await validate_available_tool_call_with_context(
            [MANAGE_AGENTS_TOOL],
            "manage_agents",
            {
                "action": "settings_update",
                "agent_id": "managed-agent",
                "settings": {
                    "provider_id": "provider-a",
                    "model": "model-a",
                    "default_workflow_id": workflow.workflow_id,
                },
            },
            context,
        )
        invalid_model = await validate_available_tool_call_with_context(
            [MANAGE_AGENTS_TOOL],
            "manage_agents",
            {
                "action": "settings_update",
                "agent_id": "managed-agent",
                "settings": {
                    "provider_id": "provider-a",
                    "model": "model-b",
                },
            },
            context,
        )
        invalid_workflow = await validate_available_tool_call_with_context(
            [MANAGE_AGENTS_TOOL],
            "manage_agents",
            {
                "action": "settings_update",
                "agent_id": "managed-agent",
                "settings": {"default_workflow_id": "missing-workflow"},
            },
            context,
        )

        assert valid["valid"] is True
        assert invalid_model["valid"] is False
        assert "Invalid model" in invalid_model["errors"][0]["message"]
        assert invalid_workflow["valid"] is False
        assert "Invalid default_workflow_id" in invalid_workflow["errors"][0]["message"]

    asyncio.run(_run())


def test_complex_native_examples_pass_handler_domain_validation(tmp_path: Path) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session, email="owner@example.com", name="Owner", password_hash="hashed"
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                display_name="Managed Agent",
                status="active",
            )
            await create_schedule(
                session,
                schedule_id="schedule-id",
                name="Existing schedule",
                schedule_type="cron",
                cron_expr="0 8 * * *",
                agent_id="managed-agent",
                task_template={"input": "Run"},
                created_by="owner@example.com",
            )
            await session.commit()

        failures = await audit_native_tool_domains(
            [MANAGE_AGENTS_TOOL],
            NativeValidationContext(
                actor_email="owner@example.com",
                current_agent_id="controller-agent",
                agent_management_deps=deps,
                session_factory=deps.session_factory,
            ),
        )
        failures.extend(
            await audit_native_tool_domains(
                [MANAGE_SCHEDULES_TOOL],
                NativeValidationContext(
                    actor_email="owner@example.com",
                    current_agent_id="managed-agent",
                    session_factory=deps.session_factory,
                ),
            )
        )

        assert failures == []
        assert MANAGE_AGENTS_TOOL.native_operations is not None
        settings_example = next(
            operation.examples[0]
            for operation in MANAGE_AGENTS_TOOL.native_operations
            if operation.operation == "settings_update"
        )
        settings_result = await handle_agent_management_tool(
            tool_name="manage_agents",
            arguments=settings_example,
            deps=deps,
            user_email="owner@example.com",
            current_agent_id="controller-agent",
            runtime_access=RuntimeAccessContext(
                user_email="owner@example.com",
                agent_id="controller-agent",
                agent_owner_email="owner@example.com",
                agent_type="primary",
            ),
        )
        assert settings_result.is_error is False

        assert MANAGE_SCHEDULES_TOOL.native_operations is not None
        schedule_example = next(
            operation.examples[0]
            for operation in MANAGE_SCHEDULES_TOOL.native_operations
            if operation.operation == "create"
        )
        schedule_result = await handle_schedule_tool(
            tool_name="manage_schedules",
            arguments=schedule_example,
            session_factory=deps.session_factory,
            scheduler=None,
            user_email="owner@example.com",
            agent_id="managed-agent",
        )
        assert schedule_result.is_error is False

    asyncio.run(_run())


def test_schedule_preflight_matches_handler_authorization_domains(tmp_path: Path) -> None:
    async def _run() -> None:
        deps = await _agent_management_test_deps(tmp_path)
        async with deps.session_factory() as session:
            await create_user(
                session,
                email="owner@example.com",
                name="Owner",
                password_hash="hashed",
            )
            await create_user(
                session,
                email="other@example.com",
                name="Other",
                password_hash="hashed",
            )
            await create_agent(
                session,
                agent_id="managed-agent",
                owner_email="owner@example.com",
                name="Managed Agent",
                status="active",
                agent_profiles={
                    "fast": {
                        "profile_id": "fast",
                        "description": "Fast responses",
                        "enabled": True,
                    }
                },
            )
            await create_workflow(
                session,
                workflow_id="allowed-workflow",
                owner_email="owner@example.com",
                name="Allowed workflow",
                definition={"steps": []},
            )
            await create_conversation(
                session,
                user_email="other@example.com",
                agent_id="managed-agent",
                context_type="direct",
                conversation_id="foreign-conversation",
            )
            await create_schedule(
                session,
                schedule_id="schedule-domain",
                name="Existing schedule",
                schedule_type="cron",
                cron_expr="0 8 * * *",
                agent_id="managed-agent",
                workflow_id="allowed-workflow",
                task_template={"delivery": {"mode": "preferred_channel"}},
                created_by="owner@example.com",
            )
            await update_schedule(
                session,
                "schedule-domain",
                workflow_id="stale-workflow",
            )
            await session.commit()

        context = NativeValidationContext(
            actor_email="owner@example.com",
            current_agent_id="managed-agent",
            session_factory=deps.session_factory,
        )
        accepted_update = {
            "action": "update",
            "schedule_id": "schedule-domain",
            "name": "Renamed despite stale workflow",
        }
        accepted_preflight = await validate_available_tool_call_with_context(
            [MANAGE_SCHEDULES_TOOL],
            "manage_schedules",
            accepted_update,
            context,
        )
        accepted_handler = await handle_schedule_tool(
            tool_name="manage_schedules",
            arguments=accepted_update,
            session_factory=deps.session_factory,
            scheduler=None,
            user_email="owner@example.com",
            agent_id="managed-agent",
        )
        assert accepted_preflight["valid"] is True
        assert accepted_handler.is_error is False

        invalid_fields = [
            {"agent_id": "missing-agent"},
            {"workflow_id": "missing-workflow"},
            {"agent_profile_id": "missing-profile"},
            {
                "delivery_mode": "specific_conversation",
                "delivery_target": "foreign-conversation",
            },
        ]
        for fields in invalid_fields:
            for action in ("create", "update"):
                arguments: dict[str, object] = {
                    "action": action,
                    **fields,
                }
                if action == "create":
                    arguments.update(
                        {
                            "name": "Invalid schedule",
                            "cron_expr": "0 8 * * *",
                            "agent_id": fields.get("agent_id", "managed-agent"),
                        }
                    )
                else:
                    arguments["schedule_id"] = "schedule-domain"

                preflight = await validate_available_tool_call_with_context(
                    [MANAGE_SCHEDULES_TOOL],
                    "manage_schedules",
                    arguments,
                    context,
                )
                handler_result = await handle_schedule_tool(
                    tool_name="manage_schedules",
                    arguments=arguments,
                    session_factory=deps.session_factory,
                    scheduler=None,
                    user_email="owner@example.com",
                    agent_id="managed-agent",
                )

                assert preflight["valid"] is False, (action, fields, preflight)
                assert handler_result.is_error is True, (action, fields, handler_result)

    asyncio.run(_run())
