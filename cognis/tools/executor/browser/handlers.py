"""Playwright-backed browser handlers."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser import humanizer
from cognis.tools.executor.browser.manager import (
    BROWSER_DEFAULT_IDLE_TIMEOUT_SECONDS,
    BROWSER_DEFAULT_MAX_SESSIONS,
    BROWSER_MANAGER_KEY,
    SUPPORTED_AUTO_CONSENT_ACTIONS,
    BrowserLifecycleError,
    BrowserManager,
    BrowserSession,
    BrowserSessionOwner,
)
from cognis.tools.executor.paths import resolve_path
from cognis.tools.registry import ToolExecutionContext


def _resolve_intensity(
    arguments: dict[str, Any], manager: BrowserManager, session: BrowserSession | Any | None = None
) -> str:
    """Resolve humanizer intensity for a single tool call.

    Falls back to ``off`` when the executor disables humanization globally;
    otherwise uses the per-call ``intensity`` argument if present and valid,
    else the executor's configured default.
    """
    humanize_input = manager.humanize_input
    session_settings = getattr(session, "browser_settings", None) if session is not None else None
    if session_settings is not None:
        humanize_input = bool(session_settings.humanize_input)
    if not humanize_input:
        return "off"
    raw = arguments.get("intensity")
    if isinstance(raw, str) and raw.strip():
        return humanizer.normalize_intensity(raw)
    return manager.humanize_intensity


def _parse_browser_settings(arguments: dict[str, Any]) -> dict[str, Any] | None:
    raw = arguments.get("browser_settings")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("browser_settings must be an object")
    allowed = {"auto_consent", "stealth_enabled", "fingerprint_hardening", "humanize_input"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unsupported browser_settings field(s): {', '.join(unknown)}")
    parsed: dict[str, Any] = {}
    if "auto_consent" in raw:
        action = str(raw["auto_consent"]).strip().lower()
        if action not in SUPPORTED_AUTO_CONSENT_ACTIONS:
            raise ValueError(
                "browser_settings.auto_consent must be one of "
                + ", ".join(SUPPORTED_AUTO_CONSENT_ACTIONS)
            )
        parsed["auto_consent"] = action
    for key in ("stealth_enabled", "fingerprint_hardening", "humanize_input"):
        if key not in raw:
            continue
        if not isinstance(raw[key], bool):
            raise ValueError(f"browser_settings.{key} must be a boolean")
        parsed[key] = raw[key]
    return parsed


_FOCUS_DISCOVERY_SCRIPT = r"""
() => {
  const el = document.activeElement;
  if (!el || !(el instanceof HTMLElement) || el === document.body) {
    return null;
  }
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute('type') || '';
  const role = el.getAttribute('role') || '';
  const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  const enabled = !(el).disabled;
  const isFillable = ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable || ['textbox', 'searchbox', 'combobox', 'spinbutton'].includes(role);
  const editable = isFillable && enabled && !(el).readOnly && type !== 'hidden';
  return {
    tag,
    role,
    type,
    name: el.getAttribute('name') || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria_label: el.getAttribute('aria-label') || '',
    autocomplete: el.getAttribute('autocomplete') || '',
    inputmode: el.getAttribute('inputmode') || '',
    visible,
    enabled,
    editable,
    disabled: !!(el).disabled,
    read_only: !!(el).readOnly,
    value_state: (type === 'password' || type === 'hidden' || (el.getAttribute('autocomplete') || '').includes('one-time-code')) ? 'redacted' : ((el.value || '') ? 'non_empty' : 'empty'),
    bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
  };
}
"""


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

_PLAYWRIGHT_LOCATOR_PREFIXES = (
    "text=",
    "role=",
    "xpath=",
    "id=",
    "data-testid=",
    "data-test-id=",
)
_MAX_BROWSER_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_BROWSER_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_BROWSER_UPLOAD_FILES = 10
_MAX_BROWSER_DOWNLOAD_TIMEOUT_MS = 120_000


def _browser_config(runtime_metadata: dict[str, Any]) -> dict[str, Any]:
    browser_cfg = runtime_metadata.get("browser")
    if isinstance(browser_cfg, dict):
        return dict(browser_cfg)
    return {
        "enabled": runtime_metadata.get("browser_enabled", True),
        "auto_install": runtime_metadata.get("browser_auto_install", False),
        "headed_allowed": runtime_metadata.get("browser_headed_allowed", False),
        "engine": runtime_metadata.get("browser_engine", "chromium"),
        "runtime": runtime_metadata.get("browser_runtime", "playwright"),
        "channel": runtime_metadata.get("browser_channel"),
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
        "stealth_enabled": runtime_metadata.get("browser_stealth_enabled"),
        "stealth_evasions": runtime_metadata.get("browser_stealth_evasions"),
        "realistic_user_agent": runtime_metadata.get("browser_realistic_user_agent", True),
        "default_timezone_id": runtime_metadata.get("browser_default_timezone_id", "UTC"),
        "default_accept_language": runtime_metadata.get(
            "browser_default_accept_language", "en-US,en;q=0.9"
        ),
        "auto_consent": runtime_metadata.get("browser_auto_consent"),
        "auto_consent_disabled_domains": runtime_metadata.get(
            "browser_auto_consent_disabled_domains"
        ),
        "auto_consent_delay_ms": runtime_metadata.get("browser_auto_consent_delay_ms", 800),
        "humanize_input": runtime_metadata.get("browser_humanize_input"),
        "humanize_intensity": runtime_metadata.get("browser_humanize_intensity", "low"),
        "fingerprint_hardening": runtime_metadata.get("browser_fingerprint_hardening"),
        "navigation_timeout_seconds": runtime_metadata.get(
            "browser_navigation_timeout_seconds", 60
        ),
        "wait_until": runtime_metadata.get("browser_wait_until", "domcontentloaded"),
        "network_idle_after_dom_seconds": runtime_metadata.get(
            "browser_network_idle_after_dom_seconds", 3
        ),
        "native_bootstrap_enabled": runtime_metadata.get("browser_native_bootstrap_enabled", True),
        "native_bootstrap_seconds": runtime_metadata.get("browser_native_bootstrap_seconds", 15),
    }


def build_manager_from_config(runtime_metadata: dict[str, Any]) -> BrowserManager:
    """Construct a BrowserManager from ``runtime_metadata['browser']``.

    This is the canonical factory shared by the remote executor runner and
    the in-process executor provider so both paths create managers with
    exactly the same configuration logic.
    """
    cfg = _browser_config(runtime_metadata)
    stealth_enabled_raw = cfg.get("stealth_enabled")
    stealth_enabled: bool | None = (
        None if stealth_enabled_raw is None else bool(stealth_enabled_raw)
    )
    humanize_input_raw = cfg.get("humanize_input")
    humanize_input: bool | None = None if humanize_input_raw is None else bool(humanize_input_raw)
    fingerprint_hardening_raw = cfg.get("fingerprint_hardening")
    fingerprint_hardening: bool | None = (
        None if fingerprint_hardening_raw is None else bool(fingerprint_hardening_raw)
    )
    auto_consent_disabled_domains_raw = cfg.get("auto_consent_disabled_domains")
    if isinstance(auto_consent_disabled_domains_raw, str):
        auto_consent_disabled_domains = [
            entry.strip() for entry in auto_consent_disabled_domains_raw.split(",") if entry.strip()
        ]
    elif isinstance(auto_consent_disabled_domains_raw, list):
        auto_consent_disabled_domains = [
            str(entry).strip() for entry in auto_consent_disabled_domains_raw if str(entry).strip()
        ]
    else:
        auto_consent_disabled_domains = []
    auto_consent_value = cfg.get("auto_consent")
    auto_consent: str | None = None if auto_consent_value is None else str(auto_consent_value)
    return BrowserManager(
        enabled=bool(cfg.get("enabled", True)),
        auto_install=bool(cfg.get("auto_install", False)),
        engine=str(cfg.get("engine", "chromium")),
        runtime=str(cfg.get("runtime") or "playwright"),
        channel=(str(cfg.get("channel")) if cfg.get("channel") else None),
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
        stealth_enabled=stealth_enabled,
        stealth_evasions=_coerce_evasions(cfg.get("stealth_evasions")),
        realistic_user_agent=bool(cfg.get("realistic_user_agent", True)),
        default_timezone_id=(
            str(cfg.get("default_timezone_id")) if cfg.get("default_timezone_id") else None
        ),
        default_accept_language=str(cfg.get("default_accept_language") or "en-US,en;q=0.9"),
        auto_consent=auto_consent,
        auto_consent_disabled_domains=auto_consent_disabled_domains,
        auto_consent_delay_ms=int(cfg.get("auto_consent_delay_ms") or 800),
        humanize_input=humanize_input,
        humanize_intensity=str(cfg.get("humanize_intensity") or "low"),
        fingerprint_hardening=fingerprint_hardening,
        navigation_timeout_seconds=int(cfg.get("navigation_timeout_seconds") or 60),
        wait_until=str(cfg.get("wait_until") or "domcontentloaded"),
        network_idle_after_dom_seconds=int(cfg.get("network_idle_after_dom_seconds") or 3),
        native_bootstrap_enabled=bool(cfg.get("native_bootstrap_enabled", True)),
        native_bootstrap_seconds=int(cfg.get("native_bootstrap_seconds") or 15),
    )


def _coerce_evasions(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return None


def _get_manager(context: ToolExecutionContext) -> BrowserManager:
    """Return the BrowserManager for this executor context.

    Look-up order (most to least persistent):
    1. ``shared_runtime_metadata`` — the long-lived per-executor dict that
       the runner/in-process provider populates at configure time.
    2. ``runtime_metadata`` — the per-call shallow copy; written here if the
       manager was found in shared so subsequent helpers see it without having
       to traverse the chain again.

    Lazy creation (build_manager_from_config) is intentionally removed: the
    manager must now be pre-populated by the executor lifecycle (Stage A fix).
    If it is missing, the executor was not configured for browser access and
    the caller should surface an actionable error.
    """
    shared = context.shared_runtime_metadata or {}
    per_call = context.runtime_metadata

    manager = per_call.get(BROWSER_MANAGER_KEY)
    if isinstance(manager, BrowserManager):
        return manager

    manager = shared.get(BROWSER_MANAGER_KEY)
    if isinstance(manager, BrowserManager):
        # Mirror into the per-call dict so callers downstream in this
        # call (e.g. web_fetch) find it without extra traversal.
        per_call[BROWSER_MANAGER_KEY] = manager
        return manager

    raise RuntimeError(
        "Browser manager is not available on this executor. "
        "Ensure browser tools are enabled and the executor is configured with "
        "browser access."
    )


def _owner_from_context(context: ToolExecutionContext) -> BrowserSessionOwner:
    metadata = context.runtime_metadata or {}
    runtime_access = metadata.get("runtime_access")
    ownership = runtime_access if isinstance(runtime_access, dict) else metadata
    scope_id = str(context.execution_scope_id or "").strip()
    if not scope_id:
        raise BrowserLifecycleError(
            "browser_unauthenticated",
            "Browser lifecycle ownership is unavailable for this execution.",
        )
    return BrowserSessionOwner(
        execution_scope_id=scope_id,
        session_id=str(ownership.get("session_id") or "") or None,
        conversation_id=str(ownership.get("conversation_id") or "") or None,
        user_email=str(ownership.get("user_email") or "") or None,
        agent_id=str(ownership.get("agent_id") or "") or None,
        parent_session_id=str(ownership.get("parent_session_id") or "") or None,
        delegation_mode=str(ownership.get("delegation_mode") or "") or None,
    )


async def _get_owned_session(
    manager: BrowserManager, context: ToolExecutionContext, session_id: str
) -> BrowserSession:
    return await manager.get_live_session(session_id, owner=_owner_from_context(context))


def _lifecycle_error_result(exc: BrowserLifecycleError) -> ToolResult:
    return ToolResult(
        output=str(exc),
        is_error=True,
        metadata={"browser_lifecycle_error": exc.code},
    )


def _site_attribution_url(*candidates: str) -> str:
    for candidate in candidates:
        parsed = urlparse(candidate)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.hostname != "patchright-init-script-inject.internal"
        ):
            return candidate
    return candidates[-1] if candidates else ""


def _sanitize_navigation_error(exc: Exception) -> str:
    message = re.sub(
        r"https?://patchright-init-script-inject\.internal(?:/[^\s]*)?",
        "[browser init script]",
        str(exc),
        flags=re.IGNORECASE,
    )
    return message[:1000]


def _selected_executor_owner(context: ToolExecutionContext) -> str | None:
    value = context.runtime_metadata.get("selected_executor_owner_email")
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = context.executor_handle.metadata.get("owner_email")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_like_navigation_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "page.goto",
            "navigation",
            "net::err_",
            "patchright-init-script-inject.internal",
        )
    )


async def _browser_block_diagnostic(
    session: BrowserSession | Any,
    *,
    requested_url: str,
    title: str,
    headless: bool,
) -> dict[str, Any] | None:
    status = getattr(session, "navigation_status", None)
    page = session.page
    body = ""
    content = getattr(page, "content", None)
    if callable(content):
        try:
            body = str(await content())[:200_000]
        except Exception:
            body = ""
    evidence = f"{title}\n{body}".lower()
    strong_waf_markers = (
        "attack id",
        "web application firewall",
        " waf ",
        "imperva",
        "incapsula",
    )
    rejected_with_vendor_reference = "request rejected" in evidence and any(
        marker in evidence for marker in ("attack id", "incident id", "reference id")
    )
    access_denied_with_reference = any(
        marker in evidence for marker in ("access denied", "zugriff verweigert")
    ) and any(marker in evidence for marker in ("error reference", "reference id"))
    failed_status = isinstance(status, int) and status >= 400
    broad_waf_match = failed_status and (
        any(marker in evidence for marker in strong_waf_markers) or rejected_with_vendor_reference
    )
    if not (broad_waf_match or access_denied_with_reference):
        return None
    final_url = _site_attribution_url(
        str(getattr(page, "url", "") or ""),
        str(getattr(session, "navigation_url", "") or ""),
        requested_url,
    )
    diagnostic: dict[str, Any] = {
        "category": "vendor_waf_block",
        "requested_url": requested_url,
        "final_url": final_url,
        "http_status": status,
        "headless": headless,
        "attribution": "requested_site",
    }
    if headless:
        diagnostic.update(
            {
                "headed_retry_recommended": True,
                "hint": (
                    "The requested site appears to reject this headless browser. Close this "
                    "session, then retry browser_open with headless=false on an executor "
                    "where headed mode is allowed."
                ),
            }
        )
    return diagnostic


def _validate_css_selector(tool_name: str, selector: str) -> None:
    normalized = selector.strip().lower()
    if normalized.startswith(_PLAYWRIGHT_LOCATOR_PREFIXES):
        raise ValueError(
            f"{tool_name} only supports CSS selectors; use browser_get_text for text-based detection."
        )
    for prefix in _PLAYWRIGHT_LOCATOR_PREFIXES:
        if f",{prefix}" in normalized or f", {prefix}" in normalized:
            raise ValueError(
                f"{tool_name} only supports CSS selectors; use browser_get_text for text-based detection."
            )


async def _discover_candidates(
    session: Any,
    *,
    mode: str,
    selector: str | None = None,
    max_results: int = 80,
    assign_attr: str | None = None,
    include_computed: bool = False,
) -> list[dict[str, Any]]:
    payload = {
        "mode": mode,
        "selector": selector,
        "maxResults": max_results,
        "assignAttr": assign_attr,
        "includeComputed": include_computed,
    }
    candidates: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(_session_frames(session)):
        try:
            result = await frame.evaluate(_CANDIDATE_DISCOVERY_SCRIPT, payload)
        except Exception:
            # Cross-origin frames are still scriptable through Playwright's
            # frame context, but detached/security-restricted frames can race.
            continue
        if not isinstance(result, list):
            continue
        for item in result:
            if not isinstance(item, dict):
                continue
            global_ref = f"e{len(candidates) + 1}"
            frame_item = dict(item)
            frame_item["ref"] = global_ref
            frame_item["frame_index"] = frame_index
            frame_item.setdefault("frame_url", _frame_url(frame))
            frame_item.setdefault("frame_name", _frame_name(frame))
            candidates.append(frame_item)
            if len(candidates) >= max_results:
                return candidates
    return candidates


def _session_frames(session: Any) -> list[Any]:
    frames = getattr(session.page, "frames", None)
    if callable(frames):
        frames = frames()
    if isinstance(frames, list) and frames:
        return frames
    return [session.page]


def _frame_at(session: Any, frame_index: int | None) -> Any:
    frames = _session_frames(session)
    if frame_index is None:
        return session.page
    if frame_index < 0 or frame_index >= len(frames):
        raise ValueError(
            "Browser ref points to a frame that is no longer available; refresh browser_snapshot and retry"
        )
    return frames[frame_index]


def _frame_url(frame: Any) -> str:
    return str(getattr(frame, "url", "") or "")


def _frame_name(frame: Any) -> str:
    name = getattr(frame, "name", "")
    if callable(name):
        try:
            name = name()
        except Exception:
            name = ""
    return str(name or "")


def _coerce_frame_index(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        "frame_index": candidate.get("frame_index"),
        "frame_url": candidate.get("frame_url"),
        "frame_name": candidate.get("frame_name"),
        "in_shadow_dom": candidate.get("in_shadow_dom"),
    }


def _safe_filename(value: Any, fallback: str) -> str:
    filename = Path(str(value or "")).name.strip()
    return filename or fallback


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value]
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _coerce_int_list(value: Any) -> list[int]:
    if isinstance(value, int):
        return [value]
    if not isinstance(value, list):
        return []
    items: list[int] = []
    for item in value:
        try:
            items.append(int(item))
        except (TypeError, ValueError):
            continue
    return items


async def _resolve_ref_target(
    session: Any,
    *,
    ref: str,
    require_editable: bool,
) -> tuple[Any, dict[str, Any]]:
    target = session.ref_map.get(ref)
    if isinstance(target, dict):
        selector = target.get("selector")
        frame_index = _coerce_frame_index(target.get("frame_index"))
    else:
        selector = target
        frame_index = None
    if not selector:
        raise ValueError(f"Unknown browser ref: {ref}")
    frame = _frame_at(session, frame_index)
    locator = frame.locator(selector)
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
            "frame_index": frame_index,
            "frame_url": _frame_url(frame) or str(info.get("frame_url") or ""),
            "frame_name": _frame_name(frame) or str(info.get("frame_name") or ""),
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
    frame = _frame_at(session, _coerce_frame_index(target.get("frame_index")))
    locator = frame.locator(exact_selector)
    if await locator.count() != 1:
        raise ValueError("Resolved selector candidate became stale; inspect the page again")
    return locator.nth(0), target


async def _resolve_selector_locator_any_frame(
    session: Any,
    *,
    selector: str,
) -> tuple[Any, dict[str, Any]]:
    _validate_css_selector("browser_upload", selector)
    matches: list[tuple[Any, dict[str, Any]]] = []
    for frame_index, frame in enumerate(_session_frames(session)):
        locator = frame.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            matches.append(
                (
                    locator.nth(index),
                    {
                        "ref": None,
                        "exact_selector": selector,
                        "frame_index": frame_index,
                        "frame_url": _frame_url(frame),
                        "frame_name": _frame_name(frame),
                    },
                )
            )
    if not matches:
        raise ValueError(
            "No element matched selector. Use browser_query or browser_snapshot for inspection."
        )
    if len(matches) > 1:
        preview = [_candidate_summary(info) for _, info in matches[:5]]
        raise ValueError(
            f"Selector matched multiple candidates; use browser_query or browser_snapshot and pick a ref. Candidates: {json.dumps(preview)}"
        )
    return matches[0]


async def _resolve_upload_target(
    session: Any,
    *,
    ref: Any,
    selector: Any,
    mode: str,
) -> tuple[Any, dict[str, Any], str]:
    if isinstance(ref, str) and ref:
        if mode == "input":
            target = session.ref_map.get(ref)
            if isinstance(target, dict):
                selector_value = target.get("selector")
                frame_index = _coerce_frame_index(target.get("frame_index"))
            else:
                selector_value = target
                frame_index = None
            if not selector_value:
                raise ValueError(f"Unknown browser ref: {ref}")
            frame = _frame_at(session, frame_index)
            locator = frame.locator(str(selector_value))
            if await locator.count() != 1:
                raise ValueError(
                    f"Browser ref {ref} is stale or ambiguous; refresh browser_snapshot and retry"
                )
            info = dict(getattr(session, "ref_metadata", {}).get(ref, {}))
            info.update(
                {
                    "ref": ref,
                    "exact_selector": str(selector_value),
                    "frame_index": frame_index,
                    "frame_url": _frame_url(frame),
                    "frame_name": _frame_name(frame),
                }
            )
            return locator.nth(0), info, "ref"
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        return chosen, info, "ref"
    if isinstance(selector, str) and selector:
        if mode == "input":
            chosen, info = await _resolve_selector_locator_any_frame(session, selector=selector)
        else:
            chosen, info = await _resolve_selector_target(
                session, selector=selector, mode="clickable"
            )
        return chosen, info, "selector"
    raise ValueError("Provide either ref or selector")


def _upload_file_payloads(arguments: dict[str, Any], context: ToolExecutionContext) -> list[Any]:
    files: list[Any] = []
    total_bytes = 0
    for raw_path in _coerce_string_list(arguments.get("file_paths")):
        path = resolve_path(raw_path, context=context)
        if not path.is_file():
            raise ValueError(f"Upload file does not exist or is not a file: {path}")
        total_bytes += path.stat().st_size
        files.append(str(path))
    source_artifacts = arguments.get("source_artifacts")
    if isinstance(source_artifacts, list):
        for index, item in enumerate(source_artifacts, start=1):
            if not isinstance(item, dict):
                continue
            content_b64 = item.get("content_b64")
            if not isinstance(content_b64, str) or not content_b64.strip():
                continue
            try:
                content = base64.b64decode(content_b64, validate=True)
            except Exception as exc:
                raise ValueError("browser_upload received invalid artifact content.") from exc
            filename = _safe_filename(item.get("filename"), f"artifact-{index}")
            mime_type = str(item.get("mime_type") or "") or mimetypes.guess_type(filename)[0]
            total_bytes += len(content)
            files.append(
                {
                    "name": filename,
                    "mimeType": mime_type or "application/octet-stream",
                    "buffer": content,
                }
            )
    if not files:
        raise ValueError("browser_upload requires file_paths or source_artifact_ids")
    if len(files) > _MAX_BROWSER_UPLOAD_FILES:
        raise ValueError(
            f"browser_upload supports at most {_MAX_BROWSER_UPLOAD_FILES} files per call"
        )
    if total_bytes > _MAX_BROWSER_UPLOAD_BYTES:
        raise ValueError(
            "browser_upload payload is too large: "
            f"{total_bytes} bytes exceeds {_MAX_BROWSER_UPLOAD_BYTES} bytes"
        )
    return files


def _store_ref_maps(session: Any, elements: list[dict[str, Any]]) -> None:
    session.ref_map = {
        str(item.get("ref")): {
            "selector": str(item.get("exact_selector")),
            "frame_index": item.get("frame_index"),
        }
        for item in elements
        if isinstance(item, dict) and item.get("ref") and item.get("exact_selector")
    }
    session.ref_metadata = {
        str(item.get("ref")): dict(item)
        for item in elements
        if isinstance(item, dict) and item.get("ref")
    }


async def _wait_after(page: Any, arguments: dict[str, Any]) -> None:
    wait_after_ms = int(arguments.get("wait_after_ms", 0) or 0)
    if wait_after_ms <= 0:
        return
    if hasattr(page, "wait_for_timeout"):
        await page.wait_for_timeout(wait_after_ms)
    else:
        await asyncio.sleep(wait_after_ms / 1000.0)


async def _type_focused_text(page: Any, text: str, *, delay_ms: int, intensity: str) -> None:
    if intensity == "off":
        if delay_ms > 0:
            try:
                await page.keyboard.type(text, delay=delay_ms)
            except TypeError:
                await page.keyboard.type(text)
        else:
            await page.keyboard.type(text)
        return
    profile = humanizer.PROFILES[humanizer.normalize_intensity(intensity)]
    for char in text:
        await page.keyboard.type(char)
        if profile.inter_key_ms > 0:
            await asyncio.sleep(profile.inter_key_ms / 1000.0)


def _require_manager(context: ToolExecutionContext) -> BrowserManager | ToolResult:
    """Return the BrowserManager or a clear ToolResult error if unavailable."""
    try:
        return _get_manager(context)
    except RuntimeError as exc:
        return ToolResult(output=str(exc), is_error=True, metadata={"browser_unavailable": True})


async def handle_browser_open(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    result_or_manager = _require_manager(context)
    if isinstance(result_or_manager, ToolResult):
        return result_or_manager
    manager = result_or_manager
    browser_settings = _parse_browser_settings(arguments)
    requested_url = str(arguments.get("url", ""))
    headless = bool(arguments.get("headless", True))
    try:
        session = await manager.open_session(
            session_id=str(arguments.get("session_id", "")),
            url=requested_url,
            headless=headless,
            auth_state=(
                arguments.get("auth_state")
                if isinstance(arguments.get("auth_state"), dict)
                else None
            ),
            profile_mode=str(arguments.get("profile_mode", "default") or "default"),
            profile_id=(str(arguments.get("profile_id")) if arguments.get("profile_id") else None),
            browser_settings=browser_settings,
            owner=_owner_from_context(context),
        )
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    except Exception as exc:
        message = _sanitize_navigation_error(exc)
        if not _looks_like_navigation_failure(exc):
            return ToolResult(
                output=f"Browser open failed before site navigation: {message}",
                is_error=True,
                metadata={
                    "browser_runtime_error": True,
                    "attribution": "executor_runtime",
                    "headless": headless,
                },
            )
        final_url = _site_attribution_url(requested_url)
        return ToolResult(
            output=f"Browser navigation to {final_url} failed: {message}",
            is_error=True,
            metadata={
                "browser_navigation_error": True,
                "requested_url": requested_url,
                "final_url": final_url,
                "attribution": "requested_site",
                "headless": headless,
            },
        )
    session_settings = getattr(session, "browser_settings", None)
    resolved_settings = session_settings.as_dict() if session_settings is not None else None
    title = await session.page.title()
    diagnostic = await _browser_block_diagnostic(
        session,
        requested_url=requested_url,
        title=title,
        headless=headless,
    )
    navigation_url = _site_attribution_url(
        str(getattr(session, "navigation_url", "") or ""),
        str(getattr(session.page, "url", "") or ""),
        requested_url,
    )
    payload = {
        "session_id": session.session_id,
        "url": session.page.url,
        "title": title,
        "headless": headless,
        "navigation_url": navigation_url,
        "navigation_status": getattr(session, "navigation_status", None),
        "navigation_ok": getattr(session, "navigation_ok", None),
        "profile_mode": session.profile_mode,
        "profile_id": session.profile_id,
        "browser_settings": resolved_settings,
        "diagnostic": diagnostic,
    }
    return ToolResult(
        output=json.dumps(payload),
        is_error=diagnostic is not None,
        metadata=(
            {
                "browser_blocked": True,
                **diagnostic,
            }
            if diagnostic is not None
            else {}
        ),
    )


async def handle_browser_list_sessions(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    del arguments
    manager = _get_manager(context)
    try:
        sessions = await manager.list_sessions(owner=_owner_from_context(context))
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    return ToolResult(output=json.dumps({"sessions": sessions}))


async def handle_browser_inspect_session(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    try:
        session = await manager.inspect_session(
            str(arguments.get("session_id", "")),
            owner=_owner_from_context(context),
        )
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    return ToolResult(output=json.dumps({"session": session}))


async def handle_browser_list_profiles(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    try:
        profiles = await manager.list_profiles(
            owner=_owner_from_context(context),
            reclaim_stale=bool(arguments.get("reclaim_stale", False)),
            include_unclaimed=bool(arguments.get("include_unclaimed", False)),
            executor_owner_email=_selected_executor_owner(context),
        )
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    return ToolResult(output=json.dumps({"profiles": profiles}))


async def handle_browser_claim_profile(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    try:
        profile = await manager.claim_legacy_profile(
            str(arguments.get("profile_id", "")),
            owner=_owner_from_context(context),
            confirm_profile_id=str(arguments.get("confirm_profile_id", "")),
            reclaim_stale=bool(arguments.get("reclaim_stale", False)),
            executor_owner_email=_selected_executor_owner(context),
        )
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    return ToolResult(
        output=json.dumps({"profile": profile}),
        metadata={"browser_profile_claimed": bool(profile["claimed"])},
    )


async def handle_browser_query(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    _store_ref_maps(session, candidates)
    return ToolResult(output=json.dumps({"matches": candidates}, ensure_ascii=False))


async def handle_browser_eval(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
        owner=_owner_from_context(context),
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
        owner=_owner_from_context(context),
    )
    return ToolResult(output=json.dumps({"events": events}, ensure_ascii=False))


async def handle_browser_snapshot(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    max_elements = max(1, min(int(arguments.get("max_elements", 40) or 40), 80))
    elements = await _discover_candidates(
        session,
        mode="actionable",
        max_results=max_elements,
        assign_attr="data-cognis-ref",
        include_computed=False,
    )
    _store_ref_maps(session, elements)
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
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    max_chars = int(arguments.get("max_chars", 4000) or 4000)
    text = await session.page.evaluate("() => (document.body?.innerText || '').trim()")
    return ToolResult(output=str(text)[:max_chars])


async def handle_browser_get_focus(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    active: dict[str, Any] | None = None
    active_frame_index: int | None = None
    active_frame: Any = session.page
    for frame_index, frame in enumerate(_session_frames(session)):
        try:
            result = await frame.evaluate(_FOCUS_DISCOVERY_SCRIPT)
        except Exception:
            continue
        if isinstance(result, dict):
            active = result
            active_frame_index = frame_index
            active_frame = frame
            break
    return ToolResult(
        output=json.dumps(
            {
                "frame": {
                    "frame_index": active_frame_index,
                    "frame_url": _frame_url(active_frame),
                    "frame_name": _frame_name(active_frame),
                },
                "element": active,
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_click(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    intensity = _resolve_intensity(arguments, manager, session)
    await humanizer.humanize_click(session.page, chosen, intensity=intensity)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "click",
                "source": source,
                "intensity": intensity,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_fill(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    intensity = _resolve_intensity(arguments, manager, session)
    await humanizer.humanize_fill(session.page, chosen, value, intensity=intensity)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "fill",
                "source": source,
                "intensity": intensity,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_focus(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    await _wait_after(session.page, arguments)
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
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    text = arguments.get("text")
    if not isinstance(text, str) and isinstance(arguments.get("value"), str):
        text = arguments.get("value")
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
    intensity = _resolve_intensity(arguments, manager, session)
    delay = int(arguments.get("delay_ms", 0) or 0)
    if intensity == "off":
        # Preserve the legacy behaviour exactly when humanization is off so
        # existing callers using ``delay_ms`` keep working.
        await chosen.focus()
        if delay > 0:
            await chosen.press_sequentially(text, delay=delay)
        else:
            await chosen.press_sequentially(text)
    else:
        await humanizer.humanize_type(session.page, chosen, text, intensity=intensity)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "type",
                "source": source,
                "intensity": intensity,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_submit_form(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    await _wait_after(session.page, arguments)
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


async def handle_browser_select(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        source = "ref"
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="fillable")
        source = "selector"
    else:
        raise ValueError("Provide either ref or selector")

    options: list[dict[str, Any]] = []
    options.extend({"value": item} for item in _coerce_string_list(arguments.get("values")))
    options.extend({"label": item} for item in _coerce_string_list(arguments.get("labels")))
    options.extend({"index": item} for item in _coerce_int_list(arguments.get("indexes")))
    if not options:
        raise ValueError("browser_select requires values, labels, or indexes")
    selected = await chosen.select_option(options)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "select",
                "source": source,
                "selected_count": len(selected) if isinstance(selected, list) else None,
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_upload(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    mode = str(arguments.get("mode", "input") or "input").lower()
    if mode not in {"input", "file_chooser"}:
        raise ValueError("browser_upload mode must be 'input' or 'file_chooser'")
    files = _upload_file_payloads(arguments, context)
    chosen, info, source = await _resolve_upload_target(
        session,
        ref=arguments.get("ref"),
        selector=arguments.get("selector"),
        mode=mode,
    )
    if mode == "input":
        await chosen.set_input_files(files)
    else:
        async with session.page.expect_file_chooser() as chooser_info:
            await humanizer.humanize_click(
                session.page, chosen, intensity=_resolve_intensity(arguments, manager, session)
            )
        chooser = await chooser_info.value
        await chooser.set_files(files)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "upload",
                "source": source,
                "mode": mode,
                "file_count": len(files),
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_download_wait(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    timeout_ms = max(
        1,
        min(int(arguments.get("timeout_ms", 30000) or 30000), _MAX_BROWSER_DOWNLOAD_TIMEOUT_MS),
    )
    async with session.page.expect_download(timeout=timeout_ms) as download_info:
        await humanizer.humanize_click(
            session.page, chosen, intensity=_resolve_intensity(arguments, manager, session)
        )
    download = await download_info.value
    filename = _safe_filename(getattr(download, "suggested_filename", None), "download.bin")
    with tempfile.TemporaryDirectory(prefix="cognis-browser-download-") as temp_dir:
        destination = Path(temp_dir) / filename
        await download.save_as(str(destination))
        size_bytes = destination.stat().st_size
        if size_bytes > _MAX_BROWSER_DOWNLOAD_BYTES:
            raise ValueError(
                "Downloaded file is too large to return as an attachment: "
                f"{size_bytes} bytes exceeds {_MAX_BROWSER_DOWNLOAD_BYTES} bytes"
            )
        content = destination.read_bytes()
    await _wait_after(session.page, arguments)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return ToolResult(
        output=json.dumps(
            {
                "action": "download_wait",
                "source": source,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(content),
                "target": _candidate_summary(info),
            },
            ensure_ascii=False,
        ),
        attachments=[
            {
                "kind": "file",
                "mime_type": mime_type,
                "filename": filename,
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )


async def handle_browser_scroll(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    delta_x = int(arguments.get("delta_x", 0) or 0)
    delta_y = int(arguments.get("delta_y", 600) or 600)
    ref = arguments.get("ref")
    selector = arguments.get("selector")
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        await chosen.evaluate(
            "(el, delta) => el.scrollBy(delta.x, delta.y)", {"x": delta_x, "y": delta_y}
        )
        source = "ref"
        target = _candidate_summary(info)
    elif isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="actionable")
        await chosen.evaluate(
            "(el, delta) => el.scrollBy(delta.x, delta.y)", {"x": delta_x, "y": delta_y}
        )
        source = "selector"
        target = _candidate_summary(info)
    else:
        await session.page.evaluate(
            "(delta) => window.scrollBy(delta.x, delta.y)", {"x": delta_x, "y": delta_y}
        )
        source = "page"
        target = None
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "scroll",
                "source": source,
                "delta_x": delta_x,
                "delta_y": delta_y,
                "target": target,
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_hover(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    await chosen.hover()
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {"action": "hover", "source": source, "target": _candidate_summary(info)},
            ensure_ascii=False,
        )
    )


async def _resolve_drag_endpoint(
    session: Any, ref: Any, selector: Any, name: str
) -> tuple[Any, dict[str, Any], str]:
    if isinstance(ref, str) and ref:
        chosen, info = await _resolve_ref_target(session, ref=ref, require_editable=False)
        return chosen, info, "ref"
    if isinstance(selector, str) and selector:
        chosen, info = await _resolve_selector_target(session, selector=selector, mode="actionable")
        return chosen, info, "selector"
    raise ValueError(f"Provide either {name}_ref or {name}_selector")


async def handle_browser_drag_drop(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    source_locator, source_info, source_kind = await _resolve_drag_endpoint(
        session,
        arguments.get("source_ref"),
        arguments.get("source_selector"),
        "source",
    )
    target_locator, target_info, target_kind = await _resolve_drag_endpoint(
        session,
        arguments.get("target_ref"),
        arguments.get("target_selector"),
        "target",
    )
    await source_locator.drag_to(target_locator)
    await _wait_after(session.page, arguments)
    return ToolResult(
        output=json.dumps(
            {
                "action": "drag_drop",
                "source": source_kind,
                "target_source": target_kind,
                "source_target": _candidate_summary(source_info),
                "drop_target": _candidate_summary(target_info),
            },
            ensure_ascii=False,
        )
    )


async def handle_browser_press(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    text = arguments.get("text")
    if not isinstance(text, str) and isinstance(arguments.get("value"), str):
        text = arguments.get("value")
    key = arguments.get("key")
    if isinstance(text, str):
        intensity = _resolve_intensity(arguments, manager, session)
        delay = int(arguments.get("delay_ms", 0) or 0)
        await _type_focused_text(session.page, text, delay_ms=delay, intensity=intensity)
        await _wait_after(session.page, arguments)
        return ToolResult(
            output=json.dumps({"action": "type", "source": "focused", "intensity": intensity})
        )
    if not isinstance(key, str) or not key:
        raise ValueError("browser_press requires key or text")
    await session.page.keyboard.press(key)
    await _wait_after(session.page, arguments)
    return ToolResult(output=json.dumps({"action": "press"}))


async def handle_browser_wait_for(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
    selector = arguments.get("selector")
    timeout_ms = int(arguments.get("timeout_ms", 10000) or 10000)
    state = str(arguments.get("state") or "visible")
    if state not in {"attached", "visible", "hidden", "detached"}:
        raise ValueError(
            "browser_wait_for state must be one of attached, visible, hidden, detached"
        )
    if isinstance(selector, str) and selector:
        _validate_css_selector("browser_wait_for", selector)
        last_error: Exception | None = None
        tasks = [
            asyncio.create_task(_frame_wait_for_selector(frame, selector, timeout_ms, state))
            for frame in _session_frames(session)
        ]
        for task in asyncio.as_completed(tasks):
            try:
                await task
                break
            except Exception as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        await session.page.wait_for_timeout(timeout_ms)
    return ToolResult(output="Wait completed.")


async def _frame_wait_for_selector(frame: Any, selector: str, timeout_ms: int, state: str) -> None:
    try:
        await frame.wait_for_selector(selector, timeout=timeout_ms, state=state)
    except TypeError:
        await frame.wait_for_selector(selector, timeout=timeout_ms)


async def handle_browser_screenshot(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session = await _get_owned_session(manager, context, str(arguments.get("session_id", "")))
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
    try:
        closed = await manager.close_session(
            str(arguments.get("session_id", "")),
            owner=_owner_from_context(context),
            allow_managed_descendant=bool(arguments.get("release_managed_descendant", False)),
        )
    except BrowserLifecycleError as exc:
        return _lifecycle_error_result(exc)
    return ToolResult(
        output=(
            "Closed browser session."
            if closed
            else "Browser session was already closed or does not exist."
        ),
        metadata={"closed": closed, "idempotent": not closed},
    )


async def handle_browser_save_auth_state(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    manager = _get_manager(context)
    session_id = str(arguments.get("session_id", ""))
    storage_state = await manager.storage_state(session_id, owner=_owner_from_context(context))
    session = await _get_owned_session(manager, context, session_id)
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
