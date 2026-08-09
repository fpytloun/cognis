from cognis.api.app import _PendingAppendWarmState


def test_running_to_completed_append_preserves_and_requeues_newer_watermark() -> None:
    state = _PendingAppendWarmState(max_sessions=8)
    running = ("stream-1", 10, "owner@example.com")
    completed = ("stream-1", 12, "owner@example.com")

    assert state.put("token-1", running) is False
    claimed_running = state.claim("token-1")
    assert claimed_running == running

    # A completed event arrives while the running watermark is resolving.
    assert state.put("token-1", completed) is False
    assert state.finish("token-1", claimed_running, succeeded=True) is True
    assert state.claim("token-1") == completed

    claimed_completed = state.claim("token-1")
    assert claimed_completed == completed
    assert state.finish("token-1", claimed_completed, succeeded=True) is False
    assert state.claim("token-1") is None


def test_older_append_cannot_replace_newer_pending_watermark() -> None:
    state = _PendingAppendWarmState(max_sessions=8)
    completed = ("stream-1", 12, "owner@example.com")

    state.put("token-1", completed)
    state.put("token-1", ("stream-1", 10, "owner@example.com"))

    assert state.claim("token-1") == completed


def test_transient_resolver_failure_requeues_exact_claim_then_success_removes_it() -> None:
    state = _PendingAppendWarmState(max_sessions=8)
    pending = ("stream-1", 12, "owner@example.com")
    state.put("token-1", pending)

    first_claim = state.claim("token-1")
    assert first_claim == pending
    assert state.finish("token-1", first_claim, succeeded=False) is True
    assert state.claim("token-1") == pending

    retry_claim = state.claim("token-1")
    assert retry_claim == pending
    assert state.finish("token-1", retry_claim, succeeded=True) is False
    assert state.claim("token-1") is None
