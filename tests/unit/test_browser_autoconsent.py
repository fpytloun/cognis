"""Tests for autoconsent + fingerprint hardening init scripts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cognis.tools.executor.browser.manager import BrowserManager


class _FakeContext:
    """Minimal stand-in for a Playwright BrowserContext."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)


# ---------------------------------------------------------------------------
# autoconsent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoconsent_init_script_registered_when_enabled() -> None:
    manager = BrowserManager(auto_consent="accept")
    ctx = _FakeContext()
    await manager._apply_autoconsent_init_script(ctx)  # noqa: SLF001
    assert len(ctx.scripts) == 1
    payload = ctx.scripts[0]
    config_line = payload.splitlines()[0]
    assert config_line.startswith("window.__cognis_autoconsent = ")
    cfg_json = config_line.removeprefix("window.__cognis_autoconsent = ").rstrip(";")
    cfg = json.loads(cfg_json)
    assert cfg["action"] == "accept"
    assert cfg["delayMs"] == 800
    assert cfg["disabledHosts"] == []


@pytest.mark.asyncio
async def test_autoconsent_skipped_when_off() -> None:
    manager = BrowserManager(stealth_enabled=False)  # auto_consent defaults to off
    ctx = _FakeContext()
    await manager._apply_autoconsent_init_script(ctx)  # noqa: SLF001
    assert ctx.scripts == []


@pytest.mark.asyncio
async def test_autoconsent_action_reject_propagates() -> None:
    manager = BrowserManager(auto_consent="reject")
    ctx = _FakeContext()
    await manager._apply_autoconsent_init_script(ctx)  # noqa: SLF001
    cfg_json = ctx.scripts[0].splitlines()[0].split("=", 1)[1].strip().rstrip(";")
    assert json.loads(cfg_json)["action"] == "reject"


@pytest.mark.asyncio
async def test_autoconsent_disabled_domains_normalized() -> None:
    manager = BrowserManager(
        auto_consent="accept",
        auto_consent_disabled_domains=["EXAMPLE.com", "  ", "Foo.BAR"],
    )
    ctx = _FakeContext()
    await manager._apply_autoconsent_init_script(ctx)  # noqa: SLF001
    cfg_json = ctx.scripts[0].splitlines()[0].split("=", 1)[1].strip().rstrip(";")
    assert json.loads(cfg_json)["disabledHosts"] == ["example.com", "foo.bar"]


@pytest.mark.asyncio
async def test_autoconsent_silent_when_asset_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.browser import manager as manager_mod

    monkeypatch.setattr(manager_mod, "load_asset", lambda _name: None)
    manager = BrowserManager(auto_consent="accept")
    ctx = _FakeContext()
    await manager._apply_autoconsent_init_script(ctx)  # noqa: SLF001
    assert ctx.scripts == []


@pytest.mark.asyncio
async def test_autoconsent_invalid_action_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported auto_consent"):
        BrowserManager(auto_consent="weird-mode")


# ---------------------------------------------------------------------------
# fingerprint hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_hardening_registers_three_scripts_for_ephemeral() -> None:
    manager = BrowserManager(fingerprint_hardening=True)
    ctx = _FakeContext()
    await manager._apply_fingerprint_hardening_init_scripts(ctx)  # noqa: SLF001
    # audio + battery + viewport (ephemeral keeps viewport jitter)
    assert len(ctx.scripts) == 3
    seeds = {script.splitlines()[0] for script in ctx.scripts}
    assert len(seeds) == 1, "all scripts should share the same seed prefix"


@pytest.mark.asyncio
async def test_fingerprint_hardening_drops_viewport_for_persistent_profile() -> None:
    manager = BrowserManager(fingerprint_hardening=True)
    ctx = _FakeContext()
    await manager._apply_fingerprint_hardening_init_scripts(  # noqa: SLF001
        ctx, profile_id="example-com"
    )
    assert len(ctx.scripts) == 2  # audio + battery; viewport dropped
    payload_blob = "\n".join(ctx.scripts)
    assert "viewport_jitter" not in payload_blob
    assert "AudioBuffer" in payload_blob
    assert "getBattery" in payload_blob


@pytest.mark.asyncio
async def test_fingerprint_hardening_skipped_when_disabled() -> None:
    manager = BrowserManager(fingerprint_hardening=False)
    ctx = _FakeContext()
    await manager._apply_fingerprint_hardening_init_scripts(ctx)  # noqa: SLF001
    assert ctx.scripts == []


@pytest.mark.asyncio
async def test_fingerprint_hardening_seed_is_deterministic_per_profile() -> None:
    m1 = BrowserManager(fingerprint_hardening=True)
    m2 = BrowserManager(fingerprint_hardening=True)
    c1 = _FakeContext()
    c2 = _FakeContext()
    await m1._apply_fingerprint_hardening_init_scripts(c1, profile_id="reddit")  # noqa: SLF001
    await m2._apply_fingerprint_hardening_init_scripts(c2, profile_id="reddit")  # noqa: SLF001
    assert c1.scripts[0].splitlines()[0] == c2.scripts[0].splitlines()[0]


