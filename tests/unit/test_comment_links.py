from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cognis.core.chat_modes import is_plan_hidden_tool, is_plan_mutating_tool_call
from cognis.core.feedback.comment_links import publication_comment_link
from cognis.tools.builtin.artifact_tools import ARTIFACT_GET_URL_TOOL
from cognis.tools.builtin.workflow import WRITE_DELIVERABLE_TOOL
from cognis.tools.executor.document import ARTIFACT_PUBLISH_TOOL


def test_comment_link_tools_declare_mutation_and_fail_closed_authority() -> None:
    for tool in (ARTIFACT_GET_URL_TOOL, ARTIFACT_PUBLISH_TOOL):
        assert tool.parameters["properties"]["allow_comments"]["type"] == "boolean"
        assert tool.non_bypassable
        assert not tool.read_only
    assert (
        WRITE_DELIVERABLE_TOOL.provider_exposure_schema["properties"]["allow_comments"]["type"]
        == "boolean"
    )
    assert WRITE_DELIVERABLE_TOOL.non_bypassable
    assert not is_plan_hidden_tool(ARTIFACT_GET_URL_TOOL)
    assert not is_plan_mutating_tool_call(ARTIFACT_GET_URL_TOOL, {})
    assert not is_plan_mutating_tool_call(ARTIFACT_GET_URL_TOOL, {"allow_comments": False})
    assert is_plan_mutating_tool_call(ARTIFACT_GET_URL_TOOL, {"allow_comments": True})


@pytest.mark.asyncio
async def test_publication_without_comments_does_not_mint_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock()
    monkeypatch.setattr("cognis.core.feedback.comment_links.create_comment_link", create)
    result = await publication_comment_link(
        None,
        requested=False,
        content_ref=f"art_{uuid4().hex}",
        user_email=None,
        agent_id=None,
    )
    assert result == {}
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_response_loss_does_not_retry_or_invalidate_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(side_effect=ConnectionError("Response lost"))
    monkeypatch.setattr("cognis.core.feedback.comment_links.create_comment_link", create)
    result = await publication_comment_link(
        None,
        requested=True,
        content_ref=f"art_{uuid4().hex}",
        user_email="owner@feedback.test",
        agent_id=uuid4().hex,
    )
    create.assert_awaited_once()
    assert result["allow_comments"] is None
    assert result["comment_link_status"] == "unconfirmed"
    assert "Do not republish" in result["comment_link_error"]["message"]


@pytest.mark.asyncio
async def test_comment_link_cancellation_remains_owned_by_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("cognis.core.feedback.comment_links.create_comment_link", create)
    with pytest.raises(asyncio.CancelledError):
        await publication_comment_link(
            None,
            requested=True,
            content_ref=f"art_{uuid4().hex}",
            user_email="owner@feedback.test",
            agent_id=uuid4().hex,
        )
    create.assert_awaited_once()
