"""Tests for the executor-side input humanizer."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any

import pytest

from cognis.tools.executor.browser import humanizer

# ---------------------------------------------------------------------------
# Profile + intensity helpers
# ---------------------------------------------------------------------------


def test_profiles_cover_all_supported_intensities() -> None:
    assert set(humanizer.PROFILES.keys()) == set(humanizer.VALID_INTENSITIES)
    assert humanizer.PROFILES["off"].waypoints == 0
    assert humanizer.PROFILES["low"].waypoints > 0
    assert (
        humanizer.PROFILES["high"].inter_key_ms
        > humanizer.PROFILES["medium"].inter_key_ms
        > humanizer.PROFILES["low"].inter_key_ms
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "low"),
        ("", "low"),
        ("LOW", "low"),
        ("MeDiUm", "medium"),
        ("bogus", "low"),
    ],
)
def test_normalize_intensity(value: str | None, expected: str) -> None:
    assert humanizer.normalize_intensity(value) == expected


def test_jitter_returns_non_negative_around_mean() -> None:
    rng = random.Random(42)
    samples = [humanizer._jitter(rng, 100.0) for _ in range(2000)]  # noqa: SLF001
    assert all(s >= 0 for s in samples)
    avg = sum(samples) / len(samples)
    # Mean of a folded gaussian with sigma=mean/4 is very close to the mean.
    assert 80.0 < avg < 130.0


def test_jitter_zero_mean_returns_zero() -> None:
    rng = random.Random(0)
    assert humanizer._jitter(rng, 0.0) == 0.0  # noqa: SLF001


# ---------------------------------------------------------------------------
# Bezier path
# ---------------------------------------------------------------------------


def test_bezier_path_starts_and_ends_at_inputs() -> None:
    rng = random.Random(123)
    path = humanizer._bezier_path(  # noqa: SLF001
        rng=rng,
        start=(0.0, 0.0),
        end=(100.0, 100.0),
        waypoints=3,
    )
    assert path[0] == (0.0, 0.0)
    assert path[-1] == (100.0, 100.0)
    assert len(path) == 5  # start + 3 intermediate + end


def test_bezier_path_zero_waypoints_is_a_straight_segment() -> None:
    rng = random.Random(7)
    path = humanizer._bezier_path(  # noqa: SLF001
        rng=rng, start=(0.0, 0.0), end=(50.0, 50.0), waypoints=0
    )
    assert path == [(0.0, 0.0), (50.0, 50.0)]


def test_bezier_path_is_deterministic_with_same_seed() -> None:
    p1 = humanizer._bezier_path(  # noqa: SLF001
        rng=random.Random(99),
        start=(10.0, 10.0),
        end=(200.0, 50.0),
        waypoints=4,
    )
    p2 = humanizer._bezier_path(  # noqa: SLF001
        rng=random.Random(99),
        start=(10.0, 10.0),
        end=(200.0, 50.0),
        waypoints=4,
    )
    assert p1 == p2


# ---------------------------------------------------------------------------
# Click / type / fill behaviour with fakes
# ---------------------------------------------------------------------------


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.events: list[tuple[str, str | None]] = []

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        del steps
        self.moves.append((x, y))

    async def down(self, *, button: str = "left") -> None:
        self.events.append(("down", button))

    async def up(self, *, button: str = "left") -> None:
        self.events.append(("up", button))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.presses: list[str] = []

    async def type(self, char: str) -> None:
        self.keys.append(char)

    async def press(self, key: str) -> None:
        self.presses.append(key)


class _FakeLocator:
    def __init__(self, *, box: dict[str, float] | None = None) -> None:
        self._box = box or {"x": 100.0, "y": 100.0, "width": 50.0, "height": 30.0}
        self.clicked = 0
        self.fill_calls: list[str] = []
        self.type_calls: list[str] = []
        self.focused = 0

    async def bounding_box(self) -> dict[str, float]:
        return self._box

    async def click(self, *, button: str = "left", click_count: int = 1) -> None:
        del button
        self.clicked += click_count

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)

    async def type(self, value: str) -> None:
        self.type_calls.append(value)

    async def focus(self) -> None:
        self.focused += 1


class _FakePage:
    def __init__(self) -> None:
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()


@pytest.mark.asyncio
async def test_humanize_click_off_passes_through_to_locator_click() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    await humanizer.humanize_click(page, locator, intensity="off")
    assert locator.clicked == 1
    assert page.mouse.moves == []


@pytest.mark.asyncio
async def test_humanize_click_low_uses_mouse_path_and_button_events() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    rng = random.Random(11)
    # No real sleeps in tests.
    await humanizer.humanize_click(page, locator, intensity="low", rng=rng)
    # Path = start + waypoints=2 + end => 4 moves. Final point should be the
    # bounding-box centre (125.0, 115.0).
    assert len(page.mouse.moves) == 4
    last = page.mouse.moves[-1]
    assert last == pytest.approx((125.0, 115.0))
    # Button down + up events fired.
    assert ("down", "left") in page.mouse.events
    assert ("up", "left") in page.mouse.events
    # Locator-level click() never invoked when humanizer succeeded.
    assert locator.clicked == 0


@pytest.mark.asyncio
async def test_humanize_click_falls_back_when_bounding_box_unknown() -> None:
    class _NoBox(_FakeLocator):
        async def bounding_box(self) -> None:
            return None

    page = _FakePage()
    locator = _NoBox()
    await humanizer.humanize_click(page, locator, intensity="medium")
    assert locator.clicked == 1


@pytest.mark.asyncio
async def test_humanize_type_off_passes_through_to_locator_type() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    await humanizer.humanize_type(page, locator, "abc", intensity="off")
    assert locator.type_calls == ["abc"]
    assert page.keyboard.keys == []


@pytest.mark.asyncio
async def test_humanize_type_low_emits_per_key_via_keyboard() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    await humanizer.humanize_type(page, locator, "abc", intensity="low", rng=random.Random(3))
    assert page.keyboard.keys == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_humanize_fill_off_passes_through_to_locator_fill() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    await humanizer.humanize_fill(page, locator, "hello", intensity="off")
    assert locator.fill_calls == ["hello"]


@pytest.mark.asyncio
async def test_humanize_fill_low_clears_then_types_per_key() -> None:
    page = _FakePage()
    locator = _FakeLocator()
    await humanizer.humanize_fill(page, locator, "abc", intensity="low", rng=random.Random(5))
    # Clear via Delete, then per-key type.
    assert "Delete" in page.keyboard.presses
    assert page.keyboard.keys == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Handler integration: intensity resolution + tool definitions
# ---------------------------------------------------------------------------


def test_resolve_intensity_uses_executor_default_when_humanize_on() -> None:
    from cognis.tools.executor.browser import handlers
    from cognis.tools.executor.browser.manager import BrowserManager

    manager = BrowserManager(humanize_input=True, humanize_intensity="medium")
    assert handlers._resolve_intensity({}, manager) == "medium"  # noqa: SLF001
    assert (
        handlers._resolve_intensity({"intensity": "high"}, manager)  # noqa: SLF001
        == "high"
    )
    # Invalid per-call values fall back to "low" (normalize_intensity).
    assert (
        handlers._resolve_intensity({"intensity": "weird"}, manager)  # noqa: SLF001
        == "low"
    )


def test_resolve_intensity_returns_off_when_humanize_disabled() -> None:
    from cognis.tools.executor.browser import handlers
    from cognis.tools.executor.browser.manager import BrowserManager

    manager = BrowserManager(humanize_input=False)
    assert handlers._resolve_intensity({}, manager) == "off"  # noqa: SLF001
    # Even an explicit per-call intensity is overridden when humanize is off.
    assert (
        handlers._resolve_intensity({"intensity": "high"}, manager)  # noqa: SLF001
        == "off"
    )


def test_browser_click_schema_advertises_intensity() -> None:
    from cognis.tools.executor.browser.definitions import browser_tool_definitions

    defs = {tool.name: tool for tool in browser_tool_definitions()}
    for name in ("browser_click", "browser_fill", "browser_type", "browser_press"):
        props = defs[name].parameters.get("properties", {})
        intensity = props.get("intensity")
        assert intensity is not None, f"{name} must expose intensity"
        assert intensity.get("enum") == ["off", "low", "medium", "high"]


def test_browser_type_and_press_schema_advertise_value_ref() -> None:
    from cognis.tools.executor.browser.definitions import browser_tool_definitions

    defs = {tool.name: tool for tool in browser_tool_definitions()}
    for name in ("browser_type", "browser_press"):
        props = defs[name].parameters.get("properties", {})
        assert props.get("value_ref") == {"type": "string"}
        assert "auth_challenge" in props


# ---------------------------------------------------------------------------
# Stage C asset loader sanity
# ---------------------------------------------------------------------------


def test_assets_module_loads_all_stage_c_files() -> None:
    from cognis.tools.executor.browser.assets import asset_dir, load_asset

    assert asset_dir().is_dir()
    for name in (
        "autoconsent.bundle.js",
        "fingerprint_audio.js",
        "fingerprint_battery.js",
        "fingerprint_viewport_jitter.js",
    ):
        body = load_asset(name)
        assert isinstance(body, str)
        assert len(body) > 100, f"{name} looks suspiciously empty"


def test_assets_module_logs_warning_for_missing_files(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from cognis.tools.executor.browser.assets import load_asset

    with caplog.at_level(logging.WARNING):
        result = load_asset("does-not-exist.js")
    assert result is None
    assert any("missing asset" in record.getMessage() for record in caplog.records)


# Used to silence flake about unused imports for SimpleNamespace if module
# evolves; concrete tests above use only the explicit fakes.
_ = SimpleNamespace, asyncio, Any
