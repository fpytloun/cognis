"""Generated titles use the existing API and live sidebar metadata boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import update

from cognis.core.conversation_titles import ConversationTitleService
from cognis.store.models import Conversation
from tests.unit.test_conversations_routes import (
    _auth_headers,
    _create_test_client,
    _seed_user_and_agent,
)
from tests.unit.test_untitled_conversation_recovery import completion, page, user_event


def test_generated_title_is_visible_in_rest_suggestion_and_ws_sidebar(monkeypatch, tmp_path):
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        client.portal.call(_seed_user_and_agent, app)
        headers = _auth_headers(app, email="user@example.com")
        response = client.post(
            "/api/v1/conversations", headers=headers, json={"agent_id": "agent-chat"}
        )
        assert response.status_code == 200
        cid = response.json()["conversation_id"]
        sidebar = AsyncMock()
        monkeypatch.setattr(app.state.ws_manager, "send_sidebar_update_to_owner", sidebar)
        guardrails = SimpleNamespace(read_events=AsyncMock(return_value=page(user_event())))
        llm = SimpleNamespace(generate=AsyncMock(return_value=completion()))
        service = ConversationTitleService(
            app.state.session_factory, guardrails, llm, app.state.event_bus
        )

        async def generate():
            return await service.generate(
                conversation_id=cid,
                session_id=cid,
                intaris_session_id=cid,
                user_email="user@example.com",
                agent_id="agent-chat",
                agent_owner_email="user@example.com",
            )

        assert client.portal.call(generate)
        row = client.get(f"/api/v1/conversations/{cid}", headers=headers)
        assert row.status_code == 200
        assert row.json()["title"] == "Oprava přihlašování"
        assert row.json()["title_source"] == "cognis"
        suggestion = client.get(f"/api/v1/conversations/{cid}/title-suggestion", headers=headers)
        assert suggestion.status_code == 200
        assert suggestion.json()["title"] == "Oprava přihlašování"
        assert suggestion.json()["available"] is True
        assert sidebar.await_count == 1
        assert sidebar.call_args.args[0] == cid
        assert sidebar.call_args.args[1]["type"] == "sidebar_conversation_upsert"
        assert sidebar.call_args.kwargs["include_subscribers"] is True


def test_title_generation_usage_is_attributed_to_the_conversation_project(monkeypatch, tmp_path):
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        client.portal.call(_seed_user_and_agent, app)
        headers = _auth_headers(app, email="user@example.com")
        response = client.post(
            "/api/v1/conversations", headers=headers, json={"agent_id": "agent-chat"}
        )
        cid = response.json()["conversation_id"]

        async def assign_project():
            async with app.state.session_factory() as db:
                await db.execute(
                    update(Conversation)
                    .where(Conversation.conversation_id == cid)
                    .values(project_id="proj-usage")
                )
                await db.commit()

        client.portal.call(assign_project)
        monkeypatch.setattr(app.state.ws_manager, "send_sidebar_update_to_owner", AsyncMock())
        guardrails = SimpleNamespace(read_events=AsyncMock(return_value=page(user_event())))
        llm = SimpleNamespace(generate=AsyncMock(return_value=completion()))
        service = ConversationTitleService(
            app.state.session_factory, guardrails, llm, app.state.event_bus
        )

        async def generate():
            return await service.generate(
                conversation_id=cid,
                session_id=cid,
                intaris_session_id=cid,
                user_email="user@example.com",
                agent_id="agent-chat",
                agent_owner_email="user@example.com",
            )

        assert client.portal.call(generate)
        # Background title generation runs outside a turn; usage must still carry the project.
        assert llm.generate.await_args.kwargs["cognis_project_id"] == "proj-usage"
