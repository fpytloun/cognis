"""Tests for escalation blocking in the agent loop."""

from __future__ import annotations

import asyncio

import pytest

from cognis.core.agent_loop import (
    PauseResolution,
    PauseWaiter,
    PendingPause,
)

# ---------------------------------------------------------------------------
# PauseWaiter escalation-specific tests
# ---------------------------------------------------------------------------


def test_pending_pause_has_conversation_id() -> None:
    pause = PendingPause(
        pause_id="escalation:call_123",
        pause_type="escalation",
        session_id="sess_1",
        conversation_id="conv_1",
        context={"call_id": "call_123", "tool_name": "dangerous_tool"},
    )
    assert pause.conversation_id == "conv_1"
    assert pause.pause_type == "escalation"


def test_find_pending_by_conversation_id() -> None:
    waiter = PauseWaiter()
    waiter.register(
        PendingPause(
            pause_id="escalation:call_1",
            pause_type="escalation",
            session_id="sess_1",
            conversation_id="conv_A",
        )
    )
    waiter.register(
        PendingPause(
            pause_id="escalation:call_2",
            pause_type="escalation",
            session_id="sess_2",
            conversation_id="conv_B",
        )
    )

    result = waiter.find_pending(pause_type="escalation", conversation_id="conv_A")
    assert result is not None
    assert result.pause_id == "escalation:call_1"

    result_b = waiter.find_pending(pause_type="escalation", conversation_id="conv_B")
    assert result_b is not None
    assert result_b.pause_id == "escalation:call_2"

    result_none = waiter.find_pending(pause_type="escalation", conversation_id="conv_C")
    assert result_none is None


def test_find_pending_by_conversation_id_skips_resolved() -> None:
    waiter = PauseWaiter()
    pause = PendingPause(
        pause_id="escalation:call_1",
        pause_type="escalation",
        conversation_id="conv_A",
    )
    waiter.register(pause)
    waiter.resolve("escalation:call_1", PauseResolution(decision="approve"))

    result = waiter.find_pending(pause_type="escalation", conversation_id="conv_A")
    assert result is None


def test_list_pending_by_conversation_and_type() -> None:
    waiter = PauseWaiter()
    waiter.register(
        PendingPause(
            pause_id="escalation:call_1",
            pause_type="escalation",
            conversation_id="conv_A",
        )
    )
    waiter.register(
        PendingPause(
            pause_id="escalation:call_2",
            pause_type="escalation",
            conversation_id="conv_A",
        )
    )
    waiter.register(
        PendingPause(
            pause_id="gate:g1",
            pause_type="gate",
            conversation_id="conv_A",
        )
    )

    result = waiter.list_pending(conversation_id="conv_A", pause_type="escalation")
    assert len(result) == 2
    assert all(p.pause_type == "escalation" for p in result)


@pytest.mark.asyncio
async def test_escalation_approve_unblocks() -> None:
    """Approving an escalation unblocks the waiting coroutine."""
    waiter = PauseWaiter()
    pause_id = "escalation:call_abc"
    waiter.register(
        PendingPause(
            pause_id=pause_id,
            pause_type="escalation",
            conversation_id="conv_1",
            context={"call_id": "call_abc"},
        )
    )

    async def _approve_soon() -> None:
        await asyncio.sleep(0.01)
        waiter.resolve(pause_id, PauseResolution(decision="approve", data={"note": "looks safe"}))

    asyncio.create_task(_approve_soon())
    result = await waiter.wait(pause_id, timeout=1.0)

    assert result.decision == "approve"
    assert result.data["note"] == "looks safe"


@pytest.mark.asyncio
async def test_escalation_deny_unblocks() -> None:
    """Denying an escalation unblocks the waiting coroutine."""
    waiter = PauseWaiter()
    pause_id = "escalation:call_def"
    waiter.register(
        PendingPause(
            pause_id=pause_id,
            pause_type="escalation",
            conversation_id="conv_1",
        )
    )

    async def _deny_soon() -> None:
        await asyncio.sleep(0.01)
        waiter.resolve(pause_id, PauseResolution(decision="deny", data={"note": "too risky"}))

    asyncio.create_task(_deny_soon())
    result = await waiter.wait(pause_id, timeout=1.0)

    assert result.decision == "deny"
    assert result.data["note"] == "too risky"


@pytest.mark.asyncio
async def test_escalation_timeout_raises() -> None:
    """Unresolved escalation times out."""
    waiter = PauseWaiter()
    pause_id = "escalation:call_timeout"
    waiter.register(
        PendingPause(
            pause_id=pause_id,
            pause_type="escalation",
            conversation_id="conv_1",
        )
    )

    with pytest.raises(TimeoutError):
        await waiter.wait(pause_id, timeout=0.01)


@pytest.mark.asyncio
async def test_escalation_double_resolve_returns_false() -> None:
    """Second resolve on the same escalation returns False."""
    waiter = PauseWaiter()
    pause_id = "escalation:call_double"
    waiter.register(
        PendingPause(
            pause_id=pause_id,
            pause_type="escalation",
            conversation_id="conv_1",
        )
    )

    first = waiter.resolve(pause_id, PauseResolution(decision="approve"))
    second = waiter.resolve(pause_id, PauseResolution(decision="deny"))

    assert first is True
    assert second is False


def test_find_pending_returns_oldest_first() -> None:
    """find_pending returns the first registered (oldest) pause."""
    waiter = PauseWaiter()
    waiter.register(
        PendingPause(
            pause_id="escalation:old",
            pause_type="escalation",
            conversation_id="conv_1",
            context={"call_id": "old"},
        )
    )
    waiter.register(
        PendingPause(
            pause_id="escalation:new",
            pause_type="escalation",
            conversation_id="conv_1",
            context={"call_id": "new"},
        )
    )

    result = waiter.find_pending(pause_type="escalation", conversation_id="conv_1")
    assert result is not None
    assert result.pause_id == "escalation:old"


# ---------------------------------------------------------------------------
# Slash command parsing tests
# ---------------------------------------------------------------------------


def test_approve_command_parsing() -> None:
    """Test /approve command note extraction."""
    cases = [
        ("/approve", "approve", None),
        ("/approve this looks safe", "approve", "this looks safe"),
        ("/approve  extra spaces  ", "approve", "extra spaces"),
        ("/deny", "deny", None),
        ("/deny too risky", "deny", "too risky"),
        ("/deny  multiple  words  ", "deny", "multiple  words"),
    ]
    for text, expected_cmd, expected_note in cases:
        stripped = text.strip()
        is_approve = stripped.startswith("/approve")
        cmd_word = "/approve" if is_approve else "/deny"
        note = stripped[len(cmd_word) :].strip() or None
        cmd = "approve" if is_approve else "deny"

        assert cmd == expected_cmd, f"Failed for {text!r}"
        assert note == expected_note, f"Failed note for {text!r}: got {note!r}"


def test_non_escalation_commands_not_matched() -> None:
    """Messages that start with /research etc. should not match /approve or /deny."""
    for text in ["/research something", "/implement feature", "/task do this", "approve this"]:
        stripped = text.strip()
        assert not (stripped.startswith("/approve") or stripped.startswith("/deny"))