@pytest.mark.asyncio
async def test_fingerprint_hardening_seed_differs_across_profiles() -> None:
    m = BrowserManager(fingerprint_hardening=True)
    c_a = _FakeContext()
    c_b = _FakeContext()
    await m._apply_fingerprint_hardening_init_scripts(c_a, profile_id="reddit")  # noqa: SLF001
    await m._apply_fingerprint_hardening_init_scripts(c_b, profile_id="github")  # noqa: SLF001
    seed_a = c_a.scripts[0].splitlines()[0]
    seed_b = c_b.scripts[0].splitlines()[0]
    assert seed_a != seed_b


@pytest.mark.asyncio
async def test_fingerprint_hardening_per_evasion_exclusion() -> None:
    manager = BrowserManager(
        fingerprint_hardening=True,
        stealth_evasions=["audio_context", "battery_api"],
    )
    ctx = _FakeContext()
    await manager._apply_fingerprint_hardening_init_scripts(ctx)  # noqa: SLF001
    # audio + battery excluded -> only viewport remains
    assert len(ctx.scripts) == 1
    assert "innerWidth" in ctx.scripts[0]


# ---------------------------------------------------------------------------
# Manager defaults / wiring
# ---------------------------------------------------------------------------


def test_stage_c_defaults_anchored_to_stealth_enabled() -> None:
    on_manager = BrowserManager()  # default playwright + stealth on
    assert on_manager.auto_consent == "accept"
    assert on_manager.humanize_input is True
    assert on_manager.fingerprint_hardening is True

    off_manager = BrowserManager(stealth_enabled=False)
    assert off_manager.auto_consent == "off"
    assert off_manager.humanize_input is False
    assert off_manager.fingerprint_hardening is False


def test_stage_c_defaults_for_patchright_disabled_by_default() -> None:
    manager = BrowserManager(runtime="patchright")
    # Patchright defaults stealth off; Stage C anchors on that.
    assert manager.auto_consent == "off"
    assert manager.humanize_input is False
    assert manager.fingerprint_hardening is False


def test_stage_c_humanize_intensity_validation() -> None:
    with pytest.raises(ValueError, match="humanize_intensity"):
        BrowserManager(humanize_intensity="bogus")


def test_apply_context_defaults_invokes_stage_c_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-test: _apply_context_defaults wires both autoconsent + fingerprint."""
    import asyncio

    manager = BrowserManager()
    ctx = _FakeContext()

    # Disable stealth entirely so we don't need playwright_stealth in scope.
    manager.stealth_enabled = False
    manager._stealth = None  # noqa: SLF001
    # But keep autoconsent + fingerprint hardening enabled.
    manager.auto_consent = "accept"
    manager.fingerprint_hardening = True

    asyncio.run(manager._apply_context_defaults(ctx, profile_id="reddit"))  # noqa: SLF001
    # autoconsent (1) + audio + battery (viewport dropped for persistent profile)
    assert len(ctx.scripts) == 3


# ---------------------------------------------------------------------------
# handlers config wiring
# ---------------------------------------------------------------------------


def test_handler_browser_config_passes_stage_c_keys() -> None:
    from cognis.tools.executor.browser import handlers

    metadata = {
        "browser": {
            "auto_consent": "reject",
            "auto_consent_disabled_domains": ["x.com"],
            "auto_consent_delay_ms": 500,
            "humanize_input": False,
            "humanize_intensity": "high",
            "fingerprint_hardening": False,
        }
    }
    cfg = handlers._browser_config(metadata)  # noqa: SLF001
    assert cfg["auto_consent"] == "reject"
    assert cfg["auto_consent_disabled_domains"] == ["x.com"]
    assert cfg["auto_consent_delay_ms"] == 500
    assert cfg["humanize_input"] is False
    assert cfg["humanize_intensity"] == "high"
    assert cfg["fingerprint_hardening"] is False


def test_handler_get_manager_constructs_with_stage_c_args() -> None:
    from cognis.tools.executor.browser import handlers

    class _Ctx:
        runtime_metadata: dict[str, Any] = {
            "browser": {
                "auto_consent": "reject",
                "auto_consent_disabled_domains": "first.com, second.com",
                "humanize_input": True,
                "humanize_intensity": "medium",
                "fingerprint_hardening": True,
            }
        }

    manager = handlers._get_manager(_Ctx())  # noqa: SLF001
    assert manager.auto_consent == "reject"
    assert manager.auto_consent_disabled_domains == ["first.com", "second.com"]
    assert manager.humanize_input is True
    assert manager.humanize_intensity == "medium"
    assert manager.fingerprint_hardening is True


def test_handler_get_manager_handles_humanize_input_undefined() -> None:
    from cognis.tools.executor.browser import handlers

    class _Ctx:
        runtime_metadata: dict[str, Any] = {"browser": {}}

    manager = handlers._get_manager(_Ctx())  # noqa: SLF001
    # humanize_input falls back to manager's stealth-anchored default.
    assert manager.humanize_input is True
