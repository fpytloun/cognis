"""Contract coverage for session recording keys accepted by Intaris."""

import re

import pytest

from cognis.providers.guardrails.event_keys import event_idempotency_key


@pytest.mark.parametrize("purpose", ["cancelled-active-stream", "slash-skill", "remember-failed"])
def test_event_keys_follow_receiver_contract(purpose: str) -> None:
    session = "sess_stream_checkpoint"
    key = event_idempotency_key(session, purpose, "turn_interrupted", 47, "payload-digest")
    assert key.startswith(f"{session}:")
    assert len(key) <= 256
    assert re.fullmatch(r"[A-Za-z0-9_.:@-]+", key)
    assert key == event_idempotency_key(session, purpose, "turn_interrupted", 47, "payload-digest")
    assert key != event_idempotency_key(session, purpose, "turn_interrupted", 48, "payload-digest")


def test_event_keys_bound_long_or_non_ascii_components() -> None:
    key = event_idempotency_key("sess_stream_checkpoint", "slash-skill", "Résumé / " * 100)
    assert len(key) <= 256
    assert key.isascii()


@pytest.mark.parametrize("session", ["", "session with spaces", "s" * 256])
def test_invalid_session_scope_fails_before_event_delivery(session: str) -> None:
    with pytest.raises(ValueError, match="session-scoped"):
        event_idempotency_key(session, "cancelled-active-stream", "turn_interrupted")
