/**
 * cognis-autoconsent — curated CMP (cookie consent) auto-dismisser.
 *
 * This script runs as a page init script on every navigation. It looks for
 * known cookie-consent dialogs (OneTrust, Cookiebot, Quantcast, Sourcepoint,
 * Didomi, TrustArc, Iubenda, Usercentrics, CookieYes, Borlabs, plus generic
 * heuristics) and clicks the configured action button.
 *
 * Configuration is supplied by the controller via two globals injected
 * BEFORE this script:
 *
 *   window.__cognis_autoconsent = {
 *     action: "accept" | "reject" | "off",
 *     delayMs: 800,
 *     disabledHosts: ["example.com", ...]
 *   };
 *
 * If `action === "off"` or the current hostname matches any entry in
 * `disabledHosts` (suffix match), the script no-ops.
 *
 * This is a self-contained, runtime-agnostic alternative to a full library
 * like @duckduckgo/autoconsent. It is deliberately small (no
 * controller/page message protocol) so it works identically in headless
 * Playwright, headed Patchright, and inside extensions if reused later.
 */
(() => {
  "use strict";

  const cfg = (window.__cognis_autoconsent || {});
  const action = (cfg.action || "off").toLowerCase();
  if (action !== "accept" && action !== "reject") return;

  const disabledHosts = Array.isArray(cfg.disabledHosts) ? cfg.disabledHosts : [];
  const host = (window.location && window.location.hostname || "").toLowerCase();
  for (const entry of disabledHosts) {
    if (typeof entry !== "string") continue;
    const norm = entry.trim().toLowerCase();
    if (!norm) continue;
    if (host === norm || host.endsWith("." + norm)) return;
  }

  const delayMs = Math.max(0, Math.min(15000, Number(cfg.delayMs) || 800));

  // Per-CMP rules. Order matters: most specific first, most generic last.
  // Each rule has:
  //   name:     CMP identifier (logged on success)
  //   detect:   selector that, if present + visible, indicates the CMP
  //   accept:   selectors to click for "accept all"
  //   reject:   selectors to click for "reject all" (deny non-essential)
  //   prep:     optional selector(s) to click first (e.g. "Customise")
  const RULES = [
    {
      name: "OneTrust",
      detect: "#onetrust-banner-sdk, #onetrust-consent-sdk",
      accept: ["#onetrust-accept-btn-handler", ".onetrust-close-btn-handler"],
      reject: [
        "#onetrust-reject-all-handler",
        ".ot-pc-refuse-all-handler",
      ],
      prep: ["#onetrust-pc-btn-handler"],
    },
    {
      name: "Cookiebot",
      detect: "#CybotCookiebotDialog",
      accept: ["#CybotCookiebotDialogBodyLevelButtonAcceptAll", "#CybotCookiebotDialogBodyButtonAccept"],
      reject: ["#CybotCookiebotDialogBodyButtonDecline", "#CybotCookiebotDialogBodyLevelButtonCustomizeClose"],
    },
    {
      name: "Quantcast",
      detect: ".qc-cmp2-summary-buttons, .qc-cmp2-container",
      accept: ['button[mode="primary"]'],
      reject: ['button[mode="secondary"]', 'button[aria-label*="Reject" i]'],
    },
    {
      name: "Sourcepoint",
      detect: '[class*="sp_message_container"], [id*="sp_message_container"]',
      accept: ['button[title="Accept"]', '.message-button.accept-all', 'button[aria-label*="Accept" i]'],
      reject: ['button[title*="Reject" i]', '.message-button[title*="Reject" i]'],
    },
    {
      name: "Didomi",
      detect: "#didomi-host, #didomi-notice",
      accept: ["#didomi-notice-agree-button"],
      reject: ["#didomi-notice-disagree-button", '[data-testid="continue-without-agreeing"]'],
    },
    {
      name: "TrustArc",
      detect: "#consent_blackbar, #truste-consent-track",
      accept: ["#truste-consent-button"],
      reject: ["#truste-consent-required"],
    },
    {
      name: "Iubenda",
      detect: "#iubenda-cs-banner",
      accept: [".iubenda-cs-accept-btn"],
      reject: [".iubenda-cs-reject-btn", ".iubenda-cs-customize-btn"],
    },
    {
      name: "Usercentrics",
      detect: "#usercentrics-root, #uc-main-banner",
      accept: ['[data-testid="uc-accept-all-button"]', 'button[data-action="acceptAll"]'],
      reject: ['[data-testid="uc-deny-all-button"]', 'button[data-action="denyAll"]'],
    },
    {
      name: "CookieYes",
      detect: ".cky-consent-container, .cky-modal",
      accept: [".cky-btn-accept"],
      reject: [".cky-btn-reject"],
    },
    {
      name: "Borlabs",
      detect: "#BorlabsCookieBox",
      accept: ['[data-cookie-accept-all]', '#CookiePrefSave'],
      reject: ['[data-cookie-refuse]', '#CookiePrefSave'],
    },
    {
      name: "Osano",
      detect: ".osano-cm-window",
      accept: [".osano-cm-accept-all"],
      reject: [".osano-cm-denyAll", ".osano-cm-deny"],
    },
    {
      name: "Klaro",
      detect: ".klaro .cookie-modal, .klaro .cookie-notice",
      accept: [".klaro .cm-btn-accept-all", ".klaro .cm-btn-accept"],
      reject: [".klaro .cm-btn-decline"],
    },
    {
      name: "Termly",
      detect: ".termly-styled-banner, [data-testid='banner-container']",
      accept: ["[data-tid='banner-accept']", "button[aria-label*='Accept All' i]"],
      reject: ["[data-tid='banner-decline']", "button[aria-label*='Decline All' i]"],
    },
    {
      name: "WordPress GDPR Cookie Compliance (Moove)",
      detect: "#moove_gdpr_cookie_info_bar",
      accept: ["#moove_gdpr_cookie_info_bar .mgbutton.moove-gdpr-infobar-allow-all"],
      reject: ["#moove_gdpr_cookie_info_bar .mgbutton.moove-gdpr-infobar-reject-btn"],
    },
    {
      name: "Complianz",
      detect: ".cmplz-cookiebanner, #cmplz-cookiebanner-container",
      accept: [".cmplz-accept", ".cmplz-btn.cmplz-accept-all"],
      reject: [".cmplz-deny", ".cmplz-btn.cmplz-deny"],
    },
    {
      name: "GDPR Cookie Consent (CookieLawInfo)",
      detect: "#cookie-law-info-bar",
      accept: ["#cookie_action_close_header", "#wt-cli-accept-all-btn"],
      reject: ["#wt-cli-reject-btn"],
    },
  ];

  function isVisible(el) {
    if (!el || !(el instanceof HTMLElement)) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || parseFloat(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function clickIfVisible(selector) {
    const nodes = document.querySelectorAll(selector);
    for (const node of nodes) {
      if (isVisible(node)) {
        try {
          node.click();
          return true;
        } catch {
          /* swallow click errors, try next */
        }
      }
    }
    return false;
  }

  function tryRule(rule) {
    const detector = document.querySelector(rule.detect);
    if (!detector || !isVisible(detector)) return false;
    const targets = (action === "accept" ? rule.accept : rule.reject) || [];
    if (!targets.length) return false;
    for (const sel of targets) {
      if (clickIfVisible(sel)) {
        try {
          // eslint-disable-next-line no-console
          console.debug("[cognis-autoconsent] " + rule.name + " " + action);
        } catch {
          /* console may be unavailable in some sandboxed contexts */
        }
        return true;
      }
    }
    return false;
  }

  // Heuristic fallback: scan visible <button>/<a> for common consent text.
  // Only used after every named rule failed and a banner-shaped container
  // is likely present (we look for elements whose computed position is
  // fixed/absolute and that contain "cookie" text).
  function heuristicFallback() {
    const banners = [];
    for (const el of document.querySelectorAll("div, section, aside, footer")) {
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      if (style.position !== "fixed" && style.position !== "sticky" && style.position !== "absolute") continue;
      const text = (el.innerText || "").toLowerCase();
      if (text.length < 8 || text.length > 4000) continue;
      if (text.includes("cookie") || text.includes("consent") || text.includes("gdpr") || text.includes("privacy")) {
        banners.push(el);
      }
    }
    if (!banners.length) return false;

    const acceptPatterns = [
      /^accept all$/i,
      /^accept$/i,
      /^agree$/i,
      /^i agree$/i,
      /^ok$/i,
      /^got it$/i,
      /^allow all$/i,
      /^allow$/i,
      /^continue$/i,
    ];
    const rejectPatterns = [
      /^reject all$/i,
      /^reject$/i,
      /^decline$/i,
      /^decline all$/i,
      /^deny$/i,
      /^deny all$/i,
      /only essential/i,
      /only necessary/i,
    ];
    const patterns = action === "accept" ? acceptPatterns : rejectPatterns;

    for (const banner of banners) {
      const buttons = banner.querySelectorAll("button, a, [role='button']");
      for (const btn of buttons) {
        if (!isVisible(btn)) continue;
        const label = (btn.innerText || btn.getAttribute("aria-label") || "").trim();
        if (!label) continue;
        for (const pat of patterns) {
          if (pat.test(label)) {
            try {
              btn.click();
              try { console.debug("[cognis-autoconsent] heuristic " + action + ": " + label); } catch {}
              return true;
            } catch {
              /* try next */
            }
          }
        }
      }
    }
    return false;
  }

  function attempt() {
    for (const rule of RULES) {
      if (tryRule(rule)) return true;
    }
    return heuristicFallback();
  }

  function start() {
    // Try once after the configured delay, then poll a few times to handle
    // banners injected late by JS (Sourcepoint, Quantcast, etc. often do).
    const deadline = Date.now() + 8000;
    let attempts = 0;
    const tick = () => {
      attempts += 1;
      if (attempt()) return;
      if (Date.now() < deadline && attempts < 20) {
        setTimeout(tick, 400);
      }
    };
    setTimeout(tick, delayMs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
