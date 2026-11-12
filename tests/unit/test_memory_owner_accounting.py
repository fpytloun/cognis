from __future__ import annotations

import asyncio
import json
import weakref
from unittest.mock import Mock

import pytest

from cognis.core import resource_attribution, session_cache
from cognis.core.resource_attribution import ResourceAttribution
from cognis.core.session_cache import CachedSessionState, SessionCache


class Payload:
    pass


@pytest.mark.asyncio
async def test_accounting_measures_each_field_without_contents_or_cache_touches():
    cache = SessionCache(Mock())
    state = CachedSessionState(session_id="private-session", intaris_session_id="private-intaris")
    state.events = [{"secret": "Never export this private payload " * 100}]
    cache._entries[state.session_id] = state
    touched = state.touched_at
    result = await cache.resource_owner_accounting()
    assert result["sessions"] == 1
    fields = result["accounting"]["fields"]
    assert fields["events"]["estimated_bytes"] > 3000
    assert all(row["roots_measured"] == 1 for row in fields.values())
    assert state.touched_at == touched
    assert cache._entries[state.session_id] is state
    encoded = json.dumps(result)
    assert "private-session" not in encoded
    assert "private-intaris" not in encoded
    assert "Never export" not in encoded
    assert "secret" not in encoded
    assert len(encoded) < 65536


@pytest.mark.asyncio
async def test_accounting_uses_bounded_slices_and_reports_incomplete_coverage():
    cache = SessionCache(Mock())
    for index in range(65):
        state = CachedSessionState(session_id=str(index), intaris_session_id=str(index))
        state.events = [{} for _ in range(10000)]
        cache._entries[state.session_id] = state
    result = await cache.resource_owner_accounting()
    accounting = result["accounting"]
    assert accounting["selected_sessions"] == 64
    assert accounting["unselected_sessions"] == 1
    assert accounting["fields"]["events"]["roots_truncated"] > 0
    assert accounting["fields"]["events"]["visited_objects"] <= 64 * 2000
    assert accounting["work_seconds"] > 0
    assert accounting["slice_budget_seconds"] == 0.002
    assert len(cache._entries) == 65


@pytest.mark.asyncio
async def test_cache_removal_between_slices_does_not_retain_graph_or_fail():
    cache = SessionCache(Mock())
    state = CachedSessionState(session_id="selected", intaris_session_id="selected")
    cache._entries[state.session_id] = state
    payload = Payload()
    state.events = [payload]
    reference = weakref.ref(payload)
    del payload, state

    async def remove():
        await asyncio.sleep(0)
        cache._entries.clear()

    await asyncio.gather(cache.resource_owner_accounting(), remove())
    assert reference() is None


@pytest.mark.asyncio
async def test_cancellation_releases_borrowed_entry():
    cache = SessionCache(Mock())
    state = CachedSessionState(session_id="selected", intaris_session_id="selected")
    cache._entries[state.session_id] = state
    payload = Payload()
    state.events = [payload]
    reference = weakref.ref(payload)
    del payload, state
    capture = asyncio.create_task(cache.resource_owner_accounting())
    await asyncio.sleep(0)
    capture.cancel()
    with pytest.raises(asyncio.CancelledError):
        await capture
    cache._entries.clear()
    assert reference() is None


@pytest.mark.asyncio
async def test_async_provider_is_awaited_and_capture_stop_remains_owner():
    cache = SessionCache(Mock())
    attribution = ResourceAttribution(configured=True, duration=1, stacks=False, log_details=False)
    attribution.start(cache.resource_owner_accounting)
    await asyncio.wait_for(attribution.task, timeout=3)
    assert attribution.report["errors"] == []
    assert attribution.report["memory"][0]["owners"]["accounting"]["selected_sessions"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["work", "wall"])
async def test_global_cutoffs_are_enforced_deterministically(monkeypatch, limit):
    cache = SessionCache(Mock())
    cache._entries["selected"] = CachedSessionState(
        session_id="selected", intaris_session_id="selected"
    )
    clock = [0.0]
    monkeypatch.setattr(session_cache, "monotonic", lambda: clock[0])

    def measure(*args, **kwargs):
        if limit == "work":
            clock[0] += 0.060
        return {
            "estimated_bytes": 1,
            "visited_objects": 1,
            "shallow_only_objects": 0,
            "truncated": False,
        }

    monkeypatch.setattr(resource_attribution, "measure_owned_objects", measure)
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if limit == "wall":
            clock[0] += 6
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = (await cache.resource_owner_accounting())["accounting"]
    assert result["stopped_reason"] == limit
    measured = sum(field["roots_measured"] for field in result["fields"].values()) + len(
        result["cache_roots"]
    )
    assert measured == (2 if limit == "work" else 1)


