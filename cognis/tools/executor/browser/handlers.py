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

_CANDIDATE_DISCOVERY_SCRIPT = r"""
(payload) => {
  const maxResults = Math.max(1, Math.min(payload.maxResults ?? 80, 200));
  const selector = payload.selector || null;
  const mode = payload.mode || 'actionable';
  const assignAttr = payload.assignAttr || null;
  const includeComputed = payload.includeComputed === true;

  const clearAttribute = (rootsToClear, attr) => {
    if (!attr) return;
    for (const root of rootsToClear) {
      const nodes = root.querySelectorAll ? root.querySelectorAll(`[${attr}]`) : [];
      for (const node of nodes) node.removeAttribute(attr);
    }
  };

  const roots = [];
  const seenRoots = new Set();
  const walkRoots = (root) => {
    if (!root || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of elements) {
      if (el.shadowRoot) walkRoots(el.shadowRoot);
    }
  };
  walkRoots(document);
  clearAttribute(roots, assignAttr);

  const matchesSelector = (el) => {
    if (!selector) return true;
    try {
      return el.matches(selector);
    } catch {
      return false;
    }
  };

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };

  const findLabelText = (el) => {
    const aria = el.getAttribute('aria-labelledby');
    if (aria) {
      const texts = aria
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText?.trim())
        .filter(Boolean);
      if (texts.length) return texts.join(' ');
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label?.innerText?.trim()) return label.innerText.trim();
    }
    const wrapped = el.closest('label');
    if (wrapped?.innerText?.trim()) return wrapped.innerText.trim();
    return '';
  };

  const purposeScore = (el, labelText, ariaLabel, placeholder, type, autocomplete, inputmode, text) => {
    const haystack = `${labelText} ${ariaLabel} ${placeholder} ${text}`.toLowerCase();
    let score = 0;
    if ((autocomplete || '').toLowerCase() === 'one-time-code') score += 100;
    if ((inputmode || '').toLowerCase() === 'numeric') score += 25;
    if ((type || '').toLowerCase() === 'tel') score += 20;
    if (/otp|one-time|authenticator|verification|2fa|mfa|code/.test(haystack)) score += 30;
    if (/password/.test(haystack) || (type || '').toLowerCase() === 'password') score += 20;
    return score;
  };

  const collectCandidates = [];
  for (const root of roots) {
    const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of elements) {
      if (!(el instanceof HTMLElement)) continue;
      if (!matchesSelector(el)) continue;
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute('role') || '';
      const type = el.getAttribute('type') || '';
      const isFillable = ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable || ['textbox', 'searchbox', 'combobox', 'spinbutton'].includes(role);
      const isClickable = ['a', 'button'].includes(tag) || ['button', 'link', 'tab', 'checkbox', 'radio'].includes(role) || el.onclick != null;
      if (mode === 'fillable' && !isFillable) continue;
      if (mode === 'clickable' && !isClickable) continue;
      if (mode === 'actionable' && !isFillable && !isClickable) continue;
      const visible = isVisible(el);
      const enabled = !(el).disabled;
      const editable = isFillable && enabled && !(el).readOnly && type !== 'hidden';
      const disabled = !!(el).disabled;
      const readOnly = !!(el).readOnly;
      const rect = el.getBoundingClientRect();
      const text = (el.innerText || el.textContent || '').trim().slice(0, 160);
      const placeholder = el.getAttribute('placeholder') || '';
      const ariaLabel = el.getAttribute('aria-label') || '';
      const autocomplete = el.getAttribute('autocomplete') || '';
      const inputmode = el.getAttribute('inputmode') || '';
      const labelText = findLabelText(el);
      const purpose_score = purposeScore(el, labelText, ariaLabel, placeholder, type, autocomplete, inputmode, text);
      collectCandidates.push({
        element: el,
        tag,
        role,
        type,
        name: el.getAttribute('name') || '',
        placeholder,
        aria_label: ariaLabel,
        label_text: labelText,
        autocomplete,
        inputmode,
        text,
        visible,
        enabled,
        editable,
        disabled,
        read_only: readOnly,
        is_clickable: isClickable,
        is_fillable: isFillable,
        value_state: (type === 'password' || type === 'hidden' || autocomplete.includes('one-time-code')) ? 'redacted' : ((el.value || '') ? 'non_empty' : 'empty'),
        bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        purpose_score,
      });
    }
  }

  const ranked = collectCandidates
    .sort((a, b) => b.purpose_score - a.purpose_score)
    .slice(0, maxResults)
    .map((item, index) => {
      const ref = `e${index + 1}`;
      if (assignAttr) item.element.setAttribute(assignAttr, ref);
      const computed = includeComputed
        ? (() => {
            const style = window.getComputedStyle(item.element);
            return {
              display: style.display,
              visibility: style.visibility,
              opacity: style.opacity,
            };
          })()
        : undefined;
      return {
        ref,
        exact_selector: assignAttr ? `[${assignAttr}="${ref}"]` : null,
        ...item,
        computed,
        frame_url: window.location.href,
        frame_name: window.name || '',
        in_shadow_dom: !!item.element.getRootNode && item.element.getRootNode() !== document,
      };
    });

  return ranked;
}
"""


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


