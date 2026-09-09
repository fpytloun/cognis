"""State fixtures must create external streams before controller sessions."""

from unittest.mock import AsyncMock

import pytest

from scripts import seed_work_conformance as seed


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [True, False])
async def test_label_streams_exist_before_controller_rows(monkeypatch, fresh):
    provider = AsyncMock()
    monkeypatch.setattr(seed, "_ensure_conversation", AsyncMock(return_value=fresh))
    create = AsyncMock()
    monkeypatch.setattr(seed, "create_session", create)
    monkeypatch.setattr(seed, "update_conversation_active_session", AsyncMock())
    await seed._seed_label_conversations(AsyncMock(), provider, "owner@example.com")
    for state in seed.LABEL_STATES:
        sid = f"sess_work_conformance_label_{state}_v{seed.FIXTURE_VERSION}"
        provider.create_session.assert_any_await(
            sid,
            f"Work conformance {state} execution label.",
            seed.E2E_AGENT_ID,
            user_id="owner@example.com",
        )
        provider.get_session.assert_any_await(sid)
    assert create.await_count == (len(seed.LABEL_STATES) if fresh else 0)


@pytest.mark.asyncio
async def test_missing_stream_blocks_controller_session_creation(monkeypatch):
    provider = AsyncMock()
    provider.get_session.side_effect = RuntimeError("stream missing")
    monkeypatch.setattr(seed, "_ensure_conversation", AsyncMock(return_value=True))
    create = AsyncMock()
    monkeypatch.setattr(seed, "create_session", create)
    with pytest.raises(RuntimeError, match="stream missing"):
        await seed._seed_label_conversations(AsyncMock(), provider, "owner@example.com")
    create.assert_not_awaited()