@pytest.mark.asyncio
async def test_graph_is_released_before_yield_not_only_at_completion(monkeypatch):
    cache = SessionCache(Mock())
    state = CachedSessionState(session_id="selected", intaris_session_id="selected")
    payload = Payload()
    state.events = [payload]
    cache._entries[state.session_id] = state
    reference = weakref.ref(payload)
    del payload, state
    entered, release = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    original_measure = resource_attribution.measure_owned_objects
    measured_events = False

    def measure(roots, **kwargs):
        nonlocal measured_events
        value = next(iter(roots))
        if isinstance(value, list) and value and isinstance(value[0], Payload):
            measured_events = True
        return original_measure(roots, **kwargs)

    async def pause(delay):
        if measured_events:
            entered.set()
            await release.wait()
        else:
            await original_sleep(delay)

    monkeypatch.setattr(resource_attribution, "measure_owned_objects", measure)
    monkeypatch.setattr(asyncio, "sleep", pause)
    capture = asyncio.create_task(cache.resource_owner_accounting())
    await entered.wait()
    cache._entries.clear()
    assert not capture.done()
    assert reference() is None
    release.set()
    await capture


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_sessions", [0, 1])
async def test_population_growth_during_yield_invalidates_complete_coverage(initial_sessions):
    cache = SessionCache(Mock())
    for index in range(initial_sessions):
        cache._entries[str(index)] = CachedSessionState(
            session_id=str(index), intaris_session_id=str(index)
        )
    task = asyncio.create_task(cache.resource_owner_accounting())
    await asyncio.sleep(0)
    cache._entries["new"] = CachedSessionState(session_id="new", intaris_session_id="new")
    result = await task
    assert result["sessions"] == initial_sessions + 1
    assert result["accounting"]["population_changed"]
    assert not result["accounting"]["coverage_complete"]


@pytest.mark.asyncio
async def test_same_size_population_replacement_invalidates_complete_coverage():
    cache = SessionCache(Mock())
    cache._entries["old"] = CachedSessionState(session_id="old", intaris_session_id="old")
    task = asyncio.create_task(cache.resource_owner_accounting())
    await asyncio.sleep(0)
    cache._entries.pop("old")
    cache._entries["new"] = CachedSessionState(session_id="new", intaris_session_id="new")
    result = await task
    assert result["sessions"] == 1
    assert result["accounting"]["population_changed"]
    assert not result["accounting"]["coverage_complete"]


@pytest.mark.asyncio
async def test_rotation_eventually_visits_sessions_beyond_selection_cap(monkeypatch):
    cache = SessionCache(Mock())
    identifiers = {}
    for index in range(70):
        state = CachedSessionState(session_id=str(index), intaris_session_id=str(index))
        cache._entries[state.session_id] = state
        identifiers[id(state.events)] = index
    clock = [0.0]
    seen = set()
    monkeypatch.setattr(session_cache, "monotonic", lambda: clock[0])

    def measure(roots, **kwargs):
        value = next(iter(roots))
        if id(value) in identifiers:
            seen.add(identifiers[id(value)])
        clock[0] += 0.002
        return {
            "estimated_bytes": 1,
            "visited_objects": 1,
            "shallow_only_objects": 0,
            "truncated": False,
        }

    monkeypatch.setattr(resource_attribution, "measure_owned_objects", measure)
    original_order = list(cache._entries)
    for _ in range(26):
        result = (await cache.resource_owner_accounting())["accounting"]
        assert result["visited_sessions"] < result["selected_sessions"]
        assert not result["coverage_complete"]
        assert result["root_visits"] < result["expected_root_visits"]
    assert seen == set(range(70))
    assert list(cache._entries) == original_order


@pytest.mark.asyncio
async def test_opaque_and_truncated_roots_are_not_reported_as_complete():
    cache = SessionCache(Mock())
    state = CachedSessionState(session_id="private", intaris_session_id="private")
    state.projection_decisions = {"private-call": [{"payload": Payload()}]}
    state.project_metadata_contexts = {"private-project": Payload()}
    cache._entries[state.session_id] = state
    result = (await cache.resource_owner_accounting())["accounting"]
    assert result["visited_sessions"] == 1
    assert result["fully_measured_sessions"] == 0
    assert not result["coverage_complete"]
    assert result["truncated_roots_are_lower_bounds"]
    assert result["fields"]["projection_decisions"]["shallow_only_objects"] > 0
    assert result["fields"]["project_metadata_contexts"]["shallow_only_objects"] > 0
    assert "private" not in json.dumps(result)
