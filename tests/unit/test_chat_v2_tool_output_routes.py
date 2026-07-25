from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_step_run,
    create_task,
    create_user,
    set_session_intaris_session_id,
    update_conversation_active_session,
)


class _GuardrailsEventProvider:
    events_by_session: dict[str, list[dict[str, Any]]] = {}

    def __init__(self) -> None:
        self.client = SimpleNamespace(aclose=self._aclose)

    async def _aclose(self) -> None:
        return None

    async def read_events(
        self,
        *,
        session_id: str,
        last_n: int | None = None,
        before_seq: int | None = None,
        limit: int = 500,
        **_kwargs: Any,
    ) -> Any:
        events = self.events_by_session.get(session_id, [])
        if before_seq is not None:
            events = [event for event in events if int(event["seq"]) < before_seq]
        page = events[-(last_n or limit) :]
        return SimpleNamespace(
            events=page,
            last_seq=int(events[-1]["seq"]) if events else 0,
            has_more=len(events) > len(page),
        )


def _headers(app: object, email: str) -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0], "user")  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_real_scoped_tool_output_routes_authorize_and_page_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")
    with TestClient(create_app()) as client:
        app = cast(Any, client.app)
        guardrails = _GuardrailsEventProvider()
        app.state.providers.guardrails = guardrails

        async def _seed() -> None:
            async with app.state.session_factory() as db:
                for email in ("owner@example.com", "other@example.com"):
                    await create_user(
                        db,
                        email=email,
                        name=email.split("@")[0],
                        password_hash=app.state.password_hasher.hash("password123"),
                        role="user",
                    )
                    await create_agent(
                        db,
                        agent_id=f"agent-{email.split('@')[0]}",
                        owner_email=email,
                        name="Agent",
                    )
                conversation = await create_conversation(
                    db,
                    conversation_id="conv-owned",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    context_type="web",
                )
                session = await create_session(
                    db,
                    conversation_id=conversation.conversation_id,
                    session_id="session-owned",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                )
                await set_session_intaris_session_id(db, session.session_id, "intaris-owned")
                await update_conversation_active_session(
                    db, conversation.conversation_id, session.session_id
                )
                await create_task(
                    db,
                    task_id="task-owned",
                    created_by="owner@example.com",
                    agent_id="agent-owner",
                    title="Owned task",
                    status="completed",
                )
                step = await create_step_run(
                    db,
                    step_run_id="step-owned",
                    task_id="task-owned",
                    step_name="review",
                    step_type="run",
                    agent_id="agent-owner",
                )
                step.conversation_id = conversation.conversation_id
                step.session_id = session.session_id
                step.intaris_session_id = "intaris-owned"
                second_conversation = await create_conversation(
                    db,
                    conversation_id="conv-second",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                    context_type="web",
                )
                second_session = await create_session(
                    db,
                    conversation_id=second_conversation.conversation_id,
                    session_id="session-second",
                    user_email="owner@example.com",
                    agent_id="agent-owner",
                )
                await set_session_intaris_session_id(
                    db, second_session.session_id, "intaris-second"
                )
                await update_conversation_active_session(
                    db, second_conversation.conversation_id, second_session.session_id
                )
                await create_task(
                    db,
                    task_id="task-second",
                    created_by="owner@example.com",
                    agent_id="agent-owner",
                    title="Second task",
                    status="completed",
                )
                second_step = await create_step_run(
                    db,
                    step_run_id="step-second",
                    task_id="task-second",
                    step_name="review",
                    step_type="run",
                    agent_id="agent-owner",
                )
                second_step.conversation_id = second_conversation.conversation_id
                second_step.session_id = second_session.session_id
                second_step.intaris_session_id = "intaris-second"
                other_conversation = await create_conversation(
                    db,
                    conversation_id="conv-other-owner",
                    user_email="other@example.com",
                    agent_id="agent-other",
                    context_type="web",
                )
                other_session = await create_session(
                    db,
                    conversation_id=other_conversation.conversation_id,
                    session_id="session-other-owner",
                    user_email="other@example.com",
                    agent_id="agent-other",
                )
                await set_session_intaris_session_id(
                    db, other_session.session_id, "intaris-other-owner"
                )
                await update_conversation_active_session(
                    db, other_conversation.conversation_id, other_session.session_id
                )
                await create_task(
                    db,
                    task_id="task-other-owner",
                    created_by="other@example.com",
                    agent_id="agent-other",
                    title="Other owner task",
                    status="completed",
                )
                other_step = await create_step_run(
                    db,
                    step_run_id="step-other-owner",
                    task_id="task-other-owner",
                    step_name="review",
                    step_type="run",
                    agent_id="agent-other",
                )
                other_step.conversation_id = other_conversation.conversation_id
                other_step.session_id = other_session.session_id
                other_step.intaris_session_id = "intaris-other-owner"
                await db.commit()

            for prefix, call_id in (
                ("tree-a", "call_saved"),
                ("tree-b", "call_second_saved"),
                ("tree-c", "call_other_saved"),
            ):
                await app.state.tool_output_store.save(
                    call_id,
                    "\n".join(f"{prefix}-line-{index}" for index in range(1, 1002)),
                )

        anyio.run(_seed)
        guardrails.events_by_session = {
            "intaris-owned": [
                {
                    "seq": 1,
                    "type": "tool_result",
                    "data": {
                        "call_id": "call_orig",
                        "recovery_call_id": "call_saved",
                        "result": "preview",
                        "has_full_output": True,
                    },
                }
            ],
            "intaris-second": [
                {
                    "seq": 1,
                    "type": "tool_result",
                    "data": {
                        "call_id": "call_second_orig",
                        "recovery_call_id": "call_second_saved",
                        "result": "second preview",
                        "has_full_output": True,
                    },
                }
            ],
            "intaris-other-owner": [
                {
                    "seq": 1,
                    "type": "tool_result",
                    "data": {
                        "call_id": "call_other_orig",
                        "recovery_call_id": "call_other_saved",
                        "result": "other preview",
                        "has_full_output": True,
                    },
                }
            ],
        }
        owner = _headers(app, "owner@example.com")
        other = _headers(app, "other@example.com")
        trees = [
            {
                "owner": owner,
                "other": other,
                "conversation": "conv-owned",
                "session": "session-owned",
                "step": "step-owned",
                "original": "call_orig",
                "recovery": "call_saved",
                "prefix": "tree-a",
            },
            {
                "owner": owner,
                "other": other,
                "conversation": "conv-second",
                "session": "session-second",
                "step": "step-second",
                "original": "call_second_orig",
                "recovery": "call_second_saved",
                "prefix": "tree-b",
            },
            {
                "owner": other,
                "other": owner,
                "conversation": "conv-other-owner",
                "session": "session-other-owner",
                "step": "step-other-owner",
                "original": "call_other_orig",
                "recovery": "call_other_saved",
                "prefix": "tree-c",
            },
        ]

        def scope_paths(tree: dict[str, Any], call_id: str) -> list[str]:
            return [
                f"/api/v1/chat/v2/conversations/{tree['conversation']}/tool-outputs/{call_id}",
                f"/api/v1/chat/v2/sessions/{tree['session']}/tool-outputs/{call_id}",
                f"/api/v1/chat/v2/task-steps/{tree['step']}/tool-outputs/{call_id}",
            ]

        def assert_exact_two_page_output(
            path: str,
            headers: dict[str, str],
            expected: str,
        ) -> None:
            first = client.get(f"{path}?offset=1&limit=1000", headers=headers)
            second = client.get(f"{path}?offset=1001&limit=1000", headers=headers)
            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["has_more_after"] is True
            assert first.json()["next_offset"] == 1001
            assert second.json()["has_more_after"] is False
            assert second.json()["next_offset"] is None
            assert f"{first.json()['content']}\n{second.json()['content']}" == expected

        for tree in trees:
            expected = "\n".join(
                f"{index}: {tree['prefix']}-line-{index}" for index in range(1, 1002)
            )
            for recovery_path, original_path in zip(
                scope_paths(tree, tree["recovery"]),
                scope_paths(tree, tree["original"]),
                strict=True,
            ):
                for path in (recovery_path, original_path):
                    assert_exact_two_page_output(path, tree["owner"], expected)
                    assert client.get(path, headers=tree["other"]).status_code == 403
                    assert (
                        client.get(f"{path}?limit=1001", headers=tree["owner"]).status_code == 422
                    )
                    assert (
                        client.get(
                            path.replace(
                                tree["recovery"] if path == recovery_path else tree["original"],
                                "call_unknown",
                            ),
                            headers=tree["owner"],
                        ).status_code
                        == 404
                    )

        for source in trees:
            for target in trees:
                if source is target:
                    continue
                for id_form in ("original", "recovery"):
                    for mismatched in scope_paths(target, source[id_form]):
                        expected_status = 403 if source["owner"] is not target["owner"] else 404
                        assert (
                            client.get(mismatched, headers=source["owner"]).status_code
                            == expected_status
                        )
        assert (
            client.get(
                "/api/v1/chat/v2/task-steps/missing-step/tool-outputs/call_saved",
                headers=owner,
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/chat/v2/sessions/missing-session/tool-outputs/call_saved",
                headers=owner,
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/chat/v2/conversations/missing-conversation/tool-outputs/call_saved",
                headers=owner,
            ).status_code
            == 404
        )
