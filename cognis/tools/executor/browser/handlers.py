"""Playwright-backed browser handlers."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlparse

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser.manager import (
    BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS,
    BROWSER_DEFAULT_MAX_SESSIONS,
    BROWSER_MANAGER_KEY,
    BrowserManager,
)
from cognis.tools.registry import ToolExecutionContext


def _browser_config(runtime_metadata: dict[str, Any]) -> dict[str, Any]:
    browser_cfg = runtime_metadata.get("browser")
    if isinstance(browser_cfg, dict):
        return dict(browser_cfg)
    return {
        "enabled": runtime_metadata.get("browser_enabled", True),
        "auto_install": runtime_metadata.get("browser_auto_install", False),
        "headed_allowed": runtime_metadata.get("browser_headed_allowed", False),
        "engine": runtime_metadata.get("browser_engine", "chromium"),
        "max_sessions": runtime_metadata.get("browser_max_sessions", BROWSER_DEFAULT_MAX_SESSIONS),
        "idle_timeout_seconds": runtime_metadata.get(
            "browser_idle_timeout_seconds", BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS
        ),
        "persistent_profiles_enabled": runtime_metadata.get(
            "browser_persistent_profiles_enabled", True
        ),
        "profile_mode_default": runtime_metadata.get(
            "browser_profile_mode_default", "persistent_local"
        ),
        "profile_base_dir": runtime_metadata.get("browser_profile_base_dir"),
        "realistic_launch": runtime_metadata.get("browser_realistic_launch", True),
        "xvfb_auto": runtime_metadata.get("browser_xvfb_auto", True),
        "locale": runtime_metadata.get("browser_locale", "en-US"),
        "timezone_id": runtime_metadata.get("browser_timezone_id"),
        "viewport_width": runtime_metadata.get("browser_viewport_width", 1365),
        "viewport_height": runtime_metadata.get("browser_viewport_height", 900),
    }


def _get_manager(context: ToolExecutionContext) -> BrowserManager:
    existing = context.runtime_metadata.get(BROWSER_MANAGER_KEY)
    if isinstance(existing, BrowserManager):
        return existing
    cfg = _browser_config(context.runtime_metadata)
    manager = BrowserManager(
        enabled=bool(cfg.get("enabled", True)),
        auto_install=bool(cfg.get("auto_install", False)),
        engine=str(cfg.get("engine", "chromium")),
        headed_allowed=bool(cfg.get("headed_allowed", False)),
        max_sessions=int(cfg.get("max_sessions", BROWSER_DEFAULT_MAX_SESSIONS)),
        idle_timeout_seconds=int(
            cfg.get("idle_timeout_seconds", BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS)
        ),
        persistent_profiles_enabled=bool(cfg.get("persistent_profiles_enabled", True)),
        profile_mode_default=str(cfg.get("profile_mode_default", "persistent_local")),
        profile_base_dir=(
            str(cfg.get("profile_base_dir")) if cfg.get("profile_base_dir") else None
        ),
        realistic_launch=bool(cfg.get("realistic_launch", True)),
        xvfb_auto=bool(cfg.get("xvfb_auto", True)),
        locale=str(cfg.get("locale", "en-US")),
        timezone_id=(str(cfg.get("timezone_id")) if cfg.get("timezone_id") else None),
        viewport_width=int(cfg.get("viewport_width", 1365)),
        viewport_height=int(cfg.get("viewport_height", 900)),
    )
    context.runtime_metadata[BROWSER_MANAGER_KEY] = manager
    return manager


async def handle_browser_open(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.open_session(
        session_id=str(arguments.get("session_id", "")),
        url=str(arguments.get("url", "")),
        headless=bool(arguments.get("headless", True)),
        auth_state=(
            arguments.get("auth_state") if isinstance(arguments.get("auth_state"), dict) else None
        ),
        profile_mode=str(arguments.get("profile_mode", "default") or "default"),
        profile_id=(str(arguments.get("profile_id")) if arguments.get("profile_id") else None),
    )
    return ToolResult(
        output=json.dumps(
            {
                "session_id": session.session_id,
                "url": session.page.url,
                "title": await session.page.title(),
                "headless": bool(arguments.get("headless", True)),
                "profile_mode": session.profile_mode,
                "profile_id": session.profile_id,
            }
        )
    )


async def handle_browser_snapshot(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    max_elements = max(1, min(int(arguments.get("max_elements", 40) or 40), 80))
    elements = await session.page.evaluate(
        """
        (maxElements) => {
          const nodes = Array.from(document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"]')).slice(0, maxElements);
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const makeSelector = (el) => {
            if (el.id) return `#${CSS.escape(el.id)}`;
            const testid = el.getAttribute('data-testid');
            if (testid) return `[data-testid="${testid}"]`;
            const name = el.getAttribute('name');
            if (name && ['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) return `${el.tagName.toLowerCase()}[name="${name}"]`;
            return el.tagName.toLowerCase();
          };
          return nodes.map((el, idx) => {
            const type = (el.getAttribute && el.getAttribute('type')) || '';
            return {
              ref: `e${idx + 1}`,
              selector: makeSelector(el),
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || '',
              text: (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 120),
              type,
              name: el.getAttribute('name') || '',
              placeholder: el.getAttribute('placeholder') || '',
              aria_label: el.getAttribute('aria-label') || '',
              autocomplete: el.getAttribute('autocomplete') || '',
              inputmode: el.getAttribute('inputmode') || '',
              visible: isVisible(el),
              enabled: !el.disabled,
              editable: ['INPUT','TEXTAREA','SELECT'].includes(el.tagName) && !el.disabled && !el.readOnly && type !== 'hidden',
              disabled: !!el.disabled,
              read_only: !!el.readOnly,
              value_state: (type === 'password' || type === 'hidden' || (el.getAttribute('autocomplete') || '').includes('one-time-code'))
                ? 'redacted'
                : ((el.value || '') ? 'non_empty' : 'empty'),
            };
          });
        }
        """,
        max_elements,
    )
    session.ref_map = {
        str(item.get("ref")): str(item.get("selector"))
        for item in elements
        if isinstance(item, dict) and item.get("ref") and item.get("selector")
    }
    title = await session.page.title()
    return ToolResult(
        output=json.dumps(
            {
                "url": session.page.url,
                "title": title,
                "elements": elements,
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_get_text(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    max_chars = int(arguments.get("max_chars", 4000) or 4000)
    text = await session.page.evaluate("() => (document.body?.innerText || '').trim()")
    return ToolResult(output=str(text)[:max_chars])


def _selector_from_args(arguments: dict[str, Any], session: Any) -> str:
    ref = arguments.get("ref")
    if isinstance(ref, str) and ref:
        selector = session.ref_map.get(ref)
        if selector:
            return selector
        raise ValueError(f"Unknown browser ref: {ref}")
    selector = arguments.get("selector")
    if isinstance(selector, str) and selector:
        return selector
    raise ValueError("Provide either ref or selector")


async def handle_browser_click(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    locator = session.page.locator(_selector_from_args(arguments, session))
    count = await locator.count()
    if count <= 0:
        raise ValueError("No matching browser element found")
    chosen = None
    chosen_index = None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            if not await candidate.is_enabled():
                continue
        except Exception:
            continue
        chosen = candidate
        chosen_index = index
        break
    if chosen is None:
        raise ValueError(
            "No visible enabled browser element matched; call browser_snapshot again and choose a visible ref"
        )
    await chosen.click()
    return ToolResult(output=f"Clicked element using visible enabled match #{chosen_index}.")


async def handle_browser_fill(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    value = arguments.get("value")
    if not isinstance(value, str):
        raise ValueError("browser_fill requires a resolved string value")
    locator = session.page.locator(_selector_from_args(arguments, session))
    count = await locator.count()
    if count <= 0:
        raise ValueError("No matching browser input found")
    chosen = None
    chosen_index = None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            if not await candidate.is_enabled():
                continue
            if not await candidate.is_editable():
                continue
        except Exception:
            continue
        chosen = candidate
        chosen_index = index
        break
    if chosen is None:
        raise ValueError(
            "No visible enabled editable browser input matched; call browser_snapshot again and choose a visible ref"
        )
    await chosen.fill(value)
    return ToolResult(output=f"Filled element using visible editable match #{chosen_index}.")


async def handle_browser_press(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    await session.page.keyboard.press(str(arguments.get("key", "")))
    return ToolResult(output="Pressed key.")


async def handle_browser_wait_for(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    selector = arguments.get("selector")
    timeout_ms = int(arguments.get("timeout_ms", 10000) or 10000)
    if isinstance(selector, str) and selector:
        await session.page.wait_for_selector(selector, timeout=timeout_ms)
    else:
        await session.page.wait_for_timeout(timeout_ms)
    return ToolResult(output="Wait completed.")


async def handle_browser_screenshot(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = manager.get_session(str(arguments.get("session_id", "")))
    content = await session.page.screenshot(type="png")
    return ToolResult(
        output="Captured screenshot.",
        attachments=[
            {
                "kind": "image",
                "mime_type": "image/png",
                "filename": f"{session.session_id}.png",
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )


async def handle_browser_close(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    await manager.close_session(str(arguments.get("session_id", "")))
    return ToolResult(output="Closed browser session.")


async def handle_browser_save_auth_state(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session_id = str(arguments.get("session_id", ""))
    storage_state = await manager.storage_state(session_id)
    session = manager.get_session(session_id)
    current_url = getattr(session.page, "url", "")
    parsed = urlparse(str(current_url))
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return ToolResult(
        output="Browser auth state captured for persistence.",
        metadata={
            "browser_auth_state": {
                "credential_id": str(arguments.get("credential_id", "")),
                "label": str(arguments.get("label", "")),
                "kind": "browser_storage_state",
                "metadata": {
                    "origin": origin,
                    "domain": parsed.hostname,
                    "session_id": session_id,
                },
                "payload": {"storage_state": storage_state},
            }
        },
    )