async def _discover_candidates(
    session: Any,
    *,
    mode: str,
    selector: str | None = None,
    max_results: int = 80,
    assign_attr: str | None = None,
    include_computed: bool = False,
) -> list[dict[str, Any]]:
    result = await session.page.evaluate(
        _CANDIDATE_DISCOVERY_SCRIPT,
        {
            "mode": mode,
            "selector": selector,
            "maxResults": max_results,
            "assignAttr": assign_attr,
            "includeComputed": include_computed,
        },
    )
    return result if isinstance(result, list) else []


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": candidate.get("ref"),
        "tag": candidate.get("tag"),
        "type": candidate.get("type"),
        "name": candidate.get("name"),
        "label_text": candidate.get("label_text"),
        "placeholder": candidate.get("placeholder"),
        "autocomplete": candidate.get("autocomplete"),
        "inputmode": candidate.get("inputmode"),
        "visible": candidate.get("visible"),
        "enabled": candidate.get("enabled"),
        "editable": candidate.get("editable"),
        "purpose_score": candidate.get("purpose_score"),
        "frame_url": candidate.get("frame_url"),
        "in_shadow_dom": candidate.get("in_shadow_dom"),
    }


async def _resolve_ref_target(
    session: Any,
    *,
    ref: str,
    require_editable: bool,
) -> tuple[Any, dict[str, Any]]:
    selector = session.ref_map.get(ref)
    if not selector:
        raise ValueError(f"Unknown browser ref: {ref}")
    locator = session.page.locator(selector)
    count = await locator.count()
    if count <= 0:
        raise ValueError(f"Browser ref {ref} is stale; refresh browser_snapshot and retry")
    if count != 1:
        raise ValueError(
            f"Browser ref {ref} resolved ambiguously; refresh browser_snapshot and retry"
        )
    candidate = locator.nth(0)
    visible = await candidate.is_visible()
    enabled = await candidate.is_enabled()
    editable = await candidate.is_editable() if require_editable else None
    if not visible or not enabled or (require_editable and not editable):
        state = "visible enabled editable input" if require_editable else "visible enabled target"
        raise ValueError(f"Browser ref {ref} is no longer a {state}")
    info = dict(getattr(session, "ref_metadata", {}).get(ref, {}))
    info.update(
        {
            "ref": ref,
            "exact_selector": selector,
            "visible": visible,
            "enabled": enabled,
            "editable": editable,
        }
    )
    return candidate, info


async def _resolve_selector_target(
    session: Any,
    *,
    selector: str,
    mode: str,
) -> tuple[Any, dict[str, Any]]:
    candidates = await _discover_candidates(
        session,
        mode=mode,
        selector=selector,
        max_results=200,
        assign_attr="data-cognis-action-ref",
        include_computed=False,
    )
    if mode == "fillable":
        viable = [
            item
            for item in candidates
            if item.get("visible") and item.get("enabled") and item.get("editable")
        ]
    else:
        viable = [item for item in candidates if item.get("visible") and item.get("enabled")]
    if not viable:
        expected = (
            "visible enabled editable input" if mode == "fillable" else "visible enabled element"
        )
        raise ValueError(
            f"No {expected} matched selector. Use browser_query or browser_snapshot for inspection."
        )
    if len(viable) > 1:
        preview = [_candidate_summary(item) for item in viable[:5]]
        raise ValueError(
            f"Selector matched multiple viable candidates; use browser_query or browser_snapshot and pick a ref. Candidates: {json.dumps(preview)}"
        )
    target = viable[0]
    exact_selector = target.get("exact_selector")
    if not isinstance(exact_selector, str) or not exact_selector:
        raise ValueError("Resolved selector candidate does not have an exact selector")
    locator = session.page.locator(exact_selector)
    if await locator.count() != 1:
        raise ValueError("Resolved selector candidate became stale; inspect the page again")
    return locator.nth(0), target


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


