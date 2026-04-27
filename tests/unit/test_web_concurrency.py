"""Tests for the web concurrency controller."""

from __future__ import annotations

import asyncio

import pytest

from cognis.tools.executor.web.concurrency import (
    WEB_CONCURRENCY_KEY,
    WebConcurrencyController,
    WebConcurrencySettings,
    build_settings_from_metadata,
    get_or_create_controller,
    host_for,
)


def test_host_for_extracts_lowercase_host() -> None:
    assert host_for("https://Example.COM/path") == "example.com"
    assert host_for("https://api.example.com:443/x?y=1") == "api.example.com"
    assert host_for("not-a-url") is None
    assert host_for("file:///etc/passwd") is None


def test_settings_defaults_are_sane() -> None:
    settings = WebConcurrencySettings()
    assert settings.global_cap >= 1
    assert settings.cap_for("direct") >= 1
    assert settings.cap_for("brave") >= 1
    # Unknown backend falls back to global cap.
    assert settings.cap_for("not-a-backend") == settings.global_cap
    assert settings.qps_for("direct") == 0.0
    assert settings.qps_for("brave") > 0.0


def test_build_settings_from_metadata_overrides() -> None:
    metadata = {
        "web_concurrency": {
            "global_cap": 7,
            "per_host_cap": 2,
            "backend_caps": {"direct": 9, "brave": 1, "bogus": "junk"},
            "rate_limits_qps": {"tavily": 2.5, "brave": "0.5"},
        }
    }
    settings = build_settings_from_metadata(metadata)
    assert settings.global_cap == 7
    assert settings.per_host_cap == 2
    assert settings.cap_for("direct") == 9
    assert settings.cap_for("brave") == 1
    # Bogus value silently kept at default for that backend.
    assert settings.cap_for("bogus") == settings.global_cap
    assert settings.qps_for("tavily") == 2.5
    assert settings.qps_for("brave") == 0.5


def test_build_settings_from_metadata_returns_defaults_for_invalid_payload() -> None:
    settings = build_settings_from_metadata({"web_concurrency": "garbage"})
    assert settings.global_cap == WebConcurrencySettings().global_cap


def test_get_or_create_controller_caches_by_metadata() -> None:
    metadata: dict[str, object] = {}
    a = get_or_create_controller(metadata)
    b = get_or_create_controller(metadata)
    assert a is b
    assert metadata[WEB_CONCURRENCY_KEY] is a


@pytest.mark.asyncio
async def test_per_host_semaphore_caps_concurrent_fetches() -> None:
    settings = WebConcurrencySettings(
        global_cap=64,
        per_host_cap=2,
        backend_caps={"direct": 64},
        rate_limits_qps={"direct": 0.0},
    )
    controller = WebConcurrencyController(settings)
    in_flight = 0
    high_water = 0
    started = asyncio.Event()

    async def worker() -> None:
        nonlocal in_flight, high_water
        async with controller.acquire(backend="direct", host="example.com"):
            in_flight += 1
            high_water = max(high_water, in_flight)
            started.set()
            # Yield enough times to interleave with peers.
            for _ in range(4):
                await asyncio.sleep(0)
            in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(10)])
    assert started.is_set()
    assert high_water <= 2


@pytest.mark.asyncio
async def test_global_cap_caps_concurrent_ops_across_hosts() -> None:
    settings = WebConcurrencySettings(
        global_cap=3,
        per_host_cap=10,
        backend_caps={"direct": 100},
        rate_limits_qps={"direct": 0.0},
    )
    controller = WebConcurrencyController(settings)
    in_flight = 0
    high_water = 0

    async def worker(idx: int) -> None:
        nonlocal in_flight, high_water
        async with controller.acquire(backend="direct", host=f"host-{idx}.example"):
            in_flight += 1
            high_water = max(high_water, in_flight)
            for _ in range(4):
                await asyncio.sleep(0)
            in_flight -= 1

    await asyncio.gather(*[worker(i) for i in range(20)])
    assert high_water <= 3


@pytest.mark.asyncio
async def test_per_backend_cap_isolates_backends() -> None:
    settings = WebConcurrencySettings(
        global_cap=64,
        per_host_cap=64,
        backend_caps={"direct": 1, "tavily": 5},
        rate_limits_qps={"direct": 0.0, "tavily": 0.0},
    )
    controller = WebConcurrencyController(settings)
    direct_high_water = 0
    tavily_high_water = 0
    direct_in_flight = 0
    tavily_in_flight = 0

    async def direct_worker() -> None:
        nonlocal direct_in_flight, direct_high_water
        async with controller.acquire(backend="direct", host="x.example"):
            direct_in_flight += 1
            direct_high_water = max(direct_high_water, direct_in_flight)
            for _ in range(2):
                await asyncio.sleep(0)
            direct_in_flight -= 1

    async def tavily_worker() -> None:
        nonlocal tavily_in_flight, tavily_high_water
        async with controller.acquire(backend="tavily", host="y.example"):
            tavily_in_flight += 1
            tavily_high_water = max(tavily_high_water, tavily_in_flight)
            for _ in range(2):
                await asyncio.sleep(0)
            tavily_in_flight -= 1

    await asyncio.gather(
        *[direct_worker() for _ in range(5)],
        *[tavily_worker() for _ in range(5)],
    )
    assert direct_high_water <= 1
    assert tavily_high_water <= 5


@pytest.mark.asyncio
async def test_rate_limit_token_bucket_throttles_burst() -> None:
    settings = WebConcurrencySettings(
        global_cap=64,
        per_host_cap=64,
        backend_caps={"brave": 10},
        rate_limits_qps={"brave": 5.0},  # 5 per second
    )
    controller = WebConcurrencyController(settings)

    async def worker() -> None:
        async with controller.acquire(backend="brave"):
            return None

    loop = asyncio.get_running_loop()
    start = loop.time()
    # 8 calls @ 5 qps should take ≥ ~0.6s (first burst absorbed by bucket
    # capacity, remaining throttled).
    await asyncio.gather(*[worker() for _ in range(8)])
    elapsed = loop.time() - start
    # Allow generous slack: token-bucket pacing is approximate, but anything
    # under 200 ms would mean throttling did not kick in.
    assert elapsed >= 0.2


@pytest.mark.asyncio
async def test_acquire_releases_on_exception() -> None:
    settings = WebConcurrencySettings(
        global_cap=1,
        per_host_cap=1,
        backend_caps={"direct": 1},
        rate_limits_qps={"direct": 0.0},
    )
    controller = WebConcurrencyController(settings)

    with pytest.raises(RuntimeError, match="boom"):
        async with controller.acquire(backend="direct", host="x.example"):
            raise RuntimeError("boom")

    # If the slot wasn't released, this would deadlock; bound it with a timeout.
    async with asyncio.timeout(1.0):
        async with controller.acquire(backend="direct", host="x.example"):
            pass