async def handle_browser_list_sessions(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del arguments
    manager = _get_manager(context)
    sessions = await manager.list_sessions()
    return ToolResult(output=json.dumps({"sessions": sessions}))


async def handle_browser_list_profiles(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del arguments
    manager = _get_manager(context)
    profiles = await manager.list_profiles()
    return ToolResult(output=json.dumps({"profiles": profiles}))


async def handle_browser_query(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    selector = str(arguments.get("selector", ""))
    if not selector:
        raise ValueError("browser_query requires a selector")
    mode = str(arguments.get("mode", "all") or "all").lower()
    candidates = await _discover_candidates(
        session,
        mode=mode,
        selector=selector,
        max_results=max(1, min(int(arguments.get("limit", 50) or 50), 200)),
        assign_attr="data-cognis-query-ref",
        include_computed=bool(arguments.get("include_computed", False)),
    )
    session.ref_map = {
        str(item.get("ref")): str(item.get("exact_selector"))
        for item in candidates
        if isinstance(item, dict) and item.get("ref") and item.get("exact_selector")
    }
    session.ref_metadata = {
        str(item.get("ref")): dict(item)
        for item in candidates
        if isinstance(item, dict) and item.get("ref")
    }
    return ToolResult(output=json.dumps({"matches": candidates}, ensure_ascii=False))


async def handle_browser_eval(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    script = arguments.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("browser_eval requires a script")
    args = arguments.get("args")
    result = await session.page.evaluate(
        """
        ({ script, args }) => {
          const fn = (0, eval)(script);
          if (typeof fn !== 'function') {
            throw new Error('browser_eval script must evaluate to a function');
          }
          return fn(...(Array.isArray(args) ? args : []));
        }
        """,
        {"script": script, "args": args if isinstance(args, list) else []},
    )
    return ToolResult(output=json.dumps({"result": result}, ensure_ascii=False))


async def handle_browser_get_console(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    events = await manager.get_console_events(
        str(arguments.get("session_id", "")),
        level=str(arguments.get("level", "all") or "all"),
        limit=int(arguments.get("limit", 100) or 100),
    )
    return ToolResult(output=json.dumps({"events": events}, ensure_ascii=False))


async def handle_browser_get_network(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    resource_types = arguments.get("resource_types")
    events = await manager.get_network_events(
        str(arguments.get("session_id", "")),
        limit=int(arguments.get("limit", 100) or 100),
        resource_types=resource_types if isinstance(resource_types, list) else None,
    )
    return ToolResult(output=json.dumps({"events": events}, ensure_ascii=False))


async def handle_browser_snapshot(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    max_elements = max(1, min(int(arguments.get("max_elements", 40) or 40), 80))
    elements = await _discover_candidates(
        session,
        mode="actionable",
        max_results=max_elements,
        assign_attr="data-cognis-ref",
        include_computed=False,
    )
    session.ref_map = {
        str(item.get("ref")): str(item.get("exact_selector"))
        for item in elements
        if isinstance(item, dict) and item.get("ref") and item.get("exact_selector")
    }
    session.ref_metadata = {
        str(item.get("ref")): dict(item)
        for item in elements
        if isinstance(item, dict) and item.get("ref")
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
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    max_chars = int(arguments.get("max_chars", 4000) or 4000)
    text = await session.page.evaluate("() => (document.body?.innerText || '').trim()")
    return ToolResult(output=str(text)[:max_chars])


async def handle_browser_click(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="clickable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")
    await chosen.click()
    return ToolResult(
        output=json.dumps(
            {
                "action": "click",
                "source": source,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_fill(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    value = arguments.get("value")
    if not isinstance(value, str):
        raise ValueError("browser_fill requires a resolved string value")
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=True)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="fillable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")
    await chosen.fill(value)
    return ToolResult(
        output=json.dumps(
            {
                "action": "fill",
                "source": source,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_focus(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="actionable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")
    await chosen.focus()
    return ToolResult(
        output=json.dumps(
            {"action": "focus", "source": source, "target": _candidate_summary(info)},
            ensure_ascii=False,
        )
    )


async def handle_browser_type(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    text = arguments.get("text")
    if not isinstance(text, str):
        raise ValueError("browser_type requires text")
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=True)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="fillable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")
    await chosen.focus()
    delay = int(arguments.get("delay_ms", 0) or 0)
    if delay > 0:
        await chosen.press_sequentially(text, delay=delay)
    else:
        await chosen.press_sequentially(text)
    return ToolResult(
        output=json.dumps(
            {"action": "type", "source": source, "target": _candidate_summary(info)},
            ensure_ascii=False,
        )
    )


async def handle_browser_submit_form(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    mode = str(arguments.get("mode", "native") or "native").lower()
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="actionable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")
    if mode == "native":
        await chosen.evaluate(
            """
            (el) => {
              const form = el.closest('form');
              if (form) {
                form.requestSubmit ? form.requestSubmit() : form.submit();
                return;
              }
              if (el instanceof HTMLElement) el.click();
            }
            """
        )
    elif mode == "event":
        await chosen.evaluate(
            """
            (el) => {
              const form = el.closest('form');
              if (form) {
                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
              } else if (el instanceof HTMLElement) {
                el.dispatchEvent(new Event('click', { bubbles: true, cancelable: true }));
              }
            }
            """
        )
    else:
        raise ValueError("browser_submit_form mode must be 'native' or 'event'")
    return ToolResult(
        output=json.dumps(
            {
                "action": "submit_form",
                "source": source,
                "mode": mode,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_press(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
    await session.page.keyboard.press(str(arguments.get("key", "")))
    return ToolResult(output="Pressed key.")


async def handle_browser_wait_for(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
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
    session = await manager.get_live_session(str(arguments.get("session_id", "")))
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
    session = await manager.get_live_session(session_id)
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
